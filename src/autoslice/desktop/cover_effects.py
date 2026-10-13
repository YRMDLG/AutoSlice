"""一键精修效果：给关键词垫爆炸贴；把抠出来的人物作为图层放进封面。纯函数，编辑器按钮和“AI 改一改”共用。"""

from __future__ import annotations

import math
from dataclasses import replace

from autoslice_cover.document_layout import OVERLAY_BASE_WIDTH, Box, TextLayout, load_font

from .cover_cutout import PersonCutout
from .cover_layout import background_geometry, canvas_size, text_area, text_layout
from .cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    ShapeObject,
    TextObject,
    Transform,
    insert_overlay,
    object_for_profile,
    set_profile_override,
)
from .cover_style import luminance

# 爆炸贴比字框宽多少倍、高多少倍（尖角要露出来）；高至少是宽的这么多，不会扁成一条。
_BURST_PAD_X = 1.45
_BURST_PAD_Y = 2.1
_BURST_MIN_ASPECT = 0.55
_BURST_RED, _BURST_YELLOW, _BURST_INK = "#FF3B30", "#FFE438", "#111111"


def add_person(document: CoverDocument, person: PersonCutout, *, object_id: str) -> CoverDocument:
    """把抠出来的人物作为图层放进封面：两个比例都先对准底图里那个人（看起来没动），之后能单独拖动缩放。

    放在其他素材和字下面；底图怎么虚化、压暗都不影响它。人物被画面下沿切开时默认贴住底边。
    """

    background = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
    if background is None:
        return document
    others = [item for item in document.objects if not isinstance(item, BackgroundObject)]
    layer = ImageObject(
        id=object_id, asset=AssetRef(path=str(person.plain)),
        z_index=min((item.z_index for item in others), default=2) - 1,
        # 人物被画面下沿切开时，下沿要一直贴着封面底边，否则缩小、挪动后露出切口。
        pin_bottom=person.box[3] >= person.source_size[1] - 2,
    )

    def placed(key: str) -> ImageObject:
        size = canvas_size(key)
        current = object_for_profile(document, background.id, key)
        drawn = background_geometry(current if isinstance(current, BackgroundObject) else background, size, person.source_size)
        factor = drawn.width / max(1, person.source_size[0])
        x0, y0, x1, _y1 = person.box
        return replace(layer, transform=Transform(
            x=(drawn.left + x0 * factor) / size[0], y=(drawn.top + y0 * factor) / size[1],
            scale=(x1 - x0) * factor / size[0] / OVERLAY_BASE_WIDTH,
        ))

    document = replace(document, objects=(*document.objects, placed(document.active_profile)), selected_object_id=object_id)
    for key in document.profiles:
        document = set_profile_override(document, key, placed(key))
    return document


def burst_id(text_id: str) -> str:
    return f"burst-{text_id}"


def _accent_box(layout: TextLayout) -> Box:
    """第一个强调词（连着的强调片段）在画布上的框；没有强调词就是整段字的墨迹框。"""

    for line in layout.lines:
        spans: list[tuple[float, float]] = []
        for run in line.runs:
            if run.accent:
                left = line.origin_x + run.offset
                spans.append((left, left + load_font(run.font_path, layout.font_size, layout.font_weight).getlength(run.text)))
            elif spans:
                break
        if spans:
            return Box(spans[0][0], line.ink.top, spans[-1][1] - spans[0][0], line.ink.height)
    return layout.ink


def _burst_for(text: TextObject, profile_key: str) -> ShapeObject:
    width, height = canvas_size(profile_key)
    box = _accent_box(text_layout(text, (width, height)))
    burst_width = min(width, box.width * _BURST_PAD_X)
    burst_height = min(height, max(box.height * _BURST_PAD_Y, burst_width * _BURST_MIN_ASPECT))
    # 字转过角度时，爆炸贴的中心跟着绕字框中心转过去。
    area = text_area(text, (width, height))
    angle = math.radians(text.transform.rotation)
    dx, dy = box.center_x - area.center_x, box.center_y - area.center_y
    center_x = area.center_x + dx * math.cos(angle) - dy * math.sin(angle)
    center_y = area.center_y + dx * math.sin(angle) + dy * math.cos(angle)
    fill = text.style.fill_color
    # 红字配黄爆炸贴，其他配红的；描边黑色，粗细跟字号走。
    reddish = fill.startswith("#") and len(fill) >= 7 and int(fill[1:3], 16) > 200 and int(fill[3:5], 16) < 120
    return ShapeObject(
        id=burst_id(text.id), shape_type="burst",
        fill=_BURST_YELLOW if reddish or luminance(fill) < 0.2 else _BURST_RED,
        stroke=_BURST_INK, stroke_width=max(4, round(text.style.font_size * 0.05)),
        width=burst_width / width, height=burst_height / height,
        transform=Transform(
            x=(center_x - burst_width / 2) / width, y=(center_y - burst_height / 2) / height,
            rotation=text.transform.rotation,
        ),
    )


def add_burst(document: CoverDocument, text_id: str, profile_key: str) -> CoverDocument:
    """在这段字的强调词（没有就整段字）后面垫一个爆炸贴，放在所有字的下面；两个比例各按自己的字位置放。

    已经垫过就原样返回。
    """

    if any(item.id == burst_id(text_id) for item in document.objects):
        return document
    current = object_for_profile(document, text_id, profile_key)
    if not isinstance(current, TextObject) or not current.visible or not current.text.strip():
        return document
    document = insert_overlay(document, _burst_for(current, profile_key))
    shape = next(item for item in document.objects if item.id == burst_id(text_id))
    for key in document.profiles:
        text = object_for_profile(document, text_id, key)
        if isinstance(text, TextObject) and text.text.strip():
            placed = _burst_for(text, key)
            document = set_profile_override(
                document, key, replace(shape, width=placed.width, height=placed.height, transform=placed.transform),
            )
    return document
