"""基于真实视频帧的双比例历史风格封面渲染器。"""

from __future__ import annotations

import secrets
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageColor, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat

from .fonts import resolve_font_path
from .style import (
    MELODY_STYLE,
    CanvasSpec,
    CoverPalette,
    CoverTemplate,
    get_canvas_spec,
    get_palette,
    get_template,
)
from .titles import CoverLine, create_cover_copy, recommend_visual_style


PRESERVE_FRAME_MODES = {
    "evidence",
    "media",
    "gameplay",
    "series_asset",
    "full_scene",
    "poster",
    "portrait_side",
    "portrait_latest",
}

INDEPENDENT_TEXT_TEMPLATES = {
    "dialog",
    "headline",
    "evidence",
    "reaction",
    "gameplay",
}

_OUTPUT_TRANSACTION_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class TextPlacement:
    """一行文字在最终画布中的位置和样式。"""

    text: str
    role: str
    color: str
    stroke_color: str
    font_size: int
    stroke_width: int
    box: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, int | str | tuple[int, int, int, int]]:
        """返回可供前端使用的文字布局。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class TextTransform:
    """一行文字相对画布的手动位置和字号缩放。"""

    x: float
    y: float
    scale: float = 1.0


@dataclass(frozen=True, slots=True)
class StickerOverlay:
    """需要合成到封面中的一张透明贴图。"""

    asset_id: str
    image_path: str
    x: float
    y: float
    width: float
    rotation: float = 0.0


@dataclass(frozen=True, slots=True)
class StickerPlacement:
    """贴图在最终画布中的实际边界框。"""

    asset_id: str
    box: tuple[int, int, int, int]
    rotation: float

    def to_dict(self) -> dict[str, str | float | tuple[int, int, int, int]]:
        """返回适合 API 输出的贴图布局。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class RenderResult:
    """一次封面渲染的输出信息。"""

    output_path: str
    canvas_key: str
    width: int
    height: int
    template_key: str
    palette_key: str
    file_size: int
    placements: tuple[TextPlacement, ...]
    sticker_placements: tuple[StickerPlacement, ...] = ()
    background_path: str | None = None

    def to_dict(self) -> dict[str, int | str | list[dict[str, object]]]:
        """返回适合 API 序列化的渲染结果。"""

        payload: dict[str, int | str | list[dict[str, object]]] = {
            "output_path": self.output_path,
            "canvas_key": self.canvas_key,
            "width": self.width,
            "height": self.height,
            "template_key": self.template_key,
            "palette_key": self.palette_key,
            "file_size": self.file_size,
            "placements": [placement.to_dict() for placement in self.placements],
            "stickers": [placement.to_dict() for placement in self.sticker_placements],
        }
        if self.background_path is not None:
            payload["background_path"] = self.background_path
        return payload


def _load_font(size: int, font_path: str | None) -> ImageFont.ImageFont:
    if font_path:
        font = ImageFont.truetype(font_path, size=size)
        try:
            if b"Black" in font.get_variation_names():
                font.set_variation_by_name("Black")
        except (AttributeError, OSError):
            pass
        return font
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size=size)
    except OSError:
        return ImageFont.load_default(size=size)


def _focus_from_template(template: CoverTemplate) -> float:
    return {
        "left": 0.36,
        "center": 0.50,
        "right": 0.64,
        "auto": 0.50,
    }.get(template.subject_anchor, 0.50)


def _side_edge_energy(image: Image.Image) -> tuple[float, float]:
    preview = image.convert("L")
    preview.thumbnail((480, 360), Image.Resampling.LANCZOS)
    edges = preview.filter(ImageFilter.FIND_EDGES)
    top = int(edges.height * 0.08)
    bottom = int(edges.height * 0.92)
    left = edges.crop((int(edges.width * 0.05), top, int(edges.width * 0.45), bottom))
    right = edges.crop((int(edges.width * 0.55), top, int(edges.width * 0.95), bottom))
    return ImageStat.Stat(left).mean[0], ImageStat.Stat(right).mean[0]


