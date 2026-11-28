"""Name planning: merge Steam shard data, the ledger and name dictionaries."""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

import db as dbmod
from backup import atomic_write
from community import (  # noqa: F401  (re-exported for CLI/GUI/tests)
    COMMUNITY_NAMES_URL,
    _read_community_csv,
    load_community_names,
    update_community_names,
)
from game_names import (
    LEARNED_CSV_NAME, ORIGINAL_CSV_NAME, SharedNames, load_original_names,
    load_shared_names, merge_original_names, names_match,
    write_learned_originals,
)
from paths import asset_dir
from pinyin import sortas_for
from shard import AppEntry, Shard, ShardError, app_names, parse_file, target_path
from store import fetch_store_names, load_store_cache  # noqa: F401
from vdf import CJK, find_string, get_node, node_text, resolve, spacing_warning

TYPE_LABELS = {
    "game": "游戏",
    "music": "原声音轨",
    "application": "软件",
    "tool": "工具",
    "other": "其他",
}

KNOWN_PRIORITIES = {
    "user_override", "local_schinese", "local_sc_schinese",
    "common_name", "shared_csv", "community", "store_api",
}
# 历史配置里可能仍保留以上来源；当前版本起建议名固定只取社区词库，
# 该集合仅为兼容旧配置的读取，不再决定建议名来源。唯一仍会出现在计划
# 输出里的旧来源是 common_name：原名即中文的条目用它标记 unchanged
# 状态（见 _plan_entry），不代表它参与建议名合成。

# 只有名称字段允许写入；写进 common.icon 之类会静默破坏 appinfo 元数据
_LANG_SUFFIX = re.compile(r"[a-z_]{2,20}")
TARGET_HINT = "只允许 common.name、common.sortas 或 name_localized.<语言>"


def is_allowed_target(target: str) -> bool:
    if not isinstance(target, str):
        return False
    if target in ("common.name", "common.sortas"):
        return True
    if target.startswith("name_localized."):
        return bool(_LANG_SUFFIX.fullmatch(target.split(".", 1)[1]))
    return False


def classify_app(shard: Shard, entry: AppEntry) -> tuple[str, str]:
    """Return (normalized category, raw common.type)."""
    common = get_node(shard.codec, entry.root, ["common"])
    raw = node_text(shard.codec, find_string(shard.codec, common, "type")) if common else ""
    key = raw.strip().lower()
    if key in ("game", "music", "application", "tool"):
        return key, raw
    return "other", raw


class NameEntrySpec(BaseModel):
    appid: int | None = None
    shard: str | None = None
    original_name: str | None = None
    original_localized: dict = Field(default_factory=dict)
    base_name: str | None = None
    desired_name: str | None = None
    source: str | None = None
    targets: list[str] | None = None
    status: str | None = None
    approved: bool = False
    manual_override: bool = False
    note: str = ""


class NamesSpec(BaseModel):
    schema_version: int
    meta: dict = Field(default_factory=dict)
    defaults: dict = Field(default_factory=dict)
    summary: dict = Field(default_factory=dict)
    entries: dict[str, NameEntrySpec] = Field(default_factory=dict)


@dataclass
class PlanEntry:
    appid: int
    shard: str
    original_name: str
    # 台账首次记录的游戏原名。Steam 文件被改写后 original_name 变成中文，
    # 这个字段保留最初英文名，仅供展示/搜索，不参与写入与审批判定。
    steam_original_name: str = ""
    localized: dict[str, str] = field(default_factory=dict)
    base_name: str = ""
    desired_name: str = ""
    custom_name: str = ""
    source: str = ""
    targets: list[str] = field(default_factory=list)
    status: str = "suggested"
    approved: bool = False
    manual_override: bool = False
    note: str = ""
    app_type: str = "other"
    type_raw: str = ""
    applied: bool = False
    in_file: bool = False
    # 只读派生提示，不写回台账、不参与审批与写入判定
    warnings: list[str] = field(default_factory=list)
    # 可选的库排序键（common.sortas 拼音首字母）；为空表示不写入该字段
    sortas: str = ""

    def to_dict(self) -> dict:
        return {
            "appid": self.appid,
            "app_type": self.app_type,
            "type_raw": self.type_raw,
            "shard": self.shard,
            "original_name": self.original_name,
            "steam_original_name": self.steam_original_name,
            "original_localized": self.localized,
            "base_name": self.base_name,
            "custom_name": self.custom_name,
            "desired_name": self.desired_name,
            "source": self.source,
            "targets": self.targets,
            "status": self.status,
            "approved": self.approved,
            "manual_override": self.manual_override,
            "note": self.note,
            "applied": self.applied,
            "in_file": self.in_file,
            "warnings": list(self.warnings),
            "sortas": self.sortas,
        }


