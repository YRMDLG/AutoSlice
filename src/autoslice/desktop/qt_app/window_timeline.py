"""时间轴：选择与框选、吸附/波形/适配、裁切、拆分合并、右键菜单、滚动同步。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollBar,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.qt_preview.theme import SIZES
from autoslice.desktop.qt_preview.window import label
from autoslice.transcription.contracts import srt_timestamp_seconds

from .timeline import SubtitleTimeline


class TimelineMixin:
    def _timeline(self):
        panel = QWidget()
        panel.setObjectName("timeline")
        panel.setMinimumHeight(110)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 5, 24, 4)
        layout.setSpacing(1)
        heading = QHBoxLayout()
        heading.addWidget(label("字幕时间轴", "sectionTitle"))
        heading.addSpacing(SIZES.space_2)
        heading.addWidget(label("拖动空白框选 · 滚轮/鼠标侧键平移 · Ctrl+滚轮缩放", "subtle"))
        heading.addStretch()
        self.snap_button = QPushButton("磁吸")
        self.snap_button.setCheckable(True)
        self.snap_button.setChecked(True)
        self.snap_button.setToolTip("自动吸附字幕边界；拖动时按住 Alt 可临时关闭")
        self.snap_button.clicked.connect(self._toggle_snapping)
        heading.addWidget(self.snap_button)
        self.waveform_button = QPushButton("波形")
        self.waveform_button.setCheckable(True)
        self.waveform_button.setChecked(True)
        self.waveform_button.setToolTip("显示轻量音频波形")
        self.waveform_button.clicked.connect(self._toggle_waveform)
        heading.addWidget(self.waveform_button)
        self.fit_button = QPushButton("适合全部")
        self.fit_button.clicked.connect(self._fit_timeline)
        heading.addWidget(self.fit_button)
        self.more_button = QPushButton("…")
        self.more_button.setToolTip("字幕编辑操作")
        self.more_button.clicked.connect(lambda: self._show_edit_menu(
            self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()), global_position=True))
        heading.addWidget(self.more_button)
        self.timeline_overlap = label("", "subtle")
        heading.addWidget(self.timeline_overlap)
        layout.addLayout(heading)
        self.timeline = SubtitleTimeline()
        self.timeline.set_waveform_visible(True)
        self.timeline.selected.connect(self._select_from_timeline)
        self.timeline.selection_requested.connect(self._timeline_selection_requested)
        self.timeline.marquee_selected.connect(self._timeline_marquee_selected)
        self.timeline.preview_changed.connect(self._timeline_preview_changed)
        self.timeline.changed.connect(self._timeline_changed)
        self.timeline.preview_requested.connect(self._timeline_preview_requested)
        self.timeline.seek_requested.connect(self._seek_to)
        self.timeline.view_changed.connect(self._sync_timeline_scroll)
        self.timeline.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.timeline.customContextMenuRequested.connect(self._show_edit_menu)
        layout.addWidget(self.timeline, 1)
        self.timeline_scroll = QScrollBar(Qt.Orientation.Horizontal)
        self.timeline_scroll.setObjectName("timelineScroll")
        self.timeline_scroll.setFixedHeight(12)
        self.timeline_scroll.setTracking(True)
        self.timeline_scroll.setSingleStep(250)
        self.timeline_scroll.valueChanged.connect(self._timeline_scroll_changed)
        layout.addWidget(self.timeline_scroll)
        self._syncing_timeline_scroll = False
        self._sync_timeline_scroll(0.0, self.timeline.span, self.timeline.duration)
        return panel

    def _timeline_selection_requested(self, cue_index: int, ctrl: bool, shift: bool):
        if not self.document:
            return
        self.selection.choose(cue_index, self._cue_order(), ctrl=ctrl, shift=shift)
        self.selected_index = self.selection.active
        # 时间轴里点到的字幕本来就已经在当前可视窗口内。
        # 这里只同步选中态和底部列表，不允许 selection 重新居中时间轴，
        # 否则会重置 _manual_pan，随后 playhead/player 回调又会二次改变视野，造成闪烁跳动。
        self._select_cue(self.selection.active, center_timeline=False)
        # 真正的 click 定位由 timeline 在 release 时发 seek_requested。
        # resize/drag 期间因此不会把 playhead 吸到字幕边界。
        self._save_session()

    def _timeline_marquee_selected(self, cue_ids: set[int]):
        if not self.document:
            return
        self.selection.replace(cue_ids, self._cue_order())
        self.selected_index = self.selection.active
        self._select_cue(self.selected_index, center_timeline=False)
        if self.selected_index is None:
            self._show_transient_status("已取消多选")
        else:
            self._show_transient_status(f"已选择 {len(self.selection.selected)} 条字幕")

    def _toggle_snapping(self):
        self.snapping = self.snap_button.isChecked() if hasattr(self, "snap_button") else not self.snapping
        self.snap_button.setChecked(self.snapping)
        if hasattr(self, "timeline"):
            self.timeline.set_snapping(self.snapping)
        self._show_transient_status("已开启磁吸" if self.snapping else "已关闭磁吸")

    def _toggle_waveform(self):
        self.waveform_visible = self.waveform_button.isChecked() if hasattr(self, "waveform_button") else not self.waveform_visible
        self.waveform_button.setChecked(self.waveform_visible)
        self.timeline.set_waveform_visible(self.waveform_visible)
        self._show_transient_status("波形已显示" if self.waveform_visible else "波形已隐藏")

    def _fit_timeline(self):
        if self.document:
            self.timeline.center = (self.timeline.duration or
                                    srt_timestamp_seconds(self.document.entries[-1].end)) / 2
            self.timeline.span = max(4.0, self.timeline.duration or self.timeline.span)
            self.timeline._manual_pan = False
            self.timeline.update()
            self.timeline._emit_view_changed()

    def _trim_start(self):
        self._trim_active("start")

    def _trim_end(self):
        self._trim_active("end")

    def _trim_active(self, side):
        if not self.document or self.selection.active is None:
            return
        entry = next((item for item in self.document.entries if item.index == self.selection.active), None)
        if not entry:
            return
        start, end = srt_timestamp_seconds(entry.start), srt_timestamp_seconds(entry.end)
        if not start < self._player_position < end:
            self._show_transient_status("定位线不在当前字幕内")
            return
        try:
            self.document.change_time(entry.index, self._player_position if side == "start" else start,
                                      end if side == "start" else self._player_position)
        except ValueError as exc:
            self._show_transient_status(str(exc))
            return
        self.model.set_document(self.document)
        self.timeline.update()
        self._edited()

    def _split_at_playhead(self):
        if not self.document or self.selection.active is None:
            return
        entry = next((item for item in self.document.entries if item.index == self.selection.active), None)
        if not entry:
            return
        editor = next((item for item in self.table.findChildren(QPlainTextEdit) if item.isVisible()), None)
        caret_text = None
        if editor:
            cursor = editor.textCursor()
            caret_text = editor.toPlainText()[:cursor.position()]
        self._commit_editor()
        try:
            new_index = self.document.split_at(entry.index, self._player_position,
                                                left_text=caret_text)
        except ValueError as exc:
            self._show_transient_status(str(exc))
            return
        self.selection.choose(new_index, self._cue_order())
        self.selected_index = new_index
        self.model.set_document(self.document)
        self._select_cue(new_index)
        self._edited()
        self._show_transient_status("已拆分字幕；请补全右侧正文后保存")

    def _show_edit_menu(self, position, *, global_position=False):
        menu = QMenu(self)
        menu.addAction("左裁到定位线 Q", self._trim_start)
        menu.addAction("右裁到定位线 W", self._trim_end)
        menu.addAction("在定位线拆分 Ctrl+B", self._split_at_playhead)
        merge = menu.addAction("合并所选字幕", self._merge_selected)
        merge.setEnabled(self._can_merge_selected())
        if not merge.isEnabled():
            merge.setToolTip("请先选择时间轴中连续相邻的字幕")
        menu.addAction("删除 Delete", self._delete)
        menu.addSeparator()
        menu.addAction("试听开头", self._audition_start)
        menu.addAction("试听结尾", self._audition_end)
        loop = menu.addAction("循环当前字幕")
        loop.setCheckable(True)
        loop.setChecked(self._loop_enabled)
        loop.triggered.connect(self._set_loop)
        anchor = position if global_position else (
            self.timeline.mapToGlobal(position) if position is not None else self.cursor().pos()
        )
        menu.exec(anchor)

    def _can_merge_selected(self):
        if not self.document or len(self.selection.selected) < 2:
            return False
        positions = [i for i, item in enumerate(self.document.entries)
                     if item.index in self.selection.selected]
        return len(positions) == len(self.selection.selected) and positions == list(
            range(min(positions), max(positions) + 1)
        )

    def _merge_selected(self):
        if not self.document:
            return
        try:
            new_index = self.document.merge_adjacent(set(self.selection.selected))
        except ValueError as exc:
            self._show_transient_status(str(exc))
            return
        self.selection.choose(new_index, self._cue_order())
        self.selected_index = new_index
        self.model.set_document(self.document)
        self._select_cue(new_index)
        self._edited()
        self._show_transient_status("已合并所选字幕")

    def _entry_bounds(self, cue_id=None):
        cue_id = cue_id if cue_id is not None else self.selection.active
        if not self.document or cue_id is None:
            return None
        entry = next((item for item in self.document.entries if item.index == cue_id), None)
        if entry is None:
            return None
        return srt_timestamp_seconds(entry.start), srt_timestamp_seconds(entry.end)

    def _select_from_timeline(self, cue_index):
        if not self.document:
            return
        self.selection.choose(cue_index, self._cue_order())
        self.selected_index = cue_index
        self._select_cue(cue_index, center_timeline=False)
        entry = next(item for item in self.document.entries if item.index == cue_index)
        self._seek_to(srt_timestamp_seconds(entry.start))
        self._save_session()

    def _timeline_preview_changed(self, cue_index):
        if not self.document:
            return
        row = next((i for i, item in enumerate(self.model.visible_entries())
                    if item.index == cue_index), None)
        if row is not None:
            self.model.dataChanged.emit(self.model.index(row, 1), self.model.index(row, 1),
                                        [Qt.ItemDataRole.DisplayRole])
        self._update_status()

    def _timeline_changed(self, cue_index):
        self._timeline_preview_changed(cue_index)
        self._edited()

    def _timeline_preview_requested(self, seconds):
        self._seek_to(seconds)

    def _sync_timeline_scroll(self, start: float, span: float, duration: float):
        if not hasattr(self, "timeline_scroll"):
            return
        self._syncing_timeline_scroll = True
        try:
            duration_ms = max(0, int(round(duration * 1000)))
            span_ms = max(1, int(round(min(span, duration) * 1000))) if duration > 0 else 1
            maximum = max(0, duration_ms - span_ms)
            self.timeline_scroll.setRange(0, maximum)
            self.timeline_scroll.setPageStep(span_ms)
            self.timeline_scroll.setValue(max(0, min(maximum, int(round(start * 1000)))))
            self.timeline_scroll.setEnabled(maximum > 0)
        finally:
            self._syncing_timeline_scroll = False

    def _timeline_scroll_changed(self, value: int):
        if getattr(self, "_syncing_timeline_scroll", False):
            return
        if hasattr(self, "timeline"):
            self.timeline.set_view_start(value / 1000.0)

    def _timeline_splitter_moved(self, _position, _index):
        if not hasattr(self, "timeline") or not self.vertical_splitter.sizes():
            return
        self._session["timeline_height"] = self.vertical_splitter.sizes()[1]
        self._save_session()
