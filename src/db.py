"""SQLite source of truth for the ledger and dictionaries.

`renamer.db` is authoritative at runtime. Legacy JSON files are imported once,
backed up under `migration-backups/`, and then ignored by normal reads. CSV
files remain explicit import/export or override layers.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_NAME = "renamer.db"
SCHEMA_VERSION = 3
LEDGER_MIGRATED_KEY = "ledger_migrated"
COMMUNITY_MIGRATED_KEY = "community_migrated"

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
CREATE TABLE IF NOT EXISTS community (
    appid INTEGER PRIMARY KEY,
    name  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS store_cache (
    appid      INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    fetched_at TEXT
);
-- (appid, source) 联合主键：同一游戏可能同时有 baseline 与 learned 记录，
-- 单主键会让 learned 用 INSERT OR REPLACE 覆盖掉 baseline。
CREATE TABLE IF NOT EXISTS original_names (
    appid         INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    modified_name TEXT DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'baseline',
    PRIMARY KEY (appid, source)
);
CREATE TABLE IF NOT EXISTS shared_names (
    appid         INTEGER PRIMARY KEY,
    original_name TEXT,
    modified_name TEXT,
    source        TEXT,
    updated_at    TEXT,
    enabled       INTEGER DEFAULT 1,
    note          TEXT
);
"""


def db_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / DB_NAME


