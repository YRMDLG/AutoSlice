import copy
import math
import unittest

from autoslice.analysis import candidate_reconciliation
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis.manual import timebase as timeline_analysis


class CandidateReconciliationTests(unittest.TestCase):
    def test_empty_values_return_empty_semantics_and_compatible_topic_copy(self):
        topic = {}

        result = candidate_reconciliation.reconcile_topic_manual_evidence(topic)

        self.assertEqual(candidate_reconciliation.topic_semantic_text(topic), "")
        self.assertEqual(
            candidate_reconciliation.danmaku_topic_alignment(topic, None),
            0.0,
        )
        self.assertEqual(
            candidate_reconciliation.danmaku_topic_alignment(topic, {}),
            0.0,
        )
        self.assertEqual(result, {})
        self.assertIsNot(result, topic)

    def test_topic_semantic_text_excludes_evidence_and_reference_labels(self):
        topic = {
            "title": "蛋糕翻车",
            "body": [
                "·烤箱开始冒烟",
                "●人工时间轴⭐⭐：0:01:50 旧证据",
                "·时间轴：0:01:55 旧证据",
                "·弹幕依据：哈哈哈",
                "·切片核心：0:02:00",
                "·参考投稿标题（仅供核对）：旧标题",
            ],
        }

        result = candidate_reconciliation.topic_semantic_text(topic)

        self.assertEqual(result, "蛋糕翻车 烤箱开始冒烟")

    def test_danmaku_alignment_cleans_filters_and_log_weights_messages(self):
        topic = {
            "title": "草莓蛋糕烤糊",
            "body": ["·主播关闭冒烟烤箱"],
        }
        evidence = {
            "representative_messages": [
                {"text": r"{\an8}草莓蛋糕烤糊", "count": 1},
                {"text": "烤箱冒烟", "count": 100},
                {"text": "哈哈哈哈", "count": 999_999},
            ]
        }
        source_evidence = copy.deepcopy(evidence)

        result = candidate_reconciliation.danmaku_topic_alignment(topic, evidence)

        semantic_text = candidate_reconciliation.topic_semantic_text(topic)
        scored = []
        for item in evidence["representative_messages"]:
            text = danmaku_analysis._clean_ass_danmaku_text(item["text"])
            if not text or danmaku_analysis._is_generic_danmaku_reaction(text):
                continue
            score = timeline_analysis.manual_alignment_score(text, semantic_text)
            if score <= 0:
                continue
            weight = 1.0 + math.log1p(max(1, int(item["count"])))
            scored.append((score, weight))
        scored.sort(key=lambda item: item[0], reverse=True)
        strongest = scored[0][0]
        weighted_average = sum(score * weight for score, weight in scored[:3]) / sum(
            weight for _, weight in scored[:3]
        )
        expected = round(strongest * 0.70 + weighted_average * 0.30, 4)
        without_generic = {"representative_messages": evidence["representative_messages"][:2]}
        equal_counts = copy.deepcopy(without_generic)
        equal_counts["representative_messages"][1]["count"] = 1

        self.assertEqual(result, expected)
        self.assertEqual(
            result,
            candidate_reconciliation.danmaku_topic_alignment(
                topic,
                without_generic,
            ),
        )
        self.assertNotEqual(
            result,
            candidate_reconciliation.danmaku_topic_alignment(topic, equal_counts),
        )
        self.assertEqual(evidence, source_evidence)

    def test_manual_overlap_accepts_each_threshold_and_rejects_below_all(self):
        cases = (
            (
                "absolute_twenty_seconds",
                {"start": 280, "end": 380},
                {"start": 100, "end": 300},
                True,
            ),
            (
                "half_manual_entry",
                {"start": 190, "end": 210},
                {"start": 100, "end": 200},
                True,
            ),
            (
                "quarter_topic",
                {"start": 130, "end": 230},
                {"start": 100, "end": 140},
                True,
            ),
            (
                "below_every_threshold",
                {"start": 131, "end": 151},
                {"start": 100, "end": 140},
                False,
            ),
            (
                "no_overlap",
                {"start": 200, "end": 220},
                {"start": 100, "end": 140},
                False,
            ),
        )

        overlaps = candidate_reconciliation.manual_entry_meaningfully_overlaps_topic
        for name, entry, topic, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(overlaps(entry, topic), expected)

    def test_reconcile_sanitizes_optimized_entries_before_attaching_evidence(self):
        topic = {
            "start": 100,
            "end": 180,
            "title": "草莓蛋糕烤糊",
            "body": ["·烤箱冒烟后关闭电源"],
            "manual_timeline": [
                {
                    "start": 110,
                    "end": 170,
                    "text": "草莓蛋糕烤糊后关闭烤箱",
                    "summary": ["烤箱开始冒烟"],
                    "source": "optimized_manual_timeline",
                    "stars": 5,
                    "original_entries": [
                        {
                            "start": 120,
                            "text": "草莓蛋糕烤糊后关闭烤箱",
                            "stars": 2,
                        },
                        {"start": 130, "text": "量子芯片发布", "stars": 5},
                    ],
                }
            ],
        }

        result = candidate_reconciliation.reconcile_topic_manual_evidence(topic)

        retained = result["manual_timeline"][0]
        self.assertEqual(
            retained["original_entries"],
            [
                {
                    "start": 120,
                    "text": "草莓蛋糕烤糊后关闭烤箱",
                    "stars": 2,
                }
            ],
        )
        self.assertEqual(retained["stars"], 2)
        self.assertEqual(result["manual_stars"], 2)
        self.assertNotIn("量子芯片发布", "\n".join(result["body"]))

    def test_reconcile_protects_only_high_star_originals_inside_topic(self):
        topic = {
            "start": 100,
            "end": 180,
            "title": "草莓蛋糕烤糊",
            "body": ["·烤箱开始冒烟"],
            "manual_timeline": [
                {
                    "start": 100,
                    "end": 180,
                    "text": "草莓蛋糕烤糊，随后提到量子芯片发布",
                    "source": "optimized_manual_timeline",
                    "stars": 5,
                    "original_entries": [
                        {"start": 120, "text": "量子芯片发布", "stars": 5},
                        {"start": 102, "text": "量子芯片发布", "stars": 5},
                    ],
                }
            ],
        }

        result = candidate_reconciliation.reconcile_topic_manual_evidence(topic)

        retained_originals = result["manual_timeline"][0]["original_entries"]
        self.assertEqual(
            retained_originals,
            [{"start": 120, "text": "量子芯片发布", "stars": 5}],
        )
        self.assertIn(
            "●人工时间轴⭐⭐⭐⭐⭐：0:02:00 量子芯片发布",
            result["body"],
        )

    def test_reconcile_removes_adjacent_unrelated_and_old_manual_body_lines(self):
        topic = {
            "start": 100,
            "end": 180,
            "title": "草莓蛋糕烤糊",
            "body": [
                "·烤箱开始冒烟",
                "●人工时间轴⭐⭐⭐⭐⭐：0:02:55 旧错误星标",
                "·时间轴：0:02:50 旧人工正文",
            ],
            "manual_timeline": [
                {
                    "start": 120,
                    "end": 140,
                    "text": "草莓蛋糕烤糊",
                    "stars": 2,
                },
                {
                    "start": 120,
                    "end": 140,
                    "text": "草莓蛋糕烤糊",
                    "stars": 2,
                },
                {
                    "start": 130,
                    "end": 160,
                    "text": "量子芯片发布",
                    "stars": 5,
                },
                {
                    "start": 175,
                    "end": 230,
                    "text": "宇宙飞船发射",
                    "stars": 5,
                },
            ],
        }

        result = candidate_reconciliation.reconcile_topic_manual_evidence(topic)

        self.assertEqual(len(result["manual_timeline"]), 2)
        self.assertTrue(all(entry["text"] == "草莓蛋糕烤糊" for entry in result["manual_timeline"]))
        evidence_line = "●人工时间轴⭐⭐：0:02:00 草莓蛋糕烤糊"
        self.assertEqual(result["body"].count(evidence_line), 1)
        body_text = "\n".join(result["body"])
        self.assertNotIn("旧错误星标", body_text)
        self.assertNotIn("旧人工正文", body_text)
        self.assertNotIn("量子芯片发布", body_text)
        self.assertNotIn("宇宙飞船发射", body_text)
        self.assertEqual(result["manual_stars"], 2)

    def test_reconcile_does_not_mutate_source_topic_or_nested_entries(self):
        topic = {
            "start": 100,
            "end": 180,
            "title": "草莓蛋糕烤糊",
            "body": [
                "·烤箱开始冒烟",
                "●人工时间轴⭐⭐⭐⭐⭐：旧证据",
            ],
            "manual_timeline": [
                {
                    "start": 110,
                    "end": 170,
                    "text": "草莓蛋糕烤糊后关闭烤箱",
                    "source": "optimized_manual_timeline",
                    "stars": 5,
                    "original_entries": [
                        {
                            "start": 120,
                            "text": "草莓蛋糕烤糊后关闭烤箱",
                            "stars": 2,
                            "metadata": {"origin": "manual"},
                        },
                        {"start": 130, "text": "量子芯片发布", "stars": 5},
                    ],
                }
            ],
            "metadata": {"final_boundary": {"changed": True}},
        }
        source_topic = copy.deepcopy(topic)

        result = candidate_reconciliation.reconcile_topic_manual_evidence(topic)

        self.assertEqual(topic, source_topic)
        self.assertIsNot(result, topic)
        self.assertIsNot(result["body"], topic["body"])
        self.assertIsNot(result["manual_timeline"], topic["manual_timeline"])
        self.assertEqual(topic["metadata"], {"final_boundary": {"changed": True}})


if __name__ == "__main__":
    unittest.main()
