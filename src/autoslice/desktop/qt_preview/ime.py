"""多行输入框的输入法位置修正。

QPlainTextEdit 回答输入法“光标在哪”时给的是视口坐标，Qt 却按整个输入框的位置换算到窗口；
输入框有边框和内边距时（我们的样式表都有），候选窗会往左上偏一个内边距，压住正在组的字。
这里把视口在输入框里的偏移补上。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtWidgets import QPlainTextEdit

_RECTS = (Qt.InputMethodQuery.ImCursorRectangle, Qt.InputMethodQuery.ImAnchorRectangle)


class ImeTextEdit(QPlainTextEdit):
    def inputMethodQuery(self, query, argument=None):  # noqa: N802 - Qt 命名
        value = super().inputMethodQuery(query) if argument is None else super().inputMethodQuery(query, argument)
        if query in _RECTS:
            offset = self.viewport().pos()
            if isinstance(value, QRectF):
                return value.translated(QPointF(offset))
            if isinstance(value, QRect):
                return value.translated(offset)
        return value
