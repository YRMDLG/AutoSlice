"""AutoCover 的包资源和可写数据路径契约。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DATA_DIR_ENV_NAME = "AUTOCOVER_DATA_DIR"
_WORKSPACE_MARKERS = ("pyproject.toml", "api_config.example.json")


def discover_workspace_root(start: str | Path | None = None) -> Path | None:
    """识别统一仓库根；安装后的包目录不会被误认为工作区。"""

    location = Path(start or __file__).expanduser().resolve()
    current = location if location.is_dir() else location.parent
    for candidate in (current, *current.parents):
        if all((candidate / marker).is_file() for marker in _WORKSPACE_MARKERS):
            return candidate
    return None


def default_user_data_dir(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """返回 AutoCover 安装态的用户数据目录。"""

    source = os.environ if environ is None else environ
    configured = str(source.get(DATA_DIR_ENV_NAME, "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = str(source.get("LOCALAPPDATA", "")).strip()
    if local_app_data:
        return (Path(local_app_data).expanduser() / "AutoSlice" / "AutoCover").resolve()
    xdg_data_home = str(source.get("XDG_DATA_HOME", "")).strip()
    if xdg_data_home:
        return (Path(xdg_data_home).expanduser() / "autoslice" / "autocover").resolve()
    return (Path.home() / ".local" / "share" / "autoslice" / "autocover").resolve()


def application_data_root(
    *,
    package_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """源码态沿用 ``autocover_tool`` 数据，安装态使用用户数据目录。"""

    package = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    workspace = discover_workspace_root(package)
    legacy_tool_root = workspace / "autocover_tool" if workspace is not None else None
    if legacy_tool_root is not None and legacy_tool_root.is_dir():
        return legacy_tool_root
    return default_user_data_dir(environ)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"
DATA_ROOT = application_data_root(package_root=PACKAGE_ROOT)
CACHE_DIR = DATA_ROOT / ".cache" / "frames"
DEFAULT_INPUT_DIR = Path(
    os.environ.get("AUTOCOVER_INPUT_DIR", DATA_ROOT / "input")
).expanduser().resolve()
DEFAULT_OUTPUT_DIR = Path(
    os.environ.get("AUTOCOVER_OUTPUT_DIR", DATA_ROOT / "covers")
).expanduser().resolve()
DEFAULT_STICKER_ROOT = Path(
    os.environ.get("AUTOCOVER_STICKER_DIR", DATA_ROOT / "stickers")
).expanduser().resolve()
DEFAULT_IMPORTED_STICKER_ROOT = Path(
    os.environ.get("AUTOCOVER_USER_ASSET_DIR", DATA_ROOT / "user-assets")
).expanduser().resolve()
LOCAL_FONT_PATH = PACKAGE_ROOT / "local" / "fonts" / "seto-bilibili.ttf"


__all__ = [
    "CACHE_DIR",
    "DATA_DIR_ENV_NAME",
    "DATA_ROOT",
    "DEFAULT_IMPORTED_STICKER_ROOT",
    "DEFAULT_INPUT_DIR",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_STICKER_ROOT",
    "LOCAL_FONT_PATH",
    "PACKAGE_ROOT",
    "STATIC_DIR",
    "TEMPLATE_DIR",
    "application_data_root",
    "default_user_data_dir",
    "discover_workspace_root",
]
