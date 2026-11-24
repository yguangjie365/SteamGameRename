#!/usr/bin/env python
"""One-click launcher for the Steam Chinese-name editor (flat src layout).

Double-clicking 启动编辑器.bat calls this file, so no PYTHONPATH or
module flags are needed. Behaviour:

* locates the Steam install (argument -> launcher.json -> registry -> common paths)
* reuses an already running editor instead of failing on a busy port
* otherwise picks the first free port starting from 8765
* opens the browser once the server is listening

Usage:
    python run.py
    python run.py --no-browser
    python run.py --steam-dir "E:\\Steam" --port 8800
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SETTINGS_FILE = ROOT / "launcher.json"
APP_MARKER = "steam-cn-renamer"
DEFAULT_PORT = 8765
PORT_TRIES = 20

COMMON_STEAM_PATHS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"C:\Steam",
    r"D:\Steam",
    r"D:\Program Files (x86)\Steam",
    r"E:\Steam",
    r"F:\Steam",
)


# ---------------------------------------------------------------- settings
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
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


# ------------------------------------------------------- steam discovery
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


# ------------------------------------------------------------- ports/http
def probe(port: int) -> dict | None:
    url = f"http://127.0.0.1:{port}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def is_running(port: int) -> bool:
    payload = probe(port)
    return bool(payload and payload.get("app") == APP_MARKER)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(preferred: int, settings: dict) -> int | None:
    candidates = [preferred]
    saved = settings.get("port")
    if isinstance(saved, int) and saved not in candidates:
        candidates.append(saved)
    candidates.extend(p for p in range(preferred, preferred + PORT_TRIES))
    seen: set[int] = set()
    for port in candidates:
        if port in seen:
            continue
        seen.add(port)
        if port_free(port):
            return port
    return None


def wait_ready(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running(port):
            return True
        time.sleep(0.15)
    return False


def find_running(preferred: int, settings: dict) -> int | None:
    """Look for an already running editor on the preferred or last used port."""
    candidates = [preferred]
    saved = settings.get("port")
    if isinstance(saved, int):
        candidates.append(saved)
    for port in dict.fromkeys(candidates):
        if is_running(port):
            return port
    return None


# ------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Steam Game Rename 一键启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python run.py                        自动检测 Steam 并打开编辑器\n"
            "  python run.py --steam-dir E:\\Steam    指定 Steam 目录\n"
            "  python run.py --no-browser           只启动服务，不打开浏览器\n"
            "  python run.py --force-new            已有实例时也另起一个\n"
        ),
    )
    parser.add_argument("--steam-dir", help="Steam 安装目录，留空则自动检测")
    parser.add_argument("--port", type=int, help="监听端口，默认 8765")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--force-new", action="store_true", help="已有实例也启动新实例")
    args = parser.parse_args(argv)

    settings = load_settings()
    preferred = args.port or settings.get("port") or DEFAULT_PORT

    running = None if args.force_new else find_running(preferred, settings)
    if running is not None:
        url = f"http://127.0.0.1:{running}/"
        print(f"编辑器已在运行：{url}")
        if not args.no_browser:
            _open(url)
        return 0

    steam_dir, source = detect_steam_dir(args.steam_dir, settings)
    if steam_dir is None:
        hinted = False
        if args.steam_dir:
            print(f"指定的 Steam 目录不可用：{args.steam_dir}")
            if looks_like_library(Path(args.steam_dir)):
                print("该目录像是游戏库，或 Steam 还没生成缓存："
                      "需要客户端根目录（含 APPCache），可先启动一次 Steam。")
                hinted = True
        print("未找到 Steam 安装目录。")
        if not hinted:
            print("若已安装 Steam，先启动一次客户端（生成 APPCache 后即可识别）。")
        return 2

    port = pick_port(preferred, settings)
    if port is None:
        print(f"端口 {preferred}-{preferred + PORT_TRIES} 都被占用，无法启动。")
        return 3

    save_settings({
        "steam_dir": str(steam_dir),
        "port": port,
        "detected_by": source,
    })

    try:
        from gui import run_gui
    except ImportError as exc:  # missing typer/httpx/pydantic etc.
        print(f"缺少依赖，无法启动：{exc}")
        print('请先安装：python -m pip install --index-url https://pypi.org/simple '
              'typer rich httpx pydantic')
        return 4

    print("=" * 52)
    print("  Steam Game Rename")
    print(f"  Steam 目录 : {steam_dir}（{source}）")
    print(f"  编辑器地址 : http://127.0.0.1:{port}/")
    print("  关闭窗口或点页面右上角“退出服务”即可停止")
    print("=" * 52)

    if not args.no_browser:
        import threading

        def _later() -> None:
            if wait_ready(port):
                _open(f"http://127.0.0.1:{port}/")

        threading.Thread(target=_later, daemon=True).start()

    run_gui(str(steam_dir), port=port, open_browser=False)
    print("编辑器已退出。")
    return 0


def _open(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
