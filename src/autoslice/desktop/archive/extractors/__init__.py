"""抽取器登记处：扫描器按这里的顺序逐个跑；新抽取器在 DEFAULT_EXTRACTORS 里加一项即可。"""

from __future__ import annotations

from typing import Sequence

from .base import ExtractContext, Extractor
from .corrections import CorrectionExtractor
from .covers import CoverExtractor
from .subtitles import SubtitleStatsExtractor
from .terms import TermExtractor

DEFAULT_EXTRACTORS: tuple[Extractor, ...] = (
    CorrectionExtractor(),
    SubtitleStatsExtractor(),
    TermExtractor(),
    CoverExtractor(),
)


def schemas(extractors: Sequence[Extractor] = DEFAULT_EXTRACTORS) -> tuple[str, ...]:
    return tuple(extractor.schema for extractor in extractors if extractor.schema)


def fact_tables(extractors: Sequence[Extractor] = DEFAULT_EXTRACTORS) -> tuple[str, ...]:
    """所有抽取器的事实表名（删除文件夹时要一起清）。"""

    return tuple(dict.fromkeys(table for extractor in extractors for table in extractor.tables))


__all__ = ["DEFAULT_EXTRACTORS", "ExtractContext", "Extractor", "fact_tables", "schemas"]
