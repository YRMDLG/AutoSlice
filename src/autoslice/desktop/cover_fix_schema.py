"""封面 AI 的逐对象参数：独立校验对象 ID、颜色和有限数值。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, fields, replace
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
    x: float | None = None
    y: float | None = None
    width: float | None = None
    font_size: float | None = None
    provided_fields: tuple[str, ...] | None = field(default=None, compare=False, repr=False)


def merge_object_edit(common: AIObjectEdit, specific: AIObjectEdit) -> AIObjectEdit:
    """逐字段合并共享与分比例参数；保留 JSON 中显式指定的零值和默认倍数。"""

    default = AIObjectEdit(specific.object_id)
    names = specific.provided_fields
    if names is None:
        names = tuple(item.name for item in fields(specific)
                      if item.name not in {"object_id", "name", "provided_fields"}
                      and getattr(specific, item.name) != getattr(default, item.name))
    return replace(common, **{name: getattr(specific, name) for name in names},
                   name=specific.name or common.name, provided_fields=None)


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


def object_changes(
    raw: Any, layers: Sequence[dict[str, Any]], *, base_edits: Sequence[AIObjectEdit] = (),
) -> tuple[AIObjectEdit, ...]:
    """仅接受当前可见、未锁定对象的有限参数；未知 ID 和任意正文替换不执行。"""

    if not isinstance(raw, list):
        return ()
    allowed = {item["id"]: item for item in layers if item.get("visible", True) and not item.get("locked", False)}
    defaults = {edit.object_id: edit for edit in base_edits}
    edits = {}
    for item in raw[:64]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        target = allowed.get(item["id"])
        if target is None or target.get("kind", "text") not in {"text", "image", "sticker", "shape"}:
            continue
        base = defaults.get(item["id"], AIObjectEdit(item["id"]))
        values = {
            "scale": _number(item, "scale", 0.65, 1.5, base.scale),
            "dx": _number(item, "dx", -0.35, 0.35, base.dx),
            "dy": _number(item, "dy", -0.35, 0.35, base.dy),
            "rotation": _number(item, "rotation", -30.0, 30.0, base.rotation),
            "x": _number(item, "x", -0.25, 1.25, base.x),
            "y": _number(item, "y", -0.25, 1.25, base.y),
        }
        if target.get("kind", "text") == "text":
            values.update(
                width_scale=_number(item, "width_scale", 0.65, 1.6, base.width_scale),
                width=_number(item, "width", 0.05, 2.0, base.width),
                font_size=_number(item, "font_size", 12, 360, base.font_size),
                line_spacing=_number(item, "line_spacing", 0.8, 1.8, base.line_spacing),
                words=base.words,
            )
            for field in ("fill", "stroke", "outer_stroke", "accent"):
                color = _color(item, field, optional=field == "outer_stroke")
                values[field] = color if color is not None else getattr(base, field)
            if isinstance(item.get("words"), list):
                words = tuple(dict.fromkeys(
                    word for word in item["words"] if isinstance(word, str) and 0 < len(word) <= 12
                    and word in target.get("text", "")
                ))[:4]
                if words or not item["words"]:
                    values["words"] = words
        edit = AIObjectEdit(item["id"], **values)
        if (edit != AIObjectEdit(item["id"]) or item["id"] in defaults) and item["id"] not in edits:
            name = str(target.get("text") or {"image": "图片", "sticker": "贴纸", "shape": "装饰"}.get(target.get("kind"), "文字"))
            edits[item["id"]] = AIObjectEdit(
                item["id"], **values, name=name.replace("\n", " / ")[:18], provided_fields=tuple(values),
            )
    return tuple(edits.values())


def edit_steps(edits: Sequence[AIObjectEdit]) -> tuple[str, ...]:
    """用原有对象的名字说明改动，避免只展示内部对象编号。"""

    steps = []
    for edit in edits:
        changes = []
        if edit.scale != 1:
            changes.append(f"{'放大' if edit.scale > 1 else '缩小'} {round(abs(edit.scale - 1) * 100)}%")
        if edit.dx or edit.dy or edit.x is not None or edit.y is not None:
            changes.append("调整位置")
        if edit.font_size is not None:
            changes.append(f"字号 {round(edit.font_size)}")
        if edit.width_scale != 1 or edit.width is not None:
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
