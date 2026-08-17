"""FunASR 检查点恢复、完整性判定与原子文件提交的唯一实现。"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable
from typing import Any

from autoslice.transcription import model_runtime
from autoslice.transcription import results as result_contracts

FUNASR_CHUNK_SEC = 120.0
FUNASR_CHUNK_PRE_CONTEXT_SEC = 20.0
FUNASR_CHECKPOINT_VERSION = 3

FACADE_EXPORTS = {
    "FUNASR_CHECKPOINT_VERSION": "FUNASR_CHECKPOINT_VERSION",
    "FUNASR_CHUNK_PRE_CONTEXT_SEC": "FUNASR_CHUNK_PRE_CONTEXT_SEC",
    "FUNASR_CHUNK_SEC": "FUNASR_CHUNK_SEC",
    "_funasr_model_runtime_signature": "funasr_model_runtime_signature",
    "_funasr_checkpoint_path": "funasr_checkpoint_path",
    "_funasr_source_fingerprint": "funasr_source_fingerprint",
    "_funasr_chunk_fingerprint": "funasr_chunk_fingerprint",
    "_funasr_chunk_input_window": "funasr_chunk_input_window",
    "_is_close_number": "is_close_number",
    "_prepare_funasr_checkpoint": "prepare_funasr_checkpoint",
    "_write_funasr_checkpoint": "write_funasr_checkpoint",
}

# 单一磁盘提交 seam；测试从 owner 替换，生产调用方不再暴露自己的副本。
replace_file_atomically = os.replace


def funasr_model_runtime_signature(foreground_only: bool = False) -> dict[str, Any]:
    """返回影响检查点兼容性的完整模型运行签名。"""

    model_source = model_runtime.resolve_funasr_model_source()
    contextual_active = "contextual" in str(model_source).casefold()
    vad_source = model_runtime.resolve_funasr_aux_model_source(
        model_runtime.FUNASR_VAD_MODEL,
        model_runtime.FUNASR_VAD_CACHE_MODEL_DIR,
    )
    punc_source = model_runtime.resolve_funasr_aux_model_source(
        model_runtime.FUNASR_PUNC_MODEL,
        model_runtime.FUNASR_PUNC_CACHE_MODEL_DIR,
    )
    speaker_source = (
        model_runtime.resolve_funasr_speaker_model_source()
        if foreground_only
        else None
    )
    return {
        "asr_model": os.path.normcase(os.path.abspath(model_source)),
        "contextual_hotwords": contextual_active,
        "vad_model": (
            os.path.normcase(os.path.abspath(vad_source)) if vad_source else None
        ),
        "punc_model": (
            os.path.normcase(os.path.abspath(punc_source)) if punc_source else None
        ),
        "speaker_model": (
            os.path.normcase(os.path.abspath(speaker_source))
            if speaker_source
            else None
        ),
        "foreground_only": bool(foreground_only),
        "foreground_audio_filter": (
            model_runtime.FUNASR_FOREGROUND_AUDIO_FILTER
            if foreground_only
            else None
        ),
        "funasr_chunk_sec": FUNASR_CHUNK_SEC,
        "funasr_chunk_pre_context_sec": FUNASR_CHUNK_PRE_CONTEXT_SEC,
    }


def funasr_checkpoint_path(video_path: str | os.PathLike[str]) -> str:
    return os.path.splitext(os.fspath(video_path))[0] + "_asr_checkpoint.json"


def funasr_source_fingerprint(
    video_path: str | os.PathLike[str],
    duration: float,
    hotwords: str = "",
    foreground_only: bool = False,
) -> str:
    stat = os.stat(video_path)
    payload = {
        "path": os.path.normcase(os.path.abspath(video_path)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration": round(float(duration), 3),
        "sample_rate": 16000,
        "channels": 1,
        "chunk_sec": FUNASR_CHUNK_SEC,
        "chunk_pre_context_sec": FUNASR_CHUNK_PRE_CONTEXT_SEC,
        "runtime_signature": funasr_model_runtime_signature(
            foreground_only=foreground_only
        ),
        "hotword_digest": hashlib.sha256(
            str(hotwords or "").encode("utf-8")
        ).hexdigest(),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def funasr_chunk_fingerprint(
    source_fingerprint: str,
    index: int,
    start: float,
    duration: float,
) -> str:
    value = f"{source_fingerprint}:{index}:{start:.3f}:{duration:.3f}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def funasr_chunk_input_window(
    index: int,
    duration: float,
) -> tuple[float, float, float, float]:
    """返回主体时间段及带前置语境的实际识别时间段。"""

    core_start = index * FUNASR_CHUNK_SEC
    core_duration = min(FUNASR_CHUNK_SEC, max(0.0, duration - core_start))
    pre_context = min(FUNASR_CHUNK_PRE_CONTEXT_SEC, core_start)
    input_start = core_start - pre_context
    input_duration = core_duration + pre_context
    return core_start, core_duration, input_start, input_duration


def is_close_number(value: Any, expected: float) -> bool:
    try:
        return math.isclose(float(value), expected, abs_tol=0.001)
    except (TypeError, ValueError):
        return False


def _new_checkpoint_payload(
    video_path: str | os.PathLike[str],
    duration: float,
    chunk_count: int,
    source_fingerprint: str,
    foreground_only: bool,
) -> dict[str, Any]:
    return {
        "version": FUNASR_CHECKPOINT_VERSION,
        "source_fingerprint": source_fingerprint,
        "runtime_signature": funasr_model_runtime_signature(
            foreground_only=foreground_only
        ),
        "video_path": os.path.abspath(video_path),
        "duration": float(duration),
        "chunk_sec": FUNASR_CHUNK_SEC,
        "chunk_pre_context_sec": FUNASR_CHUNK_PRE_CONTEXT_SEC,
        "chunk_count": int(chunk_count),
        "status": "pending",
        "foreground_only": bool(foreground_only),
        "foreground_filter_mode": (
            "speaker_diarization"
            if foreground_only
            and model_runtime.resolve_funasr_speaker_model_source()
            else "adaptive_gate"
            if foreground_only
            else "off"
        ),
        "chunks": {},
    }


def prepare_funasr_checkpoint(
    video_path: str | os.PathLike[str],
    duration: float,
    chunk_count: int,
    checkpoint_path: str | os.PathLike[str] | None = None,
    hotwords: str = "",
    foreground_only: bool = False,
) -> tuple[str, dict[str, Any]]:
    """建立新检查点，并只恢复与当前运行契约完全匹配的分块。"""

    resolved_path = os.path.abspath(
        checkpoint_path or funasr_checkpoint_path(video_path)
    )
    source_fingerprint = funasr_source_fingerprint(
        video_path,
        duration,
        hotwords=hotwords,
        foreground_only=foreground_only,
    )
    payload = _new_checkpoint_payload(
        video_path,
        duration,
        chunk_count,
        source_fingerprint,
        foreground_only,
    )
    try:
        with open(resolved_path, encoding="utf-8") as handle:
            existing = json.load(handle)
    except (OSError, ValueError, TypeError):
        existing = None
    if not isinstance(existing, dict):
        return resolved_path, payload
    if (
        existing.get("version") != FUNASR_CHECKPOINT_VERSION
        or existing.get("source_fingerprint") != source_fingerprint
        or existing.get("chunk_count") != chunk_count
    ):
        return resolved_path, payload

    existing_chunks = existing.get("chunks")
    if not isinstance(existing_chunks, dict):
        return resolved_path, payload
    if isinstance(existing.get("last_failure"), dict):
        payload["last_failure"] = existing["last_failure"]
    for index in range(chunk_count):
        start, chunk_duration, input_start, input_duration = (
            funasr_chunk_input_window(index, duration)
        )
        expected_fingerprint = funasr_chunk_fingerprint(
            source_fingerprint,
            index,
            start,
            chunk_duration,
        )
        entry = existing_chunks.get(str(index))
        if (
            isinstance(entry, dict)
            and entry.get("fingerprint") == expected_fingerprint
            and is_close_number(entry.get("input_start"), input_start)
            and is_close_number(entry.get("input_duration"), input_duration)
            and result_contracts.is_valid_funasr_result(entry.get("result"))
        ):
            payload["chunks"][str(index)] = entry
    return resolved_path, payload


def commit_file_atomically(
    temporary_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
) -> None:
    """通过可测试的 owner seam 原子提交已写完的临时文件。"""

    replace_file_atomically(temporary_path, destination_path)


def write_funasr_checkpoint(
    path: str | os.PathLike[str],
    payload: dict[str, Any],
) -> None:
    resolved_path = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(resolved_path)), exist_ok=True)
    temp_path = resolved_path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        commit_file_atomically(temp_path, resolved_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def existing_srt_is_reusable(
    srt_path: str | os.PathLike[str],
    checkpoint_path: str | os.PathLike[str],
    *,
    read_srt_entries: Callable[[str | os.PathLike[str]], list[Any]],
) -> bool:
    """仅复用结构完整且未被失败检查点标记为残缺的正式字幕。"""

    entries = read_srt_entries(srt_path)
    if not entries:
        return False
    try:
        with open(checkpoint_path, encoding="utf-8") as handle:
            checkpoint = json.load(handle)
    except (OSError, ValueError, TypeError):
        # 没有检查点时视为用户提供的完整 SRT，保持旧入口兼容。
        return not os.path.exists(checkpoint_path)
    if not isinstance(checkpoint, dict):
        return False

    if checkpoint.get("status") != "completed":
        # 用户可在失败后自行提供字幕；只有比失败检查点更新的文件才可复用。
        try:
            return os.stat(srt_path).st_mtime_ns > os.stat(checkpoint_path).st_mtime_ns
        except OSError:
            return False

    chunks = checkpoint.get("chunks")
    chunk_count = checkpoint.get("chunk_count")
    coverage = checkpoint.get("coverage")
    duration = checkpoint.get("duration")
    try:
        complete_chunks = (
            isinstance(chunks, dict)
            and int(chunk_count) > 0
            and len(chunks) == int(chunk_count)
            and all(
                result_contracts.is_valid_funasr_result(
                    chunks[str(index)].get("result")
                )
                for index in range(int(chunk_count))
            )
        )
        complete_coverage = (
            isinstance(coverage, dict)
            and is_close_number(coverage.get("start"), 0.0)
            and is_close_number(coverage.get("end"), float(duration))
        )
        expected_segments = int(checkpoint.get("segment_count"))
    except (KeyError, TypeError, ValueError):
        return False
    return complete_chunks and complete_coverage and expected_segments == len(entries)


def quarantine_incomplete_srt(
    srt_path: str | os.PathLike[str],
) -> str:
    """把失败检查点对应的旧正式字幕移出可复用路径。"""

    resolved_path = os.fspath(srt_path)
    quarantine_path = resolved_path + ".incomplete"
    commit_file_atomically(resolved_path, quarantine_path)
    return quarantine_path
