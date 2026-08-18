"""候选独立复核所需的原始证据与输入构造。"""

from autoslice import timecode
from autoslice.analysis import clip_policy
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis import evidence as candidate_evidence
from autoslice.analysis.topic import titles as title_analysis

FACADE_EXPORTS = {
    "_clip_review_candidate": "build_clip_review_candidate",
    "_fresh_manual_topic_evidence": "fresh_manual_topic_evidence",
}


def fresh_manual_topic_evidence(topic, srt_segments=None, peaks=None):
    """重建原始证据，避免沿用上一轮 AI 摘要造成错误自证。"""
    start = int(topic.get("start", 0))
    end = max(start + 1, int(topic.get("end", start + 1)))
    body = []
    if peaks:
        body.extend(
            candidate_evidence.topic_danmaku_reference_lines(start, end, peaks)
        )
    if srt_segments:
        body.extend(
            candidate_evidence.topic_srt_summary_lines(start, end, srt_segments)
        )

    for entry in topic.get("manual_timeline") or []:
        source_entries = entry.get("original_entries") or [entry]
        for source_entry in source_entries:
            stars = int(source_entry.get("stars", entry.get("stars", 0)))
            prefix = f"●人工时间轴{'⭐' * min(stars, 5)}" if stars else "·时间轴"
            line = (
                f"{prefix}：{timecode.format_elapsed(int(source_entry.get('start', start)))} "
                f"{source_entry.get('text', '')}"
            )
            if line not in body:
                body.append(line)
    return body


def build_clip_review_candidate(
    topic,
    srt_segments,
    peaks,
    density_series=None,
):
    """用原字幕重新构造高能候选，首轮标题和摘要不作为复核证据。"""
    source_start = int(topic.get("start", 0))
    source_end = max(source_start + 1, int(topic.get("end", source_start + 1)))
    review_start = max(0, source_start - clip_policy.TOPIC_PRE_CONTEXT_SEC)
    review_end = source_end + clip_policy.TOPIC_POST_CONTEXT_SEC
    candidate = dict(topic)
    candidate["start"] = review_start
    candidate["end"] = review_end
    candidate["start_str"] = timecode.format_elapsed(review_start)
    candidate["end_str"] = timecode.format_elapsed(review_end)

    core_subtitle_evidence = candidate_evidence.topic_srt_summary_lines(
        source_start,
        source_end,
        srt_segments,
    )
    candidate["core_subtitle_evidence"] = [
        title_analysis._strip_body_prefix(line)
        for line in core_subtitle_evidence
        if title_analysis._strip_body_prefix(line)
    ]
    candidate["title_cue_context"] = " ".join(
        [
            str(topic.get("title", "")),
            *candidate["core_subtitle_evidence"],
        ]
    )
    candidate["body"] = fresh_manual_topic_evidence(
        candidate,
        srt_segments=srt_segments,
        peaks=peaks,
    )
    candidate["review_original_start"] = source_start
    candidate["review_original_end"] = source_end

    density_source = density_series if density_series is not None else peaks
    if not candidate.get("danmaku_content_evidence") and density_source:
        peak_candidates = candidate_evidence.topic_peak_candidates(topic, peaks)
        if peak_candidates:
            peak_start, density = max(peak_candidates, key=lambda item: item[1])
            features = danmaku_analysis._danmaku_peak_features(
                density_source,
                peak_start,
                density,
                avg_density=danmaku_analysis._average_danmaku_density(
                    density_source
                ),
            )
            candidate["danmaku_peak_start"] = int(peak_start)
            candidate["danmaku_selection_score"] = features["selection_score"]
            candidate["danmaku_local_surge_ratio"] = features["local_surge_ratio"]
            candidate["danmaku_density_percentile"] = features[
                "density_percentile"
            ]
            candidate["danmaku_content_quality"] = features["content_quality"]
            candidate["danmaku_interaction_signal"] = features[
                "interaction_signal"
            ]
            candidate["danmaku_content_evidence"] = features["content_evidence"]
    return candidate
