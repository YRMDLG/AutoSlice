"""多容器精确切片、既有产物复用与 FFmpeg 命令构造的唯一实现。"""

from __future__ import annotations

import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

from autoslice import reporting as reporting_service
from autoslice.analysis import candidates as candidate_analysis
from autoslice.transcription import service as transcription_service
from media_formats import (
    SUPPORTED_VIDEO_EXTENSIONS,
    compatible_output_extensions,
    is_analyzable_video,
    is_sliceable_video,
    normalise_video_extension,
)
from streamer_profiles import streamer_profile_context

FACADE_EXPORTS = {
    '_GENERATED_VIDEO_SUFFIX_PATTERN': '_GENERATED_VIDEO_SUFFIX_PATTERN',
    'SLICE_DEFAULT_CONCURRENCY': 'SLICE_DEFAULT_CONCURRENCY',
    '_GENERATED_TOPIC_TEMP_RE': '_GENERATED_TOPIC_TEMP_RE',
    'SLICE_DURATION_TOLERANCE_SEC': 'SLICE_DURATION_TOLERANCE_SEC',
    'SLICE_EXACT_SEEK_PREROLL_SEC': 'SLICE_EXACT_SEEK_PREROLL_SEC',
    '_GENERATED_TOPIC_ARTIFACT_RE': '_GENERATED_TOPIC_ARTIFACT_RE',
    'SLICE_MAX_CONCURRENCY': 'SLICE_MAX_CONCURRENCY',
    'SLICE_INDEX_MIN_CLIPS': 'SLICE_INDEX_MIN_CLIPS',
    '_validated_video_path': 'validate_video_path',
    '_cleanup_stale_topic_clips': 'cleanup_stale_topic_clips',
    '_format_ffmpeg_seconds': 'format_ffmpeg_seconds',
    '_is_reusable_topic_clip': 'is_reusable_topic_clip',
    '_reuse_compatible_topic_clip': 'reuse_compatible_topic_clip',
    '_reuse_topic_clip_after_title_change': 'reuse_topic_clip_after_title_change',
    '_preferred_slice_video_encoder_args': 'preferred_slice_video_encoder_args',
    '_software_slice_video_encoder_args': 'software_slice_video_encoder_args',
    '_configured_slice_concurrency': 'configured_slice_concurrency',
    '_build_precise_slice_ffmpeg_command': 'build_precise_slice_ffmpeg_command',
    '_prepare_seekable_slice_source': 'prepare_seekable_slice_source',
    '_slice_from_marks_impl': 'slice_from_marks_impl',
    'slice_from_marks': 'slice_from_marks',
}


_dedupe_clip_marks = candidate_analysis._dedupe_clip_marks
_expand_clip_marks_with_context = candidate_analysis._expand_clip_marks_with_context
_serialized_progress_callback = candidate_analysis._serialized_progress_callback
_srt_video_duration = candidate_analysis._srt_video_duration
parse_srt_segments = candidate_analysis.parse_srt_segments
probe_video_duration = transcription_service.probe_video_duration



SLICE_EXACT_SEEK_PREROLL_SEC = 10


SLICE_DURATION_TOLERANCE_SEC = 0.5


SLICE_INDEX_MIN_CLIPS = 4


SLICE_DEFAULT_CONCURRENCY = 2


SLICE_MAX_CONCURRENCY = 2


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


_GENERATED_VIDEO_SUFFIX_PATTERN = "|".join(
    re.escape(extension.removeprefix("."))
    for extension in SUPPORTED_VIDEO_EXTENSIONS
)


_GENERATED_TOPIC_ARTIFACT_RE = re.compile(
    rf'^\d{{2,3}}_\d+s_.+\.(?:{_GENERATED_VIDEO_SUFFIX_PATTERN}|srt)$',
    re.IGNORECASE,
)


_GENERATED_TOPIC_TEMP_RE = re.compile(
    rf'^(?:\d{{2,3}}_\d+s_.+\.(?:{_GENERATED_VIDEO_SUFFIX_PATTERN})'
    rf'\.part\.(?:{_GENERATED_VIDEO_SUFFIX_PATTERN})|'
    r'\.autoslice_seek_index_\d+\.mkv)$',
    re.IGNORECASE,
)


