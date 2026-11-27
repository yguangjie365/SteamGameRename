#!/usr/bin/env python
"""Desktop shell for SteamGameRename: native window + tray + quiet sync.

Wraps the same loopback HTTP service in a pywebview window, adds a tray icon,
and补写已批准但被 Steam 覆盖的汉化 after Steam exits.

Design notes:
* The HTTP layer is untouched (`gui.make_handler`), so the web UI behaves
  identically. Only the window host differs.
* The guard polls `steam_running()` every 30 s instead of using watchdog:
  watchdog can only watch files, and watching APPCache would fire *while*
  Steam writes it — exactly when we must not touch it.
* Missing GUI dependencies are reported with an explicit pip command and a
  non-zero exit (no browser fallback since v0.5.3).

Usage:
    python run_desktop.py
    python run_desktop.py --no-guard
    python run_desktop.py --check
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
for entry in (str(ROOT), str(SRC)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

DEFAULT_GUARD_INTERVAL = 30.0
# 单实例锁端口：只 bind 不监听业务，进程退出后由系统释放。
# 两个实例会各起一个守护线程去补写，进程内的锁拦不住，必须跨进程。
INSTANCE_LOCK_PORT = 17650


def acquire_instance_lock() -> "object | None":
    """Bind the lock port; None means another instance (or a squatter) holds it."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", INSTANCE_LOCK_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


class SearchWindowApi:
    """暴露给前端：在程序内新窗口打开搜索结果，避免在浏览器标签间来回切。"""

    def open_search(self, url: str) -> bool:
        import webview

        # 只接受本程序生成的搜索引擎 URL，防止前端被注入后任意开窗
        allowed = (
            "https://www.bing.com/copilotsearch?q=",
            "https://www.google.com/search?udm=50&q=",
        )
        if not isinstance(url, str) or not any(url.startswith(p) for p in allowed):
            return False
        webview.create_window(
            "名称搜索", url, width=1100, height=820, min_size=(800, 500),
        )
        return True


def missing_desktop_deps() -> str | None:
    try:
        import webview  # noqa: F401
        import pystray  # noqa: F401
        from PIL import Image, ImageDraw  # noqa: F401
    except ImportError as exc:
        return str(exc)
    return None


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


