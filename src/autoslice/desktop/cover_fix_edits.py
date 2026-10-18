"""封面 AI 的逐对象调整：保持正文，并让关联装饰跟随。"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Sequence

from .cover_effects import burst_id
from .cover_fix_schema import AIObjectEdit
from .cover_layout import canvas_size, shape_geometry, text_area
from .cover_model import (
    CoverDocument,
    ImageObject,
    ShapeObject,
    StickerObject,
    TextObject,
    object_for_profile,
    resize_text_style,
    set_profile_override,
    stroke_for,
    update_text_object,
)


def _follow_burst(document: CoverDocument, before: TextObject, after: TextObject, key: str) -> CoverDocument:
    shape = object_for_profile(document, burst_id(before.id), key)
    if not isinstance(shape, ShapeObject) or not shape.visible or shape.locked:
        return document
    if before.transform == after.transform and before.rect == after.rect and before.style.font_size == after.style.font_size:
        return document
    ratio = after.style.font_size / max(1, before.style.font_size)
    size = canvas_size(key)
    old_area, new_area = text_area(before, size), text_area(after, size)
    old_box = shape_geometry(shape, size)
    dx, dy = old_box.center_x - old_area.center_x, old_box.center_y - old_area.center_y
    angle = math.radians(after.transform.rotation - before.transform.rotation)
    center_x = new_area.center_x + ratio * (dx * math.cos(angle) - dy * math.sin(angle))
    center_y = new_area.center_y + ratio * (dx * math.sin(angle) + dy * math.cos(angle))
    transform = replace(
        shape.transform,
        x=(center_x - old_box.width * ratio / 2) / size[0],
        y=(center_y - old_box.height * ratio / 2) / size[1],
        scale=shape.transform.scale * ratio,
        rotation=shape.transform.rotation + after.transform.rotation - before.transform.rotation,
    )
    return set_profile_override(document, key, replace(shape, transform=transform))


def apply_object_edits(document: CoverDocument, edits: Sequence[AIObjectEdit], key: str) -> CoverDocument:
    """调整当前比例的几何；文字颜色与强调词沿用编辑器的跨比例共享规则。"""

    explicit = {edit.object_id for edit in edits}
    for edit in edits:
        current = object_for_profile(document, edit.object_id, key)
        if not isinstance(current, (TextObject, ImageObject, StickerObject, ShapeObject)) or current.locked or not current.visible:
            continue
        transform = replace(
            current.transform, x=current.transform.x + edit.dx, y=current.transform.y + edit.dy,
            rotation=current.transform.rotation if edit.rotation is None else edit.rotation,
        )
        if isinstance(current, TextObject):
            size = max(12, round(current.style.font_size * edit.scale))
            ratio = size / max(1, current.style.font_size)
            new_width = min(3.0, current.rect.width * ratio * edit.width_scale)
            style = resize_text_style(current.style, size)
            colors = {name: getattr(edit, name) for name in ("fill", "stroke", "outer_stroke", "accent", "line_spacing")
                      if getattr(edit, name) is not None}
            if edit.outer_stroke is not None:
                colors["outer_stroke_width"] = (style.outer_stroke_width or stroke_for(size, 0.03)) if edit.outer_stroke else 0
            updated = replace(
                current, transform=replace(transform, x=transform.x + (current.rect.width - new_width) / 2),
                rect=replace(current.rect, width=new_width, height=min(3.0, current.rect.height * ratio)),
                style=replace(style, **colors), emphasis=current.emphasis if edit.words is None else edit.words,
            )
            if updated != current:
                document = update_text_object(document, updated, profile_key=key)
                if burst_id(current.id) not in explicit:
                    document = _follow_burst(document, current, updated, key)
        else:
            updated = replace(current, transform=replace(transform, scale=current.transform.scale * edit.scale))
            if updated != current:
                document = set_profile_override(document, key, updated)
    return document
