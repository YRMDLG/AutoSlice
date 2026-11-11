"""Qt 字幕压制适配层：私有工作文件、输出保护和旧 FFmpeg 工作流。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from threading import Event

from autoslice.desktop.foundation import DesktopStorage
from autoslice.subtitle_workflow import (
    EXACT_SUBTITLE_FONT,
    burn_subtitles,
    parse_srt_document,
    verify_exact_subtitle_font,
)


def subtitle_font_problem() -> str:
    """默认字幕字体能否精确加载：能则返回空串，否则返回给用户看的说明（结果有缓存）。"""

    try:
        result = verify_exact_subtitle_font()
    except FileNotFoundError:
        return "没找到 FFmpeg：压制字幕需要 FFmpeg 和 ffprobe，安装后重启软件。"
    except OSError as exc:
        return f"检查字幕字体失败：{exc}"
    if result["available"]:
        return ""
    actual = result["resolved"] or "其它字体"
    return (f"本机没装默认字幕字体「{EXACT_SUBTITLE_FONT}」（思源黑体 Black），压制会失败"
            f"（现在会被换成 {actual}）。安装这个字体后重启软件即可。")


class SubtitleRenderService:
    def __init__(self, storage: DesktopStorage) -> None:
        self.storage = storage

    @staticmethod
    def default_output(video: Path) -> Path:
        return video.with_name(f"{video.stem}_字幕版.mp4")

    @staticmethod
    def _publish(source: Path, destination: Path) -> None:
        """以排他创建发布成片；同名文件即使在压制中出现也不覆盖。"""
        try:
            os.link(source, destination)
        except FileExistsError:
            raise
        except OSError:
            # 私有缓存与投稿目录可能跨卷，复制时仍使用排他创建。
            with source.open("rb") as reader, destination.open("xb") as writer:
                try:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise

    def render(
        self, video_path: str | Path, corrected_srt_path: str | Path,
        expected_digest: str, *, progress_callback=None, cancel_event: Event | None = None,
    ) -> dict:
        try:
            return self._render(
                video_path, corrected_srt_path, expected_digest,
                progress_callback=progress_callback, cancel_event=cancel_event,
            )
        except Exception:
            self.storage.logs.mkdir(parents=True, exist_ok=True)
            log = self.storage.logs / "subtitle-render.log"
            with log.open("a", encoding="utf-8") as stream:
                stream.write(f"\n[{datetime.now().astimezone().isoformat()}] {Path(video_path).name}\n")
                stream.write(traceback.format_exc())
            raise

    def _render(
        self, video_path: str | Path, corrected_srt_path: str | Path,
        expected_digest: str, *, progress_callback=None, cancel_event: Event | None = None,
    ) -> dict:
        video = Path(video_path).resolve()
        corrected = Path(corrected_srt_path).resolve()
        if not video.is_file() or not corrected.is_file():
            raise ValueError("视频或已保存的校对字幕不存在")
        if corrected.parent != video.parent:
            raise ValueError("校对字幕与视频不在同一项目目录")
        if not corrected.name.endswith("_校对.srt"):
            raise ValueError("请先保存校对字幕，再压制")
        if hashlib.sha256(corrected.read_bytes()).hexdigest() != expected_digest:
            raise ValueError("校对字幕已在外部变化，请重新打开项目")
        if not parse_srt_document(corrected):
            raise ValueError("校对字幕为空，请先检查字幕")

        temp_root = self.storage.root / "cache" / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="subtitle-render-", dir=temp_root) as name:
            work = Path(name)
            private_srt = work / corrected.name
            shutil.copyfile(corrected, private_srt)
            if hashlib.sha256(private_srt.read_bytes()).hexdigest() != expected_digest:
                raise ValueError("校对字幕复制期间发生变化，请重试")
            private_output = work / "rendered.mp4"
            result = burn_subtitles(
                video, private_srt, output_path=private_output,
                progress_callback=progress_callback, cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("字幕压制已取消")
            base = self.default_output(video)
            index = 1
            while True:
                destination = base if index == 1 else base.with_name(f"{base.stem} ({index}).mp4")
                try:
                    self._publish(private_output, destination)
                    break
                except FileExistsError:
                    index += 1
            if cancel_event is not None and cancel_event.is_set():
                destination.unlink(missing_ok=True)
                raise RuntimeError("字幕压制已取消")
            return {
                "output_video_path": str(destination),
                "encoder": result["encoder"],
                "source_video_info": result["source_video_info"],
                "output_video_info": result["output_video_info"],
            }
