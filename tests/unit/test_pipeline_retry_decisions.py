import unittest

from autoslice.pipeline_retry_decisions import prepare_retry_decisions


class PrepareRetryDecisionsTests(unittest.TestCase):
    def test_probes_decides_expands_and_synchronises_in_order(self):
        topics = [{"title": "候选", "start": 10, "end": 20}]
        srt_segments = [(1, 30, "字幕")]
        calls = []
        decision_topics = [{"title": "决策后", "start": 12, "end": 22}]
        expanded = [{"title": "最终", "start": 8, "end": 28}]

        def probe(path):
            calls.append(("probe", path))
            return 120

        def decisions(*args, **kwargs):
            calls.append(("decisions", args, kwargs))
            return {
                "accepted_topics": decision_topics,
                "raw_clip_marks": [{"start": 12, "end": 22}],
                "candidate_review_audit_path": "audit-new.json",
            }

        def boundaries(*args, **kwargs):
            calls.append(("boundaries", args, kwargs))
            return {"accepted_topics": decision_topics, "clip_marks": expanded}

        result = prepare_retry_decisions(
            "recording.flv",
            topics,
            "recording.srt",
            "audit.json",
            "profile",
            srt_segments,
            filter_topics=lambda callback, values: [value for value in values if callback(value)],
            probe_video_duration=probe,
            clip_marks_from_topics=lambda values: values,
            build_clip_candidate_review_audit=lambda values: values,
            write_artifact_json=lambda *_args: None,
            parse_srt_segments=lambda _path: srt_segments,
            detect_stream_outro_clip=lambda *_args, **_kwargs: None,
            outro_topic_from_mark=lambda mark: mark,
            analysis_topics_snapshot=lambda values: values,
            prepare_pipeline_decisions=decisions,
            prepare_pipeline_boundaries=boundaries,
            expand_clip_marks_with_context=lambda *_args, **_kwargs: expanded,
            synchronise_selected_topic_ranges=lambda *_args: None,
            srt_video_duration=lambda _segments: 99,
        )

        self.assertEqual([item[0] for item in calls], ["probe", "decisions", "boundaries"])
        self.assertEqual(result["candidate_review_audit_path"], "audit-new.json")
        self.assertEqual(result["clip_marks"], expanded)
        self.assertEqual(result["accepted_topics"], decision_topics)
        self.assertEqual(result["probed_video_duration"], 120)
        self.assertEqual(result["video_duration"], 120)
        self.assertEqual(calls[1][2]["srt_segments"], srt_segments)
        self.assertEqual(calls[2][1][3], 120)

    def test_uses_srt_duration_when_probe_is_unavailable(self):
        calls = []

        def decisions(*_args, **_kwargs):
            return {
                "accepted_topics": [],
                "raw_clip_marks": [],
                "candidate_review_audit_path": "audit.json",
            }

        def boundaries(*args, **kwargs):
            calls.append((args, kwargs))
            return {"accepted_topics": [], "clip_marks": []}

        result = prepare_retry_decisions(
            "recording.flv",
            [],
            "recording.srt",
            "audit.json",
            "profile",
            [],
            filter_topics=lambda *_args: [],
            probe_video_duration=lambda _path: None,
            clip_marks_from_topics=lambda values: values,
            build_clip_candidate_review_audit=lambda values: values,
            write_artifact_json=lambda *_args: None,
            parse_srt_segments=lambda _path: [],
            detect_stream_outro_clip=lambda *_args, **_kwargs: None,
            outro_topic_from_mark=lambda mark: mark,
            analysis_topics_snapshot=lambda values: values,
            prepare_pipeline_decisions=decisions,
            prepare_pipeline_boundaries=boundaries,
            expand_clip_marks_with_context=lambda *_args, **_kwargs: [],
            synchronise_selected_topic_ranges=lambda *_args: None,
            srt_video_duration=lambda _segments: 77,
        )

        self.assertEqual(result["video_duration"], 77)
        self.assertEqual(calls[0][0][3], 77)

    def test_injected_failure_is_transparent(self):
        failure = RuntimeError("决策失败")
        with self.assertRaises(RuntimeError) as raised:
            prepare_retry_decisions(
                "recording.flv",
                [],
                "recording.srt",
                "audit.json",
                "profile",
                [],
                filter_topics=lambda *_args: [],
                probe_video_duration=lambda _path: 10,
                clip_marks_from_topics=lambda values: values,
                build_clip_candidate_review_audit=lambda values: values,
                write_artifact_json=lambda *_args: None,
                parse_srt_segments=lambda _path: [],
                detect_stream_outro_clip=lambda *_args, **_kwargs: None,
                outro_topic_from_mark=lambda mark: mark,
                analysis_topics_snapshot=lambda values: values,
                prepare_pipeline_decisions=lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
                prepare_pipeline_boundaries=lambda *_args, **_kwargs: None,
                expand_clip_marks_with_context=lambda *_args, **_kwargs: [],
                synchronise_selected_topic_ranges=lambda *_args: None,
                srt_video_duration=lambda _segments: 77,
            )
        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
