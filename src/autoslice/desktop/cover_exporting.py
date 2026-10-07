"""预览渲染、导出、导出记录与批量出图。"""

from __future__ import annotations

import glob
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from autoslice.desktop.projects import ProjectVideo, SubmissionProject
from autoslice_cover.document_render import compose_document
from autoslice_cover.renderer import save_cover_jpeg

from .cover_draft import CoverDraft
from .cover_frames import best_overview_frame
from .cover_layout import (
    canvas_size,
    document_layers,
)
from .cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    StickerObject,
)


class CoverExportService:
    """混入 CoverService；共享 storage、缓存与锁等状态。"""

    def _record_export(self, project: SubmissionProject, video: ProjectVideo, output: Path, canvas_key: str) -> None:
        """保存最近导出记录，供连续生产时轻量回退和定位。"""

        try:
            payload = json.loads(self.export_history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        entries = payload.setdefault("exports", [])
        if not isinstance(entries, list):
            entries = []
            payload["exports"] = entries
        entries.append({
            "project": project.id, "title": project.title, "video": video.path, "canvas_key": canvas_key,
            "output": str(output), "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        payload["exports"] = entries[-40:]
        self.export_history_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.export_history_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.export_history_path)

    def export_history(self) -> tuple[dict[str, object], ...]:
        try:
            payload = json.loads(self.export_history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return ()
        values = payload.get("exports") if isinstance(payload, dict) else None
        return tuple(item for item in values if isinstance(item, dict)) if isinstance(values, list) else ()

    @staticmethod
    def existing_exports(project: SubmissionProject, video: ProjectVideo) -> tuple[Path, ...]:
        """项目目录里这个视频已经导出过的封面（任意比例、任意序号）。"""

        stem = Path(video.name).stem or "封面"
        pattern = str(Path(glob.escape(project.directory)) / f"AutoCover-{glob.escape(stem)}*.jpg")
        return tuple(sorted(Path(item) for item in glob.glob(pattern)))

    def auto_cover(self, project: SubmissionProject, video: ProjectVideo) -> tuple[Path, Path]:
        """批量出图：有草稿按草稿导出；没有就全片选帧、自动排版后导出，并存成草稿方便回头再改。"""

        document, read = self.load_document(project, video)
        background = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
        image = background.asset.path if background is not None and background.asset else None
        if not image or not Path(image).is_file():
            if not Path(video.path).is_file():
                raise ValueError(f"视频不存在：{video.path}")
            keep_layout = read.status == "source_changed"
            if keep_layout:
                best = self.extract_frame_candidate(video, document.source.selected_timestamp)
            else:
                best = best_overview_frame(self.overview_candidates(video))
            if best is None:
                raise ValueError("没有取到可用画面")
            document = replace(
                document,
                source=replace(document.source, selected_timestamp=best.timestamp, image_asset_id=str(best.path)),
                objects=tuple(
                    replace(item, asset=replace(item.asset, path=str(best.path)) if item.asset else AssetRef(path=str(best.path)))
                    if isinstance(item, BackgroundObject) else item
                    for item in document.objects
                ),
            )
            if not keep_layout:
                document = self.apply_auto_layout(document, best.path)
            self.save_document(project, video, document)
        return self.export_both(project, video, document)

    def render_preview(self, video: ProjectVideo, draft: CoverDraft, *, canvas_key: str = "4x3") -> Path:
        """旧草稿调用方：转成文档后走同一条渲染管线。"""

        self._require_draft_image(draft)
        return self.render_preview_document(video, draft.to_document(), canvas_key=canvas_key)

    def render_preview_document(
        self,
        video: ProjectVideo,
        document: CoverDocument,
        *,
        canvas_key: str = "4x3",
    ) -> Path:
        """使用 v4 文档自己的文字样式渲染预览。"""

        self.previews.mkdir(parents=True, exist_ok=True)
        output = self.previews / f"{self._identity(Path(video.path))}-{canvas_key}-document-preview.jpg"
        self._render_document(document, video, output, canvas_key=canvas_key)
        return output

    def _render_document(
        self,
        document: CoverDocument,
        video: ProjectVideo,
        output: Path,
        *,
        canvas_key: str,
    ) -> None:
        if canvas_key not in {"4x3", "16x9"}:
            raise ValueError(f"不支持的封面比例：{canvas_key}")
        if not any(isinstance(item, BackgroundObject) for item in document.objects):
            raise ValueError("封面文档缺少底图对象")
        # 与 CoverCanvas 共用 cover_layout 的几何、字号和断行，保证所见即所得。
        background, layers = document_layers(document, canvas_key)
        if background is None:
            raise ValueError("请先加载底图或从当前视频取帧")
        image = compose_document(canvas_size(canvas_key), background, layers)
        try:
            save_cover_jpeg(image, output)
        finally:
            image.close()

    def export(
        self, project: SubmissionProject, video: ProjectVideo, draft: CoverDraft, *, canvas_key: str = "4x3",
    ) -> Path:
        """旧草稿调用方：转成文档后导出，命名与不覆盖规则同 export_document。"""

        self._require_draft_image(draft)
        return self.export_document(project, video, draft.to_document(), canvas_key=canvas_key)

    @staticmethod
    def _require_draft_image(draft: CoverDraft) -> None:
        if not draft.image_path or not Path(draft.image_path).is_file():
            raise ValueError("请先加载底图或从当前视频取帧")

    def export_document(
        self,
        project: SubmissionProject,
        video: ProjectVideo,
        document: CoverDocument,
        *,
        canvas_key: str = "4x3",
    ) -> Path:
        if canvas_key not in {"4x3", "16x9"}:
            raise ValueError(f"不支持的封面比例：{canvas_key}")
        stem = Path(video.name).stem or "封面"
        suffix = "" if canvas_key == "4x3" else "-16x9"
        destination = Path(project.directory) / f"AutoCover-{stem}{suffix}.jpg"
        index = 2
        while destination.exists():
            destination = Path(project.directory) / f"AutoCover-{stem}{suffix} ({index}).jpg"
            index += 1
        self._render_document(document, video, destination, canvas_key=canvas_key)
        # 导出即用户确认的成品，此时才记忆风格，临时试色不进入长期偏好。
        self.remember_style(project, document)
        for item in document.objects:
            if isinstance(item, (ImageObject, StickerObject)) and item.asset and item.asset.asset_id:
                try:
                    self.asset_library.mark_used(item.asset.asset_id, final_export=True)
                except (KeyError, OSError, ValueError):
                    pass
        self._record_export(project, video, destination, canvas_key)
        return destination

    def export_both(
        self,
        project: SubmissionProject,
        video: ProjectVideo,
        document: CoverDocument,
    ) -> tuple[Path, Path]:
        """连续生产入口：明确生成 4:3 和 16:9 两个独立文件。"""

        return (
            self.export_document(project, video, document, canvas_key="4x3"),
            self.export_document(project, video, document, canvas_key="16x9"),
        )
