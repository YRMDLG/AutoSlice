import io
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as app_module
from runtime_config import configured_path


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

    def test_request_boundary_rejects_untrusted_host_and_cross_site_write(self):
        self.assertEqual(
            self.client.get(
                "/api/service",
                headers={"Host": "attacker.example"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/scan",
                json={"video_dir": "missing"},
                headers={"Origin": "https://attacker.example"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/api/service",
                headers={"Host": "127.0.0.1:5002"},
            ).status_code,
            200,
        )

    def test_lan_mode_requires_token_and_restricts_paths(self):
        with TemporaryDirectory() as allowed_dir, TemporaryDirectory() as blocked_dir:
            env = {
                "AUTOSLICE_LAN_MODE": "1",
                "AUTOSLICE_LAN_TOKEN": "secure-token-" + "x" * 24,
                "AUTOSLICE_LAN_HOSTS": "192.168.1.20",
                "AUTOSLICE_ALLOWED_ROOTS": allowed_dir,
            }
            headers = {
                "Host": "192.168.1.20:5002",
                "Origin": "http://192.168.1.20:5002",
            }
            with patch.dict(os.environ, env, clear=False):
                unauthenticated = self.client.post(
                    "/api/subtitles/scan",
                    json={"root_dir": allowed_dir},
                    headers=headers,
                )
                blocked = self.client.post(
                    "/api/subtitles/scan",
                    json={"root_dir": blocked_dir},
                    headers={
                        **headers,
                        "X-AutoSlice-Token": env["AUTOSLICE_LAN_TOKEN"],
                    },
                )
                allowed = self.client.post(
                    "/api/subtitles/scan",
                    json={"root_dir": allowed_dir},
                    headers={
                        **headers,
                        "X-AutoSlice-Token": env["AUTOSLICE_LAN_TOKEN"],
                    },
                )

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(allowed.status_code, 200)

    def test_all_primary_pages_link_to_autocover(self):
        for path in ("/", "/topic-v2", "/direct-slice", "/subtitle-workflow"):
            response = self.client.get(path)
            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/autocover"', html)
            self.assertIn("自动封面", html)

    def test_new_pipeline_is_home_and_direct_slice_is_advanced(self):
        home = self.client.get("/").get_data(as_text=True)
        advanced = self.client.get("/direct-slice").get_data(as_text=True)

        self.assertIn("开始分析+切片", home)
        self.assertNotIn("全部切片", home)
        self.assertIn("JSON 标记重新切片", advanced)
        self.assertNotIn("全部切片", advanced)
        self.assertEqual(self.client.post("/api/slice-all", json={}).status_code, 404)

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

    def test_workspace_paths_are_generic_configurable_and_browser_persisted(self):
        with patch.dict(
                os.environ,
                {"AUTOSLICE_VIDEO_DIR": r"X:\fixtures\Recordings"},
                clear=False):
            configured = configured_path(
                "AUTOSLICE_VIDEO_DIR",
                r"X:\runtime\Fallback",
            )

        self.assertEqual(configured, Path(r"X:\fixtures\Recordings").resolve())
        for path in ("/", "/topic-v2", "/direct-slice"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotIn("1947277414", html)
            self.assertNotIn("DanmakuRender-5", html)
            self.assertNotIn(r"X:\fixtures\录播上传", html)
            self.assertIn("autoslice.video-dir", html)
            self.assertIn("autoslice.output-dir", html)


class SubtitleWorkflowPageTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def _page_script(self):
        response = self.client.get("/subtitle-workflow")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        matches = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
        self.assertTrue(matches)
        return html, matches[-1]

    def test_subtitle_workspace_keeps_all_three_desktop_panels_reachable(self):
        html, _script = self._page_script()
        with self.client.get("/static/workbench.css") as response:
            css = response.get_data(as_text=True)

        self.assertIn("overflow-x:auto;overflow-y:hidden", html)
        self.assertIn(
            "grid-template-columns:240px minmax(560px,1fr) 310px",
            html,
        )
        self.assertIn("overflow-x: auto", css)
        self.assertIn("overflow-x: auto", css.split(".topnav", 1)[1])

    def test_review_script_tracks_task_ownership_and_protects_manual_edits(self):
        html, script = self._page_script()

        for marker in (
            "taskEvents:new Map()",
            "taskContexts:new Map()",
            "state.taskContexts.get(data.task_id)",
            "if(!context)return",
            "registerTask(data.task_id,context)",
            "state.aiApplied",
            "state.protectedEdits",
            "sourceText(index)!==item.original",
            "correctedTimelineMatches",
        ):
            self.assertIn(marker, script)
        self.assertIn("重新检查", html)
        self.assertNotIn(
            "data.task_id.startsWith('subtitle_review_'))applyReview(result)",
            script,
        )

    def test_review_queue_exposes_persistent_folder_and_name_sorting(self):
        html, script = self._page_script()

        self.assertIn('id="queueSort"', html)
        for value in (
                "folder_created_desc", "folder_created_asc",
                "folder_modified_desc", "source_modified_desc",
                "name_asc", "name_desc"):
            self.assertIn(f'value="{value}"', html)
        for marker in (
                "autoslice.subtitle-queue-sort",
                "function comparePairs(",
                "function sortPairs(",
                "const selectedId=selectedPair()?.id",
                "state.pairs.findIndex(pair=>pair.id===selectedId)",
        ):
            self.assertIn(marker, script)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查页面脚本语法")
    def test_review_page_script_compiles(self):
        _, script = self._page_script()
        result = subprocess.run(
            ["node", "-e", "new Function(require('fs').readFileSync(0,'utf8'))"],
            input=script,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ImmediateThread:
    """测试中同步执行后台任务，便于核对最终状态。"""

    def __init__(self, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        self.target(*self.args, **self.kwargs)


class DeferredThread(ImmediateThread):
    """保留 queued 状态，用于验证重复任务拦截。"""

    def start(self):
        pass


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
                "flv_path": r"X:\fixtures\不存在\录播.flv",
                "manual_timeline_path": r"X:\fixtures\不存在\时间轴.docx",
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
            output_dir = Path(td) / "自动切片"
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
                        "output_dir": str(output_dir),
                        "streamer_profile_id": "zeyin",
                    },
                )

        self.assertEqual(response.status_code, 200)
        task_id = response.get_json()["task_id"]
        optimize.assert_called_once()
        self.assertEqual(
            optimize.call_args.kwargs["output_dir"],
            str(output_dir.resolve()),
        )
        self.assertEqual(
            optimize.call_args.kwargs["streamer_profile_id"].id,
            "zeyin",
        )
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
            output_dir = Path(td) / "自动切片"

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
                        "output_dir": str(output_dir),
                        "streamer_profile_id": "zeyin",
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
        self.assertEqual(
            run_pipeline.call_args.kwargs["output_dir"],
            str(output_dir.resolve()),
        )
        self.assertEqual(
            run_pipeline.call_args.kwargs["streamer_profile_id"].id,
            "zeyin",
        )

    def test_retry_clip_review_reuses_artifacts_reslices_and_blocks_pipeline(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            flv_path = root / "录播.flv"
            ass_path = root / "录播.ass"
            output_dir = root / "自动切片"
            artifact_dir = output_dir / "录播_自动切片"
            json_path = artifact_dir / "数据" / "clip_marks.json"
            for path in (flv_path, ass_path):
                path.write_bytes(b"test")
            result = {
                "report": "# 复核报告",
                "topic_count": 2,
                "clip_marks": [{"start": 10, "end": 80, "title": "测试"}],
                "json_path": str(json_path),
                "artifact_dir": str(artifact_dir),
                "overview_path": str(artifact_dir / "00_概览.md"),
            }

            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "topic_engine.retry_clip_review_from_artifacts",
                    return_value=result,
                ) as retry,
                patch(
                    "topic_engine.slice_from_marks",
                    return_value=(1, str(output_dir / "录播_话题切片")),
                ) as slicer,
            ):
                response = self.client.post(
                    "/api/retry-clip-review",
                    json={
                        "flv_path": str(flv_path),
                        "ass_path": str(ass_path),
                        "output_dir": str(output_dir),
                        "streamer_profile_id": "generic",
                    },
                )

            self.assertEqual(response.status_code, 200)
            task_id = response.get_json()["task_id"]
            retry.assert_called_once()
            self.assertEqual(retry.call_args.args, (str(flv_path),))
            self.assertEqual(retry.call_args.kwargs["ass_path"], str(ass_path))
            self.assertEqual(
                retry.call_args.kwargs["output_dir"],
                str(output_dir.resolve()),
            )
            self.assertEqual(
                retry.call_args.kwargs["streamer_profile_id"].id,
                "generic",
            )
            slicer.assert_called_once()
            task = app_module.tasks[task_id]
            self.assertEqual(task["status"], "done")
            self.assertEqual(task["task_type"], "clip_review_retry")
            task_result = json.loads(task["result"])
            self.assertEqual(task_result["slice_count"], 1)

            app_module.tasks.clear()
            with patch.object(app_module.threading, "Thread", DeferredThread):
                queued = self.client.post(
                    "/api/retry-clip-review",
                    json={"flv_path": str(flv_path), "output_dir": str(output_dir)},
                )
                blocked = self.client.post(
                    "/api/start-pipeline",
                    json={"flv_path": str(flv_path), "output_dir": str(output_dir)},
                )

        self.assertEqual(queued.status_code, 200)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()["task_id"], queued.get_json()["task_id"])

    def test_json_timeline_reslice_uses_explicit_mark_range(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            flv_path = root / "录播.flv"
            json_path = root / "clip_marks.json"
            output_dir = root / "自动切片"
            flv_path.write_bytes(b"video")
            json_path.write_text(
                json.dumps(
                    {
                        "expanded_with_context": True,
                        "time_basis": "video_elapsed_seconds",
                        "clip_marks": [
                            {"start": 100, "end": 120, "title": "测试片段"}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "topic_engine.slice_from_marks",
                    return_value=(1, str(output_dir / "录播_话题切片")),
                ) as slicer,
                patch.object(
                    app_module,
                    "process_video",
                    side_effect=AssertionError("JSON 标记不应再走旧时间轴切片"),
                ),
            ):
                response = self.client.post(
                    "/api/slice",
                    json={
                        "flv_path": str(flv_path),
                        "output_dir": str(output_dir),
                        "mode": "timeline-json",
                        "timeline_json": str(json_path),
                    },
                )

        self.assertEqual(response.status_code, 200)
        slicer.assert_called_once()
        self.assertEqual(
            slicer.call_args.args,
            (str(flv_path), str(json_path), str(output_dir)),
        )
        self.assertTrue(callable(slicer.call_args.kwargs["progress_callback"]))
        self.assertEqual(
            slicer.call_args.kwargs["streamer_profile_id"].id,
            "generic",
        )
        task = app_module.tasks[response.get_json()["task_id"]]
        self.assertEqual(task["status"], "done")
        self.assertIn("1 个片段", task["progress"])

    def test_direct_slice_blocks_different_sources_targeting_same_output(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            first_dir = root / "来源一"
            second_dir = root / "来源二"
            output_dir = root / "输出"
            first_dir.mkdir()
            second_dir.mkdir()
            first_video = first_dir / "same.flv"
            second_video = second_dir / "same.flv"
            first_json = first_dir / "marks.json"
            second_json = second_dir / "marks.json"
            for video in (first_video, second_video):
                video.write_bytes(b"video")
            for json_path in (first_json, second_json):
                json_path.write_text(
                    '{"expanded_with_context":true,"clip_marks":[]}',
                    encoding="utf-8",
                )

            with patch.object(app_module.threading, "Thread", DeferredThread):
                first = self.client.post(
                    "/api/slice",
                    json={
                        "flv_path": str(first_video),
                        "output_dir": str(output_dir),
                        "mode": "timeline-json",
                        "timeline_json": str(first_json),
                    },
                )
                conflict = self.client.post(
                    "/api/slice",
                    json={
                        "flv_path": str(second_video),
                        "output_dir": str(output_dir),
                        "mode": "timeline-json",
                        "timeline_json": str(second_json),
                    },
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.get_json()["task_id"],
            first.get_json()["task_id"],
        )

    def test_legacy_json_parser_preserves_complete_time_contract(self):
        from core import parse_timeline_json

        with TemporaryDirectory() as td:
            json_path = Path(td) / "clip_marks.json"
            json_path.write_text(
                json.dumps(
                    {
                        "time_basis": "video_elapsed_seconds",
                        "clip_marks": [{
                            "start": 100,
                            "end": 120,
                            "topic_start": 103,
                            "topic_end": 118,
                            "title": "测试片段",
                        }],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            marks = parse_timeline_json(str(json_path))

        self.assertEqual(marks, [{
            "start": 100.0,
            "end": 120.0,
            "topic_start": 103.0,
            "topic_end": 118.0,
            "title": "测试片段",
            "time_basis": "video_elapsed_seconds",
        }])

    def test_legacy_asr_entry_delegates_to_atomic_engine(self):
        from core import generate_srt

        progress = []
        with patch(
            "topic_engine.ensure_srt",
            return_value=r"X:\fixtures\录播\测试.srt",
        ) as ensure:
            result = generate_srt(
                r"X:\fixtures\录播\测试.flv",
                progress_callback=lambda *args: progress.append(args),
            )

        self.assertEqual(result, r"X:\fixtures\录播\测试.srt")
        ensure.assert_called_once_with(
            r"X:\fixtures\录播\测试.flv",
            progress_callback=unittest.mock.ANY,
        )

    def test_open_result_directory_uses_only_completed_task_artifact(self):
        with TemporaryDirectory() as td:
            output_dir = Path(td) / "自动切片"
            artifact_dir = output_dir / "录播_自动切片"
            artifact_dir.mkdir(parents=True)
            app_module.tasks["pipeline_ok"] = {
                "status": "done",
                "task_type": "topic_pipeline",
                "output_dir": str(output_dir),
                "result": json.dumps(
                    {"artifact_dir": str(artifact_dir)}, ensure_ascii=False
                ),
            }
            with patch.object(app_module.subprocess, "Popen") as popen:
                response = self.client.post(
                    "/api/open-result-directory",
                    json={"task_id": "pipeline_ok"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["path"], str(artifact_dir.resolve()))
        popen.assert_called_once_with(["explorer.exe", str(artifact_dir.resolve())])

    def test_open_result_directory_rejects_arbitrary_and_outside_paths(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            output_dir = root / "自动切片"
            outside_dir = root / "其他目录" / "伪造_自动切片"
            output_dir.mkdir()
            outside_dir.mkdir(parents=True)
            app_module.tasks["pipeline_outside"] = {
                "status": "done",
                "task_type": "topic_pipeline",
                "output_dir": str(output_dir),
                "result": json.dumps({"artifact_dir": str(outside_dir)}),
            }
            with patch.object(app_module.subprocess, "Popen") as popen:
                arbitrary = self.client.post(
                    "/api/open-result-directory",
                    json={"artifact_dir": str(outside_dir)},
                )
                outside = self.client.post(
                    "/api/open-result-directory",
                    json={"task_id": "pipeline_outside"},
                )

        self.assertEqual(arbitrary.status_code, 400)
        self.assertEqual(outside.status_code, 403)
        popen.assert_not_called()

    def test_open_result_directory_rejects_missing_directory(self):
        with TemporaryDirectory() as td:
            output_dir = Path(td) / "自动切片"
            output_dir.mkdir()
            missing_dir = output_dir / "录播_自动切片"
            app_module.tasks["pipeline_missing"] = {
                "status": "done",
                "task_type": "topic_pipeline",
                "output_dir": str(output_dir),
                "result": json.dumps({"artifact_dir": str(missing_dir)}),
            }
            with patch.object(app_module.subprocess, "Popen") as popen:
                response = self.client.post(
                    "/api/open-result-directory",
                    json={"task_id": "pipeline_missing"},
                )

        self.assertEqual(response.status_code, 404)
        popen.assert_not_called()

    def test_topic_v2_page_exposes_artifact_paths_and_safe_open_action(self):
        response = self.client.get("/topic-v2")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("打开结果目录", html)
        self.assertIn("/api/open-result-directory", html)
        self.assertIn("/api/retry-clip-review", html)
        self.assertIn("仅重新复核候选", html)
        self.assertIn("result.overview_path", html)
        self.assertIn("result.artifact_dir", html)
        self.assertIn('id="streamerProfile"', html)
        self.assertIn("/api/streamer-profiles", html)
        self.assertIn("autoslice.streamer-profile", html)
        self.assertEqual(html.count("streamer_profile_id:selectedStreamerProfile()"), 3)
        self.assertGreaterEqual(
            html.count("output_dir:document.getElementById('outputDir').value"),
            2,
        )

    def test_streamer_profiles_api_is_public_only_and_unknown_id_is_rejected(self):
        response = self.client.get("/api/streamer-profiles")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [profile["id"] for profile in payload["profiles"]],
            ["auto", "generic", "zeyin"],
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        for private_key in (
                "path_keywords", "title_style_profile", "asr_replacements"):
            self.assertNotIn(private_key, serialized)

        with TemporaryDirectory() as td:
            flv_path = Path(td) / "录播.flv"
            flv_path.write_bytes(b"video")
            invalid = self.client.post(
                "/api/start-pipeline",
                json={
                    "flv_path": str(flv_path),
                    "streamer_profile_id": "missing",
                },
            )

        self.assertEqual(invalid.status_code, 400)
        self.assertIn("未知主播配置", invalid.get_json()["error"])

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查页面脚本语法")
    def test_topic_v2_page_script_compiles(self):
        response = self.client.get("/topic-v2")
        scripts = re.findall(
            r"<script>(.*?)</script>", response.get_data(as_text=True), flags=re.S
        )
        self.assertTrue(scripts)
        result = subprocess.run(
            ["node", "-e", "new Function(require('fs').readFileSync(0,'utf8'))"],
            input=scripts[-1],
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pipeline_ids_are_unique_and_duplicate_running_source_is_rejected(self):
        with TemporaryDirectory() as td:
            flv_path = Path(td) / "同一场录播.flv"
            flv_path.write_bytes(b"video")
            pipeline_result = {
                "report": "# 测试",
                "topic_count": 1,
                "clip_marks": [],
                "json_path": str(Path(td) / "marks.json"),
            }
            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch("topic_engine.run_pipeline", return_value=pipeline_result),
            ):
                first = self.client.post(
                    "/api/start-pipeline",
                    json={"flv_path": str(flv_path)},
                )
                second = self.client.post(
                    "/api/start-pipeline",
                    json={"flv_path": str(flv_path)},
                )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertNotEqual(first.get_json()["task_id"], second.get_json()["task_id"])
            self.assertEqual(
                app_module.tasks[first.get_json()["task_id"]]["task_type"],
                "topic_pipeline",
            )

            app_module.tasks.clear()
            with patch.object(app_module.threading, "Thread", DeferredThread):
                running = self.client.post(
                    "/api/start-pipeline",
                    json={"flv_path": str(flv_path)},
                )
                duplicate = self.client.post(
                    "/api/start-pipeline",
                    json={"flv_path": str(flv_path)},
                )

        self.assertEqual(running.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.get_json()["task_id"],
            running.get_json()["task_id"],
        )

    def test_timeline_task_rejects_same_source_while_queued(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            flv_path = root / "录播.flv"
            timeline_path = root / "时间轴.docx"
            for path in (flv_path, timeline_path):
                path.write_bytes(b"test")

            with patch.object(app_module.threading, "Thread", DeferredThread):
                first_timeline = self.client.post(
                    "/api/optimize-manual-timeline",
                    json={
                        "flv_path": str(flv_path),
                        "manual_timeline_path": str(timeline_path),
                    },
                )
                duplicate_timeline = self.client.post(
                    "/api/optimize-manual-timeline",
                    json={
                        "flv_path": str(flv_path),
                        "manual_timeline_path": str(timeline_path),
                    },
                )
        self.assertEqual(first_timeline.status_code, 200)
        self.assertEqual(duplicate_timeline.status_code, 409)
        self.assertEqual(
            duplicate_timeline.get_json()["task_id"],
            first_timeline.get_json()["task_id"],
        )

    def test_removed_legacy_topic_and_task_routes_return_404(self):
        self.assertEqual(self.client.get("/topic").status_code, 404)
        self.assertEqual(self.client.post("/api/analyze-topics", json={}).status_code, 404)
        self.assertEqual(self.client.get("/api/tasks").status_code, 404)

    def test_pipeline_error_result_redacts_secrets_paths_and_traceback(self):
        with TemporaryDirectory() as td:
            flv_path = Path(td) / "录播.flv"
            flv_path.write_bytes(b"video")
            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "topic_engine.run_pipeline",
                    side_effect=RuntimeError(
                        r"token=sk-private-value 位于 X:\fixtures\个人资料\api_config.json"
                    ),
                ),
                patch.object(app_module.app.logger, "error") as logger,
            ):
                response = self.client.post(
                    "/api/start-pipeline",
                    json={"flv_path": str(flv_path)},
                )

        task = app_module.tasks[response.get_json()["task_id"]]
        self.assertEqual(task["status"], "error")
        self.assertNotIn("sk-private-value", task["result"])
        self.assertNotIn(r"X:\fixtures\个人资料", task["result"])
        self.assertNotIn("Traceback", task["result"])
        self.assertIn("[已隐藏]", task["result"])
        logger.assert_called_once()


class WebTransportSafetyTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        app_module.tasks.clear()
        if hasattr(app_module, "event_queue_lock"):
            with app_module.event_queue_lock:
                app_module.event_queues.clear()
        else:
            app_module.event_queues.clear()
        self.client = app_module.app.test_client()

    def tearDown(self):
        if hasattr(app_module, "event_queue_lock"):
            with app_module.event_queue_lock:
                app_module.event_queues.clear()
        else:
            app_module.event_queues.clear()

    def test_broadcast_uses_subscriber_snapshot_during_concurrent_registration(self):
        late_queue = app_module.queue.Queue()

        class RegisteringQueue:
            def put_nowait(self, _message):
                with app_module.event_queue_lock:
                    app_module.event_queues.append(late_queue)

        with app_module.event_queue_lock:
            app_module.event_queues.append(RegisteringQueue())
        app_module.broadcast("test", {"ok": True})

        self.assertTrue(late_queue.empty())

    def test_task_history_pruning_keeps_active_and_recent_entries(self):
        app_module.tasks.update({
            "active": {
                "status": "running",
                "created_at": 1,
            },
            "expired": {
                "status": "done",
                "completed_at": 80,
            },
            "recent-1": {
                "status": "done",
                "completed_at": 95,
            },
            "recent-2": {
                "status": "error",
                "completed_at": 96,
            },
            "recent-3": {
                "status": "done",
                "completed_at": 97,
            },
        })

        with (
            patch.object(app_module, "_TASK_HISTORY_TTL_SEC", 10),
            patch.object(app_module, "_TASK_HISTORY_LIMIT", 2),
            app_module.task_lock,
        ):
            app_module._prune_tasks_locked(now=100)

        self.assertIn("active", app_module.tasks)
        self.assertNotIn("expired", app_module.tasks)
        self.assertNotIn("recent-1", app_module.tasks)
        self.assertEqual(
            set(app_module.tasks),
            {"active", "recent-2", "recent-3"},
        )

    def test_sse_generator_exits_after_subscriber_is_removed(self):
        response = self.client.get("/api/events", buffered=False)
        stream = iter(response.response)
        initial = next(stream)
        self.assertIn(b"event: init", initial)
        with app_module.event_queue_lock:
            subscriber = app_module.event_queues[0]
            app_module.event_queues.remove(subscriber)

        with self.assertRaises(StopIteration):
            next(stream)
        response.close()

    def test_uploads_reject_path_traversal_and_wrong_extensions(self):
        cases = [
            ("/api/upload-json-timeline", "../secret.json", b"{}"),
            ("/api/upload-json-timeline", "timeline.exe", b"{}"),
            ("/api/upload-timeline", r"..\\secret.docx", b"docx"),
            ("/api/upload-timeline", "timeline.json", b"{}"),
        ]
        for endpoint, filename, content in cases:
            with self.subTest(endpoint=endpoint, filename=filename):
                response = self.client.post(
                    endpoint,
                    data={"file": (io.BytesIO(content), filename)},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 400)

    def test_valid_uploads_stay_inside_configured_directories(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            json_dir = root / "json"
            docx_dir = root / "docx"
            with (
                patch.object(app_module, "JSON_TIMELINE_UPLOAD_DIR", json_dir),
                patch.object(app_module, "MANUAL_TIMELINE_UPLOAD_DIR", docx_dir),
            ):
                json_response = self.client.post(
                    "/api/upload-json-timeline",
                    data={"file": (io.BytesIO(b'{"clip_marks": []}'), "时间轴.json")},
                    content_type="multipart/form-data",
                )
                docx_response = self.client.post(
                    "/api/upload-timeline",
                    data={"file": (io.BytesIO(b"docx"), "20260717.docx")},
                    content_type="multipart/form-data",
                )

            json_path = Path(json_response.get_json()["path"])
            docx_path = Path(docx_response.get_json()["path"])

        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(docx_response.status_code, 200)
        self.assertEqual(json_path.parent, json_dir)
        self.assertEqual(docx_path.parent, docx_dir)


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
            json={"root_dir": r"X:\fixtures\不存在\投稿"},
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
        self.assertEqual(app_module.tasks[task_id]["task_type"], "subtitle_review")
        self.assertEqual(app_module.tasks[task_id]["source_srt_path"], str(srt.resolve()))
        self.assertFalse(app_module.tasks[task_id]["force"])

    def test_force_review_bypasses_cache_and_each_completed_run_has_unique_id(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            review_result = {"suggestions": []}
            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "subtitle_workflow.suggest_subtitle_corrections",
                    return_value=review_result,
                ) as review,
            ):
                first = self.client.post(
                    "/api/subtitles/review",
                    json={
                        "video_path": str(video),
                        "srt_path": str(srt),
                        "force": True,
                    },
                )
                second = self.client.post(
                    "/api/subtitles/review",
                    json={
                        "video_path": str(video),
                        "srt_path": str(srt),
                        "force": True,
                    },
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.get_json()["task_id"], second.get_json()["task_id"])
        self.assertEqual(review.call_count, 2)
        self.assertFalse(review.call_args.kwargs["use_cache"])

    def test_duplicate_running_review_is_rejected(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            with patch.object(app_module.threading, "Thread", DeferredThread):
                first = self.client.post(
                    "/api/subtitles/review",
                    json={"video_path": str(video), "srt_path": str(srt)},
                )
                duplicate = self.client.post(
                    "/api/subtitles/review",
                    json={
                        "video_path": str(video),
                        "srt_path": str(srt),
                        "force": True,
                    },
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.get_json()["task_id"],
            first.get_json()["task_id"],
        )
        self.assertIn("正在检查", duplicate.get_json()["error"])

    def test_review_rejects_non_boolean_force_and_invalid_glossary_items(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            invalid_force = self.client.post(
                "/api/subtitles/review",
                json={
                    "video_path": str(video),
                    "srt_path": str(srt),
                    "force": "false",
                },
            )
            invalid_glossary = self.client.post(
                "/api/subtitles/review",
                json={
                    "video_path": str(video),
                    "srt_path": str(srt),
                    "glossary": ["音音", {"错误": "对象"}],
                },
            )

        self.assertEqual(invalid_force.status_code, 400)
        self.assertIn("force 必须是布尔值", invalid_force.get_json()["error"])
        self.assertEqual(invalid_glossary.status_code, 400)
        self.assertIn("词条必须是字符串", invalid_glossary.get_json()["error"])

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

    def test_duplicate_subtitle_render_is_atomically_rejected(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            with patch.object(app_module.threading, "Thread", DeferredThread):
                first = self.client.post(
                    "/api/subtitles/render",
                    json={"video_path": str(video), "srt_path": str(srt)},
                )
                duplicate = self.client.post(
                    "/api/subtitles/render",
                    json={"video_path": str(video), "srt_path": str(srt)},
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.get_json()["task_id"],
            first.get_json()["task_id"],
        )


if __name__ == "__main__":
    unittest.main()
