"""公开版运行目录配置。

所有默认目录都位于项目内，并可通过环境变量覆盖。这里不读取注册表、
其他开发工具配置或用户私有目录，确保克隆后的行为可预测。
"""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent


def configured_path(env_name: str, relative_default: str) -> Path:
    """返回环境变量指定目录，否则使用项目内相对目录。"""
    configured = str(os.environ.get(env_name, "")).strip()
    path = Path(configured).expanduser() if configured else PROJECT_DIR / relative_default
    return path.resolve()


VIDEO_DIR = configured_path("AUTOSLICE_VIDEO_DIR", "recordings")
OUTPUT_DIR = configured_path("AUTOSLICE_OUTPUT_DIR", "output")
TIMELINE_DIR = configured_path("AUTOSLICE_TIMELINE_DIR", "timelines")
SUBMISSION_DIR = configured_path("AUTOSLICE_SUBMISSION_DIR", "submissions")
AUTOCOVER_DIR = configured_path("AUTOSLICE_AUTOCOVER_DIR", "autocover_tool")
COVER_OUTPUT_DIR = configured_path("AUTOCOVER_OUTPUT_DIR", "covers")
STICKER_DIR = configured_path("AUTOCOVER_STICKER_DIR", "stickers")
AUTOCOVER_INPUT_DIR = Path(
    os.environ.get("AUTOCOVER_INPUT_DIR", OUTPUT_DIR)
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
