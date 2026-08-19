"""切片前后文、字幕安全边界、收播与非重叠决策的唯一实现。"""

from __future__ import annotations

import math
import re
from collections import defaultdict

from autoslice.analysis.review import deduplication as clip_deduplication
from autoslice.analysis.review import finalization
from autoslice.analysis.review import outro as outro_analysis
from autoslice.analysis.review import policy as clip_policy
from autoslice.analysis.review import triggers as trigger_analysis
from autoslice.streamer_profiles import current_streamer_profile
from autoslice.transcription import service as transcription_service
from autoslice.transcription import segments as transcription_segments
from autoslice.transcription import srt_io as transcription_srt_io

FACADE_EXPORTS = {
    "_BOUNDARY_EVIDENCE_STOP_TERMS": "_BOUNDARY_EVIDENCE_STOP_TERMS",
    "_NEXT_CASE_ASR_TRIGGER_RE": "_NEXT_CASE_ASR_TRIGGER_RE",
    "_TOPIC_CONCLUSION_RE": "_TOPIC_CONCLUSION_RE",
    "_TOPIC_DECISION_EVIDENCE_RE": "_TOPIC_DECISION_EVIDENCE_RE",
    "_TOPIC_DISCOURSE_CONTINUATION_RE": "_TOPIC_DISCOURSE_CONTINUATION_RE",
    "_TOPIC_LEAD_IN_TRIGGER_RE": "_TOPIC_LEAD_IN_TRIGGER_RE",
    "_TOPIC_REFUND_RE": "_TOPIC_REFUND_RE",
    "_VISUAL_CASE_SHIFT_RE": "_VISUAL_CASE_SHIFT_RE",
    "_VISUAL_REACTION_LEAD_IN_RE": "_VISUAL_REACTION_LEAD_IN_RE",
    "_VISUAL_REVIEW_TOPIC_RE": "_VISUAL_REVIEW_TOPIC_RE",
    "_boundary_context_has_speech": "_boundary_context_has_speech",
    "_boundary_context_is_relevant": "_boundary_context_is_relevant",
    "_boundary_evidence_term_counts": "_boundary_evidence_term_counts",
    "_boundary_evidence_text_is_relevant": "_boundary_evidence_text_is_relevant",
    "_expand_clip_mark_with_context": "_expand_clip_mark_with_context",
    "_expand_clip_marks_with_context": "_expand_clip_marks_with_context",
    "_find_next_topic_hard_end": "_find_next_topic_hard_end",
    "_find_relevant_topic_context_end": "_find_relevant_topic_context_end",
    "_find_relevant_topic_context_start": "_find_relevant_topic_context_start",
    "_find_topic_lead_in_start": "_find_topic_lead_in_start",
    "_find_visual_reaction_context_start": "_find_visual_reaction_context_start",
    "_looks_like_delayed_topic_conclusion": "_looks_like_delayed_topic_conclusion",
    "_looks_like_discourse_continuation": "_looks_like_discourse_continuation",
    "_looks_like_low_score_visual_case_shift": "_looks_like_low_score_visual_case_shift",
    "_looks_like_next_case_transition": "_looks_like_next_case_transition",
    "_next_report_topic_safe_boundary": "_next_report_topic_safe_boundary",
    "_normalise_boundary_evidence_text": "_normalise_boundary_evidence_text",
    "_score_boundary_evidence_text": "_score_boundary_evidence_text",
    "_split_chain_crossing_topic_end": "_split_chain_crossing_topic_end",
    "_srt_video_duration": "_srt_video_duration",
    "_subtitle_speech_chains": "_subtitle_speech_chains",
    "parse_srt_segments": "parse_srt_segments",
}


_profile_identity_names = transcription_service.profile_identity_names
_load_repaired_srt_segments = transcription_srt_io.load_repaired_srt_segments
TOPIC_CONTEXT_GAP = transcription_segments.TOPIC_CONTEXT_GAP

