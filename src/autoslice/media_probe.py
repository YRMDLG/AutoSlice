"""通过 FFprobe 读取媒体元数据的唯一实现。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

FACADE_EXPORTS = {
    "_probe_video_duration": "probe_video_duration",
}


def probe_video_duration(video_path: str | os.PathLike[str] | None) -> float | None:
    """返回媒体的正时长；路径或 FFprobe 输出无效时返回 ``None``。"""

    if not video_path:
        return None
    path = Path(video_path)
    if not path.is_file():
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                os.fspath(path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        duration = float(result.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None
    return duration if duration > 0 else None
