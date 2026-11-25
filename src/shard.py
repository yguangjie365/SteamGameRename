"""Steam appinfo v42 shard reader/writer.

Layout (all little endian):
  44-byte header: magic, universe, shard index, shard count, app count,
  index size, string count, string size, data size, index crc, header crc
  80-byte index entry per app
  string table (NUL terminated UTF-8 keys)
  data region: per app [20-byte SHA1][binary VDF blob]
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from vdf import (
    TYPE_STRING,
    BinaryVDF,
    Node,
    U32,
    VDFError,
    find_string,
    get_node,
    node_text,
    resolve,
    set_node_text,
    validate_name,
)

MAGIC_V42 = 0x0756442A
HEADER_SIZE = 44
ENTRY_HEADER_SIZE = 16
HEADER_FMT = "<11I"


class ShardError(ValueError):
    pass


@dataclass
class Header:
    magic: int
    universe: int
    shard: int
    shard_count: int
    app_count: int
    index_size: int
    string_count: int
    string_size: int
    data_size: int
    index_crc: int
    header_crc: int


@dataclass
class AppEntry:
    position: int
    appid: int
    data_offset: int
    data_size: int
    index_offset: int
    index_length: int
    stored_sha1: bytes
    blob: bytes
    root: Node


@dataclass
class Shard:
    path: Path | None
    header_raw: bytes
    header: Header
    index_bytes: bytes
    string_bytes: bytes
    keys: list[str]
    codec: BinaryVDF
    entries: list[AppEntry]
    # 解析时的 (st_mtime_ns, st_size)。用于在写入前判断磁盘上的分片是否
    # 已被 Steam 改写——只在 parse_file() 里填充，parse_bytes() 为空。
    source_key: tuple[int, int] | None = None

    @cached_property
    def appids(self) -> set[int]:
        return {e.appid for e in self.entries}

    @cached_property
    def by_appid(self) -> dict[int, AppEntry]:
        return {e.appid: e for e in self.entries}


def parse_bytes(data: bytes, path: Path | None = None) -> Shard:
    if len(data) < HEADER_SIZE:
        raise ShardError("file shorter than 44-byte header")
    header = Header(*struct.unpack_from(HEADER_FMT, data, 0))
    if header.magic != MAGIC_V42:
        raise ShardError(f"unsupported magic 0x{header.magic:08x}, expected v42")
    if header.universe != 1:
        raise ShardError(f"unsupported universe {header.universe}")

    index_start = HEADER_SIZE
    index_end = index_start + header.index_size
    string_end = index_end + header.string_size
    data_end = string_end + header.data_size
    if data_end != len(data):
        raise ShardError(f"size mismatch: declared {data_end}, actual {len(data)}")

    index_bytes = data[index_start:index_end]
    string_bytes = data[index_end:string_end]
    data_region = data[string_end:data_end]

    keys: list[str] = []
    pos = 0
    for _ in range(header.string_count):
        end = string_bytes.find(b"\x00", pos)
        if end < 0:
            raise ShardError("string table is truncated")
        keys.append(string_bytes[pos:end].decode("utf-8", "replace"))
        pos = end + 1
    if pos != len(string_bytes):
        raise ShardError("string table has trailing bytes")

    codec = BinaryVDF(keys)
    entries: list[AppEntry] = []
    cursor = 0
    for i in range(header.app_count):
        if cursor + ENTRY_HEADER_SIZE > len(index_bytes):
            raise ShardError("index table is truncated")
        summary_size = struct.unpack_from("<I", index_bytes, cursor + 12)[0]
        entry_length = ENTRY_HEADER_SIZE + summary_size
        if entry_length < ENTRY_HEADER_SIZE or cursor + entry_length > len(index_bytes):
            raise ShardError(f"entry {i} has invalid summary size {summary_size}")
        appid, offset, size = struct.unpack_from("<3I", index_bytes, cursor)
        if offset + size > len(data_region) or size <= 20:
            raise ShardError(f"entry {i} has invalid data range {offset}+{size}")
        stored_sha1 = data_region[offset : offset + 20]
        blob = data_region[offset + 20 : offset + size]
        try:
            root = codec.parse(blob)
        except VDFError as exc:
            raise ShardError(f"entry {i} appid {appid}: {exc}") from exc
        entries.append(
            AppEntry(
                i, appid, offset, size, cursor, entry_length,
                stored_sha1, blob, root,
            )
        )
        cursor += entry_length
    if cursor != len(index_bytes):
        raise ShardError("index table has trailing bytes")
    return Shard(
        path, data[:HEADER_SIZE], header, index_bytes, string_bytes,
        keys, codec, entries,
    )


def parse_file(path: str | Path) -> Shard:
    p = Path(path)
    try:
        stat = p.stat()
    except OSError:
        stat = None
    shard = parse_bytes(p.read_bytes(), p)
    if stat is not None:
        shard.source_key = (stat.st_mtime_ns, stat.st_size)
    return shard


def verify(shard: Shard) -> list[str]:
    """Return a list of integrity problems; empty means healthy."""
    problems: list[str] = []
    expected_index_crc = zlib.crc32(shard.index_bytes + shard.string_bytes) & 0xFFFFFFFF
    if expected_index_crc != shard.header.index_crc:
        problems.append("index CRC mismatch")
    expected_header_crc = zlib.crc32(shard.header_raw[:40]) & 0xFFFFFFFF
    if expected_header_crc != shard.header.header_crc:
        problems.append("header CRC mismatch")

    cursor = 0
    for entry in sorted(shard.entries, key=lambda e: e.data_offset):
        if entry.data_offset != cursor:
            problems.append(f"appid {entry.appid}: data offset gap at {entry.data_offset}")
        cursor = entry.data_offset + entry.data_size
        if hashlib.sha1(entry.blob).digest() != entry.stored_sha1:
            problems.append(f"appid {entry.appid}: binary SHA1 mismatch")
    if cursor != shard.header.data_size:
        problems.append("data region size mismatch")
    return problems


def app_names(shard: Shard, entry: AppEntry) -> tuple[str, dict[str, str]]:
    """Return (common.name, {lang: localized name}) for one app."""
    common = get_node(shard.codec, entry.root, ["common"])
    name = ""
    localized: dict[str, str] = {}
    if common is not None:
        name_node = find_string(shard.codec, common, "name")
        name = node_text(shard.codec, name_node)
        localized_node = get_node(shard.codec, common, ["name_localized"])
        if localized_node is not None:
            for lang in ("schinese", "sc_schinese"):
                node = find_string(shard.codec, localized_node, lang)
                if node is not None:
                    localized[lang] = node_text(shard.codec, node)
    return name, localized


def target_path(target: str) -> list[str]:
    """Map a names.json target like 'name_localized.schinese' to a node path."""
    parts = target.split(".")
    return ["common"] + parts if parts[0] == "name_localized" else parts


def verify_changes(shard: Shard, changes: dict[int, dict[str, str]]) -> list[str]:
    """Confirm every requested target has the expected value in the shard."""
    problems: list[str] = []
    for appid, targets in changes.items():
        entry = shard.by_appid.get(appid)
        if entry is None:
            problems.append(f"appid {appid} not found in shard")
            continue
        for target, value in targets.items():
            node = resolve(shard.codec, entry.root, target_path(target))
            actual = node_text(shard.codec, node) if node is not None else None
            if actual != value:
                problems.append(f"appid {appid} {target}: expected {value!r}, got {actual!r}")
    return problems


def check_targets(
    shard: Shard,
    changes: dict[int, dict[str, str]],
    allow_create: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Dry-run preflight: verify target paths exist, without comparing values.

    Targets listed in `allow_create` may be absent — they will be appended as
    new keys by build_bytes_with_new_keys. Their parent object must still exist.
    """
    problems: list[str] = []
    for appid, targets in changes.items():
        entry = shard.by_appid.get(appid)
        if entry is None:
            problems.append(f"appid {appid} not found in shard")
            continue
        for target in targets:
            path = target_path(target)
            node = resolve(shard.codec, entry.root, path)
            if node is not None:
                continue
            if target in allow_create:
                parent = resolve(shard.codec, entry.root, path[:-1]) if len(path) > 1 else entry.root
                if parent is None or not parent.is_object():
                    problems.append(f"appid {appid}: parent of {target!r} is missing")
                continue
            problems.append(f"appid {appid}: target {target!r} not found in shard")
    return problems


