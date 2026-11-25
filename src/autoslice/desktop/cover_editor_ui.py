"""界面构建：工具栏、画布区、右侧面板、帧条，以及提示与导出摘要。

混入 CoverEditorWidget；只用 self 上的状态，不单独实例化。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
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
from .cover_style import STYLE_PRESETS, streamer_key
from .cover_widgets import (
    FitToolbar,
    ToolbarButton,
    _ColorButton,
    _InlineTextEdit,
    _preset_icon,
    _TitleEdit,
)
from .qt_preview import motion, popups
from .qt_preview.appearance import themed
from .qt_preview.icons import icon
from .qt_preview.shell import Card, Popover
from .qt_preview.theme import COLORS, MONO_FAMILIES, STYLE
from .qt_preview.widgets import ElidedLabel


def _vline() -> QFrame:
    """工具栏里的短竖线分隔。"""

    line = QFrame()
    line.setObjectName("vline")
    line.setFixedSize(1, 16)
    return line


def _subtle_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("subtle")
    return label


def _field_label(text: str) -> QLabel:
    """属性卡里每组控件上方的小标题（12px、次要色）。"""

    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


def _value_row(color_button: QPushButton, spin: QWidget, prefix: str = "") -> QWidget:
    """描边一行：输入底色的细框里，左边色块 + 颜色名，右边宽度（设计稿“黑色 … 14 px”）。"""

    row = QFrame()
    row.setObjectName("valueRow")
    row.setFixedHeight(34)
    line = QHBoxLayout(row)
    line.setContentsMargins(4, 0, 4, 0)
    line.setSpacing(6)
    if prefix:
        name = QLabel(prefix)
        name.setObjectName("fieldLabel")
        line.addWidget(name)
    color_button.setObjectName("inRow")
    color_button.setFixedHeight(28)
    line.addWidget(color_button, 1)
    spin.setObjectName("inRow")
    spin.setFixedHeight(28)
    spin.setFixedWidth(78)
    line.addWidget(spin)
    return row


# 填充常用色（设计稿四色）：黄、白、红、青
FILL_SWATCHES = ("#FFE14D", "#FFFFFF", "#FF4F45", "#3FD2E0")


class _Swatch(QPushButton):
    """28px 色块；选中时外面一圈强调色。"""

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self.color = color.upper()
        self.setCheckable(True)
        self.setFixedSize(28, 28)
        self.setToolTip(self.color)
        self.setProperty("motion", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.isChecked():
            painter.setPen(QPen(QColor(COLORS.accent if STYLE.dark else COLORS.accent_line), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, 7, 7)
            rect = rect.adjusted(3, 3, -3, -3)
        painter.setPen(QPen(QColor(COLORS.card_border), 1))
        painter.setBrush(QColor(self.color))
        painter.drawRoundedRect(rect, 5 if self.isChecked() else 7, 5 if self.isChecked() else 7)
        painter.end()


class FrameThumb(ToolbarButton):
    """来源帧：上面 112×63 圆角缩略图（可带“AI 推荐”角标），下面等宽时间；选中时时间变强调色并标“当前”。"""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setProperty("motion", True)
        self.setMinimumWidth(72)
        self.setMaximumWidth(112)
        self.setFixedHeight(82)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._hover = motion.Channel(self, self.update)

    def sizeHint(self) -> QSize:
        return QSize(112, 82)

    def minimumSizeHint(self) -> QSize:
        return QSize(72, 82)

    def event(self, event):
        if event.type() == QEvent.Type.HoverEnter:
            self._hover.to(1.0, motion.HOVER_IN)
        elif event.type() == QEvent.Type.HoverLeave:
            self._hover.to(0.0, motion.HOVER_OUT)
        return super().event(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        width = self.width()
        thumb = QRectF(0.5, 0.5, width - 1, min(63.0, width * 63 / 112) - 1)
        path = QPainterPath()
        path.addRoundedRect(thumb, 7, 7)
        icon = self.icon()
        if icon.isNull():
            painter.fillPath(path, QColor(COLORS.raised))
        else:
            pixmap = icon.pixmap(QSize(112, 63), self.devicePixelRatioF())
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(thumb, pixmap, QRectF(pixmap.rect()))
            painter.restore()
        if self._hover.value > 0.01 and self.isEnabled():
            painter.fillPath(path, motion.faded("#FFFFFF", 0.06 * self._hover.value))
        checked = self.isChecked()
        painter.setPen(QPen(QColor(COLORS.accent if STYLE.dark else COLORS.accent_line), 1.5) if checked
                       else QPen(QColor(0, 0, 0, 60), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        stamp = float(self.property("timestamp") or 0.0)
        whole = int(stamp)
        caption = f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"
        if checked:
            caption += " · 当前"
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setFamilies(list(MONO_FAMILIES))
        painter.setFont(font)
        painter.setPen(QColor(COLORS.accent_ink if checked else COLORS.subtle))
        painter.drawText(QRectF(0, thumb.bottom() + 5, width, 14),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, caption)
        painter.end()


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
        text_only: bool = False,
    ) -> ToolbarButton:
        """工具栏、面板和帧条按钮的统一外观：图标、尺寸、提示、点击和初始可用状态。"""

        button = ToolbarButton(text)
        button.icon_with_text = not text_only
        if icon_name:
            color = COLORS.primary_text if object_name == "primary" else COLORS.muted
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
        """封面页：左列画布卡 + 来源帧卡，右列属性卡 + 另一比例小卡；两张底部卡与上方主卡同列、右端对齐。

        项目级动作（投稿文案、批量出图、保存并做下一个、导出）在 header_bar 里，
        桌面窗口把它整条挪进顶栏；单独使用编辑器时它留在页面顶部。
        """

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._gap_layouts = [root]
        self.header_bar = self._build_header_bar()
        root.addWidget(self.header_bar)
        # 警告/错误提示条；例行进度只进状态栏，不占这里。
        self.notice_label = QLabel("")
        self.notice_label.setObjectName("coverNoticeInfo")
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(False)
        root.addWidget(self.notice_label)
        # 不显示：记录当前项目与视频名，供诊断和测试读取。
        self.project_label = QLabel("")
        self.video_label = QLabel("")
        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        self._gap_layouts.append(columns)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        self._gap_layouts.append(left)
        self.canvas_card = Card()
        canvas_column = QVBoxLayout(self.canvas_card)
        canvas_column.setContentsMargins(0, 0, 0, 0)
        canvas_column.setSpacing(0)
        canvas_column.addWidget(self._build_toolbar())
        canvas_column.addWidget(self._build_canvas_area(), 1)
        left.addWidget(self.canvas_card, 1)
        self.frame_card = Card()
        self.frame_card.setFixedHeight(164)
        frame_column = QVBoxLayout(self.frame_card)
        frame_column.setContentsMargins(0, 0, 0, 0)
        frame_column.addWidget(self._build_frame_strip())
        left.addWidget(self.frame_card)
        columns.addLayout(left, 1)
        self.right_column = QWidget()
        self.right_column.setFixedWidth(300)
        right = QVBoxLayout(self.right_column)
        right.setContentsMargins(0, 0, 0, 0)
        self._gap_layouts.append(right)
        self.props_card = Card()
        props_column = QVBoxLayout(self.props_card)
        props_column.setContentsMargins(0, 0, 0, 0)
        props_column.addWidget(self._build_right_panel())
        right.addWidget(self.props_card, 1)
        self.ratio_card = Card()
        self.ratio_card.setFixedHeight(164)
        ratio_column = QVBoxLayout(self.ratio_card)
        ratio_column.setContentsMargins(0, 0, 0, 0)
        ratio_column.addWidget(self._build_ratio_card())
        right.addWidget(self.ratio_card)
        columns.addWidget(self.right_column)
        root.addLayout(columns, 1)
        themed(self, self._apply_gaps)
        # 面板折叠；无项目时先收起右侧面板，画布占满。
        self._panel_visible = True
        self._last_panel_width = 280
        self.panel_toggle.toggled.connect(self._toggle_panel)
        self._hide_panel()
        self.panel_toggle.setEnabled(False)
        self._update_canvas_hint()

    def _apply_gaps(self):
        for layout in self._gap_layouts:
            layout.setSpacing(STYLE.gap)

    def _build_header_bar(self) -> QWidget:
        """项目级动作：草稿状态 · 投稿文案 · 批量出图 | 保存并做下一个 · 导出 4:3 + 16:9（下拉：只导当前比例、导出记录、打开文件夹）。"""

        bar = QWidget()
        bar.setObjectName("workflowActions")
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.draft_status = QLabel("")
        self.draft_status.setObjectName("subtle")
        row.addWidget(self.draft_status)
        self.submission_button = self._button(
            "投稿文案", icon_name="pen", tip="让 AI 按你以前的标题口吻写 3 个备选标题、简介和标签，一键复制",
            slot=self._submission_copy, enabled=False, object_name="quiet", height=34,
        )
        self.batch_button = self._button(
            "批量出图", icon_name="stack", tip="给投稿目录里的多个视频一次出封面（4:3 + 16:9）",
            slot=self._open_batch, enabled=False, object_name="quiet", height=34,
        )
        row.addWidget(self.submission_button)
        row.addWidget(self.batch_button)
        divider = QFrame()
        divider.setObjectName("vline")
        divider.setFixedSize(1, 18)
        row.addWidget(divider)
        self.next_button = self._button(
            "保存并做下一个",
            tip="保存当前封面，切到投稿列表里的下一个视频", slot=self._request_next_video, enabled=False,
            object_name=None, height=34,
        )
        row.addWidget(self.next_button)
        # 输出摘要放在画布下方的提示行里（见 _build_canvas_area）。
        self.export_summary = ElidedLabel("")
        self.export_summary.setObjectName("subtle")
        # 单比例导出、导出记录收进导出主按钮旁的下拉里
        self.export_button = self._button(
            "导出", tip="导出当前画布比例", slot=self._export, object_name=None, enabled=False,
        )
        self.export_button.hide()
        self.history_button = self._button(
            icon_name="history", tip="导出记录：回看最近导出的封面", slot=self._open_export_history, square=True,
        )
        self.history_button.hide()
        # 投稿要两个比例，双比例导出是主动作；旧网页端同样以“保存双比例”为主按钮。
        self.export_both_button = self._button(
            "导出 4:3 + 16:9", object_name="primary", slot=self._export_both, enabled=False,
            tip="分别导出 4:3（1440×1080）与 16:9（1920×1080），不覆盖已有文件", height=34,
        )
        # 主按钮的文字永远完整显示；右侧下拉箭头与它拼成一个按钮（设计稿“导出 4:3 + 16:9 ⌄”）
        self.export_both_button.setProperty("join", "right")
        self.export_both_button.setMinimumWidth(self.export_both_button.sizeHint().width())
        export_pair = QHBoxLayout()
        export_pair.setSpacing(0)
        export_pair.addWidget(self.export_both_button)
        row.addLayout(export_pair)
        self.export_more_button = self._button(
            icon_name="chevron_down", icon_size=14, tip="只导出当前比例、导出记录、打开导出文件夹", square=True,
            object_name="primary", height=34,
        )
        self.export_more_button.setFixedWidth(28)
        self.export_more_button.setProperty("join", "left")
        self.export_menu = QMenu(self.export_more_button)
        self.export_menu.aboutToShow.connect(self._fill_export_menu)
        popups.MenuFx(self.export_menu)
        self.export_more_button.clicked.connect(
            lambda: popups.MenuFx.popup_below(self.export_menu, self.export_more_button, align_right=True))
        export_pair.addWidget(self.export_more_button)
        return bar

    def _fill_export_menu(self):
        menu = self.export_menu
        menu.clear()
        single = menu.addAction(self.export_button.text() or "导出当前比例", self.export_button.click)
        single.setEnabled(self.export_button.isEnabled())
        menu.addAction("导出记录", self.history_button.click)
        menu.addSeparator()
        folder = menu.addAction("打开导出文件夹", self._open_export_folder)
        folder.setEnabled(not self.open_export_button.isHidden())

    def _build_toolbar(self) -> QWidget:
        """画布卡顶部一行：撤销/重做 · 4:3 / 16:9 · 添加文字、素材、形状、已隐藏 · 最右端“AI 改一改”。"""

        toolbar = FitToolbar()
        toolbar.setObjectName("coverToolbar")
        toolbar.setFixedHeight(54)
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(4)

        self.undo_button = self._button(icon_name="undo", tip="撤销", slot=self._undo, square=True, enabled=False,
                                        height=32)
        self.redo_button = self._button(icon_name="redo", tip="重做", slot=self._redo, square=True, enabled=False,
                                        height=32)
        row.addWidget(self.undo_button)
        row.addWidget(self.redo_button)
        row.addWidget(_vline())

        # 比例：分段控件，和字幕页的“全部 / 待确认”同一套外观
        segmented = QWidget()
        segmented.setObjectName("segmented")
        segment_row = QHBoxLayout(segmented)
        segment_row.setContentsMargins(0, 0, 0, 0)
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
        row.addWidget(_vline())

        # 插入：文字 / 素材 / 形状常驻工具栏；“已隐藏”只在有隐藏对象时出现。
        self.add_text_button = self._button(
            "添加文字", icon_name="text", tip="新建文本框；双击画布上的文字可直接改字", slot=self._add_text,
            enabled=False, text_only=True,
        )
        self.asset_menu_button = self._button(
            "素材", icon_name="image", tip="从素材库选择或导入图片，作为可编辑对象加入画布", enabled=False,
            text_only=True,
        )
        self.asset_menu = QMenu(self.asset_menu_button)
        self.asset_menu.aboutToShow.connect(self._fill_asset_menu)
        self._fill_asset_menu()
        self._set_menu(self.asset_menu_button, self.asset_menu)
        self.shape_menu_button = self._button("形状", icon_name="shapes", tip="添加圆圈、箭头或矩形强调框", enabled=False,
                                              text_only=True)
        self._set_menu(self.shape_menu_button, self._shape_menu(self.shape_menu_button))
        self.hidden_button = self._button("已隐藏", icon_name="eye", tip="被删除或隐藏的文字和素材，点一下恢复",
                                          text_only=True)
        self.hidden_menu = QMenu(self.hidden_button)
        self.hidden_menu.aboutToShow.connect(self._fill_hidden_menu)
        self._set_menu(self.hidden_button, self.hidden_menu)
        self.hidden_button.setVisible(False)
        for button in (self.add_text_button, self.asset_menu_button, self.shape_menu_button, self.hidden_button):
            row.addWidget(button)
        row.addStretch(1)
        # 画布显示比例（显示宽度 / 导出宽度）
        self.zoom_label = QLabel("")
        self.zoom_label.setObjectName("mono")
        self.zoom_label.setToolTip("画布显示比例：导出图按这个比例缩小显示")
        row.addWidget(self.zoom_label)
        row.addSpacing(8)
        self.panel_toggle = self._button(
            icon_name="chevron_right", tip="收起/展开属性面板", square=True, checkable=True, height=32,
        )
        row.addWidget(self.panel_toggle)
        # “AI 改一改”放在画布卡工具栏最右端
        self.ai_critique_button = self._button(
            "AI 改一改", icon_name="sparkles", icon_size=13,
            tip="让 AI 逐个调整文字和素材的位置、大小与描边配色，保留你写的文案和锁定的对象；对照预览后一键应用，可撤销",
            slot=self._ai_critique, enabled=False, object_name="aiFix", height=30,
        )
        row.addWidget(self.ai_critique_button)
        # 放不下时先把插入类按钮收成图标，“AI 改一改”最后收
        toolbar.set_collapsible(
            (self.add_text_button, self.asset_menu_button, self.shape_menu_button, self.hidden_button),
            (self.ai_critique_button,),
        )
        return toolbar

    def _build_ratio_card(self) -> QWidget:
        """右下角“另一比例”小卡：另一比例的实时缩略图 + 同步按钮；两个比例分别取景（D-007）。"""

        card = QWidget()
        column = QVBoxLayout(card)
        column.setContentsMargins(18, 14, 18, 16)
        column.setSpacing(12)
        head = QHBoxLayout()
        self.ratio_title = QLabel("另一比例 · 16:9")
        self.ratio_title.setObjectName("sectionTitle")
        head.addWidget(self.ratio_title)
        head.addStretch(1)
        head.addWidget(_subtle_label("独立取景"))
        column.addLayout(head)
        body = QHBoxLayout()
        body.setSpacing(14)
        self.ratio_preview = CoverCanvas()
        self.ratio_preview.overlays = False
        self.ratio_preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.ratio_preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ratio_preview.setFixedSize(112, 63)
        body.addWidget(self.ratio_preview)
        side = QVBoxLayout()
        side.setSpacing(8)
        self.ratio_note = _subtle_label("文字和素材位置可以一键套过去")
        self.ratio_note.setWordWrap(True)
        side.addWidget(self.ratio_note)
        self.sync_ratio_button = self._button(
            "同步到 16:9",
            tip="把当前比例里调好的文字和素材位置套到另一个比例；底图取景各自保留",
            slot=self._sync_other_ratio, enabled=False, object_name=None,
        )
        side.addWidget(self.sync_ratio_button, 0, Qt.AlignmentFlag.AlignLeft)
        body.addLayout(side, 1)
        column.addLayout(body)
        self._ratio_timer = QTimer(self)
        self._ratio_timer.setSingleShot(True)
        self._ratio_timer.setInterval(120)
        self._ratio_timer.timeout.connect(self._refresh_ratio_preview)
        return card

    def _queue_ratio_preview(self):
        if hasattr(self, "_ratio_timer"):
            self._ratio_timer.start()

    def _refresh_ratio_preview(self):
        """另一比例的缩略图：同一份文档，用另一个比例的取景画；不写盘、不渲染 Pillow（D-025）。"""

        other = "16x9" if self._canvas_key == "4x3" else "4x3"
        self.ratio_title.setText(f"另一比例 · {'16:9' if other == '16x9' else '4:3'}")
        width, height = (112, 63) if other == "16x9" else (84, 63)
        self.ratio_preview.setFixedSize(width, height)
        self.ratio_preview.set_background_pixmap(self.canvas.background_pixmap())
        self.ratio_preview.set_document(self.document, other)

    def _build_canvas_area(self) -> QWidget:
        """中央画布、就地输入框和画布下方的操作提示。"""

        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(0)
        self.canvas_well = QFrame()
        self.canvas_well.setObjectName("canvasWell")
        well = QVBoxLayout(self.canvas_well)
        # 底板四周留一圈呼吸空间（设计稿画面四周有约 40px 空白；画布自己再留 8px）
        well.setContentsMargins(28, 28, 28, 28)
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
            (self.canvas.document_shown, self._queue_ratio_preview),
            (self.canvas.document_shown, self._update_zoom_label),
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
        # 画布自身不铺底，露出底板（夜航深灰、晨雾浅灰）
        palette = self.canvas.palette()
        palette.setColor(QPalette.ColorRole.Window, Qt.GlobalColor.transparent)
        self.canvas.setPalette(palette)
        self.canvas.setAutoFillBackground(False)
        self.canvas.setProperty("onWell", True)
        self.canvas.apply_frame_style()
        well.addWidget(self.canvas)
        layout.addWidget(self.canvas_well, 1)
        self.canvas.installEventFilter(self)
        # 提示行：左边操作提示，右边本次导出的文件名与比例；放不下时各自右侧省略。
        # 行高按“打开文件夹”按钮预留：按钮导出后才出现，不预留的话画布会被挤矮、往下跳一下
        info_holder = QWidget()
        info_row = QHBoxLayout(info_holder)
        info_row.setContentsMargins(8, 0, 8, 0)
        info_row.setSpacing(12)
        self.canvas_hint = ElidedLabel("")
        self.canvas_hint.setObjectName("hintLine")
        info_row.addWidget(self.canvas_hint, 3)
        self.export_summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        info_row.addWidget(self.export_summary, 2)
        # 导出后才出现：直接打开成品所在文件夹
        self.open_export_button = QPushButton("打开文件夹")
        self.open_export_button.setObjectName("tool")
        self.open_export_button.setToolTip("在资源管理器里打开刚导出的封面")
        self.open_export_button.clicked.connect(self._open_export_folder)
        self.open_export_button.hide()
        info_row.addWidget(self.open_export_button)
        self.open_export_button.ensurePolished()
        info_holder.setFixedHeight(max(44, self.open_export_button.sizeHint().height()))
        layout.addWidget(info_holder)
        return center

    def _build_right_panel(self) -> QWidget:
        """右侧属性卡：上面快速方案（方案缩略图 + AI 方案），下面当前对象（文字 / 素材 / 底图）的属性。"""

        self.right_panel = QWidget()
        self.right_panel.setObjectName("coverPanel")
        self.right_panel.setMinimumWidth(0)
        outer = QVBoxLayout(self.right_panel)
        # 右、上、下留几像素，滚动条落在卡片圆角以内，不贴着卡片边
        outer.setContentsMargins(0, 8, 4, 8)
        # 窗口不够高时整块滚动，不再把下拉框、数字框压扁叠在一起
        self.right_scroll = QScrollArea()
        self.right_scroll.setWidgetResizable(True)
        self.right_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.right_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; } QScrollArea > QWidget > QWidget#coverPanelInner { background: transparent; }")
        inner = _NoSqueezeWidget()
        inner.setObjectName("coverPanelInner")
        inner.setStyleSheet("QWidget#coverPanelInner { background: transparent; }")
        self.right_scroll.setWidget(inner)
        self.right_scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self.right_scroll)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(18, 10, 14, 10)
        layout.setSpacing(0)
        layout.addLayout(self._build_scheme_section())
        layout.addSpacing(30)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.panel_title = QLabel("封面文案")
        self.panel_title.setObjectName("sectionTitle")
        head.addWidget(self.panel_title)
        head.addStretch(1)
        layout.addLayout(head)
        layout.addSpacing(10)
        self.panel_stack = QStackedWidget()
        layout.addWidget(self.panel_stack, 1)
        self.copy_controls = self._build_text_panel()
        self.asset_controls = self._build_asset_panel()
        self.bg_controls = self._build_bg_panel()
        for panel in (self.copy_controls, self.asset_controls, self.bg_controls):
            self.panel_stack.addWidget(panel)
        # 换了属性页（文字/素材/底图）就回到顶部，免得停在上一页滚到的位置、看不到开头的按钮
        self.panel_stack.currentChanged.connect(lambda _index: self.right_scroll.verticalScrollBar().setValue(0))
        return self.right_panel

    def _build_frame_strip(self) -> QWidget:
        """来源帧卡：标题 + 锁帧 / 从指定时间取帧 / 从全片挑 7 张，下面 7 张附近帧（缩略图 + 时间）。

        拖动选帧、读取字幕页当前帧、扩大范围收进“从指定时间取帧”弹层。
        """

        strip = QWidget()
        strip.setObjectName("frameStrip")
        column = QVBoxLayout(strip)
        column.setContentsMargins(14, 8, 14, 14)
        column.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.setContentsMargins(4, 3, 0, 3)
        title = QLabel("来源帧")
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        head.addSpacing(8)
        self.frame_range_label = QLabel("播放头附近 ±3 秒")
        self.frame_range_label.setObjectName("hintLine")
        head.addWidget(self.frame_range_label)
        head.addStretch(1)
        self._frame_head = head
        column.addLayout(head)

        # 弹层：拖动选帧 + 当前帧 + 扩大范围
        picker = QWidget()
        picker_column = QVBoxLayout(picker)
        picker_column.setContentsMargins(4, 2, 4, 2)
        picker_column.setSpacing(10)
        scrub_row = QHBoxLayout()
        scrub_row.setSpacing(8)
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setRange(0, 0)
        self.frame_slider.setEnabled(False)
        self.frame_slider.setMinimumWidth(220)
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
        self.frame_center_label = QLabel("0.00s")
        self.frame_center_label.setObjectName("mono")
        self.frame_center_label.setMinimumWidth(92)
        scrub_row.addWidget(self.frame_center_label)
        scrub_row.addWidget(self.frame_slider, 1)
        self.duration_label = QLabel("")
        self.duration_label.setObjectName("subtle")
        scrub_row.addWidget(self.duration_label)
        picker_column.addLayout(scrub_row)
        picker_actions = QHBoxLayout()
        picker_actions.setSpacing(6)
        self.current_frame_button = self._button(
            "用字幕页当前帧", tip="读取字幕页播放位置", slot=self._use_current_frame, height=28, object_name=None,
        )
        self.more_frames_button = self._button(
            "扩大取帧范围", tip="扩大取帧范围", slot=self._find_more_frames, height=28, object_name=None,
        )
        picker_actions.addWidget(self.current_frame_button)
        picker_actions.addWidget(self.more_frames_button)
        picker_actions.addStretch(1)
        picker_column.addLayout(picker_actions)
        self.frame_picker = Popover(self, picker, 380)

        self.frame_lock_button = self._button(
            "锁帧", icon_name="lock", icon_size=14, tip="锁定后换方案、换文案都不换帧", slot=self._toggle_frame_lock,
            height=26, checkable=True, object_name="tool",
        )
        self.frame_lock_button.setProperty("plain", True)
        self.pick_time_button = self._button(
            "从指定时间取帧", tip="拖动选帧、读取字幕页当前帧、扩大取帧范围", height=26, object_name="tool",
        )
        self.pick_time_button.clicked.connect(
            lambda: self.frame_picker.dismiss() if self.frame_picker.isVisible()
            else self.frame_picker.popup(self.pick_time_button, align_right=True, above=True))
        self.overview_button = self._button(
            "从全片挑 7 张", tip="从整个视频按画质挑 7 张", slot=self._find_overview_frames, height=26, object_name=None,
        )
        for button in (self.frame_lock_button, self.pick_time_button, self.overview_button):
            head.addWidget(button)

        row = QHBoxLayout()
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(10)
        column.addLayout(row)
        self.nearby_frame_buttons = []
        for _ in range(7):
            button = FrameThumb("--")
            button.setObjectName("frameThumb")
            # 只画缩略图和下方时间；text() 仍是完整时间，供读取与提示
            button.set_compact(True, force=True)
            button.setCheckable(True)
            button.setIconSize(QSize(112, 63))
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
        row.addStretch(0)
        return strip

    def _build_scheme_section(self) -> QVBoxLayout:
        """快速方案：标题 + 模板 / 换一批，三张方案缩略图三选一；下面 AI 方案 + “允许 AI 尝试更好的主文案”。"""

        section = QVBoxLayout()
        section.setSpacing(12)
        scheme_head = QHBoxLayout()
        scheme_head.setSpacing(4)
        self.scheme_title = QLabel("快速方案")
        self.scheme_title.setObjectName("sectionTitle")
        scheme_head.addWidget(self.scheme_title)
        scheme_head.addStretch(1)
        self.template_button = self._button(
            "模板", tip="把排好的封面存为模板，换个视频一键套上（字的位置样式、贴纸都跟着）；批量出图也能选",
            height=24, text_only=True,
        )
        self.template_menu = QMenu(self.template_button)
        self.template_menu.aboutToShow.connect(self._fill_template_menu)
        self._set_menu(self.template_button, self.template_menu)
        scheme_head.addWidget(self.template_button)
        self.scheme_refresh_button = self._button(
            "换一批", icon_name="refresh", icon_size=13, tip="换一批：轮换文案和配色（本地生成，不调用 AI）",
            slot=self._next_scheme_batch, enabled=False, height=24,
        )
        scheme_head.addWidget(self.scheme_refresh_button)
        section.addLayout(scheme_head)
        scheme_row = QHBoxLayout()
        scheme_row.setSpacing(6)
        self.scheme_buttons: list[QPushButton] = []
        self.scheme_labels: list[QLabel] = []
        for index in range(3):
            column = QVBoxLayout()
            column.setSpacing(3)
            button = QPushButton("")
            button.setObjectName("schemeThumb")
            button.setCheckable(True)
            button.setFixedHeight(52)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setIconSize(QSize(80, 46))
            button.setEnabled(False)
            button.clicked.connect(lambda _checked=False, value=index: self._apply_scheme(value))
            caption = QLabel("—")
            caption.setObjectName("hintLine")
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column.addWidget(button)
            column.addWidget(caption)
            scheme_row.addLayout(column, 1)
            self.scheme_buttons.append(button)
            self.scheme_labels.append(caption)
        section.addLayout(scheme_row)
        # AI 方案生成中：三块骨架屏扫光占位，结果出来后收起
        self.ai_skeletons = QWidget()
        skeleton_row = QHBoxLayout(self.ai_skeletons)
        skeleton_row.setContentsMargins(0, 2, 0, 2)
        skeleton_row.setSpacing(6)
        for order in range(3):
            block = motion.Skeleton(delay=order * 120)
            block.setFixedHeight(52)
            skeleton_row.addWidget(block)
        self.ai_skeletons.hide()
        section.addWidget(self.ai_skeletons)
        ai_row = QHBoxLayout()
        ai_row.setSpacing(10)
        self.ai_scheme_button = self._button(
            "AI 方案", icon_name="sparkles", icon_size=13,
            tip="保留当前文案和自建文字，避让已有对象优化排版；勾选右侧选项才尝试更换主文案",
            slot=self._ai_schemes, enabled=False, object_name=None, height=30,
        )
        ai_row.addWidget(self.ai_scheme_button)
        self.ai_rewrite_check = QCheckBox("允许 AI 尝试更好的主文案")
        self.ai_rewrite_check.setToolTip(
            "默认保留当前文字；开启后，AI 方案和“AI 改一改”才可能给新文案。AI 方案的新文案须与当前文字及标题比较通过，自建文字块仍保留")
        ai_row.addWidget(self.ai_rewrite_check, 1)
        section.addLayout(ai_row)
        self.ai_hint = QLabel("")
        self.ai_hint.setObjectName("hintLine")
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
        self.right_column.hide()
        self.panel_toggle.setIcon(icon("chevron_left"))
        self._panel_visible = False

    def _show_panel(self):
        """展开右侧面板。"""
        self.right_column.show()
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
        """文字对象属性（设计稿顺序）：身份 + 换一版文案 / 锁定 / 隐藏 / 删除、文案、强调词、字体、
        字号与行距、填充、配色、描边与外描边、底条与强调色、对齐、阴影。"""

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        role_row = QHBoxLayout()
        role_row.setSpacing(2)
        self.copy_role_label = QLabel("")
        self.copy_role_label.setObjectName("hintLine")
        role_row.addWidget(self.copy_role_label, 1)
        self.copy_button = self._button(
            "换一版文案", icon_name="sparkles", icon_size=13, tip="只换 A/B 主文案的文字，位置和字号不变",
            slot=self._cycle_copy,
        )
        role_row.addWidget(self.copy_button)
        self.lock_text_button = self._button(
            icon_name="lock", icon_size=14, tip="锁定位置：不能拖动，快速方案和比例同步也不会挪它",
            slot=self._toggle_lock, square=True, checkable=True,
        )
        role_row.addWidget(self.lock_text_button)
        self.copy_role_label.setMinimumWidth(0)
        self.copy_role_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hide_text_button = self._button(
            icon_name="eye", icon_size=14, tip="隐藏这段文字（可在工具栏“已隐藏”里恢复）",
            slot=self._hide_selected_object, square=True,
        )
        role_row.addWidget(self.hide_text_button)
        self.delete_text_button = self._button(
            icon_name="trash", icon_size=14, tip="删除这段文字（可在工具栏“已隐藏”里恢复）",
            slot=self._delete_selected_object, square=True,
        )
        role_row.addWidget(self.delete_text_button)
        layout.addLayout(role_row)

        copy_box = QVBoxLayout()
        copy_box.setSpacing(6)
        copy_box.addWidget(_field_label("文案"))
        self.title_edit = _TitleEdit()
        self.title_edit.setPlaceholderText("封面文案…")
        self.title_edit.setFixedHeight(60)
        self.title_edit.textChanged.connect(self._draft_changed)
        copy_box.addWidget(self.title_edit)
        # 强调词：只换颜色，字号和断行不变；参考切片封面里“开盒”单独标黄的做法。
        self.emphasis_edit = QLineEdit()
        self.emphasis_edit.setPlaceholderText("强调词，空格分隔")
        self.emphasis_edit.setToolTip("这些词换成强调色，比如「开盒」「14个」；强调色在下面“强调色”里改")
        self.emphasis_edit.textChanged.connect(self._draft_changed)
        copy_box.addWidget(self.emphasis_edit)
        emphasis_actions = QHBoxLayout()
        emphasis_actions.setSpacing(4)
        self.emphasize_button = self._button(
            "强调所选", icon_name="highlight", icon_size=13,
            tip="在上面的文案框里选中几个字，点这里把它们设为强调（再点一次取消）", slot=self._emphasize_selection,
        )
        self.burst_button = self._button(
            "垫爆炸贴", icon_name="burst", icon_size=13,
            tip="在这段字的强调词后面垫一个漫画爆炸贴；没有强调词就垫在整段字后面", slot=self._add_burst_behind_text,
        )
        emphasis_actions.addWidget(self.emphasize_button)
        emphasis_actions.addWidget(self.burst_button)
        emphasis_actions.addStretch(1)
        copy_box.addLayout(emphasis_actions)
        layout.addLayout(copy_box)

        font_box = QVBoxLayout()
        font_box.setSpacing(6)
        font_box.addWidget(_field_label("字体"))
        self.font_path_edit = QLineEdit()
        self.font_path_edit.setReadOnly(True)
        self.font_path_edit.hide()
        self.font_button = QPushButton("默认字体")
        self.font_button.setObjectName("fieldButton")
        self.font_button.setFixedHeight(34)
        self.font_button.setToolTip("选择字体文件")
        self.font_button.clicked.connect(self._pick_font)
        self.font_path_edit.textChanged.connect(
            lambda text: self.font_button.setText(Path(text).stem if text else "默认字体"))
        font_box.addWidget(self.font_button)
        layout.addLayout(font_box)

        numbers = QGridLayout()
        numbers.setHorizontalSpacing(10)
        numbers.setVerticalSpacing(6)
        numbers.addWidget(_field_label("字号"), 0, 0)
        numbers.addWidget(_field_label("行距"), 0, 1)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(24, 320)
        self.font_spin.setSuffix(" px")
        self.font_spin.setFixedHeight(34)
        self.font_spin.valueChanged.connect(self._draft_changed)
        numbers.addWidget(self.font_spin, 1, 0)
        self.line_spacing_spin = QDoubleSpinBox()
        self.line_spacing_spin.setRange(0.5, 3.0)
        self.line_spacing_spin.setSingleStep(0.05)
        self.line_spacing_spin.setDecimals(2)
        self.line_spacing_spin.setFixedHeight(34)
        self.line_spacing_spin.valueChanged.connect(self._draft_changed)
        numbers.addWidget(self.line_spacing_spin, 1, 1)
        layout.addLayout(numbers)

        fill_box = QVBoxLayout()
        fill_box.setSpacing(6)
        fill_box.addWidget(_field_label("填充"))
        fill_row = QHBoxLayout()
        fill_row.setSpacing(8)
        self.fill_color_button = _ColorButton(storage=self.service.storage)
        self.fill_color_button.color_changed.connect(self._draft_changed)
        self.fill_swatches: list[QPushButton] = []
        for value in FILL_SWATCHES:
            swatch = _Swatch(value)
            swatch.clicked.connect(lambda _checked=False, color=value: self._choose_fill(color))
            self.fill_swatches.append(swatch)
            fill_row.addWidget(swatch)
        self.fill_color_button.setObjectName("swatchMore")
        self.fill_color_button.setFixedHeight(28)
        self.fill_color_button.setToolTip("更多颜色")
        fill_row.addWidget(self.fill_color_button, 1)
        self.fill_color_button.color_changed.connect(self._sync_fill_swatches)
        fill_box.addLayout(fill_row)
        # 配色预设：同时改填充、描边，常用的一步
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
        fill_box.addLayout(preset_row)
        layout.addLayout(fill_box)

        stroke_box = QVBoxLayout()
        stroke_box.setSpacing(6)
        stroke_box.addWidget(_field_label("描边"))
        self.stroke_button = _ColorButton(storage=self.service.storage)
        self.stroke_button.color_changed.connect(self._draft_changed)
        self.stroke_spin = QSpinBox()
        self.stroke_spin.setRange(0, 64)
        self.stroke_spin.setSuffix(" px")
        self.stroke_spin.setToolTip("描边宽度")
        self.stroke_spin.valueChanged.connect(self._draft_changed)
        stroke_box.addWidget(_value_row(self.stroke_button, self.stroke_spin))
        self.outer_stroke_button = _ColorButton(allow_none=True, storage=self.service.storage)
        self.outer_stroke_button.setToolTip("在描边外再加一层，常用白色外边")
        self.outer_stroke_button.color_changed.connect(self._draft_changed)
        self.outer_stroke_spin = QSpinBox()
        self.outer_stroke_spin.setRange(0, 48)
        self.outer_stroke_spin.setSuffix(" px")
        self.outer_stroke_spin.setToolTip("外描边宽度")
        self.outer_stroke_spin.valueChanged.connect(self._draft_changed)
        stroke_box.addWidget(_value_row(self.outer_stroke_button, self.outer_stroke_spin, "外描边"))
        layout.addLayout(stroke_box)

        extra = QGridLayout()
        extra.setHorizontalSpacing(10)
        extra.setVerticalSpacing(6)
        self.backdrop_button = _ColorButton(allow_none=True, allow_alpha=True, storage=self.service.storage)
        self.backdrop_button.setToolTip("文字底条，画面杂乱时提高可读性；可设透明度")
        self.backdrop_button.color_changed.connect(self._draft_changed)
        self.accent_button = _ColorButton(allow_none=True, none_label="自动", storage=self.service.storage)
        self.accent_button.setToolTip("强调词的颜色；“自动”按当前配色挑对比色")
        self.accent_button.color_changed.connect(self._draft_changed)
        extra.addWidget(_field_label("底条"), 0, 0)
        extra.addWidget(_field_label("强调色"), 0, 1)
        extra.addWidget(self.backdrop_button, 1, 0)
        extra.addWidget(self.accent_button, 1, 1)
        layout.addLayout(extra)

        align_line = QHBoxLayout()
        align_line.setSpacing(12)
        align_line.addWidget(_field_label("对齐"), 1)
        self.align_buttons: dict[str, QPushButton] = {}
        self.align_group = QButtonGroup(self)
        self.align_group.setExclusive(True)
        align_segmented = QWidget()
        align_segmented.setObjectName("segmented")
        align_row = QHBoxLayout(align_segmented)
        align_row.setContentsMargins(0, 0, 0, 0)
        align_row.setSpacing(2)
        for key, text in (("left", "左"), ("center", "中"), ("right", "右")):
            btn = self._button(text, object_name="segment", checkable=True, height=26)
            btn.setFixedWidth(34)
            self.align_group.addButton(btn)
            self.align_buttons[key] = btn
            align_row.addWidget(btn)
            btn.clicked.connect(lambda _c=False, k=key: self._set_text_align(k))
        self.align_buttons["left"].setChecked(True)
        motion.SegmentedFx(align_segmented, tuple(self.align_buttons.values()))
        align_line.addWidget(align_segmented)
        layout.addLayout(align_line)

        shadow_line = QHBoxLayout()
        shadow_line.setSpacing(12)
        shadow_line.addWidget(_field_label("阴影"), 1)
        self.shadow_check = QCheckBox("")
        self.shadow_check.setObjectName("switch")
        self.shadow_check.setToolTip("文字阴影")
        self.shadow_check.stateChanged.connect(self._draft_changed)
        shadow_line.addWidget(self.shadow_check)
        layout.addLayout(shadow_line)

        # 旧的“字体与效果”折叠已全部展开；保留对象给旧调用
        self.style_toggle = QPushButton("字体与效果")
        self.style_toggle.setCheckable(True)
        self.style_toggle.setParent(panel)
        self.style_toggle.hide()
        self.style_widget = QWidget(panel)
        self.style_widget.hide()
        layout.addStretch(1)
        return panel

    def _choose_fill(self, color: str) -> None:
        """填充色块：一点就换，和“更多颜色”里选一样记一条历史。"""

        if self.fill_color_button.color() != color.upper():
            self.fill_color_button.set_color(color)
            self.fill_color_button.color_changed.emit()

    def _sync_fill_swatches(self) -> None:
        current = self.fill_color_button.color()
        for swatch in self.fill_swatches:
            swatch.setChecked(swatch.color == current)

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
        self.shape_stroke_button = _ColorButton(storage=self.service.storage)
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
        self.shape_fill_button = _ColorButton(allow_none=True, allow_alpha=True, storage=self.service.storage)
        self.shape_fill_button.setToolTip("填充；可设透明度，选“无”只留线条")
        self.shape_fill_button.color_changed.connect(self._store_overlay_style)
        transform_form.addRow("填充", self.shape_fill_button)
        self.picture_border_button = _ColorButton(allow_none=True, storage=self.service.storage)
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
        self.crop_sides_button = self._button(
            "裁掉两侧", icon_name="expand", slot=self._crop_sides,
            tip="放大一点、略偏上，裁掉两侧弹幕栏和顶部横幅。导出后会按主播记住这次的裁切，下个视频自动沿用",
        )
        layout.addWidget(self.crop_sides_button)

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

    def eventFilter(self, obj, event):
        if obj is getattr(self, "canvas", None) and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self, self._update_zoom_label)
        return super().eventFilter(obj, event)

    def _update_zoom_label(self):
        """工具栏右侧的显示比例：画布上显示的宽度 / 导出宽度。"""

        if not hasattr(self, "zoom_label"):
            return
        width = self.canvas._export_size()[0]
        shown = self.canvas._canvas_rect().width()
        self.zoom_label.setText(f"{shown / max(1, width) * 100:.0f}%" if width and shown > 0 else "")

    def _canvas_label(self) -> str:
        return "4:3" if self._canvas_key == "4x3" else "16:9"

    def _update_canvas_hint(self):
        if self.project is None or self.video is None:
            self.canvas_hint.setText("先到“字幕”页选择投稿项目和视频")
            return
        hint = f"{self._canvas_label()} 主画布 · 拖动即移动 · 按住 Alt 不吸附 · 双击改字 · 滚轮缩放底图"
        if self._suggest_crop():
            hint = "画面带着直播界面？选中底图点“裁掉两侧”，导出后这个主播的下个视频自动沿用 · " + hint
        self.canvas_hint.setText(hint)
        self.canvas_hint.setToolTip(hint)

    def _suggest_crop(self) -> bool:
        """这个主播还没记过裁切、底图也没放大过：多半是第一次做，提醒一句“裁掉两侧”在哪。"""

        if self.document is None or self.project is None or self.service.user_framed(self.document):
            return False
        streamer = streamer_key(self.project.title)
        return bool(streamer) and not self.service.framing_memory.load(streamer)

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
        self.export_summary.setText(f"输出 {filename} · {self._canvas_label()} · {size} · 项目目录")
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


class _NoSqueezeWidget(QWidget):
    """放进滚动区的内容：最小高度就是完整高度，空间不够时滚动而不是压扁。"""

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.sizeHint().height())
