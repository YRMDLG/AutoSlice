"""CoverDocument 到共享排版与图层的适配。

画布显示与 Pillow 导出都经由本模块取得几何、字号和断行，保证所见即所得。
坐标单位为当前比例的导出像素。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import Image

from autoslice_cover.document_layout import (
    CONTEXT_MIN_FONT_SIZE,
    EMPHASIS_MIN_FONT_SIZE,
    Box,
    TextLayout,
    background_box,
    layout_text,
    overlay_box,
    shape_box,
    wrap_text,
)
from autoslice_cover.document_render import (
    BackgroundLayer,
    ImageLayer,
    Layer,
    ShapeLayer,
    TextLayer,
    TextPaint,
)
from autoslice_cover.emoji import get_emoji_font_path
from autoslice_cover.fonts import FONT_PATH_ENV, resolve_font_selection, resolve_font_stack
from autoslice_cover.paths import LOCAL_FONT_PATH

from .cover_model import (
    PROFILE_SIZES,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    RenderableObject,
    ShapeObject,
    StickerObject,
    TextObject,
    object_for_profile,
)


def canvas_size(profile_key: str) -> tuple[int, int]:
    return PROFILE_SIZES.get(profile_key, PROFILE_SIZES["4x3"])


def effective_objects(document: CoverDocument, profile_key: str) -> tuple[RenderableObject, ...]:
    """应用比例覆盖并按 z 排序；z 相同时保持文档顺序。"""

    ordered = []
    for index, base in enumerate(document.objects):
        item = object_for_profile(document, base.id, profile_key) or base
        ordered.append((item.z_index, index, item))
    return tuple(item for _z, _index, item in sorted(ordered, key=lambda value: (value[0], value[1])))


@lru_cache(maxsize=64)
def _font_stack(font_family: str, _env: str, _local_font: bool) -> tuple[str | None, ...]:
    resolution = resolve_font_selection(font_family)
    stack = resolve_font_stack(str(resolution.path) if resolution.path else None)
    emoji = get_emoji_font_path()
    if emoji and emoji not in stack:
        stack = (*stack, emoji)
    return stack


def font_stack(font_family: str) -> tuple[str | None, ...]:
    """主字体 + 系统中文回退 + 表情字体；随默认字体配置变化失效。"""

    return _font_stack(
        str(font_family or ""),
        os.environ.get(FONT_PATH_ENV, ""),
        LOCAL_FONT_PATH.is_file(),
    )


def text_area(item: TextObject, size: tuple[int, int]) -> Box:
    """文字对象的行宽区域：左上角为位置，宽度即换行宽度；高度为旧语义保留值。"""

    width, height = size
    scale = max(0.01, float(item.transform.scale or 1.0))
    return Box(
        item.transform.x * width,
        item.transform.y * height,
        item.rect.width * scale * width,
        item.rect.height * scale * height,
    )


def text_font_size(item: TextObject) -> int:
    return max(12, round(item.style.font_size * max(0.01, float(item.transform.scale or 1.0))))


def effective_stroke_width(item: TextObject) -> int:
    # A 沿用旧版 context：程序默认的重描边收回一档。
    style = item.style
    if item.copy_role == "A" and style.stroke_width >= 6 and style.font_weight >= 700:
        return 4
    return int(style.stroke_width)


def text_layout(item: TextObject, size: tuple[int, int]) -> TextLayout:
    """编辑器语义：字号固定，按行宽换行；ink 为贴合文字的选中框。"""

    style = item.style
    area = text_area(item, size)
    return wrap_text(
        item.text,
        origin=(area.left, area.top),
        width=area.width,
        size=text_font_size(item),
        stroke_width=effective_stroke_width(item),
        line_spacing=style.line_spacing,
        align=item.align,
        font_paths=font_stack(style.font_family),
        weight=style.font_weight,
    )


def fitted_font_size(item: TextObject, area: Box, *, requested: int | None = None, max_lines: int = 2) -> int:
    """在给定区域内能放下的最大字号（不超过 requested），用于自动构图和旧稿迁移。"""

    style = item.style
    layout = layout_text(
        item.text,
        area=area,
        requested_size=requested or style.font_size,
        minimum_size=CONTEXT_MIN_FONT_SIZE if item.copy_role == "A" else EMPHASIS_MIN_FONT_SIZE,
        stroke_width=effective_stroke_width(item),
        line_spacing=style.line_spacing,
        align=item.align,
        font_paths=font_stack(style.font_family),
        weight=style.font_weight,
        max_lines=max_lines,
    )
    return layout.font_size


def text_paint(item: TextObject) -> TextPaint:
    style = item.style
    return TextPaint(
        fill=style.fill_color,
        stroke=style.stroke_color,
        stroke_width=effective_stroke_width(item),
        shadow=bool(style.shadow),
        outer_stroke=style.outer_stroke or None,
        outer_stroke_width=int(style.outer_stroke_width),
        backdrop=style.backdrop or None,
    )


@lru_cache(maxsize=256)
def _image_size(path: str, _mtime_ns: int) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except (OSError, ValueError):
        return None


def image_size(path: str | None) -> tuple[int, int] | None:
    if not path:
        return None
    try:
        mtime = Path(path).stat().st_mtime_ns
    except OSError:
        return None
    return _image_size(str(path), mtime)


def asset_path(item: RenderableObject) -> str | None:
    asset = getattr(item, "asset", None)
    return asset.path if asset is not None and asset.path else None


def overlay_geometry(item: ImageObject | StickerObject, size: tuple[int, int]) -> Box | None:
    source = image_size(asset_path(item))
    if source is None:
        return None
    return overlay_box(source, size, x=item.transform.x, y=item.transform.y, scale=item.transform.scale)


def shape_geometry(item: ShapeObject, size: tuple[int, int]) -> Box:
    return shape_box(
        size, x=item.transform.x, y=item.transform.y,
        width=item.width, height=item.height, scale=item.transform.scale,
    )


def background_geometry(
    item: BackgroundObject, size: tuple[int, int], source_size: tuple[int, int],
) -> Box:
    return background_box(
        source_size, size,
        fit_mode=item.fit_mode, scale=item.scale, focus_x=item.pan_x, focus_y=item.pan_y,
    )


def document_layers(
    document: CoverDocument, profile_key: str,
) -> tuple[BackgroundLayer | None, tuple[Layer, ...]]:
    """导出用的完整图层：背景单独返回，其余按 z 排序。"""

    size = canvas_size(profile_key)
    background: BackgroundLayer | None = None
    layers: list[Layer] = []
    for item in effective_objects(document, profile_key):
        if isinstance(item, BackgroundObject):
            path = asset_path(item)
            source = image_size(path)
            if background is None and path and source is not None:
                background = BackgroundLayer(path, background_geometry(item, size, source))
            continue
        if not item.visible:
            continue
        if isinstance(item, TextObject):
            if item.text.strip():
                layers.append(TextLayer(text_layout(item, size), text_paint(item), item.transform.rotation))
        elif isinstance(item, (ImageObject, StickerObject)):
            box = overlay_geometry(item, size)
            path = asset_path(item)
            if box is not None and path:
                layers.append(ImageLayer(path, box, item.transform.rotation, item.opacity))
        elif isinstance(item, ShapeObject):
            layers.append(ShapeLayer(
                item.shape_type, shape_geometry(item, size), item.stroke,
                item.stroke_width, item.fill, item.transform.rotation,
            ))
    return background, tuple(layers)
