"""Desktop-08.8.4 时间轴鼠标状态机回归。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None

from autoslice.desktop.projects import ProjectVideo
from autoslice.desktop.subtitles import SubtitleDocument, SubtitleEntry


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class SubtitleTimelineQtTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.qt_app.timeline import SubtitleTimeline

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        video = ProjectVideo(
            name="video.mp4",
            path=str(root / "video.mp4"),
            srt_path=str(root / "video.srt"),
            corrected_srt_path=str(root / "video_校对.srt"),
            has_source_srt=True,
            has_corrected_srt=False,
            subtitle_error="",
        )
        entries = [
            SubtitleEntry(1, "00:00:01,000", "00:00:03,000", "第一条", (1,)),
            SubtitleEntry(2, "00:00:04,000", "00:00:05,500", "第二条", (2,)),
        ]
        self.document = SubtitleDocument(video, [], entries, {})
        self.app = QApplication.instance() or QApplication([])
        self.timeline = SubtitleTimeline()
        self.timeline.resize(800, 120)
        self.timeline.set_document(self.document)
        self.timeline.set_duration(10.0)
        self.timeline.span = 10.0
        self.timeline.center = 5.0
        self.timeline.show()
        self.app.processEvents()
        self.addCleanup(self.timeline.close)

    def _block(self, cue_id=1):
        return next(item for item in self.timeline._visible_blocks() if item[0].index == cue_id)

    def test_body_click_selects_without_seeking_or_moving_view(self):
        entry, start, end, rect = self._block()
        before = tuple(self.document.entries)
        seeks = []
        selections = []
        self.timeline.seek_requested.connect(seeks.append)
        self.timeline.selection_requested.connect(
            lambda cue_id, ctrl, shift: selections.append((cue_id, ctrl, shift))
        )
        view_start = self.timeline._view_start()
        point = QPoint(int(rect.center().x()), int(rect.center().y()))

        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseRelease(self.timeline, Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()

        self.assertEqual(tuple(self.document.entries), before)
        self.assertIsNone(self.timeline._drag)
        self.assertFalse(self.timeline._mouse_grabbed)
        self.assertEqual(seeks, [])
        self.assertTrue(selections)
        self.assertEqual(selections[-1][0], entry.index)
        self.assertAlmostEqual(self.timeline._view_start(), view_start, delta=0.001)

    def test_body_drag_activates_without_hold_and_release_clears_state(self):
        _entry, _start, _end, rect = self._block()
        point = QPoint(int(rect.center().x()), int(rect.center().y()))
        moved = QPoint(point.x() + 35, point.y())
        original_start = self.document.entries[0].start
        previews = []
        seeks = []
        self.timeline.preview_requested.connect(previews.append)
        self.timeline.seek_requested.connect(seeks.append)

        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(self.timeline, moved)
        self.app.processEvents()
        self.assertIsNotNone(self.timeline._drag)
        self.assertEqual(self.timeline._drag["mode"], "move")

        QTest.mouseRelease(self.timeline, Qt.MouseButton.LeftButton, pos=moved)
        self.app.processEvents()

        self.assertNotEqual(self.document.entries[0].start, original_start)
        self.assertIsNone(self.timeline._drag)
        self.assertIsNone(self.timeline._press)
        self.assertFalse(self.timeline._mouse_grabbed)
        self.assertEqual(previews, [])
        self.assertEqual(seeks, [])

    def test_edge_resize_does_not_move_playhead_or_request_seek_preview(self):
        _entry, _start, _end, rect = self._block()
        self.timeline.playhead = 7.0
        previews = []
        seeks = []
        self.timeline.preview_requested.connect(previews.append)
        self.timeline.seek_requested.connect(seeks.append)
        point = QPoint(int(rect.left() + 2), int(rect.center().y()))
        moved = QPoint(point.x() + 25, point.y())

        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(self.timeline, moved)
        self.app.processEvents()
        self.assertIsNotNone(self.timeline._drag)
        self.assertEqual(self.timeline._drag["mode"], "left")

        QTest.mouseRelease(self.timeline, Qt.MouseButton.LeftButton, pos=moved)
        self.app.processEvents()

        self.assertEqual(self.timeline.playhead, 7.0)
        self.assertEqual(previews, [])
        self.assertEqual(seeks, [])
        self.assertIsNone(self.timeline._drag)

    def test_edge_click_without_drag_does_not_seek_playhead(self):
        _entry, _start, _end, rect = self._block()
        seeks = []
        self.timeline.seek_requested.connect(seeks.append)
        point = QPoint(int(rect.left() + 2), int(rect.center().y()))

        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseRelease(self.timeline, Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()

        self.assertEqual(seeks, [])

    def test_resize_cursor_only_appears_at_edges(self):
        _entry, _start, _end, rect = self._block()
        y = int(rect.center().y())

        QTest.mouseMove(self.timeline, QPoint(int(rect.left() + 2), y))
        self.app.processEvents()
        self.assertEqual(self.timeline.cursor().shape(), Qt.CursorShape.SizeHorCursor)
        self.assertEqual(self.timeline.hovered_mode, "left")

        QTest.mouseMove(self.timeline, QPoint(int(rect.center().x()), y))
        self.app.processEvents()
        self.assertEqual(self.timeline.cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.assertEqual(self.timeline.hovered_mode, "move")

        QTest.mouseMove(self.timeline, QPoint(int(rect.right() - 2), y))
        self.app.processEvents()
        self.assertEqual(self.timeline.cursor().shape(), Qt.CursorShape.SizeHorCursor)
        self.assertEqual(self.timeline.hovered_mode, "right")

    def test_window_deactivate_finishes_active_drag(self):
        _entry, _start, _end, rect = self._block()
        point = QPoint(int(rect.center().x()), int(rect.center().y()))

        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(self.timeline, QPoint(point.x() + 30, point.y()))
        self.app.processEvents()
        self.assertIsNotNone(self.timeline._drag)

        QApplication.sendEvent(self.timeline, QEvent(QEvent.Type.WindowDeactivate))
        self.app.processEvents()

        self.assertIsNone(self.timeline._drag)
        self.assertIsNone(self.timeline._scrub)
        self.assertIsNone(self.timeline._press)
        self.assertFalse(self.timeline._mouse_grabbed)

    def test_blank_press_seeks_immediately(self):
        seeks = []
        self.timeline.seek_requested.connect(seeks.append)
        point = QPoint(int(self.timeline._x(7.0)), 90)

        QTest.mousePress(self.timeline, Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()

        self.assertTrue(seeks)
        self.assertAlmostEqual(seeks[-1], 7.0, delta=0.05)
        QTest.mouseRelease(self.timeline, Qt.MouseButton.LeftButton, pos=point)

    def test_mouse_side_pan_uses_twenty_percent_of_visible_span(self):
        self.timeline.duration = 20.0
        self.timeline.span = 5.0
        self.timeline.set_view_start(5.0)

        self.timeline.pan_view(1)
        self.assertAlmostEqual(self.timeline._view_start(), 6.0, delta=0.01)

        self.timeline.pan_view(-1)
        self.assertAlmostEqual(self.timeline._view_start(), 5.0, delta=0.01)

    def test_continuous_pan_speed_scales_with_visible_span(self):
        self.timeline.duration = 100.0
        self.timeline.span = 10.0
        self.timeline.set_view_start(20.0)
        self.timeline.pan_view_by_time(1, 0.5, speed=0.8)
        self.assertAlmostEqual(self.timeline._view_start(), 24.0, delta=0.01)

        self.timeline.span = 5.0
        self.timeline.set_view_start(20.0)
        self.timeline.pan_view_by_time(1, 0.5, speed=0.8)
        self.assertAlmostEqual(self.timeline._view_start(), 22.0, delta=0.01)

    def test_side_pan_controller_moves_while_held_and_stops_on_release(self):
        from autoslice.desktop.qt_app.timeline import TimelineSidePanController

        class Probe:
            def __init__(self):
                self.calls = []

            def pan_view_by_time(self, direction, elapsed, *, speed):
                self.calls.append((direction, elapsed, speed))

        probe = Probe()
        controller = TimelineSidePanController(probe)
        controller.start(-1)
        self.assertTrue(controller.timer.isActive())
        self.assertEqual(probe.calls[0][0], -1)

        QTest.qWait(controller.interval_ms * 3 + 10)
        self.app.processEvents()
        self.assertGreaterEqual(len(probe.calls), 2)

        controller.stop()
        stopped_count = len(probe.calls)
        self.assertFalse(controller.timer.isActive())
        QTest.qWait(controller.interval_ms * 2 + 10)
        self.app.processEvents()
        self.assertEqual(len(probe.calls), stopped_count)

    def test_side_pan_controller_stops_when_window_deactivates(self):
        from autoslice.desktop.qt_app.timeline import TimelineSidePanController

        class Probe:
            def pan_view_by_time(self, *_args, **_kwargs):
                return None

        controller = TimelineSidePanController(Probe())
        controller.start(1)
        self.assertTrue(controller.timer.isActive())
        self.assertTrue(controller.stop_for_event(QEvent.Type.WindowDeactivate))
        self.assertFalse(controller.timer.isActive())
        self.assertEqual(controller.direction, 0)
        self.assertFalse(controller.stop_for_event(QEvent.Type.WindowDeactivate))

    def test_view_start_can_be_driven_by_scrollbar(self):
        self.timeline.duration = 20.0
        self.timeline.span = 5.0
        changes = []
        self.timeline.view_changed.connect(lambda start, span, duration: changes.append(
            (start, span, duration)
        ))

        self.timeline.set_view_start(7.5)

        self.assertAlmostEqual(self.timeline._view_start(), 7.5, delta=0.01)
        self.assertTrue(self.timeline._manual_pan)
        self.assertTrue(changes)
        self.assertAlmostEqual(changes[-1][0], 7.5, delta=0.01)
        self.assertAlmostEqual(changes[-1][1], 5.0, delta=0.01)
        self.assertAlmostEqual(changes[-1][2], 20.0, delta=0.01)

    def test_waveform_follows_horizontal_zoom_window(self):
        # 100 个样本覆盖 10 秒，振幅随时间单调增加，便于验证可视窗口映射。
        self.timeline.waveform_samples = [index / 99 for index in range(100)]
        self.timeline.duration = 10.0
        midpoint = 12.0 + self.timeline._track_width() / 2

        self.timeline.center = 5.0
        self.timeline.span = 10.0
        full_view = self.timeline._waveform_amplitude(midpoint)

        self.timeline.center = 8.0
        self.timeline.span = 2.0
        zoomed_view = self.timeline._waveform_amplitude(midpoint)

        self.assertGreater(zoomed_view, full_view + 0.2)
        self.assertAlmostEqual(full_view, 0.5, delta=0.08)
        self.assertAlmostEqual(zoomed_view, 0.8, delta=0.08)


if __name__ == "__main__":
    unittest.main()
