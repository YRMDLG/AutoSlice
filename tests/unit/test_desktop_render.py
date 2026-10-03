"""Desktop-08 的输出保护与 Qt 后台任务回归。"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.subtitle_render import SubtitleRenderService
from autoslice.desktop.subtitles import SubtitleEntry

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QPlainTextEdit, QTableView
except ImportError:
    QApplication = None


class SubtitleRenderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        folder = self.root / "【中文】😊"
        folder.mkdir()
        self.video = folder / "短片.mp4"
        self.video.write_bytes(b"source-video")
        self.corrected = folder / "短片_校对.srt"
        self.corrected.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n校对文字\n", encoding="utf-8"
        )
        self.digest = hashlib.sha256(self.corrected.read_bytes()).hexdigest()
        self.service = SubtitleRenderService(DesktopStorage(self.root / "app-data"))

    def test_saved_srt_private_artifacts_and_collision(self):
        first = self.video.with_name("短片_字幕版.mp4")
        first.write_bytes(b"existing-final")

        def fake_burn(video, srt, *, output_path, progress_callback, cancel_event):
            self.assertEqual(Path(video), self.video)
            self.assertEqual(Path(srt).read_bytes(), self.corrected.read_bytes())
            self.assertNotEqual(Path(srt).parent, self.video.parent)
            Path(output_path).write_bytes(b"new-final")
            if progress_callback:
                progress_callback("字幕压制中", 50, 100)
            return {"encoder": "libx264", "source_video_info": {}, "output_video_info": {}}

        with patch("autoslice.desktop.subtitle_render.burn_subtitles", side_effect=fake_burn):
            result = self.service.render(self.video, self.corrected, self.digest)
        output = Path(result["output_video_path"])
        self.assertEqual(output.name, "短片_字幕版 (2).mp4")
        self.assertEqual(output.read_bytes(), b"new-final")
        self.assertEqual(first.read_bytes(), b"existing-final")
        self.assertEqual(self.video.read_bytes(), b"source-video")
        self.assertEqual(list((self.root / "app-data" / "cache" / "temp").iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.video.parent.iterdir()),
                         ["短片.mp4", "短片_字幕版 (2).mp4", "短片_字幕版.mp4", "短片_校对.srt"])

    def test_missing_or_changed_saved_srt_reports_error_without_output(self):
        with self.assertRaisesRegex(ValueError, "外部变化"):
            self.service.render(self.video, self.corrected, "0" * 64)
        self.assertFalse(self.service.default_output(self.video).exists())
        self.assertIn("外部变化", (self.service.storage.logs / "subtitle-render.log").read_text(encoding="utf-8"))


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class SubtitleRenderQtTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for title in ("项目甲", "项目乙"):
            folder = self.root / title
            folder.mkdir()
            (folder / "视频.mp4").touch()
            (folder / "视频.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n原文\n", encoding="utf-8"
            )
        self.app = QApplication.instance() or QApplication([])
        storage = DesktopStorage(self.root / "app-data")
        self.storage = storage
        with patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("测试无播放器")):
            self.window = DesktopWindow(SubmissionProjectService(self.root), storage)
        self.window.show()
        self.addCleanup(self._close)
        self.wait_for(lambda: len(self.window.project_buttons) >= 2)
        self.projects = {p.title: p for p in self.window.service.snapshot.projects}
        self.window._select_real_project(self.projects["项目甲"])
        self.wait_for(lambda: self.window.document is not None)

    def _close(self):
        self.window._resolve_unsaved = lambda: True
        self.window.close()

    def wait_for(self, condition):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail("Qt 后台任务未完成")

    def _make_unsaved_edit(self):
        self.wait_for(lambda: self.window._waveform_generation > 0 and not self.window._jobs)
        self.window.document.edit_text(1, "本次未正式保存")
        self.window._edited()
        self.app.processEvents()

    def _choose_unsaved_action(self, text):
        def choose(dialog):
            button = next(button for button in dialog.buttons() if button.text() == text)
            button.click()

        with patch.object(QMessageBox, "exec", new=choose):
            return self.window._resolve_unsaved()

    def test_resolve_unsaved_without_changes_keeps_direct_exit_behavior(self):
        self.wait_for(lambda: self.window._waveform_generation > 0 and not self.window._jobs)
        with patch.object(QMessageBox, "exec", side_effect=AssertionError("不应弹出确认框")):
            self.assertTrue(self.window._resolve_unsaved())
        self.wait_for(lambda: not self.window._jobs)

    def test_unsaved_dialog_keep_discard_and_cancel_branches(self):
        self._make_unsaved_edit()
        draft_path = self.storage.draft_path(
            "subtitle", self.window.project.directory, self.window.document.source_path
        )

        self.assertTrue(self._choose_unsaved_action("保留草稿并离开"))
        self.assertTrue(draft_path.is_file())

        self.window.document.edit_text(1, "第二次未正式保存")
        self.window._edited()
        self.assertFalse(self._choose_unsaved_action("取消"))
        self.assertTrue(self.window.document.dirty)
        self.assertTrue(draft_path.is_file())

        self.assertTrue(self._choose_unsaved_action("放弃修改并离开"))
        self.assertFalse(draft_path.exists())
        self.wait_for(lambda: not self.window._jobs)

    def test_discard_unsaved_edit_reopens_from_formally_saved_subtitle(self):
        self._make_unsaved_edit()
        draft_path = self.storage.draft_path(
            "subtitle", self.window.project.directory, self.window.document.source_path
        )
        self.assertTrue(self._choose_unsaved_action("放弃修改并离开"))
        self.assertFalse(draft_path.exists())

        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        with patch("autoslice.desktop.qt_app.window.MpvAdapter",
                   side_effect=OSError("测试无播放器")):
            reopened = DesktopWindow(SubmissionProjectService(self.root), self.storage)
        # 波形在异步载入完成后才生成，须在实例上替换并覆盖整个用例；构造期间 patch
        # DesktopWindow 类属性既拦不到这次调用，还会让 PySide 在构造第二个窗口时崩溃。
        waveform = patch.object(reopened.waveform_cache, "load_or_generate",
                                side_effect=RuntimeError("测试不生成波形"))
        waveform.start()
        self.addCleanup(waveform.stop)
        reopened.show()

        def close_reopened():
            reopened._resolve_unsaved = lambda: True
            reopened.close()

        self.addCleanup(close_reopened)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.app.processEvents()
            if reopened.document is not None:
                break
            time.sleep(.01)
        self.assertIsNotNone(reopened.document)
        self.assertEqual(reopened.document.entries[0].text, "原文")
        self.wait_for(lambda: not self.window._jobs)

    def test_save_then_render_keeps_ui_usable_and_prevents_duplicate_project_job(self):
        self.window.document.edit_text(1, "校对文字")
        self.window._edited()
        started = Event()
        release = Event()
        calls = []

        def fake_render(video, corrected, digest, *, progress_callback, cancel_event):
            calls.append(str(video))
            self.assertTrue(Path(corrected).is_file())
            self.assertEqual(hashlib.sha256(Path(corrected).read_bytes()).hexdigest(), digest)
            started.set()
            release.wait(5)
            output = Path(video).with_name("视频_字幕版.mp4")
            output.write_bytes(b"test-output")
            return {"output_video_path": str(output)}

        self.window.render_service.render = fake_render
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window._start_render()
        self.wait_for(started.is_set)
        self.assertTrue((self.root / "项目甲" / "视频_校对.srt").exists())
        self.assertEqual(len(calls), 1)
        self.window._start_render()
        self.assertEqual(len(calls), 1)
        self.window._select_page(1)
        self.window._select_real_project(self.projects["项目乙"])
        self.wait_for(lambda: self.window.document is not None and
                      self.window.project.title == "项目乙")
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.assertTrue(self.window.render_button.isEnabled())
        release.set()
        self.wait_for(lambda: not self.window._render_jobs)
        self.assertIn("项目甲", self.window.app_status.text())
        self.assertIn("视频_字幕版.mp4", self.window.app_status.text())
        self.assertTrue(self.window.render_location.isVisible())

    def test_inline_subtitle_editor_has_no_box_and_keeps_row_height(self):
        index = self.window.model.index(0, 2)
        row_height = self.window.table.rowHeight(0)

        self.window._cue_clicked(index)
        self.app.processEvents()

        editor = next((item for item in self.window.table.findChildren(QPlainTextEdit)
                       if item.isVisible()), None)
        self.assertIsNotNone(editor)
        self.assertEqual(self.window.table.rowHeight(0), row_height)
        self.assertIn("background: #20383D", editor.styleSheet())
        self.assertIn("border: none", editor.styleSheet())

        cursor = editor.textCursor()
        cursor.setPosition(len(editor.toPlainText()))
        editor.setTextCursor(cursor)
        for char in "连续输入123":
            editor.insertPlainText(char)
            self.app.processEvents()
            self.assertEqual(editor.textCursor().position(), len(editor.toPlainText()))

        self.assertTrue(self.window.document.entries[0].text.endswith("连续输入123"))
        cursor_position = editor.textCursor().position()
        self.window.model.dataChanged.emit(
            index, index, [Qt.ItemDataRole.BackgroundRole]
        )
        self.app.processEvents()
        self.assertEqual(editor.textCursor().position(), cursor_position)
        self.assertTrue(self.window._preview_timer.isActive())

    def test_inline_editor_mouse_drag_selects_partial_text(self):
        index = self.window.model.index(0, 2)
        self.window._cue_clicked(index)
        self.app.processEvents()
        editor = self.window._active_subtitle_editor()
        self.assertIsNotNone(editor)

        editor.setPlainText("我跟你们说拿完快递之后")
        self.app.processEvents()
        cursor = editor.textCursor()
        cursor.setPosition(1)
        start = editor.cursorRect(cursor).center()
        cursor.setPosition(6)
        end = editor.cursorRect(cursor).center()

        QTest.mousePress(editor.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(editor.viewport(), end, delay=20)
        QTest.mouseRelease(editor.viewport(), Qt.MouseButton.LeftButton, pos=end)
        self.app.processEvents()

        selected = editor.textCursor().selectedText()
        self.assertTrue(selected)
        self.assertNotEqual(selected, editor.toPlainText())
        before = editor.toPlainText()
        QTest.keyClick(editor, Qt.Key.Key_Delete)
        self.app.processEvents()
        self.assertEqual(len(editor.toPlainText()), len(before) - len(selected))
        self.assertTrue(editor.toPlainText())

    def test_normal_click_uses_extended_not_toggle_multiselection(self):
        self.assertEqual(
            self.window.table.selectionMode(),
            QTableView.SelectionMode.ExtendedSelection,
        )
        self.window.selection.choose(1, [1], ctrl=False, shift=False)
        self.window._sync_table_selection()
        self.assertEqual(self.window.selection.selected, {1})
        self.assertEqual(len(self.window.table.selectionModel().selectedRows()), 1)

    def test_native_table_selection_drift_is_restored_to_internal_selection(self):
        self.window.document.entries.append(
            SubtitleEntry(2, "00:00:02,000", "00:00:03,000", "第二条", (2,))
        )
        self.window.model.set_document(self.window.document)
        self.window.selection.choose(1, [1, 2], ctrl=False, shift=False)
        self.window.selected_index = 1
        self.window._sync_table_selection()

        selection_model = self.window.table.selectionModel()
        flags = (
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows
        )
        # 模拟关闭 editor 时 Qt 自己把“下一行”选中了。
        selection_model.select(self.window.model.index(1, 0), flags)
        self.app.processEvents()

        rows = selection_model.selectedRows()
        self.assertEqual([row.row() for row in rows], [0])
        self.assertEqual(self.window.selection.selected, {1})
        self.assertEqual(self.window.selection.active, 1)

    def test_explicit_seek_ignores_stale_player_position_and_playing_highlight(self):
        class FakePlayer:
            def __init__(self):
                self.seeks = []

            def seek(self, position, pause=True):
                self.seeks.append((position, pause))

            def close(self):
                # 用例收尾关窗时 closeEvent 会调用 player.close()
                pass

        self.window.player = FakePlayer()
        self.window._media_ready = True
        self.window._player_duration = 20.0
        self.window._player_position = 7.0
        self.window.timeline.set_playhead(7.0, playing=False)

        self.window._seek_to(0.5)
        self.assertAlmostEqual(self.window.timeline.playhead, 0.5, delta=0.001)
        self.assertEqual(self.window.model.playing_index, None)

        # 模拟 mpv 在 seek 后先回报 seek 前旧位置。
        self.window._player_status({"position": 7.0, "paused": True})
        self.assertAlmostEqual(self.window.timeline.playhead, 0.5, delta=0.001)
        self.assertEqual(self.window.model.playing_index, None)

        # 等真正目标位置到达后才解除 guard。
        self.window._player_status({"position": 0.5, "paused": True})
        self.assertAlmostEqual(self.window.timeline.playhead, 0.5, delta=0.001)
        self.assertIsNone(self.window._seek_guard_target)

    def test_bottom_subtitle_click_keeps_seek_behavior(self):
        class FakePlayer:
            def __init__(self):
                self.seeks = []

            def seek(self, position, pause=True):
                self.seeks.append((position, pause))

            def close(self):
                pass

        player = FakePlayer()
        self.window.player = player
        self.window._media_ready = True
        self.window._player_duration = 20.0
        index = self.window.model.index(0, 0)
        self.window._player_position = 9.0
        self.window._player_paused = True
        self.window._cue_clicked(index)
        # 从播放器与时间轴的结果验证跳转，不替换私有 _seek_to
        self.assertEqual(player.seeks, [(0.0, True)])
        self.assertAlmostEqual(self.window.timeline.playhead, 0.0, delta=0.001)
        self.assertEqual(self.window.selection.active, 1)

    def test_timeline_selection_preserves_manual_view(self):
        self.window.timeline.set_duration(60.0)
        self.window.timeline.span = 20.0
        self.window.timeline.set_view_start(30.0)
        self.app.processEvents()
        start_before = self.window.timeline._view_start()
        scroll_before = self.window.timeline_scroll.value()
        cue_id = self.window.document.entries[0].index
        self.window._timeline_selection_requested(cue_id, False, False)
        self.app.processEvents()
        self.assertAlmostEqual(self.window.timeline._view_start(), start_before, delta=0.001)
        self.assertEqual(self.window.timeline_scroll.value(), scroll_before)
        self.assertTrue(self.window.timeline._manual_pan)

    def test_start_playback_preserves_manual_timeline_view_and_scrollbar(self):
        class FakePlayer:
            def __init__(self):
                self.play_calls = 0
                self.pause_calls = 0

            def play(self):
                self.play_calls += 1

            def pause(self):
                self.pause_calls += 1

            def close(self):
                pass

        self.window.player = FakePlayer()
        self.window._media_ready = True
        self.window._player_paused = True
        self.window.timeline.set_duration(60.0)
        self.window.timeline.span = 20.0
        self.window.timeline.set_view_start(15.0)
        self.app.processEvents()

        start_before = self.window.timeline._view_start()
        scroll_before = self.window.timeline_scroll.value()
        self.assertTrue(self.window.timeline._manual_pan)

        self.window._toggle_play()
        self.app.processEvents()

        self.assertEqual(self.window.player.play_calls, 1)
        self.assertFalse(self.window._player_paused)
        self.assertAlmostEqual(self.window.timeline._view_start(), start_before, delta=0.001)
        self.assertEqual(self.window.timeline_scroll.value(), scroll_before)

        # 播放器位置继续更新时也不能夺走手动视野。
        self.window._player_status({"position": 18.0, "paused": False})
        self.app.processEvents()
        self.assertAlmostEqual(self.window.timeline._view_start(), start_before, delta=0.001)
        self.assertEqual(self.window.timeline_scroll.value(), scroll_before)

    def test_mouse_side_buttons_pan_timeline_view(self):
        self.window.timeline.set_duration(20.0)
        self.window.timeline.span = 5.0
        self.window.timeline.set_view_start(5.0)
        start = self.window.timeline._view_start()

        QTest.mouseClick(
            self.window.timeline,
            Qt.MouseButton.ForwardButton,
            pos=QPoint(max(20, self.window.timeline.width() // 2),
                       max(30, self.window.timeline.height() // 2)),
        )
        self.app.processEvents()
        self.assertGreater(self.window.timeline._view_start(), start)

        QTest.mouseClick(
            self.window.timeline,
            Qt.MouseButton.BackButton,
            pos=QPoint(max(20, self.window.timeline.width() // 2),
                       max(30, self.window.timeline.height() // 2)),
        )
        self.app.processEvents()
        self.assertAlmostEqual(self.window.timeline._view_start(), start, delta=0.05)

    def test_clicking_same_editing_cell_keeps_editor_and_moves_caret(self):
        index = self.window.model.index(0, 2)
        self.window._cue_clicked(index)
        self.app.processEvents()
        editor = next((item for item in self.window.table.findChildren(QPlainTextEdit)
                       if item.isVisible()), None)
        self.assertIsNotNone(editor)
        self.assertTrue(self.window._subtitle_editor_open())

        # 真实用户会在同一句字幕里反复点不同字符定位 caret；
        # 连续点击期间 editor 绝不能被 Qt 自己销毁。
        width = max(12, editor.viewport().width())
        for ratio in (0.15, 0.35, 0.55, 0.75, 0.45, 0.25):
            QTest.mouseClick(
                editor.viewport(),
                Qt.MouseButton.LeftButton,
                pos=QPoint(max(4, int(width * ratio)),
                           max(4, editor.viewport().height() // 2)),
            )
            self.app.processEvents()
            self.assertTrue(self.window._subtitle_editor_open())
            self.assertTrue(editor.isVisible())

        # Qt 某些路径会把点击落到 table viewport，同样不能退出编辑态。
        rect = self.window.table.visualRect(index)
        point = QPoint(rect.left() + max(12, rect.width() // 2), rect.center().y())
        QTest.mouseClick(
            self.window.table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=point,
        )
        self.app.processEvents()

        self.assertTrue(self.window._subtitle_editor_open())
        self.assertTrue(editor.isVisible())
        cursor_position = editor.textCursor().position()
        self.assertGreaterEqual(cursor_position, 0)
        self.assertLessEqual(cursor_position, len(editor.toPlainText()))

    def test_clicking_outside_subtitle_editor_commits_and_closes_it(self):
        index = self.window.model.index(0, 2)
        self.window._cue_clicked(index)
        self.app.processEvents()
        editor = next((item for item in self.window.table.findChildren(QPlainTextEdit)
                       if item.isVisible()), None)
        self.assertIsNotNone(editor)
        editor.insertPlainText("外点关闭")
        self.app.processEvents()
        self.assertTrue(self.window._subtitle_editor_open())

        QTest.mouseClick(
            self.window.timeline,
            Qt.MouseButton.LeftButton,
            pos=QPoint(max(20, self.window.timeline.width() // 2),
                       max(60, self.window.timeline.height() - 10)),
        )
        self.app.processEvents()

        self.assertFalse(self.window._subtitle_editor_open())
        visible_editor = next(
            (item for item in self.window.table.findChildren(QPlainTextEdit)
             if item.isVisible()),
            None,
        )
        self.assertIsNone(visible_editor)
        self.assertIn("外点关闭", self.window.document.entries[0].text)

    def test_missing_ffmpeg_reports_failure_and_keeps_document(self):
        self.window.document.save()
        before = tuple(self.window.document.entries)
        with patch("autoslice.desktop.subtitle_render.burn_subtitles",
                   side_effect=FileNotFoundError("ffmpeg")):
            self.window._start_render()
            self.wait_for(lambda: not self.window._render_jobs)
        self.assertIn("压制失败", self.window.app_status.text())
        self.assertIn("FFmpeg", self.window.app_status.text())
        self.assertEqual(tuple(self.window.document.entries), before)
        self.assertTrue((self.storage.logs / "subtitle-render.log").is_file())

    def test_close_stops_active_render_before_exit(self):
        self.window.document.save()
        started = Event()
        stopped = Event()

        def fake_render(_video, _corrected, _digest, *, progress_callback, cancel_event):
            started.set()
            if not cancel_event.wait(5):
                raise AssertionError("关闭没有请求停止")
            stopped.set()
            raise RuntimeError("字幕压制已取消")

        self.window.render_service.render = fake_render
        self.window._start_render()
        self.wait_for(started.is_set)

        def choose_stop(dialog):
            next(button for button in dialog.buttons()
                 if button.text() == "停止压制并退出").click()
            return 0

        with patch.object(QMessageBox, "exec", choose_stop):
            self.window.close()
        self.wait_for(lambda: stopped.is_set() and not self.window._render_jobs)
        self.wait_for(lambda: not self.window.isVisible())


if __name__ == "__main__":
    unittest.main()