TOPIC_PRE_CONTEXT_SEC = clip_policy.TOPIC_PRE_CONTEXT_SEC
TOPIC_POST_CONTEXT_SEC = clip_policy.TOPIC_POST_CONTEXT_SEC
TOPIC_MIN_CLIP_SEC = clip_policy.TOPIC_MIN_CLIP_SEC
TOPIC_MAX_CLIP_SEC = clip_policy.TOPIC_MAX_CLIP_SEC
TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC = clip_policy.TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC = clip_policy.TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC
TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC = clip_policy.TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC
TOPIC_AI_FOCUS_PRE_CONTEXT_SEC = clip_policy.TOPIC_AI_FOCUS_PRE_CONTEXT_SEC
TOPIC_AI_FOCUS_POST_CONTEXT_SEC = clip_policy.TOPIC_AI_FOCUS_POST_CONTEXT_SEC
TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC = clip_policy.TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC
TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC = clip_policy.TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC
TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC = (
    clip_policy.TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC
)
TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC = (
    clip_policy.TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
)
TOPIC_LEAD_IN_RECOVERY_MIN_SEC = clip_policy.TOPIC_LEAD_IN_RECOVERY_MIN_SEC
TOPIC_LEAD_IN_LOOKBACK_SEC = clip_policy.TOPIC_LEAD_IN_LOOKBACK_SEC
TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC = clip_policy.TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC
TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC = clip_policy.TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC
TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC = clip_policy.TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC
TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE = clip_policy.TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
TOPIC_HARD_TRANSITION_GAP_SEC = clip_policy.TOPIC_HARD_TRANSITION_GAP_SEC
TOPIC_RELEVANT_CONTINUATION_GAP_SEC = clip_policy.TOPIC_RELEVANT_CONTINUATION_GAP_SEC
TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC = (
    clip_policy.TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC
)
TOPIC_REFERENCE_END_TOLERANCE_SEC = clip_policy.TOPIC_REFERENCE_END_TOLERANCE_SEC
OUTRO_TRIGGER_JOIN_GAP_SEC = clip_policy.OUTRO_TRIGGER_JOIN_GAP_SEC
OUTRO_VARIANT_FAREWELL_BEFORE_SEC = clip_policy.OUTRO_VARIANT_FAREWELL_BEFORE_SEC
OUTRO_VARIANT_FAREWELL_AFTER_SEC = clip_policy.OUTRO_VARIANT_FAREWELL_AFTER_SEC


_overlap_ratio = clip_deduplication._overlap_ratio
_is_duplicate_topic = clip_deduplication._is_duplicate_topic
_dedupe_clip_marks = clip_deduplication._dedupe_clip_marks
_nearest_safe_srt_boundary = finalization._nearest_safe_srt_boundary
_merge_expanded_clip_marks = finalization._merge_expanded_clip_marks
_refresh_natural_boundary_metadata = finalization._refresh_natural_boundary_metadata
_cap_expanded_clip_mark = finalization._cap_expanded_clip_mark
_snap_clip_to_srt_segments = finalization._snap_clip_to_srt_segments
_integer_clip_bounds_outside_subtitles = finalization._integer_clip_bounds_outside_subtitles
_fit_final_clip_to_safe_srt_boundaries = finalization._fit_final_clip_to_safe_srt_boundaries
_capped_speech_chain_start = finalization._capped_speech_chain_start


def parse_srt_segments(srt_path):
    """解析 SRT，返回 [(start_s, end_s, text), ...]。时间均为视频内时间，并修复明显异常时间戳。"""
    return _load_repaired_srt_segments(srt_path)


def _srt_video_duration(srt_segments):
    """用最后一句字幕估算可用视频时长。"""
    if not srt_segments:
        return None
    return max(seg_end for _, seg_end, _ in srt_segments)


_OUTRO_ACTIVITY_VARIANT_RE = outro_analysis._OUTRO_ACTIVITY_VARIANT_RE
_OUTRO_FAREWELL_EVIDENCE = outro_analysis._OUTRO_FAREWELL_EVIDENCE
_OUTRO_TRIGGER_NORMALISE_RE = outro_analysis._OUTRO_TRIGGER_NORMALISE_RE
_detect_stream_outro_clip = outro_analysis._detect_stream_outro_clip
_has_outro_farewell_evidence = outro_analysis._has_outro_farewell_evidence
_normalise_outro_trigger_text = outro_analysis._normalise_outro_trigger_text
_outro_topic_from_mark = outro_analysis._outro_topic_from_mark

_TRIGGER_CONTEXT_TOPIC_RE = trigger_analysis._TRIGGER_CONTEXT_TOPIC_RE
_looks_like_sc_or_gift_trigger = trigger_analysis._looks_like_sc_or_gift_trigger
_is_explicit_sc_trigger = trigger_analysis._is_explicit_sc_trigger
_gift_trigger_has_question_followup = trigger_analysis._gift_trigger_has_question_followup
_find_sc_context_start = trigger_analysis._find_sc_context_start
_clip_context_requires_trigger = trigger_analysis._clip_context_requires_trigger
_is_explicit_sc_topic = trigger_analysis._is_explicit_sc_topic


