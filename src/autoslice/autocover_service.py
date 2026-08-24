"""AutoCover 本机端点校验与服务探测的唯一生产 owner。"""

import errno
import http.client
import json
import os
import re
import socket
import urllib.parse
from dataclasses import dataclass

AUTOCOVER_SERVICE_ID = "autocover"
AUTOCOVER_API_VERSION = 8
DEFAULT_AUTOCOVER_URL = "http://127.0.0.1:5010"
AUTOCOVER_OPTIONS_PATH = "/api/options"
AUTOCOVER_PROBE_READY = "ready"
AUTOCOVER_PROBE_UNAVAILABLE = "unavailable"
AUTOCOVER_PROBE_INCOMPATIBLE = "incompatible"
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_REASON_LENGTH = 240
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![\w])(?:[a-z]:\\)[^\r\n]+")
_POSIX_PRIVATE_PATH_RE = re.compile(
    r"(?<![\w])/(?:home|Users|tmp|var|etc|opt|mnt)/[^\s,;]+",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|"
    r"authorization|cookie|password|client[_ -]?secret)\b['\"]?\s*[:=]\s*"
    r"['\"]?[^\s,;}\]]+"
)


@dataclass(frozen=True)
class AutoCoverEndpoint:
    """经过安全校验的浏览器地址与固定 IPv4 loopback 探测地址。"""

    browser_url: str
    port: int

    @property
    def probe_url(self):
        return f"http://127.0.0.1:{self.port}{AUTOCOVER_OPTIONS_PATH}"


@dataclass(frozen=True)
class AutoCoverProbeResult:
    """供启动器和 Web 使用的结构化探测结果。"""

    status: str
    reason: str = ""
    payload: dict | None = None

    @property
    def ready(self):
        return self.status == AUTOCOVER_PROBE_READY


def _parse_autocover_endpoint(value):
    candidate = str(value or "").strip()
    if not candidate or "\\" in candidate or any(ord(char) < 32 for char in candidate):
        raise ValueError("AutoCover 地址格式无效")
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("AutoCover 端口无效") from exc
    if (
            parsed.scheme.casefold() != "http"
            or parsed.hostname is None
            or parsed.hostname.casefold() not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or port is None
            or not 1 <= port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment):
        raise ValueError("AutoCover 地址必须是不带实际路径的本机 HTTP 地址")
    browser_host = parsed.hostname.casefold()
    return AutoCoverEndpoint(
        browser_url=f"http://{browser_host}:{port}",
        port=port,
    )


def configured_autocover_endpoint(environ=None):
    """读取配置；任何不安全值都回退到固定的 127.0.0.1:5010。"""

    env = environ if environ is not None else os.environ
    candidate = env.get("AUTOCOVER_URL", DEFAULT_AUTOCOVER_URL)
    try:
        return _parse_autocover_endpoint(candidate)
    except ValueError:
        return _parse_autocover_endpoint(DEFAULT_AUTOCOVER_URL)


def autocover_endpoint_from_url(url):
    """严格解析可信调用方提供的本机 AutoCover URL。"""

    return _parse_autocover_endpoint(url)


def is_compatible_autocover_service(payload):
    return bool(
        isinstance(payload, dict)
        and payload.get("service") == AUTOCOVER_SERVICE_ID
        and payload.get("api_version") == AUTOCOVER_API_VERSION
    )


def _safe_reason(value, fallback):
    message = " ".join(str(value or "").split())
    if not message:
        return fallback
    if "traceback" in message.casefold():
        return fallback
    message = _SENSITIVE_VALUE_RE.sub("[已隐藏]", message)
    message = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [已隐藏]", message)
    message = re.sub(r"(?i)\bsk-[a-z0-9._-]{4,}", "[已隐藏]", message)
    message = _WINDOWS_PATH_RE.sub("[本地路径已隐藏]", message)
    message = _POSIX_PRIVATE_PATH_RE.sub("[本地路径已隐藏]", message)
    if len(message) > _MAX_REASON_LENGTH:
        message = f"{message[:_MAX_REASON_LENGTH - 1]}…"
    return message or fallback


def _read_json_payload(response):
    raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError("响应体超过安全上限")
    return json.loads(raw.decode("utf-8"))


