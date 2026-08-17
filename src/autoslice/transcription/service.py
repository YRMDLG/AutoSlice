"""FunASR 转录、SRT 规范化与可恢复检查点的唯一实现。"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime

from autoslice.transcription import (
    checkpoints as checkpoint_store,
    model_runtime,
    recognition,
    results as result_contracts,
    segments as subtitle_segments,
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
    '_read_srt_entries': '_read_srt_entries',
    'export_corrected_srt': 'export_corrected_srt',
    '_probe_video_duration': 'probe_video_duration',
    '_funasr_model_runtime_signature': '_funasr_model_runtime_signature',
    '_funasr_checkpoint_path': 'funasr_checkpoint_path',
    '_funasr_source_fingerprint': '_funasr_source_fingerprint',
    '_funasr_chunk_fingerprint': '_funasr_chunk_fingerprint',
    '_funasr_chunk_input_window': '_funasr_chunk_input_window',
    '_is_close_number': '_is_close_number',
    '_prepare_funasr_checkpoint': '_prepare_funasr_checkpoint',
    '_write_funasr_checkpoint': 'write_funasr_checkpoint',
    'ensure_srt': 'ensure_srt',
    '_load_repaired_srt_segments': '_load_repaired_srt_segments',
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


def _read_srt_entries(srt_path):
    """读取原始 SRT 条目，不提前修正时间，供异常结构识别使用。"""
    if not srt_path or not os.path.exists(srt_path):
        return []
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    pattern = r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)'
    entries = []
    for start_str, end_str, text in re.findall(pattern, content, re.DOTALL):
        clean_text = text.strip().replace("\n", " ").strip()
        if not clean_text:
            continue
        entries.append((
            subtitle_segments.parse_srt_timestamp(start_str),
            subtitle_segments.parse_srt_timestamp(end_str),
            clean_text,
        ))
    return entries


def _load_repaired_srt_segments(srt_path):
    """加载健康 SRT，并无损还原旧版 FunASR 的“全文逐字重复”异常文件。"""
    entries = _read_srt_entries(srt_path)
    if not entries:
        return []
    streamer_name = infer_streamer_name(srt_path)
    segments = []
    index = 0
    while index < len(entries):
        raw_text = entries[index][2]
        group_end = index + 1
        while group_end < len(entries) and entries[group_end][2] == raw_text:
            group_end += 1
        group = entries[index:group_end]
        tokens = raw_text.split()
        is_repeated_funasr_block = (
            len(group) >= subtitle_segments.SRT_REPEAT_REPAIR_MIN_ENTRIES
            and len(tokens) == len(group)
            and subtitle_segments.subtitle_text_size(raw_text) >= 20
        )
        if is_repeated_funasr_block:
            timed_tokens = [
                (entry[0], entry[1], token)
                for entry, token in zip(group, tokens)
            ]
            segments.extend(subtitle_segments.segment_timed_tokens(
                timed_tokens,
                streamer_name=streamer_name,
                max_chars=subtitle_segments.SUBTITLE_LEGACY_REPAIR_MAX_CHARS,
            ))
        else:
            for start_s, end_s, text in group:
                clean_text = subtitle_segments.normalise_asr_text(
                    text,
                    streamer_name=streamer_name,
                )
                if not clean_text:
                    continue
                repaired_end = subtitle_segments.repair_srt_end_time(
                    start_s,
                    end_s,
                    clean_text,
                )
                if (
                    segments
                    and clean_text == segments[-1][2]
                    and start_s - segments[-1][1]
                    <= subtitle_segments.TOPIC_CONTEXT_GAP
                ):
                    segments[-1] = (
                        segments[-1][0],
                        max(segments[-1][1], repaired_end),
                        clean_text,
                    )
                else:
                    segments.append((start_s, repaired_end, clean_text))
        index = group_end
    return sorted(segments, key=lambda item: (item[0], item[1]))


def export_corrected_srt(source_srt_path, output_path=None):
    """生成可导入剪映的校对版，不覆盖原始 SRT。"""
    segments = _load_repaired_srt_segments(source_srt_path)
    if not segments:
        return None
    output_path = os.path.abspath(
        output_path or os.path.splitext(source_srt_path)[0] + "_校对字幕.srt"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for index, (start_s, end_s, text) in enumerate(segments, 1):
            f.write(
                f"{index}\n{subtitle_segments.srt_time(start_s)} --> "
                f"{subtitle_segments.srt_time(max(end_s, start_s + 0.1))}\n"
                f"{text}\n\n"
            )
    return output_path


def probe_video_duration(video_path):
    """用 ffprobe 获取当前分段视频的精确时长；失败时返回 None。"""
    if not video_path or not os.path.isfile(video_path):
        return None
    import subprocess as sp
    try:
        result = sp.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", video_path,
            ],
            check=True,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except (OSError, ValueError, sp.CalledProcessError):
        return None


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
            read_srt_entries=_read_srt_entries,
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
        written_count = 0
        with open(srt_temp_path, "w", encoding="utf-8") as handle:
            for start, end, text_value in all_segments:
                if len(text_value) < 2:
                    continue
                written_count += 1
                handle.write(
                    f"{written_count}\n{subtitle_segments.srt_time(start)} --> "
                    f"{subtitle_segments.srt_time(end)}\n"
                    f"{text_value}\n\n"
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
