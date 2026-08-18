import unittest

from autoslice.analysis.topic import normalization
from autoslice.streamer_profiles import streamer_profile_context


class ContentNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.profile_context = streamer_profile_context("zeyin")
        self.profile_context.__enter__()
        self.addCleanup(self.profile_context.__exit__, None, None, None)

    def test_meta_filter_preserves_evidence_and_rejects_reasoning_noise(self):
        self.assertFalse(
            normalization.is_meta_body_line(
                "·字幕核查：音音明确说袜子破了",
            ),
        )
        self.assertFalse(
            normalization.is_meta_body_line(
                "·弹幕依据：局部峰值出现具体互动",
            ),
        )
        for line in (
            "·我们需要按照输出格式整理",
            "·[00:12] \"字幕原文\"",
            "·topic1: 讨论内容",
            "·主播",
        ):
            with self.subTest(line=line):
                self.assertTrue(normalization.is_meta_body_line(line))

    def test_body_cleanup_keeps_prefix_semantics_and_removes_meta_lines(self):
        self.assertEqual(
            normalization.clean_body_content(
                "·主要内容：音音讲袜子破掉的经过",
            ),
            "音音讲袜子破掉的经过",
        )
        self.assertEqual(
            normalization.normalise_body_line(
                "●观众发送礼物后音音道谢",
            ),
            "●观众发送礼物后音音道谢",
        )
        self.assertEqual(
            normalization.normalise_body_line(
                "·我们需要按照输出格式整理",
            ),
            "",
        )

    def test_json_points_are_flattened_normalized_and_filtered(self):
        points = [
            "音音解释袜子为什么会破",
            ["●观众发送礼物", "我们需要输出两个话题"],
        ]

        self.assertEqual(
            normalization.json_points_to_body(points),
            ["·音音解释袜子为什么会破", "●观众发送礼物"],
        )
        self.assertEqual(normalization.json_points_to_body(None), [])
        self.assertEqual(
            normalization.json_points_to_body(
                "第一条具体内容\n第二条具体内容",
            ),
            ["·第一条具体内容", "·第二条具体内容"],
        )

    def test_unsupported_audience_reaction_is_removed_without_losing_facts(self):
        points = [
            "·观众疯狂刷屏并起哄",
            "·现场气氛彻底沸腾",
            "·音音明确说袜子破了",
            "·弹幕依据：峰值附近有人提到袜子",
        ]

        self.assertEqual(
            normalization.filter_unsupported_ai_points(points),
            [
                "·音音明确说袜子破了",
                "·弹幕依据：峰值附近有人提到袜子",
            ],
        )


if __name__ == "__main__":
    unittest.main()
