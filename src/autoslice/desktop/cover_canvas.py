"""AutoCover 交互画布。

画布只负责鼠标/滚轮手势和轻量选中态，草稿及渲染仍由 ``cover.py`` 和
``cover_service.py`` 负责。所有坐标都以 16:9 画布的归一化值传递。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel


class CoverCanvas(QLabel):
    """支持标题拖动、背景平移和滚轮缩放的轻量 QLabel 画布。"""

    title_position_changed = Signal(float, float)
    title_position_finished = Signal()
    background_position_changed = Signal(float, float)
    background_position_finished = Signal()
    zoom_changed = Signal(float)
    selected_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(420, 260)
        self._pixmap = QPixmap()
        self._title_rect = QRectF()
        self._selected = False
        self._mode: str | None = None
        self._press = QPointF()
        self._start_x = 0.0
        self._start_y = 0.0
        self._background_x = 0.5
        self._background_y = 0.5
        self._zoom = 1.0
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(
            "background: #111820; border: 1px solid #2f414d; border-radius: 6px;"
        )

    def set_preview(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self.update()

    def pixmap(self):
        """兼容旧的 Qt smoke 调用方，同时保留自绘画布。"""

        return self._pixmap

    def set_title_rect(self, rect: QRectF):
        self._title_rect = rect
        self.update()

    def set_zoom(self, zoom: float):
        self._zoom = max(1.0, min(2.5, float(zoom)))
        self.update()

    def set_background_focus(self, x: float, y: float):
        self._background_x = max(0.0, min(1.0, float(x)))
        self._background_y = max(0.0, min(1.0, float(y)))

    def _image_rect(self) -> QRectF:
        if self._pixmap.isNull():
            return QRectF()
        target = QRectF(self.rect())
        source_ratio = self._pixmap.width() / max(1, self._pixmap.height())
        if target.width() / max(1.0, target.height()) > source_ratio:
            height = target.height()
            width = height * source_ratio
            return QRectF(target.center().x() - width / 2, target.top(), width, height)
        width = target.width()
        height = width / source_ratio
        return QRectF(target.left(), target.center().y() - height / 2, width, height)

    def _norm_point(self, point: QPointF) -> tuple[float, float] | None:
        image = self._image_rect()
        if image.isNull() or not image.contains(point):
            return None
        return (
            max(0.0, min(1.0, (point.x() - image.left()) / image.width())),
            max(0.0, min(1.0, (point.y() - image.top()) / image.height())),
        )

    def _display_title_rect(self) -> QRectF:
        image = self._image_rect()
        if image.isNull() or self._title_rect.isNull():
            return QRectF()
        return QRectF(
            image.left() + self._title_rect.left() * image.width(),
            image.top() + self._title_rect.top() * image.height(),
            self._title_rect.width() * image.width(),
            self._title_rect.height() * image.height(),
        )

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._pixmap.isNull():
            super().mousePressEvent(event)
            return
        point = event.position()
        norm = self._norm_point(point)
        if norm is None:
            return
        title_rect = self._display_title_rect()
        if title_rect.contains(point):
            self._mode = "title"
            self._selected = True
            self.selected_changed.emit(True)
        else:
            self._mode = "background"
            self._selected = False
            self.selected_changed.emit(False)
        self._press = point
        self._start_x, self._start_y = (
            (self._background_x, self._background_y)
            if self._mode == "background" else norm
        )
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._mode is None:
            super().mouseMoveEvent(event)
            return
        image = self._image_rect()
        if image.isNull():
            return
        delta_x = (event.position().x() - self._press.x()) / image.width()
        delta_y = (event.position().y() - self._press.y()) / image.height()
        if self._mode == "title":
            self.title_position_changed.emit(
                max(0.0, min(1.0, self._start_x + delta_x)),
                max(0.0, min(1.0, self._start_y + delta_y)),
            )
        else:
            self.background_position_changed.emit(
                max(0.0, min(1.0, self._start_x - delta_x)),
                max(0.0, min(1.0, self._start_y - delta_y)),
            )
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._mode is not None:
            mode = self._mode
            self._mode = None
            self.unsetCursor()
            if mode == "title":
                self.title_position_finished.emit()
            else:
                self.background_position_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._pixmap.isNull():
            super().wheelEvent(event)
            return
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        self._zoom = max(1.0, min(2.5, self._zoom + steps * 0.1))
        self.zoom_changed.emit(self._zoom)
        event.accept()

    def paintEvent(self, event):
        if self._pixmap.isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())
        image = self._image_rect()
        if not self._pixmap.isNull() and not image.isNull():
            painter.drawPixmap(image.toRect(), self._pixmap)
        if self._selected and not self._title_rect.isNull() and not image.isNull():
            rect = self._display_title_rect().adjusted(-4, -4, 4, 4)
            pen = QPen(Qt.GlobalColor.white, 1, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
        painter.end()
