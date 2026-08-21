import json
import tempfile
import unittest
from pathlib import Path

from autoslice import pipeline_retry
from autoslice.analysis.manual import candidates as manual_candidates


class PrepareRetryPipelineStateTests(unittest.TestCase):

    def _run(
            self, payload, checkpoint=None, *, policy_matches=True, focus=90,
            manual_entries=None, merge_callback=None):
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "recording.flv"
            json_path = root / "clip-marks.json"
            checkpoint_path = root / "clip-review.json"
            video_path.write_bytes(b"")
            payload = dict(payload)
            payload["clip_review_checkpoint_path"] = str(checkpoint_path)
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            if checkpoint is not None:
                checkpoint_path.write_text(
                    json.dumps(checkpoint, ensure_ascii=False),
                    encoding="utf-8",
                )

            layout = {
                "data_dir": str(root / "data"),
                "clip_marks_path": str(root / "default-clip-marks.json"),
                "report_path": str(root / "report.md"),
                "clip_review_checkpoint_path": str(root / "default-review.json"),
                "output_root": str(root),
                "artifact_dir": str(root / "artifacts"),
            }

            def clean_topics(topics):
                return list(topics)

            def snapshot(topics):
                return [dict(topic) for topic in topics]

            def merge(topics, entries):
                calls.append(("merge", entries))
                if merge_callback:
                    merge_callback(topics, entries)
                else:
                    for topic in topics:
                        topic["manual_merged"] = True

            result = pipeline_retry.prepare_retry_pipeline_state(
                video_path,
                json_path=str(json_path),
                artifact_bundle_layout=lambda *_args, **_kwargs: layout,
                organize_existing_artifacts=lambda *_args, **_kwargs: calls.append(
                    ("organize",)
                ),
                seed_artifact_from_legacy=lambda *args: calls.append(
                    ("seed", args)
                ),
                manual_timeline_for_rebuilt_report=lambda *_args: {
                    "entries": manual_entries or [{"start": 10, "end": 20}]
                },
                parse_generated_topic_report=lambda _path: [{
                    "title": "报告恢复话题",
                    "start": 1,
                    "end": 8,
                }],
                clean_topics_for_report=clean_topics,
                analysis_topics_snapshot=snapshot,
                merge_manual_timeline_topics=merge,
                clip_review_checkpoint_matches_policy=lambda _checkpoint: (
                    policy_matches
                ),
                clip_review_checkpoint_is_complete=lambda *_args: True,
                topic_review_focus_max_sec=focus,
            )
        return result, calls

    def test_restores_artifacts_and_marks_stale_checkpoint_for_rerun(self):
        topic = {
            "title": "旧策略话题",
            "start": 100,
            "end": 140,
            "clip_review_attempts": 1,
            "clip_review_validated": True,
        }
        result, calls = self._run(
            {
                "analysis_topics": [{
                    "title": "基线话题",
                    "start": 1,
                    "end": 8,
                }],
            },
            checkpoint={"stage": "completed", "topics": [topic]},
            policy_matches=False,
        )

        self.assertTrue(result["checkpoint_policy_stale"])
        self.assertFalse(result["resume_review"])
        self.assertFalse(result["reuse_completed_review"])
        self.assertEqual(
            result["stale_review_keys"],
            {(100, 140, "旧策略话题")},
        )
        self.assertEqual(result["accepted_topics"][0]["title"], "旧策略话题")
        self.assertEqual(result["analysis_topics"][0]["title"], "基线话题")
        self.assertEqual(calls[0][0], "merge")
        self.assertEqual(calls[1][0], "seed")

    def test_downgrades_long_validated_topic_before_resuming(self):
        topic = {
            "title": "过长候选",
            "start": 0,
            "end": 120,
            "can_slice": True,
            "clip_review_validated": True,
        }
        result, _calls = self._run(
            {"analysis_topics": [{"title": "基线", "start": 1, "end": 5}]},
            checkpoint={
                "stage": "reviewing",
                "topics": [topic],
            },
            focus=90,
        )

        recovered = result["accepted_topics"][0]
        self.assertTrue(result["resume_review"])
        self.assertFalse(result["reuse_completed_review"])
        self.assertFalse(recovered["clip_review_validated"])
        self.assertEqual(recovered["clip_review_rejection"], "等待独立字幕复核")
        self.assertTrue(recovered["can_slice"])
        self.assertEqual(result["stale_review_keys"], set())

    def test_recovers_baseline_from_report_and_reuses_completed_checkpoint(self):
        completed_topic = {
            "title": "已完成复核",
            "start": 20,
            "end": 40,
            "can_slice": True,
            "clip_review_validated": True,
        }
        result, _calls = self._run(
            {"analysis_topics": []},
            checkpoint={
                "stage": "completed",
                "topics": [completed_topic],
            },
        )

        self.assertEqual(result["analysis_topics"][0]["title"], "报告恢复话题")
        self.assertEqual(result["accepted_topics"][0]["title"], "已完成复核")
        self.assertFalse(result["resume_review"])
        self.assertTrue(result["reuse_completed_review"])
        self.assertFalse(result["checkpoint_policy_stale"])
        self.assertTrue({
            "data",
            "rebuilt_manual_timeline",
            "analysis_topics",
            "accepted_topics",
            "clip_review_checkpoint_path",
            "resume_review",
            "reuse_completed_review",
            "checkpoint_policy_stale",
            "stale_review_keys",
        }.issubset(result))

    def test_completed_checkpoint_keeps_new_matching_manual_candidate_pending(self):
        manual_entry = {
            "start": 10,
            "end": 20,
            "text": "人工时间轴重点",
            "stars": 0,
            "source": "optimized_manual_timeline",
        }
        result, _calls = self._run(
            {
                "analysis_topics": [{
                    "title": "最新分析话题",
                    "start": 10,
                    "end": 20,
                    "body": ["·完整事件"],
                    "ai_enriched": True,
                    "ai_focus_validated": True,
                }],
            },
            checkpoint={
                "stage": "completed",
                "topics": [{
                    "title": "旧已复核话题",
                    "start": 30,
                    "end": 40,
                    "clip_review_validated": True,
                }],
            },
            manual_entries=[manual_entry],
            merge_callback=manual_candidates.merge_manual_timeline_topics,
        )

        pending = next(
            topic for topic in result["accepted_topics"] if topic["start"] == 10
        )
        self.assertTrue(result["resume_review"])
        self.assertFalse(result["reuse_completed_review"])
        self.assertTrue(pending["clip_review_candidate"])
        self.assertEqual(pending["clip_review_rejection"], "等待独立字幕复核")

    def test_completed_checkpoint_reopens_same_range_when_manual_evidence_is_new(self):
        manual_entry = {
            "start": 10,
            "end": 20,
            "text": "人工时间轴重点",
            "stars": 0,
            "source": "optimized_manual_timeline",
        }
        result, _calls = self._run(
            {
                "analysis_topics": [{
                    "title": "最新分析话题",
                    "start": 10,
                    "end": 20,
                    "body": ["·完整事件"],
                    "ai_enriched": True,
                    "ai_focus_validated": True,
                }],
            },
            checkpoint={
                "stage": "completed",
                "topics": [{
                    "title": "旧已复核话题",
                    "start": 10,
                    "end": 20,
                    "body": ["·完整事件"],
                    "clip_review_validated": True,
                    "clip_review_attempts": 1,
                }],
            },
            manual_entries=[manual_entry],
            merge_callback=manual_candidates.merge_manual_timeline_topics,
        )

        pending = next(
            topic for topic in result["accepted_topics"] if topic["start"] == 10
        )
        self.assertTrue(result["resume_review"])
        self.assertFalse(result["reuse_completed_review"])
        self.assertTrue(pending["clip_review_candidate"])
        self.assertFalse(pending["clip_review_validated"])
        self.assertEqual(pending["clip_review_attempts"], 0)
        self.assertEqual(
            pending["manual_timeline"][0]["text"],
            "人工时间轴重点",
        )

    def test_completed_checkpoint_reopens_persisted_unreviewed_manual_candidate(self):
        manual_entry = {
            "start": 10,
            "end": 20,
            "text": "人工时间轴重点",
            "stars": 0,
            "source": "optimized_manual_timeline",
        }
        result, _calls = self._run(
            {
                "analysis_topics": [{
                    "title": "最新分析话题",
                    "start": 10,
                    "end": 20,
                    "body": ["·完整事件"],
                }],
            },
            checkpoint={
                "stage": "completed",
                "topics": [{
                    "title": "旧已复核话题",
                    "start": 10,
                    "end": 20,
                    "body": ["·完整事件"],
                    "manual_timeline": [manual_entry],
                }],
            },
            manual_entries=[manual_entry],
            merge_callback=manual_candidates.merge_manual_timeline_topics,
        )

        pending = result["accepted_topics"][0]
        self.assertTrue(result["resume_review"])
        self.assertFalse(result["reuse_completed_review"])
        self.assertTrue(pending["manual_timeline_review"])
        self.assertTrue(pending["clip_review_candidate"])
        self.assertEqual(pending["clip_review_rejection"], "等待独立字幕复核")

    def test_organizes_legacy_clip_marks_before_loading_default_artifact(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "recording.flv"
            video_path.write_bytes(b"")
            legacy_json_path = root / "recording_clip_marks.json"
            legacy_json_path.write_text(
                json.dumps({
                    "analysis_topics": [{
                        "title": "旧产物话题",
                        "start": 3,
                        "end": 9,
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            clip_marks_path = root / "artifacts" / "clip-marks.json"
            layout = {
                "data_dir": str(root / "artifacts" / "data"),
                "clip_marks_path": str(clip_marks_path),
                "report_path": str(root / "artifacts" / "report.md"),
                "clip_review_checkpoint_path": str(
                    root / "artifacts" / "review.json"
                ),
                "output_root": str(root),
                "artifact_dir": str(root / "artifacts"),
            }

            def organize(_video_path, **kwargs):
                calls.append(kwargs)
                clip_marks_path.parent.mkdir(parents=True, exist_ok=True)
                clip_marks_path.write_bytes(Path(kwargs["json_path"]).read_bytes())

            result = pipeline_retry.prepare_retry_pipeline_state(
                video_path,
                artifact_bundle_layout=lambda *_args, **_kwargs: layout,
                organize_existing_artifacts=organize,
                seed_artifact_from_legacy=lambda *_args: None,
                manual_timeline_for_rebuilt_report=lambda *_args: {"entries": []},
                parse_generated_topic_report=lambda _path: [],
                clean_topics_for_report=lambda topics: list(topics),
                analysis_topics_snapshot=lambda topics: [
                    dict(topic) for topic in topics
                ],
                merge_manual_timeline_topics=lambda *_args: None,
                clip_review_checkpoint_matches_policy=lambda _checkpoint: True,
                clip_review_checkpoint_is_complete=lambda *_args: False,
                topic_review_focus_max_sec=90,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["json_path"], str(legacy_json_path))
        self.assertEqual(result["json_path"], str(clip_marks_path))
        self.assertEqual(result["accepted_topics"][0]["title"], "旧产物话题")


if __name__ == "__main__":
    unittest.main()
