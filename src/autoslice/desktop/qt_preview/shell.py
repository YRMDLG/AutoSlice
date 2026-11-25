"""窗口外壳的通用件：卡片、窗口底、导航项、任务环、项目切换钮、弹层和顶部提示条。

只管外观与交互手感，不持有任何业务状态；颜色、圆角、动效都取当前主题。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen, QRadialGradient, QRegion
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import motion
from .icons import icon as desktop_icon
from .theme import COLORS, SIZES, STYLE


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


class Surface(QWidget):
    """窗口底：铺底色；晨雾在卡片下面画柔和投影，悬停的卡片投影加深。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("surface")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(COLORS.ground))
        if not STYLE.card_shadow:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        shadow = QColor(COLORS.shadow)
        for card in self.findChildren(Card):
            if not card.isVisible() or card.window() is not self.window():
                continue
            origin = card.mapTo(self, QPoint(0, 0))
            rect = QRectF(origin.x(), origin.y(), card.width(), card.height())
            radius = STYLE.card_radius
            # 常驻：0 1px 2px 的极淡投影
            shadow.setAlphaF(0.05)
            painter.setBrush(shadow)
            painter.drawRoundedRect(rect.translated(0, 1).adjusted(0, 0, 0, 1), radius, radius)
            lift = card.lift()
            if lift > 0.01:
                # 悬停：0 14px 34px -18px，向下晕开
                for step in range(6):
                    spread = 4 + step * 3
                    shadow.setAlphaF(0.035 * lift * (6 - step) / 6)
                    painter.setBrush(shadow)
                    painter.drawRoundedRect(rect.adjusted(spread * 0.4, 10 + step * 2, -spread * 0.4, spread),
                                            radius + spread * 0.4, radius + spread * 0.4)


class Card(QWidget):
    """悬浮卡片：底色、描边、顶边高光。夜航鼠标经过时描边沿光标亮起，晨雾悬停时投影加深。"""

    def __init__(self, parent: QWidget | None = None, *, spotlight: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.spotlight = spotlight
        self._hover = motion.Channel(self, self._changed)
        self._cursor = QPointF(-999, -999)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._follow)

    def lift(self) -> float:
        return self._hover.value

    def _edge(self) -> QRegion:
        """描边那一圈：跟随光只改这里，卡片里的表格、时间轴不用跟着重画。"""

        band = 3
        outer = QRegion(self.rect())
        return outer.subtracted(QRegion(self.rect().adjusted(band, band, -band, -band)))

    def _changed(self) -> None:
        if STYLE.dark:
            self.update(self._edge())
        elif STYLE.card_shadow:
            # 晨雾悬停只加深卡片外的投影，卡片本身不重画
            surface = self._surface()
            if surface is not None:
                origin = self.mapTo(surface, QPoint(0, 0))
                around = QRegion(QRect(origin, self.size()).adjusted(-24, -8, 24, 40))
                surface.update(around.subtracted(QRegion(QRect(origin, self.size()).adjusted(2, 2, -2, -2))))

    def _surface(self) -> QWidget | None:
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, Surface):
            parent = parent.parentWidget()
        return parent

    def _follow(self) -> None:
        position = QPointF(self.mapFromGlobal(QCursor.pos()))
        if position != self._cursor:
            self._cursor = position
            self.update(self._edge())

    def enterEvent(self, event) -> None:
        self._hover.to(1.0, motion.profile().hover_in * 3)
        if self.spotlight and STYLE.dark and motion.enabled():
            self._follow()
            self._timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover.to(0.0, motion.profile().hover_out)
        self._timer.stop()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = STYLE.card_radius
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = _rounded(rect, radius)
        painter.fillPath(path, QColor(COLORS.card))
        h = self._hover.value
        painter.setPen(QPen(QColor(COLORS.card_border), 1))
        painter.drawPath(path)
        if STYLE.dark:
            painter.setPen(QPen(QColor(COLORS.card_highlight), 1))
            painter.drawLine(QPointF(rect.left() + radius, rect.top() + 1), QPointF(rect.right() - radius, rect.top() + 1))
            if self.spotlight and h > 0.01 and self._cursor.x() > -900:
                edge = QRadialGradient(self._cursor, 240)
                color = QColor(COLORS.accent)
                color.setAlphaF(0.6 * h)
                edge.setColorAt(0.0, color)
                color.setAlphaF(0.0)
                edge.setColorAt(0.7, color)
                painter.setPen(QPen(edge, 1))
                painter.drawPath(path)


