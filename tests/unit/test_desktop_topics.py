"""桌面端话题分析服务：进度换算、取消、结果整理，以及接真流水线时只用已有字幕。

全部用假的 LLM / 假流水线和临时目录：不发真实模型请求、不识别、不压制视频。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autoslice.desktop.topics import TopicAnalysisCancelled, TopicAnalysisService, TopicRequest

SRT = "".join(
    f"{index}\n00:{index:02d}:00,000 --> 00:{index:02d}:05,000\n第{index}句：聊新衣服和观众互动\n\n"
    for index in range(1, 9)
)


def configured(model="gpt-5.6-luna", error=""):
    return SimpleNamespace(configured=not error, error=error, text_model=model)


class TopicServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "录播" / "测试录播.flv"
        self.video.parent.mkdir()
        self.video.write_bytes(b"flv")
        self.subtitle = self.video.with_suffix(".srt")
        self.subtitle.write_text(SRT, encoding="utf-8")
        self.output = self.root / "输出"

    def service(self, runner, **kwargs):
        kwargs.setdefault("settings_reader", configured)
        kwargs.setdefault("model_name", lambda: "gpt-5.6-luna")
        return TopicAnalysisService(self.output, runner=runner, **kwargs)

    def request(self):
        return TopicAnalysisService.request_for(str(self.video), str(self.subtitle))

    def test_run_passes_existing_subtitle_and_maps_result(self):
        seen = {}

        def runner(video, **kwargs):
            seen.update(kwargs, video=video)
            kwargs["progress_callback"]("Step 1/5: 使用已有字幕，跳过识别", 0, 100)
            kwargs["progress_callback"]("Step 4/5: LLM分析完成 (1/2)", 59, 100)
            return {"md_path": "报告.md", "overview_path": "概览.md", "artifact_dir": "包",
                    "topic_count": 3, "clip_marks": [{"start": 1}], "failed_chunks": [{"index": 2}],
                    "api_precheck_warning": "第 2 块没分析成功"}

        progress = []
        report = self.service(runner).run(self.request(), progress=lambda *args: progress.append(args))

        self.assertEqual(seen["video"], str(self.video))
        self.assertEqual(seen["subtitle_path"], str(self.subtitle))
        self.assertEqual(seen["manual_timeline_path"], "__none__")
        self.assertEqual(seen["output_dir"], str(self.output))
        self.assertIsNone(seen["ass_path"])
        self.assertEqual(progress, [("Step 1/5: 使用已有字幕，跳过识别", 0),
                                    ("Step 4/5: LLM分析完成 (1/2)", 59)])
        self.assertEqual((report.report_path, report.topic_count, report.clip_count, report.failed_chunk_count),
                         ("报告.md", 3, 1, 1))
        self.assertEqual(report.summary, "3 个话题，1 个可切片段，1 段没分析成功")
        self.assertEqual(report.warning, "第 2 块没分析成功")

    def test_same_name_danmaku_is_picked_up(self):
        danmaku = self.video.with_suffix(".ass")
        danmaku.write_text("[Events]\n", encoding="utf-8")
        self.assertEqual(self.request().danmaku_path, str(danmaku))

    def test_problem_blocks_missing_or_empty_subtitle(self):
        self.assertEqual(TopicAnalysisService.problem(self.request()), "")
        missing = TopicRequest(str(self.video), str(self.root / "没有.srt"))
        self.assertIn("还没有字幕", TopicAnalysisService.problem(missing))
        self.subtitle.write_text("", encoding="utf-8")
        self.assertIn("空的", TopicAnalysisService.problem(self.request()))
        with self.assertRaisesRegex(ValueError, "空的"):
            self.service(lambda *_a, **_k: self.fail("不该开跑")).run(self.request())

    def test_cancel_before_start_never_calls_pipeline(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TopicAnalysisCancelled):
            self.service(lambda *_a, **_k: self.fail("不该开跑")).run(self.request(), cancel=cancel)

    def test_cancel_raises_at_next_progress_report(self):
        cancel = threading.Event()
        reached = []

        def runner(_video, **kwargs):
            kwargs["progress_callback"]("块 1", 30, 100)
            cancel.set()
            kwargs["progress_callback"]("块 2", 40, 100)
            reached.append("不该走到这里")
            return {}

        with self.assertRaises(TopicAnalysisCancelled):
            self.service(runner).run(self.request(), cancel=cancel)
        self.assertEqual(reached, [])

    def test_failure_after_cancel_counts_as_cancelled_but_plain_failure_propagates(self):
        cancel = threading.Event()

        def swallowed(_video, **_kwargs):
            # 流水线内部把取消异常吞成别的错误时，仍按“已停止”报
            cancel.set()
            raise RuntimeError("LLM API 连续 3 个分块失败")

        with self.assertRaises(TopicAnalysisCancelled):
            self.service(swallowed).run(self.request(), cancel=cancel)

        def broken(_video, **_kwargs):
            raise RuntimeError("字幕文件没有有效内容")

        with self.assertRaisesRegex(RuntimeError, "没有有效内容"):
            self.service(broken).run(self.request(), cancel=threading.Event())

    def test_existing_report_follows_web_artifact_layout(self):
        service = self.service(lambda *_a, **_k: {})
        self.assertIsNone(service.existing_report(str(self.video)))
        report = Path(service.layout(str(self.video))["report_path"])
        self.assertEqual(report, self.output / "测试录播_自动切片" / "01_话题分析.md")
        report.parent.mkdir(parents=True)
        report.write_text("# 报告", encoding="utf-8")
        self.assertEqual(service.existing_report(str(self.video)), str(report))

    def test_ai_problem_and_model_notice(self):
        self.assertEqual(self.service(None).ai_problem(), "")
        unconfigured = SimpleNamespace(configured=False, error="", text_model="")
        self.assertIn("未配置 AI", self.service(None, settings_reader=lambda: unconfigured).ai_problem())
        self.assertIn("有误", self.service(None, settings_reader=lambda: configured(error="坏 JSON")).ai_problem())
        self.assertEqual(self.service(None).model_notice(), "")
        notice = self.service(None, settings_reader=lambda: configured("deepseek-flash")).model_notice()
        self.assertIn("gpt-5.6-luna", notice)
        self.assertIn("deepseek-flash", notice)


class TopicServicePipelineWiringTests(unittest.TestCase):
    """接真的 run_pipeline：LLM 换成假的，识别入口一碰就失败。"""

    def test_real_pipeline_uses_existing_subtitle_without_touching_user_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "投稿" / "测试主播-2026年11月12日20点00分.flv"
            source.parent.mkdir()
            source.write_bytes(b"flv")
            subtitle = source.with_name(source.stem + "_校对.srt")
            subtitle.write_text(SRT, encoding="utf-8")
            before = {path.name: path.read_bytes() for path in source.parent.iterdir()}
            output = root / "输出"
            calls = []

            def fake_llm(prompt, **kwargs):
                calls.append(kwargs.get("reasoning_stage"))
                if kwargs.get("reasoning_stage") == "analysis":
                    return json.dumps({"topics": [{
                        "start": "0:01:00", "end": "0:06:00", "title": "聊新衣服",
                        "points": ["聊到新衣服的颜色", "回应观众的猜测"], "can_slice": False,
                    }]}, ensure_ascii=False)
                return json.dumps({"reviews": []})

            environment = {"AUTOSLICE_API_BASE_URL": "http://127.0.0.1:9", "AUTOSLICE_API_TOKEN": "test-token",
                           "AUTOSLICE_API_TYPE": "openai"}
            service = TopicAnalysisService(output, settings_reader=configured, model_name=lambda: "gpt-5.6-luna")
            progress = []
            with (
                patch.dict(os.environ, environment),
                patch("autoslice.llm.transport.call_llm_with_retry", side_effect=fake_llm),
                patch("autoslice.pipeline.ensure_srt", side_effect=AssertionError("已有字幕时不得识别")),
                patch("autoslice.pipeline.probe_video_duration", return_value=600),
            ):
                report = service.run(service.request_for(str(source), str(subtitle)),
                                     progress=lambda *args: progress.append(args), cancel=threading.Event())

            after = {path.name: path.read_bytes() for path in source.parent.iterdir()}
            report_text = Path(report.report_path).read_text(encoding="utf-8")
            report_inside_output = Path(report.report_path).resolve().is_relative_to(output.resolve())

        self.assertEqual(after, before, "投稿目录里的视频和字幕不能被改动或多出文件")
        self.assertTrue(report_inside_output)
        self.assertIn("analysis", calls)
        self.assertEqual(report.topic_count, 1)
        self.assertIn("聊新衣服", report_text)
        self.assertEqual(progress[0][0], "Step 1/5: 使用已有字幕，跳过识别")
        self.assertEqual(progress[-1][1], 100)
        self.assertEqual([step for _message, step in progress], sorted(step for _message, step in progress))


if __name__ == "__main__":
    unittest.main()
