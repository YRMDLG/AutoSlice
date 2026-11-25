"""带动效的弹出层：下拉框列表与右键菜单共用圆角面板、柔和投影和高亮过渡。"""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QComboBox, QMenu, QWidget

from . import motion
from .panel import paint_panel
from .theme import COLORS, MONO_FAMILIES

# 投影留白：左、上、右、下；光源在上方，下侧留得更多
SHADOW = (10, 6, 10, 14)
OPEN = 160
CLOSE = 120
ITEM_IN = 90
ITEM_OUT = 180
_SHORTCUT = re.compile(r"^(.*?)\s+((?:Ctrl\+|Shift\+|Alt\+)*(?:[A-Z0-9/,.\[\]=;-]|Delete|Del|Space|Enter|F\d{1,2}))$")


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
        path.addRoundedRect(rect, 8, 8)
        painter.fillPath(path, motion.faded(motion.flat_hover(), hover))


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
        self._fade.to(0.0, motion.profile().menu_close)

    def _step(self) -> None:
        self.setWindowOpacity(self._fade.value)
        if self._fade.value <= 0.001:
            self.close()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        # 关闭时轻微上浮，与打开时下滑方向呼应
        painter.translate(0, -3 * (1 - self._fade.value))
        painter.drawPixmap(0, 0, self.pixmap)


def _row_progress(open_value: float, order: int) -> float:
    """弹层选项逐个错开出现：第 order 项在整体进度走过一段之后才开始。"""

    profile = motion.profile()
    k = min(order, 8) * profile.menu_item_delay / max(1, profile.menu_open)
    return max(0.0, min(1.0, (open_value - k) / max(0.2, 1 - k)))


def _enter_transform(painter: QPainter, body: QRectF, value: float, closing: bool, above: bool = False) -> None:
    """弹层进场：夜航下移 + 0.97 缩放淡入；晨雾从上往下展开（裁剪），幅度更大。关闭时只淡出上浮。"""

    profile = motion.profile()
    direction = 1 if above else -1
    painter.translate(0, (1 - value) * direction * profile.menu_shift)
    if closing:
        return
    scale = profile.menu_scale + (1 - profile.menu_scale) * value
    origin = QPointF(body.center().x(), body.bottom() if above else body.top())
    painter.translate(origin)
    painter.scale(scale, scale)
    painter.translate(-origin)
    if profile.menu_clip:
        height = body.height() * max(0.0, min(1.0, value)) + 24
        clip = QRectF(body.left() - 12, body.bottom() - height if above else body.top() - 12,
                      body.width() + 24, height + 12)
        painter.setClipRect(clip)


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
        self._open.to(1.0, motion.profile().menu_open, motion.OUT)

    def _step(self) -> None:
        self.setWindowOpacity(max(0.0, min(1.0, self._open.value * 1.4)))
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
        self._open.to(0.0, motion.profile().menu_close)

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
        _enter_transform(painter, self._body, self._open.value, self._closing, self._above)
        paint_panel(painter, self._body)
        current = self.combo.currentIndex()
        for index in range(self.combo.count()):
            rect = self._row_rect(index)
            row = 1.0 if self._closing else _row_progress(self._open.value, index)
            painter.setOpacity(row)
            painter.save()
            painter.translate(0, (1 - row) * -motion.profile().row_shift)
            active = index == self.hover
            h = self._rows.value(index, 1.0 if active else 0.0, ITEM_IN if active else ITEM_OUT)
            paint_row(painter, rect, h)
            selected = index == current
            base = COLORS.accent_ink if selected else COLORS.text_secondary
            painter.setPen(motion.mix(base, COLORS.accent_ink if selected else COLORS.text, h))
            painter.drawText(rect.adjusted(12, 0, -30, 0), Qt.AlignmentFlag.AlignVCenter,
                             self.combo.itemText(index))
            if selected:
                paint_check(painter, QPointF(rect.right() - 15, rect.center().y()), COLORS.accent_ink)
            painter.restore()

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


