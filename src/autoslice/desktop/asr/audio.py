"""识别前的音频准备：抽 16k 单声道 wav、找静音、按静音切块。ffmpeg 之外只用标准库。"""

from __future__ import annotations

import re
import subprocess
import sys
import wave
from pathlib import Path

from .types import AsrError, Chunk

# 和旧识别一致的前景增强：压住背景音乐和底噪，人声更突出
FOREGROUND_FILTER = (
    "highpass=f=80,lowpass=f=12000,afftdn=nf=-32,"
    "agate=threshold=0.012:ratio=6:attack=8:release=220"
)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def _ffmpeg(args: list[str]) -> str:
    try:
        done = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW, check=False,
        )
    except FileNotFoundError as exc:
        raise AsrError("找不到 ffmpeg，请先安装并加入 PATH") from exc
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()[-3:]
        raise AsrError("ffmpeg 失败：" + " | ".join(tail))
    return done.stderr or ""


def extract_wav(source: str | Path, target: str | Path, *, foreground: bool = False,
                start: float | None = None, duration: float | None = None) -> None:
    """抽成 16k 单声道 16 位 wav；可只取一段（秒）。"""

    command = []
    if start is not None:
        command += ["-ss", f"{max(start, 0):.3f}"]
    command += ["-i", str(source)]
    if duration is not None:
        command += ["-t", f"{duration:.3f}"]
    command += ["-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le"]
    if foreground:
        command += ["-af", FOREGROUND_FILTER]
    _ffmpeg([*command, "-y", str(target)])


def wav_duration(path: str | Path) -> float:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / float(stream.getframerate() or 16000)


def parse_silences(stderr: str, duration: float) -> list[tuple[float, float]]:
    """解析 silencedetect 的输出；末尾没有结束的静音算到音频结尾。"""

    spans: list[tuple[float, float]] = []
    open_start: float | None = None
    for line in stderr.splitlines():
        start = _SILENCE_START.search(line)
        if start:
            open_start = max(float(start.group(1)), 0.0)
            continue
        end = _SILENCE_END.search(line)
        if end and open_start is not None:
            spans.append((open_start, min(float(end.group(1)), duration)))
            open_start = None
    if open_start is not None and open_start < duration:
        spans.append((open_start, duration))
    return spans


def detect_silences(wav: str | Path, duration: float, *, noise_db: int = -35,
                    min_seconds: float = 0.35) -> list[tuple[float, float]]:
    stderr = _ffmpeg(["-i", str(wav), "-af", f"silencedetect=noise={noise_db}dB:d={min_seconds}",
                      "-f", "null", "-"])
    return parse_silences(stderr, duration)


def plan_chunks(duration: float, silences: list[tuple[float, float]], *, max_seconds: float = 45.0,
                min_seconds: float = 12.0, pad: float = 0.4) -> list[Chunk]:
    """尽量在静音中间切：每块不超过 max_seconds；找不到静音才硬切。

    每块的 core 首尾相接、不重叠；input 两头各多 pad 秒上下文，识别后只留中点落在 core 里的字。
    """

    if duration <= 0:
        return []
    cuts: list[float] = []
    cursor = 0.0
    while duration - cursor > max_seconds:
        window_low, window_high = cursor + min_seconds, cursor + max_seconds
        # 窗口里最长的静音的中点；没有就在最大长度处硬切
        candidates = [(end - start, (start + end) / 2) for start, end in silences
                      if window_low <= (start + end) / 2 <= window_high]
        cut = max(candidates)[1] if candidates else window_high
        cuts.append(cut)
        cursor = cut
    edges = [0.0, *cuts, duration]
    return [
        Chunk(index, start, end, max(0.0, start - pad), min(duration, end + pad))
        for index, (start, end) in enumerate(zip(edges, edges[1:]))
    ]
