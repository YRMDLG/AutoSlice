"""Qt 字幕工作台：项目扫描、显式保存和可恢复草稿。"""

from __future__ import annotations

import base64
from pathlib import Path

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
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.ai_review import AIReviewService
from autoslice.desktop.archive.knowledge import Knowledge, profile_id_for
from autoslice.desktop.commands import CommandDispatcher
from autoslice.desktop.correction_memory import CorrectionMemory
from autoslice.desktop.cover import CoverEditorWidget
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.jobs import BackgroundJob
from autoslice.desktop.projects import SubmissionProject, SubmissionProjectService
from autoslice.desktop.qt_preview import motion, popups
from autoslice.desktop.qt_preview.icons import icon as desktop_icon
from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.qt_preview.shell import ProjectSwitch, SaveState
from autoslice.desktop.qt_preview.window import PreviewWindow, key_hint, label, tool_icon
from autoslice.desktop.selection import CueSelection
from autoslice.desktop.subtitle_preview import SubtitlePreviewService
from autoslice.desktop.subtitle_render import SubtitleRenderService
from autoslice.desktop.subtitles import SubtitleDocument
from autoslice.desktop.topics import TopicAnalysisService
from autoslice.desktop.waveform import WaveformCache
from autoslice.runtime_config import OUTPUT_DIR

from .player import MpvAdapter
from .timeline import TimelineSidePanController
from .window_ai import AiReviewMixin
from .window_asr import RecognizeMixin
from .window_document import SubtitleDocumentMixin
from .window_find import FindReplaceMixin
from .window_input import InputRoutingMixin
from .window_player import PlayerMixin
from .window_projects import ProjectsMixin
from .window_quality import QualityReviewMixin
from .window_subtitles import SubtitleListMixin
from .window_tasks import TaskPanelMixin
from .window_timeline import TimelineMixin
from .window_topics import TopicAnalysisMixin
from .window_widgets import _StatusLabel

# 时间轴默认高度（像素）：记住过高度的按记住的来。
TIMELINE_DEFAULT_HEIGHT = 164


def _saved_submission_root(storage: DesktopStorage) -> Path | None:
    """会话里记着的投稿目录，仍然存在才用。"""

    value = (storage.read_session() or {}).get("submission_root")
    if isinstance(value, str) and value and Path(value).is_dir():
        return Path(value)
    return None


