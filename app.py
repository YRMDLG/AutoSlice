"""
AutoSlice Web 界面 — SSE 实时推送 + 控制台同步
"""

import os, sys, json, time, threading, queue, glob as glob_mod, hashlib, secrets, subprocess, re, traceback
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, render_template, request, jsonify, Response, redirect

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import process_video
from runtime_config import (
    OUTPUT_DIR,
    SUBMISSION_DIR,
    TIMELINE_DIR,
    VIDEO_DIR,
)
from streamer_profiles import (
    public_streamer_profiles,
    resolve_streamer_profile,
    streamer_profile_context,
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

tasks = {}
task_lock = threading.Lock()
event_queues = []
event_queue_lock = threading.Lock()

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_TL_DIR = os.path.join(PROJECT_DIR, "timelines")
os.makedirs(PROJECT_TL_DIR, exist_ok=True)
for _runtime_dir in (VIDEO_DIR, OUTPUT_DIR, TIMELINE_DIR, SUBMISSION_DIR):
    _runtime_dir.mkdir(parents=True, exist_ok=True)

DEFAULT_VIDEO_DIR = str(VIDEO_DIR)
DEFAULT_OUTPUT_DIR = str(OUTPUT_DIR)
DEFAULT_TIMELINE_DIR = str(TIMELINE_DIR)
DEFAULT_SUBMISSION_DIR = str(SUBMISSION_DIR)
DEFAULT_AUTOCOVER_URL = "http://127.0.0.1:5010"
AUTOSLICE_SERVICE_ID = "autoslice"
AUTOSLICE_API_VERSION = 1
JSON_TIMELINE_UPLOAD_DIR = Path(DEFAULT_OUTPUT_DIR)
MANUAL_TIMELINE_UPLOAD_DIR = Path(DEFAULT_TIMELINE_DIR)
_ACTIVE_TASK_STATUSES = {"queued", "running"}
_TASK_HISTORY_LIMIT = 200
_TASK_HISTORY_TTL_SEC = 24 * 60 * 60
_OPENABLE_RESULT_TASK_TYPES = {
    "topic_pipeline",
    "timeline_optimization",
    "clip_review_retry",
}
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![\w])(?:[a-z]:\\)[^\r\n]+")
_UPLOAD_INVALID_CHARS_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LAN_PATH_FIELDS = {
    "artifact_dir",
    "ass_path",
    "flv_path",
    "json_path",
    "manual_timeline_path",
    "optimized_timeline_path",
    "output_dir",
    "output_path",
    "report_path",
    "root_dir",
    "srt_path",
    "timeline_path",
    "video_dir",
    "video_path",
}
_LAN_COOKIE_NAME = "autoslice_lan_token"


def _configured_autocover_url(environ=None):
    """只允许跳转到本机 AutoCover，拒绝环境变量注入外部地址。"""
    env = environ if environ is not None else os.environ
    candidate = str(env.get("AUTOCOVER_URL", DEFAULT_AUTOCOVER_URL)).strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return DEFAULT_AUTOCOVER_URL
    if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1 <= port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment):
        return DEFAULT_AUTOCOVER_URL
    return candidate


def _env_flag(name, environ=None):
    env = environ if environ is not None else os.environ
    return str(env.get(name, "")).strip().casefold() in {
        "1", "true", "yes", "on",
    }


def _split_env_values(name, environ=None):
    env = environ if environ is not None else os.environ
    raw = str(env.get(name, "")).strip()
    if not raw:
        return ()
    return tuple(
        item.strip()
        for item in re.split(r"[;,]", raw)
        if item.strip()
    )


def _request_hostname():
    try:
        return (urlsplit(f"//{request.host}").hostname or "").casefold()
    except ValueError:
        return ""


def _trusted_request_hosts(environ=None):
    hosts = set(_LOCAL_HOSTS)
    if _env_flag("AUTOSLICE_LAN_MODE", environ):
        hosts.update(
            host.casefold()
            for host in _split_env_values("AUTOSLICE_LAN_HOSTS", environ)
        )
    return hosts


def _request_origin_is_trusted():
    origin = request.headers.get("Origin")
    if not origin:
        referer = request.headers.get("Referer")
        if not referer:
            return True
        origin = referer
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    origin_host = (parsed.hostname or "").casefold()
    if origin_host == _request_hostname():
        return True
    if not _env_flag("AUTOSLICE_LAN_MODE"):
        return origin_host in _LOCAL_HOSTS
    allowed_origins = {
        value.rstrip("/").casefold()
        for value in _split_env_values("AUTOSLICE_LAN_ORIGINS")
    }
    return origin.rstrip("/").casefold() in allowed_origins


def _lan_token():
    return str(os.environ.get("AUTOSLICE_LAN_TOKEN", "")).strip()


def _lan_request_is_authenticated():
    token = _lan_token()
    if len(token) < 24:
        return False
    presented = (
        request.headers.get("X-AutoSlice-Token")
        or request.cookies.get(_LAN_COOKIE_NAME)
        or request.args.get("token")
        or ""
    )
    return secrets.compare_digest(str(presented), token)


def _lan_allowed_roots():
    roots = {
        PROJECT_DIR,
        PROJECT_TL_DIR,
        DEFAULT_VIDEO_DIR,
        DEFAULT_OUTPUT_DIR,
        DEFAULT_TIMELINE_DIR,
        DEFAULT_SUBMISSION_DIR,
    }
    roots.update(_split_env_values("AUTOSLICE_ALLOWED_ROOTS"))
    return tuple(
        Path(root).expanduser().resolve(strict=False)
        for root in roots
        if str(root).strip()
    )


