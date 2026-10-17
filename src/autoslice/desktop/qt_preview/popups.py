"""带动效的弹出层：下拉框列表与右键菜单共用圆角面板、柔和投影和高亮过渡。"""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QMenu, QWidget

from . import motion
from .theme import COLORS

# 投影留白：左、上、右、下；光源在上方，下侧留得更多
SHADOW = (10, 6, 10, 14)
OPEN = 160
CLOSE = 120
ITEM_IN = 90
ITEM_OUT = 180
_SHORTCUT = re.compile(r"^(.*?)\s+((?:Ctrl\+|Shift\+|Alt\+)*(?:[A-Z0-9]|Delete|Del|Space|Enter|F\d{1,2}))$")


def paint_panel(painter: QPainter, rect: QRectF, radius: float = 8) -> None:
    """层叠圆角矩形模拟柔和投影，再画浮层面板与顶边高光。"""

    painter.setPen(Qt.PenStyle.NoPen)
    for spread in range(8, 0, -1):
        painter.setBrush(QColor(0, 0, 0, int(255 * 0.022 * (9 - spread))))
        painter.drawRoundedRect(rect.adjusted(-spread, -spread + 3, spread, spread + 3),
                                radius + spread, radius + spread)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.fillPath(path, QColor(COLORS.overlay))
    painter.setPen(motion._edge_pen(rect, COLORS.highlight, COLORS.border))
    painter.drawPath(path)


def paint_check(painter: QPainter, center: QPointF, color) -> None:
    pen = QPen(QColor(color), 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath(QPointF(center.x() - 4, center.y()))
    path.lineTo(center.x() - 1.2, center.y() + 2.8)
    path.lineTo(center.x() + 4, center.y() - 3)
    painter.drawPath(path)


def paint_row(painter: QPainter, rect: QRectF, hover: float) -> None:
    if hover > 0.01:
        path = QPainterPath()
        path.addRoundedRect(rect, 5, 5)
        painter.fillPath(path, motion.faded(COLORS.accent, hover))


class _Ghost(QWidget):
    """弹层关闭后留下的快照，原位淡出。"""

    def __init__(self, pixmap, geometry: QRect) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.pixmap = pixmap
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setGeometry(geometry)
        self._fade = motion.Channel(self, self._step, 1.0)
        self.show()
        self._fade.to(0.0, CLOSE)

    def _step(self) -> None:
        self.setWindowOpacity(self._fade.value)
        if self._fade.value <= 0.001:
            self.close()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        # 关闭时轻微上浮，与打开时下滑方向呼应
        painter.translate(0, -3 * (1 - self._fade.value))
        painter.drawPixmap(0, 0, self.pixmap)


def ghost(widget: QWidget) -> None:
    if motion.enabled() and widget.isVisible():
        _Ghost(widget.grab(), widget.geometry())


class ComboPopup(QWidget):
    """下拉列表：淡入并轻微滑出，悬停高亮渐变，当前项以对勾标出。"""

    ROW = 30
    PAD = 4

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.combo = combo
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setFont(combo.font())
        self.hover = combo.currentIndex()
        self._closing = False
        self._above = False
        self._rows = motion.KeyedAnimator(self, self.update)
        self._open = motion.Channel(self, self._step)
        metrics = QFontMetrics(self.font())
        widest = max((metrics.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())), default=0)
        self._body = QRectF(SHADOW[0], SHADOW[1], max(combo.width(), widest + 56),
                            combo.count() * self.ROW + self.PAD * 2)
        self.resize(int(self._body.width()) + SHADOW[0] + SHADOW[2],
                    int(self._body.height()) + SHADOW[1] + SHADOW[3])
        self._place()

    def _place(self) -> None:
        below = self.combo.mapToGlobal(QPoint(0, self.combo.height() + 4))
        screen = QGuiApplication.screenAt(below) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        top = below.y() - SHADOW[1]
        if top + self.height() > area.bottom():
            self._above = True
            top = self.combo.mapToGlobal(QPoint(0, -4)).y() - int(self._body.height()) - SHADOW[1]
        left = min(max(area.left(), below.x() - SHADOW[0]), area.right() - self.width())
        self.move(left, top)

    def popup(self) -> None:
        self.setWindowOpacity(0.0 if motion.enabled() else 1.0)
        self.show()
        self.setFocus()
        self._open.to(1.0, OPEN)

    def _step(self) -> None:
        self.setWindowOpacity(self._open.value)
        self.update()
        if self._closing and self._open.value <= 0.001:
            self.close()

    def dismiss(self) -> None:
        if self._closing:
            return
        # 顶层窗口切换鼠标穿透会重建原生窗口并立即关闭，这里只用标记忽略输入
        self._closing = True
        if not motion.enabled():
            self.close()
            return
        self._open.to(0.0, CLOSE)

    def _row_rect(self, index: int) -> QRectF:
        return QRectF(self._body.left() + self.PAD, self._body.top() + self.PAD + index * self.ROW,
                      self._body.width() - self.PAD * 2, self.ROW)

    def _index_at(self, point) -> int:
        for index in range(self.combo.count()):
            if self._row_rect(index).contains(QPointF(point)):
                return index
        return -1

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 从下拉框方向滑出
        painter.translate(0, (1 - self._open.value) * (5 if self._above else -5))
        paint_panel(painter, self._body)
        current = self.combo.currentIndex()
        for index in range(self.combo.count()):
            rect = self._row_rect(index)
            active = index == self.hover
            h = self._rows.value(index, 1.0 if active else 0.0, ITEM_IN if active else ITEM_OUT)
            paint_row(painter, rect, h)
            selected = index == current
            base = COLORS.accent_text if selected else COLORS.text
            painter.setPen(motion.mix(base, "#FFFFFF", h))
            painter.drawText(rect.adjusted(12, 0, -30, 0), Qt.AlignmentFlag.AlignVCenter,
                             self.combo.itemText(index))
            if selected:
                paint_check(painter, QPointF(rect.right() - 15, rect.center().y()),
                            motion.mix(COLORS.accent_text, "#FFFFFF", h))

    def mouseMoveEvent(self, event) -> None:
        if self._closing:
            return
        index = self._index_at(event.position())
        if index != self.hover and index >= 0:
            self.hover = index
            self.update()

    def leaveEvent(self, _event) -> None:
        self.hover = -1
        self.update()

    def mousePressEvent(self, event) -> None:
        if not self.rect().contains(event.position().toPoint()) or self._index_at(event.position()) < 0:
            # 点在外面：带动画收起，而不是瞬间消失
            self.dismiss()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        index = self._index_at(event.position())
        if index >= 0 and not self._closing:
            self._choose(index)

    def keyPressEvent(self, event) -> None:
        if self._closing:
            return
        key = event.key()
        count = self.combo.count()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and count:
            step = -1 if key == Qt.Key.Key_Up else 1
            self.hover = (max(0, self.hover) + step) % count if self.hover >= 0 else self.combo.currentIndex()
            self.update()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self.hover >= 0:
            self._choose(self.hover)
        elif key in (Qt.Key.Key_Escape, Qt.Key.Key_F4):
            self.dismiss()

    def _choose(self, index: int) -> None:
        self.combo.setCurrentIndex(index)
        self.combo.activated.emit(index)
        self.dismiss()


