"""公开版运行目录配置。

所有默认目录都位于项目内，并可通过环境变量覆盖。这里不读取注册表、
其他开发工具配置或用户私有目录，确保克隆后的行为可预测。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG_ENV_NAME = "AUTOSLICE_LOCAL_CONFIG"


def resolve_local_config_path(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """解析本机配置路径，便于测试和多实例显式隔离。"""

    source = environ if environ is not None else os.environ
    configured = str(source.get(LOCAL_CONFIG_ENV_NAME, "")).strip()
    path = (
        Path(configured).expanduser()
        if configured
        else PROJECT_DIR / "autoslice.local.json"
    )
    return path.resolve()


LOCAL_CONFIG_PATH = resolve_local_config_path()


_LOCAL_ENVIRONMENT_KEYS = frozenset({
    "AUTOSLICE_VIDEO_DIR",
    "AUTOSLICE_OUTPUT_DIR",
    "AUTOSLICE_TIMELINE_DIR",
    "AUTOSLICE_SUBMISSION_DIR",
    "AUTOSLICE_REFINEMENT_QUEUE_DIR",
    "AUTOSLICE_TITLE_STYLE_PROFILE",
    "AUTOSLICE_AUTOCOVER_DIR",
    "AUTOSLICE_FUNASR_DEVICE",
    "AUTOSLICE_FUNASR_HOTWORDS",
    "AUTOSLICE_FUNASR_MODEL_DIR",
    "AUTOSLICE_FUNASR_VAD_DIR",
    "AUTOSLICE_FUNASR_PUNC_DIR",
    "AUTOSLICE_LLM_PROXY_MODE",
    "AUTOSLICE_LLM_PROXY_HTTP",
    "AUTOSLICE_LLM_PROXY_HTTPS",
    "AUTOCOVER_INPUT_DIR",
    "AUTOCOVER_OUTPUT_DIR",
    "AUTOCOVER_STICKER_DIR",
    "AUTOCOVER_FONT_PATH",
})


def _read_local_environment() -> dict[str, str]:
    """读取可选本机目录配置，不让私有路径进入公开仓库。"""

    if not LOCAL_CONFIG_PATH.is_file():
        return {}
    try:
        payload = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    environment: dict[str, str] = {}
    for key, value in payload.items():
        if key not in _LOCAL_ENVIRONMENT_KEYS or not isinstance(value, str):
            continue
        cleaned = value.strip()
        if cleaned:
            environment[key] = cleaned
    return environment


LOCAL_ENVIRONMENT = _read_local_environment()


def configured_value(env_name: str, relative_default: str) -> str:
    """环境变量优先，其次本机私有配置，最后才使用公开默认目录。"""

    return (
        str(os.environ.get(env_name, "")).strip()
        or LOCAL_ENVIRONMENT.get(env_name, "")
        or str(PROJECT_DIR / relative_default)
    )


def configured_path(env_name: str, relative_default: str) -> Path:
    """返回环境变量指定目录，否则使用项目内相对目录。"""
    path = Path(configured_value(env_name, relative_default)).expanduser()
    return path.resolve()


def apply_local_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """把本机配置补入进程环境，供启动器子进程共享；显式环境变量优先。"""

    target = environ if environ is not None else os.environ
    for key, value in LOCAL_ENVIRONMENT.items():
        target.setdefault(key, value)
    return target


VIDEO_DIR = configured_path("AUTOSLICE_VIDEO_DIR", "recordings")
OUTPUT_DIR = configured_path("AUTOSLICE_OUTPUT_DIR", "output")
TIMELINE_DIR = configured_path("AUTOSLICE_TIMELINE_DIR", "timelines")
SUBMISSION_DIR = configured_path("AUTOSLICE_SUBMISSION_DIR", "submissions")
AUTOCOVER_DIR = configured_path("AUTOSLICE_AUTOCOVER_DIR", "autocover_tool")
COVER_OUTPUT_DIR = configured_path("AUTOCOVER_OUTPUT_DIR", "covers")
STICKER_DIR = configured_path("AUTOCOVER_STICKER_DIR", "stickers")
AUTOCOVER_INPUT_DIR = Path(
    configured_value("AUTOCOVER_INPUT_DIR", "output")
).expanduser().resolve()

_private_title_profile = PROJECT_DIR / "title_style_profile.json"
TITLE_STYLE_PROFILE = (
    Path(os.environ["AUTOSLICE_TITLE_STYLE_PROFILE"]).expanduser().resolve()
    if str(os.environ.get("AUTOSLICE_TITLE_STYLE_PROFILE", "")).strip()
    else (
        _private_title_profile.resolve()
        if _private_title_profile.is_file()
        else (PROJECT_DIR / "title_style_profile.example.json").resolve()
    )
)


def template_defaults() -> dict[str, str]:
    """返回 Web 页面需要的公开路径默认值。"""
    return {
        "default_video_dir": str(VIDEO_DIR),
        "default_output_dir": str(OUTPUT_DIR),
        "default_timeline_dir": str(TIMELINE_DIR),
        "default_submission_dir": str(SUBMISSION_DIR),
    }
