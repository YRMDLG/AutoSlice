"""LLM configuration and OpenAI/Anthropic compatible HTTP transport."""

import json
import os
import threading
from urllib.parse import urlsplit

import requests


_REASONING_EFFORT_VALUES = {"minimal", "low", "medium", "high", "xhigh"}
_REASONING_EFFORT_ALIASES = {"max": "xhigh"}
_DEFAULT_REASONING_EFFORT = "xhigh"
_UNSUPPORTED_REASONING_EFFORT_KEYS = set()
_REASONING_EFFORT_LOCK = threading.Lock()


def normalise_reasoning_effort(value, *, default=None):
    """校验可选推理强度；max 是 API 最高档 xhigh 的易读别名。"""
    selected = default if value is None else value
    if selected is None:
        return None
    effort = str(selected).strip().casefold()
    if effort in {"", "none", "off", "default", "false"}:
        return None
    effort = _REASONING_EFFORT_ALIASES.get(effort, effort)
    if effort not in _REASONING_EFFORT_VALUES:
        raise ValueError(
            "reasoning effort 只支持 minimal、low、medium、high、xhigh、max 或 none"
        )
    return effort


def reset_reasoning_effort_capability_cache():
    """清空进程内兼容性记录，主要供配置切换和测试使用。"""
    with _REASONING_EFFORT_LOCK:
        _UNSUPPORTED_REASONING_EFFORT_KEYS.clear()


def _reasoning_effort_capability_key(base_url, model):
    return str(base_url).casefold(), str(model).casefold()


def _reasoning_effort_is_disabled(key):
    with _REASONING_EFFORT_LOCK:
        return key in _UNSUPPORTED_REASONING_EFFORT_KEYS


def _disable_reasoning_effort(key):
    with _REASONING_EFFORT_LOCK:
        _UNSUPPORTED_REASONING_EFFORT_KEYS.add(key)


def _response_rejects_reasoning_effort(response):
    """只在服务端明确指出该字段不受支持时降级，避免吞掉其他 4xx。"""
    if getattr(response, "status_code", None) not in {400, 422}:
        return False
    try:
        text = str(response.text or "").casefold()
    except (AttributeError, TypeError, ValueError):
        return False
    return "reasoning_effort" in text or "reasoning effort" in text


class LLMApiConfig:
    """Keep legacy tuple unpacking while carrying the selected API protocol."""

    __slots__ = (
        "base_url", "token", "model", "api_type",
        "analysis_reasoning_effort", "review_reasoning_effort",
    )

    def __init__(
            self, base_url, token, model, api_type,
            analysis_reasoning_effort=_DEFAULT_REASONING_EFFORT,
            review_reasoning_effort=_DEFAULT_REASONING_EFFORT):
        self.base_url = base_url
        self.token = token
        self.model = model
        self.api_type = api_type
        self.analysis_reasoning_effort = normalise_reasoning_effort(
            analysis_reasoning_effort,
        )
        self.review_reasoning_effort = normalise_reasoning_effort(
            review_reasoning_effort,
        )

    def __iter__(self):
        return iter((self.base_url, self.token, self.model))

    def __len__(self):
        return 3

    def __getitem__(self, index):
        return (self.base_url, self.token, self.model)[index]


def infer_api_type(base_url, token):
    """Infer the protocol only for legacy configurations."""
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
        payload, source, *, default_model, default_api_type=None):
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
        aliases = {
            "openai": "openai",
            "openai-compatible": "openai",
            "chat-completions": "openai",
            "anthropic": "anthropic",
            "anthropic-compatible": "anthropic",
            "messages": "anthropic",
        }
        api_type = aliases.get(str(raw_api_type).strip().casefold())
        if api_type is None:
            raise ValueError(
                f"API 配置 api_type 只支持 openai 或 anthropic：{source}"
            )
    analysis_reasoning_effort = normalise_reasoning_effort(
        payload["analysis_reasoning_effort"]
        if "analysis_reasoning_effort" in payload
        else _DEFAULT_REASONING_EFFORT,
    )
    review_reasoning_effort = normalise_reasoning_effort(
        payload["review_reasoning_effort"]
        if "review_reasoning_effort" in payload
        else _DEFAULT_REASONING_EFFORT,
    )
    return LLMApiConfig(
        base_url,
        token,
        model,
        api_type,
        analysis_reasoning_effort=analysis_reasoning_effort,
        review_reasoning_effort=review_reasoning_effort,
    )


