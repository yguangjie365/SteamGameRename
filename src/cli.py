"""Typer/Rich CLI. All file writes are opt-in via --execute."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

import db as dbmod
from steam_locator import detect_steam_dir, load_settings

from backup import (
    CREATABLE_TARGETS,
    BackupError,
    apply_changes,
    list_backups,
    prune_backups,
    restore_backup,
    steam_running,
    steam_shutdown,
    steam_start,
)
from game_names import check_originals, load_shared_names, write_shared_names_csv
from mapping import (
    approved_changes,
    export_names_json,
    learn_original_names,
    load_names,
    pending_changes,
    plan_from_config,
    scan_shards,
    update_community_names,
)
from paths import (
    DEFAULT_BACKUPS,
    DEFAULT_CONFIG,
    DEFAULTS_DIR,
    default_backup_root,
    default_config_dir,
    ensure_config,
)
from shard import (
    Shard,
    ShardError,
    app_names,
    check_targets,
    parse_file,
    verify as verify_shard,
)

console = Console(legacy_windows=False)

TASK_NAME = "SteamCNRenamer_Sync"


def task_command(steam_dir: str, config_dir: Path, backup_root: Path) -> str:
    """Build the silent task command line (pure function, easy to verify).

    Paths containing quotes or percent signs are rejected: schtasks treats
    '%' as an environment variable and quotes would break the /TR value.
    """
    parts = (steam_dir, str(config_dir), str(backup_root))
    if any('"' in part or "%" in part for part in parts):
        raise ValueError("计划任务路径不能包含引号或百分号")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = Path(__file__).resolve()
    return (
        f'"{pythonw}" "{script}" --steam-dir "{steam_dir}" '
        f'--config-dir "{config_dir}" --backup-root "{backup_root}" '
        "sync --execute --skip-if-running"
    )


class CliExit(Exception):
    """Explicit non-zero exit for commands (typer.Exit is swallowed in
    standalone_mode=False, so `verify` used to always return 0)."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _context(ctx: typer.Context) -> tuple[Path, Path, Path]:
    state = ctx.obj
    if not state.get("steam_dir"):
        raise typer.BadParameter(
            "未能定位 Steam 安装目录；请用 --steam-dir 指定，"
            "或设置环境变量 STEAM_DIR"
        )
    return (
        Path(state["steam_dir"]) / "APPCache" / "APPInfo",
        Path(state["config_dir"]),
        Path(state["backup_root"]),
    )


app = typer.Typer(no_args_is_help=True, help="将 Steam 库游戏名称改为中文（appinfo v42）")


@app.callback()
def global_options(
    ctx: typer.Context,
    steam_dir: str | None = typer.Option(
        None, help="Steam 安装目录；留空则与桌面版一样自动探测"
    ),
    config_dir: str = typer.Option(str(DEFAULT_CONFIG), help="配置目录"),
    backup_root: str = typer.Option(str(DEFAULT_BACKUPS), help="备份根目录"),
) -> None:
    # 显式 --steam-dir 直接采信（调用方自行保证路径正确，测试也常用临时目录）；
    # 未指定时才走桌面版同一条探测链，避免非 D 盘用户每次手填。
    if steam_dir:
        found, source = Path(steam_dir), "命令行参数"
    else:
        found, source = detect_steam_dir(None, load_settings())
    # 探测失败不在这里报错：export-ledger / export-mappings 等纯台账命令
    # 不需要 Steam，只有 _context() 消费 steam_dir 的命令才需要它。
    ctx.obj = {
        "steam_dir": str(found) if found is not None else "",
        "steam_dir_source": source,
        "config_dir": ensure_config(config_dir),
        "backup_root": backup_root,
    }


@app.command()
def scan(ctx: typer.Context) -> None:
    """列出分片中的应用与名称。"""
    appinfo, _, _ = _context(ctx)
    if not appinfo.is_dir():
        raise typer.BadParameter(f"目录不存在: {appinfo}")
    table = Table("AppID", "分片", "原名", "本地中文名")
    for shard, entry in scan_shards(appinfo):
        name, localized = app_names(shard, entry)
        table.add_row(
            str(entry.appid),
            shard.path.name if shard.path else "",
            name,
            " / ".join(f"{k}:{v}" for k, v in localized.items()),
        )
    console.print(table)


