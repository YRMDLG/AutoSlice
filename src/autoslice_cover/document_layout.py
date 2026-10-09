"""AutoCover 文档的共享几何与文字排版。

Qt 画布和 Pillow 导出都从这里取“画在哪、断成几行、多大字号”，两端只
负责“怎么画”。坐标统一使用导出画布像素；画布显示时整体缩放即可，
避免画布与导出各算一套而错位。
"""

from __future__ import annotations

import threading
import unicodedata
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from .emoji import is_emoji_character

# 背景缩放、素材尺寸等编辑约束；模型、画布、面板和导出共用。
BACKGROUND_SCALE_MIN = 1.0
BACKGROUND_SCALE_MAX = 3.0
BACKGROUND_FILL = "#080B0E"
OVERLAY_BASE_WIDTH = 0.24
OVERLAY_MAX_WIDTH = 0.96
OVERLAY_MAX_HEIGHT = 0.92
SHAPE_MIN_SIZE = 0.02
SHAPE_MAX_SIZE = 0.98
CONTEXT_MIN_FONT_SIZE = 36
EMPHASIS_MIN_FONT_SIZE = 42
MAX_FONT_SIZE = 320
SHADOW_ALPHA = 184


@dataclass(frozen=True, slots=True)
class Box:
    """画布像素矩形。"""

    left: float
    top: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.left + self.width

    @property
    def bottom(self) -> float:
        return self.top + self.height

    @property
    def center_x(self) -> float:
        return self.left + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.top + self.height / 2.0

    def translated(self, dx: float, dy: float) -> "Box":
        return Box(self.left + dx, self.top + dy, self.width, self.height)

    def expanded(self, amount: float) -> "Box":
        return Box(self.left - amount, self.top - amount, self.width + amount * 2, self.height + amount * 2)

    def union(self, other: "Box") -> "Box":
        left, top = min(self.left, other.left), min(self.top, other.top)
        return Box(left, top, max(self.right, other.right) - left, max(self.bottom, other.bottom) - top)

    def intersection(self, other: "Box") -> "Box | None":
        left, top = max(self.left, other.left), max(self.top, other.top)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Box(left, top, right - left, bottom - top)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def clamp_background_scale(value: float) -> float:
    return _clamp(value, BACKGROUND_SCALE_MIN, BACKGROUND_SCALE_MAX)


# ── 背景与素材几何 ──


def background_box(
    source_size: tuple[int, int],
    canvas_size: tuple[int, int],
    *,
    fit_mode: str = "cover",
    scale: float = 1.0,
    focus_x: float = 0.5,
    focus_y: float = 0.5,
) -> Box:
    """源图在画布中的放置矩形。

    focus 与 ``ImageOps.fit`` 的 centering 同义：0 显示左/上，1 显示右/下；
    图片小于画布时（contain）它决定留边位置，0.5 为居中。
    """

    source_width, source_height = max(1, source_size[0]), max(1, source_size[1])
    canvas_width, canvas_height = canvas_size
    base = (min if fit_mode == "contain" else max)(
        canvas_width / source_width, canvas_height / source_height,
    )
    factor = base * clamp_background_scale(scale)
    width, height = source_width * factor, source_height * factor
    return Box(
        (canvas_width - width) * _clamp(focus_x, 0.0, 1.0),
        (canvas_height - height) * _clamp(focus_y, 0.0, 1.0),
        width,
        height,
    )


def focus_after_drag(focus: float, delta: float, canvas_extent: float, drawn_extent: float) -> float:
    """把指针位移换算成新的 focus，使画面始终跟手。"""

    span = canvas_extent - drawn_extent
    if abs(span) < 0.5:
        return _clamp(focus, 0.0, 1.0)
    return _clamp(focus + delta / span, 0.0, 1.0)