_TOPIC_LEAD_IN_TRIGGER_RE = re.compile(
    r'(?:对了|说到这个|说起来|你们猜|有个音悦生|有位音悦生|看到一条|念一条|刚才有|'
    r'昨天.{0,20}(?:发|送|问)|今天发的|接下来(?:看|玩)|'
    r'下一个(?:视频|话题|游戏|评价|差评|案例|商家|商品)?)'
)


_NEXT_CASE_ASR_TRIGGER_RE = re.compile(
    r'(?:再(?:看|能|给你)|来看|出给|给你|接着看|继续看)下(?:一(?:个)?|个)|'
    r'下一个(?:视频|话题|游戏|评价|差评|案例|商家|商品)?|'
    r'(?:看看|看一下)(?:他|她|商家|顾客|用户).{0,6}(?:说|写)(?:了)?什么|'
    r'(?:谁|有谁)记得(?:上次|之前).{0,20}(?:吗|嘛)?'
)


_TOPIC_DECISION_EVIDENCE_RE = re.compile(
    r'(?:判断|如何|是否|怎么办|怎么处理|结论|退款|退钱|退回|赔偿|补偿|换货)'
)


_TOPIC_CONCLUSION_RE = re.compile(
    r'(?:我觉得|所以|那就|这样(?:的话)?|应该|最终|最后|结论|总之|看来|结果|决定)'
    r'.{0,40}(?:可以|不可以|不行|不用|展示|通过|驳回|解决|处理|算了|'
    r'退款|退钱|退回|退掉|退了|赔偿|补偿|换货|保留|删除)|'
    r'(?:把|给).{0,20}(?:钱|款).{0,8}退(?:回|掉|了)|'
    r'(?:退款|退钱|退回|退掉|返钱)'
)


_TOPIC_REFUND_RE = re.compile(r'(?:退款|退钱|退回|退掉|退了|返钱|把.{0,20}钱.{0,8}退)')


_TOPIC_DISCOURSE_CONTINUATION_RE = re.compile(
    r'^(?:主要是|而且|然后|所以|但是|不过|就是|对(?:啊|呀|的)|确实|其实|'
    r'我想说|可怜|恭喜)|^.{0,16}(?:还(?:要|会|真|拿|点|给|说|有|在|数|是)|再补充)'
)


_BOUNDARY_SEMANTIC_SIGNAL_PATTERNS = {
    # 直播口语常用不同词重复同一种身体反应；只靠二字以上字面重合会把
    # “想吐/发晕”之后的“低血糖/眩晕”误判成新话题并提前截断。
    "physical_distress": re.compile(
        r'(?:想吐|恶心|反胃|发晕|晕眩|眩晕|头晕|低血糖|站不稳|'
        r'身体受不住|身体受不了|吃不消)'
    ),
}


_VISUAL_CASE_SHIFT_RE = re.compile(
    r'(?:左边|右边).{0,30}(?:赠品|原厂|非原装|遥控器|商品|图片)|'
    r'(?:原厂|非原装).{0,20}(?:遥控器|商品)|'
    r'这两个.{0,12}(?:遥控器|商品|图片)'
)


_VISUAL_REVIEW_TOPIC_RE = re.compile(
    r'(?:评价|差评|评论|照片|图片|视频|投稿|商品|外卖|美团|手套|画面)'
)


_VISUAL_REACTION_LEAD_IN_RE = re.compile(
    r'(?:这是在干|这到底是|谁.{0,8}(?:弄|放|干)|哪一个环节|'
    r'怎么回事|放大看|看一下.{0,8}(?:规格|图片)|这是什么)'
)


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


def _find_relevant_topic_context_start(mark, topic_start, topic_end, srt_segments):
    """用标题/要点匹配离核心最近的连续语链，识别真正案由起点。"""
    term_counts = _boundary_evidence_term_counts(mark)
    if not term_counts or not srt_segments:
        return None, 0
    reference_start = int(mark.get("reference_start", topic_start))
    # AI 参考起点也可能落在一句话中间，额外回看 15 秒恢复完整引子。
    search_start = max(
        0,
        reference_start - 15,
        topic_start - TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC,
    )
    search_end = min(topic_end, topic_start + TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC)
    chains = _subtitle_speech_chains(srt_segments, search_start, search_end)
    candidates = []
    for chain_index, chain in enumerate(chains):
        score = _score_boundary_evidence_text(
            " ".join(segment[2] for segment in chain),
            term_counts,
        )
        if score < TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE:
            continue
        chain_start = max(float(search_start), float(chain[0][0]))
        chain_end = min(float(search_end), float(chain[-1][1]))
        midpoint_distance = abs((chain_start + chain_end) / 2 - topic_start)
        candidates.append((midpoint_distance, -score, chain_index, chain_start, score))
    if not candidates:
        return None, 0
    _, _, chain_index, chain_start, score = min(candidates)
    while chain_index > 0:
        previous_chain = chains[chain_index - 1]
        gap = chain_start - previous_chain[-1][1]
        previous_score = _score_boundary_evidence_text(
            " ".join(segment[2] for segment in previous_chain),
            term_counts,
        )
        if gap > TOPIC_HARD_TRANSITION_GAP_SEC or previous_score <= 0:
            break
        chain_start = max(float(search_start), float(previous_chain[0][0]))
        chain_index -= 1
    return int(math.floor(chain_start)), int(score)


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