def _path_within(path, roots):
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _lan_payload_paths_are_allowed():
    if not request.is_json:
        return True
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return True
    roots = _lan_allowed_roots()
    for key, value in payload.items():
        if key not in _LAN_PATH_FIELDS or value in (None, ""):
            continue
        if not isinstance(value, str) or not _path_within(value, roots):
            return False
    return True


@app.before_request
def enforce_local_request_boundary():
    """阻止 DNS rebinding、跨站写请求和未授权的局域网访问。"""

    if _request_hostname() not in _trusted_request_hosts():
        return jsonify({"error": "拒绝不受信任的 Host"}), 403
    if request.method in _WRITE_METHODS and not _request_origin_is_trusted():
        return jsonify({"error": "拒绝跨站写请求"}), 403
    if not _env_flag("AUTOSLICE_LAN_MODE"):
        return None
    if not _lan_request_is_authenticated():
        return jsonify({"error": "局域网模式需要有效访问令牌"}), 401
    if not _lan_payload_paths_are_allowed():
        return jsonify({"error": "请求路径不在局域网允许目录内"}), 403
    return None


@app.after_request
def persist_lan_session(response):
    if (
            _env_flag("AUTOSLICE_LAN_MODE")
            and request.args.get("token")
            and _lan_request_is_authenticated()):
        response.set_cookie(
            _LAN_COOKIE_NAME,
            _lan_token(),
            httponly=True,
            samesite="Strict",
            max_age=12 * 60 * 60,
        )
    return response


def broadcast(event_type, data):
    """向所有 SSE 订阅者推送事件"""
    msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with event_queue_lock:
        subscribers = tuple(event_queues)
    dead = []
    for q in subscribers:
        try:
            q.put_nowait(msg)
        except queue.Full:
            dead.append(q)
    if dead:
        with event_queue_lock:
            for q in dead:
                if q in event_queues:
                    event_queues.remove(q)


def _task_history_timestamp(task):
    for key in ("completed_at", "updated_at", "created_at"):
        try:
            value = float(task.get(key, 0))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _prune_tasks_locked(now=None):
    """清理过期已完成任务；活动任务永不因历史上限被移除。"""
    now = time.time() if now is None else float(now)
    inactive = [
        (task_id, task)
        for task_id, task in tasks.items()
        if task.get("status") not in _ACTIVE_TASK_STATUSES
    ]
    for task_id, task in inactive:
        timestamp = _task_history_timestamp(task)
        if timestamp and now - timestamp > _TASK_HISTORY_TTL_SEC:
            tasks.pop(task_id, None)

    retained = [
        (task_id, task)
        for task_id, task in tasks.items()
        if task.get("status") not in _ACTIVE_TASK_STATUSES
    ]
    retained.sort(
        key=lambda item: _task_history_timestamp(item[1]),
        reverse=True,
    )
    for task_id, _ in retained[_TASK_HISTORY_LIMIT:]:
        tasks.pop(task_id, None)


def _console_print(message, stream=None):
    """控制台编码不支持标题字符时降级输出，日志失败不得中断任务。"""
    stream = stream or sys.stdout
    text = str(message)
    try:
        stream.write(text + "\n")
        stream.flush()
        return
    except (UnicodeEncodeError, OSError):
        pass

    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(
        encoding,
        errors="replace",
    )
    try:
        stream.write(safe_text + "\n")
        stream.flush()
    except (UnicodeEncodeError, OSError):
        pass


def update_task(task_id, **kwargs):
    """更新任务状态并广播 + 控制台输出"""
    now = time.time()
    kwargs.setdefault("updated_at", now)
    if kwargs.get("status") in {"done", "error", "cancelled"}:
        kwargs.setdefault("completed_at", now)
    with task_lock:
        _prune_tasks_locked(now)
        if task_id not in tasks:
            tasks[task_id] = {}
        tasks[task_id].update(kwargs)
        _prune_tasks_locked(now)

    # 控制台同步输出（不用 \r，直接打印）
    status = kwargs.get("status", "")
    progress = kwargs.get("progress", "")
    pct = kwargs.get("step", 0)
    if progress:
        _console_print(f"  [{task_id[:40]}] [{pct}%] {progress}")
    if status in ("done", "error"):
        result = kwargs.get("result", "")
        _console_print(f"  [{task_id[:40]}] >>> {status}: {result}")

    # SSE 广播
    broadcast("task_update", {"task_id": task_id, **kwargs})


def _pipeline_completion_progress(result):
    """生成流水线完成提示，区分报告话题数和实际切片数。"""
    clip_marks = result.get("clip_marks") or []
    topic_count = result.get("topic_count", len(clip_marks))
    return f"完成! {topic_count} 个话题, {result.get('slice_count', 0)} 个切片"


def _subtitle_task_id(prefix, path, nonce=None):
    normalized = os.path.normcase(os.path.abspath(path))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    stem = os.path.splitext(os.path.basename(path))[0][:24]
    run_nonce = str(nonce or secrets.token_hex(4))
    return f"{prefix}_{stem}_{digest}_{run_nonce}"


