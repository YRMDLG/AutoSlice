"""
AutoSlice Web 界面 — SSE 实时推送 + 控制台同步
"""

import os, sys, json, time, threading, queue, glob as glob_mod, secrets, subprocess, re, traceback, hashlib
from collections import deque
from collections.abc import MutableMapping
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, render_template, request, jsonify, Response, redirect, abort

from autoslice.media_formats import (
    SUPPORTED_VIDEO_EXTENSIONS,
    is_analyzable_video,
    is_scannable_video,
)
from autoslice.subtitle_workflow import SUBTITLE_ASR_VERSION, SUBTITLE_REVIEW_VERSION
from autoslice.streamer_profiles import (
    public_streamer_profiles,
    resolve_streamer_profile,
    streamer_profile_context,
)
from autoslice.runtime_config import OUTPUT_DIR, SUBMISSION_DIR, TIMELINE_DIR, VIDEO_DIR
from autoslice.security_policy import SecurityPolicy
from autoslice.task_registry import (
    ACTIVE_TASK_STATUSES,
    TaskLifecycleError,
    TaskRegistry,
)
from autoslice.task_results import (
    build_pipeline_result_summary,
    normalize_task_result,
)
from autoslice.task_store import (
    DEFAULT_TASK_DATABASE_PATH,
    MAX_LIST_LIMIT,
    TERMINAL_TASK_STATUSES,
    TaskNotFoundError,
    TaskStore,
)
from autoslice.paths import APPLICATION_DATA_ROOT, STATIC_DIR, TEMPLATE_DIR

app = Flask(
    __name__,
    static_folder=str(STATIC_DIR),
    template_folder=str(TEMPLATE_DIR),
)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

task_lock = threading.RLock()
event_queues = []
event_queue_lock = threading.RLock()
_event_history = deque()
_event_sequence = 0

PROJECT_DIR = str(APPLICATION_DATA_ROOT)


def _configured_directory(env_name, fallback):
    """读取可移植的本机目录默认值，支持环境变量和波浪号。"""
    raw_value = os.environ.get(env_name) or str(fallback)
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw_value)))


DEFAULT_VIDEO_DIR = _configured_directory(
    "AUTOSLICE_VIDEO_DIR",
    VIDEO_DIR,
)
DEFAULT_OUTPUT_DIR = _configured_directory(
    "AUTOSLICE_OUTPUT_DIR",
    OUTPUT_DIR,
)
DEFAULT_TIMELINE_DIR = _configured_directory(
    "AUTOSLICE_TIMELINE_DIR",
    TIMELINE_DIR,
)
DEFAULT_SUBMISSION_DIR = _configured_directory(
    "AUTOSLICE_SUBMISSION_DIR",
    SUBMISSION_DIR,
)
PROJECT_TL_DIR = DEFAULT_TIMELINE_DIR
for _runtime_dir in (
        DEFAULT_VIDEO_DIR,
        DEFAULT_OUTPUT_DIR,
        DEFAULT_TIMELINE_DIR,
        DEFAULT_SUBMISSION_DIR):
    os.makedirs(_runtime_dir, exist_ok=True)
DEFAULT_AUTOCOVER_URL = "http://127.0.0.1:5010"
AUTOSLICE_SERVICE_ID = "autoslice"
AUTOSLICE_API_VERSION = 1
LEGACY_DIRECT_SLICE_ENV = "AUTOSLICE_ENABLE_LEGACY_DIRECT_SLICE"
JSON_TIMELINE_UPLOAD_DIR = Path(DEFAULT_OUTPUT_DIR)
MANUAL_TIMELINE_UPLOAD_DIR = Path(DEFAULT_TIMELINE_DIR)
_TASK_HISTORY_LIMIT = 200
_TASK_HISTORY_TTL_SEC = 24 * 60 * 60
_SSE_EVENT_HISTORY_LIMIT = 500
_SSE_EVENT_HISTORY_TTL_SEC = 60 * 60
_SSE_SUBSCRIBER_QUEUE_SIZE = 50
_OPENABLE_RESULT_TASK_TYPES = {
    "topic_pipeline",
    "timeline_optimization",
    "clip_review_retry",
}
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![\w])(?:[a-z]:\\)[^\r\n]+")
_POSIX_PRIVATE_PATH_RE = re.compile(
    r"(?<![\w])/(?:home|Users|tmp|var|etc|opt|mnt)/[^\s,;]+",
    re.IGNORECASE,
)
_SSE_PATH_KEY_SUFFIXES = ("path", "paths", "dir", "directory", "directories")
_UPLOAD_INVALID_CHARS_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_SESSION_BOOTSTRAP_PATHS = frozenset({
    "/",
    "/direct-slice",
    "/subtitle-workflow",
    "/topic-v2",
    "/api/security/session",
})
security_policy = SecurityPolicy(
    env_prefix="AUTOSLICE",
    cookie_name="autoslice_local_session",
    access_header="X-AutoSlice-Token",
)


def _task_database_path(environ=None):
    """返回本机任务数据库路径；环境变量只覆盖本地持久化位置。"""

    env = environ if environ is not None else os.environ
    configured = str(env.get("AUTOSLICE_TASK_DB", "")).strip()
    return Path(configured or DEFAULT_TASK_DATABASE_PATH).expanduser().resolve()


