"""“AI 改一改”的每种修改怎么落到文档上，以及告诉 AI 封面现在是什么样。

都是纯函数：编辑器先用 apply_fix 算出改后的样子做预览、丢掉没变化的建议（移字挪得太少也算没变化），
用户点“应用”时再对当前文档算一次写回（期间用户可能又改过）。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from autoslice_cover.document_layout import clamp_background_scale

from .cover_ai import AIFix
from .cover_autolayout import primary_copy_ids, replace_copy
from .cover_copy import BasicCoverCopy
from .cover_layout import canvas_size, text_layout
from .cover_model import (
    BackgroundObject,
    CoverDocument,
    TextObject,
    emphasis_words,
    object_for_profile,
    resize_text_style,
    set_profile_override,
    stroke_for,
    update_text_object,
)
from .cover_style import STYLE_PRESETS, luminance

# 一步的幅度：拉近/拉远、主文案放大、A 缩小。
FIX_ZOOM = 1.25
FIX_GROW = 1.3
FIX_SHRINK = 0.8
# 倾斜角度（负数是往左上扬，和画布一致）；已经转过这么多度的不再转。
FIX_TILT = -6.0
_TILTED = 2.0
# 外描边粗细占字号的比例（同“双层描边”预设）。
FIX_OUTLINE_RATIO = 0.06
# 移字：整组文字的中心挪动不到画布高度的这么多，算没挪（本来就在那儿）。
_MIN_MOVE = 0.15
# 改完后文字离画布边缘至少留这么多（画布比例）。
_EDGE = 0.02


def _visible_texts(document: CoverDocument, profile_key: str) -> dict[str, TextObject]:
    """当前比例下看得见的 A/B 主文案。"""

    texts = {}
    for role, object_id in primary_copy_ids(document).items():
        current = object_for_profile(document, object_id, profile_key)
        if isinstance(current, TextObject) and current.visible and current.text.strip():
            texts[role] = current
    return texts


def _text_center(document: CoverDocument, profile_key: str) -> float:
    """A/B 整组文字在画布上的竖直中心（0 顶 1 底）；没有字时为 0.5。"""

    width, height = canvas_size(profile_key)
    inks = [text_layout(item, (width, height)).ink for item in _visible_texts(document, profile_key).values()]
    if not inks:
        return 0.5
    return (min(ink.top for ink in inks) + max(ink.bottom for ink in inks)) / 2 / height


def describe_state(document: CoverDocument, profile_key: str) -> str:
    """封面现在的样子，一句话给 AI：已强调的词、倾斜、外描边、配色、底图放大倍数。"""

    texts = _visible_texts(document, profile_key)
    words = [word for item in texts.values() for word in item.emphasis if word in item.text]
    headline = texts.get("B")
    parts = ["强调词：" + ("「" + "」「".join(words) + "」" if words else "无")]
    parts.append(f"字倾斜：{'有' if any(abs(item.transform.rotation) >= _TILTED for item in texts.values()) else '无'}")
    parts.append(f"外描边：{'有' if any(item.style.outer_stroke for item in texts.values()) else '无'}")
    if headline is not None:
        preset = next(
            (item for item in STYLE_PRESETS
             if item.fill.upper() == headline.style.fill.upper() and item.stroke.upper() == headline.style.stroke.upper()),
            None,
        )
        parts.append(f"配色：{preset.label if preset else '自定义'}")
    background = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
    current = object_for_profile(document, background.id, profile_key) if background is not None else None
    if isinstance(current, BackgroundObject):
        parts.append(f"画面放大 {current.scale:.1f} 倍")
    return "；".join(parts)


def apply_fix(service: Any, document: CoverDocument, image_path: str | Path, fix: AIFix, profile_key: str) -> CoverDocument:
    """一条建议（几个操作按顺序做）作用到文档上的结果；做不到或已经是这样时原样返回。service 只用来重排文字。"""

    for action in fix.actions:
        document = _apply_action(service, document, image_path, fix, action, profile_key)
    return _keep_inside(document, profile_key)


def _keep_inside(document: CoverDocument, profile_key: str) -> CoverDocument:
    """字放大、倾斜、加描边后可能出画：A/B 整组上下挪回画面里（保持上下关系），各自左右挪回。"""

    width, height = canvas_size(profile_key)
    margin_x, margin_y = width * _EDGE, height * _EDGE
    texts = _visible_texts(document, profile_key)
    inks = {role: text_layout(item, (width, height)).ink for role, item in texts.items()}
    if not inks:
        return document
    top = min(ink.top for ink in inks.values())
    bottom = max(ink.bottom for ink in inks.values())
    dy = (height - margin_y - bottom) if bottom > height - margin_y else (margin_y - top) if top < margin_y else 0.0
    for role, item in texts.items():
        ink = inks[role]
        dx = (width - margin_x - ink.right) if ink.right > width - margin_x else (margin_x - ink.left) if ink.left < margin_x else 0.0
        if dx or dy:
            moved = replace(item, transform=replace(item.transform, x=item.transform.x + dx / width, y=item.transform.y + dy / height))
            document = update_text_object(document, moved, profile_key=profile_key)
    return document


def _apply_action(
    service: Any, document: CoverDocument, image_path: str | Path, fix: AIFix, action: str, profile_key: str,
) -> CoverDocument:
    key = profile_key
    texts = _visible_texts(document, key)
    if action == "rewrite":
        return replace_copy(document, BasicCoverCopy(context=fix.context, headline=fix.headline), key)
    if action in ("move_bottom", "move_top"):
        # 只重排字（引子在上），调好的取景不动；本来就在那一边就不动。
        moved = service.apply_auto_layout(
            document, image_path, mode="lead", position="bottom" if action == "move_bottom" else "top", reframe=False,
        )
        return moved if abs(_text_center(moved, key) - _text_center(document, key)) >= _MIN_MOVE else document
    if action in ("zoom_in", "zoom_out"):
        background = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
        current = object_for_profile(document, background.id, key) if background is not None else None
        if not isinstance(current, BackgroundObject):
            return document
        factor = FIX_ZOOM if action == "zoom_in" else 1 / FIX_ZOOM
        scale = clamp_background_scale(max(1.0, current.scale * factor))
        return set_profile_override(document, key, replace(current, scale=scale)) if scale != current.scale else document
    for role, current in texts.items():
        updated = current
        if action == "emphasize":
            # 强调词落到含有它的 A/B 上，和已有的强调词合并。
            words = tuple(word for word in fix.words if word in current.text)
            if words:
                updated = replace(current, emphasis=emphasis_words((*current.emphasis, *words)))
        elif action == "bigger" and role == "B" or action == "smaller_context" and role == "A":
            size = max(24, round(current.style.font_size * (FIX_GROW if role == "B" else FIX_SHRINK)))
            ratio = size / max(1, current.style.font_size)
            new_width = min(3.0, current.rect.width * ratio)
            # 以文字框中线为准放大缩小，居中的字不会往右跑。
            updated = replace(
                current, style=resize_text_style(current.style, size),
                transform=replace(current.transform, x=current.transform.x + (current.rect.width - new_width) / 2),
                rect=replace(current.rect, width=new_width, height=min(3.0, current.rect.height * ratio)),
                wrap=replace(current.wrap, max_width=min(1.0, current.wrap.max_width * ratio)),
            )
        elif action == "tilt" and abs(current.transform.rotation) < _TILTED:
            updated = replace(current, transform=replace(current.transform, rotation=FIX_TILT))
        elif action == "outline" and not current.style.outer_stroke:
            # 描边是浅色（白边彩字）时外圈用黑色，不然外圈白色。
            outer = "#111111" if luminance(current.style.stroke) > 0.6 else "#FFFFFF"
            updated = replace(current, style=replace(
                current.style, outer_stroke=outer, outer_stroke_width=stroke_for(current.style.font_size, FIX_OUTLINE_RATIO),
            ))
        elif action == "restyle":
            preset = next((item for item in STYLE_PRESETS if item.key == fix.preset), None)
            if preset is not None:
                updated = replace(current, style=preset.apply(current.style, role))
        if updated != current:
            document = update_text_object(document, updated, profile_key=key)
    return document