def _looks_like_next_case_transition(text):
    """识别“再看下一个”及常见 ASR 误识别，避免吞入下一案例。"""
    compact = re.sub(r'\s+', '', text or "")
    return bool(_NEXT_CASE_ASR_TRIGGER_RE.search(compact))


def _looks_like_delayed_topic_conclusion(mark, text, term_counts):
    """识别与案由相符、但在短暂停顿后才说出的判断或退款结论。"""
    compact = re.sub(r'\s+', '', text or "")
    if not compact or not _TOPIC_CONCLUSION_RE.search(compact):
        return False
    evidence = re.sub(r'\s+', '', " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
        *[str(item) for item in mark.get("boundary_evidence") or []],
    ]))
    if _TOPIC_REFUND_RE.search(evidence) and _TOPIC_REFUND_RE.search(compact):
        return True
    if _TOPIC_DECISION_EVIDENCE_RE.search(evidence):
        return True
    return _score_boundary_evidence_text(compact, term_counts) > 0


def _looks_like_discourse_continuation(text):
    """识别短暂停顿后以“主要是/还……”承接上一话题的补充句。"""
    return bool(_TOPIC_DISCOURSE_CONTINUATION_RE.search(
        re.sub(r'\s+', '', text or "")
    ))


def _looks_like_low_score_visual_case_shift(text, term_counts):
    """识别未说“下一个”、但画面和对象已明显切换的新案例。"""
    compact = re.sub(r'\s+', '', text or "")
    return bool(
        _VISUAL_CASE_SHIFT_RE.search(compact)
        and _score_boundary_evidence_text(compact, term_counts)
        < TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
    )


def _next_report_topic_safe_boundary(next_topic_start, topic_end, srt_segments):
    """下一话题时间落在字幕句内时，允许当前片段保留该句到句末。"""
    next_topic_start = int(next_topic_start)
    for seg_start, seg_end, _ in srt_segments or []:
        if seg_end <= topic_end:
            continue
        if seg_start >= next_topic_start:
            break
        if seg_start < next_topic_start < seg_end and seg_end - next_topic_start <= 10:
            return int(math.ceil(seg_end)), float(seg_start)
    return next_topic_start, None


