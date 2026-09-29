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
from autoslice.desktop.qt_preview.theme import COLORS
from autoslice.desktop.subtitle_render import SubtitleRenderService

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox, QPlainTextEdit
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
        self.assertIn(f"background: {COLORS.row_selected}", editor.styleSheet())
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
