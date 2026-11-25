"""消息框换皮：QMessageBox（含 question / warning 这类静态函数弹的）改成设计稿的确认框卡片。

调用处不用改：应用级监视器在消息框第一次打磨（Polish，显示前）时套上。
标题从系统标题栏挪进卡片，前面一颗四角星（询问 / 提示用强调色，警告用警示色）；正文灰字、1.7 倍行高；
按钮靠右，取消在左、确认在右，默认按钮用主按钮样式。窗口标题保留给读屏和任务栏。
"""

from __future__ import annotations

import html
import re

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtGui import Qt as GuiQt
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QMessageBox, QPushButton, QSpacerItem

from . import popups
from .theme import COLORS, STYLE

# 投影留白：左、上、右、下（光源在上，下侧多留）
SHADOW = (12, 8, 12, 16)
# 卡片内边距：左、上、右、下（设计稿 22 22 18）
PAD = (22, 22, 22, 18)
WIDTH = 420
ICON = 16
ICON_GAP = 10
LINE = 22
# 标准按钮译文带的助记符：“是(&Y)”“否(&N)”
_MNEMONIC = re.compile(r"\s*\(&?[A-Za-z]\)$")
# 设计稿四角星（24 格坐标）
_STAR = ((12, 3), (13.8, 8.2), (19, 10), (13.8, 11.8), (12, 17), (10.2, 11.8), (5, 10), (10.2, 8.2))


def apply(box: QMessageBox) -> MessageSkin | None:
    """给消息框换皮；已换过返回 None。须在显示前（Polish 时）调用。"""

    if box.property("messageSkin"):
        return None
    box.setProperty("messageSkin", True)
    return MessageSkin(box)


