"""SQLite source of truth for the ledger and dictionaries.

`renamer.db` is authoritative at runtime. Legacy JSON files are imported once,
backed up under `migration-backups/`, and then ignored by normal reads. CSV
files remain explicit import/export or override layers.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_NAME = "localgamedb.db"
# 旧名 renamer.db 会在 ensure_db 时自动迁移（sqlite backup API 复制后
# 把旧文件移入 migration-backups/），台账等本地数据无损过渡。
SCHEMA_VERSION = 3
LEDGER_MIGRATED_KEY = "ledger_migrated"

_LEDGER_FIELDS = (
    "shard", "original_name", "original_localized", "base_name", "desired_name",
    "applied_rules", "source", "targets", "status", "approved",
    "manual_override", "note", "app_type", "type_raw",
)
# 这两组字段用 JSON 文本存储，其余用标量
_JSON_FIELDS = frozenset({"original_localized", "applied_rules", "targets"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    appid              INTEGER PRIMARY KEY,
    shard              TEXT,
    original_name      TEXT,
    original_localized TEXT,
    base_name          TEXT,
    desired_name       TEXT,
    applied_rules      TEXT,
    source             TEXT,
    targets            TEXT,
    status             TEXT,
    approved           INTEGER,
    manual_override    INTEGER,
    note               TEXT,
    app_type           TEXT,
    type_raw           TEXT,
    extra              TEXT
);
"""


def db_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / DB_NAME


