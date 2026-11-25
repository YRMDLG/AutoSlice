"""可交互的 Desktop-04.55 Qt 壳，不读取投稿或字幕文件。"""

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import fonts, motion
from .appearance import themed
from .icons import icon as desktop_icon
from .shell import Card, NavItem, Popover, ProjectSwitch, Surface, TaskRing, Toast
from .theme import COLORS, MONO_FAMILIES, SIZES, STYLE

PROJECTS = (
    ("牛乳糖凌晨突击直播：从闲聊到离谱挑战的完整录播", "18 条待确认", True),
    ("泽音今天又开了一个超长的临时直播，标题真的很长很长", "字幕已保存", False),
    ("半夜直播的那些意外名场面与即兴整活", "AI 检查中 · 62%", False),
    ("周末联动特别篇：大家一起挑战新地图", "封面草稿", False),
)


def label(text: str, object_name: str = "") -> QLabel:
    result = QLabel(text)
    if object_name:
        result.setObjectName(object_name)
    return result


def line() -> QFrame:
    result = QFrame()
    result.setObjectName("hairline")
    return result


def card(title: str, description: str = "", parent: QWidget | None = None) -> tuple[QWidget, QVBoxLayout]:
    """设置页的一块：标题 + 说明 + 内容；返回卡片和它的内容列。"""

    result = QWidget(parent)
    result.setObjectName("settingsCard")
    result.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    column = QVBoxLayout(result)
    column.setContentsMargins(20, 16, 20, 18)
    column.setSpacing(10)
    column.addWidget(label(title, "cardTitle"))
    if description:
        text = label(description, "muted")
        text.setWordWrap(True)
        column.addWidget(text)
    return result, column


