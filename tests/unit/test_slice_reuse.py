import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autoslice import slice_reuse


class SliceReuseTests(unittest.TestCase):
    @staticmethod
    def _fresh_source_and_clip(root):
        source_path = Path(root) / "录播.flv"
        output_path = Path(root) / "01_10s_片段.flv"
        source_path.write_bytes(b"source")
        output_path.write_bytes(b"clip")
        source_mtime = source_path.stat().st_mtime
        os.utime(output_path, (source_mtime + 10, source_mtime + 10))
        return source_path, output_path

    def test_force_reslice_rejects_existing_clip_without_duration_probe(self):
        with TemporaryDirectory() as directory:
            source_path, output_path = self._fresh_source_and_clip(directory)
            with (
                patch.dict(os.environ, {"AUTOSLICE_FORCE_RESLICE": "true"}),
                patch.object(slice_reuse, "probe_video_duration") as probe,
            ):
                reusable = slice_reuse.is_reusable_topic_clip(
                    output_path,
                    source_path,
                    80,
                )

        self.assertFalse(reusable)
        probe.assert_not_called()

    def test_source_update_rejects_older_clip_without_duration_probe(self):
        with TemporaryDirectory() as directory:
            source_path, output_path = self._fresh_source_and_clip(directory)
            output_mtime = output_path.stat().st_mtime
            os.utime(source_path, (output_mtime + 10, output_mtime + 10))
            with patch.object(slice_reuse, "probe_video_duration") as probe:
                reusable = slice_reuse.is_reusable_topic_clip(
                    output_path,
                    source_path,
                    80,
                )

        self.assertFalse(reusable)
        probe.assert_not_called()

    def test_duration_must_stay_inside_reuse_tolerance(self):
        with TemporaryDirectory() as directory:
            source_path, output_path = self._fresh_source_and_clip(directory)
            with patch.object(
                slice_reuse,
                "probe_video_duration",
                side_effect=(80.49, 80.51),
            ):
                within_tolerance = slice_reuse.is_reusable_topic_clip(
                    output_path,
                    source_path,
                    80,
                )
                outside_tolerance = slice_reuse.is_reusable_topic_clip(
                    output_path,
                    source_path,
                    80,
                )

        self.assertTrue(within_tolerance)
        self.assertFalse(outside_tolerance)


if __name__ == "__main__":
    unittest.main()
