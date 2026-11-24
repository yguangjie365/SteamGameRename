"""Backup, atomic write and rollback for Steam appinfo shards."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from shard import (
    Shard,
    build_bytes,
    build_bytes_with_new_keys,
    check_targets,
    parse_bytes,
    parse_file,
    verify,
    verify_changes,
)

# 允许在写入时不存在的目标字段（会自动追加 key 与子节点）
CREATABLE_TARGETS = frozenset({"common.sortas"})


class BackupError(RuntimeError):
    pass


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


_last_steam_check: tuple[float, bool] | None = None


def steam_running(ttl: float = 3.0) -> bool:
    """Best-effort detection on Windows, cached briefly to avoid process spam."""
    global _last_steam_check
    if _last_steam_check and time.monotonic() - _last_steam_check[0] < ttl:
        return _last_steam_check[1]
    result = _steam_running_uncached()
    _last_steam_check = (time.monotonic(), result)
    return result


def _steam_running_uncached() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe"],
            capture_output=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    return b"steam.exe" in result.stdout.lower()


def steam_shutdown(steam_exe: str | Path, timeout: float = 20.0) -> bool:
    """Ask Steam to close gracefully via `steam.exe -shutdown`.

    Polling waits for the process to actually exit. Returns True only if Steam
    is confirmed gone. Never kills processes: a forced kill can corrupt cloud
    saves. A timeout or launch failure is reported as False so callers can
    abort the write instead of risking an inconsistent appinfo.
    """
    exe = Path(steam_exe)
    if os.name != "nt" or not exe.is_file():
        return not steam_running(ttl=0)
    if not steam_running(ttl=0):
        return True
    try:
        subprocess.Popen(
            [str(exe), "-shutdown"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not steam_running(ttl=0):
            return True
        time.sleep(0.5)
    return not steam_running(ttl=0)


def steam_start(steam_exe: str | Path) -> bool:
    """Relaunch the Steam client after a successful write. Best-effort."""
    exe = Path(steam_exe)
    if os.name != "nt" or not exe.is_file():
        return False
    try:
        subprocess.Popen(
            [str(exe)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(exe.parent),
        )
        return True
    except OSError:
        return False


def atomic_write(path: str | Path, data: bytes) -> None:
    """Write via a unique temp file + fsync + atomic replace."""
    target = Path(path)
    descriptor, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def create_backup(appinfo_dir: str | Path, backup_root: str | Path) -> Path:
    source = Path(appinfo_dir)
    files = sorted(source.glob("appinfo_*.vdf"))
    if not files:
        raise BackupError(f"no appinfo shards found in {source}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = 1
    while True:
        destination = Path(backup_root) / (
            stamp if suffix == 1 else f"{stamp}-{suffix}"
        )
        try:
            destination.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            suffix += 1
    manifest = {
        "manifest_version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_dir": str(source),
        "files": {},
    }
    for file in files:
        target = destination / file.name
        target.write_bytes(file.read_bytes())
        manifest["files"][file.name] = sha256_file(target)
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destination


def verify_backup(backup_dir: str | Path) -> dict:
    folder = Path(backup_dir)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("manifest_version", 1)
    if version != 1:
        raise BackupError(
            f"备份清单版本 {version} 不受支持（当前程序只认 1）: {folder}"
        )
    if "files" not in manifest:
        raise BackupError(f"备份清单缺 files 字段: {folder}")
    for name, digest in manifest["files"].items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in (".", "..")
            or not (name.startswith("appinfo_") and name.endswith(".vdf"))
        ):
            raise BackupError(f"备份清单含非法文件名 {name!r}: {folder}")
        path = folder / name
        if not path.exists() or sha256_file(path) != digest:
            raise BackupError(f"backup file mismatch or missing: {name}")
    return manifest


def restore_backup(
    backup_dir: str | Path,
    appinfo_dir: str | Path,
    require_steam_closed: bool = False,
) -> int:
    if require_steam_closed and steam_running(ttl=0):
        raise BackupError("Steam is running; close it before restoring")
    manifest = verify_backup(backup_dir)
    target_dir = Path(appinfo_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in manifest["files"]:
        atomic_write(target_dir / name, (Path(backup_dir) / name).read_bytes())
    return len(manifest["files"])


def list_backups(backup_root: str | Path) -> list[dict]:
    """List restore points (newest first) with file count and total size."""
    root = Path(backup_root)
    if not root.is_dir():
        return []
    items: list[dict] = []
    for folder in sorted(root.iterdir(), reverse=True):
        manifest_path = folder / "manifest.json"
        if not folder.is_dir() or not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files = manifest.get("files", {})
        size = sum(
            (folder / name).stat().st_size
            for name in files
            if (folder / name).exists()
        )
        items.append({
            "name": folder.name,
            "created_at": manifest.get("created_at", ""),
            "files": len(files),
            "size": size,
            "mtime_ns": folder.stat().st_mtime_ns,
        })
    # 目录名是时间戳，但同秒内会带 -1/-2 后缀，字典序不可靠；按时间倒序
    items.sort(key=lambda item: (item["created_at"], item["mtime_ns"]), reverse=True)
    return items


def prune_backups(backup_root: str | Path, keep: int = 8) -> list[str]:
    """Delete the oldest restore points, keeping the newest `keep` ones.

    Only folders that contain a manifest.json are touched, and only when they
    are direct children of backup_root.
    """
    import shutil

    root = Path(backup_root)
    if keep < 1:
        raise BackupError("keep must be >= 1")
    candidates = [item for item in list_backups(root)]
    removable = candidates[keep:]
    removed: list[str] = []
    for item in removable:
        folder = root / item["name"]
        if folder.parent != root or not (folder / "manifest.json").exists():
            continue
        shutil.rmtree(folder)
        removed.append(item["name"])
    return removed


def apply_changes(
    appinfo_dir: str | Path,
    changes: dict[str, dict[int, dict[str, str]]],
    backup_root: str | Path,
    require_steam_closed: bool = True,
    parsed_shards: dict[str, Shard] | None = None,
) -> dict:
    """Backup, rebuild changed shards and atomically replace them.

    Any failure triggers a full restore from the just-created backup.
    """
    source = Path(appinfo_dir)
    if require_steam_closed and steam_running(ttl=0):
        raise BackupError("Steam is running; close it before applying changes")
    if not changes:
        return {"changed": [], "backup": None, "backup_size": 0}

    parsed: dict[str, Shard] = dict(parsed_shards or {})
    problems: list[str] = []
    for name in sorted(changes):
        if Path(name).name != name or name in (".", ".."):
            raise BackupError(f"invalid shard name: {name!r}")
        shard = parsed.get(name)
        if shard is None:
            shard = parse_file(source / name)
            parsed[name] = shard
        problems += [
            f"{name}: {problem}" for problem in check_targets(
                shard, changes[name], allow_create=CREATABLE_TARGETS
            )
        ]
    if problems:
        raise BackupError("preflight failed: " + "; ".join(problems[:5]))

    backup = create_backup(source, backup_root)
    changed: list[str] = []
    try:
        for name in sorted(changes):
            path = source / name
            shard = parsed[name]
            needs_new_keys = any(
                target in CREATABLE_TARGETS
                for targets in changes[name].values()
                for target in targets
            )
            if needs_new_keys:
                rebuilt = build_bytes_with_new_keys(
                    shard, changes[name], allow_create=CREATABLE_TARGETS
                )
            else:
                rebuilt = build_bytes(shard, changes[name])
            candidate = parse_bytes(rebuilt, path)
            problems = verify(candidate) + verify_changes(candidate, changes[name])
            if problems:
                raise BackupError(f"{name}: rebuilt file failed verification: {problems}")
            atomic_write(path, rebuilt)
            changed.append(name)
    except Exception as original:
        try:
            restore_backup(backup, source)
        except Exception as restore_error:
            raise BackupError(
                f"apply failed: {original}; rollback also failed: {restore_error}"
            ) from original
        raise
    size = sum(item.stat().st_size for item in backup.iterdir() if item.is_file())
    return {"changed": changed, "backup": str(backup), "backup_size": size}
