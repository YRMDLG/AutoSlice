"""画面构图分析：给文字找空区、给比例裁切找主体。

参考 smartcrop.js 的显著图思路（肤色、细节、饱和度三通道），在缩略图上
用纯 Pillow 计算，离线、确定、无额外依赖。只给出建议，不替用户做决定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageFilter

ANALYSIS_WIDTH = 160
_SKIN_COLOR = (0.78, 0.57, 0.44)
_SKIN_THRESHOLD = 0.80
_SATURATION_THRESHOLD = 0.40
# smartcrop 的通道权重：肤色最重要，细节次之，饱和度只作轻微参考。
SKIN_WEIGHT = 1.8
DETAIL_WEIGHT = 0.2
SATURATION_WEIGHT = 0.1


@dataclass(frozen=True, slots=True)
class SaliencyMap:
    """缩略图网格上的 0~1 通道值，配积分图做区域均值。"""

    width: int
    height: int
    skin: tuple[float, ...]
    detail: tuple[float, ...]
    saturation: tuple[float, ...]
    subtitle_band: tuple[float, float] | None
    _integrals: tuple[tuple[float, ...], ...]

    @property
    def aspect(self) -> float:
        return self.width / max(1, self.height)

    def mean(self, channel: str, box: tuple[float, float, float, float]) -> float:
        """box 为源图归一化坐标 (x0, y0, x1, y1)。"""

        index = {"skin": 0, "detail": 1, "saturation": 2, "importance": 3}[channel]
        integral = self._integrals[index]
        x0 = max(0, min(self.width, math.floor(box[0] * self.width)))
        y0 = max(0, min(self.height, math.floor(box[1] * self.height)))
        x1 = max(0, min(self.width, math.ceil(box[2] * self.width)))
        y1 = max(0, min(self.height, math.ceil(box[3] * self.height)))
        if x1 <= x0 or y1 <= y0:
            return 0.0
        stride = self.width + 1
        total = (
            integral[y1 * stride + x1] - integral[y0 * stride + x1]
            - integral[y1 * stride + x0] + integral[y0 * stride + x0]
        )
        return total / ((x1 - x0) * (y1 - y0))


def _integral(values: Sequence[float], width: int, height: int) -> tuple[float, ...]:
    stride = width + 1
    result = [0.0] * (stride * (height + 1))
    for y in range(height):
        row = 0.0
        for x in range(width):
            row += values[y * width + x]
            result[(y + 1) * stride + x + 1] = result[y * stride + x + 1] + row
    return tuple(result)


def _blur(values: list[float], width: int, height: int, radius: float) -> list[float]:
    image = Image.new("L", (width, height))
    image.putdata([max(0, min(255, round(value * 255))) for value in values])
    blurred = image.filter(ImageFilter.GaussianBlur(radius))
    return [value / 255.0 for value in blurred.getdata()]


def _subtitle_band(detail: list[float], width: int, height: int) -> tuple[float, float] | None:
    """中下部窄横向高细节带，估计烧录字幕位置（归一化 y0, y1）。"""

    left, right = int(width * 0.16), int(width * 0.84)
    top, bottom = int(height * 0.55), int(height * 0.95)
    rows = [
        sum(1 for x in range(left, right) if detail[y * width + x] > 0.35) / max(1, right - left)
        for y in range(top, bottom)
    ]
    if not rows:
        return None
    window = max(2, int(height * 0.06))
    best, best_start = 0.0, 0
    for start in range(0, len(rows) - window + 1):
        density = sum(rows[start:start + window]) / window
        if density > best:
            best, best_start = density, start
    median = sorted(rows)[len(rows) // 2]
    if best - median < 0.12:
        return None
    return (top + best_start) / height, (top + best_start + window) / height


def _analyze(path: str) -> SaliencyMap:
    with Image.open(path) as source:
        image = source.convert("RGB")
    image.thumbnail((ANALYSIS_WIDTH, ANALYSIS_WIDTH), Image.Resampling.BILINEAR)
    width, height = image.size
    pixels = list(image.getdata())
    lightness = [(0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0 for r, g, b in pixels]
    skin: list[float] = []
    saturation: list[float] = []
    skin_norm = math.sqrt(sum(value * value for value in _SKIN_COLOR))
    reference = tuple(value / skin_norm for value in _SKIN_COLOR)
    for (r, g, b), light in zip(pixels, lightness):
        magnitude = math.sqrt(r * r + g * g + b * b) or 1.0
        distance = math.sqrt(
            (r / magnitude - reference[0]) ** 2
            + (g / magnitude - reference[1]) ** 2
            + (b / magnitude - reference[2]) ** 2
        )
        tone = 1.0 - distance
        skin.append((tone - _SKIN_THRESHOLD) / (1 - _SKIN_THRESHOLD) if tone > _SKIN_THRESHOLD and 0.2 <= light <= 1.0 else 0.0)
        high, low = max(r, g, b) / 255.0, min(r, g, b) / 255.0
        middle = (high + low) / 2
        if high == low:
            value = 0.0
        else:
            spread = high - low
            value = spread / (2 - high - low) if middle > 0.5 else spread / (high + low)
        saturation.append(
            (value - _SATURATION_THRESHOLD) / (1 - _SATURATION_THRESHOLD)
            if value > _SATURATION_THRESHOLD and 0.05 <= middle <= 0.9 else 0.0
        )
    detail = [0.0] * (width * height)
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            index = y * width + x
            laplace = (
                4 * lightness[index] - lightness[index - 1] - lightness[index + 1]
                - lightness[index - width] - lightness[index + width]
            )
            detail[index] = min(1.0, abs(laplace) * 4.0)
    band = _subtitle_band(detail, width, height)
    # 肤色像素零散，模糊后才能覆盖整张脸。
    skin = _blur(skin, width, height, max(1.0, width / 40))
    importance = [
        min(1.0, SKIN_WEIGHT * s + DETAIL_WEIGHT * d + SATURATION_WEIGHT * t)
        for s, d, t in zip(skin, detail, saturation)
    ]
    return SaliencyMap(
        width, height, tuple(skin), tuple(detail), tuple(saturation), band,
        tuple(_integral(channel, width, height) for channel in (skin, detail, saturation, importance)),
    )


@lru_cache(maxsize=64)
def _cached(path: str, _mtime_ns: int) -> SaliencyMap:
    return _analyze(path)


def saliency_map(path: str | Path) -> SaliencyMap | None:
    try:
        return _cached(str(path), Path(path).stat().st_mtime_ns)
    except (OSError, ValueError):
        return None


def best_crop_focus(saliency: SaliencyMap, target_aspect: float) -> tuple[float, float]:
    """按比例裁切时让主体尽量留在画内；收益不明显时保持居中。"""

    source_aspect = saliency.aspect
    if abs(source_aspect - target_aspect) < 0.02:
        return 0.5, 0.5
    horizontal = source_aspect > target_aspect
    window = target_aspect / source_aspect if horizontal else source_aspect / target_aspect

    def inside(focus: float) -> float:
        start = (1 - window) * focus
        box = (start, 0.0, start + window, 1.0) if horizontal else (0.0, start, 1.0, start + window)
        return saliency.mean("importance", box) * window

    total = saliency.mean("importance", (0.0, 0.0, 1.0, 1.0))
    if total <= 0.01:
        return 0.5, 0.5
    centered = inside(0.5)
    best_focus, best_value = 0.5, centered
    for step in range(21):
        focus = step / 20
        value = inside(focus) - abs(focus - 0.5) * total * 0.02
        if value > best_value:
            best_focus, best_value = focus, value
    # 至少多保住 8% 的显著内容才离开居中，避免无谓的偏移。
    if inside(best_focus) < centered + total * 0.08:
        return 0.5, 0.5
    return (best_focus, 0.5) if horizontal else (0.5, best_focus)


def region_cost(saliency: SaliencyMap, box: tuple[float, float, float, float]) -> float:
    """文字区域代价：盖住人脸最重，杂乱细节次之，压字幕带再加罚。"""

    cost = saliency.mean("skin", box) * 3.0 + saliency.mean("detail", box) * 1.2
    cost += saliency.mean("saturation", box) * 0.2
    band = saliency.subtitle_band
    if band is not None:
        overlap = max(0.0, min(box[3], band[1]) - max(box[1], band[0]))
        cost += overlap / max(1e-6, band[1] - band[0]) * 0.6
    return cost
