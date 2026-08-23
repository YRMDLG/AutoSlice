import atexit
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_TEST_TASK_DATABASE_DIR = TemporaryDirectory(prefix="autoslice-test-app-tasks-")
_PREVIOUS_TASK_DATABASE = os.environ.get("AUTOSLICE_TASK_DB")
os.environ["AUTOSLICE_TASK_DB"] = str(
    Path(_TEST_TASK_DATABASE_DIR.name) / "tasks.sqlite3"
)

import autoslice.web.app as app_module
from autoslice.subtitle_workflow import parse_srt_document
from autoslice.task_registry import TaskRegistry
from autoslice.task_store import TaskStore


def _cleanup_test_task_database():
    app_module.tasks.clear()
    if _PREVIOUS_TASK_DATABASE is None:
        os.environ.pop("AUTOSLICE_TASK_DB", None)
    else:
        os.environ["AUTOSLICE_TASK_DB"] = _PREVIOUS_TASK_DATABASE
    _TEST_TASK_DATABASE_DIR.cleanup()


atexit.register(_cleanup_test_task_database)


def _bootstrapped_client():
    """模拟浏览器先加载会话端点，再调用既有写 API。"""

    client = app_module.app.test_client()
    response = client.get("/api/security/session")
    if response.status_code != 200:
        raise RuntimeError("测试客户端无法建立本机会话")
    return client


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


class ResourcePathTests(unittest.TestCase):
    def test_templates_and_static_files_do_not_depend_on_current_directory(self):
        previous = Path.cwd()
        with TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                client = app_module.app.test_client()
                page = client.get("/")
                stylesheet = client.get("/static/workbench.css")
                subtitle_script = client.get("/static/subtitle_workflow.js")
            finally:
                os.chdir(previous)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(stylesheet.status_code, 200)
        self.assertEqual(subtitle_script.status_code, 200)
        page.close()
        stylesheet.close()
        subtitle_script.close()

    def test_subtitle_template_has_one_owner_per_top_level_function(self):
        project_root = Path(__file__).resolve().parents[2]
        template_path = (
            project_root
            / "src"
            / "autoslice"
            / "resources"
            / "templates"
            / "subtitle_workflow.html"
        )
        script_path = (
            project_root
            / "src"
            / "autoslice"
            / "resources"
            / "static"
            / "subtitle_workflow.js"
        )
        template = template_path.read_text(encoding="utf-8")
        script = script_path.read_text(encoding="utf-8")
        self.assertNotIn("<script>", template)
        expected_script_tag = (
            '<script src="{{ url_for(\'static\', filename=\'subtitle_workflow.js\') }}" '
            "defer></script>"
        )
        self.assertIn(expected_script_tag, template)
        function_names = re.findall(
            r"(?m)^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
            script,
        )
        self.assertEqual(
            len(function_names),
            len(set(function_names)),
            "字幕工作台模板不得为同一顶层函数保留多个实现",
        )
        for name in (
            "selectPair",
            "renderCues",
            "deleteCue",
            "saveCorrections",
            "corrections",
        ):
            self.assertEqual(function_names.count(name), 1, name)


class ScanApiTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        self.client = _bootstrapped_client()

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
        self.client = _bootstrapped_client()

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

    def test_topic_page_fetches_full_result_by_task_id_after_sse_sanitization(self):
        _html, script = self._page_script()

        self.assertIn("async function loadTaskResult(taskId)", script)
        self.assertIn(
            "/api/tasks/${encodeURIComponent(taskId)}/result",
            script,
        )
        self.assertIn("const result=await loadTaskResult(data.task_id)", script)
        self.assertNotIn("const result=JSON.parse(data.result)", script)

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


