"""AutoCover-01 的无 UI 服务：草稿、底图缓存、取帧和导出。"""

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
    """可恢复的第一版封面编辑状态。坐标使用画布归一化值。"""

    title: str
    image_path: str | None = None
    selected_timestamp: float = 0.0
    text_x: float = 0.06
    text_y: float = 0.12
    font_size: int = 104

    def to_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "title": self.title,
            "image_path": self.image_path,
            "selected_timestamp": self.selected_timestamp,
            "text_x": self.text_x,
            "text_y": self.text_y,
            "font_size": self.font_size,
        }

    @classmethod
    def from_payload(cls, payload: object, fallback_title: str) -> "CoverDraft":
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return cls(fallback_title)
        title = str(payload.get("title") or fallback_title).strip() or fallback_title
        try:
            timestamp = max(0.0, float(payload.get("selected_timestamp", 0.0)))
            text_x = min(1.0, max(0.0, float(payload.get("text_x", 0.06))))
            text_y = min(1.0, max(0.0, float(payload.get("text_y", 0.12))))
            font_size = min(320, max(24, int(payload.get("font_size", 104))))
        except (TypeError, ValueError):
            return cls(fallback_title)
        image_path = payload.get("image_path")
        if not isinstance(image_path, str) or not image_path.strip():
            image_path = None
        return cls(title, image_path, timestamp, text_x, text_y, font_size)


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
        render_cover(
            draft.image_path,
            draft.title,
            output,
            video_path=video.path,
            canvas_key="16x9",
            template_key="headline",
            copy_lines=[draft.title],
            text_transforms=[TextTransform(draft.text_x, draft.text_y, font_size=draft.font_size)],
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
        render_cover(
            draft.image_path,
            draft.title,
            destination,
            video_path=video.path,
            canvas_key="16x9",
            template_key="headline",
            copy_lines=[draft.title],
            text_transforms=[TextTransform(draft.text_x, draft.text_y, font_size=draft.font_size)],
        )
        return destination
