"""FunASR 转录、SRT 规范化与可恢复检查点的唯一实现。"""

from __future__ import annotations

import math
import os
from datetime import datetime

from autoslice import media_probe
from autoslice.transcription import (
    checkpoints as checkpoint_store,
    model_runtime,
    recognition,
    results as result_contracts,
    segments as subtitle_segments,
    srt_io,
)
from autoslice.streamer_profiles import (
    infer_streamer_name,
    profile_identity_names,
    profile_matches_streamer,
)


FACADE_EXPORTS = {
    '_infer_streamer_name': 'infer_streamer_name',
    'FUNASR_CHECKPOINT_VERSION': 'FUNASR_CHECKPOINT_VERSION',
    'FUNASR_CHUNK_PRE_CONTEXT_SEC': 'FUNASR_CHUNK_PRE_CONTEXT_SEC',
    'FUNASR_CHUNK_SEC': 'FUNASR_CHUNK_SEC',
    '_funasr_model_runtime_signature': '_funasr_model_runtime_signature',
    '_funasr_checkpoint_path': 'funasr_checkpoint_path',
    '_funasr_source_fingerprint': '_funasr_source_fingerprint',
    '_funasr_chunk_fingerprint': '_funasr_chunk_fingerprint',
    '_funasr_chunk_input_window': '_funasr_chunk_input_window',
    '_is_close_number': '_is_close_number',
    '_prepare_funasr_checkpoint': '_prepare_funasr_checkpoint',
    '_write_funasr_checkpoint': 'write_funasr_checkpoint',
    'ensure_srt': 'ensure_srt',
    '_profile_identity_names': 'profile_identity_names',
}


# 旧调用方仍可从 service 导入这些对象；唯一实现位于 transcription.results。
_normalise_funasr_result = result_contracts.normalise_funasr_result
_is_valid_funasr_result = result_contracts.is_valid_funasr_result
_primary_speaker_segments = result_contracts.primary_speaker_segments

# 旧调用方仍可从 service 导入这些对象；唯一实现位于 transcription.checkpoints。
FUNASR_CHECKPOINT_VERSION = checkpoint_store.FUNASR_CHECKPOINT_VERSION
FUNASR_CHUNK_PRE_CONTEXT_SEC = checkpoint_store.FUNASR_CHUNK_PRE_CONTEXT_SEC
FUNASR_CHUNK_SEC = checkpoint_store.FUNASR_CHUNK_SEC
replace_file_atomically = checkpoint_store.replace_file_atomically
_funasr_model_runtime_signature = checkpoint_store.funasr_model_runtime_signature
funasr_checkpoint_path = checkpoint_store.funasr_checkpoint_path
_funasr_source_fingerprint = checkpoint_store.funasr_source_fingerprint
_funasr_chunk_fingerprint = checkpoint_store.funasr_chunk_fingerprint
_funasr_chunk_input_window = checkpoint_store.funasr_chunk_input_window
_is_close_number = checkpoint_store.is_close_number
_prepare_funasr_checkpoint = checkpoint_store.prepare_funasr_checkpoint
write_funasr_checkpoint = checkpoint_store.write_funasr_checkpoint
_existing_srt_is_reusable = checkpoint_store.existing_srt_is_reusable
_quarantine_incomplete_srt = checkpoint_store.quarantine_incomplete_srt

