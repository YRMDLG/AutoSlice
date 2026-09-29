"""Desktop 字幕页与统一项目快照的轻量 Tk 交互测试。"""

from __future__ import annotations

import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.desktop.app import DesktopApp
from autoslice.desktop.projects import SubmissionProjectService


SRT = "1\n00:00:00,000 --> 00:00:01,000\n第一条\n\n2\n00:00:02,000 --> 00:00:03,000\n第二条\n"


class DesktopAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for title in ("甲", "乙"):
            folder = root / title
            folder.mkdir()
            (folder / "成片.mp4").touch()
            (folder / "成片.srt").write_text(SRT, encoding="utf-8")
        try:
            self.window = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk 不可用：{exc}")
        self.window.withdraw()
        self.addCleanup(self.window.destroy)
        self.app = DesktopApp(self.window, SubmissionProjectService(root))
        self.window.update()

    def _select(self, index):
        self.app.project_list.selection_clear(0, tk.END)
        self.app.project_list.selection_set(index)
        self.app._select_project(None)
        self.window.update()

    def test_single_click_edit_delete_and_switch_guard(self):
        self._select(0)
        document = self.app.subtitle_document
        self.assertEqual(len(document.entries), 2)
        first_row = self.app.cue_rows.winfo_children()[0]
        editor = next(widget for widget in first_row.winfo_children() if isinstance(widget, tk.Text))
        editor.event_generate("<Button-1>")
        self.window.update()
        self.assertEqual(self.app.selected_cue_index, 1)
        editor.insert("end-1c", "改")
        self.app._edit_cue(1, editor)
        self.window.update()
        self.assertTrue(document.dirty)
        self.assertIn("改", document.entries[0].text)

        with patch("autoslice.desktop.app.messagebox.askyesnocancel", return_value=None):
            self._select(1)
        self.assertIs(self.app.subtitle_document, document)
        self.assertEqual(self.app.selection["字幕校对"], self.app.service.snapshot.projects[0].id)
        self.assertEqual(self.app.project_list.curselection(), (0,))

        self.app._delete_selected()
        self.assertEqual(len(document.entries), 1)
        self.app._undo()
        self.assertEqual(len(document.entries), 2)
        self.app._redo()
        self.assertEqual(len(document.entries), 1)
        self.app.show_page("封面制作")
        self.assertIs(self.app.subtitle_document, document)
        self.assertEqual(self.app.project_list.size(), 2)
        with patch("autoslice.desktop.app.messagebox.askyesnocancel", return_value=True):
            self.app.refresh()
        self.assertTrue(Path(document.video.corrected_srt_path).exists())
        self.app.show_page("字幕校对")
        self.assertFalse(self.app.subtitle_document.dirty)
        self.app.subtitle_document.edit_text(2, "再次修改")
        with patch("autoslice.desktop.app.messagebox.askyesnocancel", return_value=False):
            self._select(1)
        self.assertFalse(self.app.subtitle_document.dirty)
        self.assertEqual(len(self.app.subtitle_document.entries), 2)


if __name__ == "__main__":
    unittest.main()
