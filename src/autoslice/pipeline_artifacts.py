"""流水线最终报告与共同产物的持久化编排。"""

from __future__ import annotations


def persist_pipeline_artifacts(
        *, video_path, report_path, report, json_path, payload,
        source_srt_path, corrected_srt_path, clip_marks,
        task_manifest_json_path, task_manifest_md_path,
        unified_queue_json_path, unified_queue_md_path,
        artifact_layout_version, artifact_layout,
        clip_review_checkpoint_path, accepted_topics, clip_review_warning,
        checkpoint_source, clip_review_completed_at,
        write_manifest_on_queue_warning=False, queue_warning_callback=None,
        write_artifact_text, write_artifact_json, build_refinement_manifest,
        write_refinement_manifest_files, upsert_unified_refinement_queue,
        write_completed_clip_review_checkpoint, organize_existing_artifacts):
    """按固定顺序持久化调用方已准备好的最终流水线产物。"""
    write_artifact_text(report_path, report)
    write_artifact_json(json_path, payload)

    refinement_manifest = build_refinement_manifest(
        video_path,
        source_srt_path,
        corrected_srt_path,
        report_path,
        json_path,
        clip_marks,
        task_manifest_json_path,
        task_manifest_md_path,
    )
    refinement_manifest["unified_queue_json_path"] = unified_queue_json_path
    refinement_manifest["unified_queue_md_path"] = unified_queue_md_path
    refinement_manifest["artifact_layout_version"] = artifact_layout_version
    refinement_manifest["artifact_dir"] = artifact_layout["artifact_dir"]
    refinement_manifest["overview_path"] = artifact_layout["overview_path"]
    write_refinement_manifest_files(refinement_manifest)

    unified_queue_warning = None
    try:
        upsert_unified_refinement_queue(
            refinement_manifest,
            queue_json_path=unified_queue_json_path,
            queue_md_path=unified_queue_md_path,
        )
    except (OSError, ValueError, TypeError) as exc:
        unified_queue_warning = f"精调总清单更新失败: {exc}"
        if write_manifest_on_queue_warning:
            refinement_manifest["unified_queue_warning"] = unified_queue_warning
            write_refinement_manifest_files(refinement_manifest)
        if queue_warning_callback:
            queue_warning_callback(unified_queue_warning, 99, 100)

    write_completed_clip_review_checkpoint(
        clip_review_checkpoint_path,
        accepted_topics,
        warning=clip_review_warning,
        source=checkpoint_source,
        completed_at=clip_review_completed_at,
    )
    organized = organize_existing_artifacts(
        video_path,
        output_dir=artifact_layout["output_root"],
        json_path=json_path,
        report_path=report_path,
        slice_dir=artifact_layout["slice_dir"],
        artifact_dir=artifact_layout["artifact_dir"],
    )
    return {
        "organized": organized,
        "unified_queue_warning": unified_queue_warning,
        "refinement_manifest": refinement_manifest,
    }