def _third_edge_energy(image: Image.Image) -> tuple[float, float, float]:
    preview = image.convert("L")
    preview.thumbnail((480, 360), Image.Resampling.LANCZOS)
    edges = preview.filter(ImageFilter.FIND_EDGES)
    top = int(edges.height * 0.08)
    bottom = int(edges.height * 0.92)
    zones = (
        edges.crop((0, top, int(edges.width * 0.33), bottom)),
        edges.crop((int(edges.width * 0.33), top, int(edges.width * 0.67), bottom)),
        edges.crop((int(edges.width * 0.67), top, edges.width, bottom)),
    )
    return tuple(ImageStat.Stat(zone).mean[0] for zone in zones)


def _auto_adjust_text_side(image: Image.Image, template: CoverTemplate) -> CoverTemplate:
    is_portrait = image.width / image.height < 0.85
    if is_portrait and template.key in {"dialog", "headline"}:
        return replace(
            template,
            background_mode="portrait_latest",
            subject_anchor="left",
            text_anchor="left",
        )
    if template.layout == "series_center":
        if is_portrait:
            left_energy, right_energy = _side_edge_energy(image)
            if left_energy <= right_energy:
                return replace(
                    template,
                    background_mode="portrait_side",
                    subject_anchor="right",
                    text_anchor="left",
                )
            return replace(
                template,
                background_mode="portrait_side",
                subject_anchor="left",
                text_anchor="right",
            )

        left_energy, center_energy, right_energy = _third_edge_energy(image)
        if center_energy > min(left_energy, right_energy) * 1.18:
            if left_energy <= right_energy:
                return replace(template, subject_anchor="right", text_anchor="left")
            return replace(template, subject_anchor="left", text_anchor="right")
        return template

    if (
        is_portrait
        and template.text_anchor in {"left", "right"}
        and template.background_mode in {"frame", "evidence"}
    ):
        subject_anchor = "right" if template.text_anchor == "left" else "left"
        return replace(
            template,
            background_mode="portrait_side",
            subject_anchor=subject_anchor,
        )

    if template.text_anchor == "center" or template.layout not in {
        "context_quote_emphasis",
        "top_bottom",
        "evidence_split",
    }:
        return template

    left_energy, right_energy = _side_edge_energy(image)
    if left_energy > right_energy * 1.18:
        return replace(template, subject_anchor="left", text_anchor="right")
    if right_energy > left_energy * 1.18:
        return replace(template, subject_anchor="right", text_anchor="left")
    return template


