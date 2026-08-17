"""外部副作用护栏自身的架构回归测试。"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import requests
from flask import Flask

from tests.support.external_boundary_guard import (
    EXTERNAL_BOUNDARY_GUARD,
    LINUX_LOGIC_ONLY_PACKAGE_ROOTS,
)


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
            # 此处只验证滤镜参数的路径提取；Linux 纯逻辑模式另有断言确保
            # 真正的 FFmpeg/ffprobe 命令在创建进程前被拒绝。
            with EXTERNAL_BOUNDARY_GUARD.linux_logic_only_mode(False):
                EXTERNAL_BOUNDARY_GUARD.validate_subprocess([
                    "ffmpeg",
                    "-vf",
                    f"ass='{ffmpeg_path}'",
                ])

    def test_public_system_font_is_allowed_without_allowing_user_font(self):
        if not EXTERNAL_BOUNDARY_GUARD._system_font_roots:
            self.skipTest("当前平台没有已声明的系统字体目录")
        system_font = EXTERNAL_BOUNDARY_GUARD._system_font_roots[0] / "fixture.ttf"
        with EXTERNAL_BOUNDARY_GUARD.linux_logic_only_mode(False):
            EXTERNAL_BOUNDARY_GUARD.validate_media_path(system_font)

        private_font = Path(__file__).resolve().parent / "local" / "private.ttf"
        with self.assertRaisesRegex(AssertionError, "用户媒体访问"):
            EXTERNAL_BOUNDARY_GUARD.validate_media_path(private_font)

    def test_package_install_command_is_rejected_before_process_creation(self):
        with self.assertRaisesRegex(AssertionError, "包/模型安装"):
            subprocess.run([sys.executable, "-m", "pip", "install", "funasr"], check=True)

    def test_linux_logic_only_rejects_optional_media_capabilities(self):
        private_config = Path(__file__).resolve().parent / "api_config.json"
        system_font = (
            EXTERNAL_BOUNDARY_GUARD._system_font_roots[0] / "fixture.ttf"
            if EXTERNAL_BOUNDARY_GUARD._system_font_roots
            else None
        )
        optional_packages = {
            name: sys.modules.pop(name, None)
            for name in LINUX_LOGIC_ONLY_PACKAGE_ROOTS
        }
        try:
            with EXTERNAL_BOUNDARY_GUARD.linux_logic_only_mode():
                with self.assertRaisesRegex(AssertionError, "Linux 纯逻辑外部命令"):
                    subprocess.run(["ffmpeg", "-version"], check=True)
                with self.assertRaisesRegex(AssertionError, "Linux 纯逻辑外部命令"):
                    subprocess.run(["nvidia-smi"], check=True)
                for package_name in LINUX_LOGIC_ONLY_PACKAGE_ROOTS:
                    with (
                        self.subTest(package_name=package_name),
                        self.assertRaisesRegex(ImportError, "可选媒体包加载"),
                    ):
                        __import__(package_name)
                with self.assertRaisesRegex(AssertionError, "Linux 纯逻辑私人配置访问"):
                    private_config.read_text(encoding="utf-8")
                if system_font is not None:
                    with self.assertRaisesRegex(AssertionError, "Linux 纯逻辑字体访问"):
                        EXTERNAL_BOUNDARY_GUARD.validate_media_path(system_font)

                with tempfile.TemporaryDirectory() as temp_dir:
                    fixture_config = Path(temp_dir) / "api_config.json"
                    fixture_config.write_text("{}", encoding="utf-8")
                    self.assertEqual(
                        fixture_config.read_text(encoding="utf-8"),
                        "{}",
                    )
        finally:
            for package_name, existing_module in optional_packages.items():
                if existing_module is not None:
                    sys.modules[package_name] = existing_module


if __name__ == "__main__":
    unittest.main()
