"""消息框换皮（D-046）：QMessageBox 显示前换成设计稿的确认框卡片，调用处和按钮语义不变。"""

from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QPushButton
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None


def _wait(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class MessageSkinTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.qt_preview import motion

        self.app = QApplication.instance() or QApplication([])
        motion.install_app(self.app)
        self.window = QMainWindow()
        self.window.resize(600, 400)
        self.window.show()
        self.addCleanup(self.window.close)

    def _visible_box(self):
        return next(widget for widget in QApplication.topLevelWidgets()
                    if isinstance(widget, QMessageBox) and widget.isVisible())

    @staticmethod
    def _buttons(box):
        buttons = box.findChildren(QPushButton)
        return sorted(buttons, key=lambda b: b.mapTo(box, b.rect().topLeft()).x())

    def _order(self, box):
        return [button.text() for button in self._buttons(box)]

    def test_question_becomes_card_and_enter_still_means_yes(self):
        seen = {}

        def inspect():
            _wait(self.app, 0.3)
            box = self._visible_box()
            seen["frameless"] = bool(box.windowFlags() & Qt.WindowType.FramelessWindowHint)
            seen["title"] = box.text()
            seen["body"] = box.informativeText()
            seen["order"] = [box.standardButton(button) for button in self._buttons(box)]
            seen["primary"] = [box.standardButton(b) for b in box.findChildren(QPushButton) if b.objectName() == "primary"]
            QTest.keyClick(box, Qt.Key.Key_Return)

        QTimer.singleShot(0, inspect)
        answer = QMessageBox.question(self.window, "AI 检查字幕", "会消耗额度。\n开始吗？")
        self.assertTrue(seen["frameless"])
        # 标题挪进卡片，正文成了说明（带行高的富文本，换行还在）
        self.assertEqual(seen["title"], "AI 检查字幕")
        self.assertIn("会消耗额度。<br>开始吗？", seen["body"])
        # 取消在左、确认在右；“是”是主按钮，回车就是“是”
        self.assertEqual(seen["order"], [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])
        self.assertEqual(seen["primary"], [QMessageBox.StandardButton.Yes])
        self.assertEqual(answer, QMessageBox.StandardButton.Yes)

    def test_escape_still_cancels(self):
        QTimer.singleShot(0, lambda: (_wait(self.app, 0.2), QTest.keyClick(self._visible_box(), Qt.Key.Key_Escape)))
        answer = QMessageBox.question(self.window, "停止话题分析", "现在停止吗？")
        self.assertEqual(answer, QMessageBox.StandardButton.No)

    def test_custom_buttons_keep_roles_default_and_order(self):
        box = QMessageBox(self.window)
        box.setWindowTitle("未保存的字幕")
        box.setText("当前字幕有未正式保存的修改。")
        keep = box.addButton("保留草稿并离开", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("放弃修改并离开", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        seen = {}

        def inspect():
            _wait(self.app, 0.2)
            seen["order"] = self._order(box)
            seen["default"] = box.defaultButton()
            QTest.keyClick(box, Qt.Key.Key_Escape)

        QTimer.singleShot(0, inspect)
        box.exec()
        # 危险操作单独靠左，取消、稳妥的那个在右；默认按钮沿用原来回车落到的“保留草稿”
        self.assertEqual(seen["order"], ["放弃修改并离开", "取消", "保留草稿并离开"])
        self.assertIs(seen["default"], keep)
        self.assertIs(box.clickedButton(), cancel)

    def test_box_without_title_shows_body_only(self):
        seen = {}

        def inspect():
            _wait(self.app, 0.2)
            box = self._visible_box()
            seen["text"] = box.text()
            box.done(0)

        QTimer.singleShot(0, inspect)
        QMessageBox.information(self.window, "", "现在没有待确认的建议。")
        self.assertEqual(seen["text"], "现在没有待确认的建议。")


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class PanelPaintTests(unittest.TestCase):
    def tearDown(self):
        from autoslice.desktop.qt_preview import theme

        theme.apply_theme("night")

    def test_panel_body_keeps_its_own_color(self):
        # 原来描边前没清笔刷，drawPath 又用半透明投影色涂了一遍：晨雾白卡成了 #f3f1ed，夜航菜单也偏暗
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter

        from autoslice.desktop.qt_preview import popups, theme

        QApplication.instance() or QApplication([])
        for name in ("morning", "night"):
            theme.apply_theme(name)
            for fill in (theme.COLORS.dialog, None):
                image = QImage(200, 120, QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(0)
                painter = QPainter(image)
                popups.paint_panel(painter, QRectF(20, 10, 160, 90), 16, fill=fill)
                painter.end()
                want = (fill or theme.COLORS.overlay).lower()
                self.assertEqual(image.pixelColor(100, 50).name(), want, f"{name} {fill}")


if __name__ == "__main__":
    unittest.main()