def overlay_box(
    image_size: tuple[int, int],
    canvas_size: tuple[int, int],
    *,
    x: float,
    y: float,
    scale: float,
) -> Box:
    """图片/贴纸以画布宽度比例定宽、按原图比例定高，左上角为 (x, y)。"""

    image_width, image_height = max(1, image_size[0]), max(1, image_size[1])
    canvas_width, canvas_height = canvas_size
    width = canvas_width * min(OVERLAY_MAX_WIDTH, OVERLAY_BASE_WIDTH * _clamp(scale, 0.05, 4.0))
    height = width * image_height / image_width
    if height > canvas_height * OVERLAY_MAX_HEIGHT:
        height = canvas_height * OVERLAY_MAX_HEIGHT
        width = height * image_width / image_height
    return Box(x * canvas_width, y * canvas_height, width, height)


def shape_box(
    canvas_size: tuple[int, int],
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    scale: float,
) -> Box:
    canvas_width, canvas_height = canvas_size
    factor = _clamp(scale, 0.05, 4.0)
    return Box(
        x * canvas_width,
        y * canvas_height,
        canvas_width * _clamp(width * factor, SHAPE_MIN_SIZE, SHAPE_MAX_SIZE),
        canvas_height * _clamp(height * factor, SHAPE_MIN_SIZE, SHAPE_MAX_SIZE),
    )


def arrow_head_size(box: Box) -> float:
    return max(8.0, min(box.width, box.height) * 0.25)


def shadow_offset(font_size: int) -> int:
    return max(2, round(font_size / 28))


def backdrop_box(ink: Box, font_size: int) -> Box:
    """文字底条：墨迹外框按字号留白。"""

    pad_x, pad_y = font_size * 0.22, font_size * 0.12
    return Box(ink.left - pad_x, ink.top - pad_y, ink.width + pad_x * 2, ink.height + pad_y * 2)


def backdrop_radius(font_size: int) -> float:
    return font_size * 0.16


# ── 字体加载与缺字回退 ──

_LOCAL = threading.local()


def _apply_weight(font: ImageFont.FreeTypeFont, weight: int) -> None:
    """可变字体按 wght 轴取字重；静态字体保持原样。"""

    try:
        axes = font.get_variation_axes()
    except (AttributeError, OSError):
        return
    values: list[float] = []
    found = False
    for axis in axes:
        name = axis.get("name")
        if name in (b"Weight", "Weight"):
            values.append(_clamp(weight, axis["minimum"], axis["maximum"]))
            found = True
        else:
            values.append(axis.get("default", axis["minimum"]))
    if found:
        try:
            font.set_variation_by_axes(values)
        except (OSError, ValueError):
            pass


def load_font(path: str | None, size: int, weight: int = 900) -> ImageFont.ImageFont:
    """按线程缓存字体对象；FreeType face 不能跨线程共用。"""

    cache = getattr(_LOCAL, "fonts", None)
    if cache is None:
        cache = _LOCAL.fonts = {}
    key = (path, int(size), int(weight))
    font = cache.get(key)
    if font is not None:
        return font
    if len(cache) > 192:
        cache.clear()
    if path:
        font = ImageFont.truetype(path, size=int(size))
        _apply_weight(font, int(weight))
    else:
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", size=int(size))
        except OSError:
            font = ImageFont.load_default(size=int(size))
    cache[key] = font
    return font


def _glyph_signature(font: ImageFont.ImageFont, character: str) -> tuple[object, ...]:
    mask = font.getmask(character, mode="L")
    return mask.size, mask.getbbox(), bytes(mask)


def _is_emoji_font(path: str | None) -> bool:
    return bool(path) and Path(path).name.casefold() == "seguiemj.ttf"


@lru_cache(maxsize=8192)
def font_supports_character(font_path: str | None, character: str) -> bool:
    """判断字体是否真的包含字形，避免把 .notdef 缺字符号当作正文。"""

    if character.isspace() or unicodedata.category(character) in {"Cc", "Cf"}:
        return True
    if unicodedata.combining(character):
        return True
    font = load_font(font_path, 64)
    candidate = _glyph_signature(font, character)
    if candidate[1] is None:
        return False
    return candidate != _glyph_signature(font, "￿")


