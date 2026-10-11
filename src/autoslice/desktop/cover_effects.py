"""一键精修效果：给关键词垫爆炸贴；把人物抠出来加白色外发光、背景压暗。

爆炸贴是纯函数，编辑器按钮和“AI 改一改”共用。抠图用本机 OpenCV 的 GrabCut（不下载模型），
以看图标出的人物框、脸框为种子、界面框为背景；没装 OpenCV 时这个功能不可用。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageFilter

from autoslice_cover.document_layout import Box, TextLayout, load_font

from .cover_ai import AIFrameNotes
from .cover_layout import canvas_size, text_area, text_layout
from .cover_model import (
    CoverDocument,
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


# 抠图：GrabCut 在半分辨率上跑（够准、一秒多），外发光宽度占画面高度的比例，没标出人物时的默认人物框。
_CUTOUT_SCALE = 0.5
_CUTOUT_ITERATIONS = 6
_GLOW_RATIO = 0.012
_DEFAULT_PERSON = (0.22, 0.06, 0.78, 1.0)
# 抠完默认把人物以外压暗这么多。
CUTOUT_DIM = 0.45


def cutout_available() -> bool:
    """本机有没有 OpenCV（抠图靠它）。"""

    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


def make_cutout(image_path: str | Path, notes: AIFrameNotes | None, out_dir: Path) -> Path:
    """把人物抠出来：返回和底图同尺寸的透明 PNG（人物 + 白色外发光）；同一张图、同样的框直接复用。"""

    import cv2
    import numpy as np

    source = Path(image_path)
    person = notes.person if notes is not None and notes.person is not None else _DEFAULT_PERSON
    face = notes.face if notes is not None else None
    ui = notes.ui if notes is not None else ()
    stat = source.stat()
    key = hashlib.sha1(repr((str(source.resolve()), stat.st_size, stat.st_mtime_ns, person, face, ui)).encode("utf-8"))
    output = out_dir / f"{key.hexdigest()[:20]}.png"
    if output.is_file():
        return output
    image = cv2.imdecode(np.fromfile(str(source), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"底图读不出来：{source}")
    height, width = image.shape[:2]
    small = cv2.resize(image, (max(1, int(width * _CUTOUT_SCALE)), max(1, int(height * _CUTOUT_SCALE))), interpolation=cv2.INTER_AREA)
    small_height, small_width = small.shape[:2]

    def pixels(box):
        return (int(box[0] * small_width), int(box[1] * small_height), int(box[2] * small_width), int(box[3] * small_height))

    mask = np.full((small_height, small_width), cv2.GC_BGD, np.uint8)
    x0, y0, x1, y1 = pixels(person)
    mask[y0:y1, x0:x1] = cv2.GC_PR_FGD
    for box in ui:
        # 直播界面肯定不是人。
        ux0, uy0, ux1, uy1 = pixels(box)
        mask[uy0:uy1, ux0:ux1] = cv2.GC_BGD
    if face is not None:
        # 脸框往里收一圈，肯定是人。
        fx0, fy0, fx1, fy1 = pixels(face)
        dx, dy = (fx1 - fx0) // 6, (fy1 - fy0) // 6
        mask[fy0 + dy:fy1 - dy, fx0 + dx:fx1 - dx] = cv2.GC_FGD
    background_model, foreground_model = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(small, mask, None, background_model, foreground_model, _CUTOUT_ITERATIONS, cv2.GC_INIT_WITH_MASK)
    alpha = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    # 只留最大的一块（人），补上小洞。
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(alpha)
    if count > 1:
        alpha = np.where(labels == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA])), 255, 0).astype(np.uint8)
    alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    alpha = cv2.resize(alpha, (width, height), interpolation=cv2.INTER_LINEAR)
    with Image.open(source) as opened:
        picture = opened.convert("RGB")
    edge = Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(1.5))
    glow_size = max(3, round(height * _GLOW_RATIO)) | 1
    glow = edge.filter(ImageFilter.MaxFilter(glow_size * 2 + 1)).filter(ImageFilter.GaussianBlur(glow_size / 4))
    result = Image.new("RGBA", picture.size, (255, 255, 255, 0))
    result.putalpha(glow)
    person_layer = picture.convert("RGBA")
    person_layer.putalpha(edge)
    result.alpha_composite(person_layer)
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.png")
    result.save(temporary)
    temporary.replace(output)
    return output


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
