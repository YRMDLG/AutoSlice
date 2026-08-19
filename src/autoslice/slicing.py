"""多容器精确切片与 FFmpeg 命令构造的唯一实现。"""

from __future__ import annotations

import json
import os

from autoslice import reporting as reporting_service
from autoslice import media_probe
from autoslice import slice_encoding, slice_planning, slice_reuse
from autoslice.analysis import boundaries as boundary_analysis
from autoslice.analysis.review import deduplication as clip_deduplication
from autoslice.media_formats import (
    SUPPORTED_VIDEO_EXTENSIONS,
    is_analyzable_video,
    is_sliceable_video,
)
from autoslice.streamer_profiles import streamer_profile_context
from autoslice.transcription import segments as transcription_segments
from autoslice.transcription import srt_io as transcription_srt_io

FACADE_EXPORTS = {
    '_validated_video_path': 'validate_video_path',
    '_slice_from_marks_impl': 'slice_from_marks_impl',
    'slice_from_marks': 'slice_from_marks',
}


_dedupe_clip_marks = clip_deduplication._dedupe_clip_marks
_expand_clip_marks_with_context = boundary_analysis._expand_clip_marks_with_context
_srt_video_duration = transcription_segments.srt_video_duration
parse_srt_segments = transcription_srt_io.load_repaired_srt_segments
probe_video_duration = media_probe.probe_video_duration

_GENERATED_VIDEO_SUFFIX_PATTERN = slice_reuse._GENERATED_VIDEO_SUFFIX_PATTERN
_GENERATED_TOPIC_ARTIFACT_RE = slice_reuse._GENERATED_TOPIC_ARTIFACT_RE
_GENERATED_TOPIC_TEMP_RE = slice_reuse._GENERATED_TOPIC_TEMP_RE
SLICE_DURATION_TOLERANCE_SEC = slice_reuse.SLICE_DURATION_TOLERANCE_SEC
cleanup_stale_topic_clips = slice_reuse.cleanup_stale_topic_clips
is_reusable_topic_clip = slice_reuse.is_reusable_topic_clip
reuse_compatible_topic_clip = slice_reuse.reuse_compatible_topic_clip
reuse_topic_clip_after_title_change = slice_reuse.reuse_topic_clip_after_title_change

SLICE_DEFAULT_CONCURRENCY = slice_encoding.SLICE_DEFAULT_CONCURRENCY
SLICE_EXACT_SEEK_PREROLL_SEC = slice_encoding.SLICE_EXACT_SEEK_PREROLL_SEC
SLICE_MAX_CONCURRENCY = slice_encoding.SLICE_MAX_CONCURRENCY
SLICE_INDEX_MIN_CLIPS = slice_encoding.SLICE_INDEX_MIN_CLIPS
format_ffmpeg_seconds = slice_encoding.format_ffmpeg_seconds
preferred_slice_video_encoder_args = slice_encoding.preferred_slice_video_encoder_args
software_slice_video_encoder_args = slice_encoding.software_slice_video_encoder_args
configured_slice_concurrency = slice_encoding.configured_slice_concurrency
build_precise_slice_ffmpeg_command = slice_encoding.build_precise_slice_ffmpeg_command
prepare_seekable_slice_source = slice_encoding.prepare_seekable_slice_source


def validate_video_path(path, *, for_slicing=False):
    """校验公开工作流的视频输入，并保留兼容的 ``flv_path`` 参数名。"""

    normalized_path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(normalized_path):
        raise FileNotFoundError(f"录播文件不存在: {normalized_path}")
    supported = (
        is_sliceable_video(normalized_path)
        if for_slicing
        else is_analyzable_video(normalized_path)
    )
    if not supported:
        extensions = "/".join(SUPPORTED_VIDEO_EXTENSIONS)
        raise ValueError(f"不支持的视频格式；支持：{extensions}")
    return normalized_path