def _validate_ledger(data: dict, label: str) -> dict:
    """Validate and normalize ledger data; shared by JSON and SQLite paths."""
    try:
        validated = NamesSpec.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"{label} 校验失败: {exc}") from exc
    if validated.schema_version != 1:
        raise ValueError("unsupported names schema_version")
    for key, entry in validated.entries.items():
        if not key.isdigit():
            raise ValueError(f"{label}: entries key 非数字: {key!r}")
        if entry.appid is not None and int(entry.appid) != int(key):
            raise ValueError(
                f"{label}: entries[{key}].appid={entry.appid} 与 key 不一致"
            )
        bad = [t for t in (entry.targets or []) if not is_allowed_target(t)]
        if bad:
            raise ValueError(
                f"{label}: entries[{key}] 含非法写入目标 {bad}（{TARGET_HINT}）"
            )

    # 返回 pydantic 归一化后的数据：原始 dict 会让 "approved": "false" 这种
    # 宽松强制的值在下游被当成真值。用户自定义字段原样保留。
    normalized = validated.model_dump(exclude_none=True)
    raw_entries = data.get("entries") or {}
    for key, entry in normalized.get("entries", {}).items():
        extra = {
            name: value
            for name, value in (raw_entries.get(key) or {}).items()
            if name not in entry
        }
        if extra:
            entry.update(extra)
    return normalized


def _db_disabled() -> bool:
    return bool(os.environ.get("STEAM_CN_RENAMER_NO_DB"))


def _ledger_connection(path: Path):
    conn = dbmod.connect(dbmod.db_path(path.parent))
    if not dbmod.ledger_ready(conn):
        conn.close()
        dbmod.ensure_db(path.parent, ledger_validator=_validate_ledger)
        conn = dbmod.connect(dbmod.db_path(path.parent))
    return conn


def load_names(path: str | Path) -> dict:
    """Read the ledger from SQLite; legacy JSON is migration input only."""
    source = Path(path)
    if _db_disabled():
        try:
            text = source.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"找不到台账文件 {source}；请确认 --config-dir 指向包含 names.json 的目录"
            ) from exc
        except OSError as exc:
            raise FileNotFoundError(f"无法读取台账 {source}: {exc}") from exc
        return _validate_ledger(json.loads(text), "names.json")

    conn = _ledger_connection(source)
    try:
        data = dbmod.read_ledger(conn)
    finally:
        conn.close()
    return _validate_ledger(data, str(dbmod.db_path(source.parent)))


def save_names(data: dict, path: str | Path) -> None:
    """Validate and save the ledger to SQLite without writing a JSON mirror."""
    target = Path(path)
    normalized = _validate_ledger(data, str(target))
    if _db_disabled():
        payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
        atomic_write(target, payload.encode("utf-8"))
        return

    conn = _ledger_connection(target)
    try:
        dbmod.write_ledger(conn, normalized)
    finally:
        conn.close()


def export_names_json(data: dict, path: str | Path) -> None:
    """Explicitly export the current SQLite ledger as a readable JSON backup."""
    target = Path(path)
    normalized = _validate_ledger(data, str(target))
    payload = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    atomic_write(target, payload.encode("utf-8"))


# Parsed shards keyed by path, invalidated by (mtime_ns, size). Parsing all
# 247 shards costs ~145 ms and dominates a replan (all CSV loading is ~10 ms).
# Steam updates touch only a handful of shards, so reusing the rest turns a
# post-update replan from ~160 ms into ~15 ms. Shards are treated as immutable
# once parsed: build_bytes() re-parses each blob instead of mutating this tree.
_shard_cache: dict[str, tuple[int, int, Shard]] = {}
_shard_cache_lock = threading.Lock()


def clear_shard_cache() -> None:
    """Drop cached shard trees (used by tests and after manual edits)."""
    with _shard_cache_lock:
        _shard_cache.clear()


