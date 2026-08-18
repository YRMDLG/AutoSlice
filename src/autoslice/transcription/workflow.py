"""可恢复 FunASR 转录到正式 SRT 的唯一工作流实现。"""

from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any

from autoslice import media_probe
from autoslice.streamer_profiles import infer_streamer_name
from autoslice.transcription import (
    checkpoints as checkpoint_store,
)
from autoslice.transcription import (
    model_runtime,
    recognition,
    srt_io,
)
from autoslice.transcription import (
    results as result_contracts,
)
from autoslice.transcription import (
    segments as subtitle_segments,
)

FACADE_EXPORTS = {
    "ensure_srt": "ensure_srt",
}

# 可替换的公开依赖 seam；旧 service 和高层模块仍绑定同一个默认对象。
probe_video_duration = media_probe.probe_video_duration


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _bounded_core_segments(
    chunk_segments: list[tuple[float, float, str]],
    *,
    core_start: float,
    core_end: float,
    duration: float,
    require_midpoint_in_core: bool,
) -> list[tuple[float, float, str]]:
    bounded = []
    for segment_start, segment_end, text in chunk_segments:
        midpoint = (segment_start + segment_end) / 2.0
        if (
            require_midpoint_in_core
            and (midpoint < core_start or midpoint >= core_end)
        ):
            continue
        final_start = max(0.0, core_start, segment_start)
        final_end = min(duration, core_end, segment_end)
        if final_end > final_start:
            bounded.append((final_start, final_end, text))
    return bounded


def _primary_speaker_chunk_segments(
    primary_segments: list[dict[str, Any]],
    *,
    input_start: float,
    core_start: float,
    core_end: float,
    duration: float,
    streamer_name: str,
) -> list[tuple[float, float, str]]:
    rendered = []
    for speaker_segment in primary_segments:
        text_value = str(speaker_segment.get("text", "")).strip()
        timestamps = speaker_segment.get("timestamp") or []
        if timestamps:
            chunk_segments = subtitle_segments.segments_from_funasr_result(
                text_value,
                timestamps,
                offset=input_start,
                streamer_name=streamer_name,
            )
        else:
            try:
                sentence_start = input_start + float(
                    speaker_segment.get("start")
                ) / 1000.0
                sentence_end = input_start + float(
                    speaker_segment.get("end")
                ) / 1000.0
            except (TypeError, ValueError):
                continue
            chunk_segments = subtitle_segments.split_timed_subtitle_segment(
                sentence_start,
                max(sentence_end, sentence_start + 0.1),
                subtitle_segments.normalise_asr_text(
                    text_value,
                    streamer_name=streamer_name,
                ),
            )
        rendered.extend(
            _bounded_core_segments(
                chunk_segments,
                core_start=core_start,
                core_end=core_end,
                duration=duration,
                require_midpoint_in_core=True,
            )
        )
    return rendered


def _regular_chunk_segments(
    result_items: list[dict[str, Any]],
    *,
    input_start: float,
    core_start: float,
    core_end: float,
    duration: float,
    streamer_name: str,
) -> list[tuple[float, float, str]]:
    rendered = []
    for item in result_items:
        text_value = str(item.get("text", "")).strip()
        timestamps = item.get("timestamp", [])
        if not text_value or not timestamps:
            continue
        core_text, core_timestamps, token_aligned = (
            subtitle_segments.trim_funasr_tokens_to_core(
                text_value,
                timestamps,
                input_start,
                core_start,
                core_end,
                raw_text=item.get("raw_text"),
            )
        )
        if not core_text or not core_timestamps:
            continue
        chunk_segments = subtitle_segments.segments_from_funasr_result(
            core_text,
            core_timestamps,
            offset=input_start,
            streamer_name=streamer_name,
        )
        rendered.extend(
            _bounded_core_segments(
                chunk_segments,
                core_start=core_start,
                core_end=core_end,
                duration=duration,
                require_midpoint_in_core=not token_aligned,
            )
        )
    return rendered


def _collect_checkpoint_segments(
    checkpoint: dict[str, Any],
    *,
    chunk_count: int,
    duration: float,
    streamer_name: str,
    foreground_only: bool,
) -> tuple[list[tuple[float, float, str]], int, int]:
    all_segments = []
    speaker_filtered_count = 0
    speaker_filtered_chunks = 0
    chunks = checkpoint.get("chunks") or {}
    for index in range(chunk_count):
        entry = chunks.get(str(index))
        if not entry:
            raise RuntimeError(
                f"FunASR 第 {index + 1}/{chunk_count} 块缺失，未生成残缺 SRT。"
            )
        core_start, chunk_duration, input_start, _ = (
            checkpoint_store.funasr_chunk_input_window(index, duration)
        )
        core_end = core_start + chunk_duration
        result_items = entry.get("result") or []
        primary_segments, removed_count, _ = (
            result_contracts.primary_speaker_segments(result_items)
            if foreground_only
            else (None, 0, None)
        )
        if primary_segments:
            speaker_filtered_count += removed_count
            speaker_filtered_chunks += 1
            all_segments.extend(
                _primary_speaker_chunk_segments(
                    primary_segments,
                    input_start=input_start,
                    core_start=core_start,
                    core_end=core_end,
                    duration=duration,
                    streamer_name=streamer_name,
                )
            )
            continue
        all_segments.extend(
            _regular_chunk_segments(
                result_items,
                input_start=input_start,
                core_start=core_start,
                core_end=core_end,
                duration=duration,
                streamer_name=streamer_name,
            )
        )
    return all_segments, speaker_filtered_count, speaker_filtered_chunks


