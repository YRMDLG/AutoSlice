"""桌面端动效系统。

原则：
- 状态立即生效，只有视觉做过渡；逻辑与测试不等待动画。
- 进入快、退出缓：悬停 120ms 进、240ms 出；按下 90ms；选中 220ms；面板 300ms。
- 统一缓动：标准曲线 (0.2, 0, 0, 1)，快速响应、柔和落定；中途打断从当前值续接。
- 拖拽、框选、刮擦等直接操作跟手，不加动画。
- 离屏平台、系统关闭动画或 AUTOSLICE_REDUCED_MOTION=1 时全部瞬时完成。
"""

from __future__ import annotations

import ctypes
import os
import time

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QGraphicsOpacityEffect,
    QLineEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QStyle,
    QWidget,
)

from .theme import COLORS

# 时长（毫秒）
PRESS = 90
HOVER_IN = 120
HOVER_OUT = 240
TOGGLE = 180
SELECT = 220
PANEL = 300
PAGE = 220
VIEW = 260
SCROLL = 280
FADE = 180
STAGGER = 28


def _bezier(x1: float, y1: float, x2: float, y2: float) -> QEasingCurve:
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1, 1))
    return curve


STANDARD = _bezier(0.2, 0.0, 0.0, 1.0)
EMPHASIZED = _bezier(0.3, 0.0, 0.0, 1.0)


def restart(animation: QVariantAnimation, start, end, duration: int, curve: QEasingCurve | None = None) -> None:
    """复用动画重新起跑。

    跑完的动画停在终点，此时 setEndValue 会立即按新终点重算并发出 valueChanged，
    造成先跳到终点、再从起点播放的一帧闪烁；配置期间屏蔽信号即可避免。
    """

    animation.stop()
    blocked = animation.blockSignals(True)
    animation.setDuration(duration)
    if curve is not None:
        animation.setEasingCurve(curve)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setCurrentTime(0)
    animation.blockSignals(blocked)
    animation.start()

_enabled: bool | None = None


def enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = _detect()
    return _enabled


def _detect() -> bool:
    if os.environ.get("AUTOSLICE_REDUCED_MOTION", "").strip().lower() in ("1", "true", "yes"):
        return False
    app = QApplication.instance()
    if app is None or app.platformName() in ("offscreen", "minimal"):
        return False
    if os.name == "nt":
        # SPI_GETCLIENTAREAANIMATION：Windows「显示动画」开关
        value = ctypes.c_int(1)
        try:
            if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0) and not value.value:
                return False
        except (AttributeError, OSError):
            pass
    return True


def install_app(app: QApplication) -> None:
    """提示框用 Qt 自带淡入；菜单与下拉由 popups 模块自绘动效，关闭自带效果避免叠加。"""

    on = enabled()
    for effect in (Qt.UIEffect.UI_General, Qt.UIEffect.UI_AnimateTooltip, Qt.UIEffect.UI_FadeTooltip):
        QApplication.setEffectEnabled(effect, on)
    for effect in (Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu, Qt.UIEffect.UI_AnimateCombo):
        QApplication.setEffectEnabled(effect, False)


def mix(a, b, t: float) -> QColor:
    first, second = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor.fromRgbF(
        first.redF() + (second.redF() - first.redF()) * t,
        first.greenF() + (second.greenF() - first.greenF()) * t,
        first.blueF() + (second.blueF() - first.blueF()) * t,
        first.alphaF() + (second.alphaF() - first.alphaF()) * t,
    )


def faded(color, opacity: float) -> QColor:
    result = QColor(color)
    result.setAlphaF(max(0.0, min(1.0, result.alphaF() * opacity)))
    return result


def ease(t: float) -> float:
    return STANDARD.valueForProgress(max(0.0, min(1.0, t)))


