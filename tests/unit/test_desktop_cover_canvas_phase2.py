"""AutoCover Phase 2 画布交互的真实 Qt/QTest 验收。"""

from __future__ import annotations

import unittest
from time import perf_counter

try:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QPixmap, QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时只保留模型测试
    QApplication = None

if QApplication is not None:
    from autoslice.desktop.cover_canvas import CoverCanvas

from autoslice.desktop.cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    LayoutProfile,
    Rect,
    TextObject,
    TextStyle,
    Transform,
    default_profiles,
)


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class CoverCanvasPhase2QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.canvas = CoverCanvas()
        self.canvas.resize(900, 600)
        self.canvas.set_background_pixmap(QPixmap(1200, 800))
        background = BackgroundObject(id="background-main", asset=AssetRef(path="frame.jpg"))
        text = TextObject(
            id="title-main",
            text="这是一个可以换行的标题",
            transform=Transform(x=0.2, y=0.2),
            rect=Rect(width=0.4, height=0.24),
            style=TextStyle(font_size=72),
        )
        self.document = CoverDocument(objects=(background, text), profiles=default_profiles())
        self.canvas.set_document(self.document, "4x3")
        self.canvas.show()
        self.app.processEvents()

    def tearDown(self):
        self.canvas.close()

    def test_text_drag_keeps_grab_offset(self):
        changes = []
        self.canvas.object_changed.connect(lambda item, _profile: changes.append(item))
        # 4:3 canvas is centered inside 900x600; text starts at about (270, 150).
        QTest.mousePress(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(310, 180))
        QTest.mouseMove(self.canvas, QPoint(390, 240))
        QTest.mouseRelease(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(390, 240))
        self.assertTrue(changes)
        self.assertGreater(changes[-1].transform.x, 0.2)
        self.assertGreater(changes[-1].transform.y, 0.2)

    def _select_text(self) -> TextObject:
        text = self.canvas._find_text()
        center = self.canvas._display_rect(text).center().toPoint()
        QTest.mouseClick(self.canvas, Qt.MouseButton.LeftButton, pos=center)
        self.assertEqual(self.canvas._selected_object, text.id)
        return self.canvas._find_text()

    def _drag(self, start: QPointF, end: QPoint):
        begin = start.toPoint()
        QTest.mousePress(self.canvas, Qt.MouseButton.LeftButton, pos=begin)
        QTest.mouseMove(self.canvas, end)
        QTest.mouseRelease(self.canvas, Qt.MouseButton.LeftButton, pos=end)

    def test_selection_box_hugs_text_instead_of_wrap_area(self):
        short = self.canvas._find_text()
        self.canvas.set_document(
            CoverDocument(
                objects=(self.document.objects[0], TextObject(
                    id="title-main", text="短", transform=short.transform,
                    rect=Rect(width=0.6, height=0.3), style=TextStyle(font_size=72),
                )),
                profiles=default_profiles(),
            ),
            "4x3",
        )
        text = self.canvas._find_text()
        ink, area = self.canvas._text_box(text)
        self.assertLess(ink.width, area.width * 0.4)
        self.assertLess(self.canvas._display_rect(text).width(), 0.6 * self.canvas._canvas_rect().width() * 0.4)

    def test_scale_handle_grows_font_and_wrap_width_together(self):
        changes = []
        self.canvas.object_changed.connect(lambda item, _profile: changes.append(item))
        text = self._select_text()
        handle = self.canvas._handle_points(text)["scale"]
        self._drag(handle, QPoint(round(handle.x()) + 90, round(handle.y()) + 50))
        self.assertTrue(changes)
        resized = changes[-1]
        self.assertIsInstance(resized, TextObject)
        self.assertGreater(resized.style.font_size, 72)
        ratio = resized.style.font_size / 72
        self.assertAlmostEqual(resized.rect.width, 0.4 * ratio, delta=0.02)
        # 左上角是锚点，缩放不移动位置。
        self.assertAlmostEqual(resized.transform.x, text.transform.x, places=6)
        self.assertEqual(self.canvas._selected_object, text.id)

    def test_width_handle_rewraps_without_changing_font_size(self):
        changes = []
        self.canvas.object_changed.connect(lambda item, _profile: changes.append(item))
        text = self._select_text()
        before_lines = len(self.canvas._text_layout_for(text).lines)
        handle = self.canvas._handle_points(text)["width-right"]
        self._drag(handle, QPoint(round(handle.x()) + 160, round(handle.y())))
        widened = changes[-1]
        self.assertEqual(widened.style.font_size, 72)
        self.assertGreater(widened.rect.width, 0.4)
        self.assertAlmostEqual(widened.wrap.max_width, widened.rect.width, places=6)
        self.assertLessEqual(len(self.canvas._text_layout_for(widened).lines), before_lines)

    def test_left_width_handle_keeps_right_edge(self):
        changes = []
        self.canvas.object_changed.connect(lambda item, _profile: changes.append(item))
        text = self._select_text()
        right = text.transform.x + text.rect.width
        handle = self.canvas._handle_points(text)["width-left"]
        self._drag(handle, QPoint(round(handle.x()) - 60, round(handle.y())))
        moved = changes[-1]
        self.assertLess(moved.transform.x, text.transform.x)
        self.assertAlmostEqual(moved.transform.x + moved.rect.width, right, places=3)

    def test_rotate_handle_turns_around_center_and_snaps(self):
        changes = []
        self.canvas.object_changed.connect(lambda item, _profile: changes.append(item))
        text = self._select_text()
        _rect, center, _angle = self.canvas._frame(text)
        handle = self.canvas._handle_points(text)["rotate"]
        # 把按钮绕中心转约 90°：(dx, dy) → (-dy, dx)。
        dx, dy = handle.x() - center.x(), handle.y() - center.y()
        target = QPoint(round(center.x() - dy), round(center.y() + dx))
        self._drag(handle, target)
        self.assertEqual(changes[-1].transform.rotation, 90.0)
        # 旋转后手柄跟着框转，命中仍然按旋转后的框计算。
        rotated = changes[-1]
        self.canvas.set_document(
            CoverDocument(objects=(self.document.objects[0], rotated), profiles=default_profiles(), selected_object_id=rotated.id),
            "4x3",
        )
        self.canvas.set_selected_object(rotated.id)
        moved = self.canvas._handle_points(rotated)["scale"]
        self.assertEqual(self.canvas._handle_at(moved)[0], "scale")

    def test_delete_and_duplicate_buttons_emit_requests_on_click(self):
        deleted, duplicated = [], []
        self.canvas.delete_requested.connect(deleted.append)
        self.canvas.duplicate_requested.connect(duplicated.append)
        text = self._select_text()
        handles = self.canvas._handle_points(text)
        QTest.mouseClick(self.canvas, Qt.MouseButton.LeftButton, pos=handles["duplicate"].toPoint())
        QTest.mouseClick(self.canvas, Qt.MouseButton.LeftButton, pos=handles["delete"].toPoint())
        self.assertEqual(duplicated, [text.id])
        self.assertEqual(deleted, [text.id])

    def test_text_drag_snaps_to_center_at_8px_and_only_shows_guides_during_drag(self):
        changes = []
        self.canvas.object_changed.connect(lambda item, _profile: changes.append(item))
        image = self.canvas._canvas_rect()
        text = self.canvas._find_text()
        rect = self.canvas._display_rect(text)
        # 按下点相对左上角保持 0.05 / 0.05，再把左上角拖到中心线附近 4px。
        press = QPoint(
            round(image.left() + (text.transform.x + 0.05) * image.width()),
            round(image.top() + (text.transform.y + 0.05) * image.height()),
        )
        centered_x = (image.width() - rect.width()) / (2 * image.width())
        centered_y = (image.height() - rect.height()) / (2 * image.height())
        target = QPoint(
            round(image.left() + (centered_x + 0.05) * image.width() + 4),
            round(image.top() + (centered_y + 0.05) * image.height() + 4),
        )
        QTest.mousePress(self.canvas, Qt.MouseButton.LeftButton, pos=press)
        QTest.mouseMove(self.canvas, target)
        self.assertTrue(self.canvas._guide_vertical)
        self.assertTrue(self.canvas._guide_horizontal)
        # 拖动帧只更新画布本地暂态对象；文档提交只发生在释放鼠标时。
        self.assertFalse(changes)
        QTest.mouseRelease(self.canvas, Qt.MouseButton.LeftButton, pos=target)
        self.assertEqual(len(changes), 1)
        self.assertAlmostEqual(changes[-1].transform.x, centered_x, places=3)
        self.assertAlmostEqual(changes[-1].transform.y, centered_y, places=3)
        self.assertFalse(self.canvas._guide_vertical)
        self.assertFalse(self.canvas._guide_horizontal)

    def test_many_mouse_moves_only_commit_once_after_release(self):
        changes = []
        self.canvas.object_changed.connect(lambda item, _profile: changes.append(item))
        rect = self.canvas._display_rect(self.canvas._find_text())
        start = QPoint(round(rect.center().x()), round(rect.center().y()))
        started = perf_counter()
        QTest.mousePress(self.canvas, Qt.MouseButton.LeftButton, pos=start)
        for index in range(1, 61):
            QTest.mouseMove(self.canvas, QPoint(start.x() + index, start.y() + index // 2))
        elapsed_ms = (perf_counter() - started) * 1000.0
        self.assertFalse(changes)
        QTest.mouseRelease(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(start.x() + 60, start.y() + 30))
        self.assertEqual(len(changes), 1)
        # 60 次本地重绘的测试链路应保持在交互级耗时，不得触发媒体任务。
        self.assertLess(elapsed_ms, 1000.0)

    def test_blank_click_switches_back_to_background_and_keyboard_micro_adjusts_text(self):
        text = self.canvas._find_text()
        rect = self.canvas._display_rect(text)
        point = QPoint(round(rect.center().x()), round(rect.center().y()))
        QTest.mouseClick(self.canvas, Qt.MouseButton.LeftButton, pos=point)
        before = self.canvas._find_text().transform.x
        QTest.keyClick(self.canvas, Qt.Key.Key_Right)
        self.assertAlmostEqual(self.canvas._find_text().transform.x, before + 0.005, places=3)
        QTest.keyClick(self.canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        self.assertAlmostEqual(self.canvas._find_text().transform.x, before + 0.025, places=3)
        QTest.mouseClick(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(round(self.canvas._canvas_rect().right() - 24), round(self.canvas._canvas_rect().bottom() - 24)))
        self.assertEqual(self.canvas._selected_object, "background-main")
        self.assertFalse(self.canvas._guide_vertical)

    def test_delete_key_hides_selected_text_like_legacy_delete_line(self):
        text = self.canvas._find_text()
        rect = self.canvas._display_rect(text)
        QTest.mouseClick(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(round(rect.center().x()), round(rect.center().y())))
        QTest.keyClick(self.canvas, Qt.Key.Key_Delete)
        self.assertFalse(self.canvas._find_text().visible)
        self.assertEqual(self.canvas._selected_object, "background-main")

    def test_background_drag_and_wheel_are_independent_from_text(self):
        positions = []
        zooms = []
        self.canvas.background_position_changed.connect(lambda x, y: positions.append((x, y)))
        self.canvas.zoom_changed.connect(zooms.append)
        QTest.mousePress(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(780, 480))
        QTest.mouseMove(self.canvas, QPoint(720, 430))
        QTest.mouseRelease(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(720, 430))
        QTest.mousePress(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(780, 480))
        QTest.mouseRelease(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(780, 480))
        # wheel is delivered to the selected background context
        wheel = QWheelEvent(QPointF(780, 480), QPointF(780, 480), QPoint(0, 120), QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(self.canvas, wheel)
        self.assertTrue(positions)
        self.assertGreater(positions[-1][0], 0.0)
        self.assertTrue(zooms)
        self.assertGreater(self.canvas._find_background().scale, 1.0)

    def test_ratio_profiles_keep_their_own_object_override(self):
        profile_43 = self.document.profiles["4x3"]
        profile_169 = self.document.profiles["16x9"]
        profile_43 = LayoutProfile(profile_43.key, profile_43.width, profile_43.height, profile_43.safe_area, {"title-main": {"transform": {"x": 0.1, "y": 0.15}}}, profile_43.export_suffix)
        profile_169 = LayoutProfile(profile_169.key, profile_169.width, profile_169.height, profile_169.safe_area, {"title-main": {"transform": {"x": 0.6, "y": 0.2}}}, profile_169.export_suffix)
        self.document = CoverDocument(objects=self.document.objects, profiles={"4x3": profile_43, "16x9": profile_169}, active_profile="4x3")
        self.canvas.set_document(self.document, "4x3")
        self.assertAlmostEqual(self.canvas._find_text().transform.x, 0.1)
        self.canvas.set_document(self.document, "16x9")
        self.assertAlmostEqual(self.canvas._find_text().transform.x, 0.6)


if __name__ == "__main__":
    unittest.main()