def _load_shard(path: Path) -> Shard:
    try:
        stat = path.stat()
    except OSError:
        stat = None
    key = (stat.st_mtime_ns, stat.st_size) if stat else None
    with _shard_cache_lock:
        hit = _shard_cache.get(str(path))
    if hit is not None and key is not None and hit[0] == key[0] and hit[1] == key[1]:
        return hit[2]
    try:
        shard = parse_file(path)
    except ShardError as exc:
        raise ShardError(f"{path.name}: {exc}") from exc
    if key is not None:
        with _shard_cache_lock:
            _shard_cache[str(path)] = (key[0], key[1], shard)
    return shard


def scan_shards(
    appinfo_dir: str | Path,
    shard_names: set[str] | frozenset[str] | None = None,
) -> list[tuple[Shard, AppEntry]]:
    """Parse shards (incrementally cached) and flatten their entries.

    Parsing dominates a replan, so each Shard is cached by (mtime_ns, size):
    a post-Steam-update replan only re-parses the shards that actually changed.
    Threaded parsing was measured and rejected — the work is CPU-bound, so
    threads under GIL were 5-10% *slower* than serial.
    """
    folder = Path(appinfo_dir)
    if not folder.is_dir():
        raise FileNotFoundError(f"appinfo 目录不存在: {folder}")
    paths = sorted(folder.glob("appinfo_*.vdf"))
    if shard_names is not None:
        paths = [path for path in paths if path.name in shard_names]
    if not paths:
        return []

    shards: list[tuple[Shard, AppEntry]] = []
    for path in paths:
        shard = _load_shard(path)
        for entry in shard.entries:
            shards.append((shard, entry))
    return shards




def _default_targets(defaults: dict, localized: dict[str, str]) -> list[str]:
    targets = [defaults.get("primary_target", "common.name")]
    for lang in defaults.get("sync_existing_localized", ["schinese", "sc_schinese"]):
        if lang in localized:
            targets.append(f"name_localized.{lang}")
    return targets


def _existing_targets(shard: Shard, entry, targets: list[str]) -> tuple[list[str], bool]:
    """Keep only targets that really exist; report whether the primary one does."""
    primary = targets[0] if targets else "common.name"
    existing = [
        target for target in targets
        if is_allowed_target(target)
        and resolve(shard.codec, entry.root, target_path(target)) is not None
    ]
    return existing, primary in existing


def _is_applied(shard: Shard, entry, targets: list[str], desired: str) -> bool:
    """True when every target already carries the desired name in the file."""
    if not desired or not targets:
        return False
    for target in targets:
        node = resolve(shard.codec, entry.root, target_path(target))
        if node is None or node_text(shard.codec, node) != desired:
            return False
    return True


def resolve_base(
    original: str,
    localized: dict[str, str],
    priority: list[str],
    community_names: dict[int, str] | None = None,
    store_cache: dict[int, str] | None = None,
    appid: int | None = None,
    shared_names: dict[int, str] | None = None,
) -> tuple[str, str]:
    """Return the community-dictionary name as the only suggestion source.

    The signature keeps the historical arguments so callers and tests do not
    break, but 当前版本起 none of them except ``community_names``/``appid``
    can produce a suggestion. Official localization, store API, shared CSV,
    Chinese-looking original names and user overrides are explicitly excluded:
    if the community dictionary has no entry, the suggestion stays empty.
    """
    if community_names and appid is not None:
        name = (community_names.get(appid) or "").strip()
        if name:
            return name, "community"
    return "", ""


