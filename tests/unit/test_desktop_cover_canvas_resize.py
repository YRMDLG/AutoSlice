"""拉大/改宽文本框：手势中复用缓存的文字图，松手后画面必须与重新渲染逐像素一致。"""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageChops

try:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent, QPixmap
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None

from autoslice.desktop.cover_draft import CoverDraft


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class CanvasResizeTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.background = self.root / "bg.png"
        Image.new("RGB", (1440, 1080), (120, 170, 210)).save(self.background)
        self.document = CoverDraft("懂姐小音告诉你韩娱特殊操作与内幕", str(self.background), font_size=120).to_document()
        self.canvas = self._canvas(CoverCanvas)
        self.text = self.canvas._find_text()
        self.canvas.set_selected_object(self.text.id)
        self.app.processEvents()

    def _canvas(self, cls):
        canvas = cls()
        self.addCleanup(canvas.close)
        canvas.resize(960, 760)
        canvas.set_background_pixmap(QPixmap(str(self.background)))
        canvas.set_document(self.document, "4x3")
        canvas.show()
        return canvas

    def _send(self, kind, point):
        QApplication.sendEvent(self.canvas, QMouseEvent(
            kind, QPointF(point), QPointF(point), Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ))

    def _drag(self, handle, dx, dy, frames):
        start = self.canvas._handle_points(self.canvas._find_text())[handle]
        self._send(QMouseEvent.Type.MouseButtonPress, start)
        for index in range(1, frames + 1):
            self._send(QMouseEvent.Type.MouseMove, QPointF(start.x() + dx * index, start.y() + dy * index))
            self.canvas.repaint()
        self._send(QMouseEvent.Type.MouseButtonRelease, QPointF(start.x() + dx * frames, start.y() + dy * frames))
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

    def _grab(self, canvas, name):
        path = self.root / name
        canvas.grab().save(str(path))
        return Image.open(path).convert("RGB")

    def _assert_matches_fresh_render(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        fresh = CoverCanvas()
        self.addCleanup(fresh.close)
        fresh.resize(960, 760)
        fresh.set_background_pixmap(QPixmap(str(self.background)))
        fresh.set_document(self.canvas._document, "4x3")
        fresh.set_selected_object(self.text.id)
        fresh.show()
        self.app.processEvents()
        self.assertIsNone(ImageChops.difference(self._grab(self.canvas, "a.png"), self._grab(fresh, "b.png")).getbbox())

    def test_scale_handle_ends_pixel_identical_to_fresh_render(self):
        before = self.canvas._find_text().style.font_size
        self._drag("scale", 4, 3, 20)
        self.assertGreater(self.canvas._find_text().style.font_size, before)
        self._assert_matches_fresh_render()

    def test_width_handle_across_line_breaks_ends_pixel_identical(self):
        breaks = set()
        start = self.canvas._handle_points(self.canvas._find_text())["width-right"]
        self._send(QMouseEvent.Type.MouseButtonPress, start)
        for index in range(1, 40):
            self._send(QMouseEvent.Type.MouseMove, QPointF(start.x() - 8 * index, start.y()))
            self.canvas.repaint()
            breaks.add(tuple(line.text for line in self.canvas._text_layout_for(self.canvas._find_text()).lines))
        self._send(QMouseEvent.Type.MouseButtonRelease, QPointF(start.x() - 8 * 39, start.y()))
        self.app.processEvents()
        self.assertGreater(len(breaks), 1)
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self._assert_matches_fresh_render()

    def test_rewrap_shift_only_when_lines_stay_the_same(self):
        text = self.canvas._find_text()
        wider = replace(text, rect=replace(text.rect, width=text.rect.width + 0.01),
                        wrap=replace(text.wrap, max_width=min(1.0, text.wrap.max_width + 0.01)))
        narrow = replace(text, rect=replace(text.rect, width=0.2), wrap=replace(text.wrap, max_width=0.2))
        self.assertIsNotNone(self.canvas._rewrap_shift(text, wider))
        self.assertIsNone(self.canvas._rewrap_shift(text, narrow))
        self.assertIsNone(self.canvas._rewrap_shift(text, replace(wider, text="别的字")))


if __name__ == "__main__":
    unittest.main()
