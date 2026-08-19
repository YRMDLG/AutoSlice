"""切片边界上下文证据的唯一实现。"""

from __future__ import annotations

import re
from collections import defaultdict

from autoslice.analysis.review import policy as clip_policy
from autoslice.streamer_profiles import current_streamer_profile
from autoslice.transcription import segments as transcription_segments
from autoslice.transcription import service as transcription_service

FACADE_EXPORTS = {
    "_BOUNDARY_SEMANTIC_SIGNAL_PATTERNS": "_BOUNDARY_SEMANTIC_SIGNAL_PATTERNS",
    "_BOUNDARY_EVIDENCE_STOP_TERMS": "_BOUNDARY_EVIDENCE_STOP_TERMS",
    "_normalise_boundary_evidence_text": "_normalise_boundary_evidence_text",
    "_boundary_evidence_term_counts": "_boundary_evidence_term_counts",
    "_score_boundary_evidence_text": "_score_boundary_evidence_text",
    "_boundary_evidence_text_is_relevant": "_boundary_evidence_text_is_relevant",
    "_boundary_semantic_signals": "_boundary_semantic_signals",
    "_boundary_text_has_semantic_signal": "_boundary_text_has_semantic_signal",
    "_subtitle_speech_chains": "_subtitle_speech_chains",
    "_split_chain_crossing_topic_end": "_split_chain_crossing_topic_end",
    "_boundary_context_has_speech": "_boundary_context_has_speech",
    "_boundary_context_is_relevant": "_boundary_context_is_relevant",
}

_profile_identity_names = transcription_service.profile_identity_names
TOPIC_CONTEXT_GAP = transcription_segments.TOPIC_CONTEXT_GAP
TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE = clip_policy.TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE


_BOUNDARY_SEMANTIC_SIGNAL_PATTERNS = {
    # 直播口语常用不同词重复同一种身体反应；只靠二字以上字面重合会把
    # “想吐/发晕”之后的“低血糖/眩晕”误判成新话题并提前截断。
    "physical_distress": re.compile(
        r'(?:想吐|恶心|反胃|发晕|晕眩|眩晕|头晕|低血糖|站不稳|'
        r'身体受不住|身体受不了|吃不消)'
    ),
}


_BOUNDARY_EVIDENCE_STOP_TERMS = {
    "主播", "观众", "商家", "外卖", "评价", "差评", "这个", "那个",
    "真的", "然后", "开始", "继续", "感谢", "觉得", "表示", "看到", "观看",
    "内容", "话题", "视频", "弹幕", "回应", "一个", "没有", "怎么", "什么",
    "就是", "还是", "可以", "不是", "因为", "所以", "一下", "自己", "进行",
    "发现", "游戏", "关系",
    "默认",
}


def _normalise_boundary_evidence_text(value):
    return re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '', str(value or "")).lower()


def _boundary_evidence_term_counts(mark):
    """从标题和复核要点提取边界关键词；短词重复出现时权重更高。"""
    evidence = [
        mark.get("title", ""),
        mark.get("publish_title", ""),
        *(mark.get("boundary_evidence") or []),
    ]
    stop_terms = set(_BOUNDARY_EVIDENCE_STOP_TERMS)
    stop_terms.update(_profile_identity_names(current_streamer_profile()))
    counts = defaultdict(int)
    for value in evidence:
        normalised = _normalise_boundary_evidence_text(value)
        for run in re.findall(r'[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}', normalised):
            if re.fullmatch(r'[a-z0-9]+', run):
                counts[run] += 2
                continue
            for size in range(2, min(6, len(run)) + 1):
                for offset in range(len(run) - size + 1):
                    term = run[offset:offset + size]
                    if not any(
                        stop_term in term
                        for stop_term in stop_terms
                        if len(stop_term) >= 2
                    ):
                        counts[term] += 1
    return counts


def _score_boundary_evidence_text(text, term_counts):
    normalised = _normalise_boundary_evidence_text(text)
    score = 0
    for term, count in term_counts.items():
        if term not in normalised:
            continue
        length_weight = 1 if len(term) == 2 else 3 if len(term) == 3 else 5 if len(term) == 4 else 7
        score += length_weight + min(3, count - 1)
    return score


def _boundary_evidence_text_is_relevant(text, term_counts):
    """短句命中多次出现在标题/要点中的核心词时，也视为同话题证据。"""
    if _score_boundary_evidence_text(text, term_counts) >= TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE:
        return True
    normalised = _normalise_boundary_evidence_text(text)
    return any(
        len(term) >= 2 and count >= 4 and term in normalised
        for term, count in term_counts.items()
    )


def _boundary_semantic_signals(mark):
    """提取少量高置信同义概念，弥补 ASR 边界处的字面词形变化。"""

    evidence = " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
        *[str(item) for item in mark.get("boundary_evidence") or []],
    ])
    return {
        name
        for name, pattern in _BOUNDARY_SEMANTIC_SIGNAL_PATTERNS.items()
        if pattern.search(evidence)
    }


def _boundary_text_has_semantic_signal(text, semantic_signals):
    """判断后续语链是否延续候选已经明确出现的高置信概念。"""

    return any(
        _BOUNDARY_SEMANTIC_SIGNAL_PATTERNS[name].search(str(text or ""))
        for name in semantic_signals
    )


def _subtitle_speech_chains(srt_segments, minimum, maximum):
    selected = [
        segment for segment in srt_segments or []
        if segment[1] >= minimum and segment[0] <= maximum
    ]
    chains = []
    for segment in selected:
        if not chains or segment[0] - chains[-1][-1][1] > TOPIC_CONTEXT_GAP:
            chains.append([segment])
        else:
            chains[-1].append(segment)
    return chains


def _split_chain_crossing_topic_end(chain, topic_end):
    """拆开跨过核心终点的语链，避免后续新话题被连续语音整体吸入。"""
    if not chain or chain[0][0] > topic_end + 1 or chain[-1][1] <= topic_end:
        return [chain]

    core = [segment for segment in chain if segment[0] < topic_end]
    trailing = [segment for segment in chain if segment[0] >= topic_end]
    split = [core] if core else []
    split.extend([[segment] for segment in trailing])
    return split or [chain]


def _boundary_context_has_speech(start_s, end_s, srt_segments):
    if end_s <= start_s:
        return False
    return any(
        seg_end > start_s and seg_start < end_s
        for seg_start, seg_end, _ in srt_segments or []
    )


def _boundary_context_is_relevant(mark, start_s, end_s, srt_segments):
    texts = [
        text for seg_start, seg_end, text in srt_segments or []
        if seg_end > start_s and seg_start < end_s
    ]
    if not texts:
        return False
    return _score_boundary_evidence_text(
        " ".join(texts),
        _boundary_evidence_term_counts(mark),
    ) >= TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