@lru_cache(maxsize=8192)
def font_path_for_character(font_paths: tuple[str | None, ...], character: str) -> str | None:
    if is_emoji_character(character):
        for font_path in font_paths:
            if _is_emoji_font(font_path):
                return font_path
    for font_path in font_paths:
        if _is_emoji_font(font_path):
            continue
        if font_supports_character(font_path, character):
            return font_path
    return font_paths[0]


def font_runs(text: str, font_paths: tuple[str | None, ...]) -> list[tuple[str | None, str, bool]]:
    """把一行文字按实际使用的字体切成连续片段。"""

    specs: list[tuple[str | None, str, bool]] = []
    for character in text:
        font_path = font_path_for_character(font_paths, character)
        emoji = _is_emoji_font(font_path)
        if specs and specs[-1][0] == font_path and specs[-1][2] == emoji:
            previous_path, previous_text, _ = specs[-1]
            specs[-1] = previous_path, previous_text + character, emoji
        else:
            specs.append((font_path, character, emoji))
    return specs


# ── 单行测量 ──


@dataclass(frozen=True, slots=True)
class TextRun:
    """一段使用同一字体绘制的文字；offset 相对行绘制原点。"""

    text: str
    font_path: str | None
    offset: float
    emoji: bool = False
    # 强调词：换成强调色绘制，字形和位置不变。
    accent: bool = False


@dataclass(frozen=True, slots=True)
class LineMetrics:
    """以基线原点（Pillow anchor "ls"）为参照的墨迹框，含描边。"""

    text: str
    runs: tuple[TextRun, ...]
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top


def _measure_draw() -> ImageDraw.ImageDraw:
    draw = getattr(_LOCAL, "draw", None)
    if draw is None:
        draw = _LOCAL.draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    return draw


def measure_line(
    text: str,
    size: int,
    stroke_width: int,
    font_paths: tuple[str | None, ...],
    weight: int = 900,
) -> LineMetrics:
    draw = _measure_draw()
    runs: list[TextRun] = []
    bounds: list[tuple[float, float, float, float]] = []
    cursor = 0.0
    for font_path, run_text, emoji in font_runs(text, font_paths):
        font = load_font(font_path, size, weight)
        box = draw.textbbox((cursor, 0), run_text, font=font, stroke_width=stroke_width, anchor="ls")
        bounds.append(tuple(float(value) for value in box))
        runs.append(TextRun(run_text, font_path, cursor, emoji))
        cursor += float(draw.textlength(run_text, font=font))
    if not bounds:
        return LineMetrics(text, (), 0.0, 0.0, 0.0, 0.0)
    return LineMetrics(
        text,
        tuple(runs),
        min(box[0] for box in bounds),
        min(box[1] for box in bounds),
        max(box[2] for box in bounds),
        max(box[3] for box in bounds),
    )


def _advances(text: str, size: int, font_paths: tuple[str | None, ...], weight: int) -> tuple[float, ...]:
    """逐字前进宽度，用于快速估计各断行方案。"""

    draw = _measure_draw()
    return tuple(
        float(draw.textlength(character, font=load_font(font_path_for_character(font_paths, character), size, weight)))
        for character in text
    )


# ── 中文断行 ──
# 参考 BudouX / 日文禁则的思路：标点和引号边界优先，避免切开数字、
# 拉丁单词、数字与量词，以及把标点挤到行首。

_NO_LINE_START = set("，。！？、；：⁉‼⁈⁇,.!?;:）)]」』》】〉”’…—～~%")
_NO_LINE_END = set("（([「『《【〈“‘")
_SOFT_AFTER = set("，。！？、；：⁉‼⁈⁇,.!?;:…～~」』》】”’)]")
_PARTICLES = set("的了着过吗呢吧啊呀啦嘛哦哇呗")
_PRONOUNS = set("你我他她它")
_LEADING_WORDS = (
    "然后", "结果", "但是", "可是", "不过", "所以", "因为", "于是", "居然", "竟然",
    "突然", "直接", "却", "就", "都", "还", "又", "才", "也", "被", "把", "给", "让",
)
_UNITS = set("万千百亿个次楼岁元块位只条张天年月日号%")
SEMANTIC_BREAK = 0.0
PARTICLE_BREAK = 0.03
CONNECTIVE_BREAK = 0.04
SCRIPT_BREAK = 0.05
PLAIN_BREAK = 0.2