def _plan_entry(
    shard: Shard,
    entry: AppEntry,
    record: dict,
    *,
    defaults: dict,
    priority: list[str],
    include_no_zh: bool,
    include_unchanged: bool,
    community_names: dict[int, str] | None,
    store_cache: dict[int, str] | None,
    shared_names: dict[int, str] | None = None,
    original_names: dict[int, str] | None = None,
) -> PlanEntry | None:
    """Plan one app record from its suggestion and optional custom name."""
    original, localized = app_names(shard, entry)
    app_type, type_raw = classify_app(shard, entry)
    note = record.get("note", "")
    targets, primary_ok = _existing_targets(
        shard, entry, record.get("targets") or _default_targets(defaults, localized)
    )
    recorded_original = (record.get("original_name") or "").strip()
    display_original = (original_names or {}).get(entry.appid) or recorded_original
    common = {
        "appid": entry.appid,
        "shard": shard.path.name if shard.path else "",
        "original_name": original,
        "steam_original_name": display_original,
        "localized": localized,
        "app_type": app_type,
        "type_raw": type_raw,
    }

    if not primary_ok:
        if not include_no_zh:
            return None
        return PlanEntry(
            **common, base_name=original, desired_name="", source="none",
            targets=[], status="unsupported", approved=False,
            note=note or "该条目在 Steam 数据中没有名称字段，暂不支持改名",
        )

    # 建议名只来自社区词库；官方本地化、商店 API、共享 CSV、中文原名和
    # 用户自定义名都不参与。社区词库没有该 appid 时留空（no_zh）。
    base, source = resolve_base(
        original, localized, priority,
        community_names=community_names, store_cache=store_cache,
        appid=entry.appid, shared_names=shared_names,
    )
    if base and not CJK.search(base):
        # 社区词库偶有非中文条目时不作为中文建议名展示
        base, source = "", ""

    if record.get("manual_override"):
        desired = (record.get("desired_name") or "").strip()
        if not desired and not include_no_zh:
            return None
        is_approved = bool(desired) and bool(record.get("approved", True))
        in_file = bool(desired) and _is_applied(shard, entry, targets, desired)
        return PlanEntry(
            **common, base_name=base, custom_name=desired, desired_name=desired,
            source="user_override", targets=targets, note=note,
            status="approved" if is_approved else ("suggested" if desired else "no_zh"),
            approved=is_approved, manual_override=True,
            applied=is_approved and in_file, in_file=in_file,
        )

    recorded_desired = (record.get("desired_name") or "").strip()
    if record.get("approved") and recorded_desired:
        # 已批准条目：即使建议名落空（官方与词库都没有），批准的目标名仍有效
        desired = recorded_desired
        if not CJK.search(desired):
            note = note or "手动批准的目标名不含中文，请确认是否符合预期"
        approved = True
        status = "approved"
    else:
        if record.get("approved") and not recorded_desired:
            note = note or "台账标记为已批准但目标名为空，请重新采纳建议或输入自定义名称"
        if not base:
            if CJK.search(original or ""):
                # 原名即中文（如《不思议迷宫》《传送门》）：文件里已经是
                # 中文名，不存在"无中文名"的问题，也无需任何写入。建议列
                # 保持为空（社区词库仍是唯一建议来源，中文原名不进建议列），
                # 仅把状态归为 unchanged 而非 no_zh。
                desired = original
                approved = False
                status = "unchanged"
                source = "common_name"
            else:
                # 官方本地化与词库都没有：建议名留空，由用户自定义或保持原名
                if not include_no_zh:
                    return None
                return PlanEntry(
                    **common, base_name="", desired_name="",
                    source=source or "none", targets=targets, note=note,
                    status="no_zh", approved=False,
                )
        else:
            desired = base
            approved = False
            status = "unchanged" if desired == original else "suggested"

    if (
        status == "unchanged"
        and not include_unchanged
        and not include_no_zh
        and source not in ("store_api", "community")
    ):
        return None

    in_file = _is_applied(shard, entry, targets, desired)
    return PlanEntry(
        **common, base_name=base, custom_name="",
        desired_name=desired,
        source=source, targets=targets, note=note, status=status, approved=approved,
        applied=approved and in_file, in_file=in_file,
    )


