"""LLM 网关的传输无关请求、响应与错误契约。

本模块只描述协议边界，不读取运行配置、不构造业务提示词，也不执行 HTTP。
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlsplit

DEFAULT_REASONING_EFFORT = "xhigh"
DEFAULT_PROXY_MODE = "direct"


class LLMProtocol(str, Enum):
    """网关支持的兼容协议。"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class ReasoningEffort(str, Enum):
    """服务端支持的推理强度，``xhigh`` 为当前最高档。"""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class ReasoningStage(str, Enum):
    """用于从配置选择推理强度的请求阶段。"""

    ANALYSIS = "analysis"
    REVIEW = "review"


class RetryCategory(str, Enum):
    """供重试策略消费的稳定错误分类。"""

    NOT_RETRYABLE = "not_retryable"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TRANSIENT_NETWORK = "transient_network"
    RESPONSE_FORMAT = "response_format"
    RESPONSE_TRUNCATED = "response_truncated"
    STRUCTURED_OUTPUT = "structured_output"


_PROTOCOL_ALIASES = {
    "openai": LLMProtocol.OPENAI.value,
    "openai-compatible": LLMProtocol.OPENAI.value,
    "chat-completions": LLMProtocol.OPENAI.value,
    "anthropic": LLMProtocol.ANTHROPIC.value,
    "anthropic-compatible": LLMProtocol.ANTHROPIC.value,
    "messages": LLMProtocol.ANTHROPIC.value,
}
_REASONING_EFFORT_VALUES = {item.value for item in ReasoningEffort}
_REASONING_EFFORT_ALIASES = {"max": ReasoningEffort.XHIGH.value}
_DISABLED_REASONING_EFFORT_VALUES = {
    "", "none", "off", "default", "false",
}
_RETRYABLE_CATEGORIES = {
    RetryCategory.RATE_LIMITED,
    RetryCategory.SERVER_ERROR,
    RetryCategory.PROVIDER_UNAVAILABLE,
    RetryCategory.TRANSIENT_NETWORK,
    RetryCategory.RESPONSE_FORMAT,
    RetryCategory.RESPONSE_TRUNCATED,
    RetryCategory.STRUCTURED_OUTPUT,
}
_PROXY_MODES = {"direct", "system", "custom"}


class _RedactedProxyUrl(str):
    """保留真实 URL 供 requests 使用，但在容器和 mock repr 中隐藏凭据。"""

    def __repr__(self) -> str:
        return repr(redact_url_credentials(self))


def redact_url_credentials(value: Any) -> str:
    """隐藏任意文本内 HTTP(S) URL 的 userinfo。"""
    text = str(value or "")
    return re.sub(
        r"(?i)(https?://)[^\s/?#]*@",
        r"\1***:***@",
        text,
    )


def normalise_proxy_mode(value: Any) -> str:
    """归一化 LLM 代理策略，缺省时保持历史上的直连语义。"""
    mode = str(value or DEFAULT_PROXY_MODE).strip().casefold()
    if mode not in _PROXY_MODES:
        raise ValueError("LLM proxy_mode 只支持 direct、system 或 custom")
    return mode


def normalise_proxy_url(value: Any, label: str) -> Optional[str]:
    """校验显式代理 URL，不允许非 HTTP(S) 协议或代理端点路径。"""
    if value is None or not str(value).strip():
        return None
    proxy_url = str(value).strip()
    if any(character.isspace() for character in proxy_url):
        raise ValueError(f"{label} 必须是有效的 HTTP(S) 代理 URL")
    try:
        parsed = urlsplit(proxy_url)
        valid_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是有效的 HTTP(S) 代理 URL") from exc
    if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or (valid_port is not None and not 1 <= valid_port <= 65535)
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment):
        raise ValueError(f"{label} 必须是有效的 HTTP(S) 代理 URL")
    return _RedactedProxyUrl(proxy_url)


def normalise_protocol(value: Any) -> str:
    """把 OpenAI/Anthropic 兼容别名归一化为稳定协议名。"""
    if isinstance(value, LLMProtocol):
        return value.value
    protocol = _PROTOCOL_ALIASES.get(str(value or "").strip().casefold())
    if protocol is None:
        raise ValueError("LLM 协议只支持 openai 或 anthropic")
    return protocol


def normalise_reasoning_effort(
        value: Any, *, default: Any = None) -> Optional[str]:
    """归一化推理强度；``max`` 是最高档 ``xhigh`` 的别名。"""
    selected = default if value is None else value
    if selected is None:
        return None
    if isinstance(selected, ReasoningEffort):
        return selected.value
    effort = str(selected).strip().casefold()
    if effort in _DISABLED_REASONING_EFFORT_VALUES:
        return None
    effort = _REASONING_EFFORT_ALIASES.get(effort, effort)
    if effort not in _REASONING_EFFORT_VALUES:
        raise ValueError(
            "reasoning effort 只支持 minimal、low、medium、high、xhigh、max 或 none"
        )
    return effort