class Channel(QObject):
    """单通道进度；目标改变时从当前值续接，剩余距离越短用时越短。"""

    def __init__(self, parent: QObject, on_change, value: float = 0.0) -> None:
        super().__init__(parent)
        self.value = value
        self.target = value
        self._on_change = on_change
        self._animation = QVariantAnimation(self)
        self._animation.valueChanged.connect(self._step)

    def _step(self, value) -> None:
        self.value = float(value)
        self._on_change()

    def to(self, target: float, duration: int, curve: QEasingCurve = STANDARD) -> None:
        if target == self.target and (self._animation.state() == QVariantAnimation.State.Running
                                      or self.value == target):
            return
        self.target = target
        self._animation.stop()
        distance = abs(target - self.value)
        if not enabled() or duration <= 0 or distance < 1e-3:
            self.value = target
            self._on_change()
            return
        restart(self._animation, float(self.value), float(target),
                max(40, int(duration * min(1.0, 0.35 + distance * 0.65))), curve)

    def snap(self, target: float) -> None:
        self._animation.stop()
        self.target = self.value = target


class KeyedAnimator(QObject):
    """按键管理多组进度（表格行、时间轴字幕块）；绘制时声明目标，定时器推进。"""

    def __init__(self, parent: QObject, on_update) -> None:
        super().__init__(parent)
        self._on_update = on_update
        self._items: dict = {}
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def value(self, key, target: float, duration: int) -> float:
        item = self._items.get(key)
        if item is None:
            # 首次出现不补动画，直接处于目标状态
            self._items[key] = [target, target, target, 0.0, duration]
            return target
        value, start, current_target, started, _duration = item
        if target != current_target:
            if not enabled() or duration <= 0:
                item[:] = [target, target, target, 0.0, duration]
                return target
            distance = abs(target - value)
            item[:] = [value, value, target, time.monotonic(),
                       max(40, int(duration * min(1.0, 0.35 + distance * 0.65)))]
            if not self._timer.isActive():
                self._timer.start()
        return item[0]

    def reset(self) -> None:
        self._items.clear()
        self._timer.stop()

    def _tick(self) -> None:
        now = time.monotonic()
        active = False
        for item in self._items.values():
            value, start, target, started, duration = item
            if value == target:
                continue
            progress = (now - started) * 1000 / max(1, duration)
            if progress >= 1:
                item[0] = target
            else:
                item[0] = start + (target - start) * ease(progress)
                active = True
        if not active:
            self._timer.stop()
        self._on_update()


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def _edge_pen(rect: QRectF, top, body) -> QPen:
    """顶边高光到侧边的渐变描边，模拟顶光。"""

    gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
    gradient.setColorAt(0.0, QColor(top))
    gradient.setColorAt(min(1.0, 2.5 / max(1.0, rect.height())), QColor(body))
    gradient.setColorAt(1.0, QColor(body))
    return QPen(QBrush(gradient), 1)