def connect(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    migration_backups = target.parent / "migration-backups"
    if (
        (not target.exists() or target.stat().st_size == 0)
        and migration_backups.exists()
        and any(migration_backups.iterdir())
    ):
        raise ValueError(
            "检测到迁移备份，但 renamer.db 不存在或为空；为避免重新导入过期 JSON，"
            "请从 migration-backups 恢复数据库，或确认后手工移走旧迁移备份"
        )
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
    _migrate_schema(conn)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Rebuild original_names if it was created with the appid-only primary key."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    info = list(conn.execute("PRAGMA table_info(original_names)"))
    pk = [row["name"] for row in info if row["pk"]]
    if pk == ["appid"]:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS original_names_v2 (
                    appid         INTEGER NOT NULL,
                    original_name TEXT NOT NULL,
                    modified_name TEXT DEFAULT '',
                    source        TEXT NOT NULL DEFAULT 'baseline',
                    PRIMARY KEY (appid, source)
                );
                INSERT OR IGNORE INTO original_names_v2
                    (appid, original_name, modified_name, source)
                    SELECT appid, original_name, modified_name, source
                    FROM original_names;
                DROP TABLE original_names;
                ALTER TABLE original_names_v2 RENAME TO original_names;
                """
            )
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


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

def write_community(conn: sqlite3.Connection, names: dict[int, str]) -> None:
    with conn:
        conn.execute("DELETE FROM community")
        conn.executemany(
            "INSERT INTO community(appid, name) VALUES(?,?)",
            [(int(k), str(v)) for k, v in names.items()],
        )


def read_community(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        row["appid"]: row["name"]
        for row in conn.execute("SELECT appid, name FROM community")
    }


def write_original_names(
    conn: sqlite3.Connection, names: dict[int, str], source: str = "baseline"
) -> None:
    with conn:
        conn.execute("DELETE FROM original_names WHERE source=?", (source,))
        conn.executemany(
            "INSERT OR REPLACE INTO original_names(appid, original_name, source) "
            "VALUES(?,?,?)",
            [(int(k), str(v), source) for k, v in names.items()],
        )


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


def query_community(conn: sqlite3.Connection, appids) -> dict[int, str]:
    rows = _query_in(
        conn, "SELECT appid, name FROM community WHERE appid IN ({placeholders})",
        appids, ("name",),
    )
    return {appid: item["name"] for appid, item in rows.items()}


def query_originals(conn: sqlite3.Connection, appids) -> dict[int, str]:
    """Baseline wins over learned for the same appid (merge_original_names rule)."""
    ids = [int(a) for a in appids]
    out: dict[int, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        sql = (
            "SELECT appid, original_name FROM original_names "
            f"WHERE appid IN ({','.join('?' * len(chunk))}) "
            "ORDER BY CASE source WHEN 'baseline' THEN 0 ELSE 1 END"
        )
        for row in conn.execute(sql, chunk):
            out.setdefault(row["appid"], row["original_name"])
    return out


def read_original_names(conn: sqlite3.Connection, source: str | None = None) -> dict[int, str]:
    sql = "SELECT appid, original_name FROM original_names"
    args: tuple = ()
    if source is not None:
        sql += " WHERE source=?"
        args = (source,)
    return {row["appid"]: row["original_name"] for row in conn.execute(sql, args)}


def import_appids_baseline(
    conn: sqlite3.Connection, names: dict[int, str], scope: set[int] | None = None
) -> int:
    """Merge AppID -> English name rows into the baseline, keeping existing rows.

    只补缺失，绝不覆盖：已有条目的「最初英文名」优先于外部列表的「当前名」
    （改名过的游戏外部列表携带的是新名，写入会污染对照语义，§12.4 红线）。
    scope 传入时只导入该集合内的 AppID（默认 None = 全量）。
    """
    rows = [
        (int(appid), name.strip(), "baseline")
        for appid, name in names.items()
        if str(appid).isdigit() and str(name).strip()
        and (scope is None or int(appid) in scope)
    ]
    if not rows:
        return 0
    with conn:
        inserted = 0
        for row in rows:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO original_names(appid, original_name, source) "
                "VALUES(?,?,?)",
                row,
            )
            inserted += cursor.rowcount
    return inserted


def write_shared_names(conn: sqlite3.Connection, shared) -> None:
    with conn:
        conn.execute("DELETE FROM shared_names")
        # shared_names 存的是最终生效值；originals 另存一列保持"最初英文名"
        appids = set(shared.names) | set(shared.originals)
        rows = [
            (
                int(appid),
                shared.originals.get(appid),
                shared.names.get(appid),
                shared.sources.get(appid, ""),
                None,
                1,
                None,
            )
            for appid in appids
        ]
        conn.executemany(
            "INSERT INTO shared_names(appid, original_name, modified_name, source,"
            " updated_at, enabled, note) VALUES(?,?,?,?,?,?,?)",
            rows,
        )


def write_shared_warnings(conn: sqlite3.Connection, warnings: list[str]) -> None:
    with conn:
        _set_meta(conn, "shared_warnings", json.dumps(list(warnings), ensure_ascii=False))


def read_shared_warnings(conn: sqlite3.Connection) -> list[str]:
    raw = _load_json(_get_meta(conn, "shared_warnings"), [])
    return [str(item) for item in raw] if isinstance(raw, list) else []


def read_shared_names(conn: sqlite3.Connection):
    from game_names import SharedNames

    result = SharedNames()
    for row in conn.execute("SELECT * FROM shared_names"):
        appid = row["appid"]
        if row["modified_name"]:
            result.names[appid] = row["modified_name"]
        if row["original_name"]:
            result.originals[appid] = row["original_name"]
        # 空字符串 source 也要保留，才能与 CSV 解析结果逐键一致
        if row["source"] is not None:
            result.sources[appid] = row["source"]
    return result


# ---------------------------------------------------------------- migration

def ledger_ready(conn: sqlite3.Connection) -> bool:
    return _get_meta(conn, LEDGER_MIGRATED_KEY) == "1"


def community_ready(conn: sqlite3.Connection) -> bool:
    return _get_meta(conn, COMMUNITY_MIGRATED_KEY) == "1"


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


def _migrate_community_source(conn: sqlite3.Connection, config_dir: Path) -> None:
    if community_ready(conn):
        return
    source = config_dir / "community_names.json"
    stored = read_community(conn)
    if source.exists():
        _backup_file(source, config_dir)
        if not stored:
            names = _community_from_json(source)
            if names:
                write_community(conn, names)
    _set_meta(conn, COMMUNITY_MIGRATED_KEY, "1")
    conn.commit()


def migrate_from_files(
    conn: sqlite3.Connection, config_dir: Path, defaults_dir: Path | None = None,
    ledger_validator=None,
) -> dict[str, int]:
    """Import legacy files once. Returns per-source row counts."""
    stats: dict[str, int] = {}
    _migrate_ledger_source(conn, config_dir, ledger_validator)
    stats["ledger"] = len(read_ledger(conn).get("entries") or {})
    _migrate_community_source(conn, config_dir)
    stats["community"] = len(read_community(conn))

    if defaults_dir is not None:
        baseline = defaults_dir / "original_names.csv"
        if baseline.exists():
            from game_names import load_original_names

            loaded = load_original_names(baseline)
            if loaded:
                write_original_names(conn, loaded, "baseline")
                stats["original_names"] = len(loaded)
                _set_meta(conn, "baseline_mtime", str(baseline.stat().st_mtime_ns))

    learned = config_dir / "learned_originals.csv"
    if learned.exists():
        from game_names import load_original_names

        loaded = load_original_names(learned)
        if loaded:
            write_original_names(conn, loaded, "learned")
            stats["learned"] = len(loaded)

    # 共享词库：连同校验警告一起入库，避免切到 db 后丢掉"重复/格式"提示
    baseline_shared = (defaults_dir / "game_names.csv") if defaults_dir else None
    local_shared = config_dir / "game_names.csv"
    if (baseline_shared and baseline_shared.exists()) or local_shared.exists():
        from game_names import load_shared_names_from_files

        shared = load_shared_names_from_files(baseline_shared or "", local_shared)
        if shared.names or shared.originals:
            write_shared_names(conn, shared)
            write_shared_warnings(conn, shared.warnings)
            stats["shared_names"] = len(shared.names)
        _set_meta(conn, "shared_mtime", _pair_stamp(baseline_shared, local_shared))

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


def sync_shared_from_files(conn: sqlite3.Connection, baseline, local) -> None:
    """Reimport the shared CSV layers when they are newer than the store.

    Without this, editing config/game_names.csv would need a restart to take
    effect — a regression versus reading CSV on every plan.
    """
    stamp = _pair_stamp(baseline, local)
    if _get_meta(conn, "shared_mtime") == stamp:
        return
    if not ((baseline and Path(baseline).exists()) or (local and Path(local).exists())):
        return
    from game_names import load_shared_names_from_files

    shared = load_shared_names_from_files(baseline or "", local)
    if shared.names or shared.originals:
        write_shared_names(conn, shared)
        write_shared_warnings(conn, shared.warnings)
    _set_meta(conn, "shared_mtime", stamp)
    conn.commit()


def _refresh_shared(
    conn: sqlite3.Connection, config_dir: Path, defaults_dir: Path | None
) -> None:
    baseline = (defaults_dir / "game_names.csv") if defaults_dir else None
    sync_shared_from_files(conn, baseline, config_dir / "game_names.csv")


def ensure_db(
    config_dir: str | Path, defaults_dir: str | Path | None = None,
    ledger_validator=None,
) -> Path:
    """Create/migrate the database and make SQLite authoritative."""
    folder = Path(config_dir)
    target = db_path(folder)
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
            _migrate_community_source(conn, folder)
            # 基线表跟随随包 CSV 更新（新版本带来更多英文原名 / 共享词库）
            target_defaults = Path(defaults_dir) if defaults_dir else None
            _refresh_baseline(conn, target_defaults)
            _refresh_shared(conn, folder, target_defaults)
    finally:
        conn.close()
    return target


def _refresh_baseline(conn: sqlite3.Connection, defaults_dir: Path | None) -> None:
    if defaults_dir is None:
        return
    baseline = defaults_dir / "original_names.csv"
    if not baseline.exists():
        return
    stamp = str(baseline.stat().st_mtime_ns)
    if _get_meta(conn, "baseline_mtime") == stamp:
        return
    from game_names import load_original_names

    loaded = load_original_names(baseline)
    if loaded:
        write_original_names(conn, loaded, "baseline")
        _set_meta(conn, "baseline_mtime", stamp)
        conn.commit()