def _word_character(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character in "'_-")


def break_penalty(text: str, index: int) -> float | None:
    """在 text[index] 前断行的代价；None 表示禁止断开。"""

    if not 0 < index < len(text):
        return None
    before, after = text[index - 1], text[index]
    if after in _NO_LINE_START or before in _NO_LINE_END:
        return None
    if _word_character(before) and _word_character(after):
        return None
    if before.isdigit() and (after in _UNITS or after.isdigit()):
        return None
    if before in _SOFT_AFTER or before.isspace() or after.isspace():
        return SEMANTIC_BREAK
    if before in _PARTICLES:
        return PARTICLE_BREAK
    # 代词宾语之后断开通常自然（“告诉你 / 韩娱…”），但不拆“你们”“你的”。
    if before in _PRONOUNS and after not in "们的":
        return PARTICLE_BREAK
    if _word_character(before) != _word_character(after):
        return SCRIPT_BREAK
    rest = text[index:]
    if any(rest.startswith(word) for word in _LEADING_WORDS):
        return CONNECTIVE_BREAK
    return PLAIN_BREAK


def _visible_length(text: str) -> int:
    return sum(1 for character in text if not character.isspace() and character not in _SOFT_AFTER)


def _line_gap(size: int, line_spacing: float) -> int:
    return max(8, round(size * max(0.08, min(0.30, float(line_spacing) - 1.0))))


def _fits(metrics: Sequence[LineMetrics], size: int, width: float, height: float, line_spacing: float) -> bool:
    total = sum(item.height for item in metrics) + _line_gap(size, line_spacing) * (len(metrics) - 1)
    return max(item.width for item in metrics) <= width + 0.5 and total <= height + 0.5


def _largest_fitting_size(
    lines: tuple[str, ...],
    *,
    width: float,
    height: float,
    requested: int,
    minimum: int,
    estimate: int,
    stroke_width: int,
    line_spacing: float,
    font_paths: tuple[str | None, ...],
    weight: int,
) -> tuple[int, tuple[LineMetrics, ...]] | None:
    def measure(size: int) -> tuple[LineMetrics, ...]:
        return tuple(measure_line(line, size, stroke_width, font_paths, weight) for line in lines)

    size = max(minimum, min(requested, estimate))
    metrics = measure(size)
    if _fits(metrics, size, width, height, line_spacing):
        # 估计偏保守时向上补足，最多到用户字号。
        while size + 2 <= requested:
            larger = measure(size + 2)
            if not _fits(larger, size + 2, width, height, line_spacing):
                break
            size, metrics = size + 2, larger
        return size, metrics
    while size > minimum:
        size = max(minimum, size - 2)
        metrics = measure(size)
        if _fits(metrics, size, width, height, line_spacing):
            return size, metrics
    return None


@dataclass(frozen=True, slots=True)
class TextLine:
    text: str
    runs: tuple[TextRun, ...]
    origin_x: float
    baseline: float
    ink: Box


@dataclass(frozen=True, slots=True)
class TextLayout:
    """一个文字块的最终排版结果；area 中心即旋转中心。"""

    lines: tuple[TextLine, ...]
    font_size: int
    font_weight: int
    stroke_width: int
    area: Box
    ink: Box

    def translated(self, dx: float, dy: float) -> "TextLayout":
        return replace(
            self,
            lines=tuple(
                replace(line, origin_x=line.origin_x + dx, baseline=line.baseline + dy, ink=line.ink.translated(dx, dy))
                for line in self.lines
            ),
            area=self.area.translated(dx, dy),
            ink=self.ink.translated(dx, dy),
        )


