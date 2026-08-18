import json
import unittest

from autoslice.analysis import clip_policy, clip_review_prompt
from autoslice.streamer_profiles import streamer_profile_context


class ClipReviewPromptTests(unittest.TestCase):
    def setUp(self):
        self.profile_context = streamer_profile_context("zeyin")
        self.profile_context.__enter__()
        self.addCleanup(self.profile_context.__exit__, None, None, None)

    @staticmethod
    def _payload(prompt):
        return json.loads(prompt.rsplit("候选数据：\n", 1)[1])

    @staticmethod
    def _candidate():
        return {
            "start": 100,
            "end": 220,
            "slice_anchor": 160,
            "clip_candidate_sources": ["弹幕峰值", "人工高星时间轴"],
            "title": "袜子破洞引发吐槽",
            "publish_title": "【泽音】袜子破了还要怪洗衣机😂",
            "publish_title_locked": True,
            "manual_stars": 4,
            "manual_timeline": [
                {"publish_title": "袜子破洞后现场找洗衣机背锅"},
            ],
            "body": [
                "·字幕核查：音音明确说袜子破了",
                "●人工时间轴⭐⭐⭐⭐：袜子破洞",
                "·时间轴：补充人工记录",
                "·弹幕依据：0:02:20 附近峰值约 80 条/分钟",
                *[f"·补充证据 {index}" for index in range(1, 26)],
            ],
            "core_subtitle_evidence": ["字幕核查：核心原话"],
            "danmaku_peak_start": 130,
            "danmaku_selection_score": 72.5,
            "danmaku_interaction_signal": "具体互动明显",
        }

    def test_payload_keeps_time_sources_titles_and_evidence_channels(self):
        prompt = clip_review_prompt.build_clip_candidate_review_prompt(
            [self._candidate()],
            streamer_name="音音",
        )
        payload = self._payload(prompt)

        self.assertEqual(len(payload), 1)
        item = payload[0]
        self.assertEqual(item["id"], 1)
        self.assertEqual(item["reference_start"], "0:01:40")
        self.assertEqual(item["reference_end"], "0:03:40")
        self.assertEqual(item["candidate_anchor"], "0:02:40")
        self.assertEqual(
            item["candidate_sources"],
            ["弹幕峰值", "人工高星时间轴"],
        )
        self.assertEqual(item["provisional_title"], "袜子破洞引发吐槽")
        self.assertEqual(
            item["reference_publish_titles"],
            [
                "【泽音】袜子破了还要怪洗衣机😂",
                "袜子破洞后现场找洗衣机背锅",
            ],
        )
        self.assertTrue(item["publish_title_locked"])
        self.assertEqual(item["manual_star_count"], 4)
        self.assertEqual(
            item["subtitle_evidence"],
            ["字幕核查：音音明确说袜子破了"],
        )
        self.assertEqual(
            item["manual_evidence"],
            ["人工时间轴⭐⭐⭐⭐：袜子破洞", "时间轴：补充人工记录"],
        )
        self.assertEqual(
            item["density_evidence"],
            ["弹幕依据：0:02:20 附近峰值约 80 条/分钟"],
        )
        self.assertEqual(item["core_subtitle_evidence"], ["字幕核查：核心原话"])
        self.assertEqual(len(item["evidence"]), 24)
        self.assertIn("danmaku_evidence", item)
        self.assertIn(str(clip_policy.CLIP_MIN_INTEREST_SCORE), prompt)
        self.assertIn(str(clip_policy.TOPIC_REVIEW_FOCUS_MAX_SEC), prompt)

    def test_compact_payload_limits_evidence_without_losing_channels(self):
        prompt = clip_review_prompt.build_clip_candidate_review_prompt(
            [self._candidate()],
            streamer_name="音音",
            compact=True,
        )
        item = self._payload(prompt)[0]

        self.assertEqual(len(item["evidence"]), 10)
        self.assertEqual(len(item["subtitle_evidence"]), 1)
        self.assertEqual(len(item["manual_evidence"]), 2)
        self.assertEqual(len(item["density_evidence"]), 1)

    def test_defaults_use_start_anchor_nonnegative_stars_and_generic_title(self):
        prompt = clip_review_prompt.build_clip_candidate_review_prompt(
            [{"start": 0, "end": 10, "manual_stars": -3}],
            streamer_name="音音",
        )
        item = self._payload(prompt)[0]

        self.assertEqual(item["candidate_anchor"], "0:00:00")
        self.assertEqual(item["manual_star_count"], 0)
        self.assertEqual(item["provisional_title"], "待核查高能片段")
        self.assertEqual(item["candidate_sources"], [])
        self.assertFalse(item["publish_title_locked"])

    def test_empty_candidates_render_an_empty_payload(self):
        prompt = clip_review_prompt.build_clip_candidate_review_prompt(
            [],
            streamer_name="音音",
        )
        self.assertEqual(self._payload(prompt), [])


if __name__ == "__main__":
    unittest.main()
