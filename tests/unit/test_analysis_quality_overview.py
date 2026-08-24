import copy
import json
import unittest

from autoslice.analysis import quality_overview
from autoslice.analysis.review import policy as clip_policy


class QualityOverviewTests(unittest.TestCase):
    def test_builds_deterministic_distributions_sorted_clips_and_edge_reasons(self):
        clip_marks = [
            {
                "start": 300,
                "end": 500,
                "title": "三分钟以上片段",
                "editorial_interest_score": 78,
                "editorial_interest_reason": "事件完整但爆点一般",
                "peak_density": 96,
                "slice_anchor_source": "语义复核",
            },
            {
                "start": 20,
                "end": 70,
                "publish_title": "最高价值片段",
                "editorial_interest_score": 92.4,
                "editorial_interest_reason": "诱因与后果完整",
                "peak_density": 188,
                "slice_anchor_source": "弹幕峰值",
            },
            {
                "start": 100,
                "end": 220,
                "title": "标准时长片段",
                "editorial_interest_score": "84.56",
                "slice_peak_density": "123.5",
                "slice_anchor_source": "人工高星时间轴",
            },
            {
                "start": "bad",
                "end": None,
                "title": "",
                "editorial_interest_score": "missing",
                "peak_density": -1,
            },
        ]
        audit = {
            "candidates": [
                {
                    "start": 10,
                    "end": 40,
                    "title": "低于边缘线",
                    "interest_score": 59.9,
                    "interest_reason": "不应进入概览",
                },
                {
                    "start": 50,
                    "end": 80,
                    "title": "边缘候选 A",
                    "interest_score": 60,
                    "interest_reason": "  有清楚诱因\n但反转强度一般  ",
                },
                {
                    "start": 90,
                    "end": 130,
                    "title": "边缘候选 B",
                    "interest_score": 74.9,
                    "interest_reason": "",
                },
                {
                    "start": 140,
                    "end": 180,
                    "title": "已经过线",
                    "interest_score": 75,
                    "interest_reason": "不属于边缘候选",
                },
            ]
        }
        original_marks = copy.deepcopy(clip_marks)
        original_audit = copy.deepcopy(audit)

        overview = quality_overview.build_quality_overview(clip_marks, audit)

        self.assertEqual(overview["final_slice_count"], 4)
        self.assertNotIn("slice_count", overview)
        self.assertEqual(overview["duration_distribution"], {
            "total_seconds": 370,
            "minimum_seconds": 0,
            "maximum_seconds": 200,
            "average_seconds": 92.5,
            "buckets": {
                "under_90_seconds": 2,
                "from_90_to_179_seconds": 1,
                "at_least_180_seconds": 1,
            },
        })
        self.assertEqual(overview["score_distribution"], {
            "scored_count": 3,
            "missing_count": 1,
            "minimum": 78.0,
            "maximum": 92.4,
            "average": 85.0,
        })
        self.assertEqual(overview["anchor_source_counts"], {
            "人工高星时间轴": 1,
            "弹幕峰值": 1,
            "语义复核": 1,
            "锚点未记录": 1,
        })
        self.assertEqual(overview["danmaku_peak_count"], 3)
        self.assertEqual(
            [row["title"] for row in overview["clips"]],
            ["最高价值片段", "标准时长片段", "三分钟以上片段", "未命名切片"],
        )
        self.assertEqual(
            [row["score"] for row in overview["clips"]],
            [92.4, 84.6, 78.0, None],
        )
        self.assertEqual(overview["clips"][1]["peak_density"], 123.5)
        self.assertEqual(
            overview["clips"][1]["reason"],
            "投稿价值理由未记录",
        )
        self.assertEqual(overview["edge_candidate_count"], 2)
        self.assertEqual(
            [row["title"] for row in overview["edge_candidates"]],
            ["边缘候选 B", "边缘候选 A"],
        )
        self.assertEqual(
            overview["edge_candidates"][0]["reason"],
            "投稿价值理由未记录",
        )
        self.assertEqual(
            overview["edge_candidates"][1]["reason"],
            "有清楚诱因 但反转强度一般",
        )
        self.assertEqual(clip_marks, original_marks)
        self.assertEqual(audit, original_audit)

    def test_invalid_nonfinite_boolean_and_path_values_use_safe_fallbacks(self):
        overview = quality_overview.build_quality_overview(
            [
                {
                    "start": True,
                    "end": float("nan"),
                    "title": r"F:\private\recording.flv",
                    "editorial_interest_score": True,
                    "editorial_interest_reason": r"证据在 C:\secret\audit.json",
                    "peak_density": False,
                    "slice_anchor_source": r"\\server\share\anchor.json",
                },
                {
                    "start": 10,
                    "end": 40,
                    "title": "正常标题" * 100,
                    "editorial_interest_score": float("inf"),
                    "editorial_interest_reason": "正常理由" * 200,
                    "peak_density": float("-inf"),
                    "slice_anchor_source": "语义复核",
                },
            ],
            {
                "candidates": [
                    {
                        "start": 50,
                        "end": 80,
                        "title": r"../private/candidate.json",
                        "time_range": r"C:\secret\time.txt",
                        "interest_score": True,
                        "interest_reason": r"file:///C:/secret/reason.txt",
                    },
                    {
                        "start": 90,
                        "end": 130,
                        "title": r"C:\secret\candidate.json",
                        "time_range": None,
                        "interest_score": 70,
                        "interest_reason": r"参考 /home/user/private.txt",
                    },
                ],
            },
        )

        self.assertEqual(overview["final_slice_count"], 2)
        self.assertEqual(overview["score_distribution"]["scored_count"], 0)
        self.assertEqual(overview["duration_distribution"]["total_seconds"], 30)
        self.assertEqual(overview["danmaku_peak_count"], 0)
        self.assertEqual(len(overview["clips"][0]["title"]), 120)
        self.assertTrue(overview["clips"][0]["title"].endswith("…"))
        self.assertEqual(len(overview["clips"][0]["reason"]), 240)
        self.assertTrue(overview["clips"][0]["reason"].endswith("…"))
        self.assertEqual(overview["clips"][1], {
            "title": "未命名切片",
            "start": None,
            "end": None,
            "duration": 0,
            "score": None,
            "peak_density": None,
            "anchor_source": "锚点未记录",
            "reason": "投稿价值理由未记录",
        })
        self.assertEqual(overview["edge_candidate_count"], 1)
        self.assertEqual(overview["edge_candidates"][0], {
            "title": "未命名候选",
            "time_range": "0:01:30－0:02:10",
            "score": 70.0,
            "reason": "投稿价值理由未记录",
        })
        encoded = json.dumps(overview, ensure_ascii=False)
        for path_fragment in ("F:\\", "C:\\", "\\\\server", "/home/", "../"):
            self.assertNotIn(path_fragment, encoded)

    def test_limits_rows_and_encoded_size_without_losing_total_counts(self):
        marks = [{
            "start": index * 100,
            "end": index * 100 + 100,
            "title": "切片" + "长" * 500,
            "editorial_interest_score": 80,
            "editorial_interest_reason": "理由" * 500,
            "slice_anchor_source": "语义复核" * 20,
        } for index in range(500)]
        audit = {"candidates": [{
            "start": index * 100,
            "end": index * 100 + 60,
            "title": "候选" + "长" * 500,
            "interest_score": 70,
            "interest_reason": "原因" * 500,
        } for index in range(500)]}

        overview = quality_overview.build_quality_overview(marks, audit)
        encoded = json.dumps(
            overview,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertEqual(overview["final_slice_count"], 500)
        self.assertNotIn("slice_count", overview)
        self.assertEqual(overview["edge_candidate_count"], 500)
        self.assertGreater(overview["clips_truncated_count"], 0)
        self.assertGreater(overview["edge_candidates_truncated_count"], 0)
        self.assertLessEqual(len(encoded), quality_overview.MAX_OVERVIEW_BYTES)

    def test_anchor_source_counts_are_bounded_without_losing_totals(self):
        marks = [
            {
                "start": index,
                "end": index + 1,
                "slice_anchor_source": f"来源 {index:03d}",
            }
            for index in range(100)
        ]

        overview = quality_overview.build_quality_overview(marks, {})

        self.assertLessEqual(
            len(overview["anchor_source_counts"]),
            quality_overview.MAX_ANCHOR_SOURCE_ROWS + 1,
        )
        self.assertEqual(sum(overview["anchor_source_counts"].values()), 100)
        self.assertEqual(overview["anchor_source_counts"]["其他"], 80)

    def test_edge_candidate_upper_bound_uses_review_policy(self):
        self.assertEqual(
            quality_overview.EDGE_CANDIDATE_MAX_SCORE,
            clip_policy.CLIP_MIN_INTEREST_SCORE,
        )


if __name__ == "__main__":
    unittest.main()
