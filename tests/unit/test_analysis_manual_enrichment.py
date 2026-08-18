import unittest

from autoslice import timecode
from autoslice.analysis import clip_policy
from autoslice.analysis.manual import enrichment as manual_enrichment
from autoslice.streamer_profiles import streamer_profile_context


class ManualEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.profile_context = streamer_profile_context("zeyin")
        self.profile_context.__enter__()
        self.addCleanup(self.profile_context.__exit__, None, None, None)

    @staticmethod
    def _topic(**overrides):
        topic = {
            "start": 100,
            "end": 400,
            "title": "袜子破洞话题",
            "publish_title": "【泽音】原始投稿标题",
            "body": [
                "·字幕核查：音音明确说袜子破了",
                "·弹幕依据：峰值附近有人追问袜子",
                "●人工时间轴⭐⭐：袜子破了",
                "·旧的概括正文",
            ],
            "reference_only": True,
            "postcheck_pending": True,
        }
        topic.update(overrides)
        return topic

    @staticmethod
    def _item(**overrides):
        item = {
            "title": "袜子破洞引发吐槽",
            "publish_title": "袜子破了还要怪洗衣机😂",
            "focus_start": "00:02:00",
            "focus_end": "00:03:00",
            "points": ["音音发现袜子破了后解释事情经过"],
            "title_hook": {
                "type": "反差",
                "fact": "袜子破了",
                "contrast": "最后怪到洗衣机",
            },
        }
        item.update(overrides)
        return item

    def test_placeholder_detection_uses_static_and_current_streamer_phrases(self):
        for value in (
            None,
            "",
            "5-15字具体短标题",
            "具体发生了什么",
            "音音如何回应",
        ):
            with self.subTest(value=value):
                self.assertTrue(manual_enrichment.is_manual_ai_placeholder(value))
        self.assertFalse(manual_enrichment.is_manual_ai_placeholder("袜子破洞引发吐槽"))

    def test_focus_range_accepts_only_bounded_duration(self):
        topic = self._topic(end=500)
        maximum = clip_policy.TOPIC_REVIEW_FOCUS_MAX_SEC
        self.assertEqual(
            manual_enrichment.validated_ai_focus_range(
                {"focus_start": "00:01:40", "focus_end": "00:01:50"},
                topic,
            ),
            (100, 110),
        )
        self.assertEqual(
            manual_enrichment.validated_ai_focus_range(
                {
                    "focus_start": timecode.format_elapsed(120),
                    "focus_end": timecode.format_elapsed(120 + maximum),
                },
                topic,
            ),
            (120, 120 + maximum),
        )

        invalid_items = (
            {},
            {"focus_start": "bad", "focus_end": "00:02:00"},
            {"focus_start": "00:01:39", "focus_end": "00:02:00"},
            {"focus_start": "00:02:00", "focus_end": "00:01:59"},
            {"focus_start": "00:02:00", "focus_end": "00:02:09"},
            {
                "focus_start": "00:02:00",
                "focus_end": timecode.format_elapsed(120 + maximum + 1),
            },
            {"focus_start": "00:07:00", "focus_end": "00:08:21"},
        )
        for item in invalid_items:
            with self.subTest(item=item):
                self.assertIsNone(manual_enrichment.validated_ai_focus_range(item, topic))

    def test_enrichment_preserves_evidence_and_applies_valid_focus(self):
        topic = self._topic()

        enriched = manual_enrichment.enrich_manual_topic_from_item(
            topic,
            self._item(),
        )

        self.assertEqual(enriched["title"], "袜子破洞引发吐槽")
        self.assertEqual(enriched["publish_title"], "【泽音】袜子破了还要怪洗衣机😂")
        self.assertEqual(
            enriched["body"],
            [
                "·音音发现袜子破了后解释事情经过",
                "·弹幕依据：峰值附近有人追问袜子",
                "●人工时间轴⭐⭐：袜子破了",
            ],
        )
        self.assertEqual(enriched["title_hook"]["fact"], "袜子破了")
        self.assertTrue(enriched["ai_enriched"])
        self.assertFalse(enriched["postcheck_pending"])
        self.assertTrue(enriched["postcheck_validated"])
        self.assertNotIn("reference_only", enriched)
        self.assertEqual((enriched["reference_start"], enriched["reference_end"]), (100, 400))
        self.assertEqual((enriched["start"], enriched["end"]), (120, 180))
        self.assertEqual(
            (enriched["start_str"], enriched["end_str"]),
            ("0:02:00", "0:03:00"),
        )
        self.assertTrue(enriched["ai_focus_validated"])
        self.assertEqual((topic["start"], topic["end"]), (100, 400))
        self.assertTrue(topic["reference_only"])

    def test_locked_publish_title_is_not_overwritten(self):
        topic = self._topic(
            publish_title="【泽音】人工确认过的爆点标题😂",
            publish_title_locked=True,
            publish_title_source="human_review",
        )

        enriched = manual_enrichment.enrich_manual_topic_from_item(
            topic,
            self._item(publish_title="模型新写的普通标题"),
        )

        self.assertEqual(enriched["publish_title"], topic["publish_title"])
        self.assertTrue(enriched["publish_title_locked"])
        self.assertEqual(enriched["publish_title_source"], "human_review")

    def test_template_or_truncated_title_is_rebuilt_from_valid_points(self):
        for title in ("5-15字具体短标题", "音音看到袜子破洞时"):
            with self.subTest(title=title):
                enriched = manual_enrichment.enrich_manual_topic_from_item(
                    self._topic(),
                    self._item(
                        title=title,
                        points=["音音解释袜子破洞发生的完整经过"],
                    ),
                )
                self.assertIsNotNone(enriched)
                self.assertNotEqual(enriched["title"], title)
                self.assertFalse(manual_enrichment.is_manual_ai_placeholder(enriched["title"]))
                self.assertFalse(enriched["title"].endswith("时"))

    def test_invalid_points_return_none_and_invalid_focus_keeps_source_range(self):
        for points in (None, [], ["具体发生了什么"], ["观众疯狂刷屏并起哄"]):
            with self.subTest(points=points):
                self.assertIsNone(
                    manual_enrichment.enrich_manual_topic_from_item(
                        self._topic(),
                        self._item(points=points),
                    )
                )

        enriched = manual_enrichment.enrich_manual_topic_from_item(
            self._topic(),
            self._item(focus_start="00:01:00", focus_end="00:03:00"),
        )
        self.assertEqual((enriched["start"], enriched["end"]), (100, 400))
        self.assertNotIn("reference_start", enriched)
        self.assertNotIn("ai_focus_validated", enriched)


if __name__ == "__main__":
    unittest.main()
