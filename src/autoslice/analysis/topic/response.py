"""LLM 与 Markdown 话题响应中的切片判定解析。"""

from __future__ import annotations

NO_SLICE_HINTS = ("不切", "不加标记", "不建议切", "不要切", "不适合切")


FACADE_EXPORTS = {
    "_NO_SLICE_HINTS": "NO_SLICE_HINTS",
    "_is_slice_marked": "is_slice_marked",
    "_json_can_slice": "json_can_slice",
}


def is_slice_marked(raw_title: str) -> bool:
    """判断 Markdown 标题是否显式标记为可切。"""

    if any(hint in raw_title for hint in NO_SLICE_HINTS):
        return False
    return "✂" in raw_title


def json_can_slice(value: object, raw_title: object) -> bool:
    """解析 JSON ``can_slice`` 字段，并保留标题标记兼容行为。"""

    title = str(raw_title)
    if any(hint in title for hint in NO_SLICE_HINTS):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {
            "true",
            "yes",
            "y",
            "1",
            "可切",
            "切",
            "是",
        }
    return "✂" in title