class _TaskMappingView(MutableMapping):
    """旧 ``tasks`` 接口的实时 SQLite 视图，不保存第二份任务字典。"""

    _CORE_FIELDS = frozenset({
        "completed_at",
        "created_at",
        "error",
        "error_summary",
        "finished_at",
        "message",
        "metadata",
        "output_path",
        "output_paths",
        "progress",
        "result",
        "result_summary",
        "source_path",
        "source_paths",
        "status",
        "step",
        "streamer_profile",
        "streamer_profile_snapshot",
        "task_id",
        "task_type",
        "total",
        "updated_at",
    })

    @staticmethod
    def _registry():
        return task_registry

    def __getitem__(self, task_id):
        snapshot = self._registry().snapshot(str(task_id))
        if snapshot is None:
            raise KeyError(task_id)
        legacy_result = snapshot.get("result")
        if legacy_result is not None and not isinstance(legacy_result, str):
            # TaskRegistry 的规范快照保留真实 JSON 类型；旧 Web/SSE 契约继续
            # 提供 JSON 字符串，供现有页面和外部脚本渐进迁移。
            snapshot["result"] = json.dumps(
                legacy_result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        return snapshot

    def __setitem__(self, task_id, value):
        """把旧测试/外部赋值直接翻译成一次 store 替换。"""

        if not isinstance(value, dict):
            raise TypeError("任务值必须是字典")
        task_id = str(task_id)
        status = str(value.get("status") or "queued")
        task_type = str(value.get("task_type") or "legacy")
        step = value.get("step", 0)
        total = value.get("total", 100)

        raw_metadata = value.get("metadata") or {}
        if not isinstance(raw_metadata, dict):
            raise TypeError("metadata 必须是字典")
        metadata = dict(raw_metadata)
        metadata.update({
            key: item
            for key, item in value.items()
            if key not in self._CORE_FIELDS
        })

        source_paths = value.get("source_paths")
        if source_paths is None:
            source_paths = tuple(
                path
                for path in (
                    value.get("source_path"),
                    value.get("source_srt_path"),
                    value.get("source_video_path"),
                )
                if path
            )
        output_paths = value.get("output_paths")
        if output_paths is None:
            output_paths = tuple(
                path for path in (value.get("output_path"),) if path
            )

        result = value.get("result_summary", value.get("result"))
        error = value.get("error_summary", value.get("error"))
        if status in {"error", "cancelled"} and error is None:
            error = result
            result = None

        finished_at = value.get("finished_at", value.get("completed_at"))
        created_at = value.get("created_at")
        if status in TERMINAL_TASK_STATUSES:
            if finished_at is None:
                finished_at = time.time()
            if created_at is None or float(created_at) > float(finished_at):
                created_at = finished_at
        elif created_at is None:
            created_at = time.time()
        else:
            finished_at = None

        profile = value.get(
            "streamer_profile_snapshot",
            value.get("streamer_profile"),
        ) or {}
        registry = self._registry()
        with registry.store.transaction() as transaction:
            transaction.delete_task(task_id)
            transaction.create_task(
                task_id,
                task_type,
                source_paths=source_paths,
                output_paths=output_paths,
                status=status,
                progress=str(value.get("progress") or ""),
                message=str(value.get("message") or ""),
                step=step,
                total=total,
                result_summary=result,
                error_summary=error,
                metadata=metadata,
                streamer_profile_snapshot=profile,
                created_at=created_at,
                finished_at=finished_at,
            )

    def __delitem__(self, task_id):
        task_id = str(task_id)
        registry = self._registry()
        if not registry.store.delete_task(task_id):
            raise KeyError(task_id)
        registry.forget_cancellation_events((task_id,))

    def __iter__(self):
        records = self._registry().list(
            limit=MAX_LIST_LIMIT,
            order="created_asc",
        )
        return iter(tuple(record.task_id for record in records))

    def __len__(self):
        return len(self._registry().list(limit=MAX_LIST_LIMIT))

    def clear(self):
        registry = self._registry()
        with registry.store.transaction() as transaction:
            while True:
                records = transaction.list_tasks(
                    limit=MAX_LIST_LIMIT,
                    order="created_asc",
                )
                for record in records:
                    transaction.delete_task(record.task_id)
                if len(records) < MAX_LIST_LIMIT:
                    break
        registry.forget_cancellation_events()


# 生产进程只创建这一组持久任务后端。测试通过 AUTOSLICE_TASK_DB 在导入前隔离。
TASK_DATABASE_PATH = _task_database_path()
task_store = TaskStore(TASK_DATABASE_PATH, corruption_policy="quarantine")
task_registry = TaskRegistry(
    task_store,
    history_ttl_seconds=_TASK_HISTORY_TTL_SEC,
    history_limit=_TASK_HISTORY_LIMIT,
)
tasks = _TaskMappingView()

if task_store.last_recovery is not None:
    app.logger.warning(
        "任务数据库损坏，已隔离原文件并重建；恢复审计已写入任务状态目录。"
    )


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


def _legacy_direct_slice_enabled(environ=None):
    """旧版直切默认关闭，仅在明确设置环境变量时开放。"""
    return _env_flag(LEGACY_DIRECT_SLICE_ENV, environ)


@app.context_processor
def _template_feature_flags():
    return {
        "legacy_direct_slice_enabled": _legacy_direct_slice_enabled(),
    }


@app.before_request
def enforce_local_request_boundary():
    """统一阻止 DNS rebinding、跨站写入和 LAN 路径逃逸。"""

    decision = security_policy.authorize_flask_request(request)
    if not decision.allowed:
        return jsonify({"error": decision.message}), decision.status_code
    path_decision = security_policy.validate_flask_request_paths(request)
    if not path_decision.allowed:
        return jsonify({"error": path_decision.message}), path_decision.status_code
    return None


@app.after_request
def issue_local_browser_session(response):
    """页面或显式 bootstrap GET 仅通过 HttpOnly Cookie 建立本机会话。"""

    if (
            request.method == "GET"
            and request.path in _SESSION_BOOTSTRAP_PATHS
            and response.status_code < 400):
        security_policy.attach_session_cookie(
            response,
            scheme=request.scheme,
            host_header=request.host,
            secure=request.is_secure,
        )
    return response


def _redact_task_error_text(value):
    """脱敏可能进入任务历史或 SSE 的错误文本。"""

    message = " ".join(str(value).split())
    if "traceback" in message.casefold():
        return "后台处理失败（详细信息仅写入服务日志）"
    message = re.sub(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|"
        r"authorization|cookie|password|client[_ -]?secret)\s*[:=]\s*[^\s,;]+",
        "[已隐藏]",
        message,
    )
    message = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [已隐藏]", message)
    message = re.sub(r"(?i)\bsk-[a-z0-9._-]{4,}", "[已隐藏]", message)
    message = _WINDOWS_PATH_RE.sub("[本地路径已隐藏]", message)
    message = _POSIX_PRIVATE_PATH_RE.sub("[本地路径已隐藏]", message)
    return message or "后台处理失败"


def _is_sse_path_key(normalized_key):
    """判断 SSE 字段是否表示本机文件系统路径。"""

    return normalized_key.endswith(_SSE_PATH_KEY_SUFFIXES)


def _sse_path_display(value):
    """只保留路径末段，避免 SSE 把本机目录树发送给浏览器。"""

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return stripped
        # 非 Windows 运行器上的 ``os.path.basename`` 不识别反斜杠；
        # 任务结果可能来自 Windows 用户配置，因此显式兼容两种分隔符。
        parts = re.split(r"[\\/]", stripped.rstrip("\\/"))
        return parts[-1] if parts and parts[-1] else "[本地路径已隐藏]"
    if isinstance(value, (list, tuple)):
        return [_sse_path_display(item) for item in value]
    return value


def _sanitize_sse_value(value, *, key="", task_status=""):
    normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    if any(marker in normalized_key for marker in (
            "apikey", "accesstoken", "refreshtoken", "authorization",
            "cookie", "password", "privatekey", "clientsecret",
            "traceback")) or normalized_key == "token":
        return "[已隐藏]"
    if _is_sse_path_key(normalized_key):
        if isinstance(value, dict):
            return {
                str(item_key): _sanitize_sse_value(
                    item,
                    key=item_key,
                    task_status=task_status,
                )
                for item_key, item in value.items()
            }
        return _sse_path_display(value)
    if isinstance(value, dict):
        nested_status = str(value.get("status") or task_status)
        return {
            str(item_key): _sanitize_sse_value(
                item,
                key=item_key,
                task_status=nested_status,
            )
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_sse_value(item, key=key, task_status=task_status)
            for item in value
        ]
    if isinstance(value, str) and (
            normalized_key in {"error", "errorsummary"}
            or (normalized_key == "result" and task_status == "error")):
        return _redact_task_error_text(value)
    if isinstance(value, str) and normalized_key == "result":
        # 旧 SSE 契约把完成结果作为 JSON 字符串发送。先解析再按字段脱敏，
        # 这样仍保留字符串契约，同时不会把 payload 内的绝对路径原样发出。
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return _redact_task_error_text(value)
        sanitized = _sanitize_sse_value(
            parsed,
            key="result_payload",
            task_status=task_status,
        )
        return json.dumps(
            sanitized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    if isinstance(value, str) and (
            _WINDOWS_PATH_RE.search(value)
            or _POSIX_PRIVATE_PATH_RE.search(value)
    ):
        return _redact_task_error_text(value)
    return value


def _format_sse_event(event_id, event_type, data):
    return (
        f"id: {event_id}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    )


def _prune_event_history_locked(now=None):
    now = time.time() if now is None else float(now)
    while _event_history and (
            now - _event_history[0][1] > _SSE_EVENT_HISTORY_TTL_SEC):
        _event_history.popleft()
    while len(_event_history) > _SSE_EVENT_HISTORY_LIMIT:
        _event_history.popleft()


def broadcast(event_type, data):
    """发布带递增 ID 的有限 SSE 事件，并剔除已积压的订阅者。"""

    global _event_sequence
    safe_data = _sanitize_sse_value(data)
    now = time.time()
    with event_queue_lock:
        _event_sequence += 1
        event_id = _event_sequence
        message = _format_sse_event(event_id, event_type, safe_data)
        _event_history.append((event_id, now, message))
        _prune_event_history_locked(now)
        subscribers = tuple(event_queues)

    dead = []
    for subscriber in subscribers:
        try:
            subscriber.put_nowait(message)
        except queue.Full:
            dead.append(subscriber)
    if dead:
        with event_queue_lock:
            for subscriber in dead:
                if subscriber in event_queues:
                    event_queues.remove(subscriber)
    return event_id


def _prune_tasks_locked(now=None):
    """通过 TaskRegistry 清理终态历史；活动任务永不被删除。"""

    current_time = time.time() if now is None else float(now)
    return task_registry.cleanup_history(
        ttl_seconds=_TASK_HISTORY_TTL_SEC,
        keep_latest=_TASK_HISTORY_LIMIT,
        now=current_time,
    )


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
    """旧任务更新 façade：把字典参数翻译为显式 TaskRegistry 生命周期。"""

    requested = dict(kwargs)
    requested_status = requested.pop("status", None)
    progress = requested.pop("progress", None)
    message = requested.pop("message", None)
    step = requested.pop("step", None)
    total = requested.pop("total", None)
    result = normalize_task_result(
        requested.pop("result", requested.pop("result_summary", None))
    )
    error = requested.pop("error", requested.pop("error_summary", None))
    task_type = requested.pop("task_type", "legacy")
    requested.pop("updated_at", None)
    requested.pop("completed_at", None)
    requested.pop("finished_at", None)
    metadata = requested.pop("metadata", None)
    if metadata is None:
        metadata = {}
    elif not isinstance(metadata, dict):
        raise TypeError("metadata 必须是字典")
    else:
        metadata = dict(metadata)
    metadata.update(requested)

    if requested_status == "done":
        if isinstance(result, dict):
            for key in ("artifact_dir", "output_dir"):
                if result.get(key):
                    metadata[key] = result[key]

    current = task_registry.get(task_id)
    if current is None:
        # 大量旧测试直接调用 update_task；先创建 queued，再走同一生命周期。
        task_registry.store.create_task(
            task_id,
            task_type,
            status="queued",
            progress="等待处理",
            step=0,
            total=total if total is not None else 100,
        )
        current = task_registry.get(task_id)

    if current.status == "cancelled":
        return tasks[task_id]

    target_status = requested_status
    if target_status is None and current.status == "queued":
        target_status = "running"

    if target_status == "running":
        if current.status == "queued":
            task_registry.mark_running(
                task_id,
                progress=progress,
                message=message,
                metadata=metadata or None,
            )
            current = task_registry.get(task_id)
            progress = None
            message = None
            metadata = {}
        if any(value is not None for value in (progress, message, step, total)) or metadata:
            task_registry.update_progress(
                task_id,
                progress=progress,
                message=message,
                step=step,
                total=total,
                metadata=metadata or None,
            )
    elif target_status == "done":
        if current.status == "done":
            return tasks[task_id]
        if current.status == "queued":
            task_registry.mark_running(task_id)
            current = task_registry.get(task_id)
        if total is not None and total != current.total:
            task_registry.update_progress(task_id, total=total)
        task_registry.complete(
            task_id,
            result,
            progress=progress or "完成",
            message=message,
            metadata=metadata or None,
        )
    elif target_status == "error":
        if current.status == "error":
            return tasks[task_id]
        if step is not None or total is not None:
            task_registry.update_progress(task_id, step=step, total=total)
        task_registry.fail(
            task_id,
            error if error is not None else result or progress or "任务失败",
            progress=progress or "失败",
            message=message,
            metadata=metadata or None,
        )
    elif target_status == "cancelled":
        task_registry.cancel(
            task_id,
            reason=message or str(error or result or "用户已取消任务"),
            metadata=metadata or None,
        )
    elif target_status in {None, "queued"}:
        task_registry.update_progress(
            task_id,
            progress=progress,
            message=message,
            step=step,
            total=total,
            metadata=metadata or None,
        )
    else:
        raise ValueError(f"不支持的任务状态：{target_status}")

    _prune_tasks_locked()
    snapshot = tasks[task_id]

    # 控制台同步输出（不用 \r，直接打印）
    status = snapshot.get("status", "")
    current_progress = snapshot.get("progress", "")
    pct = snapshot.get("step", 0)
    if current_progress:
        _console_print(f"  [{task_id[:40]}] [{pct}%] {current_progress}")
    if status in ("done", "error"):
        _console_print(f"  [{task_id[:40]}] >>> {status}: {snapshot.get('result', '')}")

    # SSE 广播
    broadcast("task_update", {"task_id": task_id, **snapshot})
    return snapshot


def _pipeline_completion_progress(result):
    """生成流水线完成提示，区分报告话题数和实际切片数。"""
    clip_marks = result.get("clip_marks") or []
    topic_count = result.get("topic_count", len(clip_marks))
    return f"完成! {topic_count} 个话题, {result.get('slice_count', 0)} 个切片"


def _complete_pipeline_task(task_id, result):
    """以紧凑摘要完成流水线任务，失败时再用最小摘要重试。"""

    summary = build_pipeline_result_summary(result)
    progress = _pipeline_completion_progress(result)
    try:
        return update_task(
            task_id,
            status="done",
            progress=progress,
            result=summary,
            step=100,
            total=100,
        )
    except Exception as exc:
        app.logger.error(
            "流水线产物已完成，但保存任务完成摘要失败；正在使用最小摘要重试",
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    minimal_summary = {
        key: value
        for key, value in summary.items()
        if key not in {
            "api_precheck_warning",
            "clip_review_warning",
            "unified_queue_warning",
        }
    }
    try:
        return update_task(
            task_id,
            status="done",
            progress=progress,
            result=minimal_summary,
            step=100,
            total=100,
        )
    except Exception as exc:
        app.logger.error(
            "流水线产物已完成，但任务完成状态仍无法持久化；不会误报为业务失败",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        try:
            return update_task(
                task_id,
                status="running",
                progress="分析和切片产物已完成，但任务状态保存失败；结果文件已保留",
                step=100,
                total=100,
                metadata={"completion_persistence_failed": True},
            )
        except Exception:
            app.logger.error("任务完成状态失败提示也无法持久化", exc_info=True)
            return None


def _reserve_task(
        prefix, task_type, waiting_progress, *, source_paths=(),
        output_paths=(), conflict_types=None, metadata=None,
        streamer_profile=None):
    """统一调用 TaskRegistry.reserve 预约后台任务。"""

    _prune_tasks_locked()
    task_id, active_task_id = task_registry.reserve(
        task_type,
        waiting_progress,
        prefix=prefix,
        source_paths=source_paths,
        output_paths=output_paths,
        conflict_types=conflict_types,
        metadata=metadata,
        streamer_profile=streamer_profile,
        total=100,
    )
    if task_id is not None:
        broadcast("task_update", {"task_id": task_id, **tasks[task_id]})
    return task_id, active_task_id


def _reserve_source_task(
        prefix, task_type, source_path, waiting_progress, conflict_types=None,
        source_paths=(), output_paths=(), metadata=None, streamer_profile=None):
    """兼容单源任务调用，实际统一走资源预约器。"""
    return _reserve_task(
        prefix,
        task_type,
        waiting_progress,
        source_paths=(source_path, *tuple(source_paths or ())),
        output_paths=output_paths,
        conflict_types=conflict_types,
        metadata=metadata,
        streamer_profile=streamer_profile,
    )


def _set_task_output_dir(task_id, output_dir):
    """记录任务实际输出根目录，供完成后安全打开整理包。"""
    absolute_output_dir = os.path.abspath(output_dir)
    task_registry.update_progress(
        task_id,
        metadata={"output_dir": absolute_output_dir},
    )
    return absolute_output_dir


def _request_streamer_profile(data, video_path):
    """在请求线程解析并冻结主播配置，避免后台线程上下文丢失。"""
    profile_id = str(
        (data or {}).get("streamer_profile_id") or "auto"
    ).strip().casefold()
    return resolve_streamer_profile(profile_id, video_path)


def _subtitle_review_rules(streamer_profile, extra_glossary=None):
    """统一计算请求响应与后台实际使用的字幕校对规则。"""

    from autoslice.subtitle_workflow import subtitle_review_profile_rules

    return subtitle_review_profile_rules(streamer_profile, extra_glossary)


def _topic_task_output_paths(flv_path, output_dir):
    base_name = os.path.splitext(os.path.basename(flv_path))[0]
    return (
        os.path.join(output_dir, base_name + "_自动切片"),
        os.path.join(output_dir, base_name + "_话题切片"),
    )


def _completed_task_artifact_dir(task_id):
    """解析并校验已完成任务的整理包目录，拒绝任意路径打开。"""
    task = task_registry.snapshot(task_id)
    if not task:
        raise KeyError("任务不存在")
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
    if not isinstance(result, dict):
        result = {}
    artifact_path = result.get("artifact_dir") or task.get("artifact_dir")
    if not artifact_path:
        raise ValueError("任务结果中没有整理包路径")

    output_dir = task.get("output_dir")
    if not output_dir:
        raise PermissionError("任务没有记录输出目录，不能安全打开")
    output_root = Path(output_dir).expanduser().resolve(strict=True)
    artifact_dir = Path(artifact_path).expanduser().resolve(strict=True)
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


def _completed_task_report_path(task_id):
    """返回已完成自动切片任务的报告文件，并限制在整理包内部。"""

    artifact_dir = _completed_task_artifact_dir(task_id)
    task = task_registry.snapshot(task_id) or {}
    result = normalize_task_result(task.get("result"))
    report_value = result.get("md_path") if isinstance(result, dict) else None
    report_path = Path(report_value or artifact_dir / "01_话题分析.md").resolve(
        strict=True
    )
    if not report_path.is_file() or report_path.suffix.lower() != ".md":
        raise FileNotFoundError("完整话题分析报告不存在")
    try:
        report_path.relative_to(artifact_dir)
    except ValueError as exc:
        raise PermissionError("报告路径超出任务整理包") from exc
    return report_path


def _safe_task_error(error):
    """生成可发给前端的单行错误，不包含凭据、路径或堆栈。"""
    message = _redact_task_error_text(error)
    return f"{type(error).__name__}: {message}"[:500]


class _TaskCancelled(RuntimeError):
    """仅用于协作退出后台线程，不写入 error 状态。"""


def _task_cancellation_requested(task_id):
    try:
        return task_registry.cancellation_requested(task_id)
    except TaskNotFoundError:
        return False


def _raise_if_task_cancelled(task_id):
    if _task_cancellation_requested(task_id):
        raise _TaskCancelled("任务已取消")


def _record_task_error(task_id, progress, error, *, total=100):
    """堆栈仅写服务日志，SSE 和任务结果只保存脱敏摘要。"""
    if _task_cancellation_requested(task_id):
        return tasks.get(task_id)
    stack = "".join(traceback.format_tb(error.__traceback__))
    app.logger.error("%s\n%s%s", progress, stack, _safe_task_error(error))
    return update_task(
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


def _reserve_subtitle_review_task(srt_path, force, streamer_profile=None):
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
        streamer_profile=streamer_profile,
    )


def _reserve_subtitle_title_task(srt_path, streamer_profile=None):
    """同一份校对字幕同时只生成一组参考标题。"""
    return _reserve_task(
        "subtitle_title",
        "subtitle_title",
        "参考标题等待生成...",
        source_paths=(srt_path,),
        metadata={
            "source_srt_path": os.path.abspath(srt_path),
        },
        streamer_profile=streamer_profile,
    )


def _validate_subtitle_video(video_path):
    if not video_path or not os.path.isfile(video_path):
        raise ValueError("投稿视频文件不存在")
    if not is_analyzable_video(video_path):
        supported = "、".join(SUPPORTED_VIDEO_EXTENSIONS)
        raise ValueError(f"投稿视频格式不受支持，字幕工作台支持：{supported}")
    return os.path.abspath(video_path)


def _reserve_subtitle_transcription_task(video_path, foreground_only=True):
    """同一成片同时只允许一个转录任务，并预约同名 SRT 输出。"""
    srt_path = os.path.splitext(video_path)[0] + ".srt"
    pair_key = os.path.normcase(str(Path(video_path).expanduser().resolve()))
    pair_id = hashlib.sha256(pair_key.encode("utf-8")).hexdigest()[:16]
    return _reserve_task(
        "subtitle_transcribe",
        "subtitle_transcription",
        "字幕识别等待启动...",
        source_paths=(video_path,),
        output_paths=(srt_path,),
        conflict_types={"subtitle_transcription", "subtitle_render"},
        metadata={
            "source_video_path": os.path.abspath(video_path),
            "output_srt_path": os.path.abspath(srt_path),
            "subtitle_pair_id": pair_id,
            "foreground_only": bool(foreground_only),
        },
    )


def _validate_subtitle_path(srt_path):
    if not srt_path or not os.path.isfile(srt_path):
        raise ValueError("SRT 字幕文件不存在")
    if os.path.splitext(srt_path)[1].lower() != ".srt":
        raise ValueError("字幕文件必须是 SRT")
    return os.path.abspath(srt_path)


def _validate_subtitle_pair(video_path, srt_path):
    video_path = _validate_subtitle_video(video_path)
    srt_path = _validate_subtitle_path(srt_path)
    try:
        same_directory = os.path.samefile(
            os.path.dirname(video_path),
            os.path.dirname(srt_path),
        )
    except OSError:
        same_directory = (
            os.path.normcase(os.path.realpath(os.path.dirname(video_path)))
            == os.path.normcase(os.path.realpath(os.path.dirname(srt_path)))
        )
    if not same_directory:
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
        task_id, srt_path, context_title, streamer_profile,
        glossary=None, force=False):
    """后台生成字幕错字建议，不直接改文件。"""
    if _task_cancellation_requested(task_id):
        return
    update_task(
        task_id,
        status="running",
        progress="准备检查字幕错别字...",
        step=0,
        total=100,
    )

    def callback(msg, step, total):
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from autoslice.subtitle_workflow import (
            high_confidence_corrections,
            suggest_subtitle_corrections,
        )

        active_glossary, active_replacements = _subtitle_review_rules(
            streamer_profile,
            glossary,
        )

        _raise_if_task_cancelled(task_id)
        result = suggest_subtitle_corrections(
            srt_path,
            context_title=context_title,
            glossary=glossary,
            streamer_profile=streamer_profile,
            use_cache=not force,
            progress_callback=callback,
        )
        result.setdefault("streamer_profile_id", streamer_profile.id)
        result.setdefault("streamer_profile_label", streamer_profile.label)
        result.setdefault("glossary_count", len(active_glossary))
        result.setdefault("replacement_count", len(active_replacements))
        result["default_corrections"] = high_confidence_corrections(result)
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="done",
            progress=f"字幕检查完成，发现 {len(result['suggestions'])} 条建议",
            result=result,
            step=100,
            total=100,
        )
    except Exception as exc:
        _record_task_error(task_id, "字幕检查失败", exc)


def run_subtitle_title_task(
        task_id, srt_path, context_title, streamer_profile):
    """后台根据已保存的校对字幕生成参考投稿标题。"""
    if _task_cancellation_requested(task_id):
        return
    update_task(
        task_id,
        status="running",
        progress="准备理解整段字幕...",
        step=0,
        total=100,
    )

    def callback(msg, step, total):
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from autoslice.subtitle_workflow import generate_subtitle_reference_titles
        from autoslice.topic_engine import subtitle_title_services

        with streamer_profile_context(streamer_profile):
            _raise_if_task_cancelled(task_id)
            result = generate_subtitle_reference_titles(
                srt_path,
                context_title=context_title,
                progress_callback=callback,
                title_services=subtitle_title_services(),
            )
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="done",
            progress="参考标题生成完成",
            result=result,
            step=100,
            total=100,
        )
    except Exception as exc:
        _record_task_error(task_id, "参考标题生成失败", exc)


def run_subtitle_transcription_task(task_id, video_path, foreground_only=True):
    """后台为精剪成片生成同名 SRT，完成后可直接进入字幕校对。"""
    if _task_cancellation_requested(task_id):
        return
    update_task(
        task_id,
        status="running",
        progress="准备自动识别字幕...",
        step=0,
        total=100,
    )

    def callback(msg, step, total):
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from autoslice.subtitle_workflow import transcribe_submission_video
        from autoslice.topic_engine import ensure_srt

        _raise_if_task_cancelled(task_id)
        result = transcribe_submission_video(
            video_path,
            progress_callback=callback,
            foreground_only=foreground_only,
            transcription_service=ensure_srt,
        )
        filter_result = result.get("background_filter") or {}
        filter_mode = filter_result.get("mode")
        if filter_mode == "speaker_diarization":
            filtered_count = int(
                filter_result.get("speaker_filtered_segment_count") or 0
            )
            filter_note = f"，主要说话人过滤 {filtered_count} 段背景对白"
        elif filter_mode == "adaptive_gate":
            filter_note = "，已应用基础背景音门限"
        else:
            filter_note = ""
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="done",
            progress=f"字幕识别完成，共 {result['cue_count']} 条{filter_note}",
            result=result,
            step=100,
            total=100,
        )
    except Exception as exc:
        _record_task_error(task_id, "字幕识别失败", exc)


def run_subtitle_render_task(
        task_id, video_path, srt_path, style, export_settings,
        output_path=None):
    """后台把确认后的字幕压制进新视频。"""
    if _task_cancellation_requested(task_id):
        return
    update_task(
        task_id,
        status="running",
        progress="准备字幕样式和编码器...",
        step=0,
        total=100,
    )

    def callback(msg, step, total):
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from autoslice.subtitle_workflow import burn_subtitles

        _raise_if_task_cancelled(task_id)
        result = burn_subtitles(
            video_path,
            srt_path,
            style=style,
            export_settings=export_settings,
            output_path=output_path,
            progress_callback=callback,
        )
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="done",
            progress="字幕版视频压制完成",
            result=result,
            step=100,
            total=100,
        )
    except Exception as exc:
        _record_task_error(task_id, "字幕版视频压制失败", exc)


