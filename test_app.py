import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as app_module


class AutoCoverIntegrationTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def test_autocover_redirect_uses_only_configured_local_service(self):
        with patch.dict(
                os.environ,
                {"AUTOCOVER_URL": "http://127.0.0.1:5017"},
                clear=False):
            response = self.client.get("/autocover")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "http://127.0.0.1:5017")

        with patch.dict(
                os.environ,
                {"AUTOCOVER_URL": "https://example.com/steal"},
                clear=False):
            rejected = self.client.get("/autocover")

        self.assertEqual(rejected.headers["Location"], "http://127.0.0.1:5010")

    def test_all_primary_pages_link_to_autocover(self):
        for path in ("/", "/topic-v2", "/subtitle-workflow"):
            response = self.client.get(path)
            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/autocover"', html)
            self.assertIn("自动封面", html)

    def test_service_contract_reports_actual_autocover_url(self):
        with patch.dict(
                os.environ,
                {"AUTOCOVER_URL": "http://localhost:5013"},
                clear=False):
            response = self.client.get("/api/service")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "service": "autoslice",
            "api_version": 1,
            "autocover_url": "http://localhost:5013",
        })


class ImmediateThread:
    """测试中同步执行后台任务，便于核对最终状态。"""

    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        self.target(*self.args, **self.kwargs)


class TopicPipelineApiTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        app_module.tasks.clear()
        self.client = app_module.app.test_client()

    def test_update_task_does_not_fail_when_gbk_console_cannot_encode_emoji(self):
        raw_output = io.BytesIO()
        gbk_console = io.TextIOWrapper(
            raw_output,
            encoding="gbk",
            errors="strict",
        )

        with patch.object(app_module.sys, "stdout", gbk_console):
            app_module.update_task(
                "emoji_slice",
                status="done",
                progress="切片 1/19: 玩偶标题🧸",
                result='{"title":"玩偶标题🧸"}',
                step=1,
                total=19,
            )
            gbk_console.flush()

        output = raw_output.getvalue().decode("gbk")
        self.assertIn("切片 1/19", output)
        self.assertIn("emoji_slice", output)
        self.assertEqual(app_module.tasks["emoji_slice"]["status"], "done")

    def test_optimize_manual_timeline_rejects_missing_files(self):
        response = self.client.post(
            "/api/optimize-manual-timeline",
            json={
                "flv_path": r"F:\不存在\录播.flv",
                "manual_timeline_path": r"F:\不存在\时间轴.docx",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "视频文件不存在")

    def test_optimize_manual_timeline_is_independent_from_pipeline_and_slicing(self):
        with TemporaryDirectory() as td:
            flv_path = Path(td) / "泽音Melody-2026年07月14日19点59分.flv"
            ass_path = flv_path.with_suffix(".ass")
            timeline_path = Path(td) / "20260714.docx"
            optimized_json = flv_path.with_name(flv_path.stem + "_优化时间轴.json")
            optimized_md = flv_path.with_name(flv_path.stem + "_优化时间轴.md")
            for path in (flv_path, ass_path, timeline_path):
                path.write_bytes(b"test")
            expected = {
                "video_path": str(flv_path),
                "optimized_json_path": str(optimized_json),
                "optimized_md_path": str(optimized_md),
                "manual_timeline": {"path": str(timeline_path)},
            }

            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "topic_engine.optimize_manual_timeline_for_video",
                    return_value=expected,
                ) as optimize,
                patch(
                    "topic_engine.run_pipeline",
                    side_effect=AssertionError("独立优化不应运行完整分析"),
                ),
                patch(
                    "topic_engine.slice_from_marks",
                    side_effect=AssertionError("独立优化不应自动切片"),
                ),
            ):
                response = self.client.post(
                    "/api/optimize-manual-timeline",
                    json={
                        "flv_path": str(flv_path),
                        "ass_path": str(ass_path),
                        "manual_timeline_path": str(timeline_path),
                    },
                )

        self.assertEqual(response.status_code, 200)
        task_id = response.get_json()["task_id"]
        optimize.assert_called_once()
        self.assertEqual(app_module.tasks[task_id]["status"], "done")
        task_result = json.loads(app_module.tasks[task_id]["result"])
        self.assertEqual(task_result["optimized_json_path"], str(optimized_json))

    def test_start_pipeline_reuses_selected_optimized_timeline(self):
        with TemporaryDirectory() as td:
            flv_path = Path(td) / "泽音Melody-2026年07月14日19点59分.flv"
            timeline_path = Path(td) / "20260714.docx"
            optimized_path = Path(td) / "录播_优化时间轴.json"
            for path in (flv_path, timeline_path, optimized_path):
                path.write_bytes(b"test")
            pipeline_result = {
                "report": "# 测试报告",
                "topic_count": 3,
                "clip_marks": [],
                "json_path": str(Path(td) / "clip_marks.json"),
            }

            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch("topic_engine.run_pipeline", return_value=pipeline_result) as run_pipeline,
                patch(
                    "topic_engine.slice_from_marks",
                    side_effect=AssertionError("没有切片标记时不应调用切片"),
                ),
            ):
                response = self.client.post(
                    "/api/start-pipeline",
                    json={
                        "flv_path": str(flv_path),
                        "manual_timeline_mode": "manual",
                        "manual_timeline_path": str(timeline_path),
                        "optimized_timeline_path": str(optimized_path),
                    },
                )

        self.assertEqual(response.status_code, 200)
        run_pipeline.assert_called_once()
        self.assertEqual(
            run_pipeline.call_args.kwargs["optimized_timeline_path"],
            str(optimized_path),
        )
        self.assertEqual(
            run_pipeline.call_args.kwargs["manual_timeline_path"],
            str(timeline_path),
        )


class SubtitleWorkflowApiTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        app_module.tasks.clear()
        self.client = app_module.app.test_client()

    @staticmethod
    def _write_pair(root):
        folder = Path(root) / "【泽音】测试投稿"
        folder.mkdir()
        video = folder / "剪映导出.mp4"
        srt = folder / "剪映字幕.srt"
        video.write_bytes(b"video")
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n瓦衣\n",
            encoding="utf-8",
        )
        return video, srt

    def test_scan_returns_submission_pairs_and_missing_dir_is_400(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            response = self.client.post("/api/subtitles/scan", json={"root_dir": td})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["pairs"][0]["video_name"], video.name)
        self.assertEqual(payload["pairs"][0]["srt_name"], srt.name)
        missing = self.client.post(
            "/api/subtitles/scan",
            json={"root_dir": r"F:\不存在\投稿"},
        )
        self.assertEqual(missing.status_code, 400)

    def test_cues_and_save_validate_indices_without_overwriting_source(self):
        with TemporaryDirectory() as td:
            _, srt = self._write_pair(td)
            original = srt.read_bytes()
            cues_response = self.client.post(
                "/api/subtitles/cues",
                json={"srt_path": str(srt)},
            )
            invalid = self.client.post(
                "/api/subtitles/save",
                json={
                    "srt_path": str(srt),
                    "corrections": [{"index": 9, "corrected": "娃衣"}],
                },
            )
            saved = self.client.post(
                "/api/subtitles/save",
                json={
                    "srt_path": str(srt),
                    "corrections": [{
                        "index": 1,
                        "original": "瓦衣",
                        "corrected": "娃衣",
                    }],
                },
            )
            corrected = Path(saved.get_json()["corrected_srt_path"])
            corrected_text = corrected.read_text(encoding="utf-8")
            source_after = srt.read_bytes()

        self.assertEqual(cues_response.status_code, 200)
        self.assertEqual(cues_response.get_json()["cues"][0]["text"], "瓦衣")
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("序号不存在", invalid.get_json()["error"])
        self.assertEqual(saved.status_code, 200)
        self.assertIn("娃衣", corrected_text)
        self.assertEqual(source_after, original)

    def test_review_runs_in_background_and_exposes_default_corrections(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            review_result = {
                "suggestions": [{
                    "index": 1,
                    "original": "瓦衣",
                    "corrected": "娃衣",
                    "confidence": 0.97,
                }],
            }
            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "subtitle_workflow.suggest_subtitle_corrections",
                    return_value=review_result,
                ) as review,
            ):
                response = self.client.post(
                    "/api/subtitles/review",
                    json={"video_path": str(video), "srt_path": str(srt)},
                )

        self.assertEqual(response.status_code, 200)
        task_id = response.get_json()["task_id"]
        self.assertEqual(app_module.tasks[task_id]["status"], "done")
        result = json.loads(app_module.tasks[task_id]["result"])
        self.assertEqual(result["default_corrections"][0]["corrected"], "娃衣")
        self.assertEqual(review.call_args.kwargs["context_title"], "【泽音】测试投稿")

    def test_preview_returns_jpeg_and_rejects_mismatched_directory(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            with patch(
                "subtitle_workflow.render_subtitle_preview",
                return_value=(b"\xff\xd8preview", 0.5),
            ) as preview:
                response = self.client.post(
                    "/api/subtitles/preview",
                    json={
                        "video_path": str(video),
                        "srt_path": str(srt),
                        "style": {"font_name": "Noto Sans S Chinese Black"},
                    },
                )
            other = Path(td) / "other"
            other.mkdir()
            other_srt = other / "字幕.srt"
            other_srt.write_text(srt.read_text(encoding="utf-8"), encoding="utf-8")
            mismatch = self.client.post(
                "/api/subtitles/preview",
                json={"video_path": str(video), "srt_path": str(other_srt)},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.headers["X-Subtitle-Preview-Time"], "0.500")
        preview.assert_called_once()
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn("同一投稿目录", mismatch.get_json()["error"])

    def test_render_task_completes_and_rejects_source_overwrite(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            output = video.with_name("完成_字幕版.mp4")
            render_result = {
                "output_video_path": str(output),
                "encoder": "h264_nvenc",
            }
            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "subtitle_workflow.burn_subtitles",
                    return_value=render_result,
                ) as render,
            ):
                response = self.client.post(
                    "/api/subtitles/render",
                    json={
                        "video_path": str(video),
                        "srt_path": str(srt),
                        "output_path": str(output),
                    },
                )
            overwrite = self.client.post(
                "/api/subtitles/render",
                json={
                    "video_path": str(video),
                    "srt_path": str(srt),
                    "output_path": str(video),
                },
            )

        self.assertEqual(response.status_code, 200)
        task_id = response.get_json()["task_id"]
        self.assertEqual(app_module.tasks[task_id]["status"], "done")
        render.assert_called_once()
        self.assertEqual(overwrite.status_code, 400)
        self.assertIn("不能覆盖", overwrite.get_json()["error"])

    def test_render_failure_is_recorded_as_task_error(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "subtitle_workflow.burn_subtitles",
                    side_effect=RuntimeError("编码失败"),
                ),
            ):
                response = self.client.post(
                    "/api/subtitles/render",
                    json={"video_path": str(video), "srt_path": str(srt)},
                )

        task_id = response.get_json()["task_id"]
        self.assertEqual(app_module.tasks[task_id]["status"], "error")
        self.assertIn("编码失败", app_module.tasks[task_id]["result"])


if __name__ == "__main__":
    unittest.main()
