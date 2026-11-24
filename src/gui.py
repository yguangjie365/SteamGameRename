"""Local web editor for names.json (loopback-only HTTP, no data leaves the box)."""
from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from backup import (
    BackupError,
    apply_changes,
    list_backups,
    restore_backup,
    steam_running,
    steam_shutdown,
    steam_start,
)
from community import update_community_names
from paths import DEFAULT_BACKUPS, DEFAULT_CONFIG, asset_dir, ensure_config
from store import fetch_store_names
from version import __version__
from mapping import (
    TARGET_HINT,
    approved_changes,
    is_allowed_target,
    learn_original_names,
    load_names,
    pending_changes,
    plan_from_config,
    save_names,
)
from vdf import VDFError, validate_name

WEB_DIR = asset_dir("web")

# /api/plan 每次要重解析 247 个分片（约 200ms）。按"分片与配置文件的
# (name, mtime, size)"做签名缓存：写盘/保存后台账任一变化都会自动失效。
_plan_cache: dict[str, tuple[tuple, bytes]] = {}
_plan_lock = threading.Lock()


def _source_signature(appinfo: Path, config_dir: Path) -> tuple:
    paths = sorted(appinfo.glob("appinfo_*.vdf"))
    paths += [
        config_dir / name
        for name in ("names.json", "rules.json", "store_cache.json",
                     "community_names.json", "community_names.csv",
                     "game_names.csv")
    ]
    paths += [
        asset_dir("defaults") / "game_names.csv",
        asset_dir("defaults") / "original_names.csv",
    ]
    entries = []
    for path in paths:
        try:
            stat = path.stat()
            entries.append((path.name, stat.st_mtime_ns, stat.st_size))
        except OSError:
            entries.append((path.name, 0, 0))
    return (str(appinfo), str(config_dir), tuple(entries))


def plan_payload(appinfo: Path, config_dir: Path) -> bytes:
    key = f"{appinfo}|{config_dir}"
    signature = _source_signature(appinfo, config_dir)
    with _plan_lock:
        cached = _plan_cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    payload = json.dumps(
        [item.to_dict() for item in plan_from_config(appinfo, config_dir)],
        ensure_ascii=False,
    ).encode("utf-8")
    with _plan_lock:
        _plan_cache[key] = (signature, payload)
    return payload


def load_page() -> str:
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