def key_hint(button: QPushButton, keys: str) -> QLabel:
    """按钮右侧的等宽快捷键提示（如“保存字幕  Ctrl S”）：文字靠左，提示贴右，不挡点击。"""

    hint = QLabel(keys, button)
    hint.setObjectName("keyHint")
    hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    button.setProperty("keyHint", True)

    class _Dock(QObject):
        def eventFilter(self, obj, event):
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Polish):
                hint.adjustSize()
                hint.move(obj.width() - hint.width() - 14, (obj.height() - hint.height()) // 2)
            return False

    button.installEventFilter(_Dock(button))
    return hint


def tool_icon(button: QPushButton, name: str, checkable: bool = False) -> None:
    """工具按钮统一 16px 图标；可勾选按钮开启时图标随文字变蓝。"""

    size = SIZES.tool_icon_size
    button.setIcon(desktop_icon(name, COLORS.muted, size, COLORS.accent_text if checkable else None))
    button.setIconSize(QSize(size, size))


class TwoLineTitle(QWidget):
    """按可用宽度拆为两行，末行省略，避免项目列表横向滚动。"""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = text
        self.setFixedHeight(39)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 单行标题只占一行高，交给外层居中，不留空洞
        single = QFontMetrics(self.font()).horizontalAdvance(self.title) <= max(1, self.width() - 2)
        self.setFixedHeight(19 if single else 39)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setPen(QColor(COLORS.text))
        metrics = QFontMetrics(self.font())
        width = max(1, self.width() - 2)
        split = 0
        while (
            split < len(self.title) and metrics.horizontalAdvance(self.title[: split + 1]) <= width
        ):
            split += 1
        first = self.title[:split]
        second = metrics.elidedText(self.title[split:], Qt.TextElideMode.ElideRight, width)
        painter.drawText(
            0, 0, width, 19, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, first
        )
        if second:
            painter.drawText(
                0,
                19,
                width,
                19,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                second,
            )


class ProjectItem(QPushButton):
    def __init__(self, title: str, status: str, emphasized: bool, parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self.setObjectName("projectItem" if status else "projectItemCompact")
        self.setCheckable(True)
        self.setFixedHeight(SIZES.row_height if status else SIZES.project_compact_height)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        content = QVBoxLayout(self)
        content.setContentsMargins(12, 6, 10, 6)
        content.setSpacing(4)
        content.addStretch()
        content.addWidget(TwoLineTitle(title))
        if status:
            status_label = label(status, "badge" if emphasized else "subtle")
            status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            content.addWidget(status_label, alignment=Qt.AlignmentFlag.AlignLeft)
        content.addStretch()


class ProjectOption(QPushButton):
    """项目切换下拉里的一项（设计稿）：两位序号、标题 + 状态两行、当前项打勾、标题换强调色。"""

    HEIGHT = 48

    def __init__(self, number: int, title: str, status: str, parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self.number = number
        self.status = status
        self.setObjectName("projectOption")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self.HEIGHT)
        self.setMinimumWidth(0)
        self.setToolTip(title)
        self.setAccessibleName(f"{title}，{status}" if status else title)
        # 自己画悬停和选中，不挂通用按钮动效（那会多画一圈选中描边）
        self.setProperty("motion", True)
        self._hover = motion.Channel(self, self.update, 0.0)

    def enterEvent(self, event) -> None:
        self._hover.to(1.0, motion.HOVER_IN)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover.to(0.0, motion.HOVER_OUT)
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        from autoslice.desktop.qt_preview.popups import paint_check, paint_row

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        selected = self.isChecked()
        paint_row(painter, rect, max(self._hover.value, 0.55 if selected else 0.0))
        number = QFont(self.font())
        number.setFamilies(list(MONO_FAMILIES))
        number.setPixelSize(11)
        painter.setFont(number)
        painter.setPen(QColor(COLORS.subtle))
        painter.drawText(QRectF(10, 0, 22, rect.height()), Qt.AlignmentFlag.AlignVCenter, f"{self.number:02d}")
        right = rect.width() - (34 if selected else 12)
        title_font = QFont(self.font())
        title_font.setPixelSize(13)
        painter.setFont(title_font)
        painter.setPen(QColor(COLORS.accent_ink if selected else COLORS.text))
        metrics = QFontMetrics(title_font)
        text_left = 42
        width = max(10, int(right - text_left))
        painter.drawText(QRectF(text_left, 7, width, 19), Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(self.title, Qt.TextElideMode.ElideRight, width))
        status_font = QFont(self.font())
        status_font.setPixelSize(11)
        painter.setFont(status_font)
        painter.setPen(QColor(COLORS.subtle))
        painter.drawText(QRectF(text_left, 26, width, 15), Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(status_font).elidedText(self.status, Qt.TextElideMode.ElideRight, width))
        if selected:
            paint_check(painter, QPointF(rect.width() - 20, rect.center().y()), COLORS.accent_ink)


class NavigationButton(QPushButton):
    def __init__(self, glyph: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(56, 60)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 4, 0, 4)
        column.setSpacing(0)
        icon_name = {"字幕": "subtitles", "封面": "cover", "设置": "settings"}[title]
        icon = label("")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(desktop_icon(icon_name).pixmap(SIZES.icon_size, SIZES.icon_size))
        name = label(title)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setStyleSheet("font-size: 11px;")
        column.addWidget(icon)
        column.addWidget(name)
        self.toggled.connect(lambda selected: self._set_icon_colors(icon, name, icon_name, selected))
        themed(self, lambda: self._set_icon_colors(icon, name, icon_name, self.isChecked()))

    @staticmethod
    def _set_icon_colors(icon: QLabel, name: QLabel, icon_name: str, selected: bool) -> None:
        # 夜航选中项用强调色，晨雾用正文色（底板由 ButtonFx 画）
        color = (COLORS.accent_ink if STYLE.dark else COLORS.text) if selected else COLORS.subtle
        icon.setPixmap(desktop_icon(icon_name, color).pixmap(SIZES.icon_size, SIZES.icon_size))
        name.setStyleSheet(f"font-size: {SIZES.text_small}px; color: {color};")


class TimelineTrack(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(48)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        if width < 20:
            return
        painter.setPen(QPen(QColor(COLORS.divider), 1))
        painter.drawLine(0, 16, width, 16)
        for fraction, stamp in ((0.05, "17:58"), (0.38, "18:03"), (0.71, "18:08")):
            x = int(width * fraction)
            painter.setPen(QColor(COLORS.subtle))
            painter.drawText(x, 0, 65, 15, Qt.AlignmentFlag.AlignLeft, stamp)
            painter.setPen(QPen(QColor(COLORS.divider), 1))
            painter.drawLine(x, 16, x, 23)
        blocks = ((0.08, 0.23, False), (0.34, 0.20, False), (0.58, 0.24, True), (0.85, 0.12, False))
        for start, span, active in blocks:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS.accent_tint if active else COLORS.raised))
            painter.drawRoundedRect(QRectF(width * start, 28, width * span, 24), 5, 5)
            if active:
                painter.setPen(QPen(QColor(COLORS.accent), 1))
                painter.drawRoundedRect(QRectF(width * start, 28, width * span, 24), 5, 5)
        marker_x = int(width * 0.69)
        painter.setPen(QPen(QColor(COLORS.accent), 2))
        painter.drawLine(marker_x, 16, marker_x, 56)


class _ColumnsFit(QObject):
    """字幕页两列：用户没拖过分界时，宽度变化后按设计比例重排（等 QSplitter 自己分配完再改）。"""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize and not getattr(self._window, "_columns_dragged", True):
            QTimer.singleShot(0, self._window, self._window._fit_columns)
        return False


class PreviewWindow(QMainWindow):
    """窗口骨架：左侧 64px 导航卡片 + 右侧顶栏与页面；页面内容都放在悬浮卡片里。

    两套主题共用这一套布局，换主题只换颜色、圆角、间距和动效。
    """

    def __init__(self) -> None:
        fonts.load()
        super().__init__()
        self.setWindowTitle("AutoSlice · Qt 视觉预览")
        self.resize(1560, 920)
        self.setMinimumSize(960, 640)
        self._ai_open = False
        self._splitter_initialized = False
        self._gap_layouts: list = []
        self._gap_splitters: list = []
        self._build()
        motion.install_tree(self)

    def _build(self) -> None:
        self.surface = Surface()
        outer = QHBoxLayout(self.surface)
        self._gap_layouts.append((outer, True))
        self.nav_card = self._navigation()
        outer.addWidget(self.nav_card)
        main = QWidget()
        column = QVBoxLayout(main)
        column.setContentsMargins(0, 0, 0, 0)
        self._gap_layouts.append((column, False))
        self.appbar = self._appbar()
        column.addWidget(self.appbar)
        self.project_rail = self._project_rail()
        # 高度跟项目数走（列表区在填充项目时定高），不设最小高度，免得下面空一大块
        self.project_popover = Popover(self, self.project_rail, 420)
        self.project_popover.closed.connect(self._project_popover_closed)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._subtitle_page())
        self.pages.addWidget(self._cover_page())
        self.pages.addWidget(self._settings_page())
        column.addWidget(self.pages, 1)
        outer.addWidget(main, 1)
        self.setCentralWidget(self.surface)
        # 提示条挂在窗口底上，居中显示在顶栏一带，不挡时间轴
        self.toast = Toast(self.surface)
        themed(self.surface, self._apply_spacing)
        self._select_page(0)
        self._set_ai_open(False)

    def _apply_spacing(self) -> None:
        """间距随主题：夜航 10px、晨雾 12px；卡片之间的分隔条也用同一宽度。"""

        gap = STYLE.gap
        for layout, margins in self._gap_layouts:
            layout.setSpacing(gap)
            if margins:
                layout.setContentsMargins(gap, gap, gap, gap)
        for splitter in self._gap_splitters:
            splitter.setHandleWidth(gap)

    def _card(self, content: QWidget, margins=(0, 0, 0, 0)) -> Card:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(*margins)
        layout.setSpacing(0)
        layout.addWidget(content)
        return card

    def _gap_splitter(self, orientation) -> QSplitter:
        splitter = QSplitter(orientation)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(STYLE.gap)
        self._gap_splitters.append(splitter)
        return splitter

    def _appbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("appbar")
        bar.setFixedHeight(SIZES.appbar_height)
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 0, 0, 0)
        row.setSpacing(SIZES.space_3)
        self.top_project = label("牛乳糖凌晨突击直播", "projectTitle")
        self.top_meta = label("预览模式 · 无后台任务", "videoFile")
        self.project_switch = ProjectSwitch(self.top_project, self.top_meta)
        self.project_switch.clicked.connect(self._toggle_project_popover)
        row.addWidget(self.project_switch)
        row.addStretch()
        return bar

    def _toggle_project_popover(self) -> None:
        if self.project_popover.isVisible():
            self.project_popover.dismiss()
            return
        self.project_switch.set_open(True)
        motion.install_tree(self.project_rail)
        self.project_popover.popup(self.project_switch)

    def _project_popover_closed(self) -> None:
        self.project_switch.set_open(False)

    def _navigation(self) -> QWidget:
        rail = Card(spotlight=True)
        rail.setObjectName("card")
        rail.setFixedWidth(64)
        column = QVBoxLayout(rail)
        column.setContentsMargins(9, 16, 9, 12)
        column.setSpacing(4)
        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        themed(logo, lambda: logo.setPixmap(desktop_icon("logo", COLORS.accent_ink, 20).pixmap(20, 20)))
        column.addWidget(logo)
        column.addSpacing(20)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.nav_buttons = []
        for index, name in enumerate(("字幕", "封面", "设置")):
            button = NavItem(name)
            group.addButton(button, index)
            button.clicked.connect(lambda _checked=False, page=index: self._select_page(page))
            self.nav_buttons.append(button)
            column.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(1)
        self.task_ring = TaskRing()
        column.addWidget(self.task_ring, 0, Qt.AlignmentFlag.AlignHCenter)
        return rail

    def _project_rail(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("projectRail")
        column = QVBoxLayout(rail)
        column.setContentsMargins(12, 20, 12, 12)
        column.setSpacing(0)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(8, 0, 2, 0)
        title_row.addWidget(label("投稿项目", "sectionTitle"))
        title_row.addStretch()
        title_row.addWidget(label("示例", "quietBadge"))
        column.addLayout(title_row)
        column.addSpacing(14)
        column.addWidget(line())
        column.addSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        items = QWidget()
        items_layout = QVBoxLayout(items)
        items_layout.setContentsMargins(0, 0, 0, 0)
        items_layout.setSpacing(4)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.project_buttons = []
        for index, (title, status, emphasized) in enumerate(PROJECTS):
            item = ProjectItem(title, status, emphasized)
            group.addButton(item, index)
            item.clicked.connect(lambda _checked=False, title=title: self._select_project(title))
            items_layout.addWidget(item)
            self.project_buttons.append(item)
        self.project_buttons[0].setChecked(True)
        items_layout.addStretch()
        scroll.setWidget(items)
        column.addWidget(scroll, 1)
        column.addWidget(line())
        column.addSpacing(12)
        column.addWidget(label("项目将来自投稿目录 · 当前仅展示样例", "subtle"))
        return rail

    def _subtitle_page(self) -> QWidget:
        """字幕页：左列播放器 + 时间轴，右列字幕列表 + AI 检查。

        底部两张卡分别与上方主卡同列，右端对齐；两列的上下分界线联动，横向一条线。
        """

        self.subtitle_columns = self._gap_splitter(Qt.Orientation.Horizontal)
        self.vertical_splitter = self._gap_splitter(Qt.Orientation.Vertical)
        self.player_card = self._card(self._video_surface())
        self.timeline_card = self._card(self._timeline())
        self.vertical_splitter.addWidget(self.player_card)
        self.vertical_splitter.addWidget(self.timeline_card)
        self.vertical_splitter.setStretchFactor(0, 1)
        self.side_splitter = self._gap_splitter(Qt.Orientation.Vertical)
        self.list_card = self._card(self._subtitle_list())
        self.ai_panel = self._ai_panel()
        self.ai_check_card = self._card(self.ai_panel)
        self.side_splitter.addWidget(self.list_card)
        self.side_splitter.addWidget(self.ai_check_card)
        self.side_splitter.setStretchFactor(0, 1)
        self.subtitle_columns.addWidget(self.vertical_splitter)
        self.subtitle_columns.addWidget(self.side_splitter)
        self.subtitle_columns.setStretchFactor(0, 1)
        # 用户拖过左右分界就记住他的宽度；没拖过时随窗口按设计比例重算
        self._columns_dragged = False
        self.subtitle_columns.splitterMoved.connect(lambda *_: setattr(self, "_columns_dragged", True))
        self.subtitle_columns.installEventFilter(_ColumnsFit(self))
        self.vertical_splitter.splitterMoved.connect(
            lambda *_: self._mirror_rows(self.vertical_splitter, self.side_splitter))
        self.side_splitter.splitterMoved.connect(
            lambda *_: self._mirror_rows(self.side_splitter, self.vertical_splitter))
        return self.subtitle_columns

    @staticmethod
    def _mirror_rows(source: QSplitter, target: QSplitter) -> None:
        sizes = source.sizes()
        if len(sizes) != 2 or target.count() != 2 or target.sizes() == sizes:
            return
        blocked = target.blockSignals(True)
        target.setSizes(sizes)
        target.blockSignals(blocked)

    def _video_surface(self) -> QWidget:
        surface = QWidget()
        surface.setObjectName("videoSurface")
        surface.setMinimumHeight(200)
        column = QVBoxLayout(surface)
        column.setContentsMargins(24, 20, 24, 15)
        column.addStretch()
        glyph = label("▶")
        themed(glyph, lambda: glyph.setStyleSheet(
            f"color: {COLORS.accent_ink}; background: {COLORS.accent_soft};"
            "border-radius: 30px; font-size: 22px; padding-left: 3px;"
        ))
        glyph.setFixedSize(60, 60)
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(glyph, alignment=Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(18)
        title = label("视频预览区域", "bodyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)
        hint = label("连续播放器将在 Desktop-05 接入", "hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(hint)
        column.addStretch()
        controls = QHBoxLayout()
        controls.addWidget(label("00:00:00  /  00:00:00", "subtle"))
        controls.addStretch()
        controls.addWidget(label("画面与声音将在播放器接入后可用", "subtle"))
        column.addLayout(controls)
        return surface

    def _timeline(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("timeline")
        panel.setMinimumHeight(80)
        panel.setMaximumHeight(120)
        column = QVBoxLayout(panel)
        column.setContentsMargins(24, 6, 24, 5)
        column.setSpacing(1)
        header = QHBoxLayout()
        header.addWidget(label("字幕时间轴", "sectionTitle"))
        header.addSpacing(8)
        header.addWidget(label("布局示意", "subtle"))
        header.addStretch()
        column.addLayout(header)
        column.addWidget(TimelineTrack(), 1)
        return panel

    def _subtitle_list(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("subtitleList")
        panel.setMinimumHeight(170)
        column = QVBoxLayout(panel)
        column.setContentsMargins(24, 17, 24, 12)
        column.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(label("字幕列表", "sectionTitle"))
        header.addSpacing(8)
        header.addWidget(label("示例行 · 暂不可编辑", "subtle"))
        header.addStretch()
        header.addWidget(label("全部  124", "quietBadge"))
        column.addLayout(header)
        column.addWidget(line())
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        rows = QVBoxLayout(content)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(0)
        samples = (
            ("041", "17:58.20  →  18:01.84", "这个地方我们可以再看一眼。", False),
            ("042", "18:02.11  →  18:05.96", "等一下，刚刚是不是有人说漏了？", True),
            ("043", "18:06.14  →  18:09.52", "先别急，我把画面暂停一下。", False),
            ("044", "18:10.07  →  18:13.42", "好，现在应该能看清楚了。", False),
        )
        for number, timing, text, active in samples:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(2, 9, 2, 9)
            row_layout.setSpacing(15)
            row_layout.addWidget(label(number, "badge" if active else "subtle"))
            time_label = label(timing, "muted")
            time_label.setMinimumWidth(174)
            row_layout.addWidget(time_label)
            row_layout.addWidget(label(text), 1)
            if active:
                row_layout.addWidget(label("当前", "badge"))
            rows.addWidget(row)
            rows.addWidget(line())
        rows.addStretch()
        scroll.setWidget(content)
        column.addWidget(scroll, 1)
        return panel

    def _ai_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("aiPanel")
        panel.setMinimumWidth(SIZES.ai_collapsed_width)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        header = QWidget()
        header.setFixedHeight(60)
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(8, 0, 14, 0)
        self.ai_toggle = QPushButton()
        self.ai_toggle.setObjectName("quiet")
        tool_icon(self.ai_toggle, "chevron_left")
        self.ai_toggle.setFixedSize(32, 32)
        self.ai_toggle.setToolTip("展开或收起 AI 建议区")
        self.ai_toggle.clicked.connect(lambda: self._set_ai_open(not self._ai_open))
        header_row.addWidget(self.ai_toggle)
        self.ai_header = label("AI 建议", "sectionTitle")
        header_row.addWidget(self.ai_header)
        header_row.addStretch()
        column.addWidget(header)
        column.addWidget(line())
        self.ai_content = QWidget()
        content = QVBoxLayout(self.ai_content)
        content.setContentsMargins(20, 22, 20, 20)
        content.setSpacing(12)
        content.addWidget(label("待确认", "sectionTitle"))
        content.addWidget(label("AI 检查由用户主动启动。", "muted"))
        content.addSpacing(8)
        content.addWidget(line())
        content.addSpacing(8)
        content.addWidget(label("这里将显示字幕差异、简短原因与处理动作。", "hint"))
        content.addStretch()
        content.addWidget(label("Desktop-07 接入实际建议", "subtle"))
        column.addWidget(self.ai_content, 1)
        self.ai_collapsed_label = label("AI", "badge")
        self.ai_collapsed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.ai_collapsed_label, alignment=Qt.AlignmentFlag.AlignTop)
        column.addStretch()
        return panel

    def _cover_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("workArea")
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        heading = QWidget()
        heading.setFixedHeight(60)
        heading_row = QHBoxLayout(heading)
        heading_row.setContentsMargins(24, 0, 24, 0)
        heading_row.addWidget(label("封面制作", "pageTitle"))
        heading_row.addSpacing(8)
        heading_row.addWidget(label("工作区布局预览", "quietBadge"))
        heading_row.addStretch()
        column.addWidget(heading)
        column.addWidget(line())
        content = QVBoxLayout()
        content.setContentsMargins(32, 28, 32, 28)
        content.setSpacing(0)
        content.addWidget(label("封面画布", "sectionTitle"))
        content.addSpacing(16)
        surface = QWidget()
        surface.setObjectName("coverSurface")
        surface.setMinimumHeight(300)
        canvas = QVBoxLayout(surface)
        canvas.addStretch()
        title = label("16:9 画布将在 Desktop-09 接入", "bodyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        canvas.addWidget(title)
        hint = label("这里预留主视觉与编辑空间；当前不读取项目图片。", "hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        canvas.addWidget(hint)
        canvas.addStretch()
        content.addWidget(surface, 1)
        column.addLayout(content, 1)
        return page

    def _settings_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("settingsSurface")
        column = QVBoxLayout(page)
        column.setContentsMargins(32, 26, 32, 26)
        column.setSpacing(14)
        column.addWidget(label("设置", "pageTitle"))
        column.addWidget(line())
        column.addSpacing(12)
        column.addWidget(label("桌面基础设置将在 Desktop-04.6 接入", "bodyTitle"))
        column.addWidget(label("当前页面仅展示统一深色主题、文字层级和控件状态。", "hint"))
        column.addSpacing(16)
        column.addWidget(label("界面样式", "sectionTitle"))
        column.addWidget(label("深蓝灰工作台  ·  青绿色强调  ·  跟随 Qt 高 DPI 缩放", "muted"))
        column.addStretch()
        return page

    def _select_page(self, index: int) -> None:
        changed = index != self.pages.currentIndex()
        self.pages.setCurrentIndex(index)
        self._page_chrome(index)
        self.nav_buttons[index].setChecked(True)
        if changed:
            # 切页后卡片依次进场；只是视觉，页面已经可以操作
            motion.page_enter(self.pages.currentWidget())

    def _page_chrome(self, index: int) -> None:
        """顶栏随页面变化：设置页整页是一张卡，没有顶栏（设计稿）。"""

        self.appbar.setVisible(index != 2)
        self.project_switch.setVisible(index != 2)
        if index == 2 and self.project_popover.isVisible():
            self.project_popover.hide()

    def _select_project(self, title: str) -> None:
        self.top_project.setText(title if len(title) <= 24 else title[:24] + "…")
        self.project_popover.dismiss()

    def _set_ai_open(self, open_: bool) -> None:
        """AI 检查卡常驻在字幕列表下方；保留这个入口只为兼容旧调用。"""

        self._ai_open = True
        self._ai_chrome(True)

    def _ai_chrome(self, open_: bool | None) -> None:
        self.ai_content.setVisible(True)
        self.ai_header.setVisible(True)
        self.ai_collapsed_label.setVisible(False)
        if hasattr(self, "ai_toggle"):
            self.ai_toggle.setVisible(False)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._splitter_initialized:
            self._splitter_initialized = True
            QTimer.singleShot(0, self._set_initial_splitters)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._splitter_initialized:
            return
        compact = self.height() < SIZES.compact_height
        if compact != getattr(self, "_compact_layout", compact):
            self._compact_layout = compact
            QTimer.singleShot(0, self._balance_vertical_splitter)

    def _fit_columns(self) -> None:
        total = max(1, self.subtitle_columns.width())
        # 设计稿 1440 宽时两列合计 1346px、右列 400px；窄窗口不低于 340，宽窗口不超过 520
        side = max(340, min(520, round(total * 400 / 1346)))
        if self.subtitle_columns.sizes() != [max(1, total - side), side]:
            self.subtitle_columns.setSizes([max(1, total - side), side])

    def _set_initial_splitters(self) -> None:
        self._fit_columns()
        self._compact_layout = self.height() < SIZES.compact_height
        self._balance_vertical_splitter()

    def _balance_vertical_splitter(self) -> None:
        usable = sum(self.vertical_splitter.sizes()) or self.vertical_splitter.height()
        timeline = 136 if self._compact_layout else SIZES.timeline_card_height
        self.vertical_splitter.setSizes([max(200, usable - timeline), timeline])
        self._mirror_rows(self.vertical_splitter, self.side_splitter)