@app.command()
def plan(
    ctx: typer.Context,
    all: bool = typer.Option(
        False, "--all", help="包含无需改动（unchanged）与无中文（no_zh）的条目"
    ),
    as_json: bool = typer.Option(False, "--json", help="输出 JSON"),
    app_type: str = typer.Option("", "--type", help="筛选: game/music/application/tool/other"),
) -> None:
    """生成改名计划，默认只预览。"""
    appinfo, config_dir, _ = _context(ctx)
    items = plan_from_config(
        appinfo, config_dir, include_no_zh=all, include_unchanged=all
    )
    if app_type:
        items = [item for item in items if item.app_type == app_type]
    if as_json:
        console.print_json(json.dumps([i.to_dict() for i in items], ensure_ascii=False))
        return
    table = Table("批准", "AppID", "类型", "原名", "建议名称", "自定义名称", "状态")
    for item in items:
        table.add_row(
            "✔" if item.approved else "—",
            str(item.appid), item.app_type, item.original_name, item.base_name,
            item.custom_name, item.status,
        )
    console.print(table)
    console.print(f"建议 {len(items)} 条，其中已批准 {sum(1 for i in items if i.approved)} 条")


@app.command()
def verify(ctx: typer.Context) -> None:
    """校验所有分片完整性。"""
    appinfo, _, _ = _context(ctx)
    problems = 0
    for path in sorted(appinfo.glob("appinfo_*.vdf")):
        try:
            shard = parse_file(path)
        except (ShardError, OSError) as exc:
            problems += 1
            console.print(f"[red]{path.name}: 解析失败: {exc}[/red]")
            continue
        found = verify_shard(shard)
        problems += len(found)
        for problem in found:
            console.print(f"[red]{path.name}: {problem}[/red]")
    console.print(f"校验完成，问题数: {problems}")
    if problems:
        raise CliExit(1)


@app.command()
def apply(
    ctx: typer.Context,
    execute: bool = typer.Option(False, "--execute", help="真正写入文件"),
    allow_running: bool = typer.Option(
        False, "--allow-running", help="允许 Steam 运行时写入（不推荐）"
    ),
) -> None:
    """应用已批准的名称；默认 dry-run。"""
    appinfo, config_dir, backup_root = _context(ctx)
    # 故意带上 no_zh：unsupported 行只在 include_no_zh=True 时产出，
    # 而 approved_changes 会跳过它们，所以不影响实际写入内容。
    items = plan_from_config(appinfo, config_dir)
    changes = approved_changes(items)
    if not changes:
        console.print("没有已批准的改名项；请先在 names.json 中批准或使用编辑器")
        return
    unsupported = [i for i in items if i.status == "unsupported"]
    if unsupported and execute:
        console.print(
            f"[yellow]跳过 {len(unsupported)} 条缺少名称字段的记录："
            f"{', '.join(str(i.appid) for i in unsupported[:5])}[/yellow]"
        )
    target_problems: list[str] = []
    parsed_shards: dict[str, Shard] = {}
    for shard_name, apps in changes.items():
        shard = parse_file(appinfo / shard_name)
        parsed_shards[shard_name] = shard
        target_problems += [
            f"{shard_name}: {problem}" for problem in check_targets(
                shard, apps, allow_create=CREATABLE_TARGETS
            )
        ]
    if target_problems:
        for problem in target_problems:
            console.print(f"[red]{problem}[/red]")
        raise typer.BadParameter("目标字段不存在，请检查 names.json 的 targets 配置")
    table = Table("分片", "AppID", "写入目标")
    for shard_name, apps in changes.items():
        for appid, targets in apps.items():
            table.add_row(shard_name, str(appid), json.dumps(targets, ensure_ascii=False))
    console.print(table)
    if not execute:
        console.print("[yellow]DRY RUN：未写入文件。加 --execute 才会备份并写入。[/yellow]")
        return
    if steam_running(ttl=0) and not allow_running:
        raise typer.BadParameter("Steam 正在运行，请先关闭客户端")
    report = apply_changes(
        appinfo, changes, backup_root, require_steam_closed=not allow_running,
        parsed_shards=parsed_shards,
    )
    console.print(f"[green]已写入: {', '.join(report['changed'])}[/green]")
    console.print(
        f"备份位置: {report['backup']}"
        f"（{report.get('backup_size', 0) / 1e6:.2f} MB）"
    )


