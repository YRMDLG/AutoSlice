"""草稿读写、旧草稿升级、风格记忆与字幕上下文。"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from pathlib import Path

from autoslice.desktop.foundation import DraftRead
from autoslice.desktop.projects import ProjectVideo, SubmissionProject

from .cover_autolayout import (
    primary_copy_ids,
)
from .cover_copy import BasicCoverCopy, generate_basic_copy_variants
from .cover_draft import CoverDraft
from .cover_migration import (
    document_from_basic_title_values,
    document_from_payload,
)
from .cover_model import (
    BackgroundObject,
    CoverDocument,
    Rect,
    TextObject,
    object_for_profile,
)
from .cover_style import (
    CoverStyleMemory,
    streamer_key,
)


class CoverStoreService:
    """混入 CoverService；共享 storage、缓存与锁等状态。"""

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
        if read.status == "source_changed" and isinstance(read.payload, dict):
            # 视频重新导出过：沿用原来的文字、样式和素材，只换底图（旧帧来自旧文件）。
            try:
                document, _migrated = document_from_payload(read.payload, fallback_title)
            except (TypeError, ValueError):
                return fallback, read
            return self._detach_source_frame(document), read
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
    def _detach_source_frame(document: CoverDocument) -> CoverDocument:
        """去掉来自旧视频的底图和锁帧，保留选中时间，供从新视频同一时刻重新取帧。"""

        return replace(
            document,
            source=replace(document.source, image_asset_id=None, frame_locked=False),
            objects=tuple(
                replace(item, asset=None) if isinstance(item, BackgroundObject) else item
                for item in document.objects
            ),
        )

    @staticmethod
    def _apply_style_memory(document: CoverDocument, memory: CoverStyleMemory) -> CoverDocument:
        objects = tuple(
            replace(item, style=memory.text_style(role=item.copy_role))
            if isinstance(item, TextObject) else item
            for item in document.objects
        )
        return replace(document, objects=objects)

    def remember_style(self, project: SubmissionProject, document: CoverDocument) -> None:
        ids = primary_copy_ids(document)
        text = object_for_profile(document, ids["B"], "4x3") if "B" in ids else None
        if not isinstance(text, TextObject):
            return
        context = object_for_profile(document, ids["A"], "4x3") if "A" in ids else None
        context_style = context.style if isinstance(context, TextObject) and context.visible else None
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
            context_fill=context_style.fill_color if context_style and context_style.fill_color != style.fill_color else "",
            context_stroke=context_style.stroke_color if context_style and context_style.stroke_color != style.stroke_color else "",
            accent=style.accent,
        )
        self.style_memory.save(memory, streamer=streamer_key(project.title))

    @staticmethod
    def subtitle_context(video: ProjectVideo, timestamp: float, *, radius: float = 4.0) -> str:
        """读取当前帧附近的校对字幕文本；失败时返回空字符串。"""

        blocks = [
            text for start, end, text in CoverStoreService.subtitle_cues(video)
            if not (end < timestamp - radius or start > timestamp + radius)
        ]
        # 每条字幕各自成段，避免多句被拼成一个文本框。
        return " / ".join(blocks)[:240]

    @staticmethod
    def subtitle_cues(video: ProjectVideo) -> tuple[tuple[float, float, str], ...]:
        """整份字幕（优先校对版）：(起, 止, 文本)；读不到返回空。"""

        source = video.corrected_srt_path if video.has_corrected_srt else video.srt_path
        if not source or not Path(source).is_file():
            return ()
        try:
            raw = Path(source).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return ()
        import re
        def seconds(value: str) -> float:
            match = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
            if not match:
                return -1.0
            return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + int(match.group(4)) / 1000
        cues: list[tuple[float, float, str]] = []
        for block in re.split(r"\r?\n\s*\r?\n", raw):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            if len(lines) < 2 or "-->" not in lines[1]:
                continue
            start_raw, end_raw = [item.strip() for item in lines[1].split("-->", 1)]
            start, end = seconds(start_raw), seconds(end_raw)
            text = " ".join(lines[2:]).strip()
            if start >= 0 and end >= 0 and text:
                cues.append((start, end, text))
        return tuple(cues)

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

    @staticmethod
    def set_frame_locked(document: CoverDocument, locked: bool) -> CoverDocument:
        return replace(document, source=replace(document.source, frame_locked=bool(locked)))

    def has_draft(self, project: SubmissionProject, video: ProjectVideo) -> bool:
        return self.storage.read_draft("cover", project.directory, video.path).status == "ready"
