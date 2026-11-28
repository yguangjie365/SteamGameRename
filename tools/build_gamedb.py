#!/usr/bin/env python
"""Build gamedb.db (appid / name / name_cn / type) from a steamappidlist JSON.

用法：
    python tools/build_gamedb.py <games_appid.json 路径> [--db config/gamedb.db]

- appid / name 取自 JSON（name 去除首尾空白）；name_cn 留空待编辑；type 默认 'games'
- 重复运行安全：INSERT OR IGNORE，已存在的行不会被覆盖（保住你编辑的 name_cn/type）
- 数据源：github.com/steamsync/steamappidlist 的 data/games_appid.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    appid   INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    name_cn TEXT NOT NULL DEFAULT '',
    type    TEXT NOT NULL DEFAULT 'games'
);
"""


def load_records(path: Path) -> list[tuple[int, str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        # 容忍 {appid: name} 映射形态
        return [(int(k), v.strip()) for k, v in raw.items()
                if str(k).isdigit() and isinstance(v, str) and v.strip()]
    if not isinstance(raw, list):
        raise SystemExit("JSON 顶层必须是数组或对象")
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        appid, name = item.get("appid"), item.get("name")
        if isinstance(appid, int) and appid > 0 and isinstance(name, str) and name.strip():
            rows.append((appid, name.strip()))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="从全量 AppID 列表构建 gamedb.db")
    parser.add_argument("json", help="games_appid.json 路径")
    parser.add_argument("--db", default="config/gamedb.db", help="输出数据库路径")
    args = parser.parse_args()

    source = Path(args.json)
    if not source.is_file():
        raise SystemExit(f"找不到输入文件：{source}")
    rows = load_records(source)
    if not rows:
        raise SystemExit("JSON 里没有可用的 AppID 条目")

    dest = Path(args.db)
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dest)
    conn.execute(SCHEMA)
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO games(appid, name, name_cn, type) VALUES(?,?,?,?)",
            [(appid, name, "", "games") for appid, name in rows],
        )
    total = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    edited = conn.execute(
        "SELECT COUNT(*) FROM games WHERE name_cn != ''"
    ).fetchone()[0]
    conn.close()
    print(f"JSON 提供 {len(rows):,} 条；库内现共 {total:,} 条"
          f"（已编辑 name_cn {edited:,} 条）→ {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
