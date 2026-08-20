"""完整流水线的人工时间轴选择阶段 owner。"""

from __future__ import annotations

import os


def prepare_pipeline_manual_timeline(
    flv_path,
    video_base,
    srt_segments,
    peaks,
    video_duration,
    manual_timeline_path,
    optimized_timeline_path,
    streamer_display_name,
    artifact_layout,
    progress_callback=None,
    *,
    copy_artifact_file,
    load_optimized_timeline_artifact,
    prepare_optimized_manual_timeline,
):
    """选择显式优化产物或现场优化结果，并提取流水线所需数据。"""
    if optimized_timeline_path:
        selected_optimized_path = os.path.abspath(optimized_timeline_path)
        copy_artifact_file(
            selected_optimized_path,
            artifact_layout["optimized_timeline_json_path"],
        )
        selected_optimized_md_path = (
            os.path.splitext(selected_optimized_path)[0] + ".md"
        )
        copy_artifact_file(
            selected_optimized_md_path,
            artifact_layout["optimized_timeline_md_path"],
        )
        manual_timeline = load_optimized_timeline_artifact(
            artifact_layout["optimized_timeline_json_path"],
            flv_path,
            manual_timeline_path=(
                manual_timeline_path
                if manual_timeline_path not in (None, "__none__")
                else None
            ),
        )
    else:
        manual_timeline = prepare_optimized_manual_timeline(
            flv_path,
            video_base,
            srt_segments,
            peaks,
            video_duration,
            manual_timeline_path,
            streamer_name=streamer_display_name,
            progress_callback=progress_callback,
            retry_incomplete_artifact=False,
            artifact_layout=artifact_layout,
        )

    raw_manual_entry_count = int(manual_timeline.get("raw_entry_count", 0))
    manual_entries = manual_timeline.get("entries") or []
    optimization_warning = manual_timeline.get("optimization_warning")
    if manual_entries and progress_callback:
        count_label = (
            f"原始 {raw_manual_entry_count} 条 → "
            f"字幕优化 {len(manual_entries)} 个候选"
        )
        progress_callback(
            f"已加载人工时间轴: {os.path.basename(manual_timeline['path'])}，"
            f"{count_label}",
            24,
            100,
        )

    return {
        "manual_timeline": manual_timeline,
        "raw_manual_entry_count": raw_manual_entry_count,
        "manual_entries": manual_entries,
        "optimization_warning": optimization_warning,
    }
