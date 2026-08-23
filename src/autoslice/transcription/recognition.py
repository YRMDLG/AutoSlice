"""FunASR 音频准备、分块推理与模型会话执行的唯一实现。"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from autoslice.transcription import background_filter, model_runtime
from autoslice.transcription import checkpoints as checkpoint_store
from autoslice.transcription import results as result_contracts


class FunASRChunkRecognitionError(RuntimeError):
    """携带失败分块索引的可恢复推理错误。"""

    def __init__(self, message: str, *, chunk_index: int):
        super().__init__(message)
        self.chunk_index = chunk_index


@dataclass
class FunASRRecognitionSession:
    """保存一次转录任务中的模型、设备和生成参数。"""

    auto_model_type: Any
    model: Any
    device: str
    generate_kwargs: dict[str, Any]
    model_load_options: dict[str, Any]
    hotwords: str
    requested_background_filter_mode: str
    progress_callback: Any = None

    @classmethod
    def load(
        cls,
        auto_model_type: Any,
        *,
        hotwords: str,
        foreground_only: bool | None = None,
        background_filter_mode: str | None = None,
        progress_callback: Any = None,
    ) -> FunASRRecognitionSession:
        policy = background_filter.background_filter_policy(
            background_filter_mode,
            foreground_only=foreground_only,
        )
        requested_device = model_runtime.resolve_funasr_device()
        if progress_callback:
            progress_callback(f"加载 FunASR 模型({requested_device})...", 10, 100)
        model_load_options = {"background_filter_mode": policy.mode}
        model = model_runtime.load_funasr_model(
            auto_model_type,
            progress_callback=progress_callback,
            device=requested_device,
            **model_load_options,
        )
        current_device = getattr(model, "_autoslice_device", requested_device)
        return cls(
            auto_model_type=auto_model_type,
            model=model,
            device=str(current_device),
            generate_kwargs=model_runtime.funasr_generate_kwargs(
                model,
                hotwords=hotwords,
            ),
            model_load_options=model_load_options,
            hotwords=hotwords,
            requested_background_filter_mode=policy.mode,
            progress_callback=progress_callback,
        )

    @property
    def foreground_filter_mode(self) -> str:
        return str(
            getattr(
                self.model,
                "_autoslice_foreground_filter",
                background_filter.technical_filter_mode(
                    self.requested_background_filter_mode,
                    speaker_model_used=self.speaker_model_used,
                ),
            )
        )

    @property
    def speaker_model_used(self) -> bool:
        return bool(getattr(self.model, "_autoslice_spk_source", None))

    @property
    def speaker_model_load_failed(self) -> bool:
        return bool(
            getattr(
                self.model,
                "_autoslice_speaker_model_load_failed",
                False,
            )
        )

    def generate(
        self,
        audio_path: str,
        *,
        chunk_index: int,
        chunk_count: int,
    ) -> Any:
        try:
            return self.model.generate(
                input=audio_path,
                **self.generate_kwargs,
            )
        except Exception as first_error:
            if self.device.startswith("cuda"):
                if self.progress_callback:
                    self.progress_callback(
                        f"第 {chunk_index + 1} 块 GPU 转录失败，改用 CPU 重试: {first_error}",
                        10 + int((chunk_index / chunk_count) * 80),
                        100,
                    )
                self.model = None
                model_runtime.clear_funasr_cuda_cache()
                self.model = model_runtime.load_funasr_model(
                    self.auto_model_type,
                    progress_callback=self.progress_callback,
                    device="cpu",
                    **self.model_load_options,
                )
                self.device = "cpu"
                self.generate_kwargs = model_runtime.funasr_generate_kwargs(
                    self.model,
                    hotwords=self.hotwords,
                )
            elif model_runtime.FUNASR_CPU_RETRY_DELAY_SEC:
                time.sleep(model_runtime.FUNASR_CPU_RETRY_DELAY_SEC)

            try:
                return self.model.generate(
                    input=audio_path,
                    **self.generate_kwargs,
                )
            except Exception as retry_error:
                raise FunASRChunkRecognitionError(
                    f"FunASR 第 {chunk_index + 1}/{chunk_count} 块连续失败，"
                    "已保留此前检查点，未生成残缺 SRT。",
                    chunk_index=chunk_index,
                ) from retry_error


@dataclass(frozen=True)
class FunASRRecognitionResult:
    completed_indices: tuple[int, ...]
    foreground_filter_mode: str
    device: str
    speaker_model_ready: bool
    speaker_model_used: bool
    speaker_model_load_failed: bool


def _run_ffmpeg(command: list[str], run_command: Any = None) -> None:
    runner = run_command or subprocess.run
    runner(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
    )


def extract_source_audio(
    video_path: str,
    wav_path: str,
    *,
    foreground_only: bool | None = None,
    background_filter_mode: str | None = None,
    run_command: Any = None,
) -> None:
    policy = background_filter.background_filter_policy(
        background_filter_mode,
        foreground_only=foreground_only,
    )
    command = [
        "ffmpeg",
        "-i",
        video_path,
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
    ]
    if policy.apply_audio_gate:
        command.extend(["-af", model_runtime.FUNASR_FOREGROUND_AUDIO_FILTER])
    command.extend(["-y", wav_path])
    _run_ffmpeg(command, run_command=run_command)


def extract_chunk_audio(
    wav_path: str,
    chunk_path: str,
    *,
    input_start: float,
    input_duration: float,
    run_command: Any = None,
) -> None:
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-ss",
            str(input_start),
            "-i",
            wav_path,
            "-t",
            str(input_duration),
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            chunk_path,
        ],
        run_command=run_command,
    )


def _resolve_auto_model_type(auto_model_type: Any = None) -> Any:
    if auto_model_type is not None:
        return auto_model_type
    try:
        from funasr import AutoModel
    except ImportError as exc:
        raise RuntimeError("FunASR 未安装，无法生成字幕。") from exc
    return AutoModel


def _record_completed_chunk(
    checkpoint_path: str,
    checkpoint: dict[str, Any],
    *,
    index: int,
    start: float,
    chunk_duration: float,
    input_start: float,
    input_duration: float,
    result: Any,
) -> None:
    checkpoint["chunks"][str(index)] = {
        "fingerprint": checkpoint_store.funasr_chunk_fingerprint(
            checkpoint["source_fingerprint"],
            index,
            start,
            chunk_duration,
        ),
        "start": start,
        "duration": chunk_duration,
        "input_start": input_start,
        "input_duration": input_duration,
        "result": result_contracts.normalise_funasr_result(result),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    checkpoint["completed_chunk_count"] = len(checkpoint["chunks"])
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    checkpoint_store.write_funasr_checkpoint(checkpoint_path, checkpoint)


def recognize_missing_chunks(
    video_path: str,
    duration: float,
    chunk_count: int,
    missing_indices: list[int] | tuple[int, ...],
    checkpoint_path: str,
    checkpoint: dict[str, Any],
    *,
    hotwords: str = "",
    foreground_only: bool | None = None,
    background_filter_mode: str | None = None,
    progress_callback: Any = None,
    auto_model_type: Any = None,
    run_command: Any = None,
) -> FunASRRecognitionResult:
    """识别所有缺失分块，每块成功后立即原子更新检查点。"""

    policy = background_filter.background_filter_policy(
        background_filter_mode,
        foreground_only=foreground_only,
    )
    indices = tuple(int(index) for index in missing_indices)
    if not indices:
        return FunASRRecognitionResult(
            completed_indices=(),
            foreground_filter_mode=str(
                checkpoint.get("foreground_filter_mode")
                or background_filter.technical_filter_mode(
                    policy.mode,
                    speaker_model_used=bool(
                        checkpoint.get("speaker_model_used")
                    ),
                )
            ),
            device=str(checkpoint.get("device") or ""),
            speaker_model_ready=bool(
                checkpoint.get("speaker_model_ready", False)
            ),
            speaker_model_used=bool(checkpoint.get("speaker_model_used", False)),
            speaker_model_load_failed=bool(
                checkpoint.get("speaker_model_load_failed", False)
            ),
        )

    auto_model_type = _resolve_auto_model_type(auto_model_type)
    wav_path = os.path.splitext(video_path)[0] + f"_asr_{uuid.uuid4().hex[:6]}.wav"
    active_chunk_path: str | None = None
    active_chunk_index: int | None = None
    completed_indices: list[int] = []
    try:
        extract_source_audio(
            video_path,
            wav_path,
            background_filter_mode=policy.mode,
            run_command=run_command,
        )
        session = FunASRRecognitionSession.load(
            auto_model_type,
            hotwords=hotwords,
            background_filter_mode=policy.mode,
            progress_callback=progress_callback,
        )
        checkpoint["foreground_filter_mode"] = session.foreground_filter_mode
        checkpoint["device"] = session.device
        checkpoint["speaker_model_ready"] = bool(
            checkpoint.get("speaker_model_ready")
            or session.speaker_model_used
        )
        checkpoint["speaker_model_used"] = session.speaker_model_used
        checkpoint["speaker_model_load_failed"] = (
            session.speaker_model_load_failed
        )
        if progress_callback and policy.enabled:
            progress_callback(
                (
                    "CAM++ 已启用；软过滤只统计候选，严格过滤才会删除非主要说话人"
                    if session.speaker_model_used
                    else "CAM++ 文件已检测到但加载失败，已回退基础背景音门限；"
                         "未区分说话人"
                    if session.speaker_model_load_failed
                    else "CAM++ 所需本地文件缺失或不完整，已启用基础背景音门限；"
                         "未区分说话人"
                ),
                12,
                100,
            )

        for index in indices:
            active_chunk_index = index
            start, chunk_duration, input_start, input_duration = (
                checkpoint_store.funasr_chunk_input_window(index, duration)
            )
            if progress_callback:
                progress_callback(
                    f"转录中 ({index + 1}/{chunk_count})...",
                    10 + int((index / chunk_count) * 80),
                    100,
                )

            if chunk_count == 1:
                active_chunk_path = wav_path
            else:
                active_chunk_path = (
                    os.path.splitext(video_path)[0] + f"_chunk_{index}.wav"
                )
                extract_chunk_audio(
                    wav_path,
                    active_chunk_path,
                    input_start=input_start,
                    input_duration=input_duration,
                    run_command=run_command,
                )

            result = session.generate(
                active_chunk_path,
                chunk_index=index,
                chunk_count=chunk_count,
            )
            checkpoint["foreground_filter_mode"] = (
                session.foreground_filter_mode
            )
            checkpoint["device"] = session.device
            checkpoint["speaker_model_used"] = session.speaker_model_used
            checkpoint["speaker_model_load_failed"] = (
                session.speaker_model_load_failed
            )
            _record_completed_chunk(
                checkpoint_path,
                checkpoint,
                index=index,
                start=start,
                chunk_duration=chunk_duration,
                input_start=input_start,
                input_duration=input_duration,
                result=result,
            )
            completed_indices.append(index)
            if active_chunk_path != wav_path and os.path.exists(active_chunk_path):
                os.remove(active_chunk_path)
            active_chunk_path = None
            active_chunk_index = None

        return FunASRRecognitionResult(
            completed_indices=tuple(completed_indices),
            foreground_filter_mode=session.foreground_filter_mode,
            device=session.device,
            speaker_model_ready=bool(checkpoint.get("speaker_model_ready")),
            speaker_model_used=session.speaker_model_used,
            speaker_model_load_failed=session.speaker_model_load_failed,
        )
    except Exception as exc:
        if getattr(exc, "chunk_index", None) is None:
            try:
                exc.chunk_index = active_chunk_index
            except (AttributeError, TypeError):
                pass
        raise
    finally:
        if (
            active_chunk_path
            and active_chunk_path != wav_path
            and os.path.exists(active_chunk_path)
        ):
            os.remove(active_chunk_path)
        if os.path.exists(wav_path):
            os.remove(wav_path)
