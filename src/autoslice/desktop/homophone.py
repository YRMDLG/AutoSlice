"""同音检测：字幕里发音和术语表里的词一样、写法不同的片段，多半是识别听错的字（“紫雨亭”→“子语庭”）。

纯本地、不花额度、毫秒级；只给“建议”，由你逐条确认。靠 pypinyin 查拼音，没装就整体关闭（available() 为 False）。
两字词要求声调也一样（否则同音字太多、误报高），三字及以上只比拼音。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_RUN = re.compile(r"[㐀-鿿]+")
_MIN_LEN, _MAX_LEN = 2, 8


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    found: str
    term: str


def _load():
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return None
    return Style, lazy_pinyin


def available() -> bool:
    return _load() is not None


class HomophoneIndex:
    def __init__(self, terms: Iterable[str], *, enabled: bool = True) -> None:
        loaded = _load() if enabled else None
        self.ready = loaded is not None
        self._terms: set[str] = set()
        self._by_key: dict[tuple[int, tuple[str, ...]], list[str]] = {}
        if loaded is None:
            return
        self._style, self._pinyin = loaded
        for term in dict.fromkeys(str(item or "").strip() for item in terms):
            if _MIN_LEN <= len(term) <= _MAX_LEN and _RUN.fullmatch(term):
                self._terms.add(term)
                # 先出现的术语优先（固定词和主播名在前，学到的在后）
                self._by_key.setdefault((len(term), self._key(self._syllables(term), 0, len(term))), []).append(term)

    def _syllables(self, text: str) -> tuple[list[str], list[str]]:
        """(带声调, 不带声调)；纯汉字串每个字恰好一个音节。"""

        return (self._pinyin(text, style=self._style.TONE3), self._pinyin(text, style=self._style.NORMAL))

    @staticmethod
    def _key(syllables: tuple[list[str], list[str]], start: int, size: int) -> tuple[str, ...]:
        tone, plain = syllables
        return tuple((tone if size == 2 else plain)[start:start + size])

    def find(self, text: str) -> list[Hit]:
        if not self.ready or not self._by_key:
            return []
        sizes = sorted({size for size, _ in self._by_key})
        found: list[Hit] = []
        for run in _RUN.finditer(text):
            chars = run.group()
            syllables = self._syllables(chars)
            for size in sizes:
                for start in range(len(chars) - size + 1):
                    piece = chars[start:start + size]
                    if piece in self._terms:
                        continue
                    terms = self._by_key.get((size, self._key(syllables, start, size)))
                    if terms:
                        found.append(Hit(run.start() + start, run.start() + start + size, piece, terms[0]))
        # 重叠时留长的、靠前的
        chosen: list[Hit] = []
        for hit in sorted(found, key=lambda item: (-(item.end - item.start), item.start)):
            if all(hit.end <= other.start or hit.start >= other.end for other in chosen):
                chosen.append(hit)
        return sorted(chosen, key=lambda item: item.start)


def apply_hits(text: str, hits: Iterable[Hit]) -> str:
    """把命中的片段换成术语；从后往前换，前面的位置不受影响。"""

    for hit in sorted(hits, key=lambda item: item.start, reverse=True):
        text = text[:hit.start] + hit.term + text[hit.end:]
    return text


def describe(hits: Iterable[Hit]) -> str:
    return "；".join(f"“{hit.found}”发音和术语“{hit.term}”相同，疑似识别听错" for hit in hits)
