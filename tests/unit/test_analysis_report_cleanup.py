import copy
import unittest

from autoslice.analysis import timeline as timeline_analysis
from autoslice.analysis.report import cleanup as report_cleanup
from autoslice.streamer_profiles import streamer_profile_context


class ReportCleanupTests(unittest.TestCase):
    def test_report_fact_lines_filters_labels_and_strips_body_prefixes(self):
        topic = {
            "body": [
                "·草莓蛋糕烤糊",
                "●烤箱开始冒烟",
                "- 主播立即关闭电源",
                "●人工时间轴⭐⭐：0:01:20 草莓蛋糕翻车",
                "·时间轴：0:01:20 草莓蛋糕翻车",
                "·弹幕依据：0:01:30 附近峰值约 120 条/分钟",
                "·切片核心：围绕冒烟反应截取",
                "·参考投稿标题（仅供核对）：蛋糕翻车现场",
                "   ",
            ]
        }

        facts = report_cleanup.report_fact_lines(topic)

        self.assertEqual(
            facts,
            ["草莓蛋糕烤糊", "烤箱开始冒烟", "主播立即关闭电源"],
        )

    def test_trim_adjusts_both_directions_and_enforces_thirty_second_boundary(self):
        reviewed = {
            "start": 100,
            "end": 130,
            "body": ["·量子芯片发布消息"],
        }
        trim_start_topic = {
            "start": 80,
            "end": 160,
            "title": "草莓蛋糕翻车",
            "body": ["·草莓蛋糕开始冒烟"],
        }
        trim_end_topic = {
            "start": 100,
            "end": 180,
            "title": "草莓蛋糕翻车",
            "body": ["·草莓蛋糕开始冒烟"],
        }

        from_start = report_cleanup.trim_report_topic_around_reviewed_topic(
            trim_start_topic,
            reviewed,
            trim_start=True,
        )
        from_end = report_cleanup.trim_report_topic_around_reviewed_topic(
            trim_end_topic,
            {**reviewed, "start": 130, "end": 160},
            trim_start=False,
        )
        too_short = report_cleanup.trim_report_topic_around_reviewed_topic(
            {**trim_start_topic, "end": 159},
            reviewed,
            trim_start=True,
        )

        self.assertEqual((from_start["start"], from_start["end"]), (130, 160))
        self.assertEqual(from_start["start_str"], "0:02:10")
        self.assertEqual(from_start["end_str"], "0:02:40")
        self.assertEqual((from_end["start"], from_end["end"]), (100, 130))
        self.assertEqual(from_end["start_str"], "0:01:40")
        self.assertEqual(from_end["end_str"], "0:02:10")
        self.assertIsNone(too_short)

    def test_trim_uses_point_twenty_fact_threshold_and_rebuilds_titles(self):
        reviewed_fact = "abcdefghijklmnopqrst"
        removed_fact = "abc甲乙丙丁戊己庚辛"
        retained_fact = "abcd甲乙丙丁戊己庚辛壬癸子丑寅"
        topic = {
            "start": 120,
            "end": 220,
            "title": "旧标题",
            "publish_title": "旧投稿标题",
            "body": [f"·{removed_fact}", f"·{retained_fact}"],
        }
        reviewed = {
            "start": 100,
            "end": 130,
            "body": [f"·{reviewed_fact}"],
        }

        result = report_cleanup.trim_report_topic_around_reviewed_topic(
            topic,
            reviewed,
            trim_start=True,
        )

        self.assertGreaterEqual(
            timeline_analysis._manual_alignment_score(
                removed_fact,
                reviewed_fact,
            ),
            0.20,
        )
        self.assertLess(
            timeline_analysis._manual_alignment_score(
                retained_fact,
                reviewed_fact,
            ),
            0.20,
        )
        self.assertEqual(result["body"], [f"·{retained_fact}"])
        self.assertEqual(result["title"], retained_fact)
        self.assertNotEqual(result["publish_title"], "旧投稿标题")
        self.assertIn(retained_fact, result["publish_title"])

    def test_trim_reattaches_only_manual_evidence_inside_new_boundary(self):
        topic = {
            "start": 100,
            "end": 220,
            "title": "草莓蛋糕烤糊",
            "body": [
                "·草莓蛋糕烤糊后烤箱冒烟",
                "●人工时间轴⭐⭐⭐⭐⭐：0:01:50 旧错误证据",
            ],
            "manual_timeline": [
                {
                    "start": 110,
                    "end": 140,
                    "text": "草莓蛋糕烤糊",
                    "stars": 5,
                },
                {
                    "start": 160,
                    "end": 190,
                    "text": "草莓蛋糕烤糊后烤箱冒烟",
                    "stars": 2,
                },
            ],
        }

        result = report_cleanup.trim_report_topic_around_reviewed_topic(
            topic,
            {
                "start": 120,
                "end": 150,
                "body": ["·量子芯片发布消息"],
            },
            trim_start=True,
        )

        self.assertEqual(result["start"], 150)
        self.assertEqual(
            [entry["start"] for entry in result["manual_timeline"]],
            [160],
        )
        self.assertEqual(result["manual_stars"], 2)
        body_text = "\n".join(result["body"])
        self.assertIn("0:02:40 草莓蛋糕烤糊后烤箱冒烟", body_text)
        self.assertNotIn("旧错误证据", body_text)

    def test_resolve_gives_reviewed_topics_priority_in_both_directions(self):
        reviewed_first = report_cleanup.resolve_reviewed_report_overlaps(
            [
                {
                    "start": 100,
                    "end": 160,
                    "title": "蛋糕冒烟核心",
                    "clip_review_validated": True,
                    "body": ["·草莓蛋糕烤糊后烤箱冒烟"],
                },
                {
                    "start": 140,
                    "end": 240,
                    "title": "蛋糕翻车和清理现场",
                    "body": [
                        "·草莓蛋糕烤糊后烤箱冒烟",
                        "·主播关闭电源并清理烤箱",
                    ],
                },
            ]
        )
        reviewed_following = report_cleanup.resolve_reviewed_report_overlaps(
            [
                {
                    "start": 300,
                    "end": 430,
                    "title": "讨论新品预热安排",
                    "body": ["·团队讨论新品预热安排"],
                },
                {
                    "start": 400,
                    "end": 470,
                    "title": "发布量子芯片",
                    "clip_review_validated": True,
                    "body": ["·量子芯片正式发布"],
                },
            ]
        )
        deleted_short_topic = report_cleanup.resolve_reviewed_report_overlaps(
            [
                {
                    "start": 500,
                    "end": 560,
                    "title": "复核核心",
                    "clip_review_validated": True,
                    "body": ["·复核核心事实"],
                },
                {
                    "start": 540,
                    "end": 580,
                    "title": "过短普通话题",
                    "body": ["·另一个普通事实"],
                },
            ]
        )

        self.assertEqual(reviewed_first[1]["start"], 160)
        self.assertEqual(reviewed_first[1]["title"], "关闭电源并清理烤箱")
        self.assertEqual(reviewed_following[0]["end"], 400)
        self.assertEqual(
            [topic["title"] for topic in deleted_short_topic],
            ["复核核心"],
        )

    def test_resolve_does_not_trim_equal_review_state_or_large_overlap(self):
        equal_state_cases = (
            (
                "both_reviewed",
                [
                    {
                        "start": 100,
                        "end": 180,
                        "title": "前项",
                        "clip_review_validated": True,
                        "body": ["·前项事实"],
                    },
                    {
                        "start": 150,
                        "end": 230,
                        "title": "后项",
                        "clip_review_validated": True,
                        "body": ["·后项事实"],
                    },
                ],
            ),
            (
                "neither_reviewed",
                [
                    {"start": 100, "end": 180, "title": "前项", "body": ["·前项事实"]},
                    {"start": 150, "end": 230, "title": "后项", "body": ["·后项事实"]},
                ],
            ),
        )
        for name, topics in equal_state_cases:
            with self.subTest(name=name):
                self.assertEqual(
                    report_cleanup.resolve_reviewed_report_overlaps(topics),
                    topics,
                )

        large_overlap = [
            {
                "start": 300,
                "end": 600,
                "title": "复核长话题",
                "clip_review_validated": True,
                "body": ["·复核长话题事实"],
            },
            {
                "start": 350,
                "end": 650,
                "title": "普通长话题",
                "body": ["·普通长话题事实"],
            },
        ]
        self.assertEqual(
            report_cleanup.resolve_reviewed_report_overlaps(large_overlap),
            large_overlap,
        )

    def test_clean_normalises_body_rebuilds_title_and_cleans_publish_title(self):
        topics = [
            {
                "start": 100,
                "end": 180,
                "title": "量子芯片发布消息",
                "publish_title": "投稿标题建议：**草莓蛋糕烤糊，烤箱直接冒烟**",
                "body": [
                    "·主要内容：草莓蛋糕烤糊后烤箱冒烟",
                    "·我们规划话题结构",
                ],
            },
            {
                "start": 300,
                "end": 360,
                "title": "下一段",
                "body": ["·我们规划话题结构"],
            },
        ]

        with streamer_profile_context("zeyin"):
            cleaned = report_cleanup.clean_topics_for_report(topics)

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["body"], ["·草莓蛋糕烤糊后烤箱冒烟"])
        self.assertEqual(cleaned[0]["title"], "草莓蛋糕烤糊后烤箱冒烟")
        self.assertNotIn("投稿标题建议", cleaned[0]["publish_title"])
        self.assertNotIn("**", cleaned[0]["publish_title"])
        self.assertIn("草莓蛋糕烤糊后烤箱冒烟", cleaned[0]["publish_title"])

    def test_clean_reattaches_manual_evidence_without_mutating_nested_source(self):
        topics = [
            {
                "start": 100,
                "end": 180,
                "title": "草莓蛋糕烤糊",
                "body": [
                    "·草莓蛋糕烤糊后关闭烤箱",
                    "●人工时间轴⭐⭐⭐⭐⭐：0:02:10 旧错误证据",
                ],
                "manual_timeline": [
                    {
                        "start": 120,
                        "end": 150,
                        "text": "草莓蛋糕烤糊后关闭烤箱",
                        "stars": 2,
                        "metadata": {"origin": "manual"},
                    },
                    {
                        "start": 130,
                        "end": 160,
                        "text": "量子芯片发布",
                        "stars": 5,
                        "metadata": {"origin": "manual"},
                    },
                ],
                "metadata": {"boundary": {"reviewed": True}},
            }
        ]
        source_topics = copy.deepcopy(topics)

        cleaned = report_cleanup.clean_topics_for_report(topics)

        self.assertEqual(topics, source_topics)
        self.assertEqual(len(cleaned), 1)
        self.assertIsNot(cleaned[0], topics[0])
        self.assertIsNot(cleaned[0]["body"], topics[0]["body"])
        self.assertIsNot(
            cleaned[0]["manual_timeline"],
            topics[0]["manual_timeline"],
        )
        self.assertEqual(
            [entry["text"] for entry in cleaned[0]["manual_timeline"]],
            ["草莓蛋糕烤糊后关闭烤箱"],
        )
        self.assertEqual(cleaned[0]["manual_stars"], 2)
        body_text = "\n".join(cleaned[0]["body"])
        self.assertIn("草莓蛋糕烤糊后关闭烤箱", body_text)
        self.assertNotIn("旧错误证据", body_text)
        self.assertNotIn("量子芯片发布", body_text)

    def test_clean_prefers_specific_topics_dedupes_fallbacks_and_sorts(self):
        topics = [
            {
                "start": 7200,
                "end": 7800,
                "title": "第二小时兜底",
                "fallback": True,
                "body": ["·该段字幕识别较碎"],
            },
            {
                "start": 4000,
                "end": 4080,
                "title": "后出现的真实话题",
                "body": ["·主播讨论后出现的具体事件"],
            },
            {
                "start": 100,
                "end": 700,
                "title": "覆盖真实话题的兜底",
                "fallback": True,
                "body": ["·该段字幕识别较碎"],
            },
            {
                "start": 200,
                "end": 260,
                "title": "草莓蛋糕翻车",
                "body": ["·草莓蛋糕烤糊后烤箱冒烟"],
            },
            {
                "start": 202,
                "end": 258,
                "title": "重复的蛋糕翻车",
                "body": ["·主播发现蛋糕烤糊"],
            },
            {
                "start": 800,
                "end": 900,
                "title": "同小时非重叠兜底",
                "fallback": True,
                "body": ["·该段字幕识别较碎"],
            },
        ]

        cleaned = report_cleanup.clean_topics_for_report(topics)

        self.assertEqual(
            [topic["title"] for topic in cleaned],
            ["草莓蛋糕翻车", "后出现的真实话题", "第二小时兜底"],
        )
        self.assertEqual(
            [(topic["start"], topic["end"]) for topic in cleaned],
            [(200, 260), (4000, 4080), (7200, 7800)],
        )
        self.assertTrue(cleaned[-1]["fallback"])

    def test_clean_repairs_local_overlap_around_reviewed_topic(self):
        topics = [
            {
                "start": 100,
                "end": 180,
                "title": "复核后的蛋糕核心",
                "clip_review_validated": True,
                "body": ["·草莓蛋糕烤糊后烤箱冒烟"],
            },
            {
                "start": 150,
                "end": 260,
                "title": "蛋糕翻车和清理现场",
                "body": [
                    "·草莓蛋糕烤糊后烤箱冒烟",
                    "·主播关闭电源并清理烤箱",
                ],
            },
        ]

        cleaned = report_cleanup.clean_topics_for_report(topics)

        self.assertEqual(cleaned[1]["start"], 180)
        self.assertEqual(cleaned[1]["title"], "关闭电源并清理烤箱")
        self.assertLessEqual(cleaned[0]["end"], cleaned[1]["start"])


if __name__ == "__main__":
    unittest.main()
