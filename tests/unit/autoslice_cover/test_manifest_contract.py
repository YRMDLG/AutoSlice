"""AutoCover 稳定整理包契约读取测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoslice_cover.manifest_contract import RefinementManifestContract


class RefinementManifestContractTests(unittest.TestCase):
    """只允许通过整理包声明的绝对路径精确关联切片。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original = (self.root / "01_原始切片.mp4").resolve()
        self.final = (self.root / "最终精剪.mp4").resolve()
        self.subtitle = (self.root / "最终精剪.srt").resolve()
        self.original.write_bytes(b"original")
        self.final.write_bytes(b"final")
        self.subtitle.write_text("字幕", encoding="utf-8")
        self.manifest_path = self.root / "精调任务.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, tasks: list[dict[str, object]]) -> None:
        self.manifest_path.write_text(
            json.dumps({"schema_version": 1, "tasks": tasks}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _task(self, **overrides: object) -> dict[str, object]:
        task: dict[str, object] = {
            "id": "01",
            "clip_timebase": "source_video_seconds",
            "source_segment_count": 1,
            "clip_start_seconds": 100,
            "clip_end_seconds": 160,
            "slice_anchor": 132,
            "slice_anchor_source": "语义复核",
            "cover_anchor_seconds": 32,
            "cover_anchor_media_path": str(self.original),
            "editorial_interest_score": 4.5,
            "editorial_interest_reason": "结尾反转完整",
            "publish_title": "【测试】最后一句突然反转",
            "original_slice_path": str(self.original),
            "final_clip_path": None,
            "corrected_srt_path": str(self.subtitle),
        }
        task.update(overrides)
        return task

    def test_exact_original_path_exposes_stable_metadata(self) -> None:
        self._write([self._task()])

        match = RefinementManifestContract.load(self.manifest_path).match(self.original)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.match_source, "manifest_original_slice")
        self.assertEqual(match.publish_title, "【测试】最后一句突然反转")
        self.assertEqual(match.cover_anchor_seconds, 32.0)
        self.assertEqual(match.slice_anchor_source, "语义复核")
        self.assertEqual(match.editorial_interest_score, 4.5)
        self.assertEqual(match.editorial_interest_reason, "结尾反转完整")
        self.assertTrue(match.subtitle_exists)
        self.assertEqual(match.subtitle_filename, "最终精剪.srt")

    def test_exact_final_path_requires_anchor_bound_to_final_media(self) -> None:
        self._write([
            self._task(
                final_clip_path=str(self.final),
                cover_anchor_seconds=7.25,
                cover_anchor_media_path=str(self.final),
            )
        ])

        match = RefinementManifestContract.load(self.manifest_path).match(self.final)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.match_source, "manifest_final_clip")
        self.assertEqual(match.cover_anchor_seconds, 7.25)

    def test_final_only_path_exposes_title_metadata_and_bound_anchor(self) -> None:
        task = self._task(
            final_clip_path=str(self.final),
            cover_anchor_seconds=6.75,
            cover_anchor_media_path=str(self.final),
        )
        task.pop("original_slice_path")
        self._write([task])

        match = RefinementManifestContract.load(self.manifest_path).match(self.final)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.match_source, "manifest_final_clip")
        self.assertEqual(match.publish_title, "【测试】最后一句突然反转")
        self.assertEqual(match.cover_anchor_seconds, 6.75)
        self.assertEqual(match.slice_anchor_source, "语义复核")
        self.assertEqual(match.editorial_interest_score, 4.5)
        self.assertEqual(match.editorial_interest_reason, "结尾反转完整")
        self.assertTrue(match.subtitle_exists)
        self.assertEqual(match.subtitle_filename, "最终精剪.srt")

    def test_explicit_final_anchor_does_not_require_reusing_source_slice_anchor(self) -> None:
        self._write([
            self._task(
                final_clip_path=str(self.final),
                slice_anchor=None,
                cover_anchor_seconds=5.5,
                cover_anchor_media_path=str(self.final),
            )
        ])

        match = RefinementManifestContract.load(self.manifest_path).match(self.final)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.cover_anchor_seconds, 5.5)

    def test_final_path_without_final_bound_anchor_does_not_reuse_original_anchor(self) -> None:
        self._write([self._task(final_clip_path=str(self.final))])

        match = RefinementManifestContract.load(self.manifest_path).match(self.final)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertIsNone(match.cover_anchor_seconds)

    def test_renamed_file_and_duplicate_path_are_not_guessed(self) -> None:
        renamed = (self.root / "用户重命名.mp4").resolve()
        renamed.write_bytes(b"renamed")
        self._write([self._task()])
        contract = RefinementManifestContract.load(self.manifest_path)

        self.assertIsNone(contract.match(renamed))

        self._write([self._task(id="01"), self._task(id="02")])
        ambiguous = RefinementManifestContract.load(self.manifest_path)
        self.assertIsNone(ambiguous.match(self.original))

    def test_unknown_timebase_multisegment_and_conflicting_paths_are_rejected(self) -> None:
        conflicting = (self.root / "冲突路径.mp4").resolve()
        conflicting.write_bytes(b"conflict")
        cases = (
            self._task(clip_timebase="unknown"),
            self._task(source_segment_count=2),
            self._task(slice_path=str(conflicting)),
        )
        for task in cases:
            with self.subTest(task=task):
                self._write([task])
                self.assertIsNone(
                    RefinementManifestContract.load(self.manifest_path).match(
                        self.original
                    )
                )

    def test_conflicting_original_anchor_is_not_exposed_as_reliable(self) -> None:
        self._write([self._task(cover_anchor_seconds=12)])

        match = RefinementManifestContract.load(self.manifest_path).match(self.original)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertIsNone(match.cover_anchor_seconds)

    def test_missing_publish_title_keeps_reliable_non_title_metadata(self) -> None:
        task = self._task()
        task.pop("publish_title")
        self._write([task])

        match = RefinementManifestContract.load(self.manifest_path).match(self.original)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertIsNone(match.publish_title)
        self.assertEqual(match.cover_anchor_seconds, 32.0)
        self.assertEqual(match.editorial_interest_score, 4.5)

    def test_legacy_missing_and_damaged_manifests_are_safe_empty_contracts(self) -> None:
        self._write([
            {
                "id": "01",
                "slice_path": str(self.original),
                "publish_title": "旧任务标题",
            }
        ])
        self.assertIsNone(
            RefinementManifestContract.load(self.manifest_path).match(self.original)
        )

        self.manifest_path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(
            RefinementManifestContract.load(self.manifest_path).match(self.original)
        )
        self.assertIsNone(
            RefinementManifestContract.load(self.root / "不存在.json").match(
                self.original
            )
        )

    def test_legacy_slice_path_alone_does_not_create_a_new_contract(self) -> None:
        task = self._task(slice_path=str(self.original))
        task.pop("original_slice_path")
        self._write([task])

        self.assertIsNone(
            RefinementManifestContract.load(self.manifest_path).match(self.original)
        )


if __name__ == "__main__":
    unittest.main()