def slice_from_marks_impl(flv_path, json_path, output_dir, progress_callback=None):
    """
    【新功能】根据话题分析生成的 clip_marks.json 自动切片。
    完全独立于现有的弹幕切片和时间轴切片模式。

    返回: (切片数, 输出子目录)
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    marks = _dedupe_clip_marks(data.get("clip_marks", []))
    if not data.get("expanded_with_context"):
        srt_segments_for_context = parse_srt_segments(
            os.path.splitext(flv_path)[0] + ".srt"
        )
        marks = _expand_clip_marks_with_context(
            marks,
            srt_segments=srt_segments_for_context,
            video_duration=_srt_video_duration(srt_segments_for_context),
        )
    if not marks:
        if progress_callback:
            progress_callback("无切片标记", 0, 1)
        return 0, ""

    import subprocess as sp
    video_name = os.path.basename(flv_path)
    base_name = os.path.splitext(video_name)[0]
    report_dir = os.path.join(output_dir, base_name + "_话题切片")
    os.makedirs(report_dir, exist_ok=True)
    precise_video_end = None
    if any(mark.get("preserve_to_video_end") for mark in marks):
        precise_video_end = probe_video_duration(flv_path)
    subtitle_source_path = reporting_service.resolve_clip_subtitle_source(flv_path, data)
    subtitle_segments = (
        parse_srt_segments(subtitle_source_path)
        if subtitle_source_path
        else []
    )

    slice_jobs = slice_planning.build_slice_jobs(
        marks,
        flv_path,
        report_dir,
        precise_video_end=precise_video_end,
    )
    partition = slice_planning.partition_slice_jobs(
        slice_jobs,
        flv_path,
        report_dir,
    )
    reusable_jobs = partition.reusable_jobs
    pending_jobs = partition.pending_jobs
    title_renamed_count = partition.title_renamed_count

    removed_count = slice_reuse.cleanup_stale_topic_clips(
        report_dir,
        preserve_names=partition.reusable_output_names,
    )

    if progress_callback:
        if removed_count:
            progress_callback(
                f"已清理 {removed_count} 个旧字幕或失效自动产物",
                0,
                len(marks),
            )
        if reusable_jobs and pending_jobs:
            rename_note = (
                f"，其中 {title_renamed_count} 个仅更新标题"
                if title_renamed_count else ""
            )
            progress_callback(
                f"已复用 {len(reusable_jobs)} 个现有切片{rename_note}，"
                f"仅重切 {len(pending_jobs)} 个",
                0,
                len(marks),
            )
        elif reusable_jobs:
            rename_note = (
                f"，其中 {title_renamed_count} 个仅更新标题"
                if title_renamed_count else ""
            )
            progress_callback(
                f"已复用 {len(reusable_jobs)} 个现有切片{rename_note}，无需重新编码",
                0,
                len(marks),
            )
        else:
            progress_callback(f"开始切片 ({len(pending_jobs)} 段)...", 0, len(marks))

    count = len(reusable_jobs)
    if pending_jobs:
        source_span_sec = (
            _srt_video_duration(subtitle_segments)
            or max(job["end"] for job in slice_jobs)
        )
        encoding_result = slice_encoding.execute_slice_jobs(
            flv_path,
            report_dir,
            pending_jobs,
            total_mark_count=len(marks),
            source_span_sec=source_span_sec,
            subprocess_module=sp,
            probe_duration=probe_video_duration,
            progress_callback=progress_callback,
        )
        count += encoding_result.encoded_count

    reporting_service.update_refinement_manifest_after_slice(
        data.get("task_manifest_json_path"),
        report_dir,
        marks,
    )

    organized = reporting_service.organize_existing_artifacts(
        flv_path,
        output_dir=output_dir,
        json_path=json_path,
        report_path=data.get("analysis_report_path"),
        slice_dir=report_dir,
        artifact_dir=data.get("artifact_dir"),
    )

    if progress_callback:
        progress_callback(
            f"完成! {count} 个片段 → {report_dir}；概览 {organized['overview_path']}",
            len(marks),
            len(marks),
        )

    return count, report_dir


def slice_from_marks(
        flv_path, json_path, output_dir, progress_callback=None,
        streamer_profile_id="auto"):
    """按标记切片，并在当前线程显式激活调用方冻结的主播配置。"""
    flv_path = validate_video_path(flv_path, for_slicing=True)
    with streamer_profile_context(streamer_profile_id, flv_path):
        return slice_from_marks_impl(
            flv_path,
            json_path,
            output_dir,
            progress_callback=progress_callback,
        )
