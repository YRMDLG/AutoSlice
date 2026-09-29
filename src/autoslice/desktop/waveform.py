"""真实音频单轨幅度波形的后台生成与私有缓存。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WaveformData:
    duration: float
    samples: tuple[float, ...]
    key: str


class WaveformCache:
    """缓存 key 同时绑定媒体路径、大小和修改时间，媒体替换后不会误复用。"""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    @staticmethod
    def key(video: Path | str) -> str:
        path = Path(video).expanduser().resolve()
        stat = path.stat()
        value = f"{os.path.normcase(str(path))}\0{stat.st_size}\0{stat.st_mtime_ns}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def path(self, video: Path | str) -> Path:
        return self.root / f"{self.key(video)}.json"

    def load(self, video: Path | str) -> WaveformData | None:
        path = self.path(video)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            samples = tuple(float(item) for item in payload["samples"])
            duration = float(payload["duration"])
            key = str(payload["key"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        if key != self.key(video) or duration <= 0 or not samples:
            return None
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in samples):
            return None
        return WaveformData(duration, samples, key)

    def save(self, video: Path | str, data: WaveformData) -> Path:
        destination = self.path(video)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "key": data.key, "duration": data.duration,
                   "samples": list(data.samples)}
        fd, temporary_name = tempfile.mkstemp(prefix=".waveform-", suffix=".tmp",
                                               dir=destination.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def load_or_generate(self, video: Path | str, *, max_samples: int = 2400) -> WaveformData:
        cached = self.load(video)
        if cached is not None:
            return cached
        data = generate_waveform(video, max_samples=max_samples)
        self.save(video, data)
        return data


def generate_waveform(video: Path | str, *, max_samples: int = 2400) -> WaveformData:
    """用 ffmpeg 解码真实音轨为单声道 PCM，再压缩成可绘制的幅度包络。"""
    path = Path(video)
    if not path.is_file():
        raise FileNotFoundError("视频文件不存在")
    if max_samples < 32:
        raise ValueError("波形采样数量过少")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("无法读取视频时长") from exc
    if duration <= 0 or not math.isfinite(duration):
        raise RuntimeError("视频时长无效")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace")[-400:]
        raise RuntimeError(f"波形生成失败：{detail}")
    raw = result.stdout
    values = struct.iter_unpack("<h", raw[:len(raw) - len(raw) % 2])
    amplitudes = [abs(sample[0]) / 32768.0 for sample in values]
    if not amplitudes:
        amplitudes = [0.0]
    bucket_count = min(max_samples, max(1, int(round(duration * 20))))
    bucket_size = max(1, math.ceil(len(amplitudes) / bucket_count))
    samples = tuple(min(1.0, max(0.0, max(amplitudes[i:i + bucket_size], default=0.0)))
                    for i in range(0, len(amplitudes), bucket_size))
    return WaveformData(duration, samples[:max_samples], WaveformCache.key(path))