def _normalized_task_paths(paths):
    """把任务资源路径规范化并去重，供并发冲突判定使用。"""
    result = []
    seen = set()
    for path in paths or ():
        if not path:
            continue
        absolute = os.path.abspath(os.fspath(path))
        normalized = os.path.normcase(absolute)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(absolute)
    return tuple(result)


def _task_resource_set(task, key, *legacy_keys):
    raw_values = task.get(key) or ()
    values = (
        [raw_values]
        if isinstance(raw_values, (str, os.PathLike))
        else list(raw_values)
    )
    values.extend(task.get(name) for name in legacy_keys)
    return {
        os.path.normcase(os.path.abspath(value))
        for value in values
        if value
    }


def _reserve_task(
        prefix, task_type, waiting_progress, *, source_paths=(),
        output_paths=(), conflict_types=None, metadata=None):
    """原子预约后台任务；同源冲突或输出路径冲突均返回已有任务。"""
    absolute_sources = _normalized_task_paths(source_paths)
    absolute_outputs = _normalized_task_paths(output_paths)
    normalized_sources = {
        os.path.normcase(path) for path in absolute_sources
    }
    normalized_outputs = {
        os.path.normcase(path) for path in absolute_outputs
    }
    active_types = set(conflict_types or (task_type,))
    primary_path = (
        absolute_sources[0]
        if absolute_sources
        else absolute_outputs[0] if absolute_outputs else task_type
    )

    with task_lock:
        _prune_tasks_locked()
        for active_id, task in tasks.items():
            if task.get("status") not in _ACTIVE_TASK_STATUSES:
                continue
            existing_sources = _task_resource_set(
                task,
                "source_paths",
                "source_path",
                "source_srt_path",
            )
            existing_outputs = _task_resource_set(
                task,
                "output_paths",
                "output_path",
            )
            same_source = (
                task.get("task_type") in active_types
                and bool(normalized_sources & existing_sources)
            )
            same_output = bool(normalized_outputs & existing_outputs)
            if same_source or same_output:
                return None, active_id

        task_id = _subtitle_task_id(prefix, primary_path)
        task = {
            "status": "queued",
            "progress": waiting_progress,
            "step": 0,
            "total": 100,
            "task_type": task_type,
            "source_paths": list(absolute_sources),
            "output_paths": list(absolute_outputs),
            "created_at": time.time(),
        }
        if absolute_sources:
            task["source_path"] = absolute_sources[0]
        task.update(metadata or {})
        tasks[task_id] = task
    return task_id, None


def _reserve_source_task(
        prefix, task_type, source_path, waiting_progress, conflict_types=None,
        output_paths=(), metadata=None):
    """兼容单源任务调用，实际统一走资源预约器。"""
    return _reserve_task(
        prefix,
        task_type,
        waiting_progress,
        source_paths=(source_path,),
        output_paths=output_paths,
        conflict_types=conflict_types,
        metadata=metadata,
    )


def _set_task_output_dir(task_id, output_dir):
    """记录任务实际输出根目录，供完成后安全打开整理包。"""
    absolute_output_dir = os.path.abspath(output_dir)
    with task_lock:
        if task_id in tasks:
            tasks[task_id]["output_dir"] = absolute_output_dir
    return absolute_output_dir


def _request_streamer_profile(data, video_path):
    """在请求线程解析并冻结主播配置，避免后台线程上下文丢失。"""
    profile_id = str(
        (data or {}).get("streamer_profile_id") or "auto"
    ).strip().casefold()
    return resolve_streamer_profile(profile_id, video_path)


def _topic_task_output_paths(flv_path, output_dir):
    base_name = os.path.splitext(os.path.basename(flv_path))[0]
    return (
        os.path.join(output_dir, base_name + "_自动切片"),
        os.path.join(output_dir, base_name + "_话题切片"),
    )


def _completed_task_artifact_dir(task_id):
    """解析并校验已完成任务的整理包目录，拒绝任意路径打开。"""
    with task_lock:
        task = dict(tasks.get(task_id) or {})
    if not task:
        raise KeyError("任务不存在或服务已重启")
    if task.get("status") != "done":
        raise RuntimeError("任务尚未完成，不能打开结果目录")
    if task.get("task_type") not in _OPENABLE_RESULT_TASK_TYPES:
        raise PermissionError("该任务没有可打开的自动切片整理包")

    result = task.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError) as exc:
            raise ValueError("任务结果不是有效 JSON") from exc
    if not isinstance(result, dict) or not result.get("artifact_dir"):
        raise ValueError("任务结果中没有整理包路径")

    output_dir = task.get("output_dir")
    if not output_dir:
        raise PermissionError("任务没有记录输出目录，不能安全打开")
    output_root = Path(output_dir).expanduser().resolve(strict=True)
    artifact_dir = Path(result["artifact_dir"]).expanduser().resolve(strict=True)
    if not artifact_dir.is_dir():
        raise FileNotFoundError("整理包目录不存在")
    try:
        relative_path = artifact_dir.relative_to(output_root)
    except ValueError as exc:
        raise PermissionError("整理包路径超出任务输出目录") from exc
    if (
            relative_path == Path(".")
            or len(relative_path.parts) != 1
            or not artifact_dir.name.endswith("_自动切片")):
        raise PermissionError("任务结果不是有效的自动切片整理包")
    return artifact_dir


