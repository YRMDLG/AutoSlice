import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from autoslice.analysis import checkpoints
from autoslice.streamer_profiles import streamer_profile_context


class TopicAnalysisCheckpointTests(unittest.TestCase):
    def test_fingerprint_and_round_trip_include_analysis_policy(self):
        first = checkpoints.topic_analysis_prompt_fingerprint(
            "完整提示",
            "紧凑提示",
            schema_version=1,
            model="luna",
            max_tokens=16000,
            compact_max_tokens=12000,
        )
        changed = checkpoints.topic_analysis_prompt_fingerprint(
            "完整提示",
            "紧凑提示",
            schema_version=1,
            model="terra",
            max_tokens=16000,
            compact_max_tokens=12000,
        )
        self.assertNotEqual(first, changed)

        with TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "topic.json")
            responses = {"chunk-1": {"response": "测试", "fingerprint": first}}
            self.assertTrue(
                checkpoints.write_topic_analysis_checkpoint(
                    path,
                    responses,
                    1,
                    schema_version=1,
                    model="luna",
                )
            )
            self.assertEqual(
                checkpoints.load_topic_analysis_checkpoint(path, schema_version=1),
                responses,
            )
            self.assertEqual(
                checkpoints.load_topic_analysis_checkpoint(path, schema_version=2),
                {},
            )

    def test_corrupt_topic_checkpoint_is_not_reused(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "topic.json"
            path.write_text("{invalid", encoding="utf-8")
            self.assertEqual(
                checkpoints.load_topic_analysis_checkpoint(str(path), schema_version=1),
                {},
            )


class ClipReviewCheckpointTests(unittest.TestCase):
    def test_snapshot_removes_review_state_without_mutating_source(self):
        topics = [{
            "title": "测试",
            "can_slice": True,
            "clip_review_validated": True,
            "title_review_candidates": ["标题"],
            "body": ["保留正文"],
        }]
        snapshot = checkpoints.analysis_topics_snapshot(topics)

        self.assertNotIn("can_slice", snapshot[0])
        self.assertNotIn("clip_review_validated", snapshot[0])
        self.assertNotIn("title_review_candidates", snapshot[0])
        self.assertEqual(snapshot[0]["body"], ["保留正文"])
        self.assertTrue(topics[0]["can_slice"])

    def test_written_checkpoint_records_profile_and_policy(self):
        with TemporaryDirectory() as temporary, streamer_profile_context("generic"):
            path = str(Path(temporary) / "review.json")
            topics = [{"title": "测试"}]
            result = checkpoints.write_clip_review_checkpoint(
                path,
                topics,
                stage="reviewing",
            )
            payload = json.loads(Path(path).read_text(encoding="utf-8"))

        self.assertEqual(result, path)
        self.assertEqual(payload["streamer_profile_id"], "generic")
        self.assertEqual(
            payload["review_policy_version"],
            checkpoints.CLIP_REVIEW_POLICY_VERSION,
        )
        self.assertEqual(payload["stage"], "reviewing")
        self.assertEqual(payload["topics"], topics)

    def test_policy_match_and_legacy_completion_are_preserved(self):
        topics = [{
            "clip_review_attempts": 1,
            "clip_review_validated": False,
        }]
        legacy_complete = {
            "stage": "reviewing",
            "pending_count": 0,
            "batch_index": 2,
            "total_batches": 2,
        }
        self.assertTrue(
            checkpoints.clip_review_checkpoint_is_complete(
                legacy_complete,
                topics,
            )
        )
        with streamer_profile_context("zeyin"):
            self.assertTrue(checkpoints.clip_review_checkpoint_matches_policy({
                "review_policy_version": checkpoints.CLIP_REVIEW_POLICY_VERSION,
                "streamer_profile_id": "zeyin",
            }))
            self.assertTrue(checkpoints.clip_review_checkpoint_matches_policy({
                "review_policy_version": checkpoints.CLIP_REVIEW_POLICY_VERSION,
            }))
            self.assertFalse(checkpoints.clip_review_checkpoint_matches_policy({
                "review_policy_version": checkpoints.CLIP_REVIEW_POLICY_VERSION - 1,
                "streamer_profile_id": "zeyin",
            }))

    def test_completed_checkpoint_records_pending_candidates(self):
        with TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "review.json")
            checkpoints.write_completed_clip_review_checkpoint(
                path,
                [{
                    "can_slice": True,
                    "clip_review_rejection": "等待独立字幕复核",
                }],
                warning="上游暂不可用",
                completed_at="2026-08-17T22:10:00",
            )
            payload = json.loads(Path(path).read_text(encoding="utf-8"))

        self.assertEqual(payload["stage"], "completed_with_warning")
        self.assertEqual(payload["pending_count"], 1)
        self.assertEqual(payload["completed_at"], "2026-08-17T22:10:00")


if __name__ == "__main__":
    unittest.main()
