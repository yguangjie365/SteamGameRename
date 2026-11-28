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
    """程序目录下的 config/；仅显式环境变量可覆盖。

    配置始终跟随程序目录：首次运行时目录不存在也返回该路径，由
    ensure_config 创建。绝不回退到 %APPDATA%，避免"台账消失"或数据分叉。
    """
    env = os.environ.get("STEAM_CN_CONFIG")
    if env:
        return Path(env)
    return PROJECT_ROOT / "config"


def default_backup_root() -> Path:
    """程序目录下的 backups/；仅显式环境变量可覆盖。"""
    env = os.environ.get("STEAM_CN_BACKUPS")
    if env:
        return Path(env)
    return PROJECT_ROOT / "backups"


def ensure_config(config_dir: str | Path) -> Path:
    """Create missing config files from the templates bundled with the package."""
    folder = Path(config_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("names.json",):
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
    """Create or migrate renamer.db; never silently fall back to legacy JSON."""
    if os.environ.get("STEAM_CN_RENAMER_NO_DB"):
        return  # 测试用：保持纯 JSON 路径，避免在 fixtures 目录建库
    try:
        from db import ensure_db
        from mapping import _validate_ledger

        ensure_db(folder, DEFAULTS_DIR, ledger_validator=_validate_ledger)
    except Exception as exc:  # noqa: BLE001 - 初始化失败只告警，不伪造数据源
        print(f"[warn] SQLite 存储初始化失败，请检查配置目录：{exc}", file=sys.stderr)


DEFAULT_CONFIG = default_config_dir()
DEFAULT_BACKUPS = default_backup_root()