def connect(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    migration_backups = target.parent / "migration-backups"
    legacy = target.parent / LEGACY_DB_NAME
    if (
        (not target.exists() or target.stat().st_size == 0)
        and not legacy.is_file()
        and migration_backups.exists()
        and any(migration_backups.iterdir())
    ):
        raise ValueError(
            "检测到迁移备份，但 renamer.db 不存在或为空；为避免重新导入过期 JSON，"
            "请从 migration-backups 恢复数据库，或确认后手工移走旧迁移备份"
        )
    if target.name == DB_NAME:
        _migrate_legacy_filename(target.parent, target)
    try:
        conn = sqlite3.connect(str(target), timeout=10)
    except sqlite3.OperationalError as exc:
        raise ValueError(
            f"无法打开数据库 {target}：{exc}。"
            "请确认配置目录存在且可写；若数据库丢失，请从 migration-backups 恢复。"
        ) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    # 只有库还没建表时才执行 schema：executescript 每次约 5ms，而 replan 会多次
    # connect，热路径上这笔开销比它守护的东西还贵。
    try:
        conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        conn.executescript(_SCHEMA)
    return conn
def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _load_json(text: str | None, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


# ---------------------------------------------------------------- ledger

def write_ledger(conn: sqlite3.Connection, data: dict) -> None:
    """Replace the ledger with `data` (same shape as names.json).

    Uses UPSERT instead of DELETE+INSERT: unchanged rows are updated in place,
    so saving one name no longer rewrites all 803 rows. The whole operation is
    still one transaction; readers never observe an intermediate state.
    """
    entries: dict[str, dict] = data.get("entries") or {}
    rows = []
    for key, record in entries.items():
        if not str(key).isdigit():
            continue
        extra = {
            name: value for name, value in record.items()
            if name not in _LEDGER_FIELDS
        }
        rows.append((
            int(key),
            *[
                _dump(record.get(name)) if name in _JSON_FIELDS
                else (int(record[name]) if name in ("approved", "manual_override")
                      and record.get(name) is not None
                      else record.get(name))
                for name in _LEDGER_FIELDS
            ],
            _dump(extra) if extra else None,
        ))

    columns = ", ".join(_LEDGER_FIELDS)
    assignments = ", ".join(
        f"{name}=excluded.{name}" for name in (*_LEDGER_FIELDS, "extra")
    )
    with conn:
        if rows:
            conn.executemany(
                f"INSERT INTO ledger(appid, {columns}, extra) "
                f"VALUES({', '.join('?' * (len(_LEDGER_FIELDS) + 2))}) "
                f"ON CONFLICT(appid) DO UPDATE SET {assignments}",
                rows,
            )
        # Only remove rows that are actually gone from the new data. Chunked
        # to stay under SQLITE_MAX_VARIABLE_NUMBER for large ledgers.
        keep = {row[0] for row in rows}
        existing = {row[0] for row in conn.execute("SELECT appid FROM ledger")}
        removed = sorted(existing - keep)
        for start in range(0, len(removed), 500):
            chunk = removed[start:start + 500]
            conn.execute(
                f"DELETE FROM ledger WHERE appid IN ({','.join('?' * len(chunk))})",
                chunk,
            )
        _set_meta(conn, "schema_version", str(data.get("schema_version", 1)))
        for key in ("meta", "defaults", "summary"):
            _set_meta(conn, key, json.dumps(data.get(key) or {}, ensure_ascii=False))


def read_ledger(conn: sqlite3.Connection) -> dict:
    """Rebuild the names.json shape from the ledger table."""
    entries: dict[str, dict] = {}
    for row in conn.execute("SELECT * FROM ledger ORDER BY appid"):
        record: dict[str, Any] = {}
        for name in _LEDGER_FIELDS:
            value = row[name]
            if value is None:
                continue  # 原文件用 exclude_none 语义，缺字段而非 null
            if name in _JSON_FIELDS:
                value = _load_json(value, [] if name != "original_localized" else {})
            elif name in ("approved", "manual_override"):
                value = bool(value)
            record[name] = value
        record.update(_load_json(row["extra"], {}))
        entries[str(row["appid"])] = record
    return {
        "schema_version": int(_get_meta(conn, "schema_version") or 1),
        "meta": _load_json(_get_meta(conn, "meta"), {}),
        "defaults": _load_json(_get_meta(conn, "defaults"), {}),
        "summary": _load_json(_get_meta(conn, "summary"), {}),
        "entries": entries,
    }


# store_cache 的读写函数已随商店补全功能移除（§30）；表定义保留，
# 存量库中的历史数据不再被读取，也无迁移必要。

# ---------------------------------------------------------------- dictionaries
def _query_in(
    conn: sqlite3.Connection, sql_tpl: str, appids, columns: tuple[str, ...]
) -> dict[int, dict]:
    """Fetch rows for the given appids in chunks (SQLite caps host params).

    This is where SQLite actually wins: a 100k-row dictionary returns only the
    ~800 appids this machine owns in ~1 ms, while loading the whole dictionary
    (JSON or SQLite alike) costs 50-70 ms.
    """
    ids = [int(a) for a in appids]
    out: dict[int, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        sql = sql_tpl.format(placeholders=",".join("?" * len(chunk)))
        for row in conn.execute(sql, chunk):
            out[row["appid"]] = {name: row[name] for name in columns}
    return out
# ---------------------------------------------------------------- migration

def ledger_ready(conn: sqlite3.Connection) -> bool:
    return _get_meta(conn, LEDGER_MIGRATED_KEY) == "1"
def _backup_dir(config_dir: Path) -> Path:
    folder = config_dir / "migration-backups"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _backup_file(path: Path, config_dir: Path) -> Path:
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
    destination = _backup_dir(config_dir) / f"{path.name}.pre-sqlite-{stamp}.bak"
    shutil.copy2(path, destination)
    return destination


def _backup_database(conn: sqlite3.Connection, config_dir: Path) -> Path:
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
    destination = _backup_dir(config_dir) / f"renamer.pre-sqlite-{stamp}.db"
    target = sqlite3.connect(str(destination))
    try:
        conn.backup(target)
    finally:
        target.close()
    return destination


def _migrate_ledger_source(
    conn: sqlite3.Connection, config_dir: Path, ledger_validator=None
) -> None:
    """Switch legacy JSON/DB mirrors to SQLite authority exactly once.

    The old runtime read JSON first, so JSON wins a one-time conflict. Both
    sides are backed up before reconciliation; later JSON edits are ignored.
    """
    if ledger_ready(conn):
        return
    names_path = config_dir / "names.json"
    json_data: dict | None = None
    json_error: Exception | None = None
    raw_hash = ""
    if names_path.exists():
        raw = names_path.read_bytes()
        raw_hash = hashlib.sha256(raw).hexdigest()
        try:
            loaded = json.loads(raw.decode("utf-8"))
            if isinstance(loaded, dict):
                json_data = loaded
            else:
                raise ValueError("names.json 顶层必须是对象")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            json_error = exc

    stored = read_ledger(conn)
    db_has_data = bool(
        stored.get("entries") or stored.get("meta") or stored.get("defaults")
        or stored.get("summary")
    )
    if json_data is not None:
        if ledger_validator is not None:
            json_data = ledger_validator(json_data, str(names_path))
        _backup_file(names_path, config_dir)
        if db_has_data:
            _backup_database(conn, config_dir)
        if json_data != stored or not db_has_data:
            write_ledger(conn, json_data)
    elif json_error is not None:
        if names_path.exists():
            _backup_file(names_path, config_dir)
        if not db_has_data:
            raise ValueError(f"names.json 损坏且 SQLite 无可用台账: {json_error}")
        _backup_database(conn, config_dir)
    elif db_has_data:
        _backup_database(conn, config_dir)

    _set_meta(conn, LEDGER_MIGRATED_KEY, "1")
    _set_meta(conn, "ledger_json_sha256", raw_hash)
    conn.commit()


def _community_from_json(path: Path) -> dict[int, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    games = payload.get("games") if isinstance(payload, dict) else None
    if not isinstance(games, dict):
        return {}
    names: dict[int, str] = {}
    for appid, value in games.items():
        if not str(appid).isdigit():
            continue
        if isinstance(value, dict):
            value = value.get("name")
        if isinstance(value, str) and value.strip():
            names[int(appid)] = value.strip()
    return names
def migrate_from_files(
    conn: sqlite3.Connection, config_dir: Path, defaults_dir: Path | None = None,
    ledger_validator=None,
) -> dict[str, int]:
    """Import legacy files once. Returns per-source row counts."""
    stats: dict[str, int] = {}
    _migrate_ledger_source(conn, config_dir, ledger_validator)
    stats["ledger"] = len(read_ledger(conn).get("entries") or {})
    # 词库（原名对照 / 共享 CSV / 社区表）自 v1.0.1 起改由 gamedb.db 提供，
    # 迁移只处理台账。

    keys = ("defaults", "summary", "meta")
    if not any(_get_meta(conn, key) for key in keys):
        for key in keys:
            _set_meta(conn, key, "{}")
    _set_meta(conn, "schema_version", "1")
    _set_meta(
        conn, "migrated_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    conn.commit()  # 上面的 _set_meta 不在 with 块内，必须显式提交
    return stats


def _pair_stamp(*paths) -> str:
    """Fingerprint the given CSV layers by mtime; missing files use '-'."""
    parts = []
    for path in paths:
        if path:
            candidate = Path(path)
            parts.append(str(candidate.stat().st_mtime_ns) if candidate.exists() else "-")
        else:
            parts.append("-")
    return "|".join(parts)
LEGACY_DB_NAME = "renamer.db"


def _migrate_legacy_filename(folder: Path, target: Path) -> None:
    """renamer.db → localgamedb.db：v1.0.1 架构调整，台账无损过渡。

    用 sqlite backup API 复制而非文件改名，规避 WAL/SHM 伴生文件的时序
    问题；旧文件移入 migration-backups/ 留档，不参与运行。
    """
    legacy = folder / LEGACY_DB_NAME
    if not legacy.is_file():
        return
    if target.exists() and target.stat().st_size > 0:
        return  # 新库已在，不重复迁移
    backup_dir = folder / "migration-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    legacy_conn = sqlite3.connect(str(legacy))
    try:
        new_conn = sqlite3.connect(str(target))
        try:
            legacy_conn.backup(new_conn)
        finally:
            new_conn.close()
    finally:
        legacy_conn.close()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.replace(legacy, backup_dir / f"renamer.db.pre-rename-{stamp}.bak")



def ensure_db(
    config_dir: str | Path, defaults_dir: str | Path | None = None,
    ledger_validator=None,
) -> Path:
    """Create/migrate the database and make SQLite authoritative."""
    folder = Path(config_dir)
    target = db_path(folder)
    _migrate_legacy_filename(folder, target)
    fresh = not target.exists() or target.stat().st_size == 0
    conn = connect(target)
    try:
        if fresh:
            migrate_from_files(
                conn, folder, Path(defaults_dir) if defaults_dir else None,
                ledger_validator,
            )
        else:
            _migrate_ledger_source(conn, folder, ledger_validator)
    finally:
        conn.close()
    return target
