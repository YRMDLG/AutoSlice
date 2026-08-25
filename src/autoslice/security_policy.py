"""AutoSlice 与 AutoCover 共用的本机 Web 安全策略。

本模块不依赖 Flask：Host/Origin、会话令牌、LAN 配置和路径边界都可以
用普通 Python 值独立测试。Flask 应用只负责把 ``request`` 中的数据交给
``SecurityPolicy``，并把拒绝决定序列化成各自既有的 JSON 错误格式。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import secrets
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_PATH_FIELDS = frozenset({
    "artifact_dir",
    "ass_path",
    "background_path",
    "cache_dir",
    "flv_path",
    "frame_path",
    "input_dir",
    "json_path",
    "manual_timeline_path",
    "optimized_timeline_path",
    "output_dir",
    "output_path",
    "report_path",
    "root",
    "root_dir",
    "source_path",
    "srt_path",
    "sticker_path",
    "target_path",
    "timeline_json",
    "timeline_path",
    "title_file",
    "video_dir",
    "video_path",
})


@dataclass(frozen=True)
class SecurityDecision:
    """一次请求安全检查的纯数据结果。"""

    allowed: bool
    status_code: int = 200
    message: str = ""
    code: str = "ok"


@dataclass(frozen=True)
class SecuritySettings:
    """从环境变量读取并规范化后的不可变策略配置。"""

    lan_mode: bool
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[tuple[str, str, int]]
    allowed_roots: tuple[Path, ...]
    access_token: str = field(repr=False, compare=False)
    configuration_errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.configuration_errors


class SecurityConfigurationError(RuntimeError):
    """LAN 模式配置不完整或不安全。"""


class _SessionTokenStore:
    """仅在内存中保存会话令牌摘要，并限制数量和生存期。"""

    def __init__(self, *, ttl_seconds: int, max_tokens: int) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_tokens = max(8, int(max_tokens))
        self._tokens: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        expired = [
            digest
            for digest, (expires_at, _scope) in self._tokens.items()
            if expires_at <= now
        ]
        for digest in expired:
            self._tokens.pop(digest, None)
        while len(self._tokens) >= self.max_tokens:
            self._tokens.popitem(last=False)

    def issue(self, scope: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._tokens[self._digest(token)] = (now + self.ttl_seconds, scope)
        return token

    def validate(self, token: str | None, scope: str) -> bool:
        candidate = str(token or "")
        if not 32 <= len(candidate) <= 256:
            return False
        now = time.monotonic()
        digest = self._digest(candidate)
        with self._lock:
            self._prune(now)
            record = self._tokens.get(digest)
            if record is None or record[1] != scope:
                return False
            self._tokens.move_to_end(digest)
            return True


def _env_flag(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _split_values(value: object) -> tuple[str, ...]:
    raw = str(value or "")
    for separator in (",", "\r", "\n"):
        raw = raw.replace(separator, ";")
    return tuple(item.strip() for item in raw.split(";") if item.strip())


def _normalize_hostname(value: object) -> str | None:
    raw = str(value or "").strip().casefold()
    if (
        not raw
        or raw.endswith(".")
        or any(character.isspace() for character in raw)
        or any(character in raw for character in "/\\?#@%*")
    ):
        return None
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    try:
        return ipaddress.ip_address(raw).compressed.casefold()
    except ValueError:
        pass
    if len(raw) > 253:
        return None
    labels = raw.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(not (character.isalnum() or character in "-_") for character in label)
        for label in labels
    ):
        return None
    return raw


def _parse_host_header(value: object) -> tuple[str, int | None] | None:
    raw = str(value or "").strip()
    if (
        not raw
        or any(character.isspace() for character in raw)
        or any(character in raw for character in "/\\?#@")
        or (raw.count(":") > 1 and not raw.startswith("["))
    ):
        return None
    try:
        parsed = urlsplit(f"//{raw}")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    hostname = _normalize_hostname(parsed.hostname)
    if hostname is None:
        return None
    return hostname, port


def _parse_origin(value: object, *, referer: bool = False) -> tuple[str, str, int] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        explicit_port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    hostname = _normalize_hostname(parsed.hostname)
    if (
        scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    if not referer and (parsed.path not in {"", "/"} or parsed.query):
        return None
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    return scheme, hostname, port


def _request_origin(scheme: object, host_header: object) -> tuple[str, str, int] | None:
    normalized_scheme = str(scheme or "").strip().casefold()
    host = _parse_host_header(host_header)
    if normalized_scheme not in {"http", "https"} or host is None:
        return None
    hostname, explicit_port = host
    port = explicit_port if explicit_port is not None else (
        443 if normalized_scheme == "https" else 80
    )
    return normalized_scheme, hostname, port


def _token_is_strong(token: object) -> bool:
    """拒绝短令牌和明显重复的占位符，同时兼容随机 hex/base64 令牌。"""

    value = str(token or "").strip()
    if not 32 <= len(value) <= 512 or len(value.encode("utf-8")) < 32:
        return False
    counts = Counter(value)
    if len(counts) < 8 or max(counts.values()) * 2 >= len(value):
        return False
    return True


def _normalize_path_field(key: object) -> str:
    return str(key or "").strip().casefold().replace("-", "_").rstrip("[]")


_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:(?:[a-z]:[\\/])|(?:\\\\[^\\/\s]+[\\/][^\\/\s]+[\\/]))"
    r"[^\r\n<>\"']+"
)
_POSIX_PRIVATE_PATH_RE = re.compile(
    r"(?<![\w])/(?:home|Users|tmp|var|etc|opt|mnt|srv|data)/[^\r\n<>\"']+",
    re.IGNORECASE,
)


class SecurityPolicy:
    """统一执行 Host、Origin、会话令牌和 LAN 路径边界。"""

    def __init__(
        self,
        *,
        env_prefix: str,
        cookie_name: str,
        access_header: str,
        environ: Mapping[str, str] | None = None,
        path_fields: set[str] | frozenset[str] | None = None,
        session_ttl_seconds: int = 12 * 60 * 60,
        max_session_tokens: int = 512,
    ) -> None:
        self.env_prefix = env_prefix.strip().upper()
        self.cookie_name = cookie_name
        self.access_header = access_header
        self.environ = environ if environ is not None else os.environ
        self.path_fields = frozenset(
            _normalize_path_field(item)
            for item in (path_fields or DEFAULT_PATH_FIELDS)
        )
        self.session_ttl_seconds = max(60, int(session_ttl_seconds))
        self._sessions = _SessionTokenStore(
            ttl_seconds=self.session_ttl_seconds,
            max_tokens=max_session_tokens,
        )

    def _env(self, suffix: str) -> str:
        return str(self.environ.get(f"{self.env_prefix}_{suffix}", ""))

    def settings(self) -> SecuritySettings:
        lan_mode = _env_flag(self._env("LAN_MODE"))
        if not lan_mode:
            return SecuritySettings(
                lan_mode=False,
                allowed_hosts=LOOPBACK_HOSTS,
                allowed_origins=frozenset(),
                allowed_roots=(),
                access_token="",
            )

        errors: list[str] = []
        access_token = self._env("LAN_TOKEN").strip()
        if not _token_is_strong(access_token):
            errors.append("weak_token")

        raw_hosts = _split_values(self._env("LAN_HOSTS"))
        configured_hosts: set[str] = set()
        if not raw_hosts:
            errors.append("missing_hosts")
        for raw_host in raw_hosts:
            hostname = _normalize_hostname(raw_host)
            if hostname is None:
                errors.append("invalid_host")
            else:
                configured_hosts.add(hostname)

        raw_origins = _split_values(self._env("LAN_ORIGINS"))
        configured_origins: set[tuple[str, str, int]] = set()
        if not raw_origins:
            errors.append("missing_origins")
        for raw_origin in raw_origins:
            origin = _parse_origin(raw_origin)
            if origin is None:
                errors.append("invalid_origin")
                continue
            if origin[1] not in configured_hosts and origin[1] not in LOOPBACK_HOSTS:
                errors.append("origin_host_not_allowed")
                continue
            configured_origins.add(origin)

        raw_roots = _split_values(self._env("ALLOWED_ROOTS"))
        configured_roots: list[Path] = []
        if not raw_roots:
            errors.append("missing_roots")
        for raw_root in raw_roots:
            try:
                root = Path(os.path.expandvars(raw_root)).expanduser()
                if not root.is_absolute():
                    raise ValueError("LAN 允许目录必须是绝对路径")
                resolved = root.resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                errors.append("invalid_root")
                continue
            if resolved not in configured_roots:
                configured_roots.append(resolved)

        return SecuritySettings(
            lan_mode=True,
            allowed_hosts=frozenset(LOOPBACK_HOSTS | configured_hosts),
            allowed_origins=frozenset(configured_origins),
            allowed_roots=tuple(configured_roots),
            access_token=access_token,
            configuration_errors=tuple(dict.fromkeys(errors)),
        )

    def bind_host(self) -> str:
        """返回与当前策略一致的监听地址；无效 LAN 配置绝不降级放行。"""

        settings = self.settings()
        if not settings.is_valid:
            raise SecurityConfigurationError(
                "局域网模式要求强随机令牌，并显式配置允许的 Host、Origin 和绝对路径根目录"
            )
        return "0.0.0.0" if settings.lan_mode else "127.0.0.1"

    def _session_scope(
        self,
        settings: SecuritySettings,
        request_origin: tuple[str, str, int],
    ) -> str:
        mode = "lan" if settings.lan_mode else "local"
        scheme, hostname, port = request_origin
        components = [self.env_prefix, mode, scheme, hostname, str(port)]
        if settings.lan_mode:
            components.extend((
                hashlib.sha256(settings.access_token.encode("utf-8")).hexdigest(),
                repr(sorted(settings.allowed_origins)),
                repr(tuple(os.path.normcase(str(root)) for root in settings.allowed_roots)),
            ))
        return hashlib.sha256("\0".join(components).encode("utf-8")).hexdigest()

    def _session_is_valid(
        self,
        cookies: Mapping[str, Any],
        settings: SecuritySettings,
        request_origin: tuple[str, str, int],
    ) -> bool:
        return self._sessions.validate(
            cookies.get(self.cookie_name),
            self._session_scope(settings, request_origin),
        )

    def _access_token_is_valid(
        self,
        headers: Mapping[str, Any],
        settings: SecuritySettings,
    ) -> bool:
        if not settings.lan_mode or not settings.is_valid:
            return False
        presented = str(headers.get(self.access_header) or "")
        return bool(presented) and secrets.compare_digest(
            presented,
            settings.access_token,
        )

    def authorize(
        self,
        *,
        method: object,
        scheme: object,
        host_header: object,
        headers: Mapping[str, Any],
        cookies: Mapping[str, Any],
        origin: object = "",
        referer: object = "",
    ) -> SecurityDecision:
        """对不含 Flask 类型的一组请求元数据作出允许或拒绝决定。"""

        settings = self.settings()
        if not settings.is_valid:
            return SecurityDecision(
                False,
                503,
                "局域网安全配置无效，请检查令牌、Host、Origin 和允许目录",
                "invalid_lan_configuration",
            )

        request_origin = _request_origin(scheme, host_header)
        if request_origin is None or request_origin[1] not in settings.allowed_hosts:
            return SecurityDecision(
                False,
                403,
                "拒绝不受信任的 Host",
                "untrusted_host",
            )
        hostname = request_origin[1]
        session_valid = self._session_is_valid(cookies, settings, request_origin)
        access_token_valid = self._access_token_is_valid(headers, settings)

        if settings.lan_mode and not (access_token_valid or session_valid):
            return SecurityDecision(
                False,
                401,
                "局域网模式需要有效访问令牌",
                "lan_authentication_required",
            )

        if str(method or "").upper() not in WRITE_METHODS:
            return SecurityDecision(True)

        supplied_origin = str(origin or "").strip()
        supplied_referer = str(referer or "").strip()
        if supplied_origin or supplied_referer:
            candidate = _parse_origin(
                supplied_origin or supplied_referer,
                referer=not bool(supplied_origin),
            )
            trusted = candidate == request_origin
            if trusted and settings.lan_mode and hostname not in LOOPBACK_HOSTS:
                trusted = candidate in settings.allowed_origins
            if not trusted:
                return SecurityDecision(
                    False,
                    403,
                    "拒绝跨站写请求",
                    "cross_site_write",
                )
            return SecurityDecision(True)

        if session_valid or access_token_valid:
            return SecurityDecision(True)
        return SecurityDecision(
            False,
            403,
            "写请求需要同源 Origin/Referer 或有效本机会话",
            "write_proof_required",
        )

    def authorize_flask_request(self, flask_request: Any) -> SecurityDecision:
        """从 Flask request 读取最小元数据，业务路由无需理解安全细节。"""

        return self.authorize(
            method=flask_request.method,
            scheme=flask_request.scheme,
            host_header=flask_request.host,
            headers=flask_request.headers,
            cookies=flask_request.cookies,
            origin=flask_request.headers.get("Origin"),
            referer=flask_request.headers.get("Referer"),
        )

    def _is_path_field(self, key: object) -> bool:
        normalized = _normalize_path_field(key)
        return (
            normalized in self.path_fields
            or normalized.endswith("_path")
            or normalized.endswith("_paths")
            or normalized.endswith("_dir")
            or normalized.endswith("_dirs")
        )

    def _iter_path_values(self, payload: Any):
        if isinstance(payload, Mapping):
            for key, value in payload.items():
                if self._is_path_field(key):
                    if isinstance(value, (list, tuple, set)):
                        for item in value:
                            yield key, item
                    else:
                        yield key, value
                else:
                    yield from self._iter_path_values(value)
        elif isinstance(payload, (list, tuple, set)):
            for item in payload:
                yield from self._iter_path_values(item)

    @staticmethod
    def _path_within_roots(value: object, roots: tuple[Path, ...]) -> bool:
        if not isinstance(value, (str, os.PathLike)):
            return False
        raw = str(value)
        if not raw or "\x00" in raw:
            return False
        try:
            candidate = Path(os.path.expandvars(raw)).expanduser()
            if not candidate.is_absolute():
                return False
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return False
        for root in roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def validate_effective_paths(self, *values: object) -> SecurityDecision:
        """校验业务层最终选定的路径，包括未出现在请求中的默认路径。"""

        settings = self.settings()
        if not settings.is_valid:
            return SecurityDecision(
                False,
                503,
                "局域网安全配置无效，请检查令牌、Host、Origin 和允许目录",
                "invalid_lan_configuration",
            )
        if not settings.lan_mode:
            return SecurityDecision(True)
        for value in values:
            if value in (None, ""):
                continue
            if not self._path_within_roots(value, settings.allowed_roots):
                return SecurityDecision(
                    False,
                    403,
                    "资源路径不在局域网允许目录内",
                    "path_outside_allowed_roots",
                )
        return SecurityDecision(True)

    def path_is_allowed(self, value: object) -> bool:
        """供资源 owner 在登记和消费阶段执行当前策略的动态授权。"""

        return self.validate_effective_paths(value).allowed

    @staticmethod
    def _redact_path_text(value: str) -> str:
        text = str(value)
        text = _WINDOWS_ABSOLUTE_PATH_RE.sub("[本地路径已隐藏]", text)
        return _POSIX_PRIVATE_PATH_RE.sub("[本地路径已隐藏]", text)

    def redact_lan_payload(self, payload: Any) -> Any:
        """仅在 LAN 模式下递归移除响应中的本机绝对路径。"""

        settings = self.settings()
        if not settings.lan_mode:
            return payload
        if isinstance(payload, Mapping):
            return {
                key: self.redact_lan_payload(value)
                for key, value in payload.items()
            }
        if isinstance(payload, list):
            return [self.redact_lan_payload(value) for value in payload]
        if isinstance(payload, tuple):
            return tuple(self.redact_lan_payload(value) for value in payload)
        if isinstance(payload, str):
            return self._redact_path_text(payload)
        return payload

    def redact_lan_text(self, value: object) -> str:
        """仅在 LAN 模式下清理 HTML 或纯文本中的本机绝对路径。"""

        text = str(value or "")
        return self._redact_path_text(text) if self.settings().lan_mode else text

    @staticmethod
    def _upload_filename_is_safe(value: object) -> bool:
        filename = str(value or "")
        return bool(
            filename
            and filename not in {".", ".."}
            and filename == filename.strip(" .")
            and "\x00" not in filename
            and "/" not in filename
            and "\\" not in filename
            and ":" not in filename
        )

    def validate_paths(
        self,
        *,
        json_payload: Any = None,
        form_payload: Any = None,
        query_payload: Any = None,
        upload_filenames: Any = None,
    ) -> SecurityDecision:
        """在 LAN 模式下递归约束 JSON、表单、查询和上传文件名。"""

        settings = self.settings()
        if not settings.is_valid:
            return SecurityDecision(
                False,
                503,
                "局域网安全配置无效，请检查令牌、Host、Origin 和允许目录",
                "invalid_lan_configuration",
            )
        if not settings.lan_mode:
            return SecurityDecision(True)

        for payload in (json_payload, form_payload, query_payload):
            for _key, value in self._iter_path_values(payload):
                if value in (None, ""):
                    continue
                if not self._path_within_roots(value, settings.allowed_roots):
                    return SecurityDecision(
                        False,
                        403,
                        "请求路径不在局域网允许目录内",
                        "path_outside_allowed_roots",
                    )

        if upload_filenames is not None:
            values = (
                upload_filenames.values()
                if isinstance(upload_filenames, Mapping)
                else upload_filenames
            )
            for value in values:
                filenames = value if isinstance(value, (list, tuple, set)) else (value,)
                for filename in filenames:
                    if not self._upload_filename_is_safe(filename):
                        return SecurityDecision(
                            False,
                            403,
                            "上传文件名不能包含路径",
                            "unsafe_upload_filename",
                        )
        return SecurityDecision(True)

    def validate_flask_request_paths(self, flask_request: Any) -> SecurityDecision:
        """提取 Flask 的四种输入载体，再调用可独立测试的路径检查。"""

        json_payload = (
            flask_request.get_json(silent=True)
            if flask_request.is_json
            else None
        )
        form_payload = {
            key: flask_request.form.getlist(key)
            for key in flask_request.form.keys()
        }
        query_payload = {
            key: flask_request.args.getlist(key)
            for key in flask_request.args.keys()
        }
        upload_filenames = {
            key: [storage.filename for storage in flask_request.files.getlist(key)]
            for key in flask_request.files.keys()
        }
        return self.validate_paths(
            json_payload=json_payload,
            form_payload=form_payload,
            query_payload=query_payload,
            upload_filenames=upload_filenames,
        )

    def attach_session_cookie(
        self,
        response: Any,
        *,
        scheme: object,
        host_header: object,
        secure: bool = False,
    ) -> bool:
        """把随机会话令牌写入 HttpOnly Cookie；令牌不进入响应正文。"""

        settings = self.settings()
        request_origin = _request_origin(scheme, host_header)
        if (
            not settings.is_valid
            or request_origin is None
            or request_origin[1] not in settings.allowed_hosts
        ):
            return False
        token = self._sessions.issue(
            self._session_scope(settings, request_origin)
        )
        response.set_cookie(
            self.cookie_name,
            token,
            max_age=self.session_ttl_seconds,
            httponly=True,
            secure=bool(secure),
            samesite="Strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return True


__all__ = [
    "DEFAULT_PATH_FIELDS",
    "LOOPBACK_HOSTS",
    "SecurityConfigurationError",
    "SecurityDecision",
    "SecurityPolicy",
    "SecuritySettings",
    "WRITE_METHODS",
]
