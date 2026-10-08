"""Qt 字幕工作台：项目扫描、显式保存和可恢复草稿。"""

from __future__ import annotations

import base64

from PySide6.QtCore import (
    QModelIndex,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.ai_review import AIReviewService
from autoslice.desktop.commands import CommandDispatcher
from autoslice.desktop.correction_memory import CorrectionMemory
from autoslice.desktop.cover import CoverEditorWidget
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.jobs import BackgroundJob
from autoslice.desktop.projects import SubmissionProject, SubmissionProjectService
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.window import PreviewWindow, label, line
from autoslice.desktop.selection import CueSelection
from autoslice.desktop.subtitle_preview import SubtitlePreviewService
from autoslice.desktop.subtitle_render import SubtitleRenderService
from autoslice.desktop.subtitles import SubtitleDocument
from autoslice.desktop.waveform import WaveformCache

from .player import MpvAdapter
from .timeline import TimelineSidePanController
from .window_ai import AiReviewMixin
from .window_document import SubtitleDocumentMixin
from .window_input import InputRoutingMixin
from .window_player import PlayerMixin
from .window_projects import ProjectsMixin
from .window_subtitles import SubtitleListMixin
from .window_timeline import TimelineMixin
from .window_widgets import _StatusLabel


class DesktopWindow(
    ProjectsMixin,
    AiReviewMixin,
    SubtitleListMixin,
    InputRoutingMixin,
    TimelineMixin,
    PlayerMixin,
    SubtitleDocumentMixin,
    PreviewWindow,
):
    # 各职责的混入类必须排在 PreviewWindow 之前，才能覆盖它的同名构建方法。
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
        # 设置页构建时就要用到，必须在 super().__init__ 之前创建。
        self.correction_memory = CorrectionMemory(self.storage)
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

    def _run(self, action, callback):
        job = BackgroundJob(action)
        self._jobs.append(job)
        def done(result, error):
            self._jobs.remove(job)
            callback(result, error)
        job.signals.finished.connect(done)
        QThreadPool.globalInstance().start(job)

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

    def _select_page(self, index):
        super()._select_page(index)
        self._sync_shortcuts(QApplication.instance().focusWidget())
        if index == 2 and hasattr(self, "learning_panel"):
            self.learning_panel.refresh()
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


