"""方案的增量检查：避让用户对象，同时容许封面原本有意的叠放和裁切。"""

from __future__ import annotations

from autoslice_cover.document_layout import Box

from .cover_effects import burst_id
from .cover_fix_quality import object_bounds
from .cover_layout import canvas_size
from .cover_model import (
    BackgroundObject,
    CoverDocument,
    ShapeObject,
    TextObject,
    object_for_profile,
)


def _overlap(a: Box | None, b: Box | None) -> float:
    if a is None or b is None:
        return 0.0
    area = max(0.0, min(a.right, b.right) - max(a.left, b.left)) * max(
        0.0, min(a.bottom, b.bottom) - max(a.top, b.top),
    )
    return area / max(1.0, min(a.width * a.height, b.width * b.height))


def _inside(box: Box | None, canvas: Box) -> float:
    if box is None:
        return 1.0
    width = max(0.0, min(box.right, canvas.right) - max(box.left, canvas.left))
    height = max(0.0, min(box.bottom, canvas.bottom) - max(box.top, canvas.top))
    return width * height / max(1.0, box.width * box.height)


def safe_scheme(before: CoverDocument, after: CoverDocument, primary: set[str]) -> bool:
    """两个比例都不新增截字或挤压；附着在本段文字后的爆炸贴是有意叠放。"""

    for key in after.profiles:
        width, height = canvas_size(key)
        canvas = Box(0, 0, width, height)
        for text_id in primary:
            old = object_for_profile(before, text_id, key)
            new = object_for_profile(after, text_id, key)
            if not isinstance(new, TextObject) or not new.visible or not new.text.strip():
                continue
            old_box = object_bounds(old, key) if isinstance(old, TextObject) and old.visible else None
            box = object_bounds(new, key)
            if _inside(box, canvas) + 0.025 < _inside(old_box, canvas):
                return False
            for base in after.objects:
                other = object_for_profile(after, base.id, key)
                if (
                    other.id in {text_id, burst_id(text_id)} or not other.visible
                    or isinstance(other, BackgroundObject)
                    or isinstance(other, ShapeObject) and other.shape_type == "focus"
                    or isinstance(other, TextObject) and not other.text.strip()
                ):
                    continue
                previous = object_for_profile(before, base.id, key)
                previous_box = object_bounds(previous, key) if previous is not None and previous.visible else None
                hit = _overlap(box, object_bounds(other, key))
                if hit > 0.12 and hit > _overlap(old_box, previous_box) + 0.05:
                    return False
    return True