class SecurityBoundaryTests(unittest.TestCase):
    """覆盖 Host、同源写证明、内存会话和显式 LAN 路径边界。"""

    STRONG_LAN_TOKEN = "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r0"

    def setUp(self):
        app_module.app.config.update(TESTING=True)

    @classmethod
    def _lan_environment(cls, allowed_root, **overrides):
        environment = {
            "AUTOSLICE_LAN_MODE": "1",
            "AUTOSLICE_LAN_TOKEN": cls.STRONG_LAN_TOKEN,
            "AUTOSLICE_LAN_HOSTS": "192.168.1.20",
            "AUTOSLICE_LAN_ORIGINS": "http://192.168.1.20:5002",
            "AUTOSLICE_ALLOWED_ROOTS": str(allowed_root),
        }
        environment.update(overrides)
        return environment

    def test_default_mode_accepts_only_all_three_loopback_host_forms(self):
        client = app_module.app.test_client()

        for host in ("localhost:5002", "127.0.0.1:5002", "[::1]:5002"):
            with self.subTest(host=host):
                response = client.get("/api/service", headers={"Host": host})
                self.assertEqual(response.status_code, 200)

        for host in (
            "attacker.example",
            "127.0.0.1.attacker.example",
            "0.0.0.0:5002",
        ):
            with self.subTest(host=host):
                response = client.get("/api/service", headers={"Host": host})
                self.assertEqual(response.status_code, 403)
                self.assertIn("Host", response.get_json()["error"])

    def test_same_origin_origin_or_referer_allows_write_and_cross_site_is_rejected(self):
        with TemporaryDirectory() as directory:
            by_origin = app_module.app.test_client().post(
                "/api/scan",
                json={"video_dir": directory},
                headers={
                    "Host": "127.0.0.1:5002",
                    "Origin": "http://127.0.0.1:5002",
                },
            )
            by_referer = app_module.app.test_client().post(
                "/api/scan",
                json={"video_dir": directory},
                headers={
                    "Host": "[::1]:5002",
                    "Referer": "http://[::1]:5002/topic-v2",
                },
            )
            cross_site_client = app_module.app.test_client()
            cross_site_client.get("/api/security/session")
            cross_site = cross_site_client.post(
                "/api/scan",
                json={"video_dir": directory},
                headers={"Origin": "https://attacker.example"},
            )
            cross_port = app_module.app.test_client().post(
                "/api/scan",
                json={"video_dir": directory},
                headers={
                    "Host": "localhost:5002",
                    "Origin": "http://localhost:5010",
                },
            )

        self.assertEqual(by_origin.status_code, 200)
        self.assertEqual(by_referer.status_code, 200)
        self.assertEqual(cross_site.status_code, 403)
        self.assertEqual(cross_port.status_code, 403)

    def test_missing_origin_and_referer_requires_non_url_session_token(self):
        with TemporaryDirectory() as directory:
            fresh_client = app_module.app.test_client()
            rejected = fresh_client.post(
                "/api/scan",
                json={"video_dir": directory},
            )
            query_token = fresh_client.post(
                "/api/scan",
                query_string={"token": self.STRONG_LAN_TOKEN},
                json={"video_dir": directory},
            )

            browser_client = app_module.app.test_client()
            bootstrap = browser_client.get("/api/security/session")
            accepted = browser_client.post(
                "/api/scan",
                json={"video_dir": directory},
            )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(query_token.status_code, 403)
        self.assertEqual(bootstrap.get_json(), {"ok": True, "mode": "local"})
        self.assertEqual(bootstrap.headers["Cache-Control"], "no-store")
        cookie = bootstrap.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn(self.STRONG_LAN_TOKEN, cookie)
        self.assertNotIn("token", bootstrap.get_data(as_text=True).casefold())
        self.assertEqual(accepted.status_code, 200)

    def test_lan_mode_rejects_weak_or_incomplete_security_configuration(self):
        with TemporaryDirectory() as allowed_dir:
            complete = self._lan_environment(allowed_dir)
            weak = {**complete, "AUTOSLICE_LAN_TOKEN": "x" * 64}
            missing_origins = {**complete, "AUTOSLICE_LAN_ORIGINS": ""}
            headers = {
                "Host": "192.168.1.20:5002",
                "X-AutoSlice-Token": self.STRONG_LAN_TOKEN,
            }
            for environment in (weak, missing_origins):
                with self.subTest(environment=environment):
                    with patch.dict(os.environ, environment, clear=False):
                        response = app_module.app.test_client().get(
                            "/api/service",
                            headers=headers,
                        )
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn(
                        environment["AUTOSLICE_LAN_TOKEN"],
                        response.get_data(as_text=True),
                    )

    def test_lan_mode_requires_header_or_session_and_explicit_origin(self):
        with TemporaryDirectory() as allowed_dir:
            environment = self._lan_environment(
                allowed_dir,
                AUTOSLICE_LAN_HOSTS="192.168.1.20;192.168.1.21",
            )
            with patch.dict(os.environ, environment, clear=False):
                unauthenticated = app_module.app.test_client().get(
                    "/api/service",
                    headers={"Host": "192.168.1.20:5002"},
                )
                url_token = app_module.app.test_client().get(
                    "/api/service",
                    query_string={"token": self.STRONG_LAN_TOKEN},
                    headers={"Host": "192.168.1.20:5002"},
                )
                cross_site = app_module.app.test_client().post(
                    "/api/subtitles/scan",
                    json={"root_dir": allowed_dir},
                    headers={
                        "Host": "192.168.1.21:5002",
                        "Origin": "http://192.168.1.21:5002",
                        "X-AutoSlice-Token": self.STRONG_LAN_TOKEN,
                    },
                )

                browser_client = app_module.app.test_client()
                bootstrap = browser_client.get(
                    "/api/security/session",
                    headers={
                        "Host": "192.168.1.20:5002",
                        "X-AutoSlice-Token": self.STRONG_LAN_TOKEN,
                    },
                )
                session_write = browser_client.post(
                    "/api/subtitles/scan",
                    json={"root_dir": allowed_dir},
                    headers={"Host": "192.168.1.20:5002"},
                )

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(url_token.status_code, 401)
        self.assertEqual(cross_site.status_code, 403)
        self.assertEqual(bootstrap.status_code, 200)
        self.assertNotIn(
            self.STRONG_LAN_TOKEN,
            bootstrap.headers.get("Set-Cookie", ""),
        )
        self.assertEqual(session_write.status_code, 200)

    def test_lan_paths_cover_json_form_query_and_upload_inputs(self):
        with TemporaryDirectory() as allowed_dir, TemporaryDirectory() as blocked_dir:
            allowed_root = Path(allowed_dir)
            blocked_root = Path(blocked_dir)
            environment = self._lan_environment(allowed_root)
            read_headers = {
                "Host": "192.168.1.20:5002",
                "X-AutoSlice-Token": self.STRONG_LAN_TOKEN,
            }
            write_headers = {
                **read_headers,
                "Origin": "http://192.168.1.20:5002",
            }
            with patch.dict(os.environ, environment, clear=False):
                client = app_module.app.test_client()
                allowed_json = client.post(
                    "/api/subtitles/scan",
                    json={"root_dir": str(allowed_root)},
                    headers=write_headers,
                )
                blocked_json = client.post(
                    "/api/subtitles/scan",
                    json={"nested": {"root_dir": str(blocked_root)}},
                    headers=write_headers,
                )
                allowed_query = client.get(
                    "/api/service",
                    query_string={"output_path": str(allowed_root / "result.json")},
                    headers=read_headers,
                )
                blocked_query = client.get(
                    "/api/service",
                    query_string={"output_path": str(blocked_root / "result.json")},
                    headers=read_headers,
                )
                allowed_form = client.post(
                    "/api/service",
                    data={"output_dir": str(allowed_root)},
                    headers=write_headers,
                )
                blocked_form = client.post(
                    "/api/service",
                    data={"output_dir": str(blocked_root)},
                    headers=write_headers,
                )
                blocked_upload = client.post(
                    "/api/not-found",
                    data={"file": (io.BytesIO(b"docx"), "../escape.docx")},
                    headers=write_headers,
                )
                safe_upload = client.post(
                    "/api/not-found",
                    data={
                        "target_path": str(allowed_root / "safe.docx"),
                        "file": (io.BytesIO(b"docx"), "safe.docx"),
                    },
                    headers=write_headers,
                )

        self.assertEqual(allowed_json.status_code, 200)
        self.assertEqual(blocked_json.status_code, 403)
        self.assertEqual(allowed_query.status_code, 200)
        self.assertEqual(blocked_query.status_code, 403)
        self.assertEqual(allowed_form.status_code, 405)
        self.assertEqual(blocked_form.status_code, 403)
        self.assertEqual(blocked_upload.status_code, 403)
        self.assertEqual(safe_upload.status_code, 404)


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
        self.client = _bootstrapped_client()

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
            "subtitle_review_version": 5,
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
        with patch("autoslice.topic_engine.funasr_public_status", return_value=public_status):
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
        self.client = _bootstrapped_client()

    def _page_script(self):
        response = self.client.get("/subtitle-workflow")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(
            '<script src="/static/subtitle_workflow.js" defer></script>',
            html,
        )
        with self.client.get("/static/subtitle_workflow.js") as script_response:
            self.assertEqual(script_response.status_code, 200)
            self.assertIn("javascript", script_response.mimetype)
            script = script_response.get_data(as_text=True)
        return html, script

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
            "streamer_profile_id",
        ):
            self.assertIn(marker, script)
        self.assertIn("重新检查", html)
        self.assertNotIn(
            "data.task_id.startsWith('subtitle_review_'))applyReview(result)",
            script,
        )

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查批量建议交互")
    def test_review_batch_suggestions_respect_filter_protection_and_undo(self):
        html, script = self._page_script()
        for marker in (
                'id="acceptSuggestionsButton"',
                'id="ignoreSuggestionsButton"',
                'id="undoSuggestionButton"',
                'id="suggestionSummary"'):
            self.assertIn(marker, html)
        for marker in (
                "function visibleSuggestionEntries()",
                "function suggestionGroups(entries)",
                "function acceptVisibleSuggestions()",
                "function ignoreVisibleSuggestions()",
                "function undoSuggestionAction()",
                "singleCharacterReplacement",
                "state.protectedEdits"):
            self.assertIn(marker, script)
        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        runtime_assertions = r'''
globalThis.window={confirm:()=>true};
const emptyNode={querySelectorAll:()=>[],querySelector:()=>({textContent:''}),
  classList:{toggle:()=>{}},style:{},setAttribute:()=>{}};
globalThis.document={
  getElementById:()=>({...emptyNode}),
  querySelectorAll:()=>[],
};
state.sourceCues=[
  {index:1,text:'原文一',start_seconds:0,end_seconds:1,start:'00:00:00,000',end:'00:00:01,000'},
  {index:2,text:'原文一',start_seconds:1,end_seconds:2,start:'00:00:01,000',end:'00:00:02,000'},
  {index:3,text:'原文三',start_seconds:2,end_seconds:3,start:'00:00:02,000',end:'00:00:03,000'},
  {index:4,text:'没有建议',start_seconds:3,end_seconds:4,start:'00:00:03,000',end:'00:00:04,000'},
];
state.edited=new Map(state.sourceCues.map(cue=>[cue.index,cue.text]));
state.suggestions=new Map([
  [1,{index:1,original:'原文一',corrected:'校正结果',reason:'上下文确认',confidence:.99}],
  [2,{index:2,original:'原文一',corrected:'校正结果',reason:'上下文确认',confidence:.91}],
  [3,{index:3,original:'原文三',corrected:'另一结果',reason:'固定词',confidence:.88}],
]);
state.protectedEdits.add(3);
state.edited.set(3,'用户手工修改');
state.filter='suggested';
if(visibleSuggestionEntries().length!==3)throw new Error('当前筛选没有完整收集建议');
if(suggestionGroups(visibleSuggestionEntries()).length!==2)throw new Error('相同建议没有合并展示');
acceptVisibleSuggestions();
if(state.edited.get(1)!=='校正结果'||state.edited.get(2)!=='校正结果')throw new Error('批量采纳未应用到未保护建议');
if(state.edited.get(3)!=='用户手工修改')throw new Error('批量采纳覆盖了手工修改');
undoSuggestionAction();
if(state.edited.get(1)!=='原文一'||state.edited.get(2)!=='原文一')throw new Error('采纳撤销未恢复原文');
ignoreVisibleSuggestions();
if(state.ignoredSuggestions.size!==3)throw new Error('批量忽略没有作用于当前筛选');
if(state.edited.get(3)!=='用户手工修改')throw new Error('忽略操作覆盖了手工修改');
undoSuggestionAction();
if(state.ignoredSuggestions.size!==0||state.edited.get(1)!=='原文一')throw new Error('忽略撤销未恢复建议状态');
acceptVisibleSuggestions();
ignoreVisibleSuggestions();
if(state.edited.get(1)!=='原文一'||state.edited.get(2)!=='原文一')throw new Error('忽略没有撤回此前自动采纳的建议');
if(state.edited.get(3)!=='用户手工修改')throw new Error('忽略覆盖了手工修改');
'''
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

    def test_subtitle_tasks_fetch_full_result_after_sse_sanitization(self):
        _html, script = self._page_script()

        self.assertIn("async function loadTaskResult(taskId)", script)
        self.assertIn(
            "/api/tasks/${encodeURIComponent(taskId)}/result",
            script,
        )
        self.assertIn("const result=await loadTaskResult(data.task_id)", script)
        self.assertIn("async function handleTask(data)", script)

    def test_review_page_exposes_transcription_before_review(self):
        html, script = self._page_script()

        self.assertIn('id="transcribeButton"', html)
        self.assertIn('id="reflowButton"', html)
        for marker in (
                "needs_transcription",
                "function startTranscription()",
                "'/api/subtitles/transcribe'",
                "context.kind==='transcribe'",
                "subtitle_pair_id",
                "function restoreTaskContext(data)",
                "function reapplyKnownTaskStates()",
                "transcription_status",
                "pair.has_source_srt=true",
                "selectPair(state.selectedIndex)",
                "function reflowSubtitles()",
                "'/api/subtitles/reflow'",
                "can_reflow_srt"):
            self.assertIn(marker, script)

        self.assertIn('id="asrGuidance"', html)
        self.assertIn("'/api/asr-status'", script)
        self.assertIn("await scan();connectSSE();", script)

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
        self.client = _bootstrapped_client()

    def test_explicit_compatibility_page_only_offers_json_reslicing(self):
        response = self.client.get("/direct-slice")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("JSON 标记重新切片", html)
        self.assertNotIn("人工时间轴 DOCX 重切", html)
        self.assertNotIn("弹幕密度直切", html)
        self.assertNotIn("时间轴 + 密度", html)

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
                    "autoslice.topic_engine.slice_from_marks",
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
        self.client = _bootstrapped_client()

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

    def test_update_task_stores_decoded_json_summary_without_double_encoding(self):
        app_module.update_task(
            "decoded_result",
            status="done",
            result=json.dumps({"topic_count": 2}, ensure_ascii=False),
        )

        task = app_module.tasks["decoded_result"]
        self.assertEqual(task["result_summary"], {"topic_count": 2})
        self.assertEqual(json.loads(task["result"]), {"topic_count": 2})

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
                    "autoslice.topic_engine.optimize_manual_timeline_for_video",
                    return_value=expected,
                ) as optimize,
                patch(
                    "autoslice.topic_engine.run_pipeline",
                    side_effect=AssertionError("独立优化不应运行完整分析"),
                ),
                patch(
                    "autoslice.topic_engine.slice_from_marks",
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
                patch("autoslice.topic_engine.run_pipeline", return_value=pipeline_result) as run_pipeline,
                patch(
                    "autoslice.topic_engine.slice_from_marks",
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

    def test_start_pipeline_persists_compact_summary_for_large_result(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            flv_path = root / "直播.flv"
            output_dir = root / "自动切片"
            artifact_dir = output_dir / "直播_自动切片"
            slice_dir = output_dir / "直播_话题切片"
            flv_path.write_bytes(b"video")
            pipeline_result = {
                "report": "完整报告" * 50_000,
                "topic_count": 58,
                "clip_marks": [
                    {"start": index, "end": index + 30, "subtitle": "字幕" * 500}
                    for index in range(12)
                ],
                "analysis_topics": [{"body": "分析" * 30_000}],
                "json_path": str(artifact_dir / "数据" / "clip_marks.json"),
                "md_path": str(artifact_dir / "01_话题分析.md"),
                "artifact_dir": str(artifact_dir),
                "overview_path": str(artifact_dir / "00_概览.md"),
            }

            with (
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "autoslice.topic_engine.run_pipeline",
                    return_value=pipeline_result,
                ),
                patch(
                    "autoslice.topic_engine.slice_from_marks",
                    return_value=(12, str(slice_dir)),
                ),
            ):
                response = self.client.post(
                    "/api/start-pipeline",
                    json={"flv_path": str(flv_path), "output_dir": str(output_dir)},
                )

        self.assertEqual(response.status_code, 200)
        task = app_module.tasks[response.get_json()["task_id"]]
        summary = task["result_summary"]
        self.assertEqual(task["status"], "done")
        self.assertIsInstance(summary, dict)
        self.assertEqual(summary["topic_count"], 58)
        self.assertEqual(summary["slice_count"], 12)
        self.assertEqual(summary["artifact_dir"], str(artifact_dir))
        self.assertEqual(summary["slice_dir"], str(slice_dir))
        self.assertNotIn("report", summary)
        self.assertNotIn("clip_marks", summary)
        self.assertNotIn("analysis_topics", summary)
        self.assertLess(
            len(json.dumps(summary, ensure_ascii=False).encode("utf-8")),
            64 * 1024,
        )
        self.assertEqual(task["artifact_dir"], str(artifact_dir))
        assert_same_path(self, task["output_dir"], output_dir)

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
                    "autoslice.topic_engine.retry_clip_review_from_artifacts",
                    return_value=result,
                ) as retry,
                patch(
                    "autoslice.topic_engine.slice_from_marks",
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
                    "autoslice.topic_engine.slice_from_marks",
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
        from autoslice.core import parse_timeline_json

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
        from autoslice.core import generate_srt

        progress = []
        with patch(
            "autoslice.topic_engine.ensure_srt",
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
                patch("autoslice.topic_engine.run_pipeline", return_value=pipeline_result),
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
                    "autoslice.topic_engine.run_pipeline",
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


class TaskLifecycleTests(unittest.TestCase):

    def setUp(self):
        self.original_store = app_module.task_store
        self.original_registry = app_module.task_registry
        self.database_dir = TemporaryDirectory(prefix="autoslice-lifecycle-")
        self.database_path = Path(self.database_dir.name) / "tasks.sqlite3"
        self._bind_registry(
            TaskRegistry(
                TaskStore(self.database_path),
                recover_on_startup=False,
            )
        )
        app_module.app.config.update(TESTING=True)
        app_module.tasks.clear()
        with app_module.event_queue_lock:
            app_module.event_queues.clear()
            app_module._event_history.clear()
            app_module._event_sequence = 0
        self.client = _bootstrapped_client()

    def tearDown(self):
        app_module.tasks.clear()
        with app_module.event_queue_lock:
            app_module.event_queues.clear()
            app_module._event_history.clear()
            app_module._event_sequence = 0
        app_module.task_store = self.original_store
        app_module.task_registry = self.original_registry
        self.database_dir.cleanup()

    @staticmethod
    def _sse_payload(chunk):
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        data_line = next(
            line for line in text.splitlines() if line.startswith("data: ")
        )
        return json.loads(data_line.removeprefix("data: "))

    def _bind_registry(self, registry):
        app_module.task_registry = registry
        app_module.task_store = registry.store

    def _rebuild_registry(self, *, recover_on_startup=False):
        registry = TaskRegistry(
            TaskStore(self.database_path),
            recover_on_startup=recover_on_startup,
        )
        self._bind_registry(registry)
        return registry

    def test_completed_task_survives_registry_and_app_binding_rebuild(self):
        task_id, conflict = app_module._reserve_task(
            "persist",
            "topic_pipeline",
            "等待处理",
            source_paths=(Path(self.database_dir.name) / "source.flv",),
        )
        self.assertIsNone(conflict)
        app_module.update_task(
            task_id,
            status="running",
            progress="处理中",
            step=25,
            total=100,
        )
        app_module.update_task(
            task_id,
            status="done",
            progress="完成",
            result='{"topic_count": 2}',
            step=100,
            total=100,
        )

        self._rebuild_registry()

        task = app_module.tasks[task_id]
        self.assertEqual(task["status"], "done")
        self.assertEqual(json.loads(task["result"])["topic_count"], 2)

    def test_pipeline_completion_retries_minimal_summary_without_business_error(self):
        root = Path(self.database_dir.name)
        task_id, conflict = app_module._reserve_task(
            "completion-retry",
            "topic_pipeline",
            "等待处理",
            source_paths=(root / "source.flv",),
            output_paths=(root / "output",),
        )
        self.assertIsNone(conflict)
        app_module.update_task(task_id, status="running", progress="处理中")
        original_complete = app_module.task_registry.complete
        call_count = 0

        def flaky_complete(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("result_summary 序列化后不能超过 65536 字节")
            return original_complete(*args, **kwargs)

        with (
            patch.object(
                app_module.task_registry,
                "complete",
                side_effect=flaky_complete,
            ),
            patch.object(app_module.app.logger, "error") as logger,
        ):
            app_module._complete_pipeline_task(
                task_id,
                {
                    "report": "完整报告" * 50_000,
                    "topic_count": 2,
                    "clip_marks": [{"start": 1, "end": 2}],
                    "artifact_dir": str(root / "output" / "source_自动切片"),
                    "overview_path": str(
                        root / "output" / "source_自动切片" / "00_概览.md"
                    ),
                },
            )

        task = app_module.tasks[task_id]
        self.assertEqual(call_count, 2)
        self.assertEqual(task["status"], "done")
        self.assertNotIn("report", task["result_summary"])
        logger.assert_called_once()

    def test_pipeline_completion_with_oversized_path_remains_done(self):
        root = Path(self.database_dir.name)
        artifact_dir = root / "output" / "source_自动切片"
        task_id, conflict = app_module._reserve_task(
            "completion-large-path",
            "topic_pipeline",
            "等待处理",
            source_paths=(root / "source.flv",),
            output_paths=(root / "output",),
        )
        self.assertIsNone(conflict)
        app_module.update_task(task_id, status="running", progress="处理中")

        app_module._complete_pipeline_task(
            task_id,
            {
                "report": "产物已经成功生成",
                "topic_count": 1,
                "slice_count": 1,
                "artifact_dir": str(artifact_dir),
                "overview_path": "F:\\" + "超长目录\\" * 20_000 + "00_概览.md",
            },
        )

        task = app_module.tasks[task_id]
        self.assertEqual(task["status"], "done")
        self.assertIsNone(task["error_summary"])
        self.assertEqual(task["result_summary"]["artifact_dir"], str(artifact_dir))
        self.assertNotIn("overview_path", task["result_summary"])

        response = self.client.get(f"/api/tasks/{task_id}/result")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["topic_count"], 1)

    def test_completed_pipeline_report_is_read_from_artifact_directory(self):
        root = Path(self.database_dir.name)
        output_dir = root / "自动切片"
        artifact_dir = output_dir / "录播_自动切片"
        report_path = artifact_dir / "01_话题分析.md"
        artifact_dir.mkdir(parents=True)
        report_path.write_text("# 完整话题分析\n\n测试报告", encoding="utf-8")
        task_id, conflict = app_module._reserve_task(
            "report",
            "topic_pipeline",
            "等待处理",
            source_paths=(root / "录播.flv",),
            output_paths=(artifact_dir, output_dir / "录播_话题切片"),
        )
        self.assertIsNone(conflict)
        app_module._set_task_output_dir(task_id, output_dir)
        app_module.update_task(task_id, status="running", progress="处理中")
        app_module._complete_pipeline_task(
            task_id,
            {
                "report": "不进入任务表的完整报告",
                "topic_count": 1,
                "clip_marks": [],
                "artifact_dir": str(artifact_dir),
                "overview_path": str(artifact_dir / "00_概览.md"),
                "md_path": str(report_path),
            },
        )

        response = self.client.get(f"/api/tasks/{task_id}/report")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "# 完整话题分析\n\n测试报告")
        self.assertNotIn("report", app_module.tasks[task_id]["result_summary"])

    def test_startup_recovers_active_tasks_once_and_init_shows_interrupted(self):
        queued_id, _ = app_module._reserve_task(
            "queued",
            "topic_pipeline",
            "排队中",
            source_paths=(Path(self.database_dir.name) / "queued.flv",),
        )
        running_id, _ = app_module._reserve_task(
            "running",
            "subtitle_review",
            "排队中",
            source_paths=(Path(self.database_dir.name) / "running.srt",),
        )
        app_module.update_task(
            running_id,
            status="running",
            progress="处理中",
            step=3,
            total=10,
        )

        recovered = self._rebuild_registry(recover_on_startup=True)
        self.assertEqual(
            set(recovered.recovered_task_ids),
            {queued_id, running_id},
        )
        self.assertEqual(app_module.tasks[queued_id]["status"], "interrupted")
        self.assertEqual(app_module.tasks[running_id]["status"], "interrupted")

        second = self._rebuild_registry(recover_on_startup=True)
        self.assertEqual(second.recovered_task_ids, ())
        response = self.client.get("/api/events", buffered=False)
        stream = iter(response.response)
        payload = self._sse_payload(next(stream))
        response.close()
        self.assertEqual(payload[queued_id]["status"], "interrupted")
        self.assertEqual(payload[running_id]["status"], "interrupted")

    def test_same_basename_in_different_directories_has_distinct_ids(self):
        root = Path(self.database_dir.name)
        first, first_conflict = app_module._reserve_task(
            "same-name",
            "subtitle_review",
            "等待",
            source_paths=(root / "a" / "same.srt",),
        )
        second, second_conflict = app_module._reserve_task(
            "same-name",
            "subtitle_review",
            "等待",
            source_paths=(root / "b" / "same.srt",),
        )

        self.assertIsNone(first_conflict)
        self.assertIsNone(second_conflict)
        self.assertNotEqual(first, second)

    def test_same_output_is_atomically_rejected_with_existing_task_id(self):
        root = Path(self.database_dir.name)
        shared_output = root / "output" / "same.mp4"
        first, conflict = app_module._reserve_task(
            "first",
            "subtitle_render",
            "等待",
            source_paths=(root / "a.mp4",),
            output_paths=(shared_output,),
        )
        blocked, active_task_id = app_module._reserve_task(
            "second",
            "subtitle_render",
            "等待",
            source_paths=(root / "b.mp4",),
            output_paths=(shared_output,),
        )

        self.assertIsNone(conflict)
        self.assertIsNone(blocked)
        self.assertEqual(active_task_id, first)

    def test_duplicate_subtitle_render_starts_only_one_thread(self):
        folder = Path(self.database_dir.name) / "投稿"
        folder.mkdir()
        video = folder / "片段.mp4"
        srt = folder / "片段.srt"
        video.write_bytes(b"video")
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n测试\n",
            encoding="utf-8",
        )
        started = []

        class CountingDeferredThread(DeferredThread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                started.append(self)

        with patch.object(
                app_module.threading,
                "Thread",
                CountingDeferredThread):
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
        self.assertEqual(len(started), 1)
        self.assertTrue(first.get_json()["task_id"].startswith("subtitle_render_"))
        self.assertEqual(
            duplicate.get_json()["task_id"],
            first.get_json()["task_id"],
        )

    def test_cancel_is_idempotent_and_old_thread_cannot_overwrite_it(self):
        task_id, _ = app_module._reserve_task(
            "cancel",
            "topic_pipeline",
            "等待",
            source_paths=(Path(self.database_dir.name) / "cancel.flv",),
        )
        app_module.update_task(
            task_id,
            status="running",
            progress="处理中",
            step=1,
            total=100,
        )

        first = self.client.post(f"/api/tasks/{task_id}/cancel")
        repeated = self.client.post(f"/api/tasks/{task_id}/cancel")
        app_module.update_task(
            task_id,
            status="done",
            result="旧线程完成",
            step=100,
            total=100,
        )
        app_module._record_task_error(task_id, "旧线程失败", RuntimeError("boom"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(app_module.tasks[task_id]["status"], "cancelled")
        self.assertEqual(
            self.client.post("/api/tasks/missing/cancel").status_code,
            404,
        )

        done_id, _ = app_module._reserve_task(
            "done",
            "topic_pipeline",
            "等待",
            source_paths=(Path(self.database_dir.name) / "done.flv",),
        )
        app_module.update_task(done_id, status="done", result="完成")
        illegal = self.client.post(f"/api/tasks/{done_id}/cancel")
        self.assertEqual(illegal.status_code, 409)
        self.assertIn("不能取消", illegal.get_json()["error"])

    def test_metadata_and_artifact_directory_survive_restart(self):
        root = Path(self.database_dir.name)
        output_dir = root / "自动切片"
        artifact_dir = output_dir / "录播_自动切片"
        artifact_dir.mkdir(parents=True)
        task_id, _ = app_module._reserve_task(
            "artifact",
            "topic_pipeline",
            "等待",
            source_paths=(root / "录播.flv",),
            metadata={"source_srt_path": str(root / "录播.srt"), "force": True},
        )
        app_module._set_task_output_dir(task_id, output_dir)
        app_module.update_task(
            task_id,
            status="done",
            result=json.dumps(
                {"artifact_dir": str(artifact_dir)},
                ensure_ascii=False,
            ),
        )

        self._rebuild_registry()

        task = app_module.tasks[task_id]
        self.assertEqual(task["source_srt_path"], str(root / "录播.srt"))
        self.assertTrue(task["force"])
        assert_same_path(self, task["output_dir"], output_dir)
        assert_same_path(self, task["artifact_dir"], artifact_dir)
        assert_same_path(
            self,
            app_module._completed_task_artifact_dir(task_id),
            artifact_dir,
        )

    def test_mapping_metadata_cannot_override_core_fields(self):
        app_module.tasks["real-id"] = {
            "status": "done",
            "task_type": "legacy",
            "result": "ok",
            "metadata": {
                "status": "running",
                "task_id": "forged-id",
                "source_srt_path": "subtitle.srt",
            },
        }

        task = app_module.tasks["real-id"]
        self.assertEqual(task["task_id"], "real-id")
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["source_srt_path"], "subtitle.srt")

    def test_history_ttl_and_limit_never_delete_active_tasks(self):
        app_module.tasks.update({
            "active": {"status": "running", "created_at": 1},
            "expired": {"status": "done", "completed_at": 80},
            "recent-1": {"status": "done", "completed_at": 95},
            "recent-2": {"status": "error", "completed_at": 96},
            "recent-3": {"status": "done", "completed_at": 97},
        })

        deleted = app_module.task_registry.cleanup_history(
            ttl_seconds=10,
            keep_latest=2,
            now=100,
        )

        self.assertEqual(deleted, 2)
        self.assertEqual(
            set(app_module.tasks),
            {"active", "recent-2", "recent-3"},
        )

    def test_sse_init_snapshot_is_finite_and_includes_terminal_history(self):
        now = time.time()
        app_module.tasks.update({
            f"done-{index}": {
                "status": "interrupted" if index == 0 else "done",
                "completed_at": now + index / 1000,
            }
            for index in range(6)
        })

        with patch.object(app_module, "_TASK_HISTORY_LIMIT", 3):
            response = self.client.get("/api/events", buffered=False)
            stream = iter(response.response)
            first_chunk = next(stream)
            payload = self._sse_payload(first_chunk)
            response.close()

        self.assertIn(b"event: init", first_chunk)
        self.assertLessEqual(len(payload), 3)
        self.assertTrue(
            all(task["status"] in {"done", "interrupted"} for task in payload.values())
        )

    def test_sse_last_event_id_replays_only_missing_events(self):
        first_id = app_module.broadcast("task_update", {"task_id": "first"})
        second_id = app_module.broadcast("task_update", {"task_id": "second"})

        response = self.client.get(
            "/api/events",
            headers={"Last-Event-ID": str(first_id)},
            buffered=False,
        )
        stream = iter(response.response)
        chunk = next(stream)
        response.close()

        self.assertIn(f"id: {second_id}".encode(), chunk)
        self.assertNotIn(b"event: init", chunk)
        self.assertEqual(self._sse_payload(chunk)["task_id"], "second")

    def test_sse_too_old_or_invalid_id_falls_back_to_init(self):
        with patch.object(app_module, "_SSE_EVENT_HISTORY_LIMIT", 2):
            app_module.broadcast("task_update", {"task_id": "one"})
            app_module.broadcast("task_update", {"task_id": "two"})
            app_module.broadcast("task_update", {"task_id": "three"})

        for last_event_id in ("0", "invalid"):
            with self.subTest(last_event_id=last_event_id):
                response = self.client.get(
                    "/api/events",
                    headers={"Last-Event-ID": last_event_id},
                    buffered=False,
                )
                stream = iter(response.response)
                chunk = next(stream)
                response.close()
                self.assertIn(b"event: init", chunk)

    def test_sse_event_history_has_ttl_and_count_limits(self):
        with (
            patch.object(app_module, "_SSE_EVENT_HISTORY_LIMIT", 2),
            patch.object(app_module, "_SSE_EVENT_HISTORY_TTL_SEC", 10),
            patch.object(app_module.time, "time", side_effect=(0.0, 5.0, 20.0)),
        ):
            first_id = app_module.broadcast("test", {"value": 1})
            second_id = app_module.broadcast("test", {"value": 2})
            third_id = app_module.broadcast("test", {"value": 3})

        self.assertEqual((first_id, second_id, third_id), (1, 2, 3))
        self.assertEqual(
            [event_id for event_id, _created, _message in app_module._event_history],
            [third_id],
        )

    def test_sse_queue_overflow_removes_subscriber_and_stops_generator(self):
        with patch.object(app_module, "_SSE_SUBSCRIBER_QUEUE_SIZE", 1):
            response = self.client.get("/api/events", buffered=False)
            stream = iter(response.response)
            self.assertIn(b"event: init", next(stream))
            app_module.broadcast("task_update", {"task_id": "one"})
            app_module.broadcast("task_update", {"task_id": "two"})

        with self.assertRaises(StopIteration):
            next(stream)
        response.close()
        with app_module.event_queue_lock:
            self.assertEqual(app_module.event_queues, [])

    def test_concurrent_subscribers_receive_safe_consistent_snapshots(self):
        app_module.tasks.update({
            "done": {"status": "done", "result": "ok"},
            "interrupted": {"status": "interrupted", "result": "retry"},
        })

        def read_snapshot():
            client = app_module.app.test_client()
            response = client.get("/api/events", buffered=False)
            stream = iter(response.response)
            chunk = next(stream)
            response.close()
            return self._sse_payload(chunk)

        with ThreadPoolExecutor(max_workers=6) as executor:
            snapshots = list(executor.map(lambda _index: read_snapshot(), range(12)))

        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))
        self.assertEqual(set(snapshots[0]), {"done", "interrupted"})
        with app_module.event_queue_lock:
            self.assertEqual(app_module.event_queues, [])

    def test_sse_redacts_credentials_cookies_tracebacks_and_error_paths(self):
        event_id = app_module.broadcast(
            "task_update",
            {
                "status": "error",
                "error": r"token=secret C:\private\api_config.json Traceback failed",
                "cookie": "session=secret",
                "traceback": "Traceback (most recent call last)",
            },
        )
        response = self.client.get(
            "/api/events",
            headers={"Last-Event-ID": str(event_id - 1)},
            buffered=False,
        )
        stream = iter(response.response)
        chunk = next(stream)
        response.close()
        text = chunk.decode("utf-8")

        self.assertNotIn("secret", text)
        self.assertNotIn(r"C:\private", text)
        self.assertNotIn("Traceback", text)
        self.assertIn("[已隐藏]", text)

    def test_success_sse_hides_absolute_paths_and_result_api_returns_full_payload(self):
        root = Path(self.database_dir.name)
        artifact_dir = root / "整理包"
        task_id, conflict = app_module._reserve_task(
            "sse-audit",
            "topic_pipeline",
            "等待处理",
            source_paths=(root / "source.flv",),
            output_paths=(root / "output",),
        )
        self.assertIsNone(conflict)
        app_module.update_task(
            task_id,
            status="running",
            progress="处理中",
            step=20,
            total=100,
        )
        full_result = {
            "artifact_dir": str(artifact_dir),
            "overview_path": str(artifact_dir / "00_概览.md"),
            "md_path": str(artifact_dir / "01_话题分析.md"),
            "json_path": str(artifact_dir / "数据" / "clip_marks.json"),
            "slice_dir": str(root / "话题切片"),
            "srt_path": str(artifact_dir / "数据" / "校对字幕.srt"),
            "topic_count": 1,
        }
        app_module.update_task(
            task_id,
            status="done",
            progress="完成",
            result=full_result,
            step=100,
            total=100,
        )
        event_id = app_module._event_history[-1][0]
        response = self.client.get(
            "/api/events",
            headers={"Last-Event-ID": str(event_id - 1)},
            buffered=False,
        )
        stream = iter(response.response)
        chunk = next(stream)
        response.close()
        text = chunk.decode("utf-8")
        payload = self._sse_payload(chunk)

        self.assertNotIn(str(root), text)
        self.assertEqual(payload["artifact_dir"], artifact_dir.name)
        self.assertEqual(payload["source_path"], "source.flv")
        self.assertEqual(
            json.loads(payload["result"])["overview_path"],
            "00_概览.md",
        )

        init_response = self.client.get("/api/events", buffered=False)
        init_stream = iter(init_response.response)
        init_chunk = next(init_stream)
        init_response.close()
        self.assertNotIn(str(root), init_chunk.decode("utf-8"))

        result_response = self.client.get(f"/api/tasks/{task_id}/result")
        self.assertEqual(result_response.status_code, 200)
        assert_same_path(
            self,
            result_response.get_json()["artifact_dir"],
            artifact_dir,
        )


class WebTransportSafetyTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        app_module.tasks.clear()
        if hasattr(app_module, "event_queue_lock"):
            with app_module.event_queue_lock:
                app_module.event_queues.clear()
        else:
            app_module.event_queues.clear()
        self.client = _bootstrapped_client()

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

    def test_legacy_task_delete_releases_cancellation_event(self):
        task_id, conflict = app_module.task_registry.reserve(
            "subtitle_review",
            source_paths=(Path(tempfile.gettempdir()) / "legacy-delete.flv",),
        )
        self.assertIsNone(conflict)
        app_module.task_registry.mark_running(task_id)
        app_module.task_registry.cancellation_event(task_id)

        del app_module.tasks[task_id]

        self.assertNotIn(task_id, app_module.task_registry._cancellation_events)
        self.assertIsNone(app_module.task_registry.get(task_id))

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
        self.client = _bootstrapped_client()

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
            video = folder / "最终成片.flv"
            video.write_bytes(b"video")
            scan = self.client.post("/api/subtitles/scan", json={"root_dir": td})

            def fake_transcribe(
                    video_path, progress_callback=None, foreground_only=True,
                    transcription_service=None):
                self.assertTrue(callable(transcription_service))
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
                    "autoslice.subtitle_workflow.transcribe_submission_video",
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
            task["subtitle_pair_id"],
            scan.get_json()["pairs"][0]["id"],
        )
        self.assertTrue(task["foreground_only"])
        self.assertEqual(
            result["background_filter"]["mode"],
            "speaker_diarization",
        )
        self.assertTrue(generated_srt_exists)
        transcribe.assert_called_once()
        self.assertTrue(transcribe.call_args.kwargs["foreground_only"])
        from autoslice.topic_engine import ensure_srt
        self.assertIs(
            transcribe.call_args.kwargs["transcription_service"],
            ensure_srt,
        )
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
                    "autoslice.subtitle_workflow.suggest_subtitle_corrections",
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
        snapshot = review.call_args.kwargs["streamer_profile"]
        self.assertEqual(snapshot.id, "zeyin")
        self.assertEqual(snapshot.label, "泽音 Melody")
        self.assertIn("朱鹮", snapshot.subtitle_glossary)
        self.assertIn(("英英", "音音"), snapshot.asr_replacements)
        self.assertIsNone(review.call_args.kwargs["glossary"])
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
                    "autoslice.subtitle_workflow.suggest_subtitle_corrections",
                    return_value={"suggestions": []},
                ) as review,
            ):
                response = self.client.post(
                    "/api/subtitles/review",
                    json={"video_path": str(video), "srt_path": str(srt)},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["review_profile"]["id"], "generic")
        snapshot = review.call_args.kwargs["streamer_profile"]
        self.assertEqual(snapshot.id, "generic")
        self.assertEqual(snapshot.asr_replacements, ())

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
                    "autoslice.subtitle_workflow.generate_subtitle_reference_titles",
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
        from autoslice.transcription.contracts import SubtitleTitleServices
        from autoslice.topic_engine import subtitle_title_services
        injected_services = generate.call_args.kwargs["title_services"]
        expected_services = subtitle_title_services()
        self.assertIsInstance(injected_services, SubtitleTitleServices)
        self.assertIs(
            injected_services.build_title_style_prompt,
            expected_services.build_title_style_prompt,
        )
        self.assertIs(
            injected_services.normalise_publish_title,
            expected_services.normalise_publish_title,
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
                    "autoslice.subtitle_workflow.suggest_subtitle_corrections",
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
                "autoslice.subtitle_workflow.render_subtitle_preview",
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
                    "autoslice.subtitle_workflow.burn_subtitles",
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
                    "autoslice.subtitle_workflow.burn_subtitles",
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