class Divider(QWidget):
    """卡片内的分隔线：首尾各留一段，不连到卡片边框。"""

    def __init__(self, inset: int = 16, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.inset = inset
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(QRect(self.inset, 0, max(0, self.width() - self.inset * 2), 1), QColor(COLORS.divider))


class NavItem(QPushButton):
    """导航项：图标 + 两字标题；选中时夜航强调色描边、晨雾白底卡片（底板由 ButtonFx 画）。"""

    ICONS = {"字幕": "subtitles", "封面": "cover", "设置": "settings"}

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(46, 46)
        self.setAccessibleName(title)
        self.icon_name = self.ICONS[title]
        self.title = title
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 5, 0, 4)
        column.setSpacing(0)
        self.glyph = QLabel()
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.glyph.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.caption = QLabel(title)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        column.addWidget(self.glyph)
        column.addWidget(self.caption)
        self.toggled.connect(lambda _on: self.refresh())
        from .appearance import themed

        themed(self, self.refresh)

    def refresh(self) -> None:
        if self.isChecked():
            color = COLORS.accent_ink if STYLE.dark else COLORS.text
        else:
            color = COLORS.subtle
        self.glyph.setPixmap(desktop_icon(self.icon_name, color, 18).pixmap(18, 18))
        self.caption.setStyleSheet(f"font-size: {SIZES.text_small}px; color: {color}; background: transparent;")


