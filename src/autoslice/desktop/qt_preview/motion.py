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
from dataclasses import dataclass

import shiboken6
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QGraphicsOpacityEffect,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QStyle,
    QWidget,
)

from .theme import COLORS, STYLE

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
SHEEN = 720
SPRING_MOVE = 300
SPRING_MOVE_BOLD = 380


def _bezier(x1: float, y1: float, x2: float, y2: float) -> QEasingCurve:
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1, 1))
    return curve


STANDARD = _bezier(0.2, 0.0, 0.0, 1.0)
EMPHASIZED = _bezier(0.3, 0.0, 0.0, 1.0)
# 设计稿缓动：--out 快出缓停；--spring 两套主题回弹幅度不同；--soft 晨雾的柔和进出
OUT = _bezier(0.16, 1.0, 0.3, 1.0)
SPRING_NIGHT = _bezier(0.34, 1.45, 0.5, 1.0)
SPRING_MORNING = _bezier(0.34, 1.56, 0.64, 1.0)
SOFT = _bezier(0.45, 0.0, 0.2, 1.0)


def spring() -> QEasingCurve:
    return SPRING_NIGHT if STYLE.dark else SPRING_MORNING


@dataclass(frozen=True)
class Profile:
    """一套主题的动效档：夜航克制、晨雾更大胆；都以“不拖慢操作”为前提，状态先生效、动画只是视觉。"""

    hover_in: int
    hover_out: int
    press: int
    menu_open: int
    menu_close: int
    menu_item_delay: int
    menu_shift: float
    menu_scale: float
    menu_clip: bool
    dialog_open: int
    dialog_close: int
    dialog_shift: float
    veil_open: int
    toast_in: int
    toast_out: int
    toast_hold: int
    toast_shift: float
    card_enter: int
    card_stagger: int
    card_shift: float
    mist: int
    row_enter: int
    row_shift: float
    sheen_period: int


# 夜航：卡片 440ms 上移 6px、间隔 35ms；菜单 170ms 下移 4px + 0.97 缩放；对话框 260ms 上移 8px；提示条 300ms
NIGHT = Profile(
    hover_in=80, hover_out=200, press=60,
    menu_open=170, menu_close=140, menu_item_delay=15, menu_shift=4.0, menu_scale=0.97, menu_clip=False,
    dialog_open=260, dialog_close=160, dialog_shift=8.0, veil_open=220,
    toast_in=300, toast_out=180, toast_hold=3800, toast_shift=10.0,
    card_enter=440, card_stagger=35, card_shift=6.0, mist=0,
    row_enter=200, row_shift=4.0, sheen_period=1100,
)
# 晨雾：设计稿是雾散 1200ms + 卡片 900ms；为了不拖慢操作收短到 700 / 640ms，幅度保留
MORNING = Profile(
    hover_in=140, hover_out=260, press=70,
    menu_open=360, menu_close=160, menu_item_delay=40, menu_shift=8.0, menu_scale=0.98, menu_clip=True,
    dialog_open=520, dialog_close=200, dialog_shift=18.0, veil_open=320,
    toast_in=460, toast_out=200, toast_hold=4200, toast_shift=24.0,
    card_enter=640, card_stagger=60, card_shift=14.0, mist=700,
    row_enter=360, row_shift=8.0, sheen_period=1100,
)


def profile() -> Profile:
    return NIGHT if STYLE.dark else MORNING


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


def set_enabled(value: bool | None) -> None:
    """强制开 / 关动效；None 表示按系统设置重新判断（离线截图、测试、将来的“减少动效”开关用）。"""

    global _enabled
    _enabled = value


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
    if not hasattr(app, "_wheel_guard"):
        app._wheel_guard = WheelGuard(app)
        app.installEventFilter(app._wheel_guard)
    if not hasattr(app, "_dialog_watcher"):
        app._dialog_watcher = DialogWatcher(app)
        app.installEventFilter(app._dialog_watcher)


