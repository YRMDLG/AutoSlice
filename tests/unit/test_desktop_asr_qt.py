"""桌面端字幕识别的界面：没有字幕时的提示条、子进程识别全流程、取消/失败/崩溃、设置卡片与自检。

识别子进程换成一个假脚本（按设置里的“模型”字段决定成功/失败/崩溃/卡住），不加载任何识别模型。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autoslice.desktop.asr import AsrSettings, load_settings, save_settings
from autoslice.desktop.foundation import DesktopStorage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

FAKE_WORKER = r"""
import json, sys, time
spec = json.load(open(sys.argv[1], encoding="utf-8"))
def event(**payload):
    print("@@asr " + json.dumps(payload, ensure_ascii=False), flush=True)
print("第三方库的杂音", flush=True)
mode = spec["settings"].get("model", "")
if mode == "sleep":
    event(type="progress", message="识别中 1/9", fraction=0.1)
    time.sleep(30)
elif mode == "crash":
    print("CUDA error: out of memory", flush=True)
    sys.exit(3)
elif mode == "fail":
    event(type="error", message="没有安装 qwen-asr")
else:
    event(type="progress", message="识别中 1/1", fraction=0.5)
    open(spec["output"], "w", encoding="utf-8").write("1\n00:00:00,000 --> 00:00:01,000\n识别出来的字幕\n")
    event(type="done", srt_path=spec["output"], cue_count=1, seconds=0.1, audio_seconds=1.0, info={}, warnings=[])
