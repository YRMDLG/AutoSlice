import unittest

from autoslice.pipeline_review import review_pipeline_candidates


class PipelineReviewTests(unittest.TestCase):
    def test_runs_candidate_review_stages_in_order_and_returns_title_review_state(self):
        topics = [{"title": "LLM 话题"}]
        manual_entries = [{"start": 10, "end": 20}]
        calls = []
        checkpoint_calls = []

        def merge(received_topics, received_entries):
            calls.append(("merge", received_topics, received_entries))
            received_topics.append({"title": "人工候选"})

        def validate(received_topics, **kwargs):
            calls.append(("validate", received_topics, kwargs))
            return "人工时间轴警告"

        def clean(received_topics):
            calls.append(("clean", received_topics))
            return received_topics

        def snapshot(received_topics):
            calls.append(("snapshot", received_topics))
            return [{"title": item["title"]} for item in received_topics]

        def seed(destination, legacy):
            calls.append(("seed", destination, legacy))

        def write_checkpoint(path, current, **kwargs):
            checkpoint_calls.append((path, current, kwargs))

        def apply(received_topics, received_peaks, received_density, **kwargs):
            calls.append(
                (
                    "apply",
                    received_topics,
                    received_peaks,
                    received_density,
                    kwargs,
                )
            )

        def review(received_topics, **kwargs):
            calls.append(("review", received_topics, kwargs))
            kwargs["checkpoint_callback"](
                received_topics[:1],
                received_topics[1:],
                "round-2",
                3,
                4,
            )
            return "候选复核警告"

        result = review_pipeline_candidates(
            topics,
            manual_entries,
            "测试主播",
            [(0, 30, "字幕")],
            [(10, 80)],
            12.5,
            "bundle/clip-review.json",
            "recording_clip_review_checkpoint.json",
            api_precheck_warning="首轮警告",
            progress_callback="progress",
            merge_manual_timeline_topics=merge,
            validate_unmatched_manual_topics=validate,
            clean_topics_for_report=clean,
            analysis_topics_snapshot=snapshot,
            seed_artifact_from_legacy=seed,
            write_clip_review_checkpoint=write_checkpoint,
            apply_danmaku_slice_decisions=apply,
            review_peak_selected_topics=review,
        )

        self.assertIs(result["accepted_topics"], topics)
        self.assertEqual(result["api_precheck_warning"], "首轮警告；人工时间轴警告；候选复核警告")
        self.assertEqual(result["clip_review_warning"], "候选复核警告")
        self.assertEqual(
            result["analysis_topics"],
            [{"title": "LLM 话题"}, {"title": "人工候选"}],
        )
        self.assertEqual(result["clip_review_checkpoint_path"], "bundle/clip-review.json")
        self.assertEqual(
            [call[0] for call in calls],
            ["merge", "validate", "clean", "snapshot", "seed", "apply", "review", "clean", "apply"],
        )
        self.assertEqual(
            checkpoint_calls,
            [
                (
                    "bundle/clip-review.json",
                    [{"title": "LLM 话题"}, {"title": "人工候选"}],
                    {"stage": "ready"},
                ),
                (
                    "bundle/clip-review.json",
                    topics[:1],
                    {
                        "stage": "reviewing",
                        "pending_count": 1,
                        "round": "round-2",
                        "batch_index": 3,
                        "total_batches": 4,
                    },
                )
            ],
        )
        self.assertEqual(calls[5][4], {})
        self.assertEqual(calls[-1][4], {"require_clip_review": True})

    def test_propagates_injected_failures_without_interpreting_them(self):
        class ExpectedFailure(RuntimeError):
            pass

        failure = ExpectedFailure("保留原始失败")

        def fail(*_args, **_kwargs):
            raise failure

        with self.assertRaises(ExpectedFailure) as raised:
            review_pipeline_candidates(
                [],
                [],
                "测试主播",
                [],
                [],
                0,
                "checkpoint.json",
                "legacy.json",
                merge_manual_timeline_topics=fail,
                validate_unmatched_manual_topics=lambda *_args, **_kwargs: None,
                clean_topics_for_report=lambda topics: topics,
                analysis_topics_snapshot=lambda topics: topics,
                seed_artifact_from_legacy=lambda *_args: None,
                write_clip_review_checkpoint=lambda *_args, **_kwargs: None,
                apply_danmaku_slice_decisions=lambda *_args, **_kwargs: None,
                review_peak_selected_topics=lambda *_args, **_kwargs: None,
            )

        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