def _safe_task_error(error):
    """生成可发给前端的单行错误，不包含凭据、路径或堆栈。"""
    message = " ".join(str(error).split())
    message = re.sub(
        r"(?i)\b(?:api[_ -]?key|token)\s*[:=]\s*[^\s,;]+",
        "[已隐藏]",
        message,
    )
    message = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [已隐藏]", message)
    message = re.sub(r"(?i)\bsk-[a-z0-9._-]{4,}", "[已隐藏]", message)
    message = _WINDOWS_PATH_RE.sub("[本地路径已隐藏]", message)
    if not message:
        message = "后台处理失败"
    return f"{type(error).__name__}: {message}"[:500]


def _record_task_error(task_id, progress, error, *, total=100):
    """堆栈仅写服务日志，SSE 和任务结果只保存脱敏摘要。"""
    stack = "".join(traceback.format_tb(error.__traceback__))
    app.logger.error("%s\n%s%s", progress, stack, _safe_task_error(error))
    update_task(
        task_id,
        status="error",
        progress=progress,
        result=_safe_task_error(error),
        step=0,
        total=total,
    )


def _validated_upload_filename(raw_filename, allowed_suffixes):
    filename = str(raw_filename or "")
    if not filename or filename != filename.strip(" ."):
        raise ValueError("文件名为空或格式不安全")
    if filename in {".", ".."} or _UPLOAD_INVALID_CHARS_RE.search(filename):
        raise ValueError("文件名不能包含路径或 Windows 非法字符")
    suffix = Path(filename).suffix.casefold()
    if suffix not in {item.casefold() for item in allowed_suffixes}:
        expected = "、".join(sorted(allowed_suffixes))
        raise ValueError(f"只允许上传 {expected} 文件")
    return filename