def _find_relevant_topic_context_end(mark, topic_end, search_end, srt_segments):
    """保留静默后的同话题回应，并返回首个确认无关的后续语链起点。"""
    if not mark.get("boundary_evidence") or search_end <= topic_end:
        return topic_end, None, None

    term_counts = _boundary_evidence_term_counts(mark)
    semantic_signals = _boundary_semantic_signals(mark)
    chains = _subtitle_speech_chains(srt_segments, topic_end, search_end)
    if not chains:
        return topic_end, None, None

    records = []
    review_chains = [
        split_chain
        for chain in chains
        for split_chain in _split_chain_crossing_topic_end(chain, topic_end)
    ]
    for chain in review_chains:
        transition_start = next(
            (
                seg_start for seg_start, _, text in chain
                if (
                    seg_start >= topic_end
                    and (
                        _looks_like_next_case_transition(text)
                        or _looks_like_low_score_visual_case_shift(text, term_counts)
                    )
                )
            ),
            None,
        )
        evidence_text = " ".join(
            text for seg_start, _, text in chain
            if transition_start is None or seg_start < transition_start
        )
        records.append({
            "chain": chain,
            "start": max(float(topic_end), float(chain[0][0])),
            "end": max(float(topic_end), float(chain[-1][1])),
            "score": _score_boundary_evidence_text(evidence_text, term_counts),
            "conclusion": _looks_like_delayed_topic_conclusion(
                mark,
                evidence_text,
                term_counts,
            ),
            "transition_start": transition_start,
        })

    nearby_transition = next(
        (
            record["transition_start"] for record in records
            if (
                record["transition_start"] is not None
                and record["transition_start"] - topic_end <= 90
            )
        ),
        None,
    )
    if nearby_transition is not None:
        prior_segments = [
            segment
            for record in records
            for segment in record["chain"]
            if segment[0] < nearby_transition
        ]
        context_end = max(
            [float(topic_end), *[segment[1] for segment in prior_segments]]
        )
        return int(math.ceil(context_end)), int(math.floor(nearby_transition)), None

    context_end = float(topic_end)
    natural_grace_used = False
    relevant_context_seen = False
    delayed_conclusion_end = None
    for index, record in enumerate(records):
        transition_start = record["transition_start"]
        if transition_start is not None:
            before_transition = [
                segment for segment in record["chain"]
                if segment[0] < transition_start
            ]
            if before_transition:
                context_end = max(context_end, before_transition[-1][1])
            return (
                int(math.ceil(context_end)),
                int(math.floor(transition_start)),
                delayed_conclusion_end,
            )

        starts_inside_core = record["chain"][0][0] <= topic_end + 1
        record_text = " ".join(segment[2] for segment in record["chain"])
        evidence_relevant = (
            (
                _boundary_evidence_text_is_relevant(record_text, term_counts)
                or _boundary_text_has_semantic_signal(
                    record_text,
                    semantic_signals,
                )
            )
            and record["start"] - context_end <= TOPIC_RELEVANT_CONTINUATION_GAP_SEC
        )
        delayed_conclusion = (
            relevant_context_seen
            and record["conclusion"]
            and record["start"] - context_end <= TOPIC_RELEVANT_CONTINUATION_GAP_SEC
        )
        discourse_continuation = (
            record["start"] - context_end <= TOPIC_HARD_TRANSITION_GAP_SEC
            and _looks_like_discourse_continuation(
                " ".join(segment[2] for segment in record["chain"])
            )
        )
        natural_closure = (
            not natural_grace_used
            and record["start"] - context_end
            <= TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
        )
        if (
                starts_inside_core
                or evidence_relevant
                or delayed_conclusion
                or discourse_continuation
                or natural_closure):
            context_end = max(context_end, record["end"])
            if delayed_conclusion:
                delayed_conclusion_end = max(
                    delayed_conclusion_end or topic_end,
                    int(math.ceil(record["end"])),
                )
            relevant_context_seen = (
                relevant_context_seen or starts_inside_core or evidence_relevant
            )
            if record["chain"][0][0] >= topic_end and natural_closure:
                natural_grace_used = True
            continue

        future_relevant = False
        for future in records[index + 1:]:
            if future["transition_start"] is not None:
                break
            if future["start"] - context_end > TOPIC_RELEVANT_CONTINUATION_GAP_SEC:
                break
            future_text = " ".join(segment[2] for segment in future["chain"])
            if (
                    _boundary_evidence_text_is_relevant(future_text, term_counts)
                    or _boundary_text_has_semantic_signal(
                        future_text,
                        semantic_signals,
                    )):
                future_relevant = True
                break
            if relevant_context_seen and future["conclusion"]:
                future_relevant = True
                break
        if future_relevant:
            continue
        return (
            int(math.ceil(context_end)),
            int(math.floor(record["start"])),
            delayed_conclusion_end,
        )

    return int(math.ceil(context_end)), None, delayed_conclusion_end


def _find_topic_lead_in_start(reference_start, topic_start, srt_segments):
    """长话题的 AI 核心偏晚时，从参考范围内恢复明确的新话题触发语句。"""
    if not srt_segments or topic_start - reference_start < 30:
        return None
    search_start = max(reference_start, topic_start - TOPIC_LEAD_IN_LOOKBACK_SEC)
    triggers = []
    for seg_start, _, text in srt_segments:
        if seg_start < search_start:
            continue
        if seg_start >= topic_start:
            break
        if _TOPIC_LEAD_IN_TRIGGER_RE.search(re.sub(r'\s+', '', text or "")):
            triggers.append(seg_start)
    if not triggers:
        return None

    # 同一引子可能连续拆成“对了 / 你们猜”等数句。取离核心最近的
    # 一组触发词，但保留该组第一句。
    cluster_start = triggers[0]
    previous = triggers[0]
    for trigger in triggers[1:]:
        if trigger - previous > 20:
            cluster_start = trigger
        previous = trigger
    return cluster_start


def _find_visual_reaction_context_start(mark, topic_start, srt_segments):
    """为看图/评价类话题保留尚未说出主体名称时的第一反应。"""
    evidence_text = " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
        *[str(line) for line in mark.get("boundary_evidence") or []],
    ])
    if not _VISUAL_REVIEW_TOPIC_RE.search(evidence_text):
        return None

    search_start = max(0, topic_start - TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC)
    triggers = [
        seg_start
        for seg_start, _, text in srt_segments or []
        if (
            search_start <= seg_start < topic_start
            and _VISUAL_REACTION_LEAD_IN_RE.search(re.sub(r'\s+', '', text or ""))
        )
    ]
    if not triggers:
        return None

    cluster_start = triggers[0]
    previous = triggers[0]
    for trigger in triggers[1:]:
        if trigger - previous > 30:
            cluster_start = trigger
        previous = trigger
    return int(math.floor(cluster_start))


