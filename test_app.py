import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as app_module
from subtitle_workflow import parse_srt_document


def assert_same_path(testcase, actual, expected):
    """Windows 可能为同一临时路径返回 8.3 短路径，不能做字符串比较。"""

    actual_path = Path(actual)
    expected_path = Path(expected)
    if actual_path.exists() and expected_path.exists():
        testcase.assertTrue(
            actual_path.samefile(expected_path),
            f"路径不一致: {actual_path} != {expected_path}",
        )
        return
    if os.name == "nt":
        actual_suffix = _temporary_path_suffix(actual_path)
        expected_suffix = _temporary_path_suffix(expected_path)
        if actual_suffix is not None and expected_suffix is not None:
            testcase.assertEqual(actual_suffix, expected_suffix)
            return
    testcase.assertEqual(
        os.path.normcase(os.path.normpath(str(actual_path))),
        os.path.normcase(os.path.normpath(str(expected_path))),
    )


def _temporary_path_suffix(path):
    """提取临时目录后的部分，兼容 Windows 的用户目录 8.3 短路径。"""

    path_parts = tuple(part.casefold() for part in Path(path).parts)
    temp_parts = tuple(part.casefold() for part in Path(tempfile.gettempdir()).parts)
    for marker_size in range(min(3, len(temp_parts)), 0, -1):
        marker = temp_parts[-marker_size:]
        for index in range(len(path_parts) - marker_size + 1):
            if path_parts[index:index + marker_size] == marker:
                return path_parts[index + marker_size:]
    return None


class ScanApiTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def test_scan_supports_declared_formats_and_excludes_non_video_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video_names = (
                "01_直播.FLV",
                "02_直播.mp4",
                "03_直播.MkV",
                "04_直播.MOV",
                "05_直播.aVi",
            )
            for name in video_names:
                (root / name).write_bytes(b"video")
            (root / "02_直播.ass").write_text("danmaku", encoding="utf-8")
            (root / "02_直播.srt").write_text("subtitle", encoding="utf-8")
            (root / "03_直播.srt").write_text("", encoding="utf-8")
            (root / "说明.txt").write_text("not video", encoding="utf-8")
            (root / "伪装.mp4").mkdir()
            (root / "[正在录制]未完成.MP4").write_bytes(b"video")
            (root / "[录制中]未完成.avi").write_bytes(b"video")

            response = self.client.post(
                "/api/scan",
                json={"video_dir": str(root)},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], len(video_names))
        self.assertEqual(
            [video["name"] for video in payload["videos"]],
            list(video_names),
        )
        by_name = {video["name"]: video for video in payload["videos"]}
        self.assertTrue(by_name["02_直播.mp4"]["has_ass"])
        self.assertTrue(by_name["02_直播.mp4"]["has_srt"])
        self.assertFalse(by_name["03_直播.MkV"]["has_srt"])
        self.assertFalse(by_name["05_直播.aVi"]["has_ass"])


class TopicPageContractTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def _page_script(self):
        response = self.client.get("/topic-v2")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        matches = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
        self.assertTrue(matches)
        return html, matches[-1]

    def test_topic_page_uses_generic_video_filename_derivation(self):
        _html, script = self._page_script()

        self.assertIn("function replaceFileExtension(path,extension)", script)
        self.assertIn("replaceFileExtension(video.path,'.ass')", script)
        self.assertIn("name.textContent=video.name", script)
        self.assertNotIn(".flv", script.casefold())

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查文件名派生")
    def test_topic_page_derives_sidecars_for_every_supported_video_suffix(self):
        _html, script = self._page_script()
        helper_match = re.search(
            r"function replaceFileExtension\(path,extension\)\{.*?\n\}",
            script,
            flags=re.S,
        )
        self.assertIsNotNone(helper_match)
        cases = [
            [r"X:\录播\测试.FLV", r"X:\录播\测试.ass"],
            ["/录播/多点.标题.mp4", "/录播/多点.标题.ass"],
            ["/录播/测试.MkV", "/录播/测试.ass"],
            ["/录播/测试.MOV", "/录播/测试.ass"],
            ["/录播/测试.aVi", "/录播/测试.ass"],
        ]
        runtime = (
            helper_match.group(0)
            + f"\nconst cases={json.dumps(cases, ensure_ascii=False)};"
            + "for(const [source,expected] of cases){"
            + "const actual=replaceFileExtension(source,'.ass');"
            + "if(actual!==expected)throw new Error(`${source}: ${actual}`);}"
        )

        result = subprocess.run(
            [
                "node",
                "-e",
                "new Function(require('fs').readFileSync(0,'utf8'))();",
            ],
            input=runtime,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


class AutoCoverIntegrationTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.legacy_flag = patch.dict(
            os.environ,
            {app_module.LEGACY_DIRECT_SLICE_ENV: ""},
            clear=False,
        )
        self.legacy_flag.start()
        self.addCleanup(self.legacy_flag.stop)
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
        for path in ("/", "/topic-v2", "/subtitle-workflow"):
            response = self.client.get(path)
            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/autocover"', html)
            self.assertIn("自动封面", html)

    def test_legacy_direct_slice_is_hidden_until_explicitly_enabled(self):
        home = self.client.get("/").get_data(as_text=True)
        subtitle = self.client.get("/subtitle-workflow").get_data(as_text=True)

        self.assertIn('id="startButton"', home)
        self.assertNotIn('href="/direct-slice"', home)
        self.assertNotIn('href="/direct-slice"', subtitle)
        self.assertEqual(self.client.get("/direct-slice").status_code, 404)
        self.assertEqual(self.client.post("/api/slice", json={}).status_code, 404)
        self.assertEqual(self.client.post("/api/slice-all", json={}).status_code, 404)

        with patch.dict(
                os.environ,
                {app_module.LEGACY_DIRECT_SLICE_ENV: "1"},
                clear=False):
            advanced = self.client.get("/direct-slice")
            enabled_home = self.client.get("/").get_data(as_text=True)
            enabled_api = self.client.post("/api/slice", json={})

        self.assertEqual(advanced.status_code, 200)
        self.assertIn("JSON", advanced.get_data(as_text=True))
        self.assertIn('href="/direct-slice"', enabled_home)
        self.assertEqual(enabled_api.status_code, 400)

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
            "subtitle_review_version": 4,
            "subtitle_asr_version": 2,
            "autocover_url": "http://localhost:5013",
        })

    def test_asr_status_contract_is_public_and_path_free(self):
        public_status = {
            "model_key": "nano",
            "display_name": "Fun-ASR-Nano-2512",
            "device": "cuda:0",
            "model_ready": True,
            "recommended": True,
            "needs_setup": False,
            "hotword_mode": "native",
            "custom_hotwords": True,
            "summary": "当前使用推荐模型",
            "recommendation": "识别专名不准时调整热词",
            "hotword_hint": "已读取自定义热词",
            "correction_hint": "固定纠错见主播配置",
        }
        with patch("topic_engine.funasr_public_status", return_value=public_status):
            response = self.client.get("/api/asr-status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), public_status)
        serialized = json.dumps(response.get_json(), ensure_ascii=False)
        self.assertNotIn("model_path", serialized)
        self.assertNotIn("model_source", serialized)

    def test_workspace_paths_are_generic_configurable_and_browser_persisted(self):
        with patch.dict(
                os.environ,
                {"AUTOSLICE_VIDEO_DIR": r"D:\Recordings"},
                clear=False):
            configured = app_module._configured_directory(
                "AUTOSLICE_VIDEO_DIR",
                r"C:\Fallback",
            )

        self.assertEqual(configured, r"D:\Recordings")
        for path in ("/", "/topic-v2"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertNotIn("personal-recordings", html)
            self.assertNotIn("private-capture-tool", html)
            self.assertNotIn(r"X:\personal\recordings", html)
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

    def test_subtitle_transcription_defaults_to_foreground_audio(self):
        html, script = self._page_script()

        self.assertIn('id="foregroundOnly" type="checkbox" checked', html)
        self.assertIn("排除背景音", html)
        self.assertIn("foreground_only:foregroundOnly", script)
        self.assertIn("autoslice.subtitle-foreground-only", script)

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
            "deleted:new Set()",
            "function deleteCue(index)",
            "function restoreCue(index)",
            "deleted_indices:removed",
            "cue-delete",
            "reviewDictionary",
            "renderReviewDictionary(data.review_profile)",
            "renderReviewDictionary(result)",
            "renderReferenceTitles();renderReviewDictionary();",
            "replacement_count",
        ):
            self.assertIn(marker, script)
        self.assertIn("重新检查", html)
        self.assertNotIn(
            "data.task_id.startsWith('subtitle_review_'))applyReview(result)",
            script,
        )

    def test_review_page_exposes_transcription_before_review(self):
        html, script = self._page_script()

        self.assertIn('id="transcribeButton"', html)
        self.assertIn('id="reflowButton"', html)
        for marker in (
                "needs_transcription",
                "function startTranscription()",
                "'/api/subtitles/transcribe'",
                "context.kind==='transcribe'",
                "pair.has_source_srt=true",
                "selectPair(state.selectedIndex)",
                "function reflowSubtitles()",
                "'/api/subtitles/reflow'",
                "can_reflow_srt"):
            self.assertIn(marker, script)

        self.assertIn('id="asrGuidance"', html)
        self.assertIn("'/api/asr-status'", script)

    def test_review_page_exposes_adjacent_merge_and_restores_saved_groups(self):
        _html, script = self._page_script()

        for marker in (
                "mergePrevious:new Map()",
                "mergeOverrides:new Map()",
                "function mergeWithPrevious(index)",
                "function mergeWithNext(index)",
                "function unmergeGroup(index)",
                "function restoreCorrectedState(",
                "merge_pairs:pairs",
                "merge_overrides:overrides",
                "合并原文",
                "合上",
                "合下",
                "拆开"):
            self.assertIn(marker, script)

    def test_review_page_exposes_selected_cue_timing_editor(self):
        html, script = self._page_script()

        for marker in (
                "timeOverrides:new Map()",
                "selectedCueIndex:null",
                "function parseSrtTime(",
                "function formatSrtTime(",
                "function timeOverrides(",
                "data-time-start",
                "data-time-end",
                "data-toggle-cue-detail",
                "time_overrides:timings",
                "'/api/subtitles/edit-state'",
                "cue-detail"):
            self.assertIn(marker, script if marker != "cue-detail" else html)
        self.assertNotIn("row.addEventListener('click'", script)

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

    def test_review_page_persists_personalized_style_and_export_settings(self):
        _html, script = self._page_script()

        for marker in (
                "SUBTITLE_STYLE_STORAGE_KEY",
                "SUBTITLE_EXPORT_STORAGE_KEY",
                "function loadStoredObject(key)",
                "function storedStyleOverrides()",
                "function storedExportOverrides()",
                "function persistSubtitleSettings()",
                "localStorage.setItem(SUBTITLE_STYLE_STORAGE_KEY",
                "localStorage.setItem(SUBTITLE_EXPORT_STORAGE_KEY",
                "...storedStyleOverrides()",
                "...storedExportOverrides()",
                "persistSubtitleSettings();",
                "window.addEventListener('beforeunload',persistSubtitleSettings)",
                "['fontSize','outlineWidth','positionX','positionY','bitrate','fps']"):
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

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查字幕合并状态")
    def test_review_page_restores_saved_merged_timeline(self):
        _, script = self._page_script()
        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        runtime_assertions = r"""
state.sourceCues=[
  {index:1,start:'00:00:00,000',end:'00:00:01,000',settings:'',text:'第一句'},
  {index:2,start:'00:00:01,000',end:'00:00:02,000',settings:'',text:'第二句'},
  {index:3,start:'00:00:02,000',end:'00:00:03,000',settings:'',text:'第三句'}
];
state.edited=new Map(state.sourceCues.map(cue=>[cue.index,cue.text]));
const restored=restoreCorrectedState(state.sourceCues,[
  {index:1,start:'00:00:00,000',end:'00:00:03,000',settings:'',text:'完整合并句子'}
]);
if(!restored)throw new Error('合并字幕恢复失败');
if(state.mergePrevious.get(2)!==1||state.mergePrevious.get(3)!==2)throw new Error('链式合并关系错误');
if(mergedDisplayText(1)!=='完整合并句子')throw new Error('合并正文恢复错误');
const pairs=JSON.stringify(mergePairs());
if(pairs!==JSON.stringify([{first:1,second:2},{first:2,second:3}]))throw new Error('保存关系错误');
"""
        result = subprocess.run(
            [
                "node",
                "-e",
                "globalThis.localStorage={getItem:()=>null,setItem:()=>{}};"
                "new Function(require('fs').readFileSync(0,'utf8'))();",
            ],
            input=script_prefix + runtime_assertions,
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


class DirectSliceApiTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        app_module.tasks.clear()
        self.legacy_flag = patch.dict(
            os.environ,
            {app_module.LEGACY_DIRECT_SLICE_ENV: "1"},
            clear=False,
        )
        self.legacy_flag.start()
        self.addCleanup(self.legacy_flag.stop)
        self.client = app_module.app.test_client()

    def test_json_timeline_reslice_uses_existing_slice_from_marks_path(self):
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

    def test_non_json_modes_are_rejected_before_reservation_or_thread_start(self):
        migration_text = "请先运行智能分析生成 clip_marks.json"
        with (
            patch.object(app_module, "_reserve_source_task") as reserve,
            patch.object(app_module.threading, "Thread") as thread,
        ):
            responses = [
                self.client.post("/api/slice", json={"mode": mode})
                for mode in ("danmaku", "timeline", "hybrid", "")
            ]

        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertIn(migration_text, response.get_json()["error"])
        reserve.assert_not_called()
        thread.assert_not_called()
        self.assertEqual(app_module.tasks, {})

    def test_json_reslice_blocks_different_sources_targeting_same_output(self):
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


class TopicPipelineApiTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        app_module.tasks.clear()
        self.legacy_flag = patch.dict(
            os.environ,
            {app_module.LEGACY_DIRECT_SLICE_ENV: "1"},
            clear=False,
        )
        self.legacy_flag.start()
        self.addCleanup(self.legacy_flag.stop)
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
                "flv_path": r"X:\fixtures\missing\录播.flv",
                "manual_timeline_path": r"X:\fixtures\missing\时间轴.docx",
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
        assert_same_path(
            self,
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
        assert_same_path(
            self,
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
            assert_same_path(
                self,
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
        self.assertIn('id="asrStatus"', html)
        self.assertIn("'/api/asr-status'", html)
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
                        "token=test-private-value 位于 X:\\fixtures\\api_config.json"
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
        self.assertNotIn(r"X:\fixtures", task["result"])
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
        assert_same_path(self, json_path.parent, json_dir)
        assert_same_path(self, docx_path.parent, docx_dir)


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
            json={"root_dir": r"X:\fixtures\missing\投稿"},
        )
        self.assertEqual(missing.status_code, 400)

    def test_scan_and_transcribe_video_without_subtitle(self):
        with TemporaryDirectory() as td:
            folder = Path(td) / "精剪投稿"
            folder.mkdir()
            video = folder / "最终成片.mp4"
            video.write_bytes(b"video")
            scan = self.client.post("/api/subtitles/scan", json={"root_dir": td})

            def fake_transcribe(
                    video_path, progress_callback=None, foreground_only=True):
                srt = Path(video_path).with_suffix(".srt")
                srt.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\n音音测试\n",
                    encoding="utf-8",
                )
                if progress_callback:
                    progress_callback("转录完成 (1 条)", 90, 100)
                return {
                    "video_path": str(Path(video_path).resolve()),
                    "srt_path": str(srt.resolve()),
                    "cue_count": 1,
                    "background_filter": {
                        "enabled": foreground_only,
                        "mode": "speaker_diarization",
                        "speaker_filtered_segment_count": 2,
                        "speaker_filtered_chunk_count": 1,
                    },
                }

            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "subtitle_workflow.transcribe_submission_video",
                    side_effect=fake_transcribe,
                ) as transcribe,
            ):
                response = self.client.post(
                    "/api/subtitles/transcribe",
                    json={
                        "video_path": str(video),
                        "foreground_only": True,
                    },
                )
            rescanned = self.client.post(
                "/api/subtitles/scan",
                json={"root_dir": td},
            )
            task_id = response.get_json()["task_id"]
            task = app_module.tasks[task_id]
            result = json.loads(task["result"])
            generated_srt_exists = Path(result["srt_path"]).is_file()

        self.assertEqual(scan.status_code, 200)
        self.assertEqual(scan.get_json()["count"], 1)
        self.assertTrue(scan.get_json()["pairs"][0]["needs_transcription"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["task_type"], "subtitle_transcription")
        self.assertEqual(result["cue_count"], 1)
        self.assertEqual(
            result["background_filter"]["mode"],
            "speaker_diarization",
        )
        self.assertTrue(generated_srt_exists)
        transcribe.assert_called_once()
        self.assertTrue(transcribe.call_args.kwargs["foreground_only"])
        self.assertTrue(rescanned.get_json()["pairs"][0]["has_source_srt"])

    def test_transcribe_rejects_non_boolean_foreground_filter(self):
        with TemporaryDirectory() as td:
            video = Path(td) / "最终成片.mp4"
            video.write_bytes(b"video")

            response = self.client.post(
                "/api/subtitles/transcribe",
                json={
                    "video_path": str(video),
                    "foreground_only": "yes",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("foreground_only", response.get_json()["error"])

    def test_reflow_api_preserves_source_and_scanner_prefers_layout_copy(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            source_text = "这是旧字幕中需要整理为短句的超长文字" * 4
            srt.write_text(
                f"1\n00:00:00,000 --> 00:00:06,000\n{source_text}\n",
                encoding="utf-8",
            )
            source_before = srt.read_bytes()

            response = self.client.post(
                "/api/subtitles/reflow",
                json={"video_path": str(video), "srt_path": str(srt)},
            )
            payload = response.get_json()
            reflowed_path = Path(payload["srt_path"])
            reflowed_cues = parse_srt_document(reflowed_path)
            rescanned = self.client.post(
                "/api/subtitles/scan",
                json={"root_dir": td},
            ).get_json()["pairs"][0]
            source_after = srt.read_bytes()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(source_after, source_before)
        self.assertTrue(reflowed_path.name.endswith("_排版.srt"))
        self.assertGreater(payload["split_count"], 0)
        self.assertTrue(all(
            len("".join(cue.text.split())) <= payload["max_chars"]
            for cue in reflowed_cues
        ))
        assert_same_path(self, rescanned["srt_path"], reflowed_path)
        self.assertTrue(rescanned["is_reflowed_srt"])
        self.assertTrue(rescanned["can_reflow_srt"])

    def test_duplicate_subtitle_transcription_is_rejected(self):
        with TemporaryDirectory() as td:
            folder = Path(td) / "精剪投稿"
            folder.mkdir()
            video = folder / "最终成片.mp4"
            video.write_bytes(b"video")
            with patch.object(app_module.threading, "Thread", DeferredThread):
                first = self.client.post(
                    "/api/subtitles/transcribe",
                    json={"video_path": str(video)},
                )
                duplicate = self.client.post(
                    "/api/subtitles/transcribe",
                    json={"video_path": str(video)},
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.get_json()["task_id"],
            first.get_json()["task_id"],
        )

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

    def test_save_can_delete_subtitles_and_rejects_invalid_deletion_payloads(self):
        with TemporaryDirectory() as td:
            _, srt = self._write_pair(td)
            srt.write_text(
                srt.read_text(encoding="utf-8")
                + "\n2\n00:00:01,000 --> 00:00:02,000\n误识别字幕\n",
                encoding="utf-8",
            )
            original = srt.read_bytes()
            invalid_payload = self.client.post(
                "/api/subtitles/save",
                json={
                    "srt_path": str(srt),
                    "corrections": [],
                    "deleted_indices": "1",
                },
            )
            invalid_index = self.client.post(
                "/api/subtitles/save",
                json={
                    "srt_path": str(srt),
                    "corrections": [],
                    "deleted_indices": [99],
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
                    "deleted_indices": [2],
                },
            )
            corrected_cues = parse_srt_document(
                saved.get_json()["corrected_srt_path"]
            )
            source_after = srt.read_bytes()

        self.assertEqual(invalid_payload.status_code, 400)
        self.assertIn("必须是数组", invalid_payload.get_json()["error"])
        self.assertEqual(invalid_index.status_code, 400)
        self.assertIn("序号不存在", invalid_index.get_json()["error"])
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["correction_count"], 1)
        self.assertEqual(saved.get_json()["deletion_count"], 1)
        self.assertEqual(saved.get_json()["cue_count"], 1)
        self.assertEqual([cue.index for cue in corrected_cues], [1])
        self.assertEqual(corrected_cues[0].text, "娃衣")
        self.assertEqual(source_after, original)

    def test_save_merges_adjacent_subtitles_and_returns_merge_count(self):
        with TemporaryDirectory() as td:
            _, srt = self._write_pair(td)
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\n第二句\n\n"
                "3\n00:00:02,000 --> 00:00:03,000\n第三句\n",
                encoding="utf-8",
            )
            source_before = srt.read_bytes()
            response = self.client.post(
                "/api/subtitles/save",
                json={
                    "srt_path": str(srt),
                    "corrections": [],
                    "merge_pairs": [
                        {"first": 1, "second": 2},
                        {"first": 2, "second": 3},
                    ],
                    "merge_overrides": {"1": "完整合并句子"},
                },
            )
            output = Path(response.get_json()["corrected_srt_path"])
            merged = parse_srt_document(output)
            source_after = srt.read_bytes()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["merge_count"], 2)
        self.assertEqual(response.get_json()["cue_count"], 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].end, "00:00:03,000")
        self.assertEqual(merged[0].text, "完整合并句子")
        self.assertEqual(source_after, source_before)

    def test_save_adjusts_timing_and_exposes_persistent_edit_state(self):
        with TemporaryDirectory() as td:
            _, srt = self._write_pair(td)
            response = self.client.post(
                "/api/subtitles/save",
                json={
                    "srt_path": str(srt),
                    "corrections": [],
                    "time_overrides": {
                        "1": {"start": 0.2, "end": 0.9},
                    },
                },
            )
            state_response = self.client.post(
                "/api/subtitles/edit-state",
                json={"srt_path": str(srt)},
            )
            corrected = parse_srt_document(
                response.get_json()["corrected_srt_path"]
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["timing_count"], 1)
        self.assertEqual(corrected[0].start, "00:00:00,200")
        self.assertEqual(corrected[0].end, "00:00:00,900")
        self.assertEqual(state_response.status_code, 200)
        self.assertTrue(state_response.get_json()["available"])
        self.assertEqual(
            state_response.get_json()["edit_state"]["time_overrides"],
            {"1": {"start": 0.2, "end": 0.9}},
        )

        invalid = self.client.post(
            "/api/subtitles/save",
            json={
                "srt_path": str(srt),
                "corrections": [],
                "time_overrides": "bad",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("必须是对象", invalid.get_json()["error"])

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
        self.assertEqual(review.call_args.kwargs["streamer_profile_id"], "zeyin")
        self.assertEqual(review.call_args.kwargs["streamer_profile_label"], "泽音 Melody")
        self.assertIn("朱鹮", review.call_args.kwargs["glossary"])
        self.assertIn(("英英", "音音"), review.call_args.kwargs["replacements"])
        profile = response.get_json()["review_profile"]
        self.assertEqual(profile["id"], "zeyin")
        self.assertGreaterEqual(profile["glossary_count"], 40)
        self.assertEqual(profile["replacement_count"], 11)
        self.assertEqual(app_module.tasks[task_id]["task_type"], "subtitle_review")
        assert_same_path(
            self,
            app_module.tasks[task_id]["source_srt_path"],
            str(srt.resolve()),
        )
        self.assertFalse(app_module.tasks[task_id]["force"])

    def test_review_unknown_streamer_uses_generic_dictionary_without_zeyin_mapping(self):
        with TemporaryDirectory() as td:
            folder = Path(td) / "普通投稿"
            folder.mkdir()
            video = folder / "剪映导出.mp4"
            srt = folder / "剪映字幕.srt"
            video.write_bytes(b"video")
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n英英晚上好\n",
                encoding="utf-8",
            )
            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "subtitle_workflow.suggest_subtitle_corrections",
                    return_value={"suggestions": []},
                ) as review,
            ):
                response = self.client.post(
                    "/api/subtitles/review",
                    json={"video_path": str(video), "srt_path": str(srt)},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["review_profile"]["id"], "generic")
        self.assertEqual(review.call_args.kwargs["streamer_profile_id"], "generic")
        self.assertEqual(review.call_args.kwargs["replacements"], ())

    def test_reference_title_runs_in_background_with_corrected_subtitle(self):
        with TemporaryDirectory() as td:
            video, source_srt = self._write_pair(td)
            corrected_srt = source_srt.with_name(
                f"{source_srt.stem}_校对字幕.srt"
            )
            corrected_srt.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n娃衣\n",
                encoding="utf-8",
            )
            title_result = {
                "source_srt_path": str(corrected_srt.resolve()),
                "recommended_title": "【泽音】娃衣看着很正常，转身却露出最大问题😂",
                "candidates": [
                    "【泽音】娃衣看着很正常，转身却露出最大问题😂",
                    "【泽音】第一眼还是娃衣，换个角度直接看懵了",
                    "【泽音】音音认真看娃衣，最后被一个细节整沉默",
                ],
                "reason": "保留视觉反差",
            }
            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "subtitle_workflow.generate_subtitle_reference_titles",
                    return_value=title_result,
                ) as generate,
            ):
                response = self.client.post(
                    "/api/subtitles/generate-title",
                    json={
                        "video_path": str(video),
                        "srt_path": str(corrected_srt),
                        "context_title": "【泽音】测试投稿",
                    },
                )

        self.assertEqual(response.status_code, 200)
        task_id = response.get_json()["task_id"]
        task = app_module.tasks[task_id]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["task_type"], "subtitle_title")
        self.assertEqual(
            json.loads(task["result"])["recommended_title"],
            title_result["recommended_title"],
        )
        assert_same_path(
            self,
            generate.call_args.args[0],
            corrected_srt.resolve(),
        )
        self.assertEqual(
            generate.call_args.kwargs["context_title"],
            "【泽音】测试投稿",
        )

    def test_duplicate_reference_title_task_is_rejected(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            with patch.object(app_module.threading, "Thread", DeferredThread):
                first = self.client.post(
                    "/api/subtitles/generate-title",
                    json={"video_path": str(video), "srt_path": str(srt)},
                )
                duplicate = self.client.post(
                    "/api/subtitles/generate-title",
                    json={"video_path": str(video), "srt_path": str(srt)},
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.get_json()["task_id"],
            first.get_json()["task_id"],
        )

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