def cleanup_stale_topic_clips(report_dir, preserve_names=None):
    """清理失效自动产物；可保留已通过校验的现有切片视频。"""
    if not os.path.isdir(report_dir):
        return 0
    preserved = {
        str(name).casefold()
        for name in (preserve_names or [])
        if str(name).strip()
    }
    removed = 0
    for name in os.listdir(report_dir):
        if not (
                _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name)
                or _GENERATED_TOPIC_TEMP_RE.fullmatch(name)):
            continue
        if (
                _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name)
                and name.casefold() in preserved):
            continue
        path = os.path.join(report_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        removed += 1
    return removed


def format_ffmpeg_seconds(value):
    """生成稳定的 ffmpeg 秒数字符串，避免无意义的长浮点尾数。"""
    return f"{float(value):.3f}".rstrip("0").rstrip(".") or "0"


def is_reusable_topic_clip(output_path, source_path, expected_duration):
    """校验已有切片是否仍对应当前源录播和当前计划时长。"""
    force_rebuild = os.environ.get("AUTOSLICE_FORCE_RESLICE", "").strip().lower()
    if force_rebuild in {"1", "true", "yes", "on"}:
        return False
    try:
        output_stat = os.stat(output_path)
        source_stat = os.stat(source_path)
    except OSError:
        return False
    if output_stat.st_size <= 0 or output_stat.st_mtime_ns < source_stat.st_mtime_ns:
        return False
    actual_duration = probe_video_duration(output_path)
    return (
        actual_duration is not None
        and abs(float(actual_duration) - float(expected_duration))
        <= SLICE_DURATION_TOLERANCE_SEC
    )


def reuse_compatible_topic_clip(job, source_path):
    """首选产物不存在时，原路径复用兼容的历史容器产物。"""

    preferred_path = os.path.abspath(job["output_path"])
    output_stem = os.path.splitext(preferred_path)[0]
    for extension in compatible_output_extensions(source_path):
        candidate_path = output_stem + extension
        if os.path.normcase(candidate_path) == os.path.normcase(preferred_path):
            continue
        if not is_reusable_topic_clip(
                candidate_path, source_path, job["duration"]):
            continue
        job["output_path"] = candidate_path
        job["output_name"] = os.path.basename(candidate_path)
        return True
    return False


def reuse_topic_clip_after_title_change(job, report_dir, source_path):
    """起点和时长未变时复用旧视频，允许标题或候选编号发生变化。"""
    expected_name = str(job["output_name"])
    start_marker = f'_{int(job["start"])}s_'.casefold()
    try:
        names = os.listdir(report_dir)
    except OSError:
        return False
    candidates = []
    compatible_extensions = set(compatible_output_extensions(source_path))
    for name in names:
        if name.casefold() == expected_name.casefold():
            continue
        if not _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name):
            continue
        if (
                start_marker not in name.casefold()
                or normalise_video_extension(name) not in compatible_extensions):
            continue
        path = os.path.join(report_dir, name)
        try:
            modified_ns = os.stat(path).st_mtime_ns
        except OSError:
            continue
        candidates.append((modified_ns, path))
    for _modified_ns, candidate_path in sorted(candidates, reverse=True):
        if not is_reusable_topic_clip(
                candidate_path, source_path, job["duration"]):
            continue
        target_extension = normalise_video_extension(candidate_path)
        target_path = os.path.splitext(job["output_path"])[0] + target_extension
        try:
            os.replace(candidate_path, target_path)
        except OSError:
            try:
                shutil.copy2(candidate_path, target_path)
            except OSError:
                try:
                    os.remove(target_path)
                except OSError:
                    pass
                continue
        job["output_path"] = target_path
        job["output_name"] = os.path.basename(target_path)
        return True
    return False


def preferred_slice_video_encoder_args():
    """优先使用本机 NVENC；无 NVIDIA 环境时回退到高质量软件编码。"""
    requested = os.environ.get("AUTOSLICE_VIDEO_ENCODER", "auto").strip().lower()
    use_nvenc = requested in {"nvenc", "h264_nvenc"}
    if requested == "auto":
        use_nvenc = shutil.which("nvidia-smi") is not None
    if use_nvenc:
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-profile:v", "high",
            "-rc:v", "vbr", "-cq:v", "23", "-b:v", "0",
        ]
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-crf", "19",
    ]


def software_slice_video_encoder_args():
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-crf", "19",
    ]


def configured_slice_concurrency():
    """首次索引切片最多使用 2 路 NVENC，环境变量可主动降为 1。"""
    raw_value = os.environ.get(
        "AUTOSLICE_SLICE_CONCURRENCY",
        str(SLICE_DEFAULT_CONCURRENCY),
    )
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = SLICE_DEFAULT_CONCURRENCY
    return max(1, min(SLICE_MAX_CONCURRENCY, value))


