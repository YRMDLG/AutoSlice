"""封面修改的场景描述和成品检查；保留原有叠放与裁切，只拦新增问题。"""

from __future__ import annotations

import io
import math

from PIL import Image, ImageChops, ImageStat

from autoslice_cover.document_layout import Box

from .cover_layout import canvas_size, overlay_geometry, shape_geometry, text_layout
from .cover_model import (
    BackgroundObject,
    CoverDocument,
    ImageObject,
    ShapeObject,
    StickerObject,
    TextObject,
    object_for_profile,
)


def object_bounds(item, key: str) -> Box | None:
    """估计实际可见范围，包含文字外描边和旋转，供描述与增量检查共用。"""

    size = canvas_size(key)
    if isinstance(item, TextObject):
        layout = text_layout(item, size)
        ink = layout.ink
        pivot = (layout.area.center_x, layout.area.center_y)
        pad = item.style.outer_stroke_width if item.style.outer_stroke else 0
        box = Box(ink.left - pad, ink.top - pad, ink.width + 2 * pad, ink.height + 2 * pad)
    elif isinstance(item, (ImageObject, StickerObject)):
        box = overlay_geometry(item, size)
    elif isinstance(item, ShapeObject):
        box = shape_geometry(item, size)
    else:
        return None
    if box is None:
        return None
    if not isinstance(item, TextObject):
        pivot = (box.center_x, box.center_y)
    angle = math.radians(item.transform.rotation)
    dx, dy = box.center_x - pivot[0], box.center_y - pivot[1]
    center_x = pivot[0] + dx * math.cos(angle) - dy * math.sin(angle)
    center_y = pivot[1] + dx * math.sin(angle) + dy * math.cos(angle)
    width = abs(box.width * math.cos(angle)) + abs(box.height * math.sin(angle))
    height = abs(box.width * math.sin(angle)) + abs(box.height * math.cos(angle))
    return Box(center_x - width / 2, center_y - height / 2, width, height)


def describe_scene(document: CoverDocument, key: str) -> tuple[dict[str, object], ...]:
    """完整场景只描述对象和几何，不把本机素材路径交给 AI。"""

    width, height = canvas_size(key)
    objects = []
    for base in document.objects:
        item = object_for_profile(document, base.id, key)
        row = {
            "id": item.id, "kind": item.kind, "visible": item.visible, "locked": item.locked,
            "x": round(item.transform.x, 4), "y": round(item.transform.y, 4),
            "scale": item.transform.scale, "rotation": item.transform.rotation, "z": item.z_index,
        }
        box = object_bounds(item, key) if item.visible else None
        if box is not None:
            row["bounds"] = [round(box.left / width, 4), round(box.top / height, 4),
                             round(box.right / width, 4), round(box.bottom / height, 4)]
        if isinstance(item, TextObject):
            row.update(
                text=item.text, font_size=item.style.font_size,
                font_family=item.style.font_family.replace("\\", "/").rsplit("/", 1)[-1],
                line_spacing=item.style.line_spacing, fill=item.style.fill, stroke=item.style.stroke,
                outer_stroke=item.style.outer_stroke, accent=item.style.accent, words=list(item.emphasis),
                rect_width=item.rect.width, align=item.align,
            )
        elif isinstance(item, BackgroundObject):
            row.update(zoom=item.scale, pan_x=item.pan_x, pan_y=item.pan_y, dim=item.dim, blur=item.blur)
        elif isinstance(item, ShapeObject):
            row.update(shape=item.shape_type, fill=item.fill, stroke=item.stroke)
        elif isinstance(item, ImageObject):
            row["pin_bottom"] = item.pin_bottom
        objects.append(row)
    return tuple(objects)


def _overlap(first: Box, second: Box) -> float:
    return max(0.0, min(first.right, second.right) - max(first.left, second.left)) * max(
        0.0, min(first.bottom, second.bottom) - max(first.top, second.top),
    )


def safe_candidate(before: CoverDocument, after: CoverDocument, key: str) -> bool:
    """保护锁定与当前隐藏对象；共享样式可同步到另一比例，布局不得变坏。"""

    for profile in before.profiles:
        for base in before.objects:
            old = object_for_profile(before, base.id, profile)
            new = object_for_profile(after, base.id, profile)
            if new is None or (old.locked or profile == key and not old.visible) and old != new:
                return False
            if not old.visible and (new.visible or old.transform != new.transform):
                return False
            if isinstance(old, TextObject) and (not isinstance(new, TextObject) or old.text != new.text):
                return False
    return all(_safe_geometry(before, after, profile) for profile in before.profiles)


def _safe_geometry(before: CoverDocument, after: CoverDocument, key: str) -> bool:
    """共享样式可能改变其它比例的断行，各比例都检查新增越界和遮挡。"""

    width, height = canvas_size(key)
    canvas = Box(0, 0, width, height)
    pairs = []
    for base in before.objects:
        old = object_for_profile(before, base.id, key)
        new = object_for_profile(after, base.id, key)
        if isinstance(old, (ImageObject, StickerObject, ShapeObject)) and old.visible and old != new:
            old_box, new_box = object_bounds(old, key), object_bounds(new, key)
            if old_box is not None and new_box is not None:
                old_share = _overlap(old_box, canvas) / max(1.0, old_box.width * old_box.height)
                new_share = _overlap(new_box, canvas) / max(1.0, new_box.width * new_box.height)
                if new_share < old_share - 0.12:
                    return False
        if not isinstance(old, TextObject) or not old.visible or not old.text.strip():
            continue
        old_box, new_box = object_bounds(old, key), object_bounds(new, key)
        old_area = max(1.0, old_box.width * old_box.height)
        new_area = max(1.0, new_box.width * new_box.height)
        old_clip, new_clip = 1 - _overlap(old_box, canvas) / old_area, 1 - _overlap(new_box, canvas) / new_area
        if new_clip > old_clip + 0.025:
            return False
        if new.style.font_size < old.style.font_size * 0.8 and new.style.font_size * new.transform.scale / height < 0.025:
            return False
        pairs.append((old_box, new_box, old_area, new_area))
    for index, (old, new, old_area, new_area) in enumerate(pairs):
        for other_old, other_new, other_old_area, other_new_area in pairs[index + 1:]:
            old_hit = _overlap(old, other_old) / min(old_area, other_old_area)
            new_hit = _overlap(new, other_new) / min(new_area, other_new_area)
            if new_hit > 0.3 and new_hit > old_hit + 0.12:
                return False
    return True


def visually_distinct(first: bytes, second: bytes) -> bool:
    """按实际缩略图排除无感变化和近似重复，忽略少量抗锯齿差异。"""

    with Image.open(io.BytesIO(first)) as image:
        a = image.convert("RGB")
    with Image.open(io.BytesIO(second)) as image:
        b = image.convert("RGB")
    a.thumbnail((240, 180), Image.Resampling.LANCZOS)
    b = b.resize(a.size, Image.Resampling.LANCZOS)
    difference = ImageChops.difference(a, b)
    mean = sum(ImageStat.Stat(difference).mean) / 3
    channels = difference.split()
    strongest = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    changed = sum(strongest.histogram()[17:]) / max(1, a.width * a.height)
    return mean >= 0.35 and changed >= 0.006