def run_timeline_optimization_task(
        task_id, flv_path, manual_timeline_path, ass_path=None, output_dir=None,
        streamer_profile="auto"):
    """后台仅优化人工时间轴，不启动话题分析和切片。"""
    if _task_cancellation_requested(task_id):
        return
    update_task(task_id, status="running", progress="准备校准人工时间轴...", step=0, total=100)

    def callback(msg, step, total):
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from autoslice.topic_engine import optimize_manual_timeline_for_video

        _raise_if_task_cancelled(task_id)
        result = optimize_manual_timeline_for_video(
            flv_path,
            manual_timeline_path,
            ass_path=ass_path if ass_path and os.path.isfile(ass_path) else None,
            progress_callback=callback,
            output_dir=output_dir,
            streamer_profile_id=streamer_profile,
        )
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="done",
            progress="人工时间轴优化完成",
            result=result,
            step=100,
            total=100,
        )
    except Exception as exc:
        _record_task_error(task_id, "人工时间轴优化失败", exc)


def run_clip_review_retry_task(
        task_id, flv_path, ass_path=None, output_dir=None,
        streamer_profile="auto"):
    """复用现有话题产物，只重做候选复核并刷新实际切片。"""
    if _task_cancellation_requested(task_id):
        return
    update_task(
        task_id,
        status="running",
        progress="准备复用现有话题报告...",
        step=0,
        total=100,
    )

    def callback(msg, step, total):
        _raise_if_task_cancelled(task_id)
        update_task(
            task_id,
            status="running",
            progress=msg,
            step=step,
            total=total,
        )

    try:
        from autoslice.topic_engine import retry_clip_review_from_artifacts, slice_from_marks

        _raise_if_task_cancelled(task_id)
        result = retry_clip_review_from_artifacts(
            flv_path,
            ass_path=ass_path if ass_path and os.path.isfile(ass_path) else None,
            progress_callback=callback,
            output_dir=output_dir,
            streamer_profile_id=streamer_profile,
        )
        clip_marks = result.get("clip_marks") or []
        if clip_marks:
            _raise_if_task_cancelled(task_id)
            count, out_dir = slice_from_marks(
                flv_path,
                result["json_path"],
                output_dir,
                progress_callback=callback,
                streamer_profile_id=streamer_profile,
            )
            result["slice_count"] = count
            result["slice_dir"] = out_dir
        _raise_if_task_cancelled(task_id)
    except Exception as exc:
        _record_task_error(task_id, "候选复核失败", exc)
        return
    _complete_pipeline_task(task_id, result)


