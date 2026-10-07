"""AutoCover 桌面编辑器。

桌面层只维护交互状态和后台任务；媒体取帧、草稿持久化以及 Pillow 渲染
继续由 ``cover_service`` 负责。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject
from autoslice_cover.fonts import resolve_font_selection

from .cover_ai import CoverAICandidate
from .cover_canvas import CoverCanvas
from .cover_history import CoverHistory
from .cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    Rect,
    ShapeObject,
    StickerObject,
    TextObject,
    TextWrap,
    Transform,
    object_for_profile,
)
from .cover_service import CoverDraft, CoverService, wrap_cover_title
from .qt_preview.icons import icon


class _JobSignals(QObject):
    finished = Signal(object, object)


class _Job(QRunnable):
    def __init__(self, action):
        super().__init__()
        self.action = action
        self.signals = _JobSignals()

    def run(self):
        try:
            self.signals.finished.emit(self.action(), None)
        except Exception as exc:  # noqa: BLE001 - 回到界面显示可执行错误
            self.signals.finished.emit(None, exc)


class _TitleEdit(QPlainTextEdit):
    """允许手动换行，并保留旧测试/调用方使用的 ``text()``。"""

    def text(self) -> str:
        return self.toPlainText()


class CoverEditorWidget(QWidget):
    """真正可鼠标操作的 AutoCover 编辑器。"""

    status_changed = Signal(str)

    def __init__(self, storage: DesktopStorage, parent=None):
        super().__init__(parent)
        self.service = CoverService(storage)
        self.project: SubmissionProject | None = None
        self.video: ProjectVideo | None = None
        self.draft = CoverDraft("")
        self.document: CoverDocument | None = None
        self._preview_path: Path | None = None
        self._context_generation = 0
        self._busy = False
        self._preview_dirty = False
        self._pending_export = False
        self._current_playhead = 0.0
        self._frame_extract_pending = False
        self._frame_request_generation = 0
        self._nearby_request_generation = 0
        self._copy_variants = ()
        self._copy_variant_index = -1
        self._selected_text_id: str | None = None
        self._check_busy = False
        self._check_dirty = False
        self._canvas_key = "4x3"
        self._preview_request_generation = 0
        self._check_request_generation = 0
        self._jobs: set[_Job] = set()
        self.history = CoverHistory()
        self._frame_locked = False
        self._selected_frame_timestamp: float | None = None
        self._check_preview_visible = False
        self._wider_frames: tuple[tuple[Path, float], ...] = ()
        self._ai_candidates: tuple[CoverAICandidate, ...] = ()
        self._selected_ai_candidate: str | None = None
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(500)
        self._draft_timer.timeout.connect(self._save_draft)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self._render_preview)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 单行工具栏：比例 + 撤销/重做 + 导出 ──
        toolbar = QWidget()
        toolbar.setObjectName("coverToolbar")
        toolbar.setFixedHeight(44)
        toolbar_row = QHBoxLayout(toolbar)
        toolbar_row.setContentsMargins(12, 0, 12, 0)
        toolbar_row.setSpacing(6)

        self.canvas_ratio_group = QButtonGroup(self)
        self.canvas_ratio_group.setExclusive(True)
        self.canvas_ratio_buttons: dict[str, QPushButton] = {}
        for key, text in (("4x3", "4:3"), ("16x9", "16:9")):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setMinimumWidth(48)
            btn.setFixedHeight(28)
            btn.setToolTip(f"切换 {text} 主画布")
            self.canvas_ratio_group.addButton(btn)
            self.canvas_ratio_buttons[key] = btn
            toolbar_row.addWidget(btn)
            btn.clicked.connect(lambda _c=False, k=key: self._set_canvas_key(k))
        self.canvas_ratio_buttons["4x3"].setChecked(True)

        self.check_preview_toggle = QPushButton("另一比例")
        self.check_preview_toggle.setCheckable(True)
        self.check_preview_toggle.setObjectName("quiet")
        self.check_preview_toggle.setFixedHeight(28)
        self.check_preview_toggle.setEnabled(False)
        self.check_preview_toggle.setToolTip("按需显示另一输出比例的预览；隐藏时不排队渲染")
        self.check_preview_toggle.toggled.connect(self._toggle_check_preview)
        toolbar_row.addWidget(self.check_preview_toggle)

        toolbar_row.addSpacing(8)

        self.undo_button = QPushButton("")
        self.undo_button.setIcon(icon("undo"))
        self.undo_button.setIconSize(QSize(16, 16))
        self.undo_button.setFixedSize(28, 28)
        self.undo_button.setObjectName("quiet")
        self.undo_button.setToolTip("撤销")
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self._undo)
        self.redo_button = QPushButton("")
        self.redo_button.setIcon(icon("redo"))
        self.redo_button.setIconSize(QSize(16, 16))
        self.redo_button.setFixedSize(28, 28)
        self.redo_button.setObjectName("quiet")
        self.redo_button.setToolTip("重做")
        self.redo_button.setEnabled(False)
        self.redo_button.clicked.connect(self._redo)
        toolbar_row.addWidget(self.undo_button)
        toolbar_row.addWidget(self.redo_button)

        toolbar_row.addStretch(1)

        self.draft_status = QLabel("")
        self.draft_status.setObjectName("subtle")
        toolbar_row.addWidget(self.draft_status)

        self.export_button = QPushButton("导出")
        self.export_button.setObjectName("primary")
        self.export_button.setFixedHeight(28)
        self.export_button.setToolTip("导出当前画布比例")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export)
        toolbar_row.addWidget(self.export_button)
        self.export_both_button = QPushButton("导出 4:3 + 16:9")
        self.export_both_button.setFixedHeight(28)
        self.export_both_button.setToolTip("分别导出 4:3（1440×1080）与 16:9（1920×1080），不覆盖已有文件")
        self.export_both_button.setEnabled(False)
        self.export_both_button.clicked.connect(self._export_both)
        toolbar_row.addWidget(self.export_both_button)

        # 面板折叠按钮
        self.panel_toggle = QPushButton("")
        self.panel_toggle.setIcon(icon("chevron_right"))
        self.panel_toggle.setIconSize(QSize(16, 16))
        self.panel_toggle.setFixedSize(28, 28)
        self.panel_toggle.setObjectName("quiet")
        self.panel_toggle.setToolTip("收起/展开属性面板")
        self.panel_toggle.setCheckable(True)
        toolbar_row.addWidget(self.panel_toggle)
        root.addWidget(toolbar)

        # ── 隐藏控件：被代码引用但不直接放在布局里 ──
        self.project_label = QLabel("")
        self.video_label = QLabel("")
        self.export_summary = QLabel("")
        self.export_summary.setObjectName("subtle")
        self.export_summary.setWordWrap(False)
        self.export_summary.setMaximumWidth(420)
        toolbar_row.insertWidget(toolbar_row.indexOf(self.export_button), self.export_summary)
        self.notice_label = QLabel("")
        self.notice_label.setObjectName("coverNoticeInfo")
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)
        root.insertWidget(1, self.notice_label)
        self.canvas_hint = QLabel("")
        self.canvas_hint.setObjectName("subtle")
        self.canvas_ratio_status = QLabel("")
        self.canvas_ratio_status.setObjectName("subtle")
        self.copy_role_hint = QLabel("")
        self.copy_role_hint.setObjectName("subtle")
        self.copy_role_hint.setWordWrap(True)
        self.copy_policy_hint = QLabel("")
        self.copy_policy_hint.setObjectName("subtle")
        self.copy_policy_hint.setWordWrap(True)
        self.more_button = QPushButton("")
        self.advanced_widget = QWidget()
        self.advanced_widget.setVisible(False)
        self.media_controls = QWidget()
        self.media_controls.setVisible(False)
        self.import_button = QPushButton("导入底图")
        self.import_button.clicked.connect(self._import_image)
        self.import_button.setVisible(False)

        # ── 主内容区：画布 + 右侧上下文面板 ──
        content = QHBoxLayout()
        content.setSpacing(0)
        content.setContentsMargins(0, 0, 0, 0)

        # 中央画布
        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(12, 8, 12, 8)
        center_layout.setSpacing(6)
        self.canvas = CoverCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.canvas.title_position_changed.connect(self._title_position_changed)
        self.canvas.title_position_finished.connect(self._gesture_finished)
        self.canvas.background_position_changed.connect(self._background_position_changed)
        self.canvas.background_position_finished.connect(self._gesture_finished)
        self.canvas.zoom_changed.connect(self._zoom_changed)
        self.canvas.selected_changed.connect(self._canvas_selection_changed)
        self.canvas.selected_object_changed.connect(self._canvas_object_selected)
        self.canvas.safe_area_warning_changed.connect(self._canvas_safety_changed)
        self.canvas.object_changed.connect(self._canvas_object_changed)
        self.canvas.object_preview_changed.connect(self._canvas_object_preview_changed)
        center_layout.addWidget(self.canvas, 1)

        # 另一比例预览条（精简为一行）
        check_row = QHBoxLayout()
        check_row.setSpacing(8)
        check_row.setContentsMargins(0, 0, 0, 0)
        self.check_preview = QLabel("")
        self.check_preview.setObjectName("subtle")
        self.check_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.check_preview.setFixedHeight(72)
        self.check_preview.setMinimumWidth(100)
        self.check_preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.check_preview.setVisible(False)
        check_row.addWidget(self.check_preview, 1)
        center_layout.addLayout(check_row)
        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.setSpacing(8)
        self.canvas_hint.setWordWrap(False)
        self.canvas_hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        hint_row.addWidget(self.canvas_hint, 1)
        center_layout.addLayout(hint_row)
        content.addWidget(center, 1)

        # ── 右侧上下文面板（QStackedWidget 按选中对象切换）──
        self.right_panel = QWidget()
        self.right_panel.setObjectName("coverPanel")
        self.right_panel.setMinimumWidth(0)
        self.right_panel.setMaximumWidth(280)
        right_layout = QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(10, 10, 10, 10)
        right_layout.setSpacing(6)

        # 面板标题（随上下文变化）
        self.panel_title = QLabel("封面文案")
        self.panel_title.setObjectName("sectionTitle")
        right_layout.addWidget(self.panel_title)

        # 上下文面板堆栈
        self.panel_stack = QStackedWidget()
        right_layout.addWidget(self.panel_stack, 1)

        # ── 文字面板（默认）──
        self.copy_controls = self._build_text_panel()
        self.panel_stack.addWidget(self.copy_controls)

        # ── 素材对象面板 ──
        self.asset_controls = self._build_asset_panel()
        self.panel_stack.addWidget(self.asset_controls)

        # ── 背景面板 ──
        self.bg_controls = self._build_bg_panel()
        self.panel_stack.addWidget(self.bg_controls)

        # ── 空状态面板（无选中/无项目）──
        self.empty_controls = self._build_empty_panel()
        self.panel_stack.addWidget(self.empty_controls)

        # AI 入口固定在面板底部，所有上下文都能触达
        ai_row = QHBoxLayout()
        ai_row.setSpacing(4)
        self.ai_button = QPushButton("AI 三个方案")
        self.ai_button.setIcon(icon("sparkles"))
        self.ai_button.setIconSize(QSize(16, 16))
        self.ai_button.setFixedHeight(28)
        self.ai_button.setToolTip("生成可编辑候选（默认不调用真实 AI）")
        self.ai_button.clicked.connect(self._request_ai_candidates)
        ai_row.addWidget(self.ai_button, 1)
        right_layout.addLayout(ai_row)
        self.ai_candidate_label = QLabel("")
        self.ai_candidate_label.setObjectName("subtle")
        self.ai_candidate_label.setWordWrap(True)
        self.ai_candidate_label.setMaximumHeight(36)
        right_layout.addWidget(self.ai_candidate_label)

        content.addWidget(self.right_panel)

        # 面板折叠逻辑
        self._panel_visible = True
        self._last_panel_width = 280
        self.panel_toggle.toggled.connect(self._toggle_panel)
        root.addLayout(content, 1)

        # ── 底部附近帧条（精简为一行）──
        strip = QWidget()
        strip.setObjectName("frameStrip")
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(12, 4, 12, 6)
        strip_layout.setSpacing(4)
        self.frame_center_label = QLabel("0.00s")
        self.frame_center_label.setObjectName("subtle")
        self.frame_center_label.setMinimumWidth(62)
        strip_layout.addWidget(self.frame_center_label)
        self.current_frame_button = QPushButton("当前帧")
        self.current_frame_button.setFixedHeight(24)
        self.current_frame_button.setObjectName("quiet")
        self.current_frame_button.setToolTip("读取字幕页播放位置")
        self.current_frame_button.clicked.connect(self._use_current_frame)
        strip_layout.addWidget(self.current_frame_button)
        self.frame_lock_button = QPushButton("锁帧")
        self.frame_lock_button.setFixedHeight(24)
        self.frame_lock_button.setObjectName("quiet")
        self.frame_lock_button.setCheckable(True)
        self.frame_lock_button.setToolTip("锁定后候选和AI不换帧")
        self.frame_lock_button.clicked.connect(self._toggle_frame_lock)
        strip_layout.addWidget(self.frame_lock_button)
        self.more_frames_button = QPushButton("更多")
        self.more_frames_button.setFixedHeight(24)
        self.more_frames_button.setObjectName("quiet")
        self.more_frames_button.setToolTip("扩大取帧范围")
        self.more_frames_button.clicked.connect(self._find_more_frames)
        strip_layout.addWidget(self.more_frames_button)
        strip_layout.addSpacing(8)
        self.nearby_frame_buttons = []
        for _ in range(7):
            button = QPushButton("--")
            button.setObjectName("frameThumb")
            button.setCheckable(True)
            button.setMinimumWidth(86)
            button.setMaximumHeight(58)
            button.setIconSize(QSize(80, 45))
            button.setProperty("timestamp", 0.0)
            button.clicked.connect(
                lambda _checked=False, item=button: self._choose_nearby_frame(
                    float(item.property("timestamp") or 0.0)
                )
            )
            self.nearby_frame_buttons.append(button)
            strip_layout.addWidget(button, 1)
        root.addWidget(strip)

        # 初始状态：无项目时隐藏右侧面板，显示空状态
        self._hide_panel()
        self.panel_toggle.setEnabled(False)
        self._show_empty_panel()
        self._update_canvas_hint()

    def _toggle_panel(self, checked):
        """手动切换面板折叠/展开。"""
        if checked:
            self._hide_panel()
        else:
            self._show_panel()

    def _hide_panel(self):
        """收起右侧面板。"""
        if self.right_panel.width() > 0:
            self._last_panel_width = self.right_panel.width()
        self.right_panel.setMaximumWidth(0)
        self.right_panel.setMinimumWidth(0)
        self.panel_toggle.setIcon(icon("chevron_left"))
        self._panel_visible = False

    def _show_panel(self):
        """展开右侧面板。"""
        self.right_panel.setMinimumWidth(0)
        self.right_panel.setMaximumWidth(self._last_panel_width)
        self.panel_toggle.setIcon(icon("chevron_right"))
        self._panel_visible = True

    def _show_empty_panel(self):
        """显示空状态面板（无项目或无选中对象）。"""
        self.panel_title.setText("封面工具")
        self.panel_stack.setCurrentWidget(self.empty_controls)

    def _toggle_check_preview(self, checked: bool):
        """按需显示另一比例预览；隐藏时不排队渲染。"""
        self._check_preview_visible = bool(checked)
        self.check_preview.setVisible(self._check_preview_visible)
        if self._check_preview_visible:
            self._queue_check_preview()
        else:
            self._check_request_generation += 1
            self._check_busy = False
            self._check_dirty = False

    def _set_notice(self, text: str = "", level: str = "info"):
        """显示稳定的本地状态出口，避免错误只存在于隐藏控件。"""
        text = str(text or "").strip()
        if not text:
            self.notice_label.clear()
            self.notice_label.setVisible(False)
            return
        object_name = {
            "error": "coverNoticeError",
            "warning": "coverNoticeWarning",
        }.get(level, "coverNoticeInfo")
        self.notice_label.setObjectName(object_name)
        self.notice_label.setText(text)
        self.notice_label.setVisible(True)
        self.notice_label.style().unpolish(self.notice_label)
        self.notice_label.style().polish(self.notice_label)

    def _update_nearby_frame_selection(self, timestamp: float | None = None):
        if timestamp is not None:
            self._selected_frame_timestamp = max(0.0, float(timestamp))
        selected = self._selected_frame_timestamp
        distances = [
            abs(float(button.property("timestamp") or 0.0) - selected)
            if selected is not None else float("inf")
            for button in self.nearby_frame_buttons
        ]
        nearest = min(distances, default=float("inf"))
        nearest_index = distances.index(nearest) if nearest <= 0.06 else -1
        for index, button in enumerate(self.nearby_frame_buttons):
            button.setChecked(index == nearest_index)

    def _build_text_panel(self) -> QWidget:
        """文字对象属性面板：文案、字号、对齐、样式。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.title_edit = _TitleEdit()
        self.title_edit.setPlaceholderText("封面文案…")
        self.title_edit.setMaximumHeight(72)
        self.title_edit.textChanged.connect(self._draft_changed)
        layout.addWidget(self.title_edit)

        # A/B 快速切换（紧凑）
        role_row = QHBoxLayout()
        role_row.setSpacing(4)
        self.copy_role_buttons: dict[str, QPushButton] = {}
        self.copy_role_group = QButtonGroup(self)
        self.copy_role_group.setExclusive(True)
        for role, text in (("A", "A"), ("B", "B")):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.setMinimumWidth(32)
            btn.setToolTip(f"编辑文字块 {role}")
            btn.clicked.connect(lambda _c=False, r=role: self._select_copy_role(r))
            self.copy_role_group.addButton(btn)
            self.copy_role_buttons[role] = btn
            role_row.addWidget(btn)
        role_row.addStretch(1)
        self.copy_button = QPushButton("换一版")
        self.copy_button.setFixedHeight(26)
        self.copy_button.setObjectName("quiet")
        self.copy_button.setToolTip("切换本地文案候选")
        self.copy_button.clicked.connect(self._cycle_copy)
        role_row.addWidget(self.copy_button)
        layout.addLayout(role_row)

        # 字号 + 对齐
        size_align = QHBoxLayout()
        size_align.setSpacing(6)
        size_align.addWidget(QLabel("字号"))
        self.font_spin = QSpinBox()
        self.font_spin.setRange(24, 320)
        self.font_spin.setSuffix(" px")
        self.font_spin.setFixedHeight(26)
        self.font_spin.valueChanged.connect(self._draft_changed)
        size_align.addWidget(self.font_spin, 1)
        self.align_buttons: dict[str, QPushButton] = {}
        self.align_group = QButtonGroup(self)
        self.align_group.setExclusive(True)
        for key, text in (("left", "左"), ("center", "中"), ("right", "右")):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.setMinimumWidth(28)
            self.align_group.addButton(btn)
            self.align_buttons[key] = btn
            size_align.addWidget(btn)
            btn.clicked.connect(lambda _c=False, k=key: self._set_text_align(k))
        self.align_buttons["left"].setChecked(True)
        layout.addLayout(size_align)

        # 样式折叠区
        self.style_toggle = QPushButton("样式")
        self.style_toggle.setIcon(icon("chevron_right"))
        self.style_toggle.setIconSize(QSize(14, 14))
        self.style_toggle.setFixedHeight(26)
        self.style_toggle.setObjectName("quiet")
        self.style_toggle.setCheckable(True)
        layout.addWidget(self.style_toggle)
        self.style_widget = QWidget()
        style_form = QFormLayout(self.style_widget)
        style_form.setContentsMargins(8, 0, 0, 0)
        style_form.setSpacing(4)
        self.fill_edit = QLineEdit()
        self.fill_edit.setPlaceholderText("#FFE438")
        self.fill_edit.editingFinished.connect(self._draft_changed)
        style_form.addRow("填充", self.fill_edit)
        self.stroke_edit = QLineEdit()
        self.stroke_edit.setPlaceholderText("#111111")
        self.stroke_edit.editingFinished.connect(self._draft_changed)
        style_form.addRow("描边", self.stroke_edit)
        self.stroke_spin = QSpinBox()
        self.stroke_spin.setRange(0, 64)
        self.stroke_spin.valueChanged.connect(self._draft_changed)
        style_form.addRow("描边宽", self.stroke_spin)
        self.line_spacing_spin = QDoubleSpinBox()
        self.line_spacing_spin.setRange(0.5, 3.0)
        self.line_spacing_spin.setSingleStep(0.05)
        self.line_spacing_spin.setDecimals(2)
        self.line_spacing_spin.valueChanged.connect(self._draft_changed)
        style_form.addRow("行距", self.line_spacing_spin)
        self.shadow_check = QCheckBox("阴影")
        self.shadow_check.stateChanged.connect(self._draft_changed)
        style_form.addRow("", self.shadow_check)
        self.style_widget.setVisible(False)
        self.style_toggle.toggled.connect(self.style_widget.setVisible)
        self.style_toggle.toggled.connect(
            lambda checked: self.style_toggle.setIcon(icon("chevron_down" if checked else "chevron_right"))
        )
        layout.addWidget(self.style_widget)

        # 更多折叠区
        self.more_toggle = QPushButton("更多")
        self.more_toggle.setIcon(icon("chevron_right"))
        self.more_toggle.setIconSize(QSize(14, 14))
        self.more_toggle.setFixedHeight(26)
        self.more_toggle.setObjectName("quiet")
        self.more_toggle.setCheckable(True)
        layout.addWidget(self.more_toggle)
        self.more_widget = QWidget()
        more_form = QFormLayout(self.more_widget)
        more_form.setContentsMargins(8, 0, 0, 0)
        more_form.setSpacing(4)
        font_row = QHBoxLayout()
        self.font_path_edit = QLineEdit()
        self.font_path_edit.setReadOnly(True)
        self.font_path_edit.setPlaceholderText("默认字体")
        font_row.addWidget(self.font_path_edit, 1)
        self.font_button = QPushButton("…")
        self.font_button.setFixedWidth(24)
        self.font_button.clicked.connect(self._pick_font)
        font_row.addWidget(self.font_button)
        more_form.addRow("字体", font_row)
        self.rotation_spin = QDoubleSpinBox()
        self.rotation_spin.setRange(-180.0, 180.0)
        self.rotation_spin.setSingleStep(1.0)
        self.rotation_spin.setSuffix("°")
        self.rotation_spin.valueChanged.connect(self._draft_changed)
        more_form.addRow("旋转", self.rotation_spin)
        self.x_spin = self._coordinate_spin()
        self.y_spin = self._coordinate_spin()
        self.x_spin.valueChanged.connect(self._draft_changed)
        self.y_spin.valueChanged.connect(self._draft_changed)
        xy_row = QHBoxLayout()
        xy_row.addWidget(self.x_spin)
        xy_row.addWidget(self.y_spin)
        more_form.addRow("X/Y", xy_row)
        self.more_widget.setVisible(False)
        self.more_toggle.toggled.connect(self.more_widget.setVisible)
        self.more_toggle.toggled.connect(
            lambda checked: self.more_toggle.setIcon(icon("chevron_down" if checked else "chevron_right"))
        )
        layout.addWidget(self.more_widget)

        layout.addStretch(1)
        return panel

    def _build_asset_panel(self) -> QWidget:
        """素材/形状对象属性面板。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 对象操作
        ops_row = QHBoxLayout()
        ops_row.setSpacing(4)
        self.duplicate_asset_button = QPushButton("复制")
        self.duplicate_asset_button.setFixedHeight(26)
        self.duplicate_asset_button.clicked.connect(self._duplicate_selected_object)
        ops_row.addWidget(self.duplicate_asset_button)
        self.delete_asset_button = QPushButton("删除")
        self.delete_asset_button.setFixedHeight(26)
        self.delete_asset_button.clicked.connect(self._delete_selected_object)
        ops_row.addWidget(self.delete_asset_button)
        self.raise_asset_button = QPushButton("前移")
        self.raise_asset_button.setFixedHeight(26)
        self.raise_asset_button.clicked.connect(lambda: self._move_selected_layer(1))
        ops_row.addWidget(self.raise_asset_button)
        self.lower_asset_button = QPushButton("后移")
        self.lower_asset_button.setFixedHeight(26)
        self.lower_asset_button.clicked.connect(lambda: self._move_selected_layer(-1))
        ops_row.addWidget(self.lower_asset_button)
        layout.addLayout(ops_row)

        # 变换
        transform_form = QFormLayout()
        transform_form.setSpacing(4)
        self.overlay_scale_spin = QDoubleSpinBox()
        self.overlay_scale_spin.setRange(0.05, 4.0)
        self.overlay_scale_spin.setSingleStep(0.05)
        self.overlay_scale_spin.setDecimals(2)
        self.overlay_scale_spin.setSuffix("×")
        self.overlay_scale_spin.valueChanged.connect(self._store_overlay_controls)
        transform_form.addRow("缩放", self.overlay_scale_spin)
        self.overlay_rotation_spin = QDoubleSpinBox()
        self.overlay_rotation_spin.setRange(-180.0, 180.0)
        self.overlay_rotation_spin.setSingleStep(1.0)
        self.overlay_rotation_spin.setDecimals(0)
        self.overlay_rotation_spin.setSuffix("°")
        self.overlay_rotation_spin.valueChanged.connect(self._store_overlay_controls)
        transform_form.addRow("旋转", self.overlay_rotation_spin)
        layout.addLayout(transform_form)

        # 添加新素材入口
        layout.addSpacing(4)
        add_row = QHBoxLayout()
        add_row.setSpacing(4)
        self.import_asset_button2 = QPushButton("导入")
        self.import_asset_button2.setFixedHeight(26)
        self.import_asset_button2.setObjectName("quiet")
        self.import_asset_button2.setToolTip("导入图片素材")
        self.import_asset_button2.clicked.connect(self._import_asset)
        add_row.addWidget(self.import_asset_button2)
        self.add_shape_button2 = QPushButton("形状")
        self.add_shape_button2.setFixedHeight(26)
        self.add_shape_button2.setObjectName("quiet")
        self.add_shape_button2.setToolTip("添加圆圈/箭头/矩形")
        self.add_shape_button2.clicked.connect(self._add_shape)
        add_row.addWidget(self.add_shape_button2)
        layout.addLayout(add_row)

        layout.addStretch(1)
        return panel

    def _build_bg_panel(self) -> QWidget:
        """背景取景面板。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("缩放"))
        self.zoom_spin = QDoubleSpinBox()
        self.zoom_spin.setRange(1.0, 2.5)
        self.zoom_spin.setSingleStep(0.1)
        self.zoom_spin.setDecimals(1)
        self.zoom_spin.setSuffix(" ×")
        self.zoom_spin.setFixedHeight(26)
        self.zoom_spin.valueChanged.connect(self._zoom_changed)
        zoom_row.addWidget(self.zoom_spin, 1)
        layout.addLayout(zoom_row)

        fit_row = QHBoxLayout()
        fit_row.setSpacing(4)
        self.fill_button = QPushButton("充满")
        self.fill_button.setFixedHeight(26)
        self.fill_button.clicked.connect(self._fill_canvas)
        fit_row.addWidget(self.fill_button)
        self.fit_button = QPushButton("适应")
        self.fit_button.setFixedHeight(26)
        self.fit_button.clicked.connect(self._fit_canvas)
        fit_row.addWidget(self.fit_button)
        layout.addLayout(fit_row)

        # 取帧操作
        layout.addSpacing(4)
        extract_row = QHBoxLayout()
        extract_row.setSpacing(4)
        self.extract_button = QPushButton("取帧")
        self.extract_button.setFixedHeight(26)
        self.extract_button.setObjectName("quiet")
        self.extract_button.setToolTip("从当前视频指定时间取帧")
        self.extract_button.clicked.connect(self._extract_frame)
        extract_row.addWidget(self.extract_button)
        self.import_bg_button = QPushButton("导入底图")
        self.import_bg_button.setFixedHeight(26)
        self.import_bg_button.setObjectName("quiet")
        self.import_bg_button.clicked.connect(self._import_image)
        extract_row.addWidget(self.import_bg_button)
        layout.addLayout(extract_row)

        # 精确时间
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("时间"))
        self.timestamp_edit = QDoubleSpinBox()
        self.timestamp_edit.setRange(0.0, 24 * 60 * 60)
        self.timestamp_edit.setDecimals(2)
        self.timestamp_edit.setSuffix(" 秒")
        self.timestamp_edit.setFixedHeight(26)
        time_row.addWidget(self.timestamp_edit, 1)
        layout.addLayout(time_row)

        layout.addStretch(1)
        return panel

    def _build_empty_panel(self) -> QWidget:
        """无项目/无选中时的默认面板：素材入口 + AI。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        hint = QLabel("选择项目后，点击画布中的文字或素材进行编辑")
        hint.setObjectName("subtle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(8)

        # 素材入口
        assets_label = QLabel("素材")
        assets_label.setObjectName("subtle")
        layout.addWidget(assets_label)
        asset_row = QHBoxLayout()
        asset_row.setSpacing(4)
        self.import_asset_button = QPushButton("导入")
        self.import_asset_button.setFixedHeight(26)
        self.import_asset_button.setToolTip("导入图片素材")
        self.import_asset_button.clicked.connect(self._import_asset)
        asset_row.addWidget(self.import_asset_button)
        self.browse_asset_button = QPushButton("素材库")
        self.browse_asset_button.setFixedHeight(26)
        self.browse_asset_button.setToolTip("浏览本地素材")
        self.browse_asset_button.clicked.connect(self._browse_assets)
        asset_row.addWidget(self.browse_asset_button)
        self.add_shape_button = QPushButton("形状")
        self.add_shape_button.setFixedHeight(26)
        self.add_shape_button.setToolTip("添加圆圈/箭头/矩形")
        self.add_shape_button.clicked.connect(self._add_shape)
        asset_row.addWidget(self.add_shape_button)
        layout.addLayout(asset_row)

        layout.addStretch(1)
        return panel

    @staticmethod
    def _coordinate_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1.0)
        spin.setSingleStep(0.01)
        spin.setDecimals(2)
        return spin

    def _canvas_selection_changed(self, title_selected: bool):
        """根据画布选择切换右侧上下文面板。"""

        title_selected = bool(title_selected)
        if not title_selected:
            self._update_canvas_hint()
        if self.document is not None:
            if title_selected:
                text_ids = [item.id for item in self.document.objects if isinstance(item, TextObject)]
                selected = self._selected_text_id if self._selected_text_id in text_ids else next(
                    (item.id for item in self.document.objects if isinstance(item, TextObject) and item.copy_role == "B"),
                    text_ids[0] if text_ids else None,
                )
                self._selected_text_id = selected
            else:
                selected = next((item.id for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            self.document = replace(self.document, selected_object_id=selected)
        if hasattr(self, "canvas"):
            self.canvas.set_selected_object(
                self._selected_text_id if title_selected and self._selected_text_id else "background"
            )
        # 切换右侧面板
        if title_selected:
            self.panel_title.setText("封面文案")
            self.panel_stack.setCurrentWidget(self.copy_controls)
            self._sync_selected_text_controls()
        else:
            self.panel_title.setText("底图取景")
            self.panel_stack.setCurrentWidget(self.bg_controls)
        self._sync_overlay_controls()

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
        for widget in (self.font_path_edit, self.fill_edit, self.stroke_edit, self.stroke_spin, self.line_spacing_spin, self.shadow_check, self.rotation_spin):
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
            self.fill_edit.setText(text.style.fill_color)
            self.stroke_edit.setText(text.style.stroke_color)
            self.stroke_spin.setValue(int(text.style.stroke_width))
            self.line_spacing_spin.setValue(float(text.style.line_spacing))
            self.shadow_check.setChecked(bool(text.style.shadow))
            self.rotation_spin.setValue(float(text.transform.rotation))
            for role, button in self.copy_role_buttons.items():
                button.blockSignals(True)
                button.setChecked(role == text.copy_role)
                button.blockSignals(False)
            for key, button in self.align_buttons.items():
                button.blockSignals(True)
                button.setChecked(key == text.align)
                button.blockSignals(False)
        finally:
            self.title_edit.blockSignals(False)
            self.font_spin.blockSignals(False)
            for widget in (self.font_path_edit, self.fill_edit, self.stroke_edit, self.stroke_spin, self.line_spacing_spin, self.shadow_check, self.rotation_spin):
                widget.blockSignals(False)

    def _canvas_object_selected(self, object_id: str):
        if self.document is None:
            return
        selected = next((item for item in self.document.objects if item.id == object_id), None)
        if isinstance(selected, TextObject):
            self._selected_text_id = selected.id
            self.document = replace(self.document, selected_object_id=selected.id)
            self._canvas_selection_changed(True)
        elif isinstance(selected, BackgroundObject):
            self.document = replace(self.document, selected_object_id=selected.id)
            self._canvas_selection_changed(False)
        elif isinstance(selected, (ImageObject, StickerObject, ShapeObject)):
            self.document = replace(self.document, selected_object_id=selected.id)
            self.panel_title.setText("素材")
            self.panel_stack.setCurrentWidget(self.asset_controls)
            self.canvas.set_selected_object(selected.id)
            self._sync_overlay_controls()

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

    def _commit_document_change(self, before: CoverDocument | None = None) -> None:
        """原子提交文档，并让所有界面和异步任务转向同一份状态。"""

        if self.document is None:
            return
        self.draft = CoverDraft.from_document(self.document)
        # CoverDocument 是唯一主状态。候选、撤销和手势提交后先把控件重读
        # 为新文档，再启动任何延迟任务，避免旧控件反向覆盖 profile override。
        self._apply_draft()
        self._sync_overlay_controls()
        self.history.commit(self.document)
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self._invalidate_render_requests()
        self._draft_timer.start()
        self._preview_timer.start()

    def _invalidate_render_requests(self) -> None:
        """使正在运行的预览/检查预览回调失效，并允许新状态立即排队。"""

        self._preview_request_generation += 1
        self._busy = False
        self._preview_dirty = False
        self._check_request_generation += 1
        self._check_busy = False
        self._check_dirty = False

    def _undo(self):
        document = self.history.undo()
        if document is None:
            return
        self.document = document
        self.draft = CoverDraft.from_document(self.document)
        self._apply_draft()
        self._sync_overlay_controls()
        self._invalidate_render_requests()
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self._draft_timer.start()
        self._preview_timer.start()

    def _redo(self):
        document = self.history.redo()
        if document is None:
            return
        self.document = document
        self.draft = CoverDraft.from_document(self.document)
        self._apply_draft()
        self._sync_overlay_controls()
        self._invalidate_render_requests()
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self._draft_timer.start()
        self._preview_timer.start()

    def _import_asset(self):
        source, _ = QFileDialog.getOpenFileName(self, "选择封面素材", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not source or self.document is None:
            return
        try:
            asset = self.service.asset_library.import_file(source)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法导入素材", str(exc))
            return
        before = self.document
        object_id = f"image-{len(self.document.objects) + 1}"
        z_index = max((item.z_index for item in self.document.objects), default=0) + 1
        overlay = ImageObject(
            id=object_id,
            z_index=z_index,
            asset=AssetRef(path=asset.path, asset_id=asset.asset_id),
            transform=Transform(x=0.62, y=0.54, scale=0.85, rotation=0.0),
        )
        self.document = replace(self.document, objects=(*self.document.objects, overlay), selected_object_id=object_id)
        self.service.asset_library.mark_used(asset.asset_id)
        self._selected_text_id = None
        self.canvas.set_selected_object(object_id)
        self._commit_document_change(before)

    def _browse_assets(self):
        if self.document is None:
            return
        try:
            assets = self.service.asset_library.list_assets(preferred_group=self.project.title if self.project else None)
        except (OSError, ValueError) as exc:
            message = f"素材库暂不可用：{exc}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        if not assets:
            message = "素材库为空；可以先导入一张自定义图片"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        box = QMessageBox(self)
        box.setWindowTitle("本地素材")
        box.setText("素材按当前主播、最近使用和使用频率排序；选择后会作为可编辑图片对象加入画布。")
        buttons = []
        for asset in assets[:12]:
            button = box.addButton(f"{asset.name} · {asset.group} · {asset.usage_count} 次", QMessageBox.ButtonRole.AcceptRole)
            buttons.append((button, asset))
        box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        asset = next((asset for button, asset in buttons if button is clicked), None)
        if asset is not None:
            self._insert_asset_object(asset)

    def _insert_asset_object(self, asset):
        if self.document is None:
            return
        before = self.document
        object_id = f"image-{len(self.document.objects) + 1}"
        overlay_type = StickerObject if asset.group != "我的导入" else ImageObject
        overlay_kwargs = {
            "id": object_id,
            "z_index": max((item.z_index for item in self.document.objects), default=0) + 1,
            "asset": AssetRef(path=asset.path, asset_id=asset.asset_id),
            "transform": Transform(x=0.62, y=0.54, scale=0.85),
        }
        if overlay_type is StickerObject:
            overlay_kwargs["category"] = asset.group
        overlay = overlay_type(
            **overlay_kwargs,
        )
        self.document = replace(self.document, objects=(*self.document.objects, overlay), selected_object_id=object_id)
        self.service.asset_library.mark_used(asset.asset_id)
        self.canvas.set_selected_object(object_id)
        self._commit_document_change(before)

    def _add_shape(self):
        if self.document is None:
            return
        before = self.document
        choices = ("circle", "arrow", "rect")
        index = getattr(self, "_shape_cycle", -1) + 1
        self._shape_cycle = index % len(choices)
        shape_type = choices[self._shape_cycle]
        shape_id = f"shape-{len(self.document.objects) + 1}"
        z_index = max((item.z_index for item in self.document.objects), default=0) + 1
        shape = ShapeObject(id=shape_id, z_index=z_index, shape_type=shape_type, transform=Transform(x=0.58, y=0.44, scale=1.0))
        self.document = replace(self.document, objects=(*self.document.objects, shape), selected_object_id=shape_id)
        self.canvas.set_selected_object(shape_id)
        self._commit_document_change(before)
        self.status_changed.emit(f"已添加{shape_type}强调对象；再次点击可切换形状")

    def _selected_render_object(self):
        if self.document is None:
            return None
        object_id = self.document.selected_object_id
        return next((item for item in self.document.objects if item.id == object_id), None)

    def _sync_overlay_controls(self):
        """同步当前图片、贴图或强调形状的缩放和旋转控件。"""

        selected = self._selected_render_object()
        if self.document is not None and selected is not None:
            effective = object_for_profile(self.document, selected.id, self._canvas_key)
            if isinstance(effective, (ImageObject, StickerObject, ShapeObject)):
                selected = effective
        enabled = isinstance(selected, (ImageObject, StickerObject, ShapeObject))
        for widget in (self.overlay_scale_spin, self.overlay_rotation_spin):
            widget.blockSignals(True)
            widget.setEnabled(enabled)
        try:
            if enabled:
                self.overlay_scale_spin.setValue(float(selected.transform.scale))
                self.overlay_rotation_spin.setValue(float(selected.transform.rotation))
            else:
                self.overlay_scale_spin.setValue(1.0)
                self.overlay_rotation_spin.setValue(0.0)
        finally:
            for widget in (self.overlay_scale_spin, self.overlay_rotation_spin):
                widget.blockSignals(False)

    def _store_overlay_controls(self):
        """把对象缩放/旋转写入当前比例的 profile override。"""

        if self.document is None:
            return
        selected = self._selected_render_object()
        if not isinstance(selected, (ImageObject, StickerObject, ShapeObject)):
            self._sync_overlay_controls()
            return
        current = object_for_profile(self.document, selected.id, self._canvas_key)
        if not isinstance(current, (ImageObject, StickerObject, ShapeObject)):
            current = selected
        updated = replace(
            current,
            transform=replace(
                current.transform,
                scale=self.overlay_scale_spin.value(),
                rotation=self.overlay_rotation_spin.value(),
            ),
        )
        if updated == current:
            return
        profile = self.document.profiles[self._canvas_key]
        payload = {"transform": updated.transform.to_payload(), "visible": bool(updated.visible)}
        if isinstance(updated, (ImageObject, StickerObject)):
            payload["opacity"] = updated.opacity
        else:
            payload.update({
                "shape_type": updated.shape_type,
                "fill": updated.fill,
                "stroke": updated.stroke,
                "stroke_width": updated.stroke_width,
                "width": updated.width,
                "height": updated.height,
            })
        self.document = replace(
            self.document,
            active_profile=self._canvas_key,
            profiles={
                **self.document.profiles,
                self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload}),
            },
        )
        self.history.commit(self.document)
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        self.canvas.set_document(self.document, self._canvas_key)
        self.draft = CoverDraft.from_document(self.document)
        self._invalidate_render_requests()
        self._draft_timer.start()
        self._preview_timer.start()

    def _duplicate_selected_object(self):
        from dataclasses import replace as dc_replace
        item = self._selected_render_object()
        if self.document is None or not isinstance(item, (ImageObject, StickerObject, ShapeObject)):
            self.status_changed.emit("请先在画布上选中图片或强调对象")
            return
        before = self.document
        new_id = f"{item.kind}-{len(self.document.objects) + 1}"
        duplicate = dc_replace(item, id=new_id, z_index=max(obj.z_index for obj in self.document.objects) + 1, transform=dc_replace(item.transform, x=min(0.92, item.transform.x + 0.04), y=min(0.92, item.transform.y + 0.04)))
        self.document = replace(self.document, objects=(*self.document.objects, duplicate), selected_object_id=new_id)
        self.canvas.set_selected_object(new_id)
        self._commit_document_change(before)

    def _delete_selected_object(self):
        item = self._selected_render_object()
        if self.document is None or item is None or item.kind == "background":
            return
        before = self.document
        if isinstance(item, TextObject):
            self.document = replace(self.document, objects=tuple(replace(obj, visible=False) if obj.id == item.id else obj for obj in self.document.objects), selected_object_id=None)
        else:
            self.document = replace(self.document, objects=tuple(obj for obj in self.document.objects if obj.id != item.id), selected_object_id=None)
        self._commit_document_change(before)

    def _move_selected_layer(self, delta: int):
        item = self._selected_render_object()
        if self.document is None or item is None:
            return
        before = self.document
        updated = replace(item, z_index=max(-100, min(1000, item.z_index + int(delta))))
        self.document = replace(self.document, objects=tuple(updated if obj.id == item.id else obj for obj in self.document.objects))
        self._commit_document_change(before)

    def _request_ai_candidates(self):
        if self.document is None:
            return
        try:
            self._ai_candidates = self.service.ai_candidates(
                self.document, profile_key=self._canvas_key
            )
        except Exception as exc:  # noqa: BLE001 - AI 失败必须回到界面显示
            message = f"AI 候选生成失败：{exc}"
            self.ai_candidate_label.setText("AI 暂不可用，请继续手工编辑")
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        if not self._ai_candidates:
            message = "当前没有明显更优的 AI 候选，请继续手工编辑"
            self.ai_candidate_label.setText(message)
            self._set_notice(message, "warning")
            return
        recommended = next((item for item in self._ai_candidates if item.recommended), self._ai_candidates[0])
        self.ai_candidate_label.setText(f"推荐：{recommended.label} · {recommended.difference}（点击按钮后手动应用）")
        box = QMessageBox(self)
        box.setWindowTitle("AutoCover AI Beta 候选")
        box.setText("候选只改变可编辑的 CoverDocument 布局，默认不会调用真实 AI。请选择要应用的方案：")
        buttons = []
        for item in self._ai_candidates:
            button = box.addButton(f"使用：{item.label}", QMessageBox.ButtonRole.AcceptRole)
            buttons.append((button, item))
        box.addButton("暂不使用", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        selected = next((item for button, item in buttons if button is clicked), None)
        if selected is None:
            return
        before = self.document
        self.document = selected.document
        self._selected_ai_candidate = selected.candidate_id
        self._commit_document_change(before)
        self.status_changed.emit(f"已应用可编辑候选：{selected.label}")

    def _canvas_object_changed(self, item, profile_key: str):
        """接收手势提交后的对象变换，保持 CoverDocument 为唯一主状态。"""

        if self.document is None or not hasattr(item, "id"):
            return
        profile = self.document.profiles.get(profile_key)
        if profile is None:
            return
        payload = {"transform": item.transform.to_payload(), "visible": bool(getattr(item, "visible", True))}
        if isinstance(item, BackgroundObject):
            payload.update({"scale": item.scale, "pan_x": item.pan_x, "pan_y": item.pan_y, "fit_mode": item.fit_mode})
        elif isinstance(item, TextObject):
            payload.update({"rect": item.rect.to_payload(), "wrap": item.wrap.to_payload(), "align": item.align, "style": item.style.to_payload()})
        elif isinstance(item, (ImageObject, StickerObject)):
            payload.update({"opacity": item.opacity})
        elif isinstance(item, ShapeObject):
            payload.update({"shape_type": item.shape_type, "fill": item.fill, "stroke": item.stroke, "stroke_width": item.stroke_width, "width": item.width, "height": item.height})
        self.document = replace(
            self.document,
            active_profile=profile_key,
            selected_object_id=item.id,
            profiles={
                **self.document.profiles,
                profile_key: replace(profile, overrides={**profile.overrides, item.id: payload}),
            },
        )
        self.draft = CoverDraft.from_document(self.document)
        self.history.commit(self.document)
        self.undo_button.setEnabled(self.history.can_undo)
        self.redo_button.setEnabled(self.history.can_redo)
        if isinstance(item, TextObject):
            self._selected_text_id = item.id
            # Canvas 手势先发 object_changed、再发位置兼容信号；先同步字号，
            # 避免后续旧兼容入口用右侧面板的旧值覆盖画布刚调整的字号。
            self.font_spin.blockSignals(True)
            try:
                self.font_spin.setValue(int(item.style.font_size))
            finally:
                self.font_spin.blockSignals(False)
            for key, button in self.align_buttons.items():
                button.blockSignals(True)
                button.setChecked(key == item.align)
                button.blockSignals(False)
            self._update_title_rect()
        elif isinstance(item, (ImageObject, StickerObject, ShapeObject)):
            self._sync_overlay_controls()
        self._draft_timer.start()
        self._invalidate_render_requests()
        self._preview_timer.start()

    def _canvas_object_preview_changed(self, item, _profile_key: str):
        """只更新轻量控件显示；拖动帧不得写文档、保存或渲染。"""

        if isinstance(item, TextObject):
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
            for role, button in self.copy_role_buttons.items():
                button.blockSignals(True)
                button.setChecked(role == item.copy_role)
                button.blockSignals(False)
        elif isinstance(item, BackgroundObject):
            self.zoom_spin.blockSignals(True)
            try:
                self.zoom_spin.setValue(float(item.scale))
            finally:
                self.zoom_spin.blockSignals(False)

    def _canvas_safety_changed(self, warning: bool):
        if warning:
            self.canvas_hint.setText(
                f"{self._canvas_label()} · 标题靠近画布边缘，仍可继续拖动；滚轮缩放"
            )
        else:
            self._update_canvas_hint()

    def _canvas_label(self) -> str:
        return "4:3" if self._canvas_key == "4x3" else "16:9"

    def _update_canvas_hint(self):
        if self.project is None or self.video is None:
            self.canvas_hint.setText("先到“字幕”页选择投稿项目和视频")
            return
        self.canvas_hint.setText(
            f"{self._canvas_label()} 主画布 · 选中标题或底图后拖动；滚轮缩放"
        )

    def _set_canvas_key(self, canvas_key: str):
        if canvas_key not in {"4x3", "16x9"} or canvas_key == self._canvas_key:
            return
        if self.document is not None:
            self.document = replace(self.document, active_profile=canvas_key)
            self.draft = CoverDraft.from_document(self.document)
            self._draft_timer.start()
        self._canvas_key = canvas_key
        # 旧比例的后台渲染结果只丢弃，不阻塞新比例的主画布。
        self._invalidate_render_requests()
        for key, button in self.canvas_ratio_buttons.items():
            button.setChecked(key == canvas_key)
        self._update_canvas_hint()
        self._update_export_summary()
        if self.document is not None:
            self._apply_draft()
            self._sync_overlay_controls()
        if self.draft.image_path and self.video is not None:
            self.canvas.setText(f"正在准备 {self._canvas_label()} 预览…")
            self._render_preview()
            return
        self.canvas.setText(f"加载底图后在这里预览 {self._canvas_label()} 画布")

    def _update_export_summary(self):
        """把当前输出契约放在导出动作旁，tooltip 只补充完整路径。"""

        if self.video is None:
            self.export_summary.setText("未选择视频 · 输出到项目目录")
            self.export_summary.setToolTip("选择投稿项目和视频后显示封面文件名与完整输出路径")
            if hasattr(self, "export_button"):
                self.export_button.setText("导出 4:3")
                self.export_button.setToolTip("导出 4:3（1440×1080 JPG）")
            return
        stem = Path(self.video.name).stem or "封面"
        if self._canvas_key == "4x3":
            filename = f"AutoCover-{stem}.jpg"
            size = "1440×1080"
        else:
            filename = f"AutoCover-{stem}-16x9.jpg"
            size = "1920×1080"
        output_dir = Path(self.project.directory) if self.project is not None else Path(self.video.path).parent
        self.export_summary.setText(
            f"{self._canvas_label()} · {size} · 项目目录 · {filename}"
        )
        self.export_summary.setToolTip(str(output_dir / filename))
        if hasattr(self, "export_button"):
            self.export_button.setText(f"导出 {self._canvas_label()}")
            self.export_button.setToolTip(
                f"导出 {self._canvas_label()}（{size} JPG）到项目目录，不覆盖已有文件"
            )

    def _refresh_nearby_frame_strip(self, center: float):
        center = max(0.0, float(center or 0.0))
        self.frame_center_label.setText(f"当前 {center:.2f} 秒")
        offsets = (-1.20, -0.80, -0.40, 0.0, 0.40, 0.80, 1.20)
        available = self.video is not None and Path(self.video.path).is_file()
        for button, offset in zip(self.nearby_frame_buttons, offsets):
            timestamp = max(0.0, center + offset)
            button.setProperty("timestamp", timestamp)
            button.setText(f"{timestamp:.2f}s")
            button.setIcon(QIcon())
            button.setEnabled(available)
        self._update_nearby_frame_selection(
            self._selected_frame_timestamp if self._selected_frame_timestamp is not None else center
        )

    def _queue_nearby_thumbnails(self, center: float):
        # 页面未显示时不启动 FFmpeg；进入封面页后由 showEvent 补排队。
        if not self.isVisible() or self.video is None or not Path(self.video.path).is_file():
            return
        center = max(0.0, float(center or 0.0))
        offsets = (-1.20, -0.80, -0.40, 0.0, 0.40, 0.80, 1.20)
        self._nearby_request_generation += 1
        request_generation = self._nearby_request_generation
        video = self.video
        self._run(
            lambda: self.service.extract_nearby_frames(video, center, offsets),
            lambda result, error: self._nearby_frames_ready(
                request_generation, result, error
            ),
        )

    def _nearby_frames_ready(self, request_generation: int, result, error):
        if request_generation != self._nearby_request_generation:
            return
        if error:
            message = f"附近帧预览失败：{error}"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        if not result:
            self._set_notice("没有找到附近可用画面", "warning")
            return
        frames = tuple(result)
        for button in self.nearby_frame_buttons:
            target = float(button.property("timestamp") or 0.0)
            path, _timestamp = min(frames, key=lambda item: abs(item[1] - target))
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                continue
            button.setIcon(QIcon(pixmap))
        self._set_notice("")

    def _choose_nearby_frame(self, timestamp: float):
        if self.video is None:
            return
        timestamp = max(0.0, float(timestamp))
        self._update_nearby_frame_selection(timestamp)
        self.timestamp_edit.setValue(timestamp)
        self._extract_frame()

    def _toggle_frame_lock(self, checked: bool):
        if self.document is None:
            self.frame_lock_button.setChecked(False)
            return
        before = self.document
        self._frame_locked = bool(checked)
        self.document = self.service.set_frame_locked(self.document, self._frame_locked)
        self.frame_lock_button.setText("已锁帧" if self._frame_locked else "锁帧")
        self._commit_document_change(before)
        message = "已锁定当前帧，后台候选和 AI 不会自动换帧" if self._frame_locked else "已解除帧锁定"
        self.status_changed.emit(message)

    def _find_more_frames(self):
        if self.video is None or not Path(self.video.path).is_file():
            message = "请先在字幕页选择一个包含视频的投稿项目"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        center = self.timestamp_edit.value() if self.draft.image_path else self._current_playhead
        request_generation = self._nearby_request_generation + 1
        self._nearby_request_generation = request_generation
        video = self.video
        self.more_frames_button.setEnabled(False)
        self.status_changed.emit("正在按需寻找更大范围画面…")
        self._run(
            lambda: self.service.extract_wider_frames(video, center),
            lambda result, error: self._wider_frames_ready(request_generation, result, error),
        )

    def _wider_frames_ready(self, request_generation: int, result, error):
        self.more_frames_button.setEnabled(self.video is not None)
        if request_generation != self._nearby_request_generation or error:
            if error:
                message = f"扩大取帧范围失败：{error}"
                self._set_notice(message, "error")
                self.status_changed.emit(message)
            return
        self._wider_frames = tuple(result or ())
        if not self._wider_frames:
            self._set_notice("没有找到更多可用画面", "warning")
            return
        for button, item in zip(self.nearby_frame_buttons, self._wider_frames[:: max(1, len(self._wider_frames) // len(self.nearby_frame_buttons))]):
            path, timestamp = item
            button.setProperty("timestamp", timestamp)
            button.setText(f"{timestamp:.2f}s")
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                button.setIcon(QIcon(pixmap))
        self._update_nearby_frame_selection()
        message = f"已补充 {len(self._wider_frames)} 张更大范围候选"
        self.status_changed.emit(message)

    def _cycle_copy(self):
        if not self._copy_variants:
            return
        self._copy_variant_index = (self._copy_variant_index + 1) % len(self._copy_variants)
        candidate = self._copy_variants[self._copy_variant_index]
        if self.document is not None:
            objects = []
            for item in self.document.objects:
                if not isinstance(item, TextObject):
                    objects.append(item)
                    continue
                value = candidate.context if item.copy_role == "A" else candidate.headline
                visible = bool(value.strip())
                font_size = item.style.font_size
                line_count = max(1, len(wrap_cover_title(value or "标题", font_size, canvas_width=1440)))
                width = min(0.60, max(0.24, len(value or "标题") * font_size / 1440.0 * 1.18))
                height = min(0.55, max(0.06, line_count * font_size * 1.18 / 1080.0))
                objects.append(replace(
                    item,
                    text=value,
                    visible=visible,
                    rect=Rect(width=width, height=height),
                    wrap=TextWrap(max_width=width, max_lines=3),
                ))
            self.document = replace(self.document, objects=tuple(objects))
            self.draft = CoverDraft.from_document(self.document)
            self.canvas.set_document(self.document, self._canvas_key)
            self._sync_selected_text_controls()
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        else:
            self.title_edit.setPlainText(candidate.headline)
        self.status_changed.emit(
            f"已切换本地基础文案 {self._copy_variant_index + 1}/{len(self._copy_variants)}"
        )
        self._draft_timer.start()
        self._preview_timer.start()

    def set_current_playhead(self, seconds: float):
        """接收字幕页只读播放位置，不触发 seek 或修改字幕状态。"""

        self._current_playhead = max(0.0, float(seconds or 0.0))
        if self.video is not None:
            self.current_frame_button.setToolTip(
                f"使用字幕页当前帧（{self._current_playhead:.2f} 秒），不会改变播放位置"
            )
            if not self.draft.image_path:
                self._refresh_nearby_frame_strip(self._current_playhead)
                if self.isVisible() and not self._frame_extract_pending:
                    QTimer.singleShot(0, self._use_current_frame)

    def set_context(self, project: SubmissionProject | None, video: ProjectVideo | None):
        if project is None or video is None:
            self._context_generation += 1
            self.project = None
            self.video = None
            self.document = None
            self.history.reset(None)
            self._busy = False
            self._check_busy = False
            self._check_dirty = False
            self._frame_extract_pending = False
            self._frame_request_generation += 1
            self._nearby_request_generation += 1
            self._preview_request_generation += 1
            self._check_request_generation += 1
            self._copy_variants = ()
            self._copy_variant_index = -1
            self.project_label.setText("请选择项目")
            self.video_label.setText("请选择视频")
            self.draft_status.setText("未加载草稿")
            self._selected_frame_timestamp = None
            self._refresh_nearby_frame_strip(0.0)
            self._update_export_summary()
            self._update_canvas_hint()
            self.canvas.set_preview(QPixmap())
            self.canvas.set_background_pixmap(QPixmap())
            self.canvas.setText("请先在字幕页选择投稿项目和视频")
            self.check_preview.clear()
            self.check_preview.setText("展开后显示另一比例预览")
            self.check_preview_toggle.setChecked(False)
            self.check_preview_toggle.setEnabled(False)
            self.check_preview.setVisible(False)
            self._set_notice("")
            self.extract_button.setEnabled(False)
            self.current_frame_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self.export_both_button.setEnabled(False)
            self.undo_button.setEnabled(False)
            self.redo_button.setEnabled(False)
            self.frame_lock_button.setChecked(False)
            # 无项目时隐藏右侧面板，画布占满
            self._hide_panel()
            self.panel_toggle.setEnabled(False)
            self._show_empty_panel()
            return
        if self.project is not None and self.project.id == project.id and self.video is not None and self.video.path == video.path:
            return
        if self.project is not None and self.video is not None:
            self._save_draft()
        self._context_generation += 1
        self.project, self.video = project, video
        self.project_label.setText(project.title if len(project.title) <= 32 else project.title[:32] + "…")
        self.project_label.setToolTip(project.title)
        self.video_label.setText(video.name)
        self.check_preview_toggle.setEnabled(True)
        self._set_notice("")
        self._update_export_summary()
        video_path = Path(video.path)
        video_available = video_path.is_file()
        self.extract_button.setEnabled(video_available)
        self.current_frame_button.setEnabled(video_available)
        self.extract_button.setToolTip("从指定时间取帧" if video_available else f"当前视频不存在：{video_path}")
        # 有项目时恢复右侧面板
        self._show_panel()
        self.panel_toggle.setEnabled(True)
        self.panel_toggle.setChecked(False)
        self._preview_path = None
        self._busy = False
        self._check_busy = False
        self._check_dirty = False
        self.check_preview.clear()
        self.check_preview.setText("生成后显示另一比例")
        self._frame_extract_pending = False
        self._frame_request_generation += 1
        self._nearby_request_generation += 1
        self._preview_request_generation += 1
        self._check_request_generation += 1
        self._canvas_key = "4x3"
        for key, button in self.canvas_ratio_buttons.items():
            button.setChecked(key == self._canvas_key)
        self._update_canvas_hint()
        self._copy_variants = self.service.basic_copy_variants(
            project.title,
            subtitle_context=self.service.subtitle_context(video, self._current_playhead),
        )
        self.document, read = self.service.load_document(project, video)
        self.history.reset(self.document)
        self._frame_locked = bool(self.document.source.frame_locked)
        self.frame_lock_button.blockSignals(True)
        self.frame_lock_button.setChecked(self._frame_locked)
        self.frame_lock_button.setText("已锁帧" if self._frame_locked else "锁帧")
        self.frame_lock_button.blockSignals(False)
        self.draft = CoverDraft.from_document(self.document)
        self._selected_frame_timestamp = (
            float(self.draft.selected_timestamp) if self.draft.image_path else max(0.0, self._current_playhead)
        )
        self._copy_variant_index = next(
            (
                index for index, candidate in enumerate(self._copy_variants)
                if candidate.text == self.draft.title or candidate.headline == self.draft.title
            ),
            -1,
        )
        self._apply_draft()
        # 有项目时默认显示文字面板
        self._canvas_selection_changed(True)
        if not video_available:
            self.draft_status.setText("视频不可用")
            self.draft_status.setToolTip(str(video_path))
            self._set_notice(f"当前视频不可用：{video_path}", "error")
        elif read.status == "ready":
            self.draft_status.setText("已恢复草稿")
            self.draft_status.setToolTip("")
        elif read.status == "missing":
            self.draft_status.setText("新草稿 · 自动保存")
            self.draft_status.setToolTip("")
        else:
            self.draft_status.setText(f"草稿需检查：{read.status}")
            self._set_notice(f"草稿状态需要检查：{read.status}", "warning")
        self._refresh_nearby_frame_strip(
            self.draft.selected_timestamp if self.draft.image_path else self._current_playhead
        )
        if self.draft.image_path:
            self._render_preview()
            self._queue_nearby_thumbnails(self.draft.selected_timestamp)
        else:
            self.canvas.setText("正在准备当前帧…" if self.isVisible() else "进入封面页后自动加载当前帧")
            self.export_button.setEnabled(False)
            if self.isVisible() and not self._frame_extract_pending:
                QTimer.singleShot(0, self._use_current_frame)

    def _apply_draft(self):
        widgets = (self.title_edit, self.x_spin, self.y_spin, self.font_spin, self.zoom_spin)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            self.title_edit.setPlainText(self.draft.title)
            self.x_spin.setValue(self.draft.text_x)
            self.y_spin.setValue(self.draft.text_y)
            self.font_spin.setValue(self.draft.font_size)
            self.zoom_spin.setValue(self.draft.background_scale)
            self.timestamp_edit.setValue(self.draft.selected_timestamp)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self.canvas.set_zoom(self.draft.background_scale)
        self.canvas.set_background_focus(self.draft.background_x, self.draft.background_y)
        self.canvas.set_document(self.document, self._canvas_key)
        if self.document is not None:
            selected = next(
                (item for item in self.document.objects if item.id == self.document.selected_object_id),
                None,
            )
            if isinstance(selected, TextObject):
                self._selected_text_id = selected.id
                self._sync_selected_text_controls()
            elif self._selected_text_id is None:
                text = next(
                    (item for item in self.document.objects if isinstance(item, TextObject) and item.copy_role == "B"),
                    None,
                ) or next((item for item in self.document.objects if isinstance(item, TextObject)), None)
                self._selected_text_id = text.id if text else None
        self._update_title_rect()

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
        typed_text = self.title_edit.text().strip()
        if current.copy_role == "A" and not typed_text:
            typed_text = ""
        fallback_text = (self.project.title if self.project else "未命名封面") if current.copy_role == "B" else ""
        fill = self.fill_edit.text().strip() or current.style.fill_color
        stroke = self.stroke_edit.text().strip() or current.style.stroke_color
        if not (fill.startswith("#") and len(fill) in {4, 7, 9}):
            fill = current.style.fill_color
        if not (stroke.startswith("#") and len(stroke) in {4, 7, 9}):
            stroke = current.style.stroke_color
        updated = replace(
            current,
            text=typed_text or fallback_text,
            visible=bool(typed_text.strip() or fallback_text),
            transform=replace(current.transform, x=self.x_spin.value(), y=self.y_spin.value(), rotation=self.rotation_spin.value()),
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
            ),
        )
        profile = self.document.profiles[self._canvas_key]
        payload = {"transform": updated.transform.to_payload(), "visible": bool(updated.visible), "rect": updated.rect.to_payload(), "wrap": updated.wrap.to_payload(), "align": updated.align, "style": updated.style.to_payload()}
        self.document = replace(self.document, active_profile=self._canvas_key, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})

    def _store_background_controls(self):
        if self.document is None:
            return
        background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
        if background is None:
            return
        current = object_for_profile(self.document, background.id, self._canvas_key)
        if not isinstance(current, BackgroundObject):
            current = background
        updated = replace(current, scale=self.zoom_spin.value())
        profile = self.document.profiles[self._canvas_key]
        payload = {"transform": updated.transform.to_payload(), "scale": updated.scale, "pan_x": updated.pan_x, "pan_y": updated.pan_y, "fit_mode": updated.fit_mode}
        self.document = replace(self.document, active_profile=self._canvas_key, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})

    def _read_draft(self) -> CoverDraft:
        self._store_text_controls()
        self._store_background_controls()
        if self.document is not None:
            self.draft = CoverDraft.from_document(self.document)
            return self.draft
        return replace(
            self.draft,
            title=self.title_edit.text().strip() or (self.project.title if self.project else "未命名封面"),
            text_x=self.x_spin.value(),
            text_y=self.y_spin.value(),
            font_size=self.font_spin.value(),
            background_scale=self.zoom_spin.value(),
        )

    def _draft_changed(self):
        if self.project is None or self.video is None:
            return
        before = self.document
        self._store_text_controls()
        self._store_background_controls()
        self.draft = CoverDraft.from_document(self.document) if self.document else self._read_draft()
        if self.document is not None and before != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
            self._invalidate_render_requests()
        self._update_title_rect()
        self._draft_timer.start()
        self._preview_timer.start()

    def _title_position_changed(self, x: float, y: float):
        """同步坐标显示；文档和重型任务在 mouse release 提交。"""

        if self.project is None or self.video is None:
            return
        self.x_spin.blockSignals(True)
        self.y_spin.blockSignals(True)
        try:
            self.x_spin.setValue(x)
            self.y_spin.setValue(y)
        finally:
            self.x_spin.blockSignals(False)
            self.y_spin.blockSignals(False)
        if self.document is None:
            # 兼容没有 v4 文档的旧调用方；这里只更新内存草稿。
            self.draft = replace(self.draft, text_x=x, text_y=y)
        elif getattr(self.canvas, "_mode", None) is None:
            # 程序化调用（旧测试、键盘微调）仍需同步文档；真实鼠标拖动
            # 在 canvas 的 release 提交前不会走这里的重型路径。
            text = self._selected_text()
            current = object_for_profile(self.document, text.id, self._canvas_key) if text else None
            if isinstance(current, TextObject) and (abs(current.transform.x - x) > 1e-6 or abs(current.transform.y - y) > 1e-6):
                self._store_text_controls()
                self.draft = CoverDraft.from_document(self.document)

    def _background_position_changed(self, x: float, y: float):
        if self.project is None or self.video is None:
            return
        # Canvas 已经持有拖动中的本地对象；这里只刷新轻量取景显示。
        self.canvas.set_background_focus(x, y)

    def _zoom_changed(self, value: float):
        if self.project is None or self.video is None:
            return
        value = max(1.0, min(2.5, float(value)))
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(value)
        self.zoom_spin.blockSignals(False)
        before = self.document
        if self.document is not None:
            background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            if background is not None:
                current = object_for_profile(self.document, background.id, self._canvas_key)
                current = current if isinstance(current, BackgroundObject) else background
                updated = replace(current, scale=value)
                profile = self.document.profiles[self._canvas_key]
                payload = {"transform": updated.transform.to_payload(), "scale": value, "pan_x": updated.pan_x, "pan_y": updated.pan_y, "fit_mode": updated.fit_mode}
                self.document = replace(self.document, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})
            self.draft = CoverDraft.from_document(self.document)
        else:
            self.draft = replace(self._read_draft(), background_scale=value)
        if self.document is not None and before != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        self.canvas.set_zoom(value)
        self._draft_timer.start()
        self._preview_timer.start()

    def _gesture_finished(self):
        # object_changed 已在 release 提交；取消同一手势留下的 debounce，
        # 避免释放时立即保存后又在 500ms 再写一次。
        self._draft_timer.stop()
        self._save_draft()

    def _fill_canvas(self):
        self._set_background_transform(0.5, 0.5, 1.0, fit_mode="cover")

    def _fit_canvas(self):
        self._set_background_transform(0.5, 0.5, 1.0, fit_mode="contain")

    def _set_background_transform(self, x: float, y: float, scale: float, *, fit_mode: str | None = None):
        before = self.document
        if self.document is not None:
            background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            if background is not None:
                current = object_for_profile(self.document, background.id, self._canvas_key)
                current = current if isinstance(current, BackgroundObject) else background
                updated = replace(current, pan_x=x, pan_y=y, scale=scale, fit_mode=fit_mode or current.fit_mode)
                profile = self.document.profiles[self._canvas_key]
                payload = {"transform": updated.transform.to_payload(), "scale": scale, "pan_x": x, "pan_y": y, "fit_mode": updated.fit_mode}
                self.document = replace(self.document, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides={**profile.overrides, updated.id: payload})})
            self.draft = CoverDraft.from_document(self.document)
        else:
            self.draft = replace(self._read_draft(), background_x=x, background_y=y, background_scale=scale)
        if self.document is not None and before != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(scale)
        self.zoom_spin.blockSignals(False)
        self.canvas.set_zoom(scale)
        self.canvas.set_background_focus(x, y)
        self._draft_timer.start()
        self._preview_timer.start()

    def _save_draft(self):
        if self.project is None or self.video is None:
            return
        try:
            if self.document is not None:
                # 文档已经在控件信号/画布手势提交时写入；延迟保存只能消费
                # 这份快照，不能再次从可能过期的控件反向重建它。
                self.draft = CoverDraft.from_document(self.document)
                self.service.save_document(self.project, self.video, self.document)
                self.service.remember_style(self.project, self.document)
            else:
                self.draft = self._read_draft()
                self.service.save(self.project, self.video, self.draft)
            self.draft_status.setText("已保存")
        except (OSError, ValueError) as exc:
            message = f"封面草稿保存失败：{exc}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)

    def _run(self, action, callback):
        generation = self._context_generation
        job = _Job(action)
        self._jobs.add(job)

        def done(result, error):
            try:
                if generation == self._context_generation:
                    callback(result, error)
            finally:
                self._jobs.discard(job)

        job.signals.finished.connect(done)
        QThreadPool.globalInstance().start(job)

    def _import_image(self):
        source, _ = QFileDialog.getOpenFileName(self, "选择封面底图", "", "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not source or self.project is None or self.video is None:
            return
        try:
            path = self.service.import_image(source)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法导入底图", str(exc))
            return
        before_document = self.document
        if self.document is not None:
            background = next((item for item in self.document.objects if isinstance(item, BackgroundObject)), None)
            if background is not None:
                self.document = replace(self.document, objects=tuple(replace(item, asset=replace(item.asset, path=str(path)) if item.asset else AssetRef(path=str(path))) if isinstance(item, BackgroundObject) else item for item in self.document.objects))
                self.draft = CoverDraft.from_document(self.document)
            else:
                self.draft = replace(self._read_draft(), image_path=str(path))
        if self.document is not None and before_document != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        else:
            self.draft = replace(self._read_draft(), image_path=str(path))
        self._save_draft()
        self._render_preview()

    def _use_current_frame(self):
        self.timestamp_edit.setValue(self._current_playhead)
        self._update_nearby_frame_selection(self._current_playhead)
        self._refresh_nearby_frame_strip(self._current_playhead)
        self._extract_frame()

    def _extract_frame(self):
        if self.video is None or not Path(self.video.path).is_file():
            message = "请先在字幕页选择投稿项目和视频"
            self._set_notice(message, "warning")
            self.status_changed.emit(message)
            return
        first_background = not bool(self.draft.image_path)
        self.extract_button.setEnabled(False)
        self.current_frame_button.setEnabled(False)
        for button in self.nearby_frame_buttons:
            button.setEnabled(False)
        self._frame_extract_pending = True
        self._frame_request_generation += 1
        request_generation = self._frame_request_generation
        video = self.video
        timestamp = self.timestamp_edit.value()
        self._set_notice("正在从视频取帧…", "info")
        self.status_changed.emit("正在从视频取帧…")
        self._run(
            lambda: self.service.extract_frame(video, timestamp),
            lambda result, error: self._frame_ready(
                request_generation, first_background, result, error
            ),
        )

    def _frame_ready(self, request_generation: int, first_background: bool, result, error):
        if request_generation != self._frame_request_generation:
            return
        self._frame_extract_pending = False
        self.extract_button.setEnabled(self.video is not None and Path(self.video.path).is_file())
        self.current_frame_button.setEnabled(self.extract_button.isEnabled())
        self._refresh_nearby_frame_strip(self.timestamp_edit.value())
        if error:
            message = f"取帧失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        path, timestamp = result
        self._update_nearby_frame_selection(timestamp)
        before_document = self.document
        draft = replace(self._read_draft(), image_path=str(path), selected_timestamp=timestamp)
        if self.document is not None:
            backgrounds = tuple(replace(item, asset=replace(item.asset, path=str(path)) if item.asset else AssetRef(path=str(path))) if isinstance(item, BackgroundObject) else item for item in self.document.objects)
            self.document = replace(self.document, source=replace(self.document.source, selected_timestamp=timestamp, image_asset_id=str(path)), objects=backgrounds)
            draft = CoverDraft.from_document(self.document)
        if first_background:
            text_x, text_y = self.service.suggest_text_position(
                path, draft, canvas_key=self._canvas_key
            )
            draft = replace(draft, text_x=text_x, text_y=text_y)
            if self.document is not None:
                profile = self.document.profiles[self._canvas_key]
                overrides = dict(profile.overrides)
                texts = [item for item in self.document.objects if isinstance(item, TextObject)]
                has_context_block = any(item.copy_role == "A" and item.visible and item.text.strip() for item in texts)
                for text in texts:
                    # 沿用旧版的同一 anchor 槽位，但把 A/B 作为有明确
                    # gap 的上下两块；旧的“B 固定 0.18、A 往上挤”会在
                    # 两个 rect 高度变化后重新重叠。
                    target_y = (
                        max(0.16, text_y + 0.18)
                        if text.copy_role == "B" and has_context_block
                        else max(0.05, text_y)
                        if text.copy_role == "B"
                        else max(0.04, text_y)
                    )
                    updated = replace(text, transform=replace(text.transform, x=text_x, y=target_y))
                    overrides[updated.id] = {
                        "transform": updated.transform.to_payload(),
                        "rect": updated.rect.to_payload(),
                        "wrap": updated.wrap.to_payload(),
                        "align": updated.align,
                        "style": updated.style.to_payload(),
                        "visible": bool(updated.visible),
                    }
                self.document = replace(self.document, profiles={**self.document.profiles, self._canvas_key: replace(profile, overrides=overrides)})
                draft = CoverDraft.from_document(self.document)
            self.x_spin.blockSignals(True)
            self.y_spin.blockSignals(True)
            try:
                self.x_spin.setValue(text_x)
                self.y_spin.setValue(text_y)
            finally:
                self.x_spin.blockSignals(False)
                self.y_spin.blockSignals(False)
        self.draft = draft
        if self.document is not None and before_document != self.document:
            self.history.commit(self.document)
            self.undo_button.setEnabled(self.history.can_undo)
            self.redo_button.setEnabled(self.history.can_redo)
        self.timestamp_edit.setValue(timestamp)
        self._refresh_nearby_frame_strip(timestamp)
        self._save_draft()
        self._set_notice("")
        self.status_changed.emit("已加载当前视频画面")
        self._render_preview()
        self._queue_nearby_thumbnails(timestamp)

    def _render_preview(self):
        if self.video is None or not self.draft.image_path:
            return
        if self._busy:
            self._preview_dirty = True
            return
        self._preview_dirty = False
        self._busy = True
        self._set_notice(f"正在生成 {self._canvas_label()} 预览…", "info")
        self._preview_request_generation += 1
        request_generation = self._preview_request_generation
        canvas_key = self._canvas_key
        document = self.document
        # CoverDocument 是预览的唯一输入；只有兼容旧调用方的无文档路径
        # 才需要读取控件草稿。
        draft = self.draft if document is not None else self._read_draft()
        self.draft = draft
        video = self.video
        self._run(
            lambda: self.service.render_preview_document(video, document, canvas_key=canvas_key) if document is not None else self.service.render_preview(video, draft, canvas_key=canvas_key),
            lambda result, error: self._preview_ready(
                request_generation, canvas_key, result, error
            ),
        )

    def _preview_ready(self, request_generation: int, canvas_key: str, result, error):
        if (
            request_generation != self._preview_request_generation
            or canvas_key != self._canvas_key
        ):
            return
        self._busy = False
        if error:
            self.canvas.setText("预览失败")
            self._set_notice(f"封面预览失败：{error}", "error")
            self.export_button.setEnabled(False)
            return
        self._preview_path = Path(result)
        self._set_preview(self._preview_path)
        self.export_button.setEnabled(True)
        self.export_both_button.setEnabled(True)
        self._set_notice("")
        self._queue_check_preview()
        if self._preview_dirty:
            self._preview_timer.start()
        elif self._pending_export:
            self._pending_export = False
            self._start_export()

    def _queue_check_preview(self):
        # 另一比例是低优先级缩略图；主画布始终由 _canvas_key 控制。
        if (
            not self._check_preview_visible
            or not self.isVisible()
            or self.video is None
            or not self.draft.image_path
        ):
            return
        if self._check_busy:
            self._check_dirty = True
            return
        self._check_busy = True
        self._check_dirty = False
        self._check_request_generation += 1
        request_generation = self._check_request_generation
        canvas_key = "16x9" if self._canvas_key == "4x3" else "4x3"
        video = self.video
        document = self.document
        draft = self.draft if document is not None else self._read_draft()
        self._set_notice(f"正在生成 {canvas_key} 另一比例预览…", "info")
        self._run(
            lambda: self.service.render_preview_document(video, document, canvas_key=canvas_key) if document is not None else self.service.render_preview(video, draft, canvas_key=canvas_key),
            lambda result, error: self._check_preview_ready(
                request_generation, canvas_key, result, error
            ),
        )

    def _check_preview_ready(self, request_generation: int, canvas_key: str, result, error):
        if request_generation != self._check_request_generation:
            return
        self._check_busy = False
        if error:
            self.check_preview.clear()
            self.check_preview.setText(f"{canvas_key} 预览暂不可用")
            self._set_notice(f"另一比例预览失败：{error}", "warning")
        else:
            pixmap = QPixmap(str(result))
            if pixmap.isNull():
                self.check_preview.clear()
                self.check_preview.setText(f"{canvas_key} 预览图无法读取")
            else:
                self.check_preview.setText("")
                self.check_preview.setPixmap(
                    pixmap.scaled(
                        self.check_preview.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                self._set_notice("")
        if self._check_dirty:
            self._check_dirty = False
            self._queue_check_preview()

    def showEvent(self, event):
        super().showEvent(event)
        if self.video is not None and self.draft.image_path:
            QTimer.singleShot(0, lambda: self._queue_nearby_thumbnails(self.draft.selected_timestamp))
        if self._check_preview_visible and self.video is not None and self.draft.image_path:
            QTimer.singleShot(0, self._queue_check_preview)

    def _set_preview(self, path: Path):
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.canvas.setText("预览图片无法读取")
            return
        self.canvas.set_preview(pixmap)
        if self.draft.image_path:
            source_pixmap = QPixmap(self.draft.image_path)
            if not source_pixmap.isNull():
                self.canvas.set_background_pixmap(source_pixmap)
        if self.document is not None:
            self.canvas.set_document(self.document, self._canvas_key)
        self._update_title_rect()

    def _update_title_rect(self):
        if self.document is not None:
            self.canvas.set_document(self.document, self._canvas_key)
            self.canvas.update()
            return
        title = self.title_edit.text() if hasattr(self, "title_edit") else self.draft.title
        font_size = self.font_spin.value() if hasattr(self, "font_spin") else self.draft.font_size
        canvas_width = 1440 if self._canvas_key == "4x3" else 1920
        lines = wrap_cover_title(title or "标题", font_size, canvas_width=canvas_width)
        width_units = max((len(line) for line in lines), default=4)
        width = min(0.86, max(0.08, width_units * font_size / canvas_width * 1.18))
        height = min(0.72, max(0.06, len(lines) * font_size * 1.18 / 1080))
        self.canvas.set_title_rect(QRectF(self.draft.text_x, self.draft.text_y, width, height))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_title_rect()

    def _export(self):
        if self.project is None or self.video is None:
            return
        self._save_draft()
        self.export_button.setEnabled(False)
        self._pending_export = True
        self._preview_timer.stop()
        # 强制把当前草稿重新渲染一次；导出从这次预览完成回调启动，确保
        # 导出的 JPG 与画布最后显示的构图使用同一份状态。
        self._render_preview()

    def _export_both(self):
        if self.project is None or self.video is None or self.document is None:
            return
        self._save_draft()
        self.export_button.setEnabled(False)
        self.export_both_button.setEnabled(False)
        document = self.document
        project, video = self.project, self.video
        self._set_notice("正在分别导出 4:3 与 16:9…", "info")
        self.status_changed.emit("正在分别导出 4:3 与 16:9…")
        self._run(
            lambda: self.service.export_both(project, video, document),
            self._export_both_ready,
        )

    def _export_both_ready(self, result, error):
        self.export_button.setEnabled(self._preview_path is not None)
        self.export_both_button.setEnabled(self._preview_path is not None)
        if error:
            message = f"双比例导出失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        names = "、".join(Path(item).name for item in result)
        self.export_summary.setText(f"已导出 4:3 + 16:9 · 项目目录 · {names}")
        self.export_summary.setToolTip("\n".join(str(Path(item)) for item in result))
        self._set_notice("")
        self.status_changed.emit(f"双比例封面已导出：{names}")

    def _start_export(self):
        if self.project is None or self.video is None:
            self._pending_export = False
            return
        draft = self.draft if self.document is not None else self._read_draft()
        self._set_notice(f"正在导出 {self._canvas_label()} 主封面…", "info")
        self.status_changed.emit(f"正在导出 {self._canvas_label()} 主封面…")
        canvas_key = self._canvas_key
        self._run(
            lambda: self.service.export_document(self.project, self.video, self.document, canvas_key=canvas_key) if self.document is not None else self.service.export(self.project, self.video, draft, canvas_key=canvas_key),
            self._export_ready,
        )

    def _export_ready(self, result, error):
        self.export_button.setEnabled(self._preview_path is not None)
        self.export_both_button.setEnabled(self._preview_path is not None)
        if error:
            message = f"封面导出失败：{error}"
            self._set_notice(message, "error")
            self.status_changed.emit(message)
            return
        output_name = Path(result).name
        size = "1440×1080" if self._canvas_key == "4x3" else "1920×1080"
        output_dir = Path(self.project.directory) if self.project is not None else Path(result).parent
        self.export_summary.setText(
            f"已导出 · {self._canvas_label()} · {size} · 项目目录 · {output_name}"
        )
        self.export_summary.setToolTip(str(output_dir / output_name))
        message = f"封面已导出：{output_name}（{self._canvas_label()} · {size}）"
        self._set_notice("")
        self.status_changed.emit(message)
