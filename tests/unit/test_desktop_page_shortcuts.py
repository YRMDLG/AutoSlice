"""字幕页快捷键只在字幕页生效，封面页按键不能误操作字幕。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
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

    def press(self, key, modifier=Qt.KeyboardModifier.NoModifier):
        # 从窗口句柄发送，经过 Qt 的快捷键分发，和真实键盘一致。
        QTest.keyClick(self.window.windowHandle(), key, modifier)
        self.app.processEvents()

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


if __name__ == "__main__":
    unittest.main()