def build_plan(
    appinfo_dir: str | Path,
    names: dict,
    include_unchanged: bool = False,
    include_no_zh: bool = True,
    store_cache: dict[int, str] | None = None,
    community_names: dict[int, str] | None = None,
    shared: SharedNames | None = None,
    original_names: dict[int, str] | None = None,
    shard_names: set[str] | frozenset[str] | None = None,
    pairs: list[tuple[Shard, AppEntry]] | None = None,
) -> list[PlanEntry]:
    defaults = names.get("defaults", {})
    records = names.get("entries", {})
    # 建议名取值语义（当前规则）：社区词库是唯一来源。
    # 老配置里的 defaults.mapping_priority / use_shared_names 不再参与建议名，
    # 避免旧配置把官方本地化、商店名或自定义名重新带回建议列。
    priority = ["community"]
    plan: list[PlanEntry] = []

    if pairs is None:
        pairs = scan_shards(appinfo_dir, shard_names=shard_names)
    for shard, entry in pairs:
        item = _plan_entry(
            shard, entry, records.get(str(entry.appid), {}),
            defaults=defaults, priority=priority,
            include_no_zh=include_no_zh, include_unchanged=include_unchanged,
            community_names=community_names, store_cache=store_cache,
            shared_names=shared.names if shared else None,
            original_names=original_names,
        )
        if item is not None:
            plan.append(item)
    # 可选的库排序键：仅在 defaults.write_sortas_pinyin=true 且条目已批准时
    # 生成。common.sortas 在多数分片里不存在，写入走新增键路径。
    write_sortas = bool(defaults.get("write_sortas_pinyin", False))
    for item in plan:
        if write_sortas and item.approved and item.desired_name:
            item.sortas = sortas_for(item.desired_name)
        else:
            item.sortas = ""

    # 只检查最终目标名：原名是 Steam 官方数据，不该被我们的规则评判。
    # 这是派生提示，赋值（而非追加）保证重复构建不会累积。
    for item in plan:
        warning = spacing_warning(item.desired_name)
        item.warnings = [warning] if warning else []

    if shared:
        # 词库自身的格式问题挂到第一条命中该来源的行，避免静默失败
        for item in plan:
            if item.source == "shared_csv":
                item.warnings.extend(f"共享词库：{w}" for w in shared.warnings[:5])
                break
        # 原名不符通常意味着 AppID 填错——宁可提示，也不能悄悄改错游戏
        by_id = {item.appid: item for item in plan}
        for appid, recorded in shared.originals.items():
            item = by_id.get(appid)
            if item is None or item.source != "shared_csv":
                continue
            current = item.original_name or ""
            target = shared.names.get(appid, "")
            if (
                recorded.strip() and current
                and not names_match(recorded, current)
                and not (target and names_match(target, current))
            ):
                item.warnings.append(
                    f"共享词库记的原名是「{recorded}」，Steam 当前是「{item.original_name}」，请核对 AppID"
                )
    return plan


def plan_from_config(
    appinfo_dir: str | Path,
    config_dir: str | Path,
    include_no_zh: bool = True,
    include_unchanged: bool = False,
    shard_names: set[str] | frozenset[str] | None = None,
) -> list[PlanEntry]:
    """Build a plan from the SQLite ledger and local name dictionaries.

    Shared by the CLI and the web layer so both keep identical behaviour
    instead of duplicating the load/build orchestration. ``shard_names``
    restricts parsing to those shards (see ``scan_shards``); None means all.
    """
    folder = Path(config_dir)
    db_file = dbmod.db_path(folder)

    # SQLite 路径：只查本机拥有的 appid。这是 SQLite 唯一比全量加载更快的地方
    # ——10 万条词库全量加载要 50-70ms，按主键 IN 查 803 条只要 ~1ms。
    if db_file.exists():
        pairs = scan_shards(appinfo_dir, shard_names=shard_names)
        appids = {entry.appid for _, entry in pairs}
        if appids:
            baseline_shared = asset_dir("defaults") / "game_names.csv"
            local_shared = folder / "game_names.csv"
            conn = dbmod.connect(db_file)
            try:
                community = dbmod.query_community(conn, appids)
                store = dbmod.query_store(conn, appids)
                originals = dbmod.query_originals(conn, appids)
                # 共享词库复用同一个连接：少一次 connect + 一次 CSV 指纹检查
                dbmod.sync_shared_from_files(conn, baseline_shared, local_shared)
                shared = dbmod.read_shared_names(conn)
                shared.warnings = dbmod.read_shared_warnings(conn)
            finally:
                conn.close()
            if not (shared.names or shared.originals):
                # 库里的共享词库为空（未迁移）→ 回退 CSV 解析
                shared = load_shared_names(baseline_shared, local_shared)
            # 用户手工放的 CSV 覆盖层仍然优先（与 load_community_names 一致）
            overlay = folder / "community_names.csv"
            if overlay.exists():
                try:
                    community.update(
                        {
                            appid: name
                            for appid, name in _read_community_csv(overlay).items()
                            if appid in appids
                        }
                    )
                except (OSError, ValueError):
                    pass
            if community or store or originals:
                return build_plan(
                    appinfo_dir,
                    load_names(folder / "names.json"),
                    include_unchanged=include_unchanged,
                    include_no_zh=include_no_zh,
                    shard_names=shard_names,
                    store_cache=store,
                    community_names=community,
                    shared=shared,
                    original_names=originals,
                    pairs=pairs,  # 复用已扫描结果，避免第二次遍历 247 个分片
                )
        # 表为空（未迁移/全新库）→ 落到下面的文件路径

    return build_plan(
        appinfo_dir,
        load_names(folder / "names.json"),
        include_unchanged=include_unchanged,
        include_no_zh=include_no_zh,
        shard_names=shard_names,
        store_cache=load_store_cache(folder / "store_cache.json"),
        community_names=load_community_names(folder / "community_names.json"),
        shared=load_shared_names(
            asset_dir("defaults") / "game_names.csv",
            folder / "game_names.csv",
        ),
        original_names=merge_original_names(
            load_original_names(asset_dir("defaults") / ORIGINAL_CSV_NAME),
            load_original_names(folder / LEARNED_CSV_NAME),
        ),
    )