def run_slice_task(
        task_id, flv_path, output_dir, timeline_json,
        streamer_profile="auto"):
    """使用智能分析 JSON 标记执行唯一受支持的后台重切任务。"""
    if _task_cancellation_requested(task_id):
        return
    update_task(task_id, status="running", progress="准备中...", step=0)

    def callback(msg, step, total):
        _raise_if_task_cancelled(task_id)
        update_task(task_id, status="running", progress=msg, step=step, total=total)

    try:
        with streamer_profile_context(streamer_profile, flv_path):
            from autoslice.topic_engine import slice_from_marks
            _raise_if_task_cancelled(task_id)
            count, out_dir = slice_from_marks(
                flv_path,
                timeline_json,
                output_dir,
                progress_callback=callback,
                streamer_profile_id=streamer_profile,
            )
        _raise_if_task_cancelled(task_id)
        update_task(task_id, status="done",
                    progress=f"完成！{count} 个片段",
                    result=f"共切出 {count} 个片段 → {out_dir}", step=100)
    except Exception as exc:
        _record_task_error(task_id, "高级重新切片失败", exc)


# ==================== SSE 端点 ====================


def _task_init_snapshot():
    _prune_tasks_locked()
    if _TASK_HISTORY_LIMIT <= 0:
        return {}
    snapshot = task_registry.snapshot(
        limit=min(_TASK_HISTORY_LIMIT, MAX_LIST_LIMIT),
        order="updated_desc",
    )
    return _sanitize_sse_value(snapshot)