def normalise_reasoning_stage(value: Any) -> Optional[str]:
    """归一化可选请求阶段。"""
    if value is None:
        return None
    if isinstance(value, ReasoningStage):
        return value.value
    stage = str(value).strip().casefold()
    if stage not in {item.value for item in ReasoningStage}:
        raise ValueError("reasoning_stage 只支持 analysis 或 review")
    return stage


@dataclass(frozen=True)
class LLMTimeout:
    """连接与读取超时，单位为秒。"""

    connect: float
    read: float

    def __post_init__(self) -> None:
        connect = _positive_seconds(self.connect, "连接超时")
        read = _positive_seconds(self.read, "读取超时")
        object.__setattr__(self, "connect", connect)
        object.__setattr__(self, "read", read)

    def as_requests_timeout(self) -> tuple[float, float]:
        """返回 ``requests`` 接受的二元超时值，不导入传输库。"""
        return self.connect, self.read


def _positive_seconds(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是正数")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是正数") from exc
    if seconds <= 0:
        raise ValueError(f"{label}必须是正数")
    return seconds


def normalise_timeout(value: Any) -> LLMTimeout:
    """把单值或 ``(connect, read)`` 归一化为不可变超时契约。"""
    if isinstance(value, LLMTimeout):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return LLMTimeout(value, value)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return LLMTimeout(value[0], value[1])
    raise ValueError("LLM timeout 必须是正数或 (connect, read) 二元值")


@dataclass(frozen=True)
class LLMApiConfig:
    """端点、凭据、模型和推理强度配置。

    保留三元组解包行为，兼容现有调用方的
    ``base_url, token, model = config`` 用法。
    """

    base_url: str
    token: str = field(repr=False)
    model: str
    api_type: str
    analysis_reasoning_effort: Optional[str] = DEFAULT_REASONING_EFFORT
    review_reasoning_effort: Optional[str] = DEFAULT_REASONING_EFFORT
    proxy_mode: str = DEFAULT_PROXY_MODE
    http_proxy: Optional[str] = field(default=None, repr=False)
    https_proxy: Optional[str] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        base_url = str(self.base_url or "").strip().rstrip("/")
        token = str(self.token or "").strip()
        model = str(self.model or "").strip()
        if not base_url:
            raise ValueError("LLM base_url 不能为空")
        if not token:
            raise ValueError("LLM token 不能为空")
        if not model:
            raise ValueError("LLM model 不能为空")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "token", token)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "api_type", normalise_protocol(self.api_type))
        object.__setattr__(
            self,
            "analysis_reasoning_effort",
            normalise_reasoning_effort(self.analysis_reasoning_effort),
        )
        object.__setattr__(
            self,
            "review_reasoning_effort",
            normalise_reasoning_effort(self.review_reasoning_effort),
        )
        proxy_mode = normalise_proxy_mode(self.proxy_mode)
        object.__setattr__(self, "proxy_mode", proxy_mode)
        if proxy_mode == "custom":
            http_proxy = normalise_proxy_url(self.http_proxy, "http_proxy")
            https_proxy = normalise_proxy_url(self.https_proxy, "https_proxy")
            if http_proxy is None and https_proxy is None:
                raise ValueError(
                    "custom 代理模式必须配置 http_proxy 或 https_proxy"
                )
        else:
            http_proxy = None
            https_proxy = None
        object.__setattr__(self, "http_proxy", http_proxy)
        object.__setattr__(self, "https_proxy", https_proxy)

    def __iter__(self):
        return iter((self.base_url, self.token, self.model))

    def __len__(self) -> int:
        return 3

    def __getitem__(self, index):
        return (self.base_url, self.token, self.model)[index]

    def reasoning_effort_for(self, stage: Any) -> Optional[str]:
        selected_stage = normalise_reasoning_stage(stage)
        if selected_stage is None:
            return None
        return getattr(self, f"{selected_stage}_reasoning_effort")


@dataclass(frozen=True)
class LLMRequest:
    """一次已经解析完端点配置的通用 LLM 请求。"""

    prompt: str
    model: str
    protocol: str
    max_tokens: int
    timeout: LLMTimeout
    json_mode: bool = False
    reasoning_effort: Optional[str] = None
    temperature: float = 0.3

    def __post_init__(self) -> None:
        prompt = str(self.prompt)
        model = str(self.model or "").strip()
        if not prompt.strip():
            raise ValueError("LLM prompt 不能为空")
        if not model:
            raise ValueError("LLM model 不能为空")
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise ValueError("LLM max_tokens 必须是正整数")
        if self.max_tokens <= 0:
            raise ValueError("LLM max_tokens 必须是正整数")
        try:
            temperature = float(self.temperature)
        except (TypeError, ValueError) as exc:
            raise ValueError("LLM temperature 必须是 0 到 2 的数字") from exc
        if not 0 <= temperature <= 2:
            raise ValueError("LLM temperature 必须是 0 到 2 的数字")
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "protocol", normalise_protocol(self.protocol))
        object.__setattr__(self, "timeout", normalise_timeout(self.timeout))
        object.__setattr__(
            self,
            "reasoning_effort",
            normalise_reasoning_effort(self.reasoning_effort),
        )
        object.__setattr__(self, "temperature", temperature)


