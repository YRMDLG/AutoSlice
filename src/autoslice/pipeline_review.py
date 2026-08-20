"""完整流水线候选聚合与复核阶段 owner。"""

from __future__ import annotations


def review_pipeline_candidates(
    accepted_topics,
    manual_entries,
    streamer_display_name,
    srt_segments,
    peaks,
    avg_den,
    clip_review_checkpoint_path,
    legacy_clip_review_checkpoint_path,
    api_precheck_warning=None,
    progress_callback=None,
    *,
    merge_manual_timeline_topics,
    validate_unmatched_manual_topics,
    clean_topics_for_report,
    analysis_topics_snapshot,
    seed_artifact_from_legacy,
    write_clip_review_checkpoint,
    apply_danmaku_slice_decisions,
    review_peak_selected_topics,
):
    """合并人工候选、执行候选复核并准备标题复核输入。

    本阶段只负责 LLM 结果之后、标题复核之前的候选流水线。所有领域
    实现和 artifact/checkpoint 操作都通过显式参数注入，使旧 ``pipeline``
    patch seam 可以继续由生产调用方绑定，同时避免 owner 反向依赖
    ``pipeline``。
    """
    merge_manual_timeline_topics(accepted_topics, manual_entries)
    manual_validation_warning = validate_unmatched_manual_topics(
        accepted_topics,
        streamer_name=streamer_display_name,
        progress_callback=progress_callback,
        srt_segments=srt_segments,
        peaks=peaks,
    )
    if manual_validation_warning:
        api_precheck_warning = "；".join(
            item
            for item in (api_precheck_warning, manual_validation_warning)
            if item
        )

    accepted_topics = clean_topics_for_report(accepted_topics)
    analysis_topics = analysis_topics_snapshot(accepted_topics)
    seed_artifact_from_legacy(
        clip_review_checkpoint_path,
        legacy_clip_review_checkpoint_path,
    )
    write_clip_review_checkpoint(
        clip_review_checkpoint_path,
        analysis_topics,
        stage="ready",
    )
    apply_danmaku_slice_decisions(
        accepted_topics,
        peaks,
        avg_den,
    )
    def write_review_checkpoint(
            current, pending, round_label, batch_index, total_batches):
        write_clip_review_checkpoint(
            clip_review_checkpoint_path,
            current,
            stage="reviewing",
            pending_count=len(pending),
            round=round_label,
            batch_index=batch_index,
            total_batches=total_batches,
        )

    clip_review_warning = review_peak_selected_topics(
        accepted_topics,
        srt_segments=srt_segments,
        peaks=peaks,
        streamer_name=streamer_display_name,
        progress_callback=progress_callback,
        checkpoint_callback=write_review_checkpoint,
    )
    if clip_review_warning:
        api_precheck_warning = "；".join(
            item for item in (api_precheck_warning, clip_review_warning) if item
        )

    # 复核可能改变标题、正文和候选状态；第二次清洗及弹幕决策必须在
    # 复核完成后继续执行，标题复核随后仍由 pipeline 负责。
    accepted_topics = clean_topics_for_report(accepted_topics)
    apply_danmaku_slice_decisions(
        accepted_topics,
        peaks,
        avg_den,
        require_clip_review=True,
    )

    return {
        "accepted_topics": accepted_topics,
        "analysis_topics": analysis_topics,
        "api_precheck_warning": api_precheck_warning,
        "clip_review_warning": clip_review_warning,
        "clip_review_checkpoint_path": clip_review_checkpoint_path,
    }
