import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from autoslice.pipeline_reporting import (
    build_context_policy,
    build_danmaku_selection_policy,
    prepare_pipeline_report,
)


class PreparePipelineReportTests(unittest.TestCase):

    def _run(self, *, build_report=None, root="bundle"):
        layout = {
            "artifact_dir": root,
            "overview_path": f"{root}/00_概览.md",
            "report_path": f"{root}/01_话题分析.md",
            "clip_marks_path": f"{root}/数据/clip_marks.json",
            "task_manifest_json_path": f"{root}/数据/精调任务.json",
            "task_manifest_md_path": f"{root}/02_精调任务.md",
            "unified_queue_json_path": f"{root}/queue.json",
            "unified_queue_md_path": f"{root}/queue.md",
        }
        accepted_topics = [{"title": "候选", "start": 1, "end": 2}]
        analysis_topics = [{"title": "分析快照", "start": 1, "end": 2}]
        clip_marks = [{"start": 1, "end": 2, "title": "切片"}]
        manual_timeline = [{"start": 4, "end": 5}]
        calls = []

        if build_report is None:
            def build_report(*args, **kwargs):
                calls.append(("report", args, kwargs))
                return "完整报告"

        def timeline_summary(timeline):
            calls.append(("timeline", timeline))
            return {"entries": len(timeline)}

        context_policy = build_context_policy(
            pre_context_sec=10,
            post_context_sec=20,
            min_clip_sec=30,
            max_clip_sec=40,
            required_context_overflow_sec=5,
        )
        result = prepare_pipeline_report(
            video_path="videos/recording.flv",
            artifact_layout=layout,
            source_srt_path=f"{root}/数据/source.srt",
            corrected_srt_path=f"{root}/数据/corrected.srt",
            topic_analysis_checkpoint_path=f"{root}/数据/topic.json",
            clip_review_checkpoint_path=f"{root}/数据/review.json",
            candidate_review_audit_path=f"{root}/数据/audit.json",
            accepted_topics=accepted_topics,
            analysis_topics=analysis_topics,
            clip_marks=clip_marks,
            peak_info="峰值",
            failed_chunks=[3],
            api_precheck_warning="上游警告",
            clip_review_warning="复核警告",
            manual_timeline=manual_timeline,
            streamer_profile=SimpleNamespace(
                id="profile-id",
                canonical_name="canonical-name",
                report_name="展示名",
            ),
            video_duration=120.5,
            average_density=12.3456,
            density_threshold=20.9876,
            local_peak_radius_sec=8,
            manual_review_min_stars=4,
            min_editorial_interest_score=70,
            context_policy=context_policy,
            topic_analysis_model="luna",
            review_model="terra",
            clip_review_completed_at="2026-08-20T12:00:00",
            artifact_layout_version="v2",
            build_timeline_report=build_report,
            manual_timeline_summary=timeline_summary,
        )
        return {
            "result": result,
            "calls": calls,
            "layout": layout,
            "accepted_topics": accepted_topics,
            "analysis_topics": analysis_topics,
            "clip_marks": clip_marks,
            "manual_timeline": manual_timeline,
            "context_policy": context_policy,
        }

    def test_builds_report_with_complete_context(self):
        state = self._run()
        result = state["result"]
        calls = state["calls"]

        self.assertEqual(result["report"], "完整报告")
        self.assertEqual([call[0] for call in calls], ["report", "timeline"])
        report_call = calls[0]
        self.assertEqual(report_call[1], (
            "recording.flv",
            "峰值",
            state["accepted_topics"],
        ))
        self.assertEqual(report_call[2]["failed_chunks"], [3])
        self.assertEqual(report_call[2]["api_warning"], "上游警告")
        self.assertEqual(report_call[2]["streamer_name"], "展示名")
        self.assertTrue(report_call[2]["group_by_hour"])
        self.assertEqual(report_call[2]["topic_analysis_model"], "luna")
        self.assertEqual(report_call[2]["review_model"], "terra")
        self.assertIs(report_call[2]["manual_timeline"], state["manual_timeline"])
        self.assertIs(report_call[2]["clip_marks"], state["clip_marks"])
        self.assertEqual(report_call[2]["report_dir"], "bundle")

    def test_builds_complete_payload_without_copying_topic_results(self):
        state = self._run()
        payload = state["result"]["payload"]
        layout = state["layout"]

        self.assertEqual(payload["video"], "recording.flv")
        self.assertEqual(payload["streamer_profile_id"], "profile-id")
        self.assertEqual(payload["streamer_name"], "canonical-name")
        self.assertEqual(payload["streamer_display_name"], "展示名")
        self.assertEqual(payload["artifact_layout_version"], "v2")
        self.assertEqual(payload["artifact_dir"], layout["artifact_dir"])
        self.assertEqual(payload["overview_path"], layout["overview_path"])
        self.assertEqual(payload["analysis_report_path"], layout["report_path"])
        self.assertEqual(payload["video_duration"], 120.5)
        self.assertEqual(payload["model_policy"], {
            "topic_analysis": "luna",
            "manual_timeline_review": "terra",
            "clip_candidate_review": "terra",
        })
        self.assertEqual(payload["time_basis"], "video_elapsed_seconds")
        self.assertTrue(payload["expanded_with_context"])
        self.assertIs(payload["context_policy"], state["context_policy"])
        self.assertIs(payload["analysis_topics"], state["analysis_topics"])
        self.assertIs(payload["clip_marks"], state["clip_marks"])
        self.assertEqual(payload["manual_timeline"], {"entries": 1})
        self.assertEqual(payload["failed_chunks"], [3])
        self.assertEqual(payload["api_precheck_warning"], "上游警告")
        self.assertEqual(payload["clip_review_warning"], "复核警告")
        self.assertEqual(
            payload["clip_review_completed_at"],
            "2026-08-20T12:00:00",
        )
        self.assertEqual(
            state["result"]["clip_marks_path"],
            layout["clip_marks_path"],
        )
        self.assertEqual(
            state["result"]["task_manifest_md_path"],
            layout["task_manifest_md_path"],
        )

    def test_shared_policy_builders_preserve_exact_business_contract(self):
        self.assertEqual(build_context_policy(
            pre_context_sec=1,
            post_context_sec=2,
            min_clip_sec=3,
            max_clip_sec=4,
            required_context_overflow_sec=5,
        ), {
            "pre_context_sec": 1,
            "post_context_sec": 2,
            "min_clip_sec": 3,
            "max_clip_sec": 4,
            "required_context_overflow_sec": 5,
        })
        policy = build_danmaku_selection_policy(
            average_density=12.3456,
            density_threshold=20.9876,
            local_peak_radius_sec=8,
            manual_review_min_stars=4,
            min_editorial_interest_score=70,
        )
        self.assertEqual(policy["average_density"], 12.346)
        self.assertEqual(policy["density_threshold"], 20.988)
        self.assertEqual(policy["local_peak_radius_sec"], 8)
        self.assertIsNone(policy["max_clips_per_hour"])
        self.assertFalse(policy["fixed_hourly_quota"])
        self.assertEqual(policy["min_editorial_interest_score"], 70)
        self.assertFalse(policy["manual_star_can_force_slice"])
        self.assertEqual(policy["manual_star_review_min_stars"], 4)
        self.assertTrue(policy["semantic_review_can_keep_peak_moved_focus"])
        self.assertTrue(policy["independent_subtitle_review_required"])

    def test_does_not_write_files(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            state = self._run(root=str(root / "bundle"))

            self.assertEqual(list(root.rglob("*")), [])
            self.assertEqual(state["result"]["report"], "完整报告")

    def test_report_callback_errors_are_not_swallowed(self):
        failure = RuntimeError("report failed")

        with self.assertRaises(RuntimeError) as raised:
            self._run(
                build_report=lambda *_args, **_kwargs: (
                    (_ for _ in ()).throw(failure)
                ),
            )

        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
