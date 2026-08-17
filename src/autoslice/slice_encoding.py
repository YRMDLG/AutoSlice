"""精确切片编码、硬件回退与原子产物提交的唯一实现。"""

from __future__ import annotations

import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from autoslice.media_formats import normalise_video_extension
from autoslice.slice_reuse import SLICE_DURATION_TOLERANCE_SEC

FACADE_EXPORTS = {
    "SLICE_DEFAULT_CONCURRENCY": "SLICE_DEFAULT_CONCURRENCY",
    "SLICE_EXACT_SEEK_PREROLL_SEC": "SLICE_EXACT_SEEK_PREROLL_SEC",
    "SLICE_MAX_CONCURRENCY": "SLICE_MAX_CONCURRENCY",
    "SLICE_INDEX_MIN_CLIPS": "SLICE_INDEX_MIN_CLIPS",
    "_format_ffmpeg_seconds": "format_ffmpeg_seconds",
    "_preferred_slice_video_encoder_args": "preferred_slice_video_encoder_args",
    "_software_slice_video_encoder_args": "software_slice_video_encoder_args",
    "_configured_slice_concurrency": "configured_slice_concurrency",
    "_build_precise_slice_ffmpeg_command": "build_precise_slice_ffmpeg_command",
    "_prepare_seekable_slice_source": "prepare_seekable_slice_source",
}

SLICE_EXACT_SEEK_PREROLL_SEC = 10
SLICE_INDEX_MIN_CLIPS = 4
SLICE_DEFAULT_CONCURRENCY = 2
SLICE_MAX_CONCURRENCY = 2


@dataclass(frozen=True)
class SliceEncodingResult:
    """一次待编码任务批次的不可变执行结果。"""

    encoded_count: int
    final_encoder_args: tuple[str, ...]
    used_seek_index: bool
    parallel_workers: int


def format_ffmpeg_seconds(value):
    """生成稳定的 ffmpeg 秒数字符串，避免无意义的长浮点尾数。"""
    return f"{float(value):.3f}".rstrip("0").rstrip(".") or "0"


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
    return software_slice_video_encoder_args()


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
        source_path, report_dir, mark_count, subprocess_module,
        progress_callback=None, total_seek_sec=None, source_span_sec=None):
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
            or os.path.splitext(source_path)[1].lower() != ".flv"):
        return source_path, None
    try:
        source_size = os.path.getsize(source_path)
        if shutil.disk_usage(report_dir).free < source_size * 1.2:
            return source_path, None
    except OSError:
        return source_path, None

    temp_path = os.path.join(report_dir, f".autoslice_seek_index_{os.getpid()}.mkv")
    if os.path.exists(temp_path):
        os.remove(temp_path)
    if progress_callback:
        progress_callback("正在构建临时快速定位索引...", 0, mark_count)
    try:
        subprocess_module.run([
            "ffmpeg", "-y", "-i", source_path,
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", temp_path,
        ], check=True, stdout=subprocess_module.DEVNULL, stderr=subprocess_module.DEVNULL)
    except (OSError, subprocess_module.CalledProcessError):
        if os.path.exists(temp_path):
            os.remove(temp_path)
        if progress_callback:
            progress_callback("临时索引构建失败，改用源录播定位", 0, mark_count)
        return source_path, None
    return temp_path, temp_path


def _serialized_progress_callback(progress_callback):
    if progress_callback is None:
        return None
    lock = threading.Lock()

    def emit(message, current, total):
        with lock:
            progress_callback(message, current, total)

    return emit


