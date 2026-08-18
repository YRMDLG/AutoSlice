import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from autoslice import media_probe


class MediaProbeTests(unittest.TestCase):
    def test_missing_media_returns_none_without_running_ffprobe(self):
        with patch.object(media_probe.subprocess, "run") as run:
            duration = media_probe.probe_video_duration("missing.flv")

        self.assertIsNone(duration)
        run.assert_not_called()

    def test_reads_positive_duration_from_ffprobe(self):
        with TemporaryDirectory() as directory:
            video_path = Path(directory) / "录播.flv"
            video_path.write_bytes(b"video")
            completed = Mock(stdout="201.067\n")
            with patch.object(
                media_probe.subprocess,
                "run",
                return_value=completed,
            ) as run:
                duration = media_probe.probe_video_duration(video_path)

        self.assertEqual(duration, 201.067)
        command = run.call_args.args[0]
        self.assertEqual(command[-1], str(video_path))
        self.assertIn("format=duration", command)

    def test_invalid_or_non_positive_output_returns_none(self):
        with TemporaryDirectory() as directory:
            video_path = Path(directory) / "recording.flv"
            video_path.write_bytes(b"video")
            for output in ("", "invalid", "0", "-1"):
                with self.subTest(output=output), patch.object(
                    media_probe.subprocess,
                    "run",
                    return_value=Mock(stdout=output),
                ):
                    self.assertIsNone(
                        media_probe.probe_video_duration(video_path)
                    )

    def test_ffprobe_failure_returns_none(self):
        with TemporaryDirectory() as directory:
            video_path = Path(directory) / "recording.flv"
            video_path.write_bytes(b"video")
            error = subprocess.CalledProcessError(1, ["ffprobe"])
            with patch.object(
                media_probe.subprocess,
                "run",
                side_effect=error,
            ):
                duration = media_probe.probe_video_duration(video_path)

        self.assertIsNone(duration)


if __name__ == "__main__":
    unittest.main()
