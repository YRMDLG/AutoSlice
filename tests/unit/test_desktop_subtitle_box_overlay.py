"""视频上的字幕选框（D-049）：点选、拖动改位置、拖四角改字号，数值实时回到样式面板。"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, QPoint, QPointF, QSize, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None

from autoslice.subtitle_workflow import (
    DEFAULT_SUBTITLE_STYLE,
    jianying_from_pixels,
    subtitle_style_geometry,
)


class GeometryTests(unittest.TestCase):
    def test_pixels_and_jianying_units_round_trip(self):
        style = {**DEFAULT_SUBTITLE_STYLE, "x": 250.0, "y": -788.0, "font_size": 20.0}
        geometry = subtitle_style_geometry(style, 1920, 1080)
        # 原来的压制配置：1080p 下中心在 (960, 966)、字号 135px
        self.assertEqual((geometry["x"], geometry["y"], geometry["font_size"]), (1200, 966, 135.0))
        back = jianying_from_pixels(geometry["x"], geometry["y"], geometry["font_size"], 1920, 1080)
        self.assertAlmostEqual(back["x"], 250.0, delta=1.1)
        self.assertAlmostEqual(back["y"], -788.0, delta=1.1)
        self.assertAlmostEqual(back["font_size"], 20.0, places=3)


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class OverlayTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.qt_app.subtitle_box_overlay import SubtitleBoxOverlay

        self.app = QApplication.instance() or QApplication([])
        self.window = QWidget()
        self.window.resize(1000, 700)
        self.target = QWidget(self.window)
        self.target.setGeometry(20, 20, 960, 600)  # 16:9 的画面放进去：上下各留 30px 黑边
        self.window.show()
        self.addCleanup(self.window.close)
        self.state = {"style": dict(DEFAULT_SUBTITLE_STYLE), "text": "他想唱他想唱他想唱的",
                      "video_size": (1920, 1080), "editable": True}
        self.emitted = []

        test = self

        class Provider:
            def style_overlay_state(self):
                return dict(test.state) if test.state is not None else None

        self.overlay = SubtitleBoxOverlay(self.window, self.target, Provider())
        self.overlay.changed.connect(self._apply)
        self.overlay.set_active(True)
        self.addCleanup(self.overlay.set_active, False)
        self.app.processEvents()

    def _apply(self, values):
        # 模拟面板：收到就写回样式（面板会按步长取整，这里不取整）
        self.emitted.append(values)
        self.state["style"] = {**self.state["style"], **values}

    def _anchor(self):
        """字幕中心在选框窗口里的位置（画面缩放 0.5、上方黑边 30px）。"""

        geometry = subtitle_style_geometry(self.state["style"], 1920, 1080)
        return QPoint(round(geometry["x"] * 0.5), round(30 + geometry["y"] * 0.5))

    def _drag(self, start: QPoint, end: QPoint, modifiers=Qt.KeyboardModifier.NoModifier):
        QTest.mousePress(self.overlay, Qt.MouseButton.LeftButton, modifiers, start)
        for step in range(1, 5):
            point = QPointF(start.x() + (end.x() - start.x()) * step / 4, start.y() + (end.y() - start.y()) * step / 4)
            QApplication.sendEvent(self.overlay, QMouseEvent(
                QEvent.Type.MouseMove, point, point, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, modifiers))
        QTest.mouseRelease(self.overlay, Qt.MouseButton.LeftButton, modifiers, end)
        self.app.processEvents()

    def test_overlay_sits_on_the_video_and_hides_without_state(self):
        from autoslice.desktop.qt_app.subtitle_box_overlay import video_rect

        self.assertTrue(self.overlay.isVisible())
        self.assertEqual(self.overlay.size(), self.target.size())
        self.assertEqual(video_rect(QSize(960, 600), (1920, 1080)).toRect().getRect(), (0, 30, 960, 540))
        self.state = None
        self.overlay.sync()
        self.assertFalse(self.overlay.isVisible())

    def test_only_polls_while_active_and_repaints_only_on_change(self):
        from unittest.mock import patch

        # 面板关着：不轮询、不显示（原来定时器一直开着，每 120ms 整窗重画一次）
        self.overlay.set_active(False)
        self.assertFalse(self.overlay.isVisible())
        self.assertFalse(self.overlay._timer.isActive())
        self.overlay.set_active(True)
        self.assertTrue(self.overlay.isVisible())
        with patch.object(self.overlay, "update") as update:
            self.overlay.sync()
            self.overlay.sync()
            update.assert_not_called()
            self.state["text"] = "换了一句"
            self.overlay.sync()
            update.assert_called_once()

    def test_drag_box_moves_subtitle_and_snaps_to_center(self):
        anchor = self._anchor()
        # 往右上拖 100px（画面里就是 200px）：x 从 0 变到 200/960*1000 ≈ 208，y 往上加 200/540*1000 ≈ 370
        self._drag(anchor, QPoint(anchor.x() + 100, anchor.y() - 100))
        self.assertTrue(self.overlay.selected)
        last = self.emitted[-1]
        self.assertAlmostEqual(last["x"], 208.3, delta=2)
        self.assertAlmostEqual(last["y"], -788 + 370.4, delta=4)
        # 拖回离正中 3px 的地方：吸到正中（x = 0）；按住 Alt 不吸
        anchor = self._anchor()
        self._drag(anchor, QPoint(480 + 3, anchor.y()))
        self.assertEqual(self.emitted[-1]["x"], 0.0)
        anchor = self._anchor()
        self._drag(anchor, QPoint(480 + 3, anchor.y()), Qt.KeyboardModifier.AltModifier)
        self.assertAlmostEqual(self.emitted[-1]["x"], 6.25, delta=1)

    def test_corner_drag_scales_font_around_center(self):
        anchor = self._anchor()
        self._drag(anchor, anchor)  # 先点一下选中，四个角才有手柄
        box = self.overlay._box()
        corner = box.bottomRight().toPoint()
        # 角到中心的距离拉长一倍：字号翻倍（20 → 40，面板会再夹到 30 以内），中心不动
        far = QPoint(anchor.x() + (corner.x() - anchor.x()) * 2, anchor.y() + (corner.y() - anchor.y()) * 2)
        before = dict(self.state["style"])
        self._drag(corner, far)
        self.assertAlmostEqual(self.emitted[-1]["font_size"], before["font_size"] * 2, delta=0.6)
        self.assertEqual((self.state["style"]["x"], self.state["style"]["y"]), (before["x"], before["y"]))

    def test_locked_default_only_hints_and_click_outside_deselects(self):
        self.state["editable"] = False
        self.overlay.sync()
        hints = []
        self.overlay.locked_attempt.connect(lambda: hints.append(1))
        anchor = self._anchor()
        self._drag(anchor, QPoint(anchor.x() + 80, anchor.y()))
        self.assertEqual(hints, [1])
        self.assertEqual(self.emitted, [])
        QTest.mouseClick(self.overlay, Qt.MouseButton.LeftButton, pos=QPoint(40, 60))
        self.assertFalse(self.overlay.selected)


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class PanelNumbersTests(unittest.TestCase):
    def test_set_numbers_rounds_clamps_and_refreshes_once(self):
        import tempfile
        from pathlib import Path

        from autoslice.desktop.foundation import DesktopStorage
        from autoslice.desktop.qt_app.subtitle_style_panel import SubtitleStylePanel
        from autoslice.desktop.subtitle_styles import SubtitleStyleStore

        QApplication.instance() or QApplication([])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = SubtitleStyleStore(DesktopStorage(Path(temp.name)))
        panel = SubtitleStylePanel(store)
        seen = []
        panel.style_changed.connect(seen.append)
        # 默认样式锁着：不改
        self.assertFalse(panel.set_numbers({"x": 100}))
        panel.new_button.click()
        seen.clear()
        self.assertTrue(panel.set_numbers({"x": 123.6, "y": -1500, "font_size": 17.3}))
        self.assertEqual(len(seen), 1)
        style = panel.current_style()
        self.assertEqual((style["x"], style["y"], style["font_size"]), (124.0, -1000.0, 17.5))
        self.assertEqual(panel.numbers["x"].value(), 124.0)
        self.assertEqual(panel.numbers["font_size"].slider.value(), 35)


if __name__ == "__main__":
    unittest.main()
