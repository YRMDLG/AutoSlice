"""围绕当前时间的轻量字幕时间轴，直接编辑 SubtitleDocument。"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from autoslice.desktop.qt_preview import motion
from autoslice.desktop.qt_preview.motion import faded, mix
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
        self.waveform_visible = False
        self.waveform_samples: list[float] = []
        self.snap_engine = SnapEngine()
        self.snapping = True
        self.snap_guide: float | None = None
        # 动效：字幕块状态、视图平移缩放、吸附线、框选残影、波形、播放头插值
        self._fx = motion.KeyedAnimator(self, self.update)
        self._view = motion.ValueGlide(self, self._apply_view)
        self._guide = motion.Channel(self, self.update)
        self._guide_at: float | None = None
        self._marquee_fade = motion.Channel(self, self.update)
        self._marquee_last: QRectF | None = None
        self._wave = motion.Channel(self, self.update, 1.0)
        self._seek = motion.ValueGlide(self, self._apply_seek)
        self._seek_at: float | None = None
        self._anchor: tuple[float, float] | None = None
        self._rate = 1.0
        self._offset = 0.0
        self._frame = QTimer(self)
        self._frame.setInterval(16)
        self._frame.timeout.connect(self._advance)

    def _apply_view(self, values):
        self.center = values[0]
        self.span = math.exp(values[1])
        self.update()

    def glide_to(self, center: float | None = None, span: float | None = None,
                 duration: int = motion.VIEW):
        """平滑移动视图；缩放按对数插值，快慢感受均匀。"""
        goal = self._view.target
        goal_center = goal[0] if goal else self.center
        goal_span = math.exp(goal[1]) if goal else self.span
        end = (goal_center if center is None else center, math.log(goal_span if span is None else span))
        self._view.go((self.center, math.log(self.span)), end, duration)

    def _apply_seek(self, values):
        self._seek_at = values[0]
        self.update()

    def _display_playhead(self) -> float:
        if self._seek.running() and self._seek_at is not None:
            return self._seek_at
        if self._anchor is None:
            return self.playhead
        position, stamp = self._anchor
        now = time.monotonic()
        # 两次上报之间按速率外推；误差残差指数衰减，避免回跳
        return (position + min(now - stamp, .35) * self._rate
                + self._offset * math.exp(-(now - stamp) / .09))

    def _advance(self):
        if self._anchor is None or time.monotonic() - self._anchor[1] > .6:
            self._frame.stop()
        self.update()

    def set_document(self, document: SubtitleDocument | None):
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
        self._view.stop()
        self._fx.reset()
        self._marquee_last = None
        self.center = (srt_timestamp_seconds(document.entries[0].start)
                       if document and document.entries else 0.0)
        self._manual_pan = False
        self.update()

    def set_duration(self, duration: float):
        self.duration = max(0.0, duration)
        self.update()

    def set_waveform_visible(self, visible: bool):
        self.waveform_visible = bool(visible)
        self.setMinimumHeight(92 if self.waveform_visible else 54)
        self.update()

    def set_waveform(self, samples, duration: float | None = None):
        appeared = bool(samples) and not self.waveform_samples
        self.waveform_samples = [max(0.0, min(1.0, float(value))) for value in (samples or ())]
        if appeared:
            self._wave.snap(0.0)
            self._wave.to(1.0, 420)
        if duration is not None and duration > 0:
            self.duration = float(duration)
        self.update()

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
        now = time.monotonic()
        shown = self._display_playhead()
        target = max(0.0, seconds)
        if playing and motion.enabled():
            if self._anchor is not None:
                elapsed, advanced = now - self._anchor[1], target - self._anchor[0]
                if .03 < elapsed < 1.0 and 0.0 <= advanced <= elapsed * 4 + .05:
                    self._rate += (max(.1, min(4.0, advanced / elapsed)) - self._rate) * .5
            jumped = self._anchor is None or abs(shown - target) > 1.0
            self._offset = 0.0 if jumped else shown - target
            self._anchor = (target, now)
            self._seek.stop()
            if not self._frame.isActive():
                self._frame.start()
        else:
            self._anchor = None
            self._offset = 0.0
            self._frame.stop()
            if abs(shown - target) > 1e-3 and not self._scrub and not self._drag:
                # 跳转时播放头滑向新位置
                self._seek.go((shown,), (target,), motion.SELECT)
        self.playhead = target
        left = self._view_start()
        if not self._manual_pan and not self._drag and not self._scrub:
            if self.playhead < left + self.span * .2 or self.playhead > left + self.span * .8:
                self.glide_to(self.playhead)
        self.update()

    def follow_playback(self):
        self._manual_pan = False
        self.glide_to(self.playhead)
        self.update()

    def select_cue(self, index: int | None, *, center: bool = True):
        self.selected_index = index
        if center and self.document:
            entry = next((item for item in self.document.entries if item.index == index), None)
            if entry:
                self.glide_to(srt_timestamp_seconds(entry.start))
                self._manual_pan = False
        self.update()

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

    def _track_width(self) -> float:
        return max(1.0, self.width() - 24.0)

    def _x(self, seconds: float) -> float:
        return 12.0 + (seconds - self._view_start()) / self.span * self._track_width()

    def _seconds(self, x: float) -> float:
        return max(0.0, self._view_start() + (x - 12.0) / self._track_width() * self.span)

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
            painter.setPen(QPen(faded(COLORS.subtle, self._wave.value), 1))
            center = 72
            if self.waveform_samples:
                for x in range(12, max(12, self.width() - 12), 2):
                    ratio = (x - 12) / max(1, self._track_width() - 1)
                    sample = self.waveform_samples[min(len(self.waveform_samples) - 1,
                                                        int(ratio * len(self.waveform_samples)))]
                    amplitude = max(1, int(sample * 24))
                    painter.drawLine(x, int(center - amplitude), x, int(center + amplitude))
            elif self.document:
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
                text = f"{minutes:02d}:{seconds:02d}"
                # 放不下的刻度只画刻线，不画被截断的标签
                if x + 3 + painter.fontMetrics().horizontalAdvance(text) <= self.width() - 12:
                    painter.drawText(int(x + 3), 0, 55, 17, Qt.AlignmentFlag.AlignLeft, text)
                painter.setPen(QPen(QColor(COLORS.divider), 1))
                painter.drawLine(int(x), 17, int(x), 25)
            tick += spacing
        for entry, _start, _end, rect in self._visible_blocks():
            key = entry.index
            selected = key in self.selected_indices
            primary = selected and key == self.selected_index
            current = key == self.playing_index
            hovered = key == self.hovered_index and not self._drag
            s = self._fx.value((key, "select"), 1.0 if selected else 0.0, motion.SELECT)
            m = self._fx.value((key, "primary"), 1.0 if primary else 0.0, motion.SELECT)
            p = self._fx.value((key, "play"), 1.0 if current else 0.0, motion.SELECT)
            h = self._fx.value((key, "hover"), 1.0 if hovered else 0.0,
                               motion.HOVER_IN if hovered else motion.HOVER_OUT)
            fill = mix(mix(mix(COLORS.raised, COLORS.button_hover, h), COLORS.playback, p),
                       COLORS.accent_tint, s)
            border = mix(mix(mix(COLORS.divider, COLORS.button_hover_border, h),
                             COLORS.playback_border, p), COLORS.accent, s)
            painter.setPen(QPen(border, 1 + m))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 4, 4)
            if rect.width() >= 28:
                painter.setPen(mix(COLORS.muted, COLORS.text, max(s, h * .6)))
                # 主选指示条出现时序号让出位置
                painter.drawText(rect.adjusted(5 + 4 * m, 0, -3, 0), Qt.AlignmentFlag.AlignVCenter,
                                 str(entry.index))
            if m > .01:
                length = (rect.height() - 6) * motion.ease(m)
                painter.fillRect(QRectF(rect.left() + 2, rect.center().y() - length / 2, 2, length),
                                 faded(COLORS.accent, m))
        if self._marquee:
            rectangle = QRectF(self._marquee[0], self._marquee[1],
                               self._marquee[2] - self._marquee[0],
                               self._marquee[3] - self._marquee[1]).normalized()
            self._paint_marquee(painter, rectangle, 1.0)
        elif self._marquee_last is not None and self._marquee_fade.value > .01:
            self._paint_marquee(painter, self._marquee_last, self._marquee_fade.value)
        marker = self._x(self._scrub["position"] if self._scrub else self._display_playhead())
        if 12 <= marker <= self.width() - 12:
            painter.setPen(QPen(QColor(COLORS.accent), 2))
            painter.drawLine(QPointF(marker, 19), QPointF(marker, self.height() - 1))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS.accent))
            painter.drawPolygon(QPolygonF([
                QPointF(marker - 7, 18), QPointF(marker + 7, 18), QPointF(marker, 25),
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
        # 吸附线：吸住时快速浮现，松开后缓慢退去
        if self.snap_guide is not None:
            self._guide_at = self.snap_guide
            self._guide.to(1.0, motion.PRESS)
        else:
            self._guide.to(0.0, motion.HOVER_OUT)
        if self._guide_at is not None and self._guide.value > .01:
            guide = self._x(self._guide_at)
            painter.setPen(QPen(faded(COLORS.accent, self._guide.value), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(guide, 0), QPointF(guide, self.height()))

    def _paint_marquee(self, painter, rectangle: QRectF, opacity: float):
        painter.setPen(QPen(faded(COLORS.accent, opacity), 1))
        painter.setBrush(faded(COLORS.accent, 35 / 255 * opacity))
        painter.drawRect(rectangle)

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
            if rect.left() <= x <= rect.right():
                edge = min(7.0, rect.width() / 3)
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
        if time.monotonic() - press["started"] < .18 or abs(delta_x) < 3:
            return False
        if press["kind"] == "playhead":
            self._scrub = {"position": self.playhead, "last_seek": 0.0}
            press["moved"] = True
            self._press = None
            return True
        self._drag = {
            "index": press["entry"].index, "mode": press["mode"], "x": press["x"],
            "start": press["start"], "end": press["end"], "before": press["before"],
            "moved": True, "last_seek": 0.0,
        }
        self._press = None
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
                self.setCursor(Qt.CursorShape.SizeHorCursor if hit[3] == "move"
                                else Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeHorCursor if self._on_playhead(
                    event.position().x(), event.position().y()) else Qt.CursorShape.ArrowCursor)
            hovered = hit[0].index if hit else None
            if hovered != self.hovered_index:
                self.hovered_index = hovered
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
        self.document.change_time(index, start, end, remember=False)
        self.preview_changed.emit(index)
        self.update()
        now = time.monotonic()
        if now - drag["last_seek"] >= .18:
            drag["last_seek"] = now
            self.preview_requested.emit(end if drag["mode"] == "right" else start)
        event.accept()

    def leaveEvent(self, event):
        self.hovered_index = None
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._blank:
            if self._marquee:
                # 松手后框选框留一瞬残影再淡出
                self._marquee_last = QRectF(self._marquee[0], self._marquee[1],
                                            self._marquee[2] - self._marquee[0],
                                            self._marquee[3] - self._marquee[1]).normalized()
                self._marquee_fade.snap(1.0)
                self._marquee_fade.to(0.0, motion.HOVER_OUT)
                self.marquee_selected.emit(self._marquee_hits())
            else:
                self.seek_requested.emit(min(self.duration or float("inf"),
                                             self._seconds(self._blank[0])))
            self._blank = None
            self._marquee = None
            self.update()
            event.accept()
            return
        if self._scrub:
            position = self._scrub["position"]
            self._scrub = None
            self.snap_engine.reset()
            self.snap_guide = None
            self.seek_requested.emit(position)
            self.update()
            event.accept()
            return
        if self._press:
            press = self._press
            self._press = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if press["kind"] == "playhead":
                self.seek_requested.emit(self.playhead)
            elif self.document:
                self.seek_requested.emit(press["start"])
            self.update()
            event.accept()
            return
        if not self._drag or not self.document:
            return super().mouseReleaseEvent(event)
        drag = self._drag
        self._drag = None
        if drag["moved"]:
            self.document.commit_time_preview(drag["before"])
            self.changed.emit(drag["index"])
            entry = next(item for item in self.document.entries
                         if item.index == drag["index"])
            self.preview_requested.emit(srt_timestamp_seconds(entry.start))
        self.snap_engine.reset()
        self.snap_guide = None
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
        goal = self._view.target
        center = goal[0] if goal else self.center
        span = math.exp(goal[1]) if goal else self.span
        # 连续滚动在目标值上累加，视图平滑追随
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.glide_to(span=max(4.0, min(120.0, span * (.8 if direction > 0 else 1.25))), duration=200)
        else:
            self.glide_to(center=max(0.0, min(self.duration or float("inf"), center - direction * span * .25)),
                          duration=200)
            self._manual_pan = True
        self.update()
        event.accept()
