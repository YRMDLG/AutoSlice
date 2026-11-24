"""设置页“外观”：主题（夜航 / 晨雾）、强调色和夜航底色；点选即时生效并保存到本机。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QApplication, QButtonGroup, QHBoxLayout, QPushButton, QWidget

from autoslice.desktop.qt_preview import appearance, motion
from autoslice.desktop.qt_preview.appearance import Appearance
from autoslice.desktop.qt_preview.theme import COLORS, STYLE, THEMES
from autoslice.desktop.qt_preview.window import card, label


class SwatchButton(QAbstractButton):
    """一个色块：圆形颜色 + 选中时外圈描边。不走 ButtonFx，自己画。"""

    SIZE = 28

    def __init__(self, color: str, tooltip: str, parent=None) -> None:
        super().__init__(parent)
        self.color = color
        self.setCheckable(True)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._ring = motion.Channel(self, self.update)
        self._hover = motion.Channel(self, self.update)
        self.toggled.connect(lambda on: self._ring.to(1.0 if on else 0.0, motion.TOGGLE))

    def sizeHint(self) -> QSize:
        return QSize(self.SIZE, self.SIZE)

    def enterEvent(self, event) -> None:
        self._hover.to(1.0, motion.HOVER_IN)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover.to(0.0, motion.HOVER_OUT)
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        ring = max(self._ring.value, 1.0 if self.isChecked() and not motion.enabled() else 0.0)
        inset = 4.0 - 0.6 * self._hover.value
        painter.setPen(QPen(QColor(COLORS.control_border), 1))
        painter.setBrush(QColor(self.color))
        painter.drawEllipse(rect.adjusted(inset, inset, -inset, -inset))
        if ring > 0.01:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(motion.faded(COLORS.accent_ink if STYLE.dark else COLORS.accent_line, ring), 1.5))
            painter.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))


class AppearancePanel(QWidget):
    """主题和强调色；改动后整套界面立即换色，不需要重启。"""

    changed = Signal(object)

    def __init__(self, storage, parent=None) -> None:
        super().__init__(parent)
        self.storage = storage
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel, column = card("外观", "两套主题共用同一布局，只换配色和动效；点选立即生效，记在本机。")
        outer.addWidget(panel)

        theme_row = QHBoxLayout()
        theme_row.setSpacing(12)
        theme_row.addWidget(label("主题", "formLabel"))
        self.theme_segmented = QWidget()
        self.theme_segmented.setObjectName("segmented")
        segmented_row = QHBoxLayout(self.theme_segmented)
        segmented_row.setContentsMargins(3, 3, 3, 3)
        segmented_row.setSpacing(2)
        self.theme_buttons: dict[str, QPushButton] = {}
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        for name, spec in THEMES.items():
            button = QPushButton(spec.label)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setProperty("motion", True)
            button.setToolTip("深色：纯黑底、暖黑卡片" if name == "night" else "浅色：暖白底、柔和卡片")
            button.clicked.connect(lambda _checked=False, value=name: self._pick(theme=value))
            self._theme_group.addButton(button)
            self.theme_buttons[name] = button
            segmented_row.addWidget(button)
        motion.SegmentedFx(self.theme_segmented, tuple(self.theme_buttons.values()))
        theme_row.addWidget(self.theme_segmented)
        theme_row.addStretch(1)
        column.addLayout(theme_row)

        accent_row = QHBoxLayout()
        accent_row.setSpacing(12)
        accent_row.addWidget(label("强调色", "formLabel"))
        self.accent_host = QWidget()
        self.accent_layout = QHBoxLayout(self.accent_host)
        self.accent_layout.setContentsMargins(0, 0, 0, 0)
        self.accent_layout.setSpacing(6)
        accent_row.addWidget(self.accent_host)
        accent_row.addStretch(1)
        column.addLayout(accent_row)

        self.ground_row = QWidget()
        ground_line = QHBoxLayout(self.ground_row)
        ground_line.setContentsMargins(0, 0, 0, 0)
        ground_line.setSpacing(12)
        ground_line.addWidget(label("底色", "formLabel"))
        self.ground_host = QWidget()
        self.ground_layout = QHBoxLayout(self.ground_host)
        self.ground_layout.setContentsMargins(0, 0, 0, 0)
        self.ground_layout.setSpacing(6)
        ground_line.addWidget(self.ground_host)
        ground_line.addStretch(1)
        column.addWidget(self.ground_row)

        self.accent_buttons: list[SwatchButton] = []
        self.ground_buttons: list[SwatchButton] = []
        # 别处（启动、脚本）换了主题也要跟着刷新选中项
        appearance.themed(self, lambda: self._sync(appearance.current()))

    def current(self) -> Appearance:
        return appearance.current()

    def _rebuild_swatches(self, value: Appearance) -> None:
        spec = THEMES[value.theme]
        for buttons, layout in ((self.accent_buttons, self.accent_layout), (self.ground_buttons, self.ground_layout)):
            for button in buttons:
                layout.removeWidget(button)
                button.deleteLater()
            buttons.clear()
        for index, color in enumerate(spec.accents):
            button = SwatchButton(color, f"强调色 {index + 1}：{color}")
            button.setChecked(color == value.accent)
            button.clicked.connect(lambda _checked=False, choice=color: self._pick(accent=choice))
            self.accent_layout.addWidget(button)
            self.accent_buttons.append(button)
        names = {"#0A0A0A": "深邃黑", "#000000": "纯黑", "#0E0D0C": "暖黑"}
        for color in spec.grounds:
            button = SwatchButton(color, f"底色：{names.get(color, color)}")
            button.setChecked(color == value.ground)
            button.clicked.connect(lambda _checked=False, choice=color: self._pick(ground=choice))
            self.ground_layout.addWidget(button)
            self.ground_buttons.append(button)
        self.ground_row.setVisible(bool(spec.grounds))

    def _sync(self, value: Appearance) -> None:
        for name, button in self.theme_buttons.items():
            button.setChecked(name == value.theme)
        shown = ([b.color for b in self.accent_buttons], [b.color for b in self.ground_buttons])
        spec = THEMES[value.theme]
        if shown != (list(spec.accents), list(spec.grounds)):
            self._rebuild_swatches(value)
            return
        for button in self.accent_buttons:
            button.setChecked(button.color == value.accent)
        for button in self.ground_buttons:
            button.setChecked(button.color == value.ground)

    def _pick(self, *, theme: str | None = None, accent: str | None = None, ground: str | None = None) -> None:
        now = appearance.current()
        if theme is not None and theme != now.theme:
            # 换主题时强调色、底色回到新主题的默认，避免拿深色的浅蓝去配浅色底
            target = Appearance.of(theme)
        else:
            target = Appearance.of(now.theme, accent or now.accent, ground or now.ground)
        if target == now:
            self._sync(now)
            return
        applied = appearance.apply(QApplication.instance(), target)
        try:
            appearance.save(self.storage, applied)
        except OSError:
            pass
        if QApplication.instance() is None:
            self._sync(applied)
        self.changed.emit(applied)
