"""AutoSlice 唯一的 LLM 配置、HTTP、响应解析与重试实现。

传输层只接收调用方提供的通用 prompt，不包含任何话题、字幕、候选或标题
业务文案。测试可通过 ``request_post``、``call_func`` 与 ``sleep_func`` 注入
边界，避免访问真实服务。
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import requests

from autoslice.llm.contracts import (
    DEFAULT_PROXY_MODE,
    DEFAULT_REASONING_EFFORT,
    LLMApiConfig,
    LLMProviderUnavailableError,
    LLMRequest,
    LLMResponseFormatError,
    LLMResponseTruncatedError,
    LLMStructuredOutputError,
    LLMTimeout,
    LLMTransportError,
    RetryCategory,
    classify_retry,
    is_retryable_error,
    normalise_protocol,
    normalise_proxy_mode,
    normalise_proxy_url,
    normalise_reasoning_effort,
    normalise_reasoning_stage,
    normalise_timeout,
    redact_url_credentials,
)

DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_COMPACT_MAX_TOKENS = 12000
DEFAULT_REQUEST_TIMEOUT = (30, 300)
DEFAULT_RETRY_DELAYS = (3, 8, 20, 45)
DEFAULT_PROVIDER_UNAVAILABLE_RETRY_DELAYS = (3, 8)
PROJECT_DIR = Path(__file__).resolve().parents[2]

_UNSUPPORTED_REASONING_EFFORT_KEYS = set()
_REASONING_EFFORT_LOCK = threading.Lock()
_RETRY_AFTER_SHARED_RECOVERY = object()
_PROXY_ENVIRONMENT_FIELDS = {
    "AUTOSLICE_LLM_PROXY_MODE": "proxy_mode",
    "AUTOSLICE_LLM_PROXY_HTTP": "http_proxy",
    "AUTOSLICE_LLM_PROXY_HTTPS": "https_proxy",
}


class _RedactedHeaderValue(str):
    """保持真实认证头值，同时避免 repr 和 mock 失败输出泄漏凭据。"""

    def __repr__(self) -> str:
        return repr("***")


def reset_reasoning_effort_capability_cache() -> None:
    """清空进程内兼容性记录，主要供配置切换和测试使用。"""
    with _REASONING_EFFORT_LOCK:
        _UNSUPPORTED_REASONING_EFFORT_KEYS.clear()


def _reasoning_effort_capability_key(base_url: str, model: str) -> tuple[str, str]:
    return str(base_url).casefold(), str(model).casefold()


def _reasoning_effort_is_disabled(key: tuple[str, str]) -> bool:
    with _REASONING_EFFORT_LOCK:
        return key in _UNSUPPORTED_REASONING_EFFORT_KEYS


def _disable_reasoning_effort(key: tuple[str, str]) -> None:
    with _REASONING_EFFORT_LOCK:
        _UNSUPPORTED_REASONING_EFFORT_KEYS.add(key)


def _response_rejects_reasoning_effort(response: Any) -> bool:
    """只在服务端明确指出字段不受支持时降级，避免吞掉其他 4xx。"""
    if getattr(response, "status_code", None) not in {400, 422}:
        return False
    try:
        text = str(response.text or "").casefold()
    except (AttributeError, TypeError, ValueError):
        return False
    return "reasoning_effort" in text or "reasoning effort" in text


def infer_api_type(base_url: Any, token: Any) -> str:
    """仅为缺少显式协议的旧配置推断 OpenAI/Anthropic 兼容类型。"""
    lower_token = str(token).casefold()
    lower_url = str(base_url).casefold()
    if lower_token.startswith("sk-ant-"):
        return "anthropic"
    if "anthropic" in lower_url:
        return "anthropic"
    if lower_token.startswith("sk-"):
        return "openai"
    if any(marker in lower_url for marker in ("openai", "opencode.ai", "/v1")):
        return "openai"
    return "anthropic"


def normalise_api_config(
        payload: Any, source: str, *, default_model: str,
        default_api_type: Optional[str] = None) -> LLMApiConfig:
    """校验配置并返回不泄漏凭据的稳定契约。"""
    if not isinstance(payload, dict):
        raise ValueError(f"API 配置格式错误：{source} 顶层必须是 JSON 对象")

    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    token = str(payload.get("token") or "").strip()
    model = str(payload.get("model") or default_model).strip()
    if not base_url:
        raise ValueError(f"API 配置缺少 base_url：{source}")
    try:
        parsed = urlsplit(base_url)
        valid_port = parsed.port
    except ValueError as exc:
        raise ValueError(f"API base_url 不是有效的 HTTP(S) 地址：{source}") from exc
    if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or (valid_port is not None and not 1 <= valid_port <= 65535)):
        raise ValueError(f"API base_url 必须是有效的 HTTP(S) 地址：{source}")
    if not token:
        raise ValueError(f"API 配置缺少 token：{source}")
    if not model:
        raise ValueError(f"API 配置缺少 model：{source}")

    raw_api_type = payload.get(
        "api_type",
        payload.get("protocol", default_api_type),
    )
    if raw_api_type is None or not str(raw_api_type).strip():
        api_type = infer_api_type(base_url, token)
    else:
        try:
            api_type = normalise_protocol(raw_api_type)
        except ValueError as exc:
            raise ValueError(
                f"API 配置 api_type 只支持 openai 或 anthropic：{source}"
            ) from exc
    analysis_reasoning_effort = normalise_reasoning_effort(
        payload["analysis_reasoning_effort"]
        if "analysis_reasoning_effort" in payload
        else DEFAULT_REASONING_EFFORT,
    )
    review_reasoning_effort = normalise_reasoning_effort(
        payload["review_reasoning_effort"]
        if "review_reasoning_effort" in payload
        else DEFAULT_REASONING_EFFORT,
    )
    return LLMApiConfig(
        base_url,
        token,
        model,
        api_type,
        analysis_reasoning_effort=analysis_reasoning_effort,
        review_reasoning_effort=review_reasoning_effort,
        proxy_mode=payload.get("proxy_mode", DEFAULT_PROXY_MODE),
        http_proxy=payload.get("http_proxy"),
        https_proxy=payload.get("https_proxy"),
    )


def read_json_config(path: Any, *, json_loader: Optional[Callable] = None) -> Any:
    """读取显式配置文件，错误中不包含文件正文。"""
    json_loader = json_loader or json.load
    try:
        with open(path, encoding="utf-8") as file:
            return json_loader(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 API 配置文件：{path}") from exc


def _apply_proxy_environment(payload: dict, environ: dict) -> dict:
    """只应用 AutoSlice 明确命名的代理变量，绝不读取通用代理或外部密钥。"""
    merged = dict(payload)
    for env_name, field_name in _PROXY_ENVIRONMENT_FIELDS.items():
        if env_name in environ:
            merged[field_name] = environ.get(env_name)
    return merged


def load_api_config(
        *, project_dir: Any = None, default_model: Optional[str] = None,
        path_module=None, json_loader: Optional[Callable] = None,
        environ: Optional[dict] = None) -> LLMApiConfig:
    """只读取 AutoSlice 显式环境变量或项目 ``api_config.json``。"""
    project_dir = str(PROJECT_DIR if project_dir is None else project_dir)
    path_module = path_module or os.path
    json_loader = json_loader or json.load
    default_model = str(
        default_model
        or os.environ.get("AUTOSLICE_LLM_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    environ = os.environ if environ is None else environ
    env_keys = (
        "AUTOSLICE_API_BASE_URL",
        "AUTOSLICE_API_TOKEN",
        "AUTOSLICE_API_TYPE",
    )
    env_model = str(environ.get("AUTOSLICE_LLM_MODEL") or "").strip()
    env_analysis_effort = environ.get("AUTOSLICE_ANALYSIS_REASONING_EFFORT")
    env_review_effort = environ.get("AUTOSLICE_REVIEW_REASONING_EFFORT")
    if any(key in environ for key in env_keys):
        payload = _apply_proxy_environment(
            {
                "base_url": environ.get("AUTOSLICE_API_BASE_URL"),
                "token": environ.get("AUTOSLICE_API_TOKEN"),
                "model": env_model or default_model,
                "api_type": environ.get("AUTOSLICE_API_TYPE"),
                "analysis_reasoning_effort": (
                    env_analysis_effort
                    if "AUTOSLICE_ANALYSIS_REASONING_EFFORT" in environ
                    else DEFAULT_REASONING_EFFORT
                ),
                "review_reasoning_effort": (
                    env_review_effort
                    if "AUTOSLICE_REVIEW_REASONING_EFFORT" in environ
                    else DEFAULT_REASONING_EFFORT
                ),
            },
            environ,
        )
        return normalise_api_config(
            payload,
            "环境变量 AUTOSLICE_API_*",
            default_model=default_model,
        )

    auto_cfg = path_module.join(project_dir, "api_config.json")
    if path_module.exists(auto_cfg):
        payload = read_json_config(auto_cfg, json_loader=json_loader)
        if isinstance(payload, dict):
            payload = dict(payload)
            if env_model:
                payload["model"] = env_model
            if "AUTOSLICE_ANALYSIS_REASONING_EFFORT" in environ:
                payload["analysis_reasoning_effort"] = env_analysis_effort
            if "AUTOSLICE_REVIEW_REASONING_EFFORT" in environ:
                payload["review_reasoning_effort"] = env_review_effort
            payload = _apply_proxy_environment(payload, environ)
        return normalise_api_config(
            payload,
            auto_cfg,
            default_model=default_model,
        )

    raise ValueError(
        "未配置 LLM API。请复制 api_config.example.json 为 api_config.json，"
        "或设置 AUTOSLICE_API_BASE_URL、AUTOSLICE_API_TOKEN 和 "
        "AUTOSLICE_API_TYPE。"
    )


def extract_json_payload(text: Any) -> Any:
    """从纯 JSON、Markdown 代码块或混合文本中提取首个完整 JSON。"""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(stripped):
        if character not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(stripped[index:])
        except ValueError:
            continue
        return payload
    return None


def response_has_complete_json(content: Any) -> bool:
    """判断文本中是否包含可解析的完整 JSON。"""
    return bool(content and extract_json_payload(content) is not None)


def decode_response_json(response: Any, api_type: str) -> dict:
    """安全解码 HTTP 200 响应，不把正文或请求 prompt 写入异常。"""
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise LLMResponseFormatError(
            f"{api_type} API 返回了非 JSON 响应（HTTP 200）"
        ) from exc
    if not isinstance(payload, dict):
        raise LLMResponseFormatError(
            f"{api_type} API 响应顶层必须是 JSON 对象"
        )
    return payload


def _openai_content_text(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise LLMResponseFormatError(
            f"OpenAI API 的 {field_name} 字段类型错误"
        )
    parts = []
    for block in value:
        if not isinstance(block, dict):
            raise LLMResponseFormatError(
                f"OpenAI API 的 {field_name} 内容块必须是对象"
            )
        if block.get("type") not in {"text", "output_text"}:
            continue
        text = block.get("text")
        if not isinstance(text, str):
            raise LLMResponseFormatError(
                f"OpenAI API 的 {field_name} 文本块缺少 text"
            )
        parts.append(text)
    return "\n".join(part for part in parts if part)


def parse_openai_response(data: dict, model: str, max_tokens: int) -> str:
    """解析 OpenAI Chat Completions 兼容响应。"""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseFormatError("OpenAI API 响应缺少非空 choices 数组")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise LLMResponseFormatError("OpenAI API 的 choice 必须是对象")
    message = choice.get("message", {})
    if not isinstance(message, dict):
        raise LLMResponseFormatError("OpenAI API 的 message 必须是对象")
    content = _openai_content_text(message.get("content"), "message.content")
    if not content:
        reasoning_content = _openai_content_text(
            message.get("reasoning_content"),
            "message.reasoning_content",
        )
        if response_has_complete_json(reasoning_content):
            content = reasoning_content
    if not content:
        content = _openai_content_text(choice.get("text"), "choice.text")
    if choice.get("finish_reason") == "length" and not response_has_complete_json(content):
        raise LLMResponseTruncatedError(
            f"{model} 输出被截断(max_tokens={max_tokens})，将缩短提示后重试"
        )
    if not content:
        raise LLMResponseFormatError("OpenAI API 响应没有可用文本内容")
    return content


def parse_anthropic_response(data: dict, model: str, max_tokens: int) -> str:
    """解析 Anthropic Messages 兼容响应。"""
    blocks = data.get("content")
    if not isinstance(blocks, list) or not blocks:
        if data.get("stop_reason") == "max_tokens":
            raise LLMResponseTruncatedError(
                f"{model} 输出被截断(max_tokens={max_tokens})，将缩短提示后重试"
            )
        raise LLMResponseFormatError("Anthropic API 响应缺少非空 content 数组")
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            raise LLMResponseFormatError("Anthropic API 的 content 块必须是对象")
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            raise LLMResponseFormatError("Anthropic API 的文本块缺少 text")
        parts.append(text)
    content = "\n".join(part for part in parts if part)
    if data.get("stop_reason") == "max_tokens" and not response_has_complete_json(content):
        raise LLMResponseTruncatedError(
            f"{model} 输出被截断(max_tokens={max_tokens})，将缩短提示后重试"
        )
    if not content:
        raise LLMResponseFormatError("Anthropic API 响应没有可用文本内容")
    return content


def _select_http_transport(config: Any, request_post: Optional[Callable]):
    """为三种代理策略选择唯一的 requests 发送边界。"""
    proxy_mode = normalise_proxy_mode(
        getattr(config, "proxy_mode", DEFAULT_PROXY_MODE)
    )
    request_kwargs = {}
    if proxy_mode == "custom":
        http_proxy = normalise_proxy_url(
            getattr(config, "http_proxy", None),
            "http_proxy",
        )
        https_proxy = normalise_proxy_url(
            getattr(config, "https_proxy", None),
            "https_proxy",
        )
        if http_proxy is None and https_proxy is None:
            raise ValueError(
                "custom 代理模式必须配置 http_proxy 或 https_proxy"
            )
        request_kwargs["proxies"] = {
            scheme: proxy_url
            for scheme, proxy_url in (
                ("http", http_proxy),
                ("https", https_proxy),
            )
            if proxy_url is not None
        }

    if request_post is not None:
        return request_post, request_kwargs, None

    session = requests.Session()
    session.trust_env = proxy_mode == "system"
    return session.post, request_kwargs, session.close


def _safe_transport_error(
        error: requests.RequestException, *, protocol: str,
        model: str) -> LLMTransportError:
    """把 requests 异常转换成不含 URL、token 或代理凭据的稳定错误。"""
    status = llm_http_status(error)
    if isinstance(error, requests.Timeout):
        message = "LLM HTTP 请求超时"
        category = RetryCategory.TRANSIENT_NETWORK
    elif isinstance(error, requests.ConnectionError):
        message = "LLM HTTP 连接失败"
        category = RetryCategory.TRANSIENT_NETWORK
    else:
        message = (
            f"LLM HTTP 请求失败（HTTP {status}）"
            if status
            else "LLM HTTP 请求失败"
        )
        category = classify_retry(error)
    return LLMTransportError(
        message,
        retry_category=category,
        protocol=protocol,
        model=model,
        status_code=status,
    )


def _post_http_request(
        request_post: Callable, request_url: str, *, protocol: str,
        model: str, request_kwargs: dict, **kwargs: Any):
    """OpenAI 与 Anthropic 共用的 HTTP POST 和异常脱敏边界。"""
    safe_error = None
    try:
        return request_post(
            request_url,
            **kwargs,
            **request_kwargs,
        )
    except requests.RequestException as exc:
        safe_error = _safe_transport_error(
            exc,
            protocol=protocol,
            model=model,
        )
    raise safe_error


def _raise_for_status(response: Any, *, protocol: str, model: str) -> None:
    """执行统一状态检查，同时阻止 requests 异常回显代理 URL。"""
    safe_error = None
    try:
        response.raise_for_status()
        return
    except requests.RequestException as exc:
        safe_error = _safe_transport_error(
            exc,
            protocol=protocol,
            model=model,
        )
    raise safe_error


def call_compatible_api(
        prompt: str, *, max_tokens: int, json_mode: bool,
        model_override: Optional[str], request_timeout: Any,
        load_config: Callable[[], Any],
        decode_response: Optional[Callable] = None,
        parse_openai: Optional[Callable] = None,
        parse_anthropic: Optional[Callable] = None,
        request_post: Optional[Callable] = None,
        reasoning_stage: Any = None) -> str:
    """发送一次兼容请求并用本模块的唯一解析器返回文本。"""
    config = load_config()
    base_url, token, configured_model = config
    api_type = getattr(config, "api_type", None) or infer_api_type(
        base_url,
        token,
    )
    api_type = normalise_protocol(api_type)
    model = str(model_override or configured_model).strip()
    stage = normalise_reasoning_stage(reasoning_stage)
    reasoning_effort = (
        getattr(config, f"{stage}_reasoning_effort", None)
        if stage
        else None
    )
    request = LLMRequest(
        prompt=prompt,
        model=model,
        protocol=api_type,
        max_tokens=max_tokens,
        timeout=normalise_timeout(request_timeout),
        json_mode=json_mode,
        reasoning_effort=reasoning_effort,
    )
    decode_response = decode_response or decode_response_json
    parse_openai = parse_openai or parse_openai_response
    parse_anthropic = parse_anthropic or parse_anthropic_response
    request_post, proxy_request_kwargs, close_transport = _select_http_transport(
        config,
        request_post,
    )
    try:
        if request.protocol == "openai":
            request_payload = {
                "model": request.model,
                "messages": [{"role": "user", "content": request.prompt}],
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
            }
            capability_key = _reasoning_effort_capability_key(base_url, request.model)
            send_reasoning_effort = bool(
                request.reasoning_effort
                and not _reasoning_effort_is_disabled(capability_key)
            )
            if send_reasoning_effort:
                request_payload["reasoning_effort"] = request.reasoning_effort
            if request.json_mode:
                request_payload["response_format"] = {"type": "json_object"}
            request_url = f"{str(base_url).strip().rstrip('/')}/chat/completions"
            request_headers = {
                "Authorization": _RedactedHeaderValue(f"Bearer {token}"),
                "Content-Type": "application/json",
            }

            def post_openai(payload):
                return _post_http_request(
                    request_post,
                    request_url,
                    protocol=request.protocol,
                    model=request.model,
                    request_kwargs=proxy_request_kwargs,
                    headers=request_headers,
                    json=dict(payload),
                    timeout=request.timeout.as_requests_timeout(),
                )

            response = post_openai(request_payload)
            if send_reasoning_effort and _response_rejects_reasoning_effort(response):
                _disable_reasoning_effort(capability_key)
                fallback_payload = dict(request_payload)
                fallback_payload.pop("reasoning_effort", None)
                response = post_openai(fallback_payload)
            _raise_for_status(
                response,
                protocol=request.protocol,
                model=request.model,
            )
            return parse_openai(
                decode_response(response, "OpenAI"),
                request.model,
                request.max_tokens,
            )

        response = _post_http_request(
            request_post,
            f"{str(base_url).strip().rstrip('/')}/messages",
            protocol=request.protocol,
            model=request.model,
            request_kwargs=proxy_request_kwargs,
            headers={
                "x-api-key": _RedactedHeaderValue(token),
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": request.model,
                "messages": [{"role": "user", "content": request.prompt}],
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
            },
            timeout=request.timeout.as_requests_timeout(),
        )
        _raise_for_status(
            response,
            protocol=request.protocol,
            model=request.model,
        )
        return parse_anthropic(
            decode_response(response, "Anthropic"),
            request.model,
            request.max_tokens,
        )
    finally:
        if close_transport is not None:
            close_transport()


def call_llm(
        prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS,
        json_mode: bool = False, model_override: Optional[str] = None,
        reasoning_stage: Any = None, *,
        request_timeout: Any = DEFAULT_REQUEST_TIMEOUT,
        load_config_func: Optional[Callable] = None,
        request_post: Optional[Callable] = None) -> str:
    """公开网关 seam；配置加载和 HTTP 均可由测试显式注入。"""
    return call_compatible_api(
        prompt,
        max_tokens=max_tokens,
        json_mode=json_mode,
        model_override=model_override,
        request_timeout=request_timeout,
        load_config=load_config_func or load_api_config,
        request_post=request_post,
        reasoning_stage=reasoning_stage,
    )


def llm_http_status(error: BaseException) -> Optional[int]:
    """从原生 HTTP 异常或结构化异常获取状态码。"""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(error, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def short_llm_error(error: BaseException) -> str:
    """生成不含 token、请求 prompt 或响应正文的单行错误摘要。"""
    status = llm_http_status(error)
    if status is not None:
        return f"HTTP {status}"
    return redact_url_credentials(str(error)).replace("\n", " ")[:200]


def is_provider_service_unavailable(error: BaseException) -> bool:
    """判断是否应进入跨并发批次的共享恢复流程。"""
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    return classify_retry(error) in {
        RetryCategory.PROVIDER_UNAVAILABLE,
        RetryCategory.TRANSIENT_NETWORK,
    }


def is_retryable_llm_error(error: BaseException) -> bool:
    """判断错误是否适合退避重试。"""
    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    return is_retryable_error(error)


class LLMProviderRetryCoordinator:
    """让同一并发阶段只由一个请求探测暂时不可用的上游。"""

    def __init__(self, delays=DEFAULT_PROVIDER_UNAVAILABLE_RETRY_DELAYS):
        self.delays = tuple(max(0, float(value)) for value in delays)
        self._state_lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._generation = 0
        self._retry_index = 0
        self._terminal_message = None

    def generation(self) -> int:
        with self._state_lock:
            return self._generation

    def _mark_recovered(self) -> None:
        with self._state_lock:
            self._generation += 1
            self._retry_index = 0
            self._terminal_message = None

    def _terminal_error(self, error: BaseException) -> LLMProviderUnavailableError:
        status = llm_http_status(error)
        if status:
            status_note = f"HTTP {status}"
        elif classify_retry(error) is RetryCategory.TRANSIENT_NETWORK:
            status_note = "上游连接被关闭、中断或超时"
        else:
            status_note = "上游错误"
        return LLMProviderUnavailableError(
            f"上游推理服务暂不可用（{status_note}），"
            f"已完成 {len(self.delays)} 次共享恢复探测；"
            "已完成的检查点会保留，请稍后直接重试。",
            status_code=status,
        )

    def recover(
            self, observed_generation: int, request_func: Callable,
            original_error: BaseException, sleep_func: Callable = time.sleep,
            progress_callback: Optional[Callable] = None,
            progress_label: str = "API", progress_step: int = 0):
        """串行恢复探测；等待者复用状态，不重复休眠和请求。"""
        with self._recovery_lock:
            with self._state_lock:
                if self._terminal_message:
                    raise LLMProviderUnavailableError(self._terminal_message)
                if self._generation != observed_generation:
                    return _RETRY_AFTER_SHARED_RECOVERY

            last_error = original_error
            while True:
                with self._state_lock:
                    retry_index = self._retry_index
                    if retry_index >= len(self.delays):
                        terminal = self._terminal_error(last_error)
                        self._terminal_message = str(terminal)
                        raise terminal
                    self._retry_index += 1

                delay = self.delays[retry_index]
                remaining_wait = int(sum(self.delays[retry_index:]))
                delay_label = int(delay) if delay.is_integer() else delay
                if progress_callback:
                    progress_callback(
                        f"{progress_label}：上游推理服务暂不可用，"
                        f"{delay_label}s 后统一探测 "
                        f"({retry_index + 1}/{len(self.delays)}，"
                        f"最多再等待 {remaining_wait}s): {short_llm_error(last_error)}",
                        progress_step,
                        100,
                    )
                sleep_func(delay_label)
                try:
                    result = request_func()
                except Exception as exc:
                    if is_provider_service_unavailable(exc):
                        last_error = exc
                        continue
                    self._mark_recovered()
                    raise
                self._mark_recovered()
                return result


def call_llm_with_retry(
        prompt: str, compact_prompt: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        compact_max_tokens: int = DEFAULT_COMPACT_MAX_TOKENS,
        attempts: Optional[int] = None, sleep_func: Callable = time.sleep,
        progress_callback: Optional[Callable] = None,
        progress_label: str = "API", progress_step: int = 0,
        require_json: bool = False,
        retry_coordinator: Optional[LLMProviderRetryCoordinator] = None,
        model_override: Optional[str] = None, reasoning_stage: Any = None,
        *, call_func: Optional[Callable] = None,
        retry_delays=DEFAULT_RETRY_DELAYS,
        provider_retry_delays=DEFAULT_PROVIDER_UNAVAILABLE_RETRY_DELAYS,
        json_extractor: Callable = extract_json_payload) -> str:
    """统一处理限流、5xx、连接中断、响应格式和结构化输出重试。"""
    retry_delays = tuple(retry_delays)
    provider_retry_delays = tuple(provider_retry_delays)
    total_attempts = attempts or (len(retry_delays) + 1)
    last_error = None
    regular_failures = 0
    provider_failures = 0
    provider_retry_limit = min(
        len(provider_retry_delays),
        max(0, total_attempts - 1),
    )
    for attempt in range(total_attempts):
        use_compact = compact_prompt is not None and (
            regular_failures >= 2
            or isinstance(last_error, (
                LLMResponseTruncatedError,
                LLMStructuredOutputError,
            ))
        )
        active_prompt = compact_prompt if use_compact else prompt
        active_tokens = compact_max_tokens if use_compact else max_tokens

        def request_once():
            call_kwargs = {
                "max_tokens": active_tokens,
                "json_mode": require_json,
            }
            if model_override:
                call_kwargs["model_override"] = model_override
            if reasoning_stage:
                call_kwargs["reasoning_stage"] = reasoning_stage
            result = (call_func or call_llm)(active_prompt, **call_kwargs)
            if require_json and json_extractor(result) is None:
                raise LLMStructuredOutputError(
                    "模型未返回完整 JSON，将改用紧凑提示重试"
                )
            return result

        observed_generation = (
            retry_coordinator.generation() if retry_coordinator else None
        )
        try:
            return request_once()
        except Exception as exc:
            last_error = exc
            if is_provider_service_unavailable(exc):
                if retry_coordinator:
                    recovered = retry_coordinator.recover(
                        observed_generation,
                        request_once,
                        exc,
                        sleep_func=sleep_func,
                        progress_callback=progress_callback,
                        progress_label=progress_label,
                        progress_step=progress_step,
                    )
                    if recovered is _RETRY_AFTER_SHARED_RECOVERY:
                        continue
                    return recovered
                if provider_failures >= provider_retry_limit:
                    raise LLMProviderUnavailableError(
                        "上游推理服务暂不可用，"
                        f"已完成 {provider_retry_limit} 次恢复探测；"
                        "请稍后直接重试，已完成的检查点不会丢失。",
                        status_code=llm_http_status(exc),
                    ) from exc
                delay = provider_retry_delays[provider_failures]
                provider_failures += 1
                remaining_wait = sum(
                    provider_retry_delays[
                        provider_failures - 1:provider_retry_limit
                    ]
                )
                if progress_callback:
                    progress_callback(
                        f"{progress_label}：上游推理服务暂不可用，"
                        f"{delay}s 后探测 "
                        f"({provider_failures}/{provider_retry_limit}，"
                        f"最多再等待 {remaining_wait}s): {short_llm_error(exc)}",
                        progress_step,
                        100,
                    )
                sleep_func(delay)
                continue
            if not is_retryable_llm_error(exc) or attempt >= total_attempts - 1:
                raise
            delay = retry_delays[min(regular_failures, len(retry_delays) - 1)]
            regular_failures += 1
            compact_note = "，改用紧凑提示" if use_compact else ""
            if progress_callback:
                progress_callback(
                    f"{progress_label} 失败{compact_note}，{delay}s 后重试 "
                    f"({regular_failures}/{total_attempts - 1}): {short_llm_error(exc)}",
                    progress_step,
                    100,
                )
            sleep_func(delay)
    raise last_error


__all__ = [
    "DEFAULT_COMPACT_MAX_TOKENS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_PROVIDER_UNAVAILABLE_RETRY_DELAYS",
    "DEFAULT_REASONING_EFFORT",
    "DEFAULT_REQUEST_TIMEOUT",
    "DEFAULT_RETRY_DELAYS",
    "LLMApiConfig",
    "LLMProviderRetryCoordinator",
    "LLMProviderUnavailableError",
    "LLMResponseFormatError",
    "LLMResponseTruncatedError",
    "LLMStructuredOutputError",
    "LLMTimeout",
    "call_compatible_api",
    "call_llm",
    "call_llm_with_retry",
    "decode_response_json",
    "extract_json_payload",
    "infer_api_type",
    "is_provider_service_unavailable",
    "is_retryable_llm_error",
    "llm_http_status",
    "load_api_config",
    "normalise_api_config",
    "normalise_reasoning_effort",
    "normalise_timeout",
    "parse_anthropic_response",
    "parse_openai_response",
    "read_json_config",
    "reset_reasoning_effort_capability_cache",
    "response_has_complete_json",
    "short_llm_error",
]