class ButtonFx(QObject):
    """按钮背景接管绘制：悬停、按下、勾选与键盘焦点四个通道平滑过渡。"""

    _RADIUS = {"navButton": 8, "projectItem": 6, "projectItemCompact": 6, "segment": 5}

    def __init__(self, button: QPushButton) -> None:
        super().__init__(button)
        self.button = button
        self.kind = button.objectName() or "raised"
        refresh = button.update
        self.hover = Channel(self, refresh)
        self.press = Channel(self, refresh)
        self.checked = Channel(self, refresh, 1.0 if button.isChecked() else 0.0)
        self.focus = Channel(self, refresh)
        button.toggled.connect(self._toggled)
        # 背景交给这里绘制；文字颜色、尺寸仍由全局样式决定
        button.setStyleSheet("QPushButton { background: transparent; border-color: transparent; }")
        button.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        button.installEventFilter(self)

    def _toggled(self, value: bool) -> None:
        duration = SELECT if self.kind in ("navButton", "projectItem", "projectItemCompact") else TOGGLE
        self.checked.to(1.0 if value else 0.0, duration)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Paint:
            self._paint()
        elif kind == QEvent.Type.Enter:
            if self.button.isEnabled():
                self.hover.to(1.0, HOVER_IN)
        elif kind == QEvent.Type.Leave:
            self.hover.to(0.0, HOVER_OUT)
            self.press.to(0.0, HOVER_OUT)
        elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.press.to(1.0, PRESS)
        elif kind == QEvent.Type.MouseButtonRelease:
            self.press.to(0.0, HOVER_OUT)
        elif kind == QEvent.Type.FocusIn:
            keyboard = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                                          Qt.FocusReason.ShortcutFocusReason)
            self.focus.to(1.0 if keyboard else 0.0, TOGGLE)
        elif kind == QEvent.Type.FocusOut:
            self.focus.to(0.0, HOVER_OUT)
        elif kind == QEvent.Type.EnabledChange and not self.button.isEnabled():
            self.hover.snap(0.0)
            self.press.snap(0.0)
        return False

    def _paint(self) -> None:
        button = self.button
        rect = QRectF(button.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = self._RADIUS.get(self.kind, 6)
        painter = QPainter(button)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        h, p, c, f = self.hover.value, self.press.value, self.checked.value, self.focus.value
        enabled_ = button.isEnabled()
        if self.kind in ("raised", "secondary", "primary"):
            self._paint_raised(painter, rect, radius, h, p, c, enabled_)
        elif self.kind in ("navButton", "projectItem", "projectItemCompact"):
            self._paint_selectable(painter, rect, radius, h, p, c)
        elif self.kind == "segment":
            if enabled_ and h * (1 - c) > 0.01:
                painter.fillPath(_rounded(rect, radius), faded(COLORS.hover, h * (1 - c)))
        else:
            self._paint_flat(painter, rect, radius, h, p, c, enabled_)
        if f > 0.01:
            painter.setPen(QPen(faded(COLORS.focus_ring, f), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, radius, radius)
        painter.end()

    def _paint_raised(self, painter, rect, radius, h, p, c, enabled_) -> None:
        path = _rounded(rect, radius)
        if not enabled_:
            painter.fillPath(path, QColor(COLORS.panel))
            painter.setPen(QPen(QColor(COLORS.divider), 1))
            painter.drawPath(path)
            return
        if self.kind == "primary":
            top = mix(COLORS.accent_top, "#5A8FF1", h)
            bottom = mix(COLORS.accent, COLORS.accent_hover, h)
            body = mix(COLORS.accent_pressed, COLORS.accent, h)
            edge = mix(COLORS.accent_edge, "#86ADF6", h)
            pressed, pressed_edge = COLORS.accent_pressed, COLORS.accent_medium
        else:
            top = mix(COLORS.raised_top, "#2B2B28", h)
            bottom = mix(COLORS.raised, COLORS.button_hover, h)
            body = mix(COLORS.border, COLORS.button_hover_border, h)
            edge = mix(COLORS.highlight, "#45443F", h)
            pressed, pressed_edge = COLORS.button_pressed, COLORS.canvas
            if c > 0:
                top, bottom = mix(top, COLORS.accent_subtle, c), mix(bottom, COLORS.accent_subtle, c)
                body, edge = mix(body, COLORS.accent_tint, c), mix(edge, "#24375A", c)
        top, bottom, edge = mix(top, pressed, p), mix(bottom, pressed, p), mix(edge, pressed_edge, p)
        gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
        gradient.setColorAt(0, top)
        gradient.setColorAt(1, bottom)
        painter.fillPath(path, QBrush(gradient))
        painter.setPen(_edge_pen(rect, edge, body))
        painter.drawPath(path)

    def _paint_flat(self, painter, rect, radius, h, p, c, enabled_) -> None:
        path = _rounded(rect, radius)
        if not enabled_:
            return
        if c > 0.01:
            painter.fillPath(path, faded(mix(COLORS.accent_subtle, "#1A2130", h), c))
            painter.setPen(QPen(faded(COLORS.accent_tint, c), 1))
            painter.drawPath(path)
        if h * (1 - c) > 0.01:
            painter.fillPath(path, faded(COLORS.hover, h * (1 - c)))
        if p > 0.01:
            painter.fillPath(path, faded(COLORS.button_pressed, p * 0.9))

    def _paint_selectable(self, painter, rect, radius, h, p, c) -> None:
        path = _rounded(rect, radius)
        hover = COLORS.hover if self.kind == "navButton" else COLORS.project_hover
        if h * (1 - c) > 0.01:
            painter.fillPath(path, faded(hover, h * (1 - c)))
        if c > 0.01:
            painter.fillPath(path, faded(COLORS.project_selected, c))
            # 选中指示条自中心向两端生长
            inset = 10 if self.kind == "navButton" else 8
            full = rect.height() - inset * 2
            length = full * ease(c)
            bar = QRectF(rect.left(), rect.center().y() - length / 2, 2, length)
            painter.fillPath(_rounded(bar, 1), faded(COLORS.accent, min(1.0, c * 1.4)))
        if p > 0.01:
            painter.fillPath(path, faded(COLORS.button_pressed, p * 0.6))


class SegmentedFx(QObject):
    """分段控件：凸起滑块在选项间滑动。"""

    def __init__(self, container: QWidget, buttons) -> None:
        super().__init__(container)
        self.container = container
        self.buttons = list(buttons)
        self._rect = QRectF()
        self._animation = QVariantAnimation(self)
        self._animation.setEasingCurve(STANDARD)
        self._animation.valueChanged.connect(self._moved)
        for button in self.buttons:
            ButtonFx(button)
            button.toggled.connect(lambda on, b=button: on and self._slide(b))
        container.installEventFilter(self)

    def _target(self) -> QRectF:
        current = next((b for b in self.buttons if b.isChecked()), None)
        return QRectF(current.geometry()) if current else QRectF()

    def _moved(self, value) -> None:
        self._rect = QRectF(value)
        self.container.update()

    def _slide(self, _button) -> None:
        target = self._target()
        if not enabled() or self._rect.isEmpty() or not self.container.isVisible():
            self._animation.stop()
            self._moved(target)
            return
        restart(self._animation, QRectF(self._rect), target, SELECT)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            if self._animation.state() != QVariantAnimation.State.Running:
                QTimer.singleShot(0, lambda: self._moved(self._target()))
        if event.type() == QEvent.Type.Paint:
            painter = QPainter(self.container)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            outer = QRectF(self.container.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = _rounded(outer, 7)
            painter.fillPath(path, QColor(COLORS.well))
            painter.setPen(_edge_pen(outer, COLORS.canvas, COLORS.border))
            painter.drawPath(path)
            if not self._rect.isEmpty():
                thumb = self._rect.adjusted(0.5, 0.5, -0.5, -0.5)
                gradient = QLinearGradient(0, thumb.top(), 0, thumb.bottom())
                gradient.setColorAt(0, QColor(COLORS.raised_top))
                gradient.setColorAt(1, QColor(COLORS.raised))
                thumb_path = _rounded(thumb, 5)
                painter.fillPath(thumb_path, QBrush(gradient))
                painter.setPen(_edge_pen(thumb, COLORS.highlight, COLORS.border))
                painter.drawPath(thumb_path)
            painter.end()
            return True
        return False


class SplitterFx(QObject):
    """分隔条：平时是深色缝隙，悬停浮现细线，拖动时变为强调色。"""

    def __init__(self, handle: QWidget, vertical: bool) -> None:
        super().__init__(handle)
        self.handle = handle
        self.vertical = vertical
        self.hover = Channel(self, handle.update)
        self.press = Channel(self, handle.update)
        handle.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Enter:
            self.hover.to(1.0, HOVER_IN)
        elif kind == QEvent.Type.Leave:
            self.hover.to(0.0, HOVER_OUT)
        elif kind == QEvent.Type.MouseButtonPress:
            self.press.to(1.0, PRESS)
        elif kind == QEvent.Type.MouseButtonRelease:
            self.press.to(0.0, HOVER_OUT)
        elif kind == QEvent.Type.Paint:
            painter = QPainter(self.handle)
            rect = QRectF(self.handle.rect())
            painter.fillRect(rect, QColor(COLORS.canvas))
            strength = max(self.hover.value, self.press.value)
            if strength > 0.01:
                color = mix(faded("#3A3935", strength), COLORS.accent_medium, self.press.value)
                if self.vertical:
                    length = rect.width() * (0.6 + 0.4 * ease(strength))
                    line = QRectF(rect.center().x() - length / 2, rect.center().y() - 1, length, 2)
                else:
                    length = rect.height() * (0.6 + 0.4 * ease(strength))
                    line = QRectF(rect.center().x() - 1, rect.center().y() - length / 2, 2, length)
                painter.fillRect(line, color)
            painter.end()
            return True
        return False


class SliderFx(QObject):
    """滑块：悬停时把手放大，按下时浮现光晕。"""

    def __init__(self, slider: QSlider) -> None:
        super().__init__(slider)
        self.slider = slider
        self.hover = Channel(self, slider.update)
        self.press = Channel(self, slider.update)
        slider.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        slider.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Enter:
            self.hover.to(1.0, HOVER_IN)
        elif kind == QEvent.Type.Leave:
            self.hover.to(0.0, HOVER_OUT)
        elif kind == QEvent.Type.MouseButtonPress:
            self.press.to(1.0, PRESS)
        elif kind == QEvent.Type.MouseButtonRelease:
            self.press.to(0.0, HOVER_OUT)
        elif kind == QEvent.Type.Paint:
            self._paint()
            return True
        return False

    def _paint(self) -> None:
        slider = self.slider
        painter = QPainter(slider)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 与样式中 12px 把手保持一致，点击位置和绘制位置对齐
        half = 6.0
        span = max(1, slider.width() - int(half * 2))
        position = QStyle.sliderPositionFromValue(slider.minimum(), slider.maximum(), slider.value(), span)
        x = half + position
        y = slider.height() / 2
        groove = QRectF(half, y - 1.5, slider.width() - half * 2, 3)
        painter.fillPath(_rounded(groove, 1.5), QColor(COLORS.border))
        painter.fillPath(_rounded(QRectF(groove.left(), groove.top(), x - groove.left(), 3), 1.5),
                         QColor(COLORS.accent if slider.isEnabled() else COLORS.disabled))
        if self.press.value > 0.01:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(faded(COLORS.accent, 0.22 * self.press.value))
            halo = 6 + 5 * ease(self.press.value)
            painter.drawEllipse(QPointF(x, y), halo, halo)
        radius = 5.5 + 1.5 * ease(self.hover.value)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(mix(COLORS.text, "#FFFFFF", self.hover.value))
        painter.drawEllipse(QPointF(x, y), radius, radius)
        painter.end()


class _Ring(QWidget):
    def __init__(self, owner: InputFx) -> None:
        super().__init__(owner.widget)
        self.owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        base = mix(COLORS.border, COLORS.button_hover_border, self.owner.hover.value)
        color = mix(base, COLORS.focus_ring, self.owner.focus.value)
        painter.setPen(QPen(color, 1))
        painter.drawRoundedRect(rect, 6, 6)


class InputFx(QObject):
    """输入框与下拉框：边框随悬停、焦点平滑变色。"""

    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        self.widget = widget
        self.hover = Channel(self, self._repaint)
        self.focus = Channel(self, self._repaint, 1.0 if widget.hasFocus() else 0.0)
        self.ring = _Ring(self)
        self.ring.setGeometry(widget.rect())
        self.ring.show()
        widget.installEventFilter(self)

    def _repaint(self) -> None:
        self.ring.update()

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Resize:
            self.ring.setGeometry(self.widget.rect())
            self.ring.raise_()
        elif kind == QEvent.Type.Enter:
            self.hover.to(1.0, HOVER_IN)
        elif kind == QEvent.Type.Leave:
            self.hover.to(0.0, HOVER_OUT)
        elif kind == QEvent.Type.FocusIn:
            self.focus.to(1.0, TOGGLE)
        elif kind == QEvent.Type.FocusOut:
            self.focus.to(0.0, HOVER_OUT)
        return False


class ScrollFx(QObject):
    """滚轮平滑滚动；触控板的像素级滚动保持原生。"""

    STEP = 96

    def __init__(self, area: QAbstractScrollArea) -> None:
        super().__init__(area)
        self.area = area
        self.bar = area.verticalScrollBar()
        self._target = None
        self._animation = QVariantAnimation(self)
        self._animation.setEasingCurve(_bezier(0.25, 0.1, 0.25, 1.0))
        self._animation.valueChanged.connect(lambda value: self.bar.setValue(int(round(value))))
        self._animation.finished.connect(self._done)
        area.viewport().installEventFilter(self)

    def _done(self) -> None:
        self._target = None

    def scroll_to(self, target: int, duration: int = SCROLL) -> None:
        target = max(self.bar.minimum(), min(self.bar.maximum(), int(target)))
        if not enabled() or not self.area.isVisible():
            self._animation.stop()
            self._target = None
            self.bar.setValue(target)
            return
        self._target = target
        restart(self._animation, float(self.bar.value()), float(target), duration)

    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.Wheel or not enabled():
            return False
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return False
        if not event.pixelDelta().isNull() or event.angleDelta().y() == 0:
            return False
        base = self._target if self._target is not None else self.bar.value()
        self.scroll_to(base - event.angleDelta().y() / 120 * self.STEP)
        event.accept()
        return True


def smooth_scroll(area: QAbstractScrollArea, apply) -> None:
    """执行 apply（如 scrollTo）后把跳变改为平滑滚动。"""

    fx = next((child for child in area.children() if isinstance(child, ScrollFx)), None)
    bar = area.verticalScrollBar()
    before = bar.value()
    apply()
    after = bar.value()
    if fx is None or after == before or not enabled() or not area.isVisible():
        return
    bar.setValue(before)
    fx.scroll_to(after, SCROLL)


class _Fade(QWidget):
    """快照淡出层：旧画面盖在新画面上逐渐消失。"""

    def __init__(self, parent: QWidget, pixmap, rect, duration: int) -> None:
        super().__init__(parent)
        self.pixmap = pixmap
        self.opacity = 1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(rect)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(duration)
        self._animation.setEasingCurve(STANDARD)
        self._animation.setStartValue(1.0)
        self._animation.setEndValue(0.0)
        self._animation.valueChanged.connect(self._step)
        self._animation.finished.connect(self.deleteLater)
        self.show()
        self.raise_()
        self._animation.start()

    def _step(self, value) -> None:
        self.opacity = float(value)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setOpacity(self.opacity)
        painter.drawPixmap(0, 0, self.pixmap)


def crossfade(container: QWidget, apply, duration: int = PAGE) -> None:
    """先截取旧画面，再切换，旧画面淡出露出新画面。"""

    if not enabled() or not container.isVisible() or container.width() <= 0:
        apply()
        return
    snapshot = container.grab()
    geometry = container.geometry()
    apply()
    # 淡出层挂在父控件上：QSplitter 会把新子控件收为分栏
    host = container.parentWidget()
    if host is None:
        _Fade(container, snapshot, container.rect(), duration)
    else:
        _Fade(host, snapshot, geometry, duration)


def fade_out_snapshot(widget: QWidget, duration: int = FADE) -> None:
    """控件即将隐藏时留下快照淡出，逻辑上仍立即隐藏。"""

    parent = widget.parentWidget()
    if not enabled() or parent is None or not widget.isVisible():
        return
    _Fade(parent, widget.grab(), widget.geometry(), duration)


def fade_in(widget: QWidget, duration: int = FADE, delay: int = 0, start: float = 0.0) -> None:
    """透明度淡入；结束后移除效果，避免持续离屏合成。"""

    if not enabled() or (not widget.isVisible() and delay == 0):
        return
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(start)
    widget.setGraphicsEffect(effect)
    animation = QVariantAnimation(effect)
    animation.setDuration(duration)
    animation.setEasingCurve(STANDARD)
    animation.setStartValue(start)
    animation.setEndValue(1.0)
    animation.valueChanged.connect(lambda value: effect.setOpacity(float(value)))

    def finish():
        if widget.graphicsEffect() is effect:
            widget.setGraphicsEffect(None)

    animation.finished.connect(finish)
    if delay:
        QTimer.singleShot(delay, animation.start)
    else:
        animation.start()


def reveal(widget: QWidget, visible: bool = True) -> None:
    """显隐切换：出现时淡入，消失时留快照淡出；可见性本身立即生效。"""

    shown = widget.isVisible()
    if visible:
        widget.show()
        if not shown:
            fade_in(widget)
    else:
        if shown:
            fade_out_snapshot(widget)
        widget.hide()


def stagger_in(widgets, duration: int = FADE + 60) -> None:
    for order, widget in enumerate(widgets):
        fade_in(widget, duration, delay=min(order, 12) * STAGGER)


class ValueGlide(QObject):
    """数值平滑过渡（视图中心、缩放、面板宽度等）。"""

    def __init__(self, parent: QObject, apply) -> None:
        super().__init__(parent)
        self._apply = apply
        self.target = None
        self._animation = QVariantAnimation(self)
        self._animation.valueChanged.connect(self._step)
        self._animation.finished.connect(self._done)
        self._start = self._end = None
        self._on_finish = None

    def running(self) -> bool:
        return self._animation.state() == QVariantAnimation.State.Running

    def _step(self, value) -> None:
        t = float(value)
        self._apply(tuple(a + (b - a) * t for a, b in zip(self._start, self._end)))

    def _done(self) -> None:
        self.target = None
        callback, self._on_finish = self._on_finish, None
        if callback:
            callback()

    def go(self, start, end, duration: int, curve: QEasingCurve = STANDARD, on_finish=None) -> None:
        self._animation.stop()
        self._on_finish = on_finish
        if not enabled() or duration <= 0 or start == end:
            self.target = None
            self._apply(tuple(end))
            self._on_finish = None
            if on_finish:
                on_finish()
            return
        self._start, self._end, self.target = tuple(start), tuple(end), tuple(end)
        restart(self._animation, 0.0, 1.0, duration, curve)

    def stop(self) -> None:
        self._animation.stop()
        self.target = None


def install_tree(root: QWidget) -> None:
    """为子树中尚未接入的可交互控件挂上动效；可重复调用。"""

    for button in root.findChildren(QPushButton):
        if button.property("motion") or button.objectName() == "segment":
            continue
        button.setProperty("motion", True)
        ButtonFx(button)
    for splitter in root.findChildren(QSplitter):
        vertical = splitter.orientation() == Qt.Orientation.Vertical
        for index in range(1, splitter.count()):
            handle = splitter.handle(index)
            if handle is not None and not handle.property("motion"):
                handle.setProperty("motion", True)
                SplitterFx(handle, vertical)
    for slider in root.findChildren(QSlider):
        if not slider.property("motion"):
            slider.setProperty("motion", True)
            SliderFx(slider)
    for widget in root.findChildren(QComboBox) + root.findChildren(QLineEdit):
        if isinstance(widget.parentWidget(), QComboBox) or widget.property("motion"):
            continue
        widget.setProperty("motion", True)
        InputFx(widget)
    for area in root.findChildren(QAbstractScrollArea):
        if not area.property("motion") and area.verticalScrollBar() is not None:
            area.setProperty("motion", True)
            ScrollFx(area)


__all__ = [
    "ButtonFx", "Channel", "KeyedAnimator", "SegmentedFx", "ValueGlide", "crossfade", "enabled",
    "fade_in", "fade_out_snapshot", "faded", "install_app", "install_tree", "mix", "reveal", "smooth_scroll",
    "stagger_in",
]