def _save_uploaded_file(field_name, target_dir, allowed_suffixes, *, validate_json=False):
    file = request.files.get(field_name)
    if file is None:
        raise ValueError("无文件")
    filename = _validated_upload_filename(file.filename, allowed_suffixes)
    root = Path(target_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / filename).resolve()
    if destination.parent != root:
        raise ValueError("上传文件必须保存在指定目录")
    temporary = root / f".{filename}.{secrets.token_hex(6)}.upload"
    try:
        file.save(str(temporary))
        if validate_json:
            with temporary.open(encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            if not isinstance(payload, (dict, list)):
                raise ValueError("JSON 时间轴顶层必须是对象或数组")
        os.replace(temporary, destination)
    except json.JSONDecodeError as exc:
        raise ValueError("JSON 时间轴内容无效") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return destination


def _reserve_subtitle_review_task(srt_path, force):
    """原子登记检查任务；同一源字幕同时只允许一个检查。"""
    return _reserve_task(
        "subtitle_review",
        "subtitle_review",
        "字幕检查等待启动...",
        source_paths=(srt_path,),
        metadata={
            "source_srt_path": os.path.abspath(srt_path),
            "force": bool(force),
        },
    )


def _validate_subtitle_path(srt_path):
    if not srt_path or not os.path.isfile(srt_path):
        raise ValueError("SRT 字幕文件不存在")
    if os.path.splitext(srt_path)[1].lower() != ".srt":
        raise ValueError("字幕文件必须是 SRT")
    return os.path.abspath(srt_path)


def _validate_subtitle_pair(video_path, srt_path):
    if not video_path or not os.path.isfile(video_path):
        raise ValueError("投稿视频文件不存在")
    if os.path.splitext(video_path)[1].lower() not in {".mp4", ".mov", ".mkv"}:
        raise ValueError("投稿视频格式不受支持")
    video_path = os.path.abspath(video_path)
    srt_path = _validate_subtitle_path(srt_path)
    if os.path.normcase(os.path.dirname(video_path)) != os.path.normcase(os.path.dirname(srt_path)):
        raise ValueError("视频和字幕必须位于同一投稿目录")
    return video_path, srt_path


def _validate_subtitle_output_path(video_path, output_path):
    if not output_path:
        return None
    output_path = os.path.abspath(output_path)
    if os.path.splitext(output_path)[1].lower() != ".mp4":
        raise ValueError("字幕版输出文件必须是 MP4")
    if os.path.normcase(os.path.dirname(video_path)) != os.path.normcase(os.path.dirname(output_path)):
        raise ValueError("字幕版视频必须输出到原投稿目录")
    if os.path.normcase(video_path) == os.path.normcase(output_path):
        raise ValueError("字幕版输出不能覆盖原视频")
    return output_path


def run_subtitle_review_task(
        task_id, srt_path, context_title, glossary=None, force=False):
    """后台生成字幕错字建议，不直接改文件。"""
    update_task(
        task_id,
        status="running",
        progress="准备检查字幕错别字...",
        step=0,
        total=100,
    )

    def callback(msg, step, total):
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from subtitle_workflow import (
            high_confidence_corrections,
            suggest_subtitle_corrections,
        )

        result = suggest_subtitle_corrections(
            srt_path,
            context_title=context_title,
            glossary=glossary,
            use_cache=not force,
            progress_callback=callback,
        )
        result["default_corrections"] = high_confidence_corrections(result)
        update_task(
            task_id,
            status="done",
            progress=f"字幕检查完成，发现 {len(result['suggestions'])} 条建议",
            result=json.dumps(result, ensure_ascii=False),
            step=100,
            total=100,
        )
    except Exception as exc:
        _record_task_error(task_id, "字幕检查失败", exc)


def run_subtitle_render_task(
        task_id, video_path, srt_path, style, export_settings,
        output_path=None):
    """后台把确认后的字幕压制进新视频。"""
    update_task(
        task_id,
        status="running",
        progress="准备字幕样式和编码器...",
        step=0,
        total=100,
    )

    def callback(msg, step, total):
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from subtitle_workflow import burn_subtitles

        result = burn_subtitles(
            video_path,
            srt_path,
            style=style,
            export_settings=export_settings,
            output_path=output_path,
            progress_callback=callback,
        )
        update_task(
            task_id,
            status="done",
            progress="字幕版视频压制完成",
            result=json.dumps(result, ensure_ascii=False),
            step=100,
            total=100,
        )
    except Exception as exc:
        _record_task_error(task_id, "字幕版视频压制失败", exc)


def run_timeline_optimization_task(
        task_id, flv_path, manual_timeline_path, ass_path=None, output_dir=None,
        streamer_profile="auto"):
    """后台仅优化人工时间轴，不启动话题分析和切片。"""
    update_task(task_id, status="running", progress="准备校准人工时间轴...", step=0, total=100)

    def callback(msg, step, total):
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from topic_engine import optimize_manual_timeline_for_video

        result = optimize_manual_timeline_for_video(
            flv_path,
            manual_timeline_path,
            ass_path=ass_path if ass_path and os.path.isfile(ass_path) else None,
            progress_callback=callback,
            output_dir=output_dir,
            streamer_profile_id=streamer_profile,
        )
        update_task(
            task_id,
            status="done",
            progress="人工时间轴优化完成",
            result=json.dumps(result, ensure_ascii=False),
            step=100,
            total=100,
        )
    except Exception as exc:
        _record_task_error(task_id, "人工时间轴优化失败", exc)


def run_clip_review_retry_task(
        task_id, flv_path, ass_path=None, output_dir=None,
        streamer_profile="auto"):
    """复用现有话题产物，只重做候选复核并刷新实际切片。"""
    update_task(
        task_id,
        status="running",
        progress="准备复用现有话题报告...",
        step=0,
        total=100,
    )

    def callback(msg, step, total):
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from topic_engine import retry_clip_review_from_artifacts, slice_from_marks

        result = retry_clip_review_from_artifacts(
            flv_path,
            ass_path=ass_path if ass_path and os.path.isfile(ass_path) else None,
            progress_callback=callback,
            output_dir=output_dir,
            streamer_profile_id=streamer_profile,
        )
        clip_marks = result.get("clip_marks") or []
        if clip_marks:
            count, out_dir = slice_from_marks(
                flv_path,
                result["json_path"],
                output_dir,
                progress_callback=callback,
                streamer_profile_id=streamer_profile,
            )
            result["slice_count"] = count
            result["slice_dir"] = out_dir
        update_task(
            task_id,
            status="done",
            progress=_pipeline_completion_progress(result),
            result=json.dumps(result, ensure_ascii=False),
            step=100,
            total=100,
        )
    except Exception as exc:
        _record_task_error(task_id, "候选复核失败", exc)


def run_slice_task(
        task_id, flv_path, ass_path, output_dir, mode, timeline_path,
        timeline_json=None, streamer_profile="auto"):
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
        with streamer_profile_context(streamer_profile, flv_path):
            if timeline_json:
                from topic_engine import slice_from_marks
                count, out_dir = slice_from_marks(
                    flv_path,
                    timeline_json,
                    output_dir,
                    progress_callback=callback,
                    streamer_profile_id=streamer_profile,
                )
            else:
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
    with event_queue_lock:
        event_queues.append(q)

    def generate():
        # 先发送当前所有任务状态
        with task_lock:
            _prune_tasks_locked()
            current = dict(tasks)
        yield f"event: init\ndata: {json.dumps(current, ensure_ascii=False)}\n\n"
        try:
            while True:
                with event_queue_lock:
                    if q not in event_queues:
                        break
                try:
                    msg = q.get(timeout=15)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with event_queue_lock:
                if q in event_queues:
                    event_queues.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ==================== API 端点 ====================

@app.route("/")
def index():
    return render_template(
        "topic_v2.html",
        default_video_dir=DEFAULT_VIDEO_DIR,
        default_output_dir=DEFAULT_OUTPUT_DIR,
    )


@app.route("/direct-slice")
def direct_slice_page():
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
    data = request.get_json(silent=True) or {}
    flv_path = data.get("flv_path", "")
    output_dir = os.path.abspath(data.get("output_dir") or DEFAULT_OUTPUT_DIR)
    mode = data.get("mode", "danmaku")
    timeline_path = data.get("timeline_path", "")

    if not os.path.isfile(flv_path):
        return jsonify({"error": "视频文件不存在"}), 400
    try:
        streamer_profile = _request_streamer_profile(data, flv_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    ass_path = flv_path[:-4] + ".ass"
    if mode == "danmaku" and not os.path.isfile(ass_path):
        return jsonify({"error": "缺少对应的 .ass 弹幕文件"}), 400

    # 时间轴/混合模式：自动复制到项目文件夹
    if timeline_path and os.path.isfile(timeline_path):
        import shutil
        dest = os.path.join(PROJECT_TL_DIR, os.path.basename(timeline_path))
        if not os.path.exists(dest) or os.path.getmtime(timeline_path) > os.path.getmtime(dest):
            shutil.copy2(timeline_path, dest)
        timeline_path = dest

    timeline_json = data.get("timeline_json", "")
    if mode == "timeline-json":
        if not os.path.isfile(timeline_json):
            return jsonify({"error": "JSON 标记文件不存在"}), 400
        mode = "timeline"
        timeline_path = ""
    base_name = os.path.splitext(os.path.basename(flv_path))[0]
    direct_output = os.path.join(
        output_dir,
        base_name + "_话题切片" if timeline_json else base_name,
    )
    task_id, active_task_id = _reserve_source_task(
        "direct_slice",
        "direct_slice",
        flv_path,
        "高级重新切片等待启动...",
        conflict_types={
            "direct_slice",
            "topic_pipeline",
            "clip_review_retry",
        },
        output_paths=(direct_output,),
        metadata={
            "streamer_profile_id": streamer_profile.id,
            "mode": mode,
        },
    )
    if active_task_id:
        return jsonify({
            "error": "该录播或输出目录正在切片，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    _set_task_output_dir(task_id, output_dir)
    try:
        threading.Thread(
            target=run_slice_task,
            args=(
                task_id,
                flv_path,
                ass_path,
                output_dir,
                mode,
                timeline_path,
                timeline_json,
                streamer_profile,
            ),
            daemon=True,
        ).start()
    except Exception as exc:
        _record_task_error(task_id, "高级重新切片启动失败", exc)
        return jsonify({"error": _safe_task_error(exc), "task_id": task_id}), 500
    return jsonify({"task_id": task_id})


@app.route("/api/open-result-directory", methods=["POST"])
def open_result_directory():
    """打开已完成任务的整理包；请求方不能直接指定本机路径。"""
    data = request.get_json(silent=True) or {}
    task_id = str(data.get("task_id") or "").strip()
    if not task_id:
        return jsonify({"error": "缺少任务 ID"}), 400
    try:
        artifact_dir = _completed_task_artifact_dir(task_id)
    except KeyError as exc:
        return jsonify({"error": str(exc).strip("'")}), 404
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        subprocess.Popen(["explorer.exe", str(artifact_dir)])
    except OSError as exc:
        return jsonify({"error": f"无法打开结果目录: {exc}"}), 500
    return jsonify({"path": str(artifact_dir)})


# ==================== 字幕校对与压制 ====================

@app.route("/api/subtitles/defaults", methods=["GET"])
def subtitle_defaults():
    from subtitle_workflow import (
        DEFAULT_SUBTITLE_STYLE,
        DEFAULT_VIDEO_EXPORT,
        verify_exact_subtitle_font,
    )

    return jsonify({
        "submission_dir": DEFAULT_SUBMISSION_DIR,
        "style": DEFAULT_SUBTITLE_STYLE,
        "export": DEFAULT_VIDEO_EXPORT,
        "font": verify_exact_subtitle_font(),
    })


@app.route("/api/subtitles/scan", methods=["POST"])
def subtitle_scan():
    from subtitle_workflow import scan_submission_pairs

    data = request.get_json(silent=True) or {}
    root_dir = data.get("root_dir") or DEFAULT_SUBMISSION_DIR
    try:
        pairs = scan_submission_pairs(root_dir)
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"root_dir": os.path.abspath(root_dir), "pairs": pairs, "count": len(pairs)})


@app.route("/api/subtitles/cues", methods=["POST"])
def subtitle_cues():
    from subtitle_workflow import parse_srt_document

    data = request.get_json(silent=True) or {}
    try:
        srt_path = _validate_subtitle_path(data.get("srt_path", ""))
        cues = [cue.to_dict() for cue in parse_srt_document(srt_path)]
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"srt_path": srt_path, "cues": cues, "count": len(cues)})


