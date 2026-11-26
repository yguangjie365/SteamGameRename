"""Binary VDF codec used by Steam appinfo v41/v42.

Keys are u32 indices into a per-file string table. Values are stored inline.
The codec keeps raw value bytes so a parse/serialize round trip is byte exact.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

U32 = struct.Struct("<I")

TYPE_NONE = 0x00
TYPE_STRING = 0x01
TYPE_INT32 = 0x02
TYPE_FLOAT32 = 0x03
TYPE_POINTER = 0x04
TYPE_WIDESTRING = 0x05
TYPE_COLOR = 0x06
TYPE_UINT64 = 0x07
TYPE_END = 0x08
TYPE_INT64 = 0x0A
TYPE_END_ALT = 0x0B

_MAX_DEPTH = 64


class VDFError(ValueError):
    pass


@dataclass(slots=True)
class Node:
    type: int
    key_index: int
    raw: bytes = b""
    children: list["Node"] = field(default_factory=list)
    end: int = TYPE_END
    trailer: bytes = b""

    def is_object(self) -> bool:
        return self.type == TYPE_NONE


class BinaryVDF:
    """Parse and serialize binary VDF trees with an out-of-band key table."""

    def __init__(self, keys: list[str]):
        self.keys = keys

    def key(self, node: Node) -> str:
        if node.key_index >= len(self.keys):
            raise VDFError(f"key index {node.key_index} out of range")
        return self.keys[node.key_index]

    def parse(self, data: bytes) -> Node:
        root, pos = self._parse_node(data, 0, 0)
        remaining = data[pos:]
        if remaining and all(byte in (TYPE_END, TYPE_END_ALT) for byte in remaining):
            root.trailer = remaining
        elif remaining:
            raise VDFError(f"trailing {len(remaining)} unexpected bytes after root node")
        return root

    def serialize(self, node: Node) -> bytes:
        out = bytearray((node.type,))
        out += U32.pack(node.key_index)
        if node.is_object():
            for child in node.children:
                out += self.serialize(child)
            out.append(node.end)
        else:
            out += node.raw
        out += node.trailer
        return bytes(out)

    def _read_cstring(self, data: bytes, pos: int) -> tuple[bytes, int]:
        end = data.find(b"\x00", pos)
        if end < 0:
            raise VDFError("unterminated string")
        return data[pos : end + 1], end + 1

    def _read_widestring(self, data: bytes, pos: int) -> tuple[bytes, int]:
        p = pos
        while p + 1 < len(data):
            if data[p] == 0 and data[p + 1] == 0:
                return data[pos : p + 2], p + 2
            p += 2
        raise VDFError("unterminated wide string")

    def _parse_node(self, data: bytes, pos: int, depth: int = 0) -> tuple[Node, int]:
        if depth > _MAX_DEPTH:
            raise VDFError(f"object nesting deeper than {_MAX_DEPTH} levels")
        if pos >= len(data):
            raise VDFError("unexpected EOF")
        kind = data[pos]
        pos += 1
        if kind in (TYPE_END, TYPE_END_ALT):
            raise VDFError(f"unexpected end marker 0x{kind:02x} at {pos - 1}")
        if pos + 4 > len(data):
            raise VDFError("truncated key index")
        key_index = U32.unpack_from(data, pos)[0]
        pos += 4
        if key_index >= len(self.keys):
            raise VDFError(f"key index {key_index} out of range at {pos - 4}")

        if kind == TYPE_NONE:
            children: list[Node] = []
            while True:
                if pos >= len(data):
                    raise VDFError("unterminated object")
                if data[pos] in (TYPE_END, TYPE_END_ALT):
                    end = data[pos]
                    pos += 1
                    break
                child, pos = self._parse_node(data, pos, depth + 1)
                children.append(child)
            return Node(kind, key_index, b"", children, end), pos

        if kind == TYPE_STRING:
            raw, pos = self._read_cstring(data, pos)
        elif kind == TYPE_WIDESTRING:
            raw, pos = self._read_widestring(data, pos)
        elif kind in (TYPE_INT32, TYPE_FLOAT32, TYPE_POINTER, TYPE_COLOR):
            if pos + 4 > len(data):
                raise VDFError("truncated 4-byte scalar")
            raw = data[pos : pos + 4]
            pos += 4
        elif kind in (TYPE_UINT64, TYPE_INT64):
            if pos + 8 > len(data):
                raise VDFError("truncated 8-byte scalar")
            raw = data[pos : pos + 8]
            pos += 8
        else:
            raise VDFError(f"unknown node type 0x{kind:02x} at {pos - 1}")
        return Node(kind, key_index, raw, [], TYPE_END), pos


def find_child(codec: BinaryVDF, obj: Node, name: str) -> Node | None:
    """Find a direct child object by key name."""
    if not obj.is_object():
        return None
    for child in obj.children:
        if child.is_object() and codec.key(child) == name:
            return child
    return None


def find_string(codec: BinaryVDF, obj: Node, name: str) -> Node | None:
    """Find a direct string child by key name."""
    if not obj.is_object():
        return None
    for child in obj.children:
        if child.type == TYPE_STRING and codec.key(child) == name:
            return child
    return None


def get_node(codec: BinaryVDF, root: Node, path: list[str]) -> Node | None:
    """Traverse object path relative to the root node."""
    current = root
    for part in path:
        current = find_child(codec, current, part)
        if current is None:
            return None
    return current


def resolve(codec: BinaryVDF, root: Node, path: list[str]) -> Node | None:
    """Resolve a dotted path; the final component may be a string or object."""
    current = root
    for part in path[:-1]:
        current = find_child(codec, current, part)
        if current is None:
            return None
    last = path[-1]
    string_node = find_string(codec, current, last)
    return string_node if string_node is not None else find_child(codec, current, last)


def node_text(codec: BinaryVDF, node: Node | None) -> str:
    if node is None or node.type != TYPE_STRING:
        return ""
    return node.raw[:-1].decode("utf-8", "replace")


def validate_name(text: str) -> str:
    """Reject values that would corrupt a VDF string; returns encoded bytes length check."""
    if not isinstance(text, str):
        raise VDFError("name must be a string")
    if "\x00" in text:
        raise VDFError("name contains NUL byte")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise VDFError("name contains control character")
    if len(text.encode("utf-8")) > 1024:
        raise VDFError("name is longer than 1024 bytes")
    return text


# 汉字（扩展A + 基本区 + 兼容表意文字）
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")

# 汉字与 ASCII 数字紧邻的边界
_CJK_DIGIT_ADJACENT = re.compile(
    r"[\u3400-\u9fff\uf900-\ufaff][0-9]|[0-9][\u3400-\u9fff\uf900-\ufaff]"
)


def spacing_warning(text: str) -> str | None:
    """汉字与数字紧邻时给出排版建议；纯提示，不修改也不拒绝名称。

    依据 W3C《中文排版需求》：汉字与数字之间宜留不多于 1/4 汉字宽的间隔；
    纯文本环境（游戏名即属此类）以一个半角空格近似。只用于提醒，
    官方译名等紧贴写法可能刻意为之，因此不参与任何自动改写。
    """
    if not isinstance(text, str) or not text:
        return None
    match = _CJK_DIGIT_ADJACENT.search(text)
    if match is None:
        return None
    return (
        "汉字与数字紧邻，建议加一个半角空格"
        "（如「合金弹头 3」）；仅提示，不自动修改"
    )


def set_node_text(node: Node, text: str) -> bool:
    """Replace a string node value. Returns True if bytes changed."""
    if node.type != TYPE_STRING:
        raise VDFError("target node is not a string")
    encoded = validate_name(text).encode("utf-8")
    raw = encoded + b"\x00"
    if raw == node.raw:
        return False
    node.raw = raw
    return True
