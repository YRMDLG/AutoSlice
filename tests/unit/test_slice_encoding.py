import subprocess
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autoslice import slice_encoding


class SliceEncodingTests(unittest.TestCase):
    def test_encode_job_falls_back_to_cpu_and_commits_atomically(self):
        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "01_10s_测试.flv"
            job = {
                "index": 1,
                "start": 10,
                "duration": 80,
                "title": "测试",
                "output_path": str(output_path),
            }
            calls = []
            progress = []

            def fake_run(args, **_kwargs):
                calls.append(args)
                if "h264_nvenc" in args:
                    Path(args[-1]).write_bytes(b"failed-nvenc")
                    raise subprocess.CalledProcessError(1, args)
                Path(args[-1]).write_bytes(b"cpu-result")

            with patch.object(subprocess, "run", side_effect=fake_run):
                final_args = slice_encoding.encode_slice_job(
                    job,
                    "source.flv",
                    ["-c:v", "h264_nvenc"],
                    1,
                    subprocess,
                    lambda _path: 80.03,
                    lambda message, _current, _total: progress.append(message),
                )

            self.assertEqual(output_path.read_bytes(), b"cpu-result")
            self.assertFalse(Path(str(output_path) + ".part.flv").exists())
            self.assertEqual(len(calls), 2)
            self.assertIn("libx264", final_args)
            self.assertIn("NVENC 不可用，已改用 CPU 精确编码", progress)

    def test_encode_job_removes_partial_file_when_duration_is_invalid(self):
        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "01_10s_测试.mp4"
            part_path = Path(str(output_path) + ".part.mp4")
            job = {
                "index": 1,
                "start": 10,
                "duration": 80,
                "title": "测试",
                "output_path": str(output_path),
            }

            def fake_run(args, **_kwargs):
                Path(args[-1]).write_bytes(b"invalid-duration")

            with (
                patch.object(subprocess, "run", side_effect=fake_run),
                self.assertRaisesRegex(RuntimeError, "时长校验失败"),
            ):
                slice_encoding.encode_slice_job(
                    job,
                    "source.mp4",
                    ["-c:v", "libx264"],
                    1,
                    subprocess,
                    lambda _path: None,
                )

            self.assertFalse(output_path.exists())
            self.assertFalse(part_path.exists())

    def test_execute_jobs_probes_then_uses_two_nvenc_workers(self):
        jobs = [
            {
                "index": index,
                "start": index * 100,
                "duration": 80,
                "title": f"片段{index}",
                "output_path": f"clip-{index}.flv",
            }
            for index in range(1, 5)
        ]
        state = {"active": 0, "max_active": 0}
        state_lock = threading.Lock()

        def fake_encode(
                _job, _source, requested_args, _total, _subprocess,
                _probe, _progress=None):
            with state_lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            try:
                time.sleep(0.02)
                return tuple(requested_args)
            finally:
                with state_lock:
                    state["active"] -= 1

        with TemporaryDirectory() as directory:
            seek_index = Path(directory) / "seek-index.mkv"
            seek_index.write_bytes(b"index")
            with (
                patch.object(
                    slice_encoding,
                    "prepare_seekable_slice_source",
                    return_value=(str(seek_index), str(seek_index)),
                ),
                patch.object(
                    slice_encoding,
                    "preferred_slice_video_encoder_args",
                    return_value=["-c:v", "h264_nvenc"],
                ),
                patch.object(
                    slice_encoding,
                    "configured_slice_concurrency",
                    return_value=2,
                ),
                patch.object(
                    slice_encoding,
                    "encode_slice_job",
                    side_effect=fake_encode,
                ),
            ):
                result = slice_encoding.execute_slice_jobs(
                    "source.flv",
                    directory,
                    jobs,
                    total_mark_count=4,
                    source_span_sec=400,
                    subprocess_module=subprocess,
                    probe_duration=lambda _path: 80,
                )

            self.assertFalse(seek_index.exists())

        self.assertEqual(result.encoded_count, 4)
        self.assertEqual(result.parallel_workers, 2)
        self.assertTrue(result.used_seek_index)
        self.assertEqual(state["max_active"], 2)

    def test_execute_jobs_cleans_seek_index_after_encoder_failure(self):
        job = {
            "index": 1,
            "start": 10,
            "duration": 80,
            "title": "失败片段",
            "output_path": "clip.flv",
        }
        with TemporaryDirectory() as directory:
            seek_index = Path(directory) / "seek-index.mkv"
            seek_index.write_bytes(b"index")
            with (
                patch.object(
                    slice_encoding,
                    "prepare_seekable_slice_source",
                    return_value=(str(seek_index), str(seek_index)),
                ),
                patch.object(
                    slice_encoding,
                    "preferred_slice_video_encoder_args",
                    return_value=["-c:v", "libx264"],
                ),
                patch.object(
                    slice_encoding,
                    "encode_slice_job",
                    side_effect=RuntimeError("encode failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "encode failed"),
            ):
                slice_encoding.execute_slice_jobs(
                    "source.flv",
                    directory,
                    [job],
                    total_mark_count=1,
                    source_span_sec=90,
                    subprocess_module=subprocess,
                    probe_duration=lambda _path: 80,
                )

            self.assertFalse(seek_index.exists())


if __name__ == "__main__":
    unittest.main()