@app.command()
def sync(
    ctx: typer.Context,
    execute: bool = typer.Option(False, "--execute", help="真正写入文件"),
    allow_running: bool = typer.Option(
        False, "--allow-running", help="允许 Steam 运行时写入（不推荐）"
    ),
    auto_restart: bool = typer.Option(
        False, "--auto-restart",
        help="Steam 运行时先优雅关闭、写入后自动重启（仅 --execute 时生效）",
    ),
    skip_if_running: bool = typer.Option(
        False, "--skip-if-running",
        help="Steam 运行时安静跳过并以 0 退出（供计划任务使用）",
    ),
) -> None:
    """静默同步：只补回 Steam 更新后丢失的已批准名称；默认 dry-run。"""
    appinfo, config_dir, backup_root = _context(ctx)
    items = plan_from_config(appinfo, config_dir)
    changes = pending_changes(items)
    if not changes:
        console.print("[green]无需同步：所有已批准名称都已在文件中。[/green]")
        return
    count = sum(len(apps) for apps in changes.values())
    console.print(f"待同步 {count} 条（已批准但未生效），涉及 {len(changes)} 个分片")
    if not execute:
        console.print("[yellow]DRY RUN：加 --execute 才会备份并写入。[/yellow]")
        return
    problems: list[str] = []
    parsed_shards: dict[str, Shard] = {}
    for shard_name, apps in changes.items():
        shard = parse_file(appinfo / shard_name)
        parsed_shards[shard_name] = shard
        problems += [
            f"{shard_name}: {problem}" for problem in check_targets(
                shard, apps, allow_create=CREATABLE_TARGETS
            )
        ]
    if problems:
        for problem in problems:
            console.print(f"[red]{problem}[/red]")
        raise typer.BadParameter("目标字段不存在，无法同步")

    steam_exe = Path(ctx.obj["steam_dir"]) / "steam.exe"
    was_running = steam_running(ttl=0)
    needs_restart = False
    if was_running:
        if skip_if_running:
            console.print("[yellow]Steam 正在运行，跳过本次同步（计划任务模式）。[/yellow]")
            return
        if auto_restart:
            console.print("[yellow]正在优雅关闭 Steam…[/yellow]")
            if not steam_shutdown(steam_exe):
                raise typer.BadParameter(
                    "无法优雅关闭 Steam，已中止同步；请手动退出后重试"
                )
            needs_restart = True
        elif not allow_running:
            raise typer.BadParameter(
                "Steam 正在运行，请先关闭客户端，或加 --auto-restart"
            )
    try:
        report = apply_changes(
            appinfo, changes, backup_root,
            require_steam_closed=not (allow_running or needs_restart),
            parsed_shards=parsed_shards,
        )
    finally:
        # 关闭后即使写入失败也要拉起 Steam，绝不把用户留在"手动关了客户端"的状态
        if needs_restart:
            steam_start(steam_exe)
    suffix = "，已自动重启 Steam" if needs_restart else ""
    console.print(
        f"[green]同步完成：已写回 {len(report['changed'])} 个分片，"
        f"备份 {report['backup']}{suffix}[/green]"
    )


@app.command("learn-originals")
def learn_originals(ctx: typer.Context) -> None:
    """扫描分片，把本机独有的英文原名沉淀到本地补充表（只读展示用）。"""
    appinfo, config_dir, _ = _context(ctx)
    new, total = learn_original_names(appinfo, config_dir)
    console.print(f"新增 {new} 条英文名，本地补充表现有 {total} 条")


@app.command()
def restore(
    ctx: typer.Context,
    backup: str = typer.Option(..., "--backup", help="备份目录"),
    allow_running: bool = typer.Option(
        False, "--allow-running", help="允许 Steam 运行时恢复（不推荐）"
    ),
) -> None:
    """从备份恢复分片。"""
    appinfo, _, _ = _context(ctx)
    count = restore_backup(
        backup, appinfo, require_steam_closed=not allow_running
    )
    console.print(f"已从 {backup} 恢复 {count} 个分片")


@app.command()
def backups(ctx: typer.Context) -> None:
    """列出可用备份（新的在前）。"""
    _, _, backup_root = _context(ctx)
    items = list_backups(backup_root)
    if not items:
        console.print(f"{backup_root} 下没有可用备份")
        return
    table = Table("备份", "创建时间", "分片数", "体积")
    total = 0
    for item in items:
        total += item["size"]
        table.add_row(
            item["name"], item["created_at"], str(item["files"]),
            f"{item['size'] / 1e6:.2f} MB",
        )
    console.print(table)
    console.print(f"共 {len(items)} 份，合计 {total / 1e6:.2f} MB")


