"""围绕当前时间的轻量字幕时间轴，直接编辑 SubtitleDocument。"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygon
from PySide6.QtWidgets import QApplication, QWidget

from autoslice.desktop.qt_preview.theme import COLORS, SIZES
from autoslice.desktop.snap import SnapEngine
from autoslice.desktop.subtitles import SubtitleDocument
from autoslice.transcription.contracts import srt_timestamp_seconds


class SubtitleTimeline(QWidget):
    selected = Signal(int)
    selection_requested = Signal(int, bool, bool)
    marquee_selected = Signal(object)
    preview_changed = Signal(int)
    changed = Signal(int)
    preview_requested = Signal(float)
    seek_requested = Signal(float)
    view_changed = Signal(float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(54)
        self.setMouseTracking(True)
        self.document: SubtitleDocument | None = None
        self.selected_index: int | None = None
        self.selected_indices: set[int] = set()
        self.playing_index: int | None = None
        self.playhead = 0.0
        self.duration = 0.0
        self.center = 0.0
        self.span = 24.0
        self._drag = None
        self._scrub = None
        self._blank = None
        self._press = None
        self._marquee = None
        self._manual_pan = False
        self.hovered_index: int | None = None
        self.hovered_mode: str | None = None
        self._mouse_grabbed = False
        self.waveform_visible = False
        self.waveform_samples: list[float] = []
        self.snap_engine = SnapEngine()
        self.snapping = True
        self.snap_guide: float | None = None

    def _drag_threshold(self) -> int:
        app = QApplication.instance()
        return max(3, app.startDragDistance() if app is not None else 4)

    def _grab_pointer(self):
        if self._mouse_grabbed:
            return
        try:
            self.grabMouse()
            self._mouse_grabbed = True
        except RuntimeError:
            self._mouse_grabbed = False

    def _release_pointer(self):
        if not self._mouse_grabbed:
            return
        self._mouse_grabbed = False
        try:
            self.releaseMouse()
        except RuntimeError:
            pass

    def _reset_snap_state(self):
        self.snap_engine.reset()
        self.snap_guide = None

    def _finish_drag(self, *, commit: bool = True):
        drag = self._drag
        self._drag = None
        self._release_pointer()
        self._reset_snap_state()
        if drag and commit and drag["moved"] and self.document:
            self.document.commit_time_preview(drag["before"])
            self.changed.emit(drag["index"])
            # 时间调整只改字幕，不抢 playhead。整块移动和边缘 resize 都不主动 seek。
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def _finish_scrub(self, *, commit: bool = True):
        scrub = self._scrub
        self._scrub = None
        self._release_pointer()
        self._reset_snap_state()
        if scrub and commit:
            self.seek_requested.emit(scrub["position"])
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def _clear_pointer_interaction(self, *, commit_active: bool = True):
        if self._drag is not None:
            self._finish_drag(commit=commit_active)
            return
        if self._scrub is not None:
            self._finish_scrub(commit=commit_active)
            return
        self._press = None
        self._blank = None
        self._marquee = None
        self._release_pointer()
        self._reset_snap_state()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def event(self, event):
        if event.type() in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide):
            if any(item is not None for item in (
                self._press, self._drag, self._scrub, self._blank, self._marquee
            )):
                self._clear_pointer_interaction(commit_active=True)
        return super().event(event)

    def set_document(self, document: SubtitleDocument | None):
        self._release_pointer()
        self.document = document
        self.selected_index = None
        self.selected_indices.clear()
        self.playing_index = None
        self._drag = None
        self._scrub = None
        self._blank = None
        self._press = None
        self._marquee = None
        self.hovered_index = None
        self.hovered_mode = None
        self.center = (srt_timestamp_seconds(document.entries[0].start)
                       if document and document.entries else 0.0)
        self._manual_pan = False
        self.update()
        self._emit_view_changed()

    def set_duration(self, duration: float):
        self.duration = max(0.0, duration)
        self.update()
        self._emit_view_changed()

    def set_waveform_visible(self, visible: bool):
        self.waveform_visible = bool(visible)
        self.setMinimumHeight(92 if self.waveform_visible else 54)
        self.update()

    def set_waveform(self, samples, duration: float | None = None):
        self.waveform_samples = [max(0.0, min(1.0, float(value))) for value in (samples or ())]
        if duration is not None and duration > 0:
            self.duration = float(duration)
        self.update()
        self._emit_view_changed()

    def set_snapping(self, enabled: bool):
        self.snapping = bool(enabled)
        self.snap_engine.reset()
        self.snap_guide = None
        self.update()

    def _snap_position(self, value: float, event=None) -> float:
        targets = []
        if self.document:
            for entry in self.document.entries:
                targets.extend((srt_timestamp_seconds(entry.start), srt_timestamp_seconds(entry.end)))
        bypass = bool(event and event.modifiers() & Qt.KeyboardModifier.AltModifier)
        result = self.snap_engine.snap(value, targets, self._track_width() / self.span,
                                       enabled=self.snapping, bypass=bypass)
        self.snap_guide = result if self.snap_engine.target is not None else None
        return result

    def set_playhead(self, seconds: float, *, playing: bool):
        self.playhead = max(0.0, seconds)
        left = self._view_start()
        view_moved = False
        if not self._manual_pan and not self._drag and not self._scrub:
            if self.playhead < left + self.span * .2 or self.playhead > left + self.span * .8:
                self.center = self.playhead
                view_moved = True
        self.update()
        if view_moved:
            self._emit_view_changed()

    def follow_playback(self):
        self._manual_pan = False
        self.center = self.playhead
        self.update()
        self._emit_view_changed()

    def select_cue(self, index: int | None, *, center: bool = True):
        self.selected_index = index
        view_moved = False
        if center and self.document:
            entry = next((item for item in self.document.entries if item.index == index), None)
            if entry:
                self.center = srt_timestamp_seconds(entry.start)
                self._manual_pan = False
                view_moved = True
        self.update()
        if view_moved:
            self._emit_view_changed()

    def set_selection(self, active: int | None, selected: set[int], *, center: bool = False):
        self.selected_indices = set(selected)
        self.select_cue(active, center=center)

    def set_playing_cue(self, index: int | None):
        if index != self.playing_index:
            self.playing_index = index
            self.update()

    def overlap_count(self) -> int:
        if not self.document:
            return 0
        return sum(srt_timestamp_seconds(left.end) > srt_timestamp_seconds(right.start)
                   for left, right in zip(self.document.entries, self.document.entries[1:]))

    def _view_start(self) -> float:
        left = max(0.0, self.center - self.span / 2)
        return min(left, max(0.0, self.duration - self.span)) if self.duration else left

    def _emit_view_changed(self):
        visible_span = min(self.span, self.duration) if self.duration > 0 else self.span
        self.view_changed.emit(self._view_start(), max(0.0, visible_span), self.duration)

    def set_view_start(self, seconds: float):
        if self.duration <= 0:
            return
        visible_span = min(self.span, self.duration)
        maximum = max(0.0, self.duration - visible_span)
        start = max(0.0, min(float(seconds), maximum))
        self.center = start + visible_span / 2
        self._manual_pan = True
        self.update()
        self._emit_view_changed()

    def pan_view(self, direction: int, *, fraction: float = 0.20):
        """按当前缩放比例横向平移视野，不改变 playhead 或字幕时间。"""
        if self.duration <= 0 or direction == 0:
            return
        visible_span = min(self.span, self.duration)
        step = max(0.05, visible_span * max(0.01, float(fraction)))
        self.set_view_start(self._view_start() + (step if direction > 0 else -step))

    def pan_view_by_time(self, direction: int, elapsed: float, *, speed: float = 0.8):
        """按经过的真实时间平移视野，供按住侧键时的连续更新使用。

        ``speed`` 表示每秒移动多少个可视窗口。步长只依赖当前可视
        ``span`` 和计时器实际间隔，因此缩放时间轴后手感保持一致；这个
        方法不会触碰 playhead 或字幕条目的时间。
        """
        if self.duration <= 0 or direction == 0 or elapsed <= 0:
            return
        visible_span = min(self.span, self.duration)
        step = visible_span * max(0.0, float(speed)) * float(elapsed)
        if step <= 0:
            return
        self.set_view_start(self._view_start() + (step if direction > 0 else -step))

    def _track_width(self) -> float:
        return max(1.0, self.width() - 24.0)

    def _x(self, seconds: float) -> float:
        return 12.0 + (seconds - self._view_start()) / self.span * self._track_width()

    def _seconds(self, x: float) -> float:
        return max(0.0, self._view_start() + (x - 12.0) / self._track_width() * self.span)

    def _waveform_amplitude(self, x: float, pixel_width: float = 2.0) -> float:
        """返回当前可视时间窗口中某一像素列对应的波形峰值。

        waveform_samples 覆盖整段媒体，因此必须先把屏幕 x 映射到当前
        view_start/span，再映射到全局 sample 索引；不能把整个 sample 数组
        每次都硬塞进当前 widget 宽度，否则缩放时波形不会跟着放大。
        """
        if not self.waveform_samples or self.duration <= 0:
            return 0.0
        width = self._track_width()
        left_ratio = max(0.0, min(1.0, (x - 12.0) / width))
        right_ratio = max(
            0.0,
            min(1.0, (x + max(1.0, pixel_width) - 12.0) / width),
        )
        start_time = self._view_start() + left_ratio * self.span
        end_time = self._view_start() + right_ratio * self.span
        sample_count = len(self.waveform_samples)
        start_index = max(
            0,
            min(sample_count - 1, int(start_time / self.duration * sample_count)),
        )
        end_index = max(
            start_index + 1,
            min(sample_count, int(end_time / self.duration * sample_count) + 1),
        )
        return max(self.waveform_samples[start_index:end_index], default=0.0)

    def _visible_blocks(self):
        if not self.document:
            return
        left, right = self._view_start(), self._view_start() + self.span
        for entry in self.document.entries:
            start = srt_timestamp_seconds(entry.start)
            end = srt_timestamp_seconds(entry.end)
            if end < left or start > right:
                continue
            x1 = max(12.0, self._x(start))
            x2 = min(float(self.width() - 12), self._x(end))
            if x2 > x1:
                yield entry, start, end, QRectF(x1, 27, max(5.0, x2 - x1), 24)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(COLORS.divider), 1))
        painter.drawLine(12, 21, self.width() - 12, 21)
        if self.waveform_visible:
            painter.setPen(QPen(QColor(COLORS.subtle), 1))
            center = 72
            if self.waveform_samples:
                for x in range(12, max(12, self.width() - 12), 2):
                    sample = self._waveform_amplitude(float(x), 2.0)
                    amplitude = max(1, int(sample * 24))
                    painter.drawLine(x, int(center - amplitude), x, int(center + amplitude))
            else:
                painter.setPen(QColor(COLORS.subtle))
                painter.drawText(12, 57, self.width() - 24, 30,
                                 Qt.AlignmentFlag.AlignCenter, "生成波形中…")
        if not self.document:
            painter.setPen(QColor(COLORS.subtle))
            painter.drawText(12, 27, self.width() - 24, 24,
                             Qt.AlignmentFlag.AlignCenter, "选择字幕后显示局部时间轴")
            return
        spacing = next((step for step in (1, 2, 5, 10, 20, 30, 60, 120, 300)
                        if self._track_width() * step / self.span >= 85), 600)
        left = self._view_start()
        tick = int(left // spacing) * spacing
        while tick <= left + self.span:
            x = self._x(tick)
            if 12 <= x <= self.width() - 12:
                painter.setPen(QColor(COLORS.subtle))
                minutes, seconds = divmod(tick, 60)
                painter.drawText(int(x + 3), 0, 55, 17, Qt.AlignmentFlag.AlignLeft,
                                 f"{minutes:02d}:{seconds:02d}")
                painter.setPen(QPen(QColor(COLORS.divider), 1))
                painter.drawLine(int(x), 17, int(x), 25)
            tick += spacing
        for entry, _start, _end, rect in self._visible_blocks():
            selected = entry.index in self.selected_indices
            primary = entry.index == self.selected_index
            current = entry.index == self.playing_index
            hovered = entry.index == self.hovered_index
            border = COLORS.accent if selected else (COLORS.playback_border if current else COLORS.divider)
            fill = COLORS.accent_tint if selected else (COLORS.playback if current else
                                                      COLORS.hover if hovered else COLORS.raised)
            painter.setPen(QPen(QColor(border), 2 if primary else 1))
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(rect, 4, 4)
            if rect.width() >= 28:
                painter.setPen(QColor(COLORS.text if selected else COLORS.muted))
                painter.drawText(rect.adjusted(5, 0, -3, 0), Qt.AlignmentFlag.AlignVCenter,
                                 str(entry.index))
            if primary and selected:
                painter.fillRect(QRectF(rect.left() + 2, rect.top() + 3, 2, rect.height() - 6),
                                 QColor(COLORS.accent))
            if hovered and self.hovered_mode in ("left", "right"):
                edge_x = rect.left() if self.hovered_mode == "left" else rect.right()
                painter.setPen(QPen(QColor(COLORS.accent_hover), 3))
                painter.drawLine(int(edge_x), int(rect.top() + 2),
                                 int(edge_x), int(rect.bottom() - 2))
        if self._marquee:
            rectangle = QRectF(self._marquee[0], self._marquee[1],
                               self._marquee[2] - self._marquee[0],
                               self._marquee[3] - self._marquee[1]).normalized()
            painter.setPen(QPen(QColor(COLORS.accent), 1))
            tint = QColor(COLORS.accent)
            tint.setAlpha(35)
            painter.setBrush(tint)
            painter.drawRect(rectangle)
        marker = self._x(self._scrub["position"] if self._scrub else self.playhead)
        if 12 <= marker <= self.width() - 12:
            painter.setPen(QPen(QColor(COLORS.accent), 2))
            painter.drawLine(int(marker), 19, int(marker), self.height() - 1)
            painter.setBrush(QColor(COLORS.accent))
            painter.drawPolygon(QPolygon([
                QPoint(int(marker) - 7, 18), QPoint(int(marker) + 7, 18),
                QPoint(int(marker), 25),
            ]))
        if self._scrub:
            position = self._scrub["position"]
            minutes, seconds = divmod(position, 60)
            tip = f"{int(minutes):02d}:{seconds:06.3f}"
            tip_width = 82
            x = max(12, min(self.width() - tip_width - 12, int(marker) - tip_width // 2))
            painter.setPen(QColor(COLORS.divider))
            painter.setBrush(QColor(COLORS.raised))
            painter.drawRoundedRect(QRectF(x, 0, tip_width, 18), 4, 4)
            painter.setPen(QColor(COLORS.text))
            painter.drawText(x, 0, tip_width, 18, Qt.AlignmentFlag.AlignCenter, tip)
        if self.snap_guide is not None:
            guide = self._x(self.snap_guide)
            painter.setPen(QPen(QColor(COLORS.accent), 1, Qt.PenStyle.DashLine))
            painter.drawLine(int(guide), 0, int(guide), self.height())

    def _on_playhead(self, x: float, y: float) -> bool:
        if not self.document or not (0 <= y < self.height()):
            return False
        distance = abs(x - self._x(self.playhead))
        return distance <= SIZES.playhead_hit_width / 2

    def _hit(self, x: float, y: float):
        if y < 26 or y > 53:
            return None
        blocks = list(self._visible_blocks() or ())
        for entry, start, end, rect in reversed(blocks):
            if rect.contains(x, y):
                edge = min(8.0, rect.width() / 3)
                mode = ("left" if x <= rect.left() + edge else
                        "right" if x >= rect.right() - edge else "move")
                return entry, start, end, mode
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.document is None:
            return super().mousePressEvent(event)
        x, y = event.position().x(), event.position().y()
        hit = self._hit(x, y)
        # 字幕块拥有编辑优先级；竖线在没有字幕块覆盖的整段高度都可抓取。
        if hit is None and self._on_playhead(x, y):
            self._press = {"kind": "playhead", "x": x, "y": y,
                           "started": time.monotonic(), "moved": False}
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            event.accept()
            return
        if hit is None:
            if 12 <= x <= self.width() - 12:
                self._blank = (x, y)
                # 单击空白应在按下时就响应；后续若形成拖框则继续按 marquee 处理。
                self.seek_requested.emit(min(self.duration or float("inf"), self._seconds(x)))
                event.accept()
            return
        entry, start, end, mode = hit
        self._press = {"kind": "cue", "entry": entry, "start": start, "end": end,
                       "mode": mode, "x": x, "y": y, "started": time.monotonic(),
                       "moved": False, "before": tuple(self.document.entries)}
        self.select_cue(entry.index, center=False)
        self.selection_requested.emit(
            entry.index,
            bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier),
            bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
        )
        event.accept()

    def _activate_press(self, event):
        if not self._press:
            return False
        press = self._press
        delta_x = event.position().x() - press["x"]
        delta_y = event.position().y() - press["y"]
        if abs(delta_x) + abs(delta_y) < self._drag_threshold():
            return False
        if press["kind"] == "playhead":
            self._scrub = {"position": self.playhead, "last_seek": 0.0}
            self._press = None
            self._grab_pointer()
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return True
        self._drag = {
            "index": press["entry"].index, "mode": press["mode"], "x": press["x"],
            "start": press["start"], "end": press["end"], "before": press["before"],
            "moved": True, "last_seek": 0.0,
        }
        self._press = None
        self._grab_pointer()
        self.setCursor(
            Qt.CursorShape.ClosedHandCursor
            if self._drag["mode"] == "move"
            else Qt.CursorShape.SizeHorCursor
        )
        event.accept()
        return True

    def mouseMoveEvent(self, event):
        if self._blank:
            x, y = event.position().x(), event.position().y()
            if abs(x - self._blank[0]) + abs(y - self._blank[1]) >= 5:
                self._marquee = (self._blank[0], self._blank[1], x, y)
                self.update()
            event.accept()
            return
        if self._press:
            self._activate_press(event)
            if self._press:
                self.update()
                event.accept()
                return
        if self._scrub:
            position = min(self.duration or float("inf"), self._seconds(event.position().x()))
            position = self._snap_position(position, event)
            self._scrub["position"] = position
            now = time.monotonic()
            if now - self._scrub["last_seek"] >= .15:
                self._scrub["last_seek"] = now
                self.preview_requested.emit(position)
            self.update()
            event.accept()
            return
        if not self._drag or not self.document:
            hit = self._hit(event.position().x(), event.position().y())
            if hit is not None:
                mode = hit[3]
                self.setCursor(
                    Qt.CursorShape.SizeHorCursor
                    if mode in ("left", "right")
                    else Qt.CursorShape.ArrowCursor
                )
            else:
                mode = None
                self.setCursor(Qt.CursorShape.SizeHorCursor if self._on_playhead(
                    event.position().x(), event.position().y()) else Qt.CursorShape.ArrowCursor)
            hovered = hit[0].index if hit else None
            if hovered != self.hovered_index or mode != self.hovered_mode:
                self.hovered_index = hovered
                self.hovered_mode = mode
                self.update()
            return super().mouseMoveEvent(event)
        drag = self._drag
        delta = (event.position().x() - drag["x"]) / self._track_width() * self.span
        if abs(delta) < .015:
            return
        drag["moved"] = True
        index = drag["index"]
        position = next(i for i, entry in enumerate(self.document.entries)
                        if entry.index == index)
        previous = (srt_timestamp_seconds(self.document.entries[position - 1].start)
                    if position else 0.0)
        following = (srt_timestamp_seconds(self.document.entries[position + 1].start)
                     if position + 1 < len(self.document.entries) else float("inf"))
        if drag["mode"] == "move":
            start = min(max(drag["start"] + delta, previous), following)
            end = start + drag["end"] - drag["start"]
        elif drag["mode"] == "left":
            start = min(max(drag["start"] + delta, previous), following,
                        drag["end"] - .05)
            end = drag["end"]
        else:
            start = drag["start"]
            end = max(start + .05, drag["end"] + delta)
        try:
            self.document.change_time(index, start, end, remember=False)
        except ValueError:
            event.accept()
            return
        self.preview_changed.emit(index)
        self.update()
        event.accept()

    def leaveEvent(self, event):
        self.hovered_index = None
        self.hovered_mode = None
        if self._drag is None and self._scrub is None:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._blank:
            if self._marquee:
                self.marquee_selected.emit(self._marquee_hits())
            self._blank = None
            self._marquee = None
            self.update()
            event.accept()
            return
        if self._scrub:
            self._finish_scrub(commit=True)
            event.accept()
            return
        if self._press:
            press = self._press
            self._press = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if press["kind"] == "playhead":
                self.seek_requested.emit(self.playhead)
            # 时间轴字幕块点击只负责选择，不负责定位/居中。
            # 真正“选中并跳转”只保留给底部字幕列表。
            self.update()
            event.accept()
            return
        if self._drag is None or self.document is None:
            return super().mouseReleaseEvent(event)
        self._finish_drag(commit=True)
        event.accept()

    def _marquee_hits(self) -> set[int]:
        if not self._marquee:
            return set()
        area = QRectF(self._marquee[0], self._marquee[1],
                      self._marquee[2] - self._marquee[0],
                      self._marquee[3] - self._marquee[1]).normalized()
        return {entry.index for entry, _start, _end, rect in self._visible_blocks()
                if area.intersected(rect).width() >= min(12.0, rect.width() * .35)
                and area.intersected(rect).height() >= rect.height() * .4}

    def wheelEvent(self, event):
        direction = 1 if event.angleDelta().y() > 0 else -1
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.span = max(4.0, min(120.0, self.span * (.8 if direction > 0 else 1.25)))
        else:
            self.center = max(0.0, min(self.duration or float("inf"),
                                       self.center - direction * self.span * .25))
            self._manual_pan = True
        self.update()
        self._emit_view_changed()
        event.accept()


class TimelineSidePanController(QObject):
    """把鼠标侧键按住状态转换为时间轴的连续平移。"""

    interval_ms = 16
    windows_per_second = 0.8

    def __init__(self, timeline: SubtitleTimeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline
        self.direction = 0
        self._last_tick = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(self.interval_ms)
        self.timer.timeout.connect(self._tick)

    @property
    def active(self) -> bool:
        return self.direction != 0 and self.timer.isActive()

    def start(self, direction: int):
        direction = 1 if direction > 0 else -1 if direction < 0 else 0
        if direction == 0:
            self.stop()
            return
        self.direction = direction
        self._last_tick = time.monotonic()
        # 按下时先走一个计时器间隔，避免用户看到“按下后要等一帧才动”。
        self._tick(immediate=True)
        self.timer.start()

    def stop(self):
        self.direction = 0
        self._last_tick = 0.0
        self.timer.stop()

    def _tick(self, *, immediate: bool = False):
        if self.direction == 0:
            self.timer.stop()
            return
        now = time.monotonic()
        if immediate or self._last_tick <= 0:
            elapsed = self.timer.interval() / 1000.0
        else:
            elapsed = min(0.1, max(0.0, now - self._last_tick))
        self._last_tick = now
        self.timeline.pan_view_by_time(
            self.direction,
            elapsed,
            speed=self.windows_per_second,
        )

    def stop_for_event(self, event_type) -> bool:
        """窗口失焦或隐藏时停止，返回是否因此清除了活动状态。"""
        if event_type not in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide,
                              QEvent.Type.Close):
            return False
        was_active = self.direction != 0 or self.timer.isActive()
        self.stop()
        return was_active
