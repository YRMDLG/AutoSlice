"""AutoSlice 的工作区、用户数据与包资源路径契约。

源码克隆模式继续使用仓库根目录中的本机配置和运行数据；安装包脱离仓库
运行时改用用户数据目录。显式环境变量始终优先，模块不会在导入时创建目录。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

WORKSPACE_ENV_NAME = "AUTOSLICE_WORKSPACE_DIR"
USER_DATA_ENV_NAME = "AUTOSLICE_USER_DATA_DIR"
STATE_DIR_ENV_NAME = "AUTOSLICE_STATE_DIR"
_WORKSPACE_MARKERS = ("pyproject.toml", "api_config.example.json")


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _configured_path(
    environ: Mapping[str, str],
    name: str,
) -> Path | None:
    value = str(environ.get(name, "")).strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


def discover_workspace_root(
    start: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """查找源码克隆根目录；显式工作区不要求存在公开标记。"""

    source = _environment(environ)
    configured = _configured_path(source, WORKSPACE_ENV_NAME)
    if configured is not None:
        return configured

    location = Path(start or __file__).expanduser().resolve()
    current = location if location.is_dir() else location.parent
    for candidate in (current, *current.parents):
        if all((candidate / marker).is_file() for marker in _WORKSPACE_MARKERS):
            return candidate
    return None


def default_user_data_dir(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """返回安装态的用户数据根，不读取其他程序配置。"""

    source = _environment(environ)
    configured = _configured_path(source, USER_DATA_ENV_NAME)
    if configured is not None:
        return configured

    local_app_data = str(source.get("LOCALAPPDATA", "")).strip()
    if local_app_data:
        return (Path(local_app_data).expanduser() / "AutoSlice").resolve()
    xdg_data_home = str(source.get("XDG_DATA_HOME", "")).strip()
    if xdg_data_home:
        return (Path(xdg_data_home).expanduser() / "autoslice").resolve()
    return (Path.home() / ".local" / "share" / "autoslice").resolve()


def application_data_root(
    *,
    start: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """源码态使用仓库根，安装态使用用户数据目录。"""

    return discover_workspace_root(start, environ=environ) or default_user_data_dir(environ)


def state_dir(
    *,
    start: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """返回 SQLite、任务事件等可写状态目录。"""

    source = _environment(environ)
    configured = _configured_path(source, STATE_DIR_ENV_NAME)
    if configured is not None:
        return configured
    return application_data_root(start=start, environ=source) / ".autoslice-state"


def resource_directory(
    name: str,
    *,
    package_dir: str | Path | None = None,
    workspace_root: str | Path | None = None,
) -> Path:
    """优先返回包内资源，迁移期间兼容仓库根目录资源。"""

    package = Path(package_dir or Path(__file__).resolve().parent).resolve()
    packaged = package / "resources" / name
    if packaged.is_dir():
        return packaged

    workspace = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else discover_workspace_root(package)
    )
    if workspace is not None:
        legacy = workspace / name
        if legacy.is_dir():
            return legacy
    return packaged


PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_WORKSPACE_ROOT = discover_workspace_root(PACKAGE_DIR)
USER_DATA_DIR = default_user_data_dir()
APPLICATION_DATA_ROOT = SOURCE_WORKSPACE_ROOT or USER_DATA_DIR
TEMPLATE_DIR = resource_directory(
    "templates",
    package_dir=PACKAGE_DIR,
    workspace_root=SOURCE_WORKSPACE_ROOT,
)
STATIC_DIR = resource_directory(
    "static",
    package_dir=PACKAGE_DIR,
    workspace_root=SOURCE_WORKSPACE_ROOT,
)


__all__ = [
    "APPLICATION_DATA_ROOT",
    "PACKAGE_DIR",
    "SOURCE_WORKSPACE_ROOT",
    "STATE_DIR_ENV_NAME",
    "STATIC_DIR",
    "TEMPLATE_DIR",
    "USER_DATA_DIR",
    "USER_DATA_ENV_NAME",
    "WORKSPACE_ENV_NAME",
    "application_data_root",
    "default_user_data_dir",
    "discover_workspace_root",
    "resource_directory",
    "state_dir",
]
