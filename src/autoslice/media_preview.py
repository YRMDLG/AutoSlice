"""AutoSlice 任务级短片预览的安全 owner。

本模块只处理已经由 AutoSlice 任务清单登记的短片，不接受请求方提供的
文件路径、文件名或目录。token 是内存中的一次性授权记录（非一次性使用），
绑定任务、片段和登记文件，并在固定 TTL 后失效。HTTP Range 的解析也放在
这里，Web 层只负责把结果转换为 Flask 响应。
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from autoslice.artifact_store import artifact_bundle_layout
from autoslice.media_formats import media_format_for
from autoslice.security_policy import SecurityPolicy
from autoslice.task_results import normalize_task_result

MEDIA_TOKEN_TTL_SECONDS = 5 * 60
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_TOKEN_LENGTH = 256
_ALLOWED_TASK_TYPES = frozenset({
    "topic_pipeline",
    "timeline_optimization",
    "clip_review_retry",
})
_UNAVAILABLE_CLIP_STATUSES = frozenset({
    "等待自动切片",
    "待处理",
    "切片文件缺失",
    "失败",
    "error",
    "failed",
})
_FORBIDDEN_REQUEST_FIELDS = frozenset({
    "file",
    "filename",
    "media_path",
    "path",
    "path_name",
})


class MediaPreviewError(RuntimeError):
    """可安全返回给请求方的媒体预览错误。"""

    def __init__(
            self,
            message: str,
            status_code: int,
            *,
            headers: Mapping[str, str] | None = None,
    ) -> None:
        self.public_message = message
        self.status_code = status_code
        self.headers = dict(headers or {})
        super().__init__(message)


@dataclass(frozen=True)
class IssuedMediaToken:
    """签发给浏览器的最小 token 响应数据。"""

    token: str
    expires_at: float
    ttl_seconds: int


@dataclass(frozen=True)
class MediaPayload:
    """已完成 Range 判定的 HTTP 媒体响应。"""

    status_code: int
    headers: dict[str, str]
    body: Iterable[bytes]


@dataclass(frozen=True)
class _RegisteredClip:
    task_id: str
    clip_id: str
    path: Path
    filename: str
    size: int
    content_type: str
    file_identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _TokenRecord:
    task_id: str
    clip_id: str
    path: Path
    expires_at: float
    file_identity: tuple[int, int, int, int, int]


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    """返回用于拒绝删除后重建同名文件的文件快照。"""

    stat = path.stat()
    return (
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
        int(stat.st_size),
        int(getattr(stat, "st_mtime_ns", 0)),
        int(getattr(stat, "st_ctime_ns", 0)),
    )


def _safe_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise MediaPreviewError("媒体片段不存在", 404)
    if value != value.strip() or any(char in value for char in "\\/\x00\r\n"):
        raise MediaPreviewError("媒体片段不存在", 404)
    return value


def _normalised_path(path: Path) -> str:
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        canonical = Path(os.path.abspath(os.fspath(path)))
    return os.path.normcase(os.path.abspath(os.fspath(canonical)))


def _is_same_path(left: Path, right: Path) -> bool:
    return _normalised_path(left) == _normalised_path(right)


def _has_symlink_component(path: Path) -> bool:
    """检查路径的每一级，避免把别名解析差异误判为符号链接。"""

    current = Path(os.path.abspath(os.fspath(path)))
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _resolve_existing_path(path: Path, *, directory: bool = False) -> Path:
    """解析已登记路径，并拒绝符号链接改写和缺失路径。"""

    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
        if _has_symlink_component(absolute):
            raise MediaPreviewError("媒体产物登记无效", 403)
        resolved = absolute.resolve(strict=True)
    except MediaPreviewError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MediaPreviewError("媒体产物不存在", 404) from exc
    if directory and not resolved.is_dir():
        raise MediaPreviewError("媒体产物登记无效", 403)
    if not directory and not resolved.is_file():
        raise MediaPreviewError("媒体产物不存在", 404)
    return resolved


def _path_inside(path: Path, root: Path, *, direct_child: bool = False) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return not direct_child or len(relative.parts) == 1


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise MediaPreviewError("媒体产物登记无效", 403)
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        structure_decision = SecurityPolicy.validate_json_structure(payload)
        if not structure_decision.allowed:
            raise MediaPreviewError("媒体产物登记无效", 403)
    except MediaPreviewError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
        raise MediaPreviewError("媒体产物登记无效", 403) from exc
    if not isinstance(payload, dict):
        raise MediaPreviewError("媒体产物登记无效", 403)
    return payload


def _parse_single_range(value: str | None, size: int) -> tuple[int, int] | None:
    """解析单个 bytes Range；非法、多段和不可满足范围统一返回 416。"""

    if value is None or not value.strip():
        return None
    if size <= 0:
        raise MediaPreviewError("请求的媒体范围不可满足", 416)
    text = value.strip()
    if not text.lower().startswith("bytes="):
        raise MediaPreviewError("请求的媒体范围不可满足", 416)
    ranges = text[6:].split(",")
    if len(ranges) != 1:
        raise MediaPreviewError("请求的媒体范围不可满足", 416)
    item = ranges[0].strip()
    if "-" not in item:
        raise MediaPreviewError("请求的媒体范围不可满足", 416)
    start_text, end_text = (part.strip() for part in item.split("-", 1))
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise ValueError
            return max(0, size - suffix_length), size - 1
        start = int(start_text)
        if start < 0:
            raise ValueError
        if not end_text:
            end = size - 1
        else:
            end = int(end_text)
            if end < 0:
                raise ValueError
        if start >= size or end < start:
            raise ValueError
        return start, min(end, size - 1)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaPreviewError("请求的媒体范围不可满足", 416) from exc


class MediaPreviewOwner:
    """独立管理 AutoSlice 任务短片 token 和安全文件访问。"""

    def __init__(
            self,
            registry_provider: Callable[[], Any],
            *,
            clock: Callable[[], float] = time.time,
            token_factory: Callable[[], str] | None = None,
            path_authorizer: Callable[[Path], bool] | None = None,
            ttl_seconds: int = MEDIA_TOKEN_TTL_SECONDS,
    ) -> None:
        if not callable(registry_provider) or not callable(clock):
            raise TypeError("媒体预览 owner 依赖必须可调用")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ValueError("媒体 token TTL 必须是正整数")
        if ttl_seconds <= 0 or ttl_seconds > 24 * 60 * 60:
            raise ValueError("媒体 token TTL 超出安全范围")
        if token_factory is not None and not callable(token_factory):
            raise TypeError("token_factory 必须可调用")
        if path_authorizer is not None and not callable(path_authorizer):
            raise TypeError("path_authorizer 必须可调用")
        self._registry_provider = registry_provider
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._path_authorizer = path_authorizer or (lambda _path: True)
        self._ttl_seconds = ttl_seconds
        self._tokens: dict[str, _TokenRecord] = {}
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @staticmethod
    def validate_request_fields(
            query: Mapping[str, Any] | None = None,
            payload: Mapping[str, Any] | None = None,
            *,
            allowed: frozenset[str] = frozenset(),
    ) -> None:
        """拒绝所有客户端路径/文件名字段，不把它们交给任何文件 API。"""

        for source in (query or {}, payload or {}):
            if not isinstance(source, Mapping):
                raise MediaPreviewError("请求参数无效", 400)
            keys = {str(key) for key in source}
            if keys - allowed:
                raise MediaPreviewError("不支持路径或文件名参数", 400)
            if keys.intersection(_FORBIDDEN_REQUEST_FIELDS):
                raise MediaPreviewError("不支持路径或文件名参数", 400)

    def _task_snapshot(self, task_id: str) -> Mapping[str, Any]:
        task_id = _safe_identifier(task_id, "task_id")
        try:
            snapshot = self._registry_provider().snapshot(task_id)
        except (KeyError, OSError, TypeError, ValueError):
            snapshot = None
        if not isinstance(snapshot, Mapping):
            raise MediaPreviewError("任务不存在", 404)
        return snapshot

    def _ensure_path_allowed(self, *paths: Path) -> None:
        """按调用时的策略复核已登记路径，配置漂移时立即失效。"""

        for path in paths:
            try:
                allowed = bool(self._path_authorizer(path))
            except Exception:
                allowed = False
            if not allowed:
                raise MediaPreviewError("媒体路径不在当前允许范围内", 403)

    def _registered_clip(self, task_id: str, clip_id: str) -> _RegisteredClip:
        task_id = _safe_identifier(task_id, "task_id")
        clip_id = _safe_identifier(clip_id, "clip_id")
        task = self._task_snapshot(task_id)
        if task.get("status") != "done":
            raise MediaPreviewError("任务尚未完成", 409)
        if task.get("task_type") not in _ALLOWED_TASK_TYPES:
            raise MediaPreviewError("该任务没有可预览的短片", 403)

        output_ref = task.get("output_dir")
        output_paths = task.get("output_paths")
        if not output_ref or not isinstance(output_paths, (list, tuple)):
            raise MediaPreviewError("任务产物登记无效", 403)
        try:
            registered_output_paths = tuple(
                Path(item) for item in output_paths
            )
        except (TypeError, ValueError) as exc:
            raise MediaPreviewError("任务产物登记无效", 403) from exc
        try:
            output_root = _resolve_existing_path(Path(output_ref), directory=True)
        except MediaPreviewError:
            raise MediaPreviewError("任务产物登记无效", 403)
        self._ensure_path_allowed(output_root)

        result = normalize_task_result(task.get("result"))
        if not isinstance(result, Mapping):
            result = {}
        artifact_ref = result.get("artifact_dir") or task.get("artifact_dir")
        if not artifact_ref:
            raise MediaPreviewError("任务产物登记无效", 403)
        try:
            artifact_dir = _resolve_existing_path(Path(artifact_ref), directory=True)
        except MediaPreviewError:
            raise MediaPreviewError("任务产物登记无效", 403)
        self._ensure_path_allowed(artifact_dir)
        if not _path_inside(artifact_dir, output_root, direct_child=True):
            raise MediaPreviewError("任务产物归属校验失败", 403)
        if not artifact_dir.name.endswith("_自动切片"):
            raise MediaPreviewError("任务产物登记无效", 403)
        if not any(
                _is_same_path(item, artifact_dir)
                for item in registered_output_paths):
            raise MediaPreviewError("任务产物登记无效", 403)

        layout = artifact_bundle_layout(
            task.get("source_path") or f"{task_id}.flv",
            artifact_dir=str(artifact_dir),
            default_output_dir=str(output_root),
        )
        manifest_path = _resolve_existing_path(
            Path(layout["task_manifest_json_path"])
        )
        self._ensure_path_allowed(manifest_path)
        if not _is_same_path(manifest_path.parent.parent, artifact_dir):
            raise MediaPreviewError("任务产物登记无效", 403)
        manifest = _load_json(manifest_path)
        if manifest.get("artifact_dir"):
            try:
                if not _is_same_path(Path(manifest["artifact_dir"]), artifact_dir):
                    raise MediaPreviewError("任务产物归属校验失败", 403)
            except (TypeError, ValueError):
                raise MediaPreviewError("任务产物登记无效", 403) from None

        slice_ref = manifest.get("slice_output_dir")
        if not slice_ref:
            raise MediaPreviewError("媒体产物登记无效", 403)
        slice_dir = _resolve_existing_path(Path(slice_ref), directory=True)
        self._ensure_path_allowed(slice_dir)
        if not _path_inside(slice_dir, output_root, direct_child=True):
            raise MediaPreviewError("媒体产物归属校验失败", 403)
        if not slice_dir.name.endswith("_话题切片"):
            raise MediaPreviewError("媒体产物登记无效", 403)
        if not any(
                _is_same_path(item, slice_dir)
                for item in registered_output_paths):
            raise MediaPreviewError("媒体产物登记无效", 403)

        entries = manifest.get("tasks")
        if not isinstance(entries, list):
            raise MediaPreviewError("媒体产物登记无效", 403)
        matches = [
            entry for entry in entries
            if isinstance(entry, Mapping) and entry.get("id") == clip_id
        ]
        if len(matches) != 1:
            raise MediaPreviewError("媒体片段不存在", 404)
        entry = matches[0]
        status = entry.get("status")
        if isinstance(status, str) and status.strip() in _UNAVAILABLE_CLIP_STATUSES:
            raise MediaPreviewError("媒体片段尚未完成", 409)
        registered_ref = entry.get("slice_path")
        if not isinstance(registered_ref, str) or not registered_ref.strip():
            raise MediaPreviewError("媒体片段尚未登记", 404)
        registered_path = Path(registered_ref)
        if not registered_path.is_absolute():
            registered_path = slice_dir / registered_path
        clip_path = _resolve_existing_path(registered_path)
        self._ensure_path_allowed(clip_path)
        if not _path_inside(clip_path, slice_dir, direct_child=True):
            raise MediaPreviewError("媒体产物归属校验失败", 403)
        capability = media_format_for(clip_path)
        if capability is None or not capability.can_slice:
            raise MediaPreviewError("媒体文件类型不允许预览", 403)
        if entry.get("clip_filename") and entry["clip_filename"] != clip_path.name:
            raise MediaPreviewError("媒体产物登记无效", 403)
        for source_ref in task.get("source_paths") or ():
            try:
                if _is_same_path(Path(source_ref), clip_path):
                    raise MediaPreviewError("整场源视频不允许预览", 403)
            except (TypeError, ValueError):
                raise MediaPreviewError("任务产物登记无效", 403) from None
        try:
            size = clip_path.stat().st_size
        except OSError as exc:
            raise MediaPreviewError("媒体产物不存在", 404) from exc
        if size <= 0:
            raise MediaPreviewError("媒体产物不存在", 404)
        try:
            file_identity = _file_identity(clip_path)
        except OSError as exc:
            raise MediaPreviewError("媒体产物不存在", 404) from exc
        content_type = mimetypes.guess_type(clip_path.name)[0] or "application/octet-stream"
        return _RegisteredClip(
            task_id=task_id,
            clip_id=clip_id,
            path=clip_path,
            filename=clip_path.name,
            size=size,
            content_type=content_type,
            file_identity=file_identity,
        )

    def issue_token(self, task_id: str, clip_id: str) -> IssuedMediaToken:
        clip = self._registered_clip(task_id, clip_id)
        self._ensure_path_allowed(clip.path)
        now = float(self._clock())
        expires_at = now + self._ttl_seconds
        with self._lock:
            self._prune_locked(now)
            for _ in range(8):
                token = str(self._token_factory())
                if len(token) < 32 or len(token) > MAX_TOKEN_LENGTH:
                    continue
                if token not in self._tokens:
                    self._tokens[token] = _TokenRecord(
                        task_id=clip.task_id,
                        clip_id=clip.clip_id,
                        path=clip.path,
                        expires_at=expires_at,
                        file_identity=clip.file_identity,
                    )
                    return IssuedMediaToken(token, expires_at, self._ttl_seconds)
        raise MediaPreviewError("无法签发媒体访问令牌", 503)

    def _prune_locked(self, now: float) -> None:
        expired = [
            token for token, record in self._tokens.items()
            if record.expires_at <= now
        ]
        for token in expired:
            self._tokens.pop(token, None)

    def open_media(
            self,
            task_id: str,
            clip_id: str,
            token: str | None,
            *,
            range_header: str | None = None,
            method: str = "GET",
    ) -> MediaPayload:
        task_id = _safe_identifier(task_id, "task_id")
        clip_id = _safe_identifier(clip_id, "clip_id")
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
            raise MediaPreviewError("媒体不存在或访问令牌无效", 404)
        now = float(self._clock())
        with self._lock:
            self._prune_locked(now)
            record = self._tokens.get(token)
        if record is None or record.expires_at <= now:
            raise MediaPreviewError("媒体不存在或访问令牌无效", 404)
        if (
                not secrets.compare_digest(record.task_id, task_id)
                or not secrets.compare_digest(record.clip_id, clip_id)):
            raise MediaPreviewError("媒体不存在或访问令牌无效", 404)
        clip = self._registered_clip(task_id, clip_id)
        if not _is_same_path(record.path, clip.path):
            raise MediaPreviewError("媒体不存在或访问令牌无效", 404)
        self._ensure_path_allowed(record.path, clip.path)
        if record.file_identity != clip.file_identity:
            raise MediaPreviewError("媒体不存在或访问令牌无效", 404)

        try:
            byte_range = _parse_single_range(range_header, clip.size)
        except MediaPreviewError as exc:
            if exc.status_code == 416:
                exc.headers.update({
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{clip.size}",
                    "Content-Length": "0",
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                })
            raise
        if byte_range is None:
            start, end, status = 0, max(0, clip.size - 1), 200
        else:
            start, end = byte_range
            status = 206
        length = max(0, end - start + 1)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": clip.content_type,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{clip.size}"
        if method.upper() == "HEAD":
            return MediaPayload(status, headers, ())
        return MediaPayload(status, headers, self._iter_file(clip.path, start, length))

    @staticmethod
    def _iter_file(path: Path, start: int, length: int) -> Iterable[bytes]:
        def chunks() -> Iterable[bytes]:
            remaining = length
            try:
                with path.open("rb") as handle:
                    handle.seek(start)
                    while remaining > 0:
                        chunk = handle.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
            except OSError:
                # 文件在授权和响应之间被移除时不把本机错误写入响应。
                return

        return chunks()


__all__ = [
    "IssuedMediaToken",
    "MEDIA_TOKEN_TTL_SECONDS",
    "MediaPayload",
    "MediaPreviewError",
    "MediaPreviewOwner",
]
