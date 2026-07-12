"""
AutoSlice Web 界面 — SSE 实时推送 + 控制台同步
"""

import os, sys, json, time, threading, queue, glob as glob_mod
from flask import Flask, render_template, request, jsonify, Response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import process_video

app = Flask(__name__)

tasks = {}
task_lock = threading.Lock()
event_queues = []

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_VIDEO_DIR = os.path.expanduser(os.environ.get("AUTOSLICE_VIDEO_DIR", "recordings"))
DEFAULT_OUTPUT_DIR = os.path.expanduser(os.environ.get("AUTOSLICE_OUTPUT_DIR", "output"))
DEFAULT_TIMELINE_DIR = os.path.expanduser(os.environ.get("AUTOSLICE_TIMELINE_DIR", "timelines"))
PROJECT_TL_DIR = DEFAULT_TIMELINE_DIR
os.makedirs(PROJECT_TL_DIR, exist_ok=True)


def broadcast(event_type, data):
    """向所有 SSE 订阅者推送事件"""
    msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    dead = []
    for q in event_queues:
        try:
            q.put_nowait(msg)
        except queue.Full:
            dead.append(q)
    for q in dead:
        if q in event_queues:
            event_queues.remove(q)


def update_task(task_id, **kwargs):
    """更新任务状态并广播 + 控制台输出"""
    with task_lock:
        if task_id not in tasks:
            tasks[task_id] = {}
        tasks[task_id].update(kwargs)

    # 控制台同步输出（不用 \r，直接打印）
    status = kwargs.get("status", "")
    progress = kwargs.get("progress", "")
    pct = kwargs.get("step", 0)
    if progress:
        print(f"  [{task_id[:40]}] [{pct}%] {progress}")
    if status in ("done", "error"):
        result = kwargs.get("result", "")
        print(f"  [{task_id[:40]}] >>> {status}: {result}")

    # SSE 广播
    broadcast("task_update", {"task_id": task_id, **kwargs})


def _pipeline_completion_progress(result):
    """生成流水线完成提示，区分报告话题数和实际切片数。"""
    clip_marks = result.get("clip_marks") or []
    topic_count = result.get("topic_count", len(clip_marks))
    return f"完成! {topic_count} 个话题, {result.get('slice_count', 0)} 个切片"


def run_slice_task(task_id, flv_path, ass_path, output_dir, mode, timeline_path, timeline_json=None):
    """后台切片任务"""
    if timeline_path and os.path.isfile(timeline_path):
        import shutil
        dest = os.path.join(PROJECT_TL_DIR, os.path.basename(timeline_path))
        try:
            if not os.path.exists(dest) or os.path.getmtime(timeline_path) > os.path.getmtime(dest):
                shutil.copy2(timeline_path, dest)
            timeline_path = dest
        except:
            pass

    update_task(task_id, status="running", progress="准备中...", step=0)

    def callback(msg, step, total):
        update_task(task_id, status="running", progress=msg, step=step, total=total)

    try:
        count, out_dir = process_video(
            flv_path, ass_path, output_dir,
            mode=mode, timeline_path=timeline_path, timeline_json=timeline_json,
            progress_callback=callback
        )
        update_task(task_id, status="done",
                    progress=f"完成！{count} 个片段",
                    result=f"共切出 {count} 个片段 → {out_dir}", step=100)
    except Exception as e:
        update_task(task_id, status="error",
                    progress="失败",
                    result=str(e), step=0)


# ==================== SSE 端点 ====================

@app.route("/api/events")
def sse_events():
    """SSE 实时事件流"""
    q = queue.Queue(maxsize=50)
    event_queues.append(q)

    def generate():
        # 先发送当前所有任务状态
        with task_lock:
            current = dict(tasks)
        yield f"event: init\ndata: {json.dumps(current, ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            if q in event_queues:
                event_queues.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ==================== API 端点 ====================

@app.route("/")
def index():
    return render_template(
        "index.html",
        default_video_dir=DEFAULT_VIDEO_DIR,
        default_output_dir=DEFAULT_OUTPUT_DIR,
    )


