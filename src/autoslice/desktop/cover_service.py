"""AutoCover 的无 UI 服务：草稿、底图缓存、取帧和导出。"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from autoslice.desktop.foundation import DesktopStorage, DraftRead
from autoslice.desktop.projects import ProjectVideo, SubmissionProject
from autoslice_cover.composition import best_crop_focus, region_cost, saliency_map
from autoslice_cover.document_layout import Box, background_box
from autoslice_cover.document_render import compose_document
from autoslice_cover.renderer import TextTransform, render_cover, save_cover_jpeg
from autoslice_cover.text_layout import wrap_text_lines
from autoslice_cover.video import (
    FrameMetrics,
    VideoMetadata,
    extract_frame_at_timestamp,
    plan_candidate_timestamps,
    probe_video,
)

from .cover_ai import CoverAIBeta, CoverAICandidate
from .cover_assets import CoverAssetLibrary
from .cover_copy import BasicCoverCopy, generate_basic_copy_variants
from .cover_layout import canvas_size, document_layers, fitted_font_size, text_layout
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
    StickerObject,
    TextObject,
    object_for_profile,
    text_override_payload,
)
from .cover_style import (
    STYLE_PRESETS,
    CoverStyleMemory,
    CoverStyleMemoryStore,
    StylePreset,
    streamer_key,
)


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


@dataclass(frozen=True)
class CoverFrame:
    """一张已取出的候选帧及其画质评分（复用旧版 AutoCover 评分）。"""

    path: Path
    timestamp: float
    score: float
    metrics: FrameMetrics | None = None

    @property
    def subtitle_risk(self) -> float:
        return self.metrics.subtitle_risk if self.metrics else 0.0


def recommended_frame(frames: tuple[CoverFrame, ...]) -> CoverFrame | None:
    """附近帧里明显更好的一张；差距不明显时不推荐，避免噪声。"""

    scored = [item for item in frames if item.metrics is not None]
    if len(scored) < 3:
        return None
    median = statistics.median(item.score for item in scored)
    best = max(scored, key=lambda item: (item.score - item.subtitle_risk * 20, -abs(item.timestamp)))
    return best if best.score - median >= 4.0 and best.subtitle_risk < 0.5 else None


# 默认文字槽位：(x, y, 宽度)；高度统一 0.32，按比例分别给出。
_TEXT_SLOTS = {
    "4x3": ((0.06, 0.14, 0.48, "left"), (0.46, 0.14, 0.48, "right"), (0.08, 0.05, 0.72, "top"), (0.08, 0.60, 0.72, "bottom")),
    "16x9": ((0.06, 0.14, 0.44, "left"), (0.50, 0.14, 0.44, "right"), (0.08, 0.05, 0.66, "top"), (0.08, 0.60, 0.66, "bottom")),
}
_TEXT_SLOT_HEIGHT = 0.32
# 上下分置布局：参考真实投稿封面，A/B 各占上下缘一条宽带。
_SPLIT_X = 0.06
_SPLIT_WIDTH = 0.88
_SPLIT_HEIGHT = 0.22
_SPLIT_TOP = 0.05
_SPLIT_BOTTOM = 0.70
_SPLIT_FONT_A = 136
_SPLIT_FONT_B = 150
# 槽位代价表里上下两条宽带的键；不参与单槽位选择。
_SPLIT_TOP_KEY = "split-top"
_SPLIT_BOTTOM_KEY = "split-bottom"
# 只有 B 时的宽带高度：两行大字。
_BAND_HEIGHT = 0.30
# 方案的初始请求字号：自动排版在槽位里只缩不放，所以先给足。
_SCHEME_FONT_A = 73
_SCHEME_FONT_B = 104
_SCHEME_FONT_BIG = 168
# “换一批”轮换的配色；第一个方案保留当前（记忆）样式。
_SCHEME_PRESETS = ("duo", "red-bar", "double", "white", "yellow-bar", "dark-bar")


@dataclass(frozen=True)
class CoverScheme:
    """一套可直接套用的本地方案：文案、排版、配色一起换；参考旧网页端“推荐排版”。"""

    key: str
    label: str
    reason: str
    document: CoverDocument


def primary_copy_ids(document: CoverDocument) -> dict[str, str]:
    """每个角色的第一个文本框是 A/B 主文案；复制或新建的文本框不参与换文案与方案。"""

    ids: dict[str, str] = {}
    for item in document.objects:
        if isinstance(item, TextObject):
            ids.setdefault(item.copy_role, item.id)
    return ids


def best_overview_frame(frames: tuple[CoverFrame, ...]) -> CoverFrame | None:
    """全片候选里画质最好、字幕风险低的一张。"""

    if not frames:
        return None
    return max(frames, key=lambda item: (item.score - item.subtitle_risk * 20, -item.timestamp))


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
        # 同一视频只探测一次时长与尺寸；取帧任务会并发调用。
        self._metadata: dict[tuple[str, int, int], VideoMetadata] = {}
        self._metadata_lock = threading.Lock()

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
        fallback = self._apply_style_memory(fallback, self.style_memory.load(streamer_key(project.title)))
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
            outer_stroke=style.outer_stroke,
            outer_stroke_width=style.outer_stroke_width,
            backdrop=style.backdrop,
        )
        self.style_memory.save(memory, streamer=streamer_key(project.title))

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
        # 每条字幕各自成段，避免多句被拼成一个文本框。
        return " / ".join(blocks)[:240]

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

    def _video_metadata(self, video: ProjectVideo) -> VideoMetadata:
        source = Path(video.path).expanduser().resolve()
        stat = source.stat()
        key = (str(source), stat.st_size, stat.st_mtime_ns)
        with self._metadata_lock:
            cached = self._metadata.get(key)
        if cached is None:
            cached = probe_video(source)
            with self._metadata_lock:
                self._metadata[key] = cached
        return cached

    def extract_frame_candidate(self, video: ProjectVideo, timestamp: float) -> CoverFrame:
        metadata = self._video_metadata(video)
        candidate, _metadata = extract_frame_at_timestamp(
            video.path,
            min(max(0.0, float(timestamp)), metadata.duration),
            cache_dir=self.storage.thumbnails / "cover-frames",
            metadata=metadata,
        )
        return CoverFrame(Path(candidate.path), candidate.timestamp, candidate.score, candidate.metrics)

    def extract_frame(self, video: ProjectVideo, timestamp: float) -> tuple[Path, float]:
        frame = self.extract_frame_candidate(video, timestamp)
        return frame.path, frame.timestamp

    def nearby_candidates(
        self,
        video: ProjectVideo,
        center: float,
        offsets: tuple[float, ...],
    ) -> tuple[CoverFrame, ...]:
        """顺序提取当前时刻附近帧并评分，供后台缩略条使用。"""

        frames: list[CoverFrame] = []
        seen: set[int] = set()
        for offset in offsets:
            timestamp = max(0.0, float(center) + float(offset))
            key = round(timestamp * 1000)
            if key in seen:
                continue
            seen.add(key)
            frames.append(self.extract_frame_candidate(video, timestamp))
        return tuple(frames)

    def extract_nearby_frames(
        self,
        video: ProjectVideo,
        center: float,
        offsets: tuple[float, ...],
    ) -> tuple[tuple[Path, float], ...]:
        return tuple((frame.path, frame.timestamp) for frame in self.nearby_candidates(video, center, offsets))

    @staticmethod
    def _wider_offsets(span: float, count: int) -> tuple[float, ...]:
        count = max(3, min(31, int(count)))
        span = max(1.0, min(180.0, float(span)))
        step = (span * 2.0) / (count - 1)
        return tuple(-span + index * step for index in range(count))

    def wider_candidates(
        self, video: ProjectVideo, center: float, *, span: float = 12.0, count: int = 13,
    ) -> tuple[CoverFrame, ...]:
        """显式“寻找更多画面”入口；只按需采样，不在切项目时阻塞。"""

        return self.nearby_candidates(video, center, self._wider_offsets(span, count))

    def extract_wider_frames(
        self,
        video: ProjectVideo,
        center: float,
        *,
        span: float = 12.0,
        count: int = 13,
    ) -> tuple[tuple[Path, float], ...]:
        return tuple(
            (frame.path, frame.timestamp)
            for frame in self.wider_candidates(video, center, span=span, count=count)
        )

    @staticmethod
    def set_frame_locked(document: CoverDocument, locked: bool) -> CoverDocument:
        return replace(document, source=replace(document.source, frame_locked=bool(locked)))

    @staticmethod
    def frame_is_locked(document: CoverDocument) -> bool:
        return bool(document.source.frame_locked)

    def ai_candidates(self, document: CoverDocument, *, profile_key: str = "4x3") -> tuple[CoverAICandidate, ...]:
        """显式入口；真实 relay 未配置时只返回可编辑 mock 候选。"""

        return self.ai.suggest(document, profile_key=profile_key)

    @staticmethod
    def _text_slot_costs(
        image_path: str | Path,
        canvas_key: str,
        *,
        focus_x: float,
        focus_y: float,
        scale: float,
    ) -> dict[str, tuple[float, float, float]] | None:
        """各文字槽位的代价：盖住人脸 > 杂乱细节 > 字幕带；返回 {槽位: (代价, x, y)}。"""

        saliency = saliency_map(image_path)
        if saliency is None:
            return None
        try:
            with Image.open(image_path) as source:
                source_size = source.size
        except OSError:
            return None
        canvas = canvas_size(canvas_key)
        frame = background_box(source_size, canvas, scale=scale, focus_x=focus_x, focus_y=focus_y)
        scored: dict[str, tuple[float, float, float]] = {}
        for order, (x, y, width, name) in enumerate(_TEXT_SLOTS.get(canvas_key, _TEXT_SLOTS["4x3"])):
            # 画布槽位换算到源图归一化坐标，与当前取景一致。
            box = (
                (x * canvas[0] - frame.left) / frame.width,
                (y * canvas[1] - frame.top) / frame.height,
                ((x + width) * canvas[0] - frame.left) / frame.width,
                ((y + _TEXT_SLOT_HEIGHT) * canvas[1] - frame.top) / frame.height,
            )
            scored[name] = (region_cost(saliency, box) + order * 0.002, x, y)
        # 上下分置的两条宽带，与单槽位比较；不参与单槽位选择。
        for name, top in ((_SPLIT_TOP_KEY, _SPLIT_TOP), (_SPLIT_BOTTOM_KEY, _SPLIT_BOTTOM)):
            scored[name] = (region_cost(saliency, (
                (_SPLIT_X * canvas[0] - frame.left) / frame.width,
                (top * canvas[1] - frame.top) / frame.height,
                ((_SPLIT_X + _SPLIT_WIDTH) * canvas[0] - frame.left) / frame.width,
                ((top + _SPLIT_HEIGHT) * canvas[1] - frame.top) / frame.height,
            )), _SPLIT_X, top)
        return scored

    @classmethod
    def _best_text_slot(
        cls,
        image_path: str | Path,
        canvas_key: str,
        *,
        focus_x: float,
        focus_y: float,
        scale: float,
    ) -> tuple[float, float] | None:
        costs = cls._text_slot_costs(image_path, canvas_key, focus_x=focus_x, focus_y=focus_y, scale=scale)
        if not costs:
            return None
        _cost, x, y = min(value for name, value in costs.items() if name not in {_SPLIT_TOP_KEY, _SPLIT_BOTTOM_KEY})
        return x, y

    def suggest_text_position(
        self,
        image_path: str | Path,
        draft: CoverDraft,
        *,
        canvas_key: str = "4x3",
    ) -> tuple[float, float]:
        """在少量构图槽位中选择较清爽的文字区域。"""

        position = self._best_text_slot(
            image_path, canvas_key,
            focus_x=draft.background_x, focus_y=draft.background_y, scale=draft.background_scale,
        )
        return position if position is not None else (draft.text_x, draft.text_y)

    @staticmethod
    def warm_composition(image_path: str | Path) -> None:
        saliency_map(image_path)

    def apply_auto_layout(
        self, document: CoverDocument, image_path: str | Path, *, mode: str = "auto",
    ) -> CoverDocument:
        """新底图的默认构图：两个比例分别保住主体、给 A/B 找空区。

        mode="split" 强制上下分置（A 上缘、B 下缘；只有 B 时占代价更低的一条宽带），
        mode="slot" 强制放进最空的单侧槽位。
        只排 A/B 主文案；用户新建或复制的文本框不动。
        """

        saliency = saliency_map(image_path)
        background = next((item for item in document.objects if isinstance(item, BackgroundObject)), None)
        primary = set(primary_copy_ids(document).values())
        texts = [item for item in document.objects if isinstance(item, TextObject) and item.id in primary]
        has_context = any(item.copy_role == "A" and item.visible and item.text.strip() for item in texts)
        profiles = dict(document.profiles)
        # 4:3 是主画布：先定 4:3 的布局方式，16:9 跟随上下分置，避免另一比例压脸。
        main_split = False
        ordered = sorted(document.profiles.items(), key=lambda item: item[0] != "4x3")
        for key, profile in ordered:
            overrides = dict(profile.overrides)
            focus_x, focus_y, scale = 0.5, 0.5, 1.0
            if background is not None:
                current = object_for_profile(document, background.id, key)
                current = current if isinstance(current, BackgroundObject) else background
                if saliency is not None and current.fit_mode == "cover":
                    focus_x, focus_y = best_crop_focus(saliency, profile.width / max(1, profile.height))
                scale = current.scale if saliency is None else 1.0
                updated = replace(current, pan_x=focus_x, pan_y=focus_y, scale=scale)
                overrides[background.id] = {
                    "transform": updated.transform.to_payload(),
                    "visible": bool(updated.visible),
                    "scale": updated.scale,
                    "pan_x": updated.pan_x,
                    "pan_y": updated.pan_y,
                    "fit_mode": updated.fit_mode,
                }
            costs = self._text_slot_costs(image_path, key, focus_x=focus_x, focus_y=focus_y, scale=scale)
            bands = (costs.pop(_SPLIT_TOP_KEY)[0], costs.pop(_SPLIT_BOTTOM_KEY)[0]) if costs else None
            best = min(costs.items(), key=lambda item: item[1][0]) if costs else None
            # 人物居中（左右代价接近）时上下宽带通常比单侧槽位更空：代价不更高就用宽带；
            # 人物偏一侧（左右代价悬殊）时文字去另一侧。有 A 比两条宽带的平均，只有 B 比更空的一条。
            band_cost = (sum(bands) / 2 if has_context else min(bands)) if bands else None
            sides = sorted(costs[name][0] for name in ("left", "right")) if costs else None
            one_sided = sides is not None and sides[0] < sides[1] * 0.6
            split = mode == "split" or mode == "auto" and main_split or mode == "auto" and (
                best is not None
                and (
                    band_cost <= best[1][0] + 0.02 and not one_sided
                    or has_context
                    and best[0] in {"top", "bottom"}
                    and costs["top" if best[0] == "bottom" else "bottom"][0] <= best[1][0] * 1.5 + 0.05
                )
            )
            if key == "4x3":
                main_split = split
            canvas_width, canvas_height = canvas_size(key)

            def place(item: TextObject, x: float, y: float, width: float, height: float, *, requested: int, max_lines: int, align: str | None = None) -> TextObject:
                # 字号在槽位内拟合一次后固定下来；之后编辑器按固定字号换行。
                shaped = replace(item, align=align or item.align, transform=replace(item.transform, x=x, y=y, scale=1.0))
                area = Box(x * canvas_width, y * canvas_height, width * canvas_width, height * canvas_height)
                size = fitted_font_size(shaped, area, requested=requested, max_lines=max_lines) if item.text.strip() else requested
                return replace(
                    shaped,
                    rect=Rect(width=width, height=height),
                    wrap=replace(item.wrap, max_width=width, max_lines=8),
                    style=replace(item.style, font_size=size),
                )

            def text_height(item: TextObject) -> float:
                if not item.text.strip():
                    return 0.0
                return text_layout(item, (canvas_width, canvas_height)).area.height / canvas_height

            current_texts = [
                (text, current if isinstance(current := object_for_profile(document, text.id, key), TextObject) else text)
                for text in texts
            ]
            if split:
                # 上下分置：A 放上缘单行、B 放下缘，居中大字；只有 B 时占代价更低的一条宽带。
                lone = not has_context
                band_top = lone and bands is not None and bands[0] < bands[1]
                height = _BAND_HEIGHT if lone else _SPLIT_HEIGHT
                bottom_edge = 1.0 - _SPLIT_TOP if lone else _SPLIT_BOTTOM + _SPLIT_HEIGHT
                for text, current in current_texts:
                    top = text.copy_role == "A"
                    if top and lone:
                        continue
                    y = _SPLIT_TOP if top or band_top else bottom_edge - height
                    updated = place(
                        current, _SPLIT_X, y, _SPLIT_WIDTH, height,
                        requested=max(current.style.font_size, _SPLIT_FONT_A if top else _SPLIT_FONT_B),
                        max_lines=1 if top else 2, align="center",
                    )
                    if not (top or band_top):
                        # 下缘贴底：行数少时整体下移，不悬在画面中部。
                        settled = max(y, bottom_edge - text_height(updated))
                        updated = replace(updated, transform=replace(updated.transform, y=settled))
                    overrides[text.id] = text_override_payload(updated)
            elif best is not None:
                _cost, text_x, text_y = best[1]
                slot_width = next(width for _x, _y, width, name in _TEXT_SLOTS.get(key, _TEXT_SLOTS["4x3"]) if name == best[0])
                # A/B 作为同一槽位的上下两块：A 在上，B 紧跟 A 的实际高度往下排。
                cursor = max(0.04, text_y)
                for text, current in sorted(current_texts, key=lambda pair: pair[0].copy_role != "A"):
                    context = text.copy_role == "A"
                    if context and not (has_context and current.visible):
                        continue
                    updated = place(
                        current, text_x, cursor, slot_width, 0.14 if context else 0.30,
                        requested=current.style.font_size, max_lines=1 if context else 2,
                    )
                    overrides[text.id] = text_override_payload(updated)
                    cursor += text_height(updated) + (0.025 if context else 0.0)
            profiles[key] = replace(profile, overrides=overrides)
        return replace(document, profiles=profiles)

    def overview_candidates(self, video: ProjectVideo, *, count: int = 8) -> tuple[CoverFrame, ...]:
        """全片均匀候选（避开片头片尾），没有播放位置时据此自动挑首帧。"""

        metadata = self._video_metadata(video)
        timestamps = plan_candidate_timestamps(metadata.duration, count)
        with ThreadPoolExecutor(max_workers=4) as pool:
            frames = list(pool.map(lambda value: self.extract_frame_candidate(video, value), timestamps))
        return tuple(sorted(frames, key=lambda item: item.timestamp))

    @staticmethod
    def _seed_copy(
        document: CoverDocument, copy: BasicCoverCopy, preset: StylePreset | None, *, big: bool,
    ) -> CoverDocument:
        """把一套文案和样式写回 A/B 主文案，清掉它们的比例覆盖，交给自动排版重新放置。"""

        ids = set(primary_copy_ids(document).values())
        objects = []
        for item in document.objects:
            if isinstance(item, TextObject) and item.id in ids:
                value = (copy.context if item.copy_role == "A" else copy.headline).strip()
                current = object_for_profile(document, item.id, "4x3")
                style = current.style if isinstance(current, TextObject) else item.style
                if preset is not None:
                    style = preset.apply(style, item.copy_role)
                size = (_SCHEME_FONT_BIG if big else _SCHEME_FONT_B) if item.copy_role == "B" else _SCHEME_FONT_A
                item = replace(
                    item, text=value, visible=bool(value), style=replace(style, font_size=size),
                    transform=replace(item.transform, rotation=0.0, scale=1.0),
                )
            objects.append(item)
        profiles = {
            key: replace(profile, overrides={k: v for k, v in profile.overrides.items() if k not in ids})
            for key, profile in document.profiles.items()
        }
        return replace(document, objects=tuple(objects), profiles=profiles)

    def layout_schemes(
        self,
        document: CoverDocument,
        image_path: str | Path,
        variants: tuple[BasicCoverCopy, ...],
        *,
        batch: int = 0,
    ) -> tuple[CoverScheme, ...]:
        """三套方案：推荐 / 只留大字 / 换文案换配色；“换一批”轮换文案和配色。"""

        if not variants:
            ids = primary_copy_ids(document)
            texts = {
                role: item.text if isinstance(item := object_for_profile(document, object_id, "4x3"), TextObject) else ""
                for role, object_id in ids.items()
            }
            variants = (BasicCoverCopy(context=texts.get("A", ""), headline=texts.get("B", "")),)
        first = variants[batch % len(variants)]
        second = variants[(batch + 1) % len(variants)]
        presets = {preset.key: preset for preset in STYLE_PRESETS}
        preset = presets[_SCHEME_PRESETS[batch % len(_SCHEME_PRESETS)]]

        def build(copy: BasicCoverCopy, style: StylePreset | None, *, big: bool = False, mode: str = "auto") -> CoverDocument:
            return self.apply_auto_layout(self._seed_copy(document, copy, style, big=big), image_path, mode=mode)

        recommended = build(first, None)
        # 第二套换一种构图：推荐是宽带就给单侧或大字，推荐是单侧就给上下分置。
        if first.context.strip():
            middle = CoverScheme("split", "上下分置", "A 放上缘、B 放下缘，人物留在中间", build(first, None, mode="split"))
            if middle.document == recommended:
                middle = CoverScheme(
                    "headline", "大字", "只留主文案放大成一条宽带，首页小图也看得清",
                    build(replace(first, context=""), None, big=True, mode="split"),
                )
        else:
            middle = CoverScheme("side", "侧边", "文字放到画面较空的一侧，人物更完整", build(first, None, mode="slot"))
            if middle.document == recommended:
                middle = CoverScheme(
                    "headline", "大字", "主文案放大成一条宽带，首页小图也看得清",
                    build(first, None, big=True, mode="split"),
                )
        return (
            CoverScheme("recommended", "推荐", "避开人物主体自动排版：A 交代背景，B 放大爆点", recommended),
            middle,
            CoverScheme("alternate", preset.label, "换一版文案并换配色", build(second, preset)),
        )

    @staticmethod
    def apply_scheme(document: CoverDocument, scheme: CoverScheme) -> CoverDocument:
        """套用方案：只替换 A/B 主文案和底图取景；用户加的素材和文本框保持不动。"""

        source = scheme.document
        ids = set(primary_copy_ids(source).values())
        background = next((item.id for item in source.objects if isinstance(item, BackgroundObject)), None)
        keys = ids | ({background} if background else set())
        replaced = {item.id: item for item in source.objects if item.id in ids}
        objects = tuple(replaced.get(item.id, item) for item in document.objects)
        profiles = {}
        for key, profile in document.profiles.items():
            overrides = {k: v for k, v in profile.overrides.items() if k not in keys}
            theirs = source.profiles.get(key)
            if theirs is not None:
                overrides.update({k: v for k, v in theirs.overrides.items() if k in keys})
            profiles[key] = replace(profile, overrides=overrides)
        return replace(document, objects=objects, profiles=profiles)

    @staticmethod
    def scheme_thumbnail(document: CoverDocument, *, canvas_key: str = "4x3", width: int = 240) -> bytes:
        """方案缩略图（JPEG 字节）；与导出同一套图层，看到的就是套用后的样子。"""

        background, layers = document_layers(document, canvas_key)
        if background is None:
            raise ValueError("方案缺少底图")
        image = compose_document(canvas_size(canvas_key), background, layers)
        try:
            image.thumbnail((width, width), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, "JPEG", quality=88)
            return buffer.getvalue()
        finally:
            image.close()

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