def _assemble(
    shard: Shard,
    rebuilt: list[tuple[AppEntry, bytes]],
    string_bytes: bytes,
    key_count: int,
) -> bytes:
    """Rebuild index offsets and both CRCs after blob/string-table changes."""
    data_region = bytearray()
    new_index = bytearray(shard.index_bytes)
    for entry, blob in rebuilt:
        offset = len(data_region)
        size = 20 + len(blob)
        data_region += hashlib.sha1(blob).digest()
        data_region += blob
        struct.pack_into("<II", new_index, entry.index_offset + 4, offset, size)

    header = bytearray(shard.header_raw)
    struct.pack_into("<I", header, 24, key_count)          # string_count
    struct.pack_into("<I", header, 28, len(string_bytes))  # string_size
    struct.pack_into("<I", header, 32, len(data_region))   # data_size
    struct.pack_into(
        "<I", header, 36,
        zlib.crc32(bytes(new_index) + string_bytes) & 0xFFFFFFFF,
    )
    struct.pack_into("<I", header, 40, zlib.crc32(bytes(header[:40])) & 0xFFFFFFFF)
    return bytes(header) + bytes(new_index) + string_bytes + bytes(data_region)


def build_bytes(shard: Shard, changes: dict[int, dict[str, str]]) -> bytes:
    """Rebuild a shard with name changes.

    changes maps appid -> {target path: new value}, e.g.
    {1296830: {"common.name": "暖雪", "name_localized.sc_schinese": "暖雪"}}
    A target that does not exist in the shard is a configuration error and
    fails fast, instead of being silently skipped.
    """
    rebuilt: list[tuple[AppEntry, bytes]] = []
    for entry in shard.entries:
        blob = entry.blob
        wanted = changes.get(entry.appid)
        if wanted:
            root = shard.codec.parse(entry.blob)
            for target, value in wanted.items():
                node = resolve(shard.codec, root, target_path(target))
                if node is None:
                    raise ShardError(
                        f"appid {entry.appid}: target {target!r} not found in shard"
                    )
                set_node_text(node, value)
            blob = shard.codec.serialize(root)
        rebuilt.append((entry, blob))
    return _assemble(shard, rebuilt, shard.string_bytes, len(shard.keys))


