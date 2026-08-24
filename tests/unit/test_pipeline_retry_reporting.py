import unittest
from types import SimpleNamespace

from autoslice.pipeline_retry_reporting import prepare_retry_report


class PrepareRetryReportTests(unittest.TestCase):

    def _run(self, data=None, *, warning="已有警告", clip_warning="复核警告"):
        data = {"video_duration": 300.25} if data is None else data
        layout = {
            "artifact_dir": "bundle",
            "overview_path": "bundle/00_概览.md",
            "unified_queue_json_path": "bundle/queue.json",
            "unified_queue_md_path": "bundle/queue.md",
            "task_manifest_json_path": "bundle/task.json",
            "task_manifest_md_path": "bundle/task.md",
        }
        calls = []

        def build_report(*args, **kwargs):
            calls.append(("report", args, kwargs))
            return {"report": "完整报告"}

        def snapshot(topics):
            calls.append(("snapshot", topics))
            return [{"title": topic["title"]} for topic in topics]

        def timeline_summary(timeline):
            calls.append(("timeline", timeline))
            return {"entries": len(timeline)}

        result = prepare_retry_report(
            data=data,
            video_path=r"F:\Videos\recording.flv",
            report_path="bundle/report.md",
            artifact_layout=layout,
            source_srt_path="bundle/source.srt",
            corrected_srt_path="bundle/corrected.srt",
            clip_review_checkpoint_path="bundle/review.json",
            candidate_review_audit_path="bundle/audit.json",
            accepted_topics=[{"title": "候选", "start": 1, "end": 2}],
            clip_marks=[{"start": 1, "end": 2}],
            peak_info="峰值",
            failed_chunks=[3],
            clip_review_warning=clip_warning,
            rebuilt_manual_timeline=[{"start": 4, "end": 5}],
            streamer_profile=SimpleNamespace(
                id="profile-id",
                canonical_name="canonical-name",
                report_name="展示名",
            ),
            average_density=12.3456,
            density_threshold=20.9876,
            local_peak_radius_sec=8,
            manual_review_min_stars=4,
            min_editorial_interest_score=70,
            context_policy={"pre_context_sec": 10},
            clip_review_completed_at="2026-08-20T12:00:00",
            artifact_layout_version="v2",
            build_timeline_report=build_report,
            analysis_topics_snapshot=snapshot,
            manual_timeline_summary=timeline_summary,
            warning_without_previous_clip_review=lambda payload: warning,
        )
        return result, calls, layout

    def test_merges_warnings_and_builds_report_with_complete_context(self):
        result, calls, _layout = self._run()

        self.assertEqual(result["api_warning"], "已有警告；复核警告")
        self.assertEqual(result["report"], {"report": "完整报告"})
        self.assertEqual(result["analysis_topics"], [{"title": "候选"}])
        self.assertEqual([call[0] for call in calls], ["report", "snapshot", "timeline"])

        report_call = calls[0]
        self.assertEqual(report_call[1][0], "recording.flv")
        self.assertEqual(report_call[1][1], "峰值")
        self.assertEqual(report_call[1][2][0]["title"], "候选")
        self.assertEqual(report_call[2]["failed_chunks"], [3])
        self.assertEqual(report_call[2]["streamer_name"], "展示名")
        self.assertEqual(report_call[2]["manual_timeline"], [{"start": 4, "end": 5}])
        self.assertEqual(report_call[2]["report_dir"], "bundle")

    def test_payload_preserves_policy_and_uses_artifact_fallback_paths(self):
        data = {"unrelated": "保留", "video_duration": 300.25}
        result, _calls, layout = self._run(data)
        payload = result["payload"]

        self.assertIs(payload, data)
        self.assertEqual(payload["unrelated"], "保留")
        self.assertEqual(payload["video"], "recording.flv")
        self.assertEqual(payload["video_duration"], 300.25)
        self.assertEqual(payload["streamer_profile_id"], "profile-id")
        self.assertEqual(payload["streamer_name"], "canonical-name")
        self.assertEqual(payload["streamer_display_name"], "展示名")
        self.assertEqual(payload["artifact_dir"], layout["artifact_dir"])
        self.assertEqual(payload["overview_path"], layout["overview_path"])
        self.assertEqual(
            payload["unified_queue_json_path"],
            layout["unified_queue_json_path"],
        )
        self.assertEqual(
            result["task_manifest_md_path"],
            layout["task_manifest_md_path"],
        )
        self.assertEqual(payload["analysis_topics"], [{"title": "候选"}])
        self.assertEqual(payload["clip_marks"], [{"start": 1, "end": 2}])
        self.assertEqual(payload["manual_timeline"], {"entries": 1})
        self.assertFalse(payload["danmaku_selection_policy"]["fixed_hourly_quota"])
        self.assertIsNone(
            payload["danmaku_selection_policy"]["max_clips_per_hour"]
        )
        self.assertEqual(
            payload["danmaku_selection_policy"]["average_density"],
            12.346,
        )
        self.assertEqual(
            payload["danmaku_selection_policy"]["density_threshold"],
            20.988,
        )
        self.assertEqual(payload["clip_review_completed_at"], "2026-08-20T12:00:00")

    def test_existing_queue_and_manifest_paths_are_preserved_as_a_pair(self):
        data = {
            "unified_queue_json_path": "old/queue.json",
            "unified_queue_md_path": "old/queue.md",
            "task_manifest_json_path": "old/task.json",
            "task_manifest_md_path": "old/task.md",
        }
        result, _calls, _layout = self._run(data)

        self.assertEqual(result["unified_queue_json_path"], "old/queue.json")
        self.assertEqual(result["unified_queue_md_path"], "old/queue.md")
        self.assertEqual(result["task_manifest_json_path"], "old/task.json")
        self.assertEqual(result["task_manifest_md_path"], "old/task.md")

    def test_empty_warning_is_normalised_to_none(self):
        result, _calls, _layout = self._run(warning="", clip_warning="")
        self.assertIsNone(result["api_warning"])
        self.assertEqual(result["payload"]["api_precheck_warning"], None)

    def test_callback_errors_are_not_swallowed(self):
        with self.assertRaisesRegex(RuntimeError, "report failed"):
            prepare_retry_report(
                data={},
                video_path="recording.flv",
                report_path="report.md",
                artifact_layout={
                    "artifact_dir": "bundle",
                    "overview_path": "overview.md",
                    "unified_queue_json_path": "queue.json",
                    "unified_queue_md_path": "queue.md",
                    "task_manifest_json_path": "task.json",
                    "task_manifest_md_path": "task.md",
                },
                source_srt_path="source.srt",
                corrected_srt_path=None,
                clip_review_checkpoint_path="review.json",
                candidate_review_audit_path="audit.json",
                accepted_topics=[],
                clip_marks=[],
                peak_info="无弹幕数据",
                failed_chunks=[],
                clip_review_warning=None,
                rebuilt_manual_timeline=[],
                streamer_profile=SimpleNamespace(
                    id="id", canonical_name="canonical", report_name="report"
                ),
                average_density=0,
                density_threshold=0,
                local_peak_radius_sec=1,
                manual_review_min_stars=1,
                min_editorial_interest_score=1,
                context_policy={},
                clip_review_completed_at="now",
                artifact_layout_version="v2",
                build_timeline_report=lambda *_args, **_kwargs: (
                    (_ for _ in ()).throw(RuntimeError("report failed"))
                ),
                analysis_topics_snapshot=lambda topics: topics,
                manual_timeline_summary=lambda timeline: timeline,
                warning_without_previous_clip_review=lambda data: None,
            )


if __name__ == "__main__":
    unittest.main()