def _replay_events_locked(raw_last_event_id):
    """返回缺失事件；``None`` 表示 ID 无效或已过期，应回退 init。"""

    if raw_last_event_id is None:
        return None
    try:
        last_event_id = int(str(raw_last_event_id).strip())
    except (TypeError, ValueError):
        return None
    if last_event_id < 0 or last_event_id > _event_sequence:
        return None
    if last_event_id == _event_sequence:
        return []
    if not _event_history:
        return None
    oldest_id = _event_history[0][0]
    if last_event_id < oldest_id - 1:
        return None
    return [
        message
        for event_id, _created_at, message in _event_history
        if event_id > last_event_id
    ]


@app.route("/api/events")
def sse_events():
    """有限快照 + Last-Event-ID 重放的可恢复 SSE 事件流。"""

    subscriber = queue.Queue(maxsize=max(1, _SSE_SUBSCRIBER_QUEUE_SIZE))
    with event_queue_lock:
        _prune_event_history_locked()
        replay = _replay_events_locked(request.headers.get("Last-Event-ID"))
        event_queues.append(subscriber)
        if replay is None:
            current = _task_init_snapshot()
            initial_messages = [
                "event: init\n"
                f"data: {json.dumps(current, ensure_ascii=False)}\n\n"
            ]
        else:
            initial_messages = replay or [": connected\n\n"]

    def generate():
        try:
            yield from initial_messages
            while True:
                with event_queue_lock:
                    if subscriber not in event_queues:
                        break
                try:
                    msg = subscriber.get(timeout=15)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with event_queue_lock:
                if subscriber in event_queues:
                    event_queues.remove(subscriber)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def cancel_task(task_id):
    """协作取消活动任务；重复取消幂等，其他终态明确返回 409。"""

    current = task_registry.get(task_id)
    if current is None:
        return jsonify({"error": "任务不存在"}), 404
    if current.status == "cancelled":
        snapshot = tasks[task_id]
        return jsonify({"task_id": task_id, "task": snapshot})
    if current.status not in ACTIVE_TASK_STATUSES:
        return jsonify({
            "error": f"任务已处于终态 {current.status}，不能取消",
            "task_id": task_id,
            "status": current.status,
        }), 409
    try:
        task_registry.cancel(task_id)
    except TaskLifecycleError as exc:
        return jsonify({"error": str(exc), "task_id": task_id}), 409
    snapshot = tasks[task_id]
    broadcast("task_update", {"task_id": task_id, **snapshot})
    return jsonify({"task_id": task_id, "task": snapshot})


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
    if not _legacy_direct_slice_enabled():
        abort(404)
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
    candidates = (
        os.path.join(video_dir, name)
        for name in os.listdir(video_dir)
        if is_scannable_video(name)
    )
    for f in sorted(set(candidates)):
        if not os.path.isfile(f):
            continue
        name = os.path.basename(f)
        if name.startswith("[正在录制]") or name.startswith("[录制中]"):
            continue
        base = os.path.splitext(f)[0]
        has_ass = os.path.exists(base + ".ass")
        has_srt = os.path.exists(base + ".srt") and os.path.getsize(base + ".srt") > 0
        videos.append({"name": name, "path": f, "has_ass": has_ass, "has_srt": has_srt})

    return jsonify({"videos": videos, "count": len(videos)})


