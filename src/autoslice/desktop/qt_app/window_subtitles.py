"""字幕列表：表格选择同步、点选跳转、就地编辑器的打开、光标与提交。

混入 DesktopWindow，排在 PreviewWindow 之前；状态都在窗口上，不单独实例化。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.qt_preview import motion
from autoslice.desktop.qt_preview.appearance import themed
from autoslice.desktop.qt_preview.shell import Divider
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import label
from autoslice.transcription.contracts import srt_timestamp_seconds

from .window_widgets import (
    EDIT_BAR_HEIGHT,
    EDIT_BAR_TOP,
    EDIT_HINT_HEIGHT,
    EDIT_HINT_TOP,
    EDIT_PAD_X,
    EDIT_ROW_HEIGHT,
    ROW_SIDE,
    TIME_TOP,
    InlineSubtitleEditor,
    SelectionFrame,
    SubtitleTableModel,
    SubtitleTextDelegate,
    _prepare_inline_editor,
    short_span,
)


class _GapWhenShown(QObject):
    """查找条、识别横幅显示时下方留 10px，隐藏时不占位（标题行下面直接是分隔线）。"""

    def __init__(self, holder, widget) -> None:
        super().__init__(widget)
        self._holder = holder
        self._sync(widget)

    def _sync(self, widget) -> None:
        self._holder.setContentsMargins(12, 0, 12, 10 if not widget.isHidden() else 0)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.HideToParent,
                            QEvent.Type.ShowToParent):
            self._sync(obj)
        return False


class SubtitleListMixin:
    def _subtitle_list(self):
        """字幕列表卡：标题 + 全部 / 待确认 + 检查提醒，中间是字幕行，底部一行状态与操作提示。"""

        panel = QWidget()
        panel.setObjectName("subtitleList")
        panel.setMinimumHeight(170)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QWidget()
        header.setMinimumHeight(56)
        heading = QHBoxLayout(header)
        heading.setContentsMargins(18, 8, 12, 8)
        heading.setSpacing(12)
        heading.addWidget(label("字幕", "cardHeading"))
        # 筛选做成分段控件，当前筛选一目了然
        segmented = QWidget()
        segmented.setObjectName("segmented")
        segment_row = QHBoxLayout(segmented)
        segment_row.setContentsMargins(0, 0, 0, 0)
        segment_row.setSpacing(2)
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.filter_all = QPushButton("全部")
        self.filter_pending = QPushButton("待确认 0")
        for button, pending in ((self.filter_all, False), (self.filter_pending, True)):
            button.setObjectName("segment")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, value=pending: self._set_filter(value))
            self.filter_group.addButton(button)
            segment_row.addWidget(button)
        self.filter_all.setChecked(True)
        motion.SegmentedFx(segmented, (self.filter_all, self.filter_pending))
        heading.addWidget(segmented)
        heading.addStretch()
        self.quality_button = QPushButton("检查提醒 0")
        self.quality_button.setObjectName("chip")
        self.quality_button.setIconSize(QSize(10, 6))
        themed(self.quality_button, self._paint_quality_dot)
        self.quality_button.setToolTip("检查重复字、显示时间、重叠和长字幕，支持定位、试听与撤销")
        self.quality_button.setEnabled(False)
        self.quality_button.clicked.connect(self._show_quality)
        heading.addWidget(self.quality_button)
        heading.addWidget(self._build_find_toggle())
        layout.addWidget(header)
        for build in (self._build_find_bar, self._build_asr_banner):
            holder = QVBoxLayout()
            holder.setContentsMargins(12, 0, 12, 0)
            widget = build()
            holder.addWidget(widget)
            widget.installEventFilter(_GapWhenShown(holder, widget))
            layout.addLayout(holder)
        layout.addWidget(Divider(16))
        self.model = SubtitleTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.subtitle_delegate = SubtitleTextDelegate(self.table)
        self.table.setItemDelegate(self.subtitle_delegate)
        self.table.setVerticalScrollMode(QTableView.ScrollMode.ScrollPerPixel)
        self.table.setMouseTracking(True)
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
        # 设计稿的字幕行没有表头：序号、时间、正文按列对齐即可
        self.table.horizontalHeader().hide()
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
        # 设计稿：序号、正文、标记一行，时间码在正文下方；时间列只留在模型里
        self.table.setColumnHidden(1, True)
        table_row = QHBoxLayout()
        table_row.setContentsMargins(8, 8, 8, 8)
        table_row.addWidget(self.table)
        layout.addLayout(table_row, 1)
        layout.addWidget(Divider(16))
        footer = QHBoxLayout()
        footer.setContentsMargins(18, 12, 18, 12)
        footer.setSpacing(12)
        self.subtitle_hint = label("空格从选中句开头播放 · 右键可编辑、拆分、合并、删除 · 单击正文编辑", "hintLine")
        footer.addWidget(self.subtitle_hint, 1)
        # 没有字幕时（未选项目、缺字幕、加载失败）右侧给一句说明；有字幕时数量和保存状态在顶栏和筛选上
        self.subtitle_status = label("请选择项目", "hintLine")
        footer.addWidget(self.subtitle_status)
        # 底部两段字都可以被压窄，不撑大右列
        for item in (self.subtitle_hint, self.subtitle_status):
            item.setMinimumWidth(0)
            item.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addLayout(footer)
        self._row_frame = SelectionFrame(self.table)
        return panel

    def _paint_quality_dot(self):
        """检查提醒左侧的小圆点：有提醒时橙色，没有时灰色。"""

        warn = self.quality_button.isEnabled()
        color = COLORS.warning if warn else COLORS.disabled
        pixmap = QPixmap(20, 12)
        pixmap.setDevicePixelRatio(2.0)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(0, 0, 6, 6)
        painter.end()
        self.quality_button.setIcon(QIcon(pixmap))
        self.quality_button.setProperty("warn", "true" if warn else "false")
        self.quality_button.style().unpolish(self.quality_button)
        self.quality_button.style().polish(self.quality_button)

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
        if hasattr(self, "_follow_ai_selection"):
            self._follow_ai_selection()

    def _table_native_selection_changed(self, _selected, _deselected):
        if self._syncing_selection or self.document is None or self._table_pointer_selecting:
            return
        if getattr(self, "_selection_resync_pending", False):
            return
        self._selection_resync_pending = True
        QTimer.singleShot(0, self._restore_table_selection_after_native_change)

    def _restore_table_selection_after_native_change(self):
        self._selection_resync_pending = False
        if self.document is not None and not self._table_pointer_selecting:
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
        # 手动平移过时间轴后点列表里离得远的句子，时间轴也要跟过去
        self.timeline.reveal_cue(entry.index)
        if self._loop_enabled:
            self._loop_cue_id = entry.index
            self._loop_seek_pending = False
        target = srt_timestamp_seconds(entry.start)
        if abs(self._player_position - target) > .1 or not self._player_paused:
            self.timeline.set_selection(entry.index, self.selection.selected)
            self._seek_to(target)
        click_point = None
        pending = self._pending_caret_click
        if index.column() == 2 and pending and pending[0] == index.row() and pending[1] == index.column():
            click_point = pending[2]
            # 点在正文下方的时间码那一行：和点序号一样只选中，不进编辑
            top = self.table.visualRect(index).top()
            if click_point.y() - top >= TIME_TOP - 2:
                self._pending_caret_click = None
                return
        if index.column() == 2:
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
        entry = self.model.entry_at(index.row())
        # 记下改字前的字幕，提示条“撤销”一次撤回整次修改；“取消”按钮恢复原文
        self._inline_before = (entry.index, tuple(self.document.entries) if self.document else None)
        self._inline_original_text = entry.text
        self.subtitle_delegate.editing_cue = entry.index
        editor.textChanged.connect(self._refresh_inline_edit_meta)
        # 改字时先收起这一行下面的建议；这一行加高，下面放“完成 / 取消”
        self._place_inline_ai()
        self.table.setIndexWidget(index, editor)
        editor.show()
        self._place_inline_edit()
        self._sync_shortcuts()

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
        else:
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            editor.setTextCursor(cursor)

    def _step_subtitle(self, delta, *, edit):
        """键盘切句：delta 为相对当前句的行数，0 表示原地进入编辑。"""
        if self.document is None:
            return
        editing = self._editing_subtitle_index()
        if editing is not None and editing.isValid():
            row = editing.row()
        else:
            row = next((i for i, entry in enumerate(self.model.visible_entries())
                        if entry.index == self.selection.active), None)
        target = 0 if row is None else row + delta
        if not 0 <= target < len(self.model.visible_entries()):
            # 到头了：确认当前编辑，留在原句
            self._commit_editor()
            self.table.setFocus()
            return
        self._go_to_subtitle_row(target, edit=edit)

    def _go_to_subtitle_row(self, row, *, edit):
        self._commit_editor()
        entries = self.model.visible_entries()
        if self.document is None or not 0 <= row < len(entries):
            return
        entry = entries[row]
        self.selection.choose(entry.index, self._cue_order())
        self.selected_index = self.selection.active
        self._sync_table_selection()
        index = self.model.index(row, 2)
        motion.smooth_scroll(self.table, lambda: self.table.scrollTo(index))
        self.timeline.set_selection(entry.index, self.selection.selected)
        self.timeline.reveal_cue(entry.index)
        if self._loop_enabled:
            self._loop_cue_id = entry.index
            self._loop_seek_pending = False
        target = srt_timestamp_seconds(entry.start)
        if abs(self._player_position - target) > .1 or not self._player_paused:
            self._seek_to(target)
        self._save_session()
        if edit:
            self._open_inline_editor(index)
            self.table.setCurrentIndex(index)
            QTimer.singleShot(0, lambda idx=index: self._focus_subtitle_editor(idx))
        else:
            self.table.setFocus()

    def _is_subtitle_editor_widget(self, widget) -> bool:
        # 编辑操作条（完成 / 取消）也算编辑框“里面”，点它们不先触发“点外面就提交”
        bar = getattr(self, "inline_edit_bar", None)
        if bar is not None and isinstance(widget, QWidget) and (widget is bar or bar.isAncestorOf(widget)):
            return True
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
        # hide/remove 会同步触发焦点变化，必须先结束编辑状态，避免快捷键一直禁用。
        self._inline_editor = None
        self._inline_editor_index = QModelIndex()
        self.subtitle_delegate.editing_cue = None
        self._inline_drag_anchor = None
        editor.hide()
        self.table.setIndexWidget(index, None)
        editor.setParent(None)
        editor.deleteLater()
        self.table.viewport().update(self.table.visualRect(index))
        self._sync_shortcuts()
        self._place_inline_ai()
        self._place_inline_edit()
        before = getattr(self, "_inline_before", None)
        self._inline_before = None
        if before and before[1] is not None and self.document and tuple(self.document.entries) != before[1]:
            # 改字落定：整行闪一下（设计稿 edFlash / 晨雾 inkWash）
            self.subtitle_delegate.effects.play(before[0], "flash")
            self._toast(f"已修改第 {before[0]} 条字幕", undo=True, snapshot=before[1])

    def _cancel_editor(self):
        """编辑条“取消”：恢复这句改字前的原文，再按正常流程收起编辑框。"""

        editor = self._active_subtitle_editor()
        original = getattr(self, "_inline_original_text", None)
        if editor is not None and original is not None and editor.toPlainText() != original:
            editor.setPlainText(original)
        self._commit_editor()
        self.table.setFocus()

    def _finish_editor_from_bar(self):
        self._commit_editor()
        self.table.setFocus()

    def _inline_edit_bar(self) -> QWidget:
        """编辑中那一行下半截：完成 / 取消、时间 · 字数，和一行按键提示（设计稿 edrow）。"""

        bar = getattr(self, "inline_edit_bar", None)
        if bar is not None:
            return bar
        bar = QWidget(self.table.viewport())
        bar.setObjectName("inlineEditBar")
        column = QVBoxLayout(bar)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(EDIT_HINT_TOP - EDIT_BAR_TOP - EDIT_BAR_HEIGHT)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.inline_edit_done = QPushButton("完成")
        self.inline_edit_done.setObjectName("primary")
        self.inline_edit_done.setFixedHeight(EDIT_BAR_HEIGHT)
        self.inline_edit_done.setToolTip("确认这句并收起编辑框（Esc 同效）")
        self.inline_edit_done.clicked.connect(self._finish_editor_from_bar)
        row.addWidget(self.inline_edit_done)
        self.inline_edit_cancel = QPushButton("取消")
        self.inline_edit_cancel.setFixedHeight(EDIT_BAR_HEIGHT)
        self.inline_edit_cancel.setToolTip("放弃这次修改，恢复原文")
        self.inline_edit_cancel.clicked.connect(self._cancel_editor)
        row.addWidget(self.inline_edit_cancel)
        row.addStretch(1)
        self.inline_edit_meta = label("", "mono")
        self.inline_edit_meta.setMinimumWidth(0)
        self.inline_edit_meta.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.inline_edit_meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.inline_edit_meta, 1)
        column.addLayout(row)
        self.inline_edit_hint = label("Enter 下一句 · Shift Enter 换行 · Esc 或点别处完成", "hintLine")
        self.inline_edit_hint.setFixedHeight(EDIT_HINT_HEIGHT)
        self.inline_edit_hint.setMinimumWidth(0)
        self.inline_edit_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        column.addWidget(self.inline_edit_hint)
        bar.hide()
        motion.install_tree(bar)
        self.inline_edit_bar = bar
        return bar

    def _refresh_inline_edit_meta(self) -> None:
        editor = self._active_subtitle_editor()
        index = self._editing_subtitle_index()
        if editor is None or index is None or not index.isValid() or not hasattr(self, "inline_edit_meta"):
            return
        entry = self.model.entry_at(index.row())
        count = len(editor.toPlainText().replace("\n", ""))
        span = short_span(entry.start, entry.end)
        if span.startswith("00:"):
            span = span[3:]
        self.inline_edit_meta.setText(f"{span} · {count} 字")

    def _place_inline_edit(self) -> None:
        """编辑中的那一行加高到设计稿编辑态高度，操作条贴在输入框下面；没在编辑就复原行高。"""

        table = getattr(self, "table", None)
        if table is None:
            return
        header = table.verticalHeader()
        base = SIZES.subtitle_row_height
        index = self._editing_subtitle_index()
        row = index.row() if self._subtitle_editor_open() else -1
        previous = getattr(self, "_inline_edit_row", -1)
        rows = self.model.rowCount()
        if (previous != row and 0 <= previous < rows and previous != getattr(self, "_ai_inline_row", -1)
                and header.sectionSize(previous) != base):
            header.resizeSection(previous, base)
        self._inline_edit_row = row
        bar = self._inline_edit_bar()
        if row < 0:
            bar.hide()
            return
        if header.sectionSize(row) != EDIT_ROW_HEIGHT:
            header.resizeSection(row, EDIT_ROW_HEIGHT)
        cell = table.visualRect(self.model.index(row, 2))
        left = cell.left() - EDIT_PAD_X
        width = max(80, cell.width() - ROW_SIDE + EDIT_PAD_X)
        bar.setGeometry(left, cell.top() + EDIT_BAR_TOP, width,
                        EDIT_HINT_TOP + EDIT_HINT_HEIGHT - EDIT_BAR_TOP)
        self._refresh_inline_edit_meta()
        if bar.isHidden():
            bar.show()
            motion.fade_in(bar, 200)
        bar.raise_()

    def _select_cue(self, cue_index, *, center_timeline=True):
        if self.document is None:
            return
        if cue_index is not None and not self.selection.selected:
            self.selection.choose(cue_index, self._cue_order())
        row = next((i for i, entry in enumerate(self.model.visible_entries())
                    if entry.index == cue_index), None)
        if row is not None:
            self._sync_table_selection()
            motion.smooth_scroll(self.table, lambda: self.table.scrollTo(self.model.index(row, 0)))
            self.timeline.set_selection(cue_index, self.selection.selected or {cue_index},
                                         center=center_timeline)
            if self._loop_enabled:
                self._loop_cue_id = cue_index
                self._loop_seek_pending = False

    def _cue_order(self) -> list[int]:
        return [entry.index for entry in self.document.entries] if self.document else []