def _candidates(text: str, max_lines: int) -> tuple[tuple[tuple[str, ...], float], ...]:
    value = text.strip()
    result: list[tuple[tuple[str, ...], float]] = [((value,), 0.0)]
    if max_lines < 2:
        return tuple(result)
    for index in range(1, len(value)):
        penalty = break_penalty(value, index)
        if penalty is None:
            continue
        head, tail = value[:index].rstrip(), value[index:].lstrip()
        # 自动断行禁止 1~2 字孤短尾行。
        if not head or _visible_length(tail) <= 2:
            continue
        result.append(((head, tail), penalty))
    return tuple(result)


@lru_cache(maxsize=512)
def _relative_layout(
    text: str,
    width: int,
    height: int,
    requested: int,
    minimum: int,
    stroke_width: int,
    line_spacing: float,
    align: str,
    font_paths: tuple[str | None, ...],
    weight: int,
    max_lines: int,
) -> TextLayout:
    width = max(24, width)
    height = max(24, height)
    explicit = "\n" in text
    source_lines = tuple(part.strip() for part in text.replace("\r\n", "\n").split("\n") if part.strip()) or (" ",)
    if explicit:
        options: tuple[tuple[tuple[str, ...], float], ...] = ((source_lines[:max_lines], 0.0),)
    else:
        options = _candidates(source_lines[0], max_lines)

    # 先用逐字宽度线性估计每个方案能达到的字号，只精确测量前几名。
    plain = source_lines[0] if not explicit else ""
    advances = _advances(plain, requested, font_paths, weight) if plain else ()
    reference = measure_line(source_lines[0], requested, stroke_width, font_paths, weight)
    ink_height = max(1.0, reference.height)

    def estimate(lines: tuple[str, ...]) -> int:
        if explicit or not advances:
            widths = [measure_line(line, requested, stroke_width, font_paths, weight).width for line in lines]
        else:
            widths = []
            start = 0
            for line in lines:
                start = plain.find(line, start)
                widths.append(sum(advances[start:start + len(line)]) + stroke_width * 2)
                start += len(line)
        total_height = ink_height * len(lines) + _line_gap(requested, line_spacing) * (len(lines) - 1)
        ratio = min(width / max(1.0, max(widths)), height / max(1.0, total_height), 1.0)
        return int(requested * ratio) // 2 * 2

    def balance(metrics: Sequence[LineMetrics]) -> float:
        if len(metrics) < 2:
            return 0.0
        return abs(metrics[0].width - metrics[-1].width) / width * 0.08

    ranked = sorted(
        ((estimate(lines) * (1.0 - penalty), lines, penalty) for lines, penalty in options),
        key=lambda item: (-item[0], len(item[1])),
    )
    best: tuple[float, int, tuple[LineMetrics, ...]] | None = None
    for _score, lines, penalty in ranked[:6]:
        fitted = _largest_fitting_size(
            lines, width=width, height=height, requested=requested, minimum=minimum,
            estimate=estimate(lines), stroke_width=stroke_width, line_spacing=line_spacing,
            font_paths=font_paths, weight=weight,
        )
        if fitted is None:
            continue
        size, metrics = fitted
        score = size * (1.0 - penalty - balance(metrics))
        if best is None or (score, -len(metrics)) > (best[0], -len(best[2])):
            best = (score, size, metrics)
    if best is None:
        # 区域小于最小字号：保留完整文字按最小字号溢出，不退回近似切行。
        size = minimum
        metrics = tuple(measure_line(line, size, stroke_width, font_paths, weight) for line in options[0][0])
    else:
        _score, size, metrics = best

    gap = _line_gap(size, line_spacing)
    lines: list[TextLine] = []
    top = 0.0
    ink: Box | None = None
    for item in metrics:
        if align == "center":
            ink_left = (width - item.width) / 2.0
        elif align == "right":
            ink_left = width - item.width
        else:
            ink_left = 0.0
        line_ink = Box(ink_left, top, item.width, item.height)
        lines.append(TextLine(item.text, item.runs, ink_left - item.left, top - item.top, line_ink))
        ink = line_ink if ink is None else ink.union(line_ink)
        top += item.height + gap
    return TextLayout(
        tuple(lines), size, weight, stroke_width,
        Box(0.0, 0.0, float(width), float(height)), ink or Box(0.0, 0.0, 0.0, 0.0),
    )


