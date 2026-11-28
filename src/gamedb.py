"""gamedb.db — 全量游戏信息库（appid / name / name_cn / type）。

v1.0.1 架构调整：建议名称与原名对照的来源从 renamer.db 的
community/original_names 表切换为本库（维护者编辑 name_cn 后上传
GitHub 分发）。renamer.db 更名 localgamedb.db 后只存台账等本地信息。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import httpx

from backup import atomic_write

GAMEDB_FILENAME = "gamedb.db"
# 维护者把 gamedb.db 放在主仓库分发；可用环境变量覆盖。
GAMEDB_DOWNLOAD_URL = os.environ.get(
    "STEAM_CN_GAMEDB_URL",
    "https://raw.githubusercontent.com/yguangjie365/SteamGameRename/main/src/defaults/gamedb.db",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    appid   INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    name_cn TEXT NOT NULL DEFAULT '',
    type    TEXT NOT NULL DEFAULT 'games'
);
"""


def gamedb_path(config_dir: str | Path) -> Path:
    return Path(config_dir) / GAMEDB_FILENAME


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) gamedb.db and make sure the schema exists."""
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def query_names(conn: sqlite3.Connection, appids) -> dict[int, str]:
    """appid → 英文主名（原名对照展示用）。"""
    ids = [int(a) for a in appids]
    out: dict[int, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        sql = (
            "SELECT appid, name FROM games WHERE appid IN "
            f"({','.join('?' * len(chunk))})"
        )
        for row in conn.execute(sql, chunk):
            out[row["appid"]] = row["name"]
    return out


def query_name_cn(conn: sqlite3.Connection, appids) -> dict[int, str]:
    """appid → 中文名（建议名称来源）。"""
    ids = [int(a) for a in appids]
    out: dict[int, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        sql = (
            "SELECT appid, name_cn FROM games WHERE appid IN "
            f"({','.join('?' * len(chunk))}) AND name_cn != ''"
        )
        for row in conn.execute(sql, chunk):
            out[row["appid"]] = row["name_cn"]
    return out


def _looks_like_gamedb(path: Path) -> bool:
    """Basic sanity check before replacing the local copy with a download."""
    try:
        conn = sqlite3.connect(str(path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "games" not in tables:
                return False
            cols = [row[1] for row in conn.execute("PRAGMA table_info(games)")]
            return {"appid", "name", "name_cn", "type"} <= set(cols)
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def download_and_merge(
    dest: Path, url: str = GAMEDB_DOWNLOAD_URL, timeout: float = 60.0
) -> dict:
    """下载 gamedb.db，校验后与本地合并；本地已编辑的 name_cn 不被覆盖。

    合并语义（远端权威=全量列表，本地权威=人工编辑的 name_cn）：
      - 新 AppID → 插入
      - name 字段 → 以远端为准（Steam 数据会更新）
      - name_cn → 本地为空则取远端；本地非空保留人工编辑
    返回 {"inserted": N, "name_updated": N, "name_cn_filled": N, "total": N}。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    download = dest.with_suffix(".download")
    response = httpx.get(
        url, timeout=timeout, follow_redirects=True,
        headers={"User-Agent": "steam-cn-renamer/1.0 (+local tool)"},
    )
    response.raise_for_status()
    download.write_bytes(response.content)
    if not _looks_like_gamedb(download):
        download.unlink(missing_ok=True)
        raise ValueError("下载的 gamedb.db 校验失败（不是有效的游戏信息库）")

    stats = {"inserted": 0, "total": 0}
    if dest.exists():
        # 旧库留一份带时间戳的副本，合并出问题可手动回退
        stamp = dest.stat().st_mtime_ns
        backup = dest.with_suffix(f".pre-update-{stamp}.bak")
        backup.write_bytes(dest.read_bytes())

    conn = sqlite3.connect(str(dest))
    try:
        conn.execute(_SCHEMA)
        before = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        conn.execute("ATTACH DATABASE ? AS remote", (str(download),))
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO main.games(appid, name, name_cn, type) "
                "SELECT appid, name, name_cn, type FROM remote.games"
            )
            # ATTACH 场景下子查询里的本地表必须用 main. 显式限定：不带限定时
            # games 会优先解析为子查询自己的 remote.games，导致恒真条件把
            # 全库字段覆盖成第一行的值（测试抓到过）。
            conn.execute(
                "UPDATE main.games SET name = "
                "(SELECT r.name FROM remote.games r WHERE r.appid = main.games.appid) "
                "WHERE appid IN (SELECT appid FROM remote.games)"
            )
            conn.execute(
                "UPDATE main.games SET name_cn = "
                "(SELECT r.name_cn FROM remote.games r WHERE r.appid = main.games.appid) "
                "WHERE name_cn = '' "
                "AND appid IN (SELECT appid FROM remote.games WHERE name_cn != '')"
            )
        after = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        stats["inserted"] = after - before
        stats["total"] = after
    finally:
        try:
            conn.execute("DETACH DATABASE remote")
        except sqlite3.Error:
            pass
        conn.close()
    download.unlink(missing_ok=True)
    return stats