@app.route("/api/scan", methods=["POST"])
def scan():
    data = request.get_json()
    video_dir = data.get("video_dir", "")
    if not os.path.isdir(video_dir):
        return jsonify({"error": "目录不存在"})

    videos = []
    for f in sorted(glob_mod.glob(os.path.join(video_dir, "*.flv"))):
        name = os.path.basename(f)
        if name.startswith("[正在录制]") or name.startswith("[录制中]"):
            continue
        base = f[:-4]
        has_ass = os.path.exists(base + ".ass")
        has_srt = os.path.exists(base + ".srt") and os.path.getsize(base + ".srt") > 0
        videos.append({"name": name, "path": f, "has_ass": has_ass, "has_srt": has_srt})

    return jsonify({"videos": videos, "count": len(videos)})


@app.route("/api/slice", methods=["POST"])
def slice_start():
    data = request.get_json()
    flv_path = data.get("flv_path", "")
    output_dir = data.get("output_dir", DEFAULT_OUTPUT_DIR)
    mode = data.get("mode", "danmaku")
    timeline_path = data.get("timeline_path", "")

    if not os.path.isfile(flv_path):
        return jsonify({"error": "视频文件不存在"})

    ass_path = flv_path[:-4] + ".ass"
    if mode == "danmaku" and not os.path.isfile(ass_path):
        return jsonify({"error": "缺少对应的 .ass 弹幕文件"})

    # 时间轴/混合模式：自动复制到项目文件夹
    if timeline_path and os.path.isfile(timeline_path):
        import shutil
        dest = os.path.join(PROJECT_TL_DIR, os.path.basename(timeline_path))
        if not os.path.exists(dest) or os.path.getmtime(timeline_path) > os.path.getmtime(dest):
            shutil.copy2(timeline_path, dest)
        timeline_path = dest

    timeline_json = data.get("timeline_json", "")
    if mode == "timeline-json":
        mode = "timeline"
        timeline_path = ""
    task_id = os.path.basename(flv_path).replace(".flv", "")[:50]
    threading.Thread(target=run_slice_task,
                     args=(task_id, flv_path, ass_path, output_dir, mode, timeline_path, timeline_json),
                     daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/slice-all", methods=["POST"])
def slice_all():
    data = request.get_json()
    video_dir = data.get("video_dir", "")
    output_dir = data.get("output_dir", DEFAULT_OUTPUT_DIR)
    mode = data.get("mode", "danmaku")
    timeline_path = data.get("timeline_path", "")
    timeline_json = data.get("timeline_json", "")
    if mode == "timeline-json":
        mode = "timeline"
        timeline_path = ""

    if not os.path.isdir(video_dir):
        return jsonify({"error": "目录不存在"})

    task_ids = []
    for f in sorted(glob_mod.glob(os.path.join(video_dir, "*.flv"))):
        name = os.path.basename(f)
        if name.startswith("[正在录制]") or name.startswith("[录制中]"):
            continue
        ass_path = f[:-4] + ".ass"
        if mode != "timeline" and not os.path.isfile(ass_path):
            continue
        task_id = name.replace(".flv", "")[:50]
        threading.Thread(target=run_slice_task,
                         args=(task_id, f, ass_path, output_dir, mode, timeline_path, timeline_json),
                         daemon=True).start()
        task_ids.append(task_id)

    return jsonify({"task_ids": task_ids, "count": len(task_ids)})


@app.route("/api/tasks")
def list_tasks():
    with task_lock:
        return jsonify(dict(tasks))


@app.route("/api/list-json-timelines", methods=["GET"])
def list_json_timelines():
    """列出可用的 JSON 时间轴文件"""
    search_dirs = [DEFAULT_VIDEO_DIR, DEFAULT_OUTPUT_DIR]
    files = []
    for d in search_dirs:
        if os.path.isdir(d):
            for root, _, fs in os.walk(d):
                for f in fs:
                    if f.endswith("_clip_marks.json") or f.endswith("_topics.json"):
                        files.append({"name": f, "path": os.path.join(root, f)})
    return jsonify({"files": sorted(files, key=lambda x: x["name"], reverse=True)})


@app.route("/api/upload-json-timeline", methods=["POST"])
def upload_json_timeline():
    """上传 JSON 时间轴文件"""
    if "file" not in request.files:
        return jsonify({"error": "无文件"})
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"})
    upload_dir = DEFAULT_OUTPUT_DIR
    os.makedirs(upload_dir, exist_ok=True)
    save_path = os.path.join(upload_dir, file.filename)
    file.save(save_path)
    return jsonify({"path": save_path, "name": file.filename})


@app.route("/api/timelines", methods=["GET"])
def list_timelines():
    timeline_dir = DEFAULT_TIMELINE_DIR
    if not os.path.isdir(timeline_dir):
        return jsonify({"files": []})
    files = sorted(glob_mod.glob(os.path.join(timeline_dir, "*.docx")), reverse=True)
    return jsonify({"files": [{"name": os.path.basename(f), "path": f} for f in files]})


@app.route("/api/upload-timeline", methods=["POST"])
def upload_timeline():
    if "file" not in request.files:
        return jsonify({"error": "无文件"})
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "文件名为空"})
    upload_dir = DEFAULT_TIMELINE_DIR
    os.makedirs(upload_dir, exist_ok=True)
    save_path = os.path.join(upload_dir, file.filename)
    file.save(save_path)
    return jsonify({"path": save_path, "name": file.filename})


