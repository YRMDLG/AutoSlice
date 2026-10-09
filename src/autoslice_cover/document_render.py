"""按 ``document_layout`` 的几何把 AutoCover 文档合成为图片（Pillow 端）。

图层统一按 z 顺序绘制；旋转一律绕对象自身中心、正角度为顺时针，
与 Qt 画布一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageColor, ImageDraw

from .document_layout import (
    BACKGROUND_FILL,
    SHADOW_ALPHA,
    Box,
    TextLayout,
    arrow_head_size,
    backdrop_box,
    backdrop_radius,
    load_font,
    shadow_offset,
)


@dataclass(frozen=True, slots=True)
class TextPaint:
    """文字着色：外描边与底条为可选效果。"""

    fill: str
    stroke: str
    stroke_width: int
    shadow: bool = True
    outer_stroke: str | None = None
    outer_stroke_width: int = 0
    backdrop: str | None = None
    # 强调词的填充色（排版里 accent 的片段）；描边、阴影与正文相同。
    accent: str | None = None


@dataclass(frozen=True, slots=True)
class BackgroundLayer:
    path: str
    box: Box


@dataclass(frozen=True, slots=True)
class TextLayer:
    layout: TextLayout
    paint: TextPaint
    rotation: float = 0.0


@dataclass(frozen=True, slots=True)
class ImageLayer:
    path: str
    box: Box
    rotation: float = 0.0
    opacity: float = 1.0


@dataclass(frozen=True, slots=True)
class ShapeLayer:
    shape_type: str
    box: Box
    stroke: str
    stroke_width: int
    fill: str | None = None
    rotation: float = 0.0


Layer = TextLayer | ImageLayer | ShapeLayer


def rgba(color: str | None, default: str = "#000000") -> tuple[int, int, int, int]:
    """解析 #RGB/#RRGGBB/#RRGGBBAA；无效值回落默认色。"""

    value = (color or default).strip()
    try:
        if value.startswith("#") and len(value) == 9:
            red, green, blue = ImageColor.getrgb(value[:7])
            return red, green, blue, int(value[7:9], 16)
        red, green, blue = ImageColor.getrgb(value)[:3]
        return red, green, blue, 255
    except ValueError:
        return rgba(default) if value != default else (0, 0, 0, 255)


def _composite(base: Image.Image, layer: Image.Image, x: int, y: int) -> None:
    """支持负偏移和越界的 alpha 合成。"""

    left, top = max(0, x), max(0, y)
    right, bottom = min(base.width, x + layer.width), min(base.height, y + layer.height)
    if right <= left or bottom <= top:
        return
    region = layer.crop((left - x, top - y, right - x, bottom - y))
    base.alpha_composite(region, (left, top))


def _centered_canvas(content: Box, center_x: float, center_y: float, margin: float) -> Box:
    """以旋转中心为圆心、能容纳内容任意旋转的正方形局部画布。"""

    corners = (
        (content.left, content.top), (content.right, content.top),
        (content.left, content.bottom), (content.right, content.bottom),
    )
    radius = max(math.hypot(px - center_x, py - center_y) for px, py in corners) + margin
    half = math.ceil(radius)
    return Box(round(center_x) - half, round(center_y) - half, half * 2, half * 2)


def _rotate_and_composite(base: Image.Image, layer: Image.Image, region: Box, rotation: float) -> None:
    if abs(rotation) >= 0.05:
        layer = layer.rotate(-rotation, resample=Image.Resampling.BICUBIC)
    _composite(base, layer, round(region.left), round(region.top))


def paste_background(base: Image.Image, layer: BackgroundLayer) -> None:
    visible = layer.box.intersection(Box(0, 0, base.width, base.height))
    if visible is None:
        return
    with Image.open(layer.path) as source:
        source = source.convert("RGB")
        factor = layer.box.width / max(1, source.width)
        crop = (
            (visible.left - layer.box.left) / factor,
            (visible.top - layer.box.top) / factor,
            (visible.right - layer.box.left) / factor,
            (visible.bottom - layer.box.top) / factor,
        )
        size = (max(1, round(visible.width)), max(1, round(visible.height)))
        region = source.resize(size, Image.Resampling.LANCZOS, box=crop)
    base.paste(region, (round(visible.left), round(visible.top)))


def _draw_glyphs(
    draw: ImageDraw.ImageDraw,
    layout: TextLayout,
    dx: float,
    dy: float,
    *,
    fill: tuple[int, int, int, int],
    stroke_width: int,
    stroke_fill: tuple[int, int, int, int],
    emoji: bool,
    accent: tuple[int, int, int, int] | None = None,
) -> None:
    for line in layout.lines:
        for run in line.runs:
            font = load_font(run.font_path, layout.font_size, layout.font_weight)
            position = (line.origin_x + run.offset + dx, line.baseline + dy)
            if run.emoji:
                # 表情不描边、不投影，只在正文层绘制彩色字形。
                if emoji:
                    draw.text(position, run.text, font=font, embedded_color=True, anchor="ls")
                continue
            draw.text(
                position, run.text, font=font, fill=accent if run.accent and accent else fill,
                stroke_width=stroke_width, stroke_fill=stroke_fill, anchor="ls",
            )