class WheelGuard(QObject):
    """滚动页面时鼠标划过下拉框、数字框不改值：没焦点就把滚轮交给外层滚动。"""

    def eventFilter(self, obj, event) -> bool:
        if (event.type() == QEvent.Type.Wheel
                and isinstance(obj, (QComboBox, QAbstractSpinBox))
                and not obj.hasFocus()):
            # 忽略后 Qt 会继续交给父控件（滚动区）处理
            event.ignore()
            return True
        return False


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
    """按钮背景接管绘制：悬停、按下、勾选与键盘焦点四个通道平滑过渡。

    夜航所有按钮只描边不填充，主按钮是强调色描边；晨雾次按钮白底、主按钮实心强调色，悬停扫过一道高光。
    """

    _RADIUS = {"navButton": 10, "projectItem": 8, "projectItemCompact": 8, "segment": 6,
               "frameThumb": 7, "schemeThumb": 7, "projectSwitch": 10}

    def __init__(self, button: QPushButton) -> None:
        super().__init__(button)
        self.button = button
        self.kind = button.objectName() or "raised"
        refresh = button.update
        self.hover = Channel(self, refresh)
        self.press = Channel(self, refresh)
        self.checked = Channel(self, refresh, 1.0 if button.isChecked() else 0.0)
        self.focus = Channel(self, refresh)
        # 晨雾主按钮悬停时扫过的高光，0→1 为从左到右
        self.sheen = Channel(self, refresh)
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
                self.hover.to(1.0, profile().hover_in)
                if self.kind == "primary" and STYLE.filled_primary:
                    self.sheen.snap(0.0)
                    self.sheen.to(1.0, SHEEN, SOFT)
        elif kind == QEvent.Type.Leave:
            self.hover.to(0.0, profile().hover_out)
            self.press.to(0.0, profile().hover_out)
        elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.press.to(1.0, profile().press)
        elif kind == QEvent.Type.MouseButtonRelease:
            # 松开时弹回：晨雾回弹更明显
            self.press.to(0.0, profile().hover_out, spring())
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

    def _radius(self) -> float:
        return self._RADIUS.get(self.kind, STYLE.control_radius)

    def _paint(self) -> None:
        button = self.button
        rect = QRectF(button.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = self._radius()
        painter = QPainter(button)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        h, p, c, f = self.hover.value, self.press.value, self.checked.value, self.focus.value
        enabled_ = button.isEnabled()
        if p > 0.01:
            # 按下时底板按主题比例内缩（夜航 0.96、晨雾 0.94），文字不动
            shrink = (1 - STYLE.press_scale) * min(1.0, p) / 2
            rect = rect.adjusted(rect.width() * shrink, rect.height() * shrink,
                                 -rect.width() * shrink, -rect.height() * shrink)
        if self.kind in ("raised", "secondary", "primary", "square"):
            self._paint_button(painter, rect, radius, h, p, c, enabled_)
        elif self.kind == "aiFix":
            # “AI 改一改”：强调描边的次按钮（晨雾白底），悬停极淡强调色
            path = _rounded(rect, radius)
            if STYLE.filled_buttons:
                painter.fillPath(path, mix(COLORS.card, COLORS.button_hover, h))
            painter.fillPath(path, faded(COLORS.accent, 0.08 * h + 0.05 * p))
            painter.setPen(QPen(QColor(COLORS.accent_line if enabled_ else COLORS.divider), 1))
            painter.drawPath(path)
        elif self.kind == "projectSwitch":
            # 顶栏项目切换：卡片底 + 卡片描边，悬停描边提亮
            path = _rounded(rect, radius)
            painter.fillPath(path, mix(COLORS.card, COLORS.button_hover, h * 0.5))
            painter.setPen(QPen(mix(COLORS.card_border, COLORS.control_hover_border, h), 1))
            painter.drawPath(path)
        elif self.kind in ("navButton", "projectItem", "projectItemCompact"):
            self._paint_selectable(painter, rect, radius, h, p, c)
        elif self.kind == "segment":
            if enabled_ and h * (1 - c) > 0.01:
                painter.fillPath(_rounded(rect, radius), faded(flat_hover(), h * (1 - c) * 0.7))
        else:
            self._paint_flat(painter, rect, radius, h, p, c, enabled_)
        if f > 0.01:
            painter.setPen(QPen(faded(COLORS.focus_ring, f), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        painter.end()

    def _paint_button(self, painter, rect, radius, h, p, c, enabled_) -> None:
        path = _rounded(rect, radius)
        if not enabled_:
            if STYLE.filled_buttons:
                painter.fillPath(path, faded(COLORS.card, 0.6))
            painter.setPen(QPen(QColor(COLORS.divider), 1))
            painter.drawPath(path)
            return
        if self.kind == "primary":
            if STYLE.filled_primary:
                fill = mix(mix(COLORS.accent, COLORS.accent_hover, h * 0.35), COLORS.accent_pressed, p)
                painter.fillPath(path, fill)
                self._paint_sheen(painter, path, rect)
                painter.setPen(QPen(QColor(COLORS.accent_line), 1))
            else:
                # 夜航主按钮：强调色描边 + 悬停时极淡的强调色铺底和内光圈
                painter.fillPath(path, faded(COLORS.accent, 0.09 * max(h, c) + 0.05 * p))
                if h > 0.01:
                    painter.setPen(QPen(faded(COLORS.accent, 0.14 * h), 3))
                    painter.drawPath(_rounded(rect.adjusted(1.5, 1.5, -1.5, -1.5), max(1.0, radius - 1.5)))
                painter.setPen(QPen(QColor(COLORS.accent), 1))
            painter.drawPath(path)
            return
        if STYLE.filled_buttons:
            fill = mix(mix(COLORS.card, COLORS.button_hover, h), COLORS.accent_soft, c)
            painter.fillPath(path, mix(fill, COLORS.button_pressed, p))
        else:
            painter.fillPath(path, faded(COLORS.button_hover, max(h, p * 0.6)))
            if c > 0.01:
                painter.fillPath(path, faded(COLORS.accent_soft, c * 0.6))
        border = mix(COLORS.control_border, COLORS.control_hover_border, h)
        painter.setPen(QPen(mix(border, COLORS.accent_line, c), 1))
        painter.drawPath(path)

    def _paint_sheen(self, painter, path, rect) -> None:
        s = self.sheen.value
        if s <= 0.01 or s >= 0.99:
            return
        center = rect.left() + rect.width() * (-0.3 + 1.6 * s)
        band = rect.width() * 0.45
        gradient = QLinearGradient(center - band / 2, rect.top(), center + band / 2, rect.bottom())
        glow = QColor("#FFFFFF")
        glow.setAlphaF(0.0)
        gradient.setColorAt(0.0, glow)
        glow.setAlphaF(0.75 * self.hover.value)
        gradient.setColorAt(0.5, glow)
        glow.setAlphaF(0.0)
        gradient.setColorAt(1.0, glow)
        painter.save()
        painter.setClipPath(path)
        painter.fillRect(rect, QBrush(gradient))
        painter.restore()

    def _paint_flat(self, painter, rect, radius, h, p, c, enabled_) -> None:
        path = _rounded(rect, radius)
        if not enabled_:
            return
        if c > 0.01:
            if self.kind in ("frameThumb", "schemeThumb"):
                # 选中的缩略图要一眼看出来：2px 强调边框
                painter.setPen(QPen(faded(COLORS.accent if STYLE.dark else COLORS.accent_line, c), 2))
                painter.drawPath(_rounded(rect.adjusted(1, 1, -1, -1), radius))
            elif STYLE.dark:
                # 夜航开关只描边
                painter.setPen(QPen(faded(COLORS.accent_line, c), 1))
                painter.drawPath(path)
            else:
                painter.fillPath(path, faded(COLORS.accent_soft, c))
        if h * (1 - c * 0.5) > 0.01:
            painter.fillPath(path, faded(flat_hover(), h * (1 - c * 0.5)))
        if p > 0.01:
            painter.fillPath(path, faded(COLORS.button_pressed, p * 0.9))

    def _paint_selectable(self, painter, rect, radius, h, p, c) -> None:
        path = _rounded(rect, radius)
        if h * (1 - c) > 0.01:
            painter.fillPath(path, faded(flat_hover(), h * (1 - c)))
        if c > 0.01:
            if self.kind == "navButton":
                if STYLE.dark:
                    painter.setPen(QPen(faded(COLORS.accent_line, c), 1))
                    painter.drawPath(path)
                else:
                    painter.fillPath(path, faded(COLORS.card, c))
                    painter.setPen(QPen(faded(COLORS.card_border, c), 1))
                    painter.drawPath(path)
            else:
                painter.fillPath(path, faded(COLORS.project_selected, c))
        if p > 0.01:
            painter.fillPath(path, faded(COLORS.button_pressed, p * 0.6))


def flat_hover():
    """无边框按钮、列表项的悬停底色：夜航是白色 4.5% 罩层，晨雾是暖灰底。"""

    return COLORS.button_hover if STYLE.dark else COLORS.project_hover


class SegmentedFx(QObject):
    """分段控件：滑块在选项间弹簧滑动；滑块比外框内缩一圈，不贴边。"""

    INSET = 2.0

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
        if current is None:
            return QRectF()
        inner = QRectF(self.container.rect()).adjusted(self.INSET, self.INSET, -self.INSET, -self.INSET)
        return QRectF(current.geometry()).intersected(inner)

    def _moved(self, value) -> None:
        self._rect = QRectF(value)
        self.container.update()

    def _slide(self, _button) -> None:
        target = self._target()
        if not enabled() or self._rect.isEmpty() or not self.container.isVisible():
            self._animation.stop()
            self._moved(target)
            return
        restart(self._animation, QRectF(self._rect), target, SPRING_MOVE if STYLE.dark else SPRING_MOVE_BOLD,
                spring())

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            if self._animation.state() != QVariantAnimation.State.Running:
                QTimer.singleShot(0, lambda: self._moved(self._target()))
        if event.type() == QEvent.Type.Paint:
            painter = QPainter(self.container)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            outer = QRectF(self.container.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            outer_radius = STYLE.control_radius + 1
            path = _rounded(outer, outer_radius)
            painter.fillPath(path, QColor(COLORS.segment_track))
            painter.setPen(QPen(QColor(COLORS.segment_track_border), 1))
            painter.drawPath(path)
            if not self._rect.isEmpty():
                thumb = self._rect.adjusted(0.5, 0.5, -0.5, -0.5)
                thumb_radius = max(2.0, outer_radius - self.INSET)
                if not STYLE.dark:
                    # 晨雾白色滑块带一层小投影
                    painter.fillPath(_rounded(thumb.translated(0, 1), thumb_radius), faded(COLORS.shadow, 0.12))
                thumb_path = _rounded(thumb, thumb_radius)
                painter.fillPath(thumb_path, QColor(COLORS.segment_thumb))
                painter.setPen(QPen(QColor(COLORS.segment_thumb_border), 1))
                painter.drawPath(thumb_path)
            painter.end()
            return True
        return False


class SplitterFx(QObject):
    """分隔条：平时是底色缝隙，悬停浮现细线，拖动时变为强调色。"""

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
            painter.fillRect(rect, QColor(COLORS.ground))
            strength = max(self.hover.value, self.press.value)
            if strength > 0.01:
                color = mix(faded(COLORS.control_hover_border, strength), COLORS.accent_line, self.press.value)
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
        # 与样式中 14px 把手保持一致，点击位置和绘制位置对齐
        half = 7.0
        span = max(1, slider.width() - int(half * 2))
        position = QStyle.sliderPositionFromValue(slider.minimum(), slider.maximum(), slider.value(), span)
        x = half + position
        y = slider.height() / 2
        groove = QRectF(half, y - 2, slider.width() - half * 2, 4)
        painter.fillPath(_rounded(groove, 2), QColor(COLORS.control_border))
        fill = COLORS.accent if STYLE.dark else COLORS.accent_line
        painter.fillPath(_rounded(QRectF(groove.left(), groove.top(), x - groove.left(), 4), 2),
                         QColor(fill if slider.isEnabled() else COLORS.disabled))
        glow = 0.5 * self.hover.value + 0.5 * self.press.value
        if glow > 0.01:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(faded(fill, 0.25 * glow))
            halo = 7 + 4 * ease(glow)
            painter.drawEllipse(QPointF(x, y), halo, halo)
        radius = 6.5 + 1.5 * ease(self.hover.value) - 0.8 * ease(self.press.value)
        if STYLE.dark:
            painter.setPen(QPen(QColor(COLORS.card), 2))
            painter.setBrush(QColor(COLORS.text))
        else:
            painter.setPen(QPen(QColor(COLORS.control_border), 1))
            painter.setBrush(QColor(COLORS.card))
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
        radius = STYLE.control_radius
        focus = self.owner.focus.value
        base = mix(COLORS.control_border, COLORS.control_hover_border, self.owner.hover.value)
        if focus > 0.01:
            # 焦点：强调色描边 + 一圈极淡的强调色内光
            painter.setPen(QPen(faded(COLORS.focus_ring, 0.14 * focus), 3))
            painter.drawRoundedRect(rect.adjusted(1.5, 1.5, -1.5, -1.5), radius - 1.5, radius - 1.5)
        painter.setPen(QPen(mix(base, COLORS.focus_ring, focus), 1))
        painter.drawRoundedRect(rect, radius, radius)


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
            self.hover.to(1.0, profile().hover_in)
        elif kind == QEvent.Type.Leave:
            self.hover.to(0.0, profile().hover_out)
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


def fade_out_snapshot(widget: QWidget, duration: int = FADE) -> QWidget | None:
    """控件即将隐藏时留下快照淡出，逻辑上仍立即隐藏。返回淡出层，调用方可提前收掉。"""

    parent = widget.parentWidget()
    if not enabled() or parent is None or not widget.isVisible():
        return None
    return _Fade(parent, widget.grab(), widget.geometry(), duration)


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


class _Entrance(QWidget):
    """页面入场：卡片快照依次上移淡入，晨雾另加一层白雾散开。

    盖在页面上、对鼠标透明：真实控件已经就位并可点，只是这几百毫秒里看到的是快照。
    """

    def __init__(self, container: QWidget, cards: list[QWidget]) -> None:
        super().__init__(container)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(container.rect())
        self._profile = profile()
        self._ground = QColor(COLORS.ground)
        self._items = []
        for card in cards:
            if not card.isVisible() or card.width() <= 0:
                continue
            origin = card.mapTo(container, QPoint(0, 0))
            self._items.append((card.grab(), QRectF(origin.x(), origin.y(), card.width(), card.height())))
        p = self._profile
        self._total = p.card_enter + p.card_stagger * max(0, len(self._items) - 1)
        self._total = max(self._total, p.mist)
        self._elapsed = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(self._total)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(float(self._total))
        self._animation.valueChanged.connect(self._step)
        self._animation.finished.connect(self.deleteLater)
        self.show()
        self.raise_()
        self._animation.start()

    def _step(self, value) -> None:
        self._elapsed = float(value)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p = self._profile
        for order, (pixmap, rect) in enumerate(self._items):
            t = max(0.0, min(1.0, (self._elapsed - order * p.card_stagger) / p.card_enter))
            if t >= 1.0:
                continue
            e = OUT.valueForProgress(t)
            # 先用底色盖住真实卡片，再画移动中的快照
            painter.setOpacity(1.0)
            painter.fillRect(rect.adjusted(-2, -2, 2, p.card_shift + 2), self._ground)
            painter.setOpacity(e)
            painter.drawPixmap(rect.translated(0, p.card_shift * (1 - e)).toRect(), pixmap)
        if p.mist:
            m = max(0.0, min(1.0, self._elapsed / p.mist))
            veil = 1.0 - SOFT.valueForProgress(m)
            if veil > 0.01:
                painter.setOpacity(veil)
                gradient = QLinearGradient(0, 0, 0, self.height())
                gradient.setColorAt(0.0, QColor("#FFFEFB"))
                gradient.setColorAt(1.0, QColor(COLORS.ground))
                painter.fillRect(QRectF(self.rect()).translated(0, -0.03 * self.height() * (1 - veil)),
                                 QBrush(gradient))


def page_enter(container: QWidget, cards=None) -> None:
    """页面切入后调用：卡片依次进场。cards 不给时取容器里所有 objectName 为 card 的直接卡片。"""

    if not enabled() or not container.isVisible() or container.width() <= 0:
        return
    if cards is None:
        cards = [w for w in container.findChildren(QWidget, "card")
                 if w.isVisible() and not _inside_card(w, container)]
    layout = container.layout()
    if layout is not None:
        layout.activate()
    for old in container.findChildren(_Entrance):
        old.deleteLater()
    _Entrance(container, list(cards))


def _inside_card(widget: QWidget, container: QWidget) -> bool:
    parent = widget.parentWidget()
    while parent is not None and parent is not container:
        if parent.objectName() == "card":
            return True
        parent = parent.parentWidget()
    return False


class _Veil(QWidget):
    """模态对话框背后的遮罩：盖在主窗口上淡入，关闭时淡出。"""

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(host.rect())
        self._level = Channel(self, self.update)
        host.installEventFilter(self)
        self.show()
        self.raise_()
        self._level.to(1.0, profile().veil_open)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        return False

    def dismiss(self) -> None:
        self._level.to(0.0, profile().dialog_close)
        QTimer.singleShot(profile().dialog_close + 40, self, self.deleteLater)

    def paintEvent(self, _event) -> None:
        color = QColor(COLORS.scrim)
        color.setAlphaF(STYLE.scrim_alpha * max(0.0, min(1.0, self._level.value)))
        painter = QPainter(self)
        painter.fillRect(self.rect(), color)


class _WindowGhost(QWidget):
    """对话框关闭后留下的快照，原位淡出并轻微下沉。"""

    def __init__(self, pixmap, geometry) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.pixmap = pixmap
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setGeometry(geometry)
        self._fade = Channel(self, self._step, 1.0)
        self.show()
        self._fade.to(0.0, profile().dialog_close)

    def _step(self) -> None:
        self.setWindowOpacity(max(0.0, self._fade.value))
        self.update()
        if self._fade.value <= 0.001:
            self.close()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.translate(0, 4 * (1 - self._fade.value))
        painter.drawPixmap(0, 0, self.pixmap)


class DialogFx(QObject):
    """对话框：遮罩淡入，框体淡入并从下方滑到位（晨雾用弹簧、幅度更大）；关闭时快照淡出。

    隐藏事件里只撤遮罩，不碰对话框本身：对话框析构时也会先隐藏，那时再截图、改透明度会让进程崩溃
    （程序退出时还开着的对话框就是这样）。关闭快照放在 finished 信号里做，那时对话框还完好。
    """

    def __init__(self, dialog: QDialog) -> None:
        super().__init__(dialog)
        self.dialog = dialog
        self._veil: _Veil | None = None
        self._open = QVariantAnimation(self)
        self._open.valueChanged.connect(self._step)
        self._target = None
        dialog.installEventFilter(self)
        dialog.finished.connect(self._finished)

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Show and not QApplication.closingDown():
            if not enabled():
                if self.dialog.windowOpacity() < 1.0:
                    self.dialog.setWindowOpacity(1.0)
                return False
            self._target = self.dialog.pos()
            if self.dialog.isModal() and self.dialog.parentWidget() is not None:
                host = self.dialog.parentWidget().window()
                if host is not None and host.isVisible() and self._veil is None:
                    self._veil = _Veil(host)
            p = profile()
            self.dialog.setWindowOpacity(0.0)
            restart(self._open, 0.0, 1.0, p.dialog_open, OUT if STYLE.dark else spring())
        elif kind == QEvent.Type.Hide:
            self._open.stop()
            self._drop_veil()
        return False

    def _drop_veil(self) -> None:
        veil, self._veil = self._veil, None
        if veil is not None and shiboken6.isValid(veil):
            veil.dismiss()

    def _finished(self, _result) -> None:
        self._open.stop()
        self._drop_veil()
        dialog = self.dialog
        if enabled() and not QApplication.closingDown() and dialog.width() > 0:
            _WindowGhost(dialog.grab(), dialog.frameGeometry())
        dialog.setWindowOpacity(1.0)

    def _step(self, value) -> None:
        v = float(value)
        self.dialog.setWindowOpacity(max(0.0, min(1.0, v * 1.6)))
        if self._target is not None:
            shift = profile().dialog_shift * (1 - v)
            self.dialog.move(self._target.x(), int(round(self._target.y() + shift)))


class DialogWatcher(QObject):
    """应用级：任何对话框第一次显示时挂上 DialogFx。"""

    def eventFilter(self, obj, event) -> bool:
        if (event.type() == QEvent.Type.Show and isinstance(obj, QDialog) and not QApplication.closingDown()
                and not obj.property("motionDialog")):
            obj.setProperty("motionDialog", True)
            fx = DialogFx(obj)
            fx.eventFilter(obj, event)
        return False


class ProgressFx(QObject):
    """进度条扫光：有进度、还没满时一道高光循环扫过；夜航淡、晨雾亮。"""

    def __init__(self, bar: QProgressBar) -> None:
        super().__init__(bar)
        self.bar = bar
        self.overlay = _SheenLayer(self)
        self.overlay.setGeometry(bar.rect())
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self.overlay.update)
        self._started = time.monotonic()
        bar.installEventFilter(self)
        bar.valueChanged.connect(lambda _value: self._sync())

    def active(self) -> bool:
        bar = self.bar
        return (enabled() and bar.isVisible() and bar.maximum() > bar.minimum()
                and bar.minimum() < bar.value() < bar.maximum())

    def _sync(self) -> None:
        if self.active():
            if not self._timer.isActive():
                self._started = time.monotonic()
                self._timer.start()
        else:
            self._timer.stop()
            self.overlay.update()

    def phase(self) -> float:
        period = profile().sheen_period / 1000
        return ((time.monotonic() - self._started) % period) / period

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self.overlay.setGeometry(self.bar.rect())
            self.overlay.raise_()
        elif event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            QTimer.singleShot(0, self, self._sync)
        return False


class _SheenLayer(QWidget):
    def __init__(self, owner: ProgressFx) -> None:
        super().__init__(owner.bar)
        self.owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.show()

    def paintEvent(self, _event) -> None:
        if not self.owner.active():
            return
        bar = self.owner.bar
        fraction = (bar.value() - bar.minimum()) / max(1, bar.maximum() - bar.minimum())
        filled = QRectF(0, 0, self.width() * fraction, self.height())
        if filled.width() < 4:
            return
        band = 60.0
        x = -band + (filled.width() + band * 2) * self.owner.phase()
        gradient = QLinearGradient(x, 0, x + band, 0)
        glow = QColor("#FFFFFF")
        strength = 0.45 if STYLE.dark else 0.7
        for stop, alpha in ((0.0, 0.0), (0.5, strength), (1.0, 0.0)):
            glow.setAlphaF(alpha)
            gradient.setColorAt(stop, glow)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipPath(_rounded(filled, filled.height() / 2))
        painter.fillRect(filled, QBrush(gradient))


class Skeleton(QWidget):
    """加载占位块：圆角底色 + 循环扫光；AI 方案生成时占位用。delay 让几块错开。"""

    def __init__(self, parent: QWidget | None = None, delay: int = 0, radius: float = 7.0) -> None:
        super().__init__(parent)
        self.radius = radius
        self.delay = delay / 1000
        self._started = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self.update)

    def showEvent(self, event) -> None:
        self._started = time.monotonic()
        if enabled():
            self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = _rounded(rect, self.radius)
        painter.fillPath(path, QColor(COLORS.raised))
        if not enabled():
            return
        period = profile().sheen_period / 1000
        phase = ((time.monotonic() - self._started - self.delay) % period) / period
        band = max(40.0, rect.width() * 0.5)
        x = rect.left() - band + (rect.width() + band * 2) * phase
        gradient = QLinearGradient(x, 0, x + band, 0)
        glow = QColor(COLORS.highlight if STYLE.dark else "#FFFFFF")
        for stop, alpha in ((0.0, 0.0), (0.5, 0.9 if STYLE.dark else 0.8), (1.0, 0.0)):
            glow.setAlphaF(alpha)
            gradient.setColorAt(stop, glow)
        painter.setClipPath(path)
        painter.fillRect(rect, QBrush(gradient))


def pulse(widget: QWidget, rect: QRectF | None = None, color=None) -> None:
    """命中提示：在 widget 上一圈强调色光圈向外扩散后消失（查找命中、定位到某句时用）。"""

    if not enabled() or not widget.isVisible():
        return
    _Pulse(widget, rect or QRectF(widget.rect()), color or COLORS.accent)


class _Pulse(QWidget):
    def __init__(self, host: QWidget, rect: QRectF, color) -> None:
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(host.rect())
        self.target = QRectF(rect)
        self.color = QColor(color)
        self._t = Channel(self, self.update)
        self._t.value = 0.0
        self.show()
        self.raise_()
        self._t.to(1.0, 700, OUT)
        QTimer.singleShot(760, self, self.deleteLater)

    def paintEvent(self, _event) -> None:
        t = self._t.value
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        spread = 7 * t
        color = QColor(self.color)
        color.setAlphaF(0.7 * (1 - t))
        painter.setPen(QPen(color, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(self.target.adjusted(-spread, -spread, spread, spread), 4 + spread, 4 + spread)


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
    for bar in root.findChildren(QProgressBar):
        if not bar.property("motion"):
            bar.setProperty("motion", True)
            ProgressFx(bar)
    for area in root.findChildren(QAbstractScrollArea):
        if not area.property("motion") and area.verticalScrollBar() is not None:
            area.setProperty("motion", True)
            ScrollFx(area)


__all__ = [
    "ButtonFx", "Channel", "DialogFx", "KeyedAnimator", "MORNING", "NIGHT", "Profile", "ProgressFx",
    "SegmentedFx", "Skeleton", "ValueGlide", "crossfade", "enabled", "fade_in", "fade_out_snapshot", "faded",
    "flat_hover", "install_app", "install_tree", "mix", "page_enter", "profile", "pulse", "reveal",
    "set_enabled", "smooth_scroll", "spring", "stagger_in",
]