FUNASR_MODEL = model_runtime.FUNASR_MODEL
FUNASR_CONTEXTUAL_MODEL = model_runtime.FUNASR_CONTEXTUAL_MODEL
FUNASR_VAD_MODEL = model_runtime.FUNASR_VAD_MODEL
FUNASR_PUNC_MODEL = model_runtime.FUNASR_PUNC_MODEL
FUNASR_SPK_MODEL = model_runtime.FUNASR_SPK_MODEL
FUNASR_DEFAULT_DEVICE = model_runtime.FUNASR_DEFAULT_DEVICE
FUNASR_BATCH_SIZE_SEC = model_runtime.FUNASR_BATCH_SIZE_SEC
FUNASR_CPU_RETRY_DELAY_SEC = model_runtime.FUNASR_CPU_RETRY_DELAY_SEC
FUNASR_CACHE_MODEL_DIR = model_runtime.FUNASR_CACHE_MODEL_DIR
FUNASR_CONTEXTUAL_CACHE_MODEL_DIR = model_runtime.FUNASR_CONTEXTUAL_CACHE_MODEL_DIR
FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC = (
    model_runtime.FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC
)
FUNASR_NANO_MODEL = model_runtime.FUNASR_NANO_MODEL
FUNASR_NANO_CACHE_ROOTS = model_runtime.FUNASR_NANO_CACHE_ROOTS
FUNASR_VAD_CACHE_MODEL_DIR = model_runtime.FUNASR_VAD_CACHE_MODEL_DIR
FUNASR_PUNC_CACHE_MODEL_DIR = model_runtime.FUNASR_PUNC_CACHE_MODEL_DIR
FUNASR_SPK_CACHE_MODEL_DIR = model_runtime.FUNASR_SPK_CACHE_MODEL_DIR
FUNASR_SPK_WEIGHT_FILES = model_runtime.FUNASR_SPK_WEIGHT_FILES
FUNASR_FOREGROUND_AUDIO_FILTER = model_runtime.FUNASR_FOREGROUND_AUDIO_FILTER
FUNASR_HOTWORD_MAX_COUNT = model_runtime.FUNASR_HOTWORD_MAX_COUNT
FUNASR_HOTWORD_MAX_CHARS = model_runtime.FUNASR_HOTWORD_MAX_CHARS

_prepare_funasr_environment = model_runtime.prepare_funasr_environment
funasr_model_cache_candidates = model_runtime.funasr_model_cache_candidates
_funasr_nano_cache_candidates = model_runtime.funasr_nano_cache_candidates
resolve_funasr_model_source = model_runtime.resolve_funasr_model_source
resolve_funasr_aux_model_source = model_runtime.resolve_funasr_aux_model_source
resolve_funasr_speaker_model_source = (
    model_runtime.resolve_funasr_speaker_model_source
)
_funasr_hotwords = model_runtime.funasr_hotwords
_funasr_generate_kwargs = model_runtime.funasr_generate_kwargs
resolve_funasr_device = model_runtime.resolve_funasr_device
funasr_public_status = model_runtime.funasr_public_status
load_funasr_model = model_runtime.load_funasr_model
clear_funasr_cuda_cache = model_runtime.clear_funasr_cuda_cache

# 旧调用方仍可从 service 导入这些对象；唯一实现位于 transcription.segments。
for _facade_name, _owner_name in subtitle_segments.FACADE_EXPORTS.items():
    globals()[_facade_name] = getattr(subtitle_segments, _owner_name)
srt_time = subtitle_segments.srt_time
parse_srt_timestamp = subtitle_segments.parse_srt_timestamp

# 旧调用方仍可从 service 导入这些对象；唯一实现位于 transcription.srt_io。
_read_srt_entries = srt_io.read_srt_entries
_load_repaired_srt_segments = srt_io.load_repaired_srt_segments
export_corrected_srt = srt_io.export_corrected_srt
probe_video_duration = media_probe.probe_video_duration