def read_json_config(path, *, json_loader=json.load):
    try:
        with open(path, encoding="utf-8") as file:
            return json_loader(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 API 配置文件：{path}") from exc


def load_api_config(
        *, project_dir, default_model, path_module=os.path,
        json_loader=json.load, environ=None):
    """Load explicit AutoSlice environment variables or the project config."""
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
        return normalise_api_config(
            {
                "base_url": environ.get("AUTOSLICE_API_BASE_URL"),
                "token": environ.get("AUTOSLICE_API_TOKEN"),
                "model": env_model or default_model,
                "api_type": environ.get("AUTOSLICE_API_TYPE"),
                "analysis_reasoning_effort": (
                    env_analysis_effort
                    if "AUTOSLICE_ANALYSIS_REASONING_EFFORT" in environ
                    else _DEFAULT_REASONING_EFFORT
                ),
                "review_reasoning_effort": (
                    env_review_effort
                    if "AUTOSLICE_REVIEW_REASONING_EFFORT" in environ
                    else _DEFAULT_REASONING_EFFORT
                ),
            },
            "环境变量 AUTOSLICE_API_*",
            default_model=default_model,
        )

    auto_cfg = path_module.join(project_dir, "api_config.json")
    if path_module.exists(auto_cfg):
        payload = read_json_config(auto_cfg, json_loader=json_loader)
        if env_model and isinstance(payload, dict):
            payload = dict(payload)
            payload["model"] = env_model
        if isinstance(payload, dict):
            if "AUTOSLICE_ANALYSIS_REASONING_EFFORT" in environ:
                payload = dict(payload)
                payload["analysis_reasoning_effort"] = env_analysis_effort
            if "AUTOSLICE_REVIEW_REASONING_EFFORT" in environ:
                payload = dict(payload)
                payload["review_reasoning_effort"] = env_review_effort
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


def call_compatible_api(
        prompt, *, max_tokens, json_mode, model_override, request_timeout,
        load_config, decode_response, parse_openai, parse_anthropic,
        request_post=requests.post, reasoning_stage=None):
    """Send one request and delegate provider response parsing to the facade."""
    config = load_config()
    base_url, token, configured_model = config
    api_type = getattr(config, "api_type", None) or infer_api_type(
        str(base_url),
        str(token),
    )
    base_url = str(base_url).strip().rstrip("/")
    model = str(model_override or configured_model).strip()
    if not model:
        raise ValueError("LLM model 不能为空")
    if reasoning_stage not in {None, "analysis", "review"}:
        raise ValueError("reasoning_stage 只支持 analysis 或 review")
    reasoning_effort = (
        getattr(config, f"{reasoning_stage}_reasoning_effort", None)
        if reasoning_stage
        else None
    )

    if api_type == "openai":
        request_payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        capability_key = _reasoning_effort_capability_key(base_url, model)
        send_reasoning_effort = bool(
            reasoning_effort
            and not _reasoning_effort_is_disabled(capability_key)
        )
        if send_reasoning_effort:
            request_payload["reasoning_effort"] = reasoning_effort
        if json_mode:
            request_payload["response_format"] = {"type": "json_object"}
        request_url = f"{base_url}/chat/completions"
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        def post_openai(payload):
            return request_post(
                request_url,
                headers=request_headers,
                json=dict(payload),
                timeout=request_timeout,
                proxies={"http": None, "https": None},
            )

        response = post_openai(request_payload)
        if send_reasoning_effort and _response_rejects_reasoning_effort(response):
            _disable_reasoning_effort(capability_key)
            fallback_payload = dict(request_payload)
            fallback_payload.pop("reasoning_effort", None)
            response = post_openai(fallback_payload)
        response.raise_for_status()
        return parse_openai(
            decode_response(response, "OpenAI"),
            model,
            max_tokens,
        )

    if api_type == "anthropic":
        response = request_post(
            f"{base_url}/messages",
            headers={
                "x-api-key": token,
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=request_timeout,
            proxies={"http": None, "https": None},
        )
        response.raise_for_status()
        return parse_anthropic(
            decode_response(response, "Anthropic"),
            model,
            max_tokens,
        )

    raise ValueError(f"不支持的 LLM API 协议：{api_type}")
