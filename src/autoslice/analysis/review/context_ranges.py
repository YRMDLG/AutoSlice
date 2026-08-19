"""话题上下文范围的唯一实现。"""

from __future__ import annotations

import math

from autoslice.analysis.review import context_evidence
from autoslice.analysis.review import policy as clip_policy
from autoslice.analysis.review import transitions as transition_analysis

FACADE_EXPORTS = {
    "_find_relevant_topic_context_end": "_find_relevant_topic_context_end",
    "_find_relevant_topic_context_start": "_find_relevant_topic_context_start",
}


TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC = clip_policy.TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC
TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC = clip_policy.TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC
TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE = clip_policy.TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
TOPIC_HARD_TRANSITION_GAP_SEC = clip_policy.TOPIC_HARD_TRANSITION_GAP_SEC
TOPIC_RELEVANT_CONTINUATION_GAP_SEC = clip_policy.TOPIC_RELEVANT_CONTINUATION_GAP_SEC
TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC = (
    clip_policy.TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
)


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