def _mark_completed_empty(
    checkpoint_path: str,
    checkpoint: dict[str, Any],
) -> None:
    checkpoint["status"] = "completed_empty"
    checkpoint["segment_count"] = 0
    checkpoint["completed_at"] = _now_iso()
    checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)


def _mark_failed(
    checkpoint_path: str,
    checkpoint: dict[str, Any],
    exc: Exception,
    *,
    active_chunk_index: int | None,
) -> None:
    checkpoint["status"] = "failed"
    checkpoint["last_failure"] = {
        "chunk_index": active_chunk_index,
        "message": str(exc),
        "failed_at": _now_iso(),
    }
    checkpoint["completed_chunk_count"] = len(checkpoint.get("chunks") or {})
    try:
        checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)
    except OSError:
        pass


def ensure_srt(
    video_path: str,
    progress_callback=None,
    checkpoint_path: str | None = None,
    foreground_only: bool = False,
) -> str | None:
    """确保 SRT 完整存在；只有全部分块成功后才提交正式字幕。"""

    srt_path = os.path.splitext(video_path)[0] + ".srt"
    srt_temp_path = srt_path + ".tmp"
    checkpoint_path = os.path.abspath(
        checkpoint_path or checkpoint_store.funasr_checkpoint_path(video_path)
    )
    if os.path.exists(srt_path) and os.path.getsize(srt_path) > 0:
        if checkpoint_store.existing_srt_is_reusable(
            srt_path,
            checkpoint_path,
            read_srt_entries=srt_io.read_srt_entries,
        ):
            if progress_callback:
                progress_callback("SRT 已存在，跳过转录", 5, 100)
            return srt_path
        checkpoint_store.quarantine_incomplete_srt(srt_path)
        if progress_callback:
            progress_callback("检测到残缺正式 SRT，已隔离并继续恢复转录", 5, 100)

    if progress_callback:
        progress_callback("FunASR 转录中...", 5, 100)

    duration = probe_video_duration(video_path)
    if not duration:
        raise RuntimeError("无法读取录播时长，FunASR 转录未启动。")
    streamer_name = infer_streamer_name(video_path)
    hotwords = model_runtime.funasr_hotwords(
        video_path,
        streamer_name=streamer_name,
    )
    chunk_count = max(
        1,
        int(math.ceil(duration / checkpoint_store.FUNASR_CHUNK_SEC)),
    )
    checkpoint_path, checkpoint = checkpoint_store.prepare_funasr_checkpoint(
        video_path,
        duration,
        chunk_count,
        checkpoint_path=checkpoint_path,
        hotwords=hotwords,
        foreground_only=foreground_only,
    )
    checkpoint["status"] = "running"
    checkpoint["updated_at"] = _now_iso()
    checkpoint.pop("last_failure", None)
    checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)
    missing_indices = [
        index
        for index in range(chunk_count)
        if str(index) not in checkpoint["chunks"]
    ]
    if progress_callback and len(missing_indices) < chunk_count:
        progress_callback(
            f"已复用 FunASR 检查点 {chunk_count - len(missing_indices)}/{chunk_count} 块",
            10,
            100,
        )

    active_chunk_index = None
    try:
        if missing_indices:
            try:
                recognition.recognize_missing_chunks(
                    video_path,
                    duration,
                    chunk_count,
                    missing_indices,
                    checkpoint_path,
                    checkpoint,
                    hotwords=hotwords,
                    foreground_only=foreground_only,
                    progress_callback=progress_callback,
                )
            except Exception as exc:
                active_chunk_index = getattr(exc, "chunk_index", None)
                raise

        all_segments, filtered_count, filtered_chunks = (
            _collect_checkpoint_segments(
                checkpoint,
                chunk_count=chunk_count,
                duration=duration,
                streamer_name=streamer_name,
                foreground_only=foreground_only,
            )
        )
        if not all_segments:
            _mark_completed_empty(checkpoint_path, checkpoint)
            if progress_callback:
                progress_callback("未识别到有效语音，未生成空 SRT", 0, 100)
            return None

        all_segments = subtitle_segments.dedupe_overlapping_funasr_segments(
            all_segments
        )
        checkpoint["speaker_filtered_segment_count"] = filtered_count
        checkpoint["speaker_filtered_chunk_count"] = filtered_chunks
        written_count = srt_io.write_srt_segments(
            srt_temp_path,
            all_segments,
            minimum_text_chars=2,
        )
        if not written_count:
            os.remove(srt_temp_path)
            _mark_completed_empty(checkpoint_path, checkpoint)
            return None
        checkpoint_store.commit_file_atomically(srt_temp_path, srt_path)
        checkpoint["status"] = "completed"
        checkpoint["segment_count"] = written_count
        checkpoint["coverage"] = {"start": 0.0, "end": float(duration)}
        checkpoint["completed_at"] = _now_iso()
        checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)
        if progress_callback:
            progress_callback(f"转录完成 ({written_count} 条)", 90, 100)
        return srt_path
    except Exception as exc:
        _mark_failed(
            checkpoint_path,
            checkpoint,
            exc,
            active_chunk_index=active_chunk_index,
        )
        raise
    finally:
        if os.path.exists(srt_temp_path):
            os.remove(srt_temp_path)
