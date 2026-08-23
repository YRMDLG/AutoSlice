"""AutoSlice 精调任务封面衔接字段测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoslice.reporting import (
    build_refinement_manifest,
    update_refinement_manifest_after_slice,
    write_refinement_manifest_files,
)


class RefinementCoverContractTests(unittest.TestCase):
    """稳定字段必须使用明确时间基准，且无效锚点不得伪造。"""

    def _assert_same_file(self, actual_path: object, expected_path: Path) -> None:
        self.assertIsInstance(actual_path, str)
        actual = Path(actual_path)
        self.assertTrue(actual.is_absolute(), f"{actual} 不是绝对路径")
        self.assertTrue(
            actual.samefile(expected_path),
            f"{actual} 与 {expected_path} 未指向同一文件",
        )

    def _build(self, root: Path, mark: dict[str, object]) -> dict[str, object]:
        return build_refinement_manifest(
            root / "录播.flv",
            root / "录播.srt",
            root / "校对字幕.srt",
            root / "话题分析.md",
            root / "clip_marks.json",
            [mark],
            root / "精调任务.json",
            root / "精调任务.md",
        )

    def test_builds_initial_internal_anchor_and_editorial_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._build(
                root,
                {
                    "start": 100,
                    "end": 160,
                    "title": "最后一句反转",
                    "publish_title": "【测试】最后一句突然反转",
                    "slice_anchor": 132,
                    "slice_anchor_source": "语义复核",
                    "editorial_interest_score": 4.5,
                    "editorial_interest_reason": "结尾反转完整",
                },
            )

        task = manifest["tasks"][0]
        self.assertEqual(task["clip_timebase"], "source_video_seconds")
        self.assertEqual(task["source_segment_count"], 1)
        self.assertEqual(task["clip_start_seconds"], 100.0)
        self.assertEqual(task["clip_end_seconds"], 160.0)
        self.assertEqual(task["slice_anchor"], 132.0)
        self.assertEqual(task["slice_anchor_source"], "语义复核")
        self.assertEqual(task["cover_anchor_seconds"], 32.0)
        self.assertIsNone(task["cover_anchor_media_path"])
        self.assertEqual(task["editorial_interest_score"], 4.5)
        self.assertEqual(task["editorial_interest_reason"], "结尾反转完整")
        self.assertIsNone(task["original_slice_path"])
        self.assertIsNone(task["final_clip_path"])
        self.assertIsNone(task["corrected_srt_path"])

    def test_invalid_or_out_of_range_anchor_is_null_instead_of_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for anchor in (99, 161, float("nan"), True, "132"):
                with self.subTest(anchor=anchor):
                    task = self._build(
                        root,
                        {
                            "start": 100,
                            "end": 160,
                            "title": "无效锚点",
                            "slice_anchor": anchor,
                            "slice_anchor_source": "语义复核",
                        },
                    )["tasks"][0]
                    self.assertIsNone(task["cover_anchor_seconds"])

    def test_slice_update_binds_anchor_to_exact_original_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_dir = root / "录播_话题切片"
            report_dir.mkdir()
            mark = {
                "start": 100,
                "end": 160,
                "title": "最后一句反转",
                "publish_title": "【测试】最后一句突然反转",
                "slice_anchor": 132,
                "slice_anchor_source": "语义复核",
            }
            manifest = self._build(root, mark)
            manifest_path = Path(manifest["manifest_json_path"])
            write_refinement_manifest_files(manifest)
            filename = manifest["tasks"][0]["clip_filename"]
            clip_path = (report_dir / filename).resolve()
            clip_path.write_bytes(b"clip")

            self.assertTrue(
                update_refinement_manifest_after_slice(
                    manifest_path,
                    report_dir,
                    [mark],
                )
            )
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))

            task = saved["tasks"][0]
            self._assert_same_file(task["slice_path"], clip_path)
            self._assert_same_file(task["original_slice_path"], clip_path)
            self._assert_same_file(task["cover_anchor_media_path"], clip_path)

    def test_slice_update_recomputes_producer_fields_from_current_mark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_dir = root / "录播_话题切片"
            report_dir.mkdir()
            new_mark = {
                "start": 15,
                "end": 75,
                "title": "新范围",
                "slice_anchor": 48,
                "slice_anchor_source": "人工高星时间轴",
                "editorial_interest_score": 5,
                "editorial_interest_reason": "新爆点证据完整",
                "clip_timebase": "source_video_seconds",
                "source_segment_count": 2,
            }
            manifest = self._build(root, new_mark)
            task = manifest["tasks"][0]
            task.update(
                {
                    "clip_start_seconds": 1,
                    "clip_end_seconds": 2,
                    "slice_anchor": 1.5,
                    "slice_anchor_source": "陈旧来源",
                    "cover_anchor_seconds": 0.5,
                    "editorial_interest_score": 1,
                    "editorial_interest_reason": "陈旧理由",
                    "source_segment_count": 1,
                }
            )
            manifest_path = Path(manifest["manifest_json_path"])
            write_refinement_manifest_files(manifest)
            filename = task["clip_filename"]
            clip_path = (report_dir / filename).resolve()
            clip_path.write_bytes(b"clip")

            self.assertTrue(
                update_refinement_manifest_after_slice(
                    manifest_path,
                    report_dir,
                    [new_mark],
                )
            )
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))

            task = saved["tasks"][0]
            self.assertEqual(task["clip_timebase"], "source_video_seconds")
            self.assertEqual(task["source_segment_count"], 2)
            self.assertEqual(task["clip_start_seconds"], 15.0)
            self.assertEqual(task["clip_end_seconds"], 75.0)
            self.assertEqual(task["slice_anchor"], 48.0)
            self.assertEqual(task["slice_anchor_source"], "人工高星时间轴")
            self.assertEqual(task["cover_anchor_seconds"], 33.0)
            self._assert_same_file(task["cover_anchor_media_path"], clip_path)
            self.assertEqual(task["editorial_interest_score"], 5.0)
            self.assertEqual(task["editorial_interest_reason"], "新爆点证据完整")

    def test_slice_update_preserves_explicit_final_anchor_and_corrected_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_dir = root / "录播_话题切片"
            report_dir.mkdir()
            mark = {
                "start": 30,
                "end": 90,
                "title": "更新后的范围",
                "slice_anchor": 60,
                "slice_anchor_source": "语义复核",
                "editorial_interest_score": 4,
                "editorial_interest_reason": "更新后的理由",
            }
            manifest = self._build(root, mark)
            task = manifest["tasks"][0]
            final_path = (root / "人工精剪.mp4").resolve()
            corrected_srt = (root / "人工校对.srt").resolve()
            previous_subtitle = (root / "旧自动字幕.srt").resolve()
            task.update(
                {
                    "final_clip_path": str(final_path),
                    "cover_anchor_seconds": 7.25,
                    "cover_anchor_media_path": str(final_path),
                    "subtitle_path": str(previous_subtitle),
                    "corrected_srt_path": str(corrected_srt),
                }
            )
            manifest_path = Path(manifest["manifest_json_path"])
            write_refinement_manifest_files(manifest)
            clip_path = (report_dir / task["clip_filename"]).resolve()
            clip_path.write_bytes(b"clip")
            clip_path.with_suffix(".srt").write_text("自动字幕", encoding="utf-8")

            self.assertTrue(
                update_refinement_manifest_after_slice(
                    manifest_path,
                    report_dir,
                    [mark],
                )
            )
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))

        task = saved["tasks"][0]
        self.assertEqual(task["final_clip_path"], str(final_path))
        self.assertEqual(task["cover_anchor_seconds"], 7.25)
        self.assertEqual(task["cover_anchor_media_path"], str(final_path))
        self.assertEqual(task["corrected_srt_path"], str(corrected_srt))

    def test_slice_update_refreshes_legacy_automatic_corrected_srt_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_dir = root / "录播_话题切片"
            report_dir.mkdir()
            mark = {
                "start": 30,
                "end": 90,
                "title": "更新后的范围",
                "slice_anchor": 60,
            }
            manifest = self._build(root, mark)
            task = manifest["tasks"][0]
            previous_subtitle = (root / "旧自动字幕.srt").resolve()
            task["subtitle_path"] = str(previous_subtitle)
            task["corrected_srt_path"] = str(previous_subtitle)
            manifest_path = Path(manifest["manifest_json_path"])
            write_refinement_manifest_files(manifest)
            clip_path = (report_dir / task["clip_filename"]).resolve()
            clip_path.write_bytes(b"clip")
            current_subtitle = clip_path.with_suffix(".srt")
            current_subtitle.write_text("新自动字幕", encoding="utf-8")

            self.assertTrue(
                update_refinement_manifest_after_slice(
                    manifest_path,
                    report_dir,
                    [mark],
                )
            )
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))

            task = saved["tasks"][0]
            self._assert_same_file(task["subtitle_path"], current_subtitle)
            self._assert_same_file(task["corrected_srt_path"], current_subtitle)

    def test_slice_update_clears_legacy_automatic_srt_when_current_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_dir = root / "录播_话题切片"
            report_dir.mkdir()
            mark = {
                "start": 30,
                "end": 90,
                "title": "更新后的范围",
                "slice_anchor": 60,
            }
            manifest = self._build(root, mark)
            task = manifest["tasks"][0]
            previous_subtitle = (root / "旧自动字幕.srt").resolve()
            task["subtitle_path"] = str(previous_subtitle)
            task["corrected_srt_path"] = str(previous_subtitle)
            manifest_path = Path(manifest["manifest_json_path"])
            write_refinement_manifest_files(manifest)
            clip_path = (report_dir / task["clip_filename"]).resolve()
            clip_path.write_bytes(b"clip")

            self.assertTrue(
                update_refinement_manifest_after_slice(
                    manifest_path,
                    report_dir,
                    [mark],
                )
            )
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))

        task = saved["tasks"][0]
        self.assertIsNone(task["subtitle_path"])
        self.assertIsNone(task["corrected_srt_path"])

    def test_explicit_invalid_source_metadata_is_not_replaced_with_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid = self._build(
                root,
                {
                    "start": 10,
                    "end": 20,
                    "title": "无效来源声明",
                    "slice_anchor": 15,
                    "clip_timebase": 123,
                    "source_segment_count": True,
                },
            )["tasks"][0]
            multisegment = self._build(
                root,
                {
                    "start": 10,
                    "end": 20,
                    "title": "多段来源",
                    "slice_anchor": 15,
                    "clip_timebase": "source_video_seconds",
                    "source_segment_count": 2,
                },
            )["tasks"][0]

        self.assertIsNone(invalid["clip_timebase"])
        self.assertIsNone(invalid["source_segment_count"])
        self.assertEqual(multisegment["source_segment_count"], 2)


if __name__ == "__main__":
    unittest.main()
