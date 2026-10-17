"""旧封面的轻量特征：主色、明暗、饱和度、字多半落在上中下哪一段。

只用 Pillow、只读小缩略图（JPEG 直接按小尺寸解码），几十毫秒一张；没装 Pillow 就整段跳过。
这些特征只够聚类出“你常用的几种封面”；要看懂封面（人物位置、字的内容、版式类型）留给之后的看图大模型，
接口见 docs/desktop-vnext/ARCHIVE_LEARNING.md。
"""

from __future__ import annotations

import json
import os
from typing import Mapping, Sequence

from ..model import COVER_NAME_HINTS, ArchiveEntry, ScanOptions
from .base import ExtractContext, Extractor

_THUMB = 96


def _hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb[:3])


def cover_features(path: str):
    """一张封面的特征行（不含 entry_id）；打不开返回 None。"""

    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            width, height = image.size
            image.draft("RGB", (_THUMB * 3, _THUMB * 3))
            small = image.convert("RGB")
            small.thumbnail((_THUMB, _THUMB))
        quantized = small.quantize(colors=5, method=Image.Quantize.MEDIANCUT)
        palette = quantized.getpalette() or []
        total = float(small.width * small.height)
        colors = sorted(quantized.getcolors() or [], reverse=True)
        top = [
            [_hex(palette[index * 3:index * 3 + 3]), round(count / total, 3)] for count, index in colors[:3]
            if index * 3 + 3 <= len(palette)
        ]
        gray = small.convert("L")
        brightness = ImageStat.Stat(gray).mean[0] / 255
        saturation = ImageStat.Stat(small.convert("HSV")).mean[1] / 255
        edges = gray.filter(ImageFilter.FIND_EDGES)
        third = max(1, edges.height // 3)
        parts = [
            ImageStat.Stat(edges.crop((0, top_, edges.width, min(edges.height, top_ + third)))).mean[0] / 255
            for top_ in (0, third, third * 2)
        ]
        lowered = os.path.basename(path).lower()
        return (
            path, width, height, os.path.getsize(path), round(brightness, 3), round(saturation, 3),
            json.dumps(top, ensure_ascii=False), round(parts[0], 3), round(parts[1], 3), round(parts[2], 3),
            1 if any(hint in lowered for hint in COVER_NAME_HINTS) else 0,
        )
    except (OSError, ValueError, SyntaxError):
        return None


class CoverExtractor(Extractor):
    name = "covers"
    schema = """
CREATE TABLE IF NOT EXISTS covers (
    entry_id INTEGER NOT NULL, path TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, bytes INTEGER NOT NULL,
    brightness REAL NOT NULL, saturation REAL NOT NULL, palette TEXT NOT NULL,
    edge_top REAL NOT NULL, edge_mid REAL NOT NULL, edge_bottom REAL NOT NULL, named INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS covers_entry ON covers(entry_id);
"""
    tables = {"covers": (
        "path", "width", "height", "bytes", "brightness", "saturation", "palette",
        "edge_top", "edge_mid", "edge_bottom", "named",
    )}

    def enabled(self, options: ScanOptions) -> bool:
        return options.covers

    def extract(self, entry: ArchiveEntry, context: ExtractContext) -> Mapping[str, Sequence[Sequence]]:
        rows = []
        for path in entry.covers[: max(0, context.options.cover_limit)]:
            row = cover_features(path)
            if row is not None:
                rows.append(row)
        return {"covers": rows}
