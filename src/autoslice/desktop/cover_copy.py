"""AutoCover vNext 的本地确定性基础文案提取。

这里只做抽取和压缩，不调用 AI、不编造标题中不存在的事实。
输出最多两个语义块：A 为最少上下文，B 为视觉主句。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autoslice_cover.document_layout import (
    CONNECTIVE_BREAK,
    SCRIPT_BREAK,
    SEMANTIC_BREAK,
    break_penalty,
)

_PREFIX_RE = re.compile(r"^\s*(?:[〖【\[].{1,32}?[〗】\]])\s*")
_TAG_RE = re.compile(r"^\s*[〖【\[](.{1,32}?)[〗】\]]")
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_QUOTE_RE = re.compile(r"[“「『\"]([^”」』\"]+)[”」』\"]")
_BOUNDARY_RE = re.compile(r"[，,。！？!?；;：:/／⁉‼⁈⁇]+")
_RELATION_RE = re.compile(r"(结果得知|结果|然后|随后|没想到|但是|却|吓得|气得|秒变)")
_SPACE_RE = re.compile(r"\s+")

_PACKAGING = (
    "究竟发生了什么",
    "原因居然是这个",
    "到底发生了什么",
    "竟然是因为这个",
)
_TAIL_COMMENTARY = (
    "闹麻了",
    "坏事做尽",
    "真的好甜",
    "太甜了",
    "笑死",
)
_STRONG_MARKERS = (
    "退钱",
    "滚",
    "不要",
    "不敢",
    "吓",
    "气",
    "大骂",
    "鸟叫",
    "秒懂",
    "心酸",
    "温柔",
    "销毁证据",
    "红色感叹号",
    "虚假宣传",
    "请勿外放",
    "万楼",
    "低调",
    "是真的",
    "上当",
    "秒懂",
    "总会有机会",
)
_HIGH_VALUE_MARKERS = (
    "虚假宣传",
    "销毁证据",
    "红色感叹号",
    "请勿外放",
    "退钱",
    "万楼",
    "鸟叫",
    "秒懂",
    "总会有机会",
    "上当",
)
_COMPACT_MARKERS = {
    "万楼": 4,
    "虚假宣传": 8,
    "退钱": 4,
    "销毁证据": 8,
    "红色感叹号": 8,
    "鸟叫": 6,
    "秒懂": 8,
    "总会有机会": 8,
    "请勿外放": 8,
    "上当": 8,
}
# 片段截取：起点只落在语义边界上，终点可带一个语气尾字，B 可带原文紧随的强调标点。
_TRAILING_PARTICLES = "的了啊吧呢呀啦"
_EMPHASIS_TAIL = "！？!?⁉‼⁈⁇"
_PHRASE_START_BREAKS = {SEMANTIC_BREAK, CONNECTIVE_BREAK, SCRIPT_BREAK}
_LEAD_ADVERBS = ("居然", "竟然", "突然", "直接", "结果", "然后", "就", "都", "也", "还", "又", "却")
_CONTEXT_STARTERS = (
    "我",
    "你",
    "这",
    "那",
    "不要",
    "退钱",
    "滚",
    "嘘",
    "总会",
    "再",
    "原来",
)
@dataclass(frozen=True, slots=True)
class BasicCoverCopy:
    """一套基础封面文案候选。"""

    context: str = ""
    headline: str = ""

    @property
    def text(self) -> str:
        return "\n".join(part for part in (self.context, self.headline) if part).strip()


@dataclass(frozen=True, slots=True)
class _Chunk:
    text: str
    index: int
    quoted: bool
    relation: bool
    # 所在引语序号（-1 为叙述）；lead 为截取时舍去的同句前半段。
    group: int = -1
    lead: str = ""


def _clean_title(title: str) -> str:
    cleaned = _PREFIX_RE.sub("", str(title).strip(), count=1)
    return _SPACE_RE.sub(" ", cleaned).strip()


def _phrase_start(value: str, target: int, limit: int) -> int:
    """在 target 附近找语义边界作为片段起点；找不到时从关键词开始。"""

    best: tuple[tuple[int, int], int] | None = None
    for start in range(max(0, target - 2), limit + 1):
        # 助词后不作起点：“的钱”这类会把名词切成残片。
        penalty = SEMANTIC_BREAK if start == 0 else break_penalty(value, start)
        if penalty not in _PHRASE_START_BREAKS:
            continue
        key = (abs(start - target), -start)
        if best is None or key < best[0]:
            best = (key, start)
    return best[1] if best else limit


def _phrase_end(value: str, minimum: int, maximum: int) -> int:
    """在 [minimum, maximum] 内取最靠后的语义边界作为终点。"""

    for end in range(min(len(value), maximum), minimum, -1):
        if end == len(value) or (break_penalty(value, end) or 1.0) <= SCRIPT_BREAK:
            return end
    end = minimum
    if end < len(value) and value[end] in _TRAILING_PARTICLES:
        end += 1
    return end


def _display_clean(text: str) -> tuple[str, str]:
    """清洗一个片段；过长时按语义边界截取，返回（片段，被舍去的前半段）。"""

    value = text.strip().strip("“”「」『』\"' ")
    for prefix in ("结果得知", "结果", "然后", "随后", "没想到"):
        if value.startswith(prefix) and len(value) - len(prefix) >= 3:
            value = value[len(prefix) :].lstrip("，, ")
            break
    value = value.strip()
    lead = ""
    # 旧版封面常用的短爆点只保留原题中紧邻该词的片段；这是截取，不是
    # 改写。起点落在语义边界，避免“居然”被切成“然”这类断词。
    for marker, prefix_length in _COMPACT_MARKERS.items():
        index = value.find(marker)
        if index >= 0 and len(value) > len(marker) + prefix_length:
            start = _phrase_start(value, max(0, index - prefix_length), index)
            end = _phrase_end(value, index + len(marker), index + len(marker))
            lead, value = value[:start], value[start:end].strip()
            break
    if len(value) > 18:
        matches = [
            (value.find(marker), marker)
            for marker in _HIGH_VALUE_MARKERS
            if marker in value
        ]
        if matches:
            index, marker = min(matches, key=lambda item: item[0])
            start = _phrase_start(value, max(0, index - 9), index)
            end = _phrase_end(value, index + len(marker), start + 18)
            lead, value = lead + value[:start], value[start:end].strip()
    return value, lead.strip()


def _title_names(title: str) -> tuple[str, ...]:
    """前缀标签里的主播名，用于从上下文里去掉重复主语。"""

    match = _TAG_RE.match(str(title))
    if not match:
        return ()
    parts = [match.group(1), *re.split(r"[&＆、/／·・]", match.group(1))]
    return tuple(sorted({part.strip() for part in parts if len(part.strip()) >= 2}, key=len, reverse=True))


def _strip_lead(lead: str, names: tuple[str, ...]) -> str:
    """截取剩下的前半段去掉主语名和副词，作为最小上下文候选。"""

    value = lead.strip(" ，,")
    for name in names:
        if value.startswith(name):
            value = value[len(name):]
            break
    changed = True
    while changed and value:
        changed = False
        for adverb in _LEAD_ADVERBS:
            if value.endswith(adverb) and len(value) > len(adverb):
                value, changed = value[: -len(adverb)], True
            if value.startswith(adverb) and len(value) > len(adverb):
                value, changed = value[len(adverb):], True
    return value.strip(" ，,")


def _with_emphasis(text: str, title: str) -> str:
    """B 后若原文紧跟“？！”等强调标点则一并保留，仍是原题子串。"""

    if not text or text[-1] in _EMPHASIS_TAIL:
        return text
    position = title.find(text)
    if position < 0:
        return text
    tail = ""
    for character in title[position + len(text):]:
        if character not in _EMPHASIS_TAIL or len(tail) >= 2:
            break
        tail += character
    return text + tail


def _chunks(title: str) -> tuple[_Chunk, ...]:
    cleaned = _clean_title(title)
    quotes = [_display_clean(item)[0] for item in _QUOTE_RE.findall(cleaned)]
    text = _EMOJI_RE.sub("|", cleaned)
    text = re.sub(r"[“”「」『』\"]", "|", text)
    text = _RELATION_RE.sub(r"|\1", text)
    text = _BOUNDARY_RE.sub("|", text)
    result: list[_Chunk] = []
    seen: set[str] = set()
    for raw in text.split("|"):
        value, lead = _display_clean(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        group = next(
            (index for index, quote in enumerate(quotes) if quote and (value in quote or quote in value)),
            -1,
        )
        relation = bool(_RELATION_RE.match(raw.strip()))
        result.append(_Chunk(value, len(result), group >= 0, relation, group, lead))
    return tuple(result)


def _headline_score(chunk: _Chunk, total: int) -> float:
    text = chunk.text
    score = 0.0
    if chunk.quoted:
        score += 3.2
    if chunk.relation:
        score += 2.4
    if any(marker in text for marker in _STRONG_MARKERS):
        score += 2.0
    if any(marker in text for marker in _HIGH_VALUE_MARKERS):
        score += 1.8
    if re.search(r"\d|[百千万]", text):
        score += 1.5
    length = len(text)
    if 3 <= length <= 16:
        score += 1.4
    elif length > 20:
        # 基础模式宁可换用更短的原文片段，也不把整句长标题塞进一个大框。
        score -= min(10.0, (length - 20) * 0.5)
    if total > 1:
        score += 1.25 * chunk.index / (total - 1)
    if any(item in text for item in _PACKAGING):
        score -= 12.0
    if any(item in text for item in _TAIL_COMMENTARY):
        score -= 4.0
    return score
def _needs_context(chunk: _Chunk) -> bool:
    text = chunk.text
    if any(item in text for item in ("请勿外放", "虚假宣传", "销毁证据", "红色感叹号")):
        return False
    if chunk.quoted and len(text) <= 16:
        return True
    if len(text) <= 6:
        return True
    return text.startswith(_CONTEXT_STARTERS)


def _should_pair_context(chunks: tuple[_Chunk, ...], headline: _Chunk) -> bool:
    """只在标题确实由“前因 / 后续反应”组成时生成 A。

    早期实现把整条投稿标题交给一个 TextObject，斜杠分隔的对话也因此
    变成一条长句。这里保留原文片段，并只对两段式、第二段不是强结果词的
    标题补最小上下文；强结果仍允许 B 单独成立。
    """

    if headline.index != 1 or len(chunks) != 2:
        return False
    if _needs_context(headline):
        return True
    if any(marker in headline.text for marker in _STRONG_MARKERS + _HIGH_VALUE_MARKERS):
        return False
    if headline.quoted or not 6 <= len(headline.text) <= 20:
        return False
    context = chunks[0].text
    return 6 <= len(context) <= 24 and context != headline.text


def _context_for(chunks: tuple[_Chunk, ...], headline: _Chunk, names: tuple[str, ...] = ()) -> str:
    if not _needs_context(headline):
        return ""
    candidates = [item for item in chunks if item.index < headline.index]
    ranked: list[tuple[float, _Chunk]] = []
    lead = _strip_lead(headline.lead, names)
    if 2 <= len(lead) <= 12 and lead not in headline.text:
        # 同一句里紧挨 B 的前半段最能补足 B 的上下文。
        ranked.append((4.6, _Chunk(lead, headline.index, headline.quoted, False, headline.group)))
    for item in candidates:
        if any(word in item.text for word in _PACKAGING + _TAIL_COMMENTARY):
            continue
        score = 0.0
        distance = headline.index - item.index
        score += max(0.0, 3.0 - (distance - 1) * 0.65)
        if 4 <= len(item.text) <= 18:
            score += 1.2
        elif len(item.text) > 18:
            score -= min(8.0, (len(item.text) - 18) * 0.8)
        interjection = len(item.text) <= 3 or len(set(item.text)) == 1
        if interjection:
            # “嘘嘘嘘”“钓钓钓”这类语气词补不了上下文。
            score -= 2.0
        elif headline.group >= 0 and item.group == headline.group and distance == 1:
            # 同一句引语的上半句（对话或前后半句）。
            score += 2.5
        elif not item.quoted:
            # 叙述句通常是事件背景，比另一句台词更能补足上下文。
            score += 1.5
        if any(marker in item.text for marker in _STRONG_MARKERS):
            score -= 0.5
        ranked.append((score, item))
    if not ranked:
        return ""
    context = max(ranked, key=lambda pair: (pair[0], pair[1].index))[1].text
    for name in names:
        if context.startswith(name) and len(context) - len(name) >= 4:
            context = context[len(name):]
            break
    if len(context) > 20:
        return ""
    if context in headline.text or headline.text in context:
        return ""
    return context


def generate_basic_copy_variants(
    title: str,
    *,
    limit: int = 4,
    subtitle_context: str | None = None,
) -> tuple[BasicCoverCopy, ...]:
    """生成 1~4 套本地基础文案候选；第一套为默认方案。"""

    chunks = _chunks(title)
    names = _title_names(title)
    # 字幕只作为当前片段的语义上下文；只把原文片段加入候选，不进行
    # 事实补写或营销式改写。标题已有明确爆点时，优先保留标题结果。
    context_chunks = _chunks(subtitle_context or "")
    if context_chunks and (not chunks or not any(any(marker in item.text for marker in _HIGH_VALUE_MARKERS) for item in chunks)):
        offset = len(chunks)
        chunks = chunks + tuple(
            _Chunk(item.text, offset + index, item.quoted, item.relation)
            for index, item in enumerate(context_chunks)
            if item.text not in {existing.text for existing in chunks}
        )
    if not chunks:
        fallback = _clean_title(title) or "未命名封面"
        return (BasicCoverCopy(headline=fallback[:28]),)
    ranked = sorted(
        chunks,
        key=lambda item: (_headline_score(item, len(chunks)), item.index),
        reverse=True,
    )
    variants: list[BasicCoverCopy] = []
    seen: set[str] = set()
    for headline in ranked:
        if any(item in headline.text for item in _PACKAGING):
            continue
        paired_context = _context_for(chunks, headline, names) if _needs_context(headline) else (
            chunks[0].text if _should_pair_context(chunks, headline) else ""
        )
        candidate = BasicCoverCopy(
            context=paired_context,
            headline=_with_emphasis(headline.text, title),
        )
        # A/B 已是两个独立 TextObject；组合总长度不再作为“巨大文本框”
        # 的判据，只限制每个语义块本身，避免再次丢掉必要上下文。
        if candidate.context and len(candidate.context) > 24:
            candidate = BasicCoverCopy(headline=candidate.headline)
        if candidate.text and candidate.text not in seen:
            variants.append(candidate)
            seen.add(candidate.text)
        if len(variants) >= max(1, limit - 1):
            break
    if ranked:
        b_only = BasicCoverCopy(headline=_with_emphasis(ranked[0].text, title))
        if b_only.text not in seen:
            variants.append(b_only)
    return tuple(variants[: max(1, limit)]) or (
        BasicCoverCopy(headline=_clean_title(title)[:28]),
    )
