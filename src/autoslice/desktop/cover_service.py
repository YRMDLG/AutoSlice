"""AutoCover 的无 UI 服务：草稿、底图缓存、取帧和导出。"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage, DraftRead
from autoslice.desktop.projects import ProjectVideo, SubmissionProject
from autoslice_cover.renderer import TextTransform, render_cover
from autoslice_cover.video import extract_frame_at_timestamp


@dataclass(frozen=True)
class CoverDraft:
    """可恢复的封面编辑状态。坐标使用画布归一化值。"""

    title: str
    image_path: str | None = None
    selected_timestamp: float = 0.0
    text_x: float = 0.06
    text_y: float = 0.12
    font_size: int = 104
    background_x: float = 0.5
    background_y: float = 0.5
    background_scale: float = 1.0

    def to_payload(self) -> dict[str, object]:
        return {
            "version": 2,
            "title": self.title,
            "image_path": self.image_path,
            "selected_timestamp": self.selected_timestamp,
            "text_x": self.text_x,
            "text_y": self.text_y,
            "font_size": self.font_size,
            "background_x": self.background_x,
            "background_y": self.background_y,
            "background_scale": self.background_scale,
        }

    @classmethod
    def from_payload(cls, payload: object, fallback_title: str) -> "CoverDraft":
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
            return cls(fallback_title)
        title = str(payload.get("title") or fallback_title).strip() or fallback_title
        try:
            timestamp = max(0.0, float(payload.get("selected_timestamp", 0.0)))
            text_x = min(1.0, max(0.0, float(payload.get("text_x", 0.06))))
            text_y = min(1.0, max(0.0, float(payload.get("text_y", 0.12))))
            font_size = min(320, max(24, int(payload.get("font_size", 104))))
            background_x = min(1.0, max(0.0, float(payload.get("background_x", 0.5))))
            background_y = min(1.0, max(0.0, float(payload.get("background_y", 0.5))))
            background_scale = min(2.5, max(1.0, float(payload.get("background_scale", 1.0))))
        except (TypeError, ValueError):
            return cls(fallback_title)
        image_path = payload.get("image_path")
        if not isinstance(image_path, str) or not image_path.strip():
            image_path = None
        return cls(
            title, image_path, timestamp, text_x, text_y, font_size,
            background_x, background_y, background_scale,
        )


def wrap_cover_title(title: str, font_size: int, *, max_width: float = 0.86) -> tuple[str, ...]:
    """按近似字体宽度拆分标题，保留手动换行并避免超出画布。"""

    canvas_width = 1920 * max(0.55, min(0.92, max_width))
    limit = max(8.0, canvas_width / max(24.0, float(font_size)))
    lines: list[str] = []
    for source_line in (str(title).replace("\r\n", "\n").split("\n") or [""]):
        current = ""
        units = 0.0
        for char in source_line:
            width = 1.18 if ord(char) > 0x2E80 or ord(char) > 0x1F000 else (0.65 if char.isascii() else 1.0)
            if current and units + width > limit:
                lines.append(current)
                current, units = "", 0.0
            current += char
            units += width
        lines.append(current or " ")
    return tuple(lines[:8]) or (" ",)


def text_transforms_for(draft: CoverDraft, lines: tuple[str, ...]):
    """为多行标题生成同一拖动组的逐行变换。"""

    step = min(0.18, max(0.035, draft.font_size * 1.16 / 1080.0))
    max_y = max(0.0, 0.98 - step * max(0, len(lines) - 1) - draft.font_size / 1080.0)
    y = min(max_y, max(0.0, draft.text_y))
    return tuple(
        TextTransform(
            min(1.0, max(0.0, draft.text_x)),
            min(1.0, max(0.0, y + index * step)),
            font_size=draft.font_size,
        )
        for index in range(len(lines))
    )


class CoverService:
    """封面草稿、素材缓存和导出的无 UI 服务。"""

    def __init__(self, storage: DesktopStorage) -> None:
        self.storage = storage
        self.assets = storage.thumbnails / "cover-assets"
        self.previews = storage.thumbnails / "cover-previews"

    @staticmethod
    def _identity(path: Path) -> str:
        try:
            stat = path.stat()
            signature = f"{path.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}"
        except OSError:
            signature = str(path.resolve())
        return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]

    def load(self, project: SubmissionProject, video: ProjectVideo) -> tuple[CoverDraft, DraftRead]:
        fallback = CoverDraft(project.title)
        read = self.storage.read_draft("cover", project.directory, video.path)
        if read.status != "ready":
            return fallback, read
        draft = CoverDraft.from_payload(read.payload, project.title)
        if draft.image_path and not Path(draft.image_path).is_file():
            draft = replace(draft, image_path=None)
        return draft, read

    def import_image(self, source: str | Path) -> Path:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"图片不存在：{source_path}")
        suffix = source_path.suffix.casefold()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            raise ValueError("封面底图只支持 PNG、JPG、WEBP 或 BMP")
        destination = self.assets / f"{self._identity(source_path)}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file():
            shutil.copy2(source_path, destination)
        return destination

    def save(self, project: SubmissionProject, video: ProjectVideo, draft: CoverDraft) -> Path:
        return self.storage.save_draft("cover", project.directory, video.path, draft.to_payload())

    def extract_frame(self, video: ProjectVideo, timestamp: float) -> tuple[Path, float]:
        candidate, _metadata = extract_frame_at_timestamp(
            video.path,
            timestamp,
            cache_dir=self.storage.thumbnails / "cover-frames",
        )
        return Path(candidate.path), candidate.timestamp

    def render_preview(self, video: ProjectVideo, draft: CoverDraft) -> Path:
        if not draft.image_path or not Path(draft.image_path).is_file():
            raise ValueError("请先加载底图或从当前视频取帧")
        self.previews.mkdir(parents=True, exist_ok=True)
        output = self.previews / f"{self._identity(Path(video.path))}-preview.jpg"
        lines = wrap_cover_title(draft.title, draft.font_size)
        render_cover(
            draft.image_path,
            draft.title,
            output,
            video_path=video.path,
            canvas_key="16x9",
            template_key="headline",
            copy_lines=lines,
            text_transforms=text_transforms_for(draft, lines),
            focus_x=draft.background_x,
            focus_y=draft.background_y,
            background_scale=draft.background_scale,
        )
        return output

    def export(self, project: SubmissionProject, video: ProjectVideo, draft: CoverDraft) -> Path:
        if not draft.image_path or not Path(draft.image_path).is_file():
            raise ValueError("请先加载底图或从当前视频取帧")
        stem = Path(video.name).stem or "封面"
        destination = Path(project.directory) / f"AutoCover-{stem}.jpg"
        index = 2
        while destination.exists():
            destination = Path(project.directory) / f"AutoCover-{stem} ({index}).jpg"
            index += 1
        lines = wrap_cover_title(draft.title, draft.font_size)
        render_cover(
            draft.image_path,
            draft.title,
            destination,
            video_path=video.path,
            canvas_key="16x9",
            template_key="headline",
            copy_lines=lines,
            text_transforms=text_transforms_for(draft, lines),
            focus_x=draft.background_x,
            focus_y=draft.background_y,
            background_scale=draft.background_scale,
        )
        return destination
