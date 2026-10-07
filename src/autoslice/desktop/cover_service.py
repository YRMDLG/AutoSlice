"""AutoCover 的无 UI 服务：草稿、底图缓存、取帧和导出。"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

from autoslice.desktop.foundation import DesktopStorage, DraftRead
from autoslice.desktop.projects import ProjectVideo, SubmissionProject
from autoslice_cover.fonts import resolve_font_selection
from autoslice_cover.renderer import ShapeOverlay, StickerOverlay, TextTransform, render_cover
from autoslice_cover.text_layout import wrap_text_lines
from autoslice_cover.video import extract_frame_at_timestamp

from .cover_ai import CoverAIBeta, CoverAICandidate
from .cover_assets import CoverAssetLibrary
from .cover_copy import BasicCoverCopy, generate_basic_copy_variants
from .cover_migration import (
    document_from_basic_title_values,
    document_from_draft_values,
    document_from_payload,
    draft_values_from_document,
)
from .cover_model import (
    BackgroundObject,
    CoverDocument,
    ImageObject,
    Rect,
    ShapeObject,
    StickerObject,
    TextObject,
    object_for_profile,
)
from .cover_style import CoverStyleMemory, CoverStyleMemoryStore


@dataclass(frozen=True)
class CoverDraft:
    """可恢复的封面编辑状态。坐标使用画布归一化值。"""

    title: str
    image_path: str | None = None
    selected_timestamp: float = 0.0
    text_x: float = 0.06
    text_y: float = 0.18
    font_size: int = 104
    background_x: float = 0.5
    background_y: float = 0.5
    background_scale: float = 1.0

    def to_payload(self) -> dict[str, object]:
        return {
            "version": 3,
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

    def to_document(self) -> CoverDocument:
        return document_from_draft_values(
            title=self.title,
            image_path=self.image_path,
            selected_timestamp=self.selected_timestamp,
            text_x=self.text_x,
            text_y=self.text_y,
            font_size=self.font_size,
            background_x=self.background_x,
            background_y=self.background_y,
            background_scale=self.background_scale,
        )

    @classmethod
    def from_document(cls, document: CoverDocument) -> "CoverDraft":
        values = draft_values_from_document(document)
        return cls(**values)

    @classmethod
    def from_payload(cls, payload: object, fallback_title: str) -> "CoverDraft":
        if isinstance(payload, dict) and payload.get("version") == 4:
            try:
                document, _migrated = document_from_payload(payload, fallback_title)
                return cls.from_document(document)
            except (TypeError, ValueError):
                return cls(fallback_title)
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2, 3}:
            return cls(fallback_title)
        title = str(payload.get("title") or fallback_title).strip() or fallback_title
        try:
            timestamp = max(0.0, float(payload.get("selected_timestamp", 0.0)))
            text_x = min(1.0, max(0.0, float(payload.get("text_x", 0.06))))
            text_y = min(1.0, max(0.0, float(payload.get("text_y", 0.18))))
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


def wrap_cover_title(
    title: str,
    font_size: int,
    *,
    max_width: float = 0.86,
    canvas_width: int = 1440,
) -> tuple[str, ...]:
    """按近似字体宽度拆分标题，默认以 4:3 主画布宽度计算。"""

    return wrap_text_lines(
        title,
        font_size,
        max_width=max_width,
        max_lines=8,
        canvas_width=canvas_width,
    )


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
        self.asset_library = CoverAssetLibrary(storage.root)
        self.style_memory = CoverStyleMemoryStore(storage.root)
        self.ai = CoverAIBeta(enabled=False)
        self.export_history_path = storage.root / "cover-export-history.json"

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
        entries.append({"project": project.id, "video": video.path, "canvas_key": canvas_key, "output": str(output), "timestamp": datetime.now(timezone.utc).isoformat()})
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
    def _identity(path: Path) -> str:
        try:
            stat = path.stat()
            signature = f"{path.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}"
        except OSError:
            signature = str(path.resolve())
        return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]

    def basic_copy_variants(
        self, title: str, *, subtitle_context: str | None = None,
    ) -> tuple[BasicCoverCopy, ...]:
        return generate_basic_copy_variants(title, subtitle_context=subtitle_context)

    def basic_hook_variants(self, title: str, *, subtitle_context: str | None = None) -> tuple[BasicCoverCopy, ...]:
        """旧调用方兼容别名；返回基础文案候选，不生成“钩子”。"""

        return self.basic_copy_variants(title, subtitle_context=subtitle_context)

    def load_document(
        self, project: SubmissionProject, video: ProjectVideo,
    ) -> tuple[CoverDocument, DraftRead]:
        """读取 v4 文档；读取到旧 CoverDraft 时立即迁移并原子保存 v4。"""
        read = self.storage.read_draft("cover", project.directory, video.path)
        timestamp = 0.0
        if read.status == "ready" and isinstance(read.payload, dict):
            source = read.payload.get("source")
            if isinstance(source, dict):
                try:
                    timestamp = max(0.0, float(source.get("selected_timestamp", 0.0)))
                except (TypeError, ValueError):
                    timestamp = 0.0
            elif "selected_timestamp" in read.payload:
                try:
                    timestamp = max(0.0, float(read.payload.get("selected_timestamp", 0.0)))
                except (TypeError, ValueError):
                    timestamp = 0.0
        context = self.subtitle_context(video, timestamp)
        variants = self.basic_copy_variants(project.title, subtitle_context=context)
        fallback_title = variants[0].text if variants else project.title
        fallback = document_from_basic_title_values(
            title=project.title,
            image_path=None,
            selected_timestamp=0.0,
            background_x=0.5,
            background_y=0.5,
            background_scale=1.0,
            font_size=104,
        )
        fallback = self._apply_style_memory(fallback, self.style_memory.load(project.title))
        if read.status != "ready":
            return fallback, read
        try:
            document, migrated = document_from_payload(read.payload, fallback_title)
        except (TypeError, ValueError):
            return fallback, read
        document, compacted = self._compact_legacy_default_copy(
            document, fallback_title, project.title,
        )
        document, split = self._upgrade_auto_copy_structure(document, project.title)
        compacted = compacted or split
        document, relaid = self._reflow_auto_default_layout(document, canvas_key="4x3")
        compacted = compacted or relaid
        migrated = migrated or compacted
        if migrated:
            try:
                self.save_document(project, video, document)
            except (OSError, ValueError):
                # 迁移失败时保留读到的旧内容；当前编辑器仍可继续使用内存文档。
                pass
        return document, read

    @staticmethod
    def _apply_style_memory(document: CoverDocument, memory: CoverStyleMemory) -> CoverDocument:
        objects = tuple(
            replace(item, style=memory.text_style(role=item.copy_role))
            if isinstance(item, TextObject) else item
            for item in document.objects
        )
        return replace(document, objects=objects)

    def remember_style(self, project: SubmissionProject, document: CoverDocument) -> None:
        text = next((item for item in document.objects if isinstance(item, TextObject) and item.copy_role == "B"), None)
        if text is None:
            return
        style = text.style
        memory = CoverStyleMemory(
            font_family=style.font_family,
            font_weight=style.font_weight,
            headline_size=style.font_size,
            fill_color=style.fill_color,
            stroke_color=style.stroke_color,
            stroke_width=style.stroke_width,
            shadow=style.shadow,
            line_spacing=style.line_spacing,
        )
        self.style_memory.save(memory, streamer=project.title)

    @staticmethod
    def subtitle_context(video: ProjectVideo, timestamp: float, *, radius: float = 4.0) -> str:
        """读取当前帧附近的校对字幕文本；失败时返回空字符串。"""

        source = video.corrected_srt_path if video.has_corrected_srt else video.srt_path
        if not source or not Path(source).is_file():
            return ""
        try:
            raw = Path(source).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return ""
        import re
        def seconds(value: str) -> float:
            match = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
            if not match:
                return -1.0
            return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(match.group(4)) / 1000
        blocks: list[str] = []
        for block in re.split(r"\r?\n\s*\r?\n", raw):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if len(lines) < 2 or "-->" not in lines[1]:
                continue
            start_raw, end_raw = [item.strip() for item in lines[1].split("-->", 1)]
            start, end = seconds(start_raw), seconds(end_raw)
            if start < 0 or end < 0 or end < timestamp - radius or start > timestamp + radius:
                continue
            text = " ".join(lines[2:]).strip()
            if text:
                blocks.append(text)
        return " ".join(blocks)[:240]

    def _reflow_auto_default_layout(
        self, document: CoverDocument, *, canvas_key: str,
    ) -> tuple[CoverDocument, bool]:
        """迁移本轮之前保存的程序默认坐标，保留用户接管后的布局。

        旧 Phase 2.2C 会把自动对象保存成 ``copy-a/copy-b``，但把两个块
        都固定到同一左上槽位。只有在两个对象仍保持这组程序默认坐标时才
        重排；用户拖动过任一对象后坐标会偏离，加载时不会被覆盖。
        """

        image_path = document.source.image_asset_id
        if not image_path or not Path(image_path).is_file():
            return document, False
        texts = [item for item in document.objects if isinstance(item, TextObject) and item.visible and item.text.strip()]
        if not texts or not all(item.id in {"copy-a", "copy-b"} for item in texts):
            return document, False
        profile = document.profiles.get(canvas_key)
        if profile is None:
            return document, False
        effective = {
            item.id: object_for_profile(document, item.id, canvas_key)
            for item in texts
        }
        positions = {
            item.id: (
                round((effective[item.id] or item).transform.x, 3),
                round((effective[item.id] or item).transform.y, 3),
            )
            for item in texts
        }
        if not (
            positions.get("copy-a") in {(0.06, 0.08), (0.16, 0.18)}
            and positions.get("copy-b") in {(0.06, 0.16), (0.06, 0.27), (0.16, 0.33)}
        ):
            return document, False
        draft = CoverDraft.from_document(document)
        text_x, text_y = self.suggest_text_position(image_path, draft, canvas_key=canvas_key)
        has_a = any(item.copy_role == "A" for item in texts)
        overrides = dict(profile.overrides)
        changed = False
        for item in texts:
            target_y = max(0.16, text_y + 0.18) if item.copy_role == "B" and has_a else max(0.04, text_y)
            updated = replace(item, transform=replace(item.transform, x=text_x, y=target_y))
            if updated.transform != item.transform:
                changed = True
            override = dict(overrides.get(item.id) or {})
            override["transform"] = updated.transform.to_payload()
            override["rect"] = updated.rect.to_payload()
            override["wrap"] = updated.wrap.to_payload()
            overrides[item.id] = override
        if not changed:
            return document, False
        updated_profile = replace(profile, overrides=overrides)
        updated_document = replace(document, profiles={**document.profiles, canvas_key: updated_profile})
        # profile override 是 renderer 的真实输入；对象本身也同步，使画布
        # 命中测试和下一次保存看到同一坐标。
        updated_objects = tuple(
            replace(item, transform=replace(item.transform, x=text_x, y=(max(0.16, text_y + 0.18) if item.copy_role == "B" and has_a else max(0.04, text_y))))
            if isinstance(item, TextObject) and item.id in positions else item
            for item in document.objects
        )
        return replace(updated_document, objects=updated_objects), True

    @staticmethod
    def _upgrade_auto_copy_structure(
        document: CoverDocument, source_title: str,
    ) -> tuple[CoverDocument, bool]:
        """把旧的整句自动标题升级为独立 A/B；手动改过的文字保持原样。"""

        texts = [item for item in document.objects if isinstance(item, TextObject)]
        if len(texts) != 1:
            return document, False
        old = texts[0]
        normalized_old = " ".join(part.strip() for part in old.text.splitlines() if part.strip())
        normalized_source = " ".join(str(source_title).split())
        if old.id not in {"title-main", "title"}:
            return document, False
        generated = {
            " ".join(part.strip() for part in candidate.text.splitlines() if part.strip())
            for candidate in generate_basic_copy_variants(source_title, limit=4)
        }
        if normalized_old not in {normalized_source, source_title.strip(), *generated}:
            return document, False
        candidate = generate_basic_copy_variants(source_title, limit=1)
        if not candidate:
            return document, False
        upgraded = document_from_basic_title_values(
            title=source_title,
            image_path=(
                next((item.asset.path for item in document.objects if isinstance(item, BackgroundObject) and item.asset), None)
            ),
            selected_timestamp=document.source.selected_timestamp,
            background_x=next((item.pan_x for item in document.objects if isinstance(item, BackgroundObject)), 0.5),
            background_y=next((item.pan_y for item in document.objects if isinstance(item, BackgroundObject)), 0.5),
            background_scale=next((item.scale for item in document.objects if isinstance(item, BackgroundObject)), 1.0),
            font_size=old.style.font_size,
        )
        # 旧稿的底图对象和样式迁移到新文档，位置由保守 A/B 默认值接管；
        # profile override 不复制 title-main，避免旧大框再次覆盖新布局。
        upgraded = replace(
            upgraded,
            source=document.source,
            objects=tuple(
                replace(item, asset=next((base.asset for base in document.objects if isinstance(base, BackgroundObject)), item.asset))
                if isinstance(item, BackgroundObject) else item
                for item in upgraded.objects
            ),
            active_profile=document.active_profile if document.active_profile in upgraded.profiles else "4x3",
        )
        return upgraded, True

    @staticmethod
    def _compact_legacy_default_copy(
        document: CoverDocument,
        fallback_title: str,
        source_title: str,
    ) -> tuple[CoverDocument, bool]:
        """收紧早期自动生成的长 A/B 拼接，保留用户手动文案。"""

        if len(fallback_title) >= 22:
            return document, False
        text = next((item for item in document.objects if isinstance(item, TextObject)), None)
        if text is None:
            return document, False
        parts = [part.strip() for part in text.text.splitlines() if part.strip()]
        # 只有两行都能在原投稿题中找到，才认定是旧版自动 A/B 结果；
        # 任意手动改写过的内容继续原样保留。
        auto_copy = (
            len(parts) == 2
            and len(text.text) > 22
            and all(part in source_title for part in parts)
        )
        expected_width = min(0.72, max(0.22, len(fallback_title) * text.style.font_size / 1440.0 * 0.82))
        expected_height = max(0.08, text.style.font_size * 1.18 / 1080.0)
        stale_rect = (
            text.text == fallback_title
            and (
                text.rect.width > expected_width + 0.08
                or text.rect.height > expected_height + 0.08
            )
        )
        if not auto_copy and not stale_rect:
            return document, False
        font_size = max(24, int(text.style.font_size))
        width = min(0.72, max(0.22, len(fallback_title) * font_size / 1440.0 * 0.82))
        rect = Rect(width=width, height=max(0.08, font_size * 1.18 / 1080.0))
        updated = replace(text, text=fallback_title, rect=rect)
        profiles = {}
        for key, profile in document.profiles.items():
            override = profile.overrides.get(text.id)
            if isinstance(override, dict):
                override = {**override, "rect": rect.to_payload()}
                profile = replace(
                    profile,
                    overrides={**profile.overrides, text.id: override},
                )
            profiles[key] = profile
        return replace(
            document,
            objects=tuple(updated if item.id == text.id else item for item in document.objects),
            profiles=profiles,
        ), True

    def load(self, project: SubmissionProject, video: ProjectVideo) -> tuple[CoverDraft, DraftRead]:
        document, read = self.load_document(project, video)
        draft = CoverDraft.from_document(document)
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
        return self.save_document(project, video, draft.to_document())

    def save_document(
        self, project: SubmissionProject, video: ProjectVideo, document: CoverDocument,
    ) -> Path:
        if not isinstance(document, CoverDocument):
            raise TypeError("封面保存需要 CoverDocument")
        return self.storage.save_draft(
            "cover", project.directory, video.path, document.to_payload(),
        )

    def extract_frame(self, video: ProjectVideo, timestamp: float) -> tuple[Path, float]:
        candidate, _metadata = extract_frame_at_timestamp(
            video.path,
            timestamp,
            cache_dir=self.storage.thumbnails / "cover-frames",
        )
        return Path(candidate.path), candidate.timestamp

    def extract_nearby_frames(
        self,
        video: ProjectVideo,
        center: float,
        offsets: tuple[float, ...],
    ) -> tuple[tuple[Path, float], ...]:
        """顺序提取当前时刻附近帧，供后台缩略条使用。"""

        frames: list[tuple[Path, float]] = []
        seen: set[int] = set()
        for offset in offsets:
            timestamp = max(0.0, float(center) + float(offset))
            key = round(timestamp * 1000)
            if key in seen:
                continue
            seen.add(key)
            frames.append(self.extract_frame(video, timestamp))
        return tuple(frames)

    def extract_wider_frames(
        self,
        video: ProjectVideo,
        center: float,
        *,
        span: float = 12.0,
        count: int = 13,
    ) -> tuple[tuple[Path, float], ...]:
        """显式“寻找更多画面”入口；只按需采样，不在切项目时阻塞。"""

        count = max(3, min(31, int(count)))
        span = max(1.0, min(180.0, float(span)))
        step = (span * 2.0) / (count - 1)
        offsets = tuple(-span + index * step for index in range(count))
        return self.extract_nearby_frames(video, center, offsets)

    @staticmethod
    def set_frame_locked(document: CoverDocument, locked: bool) -> CoverDocument:
        return replace(document, source=replace(document.source, frame_locked=bool(locked)))

    @staticmethod
    def frame_is_locked(document: CoverDocument) -> bool:
        return bool(document.source.frame_locked)

    def ai_candidates(self, document: CoverDocument, *, profile_key: str = "4x3") -> tuple[CoverAICandidate, ...]:
        """显式入口；真实 relay 未配置时只返回可编辑 mock 候选。"""

        return self.ai.suggest(document, profile_key=profile_key)

    def suggest_text_position(
        self,
        image_path: str | Path,
        draft: CoverDraft,
        *,
        canvas_key: str = "4x3",
    ) -> tuple[float, float]:
        """在少量构图槽位中选择较清爽的文字区域，给后续主体识别留接口。"""

        canvas_width, canvas_height = {
            "4x3": (1440, 1080),
            "16x9": (1920, 1080),
        }.get(canvas_key, (1440, 1080))
        # 与旧版 _text_area/_edge_positions 同一思路：先准备左、右、上、下
        # 四个槽位，再根据画面边缘能量选锚点。槽位只负责给 A/B 一个
        # 可用区域，真实字体 fit 在 renderer 内完成。
        width = 0.72 if canvas_key == "4x3" else 0.66
        side_width = 0.48 if canvas_key == "4x3" else 0.44
        height = 0.32
        slots = (
            (0.06, 0.14, side_width, "left"),
            (max(0.06, 0.94 - side_width), 0.14, side_width, "right"),
            (0.08, 0.05, width, "top"),
            (0.08, max(0.58, 0.92 - height), width, "bottom"),
        )
        try:
            with Image.open(image_path) as source:
                preview = ImageOps.fit(
                    source.convert("RGB"),
                    (360, 270),
                    method=Image.Resampling.LANCZOS,
                    centering=(draft.background_x, draft.background_y),
                )
                edges = preview.convert("L").filter(ImageFilter.FIND_EDGES)
                scored: list[tuple[float, float, float, str]] = []
                for x, y, slot_width, slot_name in slots:
                    box = (
                        max(0, int(x * edges.width)),
                        max(0, int(y * edges.height)),
                        min(edges.width, int((x + slot_width) * edges.width)),
                        min(edges.height, int((y + height) * edges.height)),
                    )
                    region = edges.crop(box)
                    energy = (
                        ImageStat.Stat(region).mean[0]
                        if region.width and region.height
                        else 255.0
                    )
                    scored.append((energy, x, y, slot_name))
                # 中央高显著区域按“人物/证据主体”处理，文字优先放上缘或下缘；
                # 左右空区仍保留给侧边主体和旧版已验证的构图。
                center = edges.crop((int(edges.width * 0.28), int(edges.height * 0.16), int(edges.width * 0.72), int(edges.height * 0.78)))
                center_energy = ImageStat.Stat(center).mean[0] if center.width and center.height else 0.0
                left_context = edges.crop((int(edges.width * 0.05), int(edges.height * 0.16), int(edges.width * 0.25), int(edges.height * 0.78)))
                right_context = edges.crop((int(edges.width * 0.75), int(edges.height * 0.16), int(edges.width * 0.95), int(edges.height * 0.78)))
                side_context_energy = max(
                    ImageStat.Stat(left_context).mean[0] if left_context.width and left_context.height else 255.0,
                    ImageStat.Stat(right_context).mean[0] if right_context.width and right_context.height else 255.0,
                )
                side_scores = [item for item in scored if item[3] in {"left", "right"}]
                edge_scores = [item for item in scored if item[3] in {"top", "bottom"}]
                if center_energy > max(12.0, side_context_energy * 1.12):
                    # 中心主体优先上缘；若上缘纹理明显更复杂，再退到下缘。
                    top = [item for item in edge_scores if item[3] == "top"]
                    bottom = [item for item in edge_scores if item[3] == "bottom"]
                    candidates = top + bottom
                    if top and bottom and top[0][0] > bottom[0][0] * 1.25:
                        candidates = bottom
                else:
                    candidates = side_scores or edge_scores
                _score, x, y, _slot = min(candidates or scored, key=lambda item: item[0])
                return x, y
        except (OSError, ValueError):
            return draft.text_x, draft.text_y

    def render_preview(
        self,
        video: ProjectVideo,
        draft: CoverDraft,
        *,
        canvas_key: str = "4x3",
    ) -> Path:
        if not draft.image_path or not Path(draft.image_path).is_file():
            raise ValueError("请先加载底图或从当前视频取帧")
        self.previews.mkdir(parents=True, exist_ok=True)
        if canvas_key not in {"4x3", "16x9"}:
            raise ValueError(f"不支持的封面比例：{canvas_key}")
        suffix = "" if canvas_key == "4x3" else "-16x9"
        output = self.previews / f"{self._identity(Path(video.path))}{suffix}-preview.jpg"
        canvas_width = 1440 if canvas_key == "4x3" else 1920
        lines = wrap_cover_title(draft.title, draft.font_size, canvas_width=canvas_width)
        render_cover(
            draft.image_path,
            draft.title,
            output,
            video_path=video.path,
            canvas_key=canvas_key,
            template_key="headline",
            copy_lines=lines,
            text_transforms=text_transforms_for(draft, lines),
            focus_x=draft.background_x,
            focus_y=draft.background_y,
            background_scale=draft.background_scale,
        )
        return output

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

    def render_check_preview(self, video: ProjectVideo, draft: CoverDraft) -> Path:
        """保留旧调用方的 16:9 检查图接口。"""

        if not draft.image_path or not Path(draft.image_path).is_file():
            raise ValueError("请先加载底图或从当前视频取帧")
        self.previews.mkdir(parents=True, exist_ok=True)
        output = self.previews / f"{self._identity(Path(video.path))}-16x9-check.jpg"
        lines = wrap_cover_title(draft.title, draft.font_size, canvas_width=1920)
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

    def render_check_preview_document(self, video: ProjectVideo, document: CoverDocument) -> Path:
        self.previews.mkdir(parents=True, exist_ok=True)
        output = self.previews / f"{self._identity(Path(video.path))}-16x9-document-check.jpg"
        self._render_document(document, video, output, canvas_key="16x9")
        return output

    @staticmethod
    def _document_text_inputs(document: CoverDocument, canvas_key: str) -> tuple[TextObject | None, tuple[str, ...], tuple[TextTransform, ...]]:
        text = next((item for item in document.objects if isinstance(item, TextObject)), None)
        if text is None or not text.visible:
            return None, (), ()
        effective = object_for_profile(document, text.id, canvas_key)
        if not isinstance(effective, TextObject):
            effective = text
        canvas_width, canvas_height = {"4x3": (1440, 1080), "16x9": (1920, 1080)}.get(canvas_key, (1440, 1080))
        lines = wrap_text_lines(
            effective.text,
            effective.style.font_size,
            max_width=effective.wrap.max_width,
            max_lines=effective.wrap.max_lines,
            canvas_width=canvas_width,
        )
        step = effective.style.font_size * effective.style.line_spacing / canvas_height
        transforms = tuple(
            TextTransform(
                effective.transform.x,
                min(1.0, effective.transform.y + index * step),
                scale=effective.transform.scale,
                font_size=effective.style.font_size,
            )
            for index in range(len(lines))
        )
        return effective, lines, transforms

    @staticmethod
    def _document_text_blocks(document: CoverDocument, canvas_key: str) -> tuple[dict[str, object], ...]:
        """把每个 A/B TextObject 转成 renderer 的独立文字块输入。"""

        canvas_width, canvas_height = {"4x3": (1440, 1080), "16x9": (1920, 1080)}.get(canvas_key, (1440, 1080))
        blocks: list[dict[str, object]] = []
        texts = sorted(
            (item for item in document.objects if isinstance(item, TextObject)),
            key=lambda item: (item.z_index, item.copy_role != "A", item.id),
        )
        for base in texts:
            effective = object_for_profile(document, base.id, canvas_key)
            text = effective if isinstance(effective, TextObject) else base
            if not text.visible or not text.text.strip():
                continue
            # 旧版 renderer 会在真实字体 bbox 上先 fit，再决定 1 行或 2 行。
            # 新版不要在服务层按近似字符数提前切断，否则会把“男模”之类的
            # 短尾行固定保存下来。rect 现在只表示该对象的可用区域，具体
            # 断行和字号搜索交给 renderer 的 _fit_text_block。
            # 与 Qt CoverCanvas._display_rect 保持相同的 profile 变换语义。
            # 旧 renderer 忽略了 transform.scale，AI 候选放大后画布和预览
            # 会出现明显分叉。
            transform_scale = max(0.01, float(text.transform.scale or 1.0))
            display_width = min(text.rect.width * transform_scale, text.wrap.max_width)
            display_height = text.rect.height * transform_scale
            area = (
                max(0.0, min(0.96, float(text.transform.x))),
                max(0.0, min(0.94, float(text.transform.y))),
                max(0.04, min(1.0, float(text.transform.x) + display_width)),
                max(0.04, min(1.0, float(text.transform.y) + display_height)),
            )
            role = "context" if text.copy_role == "A" else "emphasis"
            style = replace(text.style, align=text.align)
            if role == "context" and style.stroke_width >= 6 and style.font_weight >= 700:
                # 旧版 context 使用同一色板但弱一档描边；保留用户已改过的
                # 字号、填充和位置，只把程序默认的过重描边收回历史比例。
                style = replace(style, stroke_width=4)
            font_resolution = resolve_font_selection(text.style.font_family)
            font_path = str(font_resolution.path) if font_resolution.path is not None else None
            blocks.append({
                "text": text.text,
                "text_area": area,
                "text_role": role,
                "text_transforms": None,
                "text_style": style,
                "font_path": font_path,
                # 保留实际解析结果，便于诊断 Qt/Pillow 字体分叉；renderer
                # 仍只消费 font_path，避免把调试字段混入绘制契约。
                "resolved_font": font_resolution.to_debug_dict(),
                "rotation": text.transform.rotation,
            })
        return tuple(blocks)

    @staticmethod
    def _document_overlays(
        document: CoverDocument, canvas_key: str,
    ) -> tuple[tuple[StickerOverlay, ...], tuple[ShapeOverlay, ...]]:
        """把图片/贴纸/强调框转换为渲染器的轻量覆盖层。"""

        stickers: list[StickerOverlay] = []
        shapes: list[ShapeOverlay] = []
        for base in sorted(document.objects, key=lambda item: item.z_index):
            item = object_for_profile(document, base.id, canvas_key)
            if item is None or not item.visible:
                continue
            if isinstance(item, (ImageObject, StickerObject)):
                path = item.asset.path if item.asset else None
                if not path or not Path(path).is_file():
                    continue
                width = 0.24 * max(0.05, min(4.0, item.transform.scale))
                stickers.append(StickerOverlay(
                    asset_id=item.asset.asset_id or item.id,
                    image_path=path,
                    x=item.transform.x,
                    y=item.transform.y,
                    width=min(0.80, width),
                    rotation=item.transform.rotation,
                ))
            elif isinstance(item, ShapeObject):
                shapes.append(ShapeOverlay(
                    shape_id=item.id,
                    shape_type=item.shape_type,
                    x=item.transform.x,
                    y=item.transform.y,
                    width=item.width * max(0.05, item.transform.scale),
                    height=item.height * max(0.05, item.transform.scale),
                    stroke=item.stroke,
                    stroke_width=item.stroke_width,
                    fill=item.fill,
                    rotation=item.transform.rotation,
                ))
        return tuple(stickers), tuple(shapes)

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
        background = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
        if background is None:
            raise ValueError("封面文档缺少底图对象")
        effective_background = object_for_profile(document, background.id, canvas_key)
        if not isinstance(effective_background, BackgroundObject):
            effective_background = background
        image_path = effective_background.asset.path if effective_background.asset else None
        if not image_path or not Path(image_path).is_file():
            raise ValueError("请先加载底图或从当前视频取帧")
        text_blocks = self._document_text_blocks(document, canvas_key)
        stickers, shapes = self._document_overlays(document, canvas_key)
        render_cover(
            image_path,
            "",
            output,
            video_path=video.path,
            canvas_key=canvas_key,
            template_key="headline",
            palette_key="latest_yellow",
            text_blocks=text_blocks,
            stickers=stickers,
            shapes=shapes,
            focus_x=effective_background.pan_x,
            focus_y=effective_background.pan_y,
            background_scale=effective_background.scale,
            background_fit_mode=effective_background.fit_mode,
        )

    def export(
        self,
        project: SubmissionProject,
        video: ProjectVideo,
        draft: CoverDraft,
        *,
        canvas_key: str = "4x3",
    ) -> Path:
        if not draft.image_path or not Path(draft.image_path).is_file():
            raise ValueError("请先加载底图或从当前视频取帧")
        if canvas_key not in {"4x3", "16x9"}:
            raise ValueError(f"不支持的封面比例：{canvas_key}")
        stem = Path(video.name).stem or "封面"
        suffix = "" if canvas_key == "4x3" else "-16x9"
        destination = Path(project.directory) / f"AutoCover-{stem}{suffix}.jpg"
        index = 2
        while destination.exists():
            destination = Path(project.directory) / f"AutoCover-{stem}{suffix} ({index}).jpg"
            index += 1
        canvas_width = 1440 if canvas_key == "4x3" else 1920
        lines = wrap_cover_title(draft.title, draft.font_size, canvas_width=canvas_width)
        render_cover(
            draft.image_path,
            draft.title,
            destination,
            video_path=video.path,
            canvas_key=canvas_key,
            template_key="headline",
            copy_lines=lines,
            text_transforms=text_transforms_for(draft, lines),
            focus_x=draft.background_x,
            focus_y=draft.background_y,
            background_scale=draft.background_scale,
        )
        self._record_export(project, video, destination, canvas_key)
        return destination

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
