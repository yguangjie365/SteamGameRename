"""CSV 交换格式的写入（export-mappings / 台账导出）。

v1.0.1 架构调整：共享词库的两层加载、原名基线与学习沉淀已随 gamedb.db
架构（开发者说明 §32）废弃——建议名与原名对照统一由 gamedb.db 提供。
本模块只保留 CSV 导出写盘与公式注入转义。
"""
from __future__ import annotations

import csv
import datetime
import io
from pathlib import Path

from backup import atomic_write

FIELDS = (
    "GameID", "OriginalName", "ModifiedName",
    "Source", "UpdatedAt", "Enabled", "Note",
)


def _safe_csv_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection (CWE-1236).

    Steam names come from third-party publishers; a cell starting with
    = + - @ TAB or CR would execute as a formula/DDE when the exported CSV
    is opened in Excel. Prefixing an apostrophe matches the front-end
    csvField() escaping so both export paths behave the same.
    """
    value = str(value)
    if value and (value[0] in "=+-@" or value[0] in "\t\r"):
        return "'" + value
    return value


def write_shared_names_csv(
    entries: list[tuple[int, str, str, str]], dest: str | Path
) -> int:
    """Export (appid, original, modified, source) rows as UTF-8-BOM CSV."""
    today = datetime.date.today().isoformat()
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(FIELDS)
    for appid, original, modified, source in entries:
        writer.writerow((
            appid,
            _safe_csv_cell(original),
            _safe_csv_cell(modified),
            _safe_csv_cell(source),
            today, "true", "",
        ))
    atomic_write(Path(dest), ("﻿" + buffer.getvalue()).encode("utf-8"))
    return len(entries)
