"""从既有产物重做候选与投稿标题复核的流水线阶段 owner。"""

from __future__ import annotations


def review_retry_candidates_and_titles(
        accepted_topics, analysis_topics, *, srt_segments, peaks, avg_den,
        streamer_name, clip_review_checkpoint_path, resume_review,
        reuse_completed_review, stale_review_keys, progress_callback=None,
        clean_topics_for_report, apply_danmaku_slice_decisions,
        append_clip_candidate_source, review_peak_selected_topics,
        review_selected_publish_titles, write_clip_review_checkpoint):
    """执行 retry 的字幕候选复核、切片重判定和投稿标题复核。

    retry 与首次分析共用领域算法，但检查点阶段名、复核续跑状态和
    ``artifact_retry`` 来源不同，因此这里保留独立的阶段编排。所有
    领域实现与持久化操作均通过参数注入，避免 owner 反向依赖
    ``pipeline`` 或 ``topic_engine``，同时保留旧调用方的替换 seam。
    """
    write_clip_review_checkpoint(
        clip_review_checkpoint_path,
        accepted_topics if (resume_review or reuse_completed_review)
        else analysis_topics,
        stage=(
            "resuming" if resume_review
            else "rebuilding" if reuse_completed_review
            else "ready"
        ),
        source="artifact_retry",
    )

    if not resume_review and not reuse_completed_review:
        apply_danmaku_slice_decisions(
            accepted_topics,
            peaks,
            avg_den,
        )
        for topic in accepted_topics:
            key = (
                int(topic.get("start", 0) or 0),
                int(topic.get("end", 0) or 0),
                str(topic.get("title", "")),
            )
            if key in stale_review_keys:
                append_clip_candidate_source(topic, "语义复核")

    if reuse_completed_review:
        clip_review_warning = None
    else:
        clip_review_warning = review_peak_selected_topics(
            accepted_topics,
            srt_segments=srt_segments,
            peaks=peaks,
            streamer_name=streamer_name,
            progress_callback=progress_callback,
            checkpoint_callback=lambda current, pending, round_label, batch_index, total_batches: (
                write_clip_review_checkpoint(
                    clip_review_checkpoint_path,
                    current,
                    stage="reviewing",
                    source="artifact_retry",
                    pending_count=len(pending),
                    round=round_label,
                    batch_index=batch_index,
                    total_batches=total_batches,
                )
            ),
            resume=resume_review,
        )

    accepted_topics = clean_topics_for_report(accepted_topics)
    apply_danmaku_slice_decisions(
        accepted_topics,
        peaks,
        avg_den,
        require_clip_review=True,
    )

    title_review_warning = review_selected_publish_titles(
        accepted_topics,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
        checkpoint_callback=lambda current, batch_index, total_batches: (
            write_clip_review_checkpoint(
                clip_review_checkpoint_path,
                current,
                stage="title_reviewing",
                source="artifact_retry",
                batch_index=batch_index,
                total_batches=total_batches,
            )
        ),
    )
    if title_review_warning:
        clip_review_warning = "；".join(
            item for item in (clip_review_warning, title_review_warning) if item
        )

    accepted_topics = clean_topics_for_report(accepted_topics)
    return {
        "accepted_topics": accepted_topics,
        "clip_review_warning": clip_review_warning,
        "title_review_warning": title_review_warning,
    }
