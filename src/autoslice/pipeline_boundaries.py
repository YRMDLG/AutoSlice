"""流水线边界扩展与选中话题范围同步阶段 owner。"""

from __future__ import annotations


def prepare_pipeline_boundaries(
    raw_clip_marks,
    accepted_topics,
    srt_segments_for_context,
    video_duration,
    *,
    expand_clip_marks_with_context,
    synchronise_selected_topic_ranges,
):
    """扩展切片边界并把最终范围同步回选中话题。"""
    clip_marks = expand_clip_marks_with_context(
        raw_clip_marks,
        srt_segments=srt_segments_for_context,
        video_duration=video_duration,
    )
    synchronise_selected_topic_ranges(accepted_topics, clip_marks)
    return {
        "clip_marks": clip_marks,
        "accepted_topics": accepted_topics,
    }
