"""AutoCover 命令行入口测试。"""

from __future__ import annotations

import io
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from autocover import API_VERSION, SERVICE_ID
from autocover.cli import (
    _configure_text_stream,
    _is_compatible_service,
    build_parser,
    find_available_port,
    main,
    run_server,
)
from autocover.video import FrameCandidate, FrameMetrics


class CliTests(unittest.TestCase):
    """验证参数、端口顺延和批量渲染流程。"""

    def test_parser_exposes_serve_and_batch_commands(self) -> None:
        parser = build_parser()

        serve = parser.parse_args(["serve", "--port", "5020", "--no-browser"])
        batch = parser.parse_args(["batch", "input", "--canvas", "4x3"])
        self.assertEqual((serve.command, serve.port), ("serve", 5020))
        self.assertEqual((batch.command, batch.canvas), ("batch", "4x3"))

    def test_find_available_port_skips_an_occupied_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]

            selected = find_available_port(port, attempts=2)

        self.assertEqual(selected, port + 1)

    def test_service_compatibility_requires_matching_contract(self) -> None:
        self.assertTrue(
            _is_compatible_service(
                {"service": SERVICE_ID, "api_version": API_VERSION}
            )
        )
        self.assertFalse(_is_compatible_service({"default_output_dir": "covers"}))
        self.assertFalse(
            _is_compatible_service(
                {"service": SERVICE_ID, "api_version": API_VERSION - 1}
            )
        )

    @patch("autocover.cli._open_browser_later")
    @patch("autocover.cli._probe_service")
    def test_server_reuses_compatible_preferred_port(self, probe, open_browser) -> None:
        probe.return_value = {"service": SERVICE_ID, "api_version": API_VERSION}

        result = run_server(5010)

        self.assertEqual(result, 0)
        probe.assert_called_once_with(5010)
        open_browser.assert_called_once_with("http://127.0.0.1:5010")

    def test_console_encoding_accepts_emoji_filenames(self) -> None:
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk")

        _configure_text_stream(stream)
        stream.write("03_萤火虫备战😊.flv")
        stream.flush()

        self.assertEqual(stream.encoding.lower(), "utf-8")
        self.assertIn("😊".encode("utf-8"), buffer.getvalue())

    def test_batch_generates_requested_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clips = root / "clips"
            clips.mkdir()
            (clips / "01_测试.mp4").write_bytes(b"video")
            frame = root / "frame.jpg"
            Image.new("RGB", (1280, 720), "#cf86ab").save(frame)
            candidate = FrameCandidate(
                path=str(frame),
                timestamp=5.0,
                score=80.0,
                metrics=FrameMetrics(0.5, 1.0, 0.6, 0.7, 0.4, 0.0),
            )
            output = root / "output"
            with patch("autocover.workspace.extract_candidate_frames", return_value=[candidate]):
                result = main([
                    "batch",
                    str(clips),
                    "--output-dir",
                    str(output),
                    "--canvas",
                    "4x3",
                ])

            self.assertEqual(result, 0)
            self.assertTrue((output / "01_测试-4x3.jpg").is_file())
            self.assertFalse((output / "01_测试-16x9.jpg").exists())

    def test_invalid_batch_directory_returns_error_code(self) -> None:
        result = main(["batch", "不存在的目录"])

        self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