def build_tray(guard: SteamGuard, window, on_quit) -> "object":
    import pystray

    def show_window(icon=None, item=None) -> None:
        try:
            window.show()
            window.restore()
        except Exception:  # noqa: BLE001 - 窗口可能已被销毁
            pass

    def run_sync(icon=None, item=None) -> None:
        changed = guard.sync_once()
        if icon is not None:
            icon.notify(guard.last_result, "SteamGameRename")
        if changed:
            try:
                window.evaluate_js("load(true)")
            except Exception:  # noqa: BLE001
                pass

    menu = pystray.Menu(
        pystray.MenuItem("显示窗口", show_window, default=True),
        pystray.MenuItem("立即静默同步", run_sync),
        pystray.MenuItem("退出", lambda icon, item: on_quit()),
    )
    return pystray.Icon("steam-cn-renamer", tray_image(), "SteamGameRename", menu)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SteamGameRename 桌面版")
    parser.add_argument("--steam-dir", help="Steam 安装目录（默认自动探测）")
    parser.add_argument("--port", type=int, default=0, help="监听端口，0 表示自动分配")
    parser.add_argument(
        "--guard-interval", type=float, default=DEFAULT_GUARD_INTERVAL,
        help="检测 Steam 退出的轮询间隔（秒，最小 5）",
    )
    parser.add_argument("--no-guard", action="store_true", help="关闭自动补写守护")
    parser.add_argument("--no-tray", action="store_true", help="不创建托盘，关闭窗口即退出")
    parser.add_argument(
        "--force-new", action="store_true",
        help="跳过单实例检测（确认没有其它实例时使用）",
    )
    parser.add_argument("--check", action="store_true", help="只校验依赖与环境后退出")
    args = parser.parse_args(argv)

    import steam_locator as locator

    missing = missing_desktop_deps()
    if missing:
        print(f"[!] 桌面依赖缺失（{missing}）")
        print("    安装：pip install pywebview pystray Pillow")
        print("    安装后重新运行本程序。")
        return 1

    # 单实例：两个进程各有一个守护线程，进程内锁拦不住并发写分片
    instance_lock = None
    if not args.check and not args.force_new:
        instance_lock = acquire_instance_lock()
        if instance_lock is None:
            print("[i] SteamGameRename 已在运行（看右下角托盘里的蓝色 S 图标）。")
            print(f"    若确认没有实例，可能是端口 {INSTANCE_LOCK_PORT} 被占用，")
            print("    可加 --force-new 跳过检测。")
            return 0

    settings = locator.load_settings()
    steam_dir, source = locator.detect_steam_dir(args.steam_dir, settings)
    if steam_dir is None:
        print("[!] 未找到 Steam 安装目录，请用 --steam-dir 指定")
        return 1

    from http.server import ThreadingHTTPServer

    from gui import make_handler
    from paths import DEFAULT_BACKUPS, DEFAULT_CONFIG, ensure_config

    config_dir = ensure_config(DEFAULT_CONFIG)
    backup_root = Path(DEFAULT_BACKUPS)

    write_lock = threading.Lock()
    # 窗口要在拿到端口后才建，所以退出回调延迟到建好再填。
    # 前端点「退出服务」时走这里，直接收掉窗口与托盘，不留空壳进程。
    quit_ref: dict[str, object] = {"fn": None}

    def request_quit() -> None:
        action = quit_ref["fn"]
        if callable(action):
            action()

    handler = make_handler(
        str(steam_dir), config_dir, backup_root, write_lock,
        on_shutdown=request_quit,
    )
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    except OSError as exc:
        print(f"[!] 无法监听 127.0.0.1:{args.port}: {exc}")
        return 1
    port = server.server_port
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()

    # 后台沉淀英文原名（与 run.py 行为一致，失败不影响主流程）
    def learn() -> None:
        try:
            from mapping import learn_original_names

            learn_original_names(Path(steam_dir) / "APPCache" / "APPInfo", config_dir)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=learn, name="learn", daemon=True).start()

    guard = SteamGuard(steam_dir, config_dir, backup_root, write_lock, args.guard_interval)

    if args.check:
        print(f"[check] 依赖 OK")
        print(f"[check] Steam 目录: {steam_dir}（{source}）")
        print(f"[check] 配置目录: {config_dir}")
        print(f"[check] 服务端口: {port}")
        server.shutdown()
        server.server_close()
        return 0

    if not args.no_guard:
        guard.start()

    import webview

    url = f"http://127.0.0.1:{port}/"
    window = webview.create_window(
        "SteamGameRename", url,
        width=1200, height=840, min_size=(900, 600),
        js_api=SearchWindowApi(),
    )

    icon = None
    quitting = threading.Event()

    def shutdown() -> None:
        if quitting.is_set():
            return
        quitting.set()
        guard.stop()
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass
        server.shutdown()
        server.server_close()

    def quit_app(*_args) -> None:
        shutdown()
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    # 让 /api/shutdown（网页里的「退出服务」）能真正结束整个程序
    quit_ref["fn"] = quit_app

    if not args.no_tray:
        icon = build_tray(guard, window, quit_app)

        def on_closing() -> bool:
            # 关闭按钮 = 收进托盘。但主动退出（托盘菜单 / 网页「退出服务」）时
            # 必须放行：window.destroy() 也会触发本事件，返回 False 会把它取消。
            if quitting.is_set():
                return True
            try:
                window.hide()
            except Exception:  # noqa: BLE001
                pass
            return False

        window.events.closing += on_closing
        try:
            icon.run_detached()
        except Exception:  # noqa: BLE001 - 托盘不可用时降级为普通窗口
            print("[warn] 托盘启动失败，关闭窗口将直接退出")
            icon = None

    try:
        webview.start()
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 无法创建窗口（{exc}）")
        print(f"    服务地址：{url}")
        shutdown()
        return 1
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
