"""封面 AI 的逐对象参数：独立校验对象 ID、颜色和有限数值。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class AIObjectEdit:
    """以已知对象 ID 为目标；位移使用画布比例，缩放相对当前大小。"""

    object_id: str
    scale: float = 1.0
    dx: float = 0.0
    dy: float = 0.0
    width_scale: float = 1.0
    rotation: float | None = None
    fill: str | None = None
    stroke: str | None = None
    outer_stroke: str | None = None
    accent: str | None = None
    line_spacing: float | None = None
    words: tuple[str, ...] | None = None
    name: str = ""


# 描边和填色（或外描边和描边）的亮度对比低于这个值，描边基本看不出来。
# 只拦“几乎同色”这种确定的坏结果，不替用户判断配色好不好看。
MIN_STROKE_CONTRAST = 1.3
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")


def _linear(channel: int) -> float:
    value = channel / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def contrast_ratio(first: object, second: object) -> float | None:
    """WCAG 相对亮度对比度（1~21）；任何一个不是 #RRGGBB 就返回 None。"""

    luminances = []
    for color in (first, second):
        if not isinstance(color, str) or not _HEX_COLOR.fullmatch(color.strip()):
            return None
        value = color.strip().lstrip("#")
        red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
        luminances.append(0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue))
    return (max(luminances) + 0.05) / (min(luminances) + 0.05)


def becomes_invisible(new: tuple[object, object], old: tuple[object, object]) -> bool:
    """这一对颜色改后几乎同色、且比原来更糟就是 True；缺颜色没法比时不拦。"""

    after = contrast_ratio(*new)
    if after is None or after >= MIN_STROKE_CONTRAST:
        return False
    before = contrast_ratio(*old)
    return before is None or after < before


def _drop_invisible_strokes(values: dict, target: dict) -> None:
    """改后描边看不出来的颜色不接受，同一条里的位置、大小等其它改动照常保留。"""

    old_fill, old_stroke, old_outer = target.get("fill"), target.get("stroke"), target.get("outer_stroke")
    fill, stroke = values.get("fill") or old_fill, values.get("stroke") or old_stroke
    if (values.get("fill") or values.get("stroke")) and becomes_invisible((fill, stroke), (old_fill, old_stroke)):
        values["fill"] = values["stroke"] = None
        stroke = old_stroke
    outer = values.get("outer_stroke")
    if outer and becomes_invisible((outer, stroke), (old_outer, old_stroke)):
        values["outer_stroke"] = None


def _number(raw: dict, name: str, low: float, high: float, default=None):
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if math.isfinite(value) and low <= value <= high else default


def _color(raw: dict, name: str, *, optional: bool = False) -> str | None:
    value = raw.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value if re.fullmatch(r"#[0-9A-F]{6}", value) or optional and value == "" else None


def object_changes(raw: Any, layers: Sequence[dict[str, Any]]) -> tuple[AIObjectEdit, ...]:
    """仅接受当前可见、未锁定对象的有限参数；未知 ID 和任意正文替换不执行。"""

    if not isinstance(raw, list):
        return ()
    allowed = {item["id"]: item for item in layers if item.get("visible", True) and not item.get("locked", False)}
    edits = {}
    for item in raw[:64]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        target = allowed.get(item["id"])
        if target is None or target.get("kind", "text") not in {"text", "image", "sticker", "shape"}:
            continue
        values = {
            "scale": _number(item, "scale", 0.65, 1.5, 1.0),
            "dx": _number(item, "dx", -0.35, 0.35, 0.0),
            "dy": _number(item, "dy", -0.35, 0.35, 0.0),
            "rotation": _number(item, "rotation", -30.0, 30.0),
        }
        if target.get("kind", "text") == "text":
            values.update(
                width_scale=_number(item, "width_scale", 0.65, 1.6, 1.0),
                line_spacing=_number(item, "line_spacing", 0.8, 1.8),
                fill=_color(item, "fill"), stroke=_color(item, "stroke"),
                outer_stroke=_color(item, "outer_stroke", optional=True), accent=_color(item, "accent"),
            )
            if isinstance(item.get("words"), list):
                words = tuple(dict.fromkeys(
                    word for word in item["words"] if isinstance(word, str) and 0 < len(word) <= 12
                    and word in target.get("text", "")
                ))[:4]
                if words or not item["words"]:
                    values["words"] = words
            _drop_invisible_strokes(values, target)
        edit = AIObjectEdit(item["id"], **values)
        if edit != AIObjectEdit(item["id"]) and item["id"] not in edits:
            name = str(target.get("text") or {"image": "图片", "sticker": "贴纸", "shape": "装饰"}.get(target.get("kind"), "文字"))
            edits[item["id"]] = AIObjectEdit(item["id"], **values, name=name.replace("\n", " / ")[:18])
    return tuple(edits.values())


def edit_steps(edits: Sequence[AIObjectEdit]) -> tuple[str, ...]:
    """用原有对象的名字说明改动，避免只展示内部对象编号。"""

    steps = []
    for edit in edits:
        changes = []
        if edit.scale != 1:
            changes.append(f"{'放大' if edit.scale > 1 else '缩小'} {round(abs(edit.scale - 1) * 100)}%")
        if edit.dx or edit.dy:
            changes.append("调整位置")
        if edit.width_scale != 1:
            changes.append("调整行宽")
        if edit.rotation is not None:
            changes.append("调整倾斜")
        if any(getattr(edit, field) is not None for field in ("fill", "stroke", "outer_stroke", "accent")):
            changes.append("协调配色与描边")
        if edit.line_spacing is not None:
            changes.append("调整行距")
        if edit.words is not None:
            changes.append("调整强调词")
        steps.append(f"「{edit.name or '文字或素材'}」：{'、'.join(changes)}")
    return tuple(steps)
