"""浮层面板底板：层叠圆角矩形模拟柔和投影，再画面板与描边。

菜单、下拉、提示条和确认框共用；只依赖主题令牌，不依赖动效（确认框换皮也用它，避免模块成环）。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

from .theme import COLORS, STYLE


def paint_panel(painter: QPainter, rect: QRectF, radius: float | None = None, fill: str | None = None) -> None:
    """层叠圆角矩形模拟柔和投影，再画浮层面板与描边；夜航投影深、晨雾投影暖而淡。"""

    radius = STYLE.menu_radius if radius is None else radius
    painter.setPen(Qt.PenStyle.NoPen)
    shadow = QColor(COLORS.shadow)
    step = (0.05 if STYLE.dark else 0.018)
    for spread in range(8, 0, -1):
        shadow.setAlphaF(min(1.0, step * (9 - spread) * STYLE.shadow_alpha / 0.7))
        painter.setBrush(shadow)
        painter.drawRoundedRect(rect.adjusted(-spread, -spread + 4, spread, spread + 4),
                                radius + spread, radius + spread)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.fillPath(path, QColor(fill or COLORS.overlay))
    # 描边前清掉笔刷：否则 drawPath 会再用最后一层半透明投影色把整块面板涂一遍（晨雾白卡发灰、夜航菜单发暗）
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(COLORS.overlay_border), 1))
    painter.drawPath(path)
    if STYLE.dark:
        # 顶边一道极淡的高光
        painter.setPen(QPen(QColor(COLORS.card_highlight), 1))
        painter.drawLine(QPointF(rect.left() + radius, rect.top() + 1), QPointF(rect.right() - radius, rect.top() + 1))