"""


def fake_worker_command(spec_path, python=""):
    return sys.executable, ["-u", "-c", FAKE_WORKER, str(spec_path)], {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class AsrQtTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for title, has_srt in (("有字幕", True), ("没字幕", False)):
            folder = self.root / title
            folder.mkdir()
            (folder / "视频.mp4").touch()
            if has_srt:
                (folder / "视频.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n原文\n", encoding="utf-8")
        self.app = QApplication.instance() or QApplication([])
        self.storage = DesktopStorage(self.root / "app-data")
        waveform = patch("autoslice.desktop.qt_app.window.WaveformCache.load",
                         return_value=SimpleNamespace(samples=(), duration=1.0))
        waveform.start()
        self.addCleanup(waveform.stop)
        worker = patch("autoslice.desktop.qt_app.asr_process.worker_command", side_effect=fake_worker_command)
        worker.start()
        self.addCleanup(worker.stop)
        with patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("测试无播放器")):
            self.window = DesktopWindow(SubmissionProjectService(self.root), self.storage)
        self.window.show()
        self.addCleanup(self._close)
        self.wait_for(lambda: len(self.window.project_buttons) >= 2)
        self.projects = {p.title: p for p in self.window.service.snapshot.projects}

    def _close(self):
        self.window._resolve_unsaved = lambda: True
        self.window.close()

    def wait_for(self, condition, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail("Qt 任务未完成")

    def select(self, title):
        self.window._select_real_project(self.projects[title])
        self.wait_for(lambda: not self.window._loading)

    def use_model(self, value):
        save_settings(self.storage, AsrSettings(model=value))

    def banner_visible(self):
        return self.window.asr_banner.isVisibleTo(self.window)

    def test_banner_only_for_videos_without_subtitles(self):
        self.select("有字幕")
        self.wait_for(lambda: self.window.document is not None)
        self.assertFalse(self.banner_visible())
        self.select("没字幕")
        self.assertTrue(self.banner_visible())
        self.assertIn("没有字幕", self.window.asr_banner.title.text())
        self.assertEqual(self.window.subtitle_status.text(), "这个视频还没有字幕")
        self.select("有字幕")
        self.assertFalse(self.banner_visible())

    def test_recognition_writes_the_srt_and_loads_it(self):
        self.use_model("ok")
        self.select("没字幕")
        self.window.asr_banner.start_button.click()
        self.assertTrue(self.window._asr_running())
        self.wait_for(lambda: not self.window._asr_running() and self.window.document is not None)
        self.assertTrue((self.root / "没字幕" / "视频.srt").is_file())
        self.assertFalse(self.banner_visible())
        self.assertEqual(self.window.document.entries[0].text, "识别出来的字幕")

    def test_failure_shows_the_reason_and_offers_retry(self):
        self.use_model("fail")
        self.select("没字幕")
        self.window.asr_banner.start_button.click()
        self.wait_for(lambda: not self.window._asr_running())
        self.assertIn("没有安装 qwen-asr", self.window.asr_banner.detail.text())
        self.assertEqual(self.window.asr_banner.start_button.text(), "重试")
        self.assertFalse((self.root / "没字幕" / "视频.srt").exists())

    def test_a_crash_without_any_event_surfaces_the_last_output_line(self):
        self.use_model("crash")
        self.select("没字幕")
        self.window.asr_banner.start_button.click()
        self.wait_for(lambda: not self.window._asr_running())
        self.assertIn("CUDA error: out of memory", self.window.asr_banner.detail.text())

    def test_cancel_kills_the_process_and_offers_to_continue(self):
        self.use_model("sleep")
        self.select("没字幕")
        self.window.asr_banner.start_button.click()
        self.wait_for(lambda: self.window.asr_banner.bar.value() > 0)
        self.window.asr_banner.cancel_button.click()
        self.wait_for(lambda: not self.window._asr_running())
        self.assertEqual(self.window.asr_banner.title.text(), "已取消")
        self.assertEqual(self.window.asr_banner.start_button.text(), "继续识别")

    def test_switching_video_hides_the_banner_and_a_cancel_elsewhere_stays_quiet(self):
        self.use_model("sleep")
        self.select("没字幕")
        self.window.asr_banner.start_button.click()
        self.wait_for(lambda: self.window.asr_banner.bar.value() > 0)
        self.select("有字幕")
        self.assertFalse(self.banner_visible())
        self.window._asr_shutdown()
        self.wait_for(lambda: not self.window._asr_running())
        self.assertFalse(self.banner_visible())

    def test_closing_the_window_stops_a_running_recognition(self):
        self.use_model("sleep")
        self.select("没字幕")
        self.window.asr_banner.start_button.click()
        self.wait_for(lambda: self.window.asr_banner.bar.value() > 0)
        self.window.close()
        self.wait_for(lambda: not self.window._asr_running())

    def test_settings_panel_round_trip_and_engine_switch(self):
        panel = self.window.asr_settings_panel
        panel.engine_box.setCurrentIndex(panel.engine_box.findData("funasr"))
        self.assertTrue(panel.model_edit.isHidden())
        panel.engine_box.setCurrentIndex(panel.engine_box.findData("qwen3-asr"))
        self.assertFalse(panel.model_edit.isHidden())
        panel.python_edit.setText("D:/py/python.exe")
        panel.hotword_check.setChecked(False)
        self.assertTrue(panel._save())
        saved = load_settings(self.storage)
        self.assertEqual((saved.engine, saved.python, saved.hotwords), ("qwen3-asr", "D:/py/python.exe", False))
        panel.python_edit.setText("")
        panel.refresh()
        self.assertEqual(panel.python_edit.text(), "D:/py/python.exe")

    def test_doctor_button_formats_the_child_process_report(self):
        report = json.dumps([{"name": "torch", "status": "fail", "detail": "没装", "hint": "pip install torch"}])
        command = (sys.executable, ["-u", "-c", f"print('噪音'); print({report!r})"], {"PYTHONIOENCODING": "utf-8"})
        panel = self.window.asr_settings_panel
        with patch("autoslice.desktop.qt_app.asr_panel.python_command", return_value=command):
            panel.check_button.click()
            self.wait_for(lambda: panel.check_button.isEnabled())
        self.assertIn("✗ torch：没装", panel.output.toPlainText())
        self.assertIn("pip install torch", panel.output.toPlainText())
        self.assertIn("1 项需要处理", panel.status.text())

    def test_doctor_button_reports_a_child_that_cannot_start(self):
        panel = self.window.asr_settings_panel
        missing = (str(self.root / "不存在" / "python.exe"), [], {})
        with patch("autoslice.desktop.qt_app.asr_panel.python_command", return_value=missing):
            panel.check_button.click()
            self.wait_for(lambda: panel.check_button.isEnabled())
        self.assertIn("自检失败", panel.status.text())


if __name__ == "__main__":
    unittest.main()
