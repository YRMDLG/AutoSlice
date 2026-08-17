"""自动测试的统一外部副作用护栏。"""

from __future__ import annotations

import atexit
import builtins
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import requests
from flask import Flask

BLOCKED_MODEL_PACKAGE_ROOTS = frozenset({
    "funasr",
    "huggingface_hub",
    "modelscope",
    "torch",
    "transformers",
})
LINUX_LOGIC_ONLY_PACKAGE_ROOTS = frozenset({"PIL", "docx", "soxr"})
LINUX_LOGIC_ONLY_EXECUTABLES = frozenset({
    "ffmpeg",
    "ffprobe",
    "nvidia-smi",
})
BLOCKED_SERVICE_PORTS = frozenset({5002, 5010})
PRIVATE_CONFIG_FILENAMES = frozenset({
    ".env",
    "api_config.json",
    "autoslice.local.json",
    "title_style_profile.json",
})
PRIVATE_MEDIA_SUFFIXES = frozenset({
    ".ass",
    ".avi",
    ".ckpt",
    ".docx",
    ".flv",
    ".gif",
    ".jpeg",
    ".jpg",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".onnx",
    ".otf",
    ".png",
    ".pt",
    ".srt",
    ".ts",
    ".ttc",
    ".ttf",
    ".wav",
    ".webp",
    ".xml",
})
SYSTEM_FONT_SUFFIXES = frozenset({".otf", ".ttc", ".ttf"})
_MEDIA_SUFFIX_PATTERN = "|".join(
    re.escape(suffix.removeprefix("."))
    for suffix in sorted(PRIVATE_MEDIA_SUFFIXES, key=len, reverse=True)
)
_WINDOWS_MEDIA_PATH_RE = re.compile(
    rf"(?i)([a-z]:[\\/][^'\";,|:]*?\.(?:{_MEDIA_SUFFIX_PATTERN}))"
)
_POSIX_MEDIA_PATH_RE = re.compile(
    rf"(?i)(/[^'\";,|:]*?\.(?:{_MEDIA_SUFFIX_PATTERN}))"
)


_ORIGINAL_IMPORT = builtins.__import__
_ORIGINAL_OPEN = builtins.open
_ORIGINAL_PATH_OPEN = Path.open
_ORIGINAL_SOCKET_BIND = socket.socket.bind
_ORIGINAL_SUBPROCESS_POPEN = subprocess.Popen
_ORIGINAL_SUBPROCESS_RUN = subprocess.run


class ModelBoundaryViolation(ImportError, AssertionError):
    """既能触发现有 CPU 降级，也能被测试作为越界失败断言。"""


def _boundary_error(kind: str, detail: object) -> AssertionError:
    return AssertionError(f"自动测试禁止未 mock 的{kind}：{detail}")


def _blocked_http_request(*args, **kwargs):
    method = kwargs.get("method") or (args[1] if len(args) > 1 else "HTTP")
    url = kwargs.get("url") or (args[2] if len(args) > 2 else "<unknown>")
    raise _boundary_error(" HTTP 请求", f"{method} {url}")


def _blocked_urlopen(url, *args, **kwargs):
    raise _boundary_error(" URL 访问", url)


def _blocked_socket_connect(sock, address):
    raise _boundary_error("网络连接", address)


def _guarded_socket_bind(sock, address):
    port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
    if port in BLOCKED_SERVICE_PORTS:
        raise _boundary_error("服务端口绑定", port)
    return _ORIGINAL_SOCKET_BIND(sock, address)


def _blocked_flask_run(app, *args, **kwargs):
    port = kwargs.get("port", args[1] if len(args) > 1 else 5000)
    raise _boundary_error(" Flask 服务启动", port)


def _is_test_double(module: object) -> bool:
    return (
        isinstance(module, ModuleType)
        and not getattr(module, "__file__", None)
    )


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    package_root = str(name).split(".", 1)[0]
    if package_root in BLOCKED_MODEL_PACKAGE_ROOTS:
        loaded_module = sys.modules.get(package_root)
        if not _is_test_double(loaded_module):
            raise ModelBoundaryViolation(
                f"自动测试禁止未 mock 的模型/GPU 包加载：{name}"
            )
    if (
        package_root in LINUX_LOGIC_ONLY_PACKAGE_ROOTS
        and EXTERNAL_BOUNDARY_GUARD.linux_logic_only
    ):
        raise ModelBoundaryViolation(
            f"Linux 纯逻辑测试禁止可选媒体包加载：{name}"
        )
    return _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)


