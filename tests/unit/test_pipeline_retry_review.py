import unittest

from autoslice.pipeline_retry_review import review_retry_candidates_and_titles


class PipelineRetryReviewTests(unittest.TestCase):
    def _run(self, *, resume=False, reuse=False):
        topics = [{
            "title": "候选",
            "start": 10,
            "end": 20,
            "can_slice": True,
        }]
        analysis_topics = [{"title": "恢复话题"}]
        calls = []
        checkpoints = []

        def write_checkpoint(path, current, **kwargs):
            checkpoints.append((path, current, kwargs))

        def apply(received, peaks, average, **kwargs):
            calls.append(("apply", received, peaks, average, kwargs))

        def append_source(topic, source):
            calls.append(("source", topic, source))

        def clean(received):
            calls.append(("clean", received))
            return received

        def review(received, **kwargs):
            calls.append(("review", received, kwargs))
            kwargs["checkpoint_callback"](
                received,
                [],
                "round-1",
                1,
                2,
            )
            return "字幕复核警告"

        def title_review(received, **kwargs):
            calls.append(("title", received, kwargs))
            kwargs["checkpoint_callback"](received, 2, 3)
            return "标题复核警告"

        result = review_retry_candidates_and_titles(
            topics,
            analysis_topics,
            srt_segments=[(10, 20, "字幕")],
            peaks=[(12, 30)],
            avg_den=4.5,
            streamer_name="测试主播",
            clip_review_checkpoint_path="bundle/review.json",
            resume_review=resume,
            reuse_completed_review=reuse,
            stale_review_keys={(10, 20, "候选")},
            progress_callback="progress",
            clean_topics_for_report=clean,
            apply_danmaku_slice_decisions=apply,
            append_clip_candidate_source=append_source,
            review_peak_selected_topics=review,
            review_selected_publish_titles=title_review,
            write_clip_review_checkpoint=write_checkpoint,
        )
        return result, calls, checkpoints, topics

    def test_runs_retry_review_and_title_review_in_order(self):
        result, calls, checkpoints, topics = self._run()

        self.assertIs(result["accepted_topics"], topics)
        self.assertEqual(
            result["clip_review_warning"],
            "字幕复核警告；标题复核警告",
        )
        self.assertEqual(result["title_review_warning"], "标题复核警告")
        self.assertEqual(
            [item[0] for item in calls],
            ["apply", "source", "review", "clean", "apply", "title", "clean"],
        )
        self.assertEqual(
            [item[2]["stage"] for item in checkpoints],
            ["ready", "reviewing", "title_reviewing"],
        )
        self.assertEqual(
            checkpoints[0][2]["source"],
            "artifact_retry",
        )
        self.assertEqual(calls[2][2]["resume"], False)

    def test_resume_skips_initial_decision_but_passes_resume_to_review(self):
        result, calls, _checkpoints, _topics = self._run(resume=True)

        self.assertEqual(result["clip_review_warning"], "字幕复核警告；标题复核警告")
        self.assertEqual(
            [item[0] for item in calls],
            ["review", "clean", "apply", "title", "clean"],
        )
        self.assertTrue(calls[0][2]["resume"])

    def test_reuses_completed_review_but_still_reviews_titles(self):
        result, calls, _checkpoints, _topics = self._run(reuse=True)

        self.assertEqual(result["clip_review_warning"], "标题复核警告")
        self.assertEqual(result["title_review_warning"], "标题复核警告")
        self.assertEqual(
            [item[0] for item in calls],
            ["clean", "apply", "title", "clean"],
        )
        self.assertEqual(calls[2][0], "title")


if __name__ == "__main__":
    unittest.main()
