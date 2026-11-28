"""Community AppID -> Chinese name dictionary (JSON + optional sibling CSV)."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import httpx

import db as dbmod
from backup import atomic_write


def _parse_community_csv_text(text: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for row in csv.DictReader(text.splitlines()):
        normalized = {
            (key or "").strip().lower(): (value or "").strip()
            for key, value in row.items()
        }
        appid = normalized.get("appid") or normalized.get("app_id")
        name = normalized.get("name") or normalized.get("chinese") or normalized.get("中文名")
        if appid and appid.isdigit() and name:
            result[int(appid)] = name
    return result


def _read_community_csv(path: Path) -> dict[int, str]:
    """Read appid,name rows; tolerates UTF-8/UTF-8-BOM/GBK/UTF-16.

    Encodings are tried in order and the first one that actually yields rows
    wins, so a UTF-16 file is not mistaken for a "successful" garbage decode.
    A table is never dropped entirely: the last resort decodes with
    replacement instead of returning {}.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return {}

    encodings: list[str] = []
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings.append("utf-16")
    encodings += ["utf-8-sig", "gbk"]
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        rows = _parse_community_csv_text(text)
        if rows:
            return rows
    return _parse_community_csv_text(raw.decode("utf-8", "replace"))


def load_community_names(path: str | Path) -> dict[int, str]:
    """Read the community dictionary from SQLite, then apply optional CSV overrides."""
    source = Path(path)
    result: dict[int, str] = {}
    if not os.environ.get("STEAM_CN_RENAMER_NO_DB"):
        db_file = dbmod.db_path(source.parent)
        conn = dbmod.connect(db_file)
        if not dbmod.community_ready(conn):
            conn.close()
            dbmod.ensure_db(source.parent)
            conn = dbmod.connect(db_file)
        try:
            result.update(dbmod.read_community(conn))
        finally:
            conn.close()
    elif source.exists():
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        games = raw.get("games", {}) if isinstance(raw, dict) else {}
        for key, value in games.items():
            if key.isdigit() and isinstance(value, str) and value.strip():
                result[int(key)] = value.strip()

    csv_path = source.with_suffix(".csv")
    if csv_path.exists():
        result.update(_read_community_csv(csv_path))
    return result


COMMUNITY_NAMES_URL = (
    "https://raw.githubusercontent.com/mangataw/SteamGameName/main/"
    "data/translations.zh-CN.json"
)

# 国内直连 raw.githubusercontent.com 常年超时；直连失败后依次尝试公共加速
# 镜像。镜像站可用性随时间变化，可用环境变量覆盖（逗号分隔，{url} 占位；
# 不含占位符则视为完整 URL，如 jsDelivr 形式）。
_COMMUNITY_MIRROR_TEMPLATES = (
    "https://ghfast.top/{url}",
    "https://gh.llkk.cc/{url}",
    "https://ghproxy.net/{url}",
    "https://cdn.jsdelivr.net/gh/mangataw/SteamGameName@main/data/translations.zh-CN.json",
)


def _candidate_urls(url: str) -> list[str]:
    """直连优先，仅默认 GitHub 源启用镜像回退（显式自定义 URL 不回退）。"""
    if url != COMMUNITY_NAMES_URL:
        return [url]
    custom = os.environ.get("STEAM_CN_GH_MIRRORS", "").strip()
    templates = (
        tuple(t.strip() for t in custom.split(",") if t.strip())
        or _COMMUNITY_MIRROR_TEMPLATES
    )
    urls = [url]
    for template in templates:
        urls.append(template.format(url=url) if "{url}" in template else template)
    return urls


def update_community_names(
    dest: str | Path, url: str = COMMUNITY_NAMES_URL
) -> tuple[int, str]:
    """Download the community dictionary (GitHub direct, then CN mirrors).

    Returns (entry_count, source_url). 内容校验失败（非法 JSON / schema
    不符 / 空词库）与连接失败一样视为该源失败，换下一个候选继续。
    """
    errors: list[str] = []
    payload: dict | None = None
    fresh: dict[int, str] | None = None
    used = ""
    for candidate in _candidate_urls(url):
        try:
            response = httpx.get(
                candidate, timeout=15, follow_redirects=True,
                headers={"User-Agent": "steam-cn-renamer/0.2 (+local tool)"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"{candidate} → {type(exc).__name__}")
            continue
        version = (
            payload.get("schemaVersion", payload.get("schema_version"))
            if isinstance(payload, dict) else None
        )
        games = payload.get("games") if isinstance(payload, dict) else None
        if version != 1:
            errors.append(f"{candidate} → schema 版本 {version!r} 不受支持")
            continue
        if not isinstance(games, dict) or not games:
            errors.append(f"{candidate} → 词库为空或结构错误")
            continue
        fresh = {
            int(key): value.strip()
            for key, value in games.items()
            if key.isdigit() and isinstance(value, str) and value.strip()
        }
        if not fresh:
            errors.append(f"{candidate} → 没有可用的中文名")
            continue
        used = candidate
        break
    if fresh is None:
        raise ValueError(
            "无法连接社区词库源（直连与镜像均失败）：\n" + "\n".join(errors)
        )

    target = Path(dest)
    if os.environ.get("STEAM_CN_RENAMER_NO_DB"):
        atomic_write(
            target,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    else:
        dbmod.ensure_db(target.parent)
        conn = dbmod.connect(dbmod.db_path(target.parent))
        try:
            dbmod.write_community(conn, fresh)
        finally:
            conn.close()
    return len(fresh), used


