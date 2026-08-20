import unittest

from autoslice.pipeline_boundaries import prepare_pipeline_boundaries


class PipelineBoundariesTests(unittest.TestCase):
    def test_expands_then_synchronises_with_injected_dependencies(self):
        raw_clip_marks = [{"title": "片段", "start": 10, "end": 20}]
        accepted_topics = [{"title": "片段", "start": 10, "end": 20}]
        srt_segments = [(1, 30, "上下文")]
        expanded_marks = [{"title": "片段", "start": 4, "end": 26}]
        calls = []

        def expand(marks, *, srt_segments, video_duration):
            calls.append(("expand", marks, srt_segments, video_duration))
            return expanded_marks

        def synchronise(topics, marks):
            calls.append(("synchronise", topics, marks))

        result = prepare_pipeline_boundaries(
            raw_clip_marks,
            accepted_topics,
            srt_segments,
            120,
            expand_clip_marks_with_context=expand,
            synchronise_selected_topic_ranges=synchronise,
        )

        self.assertEqual(
            calls,
            [
                ("expand", raw_clip_marks, srt_segments, 120),
                ("synchronise", accepted_topics, expanded_marks),
            ],
        )
        self.assertIs(result["clip_marks"], expanded_marks)
        self.assertIs(result["accepted_topics"], accepted_topics)

    def test_empty_inputs_are_transparent(self):
        calls = []

        def expand(marks, *, srt_segments, video_duration):
            calls.append((marks, srt_segments, video_duration))
            return []

        def synchronise(topics, marks):
            calls.append((topics, marks))

        result = prepare_pipeline_boundaries(
            [],
            [],
            [],
            None,
            expand_clip_marks_with_context=expand,
            synchronise_selected_topic_ranges=synchronise,
        )

        self.assertEqual(result, {"clip_marks": [], "accepted_topics": []})
        self.assertEqual(calls, [([], [], None), ([], [])])

    def test_stream_outro_marks_are_passed_through_unchanged(self):
        outro_mark = {
            "title": "收播",
            "clip_type": "stream_outro",
            "preserve_to_video_end": True,
            "start": 90,
            "end": 100,
        }
        topics = [{"title": "收播", "clip_type": "stream_outro"}]
        received = []

        def expand(marks, *, srt_segments, video_duration):
            received.append((marks, srt_segments, video_duration))
            return marks

        def synchronise(received_topics, marks):
            received.append((received_topics, marks))

        result = prepare_pipeline_boundaries(
            [outro_mark],
            topics,
            [(80, 100, "收播")],
            100,
            expand_clip_marks_with_context=expand,
            synchronise_selected_topic_ranges=synchronise,
        )

        self.assertIs(result["clip_marks"][0], outro_mark)
        self.assertIs(result["accepted_topics"], topics)
        self.assertIs(received[0][0][0], outro_mark)
        self.assertIs(received[1][0], topics)

    def test_injected_exceptions_are_propagated_without_followup_calls(self):
        class ExpectedFailure(RuntimeError):
            pass

        failure = ExpectedFailure("边界扩展失败")
        synchronise_calls = []

        def expand(*_args, **_kwargs):
            raise failure

        with self.assertRaises(ExpectedFailure) as raised:
            prepare_pipeline_boundaries(
                [],
                [],
                [],
                10,
                expand_clip_marks_with_context=expand,
                synchronise_selected_topic_ranges=lambda *_args: synchronise_calls.append(True),
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(synchronise_calls, [])

        sync_failure = ExpectedFailure("范围同步失败")
        expand_calls = []

        def expand_success(*_args, **_kwargs):
            expand_calls.append(True)
            return ["expanded"]

        def synchronise_failure(*_args):
            raise sync_failure

        with self.assertRaises(ExpectedFailure) as raised:
            prepare_pipeline_boundaries(
                [],
                [],
                [],
                10,
                expand_clip_marks_with_context=expand_success,
                synchronise_selected_topic_ranges=synchronise_failure,
            )

        self.assertIs(raised.exception, sync_failure)
        self.assertEqual(expand_calls, [True])


if __name__ == "__main__":
    unittest.main()
