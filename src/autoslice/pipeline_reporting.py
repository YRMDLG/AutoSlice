"""主流水线报告与业务 payload 的纯准备阶段 owner。"""

from __future__ import annotations

import os


def build_context_policy(
        *, pre_context_sec, post_context_sec, min_clip_sec, max_clip_sec,
        required_context_overflow_sec):
    """构建主流水线与 retry 共用的上下文策略快照。"""
    return {
        "pre_context_sec": pre_context_sec,
        "post_context_sec": post_context_sec,
        "min_clip_sec": min_clip_sec,
        "max_clip_sec": max_clip_sec,
        "required_context_overflow_sec": required_context_overflow_sec,
    }


def build_danmaku_selection_policy(
        *, average_density, density_threshold, local_peak_radius_sec,
        manual_review_min_stars, min_editorial_interest_score):
    """构建主流水线与 retry 共用的弹幕候选选择策略快照。"""
    return {
        "average_density": round(average_density, 3),
        "density_threshold": round(density_threshold, 3),
        "local_peak_radius_sec": local_peak_radius_sec,
        "max_clips_per_hour": None,
        "fixed_hourly_quota": False,
        "min_editorial_interest_score": min_editorial_interest_score,
        "manual_star_can_force_slice": False,
        "manual_star_review_min_stars": manual_review_min_stars,
        "semantic_review_can_keep_peak_moved_focus": True,
        "independent_subtitle_review_required": True,
    }


def prepare_pipeline_report(
        *, video_path, artifact_layout, source_srt_path, corrected_srt_path,
        topic_analysis_checkpoint_path, clip_review_checkpoint_path,
        candidate_review_audit_path, accepted_topics, analysis_topics,
        clip_marks, peak_info, failed_chunks, api_precheck_warning,
        clip_review_warning, manual_timeline, streamer_profile,
        average_density, density_threshold, local_peak_radius_sec,
        manual_review_min_stars, min_editorial_interest_score,
        context_policy, topic_analysis_model, review_model,
        clip_review_completed_at, artifact_layout_version,
        build_timeline_report, manual_timeline_summary):
    """构建主流水线报告和待持久化 payload，不执行任何文件写入。"""
    video_name = os.path.basename(video_path)
    streamer_name = streamer_profile.canonical_name
    streamer_display_name = streamer_profile.report_name
    unified_queue_md_path = artifact_layout["unified_queue_md_path"]

    report = build_timeline_report(
        video_name,
        peak_info,
        accepted_topics,
        failed_chunks=failed_chunks,
        api_warning=api_precheck_warning,
        streamer_name=streamer_display_name,
        group_by_hour=True,
        manual_timeline=manual_timeline,
        clip_marks=clip_marks,
        corrected_srt_path=corrected_srt_path,
        unified_queue_md_path=unified_queue_md_path,
        report_dir=artifact_layout["artifact_dir"],
        topic_analysis_model=topic_analysis_model,
        review_model=review_model,
    )
    report_path = artifact_layout["report_path"]
    clip_marks_path = artifact_layout["clip_marks_path"]
    task_manifest_json_path = artifact_layout["task_manifest_json_path"]
    task_manifest_md_path = artifact_layout["task_manifest_md_path"]
    unified_queue_json_path = artifact_layout["unified_queue_json_path"]
    payload = {
        "video": video_name,
        "streamer_profile_id": streamer_profile.id,
        "streamer_name": streamer_name,
        "streamer_display_name": streamer_display_name,
        "artifact_layout_version": artifact_layout_version,
        "artifact_dir": artifact_layout["artifact_dir"],
        "overview_path": artifact_layout["overview_path"],
        "analysis_report_path": report_path,
        "model_policy": {
            "topic_analysis": topic_analysis_model,
            "manual_timeline_review": review_model,
            "clip_candidate_review": review_model,
        },
        "source_srt_path": source_srt_path,
        "corrected_srt_path": corrected_srt_path,
        "task_manifest_json_path": task_manifest_json_path,
        "task_manifest_md_path": task_manifest_md_path,
        "unified_queue_json_path": unified_queue_json_path,
        "unified_queue_md_path": unified_queue_md_path,
        "clip_review_checkpoint_path": clip_review_checkpoint_path,
        "candidate_review_audit_path": candidate_review_audit_path,
        "topic_analysis_checkpoint_path": topic_analysis_checkpoint_path,
        "time_basis": "video_elapsed_seconds",
        "time_basis_note": (
            "start/end 均为视频内秒数，不是真实钟点；topic_start/topic_end "
            "为原话题范围，start/end 为含前后文的实际切片范围。"
        ),
        "expanded_with_context": True,
        "context_policy": context_policy,
        "danmaku_selection_policy": build_danmaku_selection_policy(
            average_density=average_density,
            density_threshold=density_threshold,
            local_peak_radius_sec=local_peak_radius_sec,
            manual_review_min_stars=manual_review_min_stars,
            min_editorial_interest_score=min_editorial_interest_score,
        ),
        "api_precheck_warning": api_precheck_warning,
        "clip_review_warning": clip_review_warning,
        "clip_review_completed_at": clip_review_completed_at,
        "failed_chunks": failed_chunks,
        "manual_timeline": manual_timeline_summary(manual_timeline),
        "analysis_topics": analysis_topics,
        "clip_marks": clip_marks,
    }
    return {
        "report": report,
        "payload": payload,
        "report_path": report_path,
        "clip_marks_path": clip_marks_path,
        "task_manifest_json_path": task_manifest_json_path,
        "task_manifest_md_path": task_manifest_md_path,
        "unified_queue_json_path": unified_queue_json_path,
        "unified_queue_md_path": unified_queue_md_path,
    }


__all__ = [
    "build_context_policy",
    "build_danmaku_selection_policy",
    "prepare_pipeline_report",
]
