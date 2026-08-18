import unittest

from autoslice.analysis import topic_formatting
from autoslice.streamer_profiles import streamer_profile_context


class TopicFormattingTests(unittest.TestCase):
    def test_report_time_uses_minutes_then_hours(self):
        self.assertEqual(topic_formatting.format_report_time(0), "00:00")
        self.assertEqual(topic_formatting.format_report_time(3599.9), "59:59")
        self.assertEqual(topic_formatting.format_report_time(3600), "1:00:00")
        self.assertEqual(topic_formatting.format_report_time(7384), "2:03:04")

    def test_topic_index_uses_all_fifty_circled_numbers(self):
        self.assertEqual(topic_formatting.topic_index_label(1), "①")
        self.assertEqual(topic_formatting.topic_index_label(20), "⑳")
        self.assertEqual(topic_formatting.topic_index_label(21), "㉑")
        self.assertEqual(topic_formatting.topic_index_label(50), "㊿")

    def test_topic_index_falls_back_outside_supported_range(self):
        self.assertEqual(topic_formatting.topic_index_label(0), "0.")
        self.assertEqual(topic_formatting.topic_index_label(51), "51.")

    @streamer_profile_context("zeyin")
    def test_topic_block_keeps_body_order_marker_and_streamer_role(self):
        block = topic_formatting.format_topic_block(
            {
                "start": 65,
                "end": 130,
                "title": "主播发现袜子破了",
                "body": [
                    "泽音Melody先检查袜子破洞",
                    "主播最后把原因怪到洗衣机上",
                ],
                "can_slice": True,
            },
            2,
            streamer_name="音音",
        )

        self.assertEqual(
            block.splitlines(),
            [
                "②[01:05－02:10]音音发现袜子破了 ✂️",
                "音音先检查袜子破洞",
                "音音最后把原因怪到洗衣机上",
            ],
        )

    def test_topic_block_can_omit_index_and_slice_marker(self):
        block = topic_formatting.format_topic_block(
            {
                "start": 5,
                "end": 15,
                "title": "普通话题",
                "body": [],
                "can_slice": False,
            },
            0,
        )

        self.assertEqual(block, "[00:05－00:15]普通话题")


if __name__ == "__main__":
    unittest.main()
