"""字幕页“分析话题”入口：按钮状态、后台进度、任务中心停止、完成后打开报告。

流水线换成假的（按需卡住或写一份报告），不发模型请求、不识别、不压制。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class TopicEntryQtTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.foundation import DesktopStorage
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow
        from autoslice.desktop.topics import TopicAnalysisService

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for title, has_srt in (("有字幕", True), ("没字幕", False)):
            folder = self.root / "投稿" / title
            folder.mkdir(parents=True)
            (folder / "录播.mp4").touch()
            if has_srt:
                (folder / "录播.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n原文\n", encoding="utf-8")
        self.app = QApplication.instance() or QApplication([])
        self.settings = SimpleNamespace(configured=True, error="", text_model="gpt-5.6-luna")
        self.release = threading.Event()
        self.mode = "report"
        self.runs = []
        self.service = TopicAnalysisService(self.root / "输出", runner=self.fake_pipeline,
                                            settings_reader=lambda: self.settings,
                                            model_name=lambda: "gpt-5.6-luna")
        waveform = patch("autoslice.desktop.qt_app.window.WaveformCache.load",
                         return_value=SimpleNamespace(samples=(), duration=1.0))
        waveform.start()
        self.addCleanup(waveform.stop)
        with patch("autoslice.desktop.qt_app.window.TopicAnalysisService", return_value=self.service), \
             patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("测试无播放器")):
            self.window = DesktopWindow(SubmissionProjectService(self.root / "投稿"),
                                        DesktopStorage(self.root / "app-data"))
        self.window.show()

        def close_window():
            self.release.set()
            self.wait_for(lambda: not self.window._jobs)
            if hasattr(self.window, "task_dialog"):
                self.window.task_dialog.close()
            self.window._resolve_unsaved = lambda: True
            self.window.close()

        self.addCleanup(close_window)
        self.wait_for(lambda: bool(self.window.project_buttons))
        self.open_project("有字幕")

    def fake_pipeline(self, video, **kwargs):
        self.runs.append(kwargs)
        progress = kwargs["progress_callback"]
        progress("Step 4/5: LLM分析完成 (1/2)", 50, 100)
        if self.mode == "loop":
            # 像真流水线一样不停报进度，直到被停止
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                progress("Step 4/5: 块 2 API", 60, 100)
                time.sleep(.01)
            return {}
        self.release.wait(5)
        report = Path(self.service.layout(video)["report_path"])
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("# 话题分析\n", encoding="utf-8")
        return {"md_path": str(report), "artifact_dir": str(report.parent), "topic_count": 2,
                "clip_marks": [], "failed_chunks": []}

    def wait_for(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail(f"等不到界面状态：{self.window.app_status.text()} / {self.window.topic_button.text()}")

    def open_project(self, title):
        project = next(p for p in self.window.service.snapshot.projects if p.title == title)
        self.window._select_real_project(project)
        self.wait_for(lambda: self.window.project is not None and self.window.project.title == title)
        self.wait_for(lambda: not self.window._jobs)

    def record(self):
        return self.window._task_records.get("topics", {})

    def start(self):
        from PySide6.QtWidgets import QMessageBox

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as ask:
            self.window.topic_button.click()
        self.assertIn("消耗额度", ask.call_args.args[2])
        self.wait_for(lambda: self.window.topic_button.text().startswith("分析中 "))

    def test_button_needs_existing_subtitle(self):
        self.assertTrue(self.window.topic_button.isEnabled())
        self.assertEqual(self.window.topic_button.text(), "分析话题")
        self.open_project("没字幕")
        self.assertFalse(self.window.topic_button.isEnabled())
        self.assertIn("先识别字幕", self.window.topic_button.toolTip())

    def test_progress_then_open_report_from_task_center(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        self.start()
        self.wait_for(lambda: self.window.topic_button.text() == "分析中 50%")
        self.assertEqual(self.record()["state"], "running")
        self.assertIn("50%", self.record()["message"])
        self.assertTrue(self.record().get("cancel"))
        self.assertTrue(self.runs[0]["subtitle_path"].endswith("录播.srt"))
        self.release.set()
        self.wait_for(lambda: self.record().get("state") == "done")
        self.assertEqual(self.window.topic_button.text(), "分析话题")
        self.assertIn("2 个话题", self.record()["message"])

        self.window._show_tasks()
        self.window.task_list.setCurrentRow(0)
        self.assertTrue(self.window.task_open.isEnabled())
        self.assertEqual(self.window.task_open.text(), "打开报告")
        self.assertFalse(self.window.task_cancel.isEnabled())
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            self.window.task_open.click()
        opened.assert_called_once_with(QUrl.fromLocalFile(self.record()["result"]))
        self.assertTrue(self.record()["result"].endswith("01_话题分析.md"))

    def test_stop_from_task_center(self):
        self.mode = "loop"
        self.start()
        self.window._show_tasks()
        self.window.task_list.setCurrentRow(0)
        self.assertTrue(self.window.task_cancel.isEnabled())
        self.window.task_cancel.click()
        self.wait_for(lambda: self.record().get("state") == "cancelled")
        self.assertIn("下次接着分析", self.record()["message"])
        self.assertEqual(self.window.topic_button.text(), "分析话题")
        self.assertTrue(self.window.topic_button.isEnabled())

    def test_existing_report_opens_without_rerunning(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QDesktopServices

        video = self.window.video_choice.currentData()
        report = Path(self.service.layout(video.path)["report_path"])
        report.parent.mkdir(parents=True)
        report.write_text("# 旧报告\n", encoding="utf-8")

        def choose_open():
            box = QApplication.activeModalWidget()
            next(button for button in box.buttons() if button.text() == "打开报告").click()

        QTimer.singleShot(0, choose_open)
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            self.window.topic_button.click()
        opened.assert_called_once()
        self.assertEqual(self.runs, [])

    def test_unconfigured_ai_is_explained_before_spending(self):
        from PySide6.QtWidgets import QMessageBox

        self.settings = SimpleNamespace(configured=False, error="", text_model="")
        with patch.object(QMessageBox, "information") as told, \
             patch.object(QMessageBox, "question") as asked:
            self.window.topic_button.click()
        self.assertIn("未配置 AI", told.call_args.args[2])
        asked.assert_not_called()
        self.assertEqual(self.runs, [])


if __name__ == "__main__":
    unittest.main()
