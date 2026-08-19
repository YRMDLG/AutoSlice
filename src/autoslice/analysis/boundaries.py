"""切片前后文、字幕安全边界、收播与非重叠决策的唯一实现。"""

from __future__ import annotations

import math

from autoslice.analysis.review import context_edges
from autoslice.analysis.review import context_evidence
from autoslice.analysis.review import deduplication as clip_deduplication
from autoslice.analysis.review import finalization
from autoslice.analysis.review import outro as outro_analysis
from autoslice.analysis.review import policy as clip_policy
from autoslice.analysis.review import transitions as transition_analysis
from autoslice.analysis.review import triggers as trigger_analysis
from autoslice.transcription import segments as transcription_segments
from autoslice.transcription import srt_io as transcription_srt_io

FACADE_EXPORTS = {
    "_expand_clip_mark_with_context": "_expand_clip_mark_with_context",
    "_expand_clip_marks_with_context": "_expand_clip_marks_with_context",
    "_find_relevant_topic_context_end": "_find_relevant_topic_context_end",
    "_find_relevant_topic_context_start": "_find_relevant_topic_context_start",
    "_srt_video_duration": "_srt_video_duration",
    "parse_srt_segments": "parse_srt_segments",
}


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
_BOUNDARY_SEMANTIC_SIGNAL_PATTERNS = context_evidence._BOUNDARY_SEMANTIC_SIGNAL_PATTERNS
_BOUNDARY_EVIDENCE_STOP_TERMS = context_evidence._BOUNDARY_EVIDENCE_STOP_TERMS
_normalise_boundary_evidence_text = context_evidence._normalise_boundary_evidence_text
_boundary_evidence_term_counts = context_evidence._boundary_evidence_term_counts
_score_boundary_evidence_text = context_evidence._score_boundary_evidence_text
_boundary_evidence_text_is_relevant = context_evidence._boundary_evidence_text_is_relevant
_boundary_semantic_signals = context_evidence._boundary_semantic_signals
_boundary_text_has_semantic_signal = context_evidence._boundary_text_has_semantic_signal
_subtitle_speech_chains = context_evidence._subtitle_speech_chains
_split_chain_crossing_topic_end = context_evidence._split_chain_crossing_topic_end
_boundary_context_has_speech = context_evidence._boundary_context_has_speech
_boundary_context_is_relevant = context_evidence._boundary_context_is_relevant


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

_NEXT_CASE_ASR_TRIGGER_RE = transition_analysis._NEXT_CASE_ASR_TRIGGER_RE
_TOPIC_CONCLUSION_RE = transition_analysis._TOPIC_CONCLUSION_RE
_TOPIC_DECISION_EVIDENCE_RE = transition_analysis._TOPIC_DECISION_EVIDENCE_RE
_TOPIC_DISCOURSE_CONTINUATION_RE = (
    transition_analysis._TOPIC_DISCOURSE_CONTINUATION_RE
)
_TOPIC_REFUND_RE = transition_analysis._TOPIC_REFUND_RE
_VISUAL_CASE_SHIFT_RE = transition_analysis._VISUAL_CASE_SHIFT_RE
_looks_like_delayed_topic_conclusion = (
    transition_analysis._looks_like_delayed_topic_conclusion
)
_looks_like_discourse_continuation = (
    transition_analysis._looks_like_discourse_continuation
)
_looks_like_low_score_visual_case_shift = (
    transition_analysis._looks_like_low_score_visual_case_shift
)
_looks_like_next_case_transition = transition_analysis._looks_like_next_case_transition
_next_report_topic_safe_boundary = transition_analysis._next_report_topic_safe_boundary

_TOPIC_LEAD_IN_TRIGGER_RE = context_edges._TOPIC_LEAD_IN_TRIGGER_RE
_VISUAL_REVIEW_TOPIC_RE = context_edges._VISUAL_REVIEW_TOPIC_RE
_VISUAL_REACTION_LEAD_IN_RE = context_edges._VISUAL_REACTION_LEAD_IN_RE
_find_topic_lead_in_start = context_edges._find_topic_lead_in_start
_find_visual_reaction_context_start = context_edges._find_visual_reaction_context_start
_find_next_topic_hard_end = context_edges._find_next_topic_hard_end


def _find_relevant_topic_context_start(mark, topic_start, topic_end, srt_segments):
    """用标题/要点匹配离核心最近的连续语链，识别真正案由起点。"""
    term_counts = context_evidence._boundary_evidence_term_counts(mark)
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
    chains = context_evidence._subtitle_speech_chains(
        srt_segments,
        search_start,
        search_end,
    )
    candidates = []
    for chain_index, chain in enumerate(chains):
        score = context_evidence._score_boundary_evidence_text(
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
        previous_score = context_evidence._score_boundary_evidence_text(
            " ".join(segment[2] for segment in previous_chain),
            term_counts,
        )
        if gap > TOPIC_HARD_TRANSITION_GAP_SEC or previous_score <= 0:
            break
        chain_start = max(float(search_start), float(previous_chain[0][0]))
        chain_index -= 1
    return int(math.floor(chain_start)), int(score)


def _find_relevant_topic_context_end(mark, topic_end, search_end, srt_segments):
    """保留静默后的同话题回应，并返回首个确认无关的后续语链起点。"""
    if not mark.get("boundary_evidence") or search_end <= topic_end:
        return topic_end, None, None

    term_counts = context_evidence._boundary_evidence_term_counts(mark)
    semantic_signals = context_evidence._boundary_semantic_signals(mark)
    chains = context_evidence._subtitle_speech_chains(
        srt_segments,
        topic_end,
        search_end,
    )
    if not chains:
        return topic_end, None, None

    records = []
    review_chains = [
        split_chain
        for chain in chains
        for split_chain in context_evidence._split_chain_crossing_topic_end(
            chain,
            topic_end,
        )
    ]
    for chain in review_chains:
        transition_start = next(
            (
                seg_start for seg_start, _, text in chain
                if (
                    seg_start >= topic_end
                    and (
                        transition_analysis._looks_like_next_case_transition(text)
                        or transition_analysis._looks_like_low_score_visual_case_shift(
                            text,
                            term_counts,
                        )
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
            "score": context_evidence._score_boundary_evidence_text(
                evidence_text,
                term_counts,
            ),
            "conclusion": transition_analysis._looks_like_delayed_topic_conclusion(
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
                context_evidence._boundary_evidence_text_is_relevant(
                    record_text,
                    term_counts,
                )
                or context_evidence._boundary_text_has_semantic_signal(
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
            and transition_analysis._looks_like_discourse_continuation(
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
                    context_evidence._boundary_evidence_text_is_relevant(
                        future_text,
                        term_counts,
                    )
                    or context_evidence._boundary_text_has_semantic_signal(
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
                transition_analysis._next_report_topic_safe_boundary(
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
        hard_context_end = context_edges._find_next_topic_hard_end(
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
        lead_in_start = context_edges._find_topic_lead_in_start(
            int(mark.get("reference_start", topic_start)),
            topic_start,
            srt_segments or [],
        )
        if lead_in_start is not None:
            if (
                    mark.get("boundary_evidence")
                    and relevant_context_start is not None
                    and lead_in_start < relevant_context_start
                    and not context_evidence._boundary_context_is_relevant(
                        mark,
                        lead_in_start,
                        relevant_context_start,
                        srt_segments or [],
                    )):
                lead_in_start = None
    if semantic_focus and not trigger_analysis._clip_context_requires_trigger(mark):
        visual_lead_in_start = context_edges._find_visual_reaction_context_start(
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
                    and context_evidence._boundary_context_has_speech(
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