def layout_text(
    text: str,
    *,
    area: Box,
    requested_size: int,
    minimum_size: int,
    stroke_width: int,
    line_spacing: float,
    align: str,
    font_paths: tuple[str | None, ...],
    weight: int = 900,
    max_lines: int = 2,
) -> TextLayout:
    """在对象区域内按真实字形搜索字号和断行；结果按区域位置平移。"""

    requested = max(24, min(MAX_FONT_SIZE, int(requested_size)))
    minimum = max(12, min(requested, int(minimum_size)))
    relative = _relative_layout(
        str(text),
        round(area.width),
        round(area.height),
        requested,
        minimum,
        max(0, int(stroke_width)),
        round(float(line_spacing), 3),
        align if align in {"left", "center", "right"} else "left",
        tuple(font_paths),
        int(weight),
        max(1, min(8, int(max_lines))),
    )
    # 缓存按取整后的区域尺寸命中；真实区域只用于平移和旋转中心。
    return replace(relative.translated(area.left, area.top), area=area)


# ── 编辑器语义：固定字号按宽度换行 ──
# 与剪映/Canva 一致：字号就是实际字号，不偷偷缩小；行宽决定换行，框贴合文字。
# 上面的 layout_text 只用于自动构图时为槽位挑字号，以及旧草稿迁移。


def _wrap_paragraph(
    value: str,
    width: float,
    size: int,
    stroke_width: int,
    font_paths: tuple[str | None, ...],
    weight: int,
) -> list[str]:
    advances = _advances(value, size, font_paths, weight)
    prefix = [0.0]
    for advance in advances:
        prefix.append(prefix[-1] + advance)
    margin = stroke_width * 2
    total = len(value)
    if prefix[total] + margin <= width:
        return [value]

    def solve(force: bool) -> list[str] | None:
        # best[j]：前 j 个字符分行后的（行数, 代价, 上一断点）；先少行，再求语义代价最小。
        best: list[tuple[int, float, int] | None] = [None] * (total + 1)
        best[0] = (0, 0.0, -1)
        for end in range(1, total + 1):
            if end < total:
                penalty = break_penalty(value, end)
                if penalty is None:
                    if not force:
                        continue
                    penalty = 1.0
            else:
                penalty = 0.0
            for start in range(end - 1, -1, -1):
                if best[start] is None:
                    continue
                span = prefix[end] - prefix[start] + margin
                if span > width and end - start > 1:
                    break
                lines, cost, _previous = best[start]
                slack = max(0.0, width - span) / width
                cost = cost + penalty + (0.0 if end == total else slack * slack * 0.3)
                if end == total and start > 0 and _visible_length(value[start:end]) <= 2:
                    cost += 5.0
                candidate = (lines + 1, cost, start)
                if best[end] is None or candidate[:2] < best[end][:2]:
                    best[end] = candidate
        if best[total] is None:
            return None
        cuts: list[int] = []
        position = total
        while position > 0:
            cuts.append(position)
            position = best[position][2]
        result, start = [], 0
        for cut in reversed(cuts):
            piece = value[start:cut].strip()
            if piece:
                result.append(piece)
            start = cut
        return result

    return solve(False) or solve(True) or [value]


