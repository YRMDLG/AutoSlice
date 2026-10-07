"""文字编辑：选中同步、就地打字、样式与字体、文字位置写回。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
)

from autoslice_cover.fonts import resolve_font_selection

from .cover_autolayout import primary_copy_ids
from .cover_draft import CoverDraft
from .cover_model import (
    TextObject,
    object_for_profile,
    set_object_visible,
    update_text_object,
)
from .cover_style import StylePreset


class CoverTextMixin:
    def _selected_text(self) -> TextObject | None:
        if self.document is None:
            return None
        texts = [item for item in self.document.objects if isinstance(item, TextObject)]
        selected_id = self._selected_text_id
        candidate = next((item for item in texts if item.id == selected_id), None)
        if candidate is None:
            candidate = next((item for item in texts if item.copy_role == "B"), None) or (texts[0] if texts else None)
            self._selected_text_id = candidate.id if candidate else None
        if candidate is None:
            return None
        effective = object_for_profile(self.document, candidate.id, self._canvas_key)
        return effective if isinstance(effective, TextObject) else candidate

    def _sync_selected_text_controls(self):
        text = self._selected_text()
        if text is None:
            return
        self.title_edit.blockSignals(True)
        self.font_spin.blockSignals(True)
        for widget in (self.font_path_edit, self.stroke_spin, self.outer_stroke_spin, self.line_spacing_spin, self.shadow_check):
            widget.blockSignals(True)
        try:
            self.title_edit.setPlainText(text.text)
            font_resolution = resolve_font_selection(text.style.font_family)
            selected_font_path = Path(text.style.font_family) if text.style.font_family else None
            self.font_path_edit.setText(
                str(selected_font_path.resolve())
                if selected_font_path is not None and selected_font_path.is_file()
                else ""
            )
            self.font_path_edit.setToolTip(
                f"实际字体：{font_resolution.family}"
                + (f" · {font_resolution.path}" if font_resolution.path else "")
                + (f" · {font_resolution.warning}" if font_resolution.warning else "")
            )
            self.font_spin.setValue(int(text.style.font_size))
            self.fill_color_button.set_color(text.style.fill_color)
            self.stroke_button.set_color(text.style.stroke_color)
            self.stroke_spin.setValue(int(text.style.stroke_width))
            self.outer_stroke_button.set_color(text.style.outer_stroke)
            self.outer_stroke_spin.setValue(int(text.style.outer_stroke_width))
            self.backdrop_button.set_color(text.style.backdrop)
            self.line_spacing_spin.setValue(float(text.style.line_spacing))
            self.shadow_check.setChecked(bool(text.style.shadow))
            self._update_role_label(text)
            self.lock_text_button.setChecked(bool(text.locked))
            for key, button in self.align_buttons.items():
                button.blockSignals(True)
                button.setChecked(key == text.align)
                button.blockSignals(False)
        finally:
            self.title_edit.blockSignals(False)
            self.font_spin.blockSignals(False)
            for widget in (self.font_path_edit, self.stroke_spin, self.outer_stroke_spin, self.line_spacing_spin, self.shadow_check):
                widget.blockSignals(False)

    def _select_copy_role(self, role: str):
        if self.document is None or role not in {"A", "B"}:
            return
        selected = next((item for item in self.document.objects if isinstance(item, TextObject) and item.copy_role == role), None)
        if selected is None:
            return
        self._selected_text_id = selected.id
        self.document = replace(self.document, selected_object_id=selected.id)
        self.canvas.set_selected_object(selected.id)
        self._sync_selected_text_controls()

    def _primary_copy_ids(self) -> dict[str, str]:
        """每个角色的第一个文本框是 A/B 主文案；复制或新建的不参与换一版。"""

        return primary_copy_ids(self.document) if self.document else {}

    def _update_role_label(self, text: TextObject | None) -> None:
        if text is None:
            self.copy_role_label.setText("")
        elif self._primary_copy_ids().get(text.copy_role) != text.id:
            self.copy_role_label.setText("自建文本框")
        else:
            self.copy_role_label.setText("主文案 B · 大字" if text.copy_role == "B" else "上下文 A · 小字")

    def _edit_text(self, object_id: str):
        """双击文字：就在文字旁边打字，画布实时显示效果。"""

        if self.document is None:
            return
        self._finish_inline_edit(True)
        self._canvas_object_selected(object_id)
        text = self._selected_text()
        if text is None or text.id != object_id:
            return
        current = object_for_profile(self.document, object_id, self._canvas_key)
        current = current if isinstance(current, TextObject) else text
        self._inline_target = object_id
        self._inline_original = current.text
        self.inline_edit.blockSignals(True)
        self.inline_edit.setPlainText(current.text)
        self.inline_edit.blockSignals(False)
        self._place_inline_edit()
        self.inline_edit.show()
        self.inline_edit.raise_()
        self.inline_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.inline_edit.selectAll()

    def _place_inline_edit(self):
        """输入框贴在文字框下方，放不下就放上方，始终留在画布里。"""

        target = next((item for item in self.canvas._layered_objects() if item.id == self._inline_target), None)
        if not isinstance(target, TextObject):
            return
        box = self.canvas._display_rect(target)
        bounds = self.canvas.rect().adjusted(8, 8, -8, -8)
        lines = max(1, min(4, self.inline_edit.toPlainText().count("\n") + 1))
        height = self.inline_edit.fontMetrics().lineSpacing() * lines + 18
        width = max(240, min(int(box.width()), bounds.width()))
        x = int(min(max(box.left(), bounds.left()), bounds.right() - width))
        y = int(box.bottom() + 10)
        if y + height > bounds.bottom():
            y = int(max(bounds.top(), box.top() - height - 10))
        self.inline_edit.setGeometry(x, y, width, height)

    def _inline_text_changed(self):
        if self._inline_target is None:
            return
        # 走右侧文案框的同一条写回路径：文字两比例共享、清空即隐藏、可撤销。
        self.title_edit.setPlainText(self.inline_edit.toPlainText())
        self.canvas.set_document(self.document, self._canvas_key)
        self._place_inline_edit()

    def _finish_inline_edit(self, keep: bool = True):
        if self._inline_target is None:
            return
        self._inline_target = None
        changed = self.inline_edit.toPlainText() != self._inline_original
        self.inline_edit.hide()
        if not keep and changed:
            self.title_edit.setPlainText(self._inline_original)
            self.canvas.set_document(self.document, self._canvas_key)
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _set_text_align(self, align: str):
        if align not in {"left", "center", "right"} or self.document is None:
            return
        text = self._selected_text()
        if text is None:
            return
        current = object_for_profile(self.document, text.id, self._canvas_key)
        current = current if isinstance(current, TextObject) else text
        updated = replace(current, align=align)
        profile = self.document.profiles[self._canvas_key]
        payload = {"transform": updated.transform.to_payload(), "visible": bool(updated.visible), "rect": updated.rect.to_payload(), "wrap": updated.wrap.to_payload(), "align": align, "style": updated.style.to_payload()}
        self.document = replace(self.document, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})
        self.history.commit(self.document)
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self.canvas.set_document(self.document, self._canvas_key)
        self._draft_timer.start()
        self._preview_timer.start()

    def _pick_font(self):
        source, _ = QFileDialog.getOpenFileName(
            self,
            "选择封面字体",
            "",
            "字体文件 (*.ttf *.ttc *.otf)",
        )
        if not source:
            return
        self.font_path_edit.setText(str(Path(source).resolve()))
        self._draft_changed()

    def _store_text_controls(self):
        if self.document is None:
            return
        text = self._selected_text()
        if text is None:
            return
        current = object_for_profile(self.document, text.id, self._canvas_key)
        if not isinstance(current, TextObject):
            current = text
        # 清空即隐藏该块；不再回填整条投稿标题。
        typed_text = self.title_edit.text().strip()
        fill = self.fill_color_button.color() or current.style.fill_color
        stroke = self.stroke_button.color() or current.style.stroke_color
        updated = replace(
            current,
            text=typed_text,
            visible=bool(typed_text),
            style=replace(
                current.style,
                font_family=(
                    str(Path(self.font_path_edit.text().strip()).resolve())
                    if self.font_path_edit.text().strip() and Path(self.font_path_edit.text().strip()).is_file()
                    else current.style.font_family
                ),
                font_size=self.font_spin.value(),
                fill=fill,
                stroke=stroke,
                stroke_width=self.stroke_spin.value(),
                shadow=self.shadow_check.isChecked(),
                line_spacing=self.line_spacing_spin.value(),
                align=current.align,
                outer_stroke=self.outer_stroke_button.color(),
                outer_stroke_width=self.outer_stroke_spin.value(),
                backdrop=self.backdrop_button.color(),
            ),
        )
        # 文案与样式写回对象本体并同步另一比例，位置和字号只写当前比例。
        self.document = update_text_object(self.document, updated, profile_key=self._canvas_key)
        # 清空即在两个比例中隐藏，重新输入即恢复；这是内容操作，不分比例。
        self.document = set_object_visible(self.document, updated.id, bool(typed_text))

    def _apply_style_preset(self, preset: StylePreset) -> None:
        if self.document is None:
            return
        before = self.document
        for item in self.document.objects:
            if not isinstance(item, TextObject):
                continue
            current = object_for_profile(self.document, item.id, self._canvas_key)
            current = current if isinstance(current, TextObject) else item
            updated = replace(current, style=preset.apply(current.style, item.copy_role))
            self.document = update_text_object(self.document, updated, profile_key=self._canvas_key)
        if self.document != before:
            self._commit_document_change(before)
            self.status_changed.emit(f"已套用样式：{preset.label}")

    def _title_position_changed(self, x: float, y: float):
        """键盘微调等程序化移动写进文档；鼠标拖动由松手时的 object_changed 提交。"""

        if self.project is None or self.video is None:
            return
        if self.document is None:
            # 兼容没有 v4 文档的旧调用方；这里只更新内存草稿。
            self.draft = replace(self.draft, text_x=x, text_y=y)
        elif getattr(self.canvas, "_mode", None) is None:
            text = self._selected_text()
            current = object_for_profile(self.document, text.id, self._canvas_key) if text else None
            if isinstance(current, TextObject) and (abs(current.transform.x - x) > 1e-6 or abs(current.transform.y - y) > 1e-6):
                moved = replace(current, transform=replace(current.transform, x=x, y=y))
                self.document = update_text_object(self.document, moved, profile_key=self._canvas_key)
                self.draft = CoverDraft.from_document(self.document)

    def _show_text_metrics(self, item: TextObject):
        """画布上改了字号或对齐后，右侧控件只显示新值，不反向写回。"""

        self._selected_text_id = item.id
        self.font_spin.blockSignals(True)
        try:
            self.font_spin.setValue(int(item.style.font_size))
        finally:
            self.font_spin.blockSignals(False)
        for key, button in self.align_buttons.items():
            button.blockSignals(True)
            button.setChecked(key == item.align)
            button.blockSignals(False)