def make_handler(
    steam_dir: str,
    config_dir: Path,
    backup_root: Path = DEFAULT_BACKUPS,
    write_lock: threading.Lock | None = None,
):
    """Build the request handler.

    ``write_lock`` lets an embedder (the desktop shell) share one lock with its
    own background writer, so a tray-triggered sync can never race the web UI.
    """
    appinfo = Path(steam_dir) / "APPCache" / "APPInfo"
    names_path = config_dir / "names.json"
    backups = Path(backup_root)
    lock = write_lock if write_lock is not None else threading.Lock()

    # 联网任务进度：由后台线程更新，前端通过 /api/task 轮询
    task_lock = threading.Lock()
    task_state: dict = {
        "running": False, "kind": "", "done": 0, "total": 0,
        "message": "", "error": "",
    }

    def _start_task(kind: str, worker) -> None:
        with task_lock:
            if task_state["running"]:
                return
            task_state.update(
                running=True, kind=kind, done=0, total=0, message="启动中…", error=""
            )

        def _run() -> None:
            try:
                worker()
            except Exception as exc:
                with task_lock:
                    task_state.update(running=False, error=str(exc),
                                      message="任务失败")
            finally:
                with task_lock:
                    task_state["running"] = False

        threading.Thread(target=_run, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self._responded = True
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _check_origin(self) -> bool:
            port = self.server.server_port
            allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host", "") not in allowed_hosts:
                self._json(403, {"error": "invalid host"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {
                f"http://{host}" for host in allowed_hosts
            }:
                self._json(403, {"error": "invalid origin"})
                return False
            return True

        def do_GET(self) -> None:
            self._guard(self._handle_get)

        def do_POST(self) -> None:
            self._guard(self._handle_post)

        def _guard(self, handler) -> None:
            self._responded = False
            try:
                handler()
            except Exception as exc:  # never leak a traceback into the socket
                if getattr(self, "_responded", False):
                    return  # 响应已写出，再发 500 会让客户端收到两个响应
                try:
                    self._json(500, {"error": f"内部错误: {exc}"})
                except Exception:
                    pass

        def _handle_get(self) -> None:
            if not self._check_origin():
                return
            path = urlparse(self.path).path
            if path == "/":
                self._send(200, load_page().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/status":
                self._json(200, {
                    "app": "steam-cn-renamer",
                    "version": __version__,
                    "steam_running": steam_running(),
                    "port": self.server.server_port,
                    "steam_dir": str(steam_dir),
                    "config_dir": str(config_dir),
                })
                return
            if path == "/api/defaults":
                try:
                    with lock:
                        data = load_names(names_path)
                    self._json(200, {"defaults": data.get("defaults", {})})
                except (OSError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                return
            if path == "/api/backups":
                self._json(200, {"backups": list_backups(backups)})
                return
            if path == "/api/task":
                with task_lock:
                    self._json(200, dict(task_state))
                return
            if path == "/api/plan":
                try:
                    self._send(200, plan_payload(appinfo, config_dir),
                               "application/json; charset=utf-8")
                except FileNotFoundError as exc:
                    self._json(400, {"error": str(exc)})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                return
            self._send(404, b"not found", "text/plain")

        def _json(self, code: int, payload: dict) -> None:
            self._send(
                code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _read_json(self) -> dict | None:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except (TypeError, ValueError):
                self._json(400, {"error": "Content-Length 不是合法数字"})
                return None
            if length <= 0 or length > 5_000_000:
                self._json(400, {"error": "invalid content length"})
                return None
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json(400, {"error": "invalid JSON"})
                return None
            if not isinstance(payload, dict):
                self._json(400, {"error": "请求体必须是 JSON 对象"})
                return None
            return payload

        def _handle_post(self) -> None:
            if not self._check_origin():
                return
            path = urlparse(self.path).path
            if path == "/api/save":
                body = self._read_json()
                if body is None:
                    return
                if not isinstance(body.get("entries"), list):
                    self._json(400, {"error": "entries must be a list"})
                    return
                fields = (
                    "shard", "original_name", "original_localized", "base_name",
                    "applied_rules", "desired_name", "source", "targets",
                    "status", "approved", "manual_override", "note",
                    "app_type", "type_raw",
                )
                with lock:
                    data = load_names(names_path)
                    entries = data.get("entries", {})
                    for item in body["entries"]:
                        if not isinstance(item, dict):
                            continue
                        appid = str(item.get("appid", ""))
                        if not appid.isdigit():
                            continue
                        targets = item.get("targets")
                        if targets is not None and (
                            not isinstance(targets, list)
                            or not all(isinstance(t, str) and is_allowed_target(t)
                                       for t in targets)
                        ):
                            self._json(400, {
                                "error": f"appid {appid}: targets 非法（{TARGET_HINT}）"
                            })
                            return
                        desired = item.get("desired_name")
                        if isinstance(desired, str) and desired:
                            try:
                                validate_name(desired)
                            except VDFError as exc:
                                self._json(400, {
                                    "error": f"appid {appid}: {exc}"
                                })
                                return
                        entries[appid] = {
                            key: item[key] for key in fields if key in item
                        }
                    data["entries"] = entries
                    summary = data.setdefault("summary", {})
                    summary["entries_in_file"] = len(entries)
                    save_names(data, names_path)
                self._json(200, {"message": f"已保存 {len(entries)} 条"})
                return

            if path == "/api/defaults":
                body = self._read_json()
                if body is None:
                    return
                defaults = body.get("defaults")
                if not isinstance(defaults, dict):
                    self._json(400, {"error": "defaults must be an object"})
                    return
                allowed = {
                    "review_required", "primary_target", "sync_existing_localized",
                    "create_missing_localized", "write_sortas_pinyin",
                    "mapping_priority",
                }
                bad_keys = [key for key in defaults if key not in allowed]
                if bad_keys:
                    self._json(400, {"error": f"不允许的默认字段: {bad_keys}"})
                    return
                langs = defaults.get("sync_existing_localized")
                if langs is not None and (
                    not isinstance(langs, list)
                    or not all(
                        isinstance(x, str) and x.isascii()
                        and x.replace("_", "").isalnum() for x in langs
                    )
                ):
                    self._json(400, {"error": "sync_existing_localized 必须是语言代码列表"})
                    return
                for flag in (
                    "review_required", "create_missing_localized",
                    "write_sortas_pinyin",
                ):
                    if flag in defaults and not isinstance(defaults[flag], bool):
                        self._json(400, {"error": f"{flag} 必须是布尔值"})
                        return
                if "primary_target" in defaults and not is_allowed_target(
                    defaults["primary_target"]
                ):
                    self._json(400, {
                        "error": f"primary_target 非法（{TARGET_HINT}）"
                    })
                    return
                priority = defaults.get("mapping_priority")
                if priority is not None and (
                    not isinstance(priority, list)
                    or not all(isinstance(x, str) for x in priority)
                ):
                    self._json(400, {"error": "mapping_priority 必须是字符串列表"})
                    return
                try:
                    with lock:
                        data = load_names(names_path)
                        data.setdefault("defaults", {}).update(defaults)
                        save_names(data, names_path)
                        _plan_cache.clear()
                except (OSError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, {
                    "message": "写入策略已保存",
                    "defaults": data["defaults"],
                })
                return

            if path == "/api/update-community":
                with task_lock:
                    if task_state["running"]:
                        self._json(409, {"error": "已有联网任务在运行"})
                        return

                def _update_worker() -> None:
                    with task_lock:
                        task_state["message"] = "正在下载社区词库…"
                    count = update_community_names(
                        config_dir / "community_names.json"
                    )
                    with task_lock:
                        task_state.update(message=f"完成，共 {count} 条", done=count)
                    # 词库缓存已变化，让 /api/plan 的签名失效
                    with _plan_lock:
                        _plan_cache.clear()

                _start_task("update-community", _update_worker)
                self._json(202, {"message": "已开始更新社区词库"})
                return

            if path == "/api/fetch-store":
                body = self._read_json() or {}
                limit = body.get("limit", 50)
                if not isinstance(limit, int) or not (1 <= limit <= 500):
                    self._json(400, {"error": "limit 必须在 1-500 之间"})
                    return
                with task_lock:
                    if task_state["running"]:
                        self._json(409, {"error": "已有联网任务在运行"})
                        return

                def _fetch_worker() -> None:
                    from shard import app_names
                    from mapping import scan_shards
                    needs: list[int] = []
                    for shard, entry in scan_shards(appinfo):
                        _, localized = app_names(shard, entry)
                        if not localized.get("schinese") and not localized.get("sc_schinese"):
                            needs.append(entry.appid)
                        if len(needs) >= limit:
                            break
                    with task_lock:
                        task_state.update(total=len(needs), message=f"准备抓取 {len(needs)} 个…")
                    found = fetch_store_names(
                        needs, config_dir / "store_cache.json"
                    )
                    with task_lock:
                        task_state.update(
                            done=len(found), message=f"从商店补全 {len(found)} 个中文名"
                        )
                    with _plan_lock:
                        _plan_cache.clear()

                _start_task("fetch-store", _fetch_worker)
                self._json(202, {"message": "已开始补全商店中文名"})
                return

            if path == "/api/sync":
                # 兼容旧前端的无请求体 POST；新前端可传 {"auto_restart": true}
                body: dict | None = {}
                try:
                    if int(self.headers.get("Content-Length", "0")) > 0:
                        body = self._read_json()
                except (TypeError, ValueError):
                    self._json(400, {"error": "invalid content length"})
                    return
                if body is None:
                    return
                auto_restart = bool(body.get("auto_restart", False))
                steam_exe = Path(steam_dir) / "steam.exe"
                needs_restart = False
                report = None
                message = ""
                restarted = False
                try:
                    with lock:
                        if steam_running(ttl=0):
                            if auto_restart:
                                if not steam_shutdown(steam_exe):
                                    self._json(409, {
                                        "error": "无法优雅关闭 Steam，已中止同步；"
                                                 "请手动退出后重试"
                                    })
                                    return
                                needs_restart = True
                            else:
                                self._json(409, {
                                    "error": "Steam 正在运行，请先关闭客户端；"
                                             "或选择自动关闭并重启"
                                })
                                return
                        plan = plan_from_config(
                            appinfo, config_dir, include_no_zh=False
                        )
                        changes = pending_changes(plan)
                        if not changes:
                            message = "无需同步：所有已批准名称都已在文件中"
                        else:
                            report = apply_changes(
                                appinfo, changes, backups,
                                require_steam_closed=not needs_restart,
                            )
                            message = f"已同步 {len(report['changed'])} 个分片"
                except (BackupError, ValueError) as exc:
                    if needs_restart:
                        steam_start(steam_exe)
                    self._json(400, {"error": str(exc)})
                    return
                except Exception as exc:
                    if needs_restart:
                        steam_start(steam_exe)
                    self._json(500, {"error": str(exc)})
                    return
                # 即使无需同步，只要关过 Steam 也要拉起，绝不把用户留在关闭状态
                if needs_restart:
                    restarted = steam_start(steam_exe)
                self._json(200, {
                    "message": message,
                    "changed": report["changed"] if report else [],
                    "backup": report["backup"] if report else None,
                    "restarted": restarted,
                })
                return

            if path == "/api/apply":
                try:
                    with lock:
                        if steam_running(ttl=0):
                            self._json(409, {"error": "Steam 正在运行，请先完全关闭客户端"})
                            return
                        plan = plan_from_config(
                            appinfo, config_dir, include_no_zh=False
                        )
                        changes = approved_changes(plan)
                        if not changes:
                            self._json(400, {"error": "没有已批准的改名项"})
                            return
                        report = apply_changes(
                            appinfo, changes, backups, require_steam_closed=True
                        )
                    self._json(200, {
                        "message": f"已写入 {len(report['changed'])} 个分片",
                        "changed": report["changed"],
                        "backup": report["backup"],
                        "backup_size": report.get("backup_size", 0),
                    })
                except (BackupError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return

            if path == "/api/apply-safe":
                body = self._read_json()
                if body is None:
                    return
                if not steam_running(ttl=0):
                    self._json(400, {"error": "Steam 未运行，请直接用「应用写入」"})
                    return
                exe = Path(steam_dir) / "steam.exe"
                try:
                    with lock:
                        # 1) 优雅关闭；失败则中止，绝不强行 kill
                        if not steam_shutdown(exe):
                            self._json(409, {
                                "error": "无法优雅关闭 Steam，已中止写入；"
                                         "请手动退出后重试"
                            })
                            return
                        # 2) 写入（Steam 已确认退出）
                        plan = plan_from_config(
                            appinfo, config_dir, include_no_zh=False
                        )
                        changes = approved_changes(plan)
                        if not changes:
                            self._json(400, {"error": "没有已批准的改名项"})
                            return
                        report = apply_changes(
                            appinfo, changes, backups, require_steam_closed=False
                        )
                        # 3) 写入成功后重启
                        restarted = steam_start(exe)
                    self._json(200, {
                        "message": f"已写入 {len(report['changed'])} 个分片",
                        "changed": report["changed"],
                        "backup": report["backup"],
                        "restarted": restarted,
                    })
                except (BackupError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return

            if path == "/api/restore":
                body = self._read_json()
                if body is None:
                    return
                name = str(body.get("backup", ""))
                if not name or name != Path(name).name or name in (".", ".."):
                    self._json(400, {"error": "备份名称不合法"})
                    return
                folder = backups / name
                if not (folder / "manifest.json").exists():
                    self._json(404, {"error": f"找不到备份 {name}"})
                    return
                try:
                    with lock:
                        count = restore_backup(
                            folder, appinfo, require_steam_closed=True
                        )
                    self._json(200, {
                        "message": f"已从 {name} 恢复 {count} 个分片",
                        "restored": count,
                    })
                except BackupError as exc:
                    self._json(409 if "Steam is running" in str(exc) else 400,
                               {"error": str(exc)})
                except Exception as exc:
                    self._json(500, {"error": str(exc)})
                return

            if path == "/api/shutdown":
                self._json(200, {"message": "服务已停止，可以关闭此页面"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            self._send(404, b"not found", "text/plain")

    return Handler


def run_gui(steam_dir: str = r"D:\Steam", config_dir: Path = DEFAULT_CONFIG,
            port: int = 8765, open_browser: bool = True,
            backup_root: Path = DEFAULT_BACKUPS) -> None:
    ensure_config(config_dir)
    config_path = Path(config_dir)
    appinfo_path = Path(steam_dir) / "APPCache" / "APPInfo"

    def _background_learn() -> None:
        try:
            learn_original_names(appinfo_path, config_path)
        except Exception:
            pass  # 学习是纯展示增强，失败绝不影响主流程

    threading.Thread(target=_background_learn, daemon=True).start()
    handler = make_handler(steam_dir, config_path, Path(backup_root))
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        print(f"无法监听 127.0.0.1:{port}: {exc}")
        return
    url = f"http://127.0.0.1:{port}/"
    print(f"编辑器地址: {url}")
    print("停止服务: 页面右上角“退出服务”，或在本窗口按 Ctrl+C")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    run_gui()


if __name__ == "__main__":
    main()
