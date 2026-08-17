import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autoslice import slice_planning


class SlicePlanningTests(unittest.TestCase):
    def test_build_jobs_keeps_source_container_and_precise_outro_end(self):
        marks = [
            {"start": 10, "end": 90, "title": "普通片段"},
            {
                "start": 100,
                "end": 131,
                "title": "晚安片段",
                "preserve_to_video_end": True,
            },
            {"start": 200, "end": 180, "title": "无效范围"},
        ]
        with TemporaryDirectory() as directory:
            report_dir = Path(directory) / "录播_话题切片"
            jobs = slice_planning.build_slice_jobs(
                marks,
                "录播.mp4",
                str(report_dir),
                precise_video_end=120.25,
            )

        self.assertEqual([job["index"] for job in jobs], [1, 2])
        self.assertEqual(jobs[0]["duration"], 80)
        self.assertEqual(jobs[1]["end"], 120.25)
        self.assertEqual(jobs[1]["duration"], 20.25)
        self.assertTrue(all(job["output_name"].endswith(".mp4") for job in jobs))
        self.assertEqual(Path(jobs[1]["output_path"]).parent, report_dir)

    def test_partition_checks_exact_container_title_reuse_then_pending(self):
        jobs = [
            {"output_path": f"clip-{index}.flv", "output_name": f"clip-{index}.flv", "duration": 80}
            for index in range(1, 5)
        ]
        with (
            patch.object(
                slice_planning.slice_reuse,
                "is_reusable_topic_clip",
                side_effect=(True, False, False, False),
            ),
            patch.object(
                slice_planning.slice_reuse,
                "reuse_compatible_topic_clip",
                side_effect=(True, False, False),
            ),
            patch.object(
                slice_planning.slice_reuse,
                "reuse_topic_clip_after_title_change",
                side_effect=(True, False),
            ),
        ):
            partition = slice_planning.partition_slice_jobs(
                jobs,
                "录播.flv",
                "输出",
            )

        self.assertEqual(partition.reusable_jobs, tuple(jobs[:3]))
        self.assertEqual(partition.pending_jobs, (jobs[3],))
        self.assertEqual(partition.title_renamed_count, 1)
        self.assertEqual(
            partition.reusable_output_names,
            tuple(job["output_name"] for job in jobs[:3]),
        )


if __name__ == "__main__":
    unittest.main()
