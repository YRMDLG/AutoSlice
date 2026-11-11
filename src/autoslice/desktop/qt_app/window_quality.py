"""字幕质检清单：定位、试听和可撤销的重复字处理。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from autoslice.desktop.subtitle_preview import boundary_duplicates
from autoslice.transcription.contracts import srt_timestamp_seconds


class QualityReviewMixin:
    def _show_quality(self):
        self._commit_editor()
        if not hasattr(self, "quality_dialog"):
            self.quality_dialog = QDialog(self)
            self.quality_dialog.setWindowTitle("字幕待检查")
            self.quality_dialog.resize(660, 390)
            layout = QVBoxLayout(self.quality_dialog)
            self.quality_title = QLabel()
            self.quality_title.setWordWrap(True)
            layout.addWidget(self.quality_title)
            self.quality_list = QListWidget()
            self.quality_list.currentRowChanged.connect(self._quality_selection_changed)
            self.quality_list.itemActivated.connect(lambda _item: self._locate_quality())
            layout.addWidget(self.quality_list, 1)
            self.quality_detail = QLabel()
            self.quality_detail.setWordWrap(True)
            layout.addWidget(self.quality_detail)
            row = QHBoxLayout()
            row.addStretch()  # 按钮靠右、按内容宽，不再撑满整行
            self.quality_locate = QPushButton("定位字幕")
            self.quality_locate.clicked.connect(self._locate_quality)
            row.addWidget(self.quality_locate)
            self.quality_listen = QPushButton("试听当前句")
            self.quality_listen.clicked.connect(self._listen_quality)
            row.addWidget(self.quality_listen)
            self.quality_fix = QPushButton("移除末尾重复字")
            self.quality_fix.clicked.connect(self._fix_quality_duplicate)
            row.addWidget(self.quality_fix)
            undo = QPushButton("撤销")
            undo.clicked.connect(self._undo)
            row.addWidget(undo)
            layout.addLayout(row)
        self._refresh_quality_panel(force=True)
        self.quality_dialog.show()
        self.quality_dialog.raise_()

    def _refresh_quality_panel(self, *, force=False):
        if not hasattr(self, "quality_list") or (not force and not self.quality_dialog.isVisible()):
            return
        selected = self.quality_list.currentItem()
        cue_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.quality_list.blockSignals(True)
        self.quality_list.clear()
        entries = self.document.entries if self.document else []
        flags = self.preview_service.quality_flags(entries, self._habit_limits())
        for entry in entries:
            if entry.index not in flags:
                continue
            item = QListWidgetItem(f"第 {entry.index} 条 · {'；'.join(flags[entry.index])}\n{entry.text[:70]}")
            item.setData(Qt.ItemDataRole.UserRole, entry.index)
            item.setToolTip(entry.text + "\n" + "\n".join(flags[entry.index]))
            self.quality_list.addItem(item)
            if entry.index == cue_id:
                self.quality_list.setCurrentItem(item)
        self.quality_list.blockSignals(False)
        if self.quality_list.currentRow() < 0 and self.quality_list.count():
            self.quality_list.setCurrentRow(0)
        title = f"{self.project.title} · {self.document.video.name}" if self.document and self.project else "尚未加载字幕"
        self.quality_title.setText(f"{title}\n{len(flags)} 条待检查；处理只修改编辑内容，支持撤销。")
        self._quality_selection_changed()

    def _quality_entry(self):
        item = self.quality_list.currentItem()
        cue_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return next((entry for entry in self.document.entries if entry.index == cue_id), None) if self.document else None

    def _quality_selection_changed(self, _row=None):
        entry = self._quality_entry()
        self.quality_locate.setEnabled(entry is not None)
        self.quality_listen.setEnabled(entry is not None and self.player is not None and self._media_ready)
        duplicate = boundary_duplicates(self.document.entries).get(entry.index) if entry else None
        self.quality_fix.setEnabled(bool(duplicate) and not self._saving)
        self.quality_fix.setText(f"移除末尾“{duplicate}”" if duplicate else "移除末尾重复字")
        self.quality_detail.setText(
            "先试听确认是否真的重复，再决定是否移除。" if duplicate else
            "时间过短请试听后在时间轴调整左右边缘；重叠请核对是否同时说话；长字幕可手动拆分或改字。"
            if entry else "当前没有待检查字幕。"
        )

    def _locate_quality(self):
        entry = self._quality_entry()
        if entry is None:
            return
        self._commit_editor()
        self._select_page(0)
        if self._pending_filter:
            self._set_filter(False)
        self.selection.choose(entry.index, self._cue_order())
        self.selected_index = entry.index
        self._select_cue(entry.index)
        self._seek_to(srt_timestamp_seconds(entry.start))

    def _listen_quality(self):
        self._locate_quality()
        self._audition_current()

    def _fix_quality_duplicate(self):
        if self._saving:
            return
        self._commit_editor()
        entry = self._quality_entry()
        tail = boundary_duplicates(self.document.entries).get(entry.index) if entry else None
        if not tail or not entry.text.endswith(tail):
            self._refresh_quality_panel()
            return
        self.document.edit_text(entry.index, entry.text[:-len(tail)])
        self.model.set_document(self.document)
        self.timeline.update()
        self._edited()