def learn_original_names(
    appinfo_dir: str | Path, config_dir: str | Path
) -> tuple[int, int]:
    """Capture English names seen in shards into the local CSV layer.

    Only non-Chinese common.name values are learned, and only for AppIDs not
    already covered by the bundled baseline. AppIDs the user has approved or
    overridden are skipped: their shard name may be a chosen alias ("CS2"),
    which must never be recorded as the original Steam name. Pure display aid:
    it never feeds into target resolution or the write path. Returns (new, total).
    """
    folder = Path(config_dir)
    local_path = folder / LEARNED_CSV_NAME
    db_file = dbmod.db_path(folder)
    baseline: dict[int, str] = {}
    local: dict[int, str] = {}

    # SQLite 优先：db 存在时 baseline/learned 都在库里，读 CSV 会与库脱节
    if db_file.exists():
        conn = dbmod.connect(db_file)
        try:
            baseline = dbmod.read_original_names(conn, "baseline")
            local = dbmod.read_original_names(conn, "learned")
        finally:
            conn.close()
    if not baseline:
        baseline = load_original_names(asset_dir("defaults") / ORIGINAL_CSV_NAME)
    if not local:
        local = load_original_names(local_path)
    learned = dict(local)

    # Skip entries the user has touched. Approved/overridden names are user
    # intent, not pristine Steam data — learning them would poison the display
    # layer with aliases. A missing or broken ledger must not block learning.
    modified: set[int] = set()
    try:
        ledger = load_names(folder / "names.json")
        for key, record in (ledger.get("entries") or {}).items():
            if not str(key).isdigit():
                continue
            if (
                record.get("approved")
                or record.get("manual_override")
                or (record.get("desired_name") or "").strip()
            ):
                modified.add(int(key))
    except (OSError, ValueError):
        pass

    for shard, entry in scan_shards(appinfo_dir):
        name, _ = app_names(shard, entry)
        appid = entry.appid
        if appid in learned or appid in baseline or appid in modified or not name:
            continue
        if CJK.search(name):
            continue
        learned[appid] = name.strip()
    new = len(learned) - len(local)
    if new:
        write_learned_originals(learned, local_path)  # CSV 交换格式
        if db_file.exists():
            conn = dbmod.connect(db_file)
            try:
                dbmod.write_original_names(conn, learned, "learned")
            finally:
                conn.close()
    return new, len(learned)


def pending_changes(plan: list[PlanEntry]) -> dict[str, dict[int, dict[str, str]]]:
    """Approved changes not yet present in the file (Steam may have reset them)."""
    grouped: dict[str, dict[int, dict[str, str]]] = {}
    for item in plan:
        if (
            not item.approved or not item.desired_name or item.in_file
            or item.status == "unsupported" or not item.targets
        ):
            continue
        values = {target: item.desired_name for target in item.targets}
        if item.sortas:
            values["common.sortas"] = item.sortas
        grouped.setdefault(item.shard, {})[item.appid] = values
    return grouped


def approved_changes(plan: list[PlanEntry]) -> dict[str, dict[int, dict[str, str]]]:
    """Group approved desired names by shard and appid for the writer.

    Entries flagged as unsupported (no writable name field) are skipped so a
    single bad row can never block the whole batch.
    """
    grouped: dict[str, dict[int, dict[str, str]]] = {}
    for item in plan:
        if not item.approved or not item.desired_name:
            continue
        if item.status == "unsupported" or not item.targets:
            continue
        values = {target: item.desired_name for target in item.targets}
        if item.sortas:
            values["common.sortas"] = item.sortas
        grouped.setdefault(item.shard, {})[item.appid] = values
    return grouped
