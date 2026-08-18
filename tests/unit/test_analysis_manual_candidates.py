import copy
import unittest

from autoslice.analysis import manual_candidates


class ManualCandidateTests(unittest.TestCase):
    def test_manual_entry_matches_topic_uses_ranges_and_optional_margin(self):
        topic = {"start": 100, "end": 120}

        self.assertTrue(
            manual_candidates.manual_entry_matches_topic(
                {"start": 119, "end": 140},
                topic,
            )
        )
        self.assertFalse(
            manual_candidates.manual_entry_matches_topic(
                {"start": 120},
                topic,
            )
        )
        self.assertTrue(
            manual_candidates.manual_entry_matches_topic(
                {"start": 121},
                topic,
                margin=2,
            )
        )

    def test_manual_merge_target_rejects_fallback_generic_and_uncuttable_topics(self):
        self.assertFalse(
            manual_candidates.is_manual_merge_target(
                {"title": "真实话题", "fallback": True}
            )
        )
        self.assertFalse(
            manual_candidates.is_manual_merge_target({"title": "日常聊天互动"})
        )
        self.assertFalse(
            manual_candidates.is_manual_merge_target(
                {"title": "游戏片段", "body": ["·全是音乐，仅播放画面"]}
            )
        )
        self.assertTrue(
            manual_candidates.is_manual_merge_target(
                {
                    "title": "人工时间轴重点",
                    "source": "manual_timeline",
                }
            )
        )

    def test_merge_attaches_starred_evidence_to_existing_real_topic(self):
        topics = [
            {
                "start": 100,
                "end": 200,
                "title": "妈妈吐槽电视烧屏",
                "body": ["·主播讲起买电视的经历"],
                "manual_stars": 1,
            }
        ]
        entry = {
            "start": 140,
            "text": "韩国医院电视好用，回国买同品牌却烧屏",
            "stars": 7,
        }
        source_entry = copy.deepcopy(entry)

        result = manual_candidates.merge_manual_timeline_topics(topics, [entry])

        self.assertIs(result, topics)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["manual_stars"], 7)
        self.assertEqual(topics[0]["manual_timeline"], [entry])
        self.assertIn(
            "●人工时间轴⭐⭐⭐⭐⭐：0:02:20 韩国医院电视好用，回国买同品牌却烧屏",
            topics[0]["body"],
        )
        self.assertEqual(entry, source_entry)

    def test_merge_adds_missed_optimized_candidate_as_reference_only(self):
        entry = {
            "start": 300,
            "end": 345,
            "text": "免费游戏坑朋友",
            "summary": ["·假装送游戏后留下社死备注"],
            "stars": 0,
            "source": "optimized_manual_timeline",
            "ai_enriched": True,
            "ai_focus_validated": True,
            "publish_title": "朋友收下免费游戏后账号备注直接社死",
        }

        topics = manual_candidates.merge_manual_timeline_topics([], [entry])

        self.assertEqual(len(topics), 1)
        topic = topics[0]
        self.assertEqual((topic["start"], topic["end"]), (300, 345))
        self.assertFalse(topic["can_slice"])
        self.assertFalse(topic["ai_enriched"])
        self.assertFalse(topic["ai_focus_validated"])
        self.assertTrue(topic["postcheck_pending"])
        self.assertTrue(topic["reference_only"])
        self.assertEqual(topic["publish_title"], entry["publish_title"])

    def test_merge_ignores_unstarred_raw_entry_and_expands_starred_raw_entry(self):
        entries = [
            {"start": 100, "text": "普通记录", "stars": 0},
            {"start": 500, "text": "高星重点", "stars": 4},
        ]

        topics = manual_candidates.merge_manual_timeline_topics([], entries)

        self.assertEqual(len(topics), 1)
        self.assertEqual((topics[0]["start"], topics[0]["end"]), (470, 650))
        self.assertEqual(topics[0]["manual_stars"], 4)
        self.assertFalse(topics[0]["reference_only"])

    def test_topics_group_same_hour_and_respect_gap_and_duration_limits(self):
        entries = [
            {"start": 3500, "text": "普通前情", "stars": 0},
            {
                "start": 3560,
                "text": "高星反转结果",
                "stars": 3,
                "alignment_score": 0.8,
            },
            {"start": 3620, "text": "跨小时新话题", "stars": 0},
            {"start": 3900, "text": "间隔过大的话题", "stars": 0},
        ]

        topics = manual_candidates.topics_from_manual_timeline(
            entries,
            max_gap_sec=120,
            max_group_duration_sec=180,
        )

        self.assertEqual(len(topics), 3)
        self.assertEqual((topics[0]["start"], topics[0]["end"]), (3470, 3710))
        self.assertIn("高星反转结果", topics[0]["title"])
        self.assertEqual(topics[0]["manual_stars"], 3)
        self.assertEqual((topics[1]["start"], topics[1]["end"]), (3620, 3740))
        self.assertEqual((topics[2]["start"], topics[2]["end"]), (3900, 4020))

    def test_explicit_ranges_stay_independent_and_keep_reference_title(self):
        entries = [
            {
                "start": 150,
                "end": 360,
                "text": "裙子被风吹起",
                "stars": 0,
                "explicit_range": True,
                "reference_publish_title": "狂风一吹裙子当场飞起",
            },
            {
                "start": 360,
                "end": 603,
                "text": "上台控场心得",
                "stars": 0,
                "explicit_range": True,
            },
        ]

        topics = manual_candidates.topics_from_manual_timeline(entries)

        self.assertEqual(
            [(topic["start"], topic["end"]) for topic in topics],
            [(150, 360), (360, 603)],
        )
        self.assertIn(
            "·参考投稿标题（仅供核对）：狂风一吹裙子当场飞起",
            topics[0]["body"],
        )

    def test_topics_include_real_subtitle_danmaku_and_manual_evidence(self):
        entries = [
            {"start": 100, "text": "先介绍游戏", "stars": 0},
            {"start": 150, "text": "朋友发现社死备注", "stars": 2},
        ]
        srt_segments = [
            (80, 110, "主播说这个游戏免费送给朋友"),
            (140, 170, "朋友收下后发现账号被改了备注"),
        ]

        topics = manual_candidates.topics_from_manual_timeline(
            entries,
            srt_segments=srt_segments,
            peaks=[(145, 160)],
        )
        body = "\n".join(topics[0]["body"])

        self.assertIn("·字幕核查：", body)
        self.assertIn("·弹幕依据：", body)
        self.assertIn("·时间轴：", body)
        self.assertIn("●人工时间轴⭐⭐：", body)

    def test_sanitize_without_original_evidence_keeps_compatible_copy(self):
        entry = {
            "start": 20,
            "end": 40,
            "text": "兼容旧优化条目",
            "stars": 1,
        }

        result = manual_candidates.sanitize_optimized_manual_entry(entry)

        self.assertEqual(result, entry)
        self.assertIsNot(result, entry)

    def test_sanitize_rejects_ai_content_unrelated_to_all_original_entries(self):
        entry = {
            "text": "草莓蛋糕烤糊以后满屋冒烟",
            "summary": ["·最后只能把烤箱关掉"],
            "original_entries": [
                {"start": 100, "text": "量子计算芯片发布", "stars": 5}
            ],
        }

        self.assertIsNone(
            manual_candidates.sanitize_optimized_manual_entry(entry)
        )

    def test_sanitize_keeps_only_grounded_originals_and_rebuilds_truthful_fields(self):
        entry = {
            "start": 100,
            "end": 200,
            "text": "韩国医院电视好用",
            "summary": ["·回国买同品牌五六年后烧屏"],
            "stars": 5,
            "highlight": True,
            "clock": "错误时间",
            "evidence": [
                "·字幕核查：电视烧屏",
                "●人工时间轴⭐⭐⭐⭐⭐：0:03:00 不相关旧证据",
            ],
            "original_entries": [
                {
                    "start": 120,
                    "clock": "2026-08-15 20:03:06",
                    "text": "韩国医院电视好用，回国买同品牌却烧屏",
                    "stars": 2,
                },
                {
                    "start": 180,
                    "clock": "2026-08-15 20:04:06",
                    "text": "下一个SC问冰淇淋口味",
                    "stars": 5,
                },
            ],
        }
        source = copy.deepcopy(entry)

        result = manual_candidates.sanitize_optimized_manual_entry(entry)

        self.assertEqual(len(result["original_entries"]), 1)
        self.assertIn("韩国医院电视", result["original_entries"][0]["text"])
        self.assertEqual(result["stars"], 2)
        self.assertTrue(result["highlight"])
        self.assertEqual(result["clock"], "2026-08-15 20:03:06")
        self.assertEqual(
            result["evidence"],
            [
                "·字幕核查：电视烧屏",
                "●人工时间轴⭐⭐：0:02:00 韩国医院电视好用，回国买同品牌却烧屏",
            ],
        )
        self.assertEqual(entry, source)


if __name__ == "__main__":
    unittest.main()
