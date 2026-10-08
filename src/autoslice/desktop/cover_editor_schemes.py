"""快速方案、换一版文案与同步到另一比例。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtGui import (
    QIcon,
    QPixmap,
)

from .cover_autolayout import ensure_context_text
from .cover_model import (
    TextObject,
    object_for_profile,
    set_object_visible,
    update_text_object,
)
from .cover_style import streamer_key


class CoverSchemesMixin:
    def _clear_schemes(self, caption: str = "—"):
        self._scheme_generation += 1
        self._schemes = ()
        self.scheme_title.setText("快速方案")
        self.ai_hint.setVisible(False)
        for button, label in zip(self.scheme_buttons, self.scheme_labels):
            button.setIcon(QIcon())
            button.setChecked(False)
            button.setEnabled(False)
            label.setText(caption)
        self.scheme_refresh_button.setEnabled(False)

    def _refresh_schemes(self, *, batch: int | None = None):
        """后台生成三套方案和缩略图；只出缩略图，不改当前封面。"""

        if self.document is None or not self.draft.image_path:
            self._clear_schemes()
            return
        if batch is not None:
            self._scheme_batch = max(0, int(batch))
        self._clear_schemes("生成中…")
        generation = self._scheme_generation
        document, image_path = self.document, self.draft.image_path
        variants, scheme_batch, service = self._copy_variants, self._scheme_batch, self.service
        streamer = streamer_key(self.project.title if self.project is not None else "") or ""

        def build():
            # 最近的作品：第二、三套方案优先给最近没用过的构图和配色。
            recent = service.works.recent(streamer=streamer, limit=6)
            schemes = service.layout_schemes(document, image_path, variants, batch=scheme_batch, recent=recent)
            return schemes, tuple(service.scheme_thumbnail(item.document, width=156) for item in schemes)

        self._run(build, lambda result, error: self._schemes_ready(generation, result, error))

    def _schemes_ready(self, generation: int, result, error):
        if generation != self._scheme_generation:
            return
        self.scheme_refresh_button.setEnabled(self.document is not None)
        if error or not result:
            for label in self.scheme_labels:
                label.setText("—")
            if error:
                self.status_changed.emit(f"方案生成失败：{error}")
            return
        schemes, thumbnails = result
        self._schemes = tuple(schemes)
        for index, (button, label) in enumerate(zip(self.scheme_buttons, self.scheme_labels)):
            if index >= len(self._schemes):
                continue
            scheme = self._schemes[index]
            pixmap = QPixmap()
            pixmap.loadFromData(thumbnails[index])
            button.setIcon(QIcon(pixmap))
            button.setEnabled(True)
            button.setToolTip(f"{scheme.label}：{scheme.reason}\n点击套用，Ctrl+Z 可撤销")
            label.setText(scheme.label)

    def _apply_scheme(self, index: int):
        if self.document is None or not 0 <= index < len(self._schemes):
            return
        scheme = self._schemes[index]
        before = self.document
        self.document = self.service.apply_scheme(self.document, scheme)
        for position, button in enumerate(self.scheme_buttons):
            button.setChecked(position == index)
        if self.document != before:
            self._commit_document_change(before)
            self.canvas.set_document(self.document, self._canvas_key)
            self._sync_selected_text_controls()
        self._applied_scheme, self._edits_after_scheme = scheme.key, 0
        self.status_changed.emit(f"已套用方案：{scheme.label}（Ctrl+Z 可撤销）")

    def _next_scheme_batch(self):
        self._refresh_schemes(batch=self._scheme_batch + 1)

    def _cycle_copy(self):
        if not self._copy_variants:
            return
        self._copy_variant_index = (self._copy_variant_index + 1) % len(self._copy_variants)
        candidate = self._copy_variants[self._copy_variant_index]
        if self.document is not None:
            self._replace_copy(candidate)
        else:
            self.title_edit.setPlainText(candidate.headline)
        self.status_changed.emit(
            f"已切换本地基础文案 {self._copy_variant_index + 1}/{len(self._copy_variants)}"
        )
        self._draft_timer.start()

    def _replace_copy(self, candidate):
        """只换 A/B 文字：位置、字号和行宽沿用当前排版；原封面只有 B 时先在 B 上方补一个 A 块。"""

        before = self.document
        if candidate.context.strip():
            self.document = ensure_context_text(self.document)
        primary = set(self._primary_copy_ids().values())
        for item in self.document.objects:
            if not isinstance(item, TextObject) or item.id not in primary:
                continue
            value = candidate.context if item.copy_role == "A" else candidate.headline
            current = object_for_profile(self.document, item.id, self._canvas_key)
            current = current if isinstance(current, TextObject) else item
            self.document = update_text_object(
                self.document, replace(current, text=value), profile_key=self._canvas_key,
            )
            self.document = set_object_visible(self.document, item.id, bool(value.strip()))
        if self.document != before:
            self._commit_document_change(before)

    def _sync_other_ratio(self):
        if self.document is None:
            return
        target = "16x9" if self._canvas_key == "4x3" else "4x3"
        before = self.document
        self.document = self.service.sync_profile(self.document, self._canvas_key, target)
        if self.document != before:
            self._commit_document_change(before)
        label = "16:9" if target == "16x9" else "4:3"
        self.status_changed.emit(f"已把 {self._canvas_label()} 的文字和素材位置同步到 {label}（Ctrl+Z 可撤销）")