def _resolved_path(file: object) -> Path | None:
    if isinstance(file, int):
        return None
    try:
        raw_path = os.fsdecode(os.fspath(file))
    except (TypeError, ValueError):
        return None
    try:
        return Path(raw_path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _system_font_roots() -> tuple[Path, ...]:
    """返回可公开读取的系统字体目录，不包含用户字体或项目字体。"""
    candidates = []
    if os.name == "nt":
        candidates.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")
    else:
        candidates.extend((Path("/usr/share/fonts"), Path("/usr/local/share/fonts")))
    roots = []
    for candidate in candidates:
        try:
            roots.append(candidate.resolve(strict=False))
        except (OSError, RuntimeError, ValueError):
            continue
    return tuple(roots)


def _media_path_candidates(argument: str) -> tuple[str, ...]:
    """从普通参数、``--key=path`` 和 FFmpeg 滤镜中提取媒体路径。"""
    normalized = argument.strip("\"'").replace(r"\:", ":").replace(r"\'", "'")
    windows_paths = _WINDOWS_MEDIA_PATH_RE.findall(normalized)
    # Windows 路径本身含有 ``/Users/...`` 片段；命中盘符路径后不能再把
    # 其中的斜杠部分当成第二条 POSIX 绝对路径。
    absolute_paths = windows_paths or _POSIX_MEDIA_PATH_RE.findall(normalized)
    if absolute_paths:
        return tuple(dict.fromkeys(absolute_paths))
    if normalized.startswith(("-", "/")) and "=" in normalized:
        normalized = normalized.split("=", 1)[1].strip("\"'")
    return (normalized,)


def _guarded_path_open(path, *args, **kwargs):
    EXTERNAL_BOUNDARY_GUARD.validate_path(path)
    return _ORIGINAL_PATH_OPEN(path, *args, **kwargs)


class ExternalBoundaryGuard:
    """进程级护栏；显式 mock 和批准的临时目录仍可正常工作。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._installed = False
        self._linux_logic_only = False
        self._patchers = []
        self._temporary_roots: dict[Path, int] = {
            Path(tempfile.gettempdir()).resolve(): 1,
        }
        self._system_font_roots = _system_font_roots()
        self._saved_environment: dict[str, str | None] = {}
        self._isolated_environment: tempfile.TemporaryDirectory[str] | None = None

    @property
    def linux_logic_only(self) -> bool:
        with self._lock:
            return self._linux_logic_only

    @contextmanager
    def linux_logic_only_mode(self, enabled: bool = True):
        """临时切换 Linux 纯逻辑约束，并在退出后恢复原模式。"""
        with self._lock:
            previous = self._linux_logic_only
            self._linux_logic_only = enabled
        try:
            yield self
        finally:
            with self._lock:
                self._linux_logic_only = previous

    def _path_is_system_font(self, path: Path) -> bool:
        return (
            path.suffix.casefold() in SYSTEM_FONT_SUFFIXES
            and any(
                path == root or root in path.parents
                for root in self._system_font_roots
            )
        )

    def _path_is_allowed(self, path: Path) -> bool:
        with self._lock:
            roots = tuple(self._temporary_roots)
            linux_logic_only = self._linux_logic_only
        if any(path == root or root in path.parents for root in roots):
            return True
        return not linux_logic_only and self._path_is_system_font(path)

    def validate_media_path(self, file: object) -> None:
        path = _resolved_path(file)
        if path is None or path.suffix.casefold() not in PRIVATE_MEDIA_SUFFIXES:
            return
        if self.linux_logic_only and self._path_is_system_font(path):
            raise _boundary_error(" Linux 纯逻辑字体访问", path)
        if not self._path_is_allowed(path):
            raise _boundary_error("用户媒体访问", path)

    def validate_private_config_path(self, file: object) -> None:
        path = _resolved_path(file)
        if (
            path is None
            or path.name.casefold() not in PRIVATE_CONFIG_FILENAMES
            or not self.linux_logic_only
            or self._path_is_allowed(path)
        ):
            return
        raise _boundary_error(" Linux 纯逻辑私人配置访问", path)

    def validate_path(self, file: object) -> None:
        self.validate_private_config_path(file)
        self.validate_media_path(file)

    @contextmanager
    def allow_temporary_root(self, path: str | os.PathLike[str]):
        root = Path(path).resolve()
        with self._lock:
            self._temporary_roots[root] = self._temporary_roots.get(root, 0) + 1
        try:
            yield root
        finally:
            with self._lock:
                remaining = self._temporary_roots[root] - 1
                if remaining:
                    self._temporary_roots[root] = remaining
                else:
                    self._temporary_roots.pop(root, None)

    def validate_subprocess(self, command: object) -> None:
        if isinstance(command, (str, bytes)):
            raw_command = os.fsdecode(command)
            try:
                parts = shlex.split(raw_command, posix=os.name != "nt")
            except ValueError:
                parts = [raw_command]
        else:
            try:
                parts = [os.fsdecode(os.fspath(part)) for part in command]
            except (TypeError, ValueError):
                parts = []
        lowered = [part.strip('"\'').casefold() for part in parts]
        if len(lowered) >= 3 and lowered[1:3] == ["-m", "pip"]:
            raise _boundary_error("包/模型安装", " ".join(parts[:3]))
        for part in parts:
            for candidate in _media_path_candidates(part):
                self.validate_media_path(candidate)
        if parts and self.linux_logic_only:
            executable = parts[0].strip('"\'').replace("\\", "/").rsplit("/", 1)[-1]
            executable = executable.casefold().removesuffix(".exe")
            if executable in LINUX_LOGIC_ONLY_EXECUTABLES:
                raise _boundary_error(" Linux 纯逻辑外部命令", executable)

    def install(
            self,
            *,
            linux_logic_only: bool | None = None,
    ) -> "ExternalBoundaryGuard":
        with self._lock:
            if linux_logic_only is not None:
                self._linux_logic_only = linux_logic_only
            if self._installed:
                return self
            self._isolated_environment = tempfile.TemporaryDirectory(
                prefix="autoslice-test-boundary-"
            )
            isolated_root = Path(self._isolated_environment.name)
            sticker_root = isolated_root / "stickers"
            imported_sticker_root = isolated_root / "imported-stickers"
            sticker_root.mkdir()
            imported_sticker_root.mkdir()
            environment_values = {
                "HF_HUB_OFFLINE": "1",
                "MODELSCOPE_LOCAL_ONLY": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "AUTOCOVER_STICKER_DIR": str(sticker_root),
                "AUTOCOVER_USER_ASSET_DIR": str(imported_sticker_root),
            }
            for key, value in environment_values.items():
                self._saved_environment[key] = os.environ.get(key)
                os.environ[key] = value
            self._patchers = [
                patch.object(builtins, "__import__", new=_guarded_import),
                patch.object(builtins, "open", new=self._guarded_open),
                patch.object(Path, "open", new=_guarded_path_open),
                patch.object(requests.sessions.Session, "request", new=_blocked_http_request),
                patch.object(urllib.request, "urlopen", new=_blocked_urlopen),
                patch.object(socket.socket, "connect", new=_blocked_socket_connect),
                patch.object(socket.socket, "bind", new=_guarded_socket_bind),
                patch.object(Flask, "run", new=_blocked_flask_run),
                patch.object(subprocess, "Popen", new=self._guarded_popen),
                patch.object(subprocess, "run", new=self._guarded_run),
            ]
            for patcher in self._patchers:
                patcher.start()
            self._installed = True
            return self

    def uninstall(self) -> None:
        with self._lock:
            if not self._installed:
                return
            for patcher in reversed(self._patchers):
                patcher.stop()
            for key, previous in self._saved_environment.items():
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous
            self._patchers = []
            self._saved_environment = {}
            if self._isolated_environment is not None:
                self._isolated_environment.cleanup()
                self._isolated_environment = None
            self._installed = False

    def _guarded_open(self, file, *args, **kwargs):
        self.validate_path(file)
        return _ORIGINAL_OPEN(file, *args, **kwargs)

    def _guarded_popen(self, args, *popen_args, **kwargs):
        self.validate_subprocess(args)
        return _ORIGINAL_SUBPROCESS_POPEN(args, *popen_args, **kwargs)

    def _guarded_run(self, args, *run_args, **kwargs):
        self.validate_subprocess(args)
        return _ORIGINAL_SUBPROCESS_RUN(args, *run_args, **kwargs)


EXTERNAL_BOUNDARY_GUARD = ExternalBoundaryGuard()


def install_test_external_boundary_guard(
        *,
        linux_logic_only: bool | None = None,
) -> ExternalBoundaryGuard:
    return EXTERNAL_BOUNDARY_GUARD.install(
        linux_logic_only=linux_logic_only,
    )


install_test_external_boundary_guard()
atexit.register(EXTERNAL_BOUNDARY_GUARD.uninstall)
