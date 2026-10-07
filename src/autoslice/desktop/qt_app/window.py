"""Qt 字幕工作台：项目扫描、显式保存和可恢复草稿。"""

from __future__ import annotations

import base64
import copy
import difflib
import html
import os
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.ai_review import AIReviewService, document_hash
from autoslice.desktop.commands import CommandDispatcher
from autoslice.desktop.cover import CoverEditorWidget
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.jobs import BackgroundJob
from autoslice.desktop.projects import ProjectSnapshot, SubmissionProject, SubmissionProjectService
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import PreviewWindow, ProjectItem, label, line
from autoslice.desktop.selection import CueSelection
from autoslice.desktop.subtitle_preview import SubtitlePreviewService
from autoslice.desktop.subtitle_render import SubtitleRenderService
from autoslice.desktop.subtitles import SubtitleDocument
from autoslice.desktop.waveform import WaveformCache
from autoslice.subtitle_workflow import DEFAULT_SUBTITLE_STYLE
from autoslice.transcription.contracts import srt_timestamp_seconds

from .player import MpvAdapter
from .timeline import SubtitleTimeline, TimelineSidePanController


class _StatusLabel(QLabel):
    """空闲时从工作栏收起；后台任务或反馈出现时才占用空间。"""

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


class _ElidedQueueButton(QPushButton):
    """待处理队列的单行按钮，按实际控件宽度显示右侧省略号。"""

    def __init__(self, text: str, parent=None):
        self._full_text = str(text)
        super().__init__(self._full_text, parent)
        self._update_elided_text()

    @property
    def full_text(self) -> str:
        return self._full_text

    def _update_elided_text(self) -> None:
        margin = self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonMargin, None, self)
        available = max(0, self.contentsRect().width() - margin * 2)
        display = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, available
        )
        if self.text() != display:
            super().setText(display)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided_text()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.EnabledChange,
        ):
            self._update_elided_text()