def submenu(parent: QMenu, title: str) -> QMenu:
    """带动效的子菜单。

    不用 parent.addMenu(title)：那样子菜单由 Qt 在 C++ 里新建，PySide 不知道它何时被删，
    下次在同一地址新建的菜单会拿到失效的旧包装，动效一碰就报“C++ 对象已删除”，子菜单弹不出来。
    """

    menu = QMenu(title, parent)
    MenuFx(menu)
    parent.addMenu(menu)
    return menu


class MenuFx(QObject):
    """右键菜单：接管绘制，保留 QMenu 的键盘导航与行为。"""

    def __init__(self, menu: QMenu, two_line: bool = False) -> None:
        super().__init__(menu)
        self.menu = menu
        # 两行菜单项（设计稿“更多”“AI 检查”菜单）：上面名称，下面一行小字说明（action 的 sub 属性）
        self.two_line = two_line
        self._above = False
        menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        left, top, right, bottom = SHADOW
        # 单行项 32px 高（设计稿右键菜单）；分隔线连上下留白共 9px
        item = "15px 24px 15px 12px" if two_line else "7px 64px 8px 12px"
        menu.setStyleSheet(
            f"QMenu {{ background: transparent; border: none; padding: {top + 4}px {right + 4}px "
            f"{bottom + 4}px {left + 4}px; }}"
            f"QMenu::item {{ padding: {item}; }}"
            "QMenu::separator { height: 1px; margin: 4px 0px; }"
        )
        # 样式表的下内边距 QMenu 不认（上下都按上边距算），最后一项会压到面板下沿外：差额用内容边距补
        menu.setContentsMargins(0, 0, 0, bottom - top)
        if two_line:
            menu.setMinimumWidth(268 + left + right + 8)
        self._rows = motion.KeyedAnimator(self, menu.update)
        self._open = motion.Channel(self, self._step)
        # popup() 已自行让出投影留白；按钮 setMenu 弹出时由 Show 事件补上
        self._manual = False
        menu.aboutToShow.connect(self._showing)
        menu.aboutToHide.connect(self._hidden)
        menu.installEventFilter(self)

    @staticmethod
    def popup_below(menu: QMenu, anchor: QWidget, align_right: bool = False) -> None:
        """在按钮下方弹出（右对齐时面板右缘对齐按钮右缘）。"""

        size = menu.sizeHint()
        x = anchor.width() - size.width() + SHADOW[2] if align_right else -SHADOW[0]
        menu.popup(anchor.mapToGlobal(QPoint(x, anchor.height() + 6 - SHADOW[1])))

    @staticmethod
    def popup_above(menu: QMenu, anchor: QWidget) -> None:
        """在按钮上方弹出（左缘对齐按钮），从下往上展开。"""

        fx = next((child for child in menu.children() if isinstance(child, MenuFx)), None)
        if fx is not None:
            fx._above = True
        size = menu.sizeHint()
        menu.popup(anchor.mapToGlobal(QPoint(-SHADOW[0], -size.height() - 6 + SHADOW[3])))

    def popup(self, position: QPoint) -> None:
        """exec 对齐可见面板而不是投影留白。"""

        self._manual = True
        self.menu.exec(position - QPoint(SHADOW[0], SHADOW[1]))

    def _hidden(self) -> None:
        self._manual = False
        ghost(self.menu)

    def _showing(self) -> None:
        self._open.snap(0.0)
        self.menu.setWindowOpacity(0.0 if motion.enabled() else 1.0)
        self._open.to(1.0, motion.profile().menu_open, motion.OUT)

    def _step(self) -> None:
        self.menu.setWindowOpacity(max(0.0, min(1.0, self._open.value * 1.4)))
        self.menu.update()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Show and not self._manual:
            # 可见面板对齐按钮左缘，与按钮留 4px 间隙
            self.menu.move(self.menu.pos() - QPoint(SHADOW[0], SHADOW[1] - 4))
            return False
        if event.type() != QEvent.Type.Paint:
            return False
        menu = self.menu
        painter = QPainter(menu)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, top, right, bottom = SHADOW
        body = QRectF(menu.rect()).adjusted(left, top, -right, -bottom)
        _enter_transform(painter, body, self._open.value, False, self._above)
        paint_panel(painter, body)
        active = menu.activeAction()
        order = -1
        for action in menu.actions():
            if not action.isVisible():
                continue
            order += 1
            painter.setOpacity(_row_progress(self._open.value, order))
            geometry = QRectF(menu.actionGeometry(action))
            row = QRectF(body.left() + 4, geometry.top(), body.width() - 8, geometry.height())
            if action.isSeparator():
                painter.setPen(QPen(QColor(COLORS.divider), 1))
                y = row.center().y()
                painter.drawLine(QPointF(row.left() + 8, y), QPointF(row.right() - 8, y))
                continue
            enabled = action.isEnabled()
            if action.property("heading"):
                # 菜单头（第几条 · 时间）：等宽小字，不可选
                font = QFont(menu.font())
                font.setFamilies(list(MONO_FAMILIES))
                font.setPixelSize(11)
                painter.setFont(font)
                painter.setPen(QColor(COLORS.subtle))
                painter.drawText(row.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter, action.text())
                painter.setFont(menu.font())
                continue
            on = enabled and action is active
            h = self._rows.value(id(action), 1.0 if on else 0.0, ITEM_IN if on else ITEM_OUT)
            paint_row(painter, row, h)
            match = _SHORTCUT.match(action.text().replace("&", ""))
            label, shortcut = (match.group(1), match.group(2)) if match else (action.text().replace("&", ""), "")
            base = COLORS.text_secondary if enabled else COLORS.disabled
            if enabled and action.property("danger"):
                base = COLORS.danger
            text = motion.mix(base, COLORS.danger if action.property("danger") else COLORS.text, h)
            painter.setPen(text)
            sub = action.property("sub") if self.two_line else None
            if sub:
                font = QFont(menu.font())
                font.setPixelSize(13)
                painter.setFont(font)
                painter.drawText(QRectF(row.left() + 12, row.top() + 6, row.width() - 24, 19),
                                 Qt.AlignmentFlag.AlignVCenter, label)
                font.setPixelSize(11)
                painter.setFont(font)
                painter.setPen(QColor(COLORS.subtle if enabled else COLORS.disabled))
                painter.drawText(QRectF(row.left() + 12, row.top() + 25, row.width() - 24, 16),
                                 Qt.AlignmentFlag.AlignVCenter, str(sub))
                painter.setFont(menu.font())
                continue
            painter.drawText(row.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter, label)
            trailing = row.right() - 12
            if action.menu() is not None:
                pen = QPen(QColor(COLORS.subtle if enabled else COLORS.disabled), 1.5)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                cx, cy = trailing - 2, row.center().y()
                painter.drawPolyline([QPointF(cx - 4, cy - 4), QPointF(cx, cy), QPointF(cx - 4, cy + 4)])
                trailing -= 16
            if action.isCheckable() and action.isChecked():
                paint_check(painter, QPointF(trailing - 4, row.center().y()), COLORS.accent_ink)
                trailing -= 20
            if shortcut:
                # 快捷键：等宽 11px 浅字靠右（设计稿）
                font = QFont(menu.font())
                font.setFamilies(list(MONO_FAMILIES))
                font.setPixelSize(11)
                painter.setFont(font)
                painter.setPen(QColor(COLORS.subtle if enabled else COLORS.disabled))
                painter.drawText(QRectF(row.left(), row.top(), trailing - row.left(), row.height()),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, shortcut)
                painter.setFont(menu.font())
        painter.end()
        return True
