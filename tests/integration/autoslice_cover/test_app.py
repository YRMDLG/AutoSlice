"""AutoCover Flask API 测试。"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from autoslice_cover import API_VERSION, SERVICE_ID
from autoslice_cover.app import ApiError, _number_value, _render_options, create_app
from autoslice_cover.renderer import render_cover as actual_render_cover
from autoslice_cover.video import FrameCandidate, FrameMetrics, VideoMetadata
from autoslice_cover.workspace import DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR

APP_MODULE = "autoslice_cover.app"
WORKSPACE_MODULE = "autoslice_cover.workspace"


def _bootstrapped_client(flask_app):
    client = flask_app.test_client()
    response = client.get("/api/security/session")
    if response.status_code != 200:
        raise RuntimeError("AutoCover 测试客户端无法建立本机会话")
    return client


def _copy_generation_response() -> dict[str, object]:
    return {
        "candidates": [
            {
                "label": "后果优先",
                "reason": "写出免费游戏的反转后果",
                "template_key": "headline",
                "palette_key": "latest_conflict",
                "lines": [
                    {"text": "朋友说送我大作", "role": "context"},
                    {"text": "点开竟是免费游戏", "role": "emphasis"},
                ],
            },
            {
                "label": "原话反差",
                "reason": "保留双方原话与结果",
                "template_key": "dialog",
                "palette_key": "latest_cyan",
                "lines": [
                    {"text": "朋友神秘送礼", "role": "context"},
                    {"text": "她说绝对是大作", "role": "quote"},
                    {"text": "结果根本不要钱", "role": "emphasis"},
                ],
            },
            {
                "label": "双角色对话",
                "reason": "不同 role 区分对话角色",
                "template_key": "evidence",
                "palette_key": "latest_soft",
                "lines": [
                    {"text": "朋友说送你一个游戏", "role": "context"},
                    {"text": "朋友：绝对是大作", "role": "quote"},
                    {"text": "主播：这不是免费的吗", "role": "neutral"},
                    {"text": "送礼送了个寂寞", "role": "emphasis"},
                ],
            },
        ]
    }


def _copy_review_response() -> dict[str, object]:
    return {
        "selected_index": 1,
        "reviews": [
            {
                "candidate_index": index,
                "original_accuracy": "pass",
                "hook_consequence": "pass",
                "clickability_score": 5 - index,
                "reason": "与字幕一致且包含后果",
            }
            for index in range(3)
        ],
    }


class ResourcePathTests(unittest.TestCase):
    def test_templates_and_static_files_do_not_depend_on_current_directory(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            try:
                os.chdir(root)
                flask_app = create_app({
                    "TESTING": True,
                    "STICKER_DIR": str(root / "贴图"),
                    "IMPORTED_STICKER_DIR": str(root / "导入贴图"),
                })
                client = _bootstrapped_client(flask_app)
                page = client.get("/")
                stylesheet = client.get("/static/styles.css")
            finally:
                os.chdir(previous)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(stylesheet.status_code, 200)
        page.close()
        stylesheet.close()


class SecurityBoundaryTests(unittest.TestCase):
    """验证 AutoCover 与 AutoSlice 使用同一套 Host/Origin/路径策略。"""

    STRONG_LAN_TOKEN = "R8s6T4u2V9w7X5y3Z1a8B6c4D2e9F7g5"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.allowed = self.root / "允许"
        self.allowed.mkdir()
        self.blocked = self.root / "越界"
        self.blocked.mkdir()
        self.app = create_app({
            "TESTING": True,
            "STICKER_DIR": str(self.root / "贴图"),
            "IMPORTED_STICKER_DIR": str(self.root / "导入贴图"),
        })

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _lan_environment(self, **overrides) -> dict[str, str]:
        environment = {
            "AUTOCOVER_LAN_MODE": "1",
            "AUTOCOVER_LAN_TOKEN": self.STRONG_LAN_TOKEN,
            "AUTOCOVER_LAN_HOSTS": "192.168.1.30",
            "AUTOCOVER_LAN_ORIGINS": "http://192.168.1.30:5010",
            "AUTOCOVER_ALLOWED_ROOTS": str(self.allowed),
        }
        environment.update(overrides)
        return environment

    def test_loopback_ipv4_localhost_ipv6_and_forged_host(self) -> None:
        client = self.app.test_client()
        for host in ("127.0.0.1:5010", "localhost:5010", "[::1]:5010"):
            with self.subTest(host=host):
                self.assertEqual(
                    client.get("/api/options", headers={"Host": host}).status_code,
                    200,
                )
        for host in ("attacker.example", "localhost.attacker.example"):
            with self.subTest(host=host):
                self.assertEqual(
                    client.get("/api/options", headers={"Host": host}).status_code,
                    403,
                )

    def test_write_needs_same_origin_referer_or_http_only_session(self) -> None:
        missing = self.app.test_client().post(
            "/api/layout-variants",
            json={"title": "缺少写证明"},
        )
        by_origin = self.app.test_client().post(
            "/api/layout-variants",
            json={"title": "同源 Origin"},
            headers={
                "Host": "127.0.0.1:5010",
                "Origin": "http://127.0.0.1:5010",
            },
        )
        by_referer = self.app.test_client().post(
            "/api/layout-variants",
            json={"title": "同源 Referer"},
            headers={
                "Host": "[::1]:5010",
                "Referer": "http://[::1]:5010/editor",
            },
        )
        browser = self.app.test_client()
        bootstrap = browser.get("/api/security/session")
        by_session = browser.post(
            "/api/layout-variants",
            json={"title": "会话写入"},
        )
        cross_site = browser.post(
            "/api/layout-variants",
            json={"title": "跨站写入"},
            headers={"Origin": "https://attacker.example"},
        )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(by_origin.status_code, 200)
        self.assertEqual(by_referer.status_code, 200)
        self.assertEqual(by_session.status_code, 200)
        self.assertEqual(cross_site.status_code, 403)
        self.assertEqual(bootstrap.get_json(), {"ok": True, "mode": "local"})
        self.assertIn("HttpOnly", bootstrap.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", bootstrap.headers["Set-Cookie"])
        self.assertNotIn("token", bootstrap.get_data(as_text=True).casefold())

    def test_lan_mode_requires_strong_non_url_token_and_explicit_origin(self) -> None:
        valid = self._lan_environment()
        weak = {**valid, "AUTOCOVER_LAN_TOKEN": "x" * 64}
        request_headers = {"Host": "192.168.1.30:5010"}
        with patch.dict(os.environ, weak, clear=False):
            weak_response = self.app.test_client().get(
                "/api/options",
                headers={
                    **request_headers,
                    "X-AutoCover-Token": weak["AUTOCOVER_LAN_TOKEN"],
                },
            )
        with patch.dict(os.environ, valid, clear=False):
            unauthenticated = self.app.test_client().get(
                "/api/options",
                headers=request_headers,
            )
            url_token = self.app.test_client().get(
                "/api/options",
                query_string={"token": self.STRONG_LAN_TOKEN},
                headers=request_headers,
            )
            wrong_origin = self.app.test_client().post(
                "/api/layout-variants",
                json={"title": "不允许的来源"},
                headers={
                    **request_headers,
                    "Origin": "http://192.168.1.31:5010",
                    "X-AutoCover-Token": self.STRONG_LAN_TOKEN,
                },
            )

        self.assertEqual(weak_response.status_code, 503)
        self.assertNotIn(
            weak["AUTOCOVER_LAN_TOKEN"],
            weak_response.get_data(as_text=True),
        )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(url_token.status_code, 401)
        self.assertEqual(wrong_origin.status_code, 403)

    def test_lan_paths_cover_json_form_query_and_upload_inputs(self) -> None:
        environment = self._lan_environment()
        read_headers = {
            "Host": "192.168.1.30:5010",
            "X-AutoCover-Token": self.STRONG_LAN_TOKEN,
        }
        write_headers = {
            **read_headers,
            "Origin": "http://192.168.1.30:5010",
        }
        with patch.dict(os.environ, environment, clear=False):
            client = self.app.test_client()
            allowed_json = client.post(
                "/api/workspace/scan",
                json={
                    "root": str(self.allowed),
                    "cache_dir": str(self.allowed / "缓存"),
                    "output_dir": str(self.allowed / "输出"),
                },
                headers=write_headers,
            )
            blocked_json = client.post(
                "/api/workspace/scan",
                json={"root": str(self.blocked)},
                headers=write_headers,
            )
            allowed_query = client.get(
                "/api/options",
                query_string={"output_path": str(self.allowed / "cover.jpg")},
                headers=read_headers,
            )
            blocked_query = client.get(
                "/api/options",
                query_string={"output_path": str(self.blocked / "cover.jpg")},
                headers=read_headers,
            )
            allowed_form = client.post(
                "/api/options",
                data={"output_dir": str(self.allowed)},
                headers=write_headers,
            )
            blocked_form = client.post(
                "/api/options",
                data={"output_dir": str(self.blocked)},
                headers=write_headers,
            )
            blocked_upload = client.post(
                "/api/not-found",
                data={"file": (io.BytesIO(b"png"), "../escape.png")},
                headers=write_headers,
            )
            safe_upload = client.post(
                "/api/not-found",
                data={
                    "target_path": str(self.allowed / "safe.png"),
                    "file": (io.BytesIO(b"png"), "safe.png"),
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


class AppTests(unittest.TestCase):
    """覆盖 API 正常流程、参数校验和媒体访问边界。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clips = self.root / "切片"
        self.clips.mkdir()
        (self.clips / "01_司机回头.mp4").write_bytes(b"video-one")
        (self.clips / "02_线下秘密.mkv").write_bytes(b"video-two")
        self.output = self.root / "输出"
        self.cache = self.root / "缓存"
        self.sticker_root = self.root / "视频素材"
        self.sticker_dir = self.sticker_root / "表情包"
        self.sticker_dir.mkdir(parents=True)
        self.sticker = self.sticker_dir / "震惊.png"
        Image.new("RGBA", (240, 160), (255, 70, 110, 220)).save(self.sticker)
        self.frame = self.root / "frame.jpg"
        Image.new("RGB", (1920, 1080), "#d884ad").save(self.frame)
        self.candidate = FrameCandidate(
            path=str(self.frame),
            timestamp=8.5,
            score=88.0,
            metrics=FrameMetrics(0.5, 1.0, 0.6, 0.7, 0.4, 0.0),
        )
        self.app = create_app({
            "TESTING": True,
            "STICKER_DIR": str(self.sticker_root),
            "IMPORTED_STICKER_DIR": str(self.root / "导入贴图"),
        })
        self.client = _bootstrapped_client(self.app)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _scan(self) -> list[dict[str, object]]:
        response = self.client.post(
            "/api/workspace/scan",
            json={
                "root": str(self.clips),
                "cache_dir": str(self.cache),
                "output_dir": str(self.output),
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["tasks"]

    def test_options_use_generic_product_labels_and_hide_profile_rules(self) -> None:
        response = self.client.get("/api/options")
        script = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(script.status_code, 200)
        script_text = script.get_data(as_text=True)
        script.close()
        payload = response.get_json()
        public_style_data = json.dumps(
            {
                "templates": payload["templates"],
                "palettes": payload["palettes"],
            },
            ensure_ascii=False,
        )
        self.assertNotIn("泽音", public_style_data)
        self.assertNotIn("音音", public_style_data)
        self.assertNotIn("晚安小音音", public_style_data)
        self.assertNotIn("cover_rules", response.get_data(as_text=True))
        self.assertNotIn("泽音", script_text)
        self.assertNotIn("音音", script_text)

    def test_scan_accepts_explicit_manifest_and_never_exposes_absolute_paths(self) -> None:
        clip_path = (self.clips / "01_司机回头.mp4").resolve()
        subtitle_path = (self.root / "司机回头_校对.srt").resolve()
        subtitle_path.write_text("字幕", encoding="utf-8")
        manifest_path = self.root / "精调任务.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tasks": [
                        {
                            "id": "01",
                            "clip_timebase": "source_video_seconds",
                            "source_segment_count": 1,
                            "clip_start_seconds": 20,
                            "clip_end_seconds": 80,
                            "slice_anchor": 47,
                            "slice_anchor_source": "语义复核",
                            "cover_anchor_seconds": 27,
                            "cover_anchor_media_path": str(clip_path),
                            "editorial_interest_score": 5,
                            "editorial_interest_reason": "司机回头形成反差",
                            "publish_title": "【测试】聊到关键处司机突然回头",
                            "original_slice_path": str(clip_path),
                            "final_clip_path": None,
                            "corrected_srt_path": str(subtitle_path),
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        response = self.client.post(
            "/api/workspace/scan",
            json={
                "root": str(self.clips),
                "cache_dir": str(self.cache),
                "output_dir": str(self.output),
                "manifest_json_path": str(manifest_path),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        task = next(item for item in payload["tasks"] if item["filename"] == clip_path.name)
        self.assertEqual(task["title"], "【测试】聊到关键处司机突然回头")
        self.assertEqual(
            task["cover_contract"],
            {
                "matched": True,
                "match_source": "manifest_original_slice",
                "cover_anchor_seconds": 27.0,
                "slice_anchor_source": "语义复核",
                "editorial_interest_score": 5.0,
                "editorial_interest_reason": "司机回头形成反差",
                "subtitle_exists": True,
                "subtitle_filename": subtitle_path.name,
            },
        )
        response_text = response.get_data(as_text=True)
        self.assertNotIn(str(clip_path), response_text)
        self.assertNotIn(str(subtitle_path), response_text)
        self.assertNotIn(str(manifest_path), response_text)

    def test_damaged_manifest_does_not_break_existing_scan_workflow(self) -> None:
        manifest_path = self.root / "损坏.json"
        manifest_path.write_text("{broken", encoding="utf-8")

        response = self.client.post(
            "/api/workspace/scan",
            json={
                "root": str(self.clips),
                "cache_dir": str(self.cache),
                "output_dir": str(self.output),
                "manifest_json_path": str(manifest_path),
            },
        )

        self.assertEqual(response.status_code, 200)
        tasks = response.get_json()["tasks"]
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(not task["cover_contract"]["matched"] for task in tasks))

    def _ready_task(self) -> dict[str, object]:
        task = self._scan()[0]
        with patch(
            f"{WORKSPACE_MODULE}.extract_candidate_frames",
            return_value=[self.candidate],
        ):
            response = self.client.post(f"/api/tasks/{task['id']}/candidates", json={"count": 4})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["task"]

    def test_requires_scan_and_validates_scan_payload(self) -> None:
        self.assertEqual(self.client.get("/api/tasks").status_code, 409)
        response = self.client.post("/api/workspace/scan", json={"root": ""})
        self.assertEqual(response.status_code, 400)
        self.assertIn("root", response.get_json()["error"])

    def test_request_boundary_rejects_untrusted_host_and_origin(self) -> None:
        untrusted_host = self.client.get(
            "/api/options",
            headers={"Host": "attacker.example"},
        )
        cross_site = self.client.post(
            "/api/workspace/scan",
            json={"root": str(self.clips)},
            headers={"Origin": "https://attacker.example"},
        )
        local = self.client.get(
            "/api/options",
            headers={"Host": "127.0.0.1:5010"},
        )

        self.assertEqual(untrusted_host.status_code, 403)
        self.assertEqual(cross_site.status_code, 403)
        self.assertEqual(local.status_code, 200)

    def test_scan_lists_tasks_and_options(self) -> None:
        tasks = self._scan()
        options = self.client.get("/api/options").get_json()

        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["status"], "pending")
        for field in (
            "folder_created_at", "folder_modified_at",
            "source_created_at", "source_modified_at",
        ):
            self.assertIsInstance(tasks[0][field], float)
            self.assertGreater(tasks[0][field], 0)
        self.assertEqual(options["service"], SERVICE_ID)
        self.assertEqual(options["api_version"], API_VERSION)
        self.assertGreaterEqual(len(options["templates"]), 9)
        self.assertEqual({item["key"] for item in options["canvases"]}, {"4x3", "16x9"})
        self.assertEqual(options["default_input_dir"], str(DEFAULT_INPUT_DIR))
        self.assertEqual(options["default_output_dir"], str(DEFAULT_OUTPUT_DIR.resolve()))
        self.assertEqual(options["default_font"]["label"], "濑户体")
        self.assertNotIn("font_path", options["default_font"])

    def test_compatibility_endpoints_include_deprecation_headers(self) -> None:
        task = self._scan()[0]
        responses = (
            self.client.get("/api/tasks"),
            self.client.patch(
                f"/api/tasks/{task['id']}",
                json={"title": "通用账号标题"},
            ),
            self.client.post("/api/export", json={"task_ids": []}),
        )

        for response in responses:
            with self.subTest(path=response.request.path):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Deprecation"], "true")
                self.assertEqual(
                    response.headers["Warning"],
                    '299 AutoCover "Deprecated compatibility endpoint"',
                )

    def test_default_font_endpoint_matches_font_status(self) -> None:
        options = self.client.get("/api/options").get_json()
        with self.client.get("/api/fonts/default") as response:
            if options["default_font"]["available"]:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.mimetype.startswith("font/"))
                self.assertGreater(len(response.data), 10_000)
            elif options["default_font"].get("fallback"):
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.mimetype.startswith("font/"))
                self.assertGreater(len(response.data), 10_000)
            else:
                self.assertEqual(response.status_code, 404)

    def test_workbench_page_and_assets_are_available(self) -> None:
        with self.client.get("/") as page:
            page_content = page.get_data(as_text=True)
            self.assertEqual(page.status_code, 200)
            self.assertIn("AutoCover", page_content)
            self.assertIn('rel="icon" href="data:,"', page_content)
            self.assertIn('id="candidate-strip"', page_content)
            self.assertIn('id="timeline-range"', page_content)
            self.assertIn('id="background-scale"', page_content)
            self.assertIn('id="upload-sticker"', page_content)
            self.assertNotIn("direct-slice", page_content)
            self.assertIn('id="palette-select"', page_content)
            self.assertIn('id="cover-overlay"', page_content)
            self.assertIn('id="layout-variants"', page_content)
            self.assertIn('id="generate-ai-copy"', page_content)
            self.assertIn('id="copy-candidates"', page_content)
            self.assertIn('id="sticker-grid"', page_content)
            self.assertIn('id="sticker-library-summary"', page_content)
            self.assertIn('id="sticker-result-count"', page_content)
            self.assertIn('id="reset-layout"', page_content)
            self.assertIn('id="save-current"', page_content)
            self.assertIn('id="font-status"', page_content)
            self.assertIn('id="task-sort"', page_content)
            self.assertIn('id="add-copy-line"', page_content)
            self.assertIn("添加一行手动文案", page_content)
            self.assertNotIn('data-inspector-tab="style"', page_content)
            self.assertIn('id="ratio-tab-4x3"', page_content)
            self.assertIn('aria-controls="cover-canvas-panel"', page_content)
            self.assertIn(
                'id="cover-canvas-panel" role="tabpanel" aria-labelledby="ratio-tab-4x3"',
                page_content,
            )
            self.assertIn('id="inspector-tab-copy"', page_content)
            self.assertIn('id="cover-background-preview"', page_content)
            self.assertIn(
                'id="inspector-panel-copy" role="tabpanel" '
                'aria-labelledby="inspector-tab-copy"',
                page_content,
            )
            copy_panel = page_content.split('data-inspector-view="copy"', 1)[1].split(
                'data-inspector-view="sticker"', 1
            )[0]
            self.assertIn('id="copy-lines"', copy_panel)
            self.assertIn('id="template-select"', copy_panel)
            self.assertIn('id="palette-select"', copy_panel)
            self.assertIn('id="common-colors"', copy_panel)
            self.assertIn('id="common-stroke-colors"', copy_panel)
            self.assertIn('value="folder_created_desc"', page_content)
            self.assertIn('value="name_desc"', page_content)
            self.assertIn('placeholder="input"', page_content)
            self.assertIn('placeholder="covers"', page_content)
            self.assertIn('id="manifest-json-path"', page_content)
            self.assertIn('name="manifest_json_path"', page_content)
            self.assertIn('aria-describedby="manifest-json-path-hint"', page_content)
            self.assertIn('id="manifest-json-path-hint"', page_content)
            self.assertIn("AutoSlice 成果 JSON（可选）", page_content)
            self.assertIn("不填时按规范 sibling 自动发现", page_content)
        with self.client.get("/static/app.js") as script:
            self.assertEqual(script.status_code, 200)
            script_content = script.get_data(as_text=True)
            self.assertIn("/api/workspace/scan", script_content)
            self.assertIn("/api/layout-variants", script_content)
            self.assertIn("/copy-variants", script_content)
            self.assertIn("function generateAiCopy(", script_content)
            self.assertIn("function setLineRole(", script_content)
            self.assertIn("/api/stickers", script_content)
            self.assertIn("background_media_token", script_content)
            self.assertIn("default_output_dir", script_content)
            self.assertIn('const EXPECTED_API_VERSION = 8', script_content)
            self.assertIn("await scanWorkspace(config, {", script_content)
            self.assertIn('{ preserveDialog = false, autoSelect = false, restoreTaskKey = "" } = {}', script_content)
            self.assertIn('state.activeTaskId = autoSelect ? state.tasks[0]?.id || null : null', script_content)
            self.assertIn('id="common-stroke-colors"', page_content)
            self.assertIn('id="stroke-color-input"', page_content)
            self.assertIn("line_stroke_colors", script_content)
            self.assertIn("COMMON_STROKE_COLORS", script_content)
            self.assertIn('data-remove-task-id', script_content)
            self.assertIn("autocover.task-sort", script_content)
            self.assertIn("function compareTasks(", script_content)
            self.assertIn("function filterStickerAssets(", script_content)
            self.assertIn("function sortTasks(", script_content)
            self.assertIn("function appendManualCopyLine(", script_content)
            self.assertIn("function removeManualCopyLine(", script_content)
            self.assertIn("restoreTaskKey: reloadTaskDraftKey() || currentTaskKey", script_content)
            self.assertIn("const localActive = [...state.drafts.entries()]", script_content)
            self.assertIn('data-remove-copy-line', script_content)
            self.assertIn('method: "DELETE"', script_content)
            self.assertIn('return saveCover([state.ratio])', script_content)
            self.assertIn("default_input_dir", script_content)
            self.assertIn("服务版本过旧", script_content)
            self.assertIn("目录扫描失败", script_content)
            self.assertIn("预览初始化失败", script_content)
            self.assertIn('elements["cover-overlay"].getBoundingClientRect()', script_content)
            self.assertIn("renderedWidth = renderedHeight * previewRatio", script_content)
            self.assertIn("function refreshInteractionState(", script_content)
            self.assertIn('elements["cover-overlay"].inert = busy', script_content)
            self.assertIn("function handleEditableElementKeydown(", script_content)
            self.assertIn("function snapElementToCanvasCenter(", script_content)
            self.assertIn("function beginBackgroundPan(", script_content)
            self.assertIn('elements["cover-frame"].addEventListener("pointerdown", beginBackgroundPan)', script_content)
            self.assertIn("function applyInteractiveBackgroundTransform(", script_content)
            self.assertIn("applyInteractiveBackgroundTransform(settings)", script_content)
            self.assertIn("function preparePreviewLayers(", script_content)
            self.assertIn("function activateInteractivePreviewLayer(", script_content)
            self.assertIn("visibleCopyLineIndices(settings)[index]", script_content)
            self.assertIn('activateInspectorTab(byId("inspector-tab-copy"))', script_content)
            self.assertIn('const DRAFT_STORAGE_KEY = "autocover.task-drafts.v1"', script_content)
            self.assertIn('const ACTIVE_TASK_SESSION_KEY = "autocover.active-task.v1"', script_content)
            self.assertIn("function persistTaskDraft(", script_content)
            self.assertIn("function restoreTaskSettings(", script_content)
            self.assertIn("loadStoredDrafts();", script_content)
            self.assertIn("persistTaskDraft(activeTask(), { immediate: true })", script_content)
            self.assertIn("applyRecommended: settings.auto_style && !Array.isArray(settings.copy_lines)", script_content)
            self.assertIn("restoreTaskKey: reloadTaskDraftKey()", script_content)
            self.assertIn('data-alignment-guide="vertical"', script_content)
            self.assertIn('data-alignment-guide="horizontal"', script_content)
            self.assertIn("function bindRovingTablist(", script_content)
            self.assertIn('"manifest-json-path"', script_content)
            self.assertIn("manifest_json_path", script_content)
        with self.client.get("/static/styles.css") as stylesheet:
            self.assertEqual(stylesheet.status_code, 200)
            css = stylesheet.get_data(as_text=True)
            self.assertIn("@media (max-width: 760px)", css)
            self.assertIn("[hidden]", css)
            self.assertIn("overflow-x: hidden", css)
            self.assertIn(".editable-element", css)
            self.assertIn(".alignment-guide.visible", css)
            self.assertIn(".cover-frame.can-pan-background", css)
            self.assertIn(".sticker-grid", css)
            self.assertIn(".sticker-element.selected", css)
            self.assertIn('font-family: "AutoCover Seto"', css)
            self.assertIn('url("/api/fonts/default?glyph-revision=2")', css)
            self.assertIn(".stroke-color-controls", css)
            self.assertIn(".task-sort-bar", css)
            self.assertIn(".copy-editor-heading", css)
            self.assertIn(".copy-line-remove", css)
            self.assertIn(".copy-candidate-button", css)
            self.assertIn(".line-role-select", css)
            self.assertIn(".sticker-library-summary", css)
            self.assertIn(".text-style-editor", css)
            self.assertIn("clamp(410px, 24vw, 500px)", css)
            self.assertIn("minmax(480px, 1fr)", css)
            self.assertIn("overflow-x: auto", css)
            self.assertNotIn("min-width: 1040px", css)
            self.assertIn("container-type: size", css)
            self.assertIn("repeat(3, 1fr)", css)
            self.assertIn("calc(177.777cqh - 64px)", css)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证成果 JSON 工作区配置")
    def test_manifest_json_path_submits_persists_restores_and_rescans(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const storage = new Map();
global.localStorage = {
  getItem(key){ return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value){ storage.set(key, String(value)); },
  removeItem(key){ storage.delete(key); },
};
window.clearTimeout = clearTimeout;
window.setTimeout = setTimeout;
const listeners = new Map();
function fakeInput(value = "") {
  return {
    value,
    checked: true,
    disabled: false,
    open: false,
    addEventListener(type, handler){ listeners.set(`${this.id || "field"}:${type}`, handler); },
    setAttribute(){},
    focus(){},
    classList:{toggle(){}},
  };
}
elements["root-path"] = Object.assign(fakeInput("F:\\Cuts"), {id:"root-path"});
elements["title-file"] = Object.assign(fakeInput("F:\\Titles.md"), {id:"title-file"});
elements["manifest-json-path"] = Object.assign(fakeInput("F:\\Bundle\\数据\\精调任务.json"), {id:"manifest-json-path"});
elements["output-path"] = Object.assign(fakeInput("F:\\Covers"), {id:"output-path"});
elements["recursive-scan"] = Object.assign(fakeInput(), {id:"recursive-scan", checked:true});
elements["scan-submit"] = Object.assign(fakeInput(), {id:"scan-submit"});
elements["open-workspace"] = Object.assign(fakeInput(), {id:"open-workspace"});
elements.rescan = Object.assign(fakeInput(), {id:"rescan"});
elements["workspace-dialog"] = {open:false, close(){}, showModal(){this.open=true;}};
elements["workspace-error"] = {textContent:"", hidden:true};
elements["workspace-summary"] = {textContent:"", title:""};
state.options = {default_input_dir:"input", default_output_dir:"covers"};
const scanCalls = [];
api = async (_path, options = {}) => {
  scanCalls.push(JSON.parse(options.body));
  return {tasks:[], drafts:[], draft_path:""};
};
setBusy = () => {};
setWorkspaceError = () => {};
mergeDiskDrafts = () => {};
sortTasks = () => {};
renderTaskList = () => {};
renderInspector = () => {};
renderCandidates = () => {};
renderTimeline = () => {};
clearPreview = () => {};
setStatus = () => {};
bindWorkspaceEvents();
await listeners.get("scan-submit:click")();
if (scanCalls[0].manifest_json_path !== "F:\\Bundle\\数据\\精调任务.json") {
  throw new Error("表单提交没有传递成果 JSON");
}
const persisted = JSON.parse(localStorage.getItem("autocover.workspace"));
if (persisted.manifest_json_path !== "F:\\Bundle\\数据\\精调任务.json") {
  throw new Error("成果 JSON 没有持久化到 workspaceConfig");
}
state.workspaceConfig = migrateWorkspaceConfig(persisted);
openWorkspaceDialog();
if (elements["manifest-json-path"].value !== "F:\\Bundle\\数据\\精调任务.json") {
  throw new Error("重新打开工作区表单没有回填成果 JSON");
}
await listeners.get("rescan:click")();
if (scanCalls[1].manifest_json_path !== "F:\\Bundle\\数据\\精调任务.json") {
  throw new Error("重新扫描没有保留成果 JSON");
}
const legacy = migrateWorkspaceConfig({root:"F:\\Legacy", title_file:null, output_dir:null, recursive:true});
if (legacy.manifest_json_path !== null) {
  throw new Error("旧 localStorage 配置没有兼容迁移为空成果 JSON");
}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证画布键盘操作")
    def test_keyboard_transform_moves_and_resizes_editable_elements(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const textModel = {x:0.5, y:0.5, font_size:100};
if (applyKeyboardTransform(textModel, "text", "ArrowLeft", false) !== "move") {
  throw new Error("文字方向键未识别为移动");
}
if (Math.abs(textModel.x - 0.495) > 0.000001) throw new Error("文字小步移动错误");
if (textModel.center_x !== false) throw new Error("方向键移动后仍保留横向居中锚点");
applyKeyboardTransform(textModel, "text", "ArrowDown", true);
if (Math.abs(textModel.y - 0.52) > 0.000001) throw new Error("文字大步移动错误");
if (textModel.center_y !== false) throw new Error("方向键移动后仍保留纵向居中锚点");
if (applyKeyboardTransform(textModel, "text", "+", false) !== "resize") {
  throw new Error("文字加号未识别为缩放");
}
if (textModel.font_size !== 104) throw new Error("文字缩放步长错误");
const stickerModel = {x:0.99, y:0.01, width:0.18};
applyKeyboardTransform(stickerModel, "sticker", "ArrowRight", true);
if (stickerModel.x !== 1) throw new Error("贴图移动未限制到画布内");
applyKeyboardTransform(stickerModel, "sticker", "-", false);
if (Math.abs(stickerModel.width - 0.17) > 0.000001) throw new Error("贴图缩放错误");
if (applyKeyboardTransform(stickerModel, "sticker", "Enter", false) !== null) {
  throw new Error("无关按键不应修改画布元素");
}
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证中心磁吸计算")
    def test_center_snap_uses_visual_bounds_and_independent_axes(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const both = snapElementToCanvasCenter(0.39, 0.45, 100, 40, 400, 300);
if (!both.snapX || !both.snapY) throw new Error("接近中心时没有双轴吸附");
if (Math.abs(both.x - 0.375) > 0.000001) throw new Error("横向吸附没有按元素宽度居中");
if (Math.abs(both.y - (130 / 300)) > 0.000001) throw new Error("纵向吸附没有按元素高度居中");
const horizontalOnly = snapElementToCanvasCenter(0.43, 0.45, 100, 40, 400, 300);
if (horizontalOnly.snapX || !horizontalOnly.snapY) throw new Error("横纵轴没有独立吸附");
if (horizontalOnly.x !== 0.43) throw new Error("离开阈值后仍被横向吸附");
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证贴图居中锚点请求")
    def test_preview_payload_preserves_sticker_center_anchors(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const payloadTask = {
  id: "center-anchor-task",
  title: "居中锚点",
  template_key: "headline",
  palette_key: "order_all_yellow",
};
state.tasks = [payloadTask];
state.activeTaskId = payloadTask.id;
state.ratio = "4x3";
const payloadSettings = defaultSettings(payloadTask);
payloadSettings.layouts["4x3"].stickers = [{
  asset_id: "sticker-1",
  x: 0.4,
  y: 0.4,
  width: 0.2,
  rotation: 0,
  center_x: true,
  center_y: true,
}];
state.settings.set(payloadTask.id, payloadSettings);
const serializedSticker = previewPayload(payloadTask, false).layouts["4x3"].stickers[0];
if (serializedSticker.center_x !== true || serializedSticker.center_y !== true) {
  throw new Error("贴图居中锚点没有随预览请求发送");
}
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证封面草稿恢复")
    def test_task_draft_round_trip_restores_text_layout_and_pan_transform(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const storage = new Map();
global.localStorage = {
  getItem(key){ return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value){ storage.set(key, String(value)); },
};
window.clearTimeout = clearTimeout;
window.setTimeout = setTimeout;
state.options = {
  templates: [{key: "headline"}],
  palettes: [{
    key: "order_all_yellow", context_color: "#ffe438", quote_color: "#16d8ed",
    emphasis_color: "#ff4433", neutral_color: "#ffffff", stroke_color: "#111111",
  }],
};
state.workspaceConfig = {root: "F:\\Videos"};
const draftTask = {
  id: "draft-task",
  relative_path: "投稿\\片段.mp4",
  title: "默认标题",
  template_key: "headline",
  palette_key: "order_all_yellow",
  selected_timestamp: 42.5,
};
state.tasks = [draftTask];
state.activeTaskId = draftTask.id;
const draftSettings = defaultSettings(draftTask);
draftSettings.copy_lines = ["第一行", "第二行"];
draftSettings.line_colors = ["#ffffff", "#d06e95"];
draftSettings.line_stroke_colors = ["#111111", "#ffffff"];
draftSettings.line_roles = ["context", "emphasis"];
draftSettings.copy_candidates = [{
  key: "ai-1", label: "AI 候选", reason: "测试草稿恢复",
  template_key: "headline", palette_key: "order_all_yellow",
  lines: [{text: "第一行", role: "context"}, {text: "第二行", role: "emphasis"}],
}];
draftSettings.selected_copy_candidate_key = "ai-1";
draftSettings.layouts["4x3"].background_scale = 1.8;
draftSettings.layouts["4x3"].focus_x = 0.2;
draftSettings.layouts["4x3"].focus_y = 0.7;
draftSettings.layouts["4x3"].text = [
  {x: 0.1, y: 0.2, scale: 1, font_size: 88},
  {x: 0.3, y: 0.4, scale: 1, font_size: 112},
];
state.settings.set(draftTask.id, draftSettings);
persistTaskDraft(draftTask, {immediate: true});
state.settings.clear();
state.drafts.clear();
loadStoredDrafts();
const restored = taskSettings(draftTask);
if (restored.copy_lines.join("|") !== "第一行|第二行") throw new Error("文案没有恢复");
if (restored.line_colors[1] !== "#d06e95") throw new Error("文字颜色没有恢复");
if (restored.line_roles.join("|") !== "context|emphasis") throw new Error("line role 没有恢复");
if (restored.copy_candidates[0]?.key !== "ai-1") throw new Error("AI 候选没有恢复");
if (restored.layouts["4x3"].text[1].font_size !== 112) throw new Error("字号布局没有恢复");
if (restored.layouts["4x3"].background_scale !== 1.8) throw new Error("画面缩放没有恢复");
if (taskDraft(draftTask).selected_timestamp !== 42.5) throw new Error("选帧时间没有恢复");
  elements["cover-background-preview"] = {style: {removeProperty(){}}};
  applyInteractiveBackgroundTransform(restored);
  if (!elements["cover-background-preview"].style.transform.includes("scale(1.8)")) {
  throw new Error("拖动预览没有应用实时缩放轨迹");
}
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证刷新任务恢复")
    def test_reload_task_key_does_not_depend_on_navigation_timing(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
global.performance = {
  getEntriesByType(){ return [{type: "navigate"}]; },
  navigation: {type: 0},
};
global.sessionStorage = {
  getItem(key){ return key === ACTIVE_TASK_SESSION_KEY ? "saved-task-key" : null; },
};
if (reloadTaskDraftKey() !== "saved-task-key") {
  throw new Error("Ctrl+F5 被报告为 navigate 时没有恢复当前任务");
}
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证磁盘草稿合并")
    def test_disk_draft_restores_multiple_video_settings_and_invalidates_stale_preview(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const storage = new Map();
global.localStorage = {
  getItem(key){ return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value){ storage.set(key, String(value)); },
};
window.clearTimeout = clearTimeout;
window.setTimeout = setTimeout;
state.options = {
  templates: [{key: "headline"}],
  palettes: [{key: "classic"}],
};
state.workspaceConfig = {root: "F:\\Videos"};
const task = {
  id: "disk-task",
  relative_path: "投稿/片段.mp4",
  title: "默认标题",
  template_key: "headline",
  palette_key: "classic",
  selected_timestamp: 12.5,
};
state.tasks = [task];
mergeDiskDrafts([{
  relative_path: task.relative_path,
  // 后端旧草稿使用 Unix 秒，前端必须归一化后再与 localStorage 毫秒比较。
  updated_at: Date.now() / 1000,
  selected_timestamp: 12.5,
  active: true,
  settings: {
    title: "磁盘标题",
    template_key: "headline",
    palette_key: "classic",
    copy_lines: ["磁盘文字"],
    line_colors: ["#ffffff"],
    line_stroke_colors: ["#111111"],
    auto_style: false,
    layouts: {
      "4x3": {text: null, stickers: [], focus_x: 0.4, focus_y: 0.6, background_scale: 1.5},
      "16x9": {text: null, stickers: [], focus_x: 0.5, focus_y: 0.5, background_scale: 1},
    },
  },
  previews: {"4x3": {media_token: "saved-preview", placements: []}},
}], state.workspaceConfig.root);
const restored = taskSettings(task);
if (restored.title !== "磁盘标题") throw new Error("没有从磁盘恢复标题");
if (ratioLayout(restored, "4x3").background_scale !== 1.5) throw new Error("没有恢复画面缩放");
if (taskDraft(task).updated_at < 1_000_000_000_000) throw new Error("磁盘草稿时间戳没有统一为毫秒");
persistTaskDraft(task, {immediate: true});
if (!taskDraft(task).previews["4x3"]) throw new Error("未修改时错误清除了磁盘预览");
restored.title = "已修改标题";
persistTaskDraft(task, {immediate: true});
if (Object.keys(taskDraft(task).previews).length) throw new Error("设置变化后仍保留过期预览");
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证刷新后的磁盘预览恢复")
    def test_newer_matching_local_draft_keeps_valid_disk_preview(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const storage = new Map();
global.localStorage = {
  getItem(key){ return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value){ storage.set(key, String(value)); },
};
window.clearTimeout = clearTimeout;
window.setTimeout = setTimeout;
state.options = {
  templates: [{key: "headline"}],
  palettes: [{key: "classic"}],
};
state.workspaceConfig = {root: "F:\\Videos"};
const task = {
  id: "matching-draft-task",
  relative_path: "投稿/片段.mp4",
  title: "默认标题",
  template_key: "headline",
  palette_key: "classic",
  selected_timestamp: 12.5,
};
state.tasks = [task];
const settings = {
  title: "已保存标题",
  template_key: "headline",
  palette_key: "classic",
  copy_lines: ["封面大字"],
  line_colors: ["#ffffff"],
  line_stroke_colors: ["#111111"],
  auto_style: false,
  layouts: {
    "4x3": {text: null, stickers: [], focus_x: 0.4, focus_y: 0.6, background_scale: 1.5},
    "16x9": {text: null, stickers: [], focus_x: 0.5, focus_y: 0.5, background_scale: 1},
  },
};
const key = taskDraftKey(task);
state.drafts.set(key, {
  updated_at: 2_000,
  selected_timestamp: 12.5,
  settings,
  previews: {"4x3": {media_token: "stale-browser-token"}},
  disk_saved: true,
});
mergeDiskDrafts([{
  relative_path: task.relative_path,
  updated_at: 1_999,
  selected_timestamp: 12.5,
  active: true,
  settings,
  previews: {"4x3": {media_token: "fresh-disk-token", placements: []}},
}], state.workspaceConfig.root);
if (taskDraft(task).previews["4x3"]?.media_token !== "fresh-disk-token") {
  throw new Error("相同设置的较新本地草稿错误丢弃了有效磁盘预览");
}

state.drafts.set(key, {
  ...taskDraft(task),
  updated_at: 3_000,
  selected_timestamp: 13.0,
});
mergeDiskDrafts([{
  relative_path: task.relative_path,
  updated_at: 2_999,
  selected_timestamp: 12.5,
  active: true,
  settings,
  previews: {"4x3": {media_token: "wrong-frame-token", placements: []}},
}], state.workspaceConfig.root);
        if (Object.keys(taskDraft(task).previews).length) {
          throw new Error("选中帧变化后仍错误复用了旧磁盘预览");
        }

        state.drafts.set(key, {
          ...taskDraft(task),
          updated_at: 4_000,
          previews: {"4x3": {media_token: "orphaned-process-token", placements: []}},
          active: true,
        });
        mergeDiskDrafts([], state.workspaceConfig.root);
        if (Object.keys(taskDraft(task).previews).length) {
          throw new Error("没有磁盘草稿时仍恢复了旧服务的预览令牌");
        }
        if (taskDraft(task).settings.title !== "已保存标题") {
          throw new Error("清理失效预览时丢失了用户设置");
        }
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证预览图层切换")
    def test_interactive_preview_uses_preloaded_background_layer(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const classList = {add(){}, remove(){}, toggle(){}, contains(){ return false; }};
const removed = [];
const settled = {
  hidden: true,
  dataset: {},
  style: {removeProperty(name){ removed.push(`settled:${name}`); }},
};
const background = {
  hidden: true,
  dataset: {},
  style: {removeProperty(name){ delete this[name]; }},
  complete: true,
  naturalWidth: 1440,
  addEventListener(){},
};
Object.assign(elements, {
  "cover-preview": settled,
  "cover-background-preview": background,
  "cover-overlay": {classList},
  "cover-frame": {classList, setAttribute(){}},
});
state.tasks = [{id: "layer-task", candidates: [{}]}];
state.activeTaskId = "layer-task";
const settings = defaultSettings(state.tasks[0]);
settings.layouts["4x3"].background_scale = 1.6;
settings.layouts["4x3"].focus_x = 0.25;
settings.layouts["4x3"].focus_y = 0.75;
state.settings.set("layer-task", settings);
const preview = {
  media_token: "settled-token",
  background_media_token: "background-token",
};
state.preview = preview;
showSettledPreview(preview);
if (settled.hidden || !background.hidden) throw new Error("静态预览图层状态错误");
showInteractivePreview(preview);
if (!settled.hidden || background.hidden) throw new Error("拖动时没有切换到纯背景图层");
if (!background.style.transform.includes("scale(1.6)")) throw new Error("纯背景没有实时缩放");
if (settled.style.transform) throw new Error("已经放大的成品预览被二次缩放");
if (!previewHasContent()) throw new Error("交互图层被误判为空预览");
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_element_drag_does_not_switch_to_background_only_preview(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)

        interaction = script.split(
            "function beginElementInteraction(", 1
        )[1].split("function renderCoverOverlay(", 1)[0]
        self.assertNotIn("showInteractivePreview(state.preview)", interaction)
        self.assertIn("文字和贴图交互始终保留完整成品预览", interaction)
        self.assertIn("setElementEditingLayer(true)", interaction)
        self.assertIn("preview-editing", script)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to verify preview loading")
    def test_preview_refresh_keeps_existing_cover_and_delays_first_loader(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const classList = { add(){}, remove(){}, toggle(){}, contains(){ return false; } };
Object.assign(elements, {
  "cover-preview": {hidden: false, src: "old.jpg"},
  "preview-loader": {hidden: true},
  "preview-state": {textContent: "old"},
  "preview-empty": {hidden: true, textContent: ""},
  "cover-overlay": {classList, replaceChildren(){}, removeAttribute(){}},
  "status-text": {textContent: ""},
  "status-detail": {textContent: "", title: ""},
  "status-dot": {classList},
});
state.preview = {media_token: "old-token", width: 1440, height: 1080};
state.activeTaskId = "task-1";
state.ratio = "4x3";
const task = {id: "task-1", candidates: [{}]};
activeTask = () => task;
previewPayload = () => ({});
setStatus = () => {};

async function runProbe() {
  let rejectRequest;
  api = () => new Promise((resolve, reject) => { rejectRequest = reject; });
  const pending = refreshPreview();
  if (!elements["preview-loader"].hidden) {
    throw new Error("existing preview displayed the loader");
  }
  rejectRequest(new Error("simulated preview failure"));
  await pending.then(
    () => { throw new Error("failed preview request unexpectedly resolved"); },
    () => {},
  );
  if (!elements["preview-loader"].hidden) throw new Error("loader stayed visible after failure");
  if (elements["cover-preview"].hidden) throw new Error("existing preview was cleared after failure");
  const stateText = elements["preview-state"].textContent;
  if (!stateText.startsWith("1440") || !stateText.includes("1080")) {
    throw new Error("previous preview dimensions were not restored");
  }

  state.preview = null;
  elements["cover-preview"].hidden = true;
  elements["preview-loader"].hidden = true;
  state.previewRequestId += 1;
  beginPreviewLoading(state.previewRequestId);
  if (!elements["preview-loader"].hidden) throw new Error("first loader appeared immediately");
  await new Promise((resolve) => setTimeout(resolve, PREVIEW_LOADER_DELAY_MS + 30));
  if (elements["preview-loader"].hidden) throw new Error("slow first request never showed loader");
  finishPreviewLoading(state.previewRequestId);
  if (!elements["preview-loader"].hidden) throw new Error("finished loader stayed visible");
}

runProbe().catch((error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){},setTimeout,clearTimeout};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证队列排序行为")
    def test_queue_sorting_orders_tasks_and_keeps_active_task(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
state.tasks = [
  {id:"a", relative_path:"10_片段.mp4", folder_created_at:100, source_created_at:100},
  {id:"b", relative_path:"2_片段.mp4", folder_created_at:200, source_created_at:50},
  {id:"c", relative_path:"11_片段.mp4", folder_created_at:200, source_created_at:80},
  {id:"d", relative_path:"缺失时间.mp4", folder_created_at:0, source_created_at:0},
];
state.activeTaskId = "b";
state.queueSort = "folder_created_desc";
sortTasks();
if (state.tasks.map((item) => item.id).join(",") !== "c,b,a,d") {
  throw new Error("最新创建排序错误");
}
state.queueSort = "name_asc";
sortTasks();
if (state.tasks.slice(0, 3).map((item) => item.id).join(",") !== "b,a,c") {
  throw new Error("名称自然排序错误");
}
if (activeTask()?.id !== "b") throw new Error("排序后当前任务发生变化");
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证贴图库筛选行为")
    def test_sticker_filter_matches_streamer_name_and_relative_path(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
const stickerProbeAssets = [
  {id:"1", name:"开心", group:"泽音melody", relative_path:"泽音melody/日常/开心.png"},
  {id:"2", name:"震惊", group:"星瞳", relative_path:"星瞳/游戏/震惊.png"},
];
if (filterStickerAssets(stickerProbeAssets, "泽音melody", "").length !== 1) {
  throw new Error("主播分组筛选错误");
}
if (filterStickerAssets(stickerProbeAssets, "", "星瞳")[0]?.id !== "2") {
  throw new Error("主播名称搜索错误");
}
if (filterStickerAssets(stickerProbeAssets, "", "游戏")[0]?.id !== "2") {
  throw new Error("相对路径搜索错误");
}
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证手动文字行行为")
    def test_manual_copy_lines_keep_colors_strokes_and_layouts_in_sync(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
state.options = {palettes:[{
  key:"test", emphasis_color:"#ff0000", emphasis_stroke_color:"#ffffff",
  neutral_color:"#eeeeee", neutral_stroke_color:"#111111", stroke_color:"#111111",
}]};
const task = {id:"manual", title:"测试", template_key:"headline", palette_key:"test"};
state.tasks = [task];
state.activeTaskId = task.id;
const settings = defaultSettings(task);
settings.copy_lines = ["第一行", "第二行"];
settings.line_colors = ["#aaaaaa", "#bbbbbb"];
settings.line_stroke_colors = ["#111111", "#222222"];
settings.layouts["4x3"].text = [
  {x:0.10, y:0.20, scale:1, font_size:100},
  {x:0.30, y:0.40, scale:1, font_size:80},
];
settings.layouts["16x9"].text = [
  {x:0.12, y:0.22, scale:1, font_size:100},
  {x:0.32, y:0.42, scale:1, font_size:80},
];
state.settings.set(task.id, settings);
if (appendManualCopyLine(settings) !== 2) throw new Error("首次新增文字行失败");
const oldLayout = JSON.stringify(settings.layouts["4x3"].text);
if (JSON.stringify(settings.layouts["16x9"].text) !== JSON.stringify([
  {x:0.12, y:0.22, scale:1, font_size:100},
  {x:0.32, y:0.42, scale:1, font_size:80},
])) throw new Error("新增空行改变了旧布局");
if (!updateManualCopyLine(settings, 2, "新增内容")) throw new Error("新增文字内容失败");
if (settings.layouts["4x3"].text.length !== 3) throw new Error("新增可见行未建立布局");
if (settings.layouts["4x3"].text[0].x !== 0.10
    || settings.layouts["4x3"].text[0].font_size !== 100) {
  throw new Error("新增文字破坏了旧行位置或字号");
}
const largeLineSettings = defaultSettings(task);
largeLineSettings.copy_lines = ["朱鹮"];
largeLineSettings.layouts["4x3"].text = [
  {x:0.30, y:0.55, scale:1, font_size:240},
];
appendManualCopyLine(largeLineSettings);
updateManualCopyLine(largeLineSettings, 1, "音音");
const largeTransforms = largeLineSettings.layouts["4x3"].text;
if (largeTransforms[0].y !== 0.55 || largeTransforms[0].font_size !== 240) {
  throw new Error("新增大字行改变了旧文字布局");
}
if (largeTransforms[1].y + 0.25 >= largeTransforms[0].y) {
  throw new Error("新增大字行与旧文字发生重叠");
}
if (!updateManualCopyLine(settings, 2, "")) throw new Error("清空文字内容失败");
if (JSON.stringify(settings.layouts["4x3"].text) !== oldLayout) {
  throw new Error("清空新增文字没有恢复旧布局");
}
for (let index = 0; index < 5; index += 1) {
  if (appendManualCopyLine(settings) < 0) throw new Error("未达到八行就拒绝添加");
}
if (settings.copy_lines.length !== 8) throw new Error("未添加到八行");
if (settings.line_colors.length !== 8 || settings.line_stroke_colors.length !== 8) {
  throw new Error("颜色或描边数组未同步扩展");
}
if (appendManualCopyLine(settings) !== -1) throw new Error("第九行未被拒绝");
if (!removeManualCopyLine(settings, 3)) throw new Error("删除文字行失败");
if (settings.copy_lines.length !== 7 || settings.line_colors.length !== 7
    || settings.line_stroke_colors.length !== 7) {
  throw new Error("删除后文字、颜色与描边数量不一致");
}
if (settings.layouts["4x3"].text.length !== 2) throw new Error("删除空行改变了文字布局");
if (!updateManualCopyLine(settings, 2, "再次加入")) throw new Error("重新加入文字失败");
if (!removeManualCopyLine(settings, 2)) throw new Error("删除可见文字行失败");
if (settings.layouts["4x3"].text.length !== 2) throw new Error("删除文字行后旧布局错误");
while (settings.copy_lines) removeManualCopyLine(settings, 0);
if (settings.line_colors !== null || settings.line_stroke_colors !== null) {
  throw new Error("删除全部文字后样式数组未清空");
}
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证 AI 候选和 role 配色行为")
    def test_ai_copy_candidate_selection_and_role_switch_apply_palette_colors(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
state.options = {palettes:[{
  key:"test", context_color:"#ffe438", context_stroke_color:"#111111",
  quote_color:"#16d8ed", quote_stroke_color:"#ffffff",
  emphasis_color:"#ff4433", emphasis_stroke_color:"#ffffff",
  neutral_color:"#ffffff", neutral_stroke_color:"#111111", stroke_color:"#111111",
}]};
const task = {id:"ai-copy", title:"测试", template_key:"headline", palette_key:"test"};
const settings = defaultSettings(task);
const candidate = {
  key:"ai-2", label:"Terra 选择", reason:"反差完整", template_key:"dialog", palette_key:"test",
  lines:[
    {text:"主题背景", role:"context"},
    {text:"朋友原话", role:"quote"},
    {text:"真正后果", role:"emphasis"},
  ],
};
applyCopyCandidate(settings, candidate);
if (settings.selected_copy_candidate_key !== "ai-2") throw new Error("候选未被选中");
if (settings.line_roles.join("|") !== "context|quote|emphasis") throw new Error("候选 role 未应用");
if (settings.line_colors.join("|") !== "#ffe438|#16d8ed|#ff4433") throw new Error("role 文字色映射错误");
if (settings.line_stroke_colors.join("|") !== "#111111|#ffffff|#ffffff") throw new Error("role 描边映射错误");
if (!setLineRole(settings, 1, "neutral")) throw new Error("role 切换失败");
if (settings.line_roles[1] !== "neutral" || settings.line_colors[1] !== "#ffffff"
    || settings.line_stroke_colors[1] !== "#111111") {
  throw new Error("role 切换后没有同步颜色和描边");
}
"""
        result = subprocess.run(
            ["node", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证标题编辑与异步推荐状态保护")
    def test_title_edit_preserves_copy_and_ai_generation_requires_explicit_click(self) -> None:
        with self.client.get("/static/app.js") as response:
            script = response.get_data(as_text=True)
        probe = r"""
function eventTarget(value = "") {
  const handlers = new Map();
  return {
    value,
    addEventListener(type, handler) { handlers.set(type, handler); },
    dispatch(type) { return handlers.get(type)?.(); },
  };
}

window.clearTimeout = clearTimeout;
window.setTimeout = (callback) => {
  Promise.resolve().then(callback);
  return 1;
};
elements["title-input"] = eventTarget("旧投稿标题");
elements["generate-ai-copy"] = eventTarget();
state.options = {
  templates: [{key: "headline"}, {key: "dialog"}],
  palettes: [{
    key: "test", context_color: "#ffe438", context_stroke_color: "#111111",
    quote_color: "#16d8ed", quote_stroke_color: "#ffffff",
    emphasis_color: "#ff4433", emphasis_stroke_color: "#ffffff",
    neutral_color: "#ffffff", neutral_stroke_color: "#111111", stroke_color: "#111111",
  }],
};
const task = {
  id: "title-state", title: "旧投稿标题", template_key: "headline", palette_key: "test",
};
state.tasks = [task];
state.activeTaskId = task.id;
const settings = defaultSettings(task);
settings.auto_style = false;
settings.copy_lines = ["手工主题", "AI 爆点"];
settings.line_roles = ["context", "emphasis"];
settings.line_colors = ["#123456", "#abcdef"];
settings.line_stroke_colors = ["#111111", "#ffffff"];
settings.copy_candidates = [{
  key: "ai-2", label: "已选 AI", reason: "保留当前候选",
  template_key: "dialog", palette_key: "test",
  lines: [{text: "手工主题", role: "context"}, {text: "AI 爆点", role: "emphasis"}],
}];
settings.selected_copy_candidate_key = "ai-2";
settings.layouts["4x3"].text = [
  {x: 0.1, y: 0.2, scale: 1, font_size: 100},
  {x: 0.3, y: 0.4, scale: 1, font_size: 120},
];
state.settings.set(task.id, settings);

const calls = [];
const pendingLayouts = [];
api = (path, options = {}) => {
  calls.push({path, body: options.body ? JSON.parse(options.body) : null});
  if (path === "/api/layout-variants") {
    return new Promise((resolve) => pendingLayouts.push(resolve));
  }
  if (path === `/api/tasks/${task.id}/copy-variants`) {
    return Promise.resolve({
      source: "fallback", selected_index: 0, warning: "",
      candidates: [{
        key: "ai-click", label: "显式生成", reason: "按钮触发",
        template_key: "dialog", palette_key: "test",
        lines: [{text: "按钮生成文案", role: "emphasis"}],
      }],
    });
  }
  throw new Error(`unexpected API call: ${path}`);
};
renderTaskList = () => {};
renderLayoutVariants = () => {};
renderCopyCandidates = () => {};
renderCopyLines = () => {};
renderInspector = () => {};
persistTaskDraft = () => {};
refreshPreview = async () => {};
setBusy = () => {};
setStatus = () => {};

bindCopyRecommendationControls();
elements["title-input"].value = "新投稿标题";
elements["title-input"].dispatch("input");
await Promise.resolve();
if (calls.length !== 1 || calls[0].path !== "/api/layout-variants") {
  throw new Error("标题编辑没有只刷新推荐排版");
}
if (calls[0].body.title !== "新投稿标题") throw new Error("推荐排版没有使用新标题");
if (settings.copy_lines.join("|") !== "手工主题|AI 爆点") throw new Error("标题编辑覆盖了文案");
if (settings.line_roles.join("|") !== "context|emphasis") throw new Error("标题编辑覆盖了 role");
if (settings.line_colors.join("|") !== "#123456|#abcdef") throw new Error("标题编辑覆盖了颜色");
if (settings.line_stroke_colors.join("|") !== "#111111|#ffffff") {
  throw new Error("标题编辑覆盖了描边");
}
if (settings.selected_copy_candidate_key !== "ai-2") throw new Error("标题编辑取消了已选候选");
if (settings.layouts["4x3"].text[1].font_size !== 120) throw new Error("标题编辑清空了文字布局");
pendingLayouts.shift()({variants: [{
  key: "layout-1", label: "新推荐", reason: "仅刷新候选",
  template_key: "headline", palette_key: "test",
  lines: [{text: "确定性推荐", role: "neutral"}],
}]});
await Promise.resolve();
await Promise.resolve();
if (settings.copy_lines.join("|") !== "手工主题|AI 爆点") {
  throw new Error("确定性推荐刷新覆盖了现有文案");
}

settings.auto_style = true;
const staleLayout = loadLayoutVariants(task, {applyRecommended: true});
await Promise.resolve();
const aiCandidate = {
  key: "ai-new", template_key: "dialog", palette_key: "test",
  lines: [{text: "后来选择的 AI 文案", role: "quote"}],
};
applyCopyCandidate(settings, aiCandidate);
settings.line_colors = ["#654321"];
settings.line_stroke_colors = ["#fedcba"];
pendingLayouts.shift()({variants: [{
  key: "stale", label: "旧推荐", reason: "不得落地",
  template_key: "headline", palette_key: "test",
  lines: [{text: "过期推荐文案", role: "neutral"}],
}]});
await staleLayout;
if (settings.copy_lines[0] !== "后来选择的 AI 文案") throw new Error("旧推荐覆盖了 AI 文案");
if (settings.line_roles[0] !== "quote") throw new Error("旧推荐覆盖了 AI role");
if (settings.line_colors[0] !== "#654321") throw new Error("旧推荐覆盖了 AI 颜色");
if (settings.line_stroke_colors[0] !== "#fedcba") throw new Error("旧推荐覆盖了 AI 描边");
if (settings.selected_copy_candidate_key !== "ai-new") throw new Error("旧推荐取消了 AI 候选");

if (calls.some((item) => item.path.includes("/copy-variants"))) {
  throw new Error("点击按钮前发生了 AI 请求");
}
await elements["generate-ai-copy"].dispatch("click");
if (calls.filter((item) => item.path.includes("/copy-variants")).length !== 1) {
  throw new Error("AI 请求没有且仅由按钮显式触发一次");
}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-"],
            input="global.window={addEventListener(){}};\n" + script + probe,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_layout_variants_follow_the_submitted_title(self) -> None:
        response = self.client.post(
            "/api/layout-variants",
            json={
                "title": (
                    "【泽音】下飞机遇到狂风，裙子当场被吹飞😱"
                    "“玛丽莲？别搞笑了，没有梦幻动作好吗！”"
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        variants = response.get_json()["variants"]
        self.assertEqual(len(variants), 3)
        self.assertEqual(variants[0]["template_key"], "dialog")
        self.assertEqual(len(variants[0]["lines"]), 4)
        self.assertEqual(
            self.client.post("/api/layout-variants", json={"title": ""}).status_code,
            400,
        )
        oversized = self.client.post("/api/layout-variants", json={"title": "长" * 501})
        self.assertEqual(oversized.status_code, 400)
        self.assertIn("500", oversized.get_json()["error"])

    def test_task_copy_variants_run_only_on_explicit_request_and_hide_paths(self) -> None:
        clip_path = (self.clips / "01_司机回头.mp4").resolve()
        subtitle_path = (self.root / "最终校对.srt").resolve()
        subtitle_path.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\n朋友说送我一个大作\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\n点开以后发现本来就是免费游戏\n",
            encoding="utf-8",
        )
        manifest_path = self.root / "文案任务.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tasks": [
                        {
                            "id": "01",
                            "clip_timebase": "source_video_seconds",
                            "source_segment_count": 1,
                            "clip_start_seconds": 0,
                            "clip_end_seconds": 60,
                            "slice_anchor": 8,
                            "slice_anchor_source": "语义复核",
                            "cover_anchor_seconds": 8,
                            "cover_anchor_media_path": str(clip_path),
                            "editorial_interest_score": 5,
                            "editorial_interest_reason": "说好送大作却是免费游戏，后果反差完整",
                            "publish_title": "【测试主播】朋友说送大作，点开却是免费游戏",
                            "original_slice_path": str(clip_path),
                            "corrected_srt_path": str(subtitle_path),
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        calls: list[tuple[str, dict[str, object]]] = []

        def runner(prompt: str, **kwargs: object) -> str:
            calls.append((prompt, kwargs))
            response = _copy_generation_response() if len(calls) == 1 else _copy_review_response()
            return json.dumps(response, ensure_ascii=False)

        self.app.config["COPY_RECOMMENDATION_RUNNER"] = runner
        scan = self.client.post(
            "/api/workspace/scan",
            json={
                "root": str(self.clips),
                "cache_dir": str(self.cache),
                "output_dir": str(self.output),
                "manifest_json_path": str(manifest_path),
            },
        )
        task = next(
            item for item in scan.get_json()["tasks"] if item["filename"] == clip_path.name
        )
        self.assertEqual(calls, [])

        rejected = self.client.post(
            f"/api/tasks/{task['id']}/copy-variants",
            json={"srt_path": str(subtitle_path)},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(calls, [])

        response = self.client.post(
            f"/api/tasks/{task['id']}/copy-variants",
            json={"title": "【测试主播】手动调整后的当前标题"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["source"], "ai")
        self.assertEqual(payload["selected_index"], 1)
        self.assertEqual(payload["selected_key"], "ai-2")
        self.assertEqual(len(payload["candidates"]), 3)
        self.assertEqual([item[1]["reasoning_stage"] for item in calls], ["analysis", "review"])
        self.assertIn("手动调整后的当前标题", calls[0][0])
        response_text = response.get_data(as_text=True)
        self.assertNotIn(str(clip_path), response_text)
        self.assertNotIn(str(subtitle_path), response_text)
        self.assertNotIn(str(manifest_path), response_text)

    def test_task_copy_variants_without_srt_never_calls_runner(self) -> None:
        tasks = self._scan()
        task = tasks[0]
        self.app.config["COPY_RECOMMENDATION_RUNNER"] = (
            lambda *_args, **_kwargs: self.fail("没有可靠 SRT 时不得调用 LLM")
        )

        response = self.client.post(
            f"/api/tasks/{task['id']}/copy-variants",
            json={"title": "当前标题"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["source"], "fallback")
        self.assertEqual(len(payload["candidates"]), 3)
        self.assertIn("未调用 AI", payload["warning"])

    def test_sticker_library_exposes_ids_but_not_local_paths(self) -> None:
        response = self.client.get("/api/stickers")

        self.assertEqual(response.status_code, 200)
        assets = response.get_json()["assets"]
        summary = response.get_json()["summary"]
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["name"], "震惊")
        self.assertEqual(summary["asset_count"], 1)
        self.assertEqual(summary["group_count"], 1)
        self.assertTrue(summary["available"])
        self.assertNotIn(str(self.root), str(assets[0]))
        self.assertNotIn(str(self.root), str(summary))
        with self.client.get(f"/api/stickers/{assets[0]['id']}/image") as image_response:
            self.assertEqual(image_response.status_code, 200)
            self.assertEqual(image_response.mimetype, "image/png")
        self.assertEqual(self.client.get("/api/stickers/not-found/image").status_code, 404)

    def test_imported_local_image_is_registered_without_exposing_path(self) -> None:
        image_bytes = io.BytesIO()
        Image.new("RGBA", (120, 80), (10, 220, 120, 180)).save(
            image_bytes,
            format="PNG",
        )
        response = self.client.post(
            "/api/stickers/import",
            data={"file": (io.BytesIO(image_bytes.getvalue()), "本地装饰.png")},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["asset"]["group"], "我的导入")
        self.assertIn("本地装饰", payload["asset"]["name"])
        self.assertNotIn(str(self.root), str(payload))
        with self.client.get(
            f"/api/stickers/{payload['asset']['id']}/image"
        ) as imported:
            self.assertEqual(imported.status_code, 200)
            self.assertEqual(imported.mimetype, "image/png")

        invalid = self.client.post(
            "/api/stickers/import",
            data={"file": (io.BytesIO(b"not-image"), "伪装.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid.status_code, 400)

    def test_candidate_endpoint_and_media_token(self) -> None:
        task = self._ready_task()
        candidate = task["candidates"][0]

        with self.client.get(f"/api/media/{candidate['token']}") as media:
            self.assertEqual(media.status_code, 200)
            self.assertEqual(media.mimetype, "image/jpeg")
        self.assertNotIn(str(self.root), str(task))

    def test_video_timeline_metadata_and_exact_frame_endpoints(self) -> None:
        task = self._ready_task()
        metadata = VideoMetadata(
            str(self.clips / task["filename"]),
            72.5,
            1920,
            1080,
            30.0,
        )
        exact_frame = FrameCandidate(
            path=str(self.frame),
            timestamp=21.4,
            score=81.0,
            metrics=self.candidate.metrics,
        )
        with patch(f"{WORKSPACE_MODULE}.probe_video", return_value=metadata):
            metadata_response = self.client.get(
                f"/api/tasks/{task['id']}/video-metadata"
            )
        with patch(
            f"{WORKSPACE_MODULE}.extract_frame_at_timestamp",
            return_value=(exact_frame, metadata),
        ):
            selected_response = self.client.post(
                f"/api/tasks/{task['id']}/select-timestamp",
                json={"timestamp": 21.4},
            )

        self.assertEqual(metadata_response.status_code, 200)
        self.assertEqual(metadata_response.get_json()["metadata"]["duration"], 72.5)
        self.assertEqual(selected_response.status_code, 200)
        self.assertEqual(
            selected_response.get_json()["task"]["selected_timestamp"],
            21.4,
        )
        self.assertEqual(
            self.client.post(
                f"/api/tasks/{task['id']}/select-timestamp",
                json={"timestamp": -1},
            ).status_code,
            400,
        )

    def test_unknown_task_and_path_like_token_return_404(self) -> None:
        self._scan()
        self.assertEqual(self.client.patch("/api/tasks/not-found", json={}).status_code, 404)
        self.assertEqual(self.client.get("/api/media/..%2F..%2FWindows").status_code, 404)

    def test_remove_task_updates_queue_without_deleting_video(self) -> None:
        tasks = self._scan()
        source = self.clips / tasks[0]["filename"]

        response = self.client.delete(f"/api/tasks/{tasks[0]['id']}")

        self.assertEqual(response.status_code, 200)
        remaining = response.get_json()["tasks"]
        self.assertEqual([task["id"] for task in remaining], [tasks[1]["id"]])
        self.assertTrue(source.is_file())
        self.assertEqual(self.client.get("/api/tasks").get_json()["tasks"], remaining)
        self.assertEqual(
            self.client.delete(f"/api/tasks/{tasks[0]['id']}").status_code,
            404,
        )

    def test_preview_renders_selected_frame(self) -> None:
        task = self._ready_task()
        response = self.client.post(
            f"/api/tasks/{task['id']}/preview",
            json={"canvas_key": "4x3", "title": "【泽音】音音当场震惊"},
        )

        self.assertEqual(response.status_code, 200)
        preview = response.get_json()["preview"]
        self.assertEqual((preview["width"], preview["height"]), (1440, 1080))
        with self.client.get(f"/api/media/{preview['media_token']}") as image_response:
            self.assertEqual(image_response.status_code, 200)

    def test_preview_applies_direct_text_and_sticker_layout(self) -> None:
        task = self._ready_task()
        asset_id = self.client.get("/api/stickers").get_json()["assets"][0]["id"]
        response = self.client.post(
            f"/api/tasks/{task['id']}/preview",
            json={
                "canvas_key": "4x3",
                "title": "【泽音】拖动标题测试",
                "template_key": "dialog",
                "copy_lines": ["标题"],
                "line_stroke_colors": ["#ffffff"],
                "layouts": {
                    "4x3": {
                        "text": [
                            {
                                "x": 0.20,
                                "y": 0.30,
                                "scale": 0.8,
                                "font_size": 104,
                                "center_x": True,
                            }
                        ],
                        "stickers": [
                            {
                                "asset_id": asset_id,
                                "x": 0.70,
                                "y": 0.25,
                                "width": 0.12,
                                "center_y": True,
                            }
                        ],
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        preview = response.get_json()["preview"]
        text_box = preview["placements"][0]["box"]
        self.assertAlmostEqual((text_box[0] + text_box[2]) / 2, 1440 / 2, delta=2)
        self.assertAlmostEqual(preview["placements"][0]["box"][1], 1080 * 0.30, delta=2)
        self.assertEqual(preview["placements"][0]["font_size"], 104)
        self.assertEqual(preview["placements"][0]["stroke_color"], "#ffffff")
        self.assertAlmostEqual(preview["stickers"][0]["box"][0], 1440 * 0.70, delta=2)
        sticker_box = preview["stickers"][0]["box"]
        self.assertAlmostEqual((sticker_box[1] + sticker_box[3]) / 2, 1080 / 2, delta=2)
        self.assertIn("background_media_token", preview)
        with self.client.get(f"/api/media/{preview['background_media_token']}") as base:
            self.assertEqual(base.status_code, 200)

    def test_render_options_support_ratio_specific_focus(self) -> None:
        library = self.app.extensions["sticker_library"]
        payload = {
            "focus_x": 0.25,
            "focus_y": 0.35,
            "layouts": {
                "4x3": {
                    "focus_x": 0.65,
                    "focus_y": 0.75,
                    "background_scale": 1.6,
                },
                "16x9": {},
            },
        }

        compact = _render_options(payload, "4x3", library)
        wide = _render_options(payload, "16x9", library)

        self.assertEqual((compact["focus_x"], compact["focus_y"]), (0.65, 0.75))
        self.assertEqual(compact["background_scale"], 1.6)
        self.assertEqual((wide["focus_x"], wide["focus_y"]), (0.25, 0.35))
        self.assertEqual(wide["background_scale"], 1.0)
        with self.assertRaisesRegex(ApiError, "focus_x"):
            _render_options(
                {"layouts": {"4x3": {"focus_x": 1.1}}},
                "4x3",
                library,
            )
        with self.assertRaisesRegex(ApiError, "background_scale"):
            _render_options(
                {"layouts": {"4x3": {"background_scale": 2.6}}},
                "4x3",
                library,
            )

    def test_preview_is_saved_to_disk_and_restored_after_service_restart(self) -> None:
        task = self._ready_task()
        draft_revision = 1_800_000_000_500
        response = self.client.post(
            f"/api/tasks/{task['id']}/preview",
            json={
                "canvas_key": "4x3",
                "draft_updated_at": draft_revision,
                "title": "磁盘草稿恢复测试",
                "template_key": "dialog",
                "palette_key": "classic",
                "copy_lines": ["第一行", "第二行"],
                "line_colors": ["#ffffff", "#d06e95"],
                "line_stroke_colors": ["#111111", "#ffffff"],
                "line_roles": ["context", "emphasis"],
                "copy_candidates": [
                    {
                        "key": "ai-1",
                        "label": "AI 候选",
                        "reason": "保存到磁盘草稿",
                        "template_key": "dialog",
                        "palette_key": "classic",
                        "lines": [
                            {"text": "第一行", "role": "context"},
                            {"text": "第二行", "role": "emphasis"},
                        ],
                    }
                ],
                "selected_copy_candidate_key": "ai-1",
                "copy_warning": "测试 warning",
                "layouts": {
                    "4x3": {
                        "focus_x": 0.35,
                        "focus_y": 0.65,
                        "background_scale": 1.4,
                        "text": None,
                        "stickers": [],
                    },
                    "16x9": {
                        "focus_x": 0.5,
                        "focus_y": 0.5,
                        "background_scale": 1.0,
                        "text": None,
                        "stickers": [],
                    },
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        saved = response.get_json()
        self.assertTrue(saved["draft_saved"])
        draft_path = Path(saved["draft_path"])
        self.assertTrue(draft_path.is_file())
        self.assertTrue((draft_path.parent / task["id"] / "4x3.jpg").is_file())
        self.assertTrue((draft_path.parent / task["id"] / "4x3-background.jpg").is_file())
        draft_payload = json.loads(draft_path.read_text(encoding="utf-8"))
        self.assertGreater(draft_payload["updated_at"], 1_000_000_000_000)

        stale_response = self.client.post(
            f"/api/tasks/{task['id']}/preview",
            json={
                "canvas_key": "4x3",
                "title": "不应覆盖的新预览",
                "draft_updated_at": draft_revision - 1000,
            },
        )
        self.assertEqual(stale_response.status_code, 409)
        preserved_payload = json.loads(draft_path.read_text(encoding="utf-8"))
        preserved_entry = preserved_payload["tasks"][task["relative_path"]]
        self.assertEqual(preserved_entry["settings"]["title"], "磁盘草稿恢复测试")
        self.assertEqual(preserved_entry["client_updated_at"], draft_revision)

        fresh_app = create_app({
            "TESTING": True,
            "STICKER_DIR": str(self.sticker_root),
            "IMPORTED_STICKER_DIR": str(self.root / "导入贴图"),
        })
        fresh_client = _bootstrapped_client(fresh_app)
        restored_response = fresh_client.post(
            "/api/workspace/scan",
            json={
                "root": str(self.clips),
                "cache_dir": str(self.cache),
                "output_dir": str(self.output),
            },
        )
        self.assertEqual(restored_response.status_code, 200)
        restored = restored_response.get_json()
        self.assertEqual(Path(restored["draft_path"]), draft_path)
        self.assertEqual(len(restored["drafts"]), 1)
        disk_draft = restored["drafts"][0]
        self.assertTrue(disk_draft["active"])
        self.assertGreater(disk_draft["updated_at"], 1_000_000_000_000)
        self.assertEqual(disk_draft["settings"]["copy_lines"], ["第一行", "第二行"])
        self.assertEqual(disk_draft["settings"]["line_roles"], ["context", "emphasis"])
        self.assertEqual(disk_draft["settings"]["copy_candidates"][0]["key"], "ai-1")
        self.assertEqual(disk_draft["settings"]["selected_copy_candidate_key"], "ai-1")
        self.assertAlmostEqual(
            disk_draft["settings"]["layouts"]["4x3"]["background_scale"],
            1.4,
        )
        preview = disk_draft["previews"]["4x3"]
        self.assertIn("media_token", preview)
        self.assertIn("background_media_token", preview)
        with fresh_client.get(f"/api/media/{preview['media_token']}") as media_response:
            self.assertEqual(media_response.status_code, 200)
        restored_task = next(
            item for item in restored["tasks"] if item["relative_path"] == task["relative_path"]
        )
        self.assertEqual(len(restored_task["candidates"]), 1)
        self.assertTrue(restored_task["candidates"][0]["cached"])
        self.assertAlmostEqual(restored_task["selected_timestamp"], 8.5)

        active_response = fresh_client.post(
            f"/api/tasks/{restored_task['id']}/draft-active",
            json={},
        )
        self.assertEqual(active_response.status_code, 200)
        self.assertTrue(active_response.get_json()["saved"])

        source_path = self.clips / Path(task["relative_path"])
        source_path.write_bytes(source_path.read_bytes() + b"-replaced")
        invalidated_app = create_app({
            "TESTING": True,
            "STICKER_DIR": str(self.sticker_root),
            "IMPORTED_STICKER_DIR": str(self.root / "导入贴图"),
        })
        invalidated_client = _bootstrapped_client(invalidated_app)
        invalidated_response = invalidated_client.post(
            "/api/workspace/scan",
            json={
                "root": str(self.clips),
                "cache_dir": str(self.cache),
                "output_dir": str(self.output),
            },
        )
        self.assertEqual(invalidated_response.status_code, 200)
        self.assertEqual(invalidated_response.get_json()["drafts"], [])

    def test_each_disk_preview_is_bound_to_its_selected_frame(self) -> None:
        frame_b = self.root / "frame-b.jpg"
        Image.new("RGB", (1920, 1080), "#4b83d1").save(frame_b)
        candidate_b = FrameCandidate(
            path=str(frame_b),
            timestamp=17.25,
            score=91.0,
            metrics=self.candidate.metrics,
        )
        task = self._scan()[0]
        with patch(
            f"{WORKSPACE_MODULE}.extract_candidate_frames",
            return_value=[self.candidate, candidate_b],
        ):
            ready = self.client.post(
                f"/api/tasks/{task['id']}/candidates",
                json={"count": 4},
            ).get_json()["task"]

        first_preview = self.client.post(
            f"/api/tasks/{task['id']}/preview",
            json={"canvas_key": "4x3", "draft_updated_at": 1_800_000_100_000},
        )
        self.assertEqual(first_preview.status_code, 200)
        second_token = ready["candidates"][1]["token"]
        selected = self.client.post(
            f"/api/tasks/{task['id']}/select-frame",
            json={"media_token": second_token},
        )
        self.assertEqual(selected.status_code, 200)
        second_preview = self.client.post(
            f"/api/tasks/{task['id']}/preview",
            json={"canvas_key": "16x9", "draft_updated_at": 1_800_000_100_001},
        )
        self.assertEqual(second_preview.status_code, 200)

        fresh_app = create_app({
            "TESTING": True,
            "STICKER_DIR": str(self.sticker_root),
            "IMPORTED_STICKER_DIR": str(self.root / "导入贴图"),
        })
        fresh_client = _bootstrapped_client(fresh_app)
        restored = fresh_client.post(
            "/api/workspace/scan",
            json={
                "root": str(self.clips),
                "cache_dir": str(self.cache),
                "output_dir": str(self.output),
            },
        ).get_json()
        self.assertEqual(len(restored["drafts"]), 1)
        previews = restored["drafts"][0]["previews"]
        self.assertNotIn("4x3", previews)
        self.assertIn("16x9", previews)
        self.assertAlmostEqual(previews["16x9"]["selected_timestamp"], 17.25)

    def test_disk_drafts_are_isolated_for_multiple_videos(self) -> None:
        tasks = self._scan()
        for index, task in enumerate(tasks):
            candidate = FrameCandidate(
                path=str(self.frame),
                timestamp=8.5 + index,
                score=88.0 - index,
                metrics=self.candidate.metrics,
            )
            with patch(
                f"{WORKSPACE_MODULE}.extract_candidate_frames",
                return_value=[candidate],
            ):
                ready_response = self.client.post(
                    f"/api/tasks/{task['id']}/candidates",
                    json={"count": 4},
                )
            self.assertEqual(ready_response.status_code, 200)
            preview_response = self.client.post(
                f"/api/tasks/{task['id']}/preview",
                json={
                    "canvas_key": "4x3",
                    "title": f"视频 {index + 1} 的独立草稿",
                    "draft_updated_at": 1_800_000_001_000 + index,
                },
            )
            self.assertEqual(preview_response.status_code, 200)
            self.assertTrue(preview_response.get_json()["draft_saved"])

        draft_path = Path(
            self.client.post(
                "/api/workspace/scan",
                json={
                    "root": str(self.clips),
                    "cache_dir": str(self.cache),
                    "output_dir": str(self.output),
                },
            ).get_json()["draft_path"]
        )
        payload = json.loads(draft_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["tasks"]), 2)
        for index, task in enumerate(tasks):
            entry = payload["tasks"][task["relative_path"]]
            self.assertEqual(entry["settings"]["title"], f"视频 {index + 1} 的独立草稿")
            self.assertTrue((draft_path.parent / task["id"] / "4x3.jpg").is_file())
        self.assertEqual(payload["active_relative_path"], tasks[-1]["relative_path"])

    def test_save_uses_independent_layouts_for_both_ratios(self) -> None:
        task = self._ready_task()
        response = self.client.post(
            f"/api/tasks/{task['id']}/save",
            json={
                "canvases": ["4x3", "16x9"],
                "template_key": "dialog",
                "copy_lines": ["字"],
                "layouts": {
                    "4x3": {"text": [{"x": 0.10, "y": 0.20, "scale": 0.7}]},
                    "16x9": {"text": [{"x": 0.55, "y": 0.60, "scale": 0.7}]},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        outputs = {item["canvas_key"]: item for item in response.get_json()["outputs"]}
        self.assertAlmostEqual(outputs["4x3"]["placements"][0]["box"][0], 1440 * 0.10, delta=2)
        self.assertAlmostEqual(outputs["16x9"]["placements"][0]["box"][0], 1920 * 0.55, delta=2)

    def test_save_writes_both_canvas_files(self) -> None:
        task = self._ready_task()
        response = self.client.post(f"/api/tasks/{task['id']}/save", json={})

        self.assertEqual(response.status_code, 200)
        outputs = response.get_json()["outputs"]
        self.assertEqual({item["canvas_key"] for item in outputs}, {"4x3", "16x9"})
        self.assertTrue((self.output / "01_司机回头-4x3.jpg").is_file())
        self.assertTrue((self.output / "01_司机回头-16x9.jpg").is_file())

    def test_dual_ratio_save_uses_one_immutable_task_snapshot(self) -> None:
        task = self._ready_task()
        workspace = self.app.extensions["cover_workspace"]
        seen_titles: list[str] = []

        def mutate_after_first_render(*args, **kwargs):
            seen_titles.append(args[1])
            if len(seen_titles) == 1:
                workspace.update_task(task["id"], title="并发修改后的标题")
            return actual_render_cover(*args, **kwargs)

        with patch(
            f"{APP_MODULE}.render_cover",
            side_effect=mutate_after_first_render,
        ):
            response = self.client.post(
                f"/api/tasks/{task['id']}/save",
                json={
                    "title": "本次导出的固定标题",
                    "canvases": ["4x3", "16x9"],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            seen_titles,
            ["本次导出的固定标题", "本次导出的固定标题"],
        )
        self.assertEqual(
            workspace.get_task(task["id"]).title,
            "并发修改后的标题",
        )

    def test_save_validates_both_ratios_before_replacing_existing_outputs(self) -> None:
        task = self._ready_task()
        self.output.mkdir(parents=True, exist_ok=True)
        compact = self.output / "01_司机回头-4x3.jpg"
        wide = self.output / "01_司机回头-16x9.jpg"
        compact.write_bytes(b"old-compact")
        wide.write_bytes(b"old-wide")

        response = self.client.post(
            f"/api/tasks/{task['id']}/save",
            json={
                "canvases": ["4x3", "16x9"],
                "copy_lines": ["标题"],
                "layouts": {
                    "4x3": {"text": [{"x": 0.1, "y": 0.2, "scale": 1.0}]},
                    "16x9": {"text": [{"x": 2.0, "y": 0.2, "scale": 1.0}]},
                },
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(compact.read_bytes(), b"old-compact")
        self.assertEqual(wide.read_bytes(), b"old-wide")

    def test_save_rolls_back_when_second_ratio_render_fails(self) -> None:
        task = self._ready_task()
        self.output.mkdir(parents=True, exist_ok=True)
        compact = self.output / "01_司机回头-4x3.jpg"
        wide = self.output / "01_司机回头-16x9.jpg"
        compact.write_bytes(b"old-compact")
        wide.write_bytes(b"old-wide")

        def fail_wide_render(*args, **kwargs):
            if kwargs.get("canvas_key") == "16x9":
                raise RuntimeError(f"第二比例失败：{self.root}")
            return actual_render_cover(*args, **kwargs)

        with patch(f"{APP_MODULE}.render_cover", side_effect=fail_wide_render):
            response = self.client.post(f"/api/tasks/{task['id']}/save", json={})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], "处理失败，请查看服务日志")
        self.assertNotIn(str(self.root), response.get_data(as_text=True))
        self.assertEqual(compact.read_bytes(), b"old-compact")
        self.assertEqual(wide.read_bytes(), b"old-wide")
        self.assertEqual([path for path in self.output.iterdir() if path.name.startswith(".")], [])

    def test_batch_export_and_validation(self) -> None:
        tasks = self._scan()
        with patch(
            f"{WORKSPACE_MODULE}.extract_candidate_frames",
            return_value=[self.candidate],
        ):
            for task in tasks:
                self.client.post(f"/api/tasks/{task['id']}/candidates", json={})

        response = self.client.post("/api/export", json={"canvases": ["4x3"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 2)
        invalid = self.client.post("/api/export", json={"canvases": ["1x1"]})
        self.assertEqual(invalid.status_code, 400)

        oversized = self.client.post(
            "/api/export",
            json={"task_ids": [tasks[0]["id"]] * 101, "canvases": ["4x3"]},
        )
        self.assertEqual(oversized.status_code, 400)
        self.assertIn("100", oversized.get_json()["error"])

    def test_rejects_invalid_candidate_and_preview_parameters(self) -> None:
        task = self._scan()[0]
        invalid_count = self.client.post(f"/api/tasks/{task['id']}/candidates", json={"count": 0})
        self.assertEqual(invalid_count.status_code, 400)

        ready = self._ready_task()
        invalid_focus = self.client.post(
            f"/api/tasks/{ready['id']}/preview",
            json={"focus_x": 2.0},
        )
        self.assertEqual(invalid_focus.status_code, 400)
        invalid_layout = self.client.post(
            f"/api/tasks/{ready['id']}/preview",
            json={
                "copy_lines": ["标题"],
                "layouts": {"4x3": {"text": [{"x": 2.0, "y": 0.5, "scale": 1.0}]}},
            },
        )
        self.assertEqual(invalid_layout.status_code, 400)
        invalid_sticker = self.client.post(
            f"/api/tasks/{ready['id']}/preview",
            json={
                "layouts": {
                    "4x3": {
                        "stickers": [
                            {"asset_id": "unknown", "x": 0.2, "y": 0.2, "width": 0.2}
                        ]
                    }
                }
            },
        )
        self.assertEqual(invalid_sticker.status_code, 404)

    def test_rejects_oversized_text_invalid_colors_and_non_finite_numbers(self) -> None:
        task = self._ready_task()
        endpoint = f"/api/tasks/{task['id']}/preview"
        cases = (
            {"title": "长" * 501},
            {"copy_lines": ["字" * 121]},
            {"copy_lines": ["字"] * 9},
            {"copy_lines": ["标题"], "line_colors": ["d06e95"]},
            {"copy_lines": ["标题"], "line_stroke_colors": ["ffffff"]},
            {
                "copy_lines": ["第一行", "第二行"],
                "line_stroke_colors": ["#ffffff"],
            },
            {"line_stroke_colors": ["#ffffff"]},
            {
                "copy_lines": ["标题"],
                "layouts": {
                    "4x3": {"text": [{"x": 0.2, "y": 0.2, "scale": float("nan")}]}
                },
            },
            {
                "copy_lines": ["标题"],
                "layouts": {
                    "4x3": {
                        "text": [
                            {"x": 0.2, "y": 0.2, "scale": 1.0, "font_size": 321}
                        ]
                    }
                },
            },
            {
                "copy_lines": ["标题"],
                "layouts": {
                    "4x3": {
                        "text": [
                            {
                                "x": 0.2,
                                "y": 0.2,
                                "scale": 1.0,
                                "font_size": float("inf"),
                            }
                        ]
                    }
                },
            },
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post(endpoint, json=payload).status_code, 400)

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ApiError):
                _number_value({"scale": value}, "scale", minimum=0.45, maximum=2.0)


if __name__ == "__main__":
    unittest.main()