@app.command("prune-backups")
def prune_backups_cmd(
    ctx: typer.Context,
    keep: int = typer.Option(8, "--keep", help="保留最新的份数"),
) -> None:
    """删除较旧的备份，只保留最近 N 份。"""
    _, _, backup_root = _context(ctx)
    removed = prune_backups(backup_root, keep)
    if not removed:
        console.print(f"无需清理（保留 {keep} 份即可）")
        return
    console.print(f"已删除 {len(removed)} 份旧备份: {', '.join(removed)}")


@app.command("update-community")
def update_community(ctx: typer.Context) -> None:
    """联网更新社区中文名词库（GitHub 直连失败自动切换国内镜像）。"""
    config_dir = Path(ctx.obj["config_dir"])  # 纯联网命令，不依赖 Steam 目录
    count, source = update_community_names(config_dir / "community_names.json")
    via = "直连" if source.startswith("https://raw.githubusercontent.com") else "国内镜像"
    console.print(f"已更新社区词库，共 {count} 条（经{via}：{source}）")


@app.command("import-applist")
def import_applist(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="steamappidlist 的 games_appid.json 路径"),
    all_apps: bool = typer.Option(
        False, "--all", help="导入全部 AppID（默认只导入本机分片与台账中出现的）"
    ),
) -> None:
    """从全量 AppID 列表补充英文原名基线（只补缺失，不覆盖已有条目）。

    数据源如 github.com/steamsync/steamappidlist 的 data/games_appid.json
    （Steam 官方 GetAppList 下线后的社区替代源）。替代 §12.4 移除的
    自动更新途径：为基线未覆盖的游戏（特别是新装的已汉化游戏）提供
    英文原名对照。
    """
    source = Path(path)
    if not source.is_file():
        raise typer.BadParameter(f"找不到输入文件：{source}")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"读取或解析失败：{exc}") from exc
    # 兼容两种形态：[{appid, name, ...}] 数组 与 {appid: name} 映射
    if isinstance(raw, dict):
        records = {str(k): str(v) for k, v in raw.items()
                   if isinstance(v, str) and str(k).isdigit()}
    elif isinstance(raw, list):
        records = {
            str(item["appid"]): str(item["name"])
            for item in raw
            if isinstance(item, dict) and str(item.get("appid", "")).isdigit()
            and isinstance(item.get("name"), str)
        }
    else:
        raise typer.BadParameter("JSON 顶层必须是数组或对象")
    if not records:
        raise typer.BadParameter("没有可用的 AppID 条目")

    appinfo, config_dir, _ = _context(ctx)
    scope: set[int] | None = None
    if not all_apps:
        scope = set()
        from mapping import scan_shards  # 复用增量缓存
        for shard, entry in scan_shards(appinfo):
            scope.add(entry.appid)
        conn = dbmod.connect(dbmod.db_path(Path(config_dir)))
        try:
            scope.update(
                int(row["appid"]) for row in conn.execute("SELECT appid FROM ledger")
            )
        finally:
            conn.close()
        console.print(f"本机范围：{len(scope)} 个 AppID（--all 可导入全量）")

    conn = dbmod.connect(dbmod.db_path(Path(config_dir)))
    try:
        inserted = dbmod.import_appids_baseline(conn, records, scope)
        total = conn.execute(
            "SELECT COUNT(*) FROM original_names WHERE source='baseline'"
        ).fetchone()[0]
    finally:
        conn.close()
    console.print(
        f"已导入 {inserted} 条新条目（已有条目保持不变）；"
        f"基线现共 {total} 条"
    )


