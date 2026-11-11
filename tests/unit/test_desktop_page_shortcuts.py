"""字幕校对连续操作中的焦点、快捷键和播放状态回归。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class PageShortcutTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.foundation import DesktopStorage
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        folder = root / "中文项目"
        folder.mkdir()
        (folder / "视频.mp4").touch()
        (folder / "视频.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n第一句\n\n2\n00:00:03,000 --> 00:00:04,000\n第二句\n",
            encoding="utf-8",
        )
        self.app = QApplication.instance() or QApplication([])
        with patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("测试无播放器")):
            self.window = DesktopWindow(SubmissionProjectService(root), DesktopStorage(root / "app-data"))
        self.window.show()

        def close_window():
            self.wait_for(lambda: not self.window._jobs and not self.window.cover_editor._jobs)
            self.window._resolve_unsaved = lambda: True
            self.window.close()

        self.addCleanup(close_window)
        self.wait_for(lambda: bool(self.window.project_buttons))
        project = next(item for item in self.window.service.snapshot.projects if item.title == "中文项目")
        self.window._select_real_project(project)
        self.wait_for(lambda: self.window.document is not None)
        self.window.activateWindow()
        self.wait_for(lambda: self.app.activeWindow() is self.window)
        self.deleted = []
        self.window.commands.register("delete_selected_cues", lambda: self.deleted.append(True))

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.01)
        self.fail("等待界面状态完成超时")

    def press(self, key, modifier=Qt.KeyboardModifier.NoModifier):
        # 从窗口句柄发送，经过 Qt 的快捷键分发，和真实键盘一致。
        QTest.keyClick(self.window.windowHandle(), key, modifier)
        self.app.processEvents()

    def open_text(self, row=0):
        index = self.window.model.index(row, 2)
        point = self.window.table.visualRect(index).topLeft() + QPoint(40, 12)
        QTest.mouseClick(self.window.table.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()
        self.assertTrue(self.window._subtitle_editor_open())
        return self.window._active_subtitle_editor()

    def use_player(self):
        self.window.player = Mock()
        self.window._media_ready = True
        self.window._player_duration = 30.0
        return self.window.player

    def test_cover_page_keys_do_not_reach_subtitle_commands(self):
        self.window._select_page(1)
        self.window.cover_editor.canvas.setFocus()
        self.press(Qt.Key.Key_Delete)
        self.assertEqual(self.deleted, [])
        self.window._select_page(0)
        self.window.table.setFocus()
        self.press(Qt.Key.Key_Delete)
        self.assertEqual(self.deleted, [True])

    def test_cover_page_undo_uses_cover_history(self):
        editor = self.window.cover_editor
        self.window._select_page(1)
        self.wait_for(lambda: editor.document is not None)
        subtitle_undo = []
        self.window.commands.register("undo", lambda: subtitle_undo.append(True))
        before = editor.document
        editor._toggle_frame_lock(True)
        self.assertNotEqual(editor.document, before)
        editor.canvas.setFocus()
        self.press(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(subtitle_undo, [])
        self.assertFalse(editor.document.source.frame_locked)

    def test_subtitle_text_then_timeline_restores_playback_shortcut(self):
        self.use_player()
        timeline = self.window.timeline
        for y in (14, timeline.height() // 2, timeline.height() - 5):
            with self.subTest(timeline_y=y):
                self.window.player.reset_mock()
                self.open_text()
                self.assertFalse(self.window._shortcuts["Space"].isEnabled())
                QTest.mouseClick(timeline, Qt.MouseButton.LeftButton,
                                pos=QPoint(timeline.width() * 3 // 4, y))
                self.app.processEvents()
                self.assertFalse(self.window._subtitle_editor_open())
                self.assertTrue(timeline.hasFocus())
                for key in ("Space", "Delete", "Q", "W", "Ctrl+B"):
                    self.assertTrue(self.window._shortcuts[key].isEnabled())
                self.press(Qt.Key.Key_Space)
                self.window.player.play.assert_called_once_with()
                self.press(Qt.Key.Key_Space)
                self.window.player.pause.assert_called_once_with()

    def test_text_space_and_delete_keep_text_scope_then_escape_restores_playback(self):
        player = self.use_player()
        editor = self.open_text()
        editor.selectAll()
        self.press(Qt.Key.Key_Space)
        self.assertEqual(editor.toPlainText(), " ")
        self.assertEqual(self.deleted, [])
        player.play.assert_not_called()
        editor.selectAll()
        self.press(Qt.Key.Key_Delete)
        self.assertEqual(editor.toPlainText(), "")
        self.assertEqual(self.deleted, [])
        editor.insertPlainText("编辑后播放")
        # 回车现在是“确认并编辑下一句”，退出编辑用 Esc
        self.press(Qt.Key.Key_Escape)
        self.assertFalse(self.window._subtitle_editor_open())
        self.press(Qt.Key.Key_Space)
        player.play.assert_called_once_with()
        self.assertEqual(self.window.document.entries[0].text, "编辑后播放")

    def test_switch_text_row_then_filter_restores_shortcuts(self):
        self.use_player()
        self.open_text().insertPlainText("修改")
        editor = self.open_text(1)
        self.assertTrue(self.window._is_subtitle_editor_widget(self.app.focusWidget()))
        self.assertFalse(self.window._shortcuts["Space"].isEnabled())
        self.assertIn("修改", self.window.document.entries[0].text)
        editor.insertPlainText("第二处")
        QTest.mouseClick(self.window.filter_pending, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertFalse(self.window._subtitle_editor_open())
        QTest.mouseClick(self.window.filter_all, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.press(Qt.Key.Key_Space)
        self.window.player.play.assert_called_once_with()
        self.assertIn("第二处", self.window.document.entries[1].text)

    def test_manual_seek_cancels_old_audition_stop(self):
        player = self.use_player()
        self.window._play_window(1.0, 2.0)
        self.window._seek_to(15.0)
        self.window._player_status({"position": 15.0, "paused": True})
        self.assertEqual(self.window._player_position, 15.0)
        self.assertIsNone(self.window._audition_until)
        self.window._toggle_play()
        self.window._player_status({"position": 15.2, "paused": False})
        self.assertEqual(self.window._player_position, 15.2)
        self.assertFalse(self.window._player_paused)
        player.pause.assert_not_called()

    def test_normal_play_pause_exits_one_shot_audition(self):
        player = self.use_player()
        self.window._play_window(1.0, 2.0)
        self.window._toggle_play()
        self.window._toggle_play()
        self.window._player_status({"position": 1.0, "paused": False})
        self.window._player_status({"position": 2.1, "paused": False})
        self.assertFalse(self.window._player_paused)
        self.assertIsNone(self.window._audition_until)
        player.pause.assert_called_once_with()

    def test_audition_end_keeps_local_pause_until_next_poll(self):
        self.use_player()
        self.window._play_window(1.0, 2.0)
        self.window._player_status({"position": 1.0, "paused": False})
        self.window._player_status({"position": 2.1, "paused": False})
        self.assertTrue(self.window._player_paused)
        self.assertEqual(self.window.play_button.text(), "播放")
        self.assertEqual(self.window._player_position, 2.0)

    def test_seek_outside_loop_exits_loop_and_seek_inside_keeps_it(self):
        player = self.use_player()
        self.window._cue_clicked(self.window.model.index(0, 0))
        self.window._set_loop(True)
        self.window._seek_to(1.5)
        self.assertTrue(self.window._loop_enabled)
        self.window._seek_to(15.0)
        self.window._toggle_play()
        self.window._player_status({"position": 15.0, "paused": False})
        self.assertFalse(self.window._loop_enabled)
        player.seek.assert_called_with(15.0, pause=True)

    def test_loop_follows_subtitle_list_selection(self):
        self.use_player()
        self.window._cue_clicked(self.window.model.index(0, 0))
        self.window._set_loop(True)
        self.window._cue_clicked(self.window.model.index(1, 0))
        self.assertTrue(self.window._loop_enabled)
        self.assertEqual(self.window._loop_cue_id, 2)

    def test_loading_video_clears_previous_playback_range_and_position(self):
        self.use_player()
        video = self.window.document.video
        self.window._play_window(1.0, 2.0)
        self.window._set_loop(True)
        self.window._seek_guard_target = 20.0
        self.window._load_video(video)
        self.assertIsNone(self.window._audition_until)
        self.assertFalse(self.window._loop_enabled)
        self.assertIsNone(self.window._loop_cue_id)
        self.assertIsNone(self.window._seek_guard_target)
        self.assertEqual(self.window.timeline.playhead, 0.0)
        self.assertEqual(self.window.timeline.duration, 0.0)

    def show_suggestions(self):
        from autoslice.desktop.ai_review import AIReviewSession, Suggestion, document_hash

        self.use_player()
        content = document_hash(self.window.document.entries)
        self.window.ai_session = AIReviewSession([
            Suggestion("first", 1, "第一句", "第一句话", "测试建议", None),
            Suggestion("second", 2, "第二句", "第二句话", "测试建议", None),
        ], content[:20], content)
        self.window._render_ai()
        self.window._set_ai_open(True)
        self.app.processEvents()

    def test_dynamic_suggestion_button_finishes_text_edit_before_locating(self):
        self.show_suggestions()
        self.open_text().insertPlainText("修改")
        button = self.window.ai_queue_layout.itemAt(1).widget()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertFalse(self.window._subtitle_editor_open())
        self.assertTrue(self.window._shortcuts["Space"].isEnabled())
        self.assertIn("修改", self.window.document.entries[0].text)
        self.assertEqual(self.window.selected_index, 2)

    def test_pending_queue_refresh_finishes_editor_before_resetting_rows(self):
        self.show_suggestions()
        self.window._set_filter(True)
        self.open_text().insertPlainText("修改")
        self.window._render_ai()
        self.app.processEvents()
        self.assertFalse(self.window._subtitle_editor_open())
        self.assertTrue(self.window._shortcuts["Space"].isEnabled())
        self.assertIn("修改", self.window.document.entries[0].text)
        self.window._select_ai("second")
        self.window._accept_ai()
        self.assertEqual(self.window.document.entries[1].text, "第二句话")
        self.window.table.setFocus()
        self.press(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.document.entries[1].text, "第二句")
        self.assertIn("修改", self.window.document.entries[0].text)

    def test_loading_video_drops_selection_pending_filter_and_ai_history(self):
        self.show_suggestions()
        self.window._set_filter(True)
        self.window.selection.choose(2, self.window._cue_order())
        self.window.selected_index = 2
        self.window._ai_skip_undo.append(("old-content", "first"))
        video = self.window.document.video
        self.window._load_video(video)
        self.assertIsNone(self.window.selection.active)
        self.assertEqual(self.window.selection.selected, set())
        self.assertIsNone(self.window.model.pending_ids)
        self.assertEqual(self.window._ai_skip_undo, [])
        self.wait_for(lambda: self.window.document is not None and not self.window._jobs)
        self.assertEqual(len(self.window.model.visible_entries()), 2)

    def test_save_then_undo_and_timeline_playback_keep_text_and_shortcuts(self):
        player = self.use_player()
        self.open_text().insertPlainText("修改")
        changed = self.window.document.entries[0].text
        QTest.mouseClick(self.window.save_button, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: not self.window._saving and not self.window._jobs)
        self.assertFalse(self.window.document.dirty)
        self.assertFalse(self.window._subtitle_editor_open())
        self.assertIn(changed, self.window.document.corrected_path.read_text(encoding="utf-8"))
        timeline = self.window.timeline
        QTest.mouseClick(timeline, Qt.MouseButton.LeftButton,
                        pos=QPoint(timeline.width() * 3 // 4, 14))
        self.app.processEvents()
        self.press(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.document.entries[0].text, "第一句")
        self.assertTrue(self.window.document.dirty)
        self.press(Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.window.document.entries[0].text, changed)
        self.press(Qt.Key.Key_Space)
        player.play.assert_called_once_with()

    def test_context_timing_and_merge_finish_text_edit_before_resetting_rows(self):
        self.use_player()
        self.open_text().insertPlainText("修改")
        changed = self.window.document.entries[0].text
        self.window._player_position = 1.25
        self.window._trim_start()
        self.app.processEvents()
        self.assertFalse(self.window._subtitle_editor_open())
        self.assertEqual(self.window.document.entries[0].start, "00:00:01,250")
        self.assertEqual(self.window.document.entries[0].text, changed)

        self.open_text()
        self.window.selection.replace({1, 2}, self.window._cue_order())
        self.window._merge_selected()
        self.app.processEvents()
        self.assertFalse(self.window._subtitle_editor_open())
        self.assertTrue(self.window._shortcuts["Space"].isEnabled())
        self.assertEqual(len(self.window.document.entries), 1)
        self.assertIn(changed, self.window.document.entries[0].text)
        self.window.table.setFocus()
        self.press(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(self.window.document.entries), 2)
        self.assertEqual(self.window.document.entries[0].text, changed)


if __name__ == "__main__":
    unittest.main()
