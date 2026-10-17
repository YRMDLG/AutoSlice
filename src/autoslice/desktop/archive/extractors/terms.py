"""候选术语：从最终字幕和标题里挖反复出现的词（主播的梗、人名、团体名、固定说法）。

不依赖分词库：取汉字连续片段的 2~6 字 n-gram 和英文词，按文件夹计数；跨文件夹的汇总、去掉“只是更长词的一截”
的碎片、过滤已有术语，都在报告阶段做。这里只管便宜、可增量。结果是候选，要不要进术语表由用户（或之后的大模型）判断。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Mapping, Sequence

from ..model import ArchiveEntry
from .base import ExtractContext, Extractor
from .common import final_cues

_ZH_RUN = re.compile(r"[一-鿿]{2,}")
_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{1,15}")
# 词的开头或结尾不会是这些虚字；整段都是虚字的更不是词。
_EDGE_STOP = frozenset("的了是在和与及而也就都着吗吧呢啊嘛呀嗯哦呃额")
_MAX_RUN = 80
MIN_GRAM, MAX_GRAM = 2, 6


def grams(text: str) -> Counter:
    """一段文字里的候选词及出现次数。"""

    found: Counter = Counter()
    for run in _ZH_RUN.findall(text):
        run = run[:_MAX_RUN]
        for size in range(MIN_GRAM, MAX_GRAM + 1):
            for start in range(0, len(run) - size + 1):
                piece = run[start:start + size]
                if piece[0] in _EDGE_STOP or piece[-1] in _EDGE_STOP:
                    continue
                found[piece] += 1
    for token in _ASCII_TOKEN.findall(text):
        found[token] += 1
    return found


class TermExtractor(Extractor):
    name = "terms"
    schema = """
CREATE TABLE IF NOT EXISTS terms (entry_id INTEGER NOT NULL, term TEXT NOT NULL, n INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS terms_entry ON terms(entry_id);
CREATE INDEX IF NOT EXISTS terms_term ON terms(term);
"""
    tables = {"terms": ("term", "n")}

    def extract(self, entry: ArchiveEntry, context: ExtractContext) -> Mapping[str, Sequence[Sequence]]:
        cues, _source = final_cues(entry, context)
        counts: Counter = Counter()
        for cue in cues:
            counts.update(grams(cue.text))
        title_terms = grams(entry.title)
        # 视频里至少说了两次，或者标题里就有，才算候选；标题里的词权重更高。
        kept = {term: n for term, n in counts.items() if n >= 2 or term in title_terms}
        for term, n in title_terms.items():
            kept[term] = kept.get(term, 0) + n * 3
        ranked = sorted(kept.items(), key=lambda item: (-item[1] * len(item[0]), item[0]))
        return {"terms": [(term, n) for term, n in ranked[: context.options.term_cap]]}
