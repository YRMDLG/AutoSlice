"""视频 → SRT：抽音频、按静音切块、逐块识别（带检查点可续跑）、分段成字幕、原子写出。

引擎只负责“一段音频 → 带时间的字”；切块、断点续跑、断句规则（≤16 字、停顿 0.65 秒切）
都复用旧识别里已经验证过的分段规划器，所以换引擎不会改变字幕的排版习惯。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage
from autoslice.streamer_profiles import (
    AUTO_PROFILE_ID,
    resolve_streamer_profile,
    streamer_profile_context,
)
from autoslice.transcription import segments as subtitle_segments
from autoslice.transcription.contracts import DEFAULT_SUBTITLE_MAX_CHARS
from autoslice.transcription.srt_io import write_srt_segments

from . import audio
from .engines import Engine
from .text import piece_words
from .types import AsrCancelled, AsrError, AsrOutputExists, Chunk, Word
from .work_lock import WorkLock

CHECKPOINT_VERSION = 1
Progress = Callable[[str, float], None]


@dataclass(frozen=True)
class TranscribeOptions:
    foreground_filter: bool = False
    max_chars: int = DEFAULT_SUBTITLE_MAX_CHARS
    overwrite: bool = False


@dataclass
class TranscribeResult:
    srt_path: str
    cue_count: int
    seconds: float
    audio_seconds: float
    info: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def work_dir_for(storage: DesktopStorage, video: Path) -> Path:
    key = hashlib.sha256(str(video.resolve()).casefold().encode("utf-8")).hexdigest()[:16]
    return storage.root / "cache" / "asr" / key


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _signature(video: Path, fingerprint: str, hotwords: Sequence[str], options: TranscribeOptions,
               chunk_seconds: float) -> str:
    stat = video.stat()
    raw = json.dumps([CHECKPOINT_VERSION, str(video.resolve()), stat.st_size, stat.st_mtime_ns, fingerprint,
                      list(hotwords), options.foreground_filter, chunk_seconds], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def words_in_core(words: list[Word], chunk: Chunk, *, last: bool) -> list[Word]:
    """只留中点落在本块 core 里的字：相邻块的上下文重叠部分由邻块负责，不会重复也不会丢。"""

    return [word for word in words
            if chunk.core_start <= (word.start + word.end) / 2 < chunk.core_end
            or (last and (word.start + word.end) / 2 >= chunk.core_end)]


def build_segments(words: list[Word], options: TranscribeOptions,
                   video_path: str | Path | None = None) -> list[tuple[float, float, str]]:
    """分段规划器按“当前主播配置”应用专名纠错（含你确认过的固定纠错），所以要先按视频路径激活主播配置。"""

    tokens = [(word.start, word.end, word.text) for word in sorted(words, key=lambda item: item.start)]
    # 桌面端的项目文件夹名就是投稿标题（带主播名），比视频文件名更可靠，用它当线索认主播
    hint = Path(video_path).parent.name if video_path else ""
    profile = resolve_streamer_profile(AUTO_PROFILE_ID, video_path, context_hint=hint)
    with streamer_profile_context(profile) as profile:
        return subtitle_segments.segment_timed_tokens(
            tokens, streamer_name=profile.canonical_name, max_chars=options.max_chars)


def publish_no_clobber(temporary: Path, target: Path) -> None:
    """原子发布且不覆盖；不支持硬链接的平台明确失败，不退回覆盖写入。"""

    if sys.platform == "win32":
        os.rename(temporary, target)
    else:
        os.link(temporary, target)
        temporary.unlink()


def _transcribe(video_path: str | Path, output_srt: str | Path, engine: Engine, *, workdir: Path,
               fingerprint: str = "", hotwords: Sequence[str] = (), options: TranscribeOptions | None = None,
               progress: Progress | None = None, cancelled: Callable[[], bool] | None = None,
               keep_work: bool = False, manage_engine: bool = True) -> TranscribeResult:
    options = options or TranscribeOptions()
    video, output = Path(video_path), Path(output_srt)
    if not video.is_file():
        raise AsrError("视频文件不存在")
    if output.exists() and not options.overwrite:
        raise AsrOutputExists(f"已有字幕，不覆盖：{output.name}")
    started = time.monotonic()
    report = progress or (lambda _message, _fraction: None)
    stop = cancelled or (lambda: False)
    workdir.mkdir(parents=True, exist_ok=True)
    signature = _signature(video, fingerprint, hotwords, options, engine.max_chunk_seconds)
    checkpoint_path = workdir / "checkpoint.json"
    checkpoint = _read_checkpoint(checkpoint_path, signature)
    warnings: list[str] = []

    report("提取音频…", 0.01)
    wav = workdir / "audio.wav"
    if not (checkpoint["audio_ready"] and wav.is_file()):
        audio.extract_wav(video, wav, foreground=options.foreground_filter)
        checkpoint.update(audio_ready=True, silences=None)
    duration = audio.wav_duration(wav)
    if duration < 0.5:
        raise AsrError("视频里没有可识别的音频")
    if checkpoint["silences"] is None:
        report("寻找停顿位置…", 0.05)
        checkpoint["silences"] = [list(item) for item in audio.detect_silences(wav, duration)]
        _write_json(checkpoint_path, checkpoint)
    if stop():
        raise AsrCancelled()
    chunks = audio.plan_chunks(duration, [tuple(item) for item in checkpoint["silences"]],
                               max_seconds=engine.max_chunk_seconds)

    done = checkpoint["chunks"]
    estimated = checkpoint["estimated"]
    info: dict = {}
    if any(str(chunk.index) not in done for chunk in chunks):
        if manage_engine:
            report("加载识别模型…", 0.08)
            info = engine.load(lambda message: report(message, 0.08), hotwords)
        try:
            for chunk in chunks:
                if stop():
                    raise AsrCancelled()
                key = str(chunk.index)
                if key not in done:
                    piece_path = workdir / f"chunk_{chunk.index}.wav"
                    audio.extract_wav(wav, piece_path, start=chunk.input_start, duration=chunk.input_duration)
                    try:
                        pieces = engine.recognize(str(piece_path))
                    finally:
                        piece_path.unlink(missing_ok=True)
                    words: list[Word] = []
                    for piece in pieces:
                        if not piece.words and chunk.index not in estimated:
                            estimated.append(chunk.index)
                        words += [Word(w.start + chunk.input_start, w.end + chunk.input_start, w.text)
                                  for w in piece_words(piece)]
                    kept = words_in_core(words, chunk, last=chunk.index == len(chunks) - 1)
                    done[key] = [[w.start, w.end, w.text] for w in kept]
                    _write_json(checkpoint_path, checkpoint)
                report(f"识别中 {chunk.index + 1}/{len(chunks)}", 0.1 + 0.85 * (chunk.core_end / duration))
        finally:
            if manage_engine:
                engine.close()
    if estimated:
        warnings.append(f"{len(estimated)} 段没有字级时间，字幕时间是按字数估算的，需要对一下轴")

    report("生成字幕…", 0.96)
    all_words = [Word(*item) for key in sorted(done, key=int) for item in done[key]]
    segments = build_segments(all_words, options, video)
    if not segments:
        raise AsrError("没有识别到有效语音")
    temporary = output.with_name(f".{output.stem}.asr-{uuid.uuid4().hex}.tmp")
    try:
        count = write_srt_segments(temporary, segments, minimum_text_chars=1)
        if not count:
            raise AsrError("没有识别到有效语音")
        if options.overwrite:
            os.replace(temporary, output)
        else:
            try:
                publish_no_clobber(temporary, output)
            except FileExistsError as exc:
                shutil.copyfile(temporary, workdir / "recognized.srt")
                raise AsrOutputExists(
                    f"已有字幕“{output.name}”，没有覆盖。识别结果保留在缓存中；"
                    "可以用 run --output <其他路径> 复用检查点另存。"
                ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    if not keep_work:
        shutil.rmtree(workdir, ignore_errors=True)
    report("完成", 1.0)
    return TranscribeResult(str(output), count, time.monotonic() - started, duration, info, warnings)


def _read_checkpoint(path: Path, signature: str) -> dict:
    fresh = {"version": CHECKPOINT_VERSION, "signature": signature, "audio_ready": False, "silences": None,
             "chunks": {}, "estimated": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fresh
    if not isinstance(payload, dict) or payload.get("signature") != signature or payload.get("version") != CHECKPOINT_VERSION:
        return fresh
    return {**fresh, **{key: payload[key] for key in ("audio_ready", "silences", "chunks", "estimated") if key in payload}}


def transcribe(video_path: str | Path, output_srt: str | Path, engine: Engine, *, workdir: Path,
               fingerprint: str = "", hotwords: Sequence[str] = (), options: TranscribeOptions | None = None,
               progress: Progress | None = None, cancelled: Callable[[], bool] | None = None,
               keep_work: bool = False, manage_engine: bool = True) -> TranscribeResult:
    """锁住检查点、识别、发布及清理的完整生命周期，避免界面和命令行相互写坏。"""

    with WorkLock(workdir):
        return _transcribe(video_path, output_srt, engine, workdir=workdir, fingerprint=fingerprint,
                           hotwords=hotwords, options=options, progress=progress, cancelled=cancelled,
                           keep_work=keep_work, manage_engine=manage_engine)