def _find_next_topic_hard_end(
        topic_end, reference_end, search_end, srt_segments,
        stop_at_gift_trigger=False):
    """核心已到参考范围末尾时，用明确转场阻止固定后文吞入下一话题。"""
    if (
        not srt_segments
        or (not stop_at_gift_trigger and reference_end - topic_end > 5)
    ):
        return None
    for index, (seg_start, _, text) in enumerate(srt_segments):
        if seg_start < topic_end:
            continue
        if seg_start > search_end:
            break
        compact = re.sub(r'\s+', '', text or "")
        if not (
            _TOPIC_LEAD_IN_TRIGGER_RE.search(compact)
            or trigger_analysis._is_explicit_sc_trigger(compact)
            or (
                trigger_analysis._looks_like_sc_or_gift_trigger(compact)
                and (
                    stop_at_gift_trigger
                    or trigger_analysis._gift_trigger_has_question_followup(
                        index,
                        search_end,
                        srt_segments,
                    )
                )
            )
        ):
            continue
        latest_boundary = math.floor(seg_start)
        boundary = finalization._nearest_safe_srt_boundary(
            latest_boundary,
            math.ceil(topic_end),
            latest_boundary,
            srt_segments,
        )
        return boundary if boundary is not None else latest_boundary
    return None