def build_precise_slice_ffmpeg_command(
        input_path, output_path, start_s, duration, video_encoder_args):
    """双重 seek 丢弃关键帧前置内容，再编码视频得到精确首尾。"""
    coarse_start = max(0.0, float(start_s) - SLICE_EXACT_SEEK_PREROLL_SEC)
    precise_offset = max(0.0, float(start_s) - coarse_start)
    command = [
        "ffmpeg", "-y",
        "-ss", format_ffmpeg_seconds(coarse_start),
        "-i", input_path,
    ]
    if precise_offset > 0:
        command.extend(["-ss", format_ffmpeg_seconds(precise_offset)])
    command.extend([
        "-t", format_ffmpeg_seconds(duration),
        "-map", "0:v:0", "-map", "0:a:0?",
        *video_encoder_args,
        "-c:a", "copy",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ])
    return command


def prepare_seekable_slice_source(
        flv_path, report_dir, mark_count, subprocess_module, progress_callback=None,
        total_seek_sec=None, source_span_sec=None):
    """多片段 FLV 先临时重封装为带索引的 MKV，避免每段线性扫描整场。"""
    seek_cost_requires_index = (
        mark_count >= 2
        and total_seek_sec is not None
        and source_span_sec is not None
        and float(source_span_sec) > 0
        and float(total_seek_sec) >= float(source_span_sec)
    )
    if (
            (mark_count < SLICE_INDEX_MIN_CLIPS and not seek_cost_requires_index)
            or os.path.splitext(flv_path)[1].lower() != ".flv"):
        return flv_path, None
    try:
        source_size = os.path.getsize(flv_path)
        if shutil.disk_usage(report_dir).free < source_size * 1.2:
            return flv_path, None
    except OSError:
        return flv_path, None

    temp_path = os.path.join(report_dir, f".autoslice_seek_index_{os.getpid()}.mkv")
    if os.path.exists(temp_path):
        os.remove(temp_path)
    if progress_callback:
        progress_callback("正在构建临时快速定位索引...", 0, mark_count)
    try:
        subprocess_module.run([
            "ffmpeg", "-y", "-i", flv_path,
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", temp_path,
        ], check=True, stdout=subprocess_module.DEVNULL, stderr=subprocess_module.DEVNULL)
    except (OSError, subprocess_module.CalledProcessError):
        if os.path.exists(temp_path):
            os.remove(temp_path)
        if progress_callback:
            progress_callback("临时索引构建失败，改用源录播定位", 0, mark_count)
        return flv_path, None
    return temp_path, temp_path


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

    slice_jobs = []
    for index, mark in enumerate(marks, 1):
        start_s = float(mark["start"])
        end_s = float(mark["end"])
        if mark.get("preserve_to_video_end") and precise_video_end:
            # JSON/报告使用向上取整的整秒作为可读边界；真正编码时必须以
            # ffprobe 的浮点时长收尾，否则计划时长会比源视频多近一秒，
            # 既可能触发时长校验失败，也无法准确表达“保留到关播”。
            end_s = min(end_s, float(precise_video_end))
        duration = end_s - start_s
        if duration <= 0:
            continue
        output_name = reporting_service.topic_clip_filename(index, mark, flv_path)
        slice_jobs.append({
            "index": index,
            "mark": mark,
            "start": start_s,
            "end": end_s,
            "duration": duration,
            "title": mark.get("title", f"片段{index}"),
            "output_name": output_name,
            "output_path": os.path.join(report_dir, output_name),
        })

    reusable_jobs = []
    pending_jobs = []
    title_renamed_count = 0
    for job in slice_jobs:
        if is_reusable_topic_clip(
                job["output_path"], flv_path, job["duration"]):
            reusable_jobs.append(job)
        elif reuse_compatible_topic_clip(job, flv_path):
            reusable_jobs.append(job)
        elif reuse_topic_clip_after_title_change(job, report_dir, flv_path):
            reusable_jobs.append(job)
            title_renamed_count += 1
        else:
            pending_jobs.append(job)

    removed_count = cleanup_stale_topic_clips(
        report_dir,
        preserve_names=[job["output_name"] for job in reusable_jobs],
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
    slice_source = flv_path
    temporary_seek_source = None
    video_encoder_args = (
        preferred_slice_video_encoder_args()
        if pending_jobs
        else None
    )
    slice_progress = _serialized_progress_callback(progress_callback)

    def encode_slice_job(job, requested_encoder_args):
        index = job["index"]
        start_s = job["start"]
        duration = job["duration"]
        title = job["title"]
        output_path = job["output_path"]
        output_extension = normalise_video_extension(output_path)
        temporary_output_path = output_path + ".part" + output_extension
        effective_encoder_args = list(requested_encoder_args)

        if slice_progress:
            slice_progress(
                f"切片 {index}/{len(marks)}: {title}",
                index,
                len(marks),
            )

        if os.path.exists(temporary_output_path):
            os.remove(temporary_output_path)
        try:
            command = build_precise_slice_ffmpeg_command(
                slice_source,
                temporary_output_path,
                start_s,
                duration,
                effective_encoder_args,
            )
            try:
                sp.run(
                    command,
                    check=True,
                    stdout=sp.DEVNULL,
                    stderr=sp.PIPE,
                    encoding="utf-8",
                    errors="replace",
                )
            except sp.CalledProcessError:
                if "h264_nvenc" not in effective_encoder_args:
                    raise
                if os.path.exists(temporary_output_path):
                    os.remove(temporary_output_path)
                effective_encoder_args = software_slice_video_encoder_args()
                if slice_progress:
                    slice_progress(
                        "NVENC 不可用，已改用 CPU 精确编码",
                        index - 1,
                        len(marks),
                    )
                command = build_precise_slice_ffmpeg_command(
                    slice_source,
                    temporary_output_path,
                    start_s,
                    duration,
                    effective_encoder_args,
                )
                sp.run(
                    command,
                    check=True,
                    stdout=sp.DEVNULL,
                    stderr=sp.PIPE,
                    encoding="utf-8",
                    errors="replace",
                )

            actual_duration = probe_video_duration(temporary_output_path)
            if (
                    actual_duration is None
                    or abs(actual_duration - duration) > SLICE_DURATION_TOLERANCE_SEC):
                raise RuntimeError(
                    f"切片 {index} 时长校验失败：计划 {duration:.3f}s，"
                    f"实际 {actual_duration if actual_duration is not None else '无法读取'}"
                )
            os.replace(temporary_output_path, output_path)
            return effective_encoder_args
        except Exception:
            if os.path.exists(temporary_output_path):
                os.remove(temporary_output_path)
            raise

    try:
        if pending_jobs:
            total_seek_sec = sum(
                max(0.0, job["start"] - SLICE_EXACT_SEEK_PREROLL_SEC)
                for job in pending_jobs
            )
            source_span_sec = (
                _srt_video_duration(subtitle_segments)
                or max(job["end"] for job in slice_jobs)
            )
            slice_source, temporary_seek_source = prepare_seekable_slice_source(
                flv_path,
                report_dir,
                len(pending_jobs),
                sp,
                progress_callback=progress_callback,
                total_seek_sec=total_seek_sec,
                source_span_sec=source_span_sec,
            )
        remaining_jobs = list(pending_jobs)
        can_probe_parallel_nvenc = (
            len(remaining_jobs) >= SLICE_INDEX_MIN_CLIPS
            and temporary_seek_source is not None
            and "h264_nvenc" in (video_encoder_args or [])
            and configured_slice_concurrency() > 1
        )
        if can_probe_parallel_nvenc:
            probe_job = remaining_jobs.pop(0)
            video_encoder_args = encode_slice_job(probe_job, video_encoder_args)
            count += 1

        can_parallel_encode = (
            can_probe_parallel_nvenc
            and "h264_nvenc" in (video_encoder_args or [])
            and len(remaining_jobs) > 1
        )
        if can_parallel_encode:
            workers = min(configured_slice_concurrency(), len(remaining_jobs))
            if slice_progress:
                slice_progress(
                    f"NVENC 探针通过，启用 {workers} 路并行切片",
                    count,
                    len(marks),
                )
            with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="autoslice-encode") as executor:
                futures = [
                    executor.submit(encode_slice_job, job, video_encoder_args)
                    for job in remaining_jobs
                ]
                for future in as_completed(futures):
                    future.result()
                    count += 1
        else:
            for job in remaining_jobs:
                video_encoder_args = encode_slice_job(job, video_encoder_args)
                count += 1
    finally:
        if temporary_seek_source and os.path.exists(temporary_seek_source):
            os.remove(temporary_seek_source)

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
