"""实时字幕预览的私有缓存服务。"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from autoslice.subtitle_workflow import _probe_video_info, build_ass_document
from autoslice.transcription.contracts import SubtitleCue


class SubtitlePreviewService:
    def __init__(self, storage):
        self.storage = storage
        self._canvas_cache = {}

    def _canvas_size(self, video_path: str | Path) -> tuple[int, int]:
        path = Path(video_path).resolve()
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        cached = self._canvas_cache.get(key)
        if cached is not None:
            return cached
        info = _probe_video_info(path)
        size = (int(info["width"]), int(info["height"]))
        self._canvas_cache = {key: size}
        return size

    def render(self, video_path: str | Path, entries, *, width: int | None = None,
               height: int | None = None, style=None) -> Path:
        path = Path(video_path).resolve()
        key = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
        root = self.storage.root / "cache" / "subtitle-preview" / key
        root.mkdir(parents=True, exist_ok=True)
        if width is None or height is None:
            try:
                detected_width, detected_height = self._canvas_size(path)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                detected_width, detected_height = 1920, 1080
            width = int(width or detected_width)
            height = int(height or detected_height)
        cues = [SubtitleCue(entry.index, entry.start, entry.end, "", entry.text)
                for entry in entries]
        document = build_ass_document(cues, width, height, style)
        output = root / "preview.ass"
        unchanged = False
        if output.is_file():
            try:
                unchanged = output.read_text(encoding="utf-8") == document
            except (OSError, UnicodeError):
                unchanged = False
        if not unchanged:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="\n", dir=root,
                    prefix=f".{output.stem}-", suffix=".tmp", delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(document)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, output)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return output

    @staticmethod
    def quality_flags(entries) -> dict[int, tuple[str, ...]]:
        flags = {}
        for entry in entries:
            issues = []
            if entry.text.count("\n") >= 2:
                issues.append("超过 2 行")
            if len(entry.text.replace("\n", "")) > 34:
                issues.append("可能超出安全宽度")
            try:
                start = _seconds(entry.start)
                end = _seconds(entry.end)
                if end - start < .65:
                    issues.append("显示时间过短")
            except ValueError:
                pass
            if issues:
                flags[entry.index] = tuple(issues)
        return flags


def _seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000