def _blurred_backdrop(
    source: Image.Image,
    size: tuple[int, int],
    focus_x: float,
    *,
    brightness: float = 0.72,
) -> Image.Image:
    backdrop = ImageOps.fit(
        source,
        size,
        method=Image.Resampling.LANCZOS,
        centering=(focus_x, 0.5),
    )
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(radius=max(size) // 55))
    return ImageEnhance.Brightness(backdrop).enhance(brightness)


def compose_background(
    frame: Image.Image,
    canvas: CanvasSpec,
    template: CoverTemplate,
    *,
    focus_x: float | None = None,
    focus_y: float = 0.5,
) -> Image.Image:
    """按模板独立适配画布，避免把 16:9 成品机械裁成 4:3。"""

    source = frame.convert("RGB")
    target_size = (canvas.width, canvas.height)
    effective_focus_x = _focus_from_template(template) if focus_x is None else focus_x
    effective_focus_x = max(0.0, min(1.0, effective_focus_x))
    focus_y = max(0.0, min(1.0, focus_y))

    source_ratio = source.width / source.height
    target_ratio = canvas.aspect_ratio
    should_preserve = (
        template.background_mode in PRESERVE_FRAME_MODES
        and abs(source_ratio - target_ratio) > 0.03
    )
    if should_preserve:
        backdrop_brightness = 0.94 if template.background_mode == "portrait_latest" else 0.78
        background = _blurred_backdrop(
            source,
            target_size,
            effective_focus_x,
            brightness=backdrop_brightness,
        )
        foreground = ImageOps.contain(source, target_size, method=Image.Resampling.LANCZOS)
        if template.subject_anchor == "left":
            x = 0
        elif template.subject_anchor == "right":
            x = canvas.width - foreground.width
        else:
            x = (canvas.width - foreground.width) // 2
        y = (canvas.height - foreground.height) // 2
        background.paste(foreground, (x, y))
    else:
        background = ImageOps.fit(
            source,
            target_size,
            method=Image.Resampling.LANCZOS,
            centering=(effective_focus_x, focus_y),
        )

    if template.background_mode == "minimal":
        background = background.filter(ImageFilter.GaussianBlur(radius=max(target_size) // 80))
        background = ImageEnhance.Brightness(background).enhance(1.08)
    elif template.background_mode != "portrait_latest" and MELODY_STYLE.background_dim > 0:
        background = ImageEnhance.Brightness(background).enhance(1.0 - MELODY_STYLE.background_dim)
    return background


def _text_area(canvas: CanvasSpec, template: CoverTemplate) -> tuple[int, int, int, int]:
    left = canvas.margin_x
    right = canvas.width - canvas.margin_x
    if template.background_mode == "portrait_latest":
        # 最新双封面会按比例重新排版，但两种画布使用相同文字宽度和字号。
        left = 250 if canvas.key == "16x9" else 14
        right = left + 1412
    elif template.background_mode == "portrait_side" and template.text_anchor == "left":
        right = int(canvas.width * (0.64 if canvas.key == "16x9" else 0.55))
    elif template.background_mode == "portrait_side" and template.text_anchor == "right":
        left = int(canvas.width * (0.36 if canvas.key == "16x9" else 0.45))
    elif template.background_mode == "series_asset" and template.text_anchor == "left":
        right = int(canvas.width * 0.47)
    elif template.background_mode == "series_asset" and template.text_anchor == "right":
        left = int(canvas.width * 0.53)
    elif template.text_anchor == "left" and template.subject_anchor == "right":
        right = int(canvas.width * (0.74 if canvas.key == "16x9" else 0.78))
    elif template.text_anchor == "right" and template.subject_anchor == "left":
        left = int(canvas.width * (0.26 if canvas.key == "16x9" else 0.22))
    if template.background_mode == "portrait_latest":
        top = 10
    else:
        top = max(canvas.text_top, int(canvas.height * template.text_top_ratio))
    bottom = min(canvas.height - 32, int(canvas.height * template.text_bottom_ratio))
    return left, top, right, bottom


def _role_scale(role: str) -> float:
    return {
        "context": 0.82,
        "quote": 0.92,
        "emphasis": 1.0,
        "neutral": 0.90,
    }.get(role, 0.90)


def _measure_text(
    draw: ImageDraw.ImageDraw,
    line: CoverLine,
    base_size: int,
    font_path: str | None,
) -> tuple[ImageFont.ImageFont, int, int, int, int]:
    font_size = max(24, int(base_size * _role_scale(line.role)))
    return _measure_text_at_size(draw, line, font_size, font_path)


def _measure_text_at_size(
    draw: ImageDraw.ImageDraw,
    line: CoverLine,
    font_size: int,
    font_path: str | None,
) -> tuple[ImageFont.ImageFont, int, int, int, int]:
    """使用指定的实际字号测量一行文字。"""

    font_size = max(24, min(320, int(font_size)))
    font = _load_font(font_size, font_path)
    stroke_width = max(3, font_size // 16)
    box = draw.textbbox((0, 0), line.text, font=font, stroke_width=stroke_width)
    width = box[2] - box[0]
    height = box[3] - box[1]
    return font, font_size, stroke_width, width, height


def _manual_measurement(
    draw: ImageDraw.ImageDraw,
    line: CoverLine,
    automatic: tuple[ImageFont.ImageFont, int, int, int, int],
    transform: TextTransform,
    canvas: CanvasSpec,
    font_path: str | None,
) -> tuple[ImageFont.ImageFont, int, int, int, int]:
    """按用户缩放值重算字号，并在必要时缩小到画布内。"""

    if not 0.45 <= transform.scale <= 2.0:
        raise ValueError("文字缩放必须在 0.45 到 2.0 之间")
    desired_size = max(24, round(automatic[1] * transform.scale))
    measured = _measure_text_at_size(draw, line, desired_size, font_path)
    maximum_width = canvas.width - 16
    maximum_height = canvas.height - 16
    while (measured[3] > maximum_width or measured[4] > maximum_height) and desired_size > 24:
        desired_size = max(24, desired_size - 2)
        measured = _measure_text_at_size(draw, line, desired_size, font_path)
    return measured


def _fit_text(
    draw: ImageDraw.ImageDraw,
    lines: Sequence[CoverLine],
    area: tuple[int, int, int, int],
    font_path: str | None,
) -> tuple[int, list[tuple[ImageFont.ImageFont, int, int, int, int]], int]:
    left, top, right, bottom = area
    max_width = right - left
    max_height = bottom - top
    maximum = min(190, int(max_height / max(1.0, len(lines) * 0.82)))
    minimum = 42
    for base_size in range(maximum, minimum - 1, -4):
        measured = [_measure_text(draw, line, base_size, font_path) for line in lines]
        gap = max(10, base_size // 7)
        total_height = sum(item[4] for item in measured) + gap * (len(lines) - 1)
        if max((item[3] for item in measured), default=0) <= max_width and total_height <= max_height:
            return base_size, measured, gap
    measured = [_measure_text(draw, line, minimum, font_path) for line in lines]
    return minimum, measured, max(8, minimum // 7)


def _fit_independent_text(
    draw: ImageDraw.ImageDraw,
    lines: Sequence[CoverLine],
    area: tuple[int, int, int, int],
    font_path: str | None,
) -> tuple[int, list[tuple[ImageFont.ImageFont, int, int, int, int]], int]:
    """让高冲击模板的每句独立放大，同时确保总高度不溢出。"""

    max_width = area[2] - area[0]
    max_height = area[3] - area[1]
    base_sizes: list[int] = []
    for line in lines:
        selected_size = 42
        for base_size in range(190, 41, -4):
            candidate = _measure_text(draw, line, base_size, font_path)
            if candidate[3] <= max_width:
                selected_size = base_size
                break
        base_sizes.append(selected_size)

    while True:
        measured = [
            _measure_text(draw, line, base_size, font_path)
            for line, base_size in zip(lines, base_sizes)
        ]
        gap = max(10, min(base_sizes, default=70) // 8)
        total_height = sum(item[4] for item in measured) + gap * (len(lines) - 1)
        if total_height <= max_height or all(base_size <= 42 for base_size in base_sizes):
            return max(base_sizes, default=42), measured, gap
        base_sizes = [max(42, base_size - 4) for base_size in base_sizes]


def _stack_positions(
    area: tuple[int, int, int, int],
    measured: Sequence[tuple[ImageFont.ImageFont, int, int, int, int]],
    gap: int,
    *,
    horizontal: str,
    vertical: str,
) -> list[tuple[int, int]]:
    left, top, right, bottom = area
    total_height = sum(item[4] for item in measured) + gap * (len(measured) - 1)
    if vertical == "center":
        y = top + max(0, (bottom - top - total_height) // 2)
    elif vertical == "bottom":
        y = bottom - total_height
    else:
        y = top

    positions = []
    for item in measured:
        width, height = item[3], item[4]
        if horizontal == "center":
            x = left + max(0, (right - left - width) // 2)
        elif horizontal == "right":
            x = right - width
        else:
            x = left
        positions.append((x, y))
        y += height + gap
    return positions


def _edge_positions(
    area: tuple[int, int, int, int],
    measured: Sequence[tuple[ImageFont.ImageFont, int, int, int, int]],
    gap: int,
    *,
    horizontal: str,
) -> list[tuple[int, int]]:
    if len(measured) <= 1:
        return _stack_positions(area, measured, gap, horizontal=horizontal, vertical="bottom")

    head_count = 2 if len(measured) >= 4 else 1
    head = _stack_positions(
        area,
        measured[:head_count],
        gap,
        horizontal=horizontal,
        vertical="top",
    )
    tail = _stack_positions(
        area,
        measured[head_count:],
        gap,
        horizontal=horizontal,
        vertical="bottom",
    )
    head_bottom = head[-1][1] + measured[head_count - 1][4]
    if tail and head_bottom + gap > tail[0][1]:
        return _stack_positions(area, measured, gap, horizontal=horizontal, vertical="center")
    return head + tail


def _portrait_latest_positions(
    area: tuple[int, int, int, int],
    measured: Sequence[tuple[ImageFont.ImageFont, int, int, int, int]],
) -> list[tuple[int, int]]:
    """复现近期竖版人物封面的比例独立文字锚点。"""

    left, top, right, bottom = area
    anchor_ratios = {
        1: (0.28,),
        2: (0.08, 0.54),
        3: (0.018, 0.245, 0.511),
        4: (0.018, 0.186, 0.463, 0.682),
    }
    ratios = anchor_ratios.get(len(measured))
    if ratios is None:
        return _stack_positions(area, measured, 12, horizontal="left", vertical="center")

    span = bottom - top
    positions: list[tuple[int, int]] = []
    previous_bottom = top
    for index, (ratio, item) in enumerate(zip(ratios, measured)):
        y = max(top + round(span * ratio), previous_bottom + 10 if positions else top)
        is_short_final_line = index == len(measured) - 1 and item[3] < (right - left) * 0.90
        x = right - item[3] if is_short_final_line else left
        positions.append((x, y))
        previous_bottom = y + item[4]

    overflow = previous_bottom - bottom
    if overflow > 0:
        positions = [(x, max(top, y - overflow)) for x, y in positions]
    return positions


def _positions_for_template(
    template: CoverTemplate,
    area: tuple[int, int, int, int],
    measured: Sequence[tuple[ImageFont.ImageFont, int, int, int, int]],
    gap: int,
) -> list[tuple[int, int]]:
    horizontal = "center" if template.text_anchor == "center" else template.text_anchor
    if template.background_mode == "portrait_latest":
        return _portrait_latest_positions(area, measured)
    if template.layout in {
        "context_quote_emphasis",
        "top_bottom",
        "evidence_split",
        "media_caption",
        "game_caption",
        "edge_caption",
        "poster_minimal",
    }:
        return _edge_positions(area, measured, gap, horizontal=horizontal)
    if template.layout in {"series_center", "center"}:
        return _stack_positions(area, measured, gap, horizontal="center", vertical="center")
    return _stack_positions(area, measured, gap, horizontal=horizontal, vertical="center")


def _manual_position(
    transform: TextTransform,
    measured: tuple[ImageFont.ImageFont, int, int, int, int],
    canvas: CanvasSpec,
) -> tuple[int, int]:
    """把归一化坐标换算为画布坐标，并保证文字边界不被截断。"""

    if not 0.0 <= transform.x <= 1.0 or not 0.0 <= transform.y <= 1.0:
        raise ValueError("文字位置必须在 0 到 1 之间")
    width, height = measured[3], measured[4]
    maximum_x = max(0, canvas.width - width - 8)
    maximum_y = max(0, canvas.height - height - 8)
    return (
        min(maximum_x, max(0, round(transform.x * canvas.width))),
        min(maximum_y, max(0, round(transform.y * canvas.height))),
    )


def draw_stickers(
    image: Image.Image,
    stickers: Sequence[StickerOverlay],
    canvas: CanvasSpec,
) -> tuple[StickerPlacement, ...]:
    """按归一化位置合成贴图，并返回最终边界框。"""

    placements: list[StickerPlacement] = []
    for sticker in stickers:
        if not sticker.asset_id:
            raise ValueError("贴图素材 ID 不能为空")
        if not 0.0 <= sticker.x <= 1.0 or not 0.0 <= sticker.y <= 1.0:
            raise ValueError("贴图位置必须在 0 到 1 之间")
        if not 0.03 <= sticker.width <= 0.80:
            raise ValueError("贴图宽度必须在画布的 3% 到 80% 之间")
        if not -180.0 <= sticker.rotation <= 180.0:
            raise ValueError("贴图旋转角度必须在 -180 到 180 度之间")

        source = Path(sticker.image_path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"贴图文件不存在：{source}")
        with Image.open(source) as opened:
            overlay = opened.convert("RGBA")
        target_width = max(1, round(canvas.width * sticker.width))
        target_height = max(1, round(overlay.height * target_width / max(1, overlay.width)))
        if target_height > canvas.height * 0.92:
            target_height = round(canvas.height * 0.92)
            target_width = max(1, round(overlay.width * target_height / max(1, overlay.height)))
        overlay = overlay.resize((target_width, target_height), Image.Resampling.LANCZOS)
        if abs(sticker.rotation) >= 0.05:
            overlay = overlay.rotate(sticker.rotation, resample=Image.Resampling.BICUBIC, expand=True)

        maximum_x = max(0, canvas.width - overlay.width)
        maximum_y = max(0, canvas.height - overlay.height)
        x = min(maximum_x, max(0, round(sticker.x * canvas.width)))
        y = min(maximum_y, max(0, round(sticker.y * canvas.height)))
        image.paste(overlay, (x, y), overlay)
        placements.append(
            StickerPlacement(
                asset_id=sticker.asset_id,
                box=(x, y, x + overlay.width, y + overlay.height),
                rotation=sticker.rotation,
            )
        )
    return tuple(placements)


def _rgb(color: str) -> tuple[int, int, int]:
    return ImageColor.getrgb(color[:7])


def draw_cover_text(
    image: Image.Image,
    lines: Sequence[CoverLine],
    canvas: CanvasSpec,
    template: CoverTemplate,
    palette: CoverPalette,
    *,
    font_path: str | None = None,
    line_colors: Sequence[str] | None = None,
    line_stroke_colors: Sequence[str] | None = None,
    text_transforms: Sequence[TextTransform] | None = None,
) -> tuple[TextPlacement, ...]:
    """在安全区内排版封面大字，并返回每行实际边界框。"""

    if not lines:
        return ()
    if line_colors is not None and len(line_colors) != len(lines):
        raise ValueError("逐行颜色数量必须与封面文案行数一致")
    if line_stroke_colors is not None and len(line_stroke_colors) != len(lines):
        raise ValueError("逐行描边颜色数量必须与封面文案行数一致")
    if text_transforms is not None and len(text_transforms) != len(lines):
        raise ValueError("文字布局数量必须与封面文案行数一致")

    resolved_font = resolve_font_path(font_path)
    draw = ImageDraw.Draw(image)
    area = _text_area(canvas, template)
    if template.key in INDEPENDENT_TEXT_TEMPLATES:
        _, measured, gap = _fit_independent_text(draw, lines, area, resolved_font)
    else:
        _, measured, gap = _fit_text(draw, lines, area, resolved_font)
    if text_transforms is None:
        positions = _positions_for_template(template, area, measured, gap)
    else:
        measured = [
            _manual_measurement(draw, line, automatic, transform, canvas, resolved_font)
            for line, automatic, transform in zip(lines, measured, text_transforms)
        ]
        positions = [
            _manual_position(transform, measured_line, canvas)
            for transform, measured_line in zip(text_transforms, measured)
        ]
    placements = []
    for index, (line, measured_line, position) in enumerate(zip(lines, measured, positions)):
        font, font_size, stroke_width, width, height = measured_line
        x, y = position
        color = line_colors[index] if line_colors is not None else palette.color_for_role(line.role)
        stroke_color = (
            line_stroke_colors[index]
            if line_stroke_colors is not None
            else palette.stroke_for_role(line.role)
        )
        shadow_offset = max(2, font_size // (34 if stroke_color == "#111111" else 24))
        draw.text(
            (x + shadow_offset, y + shadow_offset),
            line.text,
            font=font,
            fill=_rgb(palette.shadow_color),
            stroke_width=stroke_width + 1,
            stroke_fill=_rgb(palette.shadow_color),
        )
        draw.text(
            (x, y),
            line.text,
            font=font,
            fill=_rgb(color),
            stroke_width=stroke_width,
            stroke_fill=_rgb(stroke_color),
        )
        placements.append(
            TextPlacement(
                text=line.text,
                role=line.role,
                color=color,
                stroke_color=stroke_color,
                font_size=font_size,
                stroke_width=stroke_width,
                box=(x, y, x + width, y + height),
            )
        )
    return tuple(placements)


def _save_jpeg(image: Image.Image, output: Path, quality: int, max_bytes: int) -> int:
    effective_quality = max(55, min(95, quality))
    while True:
        image.save(output, format="JPEG", quality=effective_quality, optimize=True, subsampling=0)
        size = output.stat().st_size
        if size <= max_bytes or effective_quality <= 55:
            return size
        effective_quality -= 5


def _temporary_output_path(output: Path, kind: str) -> Path:
    """返回与目标文件同目录、不会与并发渲染冲突的临时路径。"""

    return output.with_name(f".{output.name}.{secrets.token_hex(8)}.{kind}")


def _cleanup_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def commit_output_transaction(pending: Sequence[tuple[Path, Path]]) -> None:
    """统一提交一组已编码文件，失败时恢复提交前的全部目标。"""

    with _OUTPUT_TRANSACTION_LOCK:
        _commit_output_transaction_locked(pending)


def _commit_output_transaction_locked(pending: Sequence[tuple[Path, Path]]) -> None:
    backups: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for _, output in pending:
            if output.exists():
                backup = _temporary_output_path(output, "backup")
                output.replace(backup)
                backups.append((output, backup))

        for temporary, output in pending:
            temporary.replace(output)
            committed.append(output)
    except BaseException as error:
        rollback_errors: list[OSError] = []
        for output in reversed(committed):
            try:
                output.unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(rollback_error)
        for output, backup in reversed(backups):
            try:
                output.unlink(missing_ok=True)
                backup.replace(output)
            except OSError as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            raise RuntimeError("封面写入失败，且旧文件恢复不完整") from error
        raise
    else:
        for _, backup in backups:
            _cleanup_file(backup)


def _save_jpeg_batch(
    outputs: Sequence[tuple[Image.Image, Path]],
    quality: int,
    max_bytes: int,
) -> tuple[int, ...]:
    """先完整编码所有 JPEG，再将它们作为一个事务提交。"""

    normalized = [str(output.resolve()).casefold() for _, output in outputs]
    if len(normalized) != len(set(normalized)):
        raise ValueError("背景输出路径不能与封面输出路径相同")

    pending: list[tuple[Path, Path]] = []
    sizes: list[int] = []
    try:
        for image, output in outputs:
            temporary = _temporary_output_path(output, "tmp")
            pending.append((temporary, output))
            sizes.append(_save_jpeg(image, temporary, quality, max_bytes))
        commit_output_transaction(pending)
        return tuple(sizes)
    finally:
        for temporary, _ in pending:
            _cleanup_file(temporary)


def _jpeg_output_path(path: str | Path) -> Path:
    """规范化 JPEG 输出路径并创建父目录。"""

    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() not in {".jpg", ".jpeg"}:
        output = output.with_suffix(".jpg")
    return output


def render_cover(
    frame_path: str | Path,
    title: str,
    output_path: str | Path,
    *,
    canvas_key: str = "16x9",
    template_key: str | None = None,
    palette_key: str | None = None,
    copy_lines: Sequence[str] | None = None,
    line_colors: Sequence[str] | None = None,
    line_stroke_colors: Sequence[str] | None = None,
    text_transforms: Sequence[TextTransform] | None = None,
    stickers: Sequence[StickerOverlay] | None = None,
    font_path: str | Path | None = None,
    focus_x: float | None = None,
    focus_y: float = 0.5,
    background_output_path: str | Path | None = None,
    quality: int = 92,
    max_bytes: int = 5_000_000,
) -> RenderResult:
    """把一张真实候选帧渲染为指定比例的历史风格封面。"""

    source = Path(frame_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"候选帧不存在：{source}")
    if max_bytes <= 0:
        raise ValueError("封面体积上限必须为正数")

    recommendation = recommend_visual_style(title)
    effective_template_key = template_key or recommendation.template_key
    template = get_template(effective_template_key)
    effective_palette_key = palette_key or (
        recommendation.palette_key if template_key is None else template.default_palette_key
    )
    palette = get_palette(effective_palette_key)
    canvas = get_canvas_spec(canvas_key)

    lines: list[CoverLine] | None = None
    if copy_lines is not None:
        cleaned = [str(line).strip() for line in copy_lines if str(line).strip()]
        if not cleaned:
            raise ValueError("自定义封面文案不能为空")
        if len(cleaned) > template.max_lines:
            raise ValueError(f"{template.label} 最多支持 {template.max_lines} 行文案")
        if len(cleaned) == 1:
            roles = ["emphasis"]
        else:
            roles = ["context"] + ["quote"] * (len(cleaned) - 2) + ["emphasis"]
        lines = [CoverLine(text, role) for text, role in zip(cleaned, roles)]

    with Image.open(source) as frame:
        layout_template = _auto_adjust_text_side(frame, template)
        if lines is None:
            max_line_units = template.max_line_units
            if (
                layout_template.background_mode in {"portrait_side", "portrait_latest"}
                and template.max_lines >= 3
            ):
                max_line_units = min(max_line_units, 26)
            lines = create_cover_copy(
                title,
                template_key=effective_template_key,
                max_line_units=max_line_units,
            )
        image = compose_background(
            frame,
            canvas,
            layout_template,
            focus_x=focus_x,
            focus_y=focus_y,
        )
    output = _jpeg_output_path(output_path)
    base_output = (
        _jpeg_output_path(background_output_path) if background_output_path is not None else None
    )
    background_image = image.copy() if base_output is not None else None
    try:
        sticker_placements = draw_stickers(image, stickers or (), canvas)
        assert lines is not None
        placements = draw_cover_text(
            image,
            lines,
            canvas,
            layout_template,
            palette,
            font_path=str(font_path) if font_path is not None else None,
            line_colors=line_colors,
            line_stroke_colors=line_stroke_colors,
            text_transforms=text_transforms,
        )

        jpeg_outputs: list[tuple[Image.Image, Path]] = []
        if background_image is not None and base_output is not None:
            jpeg_outputs.append((background_image, base_output))
        jpeg_outputs.append((image, output))
        file_sizes = _save_jpeg_batch(jpeg_outputs, quality, max_bytes)
    finally:
        if background_image is not None:
            background_image.close()
        image.close()

    file_size = file_sizes[-1]
    background_path = str(base_output.resolve()) if base_output is not None else None
    return RenderResult(
        output_path=str(output.resolve()),
        canvas_key=canvas.key,
        width=canvas.width,
        height=canvas.height,
        template_key=template.key,
        palette_key=palette.key,
        file_size=file_size,
        placements=placements,
        sticker_placements=sticker_placements,
        background_path=background_path,
    )
