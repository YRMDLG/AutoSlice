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
from autoslice.autocover_service import (
    AUTOCOVER_PROBE_INCOMPATIBLE,
    AUTOCOVER_PROBE_READY,
    AUTOCOVER_PROBE_UNAVAILABLE,
    AutoCoverProbeResult,
)
from autoslice.subtitle_workflow import parse_srt_document, save_corrected_srt
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
        preview_source = script.split(
            "async function preview", 1
        )[1].split("async function renderVideo", 1)[0]
        self.assertNotIn("saveCorrections()", preview_source)
        self.assertIn("未修改正式校对字幕", preview_source)
        render_source = script.split(
            "async function renderVideo", 1
        )[1].split("function rememberTaskEvent", 1)[0]
        self.assertNotIn("saveCorrections()", render_source)
        self.assertIn("has_corrected_srt", render_source)
        title_source = script.split(
            "async function generateReferenceTitle", 1
        )[1].split("function copyRecommendedTitle", 1)[0]
        self.assertNotIn("saveCorrections()", title_source)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查字幕预览清理")
    def test_subtitle_preview_url_cleanup_is_executable(self):
        script_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "autoslice"
            / "resources"
            / "static"
            / "subtitle_workflow.js"
        )
        script = script_path.read_text(encoding="utf-8")
        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        runtime_assertions = r'''
const image={src:'',style:{display:'block'},removeAttribute(name){if(name==='src')this.src='';}};
const placeholder={style:{display:'none'}};
globalThis.document={getElementById(id){
  if(id==='previewImage')return image;
  if(id==='previewPlaceholder')return placeholder;
  return null;
}};
globalThis.URL={revoked:[],revokeObjectURL(value){this.revoked.push(value);}};
state.previewUrl='blob:subtitle-preview';
image.src=state.previewUrl;
clearPreview();
if(URL.revoked.length!==1||URL.revoked[0]!=='blob:subtitle-preview')throw new Error('预览 URL 未释放');
if(state.previewUrl!=='')throw new Error('预览 URL 状态未清空');
if(image.src!==''||image.style.display!=='none')throw new Error('预览图片未清理');
if(placeholder.style.display!=='block')throw new Error('预览占位符未恢复');
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

    def test_f1_context_fixed_mock_covers_long_content_zoom_and_desktop_action(self):
        project_root = Path(__file__).resolve().parents[2]
        workbench_css = (
            project_root / "src" / "autoslice" / "resources" / "static" / "workbench.css"
        ).read_text(encoding="utf-8")
        autocover_css = (
            project_root
            / "src"
            / "autoslice_cover"
            / "resources"
            / "static"
            / "styles.css"
        ).read_text(encoding="utf-8")
        topic_html = (
            project_root / "src" / "autoslice" / "resources" / "templates" / "topic_v2.html"
        ).read_text(encoding="utf-8")
        subtitle_html = (
            project_root
            / "src"
            / "autoslice"
            / "resources"
            / "templates"
            / "subtitle_workflow.html"
        ).read_text(encoding="utf-8")

        viewports = (
            (1920, 1.0), (1440, 1.0), (1366, 1.0), (768, 1.0),
            (390, 1.0), (1366, 1.25), (1366, 1.5),
        )
        for service, stylesheet in (("AutoSlice", workbench_css), ("AutoCover", autocover_css)):
            self.assertIn("min-width: 940px", stylesheet, service)
            self.assertIn("flex: 1 1 0", stylesheet, service)
            narrow_rule = re.search(
                r"@media\s*\(max-width:\s*1100px\).*?\.task-context-facts\s*\{(.*?)\}",
                stylesheet,
                flags=re.S,
            )
            self.assertIsNotNone(narrow_rule, service)
            self.assertIn("min-width: 0", narrow_rule.group(1), service)
            for width, scale in viewports:
                with self.subTest(service=service, width=width, scale=scale):
                    css_width = round(width / scale)
                    facts_width = css_width - 190 if css_width <= 1100 else 940
                    self.assertLessEqual(
                        facts_width + 190,
                        css_width,
                        "固定 mock 中长标题不得推出上下文快捷入口",
                    )

        summary = topic_html.index('<section class="primary-action-summary"')
        workspace = topic_html.index('<div class="workspace-grid">')
        self.assertLess(summary, workspace)
        self.assertEqual(topic_html.count('id="startButton"'), 1)
        self.assertIn("已选择录播；可直接执行完整分析与切片", topic_html)
        self.assertIn(".desktop-editing-notice{display:none}", subtitle_html)
        self.assertIn("@media(max-width:760px)", subtitle_html)
        self.assertIn("字幕精细编辑建议在桌面完成", subtitle_html)
        self.assertIn("完整字幕编辑器保持桌面工作台布局", subtitle_html)


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

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查扫描状态行为")
    def test_topic_page_scan_state_clears_stale_selection_and_restores_only_present_path(self):
        _html, script = self._page_script()
        script_prefix = script.split("restoreWorkspacePaths();", 1)[0]
        runtime_assertions = r"""
const makeNode=()=>({
  textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,value:'',title:'',
  style:{},options:[],selectedIndex:0,selectedOptions:[],dataset:{},children:[],
  classList:{add:()=>{},remove:()=>{},toggle:()=>{}},
  replaceChildren(){this.children=[];if(this.id==='vlist')videoItems.length=0},
  add:()=>{},append(...nodes){this.textContent+=nodes.map(node=>node.textContent||'').join('')},
  appendChild(node){this.children.push(node);if(this.id==='vlist'&&node.className==='video-item')videoItems.push(node)},
  addEventListener:()=>{},setAttribute(name,value){this[name]=String(value)},removeAttribute:()=>{},
  querySelector:()=>null,querySelectorAll:()=>[],focus:()=>{},
});
const nodes=new Map();
const videoItems=[];
globalThis.document={
  getElementById:(id)=>{if(!nodes.has(id)){const node=makeNode();node.id=id;nodes.set(id,node)}return nodes.get(id)},
  querySelectorAll:(selector)=>selector==='.video-item'?videoItems:[],
  createElement:()=>makeNode(),querySelector:()=>null,
};
document.getElementById('timelineMode').value='none';
globalThis.window={};
const storage=new Map([['autoslice.last-valid-video-dir','C:\\recordings']]);
globalThis.localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,String(value))};
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const makeResponse=(status,payload)=>({ok:status>=200&&status<300,status,json:async()=>payload});
let pendingScanResolve=null;
let nextScanResponse={status:200,payload:{videos:[],count:0}};
globalThis.fetch=(url)=>{
  if(url==='/api/scan')return new Promise(resolve=>{pendingScanResolve=()=>resolve(makeResponse(nextScanResponse.status,nextScanResponse.payload))});
  throw new Error(`意外请求 ${url}`);
};
const oldPath='C:\\recordings\\旧录播.mp4';
const newPath='C:\\recordings\\新录播.mp4';
function seedOldSelection(){
  selVideo=oldPath;selAss='C:\\recordings\\旧录播.ass';selectedVideoMeta={name:'旧录播.mp4',has_ass:true,has_srt:true};
  contextTaskId='pipeline_old';currentTaskId='pipeline_old';currentTaskSnapshot={task_id:'pipeline_old',status:'done'};resultTaskId='pipeline_old';currentTaskResult={kind:'pipeline'};optimizedTimeline={jsonPath:'旧时间轴.json'};
  document.getElementById('selectedSummary').hidden=false;document.getElementById('selectedName').textContent='旧录播.mp4';
  document.getElementById('selectedPath').textContent=oldPath;document.getElementById('selectionState').textContent='旧录播.mp4';document.getElementById('progressBox').innerHTML='旧任务进度';
  document.getElementById('qualityOverview').hidden=false;document.getElementById('report').hidden=false;
  updateActions();
}
async function finishScan(status,payload){
  nextScanResponse={status,payload};
  const promise=scan();
  await Promise.resolve();
  assert(scanBusy===true,'扫描未立即进入 scanning 状态');
  assert(document.getElementById('scanButton').disabled===true,'扫描期间扫描按钮未禁用');
  assert(document.getElementById('startButton').disabled===true,'扫描期间开始分析+切片未禁用');
  assert(document.getElementById('retryButton').disabled===true,'扫描期间重试主动作未禁用');
  assert(document.getElementById('optimizeButton').disabled===true,'扫描期间时间轴主动作未禁用');
  assert(document.getElementById('videoDir').disabled===true,'扫描期间目录选择未禁用');
  assert(document.getElementById('timelineMode').disabled===true,'扫描期间分析选择未禁用');
  pendingScanResolve();
  if(status!==200)await promise.catch(()=>{});else await promise;
  assert(scanBusy===false,'扫描结束后仍保持 scanning 状态');
}
(async()=>{
  seedOldSelection();
  await finishScan(200,{videos:[{name:'旧录播.mp4',path:oldPath,has_ass:true,has_srt:true}],count:1});
  assert(selVideo===oldPath,'旧路径仍在结果中时没有恢复选择');
  assert(selectedVideoMeta?.name==='旧录播.mp4','恢复选择没有恢复视频元数据');
  assert(document.getElementById('selectedSummary').hidden===false,'成功扫描恢复选择后摘要仍隐藏');

  seedOldSelection();
  await finishScan(200,{videos:[{name:'新录播.mp4',path:newPath,has_ass:false,has_srt:false}],count:1});
  assert(selVideo===null,'旧路径不在新结果时错误恢复旧选择');
  assert(selectedVideoMeta===null,'旧路径不在新结果时仍保留元数据');
  assert(document.getElementById('selectedSummary').hidden===true,'替换选择失败后仍显示旧摘要');
  assert(contextTaskId===null,'替换选择失败后仍保留旧任务上下文');
  assert(resultTaskId===null&&currentTaskResult===null,'替换选择失败后仍保留旧结果上下文');
  assert(document.getElementById('qualityOverview').hidden===true&&document.getElementById('report').hidden===true,'替换选择失败后仍显示旧摘要或报告');
  assert(document.getElementById('startButton').disabled===true,'旧路径不在新结果时仍可执行开始分析');

  seedOldSelection();
  nextScanResponse={status:500,payload:{error:'目录不存在'}};
  const failed=scan();
  await Promise.resolve();
  assert(scanBusy===true,'失败扫描未立即进入 scanning 状态');
  pendingScanResolve();
  await failed;
  assert(selVideo===null&&selectedVideoMeta===null,'扫描失败后仍保留旧选择');
  assert(contextTaskId===null&&resultTaskId===null,'扫描失败后仍保留旧任务上下文');
  assert(currentTaskId===null&&currentTaskSnapshot===null&&document.getElementById('progressBox').innerHTML.includes('选择录播后显示任务进度'),'扫描失败后仍保留旧任务进度');
  await handleTaskSnapshot({task_id:'pipeline_old',status:'done',progress:'旧任务迟到更新',step:100,total:100});
  assert(currentTaskId===null&&contextTaskId===null,'清除选择后旧 SSE 更新重新绑定了任务上下文');
  assert(document.getElementById('qualityOverview').hidden===true&&document.getElementById('report').hidden===true,'扫描失败后仍保留旧摘要');
  assert(document.getElementById('scanRecovery').hidden===false,'扫描失败没有显示恢复入口');
  assert(document.getElementById('pageNotice').textContent.includes('扫描失败'),'扫描失败被伪装成成功');

  seedOldSelection();
  await finishScan(200,{videos:[],count:0});
  assert(selVideo===null&&selectedVideoMeta===null,'空结果后仍保留旧选择');
  assert(contextTaskId===null&&resultTaskId===null,'空结果后仍保留旧任务上下文');
  assert(document.getElementById('qualityOverview').hidden===true&&document.getElementById('report').hidden===true,'空结果后仍保留旧摘要');
  assert(document.getElementById('scanRecovery').hidden===false,'空结果没有显示重新扫描入口');
  assert(document.getElementById('pageNotice').textContent.includes('没有找到录播'),'空结果没有明确提示');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(
            ["node", "-"],
            input=script_prefix + runtime_assertions,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查当前任务上下文行为")
    def test_topic_page_current_task_context_tracks_selection_status_and_safe_result(self):
        html, script = self._page_script()
        script_prefix = script.split("restoreWorkspacePaths();", 1)[0]
        self.assertIn('aria-label="当前任务上下文"', html)
        self.assertIn('href="/subtitle-workflow"', html)
        self.assertIn('href="/autocover"', html)
        self.assertNotIn("<iframe", html.casefold())
        self.assertNotIn("setInterval(", script)
        self.assertNotIn("safeTaskId", script)
        runtime_assertions = r"""
const makeNode=()=>({
  textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,value:'',title:'',
  style:{},options:[],selectedIndex:0,selectedOptions:[],dataset:{},
  classList:{add:()=>{},remove:()=>{},toggle:()=>{}},
  replaceChildren:()=>{},add:()=>{},append:()=>{},addEventListener:()=>{},
  setAttribute(name,value){this[name]=String(value)},removeAttribute:()=>{},
  querySelector:()=>null,querySelectorAll:()=>[],focus:()=>{},
});
const nodes=new Map();
globalThis.document={
  getElementById:(id)=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id);},
  querySelectorAll:()=>[],createElement:()=>makeNode(),querySelector:()=>null,
};
globalThis.window={};
globalThis.localStorage={getItem:()=>null,setItem:()=>{}};
document.getElementById('timelineMode').value='none';
const streamer=document.getElementById('streamerProfile');
streamer.value='auto';streamer.selectedOptions=[{textContent:'自动识别'}];
const assert=(condition,message)=>{if(!condition)throw new Error(message)};

updateCurrentTaskContext();
assert(document.getElementById('contextPhase').textContent==='未选择','初始阶段错误');
assert(document.getElementById('contextOpenResultButton').disabled===true,'无结果时目录按钮未禁用');

const restoredTaskId='pipeline_中文恢复任务#1';
renderTaskProgress({
  task_id:restoredTaskId,status:'running',progress:'刷新恢复处理中',step:5,total:10,
  source_paths:['C:\\private\\recordings\\中文录播.flv','C:\\private\\recordings\\中文录播.srt'],
  streamer_profile:{id:'restored',label:'恢复任务主播'},
});
assert(contextTaskId===restoredTaskId,'恢复任务没有接入当前上下文');
assert(document.getElementById('contextVideo').textContent==='中文录播.flv','恢复任务没有从路径推导安全文件名');
assert(document.getElementById('contextPhase').textContent==='进行中','恢复任务没有显示真实阶段');
assert(document.getElementById('contextStreamer').textContent==='恢复任务主播','自动主播没有显示恢复任务公开标签');
assert(!document.getElementById('contextVideo').textContent.includes('private'),'恢复任务视频暴露了目录');

const dangerous='<img src=x onerror=globalThis.contextExecuted=true>.mp4';
selVideo=`C:\\private\\recordings\\${dangerous}`;
selectedVideoMeta={name:dangerous,has_srt:true};
contextTaskId=null;
streamer.value='zeyin';streamer.selectedOptions=[{textContent:'测试主播 <script>危险</script>'}];
updateCurrentTaskContext();
assert(document.getElementById('contextVideo').textContent===dangerous,'当前视频没有使用安全文件名');
assert(document.getElementById('contextVideo').innerHTML==='','危险标题被写入 innerHTML');
assert(document.getElementById('contextStreamer').textContent.includes('<script>'),'主播可见文本没有通过 textContent 写入');
assert(document.getElementById('contextPhase').textContent==='等待启动','选择录播后阶段错误');
assert(document.getElementById('contextPrevious').textContent==='原视频与源 SRT','选择录播后产物推导错误');

renderTaskProgress({
  task_id:'pipeline_后台旧任务',status:'running',progress:'旧任务仍在运行',step:6,total:10,
  source_path:'C:\\private\\old\\旧录播.flv',streamer_profile:{label:'旧任务主播'},
});
assert(contextTaskId===null,'后台旧任务覆盖了用户手工选择的上下文任务');
assert(document.getElementById('contextVideo').textContent===dangerous,'后台旧任务覆盖了用户手工选择的录播');
assert(document.getElementById('contextPhase').textContent==='等待启动','后台旧任务覆盖了用户选择后的阶段');
assert(document.getElementById('contextStreamer').textContent.includes('<script>'),'后台旧任务覆盖了用户显式主播选择');

const statusLabels={queued:'排队中',running:'进行中',done:'已完成',error:'失败',cancelled:'已取消',interrupted:'已中断'};
for(const [status,label] of Object.entries(statusLabels)){
  const taskId=`pipeline_context_${status}`;
  contextTaskId=taskId;
  renderTaskProgress({task_id:taskId,status,progress:status,step:5,total:10});
  assert(document.getElementById('contextPhase').textContent===label,`${status} 上下文阶段错误`);
}

selVideo='';selectedVideoMeta=null;streamer.value='auto';streamer.selectedOptions=[{textContent:'自动识别'}];
currentTaskId=null;currentTaskSnapshot=null;contextTaskId=null;
const unicodeTaskId='pipeline_中文兼容任务#完成';
renderTaskProgress({
  task_id:unicodeTaskId,status:'done',progress:'完成',step:10,total:10,
  source_path:'C:\\private\\result\\中文结果录播.flv',streamer_profile:{label:'兼容任务主播'},
});
renderResultArtifact(unicodeTaskId,{
  artifact_dir:'C:\\private\\result',overview_path:'C:\\private\\result\\00.md',
});
assert(resultTaskId===unicodeTaskId,'Unicode task_id 没有按不透明标识原样恢复');
assert(document.getElementById('contextPrevious').textContent==='整理包与切片结果','完成产物没有更新');
assert(document.getElementById('contextNext').textContent==='打开结果目录','完成后的下一步错误');
assert(document.getElementById('contextOpenResultButton').disabled===false,'Unicode task_id 没有启用现有目录按钮');
const contextText=['contextVideo','contextPhase','contextPrevious','contextNext','contextStreamer','contextResult']
  .map(id=>`${document.getElementById(id).textContent}\n${document.getElementById(id).title}`).join('\n');
assert(!contextText.includes('C:\\private'),'上下文条暴露了本机绝对路径');
assert(!contextText.includes(unicodeTaskId),'上下文 DOM 显示了 task_id');

contextTaskId='timeline_opt_context_done';
renderTaskProgress({task_id:contextTaskId,status:'done',progress:'完成',step:10,total:10});
currentTaskResult={kind:'timeline'};
renderResultArtifact(contextTaskId,{optimized_json_path:'C:\\private\\timeline.json'});
assert(document.getElementById('contextPrevious').textContent==='优化时间轴','时间轴完成产物被清理');
assert(document.getElementById('contextNext').textContent==='开始分析+切片','时间轴完成后的下一步错误');
assert(document.getElementById('contextVideo').textContent==='当前任务视频','恢复任务缺少源路径时回退文案错误');

selectedVideoMeta={};selVideo='';currentTaskSnapshot=null;currentTaskId=null;contextTaskId=null;resultTaskId=null;
updateCurrentTaskContext();
assert(document.getElementById('contextVideo').textContent==='未选择录播','缺字段回退错误');
assert(globalThis.contextExecuted!==true,'危险标题被执行');
"""
        result = subprocess.run(
            ["node", "-"],
            input=script_prefix + runtime_assertions,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查任务恢复与取消行为")
    def test_topic_page_restores_six_task_states_and_cancels_without_polling(self):
        _html, script = self._page_script()
        script_prefix = script.split("restoreWorkspacePaths();", 1)[0]
        self.assertNotIn("setInterval(", script)
        self.assertNotIn("setTimeout(connectSSE", script)
        self.assertEqual(script.count("new EventSource('/api/events')"), 1)
        runtime_assertions = r"""
const makeNode=()=>({
  textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,value:'',
  title:'',style:{},options:[],dataset:{},
  classList:{add:()=>{},remove:()=>{},toggle:()=>{}},
  replaceChildren:()=>{},add:()=>{},append:()=>{},addEventListener:()=>{},
  setAttribute:()=>{},removeAttribute:()=>{},querySelector:()=>null,
  querySelectorAll:()=>[],focus:()=>{},
});
const nodes=new Map();
globalThis.document={
  getElementById:(id)=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id);},
  querySelectorAll:()=>[],createElement:()=>makeNode(),querySelector:()=>null,
};
document.getElementById('timelineMode').value='none';
globalThis.window={};
globalThis.localStorage={getItem:()=>null,setItem:()=>{}};
globalThis.setInterval=()=>{throw new Error('禁止任务轮询')};
globalThis.setTimeout=()=>{throw new Error('禁止手工 SSE 重连定时器')};

const makeResponse=(status,payload)=>({
  ok:status>=200&&status<300,status,
  json:async()=>payload,
  text:async()=>typeof payload==='string'?payload:JSON.stringify(payload),
});
const fetchCalls=[];
let cancelMode='deferred';
let cancelResolve=null;
let deferredResultResolve=null;
let deferredReportResolve=null;
globalThis.fetch=(url,options={})=>{
  fetchCalls.push({url,options});
  if(url.endsWith('/cancel')){
    if(cancelMode==='deferred'){
      return new Promise(resolve=>{cancelResolve=()=>resolve(makeResponse(200,{
        task_id:'pipeline_cancel#1',
        task:{status:'cancelled',progress:'已取消',message:'用户已取消任务',step:2,total:10},
      }))});
    }
    if(cancelMode==='404')return Promise.resolve(makeResponse(404,{error:'任务不存在'}));
    if(cancelMode==='409')return Promise.resolve(makeResponse(409,{
      error:'任务已处于终态 done，不能取消',status:'done',
    }));
  }
  if(url.includes('pipeline_stale_result')&&url.endsWith('/result')){
    return new Promise(resolve=>{deferredResultResolve=()=>resolve(makeResponse(200,{
      artifact_dir:'C:\\old\\result',overview_path:'C:\\old\\result\\00_概览.md',
      report_available:false,
    }))});
  }
  if(url.includes('pipeline_stale_report')&&url.endsWith('/result')){
    return Promise.resolve(makeResponse(200,{
      artifact_dir:'C:\\old\\report-result',overview_path:'C:\\old\\report-result\\00_概览.md',
      report_available:true,
    }));
  }
  if(url.includes('pipeline_stale_report')&&url.endsWith('/report')){
    return new Promise(resolve=>{deferredReportResolve=()=>resolve(makeResponse(500,{error:'旧报告失败'}))});
  }
  if(url.includes('pipeline_broken')&&url.endsWith('/result')){
    return Promise.resolve(makeResponse(500,{error:'结果摘要损坏'}));
  }
  if(url.endsWith('/result'))return Promise.resolve(makeResponse(200,{
    artifact_dir:'C:\\safe\\result',overview_path:'C:\\safe\\result\\00_概览.md',
    report_available:false,
  }));
  if(url.endsWith('/report'))return Promise.resolve(makeResponse(200,'# report'));
  throw new Error(`意外请求 ${url}`);
};

class FakeEventSource{
  static instances=[];
  constructor(url){this.url=url;this.listeners={};FakeEventSource.instances.push(this)}
  addEventListener(type,handler){this.listeners[type]=handler}
  close(){this.closed=true}
}
globalThis.EventSource=FakeEventSource;
const assert=(condition,message)=>{if(!condition)throw new Error(message)};

(async()=>{
  const labels={queued:'排队中',running:'进行中',done:'已完成',error:'失败',cancelled:'已取消',interrupted:'已中断'};
  for(const [status,label] of Object.entries(labels)){
    renderTaskProgress({task_id:`pipeline_${status}`,status,progress:`${status} message`,step:4,total:10});
    assert(document.getElementById('progressBox').innerHTML.includes(label),`${status} 未显示独立状态`);
    const active=status==='queued'||status==='running';
    assert(taskBusy===active,`${status} 的 taskBusy 错误`);
    assert(document.getElementById('cancelTaskButton').hidden===!active,`${status} 的取消按钮可见性错误`);
    const hasProgressTrack=document.getElementById('progressBox').innerHTML.includes('progress-track');
    assert(hasProgressTrack===(status==='running'),`${status} 的进度条显示错误`);
  }

  renderTaskProgress({
    task_id:'pipeline_interrupted',status:'interrupted',progress:'已分析 3 块',
    message:'上次运行已中断',error:'后台进程意外退出',error_summary:'任务仍处于活动状态',
    metadata:{
      checkpoint_path:'C:\\secret\\checkpoint.json',
      startup_recovery:{
        previous_message:'正在复核候选',next_action:'使用原资源预约新任务后从检查点重试',
        checkpoint:{progress:'已完成首轮分析',step:40,total:100},
        private_path:'C:\\secret\\private.json',
      },
    },
  });
  const interruptedHtml=document.getElementById('progressBox').innerHTML;
  for(const marker of ['已分析 3 块','上次运行已中断','任务仍处于活动状态','中断前检查点：已完成首轮分析（40/100）','续跑提示']){
    assert(interruptedHtml.includes(marker),`中断详情缺少 ${marker}`);
  }
  assert(!interruptedHtml.includes('secret'), '中断卡暴露了未授权路径字段');
  assert(!interruptedHtml.includes('<button'), '中断卡虚构了自动恢复按钮');

  const activeSnapshot={
    unrelated_new:{task_id:'subtitle_new',status:'running',updated_at:999,created_at:999},
    pipeline_done:{status:'done',updated_at:90,created_at:10},
    clip_review_interrupted:{status:'interrupted',updated_at:80,created_at:20},
    pipeline_queued:{status:'queued',updated_at:100,created_at:30},
    timeline_opt_running:{
      status:'running',updated_at:110,created_at:25,
      source_paths:['C:\\private\\restore\\SSE恢复录播.flv','C:\\private\\restore\\SSE恢复录播.srt'],
      streamer_profile:{id:'sse-restored',label:'SSE 恢复主播'},
    },
  };
  assert(selectInitTask(activeSnapshot).task_id==='timeline_opt_running','init 未优先选择最新活动任务');
  const interruptedSnapshot={...activeSnapshot};
  delete interruptedSnapshot.pipeline_queued;
  delete interruptedSnapshot.timeline_opt_running;
  interruptedSnapshot.pipeline_done.updated_at=200;
  assert(selectInitTask(interruptedSnapshot).task_id==='clip_review_interrupted','init 未优先选择 interrupted');
  delete interruptedSnapshot.clip_review_interrupted;
  interruptedSnapshot.clip_review_error={status:'error',updated_at:210,created_at:50};
  assert(selectInitTask(interruptedSnapshot).task_id==='clip_review_error','init 未选择最新相关终态');
  assert(selectInitTask({
    pipeline_old:{status:'done',created_at:10},
    pipeline_new:{status:'done',created_at:20},
  }).task_id==='pipeline_new','init 没有在缺少 updated_at 时回退 created_at');
  assert(selectInitTask({subtitle_only:{task_id:'subtitle_only',status:'running',updated_at:999}})===null,'init 没有忽略不相关任务');

  connectSSE();
  connectSSE();
  assert(FakeEventSource.instances.length===1,'重复创建了 EventSource');
  FakeEventSource.instances[0].onerror();
  assert(FakeEventSource.instances.length===1,'SSE 错误触发了重复 EventSource');
  FakeEventSource.instances[0].onopen();
  currentTaskId=null;
  await FakeEventSource.instances[0].listeners.init({data:JSON.stringify(activeSnapshot)});
  assert(currentTaskId==='timeline_opt_running','SSE init 没有恢复选中的任务');
  assert(contextTaskId==='timeline_opt_running','SSE init 没有把恢复任务接入上下文');
  assert(document.getElementById('contextVideo').textContent==='SSE恢复录播.flv','SSE init 没有恢复安全视频文件名');
  assert(document.getElementById('contextPhase').textContent==='进行中','SSE init 恢复任务没有显示真实阶段');
  assert(document.getElementById('contextStreamer').textContent==='SSE 恢复主播','SSE init 没有显示主播公开标签');
  assert(!document.getElementById('contextVideo').textContent.includes('private'),'SSE init 上下文暴露了源目录');

  const resultFetchCount=()=>fetchCalls.filter(call=>call.url.endsWith('/result')).length;
  const resultFetchesBeforeIgnoredTerminal=resultFetchCount();
  await handleTaskSnapshot({
    task_id:'pipeline_old_done',status:'done',progress:'旧任务完成',step:100,total:100,
    updated_at:999,created_at:999,
  });
  assert(currentTaskId==='timeline_opt_running','其他任务的旧终态覆盖了当前活动任务');
  assert(taskBusy===true,'忽略旧终态时错误解除了 taskBusy');
  assert(resultFetchCount()===resultFetchesBeforeIgnoredTerminal,'被忽略的旧终态仍读取了结果');

  await handleTaskSnapshot({
    task_id:'pipeline_older_running',status:'running',progress:'较旧活动任务',step:2,total:10,
    updated_at:109,created_at:109,
  });
  assert(currentTaskId==='timeline_opt_running','较旧的另一活动任务覆盖了当前活动任务');
  await handleTaskSnapshot({
    task_id:'pipeline_newer_queued',status:'queued',progress:'新任务排队中',step:0,total:10,
    updated_at:111,created_at:111,
  });
  assert(currentTaskId==='pipeline_newer_queued','更新的另一活动任务没有替换当前任务');
  assert(taskBusy===true,'更新的 queued 任务没有保持 taskBusy');
  assert(!document.getElementById('progressBox').innerHTML.includes('progress-track'),'替换后的 queued 任务显示了进度条');

  currentTaskId=null;
  currentTaskSnapshot=null;
  taskBusy=false;
  clearTaskArtifacts();
  await FakeEventSource.instances[0].listeners.init({data:JSON.stringify({
    pipeline_done:{status:'done',progress:'完成',step:100,total:100,updated_at:200},
  })});
  assert(resultTaskId==='pipeline_done','done init 没有恢复结果入口');
  assert(document.getElementById('resultArtifact').hidden===false,'done 结果入口仍隐藏');
  assert(document.getElementById('progressBox').innerHTML.includes('已完成'),'done 结果恢复破坏了状态卡');

  await handleTaskSnapshot({task_id:'pipeline_broken',status:'done',progress:'完成',step:100,total:100,updated_at:300});
  assert(document.getElementById('progressBox').innerHTML.includes('已完成'),'done 结果读取失败破坏了状态卡');
  assert(document.getElementById('pageNotice').textContent.includes('结果入口恢复失败'),'done 结果读取失败没有明确提示');

  const staleResultPromise=handleTaskSnapshot({
    task_id:'pipeline_stale_result',status:'done',progress:'旧结果待恢复',step:100,total:100,
    updated_at:400,created_at:400,
  });
  await Promise.resolve();
  assert(deferredResultResolve!==null,'旧 done 的 /result 请求没有进入等待状态');
  await handleTaskSnapshot({
    task_id:'timeline_opt_new_result',status:'running',progress:'新任务处理中',step:3,total:10,
    updated_at:401,created_at:401,
  });
  setNotice('新任务结果守卫','success');
  const resultGuardHtml=document.getElementById('progressBox').innerHTML;
  deferredResultResolve();
  await staleResultPromise;
  assert(currentTaskId==='timeline_opt_new_result','旧 /result 响应覆盖了当前任务 ID');
  assert(document.getElementById('progressBox').innerHTML===resultGuardHtml,'旧 /result 响应覆盖了当前状态卡');
  assert(resultTaskId===null&&document.getElementById('resultArtifact').hidden===true,'旧 /result 响应恢复了结果入口');
  assert(document.getElementById('report').hidden===true,'旧 /result 响应恢复了报告');
  assert(document.getElementById('pageNotice').textContent==='新任务结果守卫','旧 /result 响应覆盖了 notice');

  await handleTaskSnapshot({
    task_id:'pipeline_stale_report',status:'queued',progress:'等待旧报告测试',step:0,total:10,
    updated_at:402,created_at:402,
  });
  const staleReportPromise=handleTaskSnapshot({
    task_id:'pipeline_stale_report',status:'done',progress:'旧报告待恢复',step:100,total:100,
    updated_at:403,created_at:402,
  });
  for(let index=0;index<5&&deferredReportResolve===null;index++)await Promise.resolve();
  assert(deferredReportResolve!==null,'旧 done 的 /report 请求没有进入等待状态');
  await handleTaskSnapshot({
    task_id:'clip_review_new_report',status:'running',progress:'新的复核任务',step:4,total:10,
    updated_at:404,created_at:404,
  });
  setNotice('新任务报告守卫','success');
  const reportGuardHtml=document.getElementById('progressBox').innerHTML;
  deferredReportResolve();
  await staleReportPromise;
  const guardedReport=document.getElementById('report');
  assert(currentTaskId==='clip_review_new_report','旧 /report 响应覆盖了当前任务 ID');
  assert(document.getElementById('progressBox').innerHTML===reportGuardHtml,'旧 /report 响应覆盖了当前状态卡');
  assert(guardedReport.hidden===true&&!guardedReport.textContent.includes('旧报告失败'),'旧 /report 错误覆盖了报告区域');
  assert(resultTaskId===null&&document.getElementById('resultArtifact').hidden===true,'旧 /report 响应恢复了结果入口');
  assert(document.getElementById('pageNotice').textContent==='新任务报告守卫','旧 /report 响应覆盖了 notice');

  renderTaskProgress({task_id:'pipeline_cancel#1',status:'running',progress:'处理中',step:2,total:10});
  const cancelPromise=cancelCurrentTask();
  await Promise.resolve();
  assert(document.getElementById('cancelTaskButton').disabled===true,'取消请求期间按钮未禁用');
  assert(document.getElementById('cancelTaskButton').textContent==='正在取消...','取消请求期间按钮文案错误');
  cancelResolve();
  await cancelPromise;
  assert(fetchCalls.some(call=>call.url==='/api/tasks/pipeline_cancel%231/cancel'),'取消请求没有编码 task_id');
  assert(taskBusy===false&&document.getElementById('cancelTaskButton').hidden===true,'取消成功后仍保持忙碌');

  cancelMode='404';
  renderTaskProgress({task_id:'pipeline_missing',status:'queued',progress:'排队中',step:0,total:100});
  await cancelCurrentTask();
  assert(document.getElementById('pageNotice').textContent.includes('任务不存在'),'取消 404 没有明确提示');

  cancelMode='409';
  renderTaskProgress({task_id:'pipeline_terminal',status:'running',progress:'处理中',step:5,total:10});
  await cancelCurrentTask();
  assert(document.getElementById('pageNotice').textContent.includes('不能取消'),'取消 409 没有明确提示');
  assert(taskBusy===false&&document.getElementById('progressBox').innerHTML.includes('已完成'),'取消 409 没有同步已知终态');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(
            ["node", "-"],
            input=script_prefix + runtime_assertions,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查质量概览真实展示")
    def test_topic_page_restores_sorted_quality_overview_with_safe_fallbacks(self):
        _html, script = self._page_script()
        script_prefix = script.split("restoreWorkspacePaths();", 1)[0]
        runtime_assertions = r"""
const makeNode=()=>(
  {textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,value:'',
   title:'',style:{},options:[],dataset:{},
   classList:{add:()=>{},remove:()=>{},toggle:()=>{}},
   replaceChildren:()=>{},add:()=>{},append:()=>{},addEventListener:()=>{}}
);
const nodes=new Map();
globalThis.document={
  getElementById:(id)=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id);},
  querySelectorAll:()=>[],createElement:()=>makeNode(),querySelector:()=>null,
};
document.getElementById('timelineMode').value='none';
globalThis.window={};
globalThis.localStorage={getItem:()=>null,setItem:()=>{}};
const makeResponse=(status,payload)=>(
  {ok:status>=200&&status<300,status,
   json:async()=>payload,
   text:async()=>typeof payload==='string'?payload:JSON.stringify(payload)}
);
const overview={
  overview_version:1,
  final_slice_count:3,
  duration_distribution:{total_seconds:395,minimum_seconds:65,maximum_seconds:205,average_seconds:131.7,buckets:{under_90_seconds:1,from_90_to_179_seconds:1,at_least_180_seconds:1}},
  score_distribution:{scored_count:2,missing_count:1,minimum:84,maximum:93.5,average:88.8,buckets:{'90_to_100':1,'75_to_89_9':1,'60_to_74_9':0,below_60:0}},
  anchor_source_counts:{'弹幕峰值':1,'语义复核':1,'锚点未记录':1},
  danmaku_peak_count:1,
  clips:[
    {title:'高分<片段>',start:10,end:75,duration:65,score:93.5,peak_density:188,anchor_source:'弹幕峰值',reason:'诱因和后果完整'},
    {title:'中分片段',start:100,end:225,duration:125,score:84,peak_density:null,anchor_source:'语义复核',reason:'事件完整'},
    {title:'缺省片段',start:300,end:505,duration:205,score:null,peak_density:null,anchor_source:'锚点未记录',reason:'投稿价值理由未记录'},
  ],
  clips_truncated_count:0,
  edge_candidate_count:2,
  edge_candidates:[
    {title:'边缘候选 B',time_range:'00:01:30－00:02:10',score:74.9,reason:'事件完整，但独立反转略弱'},
    {title:'边缘候选 <A>',time_range:'00:00:50－00:01:20',score:60,reason:'理由 <script>alert(1)</script>'},
  ],
  edge_candidates_truncated_count:0,
};
globalThis.fetch=(url)=>{
  if(url.includes('pipeline_old')&&url.endsWith('/result'))return Promise.resolve(makeResponse(200,{artifact_dir:'C:\\safe\\old',report_available:false}));
  if(url.endsWith('/result'))return Promise.resolve(makeResponse(200,{
    artifact_dir:'C:\\safe\\result',
    overview_path:'C:\\safe\\result\\00_概览.md',
    quality_overview:overview,
    report_available:false,
  }));
  throw new Error(`意外请求 ${url}`);
};
class FakeEventSource{
  static instances=[];
  constructor(url){this.url=url;this.listeners={};FakeEventSource.instances.push(this)}
  addEventListener(type,handler){this.listeners[type]=handler}
  close(){this.closed=true}
}
globalThis.EventSource=FakeEventSource;
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
(async()=>{
  connectSSE();
  assert(FakeEventSource.instances.length===1,'质量概览测试没有建立 SSE 连接');
  const source=FakeEventSource.instances[0];
  await source.listeners.init({data:JSON.stringify({
    pipeline_restored:{status:'done',progress:'刷新恢复完成',step:100,total:100,updated_at:10,created_at:1},
  })});
  const quality=document.getElementById('qualityOverview');
  assert(quality.hidden===false,'SSE init 刷新恢复后没有显示质量概览');
  assert(currentTaskId==='pipeline_restored','SSE init 没有恢复质量概览对应任务');

  await source.listeners.task_update({data:JSON.stringify({
    task_id:'pipeline_first',status:'queued',progress:'首次任务排队',step:0,total:100,
    updated_at:20,created_at:20,
  })});
  assert(quality.hidden===true&&quality.innerHTML==='','新任务开始时没有清理旧质量概览');
  await source.listeners.task_update({data:JSON.stringify({
    task_id:'pipeline_first',status:'done',progress:'首次任务完成',step:100,total:100,
    updated_at:30,created_at:20,
  })});
  const html=quality.innerHTML;
  assert(quality.hidden===false,'task_update 首次完成后没有显示质量概览');
  assert(html.includes('最终切片 <strong>3</strong> 个'),'最终切片数量未真实展示');
  assert(html.includes('含弹幕峰值 <strong>1</strong> 个'),'含弹幕峰值文案或数量不正确');
  assert(html.includes('短于 1分30秒 1')&&html.includes('1分30秒–3分钟 1')&&html.includes('至少 3分钟 1'),'时长分布未真实展示');
  assert(html.indexOf('高分&lt;片段&gt;')<html.indexOf('中分片段'),'最终切片没有按投稿价值降序展示');
  assert(html.indexOf('中分片段')<html.indexOf('缺省片段'),'缺失投稿价值分没有排在最后');
  assert(html.includes('投稿价值未记录')&&html.includes('峰值未记录')&&html.includes('锚点未记录'),'缺失字段没有安全回退');
  assert(html.includes('边缘候选 <strong>2</strong> 个'),'边缘候选数量未展示');
  assert(html.includes('事件完整，但独立反转略弱'),'边缘候选投稿价值理由未展示');
  assert(html.includes('边缘候选 &lt;A&gt;')&&!html.includes('<script>alert(1)</script>'),'标题或理由没有安全转义');

  await source.listeners.task_update({data:JSON.stringify({
    task_id:'pipeline_old',status:'done',progress:'旧任务完成',step:100,total:100,
    updated_at:40,created_at:2,
  })});
  assert(quality.hidden===true&&quality.innerHTML==='','旧任务缺少新字段时没有清理已有质量概览');
})().catch(error=>{process.stderr.write(error.stack||String(error));process.exitCode=1});
"""
        result = subprocess.run(
            ["node", "-"],
            input=script_prefix + runtime_assertions,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_lan_rejects_effective_default_directories_outside_allowed_root(self):
        with TemporaryDirectory() as allowed_dir, TemporaryDirectory() as blocked_dir:
            blocked = Path(blocked_dir)
            environment = self._lan_environment(Path(allowed_dir))
            headers = {
                "Host": "192.168.1.20:5002",
                "X-AutoSlice-Token": self.STRONG_LAN_TOKEN,
            }
            write_headers = {
                **headers,
                "Origin": "http://192.168.1.20:5002",
            }
            replacements = {
                "DEFAULT_VIDEO_DIR": blocked / "videos",
                "DEFAULT_OUTPUT_DIR": blocked / "output",
                "DEFAULT_TIMELINE_DIR": blocked / "timelines",
                "DEFAULT_SUBMISSION_DIR": blocked / "submissions",
                "JSON_TIMELINE_UPLOAD_DIR": blocked / "json-upload",
                "MANUAL_TIMELINE_UPLOAD_DIR": blocked / "manual-upload",
            }
            with patch.dict(os.environ, environment, clear=False):
                with patch.multiple(app_module, **replacements):
                    client = app_module.app.test_client()
                    responses = (
                        client.get("/api/list-json-timelines", headers=headers),
                        client.get("/api/timelines", headers=headers),
                        client.post(
                            "/api/subtitles/scan",
                            json={},
                            headers=write_headers,
                        ),
                        client.post(
                            "/api/upload-json-timeline",
                            data={"file": (io.BytesIO(b"{}"), "timeline.json")},
                            headers=write_headers,
                        ),
                        client.post(
                            "/api/upload-timeline",
                            data={"file": (io.BytesIO(b"docx"), "timeline.docx")},
                            headers=write_headers,
                        ),
                        client.post(
                            "/api/start-pipeline",
                            json={"flv_path": str(Path(allowed_dir) / "input.flv")},
                            headers=write_headers,
                        ),
                    )
            for response in responses:
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(str(blocked), response.get_data(as_text=True))


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

    @staticmethod
    def _probe_result(status, reason=""):
        return AutoCoverProbeResult(status=status, reason=reason)

    def test_autocover_ready_redirects_to_configured_local_service(self):
        with patch.dict(
                os.environ,
                {"AUTOCOVER_URL": "http://127.0.0.1:5017"},
                clear=False), patch.object(
                    app_module,
                    "probe_autocover_endpoint",
                    return_value=self._probe_result(
                        AUTOCOVER_PROBE_READY,
                    ),
                ) as probe:
            response = self.client.get("/autocover")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "http://127.0.0.1:5017")
        endpoint = probe.call_args.args[0]
        self.assertEqual(endpoint.port, 5017)
        self.assertEqual(endpoint.probe_url, "http://127.0.0.1:5017/api/options")

    def test_autocover_root_trailing_slash_redirect_is_normalized(self):
        with patch.dict(
                os.environ,
                {"AUTOCOVER_URL": "http://127.0.0.1:5018/"},
                clear=False), patch.object(
                    app_module,
                    "probe_autocover_endpoint",
                    return_value=self._probe_result(AUTOCOVER_PROBE_READY),
                ) as probe:
            response = self.client.get("/autocover")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "http://127.0.0.1:5018")
        self.assertEqual(probe.call_args.args[0].browser_url, "http://127.0.0.1:5018")

    def test_autocover_not_started_returns_clear_503_status_page(self):
        result = self._probe_result(
            AUTOCOVER_PROBE_UNAVAILABLE,
            "连接被拒绝（127.0.0.1:5010 未监听）",
        )
        with patch.object(
                app_module,
                "probe_autocover_endpoint",
                return_value=result):
            response = self.client.get("/autocover")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 503)
        self.assertIn("AutoCover 未启动", html)
        self.assertIn("python 启动.py", html)
        self.assertIn("连接被拒绝", html)
        self.assertIn('href="/autocover"', html)
        self.assertIn('href="/"', html)
        self.assertIn('href="/subtitle-workflow"', html)

    def test_autocover_incompatible_service_returns_safe_actual_409_reason(self):
        result = self._probe_result(
            AUTOCOVER_PROBE_INCOMPATIBLE,
            "HTTP 404 Not Found",
        )
        with patch.object(
                app_module,
                "probe_autocover_endpoint",
                return_value=result):
            response = self.client.get("/autocover")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 409)
        self.assertIn("端口已被其他服务占用或服务版本不兼容", html)
        self.assertIn("HTTP 404 Not Found", html)
        self.assertNotIn("Traceback", html)
        self.assertNotIn("Cookie", html)
        self.assertNotIn("Token", html)

    def test_malicious_autocover_url_falls_back_without_external_probe(self):
        captured = {}

        def ready_probe(endpoint):
            captured["endpoint"] = endpoint
            return self._probe_result(AUTOCOVER_PROBE_READY)

        with patch.dict(
                os.environ,
                {"AUTOCOVER_URL": "https://example.com:8443/steal?token=secret"},
                clear=False), patch.object(
                    app_module,
                    "probe_autocover_endpoint",
                    side_effect=ready_probe,
                ):
            response = self.client.get("/autocover")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "http://127.0.0.1:5010")
        endpoint = captured["endpoint"]
        self.assertEqual(endpoint.browser_url, "http://127.0.0.1:5010")
        self.assertEqual(endpoint.probe_url, "http://127.0.0.1:5010/api/options")
        self.assertNotIn("example.com", endpoint.probe_url)

    def test_configured_localhost_dynamic_port_is_probed_on_ipv4_loopback(self):
        captured = {}

        def ready_probe(endpoint):
            captured["endpoint"] = endpoint
            return self._probe_result(AUTOCOVER_PROBE_READY)

        with patch.dict(
                os.environ,
                {"AUTOCOVER_URL": "http://localhost:5013"},
                clear=False), patch.object(
                    app_module,
                    "probe_autocover_endpoint",
                    side_effect=ready_probe,
                ):
            response = self.client.get("/autocover")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "http://localhost:5013")
        self.assertEqual(captured["endpoint"].port, 5013)
        self.assertEqual(
            captured["endpoint"].probe_url,
            "http://127.0.0.1:5013/api/options",
        )

    def test_autocover_status_page_has_no_iframe_or_polling(self):
        with patch.object(
                app_module,
                "probe_autocover_endpoint",
                return_value=self._probe_result(
                    AUTOCOVER_PROBE_UNAVAILABLE,
                    "连接被拒绝",
                )):
            html = self.client.get("/autocover").get_data(as_text=True)

        normalized = html.casefold()
        self.assertNotIn("<iframe", normalized)
        self.assertNotIn("setinterval", normalized)
        self.assertNotIn("settimeout", normalized)
        self.assertNotIn("fetch(", normalized)

    def test_autocover_probe_does_not_bypass_host_boundary(self):
        with patch.object(app_module, "probe_autocover_endpoint") as probe:
            response = self.client.get(
                "/autocover",
                headers={"Host": "attacker.example"},
            )

        self.assertEqual(response.status_code, 403)
        probe.assert_not_called()

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
            "background_filter_modes": [
                {"mode": "off", "label": "关闭"},
                {"mode": "soft", "label": "软过滤"},
                {"mode": "strict", "label": "严格过滤"},
            ],
            "subtitle_background_filter_default": "soft",
            "analysis_background_filter_default": "off",
            "background_filter_limit": "单音轨无法保证 100% 分离同时人声",
        }
        with patch("autoslice.topic_engine.funasr_public_status", return_value=public_status):
            response = self.client.get("/api/asr-status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), public_status)
        serialized = json.dumps(response.get_json(), ensure_ascii=False)
        self.assertNotIn("model_path", serialized)
        self.assertNotIn("model_source", serialized)
        self.assertEqual(
            [item["mode"] for item in response.get_json()["background_filter_modes"]],
            ["off", "soft", "strict"],
        )
        self.assertIn("无法保证 100%", response.get_json()["background_filter_limit"])

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

    def test_subtitle_transcription_defaults_to_soft_three_mode_control(self):
        html, script = self._page_script()

        self.assertIn('id="backgroundFilterMode"', html)
        self.assertIn('<option value="off">关闭：完整音轨</option>', html)
        self.assertIn('<option value="soft" selected>', html)
        self.assertIn('<option value="strict">', html)
        self.assertIn("可能吞掉有效人声，仅单人直播使用", html)
        self.assertIn("background_filter_mode:mode", script)
        self.assertIn("autoslice.subtitle-background-filter-mode", script)
        self.assertIn("autoslice.subtitle-foreground-only", script)
        self.assertIn("renderBackgroundFilterResult(pair?.background_filter)", script)
        self.assertIn("过滤模型 ${result.model}", script)
        self.assertIn("推理设备 ${result.device}", script)
        for marker in (
            "data.background_filter_mode",
            "pair.background_filter=data.background_filter",
            "result.detected_speaker_count",
            "result.candidate_segment_count",
            "result.removed_segment_count",
            "result.removed_seconds",
            "result.fallback_reason",
        ):
            self.assertIn(marker, script)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证背景音模式迁移")
    def test_subtitle_background_filter_storage_migrates_legacy_boolean(self):
        _html, script = self._page_script()
        start = script.index("const BACKGROUND_FILTER_STORAGE_KEY")
        end = script.index("function selectedBackgroundFilterMode")
        helpers = script[start:end]
        node_script = f"""
const QUEUE_SORT_KEYS=new Set();
const storedQueueSort=null;
{helpers}
function migrate(initial){{
  const values=new Map(Object.entries(initial));
  global.localStorage={{
    getItem:key=>values.has(key)?values.get(key):null,
    setItem:(key,value)=>values.set(key,String(value)),
    removeItem:key=>values.delete(key),
  }};
  const mode=loadStoredBackgroundFilterMode();
  return {{mode,stored:values.get(BACKGROUND_FILTER_STORAGE_KEY),legacy:values.has(LEGACY_FOREGROUND_FILTER_STORAGE_KEY)}};
}}
process.stdout.write(JSON.stringify([
  migrate({{'autoslice.subtitle-foreground-only':'1'}}),
  migrate({{'autoslice.subtitle-foreground-only':'0'}}),
  migrate({{}}),
]));
"""
        completed = subprocess.run(
            ["node", "-"],
            input=node_script,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        migrated = json.loads(completed.stdout)
        self.assertEqual([item["mode"] for item in migrated], ["soft", "off", "soft"])
        self.assertTrue(all(item["stored"] == item["mode"] for item in migrated))
        self.assertTrue(all(not item["legacy"] for item in migrated))

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
            "function deleteCue(index,render=true)",
            "function restoreCue(index)",
            "deleted_indices:removed",
            "cue-delete",
            "reviewDictionary",
            "renderReviewDictionary(data.review_profile)",
            "renderReviewDictionary(result)",
                "renderReferenceTitles();renderReviewDictionary();",
                "replacement_count",
                "extraGlossary",
                "default_glossary_count",
                "profile-replacements",
                "suggestionReplacementCandidate",
                "streamer_profile_id",
        ):
            self.assertIn(marker, script)
        self.assertIn("重新检查", html)
        self.assertNotIn(
            "data.task_id.startsWith('subtitle_review_'))applyReview(result)",
            script,
        )

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查字幕当前任务上下文")
    def test_subtitle_current_task_context_tracks_workflow_and_safe_fallbacks(self):
        html, script = self._page_script()
        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        self.assertIn('aria-label="当前任务上下文"', html)
        self.assertIn('href="/">智能分析</a>', html)
        self.assertIn('href="/autocover"', html)
        self.assertIn(
            "height:calc(100vh - var(--topbar-height) - var(--task-context-height))",
            html,
        )
        self.assertNotIn("<iframe", html.casefold())
        self.assertNotIn("setInterval(", script)
        runtime_prelude = r"""
const makeNode=()=>({
  textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,value:'',title:'',
  style:{},dataset:{},classList:{add(){},remove(){},toggle(){}},
  setAttribute(name,value){this[name]=String(value)},removeAttribute(){},
  querySelector(){return null},querySelectorAll(){return[]},addEventListener(){},focus(){},
});
const nodes=new Map();
globalThis.document={
  getElementById:(id)=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id);},
  querySelectorAll:()=>[],querySelector:()=>null,
};
globalThis.window={confirm:()=>true};
globalThis.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
"""
        runtime_assertions = r"""
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
renderCurrentTaskContext();
assert(document.getElementById('contextPhase').textContent==='未选投稿','初始阶段错误');

const dangerous='<svg onload=globalThis.contextExecuted=true>危险投稿</svg>';
state.pairs=[{
  id:'pair-1',title:dangerous,video_path:'C:\\private\\投稿.mp4',filename:'投稿.mp4',
  needs_transcription:true,has_source_srt:false,has_corrected_srt:false,has_output_video:false,
}];
state.selectedIndex=0;
renderCurrentTaskContext();
assert(document.getElementById('contextVideo').textContent===dangerous,'投稿标题没有安全显示');
assert(document.getElementById('contextVideo').innerHTML==='','投稿标题被写入 innerHTML');
assert(document.getElementById('contextPhase').textContent==='等待自动识别','待识别阶段错误');
assert(document.getElementById('contextPrevious').textContent==='原视频','待识别产物错误');

const restoredTask={
  task_id:'subtitle_transcription_restored',task_type:'subtitle_transcription',
  subtitle_pair_id:'pair-1',status:'running',progress:'刷新后继续识别',
};
state.taskEvents.set(restoredTask.task_id,restoredTask);
const restoredContext=restoreTaskContext(restoredTask);
updateTranscriptionTaskState(restoredTask,restoredContext);
renderCurrentTaskContext();
assert(document.getElementById('contextPhase').textContent==='识别任务中','识别任务阶段错误');

Object.assign(state.pairs[0],{needs_transcription:false,has_source_srt:true,transcription_status:null});
state.taskEvents.clear();state.taskContexts.clear();
state.sourceCues=[{index:1,text:'字幕'}];
renderCurrentTaskContext();
assert(document.getElementById('contextPhase').textContent==='字幕已加载','源字幕加载阶段错误');
assert(document.getElementById('contextPrevious').textContent==='源 SRT','源字幕产物错误');

for(const status of [undefined,'queued','running']){
  const taskId=`subtitle_review_active_${status||'pending'}`;
  state.taskContexts.set(taskId,{kind:'review',pairId:'pair-1'});
  if(status)state.taskEvents.set(taskId,{task_id:taskId,status});
  assert(activeSubtitleTaskKind(state.pairs[0])==='review',`${status||'尚无事件'} 没有保持活动任务`);
  state.taskContexts.clear();state.taskEvents.clear();
}
for(const status of ['done','error','interrupted','cancelled']){
  const taskId=`subtitle_review_terminal_${status}`;
  state.taskContexts.set(taskId,{kind:'review',pairId:'pair-1'});
  state.taskEvents.set(taskId,{task_id:taskId,status});
  assert(activeSubtitleTaskKind(state.pairs[0])==='',`${status} 被错误视为活动任务`);
  renderCurrentTaskContext();
  assert(document.getElementById('contextPhase').textContent==='字幕已加载',`${status} 仍保持忙碌阶段`);
  state.taskContexts.clear();state.taskEvents.clear();
}

state.reviewProfile={label:'主播 <img src=x onerror=1>'};
state.busy=true;
setTask('启动 AI 检查...');
assert(document.getElementById('contextPhase').textContent==='AI 校对中','AI 校对忙碌阶段错误');
assert(document.getElementById('contextStreamer').textContent.includes('<img'),'主播标签没有通过 textContent 写入');

state.busy=false;
Object.assign(state.pairs[0],{has_corrected_srt:true,has_output_video:false});
state.correctedPath='C:\\private\\投稿_校对.srt';
renderCurrentTaskContext();
assert(document.getElementById('contextPhase').textContent==='已保存校对字幕','保存后的阶段错误');
assert(document.getElementById('contextPrevious').textContent==='校对 SRT','保存后的产物错误');
assert(document.getElementById('contextNext').textContent==='生成标题或压制成片','保存后的下一步错误');

state.pairs[0].has_output_video=true;
renderCurrentTaskContext();
assert(document.getElementById('contextPrevious').textContent==='压制成片','压制后的产物错误');
assert(document.getElementById('contextNext').textContent==='前往 AutoCover','压制后的下一步错误');
const contextText=['contextVideo','contextPhase','contextPrevious','contextNext','contextStreamer','contextResult']
  .map(id=>`${document.getElementById(id).textContent}\n${document.getElementById(id).title}`).join('\n');
assert(!contextText.includes('C:\\private'),'字幕上下文暴露了本机绝对路径');

state.reviewProfile=null;state.pairs[0].title='';state.pairs[0].video_path='C:\\private\\fallback.mp4';
renderCurrentTaskContext();
assert(document.getElementById('contextVideo').textContent==='fallback.mp4','缺标题时没有回退安全文件名');
assert(document.getElementById('contextStreamer').textContent==='待识别','缺主播字段回退错误');
assert(document.getElementById('contextResult').textContent==='随投稿目录保存','结果目录描述错误');
assert(globalThis.contextExecuted!==true,'危险投稿标题被执行');
"""
        result = subprocess.run(
            ["node", "-"],
            input=runtime_prelude + script_prefix + runtime_assertions,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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
                "state.acceptedSuggestions",
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
const candidate=suggestionReplacementCandidate({original:'英英晚上好',corrected:'音音晚上好'});
if(candidate?.source!=='英英'||candidate?.target!=='音音')throw new Error('没有提取安全的错词映射');
if(suggestionReplacementCandidate({original:'abc',corrected:'xyz'})!==null)throw new Error('整句建议不应直接成为主播映射');
state.filter='suggested';
if(visibleSuggestionEntries().length!==3)throw new Error('当前筛选没有完整收集建议');
if(suggestionGroups(visibleSuggestionEntries()).length!==2)throw new Error('相同建议没有合并展示');
acceptVisibleSuggestions();
if(state.edited.get(1)!=='校正结果'||state.edited.get(2)!=='校正结果')throw new Error('批量采纳未应用到未保护建议');
if(state.edited.get(3)!=='用户手工修改')throw new Error('批量采纳覆盖了手工修改');
if(state.acceptedSuggestions.size!==2||!state.acceptedSuggestions.has(1)||!state.acceptedSuggestions.has(2))throw new Error('批量采纳没有记录独立的已采纳状态');
if(state.ignoredSuggestions.size!==0)throw new Error('批量采纳错误混入 ignoredSuggestions');
if(visibleSuggestionEntries().length!==1||visibleSuggestionEntries()[0].cue.index!==3)throw new Error('采纳后未保护建议没有消失，或被保护建议被错误隐藏');
ignoreVisibleSuggestions();
if(state.ignoredSuggestions.size!==1||!state.ignoredSuggestions.has(3))throw new Error('批量忽略没有独立作用于剩余可见建议');
if([...state.acceptedSuggestions].some(index=>state.ignoredSuggestions.has(index)))throw new Error('ignore 与 accepted 状态发生混用');
if(state.edited.get(3)!=='用户手工修改')throw new Error('忽略操作覆盖了手工修改');
undoSuggestionAction();
if(state.ignoredSuggestions.size!==0||visibleSuggestionEntries().length!==1)throw new Error('忽略撤销未恢复剩余建议状态');
if(state.acceptedSuggestions.size!==2)throw new Error('忽略撤销破坏了独立的已采纳状态');
undoSuggestionAction();
if(state.edited.get(1)!=='原文一'||state.edited.get(2)!=='原文一')throw new Error('采纳撤销未恢复原文');
if(state.acceptedSuggestions.size!==0||visibleSuggestionEntries().length!==3)throw new Error('采纳撤销未恢复全部建议可见状态');
state.edited.set(1,'校正结果');
state.aiApplied.add(1);
if(visibleSuggestionEntries().length!==3)throw new Error('AI 默认预应用建议不应自动隐藏');
state.edited=new Map(state.sourceCues.map(cue=>[cue.index,cue.text]));
state.aiApplied.clear();
state.protectedEdits.clear();
state.suggestionUndoStack=[];
acceptVisibleSuggestions();
if(visibleSuggestionEntries().length!==0||state.acceptedSuggestions.size!==3)throw new Error('全无保护场景全部采纳后仍有可见建议');
if(state.ignoredSuggestions.size!==0)throw new Error('全量采纳错误复用了忽略状态');
undoSuggestionAction();
if(visibleSuggestionEntries().length!==3||state.acceptedSuggestions.size!==0)throw new Error('全量采纳撤销未恢复全部建议');
if(state.edited.get(1)!=='原文一'||state.edited.get(2)!=='原文一'||state.edited.get(3)!=='原文三')throw new Error('全量采纳撤销未恢复字幕正文');
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

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证字幕渐进披露和保存依赖")
    def test_subtitle_progressive_disclosure_and_saved_artifact_dependency(self):
        html, script = self._page_script()
        for marker in (
                'id="workflowSteps"',
                'data-workflow-step="prepare"',
                'data-workflow-step="proofread"',
                'data-workflow-step="suggestions"',
                'data-workflow-step="quality"',
                'data-workflow-step="save"',
                '<details id="asrDisclosure"',
                '<details id="suggestionDisclosure"',
                '<details id="qualityDisclosure"',
                'id="saveDependencyNote"',
                '使用已保存字幕开始压制',
        ):
            self.assertIn(marker, html)
        for marker in (
                "function asrDisclosureNeeded(pair=selectedPair())",
                "function savedSubtitleReady(pair=selectedPair())",
                "state.savedEditFingerprint",
                "details.open=wasOpen",
                "Boolean(visible.length||receipts)&&wasOpen",
                "压制</strong>和参考标题需先显式保存",
        ):
            self.assertIn(marker, script)

        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        runtime_assertions = r'''
