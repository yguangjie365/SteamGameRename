#!/usr/bin/env python
"""Update gamedb.db name_cn from a community_names.json ({appid: 中文名}).

用法：
    python tools/update_gamedb_cn.py <community_names.json> [--db config/gamedb.db] [--overwrite]

- 默认只填充 name_cn 为空的行：反复运行不会覆盖你手工编辑的中文名
- --overwrite：JSON 的值无条件覆盖（包括已手工编辑的行）
- gamedb 里不存在的 AppID 会跳过并在报告中列出（可据此扩库）
- 兼容两种 JSON 形态：顶层带 games 键（本工具社区词库格式）或直接 {appid: name}
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def load_cn_names(path: Path) -> dict[int, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    games = raw.get("games") if isinstance(raw, dict) else None
    if not isinstance(games, dict):
        games = raw if isinstance(raw, dict) else {}
    out: dict[int, str] = {}
    for appid, name in games.items():
        if str(appid).isdigit() and isinstance(name, str) and name.strip():
            out[int(appid)] = name.strip()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="把社区中文名更新到 gamedb.db 的 name_cn")
    parser.add_argument("json", help="community_names.json 路径（{appid: 中文名}）")
    parser.add_argument("--db", default="config/gamedb.db", help="gamedb.db 路径")
    parser.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已有 name_cn（默认只填充空行，保护手工编辑）",
    )
    args = parser.parse_args()

    source = Path(args.json)
    if not source.is_file():
        raise SystemExit(f"找不到输入文件：{source}")
    updates = load_cn_names(source)
    if not updates:
        raise SystemExit("JSON 里没有可用的中文名条目")

    db_file = Path(args.db)
    if not db_file.is_file():
        raise SystemExit(f"找不到数据库：{db_file}（先用 tools/build_gamedb.py 建库）")

    conn = sqlite3.connect(db_file)
    filled = overwritten = kept = missing = 0
    missing_ids: list[int] = []
    with conn:
        for appid, name_cn in sorted(updates.items()):
            row = conn.execute(
                "SELECT name_cn FROM games WHERE appid=?", (appid,)
            ).fetchone()
            if row is None:
                missing += 1
                if len(missing_ids) < 12:
                    missing_ids.append(appid)
                continue
            existing = row[0]
            if existing and not args.overwrite:
                kept += 1
                continue
            conn.execute(
                "UPDATE games SET name_cn=? WHERE appid=?", (name_cn, appid)
            )
            if existing:
                overwritten += 1
            else:
                filled += 1
    total_cn = conn.execute(
        "SELECT COUNT(*) FROM games WHERE name_cn != ''"
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    conn.close()

    print(
        f"JSON 提供 {len(updates)} 条 → 填充空行 {filled} 条"
        + (f"，覆盖已编辑 {overwritten} 条" if args.overwrite else f"，保留已编辑 {kept} 条")
        + f"；gamedb 无此 AppID 跳过 {missing} 条"
    )
    if missing_ids:
        print(f"  跳过的 AppID（前 12 个）：{missing_ids}")
    print(f"gamedb 现有中文名 {total_cn:,} / {total:,} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
