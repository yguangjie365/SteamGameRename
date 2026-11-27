#!/usr/bin/env python
"""Browser-mode launcher for SteamGameRename (flat src layout).

Double-clicking SteamGameRename.bat calls this file. Behaviour:

* locates the Steam install via steam_locator (argument -> env -> launcher.json
  -> registry -> common paths -> drive scan)
* reuses an already running editor instead of failing on a busy port
* otherwise picks the first free port starting from 8765
* opens the system browser once the server is listening
* keeps SteamGuard running so approved names are restored after Steam exits

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
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
for entry in (str(ROOT), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from steam_locator import (  # noqa: E402
    detect_steam_dir, load_settings, looks_like_library, save_settings,
)

SETTINGS_FILE = ROOT / "launcher.json"
APP_MARKER = "steam-cn-renamer"
DEFAULT_PORT = 8765
PORT_TRIES = 20
INSTANCE_LOCK_PORT = 17650  # 只 bind 不业务，跨进程单实例
DEFAULT_GUARD_INTERVAL = 30.0

COMMON_STEAM_PATHS = (
    r"C:\Program Files (x86)\Steam",
    r"C:\Program Files\Steam",
    r"C:\Steam",
    r"D:\Steam",
    r"D:\Program Files (x86)\Steam",
    r"E:\Steam",
    r"F:\Steam",
)


class SteamGuard:
    """Watch for the Steam running→exited edge and quietly rewrite names."""

    def __init__(
        self,
        steam_dir: str | Path,
        config_dir: str | Path,
        backup_root: str | Path,
        write_lock: threading.Lock,
        interval: float = DEFAULT_GUARD_INTERVAL,
    ) -> None:
        self._appinfo = Path(steam_dir) / "APPCache" / "APPInfo"
        self._config = Path(config_dir)
        self._backups = Path(backup_root)
        self._lock = write_lock
        self._interval = max(5.0, float(interval))
        self._stop = threading.Event()
        self._was_running: bool | None = None
        self.last_result = "尚未运行"

    def start(self) -> None:
        threading.Thread(target=self._loop, name="steam-guard", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def sync_once(self) -> int:
        """Rewrite approved-but-missing entries. Returns changed shard count."""
        from backup import BackupError, apply_changes, steam_running
        from mapping import pending_changes, plan_from_config

        if steam_running(ttl=0):
            self.last_result = "Steam 运行中，跳过"
            return 0
        try:
            with self._lock:  # 与 Web 的写入路径共用同一把锁
                plan = plan_from_config(self._appinfo, self._config, include_no_zh=False)
                changes = pending_changes(plan)
                if not changes:
                    self.last_result = "无需同步"
                    return 0
                report = apply_changes(
                    self._appinfo, changes, self._backups, require_steam_closed=True
                )
        except (BackupError, OSError, ValueError) as exc:
            self.last_result = f"同步失败: {exc}"
            return 0
        except Exception as exc:  # noqa: BLE001 - 守护线程绝不能因异常退出
            self.last_result = f"同步异常: {exc}"
            return 0
        changed = len(report.get("changed") or [])
        self.last_result = f"已补写 {changed} 个分片"
        return changed

    @staticmethod
    def should_sync(was_running: bool | None, running: bool) -> bool:
        """Only the running→exited edge triggers a rewrite.

        A wrong initial guess must never cause a write, so `was_running=None`
        (unknown) returns False.
        """
        return bool(was_running) and not running

    def _loop(self) -> None:
        from backup import steam_running

        try:
            running = steam_running(ttl=0)
            if not running:
                self.sync_once()  # 启动即补一次：上次被 Steam 覆盖的汉化
            self._was_running = running
        except Exception as exc:  # noqa: BLE001
            self.last_result = f"守护初始化失败: {exc}"
            self._was_running = None

        while not self._stop.wait(self._interval):
            try:
                running = steam_running(ttl=0)
                if self.should_sync(self._was_running, running):
                    self.sync_once()
                self._was_running = running
            except Exception as exc:  # noqa: BLE001
                self.last_result = f"守护异常: {exc}"


def tray_image():
    """Solid rounded square with an S mark — no external asset to ship."""
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, size - 2, size - 2), radius=15, fill=(90, 169, 255, 255))
    try:
        font = ImageFont.load_default(size=38)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    draw.text((20, 10), "S", font=font, fill=(255, 255, 255, 255))
    return image


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


def _acquire_instance_lock() -> "object | None":
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", INSTANCE_LOCK_PORT))
            sock.listen(1)
        except OSError:
            return None
        return sock


# ------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SteamGameRename 一键启动器",
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

    # 跨进程单实例锁：浏览器模式下两个进程会各起一个 Guard 守护线程，
    # 进程内锁拦不住，仍用 17650 端口只 bind 不业务。
    instance_lock = None
    if not args.force_new:
        instance_lock = _acquire_instance_lock()
        if instance_lock is None:
            running = find_running(args.port or DEFAULT_PORT, load_settings())
            url = f"http://127.0.0.1:{running}/" if running else "已有的 SteamGameRename 实例"
            print(f"编辑器已在运行：{url}")
            if running and not args.no_browser:
                _open(url)
            return 0

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
    print("  SteamGameRename")
    print(f"  Steam 目录 : {steam_dir}（{source}）")
    print(f"  编辑器地址 : http://127.0.0.1:{port}/")
    print("  关闭窗口或点页面右上角“退出服务”即可停止")
    print("=" * 52)

    # SteamGuard：Steam 退出后静默补回被覆盖的汉化。浏览器模式下同样需要。
    from paths import DEFAULT_BACKUPS, DEFAULT_CONFIG

    guard = SteamGuard(
        steam_dir, DEFAULT_CONFIG, DEFAULT_BACKUPS, threading.Lock(),
        DEFAULT_GUARD_INTERVAL,
    )
    guard.start()

    if not args.no_browser:
        def _later() -> None:
            if wait_ready(port):
                _open(f"http://127.0.0.1:{port}/")

        threading.Thread(target=_later, daemon=True).start()

    try:
        run_gui(str(steam_dir), config_dir=DEFAULT_CONFIG, backup_root=DEFAULT_BACKUPS,
                port=port, open_browser=False)
    finally:
        guard.stop()
        if instance_lock is not None:
            instance_lock.close()
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
