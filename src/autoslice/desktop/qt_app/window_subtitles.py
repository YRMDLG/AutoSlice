"""字幕列表：表格选择同步、点选跳转、就地编辑器的打开、光标与提交。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    Qt,
    QTimer,
)
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.qt_preview.theme import SIZES
from autoslice.desktop.qt_preview.window import label
from autoslice.transcription.contracts import srt_timestamp_seconds

from .window_widgets import (
    InlineSubtitleEditor,
    SubtitleTableModel,
    SubtitleTextDelegate,
    _prepare_inline_editor,
)


class SubtitleListMixin:
    def _subtitle_list(self):
        panel = QWidget()
        panel.setObjectName("subtitleList")
        panel.setMinimumHeight(170)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 12, 20, 10)
        heading = QHBoxLayout()
        heading.addWidget(label("字幕列表", "sectionTitle"))
        self.filter_all = QPushButton("全部")
        self.filter_all.clicked.connect(lambda: self._set_filter(False))
        heading.addWidget(self.filter_all)
        self.filter_pending = QPushButton("待确认 0")
        self.filter_pending.clicked.connect(lambda: self._set_filter(True))
        heading.addWidget(self.filter_pending)
        heading.addStretch()
        self.subtitle_status = label("请选择项目", "subtle")
        heading.addWidget(self.subtitle_status)
        layout.addLayout(heading)
        self.model = SubtitleTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.subtitle_delegate = SubtitleTextDelegate(self.table)
        self.table.setItemDelegate(self.subtitle_delegate)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        # MultiSelection 会让普通单击不断累积选中行，看起来像“莫名多选”。
        # ExtendedSelection 才符合桌面编辑器习惯：普通点击单选，Ctrl 增减，Shift 连选。
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.selectionModel().selectionChanged.connect(
            self._table_native_selection_changed
        )
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setFixedHeight(26)
        self.table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, SIZES.subtitle_number_width)
        self.table.setColumnWidth(1, SIZES.subtitle_time_width)
        self.table.verticalHeader().setDefaultSectionSize(SIZES.subtitle_row_height)
        self.table.clicked.connect(self._cue_clicked)
        self.table.viewport().installEventFilter(self)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(
            lambda point: self._show_edit_menu(self.table.viewport().mapToGlobal(point), global_position=True)
        )
        layout.addWidget(self.table, 1)
        return panel

    def _sync_table_selection(self):
        if not hasattr(self, "table") or not hasattr(self, "model"):
            return
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return
        self._syncing_selection = True
        try:
            selection_model.clearSelection()
            flags = (
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows
            )
            active_row = None
            for row, item in enumerate(self.model.visible_entries()):
                if item.index in self.selection.selected:
                    selection_model.select(self.model.index(row, 0), flags)
                if item.index == self.selection.active:
                    active_row = row
            if active_row is not None:
                selection_model.setCurrentIndex(
                    self.model.index(active_row, 2),
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        finally:
            self._syncing_selection = False

    def _table_native_selection_changed(self, _selected, _deselected):
        if self._syncing_selection or self.document is None:
            return
        if getattr(self, "_selection_resync_pending", False):
            return
        self._selection_resync_pending = True
        QTimer.singleShot(0, self._restore_table_selection_after_native_change)

    def _restore_table_selection_after_native_change(self):
        self._selection_resync_pending = False
        if self.document is not None:
            self._sync_table_selection()

    def _cue_clicked(self, index):
        if self.document is None:
            return
        entry = self.model.entry_at(index.row())
        modifiers = QApplication.keyboardModifiers()
        self.selection.choose(entry.index, self._cue_order(),
                              ctrl=bool(modifiers & Qt.KeyboardModifier.ControlModifier),
                              shift=bool(modifiers & Qt.KeyboardModifier.ShiftModifier))
        self.selected_index = self.selection.active
        self._sync_table_selection()
        self._row_changed(index, QModelIndex())
        target = srt_timestamp_seconds(entry.start)
        if abs(self._player_position - target) > .1 or not self._player_paused:
            self.timeline.set_selection(entry.index, self.selection.selected)
            self._seek_to(target)
        if index.column() == 2:
            click_point = None
            pending = self._pending_caret_click
            if pending and pending[0] == index.row() and pending[1] == index.column():
                click_point = pending[2]
            self._pending_caret_click = None

            editing_index = self._editing_subtitle_index()
            if editing_index is not None and editing_index.isValid() and editing_index != index:
                self._commit_editor()
            editing_index = self._editing_subtitle_index()
            if editing_index is None or not editing_index.isValid():
                self._open_inline_editor(index)
            self.table.setCurrentIndex(index)
            QTimer.singleShot(
                0,
                lambda idx=index, point=click_point: self._focus_subtitle_editor(idx, point),
            )

    def _row_changed(self, index, _previous):
        if self._syncing_selection or self.document is None or not index.isValid():
            return
        entry = self.model.entry_at(index.row())
        if entry.index == self.selected_index:
            self.timeline.set_selection(entry.index, self.selection.selected or {entry.index})
            return
        self.selected_index = entry.index
        self.selection.choose(entry.index, self._cue_order())
        self._sync_table_selection()
        self.timeline.set_selection(entry.index, self.selection.selected)
        # current index 变化只同步选中态，绝不驱动 playhead。
        # 真正的字幕点击、时间轴点击、AI 定位各自显式发 seek。
        self._save_session()

    def _active_subtitle_editor(self):
        # 不在应用级 eventFilter 路径里调用 isVisible()/Qt 属性查询；
        # 这些查询本身可能触发新的 Qt 事件，造成 eventFilter 重入。
        return getattr(self, "_inline_editor", None)

    def _editing_subtitle_index(self):
        return getattr(self, "_inline_editor_index", None)

    def _subtitle_editor_open(self) -> bool:
        index = self._editing_subtitle_index()
        return (
            getattr(self, "_inline_editor", None) is not None
            and index is not None
            and index.isValid()
        )

    def _open_inline_editor(self, index):
        if not index.isValid() or index.column() != 2:
            return
        editor = InlineSubtitleEditor(self.table.viewport())
        _prepare_inline_editor(editor, self.table.font())
        editor.setPlainText(str(index.data(Qt.ItemDataRole.EditRole) or ""))
        editor.installEventFilter(self)
        editor.viewport().installEventFilter(self)
        editor.textChanged.connect(
            lambda idx=index, item=editor: self.model.set_live_text(idx, item.toPlainText())
        )
        self._inline_editor = editor
        self._inline_editor_index = index
        self.table.setIndexWidget(index, editor)
        editor.show()

    def _focus_subtitle_editor(self, index, viewport_position=None):
        editing_index = self._editing_subtitle_index()
        if editing_index is None or not editing_index.isValid() or editing_index != index:
            return
        editor = self._active_subtitle_editor()
        if editor is None:
            return
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        if viewport_position is not None:
            self._place_caret(index, viewport_position)

    def _is_subtitle_editor_widget(self, widget) -> bool:
        current = widget
        while current is not None:
            if isinstance(current, QPlainTextEdit) and self.table.isAncestorOf(current):
                return True
            current = current.parentWidget() if hasattr(current, "parentWidget") else None
        return False

    def _event_hits_inline_editor(self, event) -> bool:
        editor = self._active_subtitle_editor()
        if editor is None or not hasattr(event, "globalPosition"):
            return False
        try:
            point = editor.mapFromGlobal(event.globalPosition().toPoint())
            return editor.rect().contains(point)
        except (RuntimeError, TypeError):
            return False

    def _editor_cursor_from_event(self, event):
        editor = self._active_subtitle_editor()
        if editor is None or not hasattr(event, "globalPosition"):
            return None
        try:
            point = editor.viewport().mapFromGlobal(event.globalPosition().toPoint())
            return editor.cursorForPosition(point)
        except (RuntimeError, TypeError):
            return None

    def _place_caret_from_global(self, event):
        editor = self._active_subtitle_editor()
        cursor = self._editor_cursor_from_event(event)
        if editor is None or cursor is None:
            return
        editor.setTextCursor(cursor)
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        editor.ensureCursorVisible()

    def _handle_inline_editor_mouse(self, watched, event) -> bool:
        editor = self._active_subtitle_editor()
        if editor is None or not self._is_subtitle_editor_widget(watched):
            return False
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            cursor = self._editor_cursor_from_event(event)
            if cursor is None:
                return False
            self._inline_drag_anchor = cursor.position()
            editor.setTextCursor(cursor)
            editor.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return True
        if event_type == QEvent.Type.MouseMove and self._inline_drag_anchor is not None:
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                self._inline_drag_anchor = None
                return False
            cursor = self._editor_cursor_from_event(event)
            if cursor is None:
                return False
            position = cursor.position()
            cursor.setPosition(self._inline_drag_anchor)
            cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
            editor.setTextCursor(cursor)
            editor.ensureCursorVisible()
            event.accept()
            return True
        if event_type == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            self._inline_drag_anchor = None
            event.accept()
            return True
        return False

    def _place_caret(self, index, viewport_position):
        if not self._subtitle_editor_open():
            return
        editor = next((item for item in self.table.findChildren(QPlainTextEdit)
                       if item.isVisible()), None)
        if editor is None:
            return
        try:
            local = editor.mapFrom(self.table.viewport(), viewport_position)
            cursor = editor.cursorForPosition(local)
            editor.setTextCursor(cursor)
            editor.setFocus(Qt.FocusReason.MouseFocusReason)
            editor.ensureCursorVisible()
        except (RuntimeError, TypeError):
            return

    def _commit_editor(self):
        index = self._editing_subtitle_index()
        editor = self._active_subtitle_editor()
        if index is None or not index.isValid() or editor is None:
            return
        self.model.finish_live_text(index, editor.toPlainText())
        editor.hide()
        self.table.setIndexWidget(index, None)
        editor.setParent(None)
        self._inline_editor = None
        self._inline_editor_index = QModelIndex()
        editor.deleteLater()
        self.table.viewport().update(self.table.visualRect(index))

    def _select_cue(self, cue_index, *, center_timeline=True):
        if self.document is None:
            return
        if cue_index is not None and not self.selection.selected:
            self.selection.choose(cue_index, self._cue_order())
        row = next((i for i, entry in enumerate(self.model.visible_entries())
                    if entry.index == cue_index), None)
        if row is not None:
            self._syncing_selection = True
            self.table.clearSelection()
            selected = self.selection.selected
            for item_row, item in enumerate(self.model.visible_entries()):
                if item.index in selected:
                    self.table.selectRow(item_row)
            self._syncing_selection = False
            self.table.scrollTo(self.model.index(row, 0))
            self.timeline.set_selection(cue_index, self.selection.selected or {cue_index},
                                         center=center_timeline)
            if self._loop_enabled:
                self._loop_cue_id = cue_index
                self._loop_seek_pending = False

    def _cue_order(self) -> list[int]:
        return [entry.index for entry in self.document.entries] if self.document else []