@app.command("export-ledger")
def export_ledger(
    ctx: typer.Context,
    output: str = typer.Option("names-export.json", "--output", "-o", help="输出 JSON 路径"),
) -> None:
    """把 SQLite 台账导出为可读 JSON 备份。"""
    config_dir = Path(ctx.obj["config_dir"])  # 纯台账命令，不依赖 Steam 目录
    data = load_names(config_dir / "names.json")
    dest = Path(output)
    if dest.exists():
        raise typer.BadParameter(f"文件已存在，请指定其他路径：{dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    export_names_json(data, dest)
    console.print(f"已导出 {len(data.get('entries') or {})} 条到 {dest}")


@app.command("export-mappings")
def export_mappings(
    ctx: typer.Context,
    output: str = typer.Option("game_names.csv", "--output", "-o", help="输出 CSV 路径"),
    approved_only: bool = typer.Option(
        True, "--approved-only/--all", help="仅导出已批准条目"
    ),
) -> None:
    """把台账里的中文名导出为共享词库 CSV（UTF-8 BOM，Excel 可直接编辑）。"""
    config_dir = Path(ctx.obj["config_dir"])  # 纯台账命令，不依赖 Steam 目录
    data = load_names(config_dir / "names.json")
    rows: list[tuple[int, str, str, str]] = []
    for key, item in data.get("entries", {}).items():
        if not key.isdigit() or (approved_only and not item.get("approved")):
            continue
        modified = (item.get("desired_name") or "").strip()
        if not modified:
            continue
        rows.append((
            int(key), item.get("original_name") or "", modified,
            item.get("source") or "",
        ))
    rows.sort()
    dest = Path(output)
    if dest.exists():
        raise typer.BadParameter(f"文件已存在，请指定其他路径：{dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = write_shared_names_csv(rows, dest)
    console.print(f"已导出 {count} 条到 {dest}")


@app.command("validate-mappings")
def validate_mappings(ctx: typer.Context) -> None:
    """校验共享词库：重复 GameID、非法行、原名是否与 Steam 数据一致。"""
    appinfo, config_dir, _ = _context(ctx)
    shared = load_shared_names(
        DEFAULTS_DIR / "game_names.csv", config_dir / "game_names.csv"
    )
    actual: dict[int, str] = {}
    if appinfo.is_dir():
        for shard, entry in scan_shards(appinfo):
            name, _ = app_names(shard, entry)
            actual[entry.appid] = name
    mismatches = check_originals(shared, actual, shared.names)
    console.print(
        f"共享词库：共 {len(shared.names)} 条，"
        f"重复 {shared.duplicates}，跳过 {shared.skipped}"
    )
    for warning in shared.warnings:
        console.print(f"[yellow]提示: {warning}[/yellow]")
    for warning in mismatches:
        console.print(f"[yellow]原名不符: {warning}[/yellow]")
    if not shared.warnings and not mismatches:
        console.print("[green]未发现问题[/green]")


@app.command("install-task")
def install_task(
    ctx: typer.Context,
    daily: str | None = typer.Option(
        None, "--daily", help="改为每天 HH:MM 运行（默认每次登录时运行）"
    ),
) -> None:
    """安装 Windows 计划任务：登录时静默补回已批准的汉化。"""
    if sys.platform != "win32":
        raise typer.BadParameter("计划任务仅支持 Windows")
    _, config_dir, backup_root = _context(ctx)
    steam_dir = str(ctx.obj["steam_dir"])
    try:
        command = task_command(steam_dir, config_dir, backup_root)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    args = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", command, "/F"]
    if daily:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily):
            raise typer.BadParameter("--daily 需为 HH:MM（24 小时制）")
        args += ["/SC", "DAILY", "/ST", daily]
    else:
        args += ["/SC", "ONLOGON"]
    try:
        done = subprocess.run(args, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise typer.BadParameter(f"无法调用 schtasks: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        raise typer.BadParameter(f"安装失败: {detail or '未知错误'}")
    when = f"每天 {daily}" if daily else "每次登录时"
    console.print(f"[green]已安装计划任务 {TASK_NAME}（{when}静默运行）[/green]")
    console.print(f"命令: {command}")


@app.command("uninstall-task")
def uninstall_task() -> None:
    """卸载静默同步计划任务。"""
    if sys.platform != "win32":
        raise typer.BadParameter("计划任务仅支持 Windows")
    try:
        done = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise typer.BadParameter(f"无法调用 schtasks: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        raise typer.BadParameter(f"卸载失败（可能未安装）: {detail or '未知错误'}")
    console.print(f"[green]已卸载计划任务 {TASK_NAME}[/green]")


def main(argv: list[str] | None = None) -> int:
    """Callable entry point; keeps compatibility with tests and old scripts."""
    try:
        app(argv if argv is not None else sys.argv[1:], standalone_mode=False)
    except CliExit as exc:
        return exc.code
    except typer.Exit as exc:
        return int(exc.exit_code)
    except typer.BadParameter as exc:
        console.print(f"[red]错误: {exc}[/red]")
        return 1
    except (BackupError, ValueError, FileNotFoundError, OSError) as exc:
        console.print(f"[red]错误: {exc}[/red]")
        return 1
    return 0


if __name__ == "__main__":
    main()