const makeNode=()=>({
  textContent:'',innerHTML:'',hidden:false,disabled:false,value:'',open:false,dataset:{},style:{},
  classList:{toggle(){},add(){},remove(){}},querySelector(){return null},querySelectorAll(){return[]},
  setAttribute(){},removeAttribute(){},addEventListener(){},focus(){},
});
const nodes=new Map();
globalThis.document={
  getElementById:id=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id)},
  querySelectorAll:()=>[],querySelector:()=>null,
};
globalThis.window={confirm:()=>true};
state.pairs=[{id:'pair-1',needs_transcription:true,subtitle_error:'',has_corrected_srt:true}];
state.selectedIndex=0;
if(!asrDisclosureNeeded())throw new Error('缺字幕没有展开 ASR 设置');
state.pairs[0].needs_transcription=false;
state.pairs[0].has_source_srt=true;
if(asrDisclosureNeeded())throw new Error('正常字幕错误展开 ASR 设置');
state.pairs[0].subtitle_error='字幕损坏';
if(!asrDisclosureNeeded())throw new Error('字幕异常没有展开 ASR 设置');
state.pairs[0].subtitle_error='';
state.sourceCues=[{index:1,text:'原文',start_seconds:0,end_seconds:1}];
state.edited=new Map([[1,'原文']]);
state.correctedPath='C:\\saved.srt';
state.savedEditFingerprint=subtitleEditFingerprint();
if(!savedSubtitleReady())throw new Error('保存结果存在时没有解锁依赖操作');
state.edited.set(1,'未保存修改');
if(savedSubtitleReady())throw new Error('未保存修改仍解锁依赖操作');
state.edited.set(1,'原文');
state.suggestions=new Map([[1,{index:1,original:'原文',corrected:'校正',reason:'上下文',confidence:.9}]]);
const suggestionDetails=nodes.get('suggestionDisclosure')||makeNode();
nodes.set('suggestionDisclosure',suggestionDetails);
renderSuggestionTools();
if(nodes.get('suggestionSummary').innerHTML.includes('1 条待处理')===false)throw new Error('建议默认摘要没有显示数量');
if(suggestionDetails.open)throw new Error('建议详情默认没有收起');
suggestionDetails.open=true;
renderSuggestionTools();
if(!suggestionDetails.open)throw new Error('用户展开的建议详情被无故收起');
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
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证参考标题的保存依赖")
    def test_reference_title_copy_requires_currently_saved_subtitle(self):
        _html, script = self._page_script()
        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        runtime_assertions = r'''
const makeNode=()=>(
  {textContent:'',innerHTML:'',hidden:false,disabled:false,value:'',open:false,dataset:{},style:{},
   classList:{toggle(){},add(){},remove(){}},querySelector(){return null},querySelectorAll(){return[]},
   setAttribute(){},removeAttribute(){},addEventListener(){},focus(){}}
);
const nodes=new Map();
globalThis.document={
  getElementById:id=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id)},
  querySelectorAll:()=>[],querySelector:()=>null,
};
globalThis.window={confirm:()=>true};
let copied='';
globalThis.navigator.clipboard={writeText:async value=>{copied=value;}};
state.pairs=[{
  id:'pair-1',title:'测试投稿',video_path:'C:\\video.mp4',srt_path:'C:\\source.srt',
  corrected_srt_path:'C:\\saved.srt',has_source_srt:true,has_corrected_srt:true,
  needs_transcription:false,subtitle_error:'',
}];
state.selectedIndex=0;
state.sourceCues=[{index:1,text:'原文',start_seconds:0,end_seconds:1}];
state.edited=new Map([[1,'原文']]);
state.correctedPath='C:\\saved.srt';
state.savedEditFingerprint=subtitleEditFingerprint();
jsonRequest=async()=>({task_id:'subtitle_title_1'});
registerTask=()=>{};
const cueEvents={};
const textarea={value:'原文',style:{},scrollHeight:42,addEventListener:(type,handler)=>{cueEvents[`textarea:${type}`]=handler;}};
const checkbox={checked:false,addEventListener:(type,handler)=>{cueEvents[`checkbox:${type}`]=handler;}};
const cueRow={
  dataset:{index:'1'},
  classList:{toggle(){}},
  addEventListener(){},
  querySelector(selector){if(selector==='.cue-edit')return textarea;if(selector==='.cue-check')return checkbox;return null;},
  querySelectorAll:()=>[],
};
wireCueRow(cueRow);
(async()=>{
  if(!savedSubtitleReady())throw new Error('保存后的字幕没有满足标题依赖');
  await generateReferenceTitle();
  state.referenceTitles={recommended_title:'推荐标题'};
  setBusy(false);
  renderReferenceTitles();
  if(nodes.get('copyTitleButton').disabled)throw new Error('保存并生成标题后复制按钮仍被禁用');
  await copyRecommendedTitle();
  if(copied!=='推荐标题')throw new Error('保存并生成标题后复制函数没有复制标题');

  textarea.value='未保存修改';
  cueEvents['textarea:input']();
  if(!nodes.get('generateTitleButton').disabled)throw new Error('字幕未保存时生成按钮没有禁用');
  if(!nodes.get('copyTitleButton').disabled)throw new Error('字幕未保存时复制按钮没有禁用');
  copied='';
  await copyRecommendedTitle();
  if(copied!=='')throw new Error('字幕未保存时复制函数仍然写入剪贴板');

  state.edited.set(1,'原文');
  state.savedEditFingerprint=subtitleEditFingerprint();
  renderReferenceTitles();
  state.suggestions=new Map([[1,{corrected:'勾选后的修改'}]]);
  checkbox.checked=true;
  cueEvents['checkbox:change']();
  if(!nodes.get('copyTitleButton').disabled)throw new Error('checkbox 修改后复制按钮没有禁用');
  let titleRequests=0;
  jsonRequest=async()=>{titleRequests+=1;return{task_id:'unexpected_title_task'};};
  await generateReferenceTitle();
  if(titleRequests!==0)throw new Error('字幕未保存时生成函数仍然发起标题请求');
})().catch(error=>{console.error(error);process.exitCode=1;});
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
            encoding="utf-8",
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
                "function unmergeGroup(index,force=false)",
                "function restoreCorrectedState(",
                "merge_pairs:pairs",
                "merge_overrides:overrides",
                "合并原文",
                "合上",
                "合下",
                "拆开",
                "cue-delete-preview",
                "cue-shift-actions"):
            self.assertIn(marker, script)
        self.assertIn("@media(max-width:760px)", _html)

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
        self.assertIn("row.addEventListener('click'", script)
        self.assertIn(
            "event.target?.closest?.('textarea,input,button,label,a,select')",
            script,
        )
        self.assertIn(".cue-detail{", html)
        self.assertIn("display:none", html.split(".cue-detail{", 1)[1].split("}", 1)[0])
        self.assertIn(
            ".cue-row.selected .cue-detail,.cue-row:focus-within .cue-detail{display:grid;",
            html,
        )
        self.assertIn("min-width:0", html.split(".cue-detail{", 1)[1].split("}", 1)[0])
        self.assertIn(".cue-time-input{width:100%;box-sizing:border-box}", html)

    def test_review_page_exposes_local_subtitle_quality_contract(self):
        html, script = self._page_script()

        for marker in (
                'id="qualityPanel"',
                'id="qualityCount"',
                'id="qualitySummary"',
                'id="qualityList"',
                "本地确定性规则",
                "提供可撤销的处理建议",
                "不会未经确认删除或写入字幕",
                "不属于 AI 建议批量工具"):
            self.assertIn(marker, html)
        for marker in (
                "qualityIgnored:new Set()",
                "qualityActions:[]",
                "function qualityIssues()",
                "function renderQualityList()",
                "function locateQualityIssue(issue)",
                "function ignoreQualityIssue(issueId)",
                "function executeQualityAction(issue)",
                "function undoQualityAction(actionId)",
                "QUALITY_RENDER_DELAY_MS=140",
                "function scheduleQualityRender()",
                "function cancelScheduledQualityRender()",
                "background-noise",
                "exact-duplicate",
                "symbol-or-short",
                "low-confidence-semantic",
                "deletion-blank",
                "abnormal-gap",
                "bridge-gap",
                "overflow-risk",
                "segmentation-residue"):
            self.assertIn(marker, script)
        self.assertIn("overflow-wrap:anywhere", html.split(".quality-panel{", 1)[1])
        self.assertIn(
            ".quality-card{grid-template-columns:minmax(0,1fr)}",
            html,
        )

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查字幕质检行为")
    def test_review_quality_rules_are_local_conservative_and_reversible(self):
        _html, script = self._page_script()
        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        runtime_assertions = r"""
const node=()=>({textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,
  value:'',style:{},dataset:{},classList:{toggle:()=>{}},setAttribute:()=>{},
  removeAttribute:()=>{},focus:()=>{},querySelector:()=>null,querySelectorAll:()=>[],
  addEventListener:()=>{}});
const nodes=new Map();
globalThis.window={confirm:()=>true,getSelection:()=>({type:'None',toString:()=>''})};
globalThis.requestAnimationFrame=(callback)=>callback();
let located=false;
globalThis.document={
  getElementById:(id)=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);},
  querySelectorAll:()=>[],
  querySelector:(selector)=>selector.includes('.cue-row[data-index="7"]')?{
    scrollIntoView:()=>{located=true;},focus:()=>{located=true;}
  }:null,
};
document.getElementById('fontSize').value='20';
document.getElementById('outlineWidth').value='100';
state.sourceCues=[
  {index:1,text:'[音乐]',start_seconds:0,end_seconds:1,start:'00:00:00,000',end:'00:00:01,000'},
  {index:2,text:'重复内容',start_seconds:1.1,end_seconds:2,start:'00:00:01,100',end:'00:00:02,000'},
  {index:3,text:'重复内容',start_seconds:2.1,end_seconds:3,start:'00:00:02,100',end:'00:00:03,000'},
  {index:4,text:'***',start_seconds:3.1,end_seconds:3.8,start:'00:00:03,100',end:'00:00:03,800'},
  {index:5,text:'啊',start_seconds:3.9,end_seconds:4.3,start:'00:00:03,900',end:'00:00:04,300'},
  {index:6,text:'这句完全不通',start_seconds:4.4,end_seconds:5.6,start:'00:00:04,400',end:'00:00:05,600'},
  {index:7,text:'本来填补空白',start_seconds:5.7,end_seconds:11.8,start:'00:00:05,700',end:'00:00:11,800'},
  {index:8,text:'继续说话',start_seconds:12,end_seconds:13,start:'00:00:12,000',end:'00:00:13,000'},
  {index:9,text:'正常前句',start_seconds:20,end_seconds:21,start:'00:00:20,000',end:'00:00:21,000'},
  {index:10,text:'这是一个明显超过默认单行安全字数并可能溢出屏幕的字幕',start_seconds:21.1,end_seconds:22.5,start:'00:00:21,100',end:'00:00:22,500'},
  {index:11,text:'所以',start_seconds:22.6,end_seconds:23,start:'00:00:22,600',end:'00:00:23,000'},
  {index:12,text:'接着说明情况',start_seconds:23.1,end_seconds:24,start:'00:00:23,100',end:'00:00:24,000'},
  {index:13,text:'了 后面的尾字漂移',start_seconds:24.1,end_seconds:25,start:'00:00:24,100',end:'00:00:25,000'},
  {index:14,text:'巨大重叠',start_seconds:30,end_seconds:40,start:'00:00:30,000',end:'00:00:40,000'},
  {index:15,text:'巨大重叠',start_seconds:31,end_seconds:32,start:'00:00:31,000',end:'00:00:32,000'},
  {index:16,text:'轻微重叠',start_seconds:42,end_seconds:44,start:'00:00:42,000',end:'00:00:44,000'},
  {index:17,text:'轻微重叠',start_seconds:43,end_seconds:45,start:'00:00:43,000',end:'00:00:45,000'},
];
state.edited=new Map(state.sourceCues.map(cue=>[cue.index,cue.text]));
state.deleted.clear();
state.deleted.add(7);
state.mergePrevious.clear();
state.mergeOverrides.clear();
state.timeOverrides.clear();
state.suggestions=new Map([[6,{index:6,original:'这句完全不通',corrected:'这句话需要人工确认',reason:'语义异常，无法确认',confidence:.55}]]);
state.qualityIgnored.clear();
state.qualityActions=[];
renderCues=()=>{};

let issues=qualityIssues();
const types=new Set(issues.map(issue=>issue.type));
for(const type of ['background-noise','exact-duplicate','symbol-or-short','low-confidence-semantic','deletion-blank','abnormal-gap','overflow-risk','segmentation-residue']){
  if(!types.has(type))throw new Error(`缺少质检类型 ${type}`);
}
if(issues.some(issue=>issue.type==='exact-duplicate'&&issue.indices.includes(15)))throw new Error('巨大负 overlap 被误判为完全重复');
if(!issues.some(issue=>issue.type==='exact-duplicate'&&issue.indices.includes(17)))throw new Error('轻微 overlap 没有进入完全重复候选');
if(state.deleted.size!==1||state.deleteSelection.size!==0)throw new Error('质检分析自动改变了删除状态');

state.timeOverrides.set(8,{start:12.2,end:13.2});
let gapIssue=qualityIssues().find(issue=>issue.type==='abnormal-gap'&&issue.indices.includes(8)&&issue.indices.includes(9));
if(gapIssue?.action?.kind!=='bridge-gap'||gapIssue.action.label!=='均分 gap（可撤销）')throw new Error('异常 gap 没有提供可执行且可撤销的均分动作');
for(const path of ['实际停顿','忽略','时间错位','均分 gap','手工调整前条结束/后条开始','疑似漏字','重新识别','核对源字幕'])if(!gapIssue.recommendation.includes(path))throw new Error(`异常 gap 建议缺少处理路径：${path}`);
const beforeCancelledGap=JSON.stringify([...state.timeOverrides]);
let confirmMessage='';
window.confirm=(message)=>{confirmMessage=message;return false;};
if(executeQualityAction(gapIssue)!==false)throw new Error('取消 confirm 后仍执行了均分 gap');
if(JSON.stringify([...state.timeOverrides])!==beforeCancelledGap||state.qualityActions.length!==0)throw new Error('取消 confirm 后修改了时间或动作状态');
for(const detail of ['前条结束时间','后条开始时间','空白中点两侧','约 0.08 秒间隔','只修改当前编辑态','不会立即保存字幕','可以撤销'])if(!confirmMessage.includes(detail))throw new Error(`均分 gap 确认说明缺少：${detail}`);
state.timeOverrides.set(9,{start:16.1,end:21});
const beforeShortGap=JSON.stringify([...state.timeOverrides]);
if(executeQualityAction(gapIssue)!==false||JSON.stringify([...state.timeOverrides])!==beforeShortGap||state.qualityActions.length!==0)throw new Error('gap 已不足 3 秒时没有安全拒绝');
state.timeOverrides.delete(9);
state.mergePrevious.set(9,8);
if(executeQualityAction(gapIssue)!==false||state.qualityActions.length!==0)throw new Error('相邻分组关系变化后没有安全拒绝');
state.mergePrevious.delete(9);
window.confirm=()=>true;
const leftTimingBefore={...groupTiming(8)},rightTimingBefore={...groupTiming(9)};
const expectedMidpoint=(leftTimingBefore.end+rightTimingBefore.start)/2;
if(!executeQualityAction(gapIssue))throw new Error('确认后没有执行均分 gap');
const bridgedLeft=groupTiming(8),bridgedRight=groupTiming(9);
if(Math.abs(bridgedLeft.start-leftTimingBefore.start)>.0005||Math.abs(bridgedLeft.end-(expectedMidpoint-.04))>.0005)throw new Error('均分 gap 没有使用前条当前 timing');
if(Math.abs(bridgedRight.start-(expectedMidpoint+.04))>.0005||Math.abs(bridgedRight.end-rightTimingBefore.end)>.0005)throw new Error('均分 gap 没有使用后条当前 timing');
if(Math.abs((bridgedRight.start-bridgedLeft.end)-.08)>.0005)throw new Error('均分 gap 后没有保留约 0.08 秒间隔');
if(qualityIssues().some(issue=>issue.type==='abnormal-gap'&&issue.indices.includes(8)&&issue.indices.includes(9)))throw new Error('均分 gap 后异常候选没有实时消失');
const bridgeAction=state.qualityActions.at(-1);
if(bridgeAction?.kind!=='bridge-gap'||!bridgeAction.leftTiming.had||bridgeAction.rightTiming.had)throw new Error('均分 gap 没有记录左右原 timeOverrides 状态');
renderQualityList();
const gapReceipt=document.getElementById('qualityList').innerHTML;
if(!gapReceipt.includes('已执行建议')||!gapReceipt.includes('尚未保存字幕')||!gapReceipt.includes('撤销建议动作'))throw new Error('均分 gap 后没有显示明确回执或撤销入口');
if(!undoQualityAction(bridgeAction.id))throw new Error('均分 gap 建议无法撤销');
if(JSON.stringify(state.timeOverrides.get(8))!==JSON.stringify({start:12.2,end:13.2})||state.timeOverrides.has(9))throw new Error('撤销均分 gap 没有恢复左右此前已有或没有的 timeOverrides');
gapIssue=qualityIssues().find(issue=>issue.type==='abnormal-gap'&&issue.indices.includes(8)&&issue.indices.includes(9));
if(!executeQualityAction(gapIssue))throw new Error('撤销后无法再次执行均分 gap');
const protectedBridgeAction=state.qualityActions.at(-1);
const manualLeft={...groupTiming(8),end:groupTiming(8).end-.2};
state.timeOverrides.set(8,manualLeft);
if(undoQualityAction(protectedBridgeAction.id)!==false||!qualityTimingMatches(groupTiming(8),manualLeft))throw new Error('均分 gap 撤销覆盖了后续手工时间修改');
if(state.qualityActions.some(action=>action.id===protectedBridgeAction.id)||!document.getElementById('taskStatus').textContent.includes('保护现有修改'))throw new Error('手工改动后没有拒绝撤销并清理陈旧回执');
state.timeOverrides.set(8,{start:12.2,end:13.2});
state.timeOverrides.delete(9);
gapIssue=qualityIssues().find(issue=>issue.type==='abnormal-gap'&&issue.indices.includes(8)&&issue.indices.includes(9));
if(!executeQualityAction(gapIssue))throw new Error('清理保护状态后无法再次执行均分 gap');
const staleBridgeAction=state.qualityActions.at(-1);
state.timeOverrides.set(9,{...groupTiming(9),start:groupTiming(9).start+.2});
renderQualityList();
if(state.qualityActions.some(action=>action.id===staleBridgeAction.id)||document.getElementById('qualityList').innerHTML.includes(`data-quality-undo="${staleBridgeAction.id}"`))throw new Error('其他入口修改时间后没有清理陈旧均分 gap 回执');
state.timeOverrides.set(8,{start:12.2,end:13.2});
state.timeOverrides.delete(9);

state.qualityActions=[];
state.qualityActionSerial=0;
for(let index=0;index<21;index+=1){state.deleteSelection.add(100+index);recordQualityAction({issueId:`history-${index}`,kind:'preview-delete',root:100+index,label:'test',location:'test'});}
pruneStaleQualityActions();
if(state.qualityActions.length!==21||state.qualityActions[0].issueId!=='history-0')throw new Error('超过 20 条后静默丢弃了仍生效的撤销记录');
state.qualityActions=[];
state.qualityActionSerial=0;
state.deleteSelection.clear();

const duplicate=issues.find(issue=>issue.type==='exact-duplicate');
if(duplicate?.action?.kind!=='preview-delete')throw new Error('重复字幕没有使用删除预览建议');
if(!duplicate.context.includes('重复内容')||!duplicate.recommendation.includes('删除预览'))throw new Error('质检结果缺少可见上下文或保守建议');
executeQualityAction(duplicate);
if(!state.deleteSelection.has(3)||state.deleted.has(3))throw new Error('建议动作直接删除字幕或没有加入预览');
state.deleteSelection.delete(3);
renderQualityList();
if(state.qualityActions.length!==0)throw new Error('手工取消删除预览后没有清理陈旧 action');
if(!executeQualityAction(duplicate)||!state.deleteSelection.has(3))throw new Error('清理删除预览陈旧记录后无法重新执行建议');
const previewAction=state.qualityActions.at(-1);
undoQualityAction(previewAction.id);
if(state.deleteSelection.has(3))throw new Error('删除预览建议无法撤销');
executeQualityAction(duplicate);
const confirmedDeleteAction=state.qualityActions.at(-1);
state.deleteSelection.delete(3);
state.deleted.add(3);
if(undoQualityAction(confirmedDeleteAction.id)!==false||state.qualityActions.length!==1)throw new Error('已确认删除被质检撤销或丢失动作记录');
state.deleted.delete(3);
state.qualityActions=[];

const mergeIssue=issues.find(issue=>issue.type==='segmentation-residue'&&issue.action?.kind==='merge');
if(!mergeIssue)throw new Error('连接词残留没有保守合并建议');
executeQualityAction(mergeIssue);
if(state.mergePrevious.get(12)!==11)throw new Error('合并建议没有复用现有相邻合并 owner');
state.mergePrevious.delete(12);
renderQualityList();
if(state.qualityActions.length!==0)throw new Error('手工拆开质检合并后没有清理陈旧 action');
if(!executeQualityAction(mergeIssue)||state.mergePrevious.get(12)!==11)throw new Error('清理合并陈旧记录后无法重新执行建议');
const mergeAction=state.qualityActions.at(-1);
undoQualityAction(mergeAction.id);
if(state.mergePrevious.has(12))throw new Error('合并建议无法撤销');
executeQualityAction(mergeIssue);
const protectedMergeAction=state.qualityActions.at(-1);
state.mergeOverrides.set(11,'用户合并后手工改写');
if(undoQualityAction(protectedMergeAction.id)!==false||state.mergeOverrides.get(11)!=='用户合并后手工改写')throw new Error('撤销覆盖了合并后的手工编辑');
state.mergeOverrides.delete(11);
state.mergePrevious.delete(12);
state.qualityActions=[];

const overflow=qualityIssues().find(issue=>issue.type==='overflow-risk'&&issue.indices.includes(10));
ignoreQualityIssue(overflow.id);
if(visibleQualityIssues().some(issue=>issue.id===overflow.id))throw new Error('忽略没有只隐藏质检候选');
if(state.edited.get(10)!==state.sourceCues[9].text||state.deleted.size!==1)throw new Error('忽略修改了字幕正文或删除状态');

state.edited.set(10,'长度正常');
if(qualityIssues().some(issue=>issue.type==='overflow-risk'&&issue.indices.includes(10)))throw new Error('编辑后没有实时消除超长候选');
state.timeOverrides.set(9,{start:13.4,end:21});
if(qualityIssues().some(issue=>issue.type==='abnormal-gap'&&issue.indices.includes(9)))throw new Error('时间调整后没有实时重算 gap');
state.deleted.add(3);
if(qualityIssues().some(issue=>issue.type==='exact-duplicate'&&issue.indices.includes(3)))throw new Error('删除状态没有实时重算重复候选');
state.suggestions.clear();
if(qualityIssues().some(issue=>issue.type==='low-confidence-semantic'))throw new Error('没有 AI confidence 证据仍生成低置信候选');

const blank=qualityIssues().find(issue=>issue.type==='deletion-blank');
locateQualityIssue(blank);
if(state.filter!=='changed'||state.selectedCueIndex!==7||!located)throw new Error('已删除候选无法定位并展开');
state.qualityIgnored.add('stale-issue');
state.qualityActions.push({id:99,issueId:'stale-issue'});
clearEditor();
if(state.qualityIgnored.size||state.qualityActions.length)throw new Error('重新加载没有安全清空质检忽略和撤销状态');
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
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查质检重算调度")
    def test_review_quality_render_scheduler_debounces_and_cancels_stale_work(self):
        _html, script = self._page_script()
        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        runtime_assertions = r"""
const scheduled=[];
const cleared=[];
globalThis.setTimeout=(callback,delay)=>{const id=scheduled.length+1;scheduled.push({id,callback,delay});return id;};
globalThis.clearTimeout=(id)=>{cleared.push(id);};
const node=()=>({textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,
  value:'',style:{},classList:{toggle:()=>{}},setAttribute:()=>{},removeAttribute:()=>{},
  querySelector:()=>null,querySelectorAll:()=>[],addEventListener:()=>{}});
globalThis.window={};
globalThis.document={getElementById:()=>node(),querySelectorAll:()=>[],querySelector:()=>null};
let renders=0;
renderQualityList=()=>{renders+=1;};
state.selectionVersion=7;
scheduleQualityRender();
if(scheduled.length!==1||scheduled[0].delay<100||scheduled[0].delay>180||renders!==0)throw new Error('quality render was not debounced');
scheduleQualityRender();
if(!cleared.includes(1)||scheduled.length!==2||renders!==0)throw new Error('second schedule did not replace the first timer');
scheduled[0].callback();
if(renders!==0)throw new Error('cancelled timer still rendered');
scheduled[1].callback();
if(renders!==1||state.qualityRenderTimer!==null)throw new Error('latest timer did not render exactly once');
scheduleQualityRender();
const staleCallback=scheduled[2].callback;
clearEditor();
const rendersAfterClear=renders;
if(!cleared.includes(3)||state.qualityRenderTimer!==null)throw new Error('clearEditor did not cancel pending quality render');
staleCallback();
if(renders!==rendersAfterClear)throw new Error('old video timer polluted the cleared editor');
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
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查字幕无障碍行为")
    def test_subtitle_accessibility_behaviour_uses_fixed_browser_mock(self):
        html, script = self._page_script()
        with self.client.get("/static/workbench.css") as stylesheet_response:
            css = stylesheet_response.get_data(as_text=True)

        self.assertIn('<label for="extraGlossary">本视频额外词条</label>', html)
        self.assertIn('id="settings-tab-style"', html)
        self.assertIn('aria-controls="settings-view-style"', html)
        self.assertIn('role="tabpanel" aria-labelledby="settings-tab-style"', html)
        self.assertIn('data-filter="all" class="active" aria-pressed="true"', html)
        self.assertIn('aria-pressed="${current}"', script)
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn("transition-duration: 0.01ms", css)

        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        runtime_assertions = r'''
