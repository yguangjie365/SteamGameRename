"""Path resolution and first-run configuration bootstrap.

Kept separate from cli.py so the web layer does not have to import Typer/Rich
just to know where config and backups live.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def asset_dir(name: str) -> Path:
    """Locate a bundled asset folder: next to the modules, else the share dir."""
    local = Path(__file__).resolve().parent / name
    if local.is_dir():
        return local
    return Path(sys.prefix) / "share" / "steam-cn-renamer" / name


DEFAULTS_DIR = asset_dir("defaults")


def default_config_dir() -> Path:
    """Project config when running from source, otherwise a per-user folder."""
    env = os.environ.get("STEAM_CN_CONFIG")
    if env:
        return Path(env)
    project = PROJECT_ROOT / "config"
    if project.is_dir():
        return project
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / "steam-cn-renamer"


def default_backup_root() -> Path:
    env = os.environ.get("STEAM_CN_BACKUPS")
    if env:
        return Path(env)
    if (PROJECT_ROOT / "config").is_dir():
        return PROJECT_ROOT / "backups"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "steam-cn-renamer" / "backups"


def ensure_config(config_dir: str | Path) -> Path:
    """Create missing config files from the templates bundled with the package."""
    folder = Path(config_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("names.json", "rules.json"):
            target = folder / name
            if target.exists():
                continue
            template = DEFAULTS_DIR / name
            if template.exists():
                shutil.copyfile(template, target)
        _bootstrap_db(folder)
    except OSError as exc:
        print(f"[warn] 无法写入配置目录 {folder}: {exc}", file=sys.stderr)
    return folder


def _bootstrap_db(folder: Path) -> None:
    """Create or migrate renamer.db. Failure must never block first run.

    Any error (sqlite3.Error, locked file, permission) is downgraded to a
    warning: loaders fall back to JSON/CSV when the db is missing or empty.
    """
    if os.environ.get("STEAM_CN_RENAMER_NO_DB"):
        return  # 测试用：保持纯 JSON 路径，避免在 fixtures 目录建库
    try:
        from db import ensure_db

        ensure_db(folder, DEFAULTS_DIR)
    except Exception as exc:  # noqa: BLE001 - 存储降级必须兜底
        print(f"[warn] SQLite 存储初始化失败，继续使用 JSON: {exc}", file=sys.stderr)


DEFAULT_CONFIG = default_config_dir()
DEFAULT_BACKUPS = default_backup_root()
