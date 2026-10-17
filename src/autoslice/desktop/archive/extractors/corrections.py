"""从“字幕初稿 + 校对稿”这对文件里学：你把哪些词改成了哪些词，平时爱动哪一类。

逐条对齐初稿和校对稿，只在一对一的条目上比较；改动分成错词替换、只改标点、删口癖、删字、加字、
换行调整，拆分、合并另算。错词替换复用桌面端已有的 correction_pairs（只学短的、像错词的替换）。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Mapping, Sequence

from autoslice.desktop.correction_memory import correction_pairs

from ..model import ArchiveEntry
from ..srt import Cue, load_cues
from .base import ExtractContext, Extractor

_PUNCT = re.compile(r"[\W_]+", re.UNICODE)
_FILLERS = frozenset("呃嗯啊哦额唉哎噢")
_MIN_OVERLAP = 0.3


def _plain(text: str) -> str:
    return _PUNCT.sub("", text)


def classify_edit(original: str, corrected: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    """一条字幕的改动属于哪一类；错词替换同时返回学到的（错词，正确词）。"""

    if original == corrected:
        return "same", ()
    if "".join(original.split()) == "".join(corrected.split()):
        return "linebreak", ()
    plain_original, plain_corrected = _plain(original), _plain(corrected)
    if plain_original == plain_corrected:
        return "punctuation", ()
    if plain_corrected == "".join(ch for ch in plain_original if ch not in _FILLERS):
        return "delete_filler", ()
    pairs = correction_pairs(original, corrected)
    if pairs:
        return "replace", pairs
    if len(plain_corrected) < len(plain_original):
        return "delete_text", ()
    if len(plain_corrected) > len(plain_original):
        return "insert_text", ()
    return "other", ()


def _overlap(a: Cue, b: Cue) -> float:
    shared = min(a.end, b.end) - max(a.start, b.start)
    return shared / max(0.001, min(a.end - a.start, b.end - b.start))


def align_cues(raw: Sequence[Cue], corrected: Sequence[Cue]) -> tuple[list[tuple[Cue, Cue]], int, int]:
    """初稿和校对稿的一对一条目，以及拆分、合并的次数。

    条数相同且按位置配起来时间基本对得上（≥80%），就按位置配；否则按时间重叠配，
    只留一对一的，拆分（一条变多条）和合并（多条变一条）另外计数。
    """

    if len(raw) == len(corrected):
        paired = list(zip(raw, corrected))
        if raw and sum(1 for a, b in paired if _overlap(a, b) >= _MIN_OVERLAP) >= 0.8 * len(paired):
            return paired, 0, 0
    raw_hits: dict[int, list[int]] = {}
    corrected_hits: dict[int, list[int]] = {}
    cursor = 0
    for j, target in enumerate(corrected):
        while cursor < len(raw) and raw[cursor].end < target.start:
            cursor += 1
        probe = cursor
        while probe < len(raw) and raw[probe].start < target.end:
            if _overlap(raw[probe], target) >= _MIN_OVERLAP:
                raw_hits.setdefault(probe, []).append(j)
                corrected_hits.setdefault(j, []).append(probe)
            probe += 1
    pairs = [
        (raw[i], corrected[js[0]]) for i, js in raw_hits.items()
        if len(js) == 1 and len(corrected_hits.get(js[0], ())) == 1
    ]
    splits = sum(1 for js in raw_hits.values() if len(js) > 1)
    merges = sum(1 for is_ in corrected_hits.values() if len(is_) > 1)
    return pairs, splits, merges


class CorrectionExtractor(Extractor):
    name = "corrections"
    schema = """
CREATE TABLE IF NOT EXISTS corrections (
    entry_id INTEGER NOT NULL, wrong TEXT NOT NULL, right TEXT NOT NULL, n INTEGER NOT NULL,
    kept INTEGER NOT NULL DEFAULT 0, context TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS corrections_entry ON corrections(entry_id);
CREATE INDEX IF NOT EXISTS corrections_pair ON corrections(wrong, right);
CREATE TABLE IF NOT EXISTS edits (entry_id INTEGER NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS edits_entry ON edits(entry_id);
"""
    tables = {"corrections": ("wrong", "right", "n", "kept", "context"), "edits": ("kind", "n")}

    def extract(self, entry: ArchiveEntry, context: ExtractContext) -> Mapping[str, Sequence[Sequence]]:
        hits: Counter[tuple[str, str]] = Counter()
        kept: Counter[tuple[str, str]] = Counter()
        contexts: dict[tuple[str, str], str] = {}
        kinds: Counter[str] = Counter()
        for video in entry.videos:
            if not (video.srt and video.corrected):
                continue
            raw, corrected = load_cues(video.srt), load_cues(video.corrected)
            if not raw or not corrected:
                continue
            pairs, splits, merges = align_cues(raw, corrected)
            kinds["split"] += splits
            kinds["merge"] += merges
            found: set[tuple[str, str]] = set()
            for before, after in pairs:
                kind, learned = classify_edit(before.text, after.text)
                kinds[kind] += 1
                for pair in learned:
                    hits[pair] += 1
                    found.add(pair)
                    contexts.setdefault(pair, before.text.replace("\n", " ")[:80])
            # 错词在校对稿的其他地方还原样留着几处：留得多说明它多半是对的词，不能当固定纠错。
            for wrong, right in found:
                kept[(wrong, right)] += sum(cue.text.count(wrong) for cue in corrected)
        return {
            "corrections": [
                (wrong, right, n, kept[(wrong, right)], contexts.get((wrong, right), "")) for (wrong, right), n in hits.items()
            ],
            "edits": [(kind, n) for kind, n in kinds.items() if n],
        }
