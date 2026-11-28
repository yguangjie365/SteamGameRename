"""Locate the Steam client install and remember the last choice.

Steam install discovery shared by the browser launcher (run.py) and the CLI.
Port probing and browser opening live in run.py; this module owns only the
discovery chain:

    命令行参数 → 环境变量 STEAM_DIR → launcher.json → 注册表 → 常见路径 → 全盘扫描
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from backup import atomic_write

ROOT = Path(__file__).resolve().parent
# 本模块位于 src/，但 launcher.json 属于项目根（与 run.py 同层）
PROJECT_ROOT = ROOT.parent
SETTINGS_FILE = PROJECT_ROOT / "launcher.json"

COMMON_STEAM_PATHS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"C:\Steam",
    r"D:\Steam",
    r"D:\Program Files (x86)\Steam",
    r"E:\Steam",
    r"F:\Steam",
)


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(data: dict) -> None:
    try:
        atomic_write(
            SETTINGS_FILE,
            (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    except OSError:
        pass


def is_steam_dir(path: Path) -> bool:
    """A usable Steam root must expose APPCache/APPInfo.

    A library folder (only steamapps) is not enough: the editor works on the
    client cache, so accepting it would only move the failure downstream.
    """
    try:
        return (path / "APPCache" / "APPInfo").is_dir()
    except OSError:
        return False


def looks_like_library(path: Path) -> bool:
    """True for a library folder, or a client that never generated APPCache."""
    try:
        return (path / "steamapps").is_dir() or (path / "APPCache").is_dir()
    except OSError:
        return False


def registry_paths() -> list[Path]:
    found: list[Path] = []
    if os.name != "nt":
        return found
    try:
        import winreg
    except ImportError:
        return found
    candidates = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for hive, subkey, value_name in candidates:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if value:
            found.append(Path(str(value)))
    return found


def drive_scan() -> list[Path]:
    """Last resort: look for a Steam folder on every mounted drive."""
    found: list[Path] = []
    if os.name != "nt":
        return found
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        for name in ("Steam", "Games\\Steam"):
            candidate = root / name
            if candidate.is_dir():
                found.append(candidate)
    return found


def detect_steam_dir(explicit: str | None, settings: dict) -> tuple[Path | None, str]:
    """Return (path, how it was found) or (None, '') when nothing matched."""
    if explicit:
        path = Path(explicit)
        return (path, "命令行参数") if is_steam_dir(path) else (None, "")

    env_value = os.environ.get("STEAM_DIR")
    if env_value and is_steam_dir(Path(env_value)):
        return Path(env_value), "环境变量 STEAM_DIR"

    saved = settings.get("steam_dir")
    if saved and is_steam_dir(Path(saved)):
        return Path(saved), "上次使用的目录"

    for path in registry_paths():
        if is_steam_dir(path):
            return path, "注册表"

    for raw in COMMON_STEAM_PATHS:
        path = Path(raw)
        if is_steam_dir(path):
            return path, "常见安装位置"

    for path in drive_scan():
        if is_steam_dir(path):
            return path, "磁盘扫描"

    return None, ""
