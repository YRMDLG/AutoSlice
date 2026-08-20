import unittest

from autoslice.pipeline_decisions import prepare_pipeline_decisions


class PipelineDecisionsTests(unittest.TestCase):
    def test_prepares_candidate_artifacts_and_refreshes_detected_outro_in_order(self):
        retained_topic = {"title": "正文", "clip_type": "highlight"}
        stale_outro = {"title": "旧收播片", "clip_type": "stream_outro"}
        topics = [stale_outro, retained_topic]
        initial_mark = {"title": "正文", "start": 10, "end": 20}
        outro_mark = {
            "title": "晚安小音音",
            "start": 90,
            "end": 120,
            "clip_type": "stream_outro",
        }
        outro_topic = {
            "title": "晚安小音音",
            "start": 90,
            "end": 120,
            "clip_type": "stream_outro",
        }
        raw_marks = [initial_mark]
        audit = {"candidate_count": 1}
        segments = [(80, 100, "今天先到这里，晚安小音音")]
        snapshot = [retained_topic.copy(), outro_topic.copy()]
        streamer_profile = object()
        calls = []

        def filter_stage(predicate, received_topics):
            calls.append(("filter", list(received_topics)))
            return [topic for topic in received_topics if predicate(topic)]

        def build_marks(received_topics):
            calls.append(("marks", list(received_topics)))
            return raw_marks

        def build_audit(received_topics):
            calls.append(("audit", list(received_topics)))
            return audit

        def write_audit(path, payload):
            calls.append(("write", path, payload))

        def parse(path):
            calls.append(("parse", path))
            return segments

        def detect(received_segments, duration, **kwargs):
            calls.append(("detect", received_segments, duration, kwargs))
            return outro_mark

        def build_outro_topic(mark):
            calls.append(("outro", mark))
            return outro_topic

        def make_snapshot(received_topics):
            calls.append(("snapshot", list(received_topics)))
            return snapshot

        result = prepare_pipeline_decisions(
            topics,
            "bundle/校对字幕.srt",
            "bundle/候选复核审计.json",
            streamer_profile,
            120.5,
            filter_topics=filter_stage,
            clip_marks_from_topics=build_marks,
            build_clip_candidate_review_audit=build_audit,
            write_artifact_json=write_audit,
            parse_srt_segments=parse,
            detect_stream_outro_clip=detect,
            outro_topic_from_mark=build_outro_topic,
            analysis_topics_snapshot=make_snapshot,
        )

        self.assertEqual(
            [call[0] for call in calls],
            ["filter", "marks", "audit", "write", "parse", "detect", "outro", "snapshot"],
        )
        self.assertEqual(calls[0][1], topics)
        self.assertEqual(calls[1][1], [retained_topic])
        self.assertEqual(calls[2][1], [retained_topic])
        self.assertEqual(
            calls[3],
            ("write", "bundle/候选复核审计.json", audit),
        )
        self.assertEqual(calls[4], ("parse", "bundle/校对字幕.srt"))
        self.assertEqual(
            calls[5],
            (
                "detect",
                segments,
                120.5,
                {"streamer_profile": streamer_profile},
            ),
        )
        self.assertIs(calls[6][1], outro_mark)
        self.assertEqual(calls[7][1], [retained_topic, outro_topic])
        self.assertEqual(result["accepted_topics"], [retained_topic, outro_topic])
        self.assertIs(result["raw_clip_marks"], raw_marks)
        self.assertEqual(result["raw_clip_marks"], [initial_mark, outro_mark])
        self.assertIs(result["candidate_review_audit"], audit)
        self.assertEqual(
            result["candidate_review_audit_path"],
            "bundle/候选复核审计.json",
        )
        self.assertIs(result["srt_segments_for_context"], segments)
        self.assertIs(result["outro_mark"], outro_mark)
        self.assertIs(result["analysis_topics"], snapshot)

    def test_preserves_no_outro_result_and_propagates_injected_failures(self):
        topics = [{"title": "正文"}]
        snapshots = []

        def snapshot(received_topics):
            snapshots.append(list(received_topics))
            return [topic.copy() for topic in received_topics]

        result = prepare_pipeline_decisions(
            topics,
            "字幕.srt",
            "审计.json",
            object(),
            None,
            filter_topics=filter,
            clip_marks_from_topics=lambda _topics: [],
            build_clip_candidate_review_audit=lambda _topics: {},
            write_artifact_json=lambda *_args: None,
            parse_srt_segments=lambda _path: [],
            detect_stream_outro_clip=lambda *_args, **_kwargs: None,
            outro_topic_from_mark=lambda _mark: self.fail("不应构建收播话题"),
            analysis_topics_snapshot=snapshot,
        )

        self.assertEqual(result["accepted_topics"], topics)
        self.assertIsNot(result["accepted_topics"], topics)
        self.assertEqual(result["raw_clip_marks"], [])
        self.assertIsNone(result["outro_mark"])
        self.assertEqual(snapshots, [topics])

        class ExpectedFailure(RuntimeError):
            pass

        failure = ExpectedFailure("保留原始失败")

        def fail(*_args, **_kwargs):
            raise failure

        with self.assertRaises(ExpectedFailure) as raised:
            prepare_pipeline_decisions(
                [],
                "字幕.srt",
                "审计.json",
                object(),
                10,
                filter_topics=filter,
                clip_marks_from_topics=lambda _topics: [],
                build_clip_candidate_review_audit=lambda _topics: {},
                write_artifact_json=fail,
                parse_srt_segments=lambda _path: [],
                detect_stream_outro_clip=lambda *_args, **_kwargs: None,
                outro_topic_from_mark=lambda _mark: {},
                analysis_topics_snapshot=lambda received: received,
            )

        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
