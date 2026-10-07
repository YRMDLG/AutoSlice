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

from autoslice_cover.document_layout import BACKGROUND_SCALE_MAX, BACKGROUND_SCALE_MIN

from .cover_canvas import CoverCanvas
from .cover_style import STYLE_PRESETS
from .cover_widgets import (
    _ColorButton,
    _InlineTextEdit,
    _preset_icon,
    _TitleEdit,
)
from .qt_preview.icons import icon


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
        object_name: str | None = "quiet",
        enabled: bool = True,
        checkable: bool = False,
    ) -> QPushButton:
        """工具栏和帧条按钮的统一外观：图标、尺寸、提示、点击和初始可用状态。"""

        button = QPushButton(text)
        if icon_name:
            button.setIcon(icon(icon_name))
            button.setIconSize(QSize(icon_size, icon_size))
        if object_name:
            button.setObjectName(object_name)
        if square:
            button.setFixedSize(height, height)
        else:
            button.setFixedHeight(height)
        if tip:
            button.setToolTip(tip)
        button.setCheckable(checkable)
        if slot is not None:
            button.clicked.connect(slot)
        button.setEnabled(enabled)
        return button

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
        """单行工具栏：比例与同步 · 撤销/重做 · 插入 · 状态 · 批量/记录/导出/下一个。"""

        toolbar = QWidget()
        toolbar.setObjectName("coverToolbar")
        toolbar.setFixedHeight(44)
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(6)

        self.canvas_ratio_group = QButtonGroup(self)
        self.canvas_ratio_group.setExclusive(True)
        self.canvas_ratio_buttons: dict[str, QPushButton] = {}
        for key, text in (("4x3", "4:3"), ("16x9", "16:9")):
            button = self._button(text, tip=f"切换 {text} 主画布", object_name=None, checkable=True)
            button.setMinimumWidth(48)
            button.clicked.connect(lambda _checked=False, value=key: self._set_canvas_key(value))
            self.canvas_ratio_group.addButton(button)
            self.canvas_ratio_buttons[key] = button
            row.addWidget(button)
        self.canvas_ratio_buttons["4x3"].setChecked(True)
        self.sync_ratio_button = self._button(
            "同步到 16:9", tip="把当前比例里调好的文字和素材位置套到另一个比例；底图取景各自保留",
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
        self.asset_menu_button.setMenu(self.asset_menu)
        self.shape_menu_button = self._button("形状", icon_name="shapes", tip="添加圆圈、箭头或矩形强调框", enabled=False)
        self.shape_menu_button.setMenu(self._shape_menu(self.shape_menu_button))
        self.hidden_button = self._button("已隐藏", icon_name="eye", tip="被删除或隐藏的文字和素材，点一下恢复")
        self.hidden_menu = QMenu(self.hidden_button)
        self.hidden_menu.aboutToShow.connect(self._fill_hidden_menu)
        self.hidden_button.setMenu(self.hidden_menu)
        self.hidden_button.setVisible(False)
        for button in (self.add_text_button, self.asset_menu_button, self.shape_menu_button, self.hidden_button):
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
        self.export_summary = QLabel("")
        self.export_summary.setObjectName("subtle")
        self.export_summary.setWordWrap(False)
        self.export_summary.setMaximumWidth(420)
        self.export_button = self._button(
            "导出", tip="导出当前画布比例", slot=self._export, object_name=None, enabled=False,
        )
        # 投稿要两个比例，双比例导出是主动作；旧网页端同样以“保存双比例”为主按钮。
        self.export_both_button = self._button(
            "导出 4:3 + 16:9", icon_name="download", object_name="primary", slot=self._export_both, enabled=False,
            tip="分别导出 4:3（1440×1080）与 16:9（1920×1080），不覆盖已有文件",
        )
        self.next_button = self._button(
            "下一个", icon_name="chevron_right", icon_size=14, tip="保存当前封面，切到投稿列表里的下一个视频",
            slot=self._request_next_video, enabled=False,
        )
        self.panel_toggle = self._button(
            icon_name="chevron_right", tip="收起/展开属性面板", square=True, checkable=True,
        )
        for widget in (
            self.batch_button, self.history_button, self.export_summary, self.export_button,
            self.export_both_button, self.next_button, self.panel_toggle,
        ):
            row.addWidget(widget)
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
        self.canvas_hint = QLabel("")
        self.canvas_hint.setObjectName("subtle")
        self.canvas_hint.setWordWrap(False)
        self.canvas_hint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.canvas_hint)
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
        self.frame_center_label.setMinimumWidth(62)
        row.addWidget(self.frame_center_label)
        self.current_frame_button = self._button(
            "当前帧", tip="读取字幕页播放位置", slot=self._use_current_frame, height=24,
        )
        self.frame_lock_button = self._button(
            "锁帧", tip="锁定后换方案、换文案都不换帧", slot=self._toggle_frame_lock, height=24, checkable=True,
        )
        self.more_frames_button = self._button("更多", tip="扩大取帧范围", slot=self._find_more_frames, height=24)
        self.overview_button = self._button(
            "全片", tip="从整个视频按画质挑 7 张", slot=self._find_overview_frames, height=24,
        )
        for button in (self.current_frame_button, self.frame_lock_button, self.more_frames_button, self.overview_button):
            row.addWidget(button)
        row.addSpacing(8)
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
                lambda _checked=False, item=button: self._choose_nearby_frame(float(item.property("timestamp") or 0.0))
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
        scheme_title = QLabel("快速方案")
        scheme_title.setObjectName("sectionTitle")
        scheme_head.addWidget(scheme_title)
        scheme_head.addStretch(1)
        self.scheme_refresh_button = QPushButton("")
        self.scheme_refresh_button.setIcon(icon("refresh"))
        self.scheme_refresh_button.setIconSize(QSize(15, 15))
        self.scheme_refresh_button.setFixedSize(26, 26)
        self.scheme_refresh_button.setObjectName("quiet")
        self.scheme_refresh_button.setToolTip("换一批：轮换文案和配色（本地生成，不调用 AI）")
        self.scheme_refresh_button.clicked.connect(self._next_scheme_batch)
        self.scheme_refresh_button.setEnabled(False)
        scheme_head.addWidget(self.scheme_refresh_button)
        section.addLayout(scheme_head)
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

        # 当前文本框身份 + 换文案；选择文本框直接在画布上点。
        role_row = QHBoxLayout()
        role_row.setSpacing(4)
        self.copy_role_label = QLabel("")
        self.copy_role_label.setObjectName("subtle")
        role_row.addWidget(self.copy_role_label, 1)
        self.lock_text_button = QPushButton("锁定")
        self.lock_text_button.setCheckable(True)
        self.lock_text_button.setFixedHeight(26)
        self.lock_text_button.setObjectName("quiet")
        self.lock_text_button.setToolTip("锁定位置：不能拖动，快速方案和比例同步也不会挪它")
        self.lock_text_button.clicked.connect(self._toggle_lock)
        role_row.addWidget(self.lock_text_button)
        self.copy_button = QPushButton("换一版文案")
        self.copy_button.setFixedHeight(26)
        self.copy_button.setObjectName("quiet")
        self.copy_button.setToolTip("只换 A/B 主文案的文字，位置和字号不变")
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
        state_row = QHBoxLayout()
        state_row.setSpacing(4)
        self.hide_asset_button = QPushButton("隐藏")
        self.hide_asset_button.setFixedHeight(26)
        self.hide_asset_button.setToolTip("先藏起来，之后从工具栏“已隐藏”恢复")
        self.hide_asset_button.clicked.connect(self._hide_selected_object)
        state_row.addWidget(self.hide_asset_button)
        self.lock_asset_button = QPushButton("锁定")
        self.lock_asset_button.setCheckable(True)
        self.lock_asset_button.setFixedHeight(26)
        self.lock_asset_button.setToolTip("锁定位置：不能拖动，比例同步也不会挪它")
        self.lock_asset_button.clicked.connect(self._toggle_lock)
        state_row.addWidget(self.lock_asset_button)
        state_row.addStretch(1)
        layout.addLayout(state_row)

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
        self._overlay_form = transform_form
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
        self.add_shape_button2.setMenu(self._shape_menu(self.add_shape_button2))
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
