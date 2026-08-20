import unittest

from autoslice.pipeline_titles import review_pipeline_publish_titles


class PipelineTitlesTests(unittest.TestCase):
    def test_reviews_titles_writes_checkpoint_merges_warning_and_cleans_topics(self):
        topics = [{"title": "候选", "publish_title": "复核前标题"}]
        cleaned_topics = [{"title": "候选", "publish_title": "复核后标题"}]
        calls = []

        def review(received_topics, **kwargs):
            calls.append(("review", received_topics, kwargs))
            received_topics[0]["publish_title"] = "复核后标题"
            kwargs["checkpoint_callback"](received_topics, 2, 3)
            return "标题复核警告"

        def write_checkpoint(path, current, **kwargs):
            calls.append(("checkpoint", path, current, kwargs))

        def clean(received_topics):
            calls.append(("clean", received_topics))
            return cleaned_topics

        result = review_pipeline_publish_titles(
            topics,
            "测试主播",
            "bundle/clip-review.json",
            api_precheck_warning="候选复核警告",
            progress_callback="progress",
            review_selected_publish_titles=review,
            write_clip_review_checkpoint=write_checkpoint,
            clean_topics_for_report=clean,
        )

        self.assertIs(result["accepted_topics"], cleaned_topics)
        self.assertEqual(
            result["api_precheck_warning"],
            "候选复核警告；标题复核警告",
        )
        self.assertEqual(result["title_review_warning"], "标题复核警告")
        self.assertEqual([call[0] for call in calls], ["review", "checkpoint", "clean"])
        self.assertIs(calls[0][1], topics)
        self.assertEqual(
            calls[0][2],
            {
                "streamer_name": "测试主播",
                "progress_callback": "progress",
                "checkpoint_callback": calls[0][2]["checkpoint_callback"],
            },
        )
        self.assertEqual(
            calls[1],
            (
                "checkpoint",
                "bundle/clip-review.json",
                topics,
                {
                    "stage": "title_reviewing",
                    "batch_index": 2,
                    "total_batches": 3,
                },
            ),
        )
        self.assertIs(calls[2][1], topics)

    def test_preserves_warning_and_propagates_injected_failures(self):
        topics = []
        result = review_pipeline_publish_titles(
            topics,
            "测试主播",
            "checkpoint.json",
            api_precheck_warning="已有警告",
            review_selected_publish_titles=lambda *_args, **_kwargs: None,
            write_clip_review_checkpoint=lambda *_args, **_kwargs: None,
            clean_topics_for_report=lambda received: received,
        )

        self.assertIs(result["accepted_topics"], topics)
        self.assertEqual(result["api_precheck_warning"], "已有警告")
        self.assertIsNone(result["title_review_warning"])

        class ExpectedFailure(RuntimeError):
            pass

        failure = ExpectedFailure("保留原始失败")

        def fail(*_args, **_kwargs):
            raise failure

        with self.assertRaises(ExpectedFailure) as raised:
            review_pipeline_publish_titles(
                [],
                "测试主播",
                "checkpoint.json",
                review_selected_publish_titles=fail,
                write_clip_review_checkpoint=lambda *_args, **_kwargs: None,
                clean_topics_for_report=lambda received: received,
            )

        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