def _expand_clip_mark_with_context(mark, srt_segments=None, video_duration=None):
    """把 LLM 标记的话题范围扩展为真正用于 ffmpeg 的前后文切片范围。"""
    topic_start = int(float(mark.get("topic_start", mark["start"])))
    topic_end = int(float(mark.get("topic_end", mark["end"])))
    if topic_end <= topic_start:
        topic_end = topic_start + 1

    relevant_context_start, relevant_context_score = _find_relevant_topic_context_start(
        mark,
        topic_start,
        topic_end,
        srt_segments or [],
    )
    if (
            relevant_context_score >= TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
            and not trigger_analysis._clip_context_requires_trigger(mark)
            and topic_start + 5 < relevant_context_start
            and relevant_context_start - topic_start <= TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC):
        topic_start = relevant_context_start

    raw_duration = topic_end - topic_start
    semantic_focus = bool(mark.get("semantic_focus_validated"))
    if semantic_focus:
        reference_start = int(mark.get("reference_start", topic_start))
        reference_end = int(mark.get("reference_end", topic_end))
        pre_context_sec = (
            TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC
            if topic_start - reference_start <= 5
            else TOPIC_AI_FOCUS_PRE_CONTEXT_SEC
        )
        post_context_sec = (
            TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC
            if reference_end - topic_end <= 5
            else TOPIC_AI_FOCUS_POST_CONTEXT_SEC
        )
        natural_pre_max_sec = TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC
        natural_post_max_sec = TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
    else:
        pre_context_sec = TOPIC_PRE_CONTEXT_SEC
        post_context_sec = TOPIC_POST_CONTEXT_SEC
        natural_pre_max_sec = TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC
        natural_post_max_sec = TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC

    start_s = max(0, topic_start - pre_context_sec)
    end_s = topic_end + post_context_sec
    hard_context_end = None
    relevant_context_end = topic_end
    delayed_conclusion_end = None
    if semantic_focus:
        next_topic_start = mark.get("next_report_topic_start")
        next_topic_boundary = None
        next_topic_crossing_start = None
        if next_topic_start is not None and int(next_topic_start) >= topic_end:
            next_topic_start = int(next_topic_start)
            next_topic_boundary, next_topic_crossing_start = (
                _next_report_topic_safe_boundary(
                    next_topic_start,
                    topic_end,
                    srt_segments or [],
                )
            )
        boundary_search_end = min(
            topic_end + TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC,
            topic_start + TOPIC_MAX_CLIP_SEC + TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC,
            int(mark.get("reference_end", topic_end))
            + TOPIC_REFERENCE_END_TOLERANCE_SEC,
        )
        if next_topic_boundary is not None:
            boundary_search_end = min(boundary_search_end, next_topic_boundary)
        (
            relevant_context_end,
            unrelated_next_start,
            delayed_conclusion_end,
        ) = _find_relevant_topic_context_end(
            mark,
            topic_end,
            boundary_search_end,
            srt_segments or [],
        )
        if (
                next_topic_crossing_start is not None
                and next_topic_start - topic_end
                <= TOPIC_RELEVANT_CONTINUATION_GAP_SEC
                and (
                    unrelated_next_start is None
                    or unrelated_next_start >= next_topic_crossing_start
                )):
            # 报告边界偶尔落在“说完这一项”这类跨界字幕中。保留整句，
            # 但若更早已有明确“下一个”，不能跨过它追到报告边界。
            relevant_context_end = max(relevant_context_end, next_topic_boundary)
            if (
                    unrelated_next_start is not None
                    and unrelated_next_start <= next_topic_crossing_start):
                unrelated_next_start = next_topic_boundary
        end_s = max(end_s, relevant_context_end)
        hard_context_end = _find_next_topic_hard_end(
            topic_end,
            int(mark.get("reference_end", topic_end)),
            end_s,
            srt_segments or [],
            stop_at_gift_trigger=trigger_analysis._clip_context_requires_trigger(mark),
        )
        if hard_context_end is not None:
            end_s = min(end_s, hard_context_end)
        if next_topic_start is not None:
            if (
                    topic_end - 5 <= next_topic_start <= end_s
                    and (
                        next_topic_start >= relevant_context_end
                        or next_topic_crossing_start is not None
                    )):
                next_topic_hard_end = next_topic_boundary or next_topic_start
                hard_context_end = (
                    min(hard_context_end, next_topic_hard_end)
                    if hard_context_end is not None
                    else next_topic_hard_end
                )
                end_s = min(end_s, hard_context_end)
        if unrelated_next_start is not None:
            hard_context_end = (
                min(hard_context_end, unrelated_next_start)
                if hard_context_end is not None
                else unrelated_next_start
            )
            end_s = min(end_s, hard_context_end)
    sc_context_start = None
    if trigger_analysis._clip_context_requires_trigger(mark):
        sc_context_start = trigger_analysis._find_sc_context_start(
            topic_start,
            srt_segments or [],
        )
    if sc_context_start is not None:
        start_s = min(start_s, sc_context_start)
    lead_in_start = None
    visual_lead_in_start = None
    if (
        semantic_focus
        and raw_duration >= TOPIC_LEAD_IN_RECOVERY_MIN_SEC
        and not trigger_analysis._clip_context_requires_trigger(mark)
    ):
        lead_in_start = _find_topic_lead_in_start(
            int(mark.get("reference_start", topic_start)),
            topic_start,
            srt_segments or [],
        )
        if lead_in_start is not None:
            if (
                    mark.get("boundary_evidence")
                    and relevant_context_start is not None
                    and lead_in_start < relevant_context_start
                    and not _boundary_context_is_relevant(
                        mark,
                        lead_in_start,
                        relevant_context_start,
                        srt_segments or [],
                    )):
                lead_in_start = None
    if semantic_focus and not trigger_analysis._clip_context_requires_trigger(mark):
        visual_lead_in_start = _find_visual_reaction_context_start(
            mark,
            topic_start,
            srt_segments or [],
        )

    boundary_trimmed_context = False
    if (
            trigger_analysis._clip_context_requires_trigger(mark)
            and sc_context_start is None
            and trigger_analysis._is_explicit_sc_topic(mark)):
        # 无法在字幕中找到明确 SC 名词时，AI 复核核心起点就是最可信的提问起点；
        # 不再机械带入前一话题的固定 20 秒。
        start_s = topic_start
        boundary_trimmed_context = True
    else:
        semantic_context_starts = [
            value for value in (
                sc_context_start,
                lead_in_start,
                visual_lead_in_start,
                relevant_context_start,
            )
            if value is not None and value <= topic_start
        ]
        if semantic_context_starts:
            semantic_context_start = min(semantic_context_starts)
            if semantic_context_start < start_s:
                start_s = semantic_context_start
            elif (
                    semantic_context_start > start_s
                    and _boundary_context_has_speech(
                        start_s,
                        semantic_context_start,
                        srt_segments or [],
                    )):
                # 语义案由前已有另一段讲话时裁掉；纯静默/画面铺垫仍保留。
                start_s = semantic_context_start
                boundary_trimmed_context = True

    if end_s - start_s < TOPIC_MIN_CLIP_SEC and not boundary_trimmed_context:
        deficit = TOPIC_MIN_CLIP_SEC - (end_s - start_s)
        left = deficit if hard_context_end is not None else int(deficit * 0.4)
        right = 0 if hard_context_end is not None else deficit - left
        start_s = max(0, start_s - left)
        end_s += right

    context_duration_limit = TOPIC_MAX_CLIP_SEC
    if relevant_context_end > topic_end:
        context_duration_limit += TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
    if raw_duration < TOPIC_MAX_CLIP_SEC and end_s - start_s > context_duration_limit:
        end_s = start_s + context_duration_limit
        if end_s < topic_end:
            end_s = topic_end
            start_s = max(0, end_s - context_duration_limit)

    context_start_s = start_s
    context_end_s = end_s
    start_s, end_s = finalization._snap_clip_to_srt_segments(
        start_s,
        end_s,
        srt_segments or [],
        natural_pre_max_sec=natural_pre_max_sec,
        natural_post_max_sec=natural_post_max_sec,
    )
    if hard_context_end is not None:
        end_s = min(end_s, hard_context_end)

    if video_duration:
        end_s = min(end_s, video_duration)
        if end_s - start_s < TOPIC_MIN_CLIP_SEC and start_s > 0:
            start_s = max(0, end_s - TOPIC_MIN_CLIP_SEC)

    expanded = dict(mark)
    expanded["topic_start"] = topic_start
    expanded["topic_end"] = topic_end
    if int(expanded.get("report_start", topic_start)) < topic_start:
        expanded["report_start"] = topic_start
    expanded["start"], expanded["end"] = finalization._integer_clip_bounds_outside_subtitles(
        start_s,
        end_s,
        srt_segments or [],
    )
    if hard_context_end is not None:
        expanded["end"] = min(expanded["end"], int(hard_context_end))
        expanded["hard_context_end"] = int(hard_context_end)
    expanded["time_basis"] = "video_elapsed_seconds"
    expanded["context_expanded"] = True
    expanded["context_pre_sec"] = pre_context_sec
    expanded["context_post_sec"] = post_context_sec
    expanded["context_start_before_natural"] = int(context_start_s)
    expanded["context_end_before_natural"] = int(context_end_s)
    required_context_starts = [
        value for value in (
            sc_context_start,
            lead_in_start,
            visual_lead_in_start,
            relevant_context_start,
        )
        if value is not None and value < topic_start
    ]
    if required_context_starts:
        expanded["required_context_start"] = int(min(required_context_starts))
    if relevant_context_end > topic_end:
        expanded["required_context_end"] = int(relevant_context_end)
    if delayed_conclusion_end is not None:
        expanded["required_context_overflow_end"] = int(delayed_conclusion_end)
    expanded = finalization._cap_expanded_clip_mark(expanded)
    return finalization._refresh_natural_boundary_metadata(expanded)