def draw_text_layer(base: Image.Image, layer: TextLayer) -> None:
    layout, paint = layer.layout, layer.paint
    if not layout.lines:
        return
    stroke = max(0, int(paint.stroke_width))
    outer = max(0, int(paint.outer_stroke_width)) if paint.outer_stroke else 0
    offset = shadow_offset(layout.font_size)
    content = layout.ink
    backdrop = backdrop_box(layout.ink, layout.font_size) if paint.backdrop else None
    if backdrop is not None:
        content = content.union(backdrop)
    region = _centered_canvas(content.union(layout.area), layout.area.center_x, layout.area.center_y, offset + 4)
    dx, dy = -region.left, -region.top
    size = (int(region.width), int(region.height))
    layer_image = Image.new("RGBA", size, (0, 0, 0, 0))
    if backdrop is not None:
        ImageDraw.Draw(layer_image).rounded_rectangle(
            (backdrop.left + dx, backdrop.top + dy, backdrop.right + dx, backdrop.bottom + dy),
            radius=backdrop_radius(layout.font_size),
            fill=rgba(paint.backdrop),
        )
    if paint.shadow:
        # 不透明绘制后整体降透明度，避免描边与填充重叠处加深。
        shadow = Image.new("RGBA", size, (0, 0, 0, 0))
        _draw_glyphs(
            ImageDraw.Draw(shadow), layout, dx + offset, dy + offset,
            fill=(0, 0, 0, 255), stroke_width=stroke + outer + 1, stroke_fill=(0, 0, 0, 255), emoji=False,
        )
        shadow.putalpha(shadow.getchannel("A").point(lambda value: value * SHADOW_ALPHA // 255))
        layer_image.alpha_composite(shadow)
    draw = ImageDraw.Draw(layer_image)
    if outer:
        outer_color = rgba(paint.outer_stroke)
        _draw_glyphs(draw, layout, dx, dy, fill=outer_color, stroke_width=stroke + outer, stroke_fill=outer_color, emoji=False)
    fill, stroke_fill = rgba(paint.fill, "#FFE438"), rgba(paint.stroke, "#111111")
    if paint.accent and stroke and any(run.accent for line in layout.lines for run in line.runs):
        # 有强调词时先整段描边再整段填色，避免后一段的描边压到前一段的字（与单次绘制逐像素一致）。
        _draw_glyphs(draw, layout, dx, dy, fill=stroke_fill, stroke_width=stroke, stroke_fill=stroke_fill, emoji=False)
        _draw_glyphs(
            draw, layout, dx, dy, fill=fill, stroke_width=0, stroke_fill=stroke_fill, emoji=True, accent=rgba(paint.accent),
        )
    else:
        _draw_glyphs(
            draw, layout, dx, dy, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill, emoji=True,
            accent=rgba(paint.accent) if paint.accent else None,
        )
    _rotate_and_composite(base, layer_image, region, layer.rotation)


def draw_image_layer(base: Image.Image, layer: ImageLayer) -> None:
    if not Path(layer.path).is_file():
        return
    with Image.open(layer.path) as source:
        image = source.convert("RGBA").resize(
            (max(1, round(layer.box.width)), max(1, round(layer.box.height))),
            Image.Resampling.LANCZOS,
        )
    opacity = max(0.0, min(1.0, float(layer.opacity)))
    if opacity < 1.0:
        image.putalpha(image.getchannel("A").point(lambda value: round(value * opacity)))
    if abs(layer.rotation) >= 0.05:
        image = image.rotate(-layer.rotation, resample=Image.Resampling.BICUBIC, expand=True)
    _composite(
        base, image,
        round(layer.box.center_x - image.width / 2), round(layer.box.center_y - image.height / 2),
    )


def draw_shape_layer(base: Image.Image, layer: ShapeLayer) -> None:
    box = layer.box
    width = max(1, int(layer.stroke_width))
    region = _centered_canvas(box, box.center_x, box.center_y, width + 4)
    dx, dy = -region.left, -region.top
    image = Image.new("RGBA", (int(region.width), int(region.height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = rgba(layer.stroke, "#FFDB4D")
    fill = rgba(layer.fill) if layer.fill else None
    # 描边以框线为中心，与 Qt QPen 的绘制方式一致。
    half = width / 2.0
    outline = (box.left + dx - half, box.top + dy - half, box.right + dx + half, box.bottom + dy + half)
    inner = (box.left + dx + half, box.top + dy + half, box.right + dx - half, box.bottom + dy - half)
    if layer.shape_type == "circle":
        if fill:
            draw.ellipse(inner, fill=fill)
        draw.ellipse(outline, outline=stroke, width=width)
    elif layer.shape_type == "arrow":
        start = (box.left + dx, box.bottom + dy)
        end = (box.right + dx, box.top + dy)
        draw.line((start, end), fill=stroke, width=width)
        head = arrow_head_size(box)
        draw.polygon((end, (end[0] - head, end[1]), (end[0], end[1] + head)), fill=stroke)
    else:
        if fill:
            draw.rectangle(inner, fill=fill)
        draw.rectangle(outline, outline=stroke, width=width)
    _rotate_and_composite(base, image, region, layer.rotation)


def compose_document(
    canvas_size: tuple[int, int],
    background: BackgroundLayer | None,
    layers: Sequence[Layer],
) -> Image.Image:
    """按给定顺序合成，返回 RGB 成品。layers 须已按 z 排序。"""

    base = Image.new("RGBA", canvas_size, rgba(BACKGROUND_FILL))
    if background is not None and Path(background.path).is_file():
        paste_background(base, background)
    for layer in layers:
        if isinstance(layer, TextLayer):
            draw_text_layer(base, layer)
        elif isinstance(layer, ImageLayer):
            draw_image_layer(base, layer)
        elif isinstance(layer, ShapeLayer):
            draw_shape_layer(base, layer)
    return base.convert("RGB")