@lru_cache(maxsize=512)
def _relative_wrap(
    text: str,
    width: int,
    size: int,
    stroke_width: int,
    line_spacing: float,
    align: str,
    font_paths: tuple[str | None, ...],
    weight: int,
) -> TextLayout:
    width = max(8, width)
    paragraphs = [part.strip() for part in text.replace("\r\n", "\n").split("\n") if part.strip()]
    texts: list[str] = []
    for paragraph in paragraphs:
        effective = float(width)
        for _attempt in range(3):
            pieces = _wrap_paragraph(paragraph, effective, size, stroke_width, font_paths, weight)
            # 逐字宽度是近似值，实测超出时收窄重排。
            overflow = max(
                measure_line(piece, size, stroke_width, font_paths, weight).width for piece in pieces
            ) - width
            if overflow <= 0.5 or len(pieces) == len(paragraph):
                break
            effective -= overflow + 1
        texts.extend(pieces)
    metrics = [measure_line(line, size, stroke_width, font_paths, weight) for line in texts]
    gap = _line_gap(size, line_spacing)
    lines: list[TextLine] = []
    top = 0.0
    ink: Box | None = None
    for item in metrics:
        if align == "center":
            ink_left = (width - item.width) / 2.0
        elif align == "right":
            ink_left = width - item.width
        else:
            ink_left = 0.0
        line_ink = Box(ink_left, top, item.width, item.height)
        lines.append(TextLine(item.text, item.runs, ink_left - item.left, top - item.top, line_ink))
        ink = line_ink if ink is None else ink.union(line_ink)
        top += item.height + gap
    height = max(1.0, top - gap) if lines else 1.0
    return TextLayout(
        tuple(lines), size, weight, stroke_width,
        Box(0.0, 0.0, float(width), height), ink or Box(0.0, 0.0, 0.0, 0.0),
    )


def _accent_mask(text: str, words: tuple[str, ...]) -> tuple[bool, ...]:
    """text 里每个字是否落在某个强调词内（同一个词出现几次都标）。"""

    mask = [False] * len(text)
    for word in words:
        if not word:
            continue
        start = text.find(word)
        while start >= 0:
            mask[start:start + len(word)] = [True] * len(word)
            start = text.find(word, start + len(word))
    return tuple(mask)


@lru_cache(maxsize=256)
def emphasize(layout: TextLayout, words: tuple[str, ...]) -> TextLayout:
    """按强调词把每行的字体片段再切开并标 accent；词跨行也能标上。只改颜色，几何不变。"""

    words = tuple(word.strip() for word in words if word and word.strip())
    if not words or not layout.lines:
        return layout
    mask = _accent_mask("".join(line.text for line in layout.lines), words)
    if not any(mask):
        return layout
    draw = _measure_draw()
    lines: list[TextLine] = []
    position = 0
    for line in layout.lines:
        runs: list[TextRun] = []
        for run in line.runs:
            flags = mask[position:position + len(run.text)]
            position += len(run.text)
            font = load_font(run.font_path, layout.font_size, layout.font_weight)
            start = 0
            while start < len(run.text):
                end = start + 1
                while end < len(run.text) and flags[end] == flags[start]:
                    end += 1
                offset = run.offset + (float(draw.textlength(run.text[:start], font=font)) if start else 0.0)
                runs.append(replace(run, text=run.text[start:end], offset=offset, accent=bool(flags[start]) and not run.emoji))
                start = end
        lines.append(replace(line, runs=tuple(runs)))
    return replace(layout, lines=tuple(lines))


def wrap_text(
    text: str,
    *,
    origin: tuple[float, float],
    width: float,
    size: int,
    stroke_width: int,
    line_spacing: float,
    align: str,
    font_paths: tuple[str | None, ...],
    weight: int = 900,
) -> TextLayout:
    """固定字号、按宽度换行；area 高度即文字总高，ink 为贴合文字的框。"""

    relative = _relative_wrap(
        str(text),
        max(8, round(width)),
        max(8, min(MAX_FONT_SIZE, int(size))),
        max(0, int(stroke_width)),
        round(float(line_spacing), 3),
        align if align in {"left", "center", "right"} else "left",
        tuple(font_paths),
        int(weight),
    )
    return relative.translated(origin[0], origin[1])