def _expand_clip_marks_with_context(marks, srt_segments=None, video_duration=None):
    """批量扩展切片上下文；输入/输出时间均为视频内秒数。"""
    outro_marks = [
        dict(mark) for mark in (marks or [])
        if mark.get("clip_type") == "stream_outro" and mark.get("preserve_to_video_end")
    ]
    ordinary_marks = [
        mark for mark in (marks or [])
        if mark.get("clip_type") != "stream_outro"
    ]

    def overlaps_outro(mark):
        try:
            start = float(mark["start"])
            end = float(mark["end"])
        except (KeyError, TypeError, ValueError):
            return False
        return any(
            start < float(outro["end"]) and end > float(outro["start"])
            for outro in outro_marks
        )

    # 收播片是用户明确指定的独立系列。普通尾部候选与它重叠时直接让位，
    # 既不合并成超长杂项，也不会输出两条内容高度重复的视频。
    ordinary_marks = [mark for mark in ordinary_marks if not overlaps_outro(mark)]
    expanded = [
        _expand_clip_mark_with_context(mark, srt_segments=srt_segments, video_duration=video_duration)
        for mark in _dedupe_clip_marks(ordinary_marks)
    ]
    merged = finalization._merge_expanded_clip_marks(expanded, srt_segments=srt_segments)
    ordinary_final = [
        finalization._fit_final_clip_to_safe_srt_boundaries(mark, srt_segments or [])
        for mark in merged
    ]
    ordinary_final = [mark for mark in ordinary_final if not overlaps_outro(mark)]
    outro_final = []
    for mark in _dedupe_clip_marks(outro_marks):
        item = dict(mark)
        if video_duration:
            item["end"] = int(math.ceil(float(video_duration)))
            item["topic_end"] = item["end"]
            item["report_end"] = item["end"]
        outro_final.append(finalization._fit_final_clip_to_safe_srt_boundaries(item, srt_segments or []))
    return sorted(
        [*ordinary_final, *outro_final],
        key=lambda item: (int(item.get("start", 0)), int(item.get("end", 0)), item.get("title", "")),
    )
