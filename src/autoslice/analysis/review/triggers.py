"""SC 与礼物触发上下文的唯一实现。"""

from __future__ import annotations

import re

from autoslice.analysis.review import policy as clip_policy
from autoslice.transcription import segments as transcription_segments

FACADE_EXPORTS = {
    "_TRIGGER_CONTEXT_TOPIC_RE": "_TRIGGER_CONTEXT_TOPIC_RE",
    "_looks_like_sc_or_gift_trigger": "_looks_like_sc_or_gift_trigger",
    "_is_explicit_sc_trigger": "_is_explicit_sc_trigger",
    "_gift_trigger_has_question_followup": "_gift_trigger_has_question_followup",
    "_find_sc_context_start": "_find_sc_context_start",
    "_clip_context_requires_trigger": "_clip_context_requires_trigger",
    "_is_explicit_sc_topic": "_is_explicit_sc_topic",
}

SC_TRIGGER_KEYWORDS = clip_policy.SC_TRIGGER_KEYWORDS
THANKS_TRIGGER_RE = clip_policy.THANKS_TRIGGER_RE
SC_CONTEXT_LOOKBACK_SEC = clip_policy.SC_CONTEXT_LOOKBACK_SEC
SC_FALLBACK_GIFT_LOOKBACK_SEC = clip_policy.SC_FALLBACK_GIFT_LOOKBACK_SEC
TOPIC_CONTEXT_GAP = transcription_segments.TOPIC_CONTEXT_GAP


def _looks_like_sc_or_gift_trigger(text):
    """判断字幕文本是否像 SC/礼物/付费留言触发点；兼容 ASR 把 SC 漏识别的情况。"""
    compact = re.sub(r'\s+', ' ', (text or "")).strip()
    if not compact:
        return False
    lower = compact.lower()
    if any(keyword in lower for keyword in SC_TRIGGER_KEYWORDS):
        return True
    return bool(THANKS_TRIGGER_RE.search(compact))


def _is_explicit_sc_trigger(text):
    """只有明确识别到 SC/醒目留言时，才允许跨较长时间回溯。"""
    lower = re.sub(r'\s+', ' ', (text or "")).strip().lower()
    return any(keyword in lower for keyword in (
        "sc", "s c", "super chat", "superchat", "醒目留言", "醒目", "付费留言",
    ))


def _gift_trigger_has_question_followup(index, topic_start, srt_segments, window_sec=45):
    """ASR 漏掉 SC 名词时，用紧随礼物感谢后的提问文本确认关联。"""
    if not 0 <= index < len(srt_segments):
        return False
    trigger_start = srt_segments[index][0]
    texts = []
    for seg_start, _, text in srt_segments[index:index + 12]:
        if seg_start > topic_start or seg_start > trigger_start + window_sec:
            break
        texts.append(text or "")
    compact = re.sub(r'\s+', '', "".join(texts))
    return bool(re.search(
        r'(?:他说|她说|音悦生说|观众说|问|留言).{0,50}'
        r'(?:吗|呢|怎么|为何|为什么|能不能|可不可以|怎么办|[？?])',
        compact,
    ))


def _find_sc_context_start(topic_start, srt_segments, lookback_sec=SC_CONTEXT_LOOKBACK_SEC):
    """在话题前回溯 SC/礼物触发字幕，返回应纳入切片的更早起点。"""
    if not srt_segments:
        return None
    window_start = max(0, topic_start - lookback_sec)
    candidates = [
        (idx, seg)
        for idx, seg in enumerate(srt_segments)
        if window_start <= seg[0] <= topic_start and _looks_like_sc_or_gift_trigger(seg[2])
    ]
    if not candidates:
        return None

    eligible = []
    for idx, seg in candidates:
        distance = topic_start - seg[0]
        if (
            distance <= SC_FALLBACK_GIFT_LOOKBACK_SEC
            or _is_explicit_sc_trigger(seg[2])
            or _gift_trigger_has_question_followup(idx, topic_start, srt_segments)
        ):
            eligible.append((idx, seg))
    if not eligible:
        return None

    idx, seg = eligible[-1]  # 用离话题最近的触发点，避免把更早无关礼物也切进来。
    start_s = seg[0]
    # SC 文本可能被 ASR 切成几句，向前吸附很近的连续字幕，保留完整提问/感谢。
    cursor = idx - 1
    while cursor >= 0:
        prev_start, prev_end, _ = srt_segments[cursor]
        if start_s - prev_end > TOPIC_CONTEXT_GAP or topic_start - prev_start > lookback_sec:
            break
        start_s = prev_start
        cursor -= 1
    return start_s


_TRIGGER_CONTEXT_TOPIC_RE = re.compile(
    r'(?:\bSC\b|s\s*c|super\s*chat|醒目留言|付费留言|'
    r'观众.{0,10}(?:留言|提问|问题|投稿|来信)|'
    r'(?:念|读|回应|回答).{0,10}(?:留言|提问|问题|投稿|来信)|'
    r'感谢.{0,12}(?:礼物|舰长|提督|总督)|(?:礼物|舰长|提督|总督).{0,10}(?:感谢|回应))',
    re.IGNORECASE,
)


def _clip_context_requires_trigger(mark):
    """判断话题是否确实由 SC、留言或礼物触发，避免普通话题回溯无关感谢。"""
    if "context_requires_trigger" in mark:
        return bool(mark.get("context_requires_trigger"))
    text = " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
        *[str(line) for line in mark.get("body") or []],
    ])
    return bool(_TRIGGER_CONTEXT_TOPIC_RE.search(text))


def _is_explicit_sc_topic(mark):
    text = " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
    ]).lower()
    return bool(re.search(r'(?:\bsc\b|s\s*c|super\s*chat|醒目留言|付费留言)', text))
