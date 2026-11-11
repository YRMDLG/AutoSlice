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
from autoslice.desktop.asr.jobs import worker_command as real_worker_command
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
elif mode == "exists":
    open(spec["output"], "w", encoding="utf-8").write("1\n00:00:00,000 --> 00:00:01,000\n用户保留的字幕\n")
    event(type="error", code="exists", message="已有字幕，未覆盖")
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

    def test_existing_output_event_reaches_window_and_loads_the_preserved_subtitle(self):
        self.use_model("exists")
        self.select("没字幕")
        self.window.asr_banner.start_button.click()
        self.wait_for(lambda: not self.window._asr_running() and self.window.document is not None)
        self.assertFalse(self.banner_visible())
        self.assertEqual(self.window.document.entries[0].text, "用户保留的字幕")

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

    def test_finishing_elsewhere_reenables_the_current_missing_subtitle_video(self):
        folder = self.root / "另一个没字幕"
        folder.mkdir()
        (folder / "视频.mp4").touch()
        self.window.refresh()
        self.wait_for(lambda: not self.window._jobs and any(
            project.title == "另一个没字幕" for project in self.window.service.snapshot.projects))
        self.projects = {p.title: p for p in self.window.service.snapshot.projects}
        for state in ("error", "cancelled"):
            with self.subTest(state=state):
                self.use_model("sleep")
                self.select("没字幕")
                self.window.asr_banner.start_button.click()
                self.wait_for(lambda: self.window.asr_banner.bar.value() > 0)
                self.select("另一个没字幕")
                self.assertFalse(self.window.asr_banner.start_button.isEnabled())
                self.window._asr_job.cancel()
                self.wait_for(lambda: not self.window._asr_running())
                # 错误事件与取消事件共用当前视频状态收口。
                if state == "error":
                    self.window._asr_video = str(self.root / "没字幕" / "视频.mp4")
                    self.window._asr_finished("error", {"message": "模拟识别失败"})
                self.assertTrue(self.window.asr_banner.start_button.isEnabled())
                self.assertNotIn("正在识别另一个", self.window.asr_banner.detail.text())

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

    def test_engine_box_keeps_its_height_and_note_is_not_clipped(self):
        # 走查 M2：说明和下拉框挤在表单同一格时，下拉框被压到 18 像素
        panel = self.window.asr_settings_panel
        self.window._open_asr_settings()
        self.app.processEvents()
        self.assertGreaterEqual(panel.engine_box.height(), panel.engine_box.sizeHint().height())
        note = panel.engine_note
        self.assertGreaterEqual(note.height(), note.heightForWidth(note.width()) - 1)
        self.assertGreater(note.geometry().top(), panel.engine_box.geometry().top())
        # 走查 M15：说明里不再让用户敲命令，换成按钮
        self.assertNotIn("python -m", "".join(label.text() for label in panel.findChildren(type(note))))

    def test_bench_button_runs_the_comparison_and_reports_where_the_report_is(self):
        panel = self.window.asr_settings_panel
        command = (sys.executable, ["-u", "-c", "print('片段 1/2'); print('片段 2/2')"], {"PYTHONIOENCODING": "utf-8"})
        with patch("autoslice.desktop.qt_app.asr_panel.QFileDialog.getExistingDirectory", return_value=str(self.root)), \
                patch("autoslice.desktop.qt_app.asr_panel.python_command", return_value=command) as build:
            panel.bench_button.click()
            self.wait_for(lambda: panel.bench_button.isEnabled())
        arguments = build.call_args.args[1]
        self.assertEqual(arguments[:3], ["bench", "--root", str(self.root)])
        self.assertIn("--out", arguments)
        self.assertIn("片段 2/2", panel.output.toPlainText())
        self.assertIn("对比完成", panel.status.text())

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

    def test_installed_build_without_a_configured_python_explains_what_to_do(self):
        panel = self.window.asr_settings_panel
        self.use_model("ok")
        self.select("没字幕")
        with patch.object(sys, "frozen", True, create=True), \
                patch("autoslice.desktop.qt_app.asr_process.worker_command", side_effect=real_worker_command):
            self.window.asr_banner.start_button.click()
            self.wait_for(lambda: not self.window._asr_running() and "识别用的 Python" in self.window.asr_banner.detail.text())
            panel.python_edit.setText("")
            panel.check_button.click()
        self.assertIn("识别用的 Python", panel.status.text())
        self.assertTrue(panel.check_button.isEnabled())

    def test_doctor_button_reports_a_child_that_cannot_start(self):
        panel = self.window.asr_settings_panel
        missing = (str(self.root / "不存在" / "python.exe"), [], {})
        with patch("autoslice.desktop.qt_app.asr_panel.python_command", return_value=missing):
            panel.check_button.click()
            self.wait_for(lambda: panel.check_button.isEnabled())
        self.assertIn("自检失败", panel.status.text())


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class RecognizeJobOutcomeTests(unittest.TestCase):
    """进程结束后怎么对用户说：后台清理没赶上，不能把已经写好的字幕说成失败。"""

    DONE = ("done", {"srt_path": "x.srt", "cue_count": 3, "seconds": 1.0, "warnings": ["对一下轴"]})

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    def outcome(self, events, *, cancelled=False, cleanup="", code=0, tail="", start_failed=False):
        from autoslice.desktop.asr.jobs import format_event
        from autoslice.desktop.qt_app.asr_process import RecognizeJob

        job = RecognizeJob()
        self.addCleanup(job.deleteLater)
        results = []
        job.finished.connect(lambda state, payload: results.append((state, payload)))
        runner = job._runner
        for kind, payload in events:
            runner.line.emit(format_event(kind, **payload))
        runner.cleanup_error, runner.start_failed = cleanup, start_failed
        runner.finished.emit(code, tail, cancelled)
        self.app.processEvents()
        self.assertEqual(len(results), 1)
        return results[0]

    def test_cleanup_timeout_after_a_finished_recognition_is_a_warning_not_a_failure(self):
        state, payload = self.outcome([self.DONE], cleanup="识别子进程清理失败：5 秒内没退出")
        self.assertEqual(state, "done")
        self.assertEqual(payload["cue_count"], 3)
        self.assertEqual(payload["warnings"], ["识别子进程清理失败：5 秒内没退出", "对一下轴"])

    def test_clean_finish_keeps_the_original_warnings(self):
        state, payload = self.outcome([self.DONE])
        self.assertEqual((state, payload["warnings"]), ("done", ["对一下轴"]))

    def test_cleanup_failure_without_a_finished_result_is_still_an_error(self):
        cases = (([], False), ([("error", {"message": "失败"})], False), ([], True), ([self.DONE], True))
        for events, cancelled in cases:
            with self.subTest(events=events, cancelled=cancelled):
                state, payload = self.outcome(events, cancelled=cancelled, cleanup="识别子进程清理失败", tail="末尾输出")
                self.assertEqual(state, "error")
                self.assertIn("末尾输出", payload["message"])

    def test_a_real_exit_code_of_minus_one_is_not_mistaken_for_a_cleanup_failure(self):
        state, _payload = self.outcome([self.DONE], code=-1)
        self.assertEqual(state, "done")

    def test_start_failure_reports_the_launch_message(self):
        state, payload = self.outcome([], code=-1, tail="没法启动 python", start_failed=True)
        self.assertEqual((state, payload["message"]), ("error", "没法启动 python"))


if __name__ == "__main__":
    unittest.main()
