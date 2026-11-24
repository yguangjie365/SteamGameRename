"""Single-file SQLite store for the ledger and dictionaries.

Design: the database is the runtime source of truth once it exists; the JSON
and CSV files become mirrors/exchange formats. `renamer.db` lives next to
names.json, so every existing loader only needs `path.parent` to find it and
all public signatures stay unchanged.

Safety:
* Migration is idempotent and only runs when the db file is absent or empty.
* JSON is rewritten as a mirror on every ledger save, so a user can always
  read, diff or hand-edit it, and roll back by deleting the db file.
* Anyone who wants the pre-SQLite behaviour just deletes renamer.db.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_NAME = "renamer.db"
SCHEMA_VERSION = 2

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
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
    """Replace the ledger with `data` (same shape as names.json)."""
    entries: dict[str, dict] = data.get("entries") or {}
    with conn:
        conn.execute("DELETE FROM ledger")
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
        conn.executemany(
            f"INSERT INTO ledger(appid, {', '.join(_LEDGER_FIELDS)}, extra) "
            f"VALUES({', '.join('?' * (len(_LEDGER_FIELDS) + 2))})",
            rows,
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


def write_store_cache(
    conn: sqlite3.Connection, cache: dict, raw: dict | None = None
) -> None:
    raw = raw or {}
    with conn:
        conn.execute("DELETE FROM store_cache")
        rows = []
        for appid, name in cache.items():
            stamp = ""
            item = raw.get(str(appid)) or raw.get(appid) or {}
            if isinstance(item, dict):
                stamp = str(item.get("fetched_at") or "")
            rows.append((int(appid), str(name), stamp))
        conn.executemany(
            "INSERT INTO store_cache(appid, name, fetched_at) VALUES(?,?,?)", rows
        )


def read_store_cache(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        row["appid"]: row["name"]
        for row in conn.execute("SELECT appid, name FROM store_cache")
    }


def read_store_raw(conn: sqlite3.Connection) -> dict[str, dict]:
    """Original {appid: {name, fetched_at}} shape, for rewriting store_cache.json."""
    return {
        str(row["appid"]): {"name": row["name"], "fetched_at": row["fetched_at"]}
        for row in conn.execute("SELECT appid, name, fetched_at FROM store_cache")
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


def query_store(conn: sqlite3.Connection, appids) -> dict[int, str]:
    rows = _query_in(
        conn, "SELECT appid, name FROM store_cache WHERE appid IN ({placeholders})",
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

def migrate_from_files(
    conn: sqlite3.Connection, config_dir: Path, defaults_dir: Path | None = None
) -> dict[str, int]:
    """Import whatever files exist. Returns per-source row counts."""
    stats: dict[str, int] = {}

    names_path = config_dir / "names.json"
    if names_path.exists():
        data = json.loads(names_path.read_text(encoding="utf-8"))
        write_ledger(conn, data)
        stats["ledger"] = len(data.get("entries") or {})

    community_path = config_dir / "community_names.json"
    if community_path.exists():
        payload = json.loads(community_path.read_text(encoding="utf-8"))
        games = payload.get("games") if isinstance(payload, dict) else None
        if isinstance(games, dict):
            names = {}
            for appid, value in games.items():
                if not str(appid).isdigit():
                    continue
                if isinstance(value, dict):
                    value = value.get("name")
                if value:
                    names[int(appid)] = str(value)
            if names:
                write_community(conn, names)
                stats["community"] = len(names)

    store_path = config_dir / "store_cache.json"
    if store_path.exists():
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            flat = {}
            for appid, value in raw.items():
                if not str(appid).isdigit():
                    continue
                name = value.get("name") if isinstance(value, dict) else value
                if name:
                    flat[int(appid)] = str(name)
            if flat:
                write_store_cache(conn, flat, raw)
                stats["store_cache"] = len(flat)

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
    _set_meta(conn, "migrated_at", "1")
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


def ensure_db(config_dir: str | Path, defaults_dir: str | Path | None = None) -> Path:
    """Create and migrate the db if missing. Idempotent; safe to call often."""
    folder = Path(config_dir)
    target = db_path(folder)
    fresh = not target.exists()
    conn = connect(target)
    try:
        if fresh:
            migrate_from_files(
                conn, folder, Path(defaults_dir) if defaults_dir else None
            )
        else:
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