def _unavailable_reason(exc, endpoint):
    safe_detail = _safe_reason(exc, "连接失败")
    return f"{safe_detail}（127.0.0.1:{endpoint.port} 未监听或暂不可用）"


def _is_connect_unavailable_error(exc):
    return isinstance(exc, (ConnectionRefusedError, TimeoutError, socket.timeout)) or getattr(
        exc,
        "errno",
        None,
    ) in {
        errno.ECONNREFUSED,
        errno.ETIMEDOUT,
        10060,
        10061,
    }


def probe_autocover_endpoint(
        endpoint, timeout=0.8, connection_factory=http.client.HTTPConnection):
    """以单个直连连接探测固定 IPv4 loopback，不经过 DNS、代理或重定向。"""

    if not isinstance(endpoint, AutoCoverEndpoint):
        raise TypeError("endpoint 必须是 AutoCoverEndpoint")
    connection = connection_factory(
        "127.0.0.1",
        endpoint.port,
        timeout=timeout,
    )
    try:
        connection.connect()
    except (OSError, http.client.HTTPException) as exc:
        try:
            connection.close()
        except OSError:
            pass
        if _is_connect_unavailable_error(exc):
            return AutoCoverProbeResult(
                AUTOCOVER_PROBE_UNAVAILABLE,
                _unavailable_reason(exc, endpoint),
            )
        return AutoCoverProbeResult(
            AUTOCOVER_PROBE_INCOMPATIBLE,
            f"连接阶段异常：{_safe_reason(exc, '无法建立本机 HTTP 连接')}",
        )
    try:
        connection.request(
            "GET",
            AUTOCOVER_OPTIONS_PATH,
            headers={"Accept": "application/json"},
        )
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            reason = _safe_reason(response.reason, "未知 HTTP 状态")
            return AutoCoverProbeResult(
                AUTOCOVER_PROBE_INCOMPATIBLE,
                f"HTTP {response.status} {reason}",
            )
        payload = _read_json_payload(response)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return AutoCoverProbeResult(
            AUTOCOVER_PROBE_INCOMPATIBLE,
            f"响应不是有效 JSON：{_safe_reason(exc, 'JSON 解析失败')}",
        )
    except http.client.HTTPException as exc:
        return AutoCoverProbeResult(
            AUTOCOVER_PROBE_INCOMPATIBLE,
            f"HTTP 响应无效：{_safe_reason(exc, '无法解析 HTTP 响应')}",
        )
    except OSError as exc:
        return AutoCoverProbeResult(
            AUTOCOVER_PROBE_INCOMPATIBLE,
            f"连接后返回异常：{_safe_reason(exc, '本机服务响应异常')}",
        )
    finally:
        try:
            connection.close()
        except OSError:
            pass

    if not isinstance(payload, dict):
        return AutoCoverProbeResult(
            AUTOCOVER_PROBE_INCOMPATIBLE,
            f"JSON 顶层类型不兼容：{type(payload).__name__}",
        )
    if not is_compatible_autocover_service(payload):
        service = _safe_reason(payload.get("service"), "缺失")
        api_version = _safe_reason(payload.get("api_version"), "缺失")
        return AutoCoverProbeResult(
            AUTOCOVER_PROBE_INCOMPATIBLE,
            (
                "服务契约不兼容："
                f"service={service}，api_version={api_version}；"
                f"期望 service={AUTOCOVER_SERVICE_ID}，"
                f"api_version={AUTOCOVER_API_VERSION}"
            ),
            payload,
        )
    return AutoCoverProbeResult(AUTOCOVER_PROBE_READY, payload=payload)


def probe_autocover_service(
        port, timeout=0.8, connection_factory=http.client.HTTPConnection):
    """保留 launcher 的旧 payload/None 探测契约。"""

    try:
        numeric_port = int(port)
    except (TypeError, ValueError):
        return None
    if not 1 <= numeric_port <= 65535:
        return None
    endpoint = AutoCoverEndpoint(
        browser_url=f"http://127.0.0.1:{numeric_port}",
        port=numeric_port,
    )
    result = probe_autocover_endpoint(
        endpoint,
        timeout=timeout,
        connection_factory=connection_factory,
    )
    return result.payload if isinstance(result.payload, dict) else None
