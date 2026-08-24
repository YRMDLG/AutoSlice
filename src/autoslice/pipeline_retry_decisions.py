"""artifact retry 的候选决策与边界准备阶段 owner。"""

from __future__ import annotations


def prepare_retry_decisions(
        flv_path, accepted_topics, srt_path, candidate_review_audit_path,
        streamer_profile, srt_segments, *, filter_topics,
        probe_video_duration, clip_marks_from_topics,
        build_clip_candidate_review_audit, write_artifact_json,
        parse_srt_segments, detect_stream_outro_clip, outro_topic_from_mark,
        analysis_topics_snapshot, prepare_pipeline_decisions,
        prepare_pipeline_boundaries, expand_clip_marks_with_context,
        synchronise_selected_topic_ranges, srt_video_duration):
    """准备 retry 最终切片所需的候选决策、收播片和边界结果。

    该阶段只负责连接已有的决策与边界 owner；候选筛选、收播检测、边界
    扩展和范围同步均通过显式依赖注入，避免 retry 路径重新实现主流程逻辑。
    """
    probed_video_duration = probe_video_duration(flv_path)
    decision_preparation = prepare_pipeline_decisions(
        accepted_topics,
        srt_path,
        candidate_review_audit_path,
        streamer_profile,
        probed_video_duration,
        filter_topics=filter_topics,
        clip_marks_from_topics=clip_marks_from_topics,
        build_clip_candidate_review_audit=build_clip_candidate_review_audit,
        write_artifact_json=write_artifact_json,
        parse_srt_segments=parse_srt_segments,
        detect_stream_outro_clip=detect_stream_outro_clip,
        outro_topic_from_mark=outro_topic_from_mark,
        analysis_topics_snapshot=analysis_topics_snapshot,
        srt_segments=srt_segments,
    )
    accepted_topics = decision_preparation["accepted_topics"]
    raw_clip_marks = decision_preparation["raw_clip_marks"]
    candidate_review_audit_path = decision_preparation[
        "candidate_review_audit_path"
    ]
    candidate_review_audit = decision_preparation.get("candidate_review_audit")
    video_duration = probed_video_duration or srt_video_duration(srt_segments)
    boundary_preparation = prepare_pipeline_boundaries(
        raw_clip_marks,
        accepted_topics,
        srt_segments,
        video_duration,
        expand_clip_marks_with_context=expand_clip_marks_with_context,
        synchronise_selected_topic_ranges=synchronise_selected_topic_ranges,
    )
    return {
        "accepted_topics": boundary_preparation["accepted_topics"],
        "clip_marks": boundary_preparation["clip_marks"],
        "candidate_review_audit": candidate_review_audit,
        "candidate_review_audit_path": candidate_review_audit_path,
        "probed_video_duration": probed_video_duration,
        "video_duration": video_duration,
    }


__all__ = ["prepare_retry_decisions"]
