import math
import unittest

from autoslice.analysis import clip_scoring


class ClipScoringTests(unittest.TestCase):
    def test_interest_score_rejects_invalid_nonfinite_and_out_of_range_values(self):
        for value in (None, "", "invalid", -0.1, 100.1, math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                self.assertIsNone(clip_scoring.parse_clip_interest_score(value))

        self.assertEqual(clip_scoring.parse_clip_interest_score("82.56"), 82.6)

    def test_star_bonus_and_cap_only_reward_strong_manual_markers(self):
        self.assertEqual(clip_scoring.parse_clip_star_bonus(8), 8.0)
        self.assertIsNone(clip_scoring.parse_clip_star_bonus(8.1))
        self.assertEqual(
            [clip_scoring.clip_star_bonus_cap(value) for value in (0, 2, 3, 4, 5, 20)],
            [0.0, 0.0, 2.0, 5.0, 8.0, 8.0],
        )
        self.assertEqual(clip_scoring.clip_star_bonus_cap("invalid"), 0.0)

    def test_interest_reason_is_normalized_and_bounded(self):
        reason = clip_scoring.clip_interest_reason({
            "interest_reason": "  前因\n\t反转   后果  " + "长" * 300,
        })

        self.assertTrue(reason.startswith("前因 反转 后果"))
        self.assertLessEqual(len(reason), 240)

    def test_review_audit_filters_noise_sorts_and_preserves_status(self):
        audit = clip_scoring.build_clip_candidate_review_audit([
            {
                "start": 20,
                "end": 30,
                "title": "已切片",
                "clip_candidate_sources": ["danmaku_peak"],
                "manual_stars": "invalid",
                "clip_interest_score": "86.54",
                "can_slice": True,
            },
            {
                "start": 10,
                "end": 15,
                "title": "未通过",
                "clip_review_validated": False,
                "clip_review_rejection": "证据不足",
                "manual_stars": 4,
            },
            {"start": 1, "end": 2, "title": "普通报告话题"},
        ])

        self.assertEqual(audit["candidate_count"], 2)
        self.assertEqual(audit["approved_count"], 1)
        self.assertEqual(
            [item["title"] for item in audit["candidates"]],
            ["未通过", "已切片"],
        )
        self.assertEqual(audit["candidates"][0]["status"], "未通过复核")
        self.assertEqual(audit["candidates"][0]["manual_stars"], 4)
        self.assertEqual(audit["candidates"][1]["status"], "已通过并生成切片")
        self.assertEqual(audit["candidates"][1]["manual_stars"], 0)
        self.assertEqual(audit["candidates"][1]["interest_score"], 86.5)


if __name__ == "__main__":
    unittest.main()