@app.route("/api/slice", methods=["POST"])
def slice_start():
    if not _legacy_direct_slice_enabled():
        return jsonify({"error": "旧版高级重新切片功能未启用"}), 404
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "")
    if mode != "timeline-json":
        return jsonify({
            "error": (
                "旧版弹幕、DOCX 时间轴和混合直切模式已退役；"
                "请先运行智能分析生成 clip_marks.json，再使用 timeline-json 模式重新切片"
            )
        }), 400

    flv_path = data.get("flv_path", "")
    output_dir = os.path.abspath(data.get("output_dir") or DEFAULT_OUTPUT_DIR)
    timeline_json = data.get("timeline_json", "")

    if not os.path.isfile(flv_path):
        return jsonify({"error": "视频文件不存在"}), 400
    if not os.path.isfile(timeline_json):
        return jsonify({"error": "JSON 标记文件不存在"}), 400
    try:
        streamer_profile = _request_streamer_profile(data, flv_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    base_name = os.path.splitext(os.path.basename(flv_path))[0]
    direct_output = os.path.join(output_dir, base_name + "_话题切片")
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
        source_paths=(timeline_json,),
        output_paths=(direct_output,),
        metadata={
            "streamer_profile_id": streamer_profile.id,
            "mode": mode,
        },
        streamer_profile=streamer_profile,
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
                output_dir,
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


@app.route("/api/tasks/<task_id>/report", methods=["GET"])
def completed_task_report(task_id):
    """按任务读取整理包内的 Markdown 报告，不把完整报告复制进任务表。"""

    try:
        report_path = _completed_task_report_path(task_id)
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
        content = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        return jsonify({"error": f"无法读取完整话题分析报告: {exc}"}), 500
    return Response(content, mimetype="text/markdown")


@app.route("/api/tasks/<task_id>/result", methods=["GET"])
def completed_task_result(task_id):
    """按任务 ID 返回完成结果；绝对路径只在显式本机请求中提供。"""

    task = task_registry.snapshot(task_id)
    if task is None:
        return jsonify({"error": "任务不存在"}), 404
    if task.get("status") != "done":
        return jsonify({"error": "任务尚未完成"}), 409
    result = normalize_task_result(task.get("result"))
    if result is None:
        return jsonify({})
    if isinstance(result, dict):
        return jsonify(result)
    return jsonify({"result": result})


# ==================== 字幕校对与压制 ====================

@app.route("/api/subtitles/defaults", methods=["GET"])
def subtitle_defaults():
    from autoslice.subtitle_workflow import (
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
    from autoslice.subtitle_workflow import scan_submission_pairs

    data = request.get_json(silent=True) or {}
    root_dir = data.get("root_dir") or DEFAULT_SUBMISSION_DIR
    try:
        pairs = scan_submission_pairs(root_dir)
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"root_dir": os.path.abspath(root_dir), "pairs": pairs, "count": len(pairs)})


