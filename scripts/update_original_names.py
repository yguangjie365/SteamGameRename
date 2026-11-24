#!/usr/bin/env python3
"""Merge newly seen English names from Steam's AppList into original_names.csv.

Existing rows are never overwritten: this table stores the ORIGINAL English
name, while GetAppList exposes the CURRENT name (which may be a later rename).
Only AppIDs absent from the CSV and carrying an ASCII-only name are appended,
bounded by --limit so the distributed file stays small.
"""
from __future__ import annotations

import argparse
import csv
import json
import urllib.request
from pathlib import Path

API = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
DEFAULT_CSV = (
    Path(__file__).resolve().parents[1] / "src" / "defaults" / "original_names.csv"
)
FIELDS = ("GameID", "OriginalName", "ModifiedName")


def fetch_applist(timeout: int = 60) -> dict[int, str]:
    request = urllib.request.Request(
        API, headers={"User-Agent": "steam-cn-renamer-db-bot/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    apps: dict[int, str] = {}
    for item in payload["applist"]["apps"]:
        raw = str(item.get("appid", ""))
        name = str(item.get("name", "")).strip()
        if raw.isdigit() and name:
            apps[int(raw)] = name
    return apps


def is_ascii_name(name: str) -> bool:
    return all(ord(char) < 128 for char in name) and any(
        char.isalpha() for char in name
    )


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument(
        "--limit", type=int, default=200, help="最多追加条数（默认 200）"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    applist = fetch_applist()
    rows = read_rows(args.csv)
    known = {
        int(row["GameID"])
        for row in rows
        if str(row.get("GameID", "")).isdigit()
    }
    candidates = sorted(
        appid
        for appid in applist
        if appid not in known and is_ascii_name(applist[appid])
    )
    additions = [
        {"GameID": str(appid), "OriginalName": applist[appid], "ModifiedName": ""}
        for appid in candidates[: max(0, args.limit)]
    ]

    print(
        f"AppList 条目: {len(applist)}；现有: {len(rows)}；"
        f"可新增: {len(candidates)}；本次追加: {len(additions)}"
    )
    if args.dry_run:
        for row in additions[:10]:
            print(f"  {row['GameID']}  {row['OriginalName']}")
        return 0
    if not additions:
        return 0

    # 原表带 UTF-8 BOM；追加时用不带 BOM 的 utf-8，并先补齐末尾换行
    raw = args.csv.read_bytes()
    if raw and not raw.endswith(b"\n"):
        with args.csv.open("ab") as stream:
            stream.write(b"\n")
    with args.csv.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=list(FIELDS)).writerows(additions)
    print("已追加到", args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