def build_bytes_with_new_keys(
    shard: Shard,
    changes: dict[int, dict[str, str]],
    allow_create: set[str] | frozenset[str] = frozenset(),
) -> bytes:
    """Like build_bytes, but may create whitelisted targets that are absent.

    New keys are appended to the end of the per-shard string table, so all
    existing key indices remain valid. Only leaf names in `allow_create` can
    be created and only under an existing parent object (e.g. common.sortas
    under common). Anything else still fails fast.
    """
    # Pass 1: find which (appid, target) pairs actually need creation.
    creations: dict[int, set[str]] = {}
    for appid, targets in changes.items():
        entry = shard.by_appid.get(appid)
        if entry is None:
            continue
        for target in targets:
            if target in allow_create and resolve(
                shard.codec, entry.root, target_path(target)
            ) is None:
                creations.setdefault(appid, set()).add(target)

    # Pass 2: append the needed leaf keys to the string table.
    keys = list(shard.keys)
    key_index = {key: i for i, key in enumerate(keys)}
    string_bytes = bytearray(shard.string_bytes)
    for appid, targets in creations.items():
        for target in targets:
            leaf = target_path(target)[-1]
            if leaf not in key_index:
                key_index[leaf] = len(keys)
                keys.append(leaf)
                string_bytes += leaf.encode("utf-8") + b"\x00"

    codec = BinaryVDF(keys)  # old blobs stay parseable: old indices unchanged

    rebuilt: list[tuple[AppEntry, bytes]] = []
    for entry in shard.entries:
        blob = entry.blob
        wanted = changes.get(entry.appid)
        if wanted:
            root = codec.parse(entry.blob)
            for target, value in wanted.items():
                path = target_path(target)
                node = resolve(codec, root, path)
                if node is None:
                    if target not in creations.get(entry.appid, set()):
                        raise ShardError(
                            f"appid {entry.appid}: target {target!r} not found in shard"
                        )
                    parent = resolve(codec, root, path[:-1]) if len(path) > 1 else root
                    if parent is None or not parent.is_object():
                        raise ShardError(
                            f"appid {entry.appid}: parent of {target!r} is missing"
                        )
                    node = Node(
                        TYPE_STRING, key_index[path[-1]],
                        validate_name(value).encode("utf-8") + b"\x00",
                        [], 0x08,
                    )
                    parent.children.append(node)
                else:
                    set_node_text(node, value)
            blob = codec.serialize(root)
        rebuilt.append((entry, blob))
    return _assemble(shard, rebuilt, bytes(string_bytes), len(keys))
