"""artifact retry 的报告与最终 payload 准备阶段 owner。"""

from __future__ import annotations

import os

from autoslice.pipeline_reporting import build_danmaku_selection_policy


def prepare_retry_report(
        *, data, video_path, report_path, artifact_layout,
        source_srt_path, corrected_srt_path, clip_review_checkpoint_path,
        candidate_review_audit_path, accepted_topics, clip_marks, peak_info,
        failed_chunks, clip_review_warning, rebuilt_manual_timeline,
        streamer_profile, average_density, density_threshold,
        local_peak_radius_sec, manual_review_min_stars,
        min_editorial_interest_score, context_policy,
        clip_review_completed_at, artifact_layout_version,
        build_timeline_report, analysis_topics_snapshot,
        manual_timeline_summary, warning_without_previous_clip_review,
        review_model=None,
):
    """构建 retry 报告并更新共同产物 payload，不执行任何文件写入。"""
    base_warning = warning_without_previous_clip_review(data)
    api_warning = "；".join(
        item for item in (base_warning, clip_review_warning) if item
    ) or None

    unified_queue_json_path = data.get("unified_queue_json_path")
    unified_queue_md_path = data.get("unified_queue_md_path")
    if not unified_queue_json_path or not unified_queue_md_path:
        unified_queue_json_path = artifact_layout["unified_queue_json_path"]
        unified_queue_md_path = artifact_layout["unified_queue_md_path"]

    video_name = os.path.basename(video_path)
    streamer_name = streamer_profile.canonical_name
    streamer_display_name = streamer_profile.report_name
    report = build_timeline_report(
        video_name,
        peak_info,
        accepted_topics,
        failed_chunks=failed_chunks,
        api_warning=api_warning,
        streamer_name=streamer_display_name,
        group_by_hour=True,
        manual_timeline=rebuilt_manual_timeline,
        clip_marks=clip_marks,
        corrected_srt_path=corrected_srt_path,
        unified_queue_md_path=unified_queue_md_path,
        report_dir=artifact_layout["artifact_dir"],
        review_model=review_model,
    )
    analysis_topics = analysis_topics_snapshot(accepted_topics)

    data.update({
        "video": video_name,
        "streamer_profile_id": streamer_profile.id,
        "streamer_name": streamer_name,
        "streamer_display_name": streamer_display_name,
        "artifact_layout_version": artifact_layout_version,
        "artifact_dir": artifact_layout["artifact_dir"],
        "overview_path": artifact_layout["overview_path"],
        "analysis_report_path": report_path,
        "source_srt_path": source_srt_path,
        "corrected_srt_path": corrected_srt_path,
        "unified_queue_json_path": unified_queue_json_path,
        "unified_queue_md_path": unified_queue_md_path,
        "clip_review_checkpoint_path": clip_review_checkpoint_path,
        "candidate_review_audit_path": candidate_review_audit_path,
        "expanded_with_context": True,
        "context_policy": context_policy,
        "danmaku_selection_policy": build_danmaku_selection_policy(
            average_density=average_density,
            density_threshold=density_threshold,
            local_peak_radius_sec=local_peak_radius_sec,
            manual_review_min_stars=manual_review_min_stars,
            min_editorial_interest_score=min_editorial_interest_score,
        ),
        "api_precheck_warning": api_warning,
        "clip_review_warning": clip_review_warning,
        "manual_timeline": manual_timeline_summary(rebuilt_manual_timeline),
        "analysis_topics": analysis_topics,
        "clip_marks": clip_marks,
        "clip_review_completed_at": clip_review_completed_at,
    })
    if review_model:
        model_policy = dict(data.get("model_policy") or {})
        model_policy.update({
            "manual_timeline_review": review_model,
            "clip_candidate_review": review_model,
        })
        data["model_policy"] = model_policy

    task_manifest_json_path = data.get("task_manifest_json_path")
    task_manifest_md_path = data.get("task_manifest_md_path")
    if not task_manifest_json_path or not task_manifest_md_path:
        task_manifest_json_path = artifact_layout["task_manifest_json_path"]
        task_manifest_md_path = artifact_layout["task_manifest_md_path"]

    return {
        "report": report,
        "analysis_topics": analysis_topics,
        "api_warning": api_warning,
        "unified_queue_json_path": unified_queue_json_path,
        "unified_queue_md_path": unified_queue_md_path,
        "task_manifest_json_path": task_manifest_json_path,
        "task_manifest_md_path": task_manifest_md_path,
        "payload": data,
    }


__all__ = ["prepare_retry_report"]
