"""快速方案、换一版文案、模板（存为模板、套用、删除）与同步到另一比例。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from PySide6.QtGui import (
    QIcon,
    QPixmap,
)
from PySide6.QtWidgets import QInputDialog, QMessageBox

from .cover_autolayout import replace_copy
from .cover_style import streamer_key
from .cover_templates import apply_template, make_template


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
            if self.document.source.selected_timestamp != before.source.selected_timestamp:
                # AI 方案换了帧：附近帧缩略条跟到新时刻。
                timestamp = self.document.source.selected_timestamp
                self._update_nearby_frame_selection(timestamp)
                self._refresh_nearby_frame_strip(timestamp)
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
        self.document = replace_copy(self.document, candidate, self._canvas_key)
        if self.document != before:
            self._commit_document_change(before)

    def _fill_template_menu(self):
        """模板菜单：存为模板；最近用过的模板在前，点一下套用；删除放子菜单。"""

        menu = self.template_menu
        menu.clear()
        ready = self.document is not None
        menu.addAction("把这张存为模板…", self._save_template).setEnabled(ready)
        templates = self.service.templates.list()
        if templates:
            menu.addSeparator()
            for template in templates:
                menu.addAction(f"套用：{template.name}", lambda value=template: self._apply_template(value)).setEnabled(ready)
            delete = menu.addMenu("删除模板")
            for template in templates:
                delete.addAction(template.name, lambda value=template: self._delete_template(value))
        else:
            menu.addAction("还没有模板：排好一张后存为模板").setEnabled(False)

    def _save_template(self):
        if self.document is None:
            return
        streamer = streamer_key(self.project.title) if self.project is not None else None
        name, ok = QInputDialog.getText(
            self, "存为模板", "模板名（记下字的样式和位置、贴纸，不记底图和文案）：",
            text=f"{streamer}日更" if streamer else "我的模板",
        )
        if not ok or not name.strip():
            return
        template = make_template(self.document, name, frames_dir=self.service.storage.thumbnails / "cover-frames")
        self.service.templates.save(template)
        self.service.templates.mark_used(template.template_id)
        self.status_changed.emit(f"已存为模板「{template.name}」：换个视频点“模板”就能套用，批量出图也能选")

    def _apply_template(self, template):
        if self.document is None:
            return
        before = self.document
        self.document = apply_template(self.document, template, self._canvas_key)
        self.service.templates.mark_used(template.template_id)
        if self.document != before:
            self._commit_document_change(before)
            self.canvas.set_document(self.document, self._canvas_key)
            self._sync_selected_text_controls()
        self.status_changed.emit(f"已套用模板「{template.name}」（Ctrl+Z 可撤销）")

    def _delete_template(self, template):
        answer = QMessageBox.question(self, "删除模板", f"删除模板「{template.name}」？已经做好的封面不受影响。")
        if answer == QMessageBox.StandardButton.Yes:
            self.service.templates.delete(template.template_id)
            self.status_changed.emit(f"已删除模板「{template.name}」")

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