class MessageSkin(QObject):
    def __init__(self, box: QMessageBox) -> None:
        super().__init__(box)
        self.box = box
        icon = box.icon()
        self.color = (COLORS.danger if icon == QMessageBox.Icon.Critical else
                      COLORS.warning if icon == QMessageBox.Icon.Warning else COLORS.accent_ink)
        title, body, extra = box.windowTitle(), box.text(), box.informativeText()
        if extra:
            body = f"{body}\n\n{extra}" if body else extra
        self.has_title = bool(title.strip())
        # 先设透明再改窗口标志：改标志会重建原生窗口，透明要赶在重建前，否则投影留白是黑的
        box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        box.setWindowFlags(box.windowFlags() | Qt.WindowType.FramelessWindowHint
                           | Qt.WindowType.NoDropShadowWindowHint)
        # 换图标、补说明都会让 QMessageBox 重排一次布局，所以布局细节放最后
        box.setIcon(QMessageBox.Icon.NoIcon)
        if self.has_title:
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setText(title)
            box.setInformativeText(body)
        else:
            box.setText(body)
        # 按钮改名要在设样式表之前：设样式表时会重新套一遍子控件，之后再改名不生效
        default = box.defaultButton() or self._pick_default()
        if default is not None and default is not box.defaultButton():
            box.setDefaultButton(default)
        for button in box.findChildren(QPushButton):
            button.setText(_MNEMONIC.sub("", button.text()))
            if button is default:
                button.setObjectName("primary")
        box.setStyleSheet(
            "QMessageBox { background: transparent; }"
            f"QLabel#qt_msgbox_label {{ color: {COLORS.text if self.has_title else COLORS.muted};"
            f" font-size: {15 if self.has_title else 13}px; font-weight: {600 if self.has_title else 400}; }}"
            f"QLabel#qt_msgbox_informativelabel {{ color: {COLORS.muted}; font-size: 13px; }}"
            # 1 = MacLayout：整体靠右，取消 / 否在左、是 / 接受在最右，危险操作单独靠左
            # （样式表不认 5 = AndroidLayout）
            "QDialogButtonBox { button-layout: 1; }"
            "QDialogButtonBox QPushButton { min-height: 34px; max-height: 34px; padding: 0 14px;"
            " border-radius: 8px; font-size: 13px; }"
            "QDialogButtonBox QPushButton#primary { padding: 0 16px; }"
        )
        self._layout(body)
        box.installEventFilter(self)

    def _pick_default(self) -> QPushButton | None:
        """没指定默认按钮时补一个，回车落点与原来一致。

        question() 只给“接受”角色设默认，“是”不算；原来靠系统按钮顺序“是”在最前、回车就是“是”。
        换成“取消在左、确认在右”后顺序变了，所以显式指定：先找是 / 接受，没有就取第一个不是取消的。
        """

        box = self.box
        buttons = [button for button in box.findChildren(QPushButton) if box.buttonRole(button)
                   != QMessageBox.ButtonRole.InvalidRole]
        sure = (QMessageBox.ButtonRole.YesRole, QMessageBox.ButtonRole.AcceptRole)
        quiet = (QMessageBox.ButtonRole.RejectRole, QMessageBox.ButtonRole.NoRole)
        return (next((button for button in buttons if box.buttonRole(button) in sure), None)
                or next((button for button in buttons if box.buttonRole(button) not in quiet), None))

    def _layout(self, body: str) -> None:
        box = self.box
        grid = box.layout()
        if grid is None:
            return
        left, top, right, bottom = SHADOW
        grid.setContentsMargins(left + PAD[0], top + PAD[1], right + PAD[2], bottom + PAD[3])
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(12)
        # QMessageBox 在文字左边塞了一列 15px 的缩进，卡片里不要
        for index in range(grid.count()):
            spacer = grid.itemAt(index).spacerItem()
            if isinstance(spacer, QSpacerItem):
                spacer.changeSize(0, 0)
        grid.invalidate()
        width = WIDTH - PAD[0] - PAD[2]
        label = box.findChild(QLabel, "qt_msgbox_label")
        if label is not None:
            label.setContentsMargins(ICON + ICON_GAP if self.has_title else 0, 0, 0, 0)
            if not self.has_title:
                label.setWordWrap(True)
                label.setMinimumWidth(width)
        info = box.findChild(QLabel, "qt_msgbox_informativelabel")
        if info is not None:
            info.setContentsMargins(0, 0, 0, 0)
            info.setWordWrap(True)
            info.setMinimumWidth(width)
            if body and not GuiQt.mightBeRichText(body):
                # 纯文字转成带行高的富文本，换行照旧。行高按设计稿 13px × 1.7 定死：
                # Qt 的百分比行高按字体行高算，中文字体行高本身约 1.5 倍字号，会放得过头
                info.setTextFormat(Qt.TextFormat.RichText)
                info.setText(f'<div style="line-height:{LINE}px">'
                             + html.escape(body).replace("\n", "<br>") + "</div>")
        buttons = box.findChild(QDialogButtonBox)
        if buttons is not None:
            buttons.setContentsMargins(0, 6, 0, 0)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.box and event.type() == QEvent.Type.Paint:
            self._paint()
            return True
        return False

    def _paint(self) -> None:
        box = self.box
        painter = QPainter(box)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, top, right, bottom = SHADOW
        card = QRectF(box.rect()).adjusted(left + .5, top + .5, -right - .5, -bottom - .5)
        popups.paint_panel(painter, card, STYLE.dialog_radius, fill=COLORS.dialog)
        label = box.findChild(QLabel, "qt_msgbox_label")
        if self.has_title and label is not None and label.isVisible():
            geometry = label.geometry()
            line = label.fontMetrics().height()
            scale = ICON / 24
            origin = QPointF(geometry.left(), geometry.top() + (line - ICON) / 2)
            path = QPainterPath()
            for index, (x, y) in enumerate(_STAR):
                point = origin + QPointF(x * scale, y * scale)
                path.moveTo(point) if index == 0 else path.lineTo(point)
            path.closeSubpath()
            pen = QPen(QColor(self.color), max(1.2, 1.6 * scale))
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.end()