class ComboBox(QComboBox):
    """接口与 QComboBox 一致，只替换弹出层。"""

    def showPopup(self) -> None:
        if not self.count():
            return
        existing = getattr(self, "_motion_popup", None)
        if existing is not None and existing.isVisible():
            existing.dismiss()
            return
        self._motion_popup = ComboPopup(self)
        self._motion_popup.destroyed.connect(lambda: setattr(self, "_motion_popup", None))
        self._motion_popup.popup()

    def hidePopup(self) -> None:
        popup = getattr(self, "_motion_popup", None)
        if popup is not None:
            popup.dismiss()


class MenuFx(QObject):
    """右键菜单：接管绘制，保留 QMenu 的键盘导航与行为。"""

    def __init__(self, menu: QMenu) -> None:
        super().__init__(menu)
        self.menu = menu
        menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        left, top, right, bottom = SHADOW
        menu.setStyleSheet(
            f"QMenu {{ background: transparent; border: none; padding: {top + 4}px {right + 4}px "
            f"{bottom + 4}px {left + 4}px; }}"
            "QMenu::item { padding: 6px 64px 6px 12px; }"
            "QMenu::separator { height: 9px; }"
        )
        self._rows = motion.KeyedAnimator(self, menu.update)
        self._open = motion.Channel(self, self._step)
        menu.aboutToShow.connect(self._showing)
        menu.aboutToHide.connect(lambda: ghost(menu))
        menu.installEventFilter(self)

    def popup(self, position: QPoint) -> None:
        """exec 对齐可见面板而不是投影留白。"""

        self.menu.exec(position - QPoint(SHADOW[0], SHADOW[1]))

    def _showing(self) -> None:
        self._open.snap(0.0)
        self.menu.setWindowOpacity(0.0 if motion.enabled() else 1.0)
        self._open.to(1.0, OPEN)

    def _step(self) -> None:
        self.menu.setWindowOpacity(self._open.value)
        self.menu.update()

    def eventFilter(self, obj, event) -> bool:
        if event.type() != QEvent.Type.Paint:
            return False
        menu = self.menu
        painter = QPainter(menu)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(0, (1 - self._open.value) * -5)
        left, top, right, bottom = SHADOW
        body = QRectF(menu.rect()).adjusted(left, top, -right, -bottom)
        paint_panel(painter, body)
        active = menu.activeAction()
        for action in menu.actions():
            if not action.isVisible():
                continue
            geometry = QRectF(menu.actionGeometry(action))
            row = QRectF(body.left() + 4, geometry.top(), body.width() - 8, geometry.height())
            if action.isSeparator():
                painter.setPen(QPen(QColor(COLORS.divider), 1))
                y = row.center().y()
                painter.drawLine(QPointF(row.left() + 8, y), QPointF(row.right() - 8, y))
                continue
            enabled = action.isEnabled()
            on = enabled and action is active
            h = self._rows.value(id(action), 1.0 if on else 0.0, ITEM_IN if on else ITEM_OUT)
            paint_row(painter, row, h)
            match = _SHORTCUT.match(action.text().replace("&", ""))
            label, shortcut = (match.group(1), match.group(2)) if match else (action.text().replace("&", ""), "")
            text = motion.mix(COLORS.text if enabled else COLORS.disabled, "#FFFFFF", h)
            painter.setPen(text)
            painter.drawText(row.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter, label)
            trailing = row.right() - 12
            if action.isCheckable() and action.isChecked():
                paint_check(painter, QPointF(trailing - 4, row.center().y()),
                            motion.mix(COLORS.accent_text, "#FFFFFF", h))
                trailing -= 20
            if shortcut:
                painter.setPen(motion.mix(COLORS.subtle if enabled else COLORS.disabled,
                                          motion.faded("#FFFFFF", 0.8), h))
                painter.drawText(QRectF(row.left(), row.top(), trailing - row.left(), row.height()),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, shortcut)
        painter.end()
        return True
