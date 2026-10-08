"""AI 接口配置：设置页读写 api_config.json，并提供看图模型名。

沿用 AutoSlice 已有的 api_config.json（源码运行时在仓库根目录，已被 git 忽略；
安装版在用户数据目录）。看图模型存为额外字段 vision_model，旧代码会忽略它。
环境变量 AUTOSLICE_API_* 优先于文件，此时设置页只读。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from autoslice.llm.contracts import is_loopback_host
from autoslice.llm.transport import PROJECT_DIR, load_api_config, normalise_api_config

from .foundation import DesktopStorage

# 用户中转上常用的两个模型，界面里作候选，可改成任意名字。
SUGGESTED_MODELS = ("gpt-5.6-terra", "gpt-5.6-luna")
_ENV_KEYS = ("AUTOSLICE_API_BASE_URL", "AUTOSLICE_API_TOKEN")
# 粘贴完整接口地址时，按结尾判断接口类型并截成基础地址。
_ENDPOINT_SUFFIXES = (
    ("/responses", "openai-responses"),
    ("/chat/completions", "openai"),
    ("/messages", "anthropic"),
)


def split_endpoint(url: str, api_type: str = "") -> tuple[str, str]:
    """“…/v1/responses” → (“…/v1”, “openai-responses”)；只到 /v1 时沿用给定类型。"""

    value = str(url or "").strip().rstrip("/")
    for suffix, kind in _ENDPOINT_SUFFIXES:
        if value.casefold().endswith(suffix):
            return value[: -len(suffix)], kind
    return value, api_type or "openai-responses"


def is_plain_http(url: str) -> bool:
    """非本机的 http 地址：密钥会明文传输。"""

    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    return parsed.scheme.casefold() == "http" and not is_loopback_host(parsed.hostname)


@dataclass(frozen=True, slots=True)
class AISettings:
    base_url: str = ""
    api_type: str = "openai"
    has_token: bool = False
    text_model: str = ""
    vision_model: str = ""
    allow_insecure_http: bool = False
    # "file" / "env" / "none"
    source: str = "none"
    error: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.has_token and self.text_model)


def ai_config_path() -> Path:
    return Path(PROJECT_DIR) / "api_config.json"


def _read_payload(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_ai_settings(path: Path | None = None, environ: dict | None = None) -> AISettings:
    """当前配置的摘要；密钥只报告有没有，不返回内容。"""

    environ = os.environ if environ is None else environ
    path = path or ai_config_path()
    payload = _read_payload(path)
    vision = str(payload.get("vision_model") or "").strip()
    if any(environ.get(key) for key in _ENV_KEYS):
        try:
            config = load_api_config(environ=environ)
        except ValueError as exc:
            return AISettings(source="env", error=str(exc))
        return AISettings(
            base_url=config.base_url, api_type=config.api_type, has_token=True,
            text_model=config.model, vision_model=vision or config.model,
            allow_insecure_http=config.allow_insecure_http, source="env",
        )
    if not payload:
        return AISettings()
    model = str(payload.get("model") or "").strip()
    return AISettings(
        base_url=str(payload.get("base_url") or "").strip(),
        api_type=str(payload.get("api_type") or payload.get("protocol") or "openai").strip() or "openai",
        has_token=bool(str(payload.get("token") or "").strip()),
        text_model=model, vision_model=vision or model,
        allow_insecure_http=payload.get("allow_insecure_http") is True, source="file",
    )


def save_ai_settings(
    *,
    base_url: str,
    api_type: str,
    token: str,
    text_model: str,
    vision_model: str,
    allow_insecure_http: bool = False,
    path: Path | None = None,
) -> Path:
    """写回 api_config.json：保留文件里其他字段；密钥留空表示沿用已保存的。

    base_url 可以是完整接口地址（…/responses 等），会拆成基础地址和接口类型。
    """

    path = path or ai_config_path()
    payload = _read_payload(path)
    token = token.strip() or str(payload.get("token") or "").strip()
    base_url, api_type = split_endpoint(base_url, api_type.strip())
    payload.update({
        "base_url": base_url,
        "api_type": api_type,
        "allow_insecure_http": bool(allow_insecure_http),
        "token": token,
        "model": text_model.strip(),
        "vision_model": vision_model.strip() or text_model.strip(),
    })
    payload.pop("protocol", None)
    # 先按正式规则校验，避免写进一份读不出来的配置。
    normalise_api_config(payload, str(path), default_model="")
    DesktopStorage._write_json(path, payload)
    return path


def vision_model_name(path: Path | None = None) -> str:
    """看图用的模型；没单独配置时用文字模型。"""

    settings = read_ai_settings(path)
    return settings.vision_model or settings.text_model


def _red_square_png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (220, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def check_connection(llm=None) -> tuple[str, ...]:
    """文字模型、看图模型各问一句；返回给人看的结果，某个失败不影响另一个。"""

    if llm is None:
        from autoslice.llm.transport import call_llm as llm
    settings = read_ai_settings()
    if not settings.configured:
        return ("还没配置完整：需要接口地址、密钥和文字模型。",)
    lines = []
    # 推理模型的思考也算输出额度，给足，免得只思考没回答。
    for label, model, prompt, images in (
        ("文字模型", settings.text_model, "只回复两个字：可用", ()),
        ("看图模型", settings.vision_model, "这张图主要是什么颜色？只回答颜色。", (("image/png", _red_square_png()),)),
    ):
        try:
            answer = llm(prompt, max_tokens=1024, model_override=model, images=images)
        except Exception as exc:  # noqa: BLE001 - 连接测试要把任何失败原因展示给用户
            lines.append(f"✗ {label} {model}：{str(exc)[:160]}")
        else:
            lines.append(f"✓ {label} {model}：{answer.strip()[:40]}")
    return tuple(lines)