class SubtitleTableModel(QAbstractTableModel):
    edited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document: SubtitleDocument | None = None
        self.playing_index: int | None = None
        self.pending_ids: set[int] | None = None

    def visible_entries(self):
        entries = self.document.entries if self.document else []
        return [item for item in entries if item.index in self.pending_ids] if self.pending_ids is not None else entries

    def entry_at(self, row):
        return self.visible_entries()[row]

    def set_pending_filter(self, cue_ids):
        self.beginResetModel()
        self.pending_ids = set(cue_ids) if cue_ids is not None else None
        self.endResetModel()

    def set_document(self, document: SubtitleDocument | None):
        self.beginResetModel()
        self.document = document
        self.playing_index = None
        self.endResetModel()

    def set_playing_index(self, cue_index: int | None):
        if cue_index == self.playing_index or self.document is None:
            return
        old = self.playing_index
        self.playing_index = cue_index
        for item in (old, cue_index):
            row = next((i for i, entry in enumerate(self.visible_entries())
                        if entry.index == item), None)
            if row is not None:
                self.dataChanged.emit(self.index(row, 0), self.index(row, 2),
                                      [Qt.ItemDataRole.BackgroundRole])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() or self.document is None else len(self.visible_entries())

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 3

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ("", "时间", "字幕正文")[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.document is None:
            return None
        entry = self.entry_at(index.row())
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return (str(entry.index), f"{entry.start}  →  {entry.end}", entry.text)[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 2:
            return "单击直接编辑；Ctrl+Z / Ctrl+Y 撤销或重做"
        if role == Qt.ItemDataRole.BackgroundRole and entry.index == self.playing_index:
            return QColor(COLORS.playback)
        return None

    def flags(self, index):
        flags = super().flags(index)
        return flags | Qt.ItemFlag.ItemIsEditable if index.column() == 2 else flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or index.column() != 2 or self.document is None:
            return False
        if self.entry_at(index.row()).text == str(value):
            return True
        self.document.edit_text(self.entry_at(index.row()).index, str(value))
        self.dataChanged.emit(index, index, [
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.EditRole,
        ])
        self.edited.emit()
        return True

    def set_live_text(self, index, value):
        """编辑器输入期间只更新文档，不反向刷新 editor，避免 caret 被重置。"""
        if not index.isValid() or index.column() != 2 or self.document is None:
            return False
        value = str(value)
        entry = self.entry_at(index.row())
        if entry.text == value:
            return True
        self.document.edit_text(entry.index, value)
        self.edited.emit()
        return True

    def finish_live_text(self, index, value):
        """结束编辑时再让 cell 重绘一次；活动 editor 不参与这次回写。"""
        if not index.isValid() or index.column() != 2 or self.document is None:
            return False
        value = str(value)
        entry = self.entry_at(index.row())
        if entry.text != value:
            self.document.edit_text(entry.index, value)
            self.edited.emit()
        self.dataChanged.emit(index, index, [
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.EditRole,
        ])
        return True


class InlineSubtitleEditor(QPlainTextEdit):
    """字幕正文专用 editor：明确支持鼠标拖选局部文字。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selection_anchor = None
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._selection_anchor = self.textCursor().position()

    def mouseMoveEvent(self, event):
        if (self._selection_anchor is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            position = self.cursorForPosition(event.position().toPoint()).position()
            cursor = self.textCursor()
            cursor.setPosition(self._selection_anchor)
            cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            self.ensureCursorVisible()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._selection_anchor = None


class SubtitleTextDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._table = parent
        self._editing = None

    @staticmethod
    def _key(index):
        return (index.row(), index.column())

    def createEditor(self, parent, option, index):
        editor = QPlainTextEdit(parent)
        _prepare_inline_editor(editor, option.font)
        editor.setProperty("autoslice_editor_initialised", False)
        editor.installEventFilter(parent.window())
        self._editing = self._key(index)
        try:
            self._table.viewport().update(self._table.visualRect(index))
        except RuntimeError:
            pass
        editor.destroyed.connect(lambda: self._clear_editing(index))
        editor.textChanged.connect(
            lambda: index.model().set_live_text(index, editor.toPlainText())
        )
        return editor

    def _clear_editing(self, index):
        if self._editing == self._key(index):
            self._editing = None
        try:
            self._table.viewport().update(self._table.visualRect(index))
        except RuntimeError:
            pass

    def eventFilter(self, editor, event):
        if isinstance(editor, QPlainTextEdit) and event.type() == QEvent.Type.FocusOut:
            # 不让 QStyledItemDelegate 因短暂 focus out 自动关闭 editor。
            # 是否结束字幕编辑由 DesktopWindow 的“点击编辑器外部”规则统一决定。
            return False
        return super().eventFilter(editor, event)

    def paint(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        if index.column() < 2:
            option.font.setPixelSize(SIZES.text_small)
            option.palette.setColor(option.palette.ColorRole.Text, QColor(COLORS.subtle if index.column() == 0 else COLORS.muted))
            option.palette.setColor(option.palette.ColorRole.HighlightedText, QColor(COLORS.muted))
        if self._editing == self._key(index):
            background = QStyleOptionViewItem(option)
            self.initStyleOption(background, index)
            background.text = ""
            super().paint(painter, background, index)
        else:
            super().paint(painter, option, index)
        painter.save()
        if index.column() == 0 and option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect.left(), option.rect.top(), 2, option.rect.height(), QColor(COLORS.accent_pressed))
        painter.setPen(QColor(COLORS.divider))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.restore()

    def setEditorData(self, editor, index):
        if editor.property("autoslice_editor_initialised"):
            return
        editor.blockSignals(True)
        try:
            editor.setPlainText(str(index.data(Qt.ItemDataRole.EditRole) or ""))
            editor.setProperty("autoslice_editor_initialised", True)
        finally:
            editor.blockSignals(False)

    def setModelData(self, editor, model, index):
        model.finish_live_text(index, editor.toPlainText())


def _prepare_inline_editor(editor: QPlainTextEdit, font) -> None:
    """字幕行内编辑框：贴合表格行、不换行、选中行底色。两条编辑入口共用。"""

    editor.setFont(font)
    editor.document().setDocumentMargin(0)
    editor.setTabChangesFocus(True)
    editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    editor.setContentsMargins(0, 0, 0, 0)
    editor.setViewportMargins(0, 0, 0, 0)
    editor.setAutoFillBackground(True)
    editor.viewport().setAutoFillBackground(True)
    editor.setStyleSheet(
        f"QPlainTextEdit {{ background: {COLORS.row_selected}; color: {COLORS.text}; "
        f"border: none; padding: 10px 6px 0 6px; "
        f"selection-background-color: {COLORS.accent_pressed}; }}"
        "QPlainTextEdit:focus { border: none; }"
    )
    editor.viewport().setStyleSheet(
        f"background: {COLORS.row_selected}; border: none;"
    )


class DesktopWindow(PreviewWindow):
    ai_progress = Signal(int, str, int, int)
    render_progress = Signal(str, object, str)

    def __init__(self, service: SubmissionProjectService | None = None,
                 storage: DesktopStorage | None = None):
        self.service = service or SubmissionProjectService()
        self.storage = storage or DesktopStorage()
        self.ai_service = AIReviewService(self.storage)
        self.render_service = SubtitleRenderService(self.storage)
        self.preview_service = SubtitlePreviewService(self.storage)
        self.waveform_cache = WaveformCache(self.storage.waveforms)
        self._render_jobs = {}
        self._render_result = None
        self._save_then_render = False
        self._close_after_render = False
        self.ai_session = None
        self._ai_running = False
        self._ai_cancel = None
        self._ai_generation = 0
        self._ai_selected = None
        self._ai_undo = []
        self._ai_redo = []
        self._ai_skip_undo = []
        self._ai_skip_redo = []
        self._ai_return_count = 0
        self.document: SubtitleDocument | None = None
        self.project: SubmissionProject | None = None
        self.selected_index: int | None = None
        self.selection = CueSelection()
        self.snapping = True
        self.waveform_visible = True
        self._loading = False
        self._saving = False
        self._scan_generation = 0
        self._jobs = []
        self._session = self.storage.read_session() or {}
        self._baseline = None
        # PreviewWindow.__init__ 会在构建 QTableView 时安装本类 eventFilter，
        # 所以这些状态必须在 super() 前就存在。
        self._inline_editor = None
        self._inline_editor_index = QModelIndex()
        self._inline_drag_anchor = None
        self._event_filter_busy = False
        super().__init__()
        self.cover_editor.status_changed.connect(self._show_transient_status)
        self.cover_editor.next_video_requested.connect(self._next_cover_video)
        self._timeline_side_pan = TimelineSidePanController(self.timeline, self)
        self.ai_progress.connect(self._show_ai_progress)
        self.render_progress.connect(self._show_render_progress)
        self.nav_badge = QLabel(self.nav_buttons[0])
        self.nav_badge.setObjectName("badge")
        self.nav_badge.move(37, 1)
        self.nav_badge.hide()
        self.setWindowTitle("AutoSlice · 字幕校对")
        try:
            self.player = MpvAdapter(int(self.video_widget.winId()), parent=self)
            self.player.status_changed.connect(self._player_status)
            self.player.error.connect(self._player_error)
        except (OSError, FileNotFoundError) as exc:
            self.player = None
            self.video_placeholder.setText(f"播放器不可用：{exc}")
        self._player_position = 0.0
        self._player_duration = 0.0
        self._player_paused = True
        self._media_ready = False
        self._pending_seek = None
        self._resume_pending = False
        self._player_load_token = 0
        self._hwdec = None
        self._waveform_generation = 0
        self._loop_enabled = False
        self._loop_cue_id = None
        self._loop_seek_pending = False
        self._audition_until = None
        self._pending_caret_click = None
        self._seek_guard_target = None
        self._seek_guard_until = 0.0
        self.model.edited.connect(self._edited)
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)
        self._draft_timer.timeout.connect(self._write_draft)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(35)
        self._preview_timer.timeout.connect(self._refresh_subtitle_preview)
        self._preview_attached = False
        self.commands = CommandDispatcher()
        for name, action in (
            ("play_pause", self._toggle_play),
            ("delete_selected_cues", self._delete),
            ("undo", self._undo), ("redo", self._redo),
            ("toggle_snapping", self._toggle_snapping),
            ("toggle_waveform", self._toggle_waveform),
            ("fit_timeline", self._fit_timeline),
            ("trim_start_to_playhead", self._trim_start),
            ("trim_end_to_playhead", self._trim_end),
            ("split_at_playhead", self._split_at_playhead),
        ):
            self.commands.register(name, action)
        self._shortcuts = {}
        for shortcut, action, command in (("Ctrl+Z", self._undo, "undo"), ("Ctrl+Y", self._redo, "redo"),
                                 ("Delete", self._delete, "delete_selected_cues"),
                                 ("Ctrl+Space", self._toggle_play, "play_pause"),
                                 ("Space", self._toggle_play, "play_pause"),
                                 ("F8", self._toggle_play, "play_pause"),
                                 ("F1", self._audition_current, None),
                                 ("F2", self._audition_before, None),
                                 ("F3", self._audition_after, None),
                                 ("F4", self._audition_once, None),
                                 ("Q", self._trim_start, "trim_start_to_playhead"),
                                 ("W", self._trim_end, "trim_end_to_playhead"),
                                 ("Ctrl+B", self._split_at_playhead, "split_at_playhead"),
                                 ("Esc", self._escape_context, None)):
            key = QShortcut(QKeySequence(shortcut), self)
            key.setContext(Qt.ShortcutContext.WindowShortcut)
            key.activated.connect(lambda name=command, fallback=action:
                                  self.commands.dispatch(name) if name else fallback())
            self._shortcuts[shortcut] = key
        # currentRowChanged 可能因关闭 inline editor / current index 内部变化而触发，
        # 不能拿它驱动 seek，否则时间轴空白点击会出现“目标位置→下一字幕→目标位置”的闪跳。
        # 真正的字幕点击由 _cue_clicked 显式处理。
        self._syncing_selection = False
        from PySide6.QtWidgets import QApplication
        QApplication.instance().focusChanged.connect(self._focus_changed)
        # 不再把 DesktopWindow 安装成 QApplication 全局 eventFilter。
        # 多个窗口/测试窗口并存时，全局 filter 会互相重入；只监听本窗口自己的控件。
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)
        self._restore_window()
        self._select_page(int(self._session.get("page", 0)) if self._session.get("page") in (0, 1, 2) else 0)
        QTimer.singleShot(0, self.refresh)

    def _cover_page(self):
        """构建独立 AutoCover 工作区，保持项目列表与字幕页共用。"""

        page = QWidget()
        page.setObjectName("workArea")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        heading = QWidget()
        heading.setFixedHeight(60)
        row = QHBoxLayout(heading)
        row.setContentsMargins(24, 0, 24, 0)
        row.addWidget(label("封面制作", "pageTitle"))
        row.addStretch()
        row.addWidget(label("与字幕页共享当前投稿项目", "muted"))
        layout.addWidget(heading)
        layout.addWidget(line())
        self.cover_editor = CoverEditorWidget(self.storage)
        layout.addWidget(self.cover_editor, 1)
        return page

    def _appbar(self):
        bar = QWidget()
        bar.setObjectName("appbar")
        bar.setFixedHeight(SIZES.appbar_height)
        row = QHBoxLayout(bar)
        row.setContentsMargins(22, 0, 20, 0)
        row.addWidget(label("AUTOSLICE", "brand"))
        row.addSpacing(SIZES.space_4)
        self.top_project = label("请选择项目", "projectTitle")
        self.top_project.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.top_project.setMinimumWidth(100)
        self.top_project.setMaximumWidth(520)
        row.addWidget(self.top_project)
        row.addStretch()
        self.app_status = _StatusLabel("")
        self.app_status.setObjectName("statusLabel")
        self.app_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.app_status.setMaximumWidth(360)
        row.addWidget(self.app_status)
        self.render_location = QPushButton("定位输出")
        self.render_location.setObjectName("quiet")
        self.render_location.setIcon(desktop_icon("folder", COLORS.muted))
        self.render_location.setIconSize(QSize(SIZES.icon_size, SIZES.icon_size))
        self.render_location.clicked.connect(self._locate_render_output)
        self.render_location.hide()
        row.addWidget(self.render_location)
        self.ai_return = QPushButton("查看待确认")
        self.ai_return.clicked.connect(self._return_to_ai)
        self.ai_return.hide()
        row.addWidget(self.ai_return)
        return bar

    def _project_rail(self):
        rail = QWidget()
        rail.setObjectName("projectRail")
        column = QVBoxLayout(rail)
        column.setContentsMargins(12, 20, 12, 12)
        column.setSpacing(8)
        heading = QHBoxLayout()
        heading.addWidget(label("投稿项目", "sectionTitle"))
        heading.addStretch()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        heading.addWidget(refresh)
        column.addLayout(heading)
        column.addWidget(line())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        self.projects_layout = QVBoxLayout(container)
        self.projects_layout.setContentsMargins(0, 0, 0, 0)
        self.projects_layout.setSpacing(4)
        self.projects_layout.addStretch()
        scroll.setWidget(container)
        column.addWidget(scroll, 1)
        self.project_group = QButtonGroup(self)
        self.project_group.setExclusive(True)
        self.project_buttons = {}
        self.scan_status = label(str(self.service.root), "subtle")
        self.scan_status.setWordWrap(True)
        column.addWidget(self.scan_status)
        return rail

    def _subtitle_work_area(self):
        area = QWidget()
        area.setObjectName("workArea")
        column = QVBoxLayout(area)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        heading = QWidget()
        heading.setObjectName("workToolbar")
        heading.setFixedHeight(58)
        row = QHBoxLayout(heading)
        row.setContentsMargins(24, 0, 24, 0)
        row.addWidget(label("视频", "subtle"))
        self.video_name = label("选择视频", "videoFile")
        self.video_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.video_name.setMinimumWidth(0)
        row.addWidget(self.video_name, 1)
        self.video_choice = QComboBox()
        self.video_choice.setMinimumWidth(160)
        self.video_choice.currentIndexChanged.connect(self._video_changed)
        row.addWidget(self.video_choice, 1)
        actions = QWidget()
        actions.setObjectName("workflowActions")
        action_row = QHBoxLayout(actions)
        action_row.setContentsMargins(2, 2, 2, 2)
        action_row.setSpacing(2)
        self.save_button = QPushButton("保存字幕")
        self.save_button.setObjectName("primary")
        self.save_button.setIcon(desktop_icon("save", COLORS.canvas))
        self.save_button.setIconSize(QSize(SIZES.icon_size, SIZES.icon_size))
        self.save_button.clicked.connect(self.save)
        self.save_button.setEnabled(False)
        self.render_button = QPushButton("压制字幕")
        self.render_button.setObjectName("secondary")
        self.render_button.setIcon(desktop_icon("render", COLORS.text))
        self.render_button.setIconSize(QSize(SIZES.icon_size, SIZES.icon_size))
        self.render_button.setToolTip("使用默认字幕样式，将已保存的校对字幕压制到新视频")
        self.render_button.clicked.connect(self._start_render)
        self.render_button.setEnabled(False)
        action_row.addWidget(self.save_button)
        action_row.addWidget(self.render_button)
        row.addWidget(actions)
        column.addWidget(heading)
        column.addWidget(line())
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.setHandleWidth(4)
        self.vertical_splitter.setChildrenCollapsible(False)
        self.vertical_splitter.addWidget(self._video_surface())
        self.vertical_splitter.addWidget(self._timeline())
        self.vertical_splitter.addWidget(self._subtitle_list())
        self.vertical_splitter.splitterMoved.connect(self._timeline_splitter_moved)
        self.vertical_splitter.setStretchFactor(0, 5)
        self.vertical_splitter.setStretchFactor(2, 3)
        column.addWidget(self.vertical_splitter, 1)
        return area

    def _video_surface(self):
        surface = QWidget()
        surface.setObjectName("videoSurface")
        surface.setMinimumHeight(200)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(12, 12, 12, 0)
        self.video_stack = QStackedWidget()
        self.video_placeholder = label("选择项目后可校对真实 SRT\n视频播放将在 Desktop-05 接入", "hint")
        self.video_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_stack.addWidget(self.video_placeholder)
        self.video_widget = QWidget()
        self.video_widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.video_widget.setStyleSheet("background: #000000;")
        self.video_stack.addWidget(self.video_widget)
        layout.addWidget(self.video_stack, 1)
        controls_bar = QWidget()
        controls_bar.setObjectName("playerControls")
        controls = QHBoxLayout(controls_bar)
        controls.setContentsMargins(8, 6, 8, 6)
        controls.setSpacing(SIZES.space_3)
        self.play_button = QPushButton("播放")
        self.play_button.setIcon(desktop_icon("play", COLORS.text))
        self.play_button.setIconSize(QSize(SIZES.icon_size, SIZES.icon_size))
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_button)
        self.preview_toggle = QPushButton("字幕预览")
        self.preview_toggle.setCheckable(True)
        self.preview_toggle.setChecked(True)
        self.preview_toggle.setToolTip("显示接近最终压制样式的实时字幕预览")
        self.preview_toggle.clicked.connect(self._toggle_subtitle_preview)
        controls.addWidget(self.preview_toggle)
        self.time_label = label("00:00:00 / 00:00:00", "timecode")
        controls.addWidget(self.time_label)
        controls.addStretch()
        volume_icon = label("")
        volume_icon.setPixmap(desktop_icon("volume").pixmap(SIZES.icon_size, SIZES.icon_size))
        volume_icon.setToolTip("音量")
        controls.addWidget(volume_icon)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(50)
        self.volume.setFixedWidth(90)
        self.volume.valueChanged.connect(self._volume_changed)
        controls.addWidget(self.volume)
        self.speed = QComboBox()
        for value in (0.75, 1.0, 1.25, 1.5, 2.0):
            self.speed.addItem(f"{value:g}×", value)
        self.speed.setCurrentIndex(1)
        self.speed.currentIndexChanged.connect(self._speed_changed)
        controls.addWidget(self.speed)
        layout.addWidget(controls_bar)
        return surface

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

    def _ai_panel(self):
        panel = QWidget()
        panel.setObjectName("aiPanel")
        panel.setMinimumWidth(SIZES.ai_collapsed_width)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.ai_toggle = QPushButton("‹")
        self.ai_toggle.setObjectName("quiet")
        self.ai_toggle.setFixedSize(32, 32)
        self.ai_toggle.clicked.connect(lambda: self._set_ai_open(not self._ai_open))
        header.addWidget(self.ai_toggle)
        self.ai_header = label("AI 建议", "sectionTitle")
        header.addWidget(self.ai_header)
        header.addStretch()
        self.ai_run = QPushButton("AI 检查")
        self.ai_run.setObjectName("quiet")
        self.ai_run.setFixedHeight(28)
        self.ai_run.setToolTip("重新检查会再次调用 AI 并消耗额度")
        self.ai_run.clicked.connect(self._start_ai_check)
        header.addWidget(self.ai_run)
        column.addLayout(header)
        column.addWidget(line())
        self.ai_content = QScrollArea()
        self.ai_content.setWidgetResizable(True)
        self.ai_content.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        ai_body = QWidget()
        content = QVBoxLayout(ai_body)
        content.setContentsMargins(16, 12, 16, 14)
        content.setSpacing(8)
        self.ai_note = label("AI 只生成待确认建议，不修改字幕。", "muted")
        self.ai_note.setWordWrap(True)
        content.addWidget(self.ai_note)

        self.ai_card = QWidget()
        self.ai_card.setObjectName("aiSuggestionCard")
        card = QVBoxLayout(self.ai_card)
        card.setContentsMargins(12, 10, 12, 10)
        card.setSpacing(6)

        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.ai_cue = label("", "subtle")
        self.ai_cue.setObjectName("aiMeta")
        meta.addWidget(self.ai_cue)
        meta.addStretch()
        self.ai_confidence = label("", "subtle")
        self.ai_confidence.setObjectName("aiMeta")
        meta.addWidget(self.ai_confidence)
        card.addLayout(meta)

        self.ai_original_label = label("原文", "subtle")
        self.ai_original_label.setObjectName("aiFieldLabel")
        card.addWidget(self.ai_original_label)
        self.ai_original = label("", "muted")
        self.ai_original.setObjectName("aiOriginal")
        self.ai_original.setWordWrap(True)
        self.ai_original.setMinimumWidth(0)
        self.ai_original.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ai_original.setMaximumHeight(40)
        card.addWidget(self.ai_original)

        self.ai_diff_label = label("建议", "subtle")
        self.ai_diff_label.setObjectName("aiFieldLabel")
        card.addWidget(self.ai_diff_label)
        self.ai_diff = QLabel("")
        self.ai_diff.setObjectName("aiDiff")
        self.ai_diff.setWordWrap(True)
        self.ai_diff.setTextFormat(Qt.TextFormat.RichText)
        self.ai_diff.setMinimumWidth(0)
        self.ai_diff.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ai_diff.setMaximumHeight(48)
        card.addWidget(self.ai_diff)

        self.ai_reason_label = label("原因", "subtle")
        self.ai_reason_label.setObjectName("aiFieldLabel")
        card.addWidget(self.ai_reason_label)
        self.ai_reason = label("", "subtle")
        self.ai_reason.setObjectName("aiReason")
        self.ai_reason.setWordWrap(True)
        self.ai_reason.setMinimumWidth(0)
        self.ai_reason.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.ai_reason.setMaximumHeight(54)
        card.addWidget(self.ai_reason)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.ai_accept = QPushButton("采纳")
        self.ai_accept.setObjectName("primary")
        self.ai_accept.clicked.connect(self._accept_ai)
        actions.addWidget(self.ai_accept, 2)
        self.ai_skip = QPushButton("跳过")
        self.ai_skip.setObjectName("secondary")
        self.ai_skip.clicked.connect(self._skip_ai)
        actions.addWidget(self.ai_skip, 1)
        card.addLayout(actions)
        content.addWidget(self.ai_card)
        self.ai_card.hide()

        self.ai_queue_header = label("待处理 0", "subtle")
        self.ai_queue_header.setObjectName("aiQueueHeader")
        content.addWidget(self.ai_queue_header)
        self.ai_queue_header.hide()
        self.ai_queue = QScrollArea()
        self.ai_queue.setWidgetResizable(True)
        self.ai_queue.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ai_queue.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.ai_queue.setMaximumHeight(132)
        self.ai_queue_widget = QWidget()
        self.ai_queue_layout = QVBoxLayout(self.ai_queue_widget)
        self.ai_queue_layout.setContentsMargins(0, 0, 0, 0)
        self.ai_queue_layout.setSpacing(2)
        self.ai_queue_layout.addStretch()
        self.ai_queue.setWidget(self.ai_queue_widget)
        content.addWidget(self.ai_queue)
        self.ai_queue.hide()
        content.addStretch(1)
        self.ai_content.setWidget(ai_body)
        column.addWidget(self.ai_content, 1)
        self.ai_collapsed_label = label("AI", "badge")
        column.addWidget(self.ai_collapsed_label, alignment=Qt.AlignmentFlag.AlignTop)
        return panel

    def _set_ai_open(self, open_):
        super()._set_ai_open(open_)
        self.ai_run.setVisible(open_)

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

    def _settings_page(self):
        page = QWidget()
        page.setObjectName("settingsSurface")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(32, 26, 32, 26)
        layout.setSpacing(16)
        layout.addWidget(label("设置", "pageTitle"))
        layout.addWidget(line())

        # ── 投稿目录 ──
        layout.addWidget(label("投稿目录", "sectionTitle"))
        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)
        directory = label(str(self.service.root), "muted")
        directory.setWordWrap(True)
        dir_row.addWidget(directory, 1)
        browse_dir = QPushButton("更改")
        browse_dir.setObjectName("quiet")
        browse_dir.setFixedHeight(28)
        browse_dir.setToolTip("选择投稿项目根目录")
        browse_dir.clicked.connect(self._browse_submission_root)
        dir_row.addWidget(browse_dir)
        layout.addLayout(dir_row)

        layout.addSpacing(8)
        layout.addWidget(line())
        layout.addSpacing(8)

        # ── 字幕压制 ──
        layout.addWidget(label("字幕压制", "sectionTitle"))
        layout.addWidget(label(
            f"默认样式：{DEFAULT_SUBTITLE_STYLE['font_name']} · "
            f"#{DEFAULT_SUBTITLE_STYLE['outline_color']} 描边", "muted"
        ))
        layout.addWidget(label("压制使用既有 FFmpeg 工作流，NVENC 可用时自动优先。", "muted"))

        layout.addSpacing(8)
        layout.addWidget(line())
        layout.addSpacing(8)

        # ── 封面 ──
        layout.addWidget(label("封面", "sectionTitle"))
        layout.addWidget(label("封面字体、默认样式和素材库在封面编辑器中直接管理。", "muted"))
        layout.addWidget(label("桌面草稿保存在用户应用数据目录，重启后可恢复。", "muted"))

        layout.addSpacing(8)
        layout.addWidget(line())
        layout.addSpacing(8)

        # ── AI ──
        layout.addWidget(label("AI 检查", "sectionTitle"))
        ai_help = label("沿用 AutoSlice 的 api_config.json 或环境变量配置。"
                        "AI 只在手动点击时运行，不会在页面加载或切换时自动调用。", "muted")
        ai_help.setWordWrap(True)
        layout.addWidget(ai_help)

        layout.addSpacing(8)
        layout.addWidget(line())
        layout.addSpacing(8)

        # ── 播放器 ──
        layout.addWidget(label("播放器", "sectionTitle"))
        player_status = "libmpv 已连接" if getattr(self, "player", None) is not None else "libmpv 未连接（仅预览模式）"
        layout.addWidget(label(player_status, "muted"))
        hwdec = getattr(self, "_hwdec", None)
        if hwdec:
            layout.addWidget(label(f"硬件解码：{hwdec}", "muted"))

        layout.addStretch()
        scroll.setWidget(body)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(scroll)
        return page

    def _browse_submission_root(self):
        """让用户选择投稿目录。"""
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "选择投稿目录", str(self.service.root))
        if path:
            self.service.root = Path(path)
            self._show_transient_status(f"投稿目录已更新：{path}")
            self.refresh()

    def _set_filter(self, pending):
        self._commit_editor()
        self._pending_filter = pending
        ids = {item.cue_id for item in self.ai_session.pending} if pending and self.ai_session else set()
        self.model.set_pending_filter(ids if pending else None)
        self._select_cue(self.selected_index)

    def _render_ai(self):
        if not hasattr(self, "ai_queue_layout"):
            return
        pending = self.ai_session.pending if self.ai_session else []
        count = len(pending)
        self.filter_pending.setText(f"待确认 {count}")
        badge_count = max(count, self._ai_return_count)
        self.nav_buttons[0].setToolTip(f"字幕校对 · 待确认 {badge_count}" if badge_count else "字幕校对")
        if hasattr(self, "nav_badge"):
            self.nav_badge.setText(str(badge_count))
            self.nav_badge.adjustSize()
            self.nav_badge.setVisible(badge_count > 0)
        self.ai_collapsed_label.setText(f"AI {count}" if count else "AI")
        self.ai_run.setEnabled(bool(self.document))
        self.ai_run.setText("取消检查" if self._ai_running else
                            "重新检查" if self.ai_session else "AI 检查")
        pending_ids = {item.suggestion_id for item in pending}
        if self._ai_selected not in pending_ids:
            self._ai_selected = pending[0].suggestion_id if pending else None
        selected_position = next((i for i, item in enumerate(pending, 1)
                                  if item.suggestion_id == self._ai_selected), 0)
        while self.ai_queue_layout.count() > 1:
            item = self.ai_queue_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for item in pending:
            summary = (f"{item.original_text} → {item.suggested_text}"
                       if item.original_text != item.suggested_text else item.original_text)
            full_text = f"第 {item.cue_id} 条  {summary}  · 待确认"
            button = _ElidedQueueButton(full_text)
            button.setObjectName("aiQueueItem")
            button.setToolTip(
                f"原文：{item.original_text}\n建议：{item.suggested_text}\n原因：{item.reason}"
            )
            button.setAccessibleName(f"第 {item.cue_id} 条待处理问题")
            button.setProperty("suggestion_id", item.suggestion_id)
            button.setCheckable(True)
            button.setChecked(item.suggestion_id == self._ai_selected)
            button.setFixedHeight(30)
            button.clicked.connect(lambda _checked=False, sid=item.suggestion_id: self._select_ai(sid))
            self.ai_queue_layout.insertWidget(self.ai_queue_layout.count() - 1, button)
        self.ai_header.setText(f"AI 建议  {selected_position} / {count}" if self.ai_session else "AI 建议")
        self.ai_queue_header.setText(f"待处理 {count}")
        self.ai_queue_header.setVisible(count > 0)
        self.ai_queue.setVisible(count > 0)
        if count:
            self.ai_queue.setFixedHeight(min(132, count * 32 + 2))
        if self.ai_session and not count and not self._ai_running:
            self.ai_note.setText("已处理所有建议。需要时可重新检查。")
        routine_notes = ("AI 只生成待确认建议，不修改字幕。", "检查完成。建议需逐条确认。",
                         "已恢复待确认建议。")
        self.ai_note.setVisible(not count or self._ai_running or
                                self.ai_note.text() not in routine_notes)
        self._show_ai_detail()
        if getattr(self, "_pending_filter", False):
            self.model.set_pending_filter({item.cue_id for item in pending})

    @staticmethod
    def _diff_html(original, suggested):
        def wrap_text(value):
            return "&#8203;".join(html.escape(char) for char in value)

        pieces = []
        for kind, a, b, c, d in difflib.SequenceMatcher(None, original, suggested).get_opcodes():
            old = wrap_text(original[a:b])
            new = wrap_text(suggested[c:d])
            if kind == "equal":
                pieces.append(new)
            else:
                if old:
                    pieces.append(f'<span style="color:{COLORS.ai_removed};text-decoration:line-through">{old}</span>')
                if new:
                    pieces.append(f'<span style="color:{COLORS.ai_added};font-weight:600">{new}</span>')
        return "".join(pieces).replace("\n", "<br>")

    def _show_ai_detail(self):
        item = self.ai_session.find(self._ai_selected) if self.ai_session and self._ai_selected else None
        if item is None:
            self.ai_card.hide()
            self.ai_diff.hide()
            self.ai_cue.hide()
            self.ai_original.hide()
            self.ai_reason.hide()
            self.ai_confidence.hide()
            self.ai_accept.hide()
            self.ai_skip.hide()
            return
        entry = next((entry for entry in self.document.entries if entry.index == item.cue_id), None) if self.document else None
        conflict = entry is None or entry.text != item.original_text
        self.ai_card.show()
        self.ai_cue.setText(f"第 {item.cue_id} 条")
        self.ai_cue.show()
        self.ai_original.setText(item.original_text)
        self.ai_original.setToolTip(item.original_text)
        self.ai_original.show()
        self.ai_diff.setText(self._diff_html(item.original_text, item.suggested_text))
        self.ai_diff.setToolTip(f"建议：{item.suggested_text}")
        self.ai_diff.show()
        reason = item.reason + ("\n字幕已修改或删除；建议基于旧文本，请重新判断或跳过。" if conflict else "")
        self.ai_reason.setText(reason)
        self.ai_reason.setToolTip(reason)
        self.ai_reason.show()
        self.ai_confidence.setText(f"置信度 {item.confidence:.0%}" if item.confidence is not None else "")
        self.ai_confidence.setVisible(item.confidence is not None)
        self.ai_accept.setEnabled(not conflict and not self._saving)
        self.ai_accept.show()
        self.ai_skip.show()

    def _select_ai(self, suggestion_id):
        self._ai_selected = suggestion_id
        if self.ai_session:
            pending = self.ai_session.pending
            selected_position = next((i for i, item in enumerate(pending, 1)
                                      if item.suggestion_id == suggestion_id), 0)
            self.ai_header.setText(f"AI 建议  {selected_position} / {len(pending)}")
        for index in range(self.ai_queue_layout.count() - 1):
            button = self.ai_queue_layout.itemAt(index).widget()
            if button is not None:
                button.setChecked(button.property("suggestion_id") == suggestion_id)
        self._show_ai_detail()
        if not self.ai_session or not self.document:
            return
        item = self.ai_session.find(suggestion_id)
        entry = next((entry for entry in self.document.entries if entry.index == item.cue_id), None)
        if entry is not None:
            self.selection.choose(entry.index, self._cue_order())
            self.selected_index = entry.index
            if getattr(self, "_pending_filter", False):
                self.model.set_pending_filter({s.cue_id for s in self.ai_session.pending})
            self._select_cue(entry.index)
            self._seek_to(srt_timestamp_seconds(entry.start))
            self._save_session()

    def _accept_ai(self):
        if not self.document or not self.ai_session or not self._ai_selected:
            return
        self._commit_editor()
        item = self.ai_session.find(self._ai_selected)
        entry = next((entry for entry in self.document.entries if entry.index == item.cue_id), None)
        if entry is None or entry.text != item.original_text:
            self._show_ai_detail()
            return
        before = document_hash(self.document.entries)
        self.document.edit_text(item.cue_id, item.suggested_text)
        after = document_hash(self.document.entries)
        self._ai_undo.append((before, after, item.suggestion_id))
        self._ai_redo.clear()
        self.ai_session.mark(item.suggestion_id, "accepted", self.document.entries)
        self._save_ai_session()
        self.model.set_document(self.document)
        self._edited()
        self._render_ai()
        if self.ai_session.pending:
            next_item = next((candidate for candidate in self.ai_session.pending
                              if candidate.cue_id > item.cue_id), self.ai_session.pending[0])
            self._select_ai(next_item.suggestion_id)
        else:
            self._select_cue(item.cue_id)

    def _skip_ai(self):
        if not self.document or not self.ai_session or not self._ai_selected:
            return
        current = self.ai_session.find(self._ai_selected)
        self._ai_skip_undo.append((document_hash(self.document.entries), self._ai_selected))
        self._ai_skip_redo.clear()
        self.ai_session.mark(self._ai_selected, "skipped", self.document.entries)
        self._save_ai_session()
        self._render_ai()
        if self.ai_session.pending:
            next_item = next((candidate for candidate in self.ai_session.pending
                              if candidate.cue_id > current.cue_id), self.ai_session.pending[0])
            self._select_ai(next_item.suggestion_id)

    def _save_ai_session(self):
        if self.ai_session and self.document and self.project:
            try:
                self.ai_service.save(self.document, self.project.title, self.ai_session)
            except (OSError, ValueError) as exc:
                self.ai_note.setText(f"建议状态暂未保存：{exc}")

    def _return_to_ai(self):
        self._select_page(0)
        project = next((p for p in self.service.snapshot.projects if p.id == self._ai_return_project), None)
        if project:
            self._session["video_path"] = getattr(self, "_ai_return_video_path", "")
            self._select_real_project(project)
        if self.document and self.ai_session and self.ai_session.pending:
            self._set_ai_open(True)
            self._select_ai(self.ai_session.pending[0].suggestion_id)
        self._ai_return_count = 0
        self.ai_return.hide()
        self._render_ai()

    def _start_ai_check(self):
        if self._ai_running:
            self._ai_cancel.set()
            self.ai_note.setText("正在取消检查…")
            return
        if not self.document or not self.project:
            return
        self._commit_editor()
        if self.ai_session:
            choice = QMessageBox.question(self, "重新检查字幕",
                                          "重新检查会再次调用 AI 并消耗额度，确定继续吗？")
            if choice != QMessageBox.StandardButton.Yes:
                return
        snapshot = copy.copy(self.document)
        snapshot.entries = list(self.document.entries)
        project = self.project
        force = self.ai_session is not None
        self._ai_running = True
        self._ai_cancel = threading.Event()
        cancellation = self._ai_cancel
        self._ai_generation += 1
        generation = self._ai_generation
        self.app_status.setText(f"{project.title} · AI 检查中")
        self.ai_note.setText("后台检查中，可切换页面。")
        self._render_ai()
        def action():
            def progress(_message, _step, _total):
                if cancellation.is_set():
                    raise RuntimeError("检查已取消")
                self.ai_progress.emit(generation, project.title, _step, _total)
            result = self.ai_service.check(snapshot, project.title, force=force,
                                          progress_callback=progress)
            if cancellation.is_set():
                raise RuntimeError("检查已取消")
            self.ai_service.save(snapshot, project.title, result)
            return result
        self._run(action, lambda result, error: self._ai_finished(
            generation, project, snapshot, result, error))

    def _show_ai_progress(self, generation, project_title, step, total):
        if generation == self._ai_generation and self._ai_running:
            self.app_status.setText(f"{project_title} · AI 检查中"
                                    + (f" ({step}/{total} 批)" if total > 0 else ""))

    def _ai_finished(self, generation, project, snapshot, result, error):
        if generation != self._ai_generation:
            return
        self._ai_running = False
        if error:
            message = str(error)
            cancelled = "检查已取消" in message
            self.app_status.setText(f"{project.title} · {'检查已取消' if cancelled else 'AI 检查失败'}")
            self.ai_note.setText("检查已取消，可稍后主动重试。" if cancelled else
                                 "未配置 AI，请在设置页配置后主动重试。" if "未配置" in message else
                                 f"检查失败：{message}")
        else:
            self._show_transient_status("✓ AI 检查完成")
            self.ai_note.setText("检查完成。建议需逐条确认。")
            if self.document and self.document.video.path == snapshot.video.path and document_hash(self.document.entries) == result.content_hash:
                self.ai_session = result
                self._ai_selected = None
                self._render_ai()
                if result.pending:
                    self._set_ai_open(True)
                    if self.pages.currentIndex() != 0:
                        self._ai_return_project = project.id
                        self._ai_return_video_path = snapshot.video.path
                        self._ai_return_count = len(result.pending)
                        self.ai_return.setText(f"查看 {project.title} 的 {len(result.pending)} 条建议")
                        self.ai_return.show()
            elif self.document and self.document.video.path == snapshot.video.path:
                self.app_status.setText(f"{project.title} · 字幕已变化，建议未应用")
                self.ai_note.setText("检查期间字幕已变化，请主动重新检查。")
            elif result.pending:
                self._ai_return_project = project.id
                self._ai_return_video_path = snapshot.video.path
                self._ai_return_count = len(result.pending)
                self.ai_return.setText(f"查看 {project.title} 的 {len(result.pending)} 条建议")
                self.ai_return.show()
        self._render_ai()

    def _run(self, action, callback):
        job = BackgroundJob(action)
        self._jobs.append(job)
        def done(result, error):
            self._jobs.remove(job)
            callback(result, error)
        job.signals.finished.connect(done)
        QThreadPool.globalInstance().start(job)

    def refresh(self):
        if self._loading or self._saving or not self._resolve_unsaved():
            return
        self._scan_generation += 1
        generation = self._scan_generation
        self.scan_status.setText("正在扫描投稿目录…")
        self._run(self.service.refresh, lambda result, error: self._scanned(generation, result, error))

    def _scanned(self, generation, snapshot, error):
        if generation != self._scan_generation:
            return
        if error:
            self.scan_status.setText(f"扫描失败：{error}")
            return
        assert isinstance(snapshot, ProjectSnapshot)
        self.scan_status.setText(f"{snapshot.status} · {len(snapshot.projects)} 个项目")
        self.cover_editor.set_project_list(snapshot.projects)
        while self.projects_layout.count() > 1:
            item = self.projects_layout.takeAt(0)
            item.widget().deleteLater()
        self.project_buttons.clear()
        for project in snapshot.projects:
            status = project.status
            if status == "素材已识别":
                status = "字幕已保存" if any(video.has_corrected_srt for video in project.videos) else ""
            button = ProjectItem(project.title, status, False)
            self.project_group.addButton(button)
            button.clicked.connect(lambda _checked=False, p=project: self._select_real_project(p))
            self.projects_layout.insertWidget(self.projects_layout.count() - 1, button)
            self.project_buttons[project.id] = button
        project_id = self.project.id if self.project else self._session.get("project_id")
        selected = next((p for p in snapshot.projects if p.id == project_id), None)
        if selected:
            self.project = None
            self.document = None
            self.model.set_document(None)
            self.timeline.set_document(None)
            self._select_real_project(selected)
        elif self.project:
            self.project = None
            self.document = None
            self.model.set_document(None)
            self.timeline.set_document(None)
            self.top_project.setText("请选择项目")
            self.video_name.setText("选择视频")
            self.cover_editor.set_context(None, None)

    def _select_real_project(self, project):
        if self._loading or self._saving:
            self._restore_project_button()
            return
        if self.project and project.id == self.project.id and self.document is not None:
            self.project_buttons[project.id].setChecked(True)
            return
        if not self._resolve_unsaved():
            self._restore_project_button()
            return
        self.project = project
        self.ai_session = None
        self._ai_selected = None
        self._pending_filter = False
        self._render_ai()
        self.selected_index = None
        self.document = None
        self.model.set_document(None)
        self.timeline.set_document(None)
        self.top_project.setText(project.title if len(project.title) <= 35 else project.title[:35] + "…")
        self.top_project.setToolTip(project.title)
        self.project_buttons[project.id].setChecked(True)
        self.cover_editor.set_context(project, None)
        self.video_choice.blockSignals(True)
        self.video_choice.clear()
        for video in project.videos:
            self.video_choice.addItem(video.name, video)
        preferred = self._session.get("video_path")
        index = next((i for i, video in enumerate(project.videos) if video.path == preferred), 0)
        self.video_choice.setCurrentIndex(index if project.videos else -1)
        self.video_choice.blockSignals(False)
        single_video = len(project.videos) == 1
        self.video_choice.setVisible(len(project.videos) > 1)
        self.video_name.setVisible(single_video or not project.videos)
        self.video_name.setText(project.videos[index].name if project.videos else "该项目没有视频")
        self.video_name.setToolTip(project.videos[index].name if project.videos else "")
        if project.videos:
            self._load_video(project.videos[index])
        else:
            self._media_ready = False
            self.play_button.setEnabled(False)
            if self.player:
                self.player.pause()
            self.video_placeholder.setText("该项目没有视频")
            self.video_stack.setCurrentIndex(0)
            self.subtitle_status.setText(project.error or "该项目没有视频")
        self._save_session()

    def _next_cover_video(self):
        """封面页“下一个”：同项目的下一个视频，否则列表里的下一个项目；页面停在封面。"""

        if self.project is None:
            return
        current = self.cover_editor.video
        videos = list(self.project.videos)
        index = next((i for i, video in enumerate(videos) if current is not None and video.path == current.path), -1)
        if 0 <= index < len(videos) - 1:
            self.video_choice.setCurrentIndex(index + 1)
            return
        projects = [project for project in self.service.snapshot.projects if project.videos]
        position = next((i for i, project in enumerate(projects) if project.id == self.project.id), -1)
        if position + 1 >= len(projects):
            self._show_transient_status("已经是投稿列表里的最后一个视频")
            return
        self._select_real_project(projects[position + 1])
        if self.project is projects[position + 1]:
            self._show_transient_status(f"已切到下一个：{self.project.title}")

    def _restore_project_button(self):
        if self.project and self.project.id in self.project_buttons:
            self.project_buttons[self.project.id].setChecked(True)

    def _video_changed(self, index):
        if index < 0 or self.project is None:
            return
        if self._loading or self._saving:
            self.video_choice.blockSignals(True)
            old_path = self.document.video.path if self.document else self._session.get("video_path")
            old = next((i for i, v in enumerate(self.project.videos) if v.path == old_path), 0)
            self.video_choice.setCurrentIndex(old)
            self.video_choice.blockSignals(False)
            return
        video = self.video_choice.itemData(index)
        if self.document and self.document.video.path == video.path:
            return
        if not self._resolve_unsaved():
            if self.document:
                old = next((i for i, v in enumerate(self.project.videos)
                            if v.path == self.document.video.path), 0)
                self.video_choice.blockSignals(True)
                self.video_choice.setCurrentIndex(old)
                self.video_choice.blockSignals(False)
            return
        self._load_video(video)

    def _load_video(self, video):
        if self.project is not None:
            self.cover_editor.set_context(self.project, video)
        self.ai_session = None
        self._ai_selected = None
        self._pending_filter = False
        self._render_ai()
        self._media_ready = False
        self._preview_attached = False
        self._pending_seek = None
        self.play_button.setEnabled(False)
        self._player_paused = True
        self._set_play_button(True)
        if self.player:
            self._player_load_token += 1
            token = self._player_load_token
            self.video_stack.setCurrentIndex(1)
            self._player_duration = 0.0
            self._player_position = 0.0
            self._resume_pending = self._session.get("video_path") == video.path
            self.time_label.setText("正在加载视频…")
            self.player.load(video.path)
            QTimer.singleShot(20000, lambda: self._check_player_load(token))
        else:
            self.video_stack.setCurrentIndex(0)
        self._loading = True
        self.document = None
        self.model.set_document(None)
        self.timeline.set_document(None)
        self.subtitle_status.setText("正在加载字幕…")
        project = self.project
        def action():
            document = SubtitleDocument.load(video)
            baseline = self.storage.capture_baseline(
                video.srt_path, dependencies=[video.corrected_srt_path]
            )
            draft = self.storage.read_draft(
                "subtitle", project.directory, video.srt_path,
                dependencies=[video.corrected_srt_path],
            )
            return document, baseline, draft
        self._run(action, lambda result, error: self._loaded(project, video, result, error))

    def _loaded(self, project, video, result, error):
        self._loading = False
        if self.project is not project or self.video_choice.currentData() != video:
            return
        if error:
            self.subtitle_status.setText(f"字幕无法加载：{error}")
            return
        document, self._baseline, draft = result
        if draft.status == "ready":
            choice = QMessageBox.question(
                self, "恢复字幕草稿", "发现未正式保存的字幕草稿。是否恢复？\n正式 SRT 不会因此写入。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if choice == QMessageBox.StandardButton.Yes:
                try:
                    self.selected_index = document.restore_draft(draft.payload)
                except ValueError as exc:
                    QMessageBox.warning(self, "草稿无法恢复", str(exc))
            else:
                self._remove_draft(document)
        elif draft.status not in ("missing",):
            QMessageBox.warning(self, "草稿需要检查",
                                f"字幕草稿状态：{draft.status}。已保留草稿，没有自动应用。")
        self.document = document
        self.model.set_document(document)
        self.timeline.set_document(document)
        self.timeline.set_waveform(None, self._player_duration or None)
        self._start_waveform(video.path)
        self.ai_session, ai_status = self.ai_service.load(document, project.title)
        if ai_status == "missing":
            try:
                self.ai_service.config_loader()
            except (OSError, ValueError):
                ai_status = "unconfigured"
        self.ai_note.setText("旧建议已过期，请主动重新检查。" if ai_status == "stale" else
                             "未配置 AI，请在设置页配置后主动检查。" if ai_status == "unconfigured" else
                             "已恢复待确认建议。" if ai_status == "ready" else
                             "AI 只生成待确认建议，不修改字幕。")
        self._render_ai()
        if getattr(self, "_ai_return_project", None) == project.id and self.ai_session and self.ai_session.pending:
            self._set_ai_open(True)
            self._select_ai(self.ai_session.pending[0].suggestion_id)
        if self.selected_index is None and self._session.get("video_path") == video.path:
            self.selected_index = self._session.get("cue_index")
        self._select_cue(self.selected_index)
        self._update_status()
        if self._media_ready and self.preview_toggle.isChecked():
            self._refresh_subtitle_preview()
        self._save_session()

    def _start_waveform(self, video_path):
        self._waveform_generation += 1
        generation = self._waveform_generation
        cached = self.waveform_cache.load(video_path)
        if cached is not None:
            self.timeline.set_waveform(cached.samples, cached.duration)
            return
        self.timeline.set_waveform(None, self._player_duration or None)
        self._show_transient_status("正在生成波形…")
        self._run(
            lambda: self.waveform_cache.load_or_generate(video_path),
            lambda result, error: self._waveform_ready(generation, result, error),
        )

    def _waveform_ready(self, generation, result, error):
        if generation != self._waveform_generation or self.document is None:
            return
        if error:
            self._show_transient_status(f"波形生成失败：{error}")
            return
        self.timeline.set_waveform(result.samples, result.duration)
        self._show_transient_status("波形已就绪")

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

    def _is_timeline_input_widget(self, widget) -> bool:
        current = widget
        timeline_scroll = getattr(self, "timeline_scroll", None)
        while current is not None:
            if current is getattr(self, "timeline", None) or current is timeline_scroll:
                return True
            current = current.parentWidget() if hasattr(current, "parentWidget") else None
        return False

    def _timeline_side_button_direction(self, button):
        if button == Qt.MouseButton.BackButton:
            return -1
        if button == Qt.MouseButton.ForwardButton:
            return 1
        return 0

    def _stop_timeline_side_pan(self, event_type=None):
        controller = getattr(self, "_timeline_side_pan", None)
        if controller is not None:
            if event_type is None:
                controller.stop()
            else:
                controller.stop_for_event(event_type)

    def event(self, event):
        if event.type() in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide,
                            QEvent.Type.Close):
            self._stop_timeline_side_pan(event.type())
        return super().event(event)

    def _focus_changed(self, _old, now):
        # 不用 focus loss 自动提交：同一编辑单元格内部的鼠标点击在 Qt 中可能
        # 先短暂触发 focus 迁移，若此时提交会让用户无法二次点击定位 caret。
        # 真正的“点到编辑器外部就结束编辑”由全局 MouseButtonPress 过滤器负责。
        self._sync_shortcuts(now)

    def _sync_shortcuts(self, focus=None):
        """字幕快捷键只在字幕页生效；封面页的 Delete、撤销等交给封面编辑器。"""

        if not getattr(self, "_shortcuts", None):
            return
        subtitle_page = self.pages.currentIndex() == 0
        editing = self._is_subtitle_editor_widget(focus) or self._subtitle_editor_open()
        for key, shortcut in self._shortcuts.items():
            text_sensitive = key in ("Space", "Delete", "Q", "W", "Ctrl+B")
            shortcut.setEnabled(subtitle_page and not (text_sensitive and editing))

    def _escape_context(self):
        if self._subtitle_editor_open():
            self._commit_editor()
            self.table.setFocus()
            return
        self.selection.clear()
        self.selected_index = None
        self.timeline.set_selection(None, set(), center=False)
        self.table.clearSelection()

    def eventFilter(self, watched, event):
        if getattr(self, "_event_filter_busy", False):
            return False
        self._event_filter_busy = True
        try:
            return self._event_filter_impl(watched, event)
        finally:
            self._event_filter_busy = False

    def _event_filter_impl(self, watched, event):
        if self._subtitle_editor_open() and self._handle_inline_editor_mouse(watched, event):
            return True
        if (event.type() == QEvent.Type.MouseButtonPress
                and self._subtitle_editor_open()):
            watched_inside = self._is_subtitle_editor_widget(watched)
            geometry_inside = self._event_hits_inline_editor(event)
            if watched_inside or geometry_inside:
                # 事件源本身属于 editor 时优先相信控件层级，避免高 DPI / 坐标换算
                # 在边缘出现 1px 误判，导致“点文字却退出编辑”的反复横跳。
                if not watched_inside:
                    self._place_caret_from_global(event)
                    event.accept()
                    return True
            else:
                # 只有事件源和屏幕坐标都确认在 editor 外部才退出。
                self._commit_editor()
        if event.type() == QEvent.Type.MouseButtonPress:
            direction = self._timeline_side_button_direction(event.button())
            if direction and self._is_timeline_input_widget(watched):
                self._timeline_side_pan.start(direction)
                event.accept()
                return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            # 释放可能发生在时间轴外部；只要当前确实在连续平移，就应立即停下。
            direction = self._timeline_side_button_direction(event.button())
            if direction and getattr(self._timeline_side_pan, "direction", 0):
                self._timeline_side_pan.stop()
                event.accept()
                return True
        if watched is self.table.viewport() and event.type() == QEvent.Type.MouseButtonPress \
                and event.button() == Qt.MouseButton.LeftButton:
            index = self.table.indexAt(event.position().toPoint())
            if index.isValid() and index.column() == 2 and self.document is not None:
                self._pending_caret_click = (
                    index.row(),
                    index.column(),
                    event.position().toPoint(),
                )
            else:
                self._pending_caret_click = None
        if isinstance(watched, QPlainTextEdit) and event.type() == QEvent.Type.KeyPress:
            if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                    and not event.modifiers()):
                self._commit_editor()
                self.table.setFocus()
                return True
            if (event.key() == Qt.Key.Key_Z
                    and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
                self._undo()
                return True
            if (event.key() == Qt.Key.Key_Y
                    and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
                self._redo()
                return True
        return super().eventFilter(watched, event)

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

    def _audition(self, side):
        bounds = self._entry_bounds()
        if bounds is None:
            return
        start, end = bounds
        if side == "start":
            self._play_window(max(0.0, start - .45), min(self._player_duration, start + .5))
        else:
            self._play_window(max(0.0, end - .5), min(self._player_duration, end + .45))

    def _audition_start(self):
        self._audition("start")

    def _audition_end(self):
        self._audition("end")

    def _audition_current(self):
        bounds = self._entry_bounds()
        if bounds:
            self._play_window(max(0.0, bounds[0] - .2),
                              min(self._player_duration, bounds[1] + .2))

    def _audition_before(self):
        bounds = self._entry_bounds()
        if bounds:
            self._play_window(max(0.0, bounds[0] - .5), bounds[0])

    def _audition_after(self):
        bounds = self._entry_bounds()
        if bounds:
            self._play_window(bounds[1], min(self._player_duration, bounds[1] + .5))

    def _audition_once(self):
        bounds = self._entry_bounds()
        if bounds:
            self._play_window(bounds[0], min(self._player_duration, bounds[1]))

    def _play_window(self, start, end):
        if not self.player or not self._media_ready or end <= start:
            return
        self._audition_until = end
        self._loop_enabled = False
        self._seek_to(start)
        self._player_paused = False
        self.player.seek(start, pause=False)
        self.player.play()
        self._set_play_button(False)

    def _set_loop(self, enabled):
        self._loop_enabled = bool(enabled)
        self._audition_until = None
        self._loop_cue_id = self.selection.active if enabled else None
        self._loop_seek_pending = False
        self._show_transient_status("已开启当前字幕循环" if enabled else "已关闭当前字幕循环")

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

    def _seek_to(self, seconds):
        if not self.player:
            return
        if not self._media_ready:
            self._pending_seek = max(0.0, float(seconds))
            return
        target = max(0.0, min(float(seconds), self._player_duration))
        self._pending_seek = None
        self._player_paused = True
        self._set_play_button(True)
        self._player_position = target
        self._seek_guard_target = target
        self._seek_guard_until = time.monotonic() + 1.0
        # 主动 seek 后先清掉旧的“正在播放字幕”高亮，避免 mpv 的旧 position
        # 回调把下一条字幕短暂/稳定染成选中态。
        if self.document:
            self.model.set_playing_index(None)
        self.timeline.set_playing_cue(None)
        self.timeline.set_playhead(target, playing=False)
        self.player.seek(target, pause=True)

    def _edited(self):
        if self.ai_session and self.document and self.project:
            self.ai_session.content_hash = document_hash(self.document.entries)
            self._save_ai_session()
            self._show_ai_detail()
        self._update_status()
        if self.document and not self.document.dirty:
            self._draft_timer.stop()
            try:
                self._remove_draft(self.document)
            except OSError as exc:
                self.app_status.setText(f"草稿清理失败：{exc}")
        else:
            self._draft_timer.start()
        self._preview_timer.start()

    def _refresh_subtitle_preview(self):
        if not self.document or not self.player or not self.preview_toggle.isChecked():
            return
        try:
            path = self.preview_service.render(self.document.video.path, self.document.entries)
            self.player.set_subtitle(path)
            self._preview_attached = True
        except (OSError, ValueError) as exc:
            self._show_transient_status(f"字幕预览暂不可用：{exc}")

    def _toggle_subtitle_preview(self):
        if not self.player:
            return
        if self.preview_toggle.isChecked():
            self._refresh_subtitle_preview()
        else:
            self.player.clear_subtitle()
            self._preview_attached = False

    def _update_status(self):
        if self.document is None:
            self.subtitle_status.setText("请选择有字幕的视频")
            self.save_button.setEnabled(False)
            self.render_button.setEnabled(False)
            self.render_button.setText("压制字幕")
            self.render_button.setIcon(desktop_icon("render", COLORS.text))
            self.timeline_overlap.setText("")
            return
        suffix = " · 未保存" if self.document.dirty else " · 已保存"
        quality = self.preview_service.quality_flags(self.document.entries)
        quality_text = f" · ⚠ {len(quality)} 条待检查" if quality else ""
        self.subtitle_status.setText(f"全部 {len(self.document.entries)} 条{suffix}{quality_text}")
        self.subtitle_status.setToolTip("\n".join(
            f"第 {index} 条：{'、'.join(issues)}" for index, issues in quality.items()
        ))
        self.save_button.setEnabled(self.document.dirty and not self._saving)
        running = bool(self.project and self.project.id in self._render_jobs)
        self.render_button.setEnabled(not self._saving and bool(self.project) and not running)
        self.render_button.setText("压制中…" if running else "压制字幕")
        self.render_button.setIcon(desktop_icon("render", COLORS.subtle if running else COLORS.text))
        self.timeline_overlap.setText("存在重叠" if self.timeline.overlap_count() else "")

    def _show_transient_status(self, text: str, timeout: int = 3600) -> None:
        """显示短反馈；下一次真实后台状态会自然覆盖它。"""
        self.app_status.setText(text)
        QTimer.singleShot(timeout, lambda: self.app_status.setText("")
                          if self.app_status.text() == text else None)

    def _write_draft(self):
        if not self.document or not self.project:
            return
        if not self.document.dirty:
            self._remove_draft(self.document)
            return
        try:
            self.storage.save_draft(
                "subtitle", self.project.directory, self.document.source_path,
                self.document.draft_payload(self.selected_index),
                dependencies=[self.document.corrected_path], baseline=self._baseline,
            )
            self._save_session()
        except OSError as exc:
            self.app_status.setText(f"草稿保存失败：{exc}")

    def _remove_draft(self, document):
        self.storage.draft_path("subtitle", self.project.directory,
                                document.source_path).unlink(missing_ok=True)

    def _resolve_unsaved(self):
        if self._saving or self._loading:
            return False
        self._commit_editor()
        if not self.document or not self.document.dirty:
            return True
        self._draft_timer.stop()
        self._write_draft()
        dialog = QMessageBox(self)
        dialog.setWindowTitle("未保存的字幕")
        dialog.setText("当前字幕有未正式保存的修改。可先返回点击“保存校对字幕”，或选择离开时如何处理草稿。")
        keep = dialog.addButton("保留草稿并离开", QMessageBox.ButtonRole.DestructiveRole)
        discard = dialog.addButton("放弃修改并离开", QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is discard:
            self._remove_draft(self.document)
            return True
        return clicked is keep

    def save(self):
        if self._saving or not self.document:
            return
        self._commit_editor()
        self._draft_timer.stop()
        self._write_draft()
        self._saving = True
        document = self.document
        self._update_status()
        self.table.setEnabled(False)
        self.timeline.setEnabled(False)
        self._run(document.save, lambda result, error: self._saved(document, result, error))

    def _saved(self, document, result, error):
        self._saving = False
        self.table.setEnabled(True)
        self.timeline.setEnabled(True)
        if error:
            self._save_then_render = False
            QMessageBox.warning(self, "保存失败", str(error))
        elif self.document is document:
            self._remove_draft(document)
            self._baseline = self.storage.capture_baseline(
                document.source_path, dependencies=[document.corrected_path]
            )
            self._show_transient_status(f"✓ 已保存 · {Path(result).name}")
        self._update_status()
        if not error and self._save_then_render and self.document is document:
            self._save_then_render = False
            QTimer.singleShot(0, self._start_render)
        else:
            self._save_then_render = False
        if not error and self.document is document:
            self.save_button.setText("✓ 已保存")
            QTimer.singleShot(1800, lambda: self.save_button.setText("保存字幕")
                              if self.save_button.text() == "✓ 已保存" else None)

    def _start_render(self, _checked=False):
        if not self.document or not self.project or self._saving:
            return
        self._commit_editor()
        project = self.project
        if project.id in self._render_jobs:
            self.app_status.setText(f"{project.title} · 已在压制中")
            return
        document = self.document
        if document.dirty or not document.corrected_path.is_file():
            choice = QMessageBox.question(
                self, "保存并压制字幕",
                "压制只使用正式保存的校对字幕。是否先保存当前字幕，再开始后台压制？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if choice == QMessageBox.StandardButton.Yes:
                self._save_then_render = True
                self.save()
            return
        key = project.id
        cancellation = threading.Event()
        self._render_jobs[key] = {"title": project.title, "cancel": cancellation,
                                  "percent": 0, "percent_enabled": True}
        self._render_result = None
        self.render_location.hide()
        self.app_status.setText(f"{project.title} · 正在压制…")
        self.app_status.setToolTip("")
        self._update_status()
        video_path = document.video.path
        corrected_path = document.corrected_path
        digest = document.corrected_fingerprint

        def action():
            def progress(message, step, total):
                percent = step if total == 100 and step < 100 else None
                self.render_progress.emit(key, percent, message)
            return self.render_service.render(
                video_path, corrected_path, digest,
                progress_callback=progress, cancel_event=cancellation,
            )

        self._run(action, lambda result, error: self._render_finished(key, result, error))

    def _show_render_progress(self, key, percent, message):
        job = self._render_jobs.get(key)
        if job is None:
            return
        if "NVENC" in message:
            job["percent_enabled"] = False
        elif percent is not None and job["percent_enabled"]:
            job["percent"] = max(job["percent"], percent)
        suffix = f" {job['percent']}%" if percent is not None and job["percent_enabled"] else ""
        self.app_status.setText(f"{job['title']} · 正在压制{suffix}")

    def _render_finished(self, key, result, error):
        job = self._render_jobs.pop(key, None)
        if job is None:
            return
        if error:
            if job["cancel"].is_set():
                self.app_status.setText(f"{job['title']} · 压制已停止")
            else:
                detail = str(error)
                if isinstance(error, FileNotFoundError):
                    hint = "请检查 FFmpeg 和 ffprobe 是否可用"
                elif "字体" in detail:
                    hint = "请安装默认字幕字体后重试"
                elif isinstance(error, PermissionError):
                    hint = "请检查投稿目录的写入权限"
                else:
                    hint = "请检查视频与校对字幕后重试"
                self.app_status.setText(f"{job['title']} · 压制失败：{hint}")
                self.app_status.setToolTip(f"{detail}\n详细记录：{self.storage.logs / 'subtitle-render.log'}")
        else:
            self._render_result = Path(result["output_video_path"])
            self._show_transient_status(f"{job['title']} · ✓ 压制完成 · {self._render_result.name}")
            self.app_status.setToolTip(str(self._render_result))
            self.render_location.setToolTip(str(self._render_result))
            name = self._render_result.name
            self.render_location.setText(f"定位：{name[:16]}{'…' if len(name) > 16 else ''}")
            self.render_location.show()
        if self._render_jobs:
            active = next(iter(self._render_jobs.values()))
            self.app_status.setText(f"{active['title']} · 正在压制…")
        self._update_status()
        if self._close_after_render and not self._render_jobs:
            QTimer.singleShot(0, self.close)

    def _locate_render_output(self):
        path = self._render_result
        if path is None or not path.is_file():
            QMessageBox.warning(self, "输出文件不存在", "成片可能已被移动，请检查投稿目录。")
            return
        if os.name == "nt":
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _delete(self):
        if self.document is None or self._saving:
            return
        self._commit_editor()
        selected = set(self.selection.selected)
        if not selected and self.selected_index is not None:
            selected = {self.selected_index}
        if not selected:
            return
        try:
            self.document.delete_many(selected)
        except ValueError as exc:
            QMessageBox.warning(self, "无法删除", str(exc))
            return
        self.selection.clear()
        self.selected_index = None
        self.model.set_document(self.document)
        self.timeline.select_cue(None, center=False)
        self.timeline.update()
        self._edited()

    def _undo(self):
        if self._saving:
            return
        self._commit_editor()
        before = document_hash(self.document.entries) if self.document else None
        if self.ai_session and self._ai_skip_undo and self._ai_skip_undo[-1][0] == before:
            _, suggestion_id = self._ai_skip_undo.pop()
            self._ai_skip_redo.append((before, suggestion_id))
            self.ai_session.mark(suggestion_id, "pending", self.document.entries)
            self._save_ai_session()
            self._render_ai()
            return
        if self.document and self.document.undo():
            after = document_hash(self.document.entries)
            if self.ai_session and self._ai_undo and self._ai_undo[-1][:2] == (after, before):
                transition = self._ai_undo.pop()
                self._ai_redo.append(transition)
                self.ai_session.mark(transition[2], "pending", self.document.entries)
            self.model.set_document(self.document)
            self.timeline.update()
            self._select_cue(self.selected_index)
            self._edited()
            self._render_ai()

    def _redo(self):
        if self._saving:
            return
        self._commit_editor()
        before = document_hash(self.document.entries) if self.document else None
        if self.ai_session and self._ai_skip_redo and self._ai_skip_redo[-1][0] == before:
            _, suggestion_id = self._ai_skip_redo.pop()
            self._ai_skip_undo.append((before, suggestion_id))
            self.ai_session.mark(suggestion_id, "skipped", self.document.entries)
            self._save_ai_session()
            self._render_ai()
            return
        if self.document and self.document.redo():
            after = document_hash(self.document.entries)
            if self.ai_session and self._ai_redo and self._ai_redo[-1][:2] == (before, after):
                transition = self._ai_redo.pop()
                self._ai_undo.append(transition)
                self.ai_session.mark(transition[2], "accepted", self.document.entries)
            self.model.set_document(self.document)
            self.timeline.update()
            self._select_cue(self.selected_index)
            self._edited()
            self._render_ai()

    def _select_page(self, index):
        super()._select_page(index)
        self._sync_shortcuts(QApplication.instance().focusWidget())
        if index == 1 and hasattr(self, "cover_editor"):
            # 只读取字幕页当前播放位置，切页不 seek、不暂停、不修改字幕状态。
            self.cover_editor.set_current_playhead(getattr(self, "_player_position", 0.0))
        if hasattr(self, "storage") and hasattr(self, "_draft_timer"):
            self._save_session()

    def _balance_vertical_splitter(self):
        super()._balance_vertical_splitter()
        saved = self._session.get("timeline_height")
        if not isinstance(saved, (int, float)) or not hasattr(self, "vertical_splitter"):
            return
        sizes = self.vertical_splitter.sizes()
        if len(sizes) != 3:
            return
        timeline = max(self.timeline.minimumHeight(), int(saved))
        available = sum(sizes)
        if timeline >= available - 370:
            return
        remaining = available - timeline
        video = max(200, min(remaining - 170, sizes[0]))
        self.vertical_splitter.setSizes([video, timeline, max(170, remaining - video)])

    def _save_session(self):
        try:
            self.storage.save_session({
                "page": self.pages.currentIndex(),
                "project_id": self.project.id if self.project else None,
                "video_path": self.document.video.path if self.document else self.video_choice.currentData().path
                if self.video_choice.currentData() else None,
                "cue_index": self.selected_index,
                "playback_seconds": getattr(self, "_player_position", 0.0),
                "timeline_height": (self.vertical_splitter.sizes()[1]
                                     if hasattr(self, "vertical_splitter") and self.vertical_splitter.sizes()
                                     else None),
                "geometry": base64.b64encode(bytes(self.saveGeometry())).decode("ascii"),
            })
        except OSError as exc:
            self.app_status.setText(f"会话保存失败：{exc}")

    def _restore_window(self):
        encoded = self._session.get("geometry")
        if not isinstance(encoded, str):
            return
        try:
            self.restoreGeometry(base64.b64decode(encoded, validate=True))
        except (ValueError, TypeError):
            return
        screens = self.screen().virtualSiblings() if self.screen() else []
        if not any(self.frameGeometry().intersects(screen.availableGeometry()) for screen in screens):
            self.move(80, 80)

    def closeEvent(self, event):
        if self._render_jobs:
            if self._close_after_render:
                event.ignore()
                return
            dialog = QMessageBox(self)
            dialog.setWindowTitle("字幕仍在压制")
            dialog.setText("后台字幕压制仍在运行。关闭窗口会中断当前任务。")
            background = dialog.addButton("继续后台（最小化）", QMessageBox.ButtonRole.AcceptRole)
            stop = dialog.addButton("停止压制并退出", QMessageBox.ButtonRole.DestructiveRole)
            dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            dialog.exec()
            if dialog.clickedButton() is background:
                self.showMinimized()
            elif dialog.clickedButton() is stop:
                self._close_after_render = True
                for job in self._render_jobs.values():
                    job["cancel"].set()
                self.app_status.setText("正在停止字幕压制…")
            event.ignore()
            return
        if self._saving or self._loading:
            event.ignore()
            return
        if not self._resolve_unsaved():
            event.ignore()
            return
        if hasattr(self, "cover_editor"):
            self.cover_editor.flush_draft()
        self._save_session()
        super().closeEvent(event)
        if event.isAccepted() and self.player:
            self.player.close()

    def _toggle_play(self):
        if not self.player or not self._media_ready:
            return
        if self._player_paused:
            self._player_paused = False
            # 开始播放不应抢走用户手动调整过的时间轴视野。
            # timeline.set_playhead() 会在允许跟随时自行处理播放跟随；
            # 若用户已通过滚动条/滚轮/侧键平移，_manual_pan 会保持当前视窗。
            self.player.play()
        else:
            self._player_paused = True
            self.player.pause()
        self._set_play_button(self._player_paused)

    def _set_play_button(self, paused):
        self.play_button.setText("播放" if paused else "暂停")
        self.play_button.setIcon(desktop_icon("play" if paused else "pause", COLORS.text))

    def _volume_changed(self, value):
        if getattr(self, "player", None):
            self.player.set_volume(value)

    def _speed_changed(self):
        if getattr(self, "player", None):
            self.player.set_speed(float(self.speed.currentData()))

    @staticmethod
    def _clock(seconds):
        value = max(0, int(seconds or 0))
        return f"{value // 3600:02d}:{value // 60 % 60:02d}:{value % 60:02d}"

    def _player_status(self, state):
        if not self.project or not self.project.videos:
            return
        duration = state.get("duration")
        position = state.get("position")
        if position is not None and self._seek_guard_target is not None:
            if abs(float(position) - self._seek_guard_target) <= .35:
                self._seek_guard_target = None
                self._seek_guard_until = 0.0
            elif time.monotonic() < self._seek_guard_until:
                # mpv seek 后可能先回报 seek 前的旧 position。这个旧位置不能再
                # 驱动 playhead / playing cue，否则用户会看到“跳到下一字幕”的假选中。
                position = None
            else:
                self._seek_guard_target = None
                self._seek_guard_until = 0.0
        requested_seek = False
        if duration is not None and duration > 0:
            self._player_duration = duration
            if state.get("idle") is False:
                self._media_ready = True
                self.play_button.setEnabled(True)
                if self.document and self.preview_toggle.isChecked() and not self._preview_attached:
                    self._refresh_subtitle_preview()
            self.timeline.set_duration(duration)
            if self._pending_seek is not None and self._media_ready:
                self._resume_pending = False
                self._seek_to(self._pending_seek)
                requested_seek = True
            elif self._resume_pending:
                self._resume_pending = False
                resume = self._session.get("playback_seconds", 0)
                if isinstance(resume, (int, float)) and 0 < resume < duration:
                    self._seek_to(float(resume))
                    requested_seek = True
        if position is not None and not requested_seek:
            self._player_position = position
            self.timeline.set_playhead(position, playing=state.get("paused") is False)
            if hasattr(self, "cover_editor"):
                self.cover_editor.set_current_playhead(position)
            if self._audition_until is not None and position >= self._audition_until:
                until = self._audition_until
                self._audition_until = None
                self._player_paused = True
                self.player.pause()
                self._seek_to(until)
            elif self._loop_enabled and self._loop_cue_id is not None:
                bounds = self._entry_bounds(self._loop_cue_id)
                if bounds is not None:
                    loop_start = max(0.0, bounds[0] - .2)
                    loop_end = min(self._player_duration, bounds[1] + .2)
                    if position < loop_end - .05:
                        self._loop_seek_pending = False
                    if position >= loop_end and not self._loop_seek_pending:
                        self._loop_seek_pending = True
                        self._player_position = loop_start
                        self.player.seek(loop_start, pause=False)
        if state.get("paused") is not None and not requested_seek:
            self._player_paused = state["paused"]
        if "hwdec" in state:
            self._hwdec = state["hwdec"]
        self._set_play_button(self._player_paused)
        self.time_label.setText(
            f"{self._clock(self._player_position)} / {self._clock(self._player_duration)}"
        )
        if self.document and position is not None and not requested_seek:
            active = next((entry.index for entry in self.document.entries
                           if srt_timestamp_seconds(entry.start) <= position
                           < srt_timestamp_seconds(entry.end)), None)
            self.model.set_playing_index(active)
            self.timeline.set_playing_cue(active)

    def _player_error(self, message):
        self.app_status.setText(f"播放器错误：{message}")
        if not self._media_ready:
            self.play_button.setEnabled(False)
            self.video_placeholder.setText(f"视频无法播放：{message}")
            self.video_stack.setCurrentIndex(0)

    def _check_player_load(self, token):
        if token == self._player_load_token and self._player_duration <= 0:
            self._player_error("视频加载超时或格式不受支持")
