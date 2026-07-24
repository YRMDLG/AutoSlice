"""AutoCover Flask API 测试。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app import ApiError, _number_value, _render_options, create_app
from autocover import API_VERSION, SERVICE_ID
from autocover.renderer import render_cover as actual_render_cover
from autocover.video import FrameCandidate, FrameMetrics
from autocover.workspace import DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR


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
        self.app = create_app({"TESTING": True, "STICKER_DIR": str(self.sticker_root)})
        self.client = self.app.test_client()

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

    def _ready_task(self) -> dict[str, object]:
        task = self._scan()[0]
        with patch("autocover.workspace.extract_candidate_frames", return_value=[self.candidate]):
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
            else:
                self.assertEqual(response.status_code, 404)

    def test_workbench_page_and_assets_are_available(self) -> None:
        with self.client.get("/") as page:
            page_content = page.get_data(as_text=True)
            self.assertEqual(page.status_code, 200)
            self.assertIn("AutoCover", page_content)
            self.assertIn('rel="icon" href="data:,"', page_content)
            self.assertIn('id="candidate-strip"', page_content)
            self.assertIn('id="palette-select"', page_content)
            self.assertIn('id="cover-overlay"', page_content)
            self.assertIn('id="layout-variants"', page_content)
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
        with self.client.get("/static/app.js") as script:
            self.assertEqual(script.status_code, 200)
            script_content = script.get_data(as_text=True)
            self.assertIn("/api/workspace/scan", script_content)
            self.assertIn("/api/layout-variants", script_content)
            self.assertIn("/api/stickers", script_content)
            self.assertIn("background_media_token", script_content)
            self.assertIn("default_output_dir", script_content)
            self.assertIn('const EXPECTED_API_VERSION = 5', script_content)
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
            self.assertIn("function bindRovingTablist(", script_content)
        with self.client.get("/static/styles.css") as stylesheet:
            self.assertEqual(stylesheet.status_code, 200)
            css = stylesheet.get_data(as_text=True)
            self.assertIn("@media (max-width: 760px)", css)
            self.assertIn("[hidden]", css)
            self.assertIn("overflow-x: hidden", css)
            self.assertIn(".editable-element", css)
            self.assertIn(".sticker-grid", css)
            self.assertIn(".sticker-element.selected", css)
            self.assertIn('font-family: "AutoCover Seto"', css)
            self.assertIn('url("/api/fonts/default?glyph-revision=1")', css)
            self.assertIn(".stroke-color-controls", css)
            self.assertIn(".task-sort-bar", css)
            self.assertIn(".copy-editor-heading", css)
            self.assertIn(".copy-line-remove", css)
            self.assertIn(".sticker-library-summary", css)
            self.assertIn(".text-style-editor", css)
            self.assertIn("clamp(410px, 24vw, 500px)", css)
            self.assertIn("minmax(480px, 1fr)", css)
            self.assertIn("overflow-x: auto", css)
            self.assertNotIn("min-width: 1040px", css)
            self.assertIn("container-type: size", css)
            self.assertIn("repeat(3, 1fr)", css)
            self.assertIn("calc(177.777cqh - 64px)", css)

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
applyKeyboardTransform(textModel, "text", "ArrowDown", true);
if (Math.abs(textModel.y - 0.52) > 0.000001) throw new Error("文字大步移动错误");
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
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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
            timeout=10,
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
            timeout=10,
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
            timeout=10,
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
            timeout=10,
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

    def test_candidate_endpoint_and_media_token(self) -> None:
        task = self._ready_task()
        candidate = task["candidates"][0]

        with self.client.get(f"/api/media/{candidate['token']}") as media:
            self.assertEqual(media.status_code, 200)
            self.assertEqual(media.mimetype, "image/jpeg")
        self.assertNotIn(str(self.root), str(task))

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
                            {"x": 0.20, "y": 0.30, "scale": 0.8, "font_size": 104}
                        ],
                        "stickers": [
                            {
                                "asset_id": asset_id,
                                "x": 0.70,
                                "y": 0.25,
                                "width": 0.12,
                            }
                        ],
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        preview = response.get_json()["preview"]
        self.assertAlmostEqual(preview["placements"][0]["box"][0], 1440 * 0.20, delta=2)
        self.assertAlmostEqual(preview["placements"][0]["box"][1], 1080 * 0.30, delta=2)
        self.assertEqual(preview["placements"][0]["font_size"], 104)
        self.assertEqual(preview["placements"][0]["stroke_color"], "#ffffff")
        self.assertAlmostEqual(preview["stickers"][0]["box"][0], 1440 * 0.70, delta=2)
        self.assertIn("background_media_token", preview)
        with self.client.get(f"/api/media/{preview['background_media_token']}") as base:
            self.assertEqual(base.status_code, 200)

    def test_render_options_support_ratio_specific_focus(self) -> None:
        library = self.app.extensions["sticker_library"]
        payload = {
            "focus_x": 0.25,
            "focus_y": 0.35,
            "layouts": {
                "4x3": {"focus_x": 0.65, "focus_y": 0.75},
                "16x9": {},
            },
        }

        compact = _render_options(payload, "4x3", library)
        wide = _render_options(payload, "16x9", library)

        self.assertEqual((compact["focus_x"], compact["focus_y"]), (0.65, 0.75))
        self.assertEqual((wide["focus_x"], wide["focus_y"]), (0.25, 0.35))
        with self.assertRaisesRegex(ApiError, "focus_x"):
            _render_options(
                {"layouts": {"4x3": {"focus_x": 1.1}}},
                "4x3",
                library,
            )

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

        with patch("app.render_cover", side_effect=mutate_after_first_render):
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

        with patch("app.render_cover", side_effect=fail_wide_render):
            response = self.client.post(f"/api/tasks/{task['id']}/save", json={})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], "处理失败，请查看服务日志")
        self.assertNotIn(str(self.root), response.get_data(as_text=True))
        self.assertEqual(compact.read_bytes(), b"old-compact")
        self.assertEqual(wide.read_bytes(), b"old-wide")
        self.assertEqual([path for path in self.output.iterdir() if path.name.startswith(".")], [])

    def test_batch_export_and_validation(self) -> None:
        tasks = self._scan()
        with patch("autocover.workspace.extract_candidate_frames", return_value=[self.candidate]):
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
