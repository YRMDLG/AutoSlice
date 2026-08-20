"""完整流水线收播片与候选切片决策准备阶段 owner。"""

from __future__ import annotations


def prepare_pipeline_decisions(
    accepted_topics,
    srt_path,
    candidate_review_audit_path,
    streamer_profile,
    probed_video_duration,
    *,
    filter_topics,
    clip_marks_from_topics,
    build_clip_candidate_review_audit,
    write_artifact_json,
    parse_srt_segments,
    detect_stream_outro_clip,
    outro_topic_from_mark,
    analysis_topics_snapshot,
    srt_segments=None,
):
    """去除旧收播片并准备边界扩展所需的候选决策产物。

    收播检测、候选审计和 artifact 操作均通过显式参数注入，使旧
    ``pipeline`` patch seam 继续生效，同时避免本 owner 反向依赖
    ``pipeline`` 或各领域实现。
    """
    accepted_topics = list(filter_topics(
        lambda topic: topic.get("clip_type") != "stream_outro",
        accepted_topics,
    ))
    raw_clip_marks = clip_marks_from_topics(accepted_topics)
    candidate_review_audit = build_clip_candidate_review_audit(accepted_topics)
    write_artifact_json(candidate_review_audit_path, candidate_review_audit)
    srt_segments_for_context = (
        parse_srt_segments(srt_path)
        if srt_segments is None
        else srt_segments
    )
    outro_mark = detect_stream_outro_clip(
        srt_segments_for_context,
        probed_video_duration,
        streamer_profile=streamer_profile,
    )
    if outro_mark:
        accepted_topics.append(outro_topic_from_mark(outro_mark))
        raw_clip_marks.append(outro_mark)
    analysis_topics = analysis_topics_snapshot(accepted_topics)

    return {
        "accepted_topics": accepted_topics,
        "raw_clip_marks": raw_clip_marks,
        "candidate_review_audit": candidate_review_audit,
        "candidate_review_audit_path": candidate_review_audit_path,
        "srt_segments_for_context": srt_segments_for_context,
        "outro_mark": outro_mark,
        "analysis_topics": analysis_topics,
    }
