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
PROJECT_TL_DIR = os.path.join(PROJECT_DIR, "timelines")
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


def run_slice_task(task_id, flv_path, ass_path, output_dir, mode, timeline_path):
    """后台切片任务"""
    # 时间轴自动复制到项目文件夹
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
            mode=mode, timeline_path=timeline_path,
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
    return render_template("index.html")


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
    output_dir = data.get("output_dir", r"F:\Videos\自动切片")
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

    task_id = os.path.basename(flv_path).replace(".flv", "")[:50]
    threading.Thread(target=run_slice_task,
                     args=(task_id, flv_path, ass_path, output_dir, mode, timeline_path),
                     daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/slice-all", methods=["POST"])
def slice_all():
    data = request.get_json()
    video_dir = data.get("video_dir", "")
    output_dir = data.get("output_dir", r"F:\Videos\自动切片")
    mode = data.get("mode", "danmaku")
    timeline_path = data.get("timeline_path", "")

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
                         args=(task_id, f, ass_path, output_dir, mode, timeline_path),
                         daemon=True).start()
        task_ids.append(task_id)

    return jsonify({"task_ids": task_ids, "count": len(task_ids)})


@app.route("/api/tasks")
def list_tasks():
    with task_lock:
        return jsonify(dict(tasks))


@app.route("/api/timelines", methods=["GET"])
def list_timelines():
    timeline_dir = r"F:\切片时间轴"
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
    upload_dir = r"F:\切片时间轴"
    os.makedirs(upload_dir, exist_ok=True)
    save_path = os.path.join(upload_dir, file.filename)
    file.save(save_path)
    return jsonify({"path": save_path, "name": file.filename})


if __name__ == "__main__":
    # 启动逻辑在 启动.py 里，这样 Ctrl+C 一键全停
    pass
