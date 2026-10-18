"""界面构建：工具栏、画布区、右侧面板、帧条，以及提示与导出摘要。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from autoslice_cover.document_layout import (
    BACKGROUND_SCALE_MAX,
    BACKGROUND_SCALE_MIN,
    OVERLAY_SCALE_MAX,
)

from .cover_canvas import CoverCanvas
from .cover_style import STYLE_PRESETS
from .cover_widgets import (
    FitToolbar,
    ToolbarButton,
    _ColorButton,
    _InlineTextEdit,
    _preset_icon,
    _TitleEdit,
)
from .qt_preview import motion, popups
from .qt_preview.icons import icon
from .qt_preview.theme import COLORS
from .qt_preview.widgets import ElidedLabel


class CoverLayoutMixin:
    def _button(
        self,
        text: str = "",
        *,
        icon_name: str | None = None,
        icon_size: int = 16,
        tip: str = "",
        slot=None,
        height: int = 28,
        square: bool = False,
        object_name: str | None = "tool",
        enabled: bool = True,
        checkable: bool = False,
    ) -> ToolbarButton:
        """工具栏、面板和帧条按钮的统一外观：图标、尺寸、提示、点击和初始可用状态。"""

        button = ToolbarButton(text)
        if icon_name:
            color = COLORS.on_accent if object_name == "primary" else COLORS.muted
            button.setIcon(icon(icon_name, color, icon_size, COLORS.accent_text if checkable else None))
            button.setIconSize(QSize(icon_size, icon_size))
        if object_name:
            button.setObjectName(object_name)
        if square:
            button.setFixedSize(height, height)
            button.setProperty("square", True)
        else:
            button.setFixedHeight(height)
        if tip:
            button.setToolTip(tip)
        button.setCheckable(checkable)
        if slot is not None:
            button.clicked.connect(slot)
        button.setEnabled(enabled)
        return button

    @staticmethod
    def _set_menu(button: QPushButton, menu: QMenu) -> None:
        """按钮挂菜单：加弹层动效，并给下拉箭头留出位置。"""

        popups.MenuFx(menu)
        button.setProperty("hasMenu", True)
        button.setMenu(menu)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())
        # 警告/错误提示条；例行进度只进状态栏，不占这里。
        self.notice_label = QLabel("")
        self.notice_label.setObjectName("coverNoticeInfo")
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)
        root.addWidget(self.notice_label)
        # 不显示：记录当前项目与视频名，供诊断和测试读取。
        self.project_label = QLabel("")
        self.video_label = QLabel("")
        content = QHBoxLayout()
        content.setSpacing(0)
        content.setContentsMargins(0, 0, 0, 0)
        content.addWidget(self._build_canvas_area(), 1)
        content.addWidget(self._build_right_panel())
        root.addLayout(content, 1)
        root.addWidget(self._build_frame_strip())
        # 面板折叠；无项目时先收起右侧面板，画布占满。
        self._panel_visible = True
        self._last_panel_width = 280
        self.panel_toggle.toggled.connect(self._toggle_panel)
        self._hide_panel()
        self.panel_toggle.setEnabled(False)
        self._update_canvas_hint()

    def _build_toolbar(self) -> QWidget:
        """单行工具栏：比例与同步 · 撤销/重做 · 插入 · 状态 · 批量/记录/导出/下一个；放不下时按钮收成只显示图标。"""

        toolbar = FitToolbar()
        toolbar.setObjectName("coverToolbar")
        toolbar.setFixedHeight(44)
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(6)

        # 比例：分段控件，和字幕页的“全部 / 待确认”同一套外观
        segmented = QWidget()
        segmented.setObjectName("segmented")
        segment_row = QHBoxLayout(segmented)
        segment_row.setContentsMargins(2, 2, 2, 2)
        segment_row.setSpacing(2)
        self.canvas_ratio_group = QButtonGroup(self)
        self.canvas_ratio_group.setExclusive(True)
        self.canvas_ratio_buttons: dict[str, QPushButton] = {}
        for key, text in (("4x3", "4:3"), ("16x9", "16:9")):
            button = self._button(
                text, tip=f"切换 {text} 主画布", object_name="segment", checkable=True, height=24,
            )
            button.setMinimumWidth(48)
            button.clicked.connect(lambda _checked=False, value=key: self._set_canvas_key(value))
            self.canvas_ratio_group.addButton(button)
            self.canvas_ratio_buttons[key] = button
            segment_row.addWidget(button)
        self.canvas_ratio_buttons["4x3"].setChecked(True)
        motion.SegmentedFx(segmented, tuple(self.canvas_ratio_buttons.values()))
        row.addWidget(segmented)
        self.sync_ratio_button = self._button(
            "同步到 16:9", icon_name="refresh",
            tip="把当前比例里调好的文字和素材位置套到另一个比例；底图取景各自保留",
            slot=self._sync_other_ratio, enabled=False,
        )
        row.addWidget(self.sync_ratio_button)
        row.addSpacing(8)

        self.undo_button = self._button(icon_name="undo", tip="撤销", slot=self._undo, square=True, enabled=False)
        self.redo_button = self._button(icon_name="redo", tip="重做", slot=self._redo, square=True, enabled=False)
        row.addWidget(self.undo_button)
        row.addWidget(self.redo_button)
        row.addSpacing(8)

        # 插入：文字 / 素材 / 形状常驻工具栏；“已隐藏”只在有隐藏对象时出现。
        self.add_text_button = self._button(
            "文字", icon_name="text", tip="新建文本框；双击画布上的文字可直接改字", slot=self._add_text, enabled=False,
        )
        self.asset_menu_button = self._button(
            "素材", icon_name="image", tip="从素材库选择或导入图片，作为可编辑对象加入画布", enabled=False,
        )
        self.asset_menu = QMenu(self.asset_menu_button)
        self.asset_menu.aboutToShow.connect(self._fill_asset_menu)
        self._fill_asset_menu()
        self._set_menu(self.asset_menu_button, self.asset_menu)
        self.shape_menu_button = self._button("形状", icon_name="shapes", tip="添加圆圈、箭头或矩形强调框", enabled=False)
        self._set_menu(self.shape_menu_button, self._shape_menu(self.shape_menu_button))
        self.hidden_button = self._button("已隐藏", icon_name="eye", tip="被删除或隐藏的文字和素材，点一下恢复")
        self.ai_critique_button = self._button(
            "AI 改一改", icon_name="sparkles", tip="让 AI 看这张封面，给出能一键应用的修改（移字、拉近、放大、换文案），可撤销",
            slot=self._ai_critique, enabled=False,
        )
        self.hidden_menu = QMenu(self.hidden_button)
        self.hidden_menu.aboutToShow.connect(self._fill_hidden_menu)
        self._set_menu(self.hidden_button, self.hidden_menu)
        self.hidden_button.setVisible(False)
        for button in (self.add_text_button, self.asset_menu_button, self.shape_menu_button, self.hidden_button, self.ai_critique_button):
            row.addWidget(button)
        row.addStretch(1)

        self.draft_status = QLabel("")
        self.draft_status.setObjectName("subtle")
        row.addWidget(self.draft_status)
        self.batch_button = self._button(
            "批量出图", icon_name="layers", tip="给投稿目录里的多个视频一次出封面（4:3 + 16:9）",
            slot=self._open_batch, enabled=False,
        )
        self.history_button = self._button(
            icon_name="history", tip="导出记录：回看最近导出的封面", slot=self._open_export_history, square=True,
        )
        self.submission_button = self._button(
            "投稿文案", icon_name="sparkles", tip="让 AI 按你以前的标题口吻写 3 个备选标题、简介和标签，一键复制",
            slot=self._submission_copy, enabled=False,
        )
        # 输出摘要放在画布下方的提示行里（见 _build_canvas_area），工具栏只留动作。
        self.export_summary = ElidedLabel("")
        self.export_summary.setObjectName("subtle")
        self.export_button = self._button(
            "导出", tip="导出当前画布比例", slot=self._export, object_name=None, enabled=False,
        )
        # 投稿要两个比例，双比例导出是主动作；旧网页端同样以“保存双比例”为主按钮。
        self.export_both_button = self._button(
            "导出 4:3 + 16:9", icon_name="download", object_name="primary", slot=self._export_both, enabled=False,
            tip="分别导出 4:3（1440×1080）与 16:9（1920×1080），不覆盖已有文件",
        )
        # 主按钮的文字永远完整显示
        self.export_both_button.setMinimumWidth(self.export_both_button.sizeHint().width())
        self.next_button = self._button(
            "下一个", icon_name="chevron_right", icon_size=14, tip="保存当前封面，切到投稿列表里的下一个视频",
            slot=self._request_next_video, enabled=False,
        )
        self.panel_toggle = self._button(
            icon_name="chevron_right", tip="收起/展开属性面板", square=True, checkable=True,
        )
        for widget in (
            self.batch_button, self.submission_button, self.history_button, self.export_button,
            self.export_both_button, self.next_button, self.panel_toggle,
        ):
            row.addWidget(widget)
        # 先收低频的（同步、批量、投稿文案、下一个），还放不下再收文字/素材/形状/AI
        toolbar.set_collapsible(
            (self.sync_ratio_button, self.batch_button, self.submission_button, self.next_button),
            (self.add_text_button, self.asset_menu_button, self.shape_menu_button, self.hidden_button,
             self.ai_critique_button),
        )
        return toolbar

    def _build_canvas_area(self) -> QWidget:
        """中央画布、就地输入框和画布下方的操作提示。"""

        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        self.canvas = CoverCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        for signal, slot in (
            (self.canvas.title_position_changed, self._title_position_changed),
            (self.canvas.title_position_finished, self._gesture_finished),
            (self.canvas.background_position_finished, self._gesture_finished),
            (self.canvas.zoom_changed, self._zoom_changed),
            (self.canvas.selected_changed, self._canvas_selection_changed),
            (self.canvas.selected_object_changed, self._canvas_object_selected),
            (self.canvas.safe_area_warning_changed, self._canvas_safety_changed),
            (self.canvas.object_changed, self._canvas_object_changed),
            (self.canvas.object_preview_changed, self._canvas_object_preview_changed),
            (self.canvas.delete_requested, self._delete_object),
            (self.canvas.duplicate_requested, self._duplicate_object),
            (self.canvas.edit_requested, self._edit_text),
        ):
            signal.connect(slot)
        self.canvas.locked_hint.connect(lambda: self.status_changed.emit("对象已锁定：先在右侧解锁再移动或删除"))
        self.inline_edit = _InlineTextEdit(self.canvas)
        self.inline_edit.setObjectName("inlineTextEdit")
        self.inline_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inline_edit.setToolTip("回车换行；Ctrl+回车或点别处完成；Esc 放弃")
        self.inline_edit.hide()
        self.inline_edit.textChanged.connect(self._inline_text_changed)
        self.inline_edit.finished.connect(self._finish_inline_edit)
        layout.addWidget(self.canvas, 1)
        # 提示行：左边操作提示，右边本次导出的文件名与比例；放不下时各自右侧省略
        info_row = QHBoxLayout()
        info_row.setSpacing(12)
        self.canvas_hint = ElidedLabel("")
        self.canvas_hint.setObjectName("subtle")
        info_row.addWidget(self.canvas_hint, 3)
        self.export_summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self.export_summary, 2)
        layout.addLayout(info_row)
        return center

    def _build_right_panel(self) -> QWidget:
        """右侧：快速方案在上，当前对象（文字/素材/底图）的属性在下。"""

        self.right_panel = QWidget()
        self.right_panel.setObjectName("coverPanel")
        self.right_panel.setMinimumWidth(0)
        self.right_panel.setMaximumWidth(280)
        layout = QVBoxLayout(self.right_panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        layout.addLayout(self._build_scheme_section())
        layout.addSpacing(6)
        self.panel_title = QLabel("封面文案")
        self.panel_title.setObjectName("sectionTitle")
        layout.addWidget(self.panel_title)
        self.panel_stack = QStackedWidget()
        layout.addWidget(self.panel_stack, 1)
        self.copy_controls = self._build_text_panel()
        self.asset_controls = self._build_asset_panel()
        self.bg_controls = self._build_bg_panel()
        for panel in (self.copy_controls, self.asset_controls, self.bg_controls):
            self.panel_stack.addWidget(panel)
        return self.right_panel

    def _build_frame_strip(self) -> QWidget:
        """底部帧条：拖动选帧进度条 + 当前帧/锁帧/更多/全片 + 7 张附近帧。"""

        strip = QWidget()
        strip.setObjectName("frameStrip")
        column = QVBoxLayout(strip)
        column.setContentsMargins(12, 4, 12, 6)
        column.setSpacing(2)
        # 拖动选帧：参考旧网页端“精确选帧”，松手后从这个时间取帧。
        scrub_row = QHBoxLayout()
        scrub_row.setSpacing(6)
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setRange(0, 0)
        self.frame_slider.setEnabled(False)
        self.frame_slider.setToolTip("拖动选帧：松手后从这个时间取帧")
        self.frame_slider.sliderMoved.connect(lambda value: self.frame_center_label.setText(f"选到 {value / 10:.1f} 秒"))
        self.frame_slider.sliderReleased.connect(self._slider_frame)
        self._slider_timer = QTimer(self)
        self._slider_timer.setSingleShot(True)
        self._slider_timer.setInterval(300)
        self._slider_timer.timeout.connect(self._slider_frame)
        self.frame_slider.valueChanged.connect(
            lambda _value: None if self.frame_slider.isSliderDown() else self._slider_timer.start()
        )
        scrub_row.addWidget(self.frame_slider, 1)
        self.duration_label = QLabel("")
        self.duration_label.setObjectName("subtle")
        scrub_row.addWidget(self.duration_label)
        column.addLayout(scrub_row)

        row = QHBoxLayout()
        row.setSpacing(4)
        column.addLayout(row)
        self.frame_center_label = QLabel("0.00s")
        self.frame_center_label.setObjectName("subtle")
        self.frame_center_label.setMinimumWidth(92)
        row.addWidget(self.frame_center_label)
        self.current_frame_button = self._button(
            "当前帧", tip="读取字幕页播放位置", slot=self._use_current_frame, height=24, object_name="quiet",
        )
        self.frame_lock_button = self._button(
            "锁帧", tip="锁定后换方案、换文案都不换帧", slot=self._toggle_frame_lock, height=24, checkable=True,
            object_name="quiet",
        )
        self.more_frames_button = self._button(
            "更多", tip="扩大取帧范围", slot=self._find_more_frames, height=24, object_name="quiet",
        )
        self.overview_button = self._button(
            "全片", tip="从整个视频按画质挑 7 张", slot=self._find_overview_frames, height=24, object_name="quiet",
        )
        for button in (self.current_frame_button, self.frame_lock_button, self.more_frames_button, self.overview_button):
            row.addWidget(button)
        row.addSpacing(8)
        self.nearby_frame_buttons = []
        for _ in range(7):
            button = ToolbarButton("--")
            button.setObjectName("frameThumb")
            # 时间戳叠在缩略图上，按钮只画图；text() 仍是完整时间，供读取与提示
            button.set_compact(True, force=True)
            button.setCheckable(True)
            button.setMinimumWidth(86)
            button.setMaximumHeight(58)
            button.setIconSize(QSize(80, 45))
            button.setProperty("timestamp", 0.0)
            button.clicked.connect(
                lambda _checked=False, item=button: self._choose_nearby_frame(float(item.property("timestamp") or 0.0))
            )
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda position, item=button: self._frame_button_menu(item, position)
            )
            self.nearby_frame_buttons.append(button)
            row.addWidget(button, 1)
        return strip

    def _build_scheme_section(self) -> QVBoxLayout:
        """快速方案：参考旧网页端“推荐排版”，三张缩略图三选一后再微调。"""

        section = QVBoxLayout()
        section.setSpacing(4)
        scheme_head = QHBoxLayout()
        scheme_head.setSpacing(4)
        self.scheme_title = QLabel("快速方案")
        self.scheme_title.setObjectName("sectionTitle")
        scheme_head.addWidget(self.scheme_title)
        scheme_head.addStretch(1)
        self.ai_scheme_button = self._button(
            "AI 方案", icon_name="sparkles",
            tip="保留当前文案和自建文字，避让已有对象优化排版；勾选下方选项才尝试更换主文案",
            slot=self._ai_schemes, enabled=False,
        )
        scheme_head.addWidget(self.ai_scheme_button)
        self.template_button = self._button(
            "模板", icon_name="template",
            tip="把排好的封面存为模板，换个视频一键套上（字的位置样式、贴纸都跟着）；批量出图也能选",
        )
        self.template_menu = QMenu(self.template_button)
        self.template_menu.aboutToShow.connect(self._fill_template_menu)
        self._set_menu(self.template_button, self.template_menu)
        scheme_head.addWidget(self.template_button)
        self.scheme_refresh_button = self._button(
            icon_name="refresh", tip="换一批：轮换文案和配色（本地生成，不调用 AI）",
            slot=self._next_scheme_batch, square=True, enabled=False,
        )
        scheme_head.addWidget(self.scheme_refresh_button)
        section.addLayout(scheme_head)
        self.ai_rewrite_check = QCheckBox("允许 AI 尝试更好的主文案")
        self.ai_rewrite_check.setToolTip("默认保留当前文字；开启后，新文案须与当前文字及标题比较通过，自建文字块仍保留")
        section.addWidget(self.ai_rewrite_check)
        scheme_row = QHBoxLayout()
        scheme_row.setSpacing(4)
        self.scheme_buttons: list[QPushButton] = []
        self.scheme_labels: list[QLabel] = []
        for index in range(3):
            column = QVBoxLayout()
            column.setSpacing(2)
            button = QPushButton("")
            button.setObjectName("schemeThumb")
            button.setCheckable(True)
            button.setFixedSize(84, 66)
            button.setIconSize(QSize(78, 58))
            button.setEnabled(False)
            button.clicked.connect(lambda _checked=False, value=index: self._apply_scheme(value))
            caption = QLabel("—")
            caption.setObjectName("subtle")
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
            column.addWidget(caption)
            scheme_row.addLayout(column)
            self.scheme_buttons.append(button)
            self.scheme_labels.append(caption)
        section.addLayout(scheme_row)
        self.ai_hint = QLabel("")
        self.ai_hint.setObjectName("subtle")
        self.ai_hint.setWordWrap(True)
        self.ai_hint.setVisible(False)
        section.addWidget(self.ai_hint)
        return section

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

    def _set_notice(self, text: str = "", level: str = "info"):
        """警告和错误用画布上方的提示条；例行进度只进状态栏，不让画布上下跳。"""
        text = str(text or "").strip()
        if level == "info":
            if text:
                self.status_changed.emit(text)
            text = ""
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

        # 强调词：只换颜色，字号和断行不变；参考切片封面里“开盒”单独标黄的做法。
        self.emphasis_edit = QLineEdit()
        self.emphasis_edit.setPlaceholderText("强调词，空格分隔")
        self.emphasis_edit.setToolTip("这些词换成强调色，比如「开盒」「14个」；强调色在“字体与效果”里改")
        self.emphasis_edit.textChanged.connect(self._draft_changed)
        layout.addWidget(self.emphasis_edit)
        emphasis_actions = QHBoxLayout()
        emphasis_actions.setSpacing(4)
        self.emphasize_button = self._button(
            "强调所选", icon_name="highlight",
            tip="在上面的文案框里选中几个字，点这里把它们设为强调（再点一次取消）", slot=self._emphasize_selection,
        )
        self.burst_button = self._button(
            "垫爆炸贴", icon_name="burst",
            tip="在这段字的强调词后面垫一个漫画爆炸贴；没有强调词就垫在整段字后面", slot=self._add_burst_behind_text,
        )
        emphasis_actions.addWidget(self.emphasize_button, 1)
        emphasis_actions.addWidget(self.burst_button, 1)
        layout.addLayout(emphasis_actions)

        # 当前文本框身份 + 换文案；选择文本框直接在画布上点。
        role_row = QHBoxLayout()
        role_row.setSpacing(4)
        self.copy_role_label = QLabel("")
        self.copy_role_label.setObjectName("subtle")
        role_row.addWidget(self.copy_role_label, 1)
        self.lock_text_button = self._button(
            icon_name="lock", tip="锁定位置：不能拖动，快速方案和比例同步也不会挪它",
            slot=self._toggle_lock, square=True, checkable=True,
        )
        role_row.addWidget(self.lock_text_button)
        self.copy_button = self._button(
            "换一版文案", icon_name="refresh", tip="只换 A/B 主文案的文字，位置和字号不变", slot=self._cycle_copy,
        )
        role_row.addWidget(self.copy_button)
        layout.addLayout(role_row)

        # 字号 + 对齐
        size_align = QHBoxLayout()
        size_align.setSpacing(6)
        size_align.addWidget(QLabel("字号"))
        self.font_spin = QSpinBox()
        self.font_spin.setRange(24, 320)
        self.font_spin.setSuffix(" px")
        self.font_spin.setFixedHeight(28)
        self.font_spin.valueChanged.connect(self._draft_changed)
        size_align.addWidget(self.font_spin, 1)
        self.align_buttons: dict[str, QPushButton] = {}
        self.align_group = QButtonGroup(self)
        self.align_group.setExclusive(True)
        align_segmented = QWidget()
        align_segmented.setObjectName("segmented")
        align_row = QHBoxLayout(align_segmented)
        align_row.setContentsMargins(2, 2, 2, 2)
        align_row.setSpacing(2)
        for key, text in (("left", "左"), ("center", "中"), ("right", "右")):
            btn = self._button(text, object_name="segment", checkable=True, height=24)
            btn.setMinimumWidth(30)
            self.align_group.addButton(btn)
            self.align_buttons[key] = btn
            align_row.addWidget(btn)
            btn.clicked.connect(lambda _c=False, k=key: self._set_text_align(k))
        self.align_buttons["left"].setChecked(True)
        motion.SegmentedFx(align_segmented, tuple(self.align_buttons.values()))
        size_align.addWidget(align_segmented)
        layout.addLayout(size_align)

        # 配色预设常驻：最常用的一步，不藏在折叠里。
        preset_row = QHBoxLayout()
        preset_row.setSpacing(3)
        self.style_preset_buttons: dict[str, QPushButton] = {}
        for preset in STYLE_PRESETS:
            button = QPushButton("")
            button.setIcon(_preset_icon(preset))
            button.setIconSize(QSize(26, 20))
            button.setFixedSize(32, 26)
            button.setObjectName("quiet")
            button.setToolTip(f"{preset.label}（同时应用到所有文本框）")
            button.clicked.connect(lambda _checked=False, item=preset: self._apply_style_preset(item))
            self.style_preset_buttons[preset.key] = button
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)

        # 样式细节折叠区
        self.style_toggle = QPushButton("字体与效果")
        self.style_toggle.setIcon(icon("chevron_right"))
        self.style_toggle.setIconSize(QSize(14, 14))
        self.style_toggle.setFixedHeight(26)
        self.style_toggle.setObjectName("quiet")
        self.style_toggle.setCheckable(True)
        layout.addWidget(self.style_toggle)
        self.style_widget = QWidget()
        style_outer = QVBoxLayout(self.style_widget)
        style_outer.setContentsMargins(8, 0, 0, 0)
        style_outer.setSpacing(4)
        style_form = QFormLayout()
        style_form.setContentsMargins(0, 0, 0, 0)
        style_form.setSpacing(4)
        style_outer.addLayout(style_form)
        font_row = QHBoxLayout()
        self.font_path_edit = QLineEdit()
        self.font_path_edit.setReadOnly(True)
        self.font_path_edit.setPlaceholderText("默认字体")
        font_row.addWidget(self.font_path_edit, 1)
        self.font_button = QPushButton("…")
        self.font_button.setFixedWidth(24)
        self.font_button.clicked.connect(self._pick_font)
        font_row.addWidget(self.font_button)
        style_form.addRow("字体", font_row)
        self.fill_color_button = _ColorButton()
        self.fill_color_button.color_changed.connect(self._draft_changed)
        style_form.addRow("填充", self.fill_color_button)
        stroke_row = QHBoxLayout()
        stroke_row.setSpacing(4)
        self.stroke_button = _ColorButton()
        self.stroke_button.color_changed.connect(self._draft_changed)
        self.stroke_spin = QSpinBox()
        self.stroke_spin.setRange(0, 64)
        self.stroke_spin.setToolTip("描边宽度")
        self.stroke_spin.valueChanged.connect(self._draft_changed)
        stroke_row.addWidget(self.stroke_button, 1)
        stroke_row.addWidget(self.stroke_spin)
        style_form.addRow("描边", stroke_row)
        outer_row = QHBoxLayout()
        outer_row.setSpacing(4)
        self.outer_stroke_button = _ColorButton(allow_none=True)
        self.outer_stroke_button.setToolTip("在描边外再加一层，常用白色外边")
        self.outer_stroke_button.color_changed.connect(self._draft_changed)
        self.outer_stroke_spin = QSpinBox()
        self.outer_stroke_spin.setRange(0, 48)
        self.outer_stroke_spin.setToolTip("外描边宽度")
        self.outer_stroke_spin.valueChanged.connect(self._draft_changed)
        outer_row.addWidget(self.outer_stroke_button, 1)
        outer_row.addWidget(self.outer_stroke_spin)
        style_form.addRow("外描边", outer_row)
        self.backdrop_button = _ColorButton(allow_none=True, allow_alpha=True)
        self.backdrop_button.setToolTip("文字底条，画面杂乱时提高可读性；可设透明度")
        self.backdrop_button.color_changed.connect(self._draft_changed)
        style_form.addRow("底条", self.backdrop_button)
        self.accent_button = _ColorButton(allow_none=True, none_label="自动")
        self.accent_button.setToolTip("强调词的颜色；“自动”按当前配色挑对比色")
        self.accent_button.color_changed.connect(self._draft_changed)
        style_form.addRow("强调色", self.accent_button)
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

        layout.addStretch(1)
        return panel

    def _build_asset_panel(self) -> QWidget:
        """素材/形状对象属性面板。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 对象操作：两列六个按钮
        ops = QGridLayout()
        ops.setHorizontalSpacing(4)
        ops.setVerticalSpacing(4)
        self.duplicate_asset_button = self._button(
            "复制", icon_name="copy", slot=self._duplicate_selected_object, tip="复制一份，稍微错开放置",
        )
        self.delete_asset_button = self._button(
            "删除", icon_name="trash", slot=self._delete_selected_object, tip="删除所选（可撤销）",
        )
        self.raise_asset_button = self._button(
            "前移", icon_name="arrow_up", slot=lambda: self._move_selected_layer(1), tip="往上一层",
        )
        self.lower_asset_button = self._button(
            "后移", icon_name="arrow_down", slot=lambda: self._move_selected_layer(-1), tip="往下一层",
        )
        self.hide_asset_button = self._button(
            "隐藏", icon_name="eye_off", slot=self._hide_selected_object, tip="先藏起来，之后从工具栏“已隐藏”恢复",
        )
        self.lock_asset_button = self._button(
            "锁定", icon_name="lock", slot=self._toggle_lock, checkable=True,
            tip="锁定位置：不能拖动，比例同步也不会挪它",
        )
        for position, button in enumerate((
            self.duplicate_asset_button, self.delete_asset_button, self.raise_asset_button,
            self.lower_asset_button, self.hide_asset_button, self.lock_asset_button,
        )):
            ops.addWidget(button, position // 2, position % 2)
        layout.addLayout(ops)

        # 变换
        transform_form = QFormLayout()
        transform_form.setSpacing(4)
        self.overlay_scale_spin = QDoubleSpinBox()
        self.overlay_scale_spin.setRange(0.05, OVERLAY_SCALE_MAX)
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
        self.overlay_opacity_spin = QSpinBox()
        self.overlay_opacity_spin.setRange(5, 100)
        self.overlay_opacity_spin.setSingleStep(5)
        self.overlay_opacity_spin.setSuffix(" %")
        self.overlay_opacity_spin.valueChanged.connect(self._store_overlay_style)
        transform_form.addRow("透明度", self.overlay_opacity_spin)
        self.shape_stroke_button = _ColorButton()
        self.shape_stroke_button.color_changed.connect(self._store_overlay_style)
        self.shape_stroke_spin = QSpinBox()
        self.shape_stroke_spin.setRange(1, 64)
        self.shape_stroke_spin.setToolTip("线宽")
        self.shape_stroke_spin.valueChanged.connect(self._store_overlay_style)
        shape_stroke_row = QHBoxLayout()
        shape_stroke_row.setSpacing(4)
        shape_stroke_row.addWidget(self.shape_stroke_button, 1)
        shape_stroke_row.addWidget(self.shape_stroke_spin)
        transform_form.addRow("线条", shape_stroke_row)
        self.shape_fill_button = _ColorButton(allow_none=True, allow_alpha=True)
        self.shape_fill_button.setToolTip("填充；可设透明度，选“无”只留线条")
        self.shape_fill_button.color_changed.connect(self._store_overlay_style)
        transform_form.addRow("填充", self.shape_fill_button)
        self.picture_border_button = _ColorButton(allow_none=True)
        self.picture_border_button.setToolTip("图片边框，拼图小图常用白边；选“无”去掉")
        self.picture_border_button.color_changed.connect(self._store_overlay_style)
        self.picture_border_spin = QSpinBox()
        self.picture_border_spin.setRange(0, 64)
        self.picture_border_spin.setToolTip("边框宽度")
        self.picture_border_spin.valueChanged.connect(self._store_overlay_style)
        border_row = QHBoxLayout()
        border_row.setSpacing(4)
        border_row.addWidget(self.picture_border_button, 1)
        border_row.addWidget(self.picture_border_spin)
        transform_form.addRow("边框", border_row)
        self.person_outline_check = QCheckBox("人物白边")
        self.person_outline_check.setToolTip("抠出来的人物描一圈白边")
        self.person_outline_check.toggled.connect(self._toggle_person_outline)
        self.person_pin_check = QCheckBox("贴住底边")
        self.person_pin_check.setToolTip("人物下沿是画面切口：缩小、挪动时下沿始终贴着封面底边")
        self.person_pin_check.toggled.connect(self._toggle_person_pin)
        person_row = QHBoxLayout()
        person_row.setSpacing(8)
        person_row.addWidget(self.person_outline_check)
        person_row.addWidget(self.person_pin_check)
        person_row.addStretch(1)
        transform_form.addRow("人物", person_row)
        self._overlay_form = transform_form
        layout.addLayout(transform_form)

        # 添加新素材入口
        layout.addSpacing(4)
        add_row = QHBoxLayout()
        add_row.setSpacing(4)
        self.import_asset_button2 = self._button(
            "导入", icon_name="upload", tip="导入图片素材", slot=self._import_asset,
        )
        add_row.addWidget(self.import_asset_button2)
        self.add_shape_button2 = self._button(
            "形状", icon_name="shapes", tip="添加圆圈、箭头、矩形框、爆炸贴、集中线",
        )
        self._set_menu(self.add_shape_button2, self._shape_menu(self.add_shape_button2))
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
        self.zoom_spin.setRange(BACKGROUND_SCALE_MIN, BACKGROUND_SCALE_MAX)
        self.zoom_spin.setSingleStep(0.1)
        self.zoom_spin.setDecimals(1)
        self.zoom_spin.setSuffix(" ×")
        self.zoom_spin.setFixedHeight(26)
        self.zoom_spin.valueChanged.connect(self._zoom_changed)
        zoom_row.addWidget(self.zoom_spin, 1)
        layout.addLayout(zoom_row)

        fit_row = QHBoxLayout()
        fit_row.setSpacing(4)
        self.fill_button = self._button("充满", icon_name="expand", slot=self._fill_canvas, tip="底图铺满画布")
        fit_row.addWidget(self.fill_button, 1)
        self.fit_button = self._button("适应", icon_name="contain", slot=self._fit_canvas, tip="底图完整放进画布")
        fit_row.addWidget(self.fit_button, 1)
        layout.addLayout(fit_row)

        # 人物抠图：抠出来的人物是单独的图层，能拖动、缩放（参考切片封面：人物清楚、背景虚化，或人物放到内容画面的角落）。
        self.cutout_button = self._button(
            "抠出人物", icon_name="person", object_name="secondary", slot=self._cutout_person,
        )
        layout.addWidget(self.cutout_button)
        effect_row = QHBoxLayout()
        effect_row.setSpacing(4)
        self.blur_spin = QSpinBox()
        self.blur_spin.setRange(0, 100)
        self.blur_spin.setSingleStep(10)
        self.blur_spin.setPrefix("虚化 ")
        self.blur_spin.setSuffix(" %")
        self.blur_spin.setFixedHeight(28)
        self.blur_spin.setToolTip("把底图糊掉，人物图层和字更突出")
        self.blur_spin.valueChanged.connect(self._background_effect_changed)
        effect_row.addWidget(self.blur_spin, 1)
        self.dim_spin = QSpinBox()
        self.dim_spin.setRange(0, 80)
        self.dim_spin.setSingleStep(5)
        self.dim_spin.setPrefix("压暗 ")
        self.dim_spin.setSuffix(" %")
        self.dim_spin.setFixedHeight(28)
        self.dim_spin.setToolTip("把底图压暗")
        self.dim_spin.valueChanged.connect(self._background_effect_changed)
        effect_row.addWidget(self.dim_spin, 1)
        layout.addLayout(effect_row)

        # 取帧操作
        layout.addSpacing(4)
        extract_row = QHBoxLayout()
        extract_row.setSpacing(4)
        self.extract_button = self._button(
            "取帧", icon_name="camera", tip="从当前视频指定时间取帧", slot=self._extract_frame,
        )
        extract_row.addWidget(self.extract_button, 1)
        self.import_bg_button = self._button("导入底图", icon_name="image", slot=self._import_image, tip="用本机图片当底图")
        extract_row.addWidget(self.import_bg_button, 1)
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

    def _canvas_label(self) -> str:
        return "4:3" if self._canvas_key == "4x3" else "16:9"

    def _update_canvas_hint(self):
        if self.project is None or self.video is None:
            self.canvas_hint.setText("先到“字幕”页选择投稿项目和视频")
            return
        self.canvas_hint.setText(
            f"{self._canvas_label()} 主画布 · 拖动文字或底图；四角缩放/旋转，两侧改行宽；双击改字；滚轮缩放底图"
        )

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

    def _canvas_safety_changed(self, warning: bool):
        if warning:
            self.canvas_hint.setText(
                f"{self._canvas_label()} · 标题靠近画布边缘，仍可继续拖动；滚轮缩放"
            )
        else:
            self._update_canvas_hint()
