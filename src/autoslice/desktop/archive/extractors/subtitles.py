"""字幕习惯：你最终发布的字幕一条多长、停多久、读多快、爱不爱加标点和换行。

后面用来给“字幕质量提醒”定你自己的标准（比如你的字幕 90% 都在 X 字/秒以内，超过就提醒），
而不是套一个通用阈值。
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from ..model import ArchiveEntry
from .base import ExtractContext, Extractor
from .common import final_cues

_CHARS = re.compile(r"\w", re.UNICODE)
_TRAILING = frozenset("，。！？、；：,.!?;:…~～")
SHORT_SECONDS = 0.65
LONG_SECONDS = 8.0


def quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))])


class SubtitleStatsExtractor(Extractor):
    name = "subtitle_stats"
    schema = """
CREATE TABLE IF NOT EXISTS subtitle_stats (
    entry_id INTEGER NOT NULL, source TEXT NOT NULL, cues INTEGER NOT NULL, chars INTEGER NOT NULL, seconds REAL NOT NULL,
    cps_p50 REAL NOT NULL, cps_p90 REAL NOT NULL, cps_max REAL NOT NULL, chars_p50 REAL NOT NULL, chars_p90 REAL NOT NULL,
    line_p90 REAL NOT NULL, multiline REAL NOT NULL, trailing_punct REAL NOT NULL,
    short INTEGER NOT NULL, long INTEGER NOT NULL, overlap INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS subtitle_stats_entry ON subtitle_stats(entry_id);
"""
    tables = {"subtitle_stats": (
        "source", "cues", "chars", "seconds", "cps_p50", "cps_p90", "cps_max", "chars_p50", "chars_p90",
        "line_p90", "multiline", "trailing_punct", "short", "long", "overlap",
    )}

    def extract(self, entry: ArchiveEntry, context: ExtractContext) -> Mapping[str, Sequence[Sequence]]:
        cues, source = final_cues(entry, context)
        if not cues:
            return {"subtitle_stats": []}
        chars, speeds, line_widths, multiline, trailing, short, long_, overlap = [], [], [], 0, 0, 0, 0, 0
        seconds = 0.0
        ordered = sorted(cues, key=lambda cue: cue.start)
        for position, cue in enumerate(ordered):
            duration = cue.end - cue.start
            lines = [line for line in cue.text.split("\n") if line.strip()]
            count = len(_CHARS.findall(cue.text))
            chars.append(count)
            speeds.append(count / max(duration, 0.05))
            line_widths.append(max((len(_CHARS.findall(line)) for line in lines), default=0))
            multiline += 1 if len(lines) >= 2 else 0
            trailing += 1 if cue.text.rstrip()[-1:] in _TRAILING else 0
            short += 1 if duration < SHORT_SECONDS else 0
            long_ += 1 if duration > LONG_SECONDS else 0
            if position + 1 < len(ordered) and ordered[position + 1].start < cue.end - 0.01:
                overlap += 1
            seconds += duration
        total = len(ordered)
        return {"subtitle_stats": [(
            source, total, sum(chars), round(seconds, 2),
            round(quantile(speeds, 0.5), 3), round(quantile(speeds, 0.9), 3), round(max(speeds), 3),
            quantile(chars, 0.5), quantile(chars, 0.9), quantile(line_widths, 0.9),
            round(multiline / total, 4), round(trailing / total, 4), short, long_, overlap,
        )]}