def encode_slice_job(
        job, source_path, requested_encoder_args, total_mark_count,
        subprocess_module, probe_duration, progress_callback=None):
    """编码一个切片，校验时长后原子替换正式产物。"""
    index = job["index"]
    duration = job["duration"]
    output_path = job["output_path"]
    output_extension = normalise_video_extension(output_path)
    temporary_output_path = output_path + ".part" + output_extension
    effective_encoder_args = list(requested_encoder_args)

    if progress_callback:
        progress_callback(
            f"切片 {index}/{total_mark_count}: {job['title']}",
            index,
            total_mark_count,
        )

    if os.path.exists(temporary_output_path):
        os.remove(temporary_output_path)
    try:
        command = build_precise_slice_ffmpeg_command(
            source_path,
            temporary_output_path,
            job["start"],
            duration,
            effective_encoder_args,
        )
        try:
            subprocess_module.run(
                command,
                check=True,
                stdout=subprocess_module.DEVNULL,
                stderr=subprocess_module.PIPE,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess_module.CalledProcessError:
            if "h264_nvenc" not in effective_encoder_args:
                raise
            if os.path.exists(temporary_output_path):
                os.remove(temporary_output_path)
            effective_encoder_args = software_slice_video_encoder_args()
            if progress_callback:
                progress_callback(
                    "NVENC 不可用，已改用 CPU 精确编码",
                    index - 1,
                    total_mark_count,
                )
            command = build_precise_slice_ffmpeg_command(
                source_path,
                temporary_output_path,
                job["start"],
                duration,
                effective_encoder_args,
            )
            subprocess_module.run(
                command,
                check=True,
                stdout=subprocess_module.DEVNULL,
                stderr=subprocess_module.PIPE,
                encoding="utf-8",
                errors="replace",
            )

        actual_duration = probe_duration(temporary_output_path)
        if (
                actual_duration is None
                or abs(actual_duration - duration) > SLICE_DURATION_TOLERANCE_SEC):
            raise RuntimeError(
                f"切片 {index} 时长校验失败：计划 {duration:.3f}s，"
                f"实际 {actual_duration if actual_duration is not None else '无法读取'}"
            )
        os.replace(temporary_output_path, output_path)
        return tuple(effective_encoder_args)
    except Exception:
        if os.path.exists(temporary_output_path):
            os.remove(temporary_output_path)
        raise


def execute_slice_jobs(
        source_path, report_dir, pending_jobs, *, total_mark_count,
        source_span_sec, subprocess_module, probe_duration,
        progress_callback=None):
    """准备 seek 源并执行一批切片，必要时探针后启用两路 NVENC。"""
    if not pending_jobs:
        return SliceEncodingResult(0, (), False, 1)

    progress = _serialized_progress_callback(progress_callback)
    requested_encoder_args = preferred_slice_video_encoder_args()
    total_seek_sec = sum(
        max(0.0, job["start"] - SLICE_EXACT_SEEK_PREROLL_SEC)
        for job in pending_jobs
    )
    slice_source = source_path
    temporary_seek_source = None
    encoded_count = 0
    parallel_workers = 1

    try:
        slice_source, temporary_seek_source = prepare_seekable_slice_source(
            source_path,
            report_dir,
            len(pending_jobs),
            subprocess_module,
            progress_callback=progress,
            total_seek_sec=total_seek_sec,
            source_span_sec=source_span_sec,
        )
        remaining_jobs = list(pending_jobs)
        can_probe_parallel_nvenc = (
            len(remaining_jobs) >= SLICE_INDEX_MIN_CLIPS
            and temporary_seek_source is not None
            and "h264_nvenc" in requested_encoder_args
            and configured_slice_concurrency() > 1
        )
        if can_probe_parallel_nvenc:
            probe_job = remaining_jobs.pop(0)
            requested_encoder_args = list(encode_slice_job(
                probe_job,
                slice_source,
                requested_encoder_args,
                total_mark_count,
                subprocess_module,
                probe_duration,
                progress,
            ))
            encoded_count += 1

        can_parallel_encode = (
            can_probe_parallel_nvenc
            and "h264_nvenc" in requested_encoder_args
            and len(remaining_jobs) > 1
        )
        if can_parallel_encode:
            parallel_workers = min(
                configured_slice_concurrency(),
                len(remaining_jobs),
            )
            if progress:
                progress(
                    f"NVENC 探针通过，启用 {parallel_workers} 路并行切片",
                    encoded_count,
                    total_mark_count,
                )
            with ThreadPoolExecutor(
                    max_workers=parallel_workers,
                    thread_name_prefix="autoslice-encode") as executor:
                futures = [
                    executor.submit(
                        encode_slice_job,
                        job,
                        slice_source,
                        requested_encoder_args,
                        total_mark_count,
                        subprocess_module,
                        probe_duration,
                        progress,
                    )
                    for job in remaining_jobs
                ]
                for future in as_completed(futures):
                    future.result()
                    encoded_count += 1
        else:
            for job in remaining_jobs:
                requested_encoder_args = list(encode_slice_job(
                    job,
                    slice_source,
                    requested_encoder_args,
                    total_mark_count,
                    subprocess_module,
                    probe_duration,
                    progress,
                ))
                encoded_count += 1
    finally:
        if temporary_seek_source and os.path.exists(temporary_seek_source):
            os.remove(temporary_seek_source)

    return SliceEncodingResult(
        encoded_count=encoded_count,
        final_encoder_args=tuple(requested_encoder_args),
        used_seek_index=temporary_seek_source is not None,
        parallel_workers=parallel_workers,
    )