class DesktopWindow(
    ProjectsMixin,
    AiReviewMixin,
    RecognizeMixin,
    SubtitleListMixin,
    FindReplaceMixin,
    InputRoutingMixin,
    TimelineMixin,
    PlayerMixin,
    SubtitleDocumentMixin,
    QualityReviewMixin,
    TaskPanelMixin,
    TopicAnalysisMixin,
    PreviewWindow,
):
    # 各职责的混入类必须排在 PreviewWindow 之前，才能覆盖它的同名构建方法。
    ai_progress = Signal(int, str, int, int)
    # 一批检查完：(第几次检查, 当时的字幕快照, 项目标题, 到目前为止的结果)
    ai_partial = Signal(int, object, str, object)
    render_progress = Signal(str, object, str)

    def __init__(self, service: SubmissionProjectService | None = None,
                 storage: DesktopStorage | None = None):
        self.storage = storage or DesktopStorage()
        # 上次在设置里选过的投稿目录优先；目录没了就回到默认配置
        self.service = service or SubmissionProjectService(_saved_submission_root(self.storage))
        self.ai_service = AIReviewService(self.storage)
        self.render_service = SubtitleRenderService(self.storage)
        self.preview_service = SubtitlePreviewService(self.storage)
        self.waveform_cache = WaveformCache(self.storage.waveforms)
        # 设置页构建时就要用到，必须在 super().__init__ 之前创建。
        self.correction_memory = CorrectionMemory(self.storage)
        # 话题报告和网页端放同一个输出目录，不放进投稿目录（免得被当成新视频扫进来）
        self.topic_service = TopicAnalysisService(OUTPUT_DIR)
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
        self.service.set_sort_mode(self._session.get("project_sort", "folder_created_desc"))
        self._baseline = None
        # PreviewWindow.__init__ 会在构建 QTableView 时安装本类 eventFilter，
        # 所以这些状态必须在 super() 前就存在。
        self._inline_editor = None
        self._inline_editor_index = QModelIndex()
        self._inline_drag_anchor = None
        self._event_filter_busy = False
        self._table_pointer_selecting = False
        super().__init__()
        self.cover_editor.status_changed.connect(self._show_transient_status)
        self.cover_editor.material_directory_changed.connect(self.settings_material_directory.setText)
        self.cover_editor.next_video_requested.connect(self._next_cover_video)
        self._timeline_side_pan = TimelineSidePanController(self.timeline, self)
        self.ai_progress.connect(self._show_ai_progress)
        self.ai_partial.connect(self._ai_partial_ready)
        self.render_progress.connect(self._show_render_progress)
        # 待确认数：导航项右上角的小圆点数字，不压住图标
        self.nav_badge = QLabel(self.nav_buttons[0])
        self.nav_badge.setObjectName("navBadge")
        self.nav_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.nav_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.nav_badge.hide()
        # 导航底部的任务环就是“任务”入口：压制、识别、AI 检查、话题分析都在里面
        self.tasks_button = self.task_ring
        self.tasks_button.clicked.connect(self._show_tasks)
        # 封面的项目级动作（投稿文案、批量出图、保存并做下一个、导出）挪进顶栏
        self.cover_actions_row.addWidget(self.cover_editor.header_bar)
        self.vertical_splitter.splitterMoved.connect(self._timeline_splitter_moved)
        self.setWindowTitle("AutoSlice · 字幕校对")
        try:
            self.player = MpvAdapter(int(self.video_widget.winId()), parent=self)
            self.player.status_changed.connect(self._player_status)
            self.player.error.connect(self._player_error)
        except (OSError, FileNotFoundError) as exc:
            self.player = None
            self.video_placeholder.setText(str(exc))
            self.video_placeholder.setWordWrap(True)
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
                                 ("Ctrl+S", self.save, None),
                                 ("Delete", self._delete, "delete_selected_cues"),
                                 ("Ctrl+Space", self._toggle_play, "play_pause"),
                                 ("Space", self._space_play, None),
                                 ("Ctrl+F", self._open_find, None),
                                 ("F8", self._toggle_play, "play_pause"),
                                 ("F1", self._audition_current, None),
                                 ("F2", self._audition_before, None),
                                 ("F3", self._audition_after, None),
                                 ("F4", self._audition_once, None),
                                 ("Q", self._trim_start, "trim_start_to_playhead"),
                                 ("W", self._trim_end, "trim_end_to_playhead"),
                                 ("Ctrl+B", self._split_at_playhead, "split_at_playhead"),
                                 ("Alt+A", self._accept_ai, None),
                                 ("Alt+S", self._skip_ai, None),
                                 ("Ctrl+/", self._show_shortcuts, None),
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
        motion.install_tree(self)
        QTimer.singleShot(0, self.refresh)

    def _cover_page(self):
        """独立 AutoCover 工作区，项目与字幕页共用；顶栏动作随页面切换。"""

        page = QWidget()
        page.setObjectName("workArea")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.cover_editor = CoverEditorWidget(self.storage)
        layout.addWidget(self.cover_editor, 1)
        return page

    def _appbar(self):
        """顶栏：上一个 / 项目切换 / 下一个、视频选择，右侧是状态和当前页的主动作。"""

        bar = QWidget()
        bar.setObjectName("appbar")
        bar.setFixedHeight(SIZES.appbar_height)
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 0, 0, 0)
        row.setSpacing(10)
        self.project_prev = QPushButton()
        self.project_next = QPushButton()
        for button, name, tip, step in ((self.project_prev, "chevron_left", "上一个项目", -1),
                                        (self.project_next, "chevron_right", "下一个项目", 1)):
            button.setObjectName("square")
            button.setFixedSize(32, 32)
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            tool_icon(button, name)
            button.clicked.connect(lambda _checked=False, delta=step: self._step_project(delta))
        self.top_project = label("请选择项目", "projectTitle")
        self.top_project.setMinimumWidth(100)
        self.top_project.setMaximumWidth(480)
        self.video_name = label("选择视频", "videoFile")
        self.video_name.setMaximumWidth(480)
        self.project_switch = ProjectSwitch(self.top_project, self.video_name)
        self.project_switch.setToolTip("切换投稿项目")
        self.project_switch.clicked.connect(self._toggle_project_popover)
        row.addWidget(self.project_prev)
        row.addWidget(self.project_switch)
        row.addWidget(self.project_next)
        self.video_choice = popups.ComboBox()
        self.video_choice.setMinimumWidth(160)
        self.video_choice.setMaximumWidth(260)
        self.video_choice.setToolTip("这个项目里有几个视频，选择要校对的一个")
        self.video_choice.currentIndexChanged.connect(self._video_changed)
        row.addWidget(self.video_choice)
        self.settings_title = label("设置", "pageTitle")
        self.settings_title.hide()
        row.addWidget(self.settings_title)
        # 空白都给这里：项目切换按内容宽度，不被拉长
        row.addStretch(1)
        self.app_status = _StatusLabel("")
        self.app_status.setObjectName("statusLabel")
        self.app_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.app_status.setMaximumWidth(360)
        row.addWidget(self.app_status)
        self.save_state = SaveState()
        self.save_state.hide()
        row.addWidget(self.save_state)
        self.render_location = QPushButton("定位输出")
        self.render_location.setObjectName("quiet")
        self.render_location.setIcon(desktop_icon("folder", COLORS.muted))
        self.render_location.setIconSize(QSize(SIZES.icon_size, SIZES.icon_size))
        self.render_location.clicked.connect(self._locate_render_output)
        motion.reveal(self.render_location, False)
        row.addWidget(self.render_location)
        self.ai_return = QPushButton("查看待确认")
        self.ai_return.clicked.connect(self._return_to_ai)
        motion.reveal(self.ai_return, False)
        row.addWidget(self.ai_return)
        # 页面动作：字幕页是话题分析、压制、保存；封面页的动作由封面编辑器提供（见 _attach_cover_actions）
        self.subtitle_actions = QWidget()
        self.subtitle_actions.setObjectName("workflowActions")
        action_row = QHBoxLayout(self.subtitle_actions)
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)
        self.save_button = QPushButton("保存字幕")
        self.save_button.setObjectName("primary")
        self.save_button.setToolTip("写入正式的校对字幕（Ctrl+S）")
        self.save_button.clicked.connect(self.save)
        self.save_button.setEnabled(False)
        key_hint(self.save_button, "Ctrl S")
        self.render_button = QPushButton("压制字幕")
        self.render_button.setObjectName("secondary")
        self.render_button.setToolTip("使用默认字幕样式，将已保存的校对字幕压制到新视频")
        self.render_button.clicked.connect(self._start_render)
        self.render_button.setEnabled(False)
        action_row.addWidget(self._build_topic_button())
        action_row.addWidget(self._build_more_button())
        action_row.addWidget(self.render_button)
        action_row.addWidget(self.save_button)
        row.addWidget(self.subtitle_actions)
        self.cover_actions = QWidget()
        self.cover_actions.setObjectName("workflowActions")
        self.cover_actions_row = QHBoxLayout(self.cover_actions)
        self.cover_actions_row.setContentsMargins(0, 0, 0, 0)
        self.cover_actions_row.setSpacing(8)
        self.cover_actions.hide()
        row.addWidget(self.cover_actions)
        return bar

    def _step_project(self, delta: int) -> None:
        """顶栏左右箭头：按项目列表顺序切到上一个 / 下一个项目。"""

        ids = list(getattr(self, "project_buttons", {}))
        if not ids:
            return
        current = self.project.id if self.project else None
        index = ids.index(current) if current in ids else (-1 if delta > 0 else 0)
        target = ids[(index + delta) % len(ids)]
        if target != current:
            self.project_buttons[target].click()

    def _page_chrome(self, index: int) -> None:
        super()._page_chrome(index)
        for widget in (self.project_prev, self.project_next):
            widget.setVisible(index == 0)
        self.video_choice.setVisible(index != 2 and self.video_choice.count() > 1)
        # 设置页的标题在页面左侧目录上方，顶栏不再重复
        self.settings_title.setVisible(False)
        self.subtitle_actions.setVisible(index == 0)
        self.cover_actions.setVisible(index == 1 and self.cover_actions_row.count() > 0)
        if hasattr(self, "save_state"):
            self.save_state.setVisible(index == 0 and getattr(self, "document", None) is not None)
            self._refresh_save_state()

    def _run(self, action, callback):
        job = BackgroundJob(action)
        self._jobs.append(job)
        def done(result, error):
            self._jobs.remove(job)
            callback(result, error)
        job.signals.finished.connect(done)
        QThreadPool.globalInstance().start(job)

    def _update_status(self):
        self._refresh_topic_button()
        if hasattr(self, "ai_accept"):
            self._show_ai_detail()
            self.ai_skip.setEnabled(not self._saving)
            self.ai_run.setEnabled(self.document is not None and not self._saving)
            self.ai_local.setEnabled(self.document is not None and not self._saving and not self._ai_running)
            for button in (self.ai_selected_check, self.ai_changed_check):
                button.setEnabled(self.document is not None and not self._saving and not self._ai_running)
        self._refresh_quality_panel()
        # 列表底部：有字幕时是操作提示，没有字幕时换成说明
        self.subtitle_status.setVisible(self.document is None)
        self.subtitle_hint.setVisible(self.document is not None)
        self.filter_all.setText(f"全部 {len(self.document.entries)}" if self.document else "全部")
        if self.document is None:
            self.subtitle_status.setText("请选择有字幕的视频")
            self.save_button.setEnabled(False)
            self.render_button.setEnabled(False)
            self.render_button.setText("压制字幕")
            self.timeline_overlap.setText("")
            self.quality_button.setEnabled(False)
            self.quality_button.setText("检查提醒 0")
            self._paint_quality_dot()
            self._refresh_save_state()
            return
        suffix = " · 未保存" if self.document.dirty else " · 已保存"
        quality = self.preview_service.quality_flags(self.document.entries, self._habit_limits())
        quality_text = f" · ⚠ {len(quality)} 条待检查" if quality else ""
        self.quality_button.setText(f"检查提醒 {len(quality)}")
        self.quality_button.setEnabled(bool(quality))
        self._paint_quality_dot()
        self._refresh_save_state()
        self.subtitle_status.setText(f"全部 {len(self.document.entries)} 条{suffix}{quality_text}")
        self.subtitle_status.setToolTip("\n".join(
            f"第 {index} 条：{'、'.join(issues)}" for index, issues in quality.items()
        ))
        self.save_button.setEnabled(self.document.dirty and not self._saving)
        running = bool(self.project and self.project.id in self._render_jobs)
        self.render_button.setEnabled(not self._saving and bool(self.project) and not running)
        self.render_button.setText("压制中…" if running else "压制字幕")
        overlaps = self.timeline.overlap_count()
        self.timeline_overlap.setText(f"{overlaps} 处重叠" if overlaps else "")
        self.timeline_overlap.setToolTip("时间轴上标红的地方：前后两条字幕的时间叠在一起" if overlaps else "")

    def _refresh_save_state(self) -> None:
        """顶栏保存状态 + 项目说明行（视频 · N 条 · 第几个项目 / 共几个）。"""

        document = getattr(self, "document", None)
        self.save_state.setVisible(document is not None and self.pages.currentIndex() == 0)
        if document is not None:
            self.save_state.set_state(not document.dirty, document.change_count)
        project = getattr(self, "project", None)
        if project is None or not project.videos:
            return
        if self.pages.currentIndex() == 1:
            self.video_name.setText(f"与字幕页共享项目 · 播放头 {self._clock(getattr(self, '_player_position', 0.0))}")
            return
        video = self.video_choice.currentData() or project.videos[0]
        parts = [video.name]
        if document is not None:
            parts.append(f"{len(document.entries)} 条")
        ids = list(getattr(self, "project_buttons", {}))
        if project.id in ids:
            parts.append(f"{ids.index(project.id) + 1} / {len(ids)}")
        self.video_name.setText(" · ".join(parts))

    def _habit_limits(self) -> dict:
        """从旧切片学到的个人字幕阈值；没学过是空字典。按视频缓存，学到新东西后 _forget_habit_cache 清掉。"""

        video = self.document.video.path if self.document else ""
        cached = getattr(self, "_habit_cache", None)
        if cached is None or cached[0] != video:
            profile_id = profile_id_for(video, self.project.title if self.project else "") if video else ""
            cached = (video, Knowledge(self.storage).subtitle_limits(profile_id) if profile_id else {})
            self._habit_cache = cached
        return cached[1]

    def forget_habit_cache(self) -> None:
        self._habit_cache = None
        if getattr(self, "document", None) is not None:
            self._update_status()

    def _toast(self, message: str, *, undo: bool = False, snapshot=None) -> None:
        """顶部提示条；undo 为真时带“撤销”（snapshot 给定时一次撤回到那一刻）。"""

        if not hasattr(self, "toast"):
            return
        callback = None
        if undo:
            callback = (lambda: self._undo_to(snapshot)) if snapshot is not None else self._undo
        self.toast.show_message(message, undo=callback)

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
        if index == 2 and hasattr(self, "archive_panel"):
            self.archive_panel.refresh()
        if index == 2:
            self._check_render_font()
        if hasattr(self, "project_popover") and self.project_popover.isVisible():
            self.project_popover.hide()
        if index == 1 and hasattr(self, "cover_editor"):
            # 只读取字幕页当前播放位置，切页不 seek、不暂停、不修改字幕状态。
            self.cover_editor.set_current_playhead(getattr(self, "_player_position", 0.0))
        if hasattr(self, "storage") and hasattr(self, "_draft_timer"):
            self._save_session()

    def _balance_vertical_splitter(self):
        super()._balance_vertical_splitter()
        if not hasattr(self, "vertical_splitter"):
            return
        # 只认用户亲手拖过的高度；旧版布局存的 timeline_height 不再沿用（那时卡片结构不同）
        saved = self._session.get("timeline_card_height")
        if not isinstance(saved, (int, float)) or saved <= 0:
            # 没拖过时间轴：默认就给足高度，波形和字幕块不用先拖大才看得清；窄屏紧凑布局不动。
            if getattr(self, "_compact_layout", False):
                return
            saved = TIMELINE_DEFAULT_HEIGHT
        sizes = self.vertical_splitter.sizes()
        if len(sizes) != 2:
            return
        timeline = max(self.timeline_card.minimumSizeHint().height(), int(saved))
        available = sum(sizes)
        if timeline >= available - 200:
            return
        self.vertical_splitter.setSizes([available - timeline, timeline])
        self._mirror_rows(self.vertical_splitter, self.side_splitter)

    def _save_session(self):
        try:
            self.storage.save_session({
                "page": self.pages.currentIndex(),
                "submission_root": str(self.service.root),
                "project_sort": self.service.sort_mode,
                "project_id": self.project.id if self.project else None,
                "video_path": self.document.video.path if self.document else self.video_choice.currentData().path
                if self.video_choice.currentData() else None,
                "cue_index": self.selected_index,
                "playback_seconds": getattr(self, "_player_position", 0.0),
                # 存会话时不读当前分界（切页、窗口未布局时可能是 0），只转存用户拖过的值
                "timeline_card_height": self._session.get("timeline_card_height"),
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
        if hasattr(self, "archive_panel"):
            self.archive_panel.stop()
        if hasattr(self, "asr_settings_panel"):
            self.asr_settings_panel.stop()
        self._asr_shutdown()
        self._topic_shutdown()
        self._save_session()
        super().closeEvent(event)
        if event.isAccepted() and self.player:
            self.player.close()