class TaskRing(QPushButton):
    """导航底部的后台任务环：压制进行中画进度环，点开看任务中心。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("taskRing")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 40)
        self.setToolTip("后台任务")
        self.setAccessibleName("后台任务")
        self.setProperty("motion", True)
        self._progress: float | None = None
        self._shown = motion.Channel(self, self.update)
        self._spin_started = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self.update)

    def progress(self) -> float | None:
        return self._progress

    def set_progress(self, value: float | None, tooltip: str = "") -> None:
        """value 为 0~1 的进度；None 表示没有在跑的任务；-1 表示进度未知（转圈）。"""

        self._progress = value
        self.setToolTip(tooltip or ("后台任务" if value is None else "后台任务进行中"))
        self._shown.to(0.0 if value is None else 1.0, motion.SELECT)
        if value is not None and value < 0 and motion.enabled():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(40, 40)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        radius = 8.0
        hover = self.underMouse()
        track = QColor(COLORS.control_hover_border if hover else COLORS.card_border)
        painter.setPen(QPen(track, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, radius, radius)
        shown = self._shown.value
        value = self._progress
        if shown > 0.01:
            pen = QPen(motion.faded(COLORS.accent if STYLE.dark else COLORS.accent_line, shown), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            box = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
            if value is not None and value >= 0:
                painter.drawArc(box, 90 * 16, -int(360 * 16 * max(0.0, min(1.0, value))))
            else:
                start = (time.monotonic() - self._spin_started) * 360 % 360
                painter.drawArc(box, int(-start * 16), 100 * 16)
        if value is None:
            # 空闲时中间一个小点，说明这里能点开任务中心
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS.subtle))
            painter.drawEllipse(center, 1.6, 1.6)


class ProjectSwitch(QPushButton):
    """顶栏项目切换钮：项目名 + 一行说明 + 下拉箭头，点开弹出项目列表。"""

    def __init__(self, title: QLabel, meta: QLabel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("projectSwitch")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(44)
        self.setMaximumHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 4, 10, 4)
        row.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.setContentsMargins(0, 0, 0, 0)
        for item in (title, meta):
            item.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            text.addWidget(item)
        row.addLayout(text, 1)
        self.chevron = QLabel()
        self.chevron.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.chevron)
        self._open = motion.Channel(self, self._turn)
        from .appearance import themed

        themed(self, self._turn)

    def set_open(self, open_: bool) -> None:
        self._open.to(1.0 if open_ else 0.0, motion.SELECT)

    def _turn(self) -> None:
        name = "chevron_up" if self._open.value > 0.5 else "chevron_down"
        self.chevron.setPixmap(desktop_icon(name, COLORS.subtle, 14).pixmap(14, 14))


class Popover(QWidget):
    """弹层：圆角面板 + 柔和投影，承载任意内容（项目列表、检查提醒、音量……）。

    点外面自动收起；打开 / 关闭动效与下拉菜单一致。
    """

    closed = Signal()
    SHADOW = (12, 8, 12, 16)

    def __init__(self, parent: QWidget, content: QWidget, width: int = 420) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.content = content
        self.panel_width = width
        left, top, right, bottom = self.SHADOW
        layout = QVBoxLayout(self)
        layout.setContentsMargins(left + 6, top + 6, right + 6, bottom + 6)
        layout.addWidget(content)
        self._open = motion.Channel(self, self._step)
        self._closing = False
        self._above = False

    def popup(self, anchor: QWidget, align_right: bool = False, max_height: int = 560, above: bool = False) -> None:
        left, top, right, bottom = self.SHADOW
        self._closing = False
        self._above = above
        self.content.show()
        hint = self.layout().sizeHint()
        height = min(max_height, hint.height())
        self.resize(self.panel_width + left + right, height)
        if above:
            point = anchor.mapToGlobal(QPoint(anchor.width() if align_right else 0, -8))
            y = point.y() - height + bottom
        else:
            point = anchor.mapToGlobal(QPoint(anchor.width() if align_right else 0, anchor.height() + 6))
            y = point.y() - top
        x = point.x() - (self.width() - right if align_right else left)
        screen = anchor.screen().availableGeometry() if anchor.screen() else None
        if screen is not None:
            x = max(screen.left(), min(x, screen.right() - self.width()))
            y = max(screen.top(), min(y, screen.bottom() - self.height()))
        self.move(x, y)
        self.setWindowOpacity(0.0 if motion.enabled() else 1.0)
        self.show()
        self._open.snap(0.0)
        self._open.to(1.0, motion.profile().menu_open, motion.OUT)

    def dismiss(self) -> None:
        if self._closing or not self.isVisible():
            return
        self._closing = True
        if not motion.enabled():
            self.hide()
            return
        self._open.to(0.0, motion.profile().menu_close)

    def _step(self) -> None:
        self.setWindowOpacity(max(0.0, min(1.0, self._open.value * 1.4)))
        self.update()
        if self._closing and self._open.value <= 0.001:
            self.hide()

    def hideEvent(self, event) -> None:
        self._closing = False
        super().hideEvent(event)
        if QApplication.closingDown():
            # 退出时随窗口一起销毁：不再改透明度、不发信号，免得碰到已经拆掉的控件
            return
        self.setWindowOpacity(1.0)
        self.closed.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        from .popups import paint_panel

        left, top, right, bottom = self.SHADOW
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(left, top, -right, -bottom)
        value = self._open.value
        shift = motion.profile().menu_shift * (1 - value) * (1 if self._above else -1)
        painter.translate(0, shift)
        paint_panel(painter, body)


class Toast(QWidget):
    """顶部居中的提示条：对勾 + 文字 + 可选“撤销”，底边一条细线倒计时；鼠标停留时暂停。

    挂在窗口内容上，位置在顶栏一带，不会挡住时间轴。
    """

    undo_requested = Signal()

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.host = host
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 9, 10, 11)
        row.setSpacing(14)
        self.check = QLabel()
        self.check.setFixedSize(14, 14)
        row.addWidget(self.check)
        self.message = QLabel("")
        self.message.setObjectName("toastText")
        row.addWidget(self.message)
        self.undo_button = QPushButton("撤销")
        self.undo_button.setObjectName("primary")
        self.undo_button.setFixedHeight(28)
        self.undo_button.clicked.connect(self._undo)
        row.addWidget(self.undo_button)
        self._undo_callback = None
        self._enter = motion.Channel(self, self._place)
        # 倒计时细线：剩余比例从 1 线性走到 0，悬停暂停、离开续上
        self._fraction = 1.0
        self._clock = QVariantAnimation(self)
        self._clock.setEasingCurve(QEasingCurve(QEasingCurve.Type.Linear))
        self._clock.valueChanged.connect(self._tick)
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self.dismiss)
        self._closing = False
        self._remaining = self._total = 0
        self._started = 0.0
        host.installEventFilter(self)
        self.hide()

    def show_message(self, text: str, undo=None, hold: int | None = None) -> None:
        profile = motion.profile()
        self._undo_callback = undo
        self.message.setText(text)
        self.undo_button.setVisible(undo is not None)
        self.check.setPixmap(desktop_icon("check", COLORS.accent_ink if STYLE.dark else COLORS.success, 14).pixmap(14, 14))
        self.adjustSize()
        self._closing = False
        self.show()
        self.raise_()
        self._enter.snap(0.0)
        self._enter.to(1.0, profile.toast_in, motion.spring())
        self._remaining = self._total = hold or profile.toast_hold
        self._start_clock()

    def _tick(self, value) -> None:
        self._fraction = float(value)
        self.update()

    def _start_clock(self) -> None:
        self._started = time.monotonic()
        self._hold.start(self._remaining)
        self._fraction = self._remaining / max(1, self._total)
        self._clock.stop()
        if motion.enabled():
            motion.restart(self._clock, self._fraction, 0.0, self._remaining)

    def dismiss(self) -> None:
        if self._closing or not self.isVisible():
            return
        self._closing = True
        self._hold.stop()
        if not motion.enabled():
            self.hide()
            return
        self._enter.to(0.0, motion.profile().toast_out)

    def _undo(self) -> None:
        callback, self._undo_callback = self._undo_callback, None
        self.dismiss()
        if callback is not None:
            callback()

    def _place(self) -> None:
        value = self._enter.value
        shift = motion.profile().toast_shift * (1 - value)
        x = (self.host.width() - self.width()) // 2
        self.move(max(0, x), int(14 - shift))
        self.update()
        if self._closing and value <= 0.001:
            self.hide()
            self._closing = False

    def enterEvent(self, event) -> None:
        # 鼠标停在提示条上时暂停倒计时，方便点“撤销”
        if self._hold.isActive():
            self._remaining = max(400, self._hold.remainingTime())
            self._hold.stop()
            self._clock.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self.isVisible() and not self._closing and not self._hold.isActive():
            self._start_clock()
        super().leaveEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.host and event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        value = max(0.0, min(1.0, self._enter.value))
        painter.setOpacity(value if self._closing else min(1.0, value * 1.5))
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 12 if STYLE.dark else 14
        path = _rounded(rect, radius)
        painter.fillPath(path, QColor(COLORS.toast))
        painter.setPen(QPen(QColor(COLORS.toast_border), 1))
        painter.drawPath(path)
        remaining = max(0.0, min(1.0, self._fraction))
        if remaining > 0.001:
            line = QColor(COLORS.accent_line)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(line)
            inset = 14.0
            width = (rect.width() - inset * 2) * remaining
            painter.drawRoundedRect(QRectF(rect.left() + inset, rect.bottom() - 4, width, 2), 1, 1)

