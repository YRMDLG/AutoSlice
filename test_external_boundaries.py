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
import unittest
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import requests
from flask import Flask


BLOCKED_MODEL_PACKAGE_ROOTS = frozenset({
    "funasr",
    "huggingface_hub",
    "modelscope",
    "torch",
    "transformers",
})
BLOCKED_SERVICE_PORTS = frozenset({5002, 5010})
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
    EXTERNAL_BOUNDARY_GUARD.validate_media_path(path)
    return _ORIGINAL_PATH_OPEN(path, *args, **kwargs)


class ExternalBoundaryGuard:
    """进程级护栏；显式 mock 和批准的临时目录仍可正常工作。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._installed = False
        self._patchers = []
        self._temporary_roots: dict[Path, int] = {
            Path(tempfile.gettempdir()).resolve(): 1,
        }
        self._system_font_roots = _system_font_roots()
        self._saved_environment: dict[str, str | None] = {}
        self._isolated_environment: tempfile.TemporaryDirectory[str] | None = None

    def _path_is_allowed(self, path: Path) -> bool:
        with self._lock:
            roots = tuple(self._temporary_roots)
        if any(path == root or root in path.parents for root in roots):
            return True
        return (
            path.suffix.casefold() in SYSTEM_FONT_SUFFIXES
            and any(
                path == root or root in path.parents
                for root in self._system_font_roots
            )
        )

    def validate_media_path(self, file: object) -> None:
        path = _resolved_path(file)
        if path is None or path.suffix.casefold() not in PRIVATE_MEDIA_SUFFIXES:
            return
        if not self._path_is_allowed(path):
            raise _boundary_error("用户媒体访问", path)

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

    def install(self) -> "ExternalBoundaryGuard":
        with self._lock:
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
        self.validate_media_path(file)
        return _ORIGINAL_OPEN(file, *args, **kwargs)

    def _guarded_popen(self, args, *popen_args, **kwargs):
        self.validate_subprocess(args)
        return _ORIGINAL_SUBPROCESS_POPEN(args, *popen_args, **kwargs)

    def _guarded_run(self, args, *run_args, **kwargs):
        self.validate_subprocess(args)
        return _ORIGINAL_SUBPROCESS_RUN(args, *run_args, **kwargs)


EXTERNAL_BOUNDARY_GUARD = ExternalBoundaryGuard()


def install_test_external_boundary_guard() -> ExternalBoundaryGuard:
    return EXTERNAL_BOUNDARY_GUARD.install()


install_test_external_boundary_guard()
atexit.register(EXTERNAL_BOUNDARY_GUARD.uninstall)


class ExternalBoundaryTests(unittest.TestCase):

    def test_unmocked_http_is_rejected_but_explicit_mock_is_allowed(self):
        with self.assertRaisesRegex(AssertionError, "HTTP 请求"):
            requests.get("https://example.invalid/external-boundary", timeout=1)

        response = Mock(status_code=200)
        with patch.object(requests.sessions.Session, "request", return_value=response) as request:
            self.assertIs(
                requests.get("https://example.invalid/mocked", timeout=1),
                response,
            )
        request.assert_called_once()

    def test_real_model_package_is_rejected_but_module_double_is_allowed(self):
        existing = sys.modules.pop("funasr", None)
        try:
            with self.assertRaisesRegex(AssertionError, "模型/GPU 包加载"):
                __import__("funasr")
            fake_funasr = ModuleType("funasr")
            with patch.dict(sys.modules, {"funasr": fake_funasr}):
                self.assertIs(__import__("funasr"), fake_funasr)
        finally:
            if existing is not None:
                sys.modules["funasr"] = existing

    def test_real_flask_start_and_product_ports_are_rejected(self):
        with self.assertRaisesRegex(AssertionError, "Flask 服务启动"):
            Flask("external-boundary").run(port=5002)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            with self.assertRaisesRegex(AssertionError, "服务端口绑定"):
                server.bind(("127.0.0.1", 5010))

    def test_private_media_is_rejected_but_temporary_media_is_allowed(self):
        private_video = Path(__file__).resolve().parent / "recordings" / "private.flv"
        with self.assertRaisesRegex(AssertionError, "用户媒体访问"):
            private_video.read_bytes()
        with self.assertRaisesRegex(AssertionError, "用户媒体访问"):
            subprocess.run(["ffmpeg", "-i", str(private_video)], check=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            temporary_srt = Path(temp_dir) / "fixture.srt"
            temporary_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
            self.assertIn("测试", temporary_srt.read_text(encoding="utf-8"))

            # Chromium 等命令使用 --option=path 传递临时输出，不能把整个
            # 参数误解析为仓库下的相对媒体路径。
            EXTERNAL_BOUNDARY_GUARD.validate_subprocess([
                "browser",
                f"--screenshot={temporary_srt.with_suffix('.png')}",
            ])
            ffmpeg_path = str(temporary_srt).replace("\\", "/").replace(":", r"\:")
            EXTERNAL_BOUNDARY_GUARD.validate_subprocess([
                "ffmpeg",
                "-vf",
                f"ass='{ffmpeg_path}'",
            ])

    def test_public_system_font_is_allowed_without_allowing_user_font(self):
        if not EXTERNAL_BOUNDARY_GUARD._system_font_roots:
            self.skipTest("当前平台没有已声明的系统字体目录")
        system_font = EXTERNAL_BOUNDARY_GUARD._system_font_roots[0] / "fixture.ttf"
        EXTERNAL_BOUNDARY_GUARD.validate_media_path(system_font)

        private_font = Path(__file__).resolve().parent / "local" / "private.ttf"
        with self.assertRaisesRegex(AssertionError, "用户媒体访问"):
            EXTERNAL_BOUNDARY_GUARD.validate_media_path(private_font)

    def test_package_install_command_is_rejected_before_process_creation(self):
        with self.assertRaisesRegex(AssertionError, "包/模型安装"):
            subprocess.run([sys.executable, "-m", "pip", "install", "funasr"], check=True)


if __name__ == "__main__":
    unittest.main()
