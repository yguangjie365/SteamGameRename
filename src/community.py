"""Community AppID -> Chinese name dictionary (JSON + optional sibling CSV)."""
from __future__ import annotations

import csv
import json
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
    """Read the community AppID -> zh-CN dictionary (SteamGameName format).

    A sibling CSV (community_names.csv) is merged on top, so users can drop
    any table exported from SteamCN/3DM/its own list without converting JSON.
    """
    source = Path(path)
    result: dict[int, str] = {}

    # SQLite 优先：社区词库是最可能膨胀的数据源（全量 Steam 可到 10 万条）。
    # CSV 覆盖层仍然生效，用户丢一个 community_names.csv 进来就能局部覆盖。
    db_file = dbmod.db_path(source.parent)
    if db_file.exists():
        conn = dbmod.connect(db_file)
        try:
            stored = dbmod.read_community(conn)
        finally:
            conn.close()
        if stored:
            result.update(stored)
            csv_path = source.with_suffix(".csv")
            if csv_path.exists():
                result.update(_read_community_csv(csv_path))
            return result

    if source.exists():
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


def update_community_names(
    dest: str | Path, url: str = COMMUNITY_NAMES_URL
) -> int:
    """Download the community dictionary, validate it, and write atomically."""
    try:
        response = httpx.get(
            url, timeout=30, follow_redirects=True,
            headers={"User-Agent": "steam-cn-renamer/0.2 (+local tool)"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ValueError(f"无法连接社区词库源（{type(exc).__name__}）：{url}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("社区词库源返回的不是合法 JSON") from exc
    version = payload.get("schemaVersion", payload.get("schema_version")) if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or version != 1:
        raise ValueError(f"社区词库格式不受支持（schema 版本 {version!r}）")
    games = payload.get("games")
    if not isinstance(games, dict) or not games:
        raise ValueError("社区词库为空或结构错误")
    atomic_write(
        dest,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    # 同步进 SQLite：load_community_names 优先读 db，只写文件会读到旧数据
    db_file = dbmod.db_path(Path(dest).parent)
    if db_file.exists():
        fresh = {
            int(key): value.strip()
            for key, value in games.items()
            if key.isdigit() and isinstance(value, str) and value.strip()
        }
        if fresh:
            conn = dbmod.connect(db_file)
            try:
                dbmod.write_community(conn, fresh)
            finally:
                conn.close()
    return sum(
        1 for key, value in games.items()
        if key.isdigit() and isinstance(value, str) and value.strip()
    )


