"""视频上的字幕选框（D-049）：调字幕样式时，像剪映一样在画面上点选字幕、拖动改位置、拖四角改大小。

视频区是 mpv 的原生窗口，普通子控件盖不住它，所以选框是一个贴在视频区上方的透明小窗口：
不抢焦点、不进任务栏，跟着主窗口走。框和手柄由这里画，字幕本身仍由 mpv（libass）按样式实时重画。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QWidget

from autoslice.subtitle_workflow import (
    jianying_from_pixels,
    subtitle_canvas_bounds,
    subtitle_style_geometry,
)

CHROME = QColor(59, 160, 255)        # 和封面画布的选中框同色
LOCKED = QColor(200, 205, 212, 230)
PAD = 6.0
HANDLE_RADIUS = 9.0
CENTER_SNAP_PX = 8.0
PLACEHOLDER = "字幕位置"
CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")


def video_rect(widget_size: QSize, video_size: tuple[int, int]) -> QRectF:
    """mpv 按原比例把画面居中放进视频区（上下或左右留黑边）。"""

    width, height = max(1, video_size[0]), max(1, video_size[1])
    scale = min(widget_size.width() / width, widget_size.height() / height)
    shown_width, shown_height = width * scale, height * scale
    return QRectF((widget_size.width() - shown_width) / 2, (widget_size.height() - shown_height) / 2,
                  shown_width, shown_height)


def text_box(style: dict, text: str, video_size: tuple[int, int], canvas=None) -> QRectF:
    """字幕在画面像素坐标里大致占的框：以锚点为中心，按字号量字宽、按行数算高，再加上描边。

    ASS 的字号是“行高”（升部 + 降部），所以先按 Qt 量到的行高换算比例；字体对不上时只是框略大略小，
    中心始终准确（压制用的也是中心锚点）。
    """

    geometry = subtitle_style_geometry(style, *video_size, canvas)
    font_px = max(1.0, geometry["font_size"])
    font = QFont(style.get("font_name") or "")
    font.setPixelSize(max(1, round(font_px)))
    font.setBold(bool(style.get("bold", True)))
    metrics = QFontMetricsF(font)
    line_height = max(1.0, metrics.ascent() + metrics.descent())
    ratio = font_px / line_height
    lines = (text or PLACEHOLDER).split("\n")
    width = max(metrics.horizontalAdvance(line) for line in lines) * ratio
    height = font_px * len(lines)
    outline = geometry["outline"]
    width, height = width + outline * 2, height + outline * 2
    return QRectF(geometry["x"] - width / 2, geometry["y"] - height / 2, width, height)


class SubtitleBoxOverlay(QWidget):
    """provider 要提供：style_overlay_state() -> dict | None（None 表示现在不该显示）。

    state 里有 style、text、video_size、canvas（压制画布，竖屏等视频按它换算位置）、editable。拖动时发 changed（剪映单位的 x / y / font_size），
    默认样式不能改时发 locked_attempt。
    """

    changed = Signal(dict)
    locked_attempt = Signal()

    def __init__(self, window: QWidget, target: QWidget, provider) -> None:
        super().__init__(window, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMouseTracking(True)
        self.setWindowTitle("字幕位置")
        self._window = window
        self._target = target
        self._provider = provider
        self._state: dict | None = None
        self.selected = False
        self._hover: str | None = None
        self._drag: dict | None = None
        self._guide = False
        # 只在样式面板开着时轮询（播放中字幕换句、弹出模态窗口都要跟上）；平时不占资源
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.sync)
        window.installEventFilter(self)
        target.installEventFilter(self)

    def set_active(self, active: bool) -> None:
        """样式面板开 / 关时调用：开着才轮询，关了立刻收起。"""

        if active:
            self._timer.start()
        else:
            self._timer.stop()
        self.sync()

    # ── 跟随主窗口 ──
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # 主窗口拆除过程中 Python 这一侧可能已经清空，事件还会进来：别抛错
        timer = getattr(self, "_timer", None)
        if timer is not None and timer.isActive() and event.type() in (
                QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.WindowStateChange,
                QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate,
                QEvent.Type.Show, QEvent.Type.Hide):
            QTimer.singleShot(0, self, self.sync)
        return False

    def sync(self) -> None:
        """该不该显示、贴在哪儿。状态和位置都没变就不重画（透明分层窗口每次重画都是整块刷新）。"""

        state = self._provider.style_overlay_state() if self._timer.isActive() else None
        if state is None or not self._target.isVisible():
            if self._drag is None and (self._state is not None or self.isVisible()):
                self._state = None
                self.selected = False
                self.hide()
            return
        changed = state != self._state
        self._state = state
        geometry = self._target.rect().translated(self._target.mapToGlobal(QPoint(0, 0)))
        if self.geometry() != geometry:
            self.setGeometry(geometry)
            changed = True
        if not self.isVisible():
            self.show()
            changed = True
        if changed:
            self.update()

    # ── 坐标 ──
    def _video(self) -> QRectF:
        return video_rect(self.size(), self._state["video_size"])

    def _scale(self) -> float:
        width = max(1, self._state["video_size"][0])
        return self._video().width() / width

    def _box(self) -> QRectF:
        """选框在本窗口里的位置（含留白）。"""

        frame = text_box(self._state["style"], self._state.get("text") or "", self._state["video_size"],
                         self._state.get("canvas"))
        video, scale = self._video(), self._scale()
        rect = QRectF(video.x() + frame.x() * scale, video.y() + frame.y() * scale,
                      frame.width() * scale, frame.height() * scale)
        return rect.adjusted(-PAD, -PAD, PAD, PAD)

    def _corners(self, box: QRectF) -> dict[str, QPointF]:
        return {"top-left": box.topLeft(), "top-right": box.topRight(),
                "bottom-left": box.bottomLeft(), "bottom-right": box.bottomRight()}

    def _hit(self, point: QPointF) -> str | None:
        if self._state is None:
            return None
        box = self._box()
        if self.selected and self._state.get("editable"):
            for name, corner in self._corners(box).items():
                if math.hypot(point.x() - corner.x(), point.y() - corner.y()) <= HANDLE_RADIUS + 3:
                    return name
        return "move" if box.contains(point) else None

    def _geometry(self) -> dict:
        return subtitle_style_geometry(self._state["style"], *self._state["video_size"], self._state.get("canvas"))

    def _anchor(self) -> QPointF:
        geometry = self._geometry()
        return QPointF(geometry["x"], geometry["y"])

    # ── 鼠标 ──
    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._state is None:
            return super().mousePressEvent(event)
        point = event.position()
        hit = self._hit(point)
        if hit is None:
            self.selected = False
            self.update()
            return
        self.selected = True
        if not self._state.get("editable"):
            self.locked_attempt.emit()
            self.update()
            return
        geometry = self._geometry()
        video, scale = self._video(), self._scale()
        center = QPointF(video.x() + geometry["x"] * scale, video.y() + geometry["y"] * scale)
        self._drag = {
            "kind": hit, "press": point, "anchor": self._anchor(), "center": center,
            "font_px": geometry["font_size"],
            "distance": max(1.0, math.hypot(point.x() - center.x(), point.y() - center.y())),
        }
        self.update()

    def mouseMoveEvent(self, event) -> None:
        point = event.position()
        if self._drag is None or self._state is None:
            hover = self._hit(point)
            if hover != self._hover:
                self._hover = hover
                self.update()
            # 默认样式锁着：能点（会提示先新建），但不显示“可拖”的光标
            locked = not self._state.get("editable") if self._state else True
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover and locked else
                           Qt.CursorShape.SizeAllCursor if hover == "move" else
                           Qt.CursorShape.SizeFDiagCursor if hover in ("top-left", "bottom-right") else
                           Qt.CursorShape.SizeBDiagCursor if hover else Qt.CursorShape.ArrowCursor)
            return
        drag, scale = self._drag, self._scale()
        width, height = self._state["video_size"]
        canvas = self._state.get("canvas")
        anchor = drag["anchor"]
        if drag["kind"] == "move":
            center_x = anchor.x() + (point.x() - drag["press"].x()) / scale
            center_y = anchor.y() + (point.y() - drag["press"].y()) / scale
            # 靠近水平正中 8px 就吸过去（和封面画布一样），按住 Alt 不吸
            bypass = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
            self._guide = not bypass and abs(center_x - width / 2) * scale <= CENTER_SNAP_PX
            if self._guide:
                center_x = width / 2
            # 中心限制在压制画布里（竖屏视频的左右两侧在成片里是黑边，字幕可以放过去）
            left, top, right, bottom = subtitle_canvas_bounds(width, height, canvas)
            center_x = min(max(center_x, left), right)
            center_y = min(max(center_y, top), bottom)
            values = jianying_from_pixels(center_x, center_y, drag["font_px"], width, height, canvas)
            self.changed.emit({"x": values["x"], "y": values["y"]})
            self._refresh_state()
        else:
            # 拖角：以字幕中心为准等比缩放字号（中心不动，和压制用的中心锚点一致）
            distance = math.hypot(point.x() - drag["center"].x(), point.y() - drag["center"].y())
            font_px = drag["font_px"] * max(0.05, distance / drag["distance"])
            values = jianying_from_pixels(anchor.x(), anchor.y(), font_px, width, height, canvas)
            self.changed.emit({"font_size": values["font_size"]})
            self._refresh_state()
        self.update()

    def _refresh_state(self) -> None:
        """发出改动后立刻按面板的新数值重画框，不等下一次定时同步。"""

        state = self._provider.style_overlay_state()
        if state is not None:
            self._state = state

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is not None:
            self._drag = None
            self._guide = False
            self.update()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    # ── 绘制 ──
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 几乎透明的底：分层窗口里全透明的像素会把点击漏给下面，要接住视频区里的点击
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        if self._state is None:
            return
        box = self._box()
        editable = bool(self._state.get("editable"))
        if self._guide:
            video = self._video()
            painter.setPen(QPen(CHROME, 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(video.center().x(), video.top()), QPointF(video.center().x(), video.bottom()))
        if self.selected:
            painter.setPen(QPen(CHROME if editable else LOCKED, 1.5,
                                Qt.PenStyle.SolidLine if editable else Qt.PenStyle.DashLine))
        elif self._hover:
            painter.setPen(QPen(QColor(CHROME.red(), CHROME.green(), CHROME.blue(), 170), 1))
        else:
            # 没选中时也留一道淡虚线，告诉人“这里可以点”（没字幕的空白处也能先摆好位置）
            painter.setPen(QPen(QColor(255, 255, 255, 110), 1, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(box)
        if not self._state.get("text"):
            painter.setPen(QColor(255, 255, 255, 150))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "这句没有字幕 · 拖这里摆位置")
        if self.selected and editable:
            for name, corner in self._corners(box).items():
                self._draw_handle(painter, name, corner)

    @staticmethod
    def _draw_handle(painter: QPainter, name: str, center: QPointF) -> None:
        """白底圆形 + 斜向双箭头，和封面画布的缩放手柄一个样子。"""

        painter.save()
        painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
        painter.setBrush(QColor(255, 255, 255, 245))
        painter.drawEllipse(center, HANDLE_RADIUS, HANDLE_RADIUS)
        pen = QPen(QColor(40, 48, 58), 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        r = HANDLE_RADIUS * 0.42
        sign = 1 if name in ("top-left", "bottom-right") else -1
        start, end = QPointF(center.x() - r, center.y() - r * sign), QPointF(center.x() + r, center.y() + r * sign)
        painter.drawLine(start, end)
        for tip, direction in ((end, -1), (start, 1)):
            painter.drawLine(tip, QPointF(tip.x() + direction * r * 0.75, tip.y()))
            painter.drawLine(tip, QPointF(tip.x(), tip.y() + direction * sign * r * 0.75))
        painter.restore()
