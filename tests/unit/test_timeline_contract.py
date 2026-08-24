import copy
import json
import unittest

from autoslice import timeline_contract


class TimelineContractTests(unittest.TestCase):
    def test_serializes_existing_owners_without_using_quality_overview(self):
        clips = [{
            "start": 20,
            "end": 40.25,
            "title": "成片片段",
            "slice_anchor_source": "语义复核",
            "editorial_interest_reason": "诱因和结果完整",
        }]
        audit = {"candidates": [{
            "start": 60,
            "end": 75,
            "title": "边缘候选",
            "candidate_sources": ["人工时间轴"],
            "interest_reason": "有明确反转",
        }]}
        clips_before = copy.deepcopy(clips)
        audit_before = copy.deepcopy(audit)

        result = timeline_contract.serialize_timeline(
            "task-1",
            100,
            clips,
            audit,
            generated_at="2026-08-25T00:00:00Z",
        )

        self.assertEqual(
            set(result),
            {
                "schema_version", "task_id", "video_duration", "clips",
                "edge_candidates", "complete", "truncated", "generated_at",
                "incomplete_reasons",
            },
        )
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["video_duration"], 100.0)
        self.assertTrue(result["complete"])
        self.assertFalse(result["truncated"])
        for row in result["clips"] + result["edge_candidates"]:
            self.assertIsInstance(row["id"], str)
            self.assertIsInstance(row["start"], (int, float))
            self.assertIsInstance(row["end"], (int, float))
            self.assertTrue(row["source"])
            self.assertTrue(row["reason"])
        self.assertEqual(clips, clips_before)
        self.assertEqual(audit, audit_before)

    def test_explicit_empty_data_is_complete_but_missing_old_data_is_incomplete(self):
        empty = timeline_contract.serialize_timeline(
            "task-empty", 0, [], {"candidates": []}, generated_at="fixed"
        )
        missing = timeline_contract.serialize_timeline(
            "task-old", 0, generated_at="fixed"
        )

        self.assertTrue(empty["complete"])
        self.assertEqual(empty["clips"], [])
        self.assertEqual(empty["edge_candidates"], [])
        self.assertFalse(missing["complete"])
        self.assertEqual(missing["truncated"], False)
        self.assertIn("clips_missing", missing["incomplete_reasons"])
        self.assertIn("edge_candidates_missing", missing["incomplete_reasons"])

    def test_missing_and_abnormal_times_are_excluded_without_guessing(self):
        result = timeline_contract.serialize_timeline(
            "task-invalid",
            100,
            [
                {"start": 10, "title": "缺结束"},
                {"start": -1, "end": 5, "source": "x", "reason": "y"},
                {"start": 20, "end": 10, "source": "x", "reason": "y"},
                {"start": 90, "end": 101, "source": "x", "reason": "y"},
                {"start": float("nan"), "end": 30, "source": "x", "reason": "y"},
            ],
            {"candidates": []},
            generated_at="fixed",
        )

        self.assertEqual(result["clips"], [])
        self.assertFalse(result["complete"])
        self.assertFalse(result["truncated"])
        self.assertIn("clip_time_missing", result["incomplete_reasons"])
        self.assertIn("clip_time_invalid", result["incomplete_reasons"])

    def test_duplicate_ids_keep_first_record_and_mark_incomplete(self):
        result = timeline_contract.serialize_timeline(
            "task-duplicate",
            100,
            [
                {"id": "same", "start": 10, "end": 20, "source": "a", "reason": "first"},
                {"id": "same", "start": 30, "end": 40, "source": "b", "reason": "second"},
            ],
            {"candidates": [{
                "id": "same", "start": 50, "end": 60, "source": "c", "reason": "third"
            }]},
            generated_at="fixed",
        )

        self.assertEqual(len(result["clips"]), 1)
        self.assertEqual(result["clips"][0]["start"], 10.0)
        self.assertEqual(result["edge_candidates"], [])
        self.assertFalse(result["complete"])
        self.assertIn("duplicate_id", result["incomplete_reasons"])

    def test_derived_ids_are_stable_and_rows_are_sorted(self):
        first = timeline_contract.serialize_timeline(
            "task-stable", 100,
            [
                {"start": 20, "end": 30, "source": "b", "reason": "r2"},
                {"start": 5, "end": 10, "source": "a", "reason": "r1"},
            ],
            {"candidates": []}, generated_at="fixed"
        )
        second = timeline_contract.serialize_timeline(
            "task-stable", 100,
            [
                {"start": 5, "end": 10, "source": "a", "reason": "r1"},
                {"start": 20, "end": 30, "source": "b", "reason": "r2"},
            ],
            {"candidates": []}, generated_at="fixed"
        )
        changed_reason = timeline_contract.serialize_timeline(
            "task-stable", 100,
            [{"start": 5, "end": 10, "source": "a", "reason": "后续补充理由"}],
            {"candidates": []}, generated_at="fixed"
        )

        self.assertEqual(first["clips"], second["clips"])
        self.assertEqual(first["clips"][0]["id"], changed_reason["clips"][0]["id"])
        self.assertTrue(first["complete"])
        self.assertNotIn("clip_id_derived", first["incomplete_reasons"])

    def test_oversized_rows_are_explicitly_truncated(self):
        clips = [
            {"id": f"clip-{index}", "start": index, "end": index + 0.5,
             "source": "test", "reason": "test"}
            for index in range(timeline_contract.MAX_CLIPS + 1)
        ]
        result = timeline_contract.serialize_timeline(
            "task-long", timeline_contract.MAX_CLIPS + 2, clips, {"candidates": []},
            generated_at="fixed"
        )

        self.assertEqual(len(result["clips"]), timeline_contract.MAX_CLIPS)
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertIn("clips_limit", result["incomplete_reasons"])

    def test_byte_budget_also_sets_truncated_instead_of_silent_crop(self):
        result = timeline_contract.serialize_timeline(
            "task-bytes", 100,
            [{"id": "one", "start": 1, "end": 2, "source": "s", "reason": "r"}],
            {"candidates": []}, generated_at="fixed", max_bytes=240
        )

        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), 240)
        self.assertEqual(result["clips"], [])
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertIn("payload_limit", result["incomplete_reasons"])

    def test_local_paths_are_not_exposed_and_make_payload_incomplete(self):
        result = timeline_contract.serialize_timeline(
            r"F:\\private\\task.json",
            100,
            [{
                "start": 1,
                "end": 2,
                "title": r"C:\\private\\title.srt",
                "source": r"file:///C:/private/source.txt",
                "reason": "../private/reason.txt",
            }],
            {"candidates": []},
            generated_at="fixed",
        )

        self.assertEqual(result["task_id"], "unknown-task")
        row = result["clips"][0]
        self.assertEqual(row["title"], "未命名")
        self.assertEqual(row["source"], "未知来源")
        self.assertEqual(row["reason"], "未记录原因")
        self.assertFalse(result["complete"])
        self.assertIn("task_id_missing", result["incomplete_reasons"])
        self.assertIn("clip_title_missing", result["incomplete_reasons"])
        self.assertIn("clip_source_missing", result["incomplete_reasons"])
        self.assertIn("clip_reason_missing", result["incomplete_reasons"])

    def test_long_text_is_bounded_and_explicitly_incomplete(self):
        result = timeline_contract.serialize_timeline(
            "task-long-text",
            100,
            [{
                "start": 1,
                "end": 2,
                "title": "标题" * 200,
                "source": "来源" * 100,
                "reason": "原因" * 200,
            }],
            {"candidates": []},
            generated_at="fixed",
        )

        row = result["clips"][0]
        self.assertLessEqual(len(row["title"]), timeline_contract.MAX_TITLE_CHARS)
        self.assertLessEqual(len(row["source"]), timeline_contract.MAX_SOURCE_CHARS)
        self.assertLessEqual(len(row["reason"]), timeline_contract.MAX_REASON_CHARS)
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertIn("text_limit", result["incomplete_reasons"])

    def test_invalid_explicit_id_is_not_exposed(self):
        result = timeline_contract.serialize_timeline(
            "task-id",
            100,
            [{
                "id": r"C:\\private\\clip.json",
                "start": 1,
                "end": 2,
                "source": "测试",
                "reason": "测试",
            }],
            {"candidates": []},
            generated_at="fixed",
        )

        self.assertFalse(result["complete"])
        self.assertIn("clip_id_invalid", result["incomplete_reasons"])
        self.assertNotIn("C:\\private", result["clips"][0]["id"])


if __name__ == "__main__":
    unittest.main()
