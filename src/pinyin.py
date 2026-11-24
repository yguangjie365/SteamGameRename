"""Zero-dependency Hanyu Pinyin initials for Steam library sorting.

Steam's ``common.sortas`` is a free-form string used to order a game in the
library list. A compact uppercase-initials form (e.g. 猛兽派对 -> MSPD) gives
a stable A-Z ordering without shipping a heavyweight pinyin dictionary.

The initial of a CJK ideograph is derived from its GB2312 code point using the
standard boundary table of the 23 pinyin initial groups. Characters outside
GB2312 (rare/extended ideographs) fall back to being kept verbatim, which is
safe: they simply do not participate in A-Z sorting for that character.
"""
from __future__ import annotations

import bisect

# GB2312 level-1 hanzi (0xB0A1-0xD7F9) are ordered by pinyin. Each boundary is
# the first hanzi of a new initial group. We bisect on the 2-byte GB2312 code,
# which preserves the pinyin ordering that Unicode code points do not.
_BOUNDARY_CHARS = (
    "啊", "八", "擦", "搭", "蛾", "发", "噶", "哈", "击", "喀", "垃", "妈",
    "拿", "哦", "啪", "期", "然", "撒", "塌", "哇", "昔", "压", "匝",
)
_INITIALS = "ABCDEFGHJKLMNOPQRSTWXYZ"
# 23 groups; note pinyin has no I/U/V initial, so 23 boundaries -> 23 letters.
_BOUNDARIES = tuple(c.encode("gb2312") for c in _BOUNDARY_CHARS)


def _hanzi_initial(char: str) -> str | None:
    """Return the pinyin initial for one GB2312 hanzi, or None if unknown."""
    code = char.encode("gb2312", errors="ignore")
    # Level-1 hanzi only (0xB0A1-0xD7F9) are ordered by pinyin. Level-2 hanzi
    # are ordered by radical; guessing an initial from them gives wrong results
    # (e.g. 殇 -> Z instead of S), so fall back to keeping the character.
    if len(code) != 2 or not (b"\xb0\xa1" <= code <= b"\xd7\xf9"):
        return None
    index = bisect.bisect_right(_BOUNDARIES, code) - 1
    if index < 0:
        return None
    return _INITIALS[index]


def to_initials(text: str) -> str:
    """Convert a Chinese name to uppercase pinyin initials for sortas.

    - CJK ideographs -> their pinyin initial (A-Z)
    - ASCII letters/digits -> kept as-is (uppercased letters)
    - spaces and punctuation -> dropped
    - other characters (e.g. rare hanzi) -> kept verbatim
    """
    if not isinstance(text, str) or not text:
        return ""
    out: list[str] = []
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            initial = _hanzi_initial(char)
            if initial:
                out.append(initial)
            else:
                out.append(char)
        elif char.isascii() and char.isalnum():
            out.append(char.upper())
        # spaces / punctuation / symbols are dropped
    return "".join(out)


def sortas_for(name: str) -> str:
    """Build the value written to common.sortas."""
    value = to_initials(name)
    return value[:128]  # keep sortas short; Steam only needs a sort key
