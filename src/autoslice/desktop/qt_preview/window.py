"""可交互的 Desktop-04.55 Qt 壳，不读取投稿或字幕文件。"""

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
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

from .icons import icon as desktop_icon
from .theme import COLORS, SIZES

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
        self._set_icon_colors(icon, name, icon_name, False)

    @staticmethod
    def _set_icon_colors(icon: QLabel, name: QLabel, icon_name: str, selected: bool) -> None:
        color = COLORS.text if selected else COLORS.muted
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


class PreviewWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AutoSlice · Qt 视觉预览")
        self.resize(1560, 920)
        self.setMinimumSize(960, 640)
        self._ai_open = False
        self._last_ai_width = SIZES.ai_width
        self._splitter_initialized = False
        self._build()

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._appbar())

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self._navigation())

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(4)
        self.main_splitter.setChildrenCollapsible(False)
        self.project_rail = self._project_rail()
        self.project_rail.setMinimumWidth(SIZES.project_min_width)
        self.main_splitter.addWidget(self.project_rail)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._subtitle_page())
        self.pages.addWidget(self._cover_page())
        self.pages.addWidget(self._settings_page())
        self.main_splitter.addWidget(self.pages)
        self.main_splitter.setStretchFactor(1, 1)
        body_layout.addWidget(self.main_splitter, 1)
        root_layout.addWidget(body, 1)
        self.setCentralWidget(root)
        self._select_page(0)
        self._set_ai_open(False)

    def _appbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("appbar")
        bar.setFixedHeight(SIZES.appbar_height)
        row = QHBoxLayout(bar)
        row.setContentsMargins(22, 0, 20, 0)
        row.setSpacing(SIZES.space_3)
        row.addWidget(label("AUTOSLICE", "brand"))
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet(f"color: {COLORS.divider};")
        row.addSpacing(13)
        row.addWidget(divider)
        self.top_project = label("牛乳糖凌晨突击直播", "muted")
        row.addWidget(self.top_project)
        row.addStretch()
        row.addWidget(label("●", "badge"))
        row.addWidget(label("预览模式 · 无后台任务", "muted"))
        return bar

    def _navigation(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("navRail")
        rail.setFixedWidth(SIZES.nav_width)
        column = QVBoxLayout(rail)
        column.setContentsMargins(8, 17, 8, 17)
        column.setSpacing(SIZES.space_2)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.nav_buttons = []
        for index, (glyph, name) in enumerate((("≡", "字幕"), ("▧", "封面"), ("⚙", "设置"))):
            button = NavigationButton(glyph, name)
            group.addButton(button, index)
            button.clicked.connect(lambda _checked=False, page=index: self._select_page(page))
            self.nav_buttons.append(button)
            if index == 2:
                column.addStretch()
            column.addWidget(button)
        column.addStretch(0)
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
        self.subtitle_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.subtitle_splitter.setHandleWidth(4)
        self.subtitle_splitter.setChildrenCollapsible(False)
        self.work_area = self._subtitle_work_area()
        self.work_area.setMinimumWidth(410)
        self.subtitle_splitter.addWidget(self.work_area)
        self.ai_panel = self._ai_panel()
        self.subtitle_splitter.addWidget(self.ai_panel)
        self.subtitle_splitter.setStretchFactor(0, 1)
        return self.subtitle_splitter

    def _subtitle_work_area(self) -> QWidget:
        area = QWidget()
        area.setObjectName("workArea")
        column = QVBoxLayout(area)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        heading = QWidget()
        heading.setFixedHeight(60)
        heading_row = QHBoxLayout(heading)
        heading_row.setContentsMargins(24, 0, 24, 0)
        heading_row.addWidget(label("字幕校对", "pageTitle"))
        heading_row.addSpacing(8)
        heading_row.addWidget(label("工作区布局预览", "quietBadge"))
        heading_row.addStretch()
        heading_row.addWidget(label("视频 01 / 03", "muted"))
        column.addWidget(heading)
        column.addWidget(line())

        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self.vertical_splitter.setHandleWidth(4)
        self.vertical_splitter.setChildrenCollapsible(False)
        self.vertical_splitter.addWidget(self._video_surface())
        self.vertical_splitter.addWidget(self._timeline())
        self.vertical_splitter.addWidget(self._subtitle_list())
        self.vertical_splitter.setStretchFactor(0, 5)
        self.vertical_splitter.setStretchFactor(2, 3)
        column.addWidget(self.vertical_splitter, 1)
        return area

    def _video_surface(self) -> QWidget:
        surface = QWidget()
        surface.setObjectName("videoSurface")
        surface.setMinimumHeight(200)
        column = QVBoxLayout(surface)
        column.setContentsMargins(24, 20, 24, 15)
        column.addStretch()
        glyph = label("▶")
        glyph.setStyleSheet(
            f"color: {COLORS.accent_text}; background: {COLORS.accent_tint};"
            "border-radius: 30px; font-size: 22px; padding-left: 3px;"
        )
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
        self.ai_toggle = QPushButton("‹")
        self.ai_toggle.setObjectName("quiet")
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
        self.pages.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        self.project_rail.setVisible(index != 2)
        self.top_project.setVisible(index != 2)
        if index != 2:
            QTimer.singleShot(0, self._restore_project_width)

    def _select_project(self, title: str) -> None:
        self.top_project.setText(title if len(title) <= 24 else title[:24] + "…")

    def _restore_project_width(self) -> None:
        sizes = self.main_splitter.sizes()
        if len(sizes) == 2 and sizes[0] < SIZES.project_min_width:
            self.main_splitter.setSizes(
                [SIZES.project_width, max(1, sum(sizes) - SIZES.project_width)]
            )

    def _set_ai_open(self, open_: bool) -> None:
        if self._ai_open and not open_:
            self._last_ai_width = max(SIZES.ai_min_width, self.subtitle_splitter.sizes()[1])
        self._ai_open = open_
        self.ai_content.setVisible(open_)
        self.ai_header.setVisible(open_)
        self.ai_collapsed_label.setVisible(not open_)
        self.ai_toggle.setText("›" if open_ else "‹")
        self.ai_toggle.setToolTip("收起 AI 建议区" if open_ else "展开 AI 建议区")
        self.ai_panel.setMinimumWidth(SIZES.ai_min_width if open_ else SIZES.ai_collapsed_width)
        self.ai_panel.setMaximumWidth(16777215 if open_ else SIZES.ai_collapsed_width)
        available = max(1, sum(self.subtitle_splitter.sizes()))
        width = self._last_ai_width if open_ else SIZES.ai_collapsed_width
        self.subtitle_splitter.setSizes([max(1, available - width), width])

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

    def _set_initial_splitters(self) -> None:
        self.main_splitter.setSizes(
            [SIZES.project_width, max(1, self.main_splitter.width() - SIZES.project_width)]
        )
        self.subtitle_splitter.setSizes(
            [
                max(1, self.subtitle_splitter.width() - SIZES.ai_collapsed_width),
                SIZES.ai_collapsed_width,
            ]
        )
        self._compact_layout = self.height() < SIZES.compact_height
        self._balance_vertical_splitter()

    def _balance_vertical_splitter(self) -> None:
        usable = self.vertical_splitter.height() - 2 * self.vertical_splitter.handleWidth()
        timeline = 76 if self._compact_layout else SIZES.timeline_height
        video_fraction = (SIZES.compact_video_fraction if self._compact_layout
                          else SIZES.wide_video_fraction)
        self.vertical_splitter.setSizes(
            [
                max(200, int((usable - timeline) * video_fraction)),
                timeline,
                max(170, int((usable - timeline) * (1 - video_fraction))),
            ]
        )