def ensure_srt(
        video_path, progress_callback=None, checkpoint_path=None,
        foreground_only=False):
    """确保 SRT 存在；分块检查点可恢复，全部成功后才原子写入正式字幕。"""
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
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    checkpoint.pop("last_failure", None)
    checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)
    missing_indices = [
        index for index in range(chunk_count)
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

        all_segments = []
        speaker_filtered_count = 0
        speaker_filtered_chunks = 0
        for index in range(chunk_count):
            entry = checkpoint["chunks"].get(str(index))
            if not entry:
                raise RuntimeError(
                    f"FunASR 第 {index + 1}/{chunk_count} 块缺失，未生成残缺 SRT。"
                )
            start, chunk_duration, input_start, _ = checkpoint_store.funasr_chunk_input_window(
                index, duration
            )
            core_end = start + chunk_duration
            result_items = entry.get("result") or []
            primary_segments, removed_count, _ = (
                result_contracts.primary_speaker_segments(result_items)
                if foreground_only
                else (None, 0, None)
            )
            if primary_segments:
                speaker_filtered_count += removed_count
                speaker_filtered_chunks += 1
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
                    for segment in chunk_segments:
                        segment_midpoint = (segment[0] + segment[1]) / 2.0
                        if segment_midpoint < start or segment_midpoint >= core_end:
                            continue
                        bounded_start = max(0.0, start, segment[0])
                        bounded_end = min(duration, core_end, segment[1])
                        if bounded_end > bounded_start:
                            all_segments.append(
                                (bounded_start, bounded_end, segment[2])
                            )
                continue

            for item in result_items:
                text_value = str(item.get("text", "")).strip()
                raw_text_value = item.get("raw_text")
                timestamps = item.get("timestamp", [])
                if text_value and timestamps:
                    core_text, core_timestamps, token_aligned = (
                        subtitle_segments.trim_funasr_tokens_to_core(
                            text_value,
                            timestamps,
                            input_start,
                            start,
                            core_end,
                            raw_text=raw_text_value,
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
                    for segment in chunk_segments:
                        segment_midpoint = (segment[0] + segment[1]) / 2.0
                        if (
                                not token_aligned
                                and (segment_midpoint < start or segment_midpoint >= core_end)):
                            continue
                        bounded_start = max(0.0, start, segment[0])
                        bounded_end = min(duration, core_end, segment[1])
                        if bounded_end > bounded_start:
                            all_segments.append(
                                (bounded_start, bounded_end, segment[2])
                            )

        if not all_segments:
            checkpoint["status"] = "completed_empty"
            checkpoint["segment_count"] = 0
            checkpoint["completed_at"] = datetime.now().isoformat(timespec="seconds")
            checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)
            if progress_callback:
                progress_callback("未识别到有效语音，未生成空 SRT", 0, 100)
            return None

        all_segments = subtitle_segments.dedupe_overlapping_funasr_segments(
            all_segments
        )
        checkpoint["speaker_filtered_segment_count"] = speaker_filtered_count
        checkpoint["speaker_filtered_chunk_count"] = speaker_filtered_chunks
        written_count = srt_io.write_srt_segments(
            srt_temp_path,
            all_segments,
            minimum_text_chars=2,
        )
        if not written_count:
            os.remove(srt_temp_path)
            checkpoint["status"] = "completed_empty"
            checkpoint["segment_count"] = 0
            checkpoint["completed_at"] = datetime.now().isoformat(timespec="seconds")
            checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)
            return None
        checkpoint_store.commit_file_atomically(srt_temp_path, srt_path)
        checkpoint["status"] = "completed"
        checkpoint["segment_count"] = written_count
        checkpoint["coverage"] = {"start": 0.0, "end": float(duration)}
        checkpoint["completed_at"] = datetime.now().isoformat(timespec="seconds")
        checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)
        if progress_callback:
            progress_callback(f"转录完成 ({written_count} 条)", 90, 100)
        return srt_path
    except Exception as exc:
        checkpoint["status"] = "failed"
        checkpoint["last_failure"] = {
            "chunk_index": active_chunk_index,
            "message": str(exc),
            "failed_at": datetime.now().isoformat(timespec="seconds"),
        }
        checkpoint["completed_chunk_count"] = len(checkpoint.get("chunks") or {})
        try:
            checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)
        except OSError:
            pass
        raise
    finally:
        if os.path.exists(srt_temp_path):
            os.remove(srt_temp_path)
