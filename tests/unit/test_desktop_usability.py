"""新手走查（2026-11-10）发现的手感问题回归测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QScrollArea, QSpinBox, QVBoxLayout, QWidget

from autoslice.desktop.qt_preview import motion


def _wheel(widget, notches=-1):
    center = QPointF(widget.rect().center())
    event = QWheelEvent(center, QPointF(widget.mapToGlobal(center.toPoint())), QPoint(0, 0),
                        QPoint(0, 120 * notches), Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, event)


class WheelGuardTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        motion.install_app(self.app)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        self.combo = QComboBox()
        self.combo.addItems(["Qwen3-ASR", "FunASR", "其它"])
        self.spin = QSpinBox()
        self.spin.setRange(0, 300)
        self.spin.setValue(140)
        layout.addWidget(self.combo)
        layout.addWidget(self.spin)
        layout.addSpacing(2000)
        self.area.setWidget(page)
        self.area.resize(300, 200)
        self.area.show()
        self.app.processEvents()
        self.addCleanup(self.area.close)

    def test_unfocused_combo_and_spin_ignore_wheel(self):
        self.area.setFocus()
        _wheel(self.combo)
        _wheel(self.spin)
        self.app.processEvents()
        self.assertEqual(self.combo.currentText(), "Qwen3-ASR")
        self.assertEqual(self.spin.value(), 140)

    def test_guard_leaves_event_unaccepted_so_real_wheel_reaches_scroll_area(self):
        # 程序合成的滚轮事件 Qt 不向上冒泡；真实滚轮在“拦下且未接受”时会继续交给父控件
        self.area.setFocus()
        self.app.processEvents()
        self.assertFalse(self.combo.hasFocus())
        center = QPointF(self.combo.rect().center())
        event = QWheelEvent(center, center, QPoint(0, 0), QPoint(0, -120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        event.accept()
        self.assertTrue(self.app._wheel_guard.eventFilter(self.combo, event))
        self.assertFalse(event.isAccepted())

    def test_focused_spin_still_changes_with_wheel(self):
        self.spin.setFocus()
        self.app.processEvents()
        _wheel(self.spin, 1)
        self.app.processEvents()
        self.assertEqual(self.spin.value(), 141)


class LearningPanelSpeedTests(unittest.TestCase):
    def test_refresh_reads_each_streamer_profile_once(self):
        # 走查 N4：191 条纠错记忆曾逐条重读主播配置，进设置页卡 1 秒
        from autoslice.desktop.qt_app.learning_panel import LearningPanel

        QApplication.instance() or QApplication([])
        entry = SimpleNamespace(wrong="教个", right="交个", promoted=False, note="", kept=0, videos=1)
        memory = Mock()
        memory.entries.return_value = [("zeyin", entry)] * 120 + [("generic", entry)] * 80
        with patch("autoslice.desktop.qt_app.learning_panel.resolve_streamer_profile",
                   return_value=SimpleNamespace(label="泽音")) as resolve:
            panel = LearningPanel(memory)
        self.addCleanup(panel.deleteLater)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(panel.list.count(), 200)


class SubtitleFontProblemTests(unittest.TestCase):
    def test_reports_missing_font_with_actual_substitute(self):
        # 走查 N1：缺默认字幕字体时压制必失败，要说清缺哪个字体、被换成了什么
        from autoslice.desktop import subtitle_render

        with patch.object(subtitle_render, "verify_exact_subtitle_font",
                          return_value={"available": False, "resolved": "Arial-BoldMT"}):
            text = subtitle_render.subtitle_font_problem()
        self.assertIn(subtitle_render.EXACT_SUBTITLE_FONT, text)
        self.assertIn("Arial-BoldMT", text)
        with patch.object(subtitle_render, "verify_exact_subtitle_font", return_value={"available": True}):
            self.assertEqual(subtitle_render.subtitle_font_problem(), "")
        with patch.object(subtitle_render, "verify_exact_subtitle_font", side_effect=FileNotFoundError("ffmpeg")):
            self.assertIn("FFmpeg", subtitle_render.subtitle_font_problem())


class ArchiveMissingFolderTests(unittest.TestCase):
    def test_folders_missing_on_this_computer_are_marked(self):
        # 走查 N15：主力机的 F:、G: 盘路径照常列出，没有任何标记
        import tempfile

        from autoslice.desktop.qt_app.archive_panel import ArchivePanel

        QApplication.instance() or QApplication([])
        here = tempfile.mkdtemp()
        service = Mock()
        service.roots.return_value = [here, "Z:/不存在的旧切片"]
        service.root_defaults.return_value = {}
        service.streamer_choices.return_value = []
        service.summary.return_value = {"entries": 0}
        service.has_data.return_value = False
        panel = ArchivePanel(service, lambda action, callback: callback(action(), None))
        self.addCleanup(panel.deleteLater)
        panel.refresh()
        self.assertEqual(panel.folders.item(0).text(), here)
        self.assertIn("本机找不到", panel.folders.item(1).text())
        self.assertEqual(panel._selected_paths(), [])


class SubtitleFilterCacheTests(unittest.TestCase):
    def test_pending_filter_rows_stay_correct_after_edits_and_splits(self):
        # 改进清单 D4：“待确认”筛选下每画一格都重筛整份字幕，3000 条画一屏约 100 毫秒
        from dataclasses import replace

        from autoslice.desktop.qt_app.window_widgets import SubtitleTableModel
        from autoslice.desktop.subtitles import SubtitleEntry

        QApplication.instance() or QApplication([])
        entries = [SubtitleEntry(i, "00:00:01,000", "00:00:02,000", f"第{i}条", (i,)) for i in range(1, 11)]
        model = SubtitleTableModel()
        model.set_document(SimpleNamespace(entries=entries))
        model.set_pending_filter({2, 5, 9})
        self.assertEqual([model.entry_at(row).index for row in range(model.rowCount())], [2, 5, 9])
        entries[4] = replace(entries[4], text="改过")  # 改字：原位替换
        self.assertEqual(model.entry_at(1).text, "改过")
        entries[1:2] = [replace(entries[1], text="前半"), SubtitleEntry(11, "00:00:01,500", "00:00:02,000", "后半", (2,))]
        self.assertEqual(model.rowCount(), 3)  # 拆分：长度变了，重算
        self.assertEqual([model.entry_at(row).index for row in range(3)], [2, 5, 9])
        del entries[:]
        entries.extend(SubtitleEntry(i, "00:00:01,000", "00:00:02,000", "新", (i,)) for i in (9, 5, 2))
        self.assertEqual([model.entry_at(row).index for row in range(model.rowCount())], [9, 5, 2])
        model.set_pending_filter(None)
        self.assertEqual(model.rowCount(), 3)


if __name__ == "__main__":
    unittest.main()