@app.route("/api/subtitles/cues", methods=["POST"])
def subtitle_cues():
    from autoslice.subtitle_workflow import parse_srt_document

    data = request.get_json(silent=True) or {}
    try:
        srt_path = _validate_subtitle_path(data.get("srt_path", ""))
        cues = [cue.to_dict() for cue in parse_srt_document(srt_path)]
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"srt_path": srt_path, "cues": cues, "count": len(cues)})


@app.route("/api/subtitles/edit-state", methods=["POST"])
def subtitle_edit_state():
    from autoslice.subtitle_workflow import load_subtitle_edit_state

    data = request.get_json(silent=True) or {}
    try:
        srt_path = _validate_subtitle_path(data.get("srt_path", ""))
        edit_state = load_subtitle_edit_state(srt_path)
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "srt_path": srt_path,
        "available": edit_state is not None,
        "edit_state": edit_state,
    })


@app.route("/api/subtitles/transcribe", methods=["POST"])
def subtitle_transcribe():
    data = request.get_json(silent=True) or {}
    try:
        video_path = _validate_subtitle_video(data.get("video_path", ""))
        foreground_only = data.get("foreground_only", True)
        if not isinstance(foreground_only, bool):
            raise ValueError("foreground_only 必须是布尔值")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    task_id, active_task_id = _reserve_subtitle_transcription_task(
        video_path,
        foreground_only,
    )
    if active_task_id:
        return jsonify({
            "error": "该视频正在识别字幕，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    try:
        threading.Thread(
            target=run_subtitle_transcription_task,
            args=(task_id, video_path, foreground_only),
            daemon=True,
        ).start()
    except Exception as exc:
        _record_task_error(task_id, "字幕识别启动失败", exc)
        return jsonify({"error": _safe_task_error(exc), "task_id": task_id}), 500
    return jsonify({"task_id": task_id})


@app.route("/api/subtitles/reflow", methods=["POST"])
def subtitle_reflow():
    """为旧版过长 SRT 生成排版副本，不改写源字幕。"""
    from autoslice.subtitle_workflow import reflow_subtitle_srt_for_display

    data = request.get_json(silent=True) or {}
    try:
        video_path, srt_path = _validate_subtitle_pair(
            data.get("video_path", ""),
            data.get("srt_path", ""),
        )
        result = reflow_subtitle_srt_for_display(srt_path)
        try:
            result_matches_source = os.path.samefile(
                result["source_srt_path"],
                srt_path,
            )
        except OSError:
            result_matches_source = (
                os.path.normcase(os.path.realpath(result["source_srt_path"]))
                == os.path.normcase(os.path.realpath(srt_path))
            )
        if not result_matches_source:
            raise ValueError("整理结果不属于当前源字幕")
        result["video_path"] = video_path
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


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
    try:
        profile_id = str(data.get("streamer_profile_id") or "auto").strip().casefold()
        streamer_profile = resolve_streamer_profile(
            profile_id,
            video_path,
            context_hint=context_title,
        )
        review_glossary, review_replacements = _subtitle_review_rules(
            streamer_profile,
            glossary,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    task_id, active_task_id = _reserve_subtitle_review_task(
        srt_path,
        force,
        streamer_profile,
    )
    if active_task_id:
        return jsonify({
            "error": "该字幕正在检查，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    try:
        threading.Thread(
            target=run_subtitle_review_task,
            args=(
                task_id,
                srt_path,
                context_title,
                streamer_profile,
                glossary,
                force,
            ),
            daemon=True,
        ).start()
    except Exception as exc:
        _record_task_error(task_id, "字幕检查启动失败", exc)
        return jsonify({
            "error": f"字幕检查启动失败: {_safe_task_error(exc)}",
            "task_id": task_id,
        }), 500
    return jsonify({
        "task_id": task_id,
        "review_profile": {
            "id": streamer_profile.id,
            "label": streamer_profile.label,
            "glossary_count": len(review_glossary),
            "replacement_count": len(review_replacements),
        },
    })


@app.route("/api/subtitles/generate-title", methods=["POST"])
def subtitle_generate_title():
    """根据当前已保存字幕生成参考标题，不改视频名或字幕文件。"""
    data = request.get_json(silent=True) or {}
    try:
        video_path, srt_path = _validate_subtitle_pair(
            data.get("video_path", ""),
            data.get("srt_path", ""),
        )
        streamer_profile = _request_streamer_profile(data, video_path)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    context_title = str(
        data.get("context_title") or os.path.basename(os.path.dirname(video_path))
    ).strip()
    if not context_title:
        context_title = Path(video_path).stem
    if len(context_title) > 300:
        return jsonify({"error": "视频标题过长"}), 400

    task_id, active_task_id = _reserve_subtitle_title_task(
        srt_path,
        streamer_profile,
    )
    if active_task_id:
        return jsonify({
            "error": "该字幕正在生成参考标题，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    try:
        threading.Thread(
            target=run_subtitle_title_task,
            args=(task_id, srt_path, context_title, streamer_profile),
            daemon=True,
        ).start()
    except Exception as exc:
        _record_task_error(task_id, "参考标题任务启动失败", exc)
        return jsonify({
            "error": f"参考标题任务启动失败: {_safe_task_error(exc)}",
            "task_id": task_id,
        }), 500
    return jsonify({"task_id": task_id})


@app.route("/api/subtitles/save", methods=["POST"])
def subtitle_save():
    from autoslice.subtitle_workflow import parse_srt_document, save_corrected_srt

    data = request.get_json(silent=True) or {}
    corrections = data.get("corrections", [])
    deleted_indices = data.get("deleted_indices", [])
    merge_pairs = data.get("merge_pairs", [])
    merge_overrides = data.get("merge_overrides", {})
    time_overrides = data.get("time_overrides", {})
    if not isinstance(corrections, list):
        return jsonify({"error": "字幕修正必须是数组"}), 400
    if not isinstance(deleted_indices, list):
        return jsonify({"error": "删除字幕序号必须是数组"}), 400
    if not isinstance(merge_pairs, list):
        return jsonify({"error": "字幕合并关系必须是数组"}), 400
    if not isinstance(merge_overrides, dict):
        return jsonify({"error": "合并字幕正文必须是对象"}), 400
    if not isinstance(time_overrides, dict):
        return jsonify({"error": "字幕时间调整必须是对象"}), 400
    try:
        srt_path = _validate_subtitle_path(data.get("srt_path", ""))
        output_path = save_corrected_srt(
            srt_path,
            corrections,
            deleted_indices=deleted_indices,
            merge_pairs=merge_pairs,
            merge_overrides=merge_overrides,
            time_overrides=time_overrides,
        )
        cue_count = len(parse_srt_document(output_path))
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "source_srt_path": srt_path,
        "corrected_srt_path": output_path,
        "correction_count": len(corrections),
        "deletion_count": len(deleted_indices),
        "merge_count": len(merge_pairs),
        "timing_count": len(time_overrides),
        "cue_count": cue_count,
    })


@app.route("/api/subtitles/preview", methods=["POST"])
def subtitle_preview():
    from autoslice.subtitle_workflow import render_subtitle_preview

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


@app.route("/api/security/session", methods=["GET"])
def bootstrap_security_session():
    """建立 HttpOnly 本机会话；公开 JSON 不包含令牌或 Cookie 值。"""

    mode = "lan" if security_policy.settings().lan_mode else "local"
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/service")
def service_contract():
    return jsonify({
        "service": AUTOSLICE_SERVICE_ID,
        "api_version": AUTOSLICE_API_VERSION,
        # 启动器用它拒绝复用尚未加载当前字幕校对规则的旧进程。
        "subtitle_review_version": SUBTITLE_REVIEW_VERSION,
        "subtitle_asr_version": SUBTITLE_ASR_VERSION,
        "autocover_url": _configured_autocover_url(),
    })


@app.route("/api/streamer-profiles")
def streamer_profiles_contract():
    """返回前端可选择的公开主播配置，不暴露路径和 ASR 内部规则。"""
    try:
        return jsonify({"profiles": public_streamer_profiles()})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/asr-status")
def asr_status_contract():
    """展示当前 FunASR 模型与调整入口，不返回任何本机模型路径。"""

    from autoslice.topic_engine import funasr_public_status

    return jsonify(funasr_public_status())


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
        source_paths=tuple(
            path
            for path in (
                ass_path if os.path.isfile(ass_path) else None,
                manual_timeline_path
                if manual_timeline_path not in {None, "__none__"}
                else None,
                optimized_timeline_path,
            )
            if path
        ),
        output_paths=_topic_task_output_paths(flv_path, output_dir),
        metadata={"streamer_profile_id": streamer_profile.id},
        streamer_profile=streamer_profile,
    )
    if active_task_id:
        return jsonify({
            "error": "该录播正在进行完整分析，请等待当前任务完成",
            "task_id": active_task_id,
        }), 409
    _set_task_output_dir(task_id, output_dir)

    def run():
        if _task_cancellation_requested(task_id):
            return
        try:
            from autoslice.topic_engine import run_pipeline, slice_from_marks

            def cb(msg, step, total):
                _raise_if_task_cancelled(task_id)
                update_task(task_id, status="running", progress=msg, step=step, total=total)

            _raise_if_task_cancelled(task_id)
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
                _raise_if_task_cancelled(task_id)
                count, out_dir = slice_from_marks(
                    flv_path, result["json_path"], output_dir,
                    progress_callback=cb,
                    streamer_profile_id=streamer_profile,
                )
                result["slice_count"] = count
                result["slice_dir"] = out_dir

            _raise_if_task_cancelled(task_id)
        except Exception as exc:
            _record_task_error(task_id, "完整分析失败", exc)
            return
        _complete_pipeline_task(task_id, result)

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
        source_paths=(ass_path,) if os.path.isfile(ass_path) else (),
        output_paths=_topic_task_output_paths(flv_path, output_dir),
        metadata={"streamer_profile_id": streamer_profile.id},
        streamer_profile=streamer_profile,
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
        source_paths=(
            manual_timeline_path,
            *((ass_path,) if os.path.isfile(ass_path) else ()),
        ),
        output_paths=(_topic_task_output_paths(flv_path, output_dir)[0],),
        metadata={"streamer_profile_id": streamer_profile.id},
        streamer_profile=streamer_profile,
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
