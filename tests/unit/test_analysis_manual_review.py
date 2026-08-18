import json
import unittest
from unittest.mock import patch

from autoslice.analysis.manual import review as manual_review
from autoslice.llm.transport import LLMStructuredOutputError
from autoslice.streamer_profiles import streamer_profile_context


class ManualReviewTests(unittest.TestCase):
    def test_empty_manual_review_does_not_call_llm(self):
        with patch("autoslice.llm.transport.call_llm_with_retry") as call:
            updated = manual_review.enrich_manual_topics_with_llm([])

        self.assertEqual(updated, 0)
        call.assert_not_called()

    @streamer_profile_context("zeyin")
    def test_structured_response_enriches_topic_and_uses_review_stage(self):
        topics = [
            {
                "start": 100,
                "end": 220,
                "title": "袜子破了",
                "publish_title": "【泽音】旧标题",
                "body": [
                    "·字幕核查：音音发现袜子破洞后怪起洗衣机",
                    "●人工时间轴⭐⭐：袜子破了",
                ],
                "can_slice": False,
            }
        ]
        response = json.dumps(
            {
                "topics": [
                    {
                        "id": 1,
                        "title": "袜子破洞怪洗衣机",
                        "publish_title": "【泽音】袜子破了还要怪洗衣机😂",
                        "focus_start": "0:01:45",
                        "focus_end": "0:03:10",
                        "points": ["音音发现袜子破洞后把原因怪到洗衣机上"],
                    }
                ]
            },
            ensure_ascii=False,
        )

        with patch(
            "autoslice.llm.transport.call_llm_with_retry",
            return_value=response,
        ) as call:
            updated = manual_review.enrich_manual_topics_with_llm(
                topics,
                streamer_name="音音",
            )

        self.assertEqual(updated, 1)
        self.assertEqual(topics[0]["title"], "袜子破洞怪洗衣机")
        self.assertEqual(
            topics[0]["publish_title"],
            "【泽音】袜子破了还要怪洗衣机😂",
        )
        self.assertTrue(topics[0]["ai_enriched"])
        self.assertTrue(topics[0]["ai_focus_validated"])
        self.assertEqual((topics[0]["start"], topics[0]["end"]), (105, 190))
        self.assertEqual(call.call_args.kwargs["reasoning_stage"], "review")
        self.assertTrue(call.call_args.kwargs["require_json"])
        self.assertIn("人工时间轴只是线索", call.call_args.args[0])
        self.assertIn("字幕核查", call.call_args.args[0])

    def test_invalid_topics_shape_raises_structured_output_error(self):
        topics = [
            {
                "start": 10,
                "end": 80,
                "title": "候选",
                "body": ["·字幕核查：完整字幕证据"],
            }
        ]
        response = json.dumps({"topics": "invalid"}, ensure_ascii=False)

        with (
            patch(
                "autoslice.llm.transport.call_llm_with_retry",
                return_value=response,
            ),
            self.assertRaisesRegex(
                LLMStructuredOutputError,
                "未返回 topics 数组",
            ),
        ):
            manual_review.enrich_manual_topics_with_llm(topics)

    def test_batch_failure_marks_reference_only_and_reports_checkpoint(self):
        topics = [
            {
                "start": 100,
                "end": 180,
                "title": "候选",
                "body": ["·字幕核查：完整字幕证据"],
            }
        ]
        checkpoints = []
        progress = []

        with patch(
            "autoslice.analysis.manual.review.enrich_manual_topics_with_llm",
            side_effect=RuntimeError("上游暂不可用"),
        ):
            warning = manual_review.enrich_manual_topics_in_batches(
                topics,
                batch_size=3,
                progress_callback=lambda message, step, total: progress.append(
                    (message, step, total)
                ),
                batch_result_callback=lambda completed, remaining, warnings: checkpoints.append(
                    (completed, remaining, warnings)
                ),
            )

        self.assertTrue(topics[0]["reference_only"])
        self.assertIn("上游暂不可用", warning)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(len(checkpoints[0][0]), 1)
        self.assertEqual(checkpoints[0][1], [])
        self.assertEqual(len(checkpoints[0][2]), 1)
        self.assertEqual(progress[0][1], 22)
        self.assertEqual(progress[-1][1], 24)

    def test_unmatched_validation_rebuilds_evidence_and_restores_order(self):
        manual_topic = {
            "start": 100,
            "end": 180,
            "title": "人工线索",
            "body": ["·旧 AI 摘要：不应继续作为证据"],
            "source": "optimized_manual_timeline",
            "postcheck_pending": True,
            "manual_timeline": [{"start": 120, "text": "袜子破了", "stars": 2}],
        }
        topics = [
            manual_topic,
            {
                "start": 0,
                "end": 60,
                "title": "普通话题",
                "source": "subtitle",
                "ai_enriched": True,
            },
        ]
        seen_body = []

        def enrich(candidates, **kwargs):
            seen_body.extend(candidates[0]["body"])
            candidates[0]["ai_enriched"] = True
            candidates[0]["postcheck_pending"] = False
            candidates[0].pop("reference_only", None)
            self.assertEqual(kwargs["progress_start"], 94)
            self.assertEqual(kwargs["progress_end"], 94)
            return None

        with patch(
            "autoslice.analysis.manual.review.enrich_manual_topics_in_batches",
            side_effect=enrich,
        ):
            warning = manual_review.validate_unmatched_manual_topics(
                topics,
                srt_segments=[
                    (100, 150, "音音发现袜子破洞后怪洗衣机"),
                ],
                peaks=[(120, 80)],
            )

        self.assertIsNone(warning)
        self.assertEqual([topic["start"] for topic in topics], [0, 100])
        self.assertIs(topics[1], manual_topic)
        evidence = "\n".join(seen_body)
        self.assertIn("音音发现袜子破洞", evidence)
        self.assertIn("袜子破了", evidence)
        self.assertNotIn("旧 AI 摘要", evidence)

    def test_unmatched_validation_skips_when_no_manual_candidates(self):
        topics = [
            {
                "start": 0,
                "end": 60,
                "title": "普通话题",
                "source": "subtitle",
            }
        ]

        with patch("autoslice.analysis.manual.review.enrich_manual_topics_in_batches") as enrich:
            warning = manual_review.validate_unmatched_manual_topics(topics)

        self.assertIsNone(warning)
        enrich.assert_not_called()


if __name__ == "__main__":
    unittest.main()