const makeNode=(name,dataset={})=>{
  const attributes=new Map();
  const handlers={};
  const node={name,dataset,hidden:false,handlers,
    classList:{values:new Set(),toggle:(value,active)=>{if(active)node.classList.values.add(value);else node.classList.values.delete(value);}},
    setAttribute:(key,value)=>attributes.set(key,String(value)),
    getAttribute:(key)=>attributes.get(key),
    addEventListener:(type,handler)=>{handlers[type]=handler;},
    focus:()=>{node.focused=true;globalThis.document.activeElement=node;},
  };
  return node;
};
const tabs=[makeNode('style',{settingsTab:'style'}),makeNode('export',{settingsTab:'export'})];
const views=[makeNode('style-view',{settingsView:'style'}),makeNode('export-view',{settingsView:'export'})];
const filters=[makeNode('suggested',{filter:'suggested'}),makeNode('all',{filter:'all'}),makeNode('changed',{filter:'changed'})];
globalThis.document={activeElement:null,querySelectorAll:(selector)=>{
  if(selector==='.settings-tabs [data-settings-tab]')return tabs;
  if(selector==='.settings-view[data-settings-view]')return views;
  if(selector==='#cueFilters button')return filters;
  return [];
}};
initSettingsTabs();
if(tabs[0].getAttribute('tabindex')!=='0'||tabs[1].getAttribute('tabindex')!=='-1')throw new Error('字幕 tab 初始 roving tabindex 错误');
let prevented=false;
tabs[0].handlers.keydown({key:'ArrowRight',currentTarget:tabs[0],preventDefault:()=>{prevented=true;}});
if(!prevented||document.activeElement!==tabs[1]||tabs[1].getAttribute('aria-selected')!=='true'||tabs[0].getAttribute('tabindex')!=='-1')throw new Error('ArrowRight 没有移动焦点或更新 roving 状态');
tabs[1].handlers.keydown({key:'ArrowLeft',currentTarget:tabs[1],preventDefault:()=>{}});
if(document.activeElement!==tabs[0]||tabs[0].getAttribute('aria-selected')!=='true')throw new Error('ArrowLeft 没有移动焦点');
tabs[0].handlers.keydown({key:'End',currentTarget:tabs[0],preventDefault:()=>{}});
if(document.activeElement!==tabs[1]||tabs[1].getAttribute('tabindex')!=='0')throw new Error('End 没有移动到末尾 tab');
tabs[1].handlers.keydown({key:'Home',currentTarget:tabs[1],preventDefault:()=>{}});
if(document.activeElement!==tabs[0]||tabs[0].getAttribute('tabindex')!=='0')throw new Error('Home 没有移动到首个 tab');
state.filter='changed';
updateCueFilterButtons();
if(filters[2].getAttribute('aria-pressed')!=='true'||filters[0].getAttribute('aria-pressed')!=='false')throw new Error('筛选按钮 aria-pressed 没有同步');
state.sourceCues=[
  {index:1,start:'00:00:01,000',end:'00:00:02,500',start_seconds:1,end_seconds:2.5,text:'第一句'},
  {index:2,start:'00:00:02,500',end:'00:00:04,000',start_seconds:2.5,end_seconds:4,text:'第二句'}
];
state.edited=new Map(state.sourceCues.map(cue=>[cue.index,cue.text]));
state.filter='all';
const firstCue=renderCueRow(state.sourceCues[0]);
const secondCue=renderCueRow(state.sourceCues[1]);
if(!firstCue.includes('id="cue-edit-1"')||!secondCue.includes('id="cue-edit-2"'))throw new Error('动态字幕编辑框 id 不唯一');
if(!firstCue.includes('for="cue-edit-1"')||!firstCue.includes('第 1 条字幕')||!firstCue.includes('00:00:01,000–00:00:02,500')||!firstCue.includes('校对后文本'))throw new Error('动态字幕编辑框缺少真实时间范围 label');
'''
        result = subprocess.run(
            [
                "node",
                "-e",
                "globalThis.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};"
                "new Function(require('fs').readFileSync(0,'utf8'))();",
            ],
            input=script_prefix + runtime_assertions,
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

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查字幕编辑行为")
    def test_review_edit_actions_keep_timeline_restore_and_payload_contract(self):
        _html, script = self._page_script()
        script_prefix = script.split(
            "document.getElementById('scanButton').addEventListener",
            1,
        )[0]
        runtime_assertions = r"""
const node=()=>({textContent:'',className:'',hidden:false,disabled:false,value:'',style:{},
  classList:{toggle:()=>{}},setAttribute:()=>{},removeAttribute:()=>{},focus:()=>{},
  querySelector:()=>null,querySelectorAll:()=>[],addEventListener:()=>{}});
globalThis.window={confirm:()=>true,getSelection:()=>({type:'None',toString:()=>''})};
globalThis.requestAnimationFrame=(callback)=>callback();
globalThis.document={getElementById:()=>node(),querySelectorAll:()=>[],querySelector:()=>null};
state.sourceCues=[
  {index:1,start:'00:00:00,000',end:'00:00:01,000',start_seconds:0,end_seconds:1,text:'第一句'},
  {index:2,start:'00:00:01,000',end:'00:00:02,000',start_seconds:1,end_seconds:2,text:'第二句'},
  {index:3,start:'00:00:02,000',end:'00:00:03,000',start_seconds:2,end_seconds:3,text:'第三句'}
];
state.edited=new Map(state.sourceCues.map(cue=>[cue.index,cue.text]));
state.mergePrevious.clear();
state.mergeOverrides.clear();
state.timeOverrides.clear();
state.deleted.clear();
state.deletedSnapshots.clear();
state.deleteSelection.clear();
mergeGroups(1,2);
if(groupTiming(1).start!==0||groupTiming(1).end!==2)throw new Error('首次合并没有覆盖首条到末条时间');
mergeGroups(1,3);
if(groupTiming(1).start!==0||groupTiming(1).end!==3)throw new Error('链式合并没有覆盖首条到末条时间');
if(validateTiming(2,1,1.5)!=='合并字幕必须调整整组时间')throw new Error('合并子条仍可单独调整');
state.mergePrevious.clear();
state.mergeOverrides.clear();
state.timeOverrides.clear();
state.edited.set(1,'手工第一句');
state.edited.set(2,'手工第二句');
mergeGroups(1,2);
state.timeOverrides.set(1,{start:.25,end:2.5});
if(mergedDisplayText(1)!=='手工第一句手工第二句')throw new Error('合并没有保留成员正文');
if(!unmergeGroup(1)||state.mergePrevious.size!==0)throw new Error('拆分合并组失败');
if(state.edited.get(1)!=='手工第一句'||state.edited.get(2)!=='手工第二句')throw new Error('拆分丢失成员正文');
if(state.timeOverrides.get(1)?.start!==.25||state.timeOverrides.get(2)?.end!==2.5)throw new Error('拆分丢失两端时间调整');
mergeGroups(1,2);
if(groupTiming(1).start!==.25||groupTiming(1).end!==2.5)throw new Error('重新合并没有保留时间调整');
state.mergeOverrides.set(1,'手工改写的合并正文');
if(unmergeGroup(1)!==false||state.mergePrevious.get(2)!==1)throw new Error('手工合并正文被静默丢失');
state.mergeOverrides.clear();
if(!unmergeGroup(1))throw new Error('恢复原文后仍无法拆分合并组');
state.mergePrevious.clear();
state.mergeOverrides.clear();
state.timeOverrides.clear();
if(validateTiming(1,.25,1.5)!=='')throw new Error('后端允许的时间重叠被前端误拒绝');
if(!validateTiming(1,-.1,.5))throw new Error('负开始时间未被拒绝');
if(!validateTiming(1,1,.5))throw new Error('结束早于开始未被拒绝');
if(!validateTiming(1,1.1,1.8))throw new Error('开始时间跨越下一条时间轴未被拒绝');
let shiftInput={value:'1',focus:()=>{}};
document.querySelector=()=>shiftInput;
if(validateTiming(1,.25,1.5)!=='')throw new Error('时间轴安全校验错误');
shiftCueTiming(1,-1);
if(state.timeOverrides.size!==0)throw new Error('整段前移越过 0 未被拒绝');

let renderCount=0;
renderCues=()=>{renderCount+=1;};
const cancelNode={addEventListener:(type,callback)=>{if(type==='click')cancelNode.click=callback;}};
const confirmNode={addEventListener:(type,callback)=>{if(type==='click')confirmNode.click=callback;}};
const previewList={querySelector:(selector)=>selector.includes('cancel')?cancelNode:confirmNode};
state.deleteSelection.add(2);
wireDeletePreview(previewList);
cancelNode.click();
if(state.deleteSelection.size!==0||state.deleted.size!==0)throw new Error('删除预览取消没有恢复待删除状态');
state.deleteSelection.add(2);
confirmNode.click();
if(!state.deleted.has(2)||state.deleteSelection.size!==0)throw new Error('删除预览确认没有提交删除');
restoreCue(2);
if(state.deleted.has(2))throw new Error('删除后无法恢复字幕');

state.deleted.clear();
state.deletedSnapshots.clear();
state.filter='changed';
if(!applySavedEditState({deleted_indices:[2]}))throw new Error('已保存删除状态无法重新加载');
if(!state.deletedSnapshots.has(2)||!renderCueRow(state.sourceCues[1]).includes('恢复'))throw new Error('重新加载后没有恢复入口');
restoreCue(2);
state.filter='all';

state.pairs=[{id:'pair-1',srt_path:'source.srt'}];
state.selectedIndex=0;
state.edited=new Map(state.sourceCues.map(cue=>[cue.index,cue.text]));
state.edited.set(1,'修改第一句');
state.deleted.add(3);
state.mergePrevious.set(2,1);
state.mergeOverrides.set(1,'合并正文');
state.timeOverrides.set(1,{start:.25,end:2.25});
persistSubtitleSettings=()=>{};
renderQueue=()=>{};
updateArtifact=()=>{};
let payload=null;
globalThis.fetch=async (_url,options)=>({ok:true,json:async()=>{payload=JSON.parse(options.body);return{corrected_srt_path:'corrected.srt'};}});
saveCorrections().then(()=>{
for(const key of ['srt_path','corrections','deleted_indices','merge_pairs','merge_overrides','time_overrides'])if(!(key in payload))throw new Error(`payload 缺少 ${key}`);
if(JSON.stringify(payload.merge_pairs)!==JSON.stringify([{first:1,second:2}]))throw new Error('合并 payload 字段不兼容');
if(JSON.stringify(payload.deleted_indices)!==JSON.stringify([3]))throw new Error('删除 payload 字段不兼容');
if(payload.time_overrides['1'].start!==.25||payload.time_overrides['1'].end!==2.25)throw new Error('时间 payload 字段不兼容');
const payloadKeys=Object.keys(payload).sort();
const expectedKeys=['corrections','deleted_indices','merge_overrides','merge_pairs','srt_path','time_overrides'];
if(JSON.stringify(payloadKeys)!==JSON.stringify(expectedKeys))throw new Error(`质检状态污染保存 payload：${payloadKeys}`);

const handlers={};
const row={dataset:{index:'1'},querySelector:()=>null,querySelectorAll:()=>[],
  addEventListener:(type,callback)=>{handlers[type]=callback;}};
state.selectedCueIndex=null;
renderCount=0;
wireCueRow(row);
handlers.click({target:{closest:()=>null}});
if(state.selectedCueIndex!==1||renderCount!==1)throw new Error('字幕行单击没有选中');
for(const type of ['textarea','input','button']){
  state.selectedCueIndex=null;
  handlers.click({target:{closest:()=>({nodeName:type.toUpperCase()})}});
  if(state.selectedCueIndex!==null)throw new Error(`${type} 点击错误触发行切换`);
}
state.selectedCueIndex=null;
window.getSelection=()=>({type:'Range',toString:()=> '用户选择文本'});
handlers.click({target:{closest:()=>null}});
if(state.selectedCueIndex!==null||renderCount!==1)throw new Error('长按选择文本被 renderCues 打断');
}).catch(error=>{console.error(error);process.exitCode=1;});
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
                "quality_overview": {
                    "overview_version": 1,
                    "final_slice_count": 12,
                    "clips": [{"title": "高分切片", "score": 90}],
                    "edge_candidate_count": 1,
                    "edge_candidates": [{"title": "边缘候选", "score": 70}],
                },
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
        self.assertEqual(summary["quality_overview"]["final_slice_count"], 12)
        self.assertNotIn("slice_count", summary["quality_overview"])
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
                "quality_overview": {
                    "overview_version": 1,
                    "final_slice_count": 1,
                    "clips": [{"title": "测试", "score": 88}],
                    "edge_candidate_count": 1,
                    "edge_candidates": [{"title": "边缘候选", "score": 70}],
                },
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
            self.assertEqual(
                task_result["quality_overview"]["final_slice_count"],
                1,
            )
            self.assertNotIn("slice_count", task_result["quality_overview"])

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

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查只读时间轴行为")
    def test_topic_page_renders_independent_timeline_and_explicit_incomplete_state(self):
        response = self.client.get("/topic-v2")
        self.assertEqual(response.status_code, 200)
        scripts = re.findall(
            r"<script>(.*?)</script>", response.get_data(as_text=True), flags=re.S
        )
        self.assertTrue(scripts)
        script = scripts[-1]
        script_prefix = script.split("restoreWorkspacePaths();", 1)[0]
        runtime_assertions = r"""
const makeNode=()=>({
  textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,value:'all',title:'',
  style:{},options:[],selectedIndex:0,selectedOptions:[],dataset:{},parentElement:null,
  classList:{add:()=>{},remove:()=>{},toggle:()=>{}},
  addEventListener:()=>{},setAttribute(name,value){this[name]=String(value)},
  querySelectorAll:()=>[],replaceChildren:()=>{},
});
const nodes=new Map();
globalThis.document={
  getElementById:(id)=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id)},
  querySelectorAll:()=>[],createElement:()=>makeNode(),querySelector:()=>null,
};
document.getElementById('timelineMode').value='none';
globalThis.window={};
globalThis.localStorage={getItem:()=>null,setItem:()=>{}};
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const payload={
  schema_version:1,task_id:'pipeline_timeline',video_duration:600,
  clips:[{id:'clip-1',start:60,end:120,title:'完整片段',source:'切片标记',reason:'前后文完整'}],
  edge_candidates:[{id:'edge-1',start:300,end:360,title:'边缘候选',source:'候选复核',reason:'待人工确认 <script>alert(1)</script>'}],
  complete:false,truncated:true,incomplete_reasons:['clips_limit'],generated_at:'2026-08-25T00:00:00Z',
};
const requests=[];
globalThis.fetch=(url)=>{requests.push(url);return Promise.resolve({ok:true,status:200,json:async()=>payload})};
(async()=>{
  currentTaskId='pipeline_timeline';
  await loadReadonlyTimeline('pipeline_timeline');
  const section=document.getElementById('readonlyTimeline');
  const track=document.getElementById('timelineTrack');
  const cards=document.getElementById('timelineCards');
  const integrity=document.getElementById('timelineIntegrity');
  assert(requests.length===1&&requests[0]==='/api/tasks/pipeline_timeline/timeline','时间轴没有使用独立只读接口');
  assert(section.hidden===false,'成功读取后没有显示时间轴');
  assert(document.getElementById('timelineState').textContent==='不完整','complete=false 没有展示不完整状态');
  assert(integrity.textContent.includes('数据不完整')&&integrity.textContent.includes('数据已截断'),'完整性字段没有展示不完整和截断提示');
  assert(track.innerHTML.includes('clip-1')&&track.innerHTML.includes('left:10%'),'最终切片没有按整场时长绘制比例位置');
  assert(cards.innerHTML.includes('ID：clip-1')&&cards.innerHTML.includes('来源：候选复核'),'结果卡片没有稳定 ID 和来源');
  assert(!cards.innerHTML.includes('<script>alert(1)</script>'),'结果卡片没有安全转义长内容');
  selectTimelineRecord('clip:clip-1');
  assert(document.getElementById('timelineDetails').innerHTML.includes('ID：clip-1'),'时间轴选择没有显示稳定 ID 详情');
  assert(document.getElementById('timelineDetails').innerHTML.includes('前后文完整'),'时间轴选择没有显示可解释原因');
  document.getElementById('timelineFilter').value='candidate';
  renderReadonlyTimeline(readonlyTimeline);
  assert(cards.innerHTML.includes('edge-1')&&!cards.innerHTML.includes('完整片段'),'筛选没有只显示边缘候选');
  assert(document.getElementById('timelineDetails').hidden===true,'筛选后仍显示已隐藏记录的旧详情');
  renderReadonlyTimeline({video_duration:null,clips:[],edge_candidates:[],complete:false,truncated:false,incomplete_reasons:['video_duration_invalid']});
  assert(document.getElementById('timelineScale').innerHTML.includes('缺少整场时长'),'缺失整场时长没有明确提示');
  assert(track.innerHTML.includes('缺少整场时长'),'缺失整场时长没有停止绘制比例时间块');
  assert(document.getElementById('timelineCount').textContent.includes('时长未记录'),'缺失整场时长没有明确计数提示');
})().catch(error=>{process.stderr.write(error.stack||String(error));process.exitCode=1});
"""
        result = subprocess.run(
            ["node", "-"],
            input=script_prefix + runtime_assertions,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 检查时间轴与短片预览联动")
    def test_topic_page_links_timeline_selection_to_safe_clip_preview_without_stale_responses(self):
        response = self.client.get("/topic-v2")
        self.assertEqual(response.status_code, 200)
        scripts = re.findall(
            r"<script>(.*?)</script>", response.get_data(as_text=True), flags=re.S
        )
        self.assertTrue(scripts)
        script_prefix = scripts[-1].split("restoreWorkspacePaths();", 1)[0]
        runtime_assertions = r"""
const makeClassList=()=>({
  values:new Set(),
  add(...names){names.forEach(name=>this.values.add(name))},
  remove(...names){names.forEach(name=>this.values.delete(name))},
  toggle(name,force){const next=force===undefined?!this.values.has(name):Boolean(force);if(next)this.values.add(name);else this.values.delete(name);return next;},
});
const makeNode=()=>({
  textContent:'',innerHTML:'',className:'',hidden:false,disabled:false,value:'all',title:'',src:'',
  style:{},options:[],selectedIndex:0,selectedOptions:[],dataset:{},parentElement:{classList:makeClassList()},
  classList:makeClassList(),handlers:{},
  addEventListener(name,handler){this.handlers[name]=handler},
  setAttribute(name,value){this[name]=String(value)},removeAttribute(name){if(name==='src')this.src=''},
  replaceChildren:()=>{},querySelectorAll:()=>[],focus:()=>{},pause(){this.paused=true},load(){this.loaded=true},
});
const nodes=new Map();
globalThis.document={
  getElementById:(id)=>{if(!nodes.has(id))nodes.set(id,makeNode());return nodes.get(id)},
  createElement:()=>makeNode(),querySelector:()=>null,
};
const interactive=[];
const setInteractive=(keys)=>{
  interactive.length=0;
  keys.forEach(key=>interactive.push({dataset:{'timelineKey':key},classList:makeClassList(),handlers:{},
    addEventListener(name,handler){this.handlers[name]=handler},
    setAttribute(name,value){this[name]=String(value)}}));
};
document.querySelectorAll=(selector)=>selector==='[data-timeline-key]'?interactive:[];
document.getElementById('timelineMode').value='none';
document.getElementById('timelineFilter').value='all';
globalThis.window={location:{origin:'http://autoslice.test'}};
globalThis.localStorage={getItem:()=>null,setItem:()=>{}};
const assert=(condition,message)=>{if(!condition)throw new Error(message)};
const makeResponse=(status,payload)=>({ok:status>=200&&status<300,status,json:async()=>payload});
const payload={
  schema_version:1,task_id:'pipeline-link',video_duration:600,
  clips:[
    {id:'clip-a',clip_id:'clip-a',start:60,end:120,title:'片段 A',source:'切片标记',reason:'A 的前后文'},
    {id:'clip-b',clip_id:'clip-b',start:180,end:240,title:'片段 B',source:'切片标记',reason:'B 的前后文'},
  ],
  edge_candidates:[{id:'edge-1',start:300,end:360,title:'边缘候选',source:'候选复核',reason:'只可查看'}],
  complete:true,truncated:false,incomplete_reasons:[],generated_at:'2026-08-25T00:00:00Z',
};
const requests=[];const pendingTokens=[];
globalThis.fetch=(url,options={})=>{
  requests.push({url,options});
  if(url==='/api/tasks/pipeline-link/timeline'){
    setInteractive(['clip:clip-a','clip:clip-b','candidate:edge-1']);
    return Promise.resolve(makeResponse(200,payload));
  }
  if(url.endsWith('/media-token')){
    pendingTokens.push({url,options,resolve:null});
    return new Promise(resolve=>{pendingTokens[pendingTokens.length-1].resolve=resolve});
  }
  throw new Error(`意外请求 ${url}`);
};
const settle=async()=>{for(let index=0;index<8;index++)await Promise.resolve()};
(async()=>{
  currentTaskId='pipeline-link';
  await loadReadonlyTimeline('pipeline-link');
  assert(interactive.length===3,'固定 mock 没有同时建立色块和结果卡片交互入口');

  // 色块点击与结果卡片点击都必须选择同一套稳定记录，并更新详情。
  interactive[0].handlers.click();
  await settle();
  assert(selectedTimelineKey==='clip:clip-a','色块点击没有保留片段 A 选中状态');
  assert(document.getElementById('timelineDetails').innerHTML.includes('A 的前后文'),'色块点击没有显示对应详情');
  assert(pendingTokens.length===1&&pendingTokens[0].url==='/api/tasks/pipeline-link/clips/clip-a/media-token','色块点击没有发起任务级 token 请求');

  interactive[1].handlers.click();
  await settle();
  assert(selectedTimelineKey==='clip:clip-b','结果卡片点击没有切换到片段 B');
  assert(document.getElementById('timelineDetails').innerHTML.includes('B 的前后文'),'结果卡片点击没有显示对应详情');
  assert(pendingTokens.length===2,'快速点击没有建立第二个独立 token 请求');
  assert(pendingTokens.every(item=>item.options.method==='POST'),'token 请求方法不是 POST');
  assert(pendingTokens.every(item=>!item.url.includes('path=')),'token 请求包含任意 path 参数');

  // B 先返回，随后 A 的旧响应返回；旧响应不得覆盖播放器或新选择。
  pendingTokens[1].resolve(makeResponse(200,{media_url:'/api/tasks/pipeline-link/clips/clip-b/media?token=safe-b-token'}));
  await settle();
  const video=document.getElementById('timelinePreviewVideo');
  assert(video.hidden===false&&video.src==='/api/tasks/pipeline-link/clips/clip-b/media?token=safe-b-token','安全 media_url 没有用于打开短片播放器');
  video.onerror();
  assert(document.getElementById('timelinePreviewState').textContent.includes('播放器加载失败')&&selectedTimelineKey==='clip:clip-b','播放器失败没有显示可理解错误或清除选择');
  pendingTokens[0].resolve(makeResponse(200,{media_url:'/api/tasks/pipeline-link/clips/clip-a/media?token=old-a-token'}));
  await settle();
  assert(video.src.includes('/clip-b/media?token=safe-b-token'),'旧 token 响应覆盖了新选中的播放器');

  closeTimelinePreview();
  assert(video.hidden===true&&selectedTimelineKey==='clip:clip-b','关闭播放器错误清除了时间轴选中状态');
  assert(document.getElementById('timelineDetails').hidden===false&&document.getElementById('timelineDetails').innerHTML.includes('B 的前后文'),'关闭播放器错误清除了详情');

  // 边缘候选明确不可预览，且不产生媒体请求。
  const mediaRequestCount=()=>requests.filter(item=>item.url.endsWith('/media-token')).length;
  const beforeCandidate=mediaRequestCount();
  interactive[2].handlers.click();
  await settle();
  assert(document.getElementById('timelinePreviewState').textContent==='暂无可预览短片','边缘候选没有明确显示暂无可预览短片');
  assert(mediaRequestCount()===beforeCandidate,'边缘候选错误请求了媒体 token');

  // token 失败仍保留选择/详情，并且不把服务端敏感文本回显到页面。
  setInteractive(['clip:clip-fail']);
  renderReadonlyTimeline({task_id:'pipeline-link',video_duration:600,clips:[{id:'clip-fail',clip_id:'clip-fail',start:10,end:20,title:'失败片段',reason:'保留详情'}],edge_candidates:[],complete:false,truncated:false,incomplete_reasons:['clips_missing']});
  interactive[0].handlers.click();
  await settle();
  const failedToken=pendingTokens[pendingTokens.length-1];
  failedToken.resolve(makeResponse(500,{error:'token=secret C:\\private\\secret.mp4'}));
  await settle();
  const failureState=document.getElementById('timelinePreviewState').textContent;
  assert(selectedTimelineKey==='clip:clip-fail'&&document.getElementById('timelineDetails').innerHTML.includes('保留详情'),'token 失败清除了选择或详情');
  assert(failureState.includes('短片预览失败')&&!failureState.includes('secret')&&!failureState.includes('private'),'token 失败回显了未脱敏错误');

  // 显式空 clip_id 不能猜文件；旧契约/完整契约仍只允许稳定 ID。
  setInteractive(['clip:clip-no-media']);
  renderReadonlyTimeline({task_id:'pipeline-link',video_duration:600,clips:[{id:'clip-no-media',clip_id:'',start:30,end:40,title:'无媒体片段'}],edge_candidates:[],complete:true,truncated:false,incomplete_reasons:[]});
  interactive[0].handlers.click();
  await settle();
  assert(document.getElementById('timelinePreviewState').textContent==='暂无可预览短片','没有可播放 clip_id 时没有明确提示');
  assert(mediaRequestCount()===beforeCandidate+1,'没有可播放 clip_id 仍然请求了 token');

  setInteractive(['clip:clip-legacy-without-media-id']);
  renderReadonlyTimeline({task_id:'pipeline-link',video_duration:600,clips:[{id:'clip-legacy-without-media-id',start:40,end:50,title:'旧契约片段'}],edge_candidates:[],complete:true,truncated:false,incomplete_reasons:[]});
  interactive[0].handlers.click();
  await settle();
  assert(document.getElementById('timelinePreviewState').textContent==='暂无可预览短片','缺少 clip_id 的旧数据被错误当成媒体 ID');
  assert(mediaRequestCount()===beforeCandidate+1,'缺少 clip_id 的旧数据仍然请求了 token');

  // 任务变化会清理选择并使旧媒体响应失效；旧响应不能污染新任务页面。
  setInteractive(['clip:clip-old']);
  renderReadonlyTimeline({task_id:'pipeline-old',video_duration:600,clips:[{id:'clip-old',clip_id:'clip-old',start:50,end:60,title:'旧任务片段'}],edge_candidates:[],complete:true,truncated:false,incomplete_reasons:[]});
  currentTaskId='pipeline-old';
  interactive[0].handlers.click();
  await settle();
  const oldPending=pendingTokens[pendingTokens.length-1];
  renderTaskProgress({task_id:'pipeline-new',status:'running',progress:'新任务',step:1,total:10});
  oldPending.resolve(makeResponse(200,{media_url:'/api/tasks/pipeline-old/clips/clip-old/media?token=old-task-token'}));
  await settle();
  assert(currentTaskId==='pipeline-new'&&video.hidden===true&&!video.src,'当前任务切换后旧媒体响应污染了播放器');
  assert(selectedTimelineKey===''&&document.getElementById('timelineDetails').hidden===true,'当前任务切换后旧时间轴选择仍残留');

  // 固定 mock 只允许只读时间轴和任务级媒体接口，不触发重切片、FFmpeg 或整场编码。
  assert(requests.every(item=>item.url.includes('/timeline')||item.url.endsWith('/media-token')),'联动触发了非只读/非媒体接口');
  assert(!requests.some(item=>item.url.includes('path=')||item.url.toLowerCase().includes('ffmpeg')||item.url.includes('start-pipeline')),'请求包含 path=、FFmpeg 或重切片入口');

  // 空、不完整、旧数据必须保持明确而不猜测。
  renderReadonlyTimeline({task_id:'pipeline-new',video_duration:null,clips:null,edge_candidates:null,complete:false,truncated:false,incomplete_reasons:['clips_missing','edge_candidates_missing','video_duration_invalid']});
  assert(document.getElementById('timelineState').textContent==='不完整','旧/不完整数据没有保留不完整状态');
  assert(document.getElementById('timelineTrack').innerHTML.includes('缺少整场时长'),'空旧数据错误绘制了时间块');
})().catch(error=>{process.stderr.write(error.stack||String(error));process.exitCode=1});
"""
        result = subprocess.run(
            ["node", "-"],
            input=script_prefix + runtime_assertions,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_topic_page_timeline_layout_is_scrollable_and_mobile_safe(self):
        project_root = Path(__file__).resolve().parents[2]
        stylesheet = (
            project_root / "src" / "autoslice" / "resources" / "static" / "workbench.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".timeline-scale-wrap", stylesheet)
        self.assertIn("overflow-x: auto", stylesheet)
        self.assertIn("min-width: 620px", stylesheet)
        self.assertIn(".timeline-card", stylesheet)
        mobile_rules = stylesheet.split("@media (max-width: 760px)", 1)[1]
        self.assertIn(".timeline-controls", mobile_rules)
        self.assertIn("flex-wrap: wrap", mobile_rules)

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


class TimelineApiTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config.update(TESTING=True)
        app_module.tasks.clear()
        self.directory = TemporaryDirectory(prefix="autoslice-timeline-api-")
        self.root = Path(self.directory.name)
        self.client = _bootstrapped_client()

    def tearDown(self):
        app_module.tasks.clear()
        self.directory.cleanup()

    def _create_done_task(
            self,
            task_id="timeline-success",
            *,
            with_artifacts=True,
            artifact_root=None,
            result=None,
            video_duration=120,
            task_type="topic_pipeline",
    ):
        output_dir = self.root / f"{task_id}-output"
        artifact_dir = (
            Path(artifact_root)
            if artifact_root is not None
            else output_dir / f"{task_id}_自动切片"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        if with_artifacts:
            data_dir = artifact_dir / "数据"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "clip_marks.json").write_text(
                json.dumps({
                    "video_duration": video_duration,
                    "clip_marks": [{
                        "id": "clip-1",
                        "start": 10,
                        "end": 30,
                        "title": "真实片段",
                        "source": "切片标记",
                        "reason": "成功样本",
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (data_dir / "候选复核明细.json").write_text(
                json.dumps({
                    "candidates": [{
                        "id": "edge-1",
                        "start": 45,
                        "end": 60,
                        "title": "边缘候选",
                        "source": "候选复核",
                        "reason": "待人工确认",
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (data_dir / "精调任务.json").write_text(
                json.dumps({
                    "tasks": [{
                        "id": "01",
                        "start": 10,
                        "end": 30,
                        "status": "已完成",
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        metadata = {"output_dir": str(output_dir)}
        if artifact_root is not None or with_artifacts:
            metadata["artifact_dir"] = str(artifact_dir)
        task_result = {
            "artifact_dir": str(artifact_dir),
            "quality_overview": {
                "clips": [{"start": 900, "end": 901}],
                "edge_candidates": [],
            },
        }
        if result is not None:
            task_result.update(result)
        app_module.task_registry.store.create_task(
            task_id,
            task_type,
            source_path=self.root / f"{task_id}.flv",
            output_paths=(output_dir,),
            status="done",
            result_summary=task_result,
            metadata=metadata,
        )
        return output_dir, artifact_dir

    def test_timeline_returns_registered_records_and_integrity_fields(self):
        self._create_done_task()

        response = self.client.get("/api/tasks/timeline-success/timeline")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["task_id"], "timeline-success")
        self.assertEqual(payload["video_duration"], 120.0)
        self.assertEqual(payload["clips"][0]["id"], "clip-1")
        self.assertEqual(payload["clips"][0]["clip_id"], "01")
        self.assertEqual(payload["edge_candidates"][0]["id"], "edge-1")
        self.assertTrue(payload["complete"])
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["incomplete_reasons"], [])
        self.assertNotIn("quality_overview", payload)

    def test_timeline_clip_id_matching_prefers_valid_precise_times_and_falls_back(self):
        cases = (
            (
                "precise",
                {"start": "legacy-invalid", "end": "legacy-invalid",
                 "clip_start_seconds": 1.2345, "clip_end_seconds": 2.3456},
                {"start": 1.2345, "end": 2.3456},
                "precise-id",
            ),
            (
                "null-precise",
                {"start": 10, "end": 20, "clip_start_seconds": None,
                 "clip_end_seconds": None},
                {"start": 10, "end": 20},
                "legacy-null-id",
            ),
            (
                "single-precise",
                {"start": 30, "end": 40, "clip_start_seconds": 30.5},
                {"start": 30, "end": 40},
                "legacy-single-id",
            ),
            (
                "invalid-precise",
                {"start": 50, "end": 60, "clip_start_seconds": "bad",
                 "clip_end_seconds": float("inf")},
                {"start": 50, "end": 60},
                "legacy-invalid-id",
            ),
            (
                "bool-precise",
                {"start": 90, "end": 100, "clip_start_seconds": True,
                 "clip_end_seconds": 100},
                {"start": 90, "end": 100},
                "legacy-bool-id",
            ),
            (
                "nan-precise",
                {"start": 110, "end": 120, "clip_start_seconds": float("nan"),
                 "clip_end_seconds": 120},
                {"start": 110, "end": 120},
                "legacy-nan-id",
            ),
            (
                "overflow-precise",
                {"start": 130, "end": 140, "clip_start_seconds": 10 ** 400,
                 "clip_end_seconds": 10 ** 400 + 1},
                {"start": 130, "end": 140},
                "legacy-overflow-id",
            ),
            (
                "invalid-interval",
                {"start": 70, "end": 80, "clip_start_seconds": 80,
                 "clip_end_seconds": 70},
                {"start": 70, "end": 80},
                "legacy-invalid-interval-id",
            ),
            (
                "zero",
                {"start": 0, "end": 1, "clip_start_seconds": 0.0,
                 "clip_end_seconds": 1.0},
                {"start": 0.0, "end": 1.0},
                "zero-id",
            ),
        )
        for case_name, entry, record, expected_id in cases:
            with self.subTest(case=case_name):
                attached = app_module._attach_registered_timeline_clip_ids(
                    [record],
                    {"tasks": [{**entry, "id": expected_id}]},
                )
                self.assertEqual(attached[0]["clip_id"], expected_id)

    def test_timeline_clip_id_matching_does_not_guess_conflicting_valid_times(self):
        attached = app_module._attach_registered_timeline_clip_ids(
            [{"start": 100.0, "end": 110.0}],
            {
                "tasks": [{
                    "id": "conflict-id",
                    "start": 10,
                    "end": 20,
                    "clip_start_seconds": 100.0,
                    "clip_end_seconds": 110.0,
                }],
            },
        )

        self.assertNotIn("clip_id", attached[0])

    def test_timeline_returns_404_for_unknown_task(self):
        response = self.client.get("/api/tasks/not-registered/timeline")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json(), {"error": "任务不存在"})

    def test_timeline_rejects_unowned_artifact_and_arbitrary_path_query(self):
        _, foreign_artifact = self._create_done_task("foreign-artifact")
        output_dir, _ = self._create_done_task(
            "timeline-owned",
            with_artifacts=False,
            artifact_root=foreign_artifact,
        )
        # 让输出根目录仍然属于当前任务，但登记的整理包位于另一个任务目录。
        self.assertTrue(output_dir.is_dir())
        forbidden = self.client.get(
            "/api/tasks/timeline-owned/timeline",
            query_string={"path": str(foreign_artifact / "数据" / "clip_marks.json")},
        )

        self.assertEqual(forbidden.status_code, 400)
        self.assertEqual(forbidden.get_json(), {"error": "不支持 path 参数"})

        forbidden_without_query = self.client.get(
            "/api/tasks/timeline-owned/timeline"
        )
        self.assertEqual(forbidden_without_query.status_code, 403)

    def test_timeline_rejects_symlinked_artifact_file(self):
        output_dir, artifact_dir = self._create_done_task(
            "timeline-symlink",
            with_artifacts=False,
        )
        data_dir = artifact_dir / "数据"
        data_dir.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside.json"
        outside.write_text(
            json.dumps({"clip_marks": [{"start": 1, "end": 2}]}),
            encoding="utf-8",
        )
        try:
            os.symlink(outside, data_dir / "clip_marks.json")
            (data_dir / "候选复核明细.json").write_text(
                json.dumps({"candidates": []}),
                encoding="utf-8",
            )
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"当前环境不支持创建符号链接：{exc}")

        response = self.client.get(
            "/api/tasks/timeline-symlink/timeline"
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(output_dir.is_dir())

    def test_timeline_rejects_unfinished_and_disallowed_tasks(self):
        output_dir = self.root / "unfinished-output"
        app_module.task_registry.store.create_task(
            "timeline-running",
            "topic_pipeline",
            source_path=self.root / "running.flv",
            output_paths=(output_dir,),
            status="queued",
        )
        running_response = self.client.get(
            "/api/tasks/timeline-running/timeline"
        )
        self.assertEqual(running_response.status_code, 409)

        output_dir, artifact_dir = self._create_done_task(
            "timeline-disallowed",
            with_artifacts=False,
            task_type="subtitle_review",
        )
        disallowed_response = self.client.get(
            "/api/tasks/timeline-disallowed/timeline"
        )
        self.assertEqual(disallowed_response.status_code, 403)
        self.assertTrue(output_dir.is_dir())
        self.assertTrue(artifact_dir.parent.is_dir())

    def test_old_task_and_missing_timeline_data_are_explicitly_incomplete(self):
        self._create_done_task(
            "timeline-old",
            with_artifacts=False,
            result={},
            video_duration=None,
        )
        old_response = self.client.get("/api/tasks/timeline-old/timeline")
        old_payload = old_response.get_json()
        self.assertEqual(old_response.status_code, 200)
        self.assertFalse(old_payload["complete"])
        self.assertIn("clips_missing", old_payload["incomplete_reasons"])
        self.assertIn("edge_candidates_missing", old_payload["incomplete_reasons"])

        legacy_artifact = self.root / "legacy-output" / "legacy_自动切片"
        app_module.task_registry.store.create_task(
            "timeline-legacy-without-output-root",
            "topic_pipeline",
            source_path=self.root / "legacy.flv",
            status="done",
            result_summary={"artifact_dir": str(legacy_artifact)},
        )
        legacy_response = self.client.get(
            "/api/tasks/timeline-legacy-without-output-root/timeline"
        )
        legacy_payload = legacy_response.get_json()
        self.assertEqual(legacy_response.status_code, 200)
        self.assertFalse(legacy_payload["complete"])
        self.assertIn("clips_missing", legacy_payload["incomplete_reasons"])
        self.assertIn(
            "edge_candidates_missing",
            legacy_payload["incomplete_reasons"],
        )

        _, artifact_dir = self._create_done_task(
            "timeline-missing-data",
            with_artifacts=False,
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        missing_response = self.client.get(
            "/api/tasks/timeline-missing-data/timeline"
        )
        missing_payload = missing_response.get_json()
        self.assertEqual(missing_response.status_code, 200)
        self.assertFalse(missing_payload["complete"])
        self.assertIn("clips_missing", missing_payload["incomplete_reasons"])
        self.assertIn("edge_candidates_missing", missing_payload["incomplete_reasons"])

    def test_timeline_serializer_failure_is_safe_server_error(self):
        self._create_done_task("timeline-serializer-error")

        with patch.object(
                app_module.timeline_contract,
                "serialize_timeline",
                side_effect=RuntimeError("不应泄露的本机路径"),
        ):
            response = self.client.get(
                "/api/tasks/timeline-serializer-error/timeline"
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "无法生成时间轴"})
        self.assertNotIn("不应泄露", response.get_data(as_text=True))


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
                    transcription_service=None, background_filter_mode=None):
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
                        "requested_mode": background_filter_mode or "soft",
                        "actual_mode": background_filter_mode or "soft",
                        "enabled": True,
                        "speaker_model_ready": True,
                        "speaker_model_used": True,
                        "used": True,
                        "detected_speaker_count": 2,
                        "detected_speaker_count_scope": "max_per_chunk",
                        "removed_segment_count": 0,
                        "removed_seconds": 0.0,
                        "candidate_segment_count": 2,
                        "candidate_seconds": 1.5,
                        "model": "CAM++",
                        "device": "cuda:0",
                        "fallback_reason": "",
                        "mode": "speaker_diarization",
                        "speaker_filtered_segment_count": 0,
                        "speaker_filtered_chunk_count": 0,
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
                        "background_filter_mode": "soft",
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
        self.assertEqual(task["background_filter_mode"], "soft")
        self.assertEqual(task["background_filter"], result["background_filter"])
        self.assertEqual(
            result["background_filter"]["actual_mode"],
            "soft",
        )
        self.assertTrue(generated_srt_exists)
        transcribe.assert_called_once()
        self.assertEqual(
            transcribe.call_args.kwargs["background_filter_mode"],
            "soft",
        )
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

    def test_transcribe_accepts_new_and_legacy_background_filter_payloads(self):
        cases = (
            ({}, "soft", True),
            ({"foreground_only": False}, "off", False),
            ({"foreground_only": True}, "soft", True),
            ({"background_filter_mode": "strict"}, "strict", True),
            (
                {
                    "background_filter_mode": "soft",
                    "foreground_only": True,
                },
                "soft",
                True,
            ),
        )
        with TemporaryDirectory() as td, patch.object(
            app_module.threading,
            "Thread",
            DeferredThread,
        ):
            for index, (fields, expected_mode, expected_legacy) in enumerate(cases):
                with self.subTest(fields=fields):
                    video = Path(td) / f"case-{index}.mp4"
                    video.write_bytes(b"video")
                    response = self.client.post(
                        "/api/subtitles/transcribe",
                        json={"video_path": str(video), **fields},
                    )
                    task = app_module.tasks[response.get_json()["task_id"]]

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        task["background_filter_mode"],
                        expected_mode,
                    )
                    self.assertIs(task["foreground_only"], expected_legacy)

    def test_transcribe_rejects_invalid_or_conflicting_new_mode(self):
        with TemporaryDirectory() as td:
            video = Path(td) / "最终成片.mp4"
            video.write_bytes(b"video")
            invalid = self.client.post(
                "/api/subtitles/transcribe",
                json={
                    "video_path": str(video),
                    "background_filter_mode": "aggressive",
                },
            )
            conflicting = self.client.post(
                "/api/subtitles/transcribe",
                json={
                    "video_path": str(video),
                    "background_filter_mode": "strict",
                    "foreground_only": True,
                },
            )

        self.assertEqual(invalid.status_code, 400)
        self.assertIn("background_filter_mode", invalid.get_json()["error"])
        self.assertEqual(conflicting.status_code, 400)
        self.assertIn("含义不一致", conflicting.get_json()["error"])

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
        self.assertGreaterEqual(profile["default_glossary_count"], 40)
        self.assertEqual(profile["extra_glossary_count"], 0)
        self.assertEqual(profile["replacement_count"], 11)
        self.assertEqual(app_module.tasks[task_id]["task_type"], "subtitle_review")
        assert_same_path(
            self,
            app_module.tasks[task_id]["source_srt_path"],
            str(srt.resolve()),
        )
        self.assertFalse(app_module.tasks[task_id]["force"])

    def test_review_passes_extra_glossary_and_profile_replacement_requires_confirmation(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            override_path = Path(td) / "profile-overrides.json"
            with (
                patch.dict(
                    os.environ,
                    {"AUTOSLICE_STREAMER_PROFILE_OVERRIDES": str(override_path)},
                    clear=False,
                ),
                patch.object(app_module.threading, "Thread", ImmediateThread),
                patch(
                    "autoslice.subtitle_workflow.suggest_subtitle_corrections",
                    return_value={"suggestions": []},
                ) as review,
            ):
                response = self.client.post(
                    "/api/subtitles/review",
                    json={
                        "video_path": str(video),
                        "srt_path": str(srt),
                        "glossary": ["本视频专名"],
                    },
                )
                missing_confirmation = self.client.post(
                    "/api/subtitles/profile-replacements",
                    json={
                        "streamer_profile_id": "zeyin",
                        "source": "测试错词",
                        "target": "测试正词",
                    },
                )
                profile = response.get_json()["review_profile"]
                saved = self.client.post(
                    "/api/subtitles/profile-replacements",
                    json={
                        "streamer_profile_id": "zeyin",
                        "source": "测试错词",
                        "target": "测试正词",
                        "profile_fingerprint": profile["profile_fingerprint"],
                        "confirm_scope": True,
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(review.call_args.kwargs["glossary"], ["本视频专名"])
        self.assertEqual(profile["extra_glossary_count"], 1)
        self.assertEqual(missing_confirmation.status_code, 400)
        self.assertIn("确认影响范围", missing_confirmation.get_json()["error"])
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.get_json()["added"])
        self.assertEqual(saved.get_json()["replacement_count"], 12)

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
        from autoslice.topic_engine import subtitle_title_services
        from autoslice.transcription.contracts import SubtitleTitleServices
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

    def test_preview_uses_temporary_edited_srt_without_persisting_state(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            original = srt.read_bytes()
            preview_paths = []

            def inspect_preview(_video_path, preview_srt, **_kwargs):
                preview_path = Path(preview_srt)
                preview_paths.append(preview_path)
                self.assertNotEqual(preview_path.resolve(), srt.resolve())
                self.assertEqual(
                    parse_srt_document(preview_path)[0].text,
                    "娃衣",
                )
                return b"\xff\xd8preview", 0.5

            with patch(
                "autoslice.subtitle_workflow.render_subtitle_preview",
                side_effect=inspect_preview,
            ) as preview:
                response = self.client.post(
                    "/api/subtitles/preview",
                    json={
                        "video_path": str(video),
                        "srt_path": str(srt),
                        "corrections": [{
                            "index": 1,
                            "original": "瓦衣",
                            "corrected": "娃衣",
                        }],
                    },
                )
            corrected = srt.with_name(f"{srt.stem}_校对.srt")
            edit_state = srt.with_name(f"{srt.stem}_校对状态.json")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(preview_paths), 1)
            self.assertFalse(preview_paths[0].exists())
            self.assertFalse(corrected.exists())
            self.assertFalse(edit_state.exists())
            self.assertEqual(srt.read_bytes(), original)
            preview.assert_called_once()

    def test_preview_failure_preserves_last_explicitly_saved_subtitle(self):
        with TemporaryDirectory() as td:
            video, srt = self._write_pair(td)
            saved = self.client.post(
                "/api/subtitles/save",
                json={
                    "srt_path": str(srt),
                    "corrections": [{
                        "index": 1,
                        "original": "瓦衣",
                        "corrected": "已保存字幕",
                    }],
                },
            )
            corrected = Path(saved.get_json()["corrected_srt_path"])
            edit_state = srt.with_name(f"{srt.stem}_校对状态.json")
            corrected_before = corrected.read_bytes()
            state_before = edit_state.read_bytes()
            preview_paths = []

            def fail_preview(_video_path, preview_srt, **_kwargs):
                preview_path = Path(preview_srt)
                preview_paths.append(preview_path)
                self.assertTrue(preview_path.is_file())
                raise RuntimeError("模拟预览失败")

            with patch(
                "autoslice.subtitle_workflow.render_subtitle_preview",
                side_effect=fail_preview,
            ):
                response = self.client.post(
                    "/api/subtitles/preview",
                    json={
                        "video_path": str(video),
                        "srt_path": str(srt),
                        "corrections": [{
                            "index": 1,
                            "original": "瓦衣",
                            "corrected": "未保存预览",
                        }],
                    },
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(len(preview_paths), 1)
            self.assertFalse(preview_paths[0].exists())
            self.assertFalse(preview_paths[0].parent.exists())
            self.assertEqual(corrected.read_bytes(), corrected_before)
            self.assertEqual(edit_state.read_bytes(), state_before)

    def test_preview_mode_requires_non_formal_temporary_output(self):
        with TemporaryDirectory() as td:
            _video, srt = self._write_pair(td)
            corrected = srt.with_name(f"{srt.stem}_校对.srt")
            state_path = srt.with_name(f"{srt.stem}_校对状态.json")
            corrected.write_text(srt.read_text(encoding="utf-8"), encoding="utf-8")
            state_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                save_corrected_srt(
                    srt,
                    [{"index": 1, "original": "瓦衣", "corrected": "娃衣"}],
                    persist_state=False,
                )
            with self.assertRaises(ValueError):
                save_corrected_srt(
                    srt,
                    [{"index": 1, "original": "瓦衣", "corrected": "娃衣"}],
                    output_path=corrected,
                    persist_state=False,
                )
            self.assertEqual(
                corrected.read_text(encoding="utf-8"),
                srt.read_text(encoding="utf-8"),
            )
            self.assertEqual(state_path.read_text(encoding="utf-8"), "{}")

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