@dataclass(frozen=True)
class LLMResponse:
    """网关返回给业务层的最小、可测试响应。"""

    text: str
    model: str
    protocol: str
    finish_reason: Optional[str] = None

    def __post_init__(self) -> None:
        text = str(self.text)
        model = str(self.model or "").strip()
        if not text:
            raise ValueError("LLM response text 不能为空")
        if not model:
            raise ValueError("LLM response model 不能为空")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "protocol", normalise_protocol(self.protocol))


class LLMGatewayError(RuntimeError):
    """带稳定错误码、重试分类和安全上下文的网关异常。"""

    code = "llm_gateway_error"
    retry_category = RetryCategory.NOT_RETRYABLE

    def __init__(
            self, message: str, *, protocol: Any = None,
            model: Optional[str] = None, status_code: Optional[int] = None):
        super().__init__(str(message))
        self.protocol = (
            normalise_protocol(protocol) if protocol is not None else None
        )
        self.model = str(model).strip() if model is not None else None
        self.status_code = int(status_code) if status_code is not None else None

    @property
    def retryable(self) -> bool:
        return self.retry_category in _RETRYABLE_CATEGORIES

    def to_dict(self) -> dict[str, Any]:
        """生成不含 token、请求正文和底层异常详情的安全结构。"""
        payload = {
            "code": self.code,
            "message": str(self),
            "retry_category": self.retry_category.value,
            "retryable": self.retryable,
        }
        if self.protocol is not None:
            payload["protocol"] = self.protocol
        if self.model:
            payload["model"] = self.model
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        return payload


class LLMTransportError(LLMGatewayError):
    """HTTP 或连接层失败。"""

    code = "llm_transport_error"

    def __init__(self, message: str, *, retry_category=RetryCategory.NOT_RETRYABLE,
                 **context: Any):
        super().__init__(message, **context)
        if not isinstance(retry_category, RetryCategory):
            retry_category = RetryCategory(str(retry_category))
        self.retry_category = retry_category


class LLMResponseFormatError(LLMGatewayError):
    """上游成功响应不符合所选兼容协议。"""

    code = "llm_response_format"
    retry_category = RetryCategory.RESPONSE_FORMAT


class LLMResponseTruncatedError(LLMGatewayError):
    """模型因输出额度耗尽而未返回完整内容。"""

    code = "llm_response_truncated"
    retry_category = RetryCategory.RESPONSE_TRUNCATED


class LLMStructuredOutputError(LLMGatewayError):
    """模型返回文本，但没有完整的业务所需结构。"""

    code = "llm_structured_output"
    retry_category = RetryCategory.STRUCTURED_OUTPUT


class LLMProviderUnavailableError(LLMGatewayError):
    """上游服务在有限恢复探测后仍不可用。"""

    code = "llm_provider_unavailable"
    retry_category = RetryCategory.PROVIDER_UNAVAILABLE


def http_retry_category(status_code: Any) -> RetryCategory:
    """按 HTTP 状态码给出稳定重试分类。"""
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        return RetryCategory.NOT_RETRYABLE
    if status == 429:
        return RetryCategory.RATE_LIMITED
    if status in {502, 503, 504}:
        return RetryCategory.PROVIDER_UNAVAILABLE
    if 500 <= status < 600:
        return RetryCategory.SERVER_ERROR
    return RetryCategory.NOT_RETRYABLE


def classify_retry(error: BaseException) -> RetryCategory:
    """对结构化网关异常、HTTP 异常和连接异常进行无副作用分类。"""
    if isinstance(error, LLMGatewayError):
        return error.retry_category
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        status_code = getattr(error, "status_code", None)
    status_category = http_retry_category(status_code)
    if status_category is not RetryCategory.NOT_RETRYABLE:
        return status_category
    if isinstance(error, (ConnectionError, TimeoutError)):
        return RetryCategory.TRANSIENT_NETWORK
    return RetryCategory.NOT_RETRYABLE


def is_retryable_error(error: BaseException) -> bool:
    """判断错误是否属于网关允许退避重试的分类。"""
    return classify_retry(error) in _RETRYABLE_CATEGORIES