# ==================== 话题分析 ====================

@app.route("/topic-v2")
def topic_v2_page():
    return render_template(
        "topic_v2.html",
        default_video_dir=DEFAULT_VIDEO_DIR,
        default_output_dir=DEFAULT_OUTPUT_DIR,
        default_timeline_dir=DEFAULT_TIMELINE_DIR,
    )


@app.route("/api/start-pipeline", methods=["POST"])
def start_pipeline():
    """启动完整话题分析流水线（v2）"""
    data = request.get_json()
    flv_path = data.get("flv_path", "")
    ass_path = data.get("ass_path", "")
    output_dir = data.get("output_dir", DEFAULT_OUTPUT_DIR)
    manual_timeline_mode = data.get("manual_timeline_mode", "auto")
    manual_timeline_path = data.get("manual_timeline_path", "")

    if not os.path.isfile(flv_path):
        return jsonify({"error": "视频文件不存在"})
    if manual_timeline_mode == "manual" and not os.path.isfile(manual_timeline_path):
        return jsonify({"error": "指定的辅助时间轴文件不存在"})
    if manual_timeline_mode == "none":
        manual_timeline_path = "__none__"
    elif manual_timeline_mode != "manual":
        manual_timeline_path = None

    task_id = "pipeline_" + os.path.basename(flv_path).replace(".flv", "")[:35]

    def run():
        try:
            from topic_engine import run_pipeline, slice_from_marks

            def cb(msg, step, total):
                update_task(task_id, status="running", progress=msg, step=step, total=total)

            result = run_pipeline(
                flv_path,
                ass_path if os.path.exists(ass_path) else None,
                progress_callback=cb,
                manual_timeline_path=manual_timeline_path,
            )

            # 用新的独立切片功能，不依赖现有切片模式
            clip_marks = result.get("clip_marks", [])
            if clip_marks:
                count, out_dir = slice_from_marks(
                    flv_path, result["json_path"], output_dir,
                    progress_callback=cb
                )
                result["slice_count"] = count
                result["slice_dir"] = out_dir

            update_task(task_id, status="done",
                        progress=_pipeline_completion_progress(result),
                        result=json.dumps(result, ensure_ascii=False),
                        step=100)
        except Exception as e:
            import traceback
            update_task(task_id, status="error", progress="失败",
                        result=f"{e}\n{traceback.format_exc()}", step=0)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"task_id": task_id})




if __name__ == "__main__":
    pass
