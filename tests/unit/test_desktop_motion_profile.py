"""两套主题的动效档与通用动效件：只验证逻辑与清理，不测手感。"""

from __future__ import annotations

import time
import unittest

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QProgressBar, QVBoxLayout, QWidget

from autoslice.desktop.qt_preview import motion, popups, theme


def _wait(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


class MotionProfileTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        motion.set_enabled(None)
        theme.apply_theme("night")

    def test_profile_follows_theme_and_morning_is_bolder_but_still_quick(self):
        theme.apply_theme("night")
        night = motion.profile()
        theme.apply_theme("morning")
        morning = motion.profile()
        self.assertIs(night, motion.NIGHT)
        self.assertIs(morning, motion.MORNING)
        self.assertGreater(morning.card_shift, night.card_shift)
        self.assertGreater(morning.menu_open, night.menu_open)
        self.assertTrue(morning.menu_clip)
        # 入场时长按设计稿（D-044）：夜航卡片 440ms、间隔 35ms；晨雾雾散 1200ms、卡片 900ms、间隔 80ms。
        # 入场只是视觉，页面一出现就能操作；悬停反馈仍要快
        self.assertEqual((night.card_enter, night.card_stagger, night.mist), (440, 35, 0))
        self.assertEqual((morning.card_enter, morning.card_stagger, morning.mist), (900, 80, 1200))
        for value in (night, morning):
            self.assertLessEqual(value.hover_in, 160)
            self.assertLessEqual(value.row_enter, 460)

    def test_menu_rows_appear_in_order(self):
        for name in ("night", "morning"):
            theme.apply_theme(name)
            self.assertEqual(popups._row_progress(0.0, 0), 0.0)
            self.assertEqual(popups._row_progress(1.0, 5), 1.0)
            self.assertGreater(popups._row_progress(0.3, 0), popups._row_progress(0.3, 4))

    def test_page_enter_overlay_is_click_through_and_cleans_up(self):
        motion.set_enabled(True)
        page = QWidget()
        page.resize(400, 300)
        layout = QVBoxLayout(page)
        cards = []
        for _ in range(3):
            card = QWidget()
            card.setObjectName("card")
            card.setMinimumHeight(60)
            layout.addWidget(card)
            cards.append(card)
        page.show()
        self.app.processEvents()
        motion.page_enter(page)
        overlays = page.findChildren(motion._Entrance)
        self.assertEqual(len(overlays), 1)
        self.assertTrue(overlays[0].testAttribute(motion.Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        _wait(self.app, 0.6)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(page.findChildren(motion._Entrance), [])
        page.close()

    def test_reduced_motion_skips_entrance(self):
        motion.set_enabled(False)
        page = QWidget()
        card = QWidget(page)
        card.setObjectName("card")
        page.show()
        motion.page_enter(page)
        self.assertEqual(page.findChildren(motion._Entrance), [])
        page.close()

    def test_progress_sheen_runs_only_while_in_progress(self):
        motion.set_enabled(True)
        host = QWidget()
        bar = QProgressBar(host)
        bar.setRange(0, 100)
        motion.install_tree(host)
        fx = bar.findChild(motion.ProgressFx)
        host.show()
        bar.setValue(0)
        self.assertFalse(fx.active())
        bar.setValue(40)
        self.assertTrue(fx.active())
        bar.setValue(100)
        self.assertFalse(fx.active())
        host.close()

    def test_modal_dialog_gets_veil_on_parent_and_veil_goes_away(self):
        motion.set_enabled(True)
        motion.install_app(self.app)
        window = QMainWindow()
        window.resize(500, 400)
        window.show()
        dialog = QDialog(window)
        dialog.setModal(True)
        dialog.show()
        self.app.processEvents()
        self.assertTrue(dialog.property("motionDialog"))
        self.assertEqual(len(window.findChildren(motion._Veil)), 1)
        # 关闭走 finished：撤遮罩、留快照淡出、透明度复原
        dialog.reject()
        _wait(self.app, 0.4)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(window.findChildren(motion._Veil), [])
        self.assertEqual(dialog.windowOpacity(), 1.0)
        window.close()

    def test_dialog_created_inside_qt_still_fades_in_after_garbage_collection(self):
        # QMessageBox.question 的对话框由 Qt 内部创建，没有常驻 Python 包装；动效对象若被回收，
        # 对话框会停在全透明、只剩遮罩（2026-11-26 实机发现）
        import gc

        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMessageBox

        motion.set_enabled(True)
        motion.install_app(self.app)
        window = QMainWindow()
        window.resize(500, 400)
        window.show()
        seen = {}

        def inspect():
            gc.collect()
            _wait(self.app, motion.profile().dialog_open / 1000 + 0.3)
            box = next(widget for widget in QApplication.topLevelWidgets()
                       if isinstance(widget, QMessageBox) and widget.isVisible())
            seen["opacity"] = box.windowOpacity()
            box.done(int(QMessageBox.StandardButton.No))

        QTimer.singleShot(0, inspect)
        QMessageBox.question(window, "恢复字幕草稿", "发现未正式保存的字幕草稿。是否恢复？")
        _wait(self.app, 0.3)
        self.assertEqual(seen.get("opacity"), 1.0)
        window.close()

    def test_skeleton_and_pulse_render_without_errors(self):
        motion.set_enabled(True)
        host = QWidget()
        host.resize(200, 80)
        skeleton = motion.Skeleton(host, delay=120)
        skeleton.resize(120, 60)
        host.show()
        self.assertFalse(skeleton.grab().isNull())
        motion.pulse(host)
        self.assertEqual(len(host.findChildren(motion._Pulse)), 1)
        host.close()


if __name__ == "__main__":
    unittest.main()
