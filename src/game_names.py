"""Shared AppID -> Chinese name table: one CSV, two layers, git-friendly.

The bundled baseline ships with the program and is updated with new releases.
An optional local CSV in the config dir overrides individual GameIDs without
touching the baseline, so team maintenance stays mergeable.
"""
from __future__ import annotations

import csv
import datetime
import io
from dataclasses import dataclass, field
from pathlib import Path

import db as dbmod
from backup import atomic_write
from vdf import CJK

FIELDS = (
    "GameID", "OriginalName", "ModifiedName",
    "Source", "UpdatedAt", "Enabled", "Note",
)
CSV_NAME = "game_names.csv"
ORIGINAL_CSV_NAME = "original_names.csv"
# 本机运行时自动沉淀的英文名补充层，与随版本分发的只读基线分开存放
LEARNED_CSV_NAME = "learned_originals.csv"
_DISABLED = {"false", "0", "no", "off", "否", "禁用"}


def load_original_names(path: str | Path) -> dict[int, str]:
    """Read the immutable AppID -> original English name table.

    Unlike names.json (whose original_name is overwritten after a rename),
    this bundled CSV is never written by the program, so it always keeps the
    name Steam shipped with. Used for display only.
    """
    path = Path(path)
    out: dict[int, str] = {}
    if not path.exists():
        return out
    reader = csv.DictReader(io.StringIO(_decode(path)))
    for row in reader:
        try:
            appid = int((row.get("GameID") or "").strip())
        except (TypeError, ValueError):
            continue
        name = (row.get("OriginalName") or "").strip()
        if appid > 0 and name:
            out[appid] = name
    return out


def names_match(left: str, right: str) -> bool:
    """Compare names ignoring whitespace (rule R5 may have added a thin space)."""
    return "".join((left or "").split()) == "".join((right or "").split())


@dataclass
class SharedNames:
    names: dict[int, str] = field(default_factory=dict)
    originals: dict[int, str] = field(default_factory=dict)
    sources: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    duplicates: int = 0
    skipped: int = 0


def _decode(path: Path) -> str:
    """UTF-8 BOM, UTF-16, GBK all work; never silently return an empty table."""
    raw = path.read_bytes()
    encodings = ["utf-16"] if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else []
    encodings += ["utf-8-sig", "gbk", "utf-8"]
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _parse(text: str, label: str, out: SharedNames) -> None:
    reader = csv.DictReader(text.splitlines())
    headers = reader.fieldnames
    if not headers:
        out.warnings.append(f"{label}: 文件为空或缺少表头")
        return
    columns = {(name or "").strip().lower(): name for name in headers}
    required = ("gameid", "originalname", "modifiedname")
    missing = [name for name in required if name not in columns]
    if missing:
        out.warnings.append(
            f"{label}: 缺少必需列 {missing}（需要 GameID,OriginalName,ModifiedName）"
        )
        return

    def cell(row: dict, key: str) -> str:
        column = columns.get(key)
        return (row.get(column) or "").strip() if column else ""

    for line, row in enumerate(reader, start=2):
        gid = cell(row, "gameid")
        if not gid:
            continue
        if not gid.isdigit():
            out.warnings.append(f"{label}:{line} GameID {gid!r} 不是数字，已跳过")
            out.skipped += 1
            continue
        if cell(row, "enabled").lower() in _DISABLED:
            continue
        modified = cell(row, "modifiedname")
        if not modified:
            out.warnings.append(f"{label}:{line} ModifiedName 为空，已跳过")
            out.skipped += 1
            continue
        if not CJK.search(modified):
            # 工具的目标是中文名，但保留用户数据，只在页面/CLI 提示
            out.warnings.append(f"{label}:{line} ModifiedName 不含中文：{modified!r}")
        appid = int(gid)
        if appid in out.names:
            out.duplicates += 1
            out.warnings.append(
                f"{label}:{line} GameID {appid} 重复，后出现的条目覆盖前一条"
            )
        out.names[appid] = modified
        out.originals[appid] = cell(row, "originalname")
        out.sources[appid] = cell(row, "source")


def load_shared_names_from_files(
    baseline: str | Path, local: str | Path | None = None
) -> SharedNames:
    """Parse the CSV layers directly. Used by the SQLite migration itself."""
    out = SharedNames()
    for path, label in ((baseline, "共享基线"), (local, "本地覆盖")):
        if not path:
            continue
        source = Path(path)
        if not source.exists():
            continue
        try:
            text = _decode(source)
        except OSError as exc:
            out.warnings.append(f"{label}读取失败：{exc}")
            continue
        _parse(text, str(label), out)
    return out


def load_shared_names(baseline: str | Path, local: str | Path | None = None) -> SharedNames:
    """Load the shared dictionary, preferring the SQLite store when present.

    Falls back to parsing the CSVs (baseline + optional local override) when no
    database exists, so a source checkout and every test keeps working.
    """
    probe = Path(local) if local else Path(baseline)
    db_file = dbmod.db_path(probe.parent)
    if db_file.exists():
        conn = dbmod.connect(db_file)
        try:
            # CSV 改了（多人维护场景）必须立刻生效，不能等下次启动
            dbmod.sync_shared_from_files(conn, baseline, local)
            stored = dbmod.read_shared_names(conn)
            warnings = dbmod.read_shared_warnings(conn)
        finally:
            conn.close()
        if stored.names or stored.originals:
            stored.warnings = warnings
            return stored
    return load_shared_names_from_files(baseline, local)


def check_originals(
    shared: SharedNames,
    actual: dict[int, str],
    modified: dict[int, str] | None = None,
) -> list[str]:
    """Warn when a GameID looks mismatched against Steam's current name.

    A current name equal to the recorded ModifiedName is fine: that means the
    rename was already applied. Only a name matching neither the recorded
    original nor the target suggests a mistyped AppID.
    """
    warnings: list[str] = []
    for appid, recorded in shared.originals.items():
        current = (actual.get(appid) or "").strip()
        if not recorded or not current or names_match(recorded, current):
            continue
        target = (modified or {}).get(appid, "")
        if target and names_match(target, current):
            continue
        warnings.append(
            f"AppID {appid} 原名不符：词库记的是「{recorded}」，"
            f"Steam 当前是「{current}」，请核对后使用"
        )
    return warnings


def merge_original_names(
    baseline: dict[int, str], learned: dict[int, str]
) -> dict[int, str]:
    """Overlay locally learned names onto the bundled read-only baseline.

    Baseline wins on conflict: it was exported from a clean ledger and will
    never be rewritten by the tool, whereas a learned entry is a best-effort
    capture of whatever name the shard happened to hold at scan time.
    """
    merged = dict(learned)
    merged.update(baseline)
    return merged


def write_learned_originals(
    names: dict[int, str], dest: str | Path
) -> int:
    """Persist learned AppID -> English name rows atomically (UTF-8 BOM)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(("GameID", "OriginalName"))
    for appid in sorted(names):
        writer.writerow((appid, names[appid]))
    atomic_write(Path(dest), ("\ufeff" + buffer.getvalue()).encode("utf-8"))
    return len(names)


def write_shared_names_csv(
    entries: list[tuple[int, str, str, str]], dest: str | Path
) -> int:
    """Export (appid, original, modified, source) rows as UTF-8-BOM CSV."""
    today = datetime.date.today().isoformat()
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(FIELDS)
    for appid, original, modified, source in entries:
        writer.writerow((appid, original, modified, source, today, "true", ""))
    atomic_write(Path(dest), ("\ufeff" + buffer.getvalue()).encode("utf-8"))
    return len(entries)