@app.route("/api/subtitles/review", methods=["POST"])
def subtitle_review():
    data = request.get_json(silent=True) or {}
    try:
        video_path, srt_path = _validate_subtitle_pair(
            data.get("video_path", ""),
            data.get("srt_path", ""),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    force = data.get("force", False)
    if not isinstance(force, bool):
        return jsonify({"error": "force 必须是布尔值"}), 400
    context_title = str(
        data.get("context_title") or os.path.basename(os.path.dirname(video_path))
    ).strip()
    if len(context_title) > 300:
        return jsonify({"error": "视频标题过长"}), 400
    glossary = data.get("glossary")
    if glossary is not None and not isinstance(glossary, list):
        return jsonify({"error": "优先词表必须是数组"}), 400
    if glossary is not None:
        if any(not isinstance(item, str) for item in glossary):
            return jsonify({"error": "优先词表中的词条必须是字符串"}), 400
        glossary = [item.strip() for item in glossary if item.strip()]
        if len(glossary) > 100 or any(len(item) > 100 for item in glossary):
            return jsonify({"error": "优先词表过长"}), 400

    task_id, active_task_id = _reserve_subtitle_review_task(srt_path, force)
    if active_task_id:
        return jsonify({
            "error": "该字幕正在检查，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    try:
        threading.Thread(
            target=run_subtitle_review_task,
            args=(task_id, srt_path, context_title, glossary, force),
            daemon=True,
        ).start()
    except Exception as exc:
        update_task(
            task_id,
            status="error",
            progress="字幕检查启动失败",
            result=str(exc),
            step=0,
            total=100,
        )
        return jsonify({"error": f"字幕检查启动失败: {exc}"}), 500
    return jsonify({"task_id": task_id})


@app.route("/api/subtitles/save", methods=["POST"])
def subtitle_save():
    from subtitle_workflow import parse_srt_document, save_corrected_srt

    data = request.get_json(silent=True) or {}
    corrections = data.get("corrections", [])
    if not isinstance(corrections, list):
        return jsonify({"error": "字幕修正必须是数组"}), 400
    try:
        srt_path = _validate_subtitle_path(data.get("srt_path", ""))
        output_path = save_corrected_srt(srt_path, corrections)
        cue_count = len(parse_srt_document(output_path))
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "source_srt_path": srt_path,
        "corrected_srt_path": output_path,
        "correction_count": len(corrections),
        "cue_count": cue_count,
    })


@app.route("/api/subtitles/preview", methods=["POST"])
def subtitle_preview():
    from subtitle_workflow import render_subtitle_preview

    data = request.get_json(silent=True) or {}
    try:
        video_path, srt_path = _validate_subtitle_pair(
            data.get("video_path", ""),
            data.get("srt_path", ""),
        )
        image_bytes, selected_time = render_subtitle_preview(
            video_path,
            srt_path,
            style=data.get("style"),
            preview_time=data.get("preview_time"),
            export_settings=data.get("export"),
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return jsonify({"error": str(exc)}), 400
    response = Response(image_bytes, mimetype="image/jpeg")
    response.headers["X-Subtitle-Preview-Time"] = f"{selected_time:.3f}"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/subtitles/render", methods=["POST"])
def subtitle_render():
    data = request.get_json(silent=True) or {}
    try:
        video_path, srt_path = _validate_subtitle_pair(
            data.get("video_path", ""),
            data.get("srt_path", ""),
        )
        output_path = _validate_subtitle_output_path(
            video_path,
            data.get("output_path", ""),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    output_path = output_path or (
        os.path.splitext(video_path)[0] + "_字幕版.mp4"
    )
    task_id, active_task_id = _reserve_task(
        "subtitle_render",
        "subtitle_render",
        "字幕版视频等待压制...",
        source_paths=(video_path, srt_path),
        output_paths=(output_path,),
        metadata={
            "source_srt_path": srt_path,
            "output_path": output_path,
        },
    )
    if active_task_id:
        return jsonify({
            "error": "该视频正在压制字幕，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    try:
        threading.Thread(
            target=run_subtitle_render_task,
            args=(
                task_id,
                video_path,
                srt_path,
                data.get("style"),
                data.get("export"),
                output_path,
            ),
            daemon=True,
        ).start()
    except Exception as exc:
        _record_task_error(task_id, "字幕版视频压制启动失败", exc)
        return jsonify({"error": _safe_task_error(exc), "task_id": task_id}), 500
    return jsonify({"task_id": task_id})


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
    try:
        save_path = _save_uploaded_file(
            "file",
            JSON_TIMELINE_UPLOAD_DIR,
            {".json"},
            validate_json=True,
        )
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"path": str(save_path), "name": save_path.name})


@app.route("/api/timelines", methods=["GET"])
def list_timelines():
    timeline_dir = DEFAULT_TIMELINE_DIR
    if not os.path.isdir(timeline_dir):
        return jsonify({"files": []})
    files = sorted(glob_mod.glob(os.path.join(timeline_dir, "*.docx")), reverse=True)
    return jsonify({"files": [{"name": os.path.basename(f), "path": f} for f in files]})


@app.route("/api/upload-timeline", methods=["POST"])
def upload_timeline():
    try:
        save_path = _save_uploaded_file(
            "file",
            MANUAL_TIMELINE_UPLOAD_DIR,
            {".docx"},
        )
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"path": str(save_path), "name": save_path.name})


# ==================== 话题分析 ====================

@app.route("/topic-v2")
def topic_v2_page():
    return render_template(
        "topic_v2.html",
        default_video_dir=DEFAULT_VIDEO_DIR,
        default_output_dir=DEFAULT_OUTPUT_DIR,
    )


@app.route("/subtitle-workflow")
def subtitle_workflow_page():
    return render_template("subtitle_workflow.html")


@app.route("/autocover")
def autocover_page():
    return redirect(_configured_autocover_url())


@app.route("/api/service")
def service_contract():
    return jsonify({
        "service": AUTOSLICE_SERVICE_ID,
        "api_version": AUTOSLICE_API_VERSION,
        "autocover_url": _configured_autocover_url(),
    })


@app.route("/api/streamer-profiles")
def streamer_profiles_contract():
    """返回前端可选择的公开主播配置，不暴露路径和 ASR 内部规则。"""
    try:
        return jsonify({"profiles": public_streamer_profiles()})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/start-pipeline", methods=["POST"])
def start_pipeline():
    """启动完整话题分析流水线（v2）"""
    data = request.get_json(silent=True) or {}
    flv_path = data.get("flv_path", "")
    ass_path = data.get("ass_path", "")
    output_dir = os.path.abspath(
        data.get("output_dir") or DEFAULT_OUTPUT_DIR
    )
    manual_timeline_mode = data.get("manual_timeline_mode", "none")
    manual_timeline_path = data.get("manual_timeline_path", "")
    optimized_timeline_path = data.get("optimized_timeline_path", "")

    if not os.path.isfile(flv_path):
        return jsonify({"error": "视频文件不存在"}), 400
    try:
        streamer_profile = _request_streamer_profile(data, flv_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if manual_timeline_mode == "manual" and not os.path.isfile(manual_timeline_path):
        return jsonify({"error": "指定的辅助时间轴文件不存在"})
    if optimized_timeline_path and not os.path.isfile(optimized_timeline_path):
        return jsonify({"error": "指定的优化时间轴文件不存在"})
    if manual_timeline_mode == "none":
        manual_timeline_path = "__none__"
        optimized_timeline_path = None
    elif manual_timeline_mode != "manual":
        manual_timeline_path = None
        optimized_timeline_path = None

    task_id, active_task_id = _reserve_source_task(
        "pipeline",
        "topic_pipeline",
        flv_path,
        "完整分析等待启动...",
        conflict_types={"topic_pipeline", "clip_review_retry", "direct_slice"},
        output_paths=_topic_task_output_paths(flv_path, output_dir),
        metadata={"streamer_profile_id": streamer_profile.id},
    )
    if active_task_id:
        return jsonify({
            "error": "该录播正在进行完整分析，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    _set_task_output_dir(task_id, output_dir)

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
                optimized_timeline_path=optimized_timeline_path,
                output_dir=output_dir,
                streamer_profile_id=streamer_profile,
            )

            # 用新的独立切片功能，不依赖现有切片模式
            clip_marks = result.get("clip_marks", [])
            if clip_marks:
                count, out_dir = slice_from_marks(
                    flv_path, result["json_path"], output_dir,
                    progress_callback=cb,
                    streamer_profile_id=streamer_profile,
                )
                result["slice_count"] = count
                result["slice_dir"] = out_dir

            update_task(task_id, status="done",
                        progress=_pipeline_completion_progress(result),
                        result=json.dumps(result, ensure_ascii=False),
                        step=100)
        except Exception as exc:
            _record_task_error(task_id, "完整分析失败", exc)

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception as exc:
        _record_task_error(task_id, "完整分析启动失败", exc)
        return jsonify({"error": _safe_task_error(exc), "task_id": task_id}), 500
    return jsonify({"task_id": task_id})


@app.route("/api/retry-clip-review", methods=["POST"])
def retry_clip_review():
    """复用已有逐话题产物，仅重新复核高能候选并刷新切片。"""
    data = request.get_json(silent=True) or {}
    flv_path = data.get("flv_path", "")
    ass_path = data.get("ass_path", "")
    output_dir = os.path.abspath(
        data.get("output_dir") or DEFAULT_OUTPUT_DIR
    )
    if not os.path.isfile(flv_path):
        return jsonify({"error": "视频文件不存在"}), 400
    try:
        streamer_profile = _request_streamer_profile(data, flv_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    task_id, active_task_id = _reserve_source_task(
        "clip_review",
        "clip_review_retry",
        flv_path,
        "候选复核等待启动...",
        conflict_types={"topic_pipeline", "clip_review_retry", "direct_slice"},
        output_paths=_topic_task_output_paths(flv_path, output_dir),
        metadata={"streamer_profile_id": streamer_profile.id},
    )
    if active_task_id:
        return jsonify({
            "error": "该录播正在分析或复核，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    _set_task_output_dir(task_id, output_dir)
    try:
        threading.Thread(
            target=run_clip_review_retry_task,
            args=(task_id, flv_path, ass_path, output_dir, streamer_profile),
            daemon=True,
        ).start()
    except Exception as exc:
        _record_task_error(task_id, "候选复核启动失败", exc)
        return jsonify({"error": _safe_task_error(exc), "task_id": task_id}), 500
    return jsonify({"task_id": task_id})


@app.route("/api/optimize-manual-timeline", methods=["POST"])
def optimize_manual_timeline():
    """启动独立人工时间轴优化任务。"""
    data = request.get_json(silent=True) or {}
    flv_path = data.get("flv_path", "")
    ass_path = data.get("ass_path", "")
    manual_timeline_path = data.get("manual_timeline_path", "")
    output_dir = os.path.abspath(
        data.get("output_dir") or DEFAULT_OUTPUT_DIR
    )
    if not os.path.isfile(flv_path):
        return jsonify({"error": "视频文件不存在"}), 400
    if not os.path.isfile(manual_timeline_path):
        return jsonify({"error": "指定的人工时间轴 DOCX 不存在"}), 400
    try:
        streamer_profile = _request_streamer_profile(data, flv_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    task_id, active_task_id = _reserve_source_task(
        "timeline_opt",
        "timeline_optimization",
        flv_path,
        "人工时间轴优化等待启动...",
        output_paths=(_topic_task_output_paths(flv_path, output_dir)[0],),
        metadata={"streamer_profile_id": streamer_profile.id},
    )
    if active_task_id:
        return jsonify({
            "error": "该录播正在优化人工时间轴，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    _set_task_output_dir(task_id, output_dir)
    try:
        threading.Thread(
            target=run_timeline_optimization_task,
            args=(
                task_id,
                flv_path,
                manual_timeline_path,
                ass_path,
                output_dir,
                streamer_profile,
            ),
            daemon=True,
        ).start()
    except Exception as exc:
        _record_task_error(task_id, "人工时间轴优化启动失败", exc)
        return jsonify({"error": _safe_task_error(exc), "task_id": task_id}), 500
    return jsonify({"task_id": task_id})


if __name__ == "__main__":
    pass
