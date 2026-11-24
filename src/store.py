"""Steam Store Chinese-name lookups with an on-disk cache."""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

import db as dbmod
from backup import atomic_write
from rules import CJK


def load_store_cache(path: str | Path) -> dict[int, str]:
    """Read store_cache.json as {appid: chinese_name}; missing file is empty."""
    cache_path = Path(path)
    db_file = dbmod.db_path(cache_path.parent)
    if db_file.exists():
        conn = dbmod.connect(db_file)
        try:
            stored = dbmod.read_store_cache(conn)
        finally:
            conn.close()
        if stored:
            return stored
    if not cache_path.exists():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[int, str] = {}
    for key, value in raw.items():
        if key.isdigit() and isinstance(value, dict) and value.get("name"):
            result[int(key)] = value["name"]
    return result


def fetch_store_names(
    appids: list[int], cache_path: str | Path, ttl_days: int = 30
) -> dict[int, str]:
    """Fetch Simplified Chinese names from the Steam Store API with a disk cache."""
    cache_file = Path(cache_path)
    cache: dict[str, dict] = {}
    if cache_file.exists():
        try:
            loaded = json.loads(cache_file.read_text(encoding="utf-8"))
            cache = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            cache = {}
    now = time.time()
    result: dict[int, str] = {}
    pending: list[int] = []

    for appid in appids:
        key = str(appid)
        cached = cache.get(key)
        if cached and now - cached.get("fetched_at", 0) < ttl_days * 86400:
            if cached.get("name"):
                result[appid] = cached["name"]
        else:
            pending.append(appid)

    headers = {"User-Agent": "steam-cn-renamer/0.2 (+local tool)"}
    with httpx.Client(timeout=15, headers=headers, follow_redirects=True) as client:
        for offset in range(0, len(pending), 50):
            batch = pending[offset : offset + 50]
            url = (
                "https://store.steampowered.com/api/appdetails?appids="
                + ",".join(map(str, batch))
                + "&l=schinese&cc=cn&filters=basic"
            )
            payload = None
            for attempt in range(3):
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    payload = response.json()
                    break
                except (httpx.HTTPError, json.JSONDecodeError):
                    if attempt == 2:
                        payload = None
                    else:
                        time.sleep(0.5 * (attempt + 1))
            if not isinstance(payload, dict):
                continue
            for appid in batch:
                entry = payload.get(str(appid), {})
                if not entry.get("success"):
                    continue
                data = entry.get("data")
                name = data.get("name", "") if isinstance(data, dict) else ""
                if not name or not CJK.search(name):
                    continue
                result[appid] = name
                cache[str(appid)] = {"name": name, "fetched_at": now}

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        cache_file,
        (json.dumps(cache, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    # 同步进 SQLite：load_store_cache 优先读 db
    db_file = dbmod.db_path(cache_file.parent)
    if db_file.exists():
        flat = {
            int(key): value["name"]
            for key, value in cache.items()
            if str(key).isdigit() and isinstance(value, dict) and value.get("name")
        }
        if flat:
            conn = dbmod.connect(db_file)
            try:
                dbmod.write_store_cache(conn, flat, cache)
            finally:
                conn.close()
    return result
