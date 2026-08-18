import ast
import copy
import unittest
from pathlib import Path

from autoslice.analysis import clip_policy
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis import slice_decisions as legacy_slice_decisions
from autoslice.analysis.review import decisions as slice_decisions

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"


class SliceDecisionOwnershipTests(unittest.TestCase):
    def test_review_owner_and_legacy_facade_preserve_identity(self):
        owner_path = SRC_ROOT / "autoslice/analysis/review/decisions.py"
        facade_path = SRC_ROOT / "autoslice/analysis/slice_decisions.py"
        owner_tree = ast.parse(owner_path.read_text(encoding="utf-8"))
        facade_tree = ast.parse(facade_path.read_text(encoding="utf-8"))
        definition_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

        self.assertEqual(len(owner_path.read_text(encoding="utf-8").splitlines()), 545)
        self.assertEqual(
            len([node for node in owner_tree.body if isinstance(node, ast.FunctionDef)]),
            12,
        )
        self.assertFalse(
            any(isinstance(node, ast.ClassDef) for node in owner_tree.body)
        )
        self.assertEqual(len(facade_path.read_text(encoding="utf-8").splitlines()), 10)
        self.assertFalse(
            any(isinstance(node, definition_types) for node in facade_tree.body)
        )
        self.assertIs(legacy_slice_decisions.FACADE_EXPORTS, slice_decisions.FACADE_EXPORTS)
        for name, value in vars(slice_decisions).items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertIs(getattr(legacy_slice_decisions, name), value)


class SliceDecisionWindowTests(unittest.TestCase):
    def test_peak_focus_and_short_long_topic_windows(self):
        long_topic = {
            "start": 100,
            "end": 600,
            "title": "完整生日事件",
            "body": ["·事件从准备持续到最终反应"],
        }
        peaks = [(150, 80), (400, 120)]

        focus = slice_decisions.topic_peak_focus_window(long_topic, peaks)

        self.assertEqual(
            focus,
            {"start": 400, "end": 460, "anchor": 430, "density": 120},
        )

        short_topic = {
            "start": 100,
            "end": 200,
            "title": "短事件完整反应",
            "body": ["·完整经过和结果"],
        }
        short_result = slice_decisions.assign_topic_slice_window(
            short_topic,
            [(120, 100)],
        )
        long_result = slice_decisions.assign_topic_slice_window(long_topic, peaks)

        self.assertIs(short_result, short_topic)
        self.assertEqual((short_topic["slice_start"], short_topic["slice_end"]), (100, 200))
        self.assertEqual(short_topic["slice_anchor"], 150)
        self.assertIs(long_result, long_topic)
        self.assertEqual((long_topic["slice_start"], long_topic["slice_end"]), (400, 460))
        self.assertEqual(long_topic["slice_anchor_source"], "弹幕峰值")
        self.assertTrue(any(line.startswith("·切片核心：") for line in long_topic["body"]))

        without_peak = {
            "start": 100,
            "end": 200,
            "title": "没有峰值的事件",
            "can_slice": True,
        }
        self.assertIs(
            slice_decisions.assign_topic_slice_window(without_peak, []),
            without_peak,
        )
        self.assertFalse(without_peak["can_slice"])

    def test_content_cuttable_rejects_all_non_content_categories(self):
        valid = {"title": "草莓蛋糕烤糊", "body": ["·主播关闭冒烟烤箱"]}
        rejected = (
            {**valid, "fallback": True},
            {**valid, "reference_only": True},
            {**valid, "source": "manual_timeline"},
            {**valid, "source": "optimized_manual_timeline", "ai_enriched": False},
            {"title": "其他话题", "body": ["·有正文但标题无效"]},
            {"title": "", "body": []},
            {"title": "游戏过程", "body": ["·仅播放游戏角色对话语音"]},
        )

        self.assertTrue(slice_decisions.is_content_cuttable_topic(valid))
        for topic in rejected:
            with self.subTest(topic=topic):
                self.assertFalse(slice_decisions.is_content_cuttable_topic(topic))

    def test_refresh_danmaku_evidence_removes_stale_core_and_peak(self):
        topic = {
            "start": 200,
            "end": 300,
            "body": [
                "·切片核心：旧核心",
                "·弹幕依据：0:00:10 附近峰值约 999 条/分钟",
                "·主播讲清事件结果",
                "●人工时间轴⭐⭐⭐⭐：事件记录",
            ],
        }

        best = slice_decisions.refresh_topic_danmaku_evidence(
            topic,
            [(10, 999), (220, 80)],
        )

        self.assertEqual(best, (220, 80))
        body_text = "\n".join(topic["body"])
        self.assertNotIn("切片核心", body_text)
        self.assertNotIn("0:00:10", body_text)
        self.assertEqual(body_text.count("·弹幕依据："), 1)
        self.assertIn("0:03:40", body_text)
        self.assertLess(body_text.index("·弹幕依据："), body_text.index("●人工时间轴"))


class SliceDecisionEvidenceTests(unittest.TestCase):
    def test_candidate_sources_are_cleaned_deduplicated_and_flagged(self):
        topic = {"clip_candidate_sources": [" 弹幕峰值 ", "", "语义复核"]}

        result = slice_decisions.append_clip_candidate_source(topic, "弹幕峰值")
        slice_decisions.append_clip_candidate_source(topic, "人工高星时间轴")

        self.assertIsNone(result)
        self.assertEqual(
            topic["clip_candidate_sources"],
            ["弹幕峰值", "语义复核", "人工高星时间轴"],
        )
        self.assertTrue(topic["clip_review_candidate"])

    def test_high_star_manual_evidence_supports_direct_nested_and_rejects_missing(self):
        threshold = clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS
        direct_body = {
            "manual_stars": threshold,
            "body": ["●人工时间轴⭐⭐⭐⭐：可追溯记录"],
        }
        direct_entry = {
            "manual_stars": threshold,
            "manual_timeline": [{"start": 120, "stars": threshold}],
        }
        nested_entry = {
            "manual_stars": threshold,
            "manual_timeline": [
                {
                    "start": 100,
                    "stars": 1,
                    "original_entries": [{"start": 130, "stars": threshold}],
                }
            ],
        }
        missing = {"manual_stars": threshold, "body": ["·只有正文，没有人工证据"]}

        self.assertTrue(slice_decisions.has_high_star_manual_evidence(direct_body))
        self.assertTrue(slice_decisions.has_high_star_manual_evidence(direct_entry))
        self.assertTrue(slice_decisions.has_high_star_manual_evidence(nested_entry))
        self.assertFalse(slice_decisions.has_high_star_manual_evidence(missing))
        self.assertFalse(
            slice_decisions.has_high_star_manual_evidence(
                {
                    "manual_stars": threshold - 1,
                    "manual_timeline": [{"stars": threshold + 1}],
                }
            )
        )

    def test_manual_anchor_prefers_stars_then_center_and_falls_back(self):
        threshold = clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS
        topic = {
            "start": 100,
            "end": 300,
            "manual_timeline": [
                {"start": 200, "stars": threshold},
                {"start": 120, "stars": threshold + 1},
                {"start": 190, "stars": threshold + 1},
                {"start": 500, "stars": threshold + 10},
            ],
        }

        self.assertEqual(slice_decisions.manual_review_anchor(topic), 190)
        self.assertEqual(
            slice_decisions.manual_review_anchor(
                {"start": 100, "end": 300, "manual_timeline": [{"start": 500, "stars": 9}]}
            ),
            200,
        )

    def test_review_interest_threshold_and_missing_score_compatibility(self):
        threshold = clip_policy.CLIP_MIN_INTEREST_SCORE

        self.assertFalse(slice_decisions.reviewed_topic_has_required_interest({}))
        self.assertFalse(
            slice_decisions.reviewed_topic_has_required_interest(
                {"clip_review_validated": True, "clip_interest_score": threshold - 0.1}
            )
        )
        self.assertTrue(
            slice_decisions.reviewed_topic_has_required_interest(
                {"clip_review_validated": True, "clip_interest_score": threshold}
            )
        )
        self.assertTrue(
            slice_decisions.reviewed_topic_has_required_interest({"clip_review_validated": True})
        )

    def test_reviewed_semantic_window_keeps_final_topic_boundaries(self):
        topic = {"start": 100, "end": 160, "can_slice": False}

        result = slice_decisions.assign_reviewed_semantic_slice_window(
            topic,
            "语义复核",
            anchor=135,
        )

        self.assertIs(result, topic)
        self.assertEqual((topic["slice_start"], topic["slice_end"]), (100, 160))
        self.assertEqual(topic["slice_anchor"], 135)
        self.assertEqual(topic["slice_anchor_source"], "语义复核")
        self.assertTrue(topic["can_slice"])


class SliceDecisionSelectionTests(unittest.TestCase):
    @staticmethod
    def _reviewed_topic(start, end, title, sources, score=90):
        return {
            "start": start,
            "end": end,
            "title": title,
            "body": [f"·{title}有完整事件和结果"],
            "clip_review_validated": True,
            "clip_interest_score": score,
            "clip_candidate_sources": list(sources),
        }

    def test_reviewed_peak_semantic_and_manual_sources_all_land_on_final_boundaries(self):
        topics = [
            self._reviewed_topic(100, 180, "峰值事件", ["弹幕峰值"]),
            self._reviewed_topic(500, 560, "语义事件", ["语义复核"]),
            {
                **self._reviewed_topic(900, 960, "人工事件", ["人工高星时间轴"]),
                "manual_timeline": [{"start": 920, "stars": 5}],
            },
        ]

        result = slice_decisions.apply_reviewed_slice_decisions(
            topics,
            [(120, 100)],
            avg_density=20,
        )

        self.assertIs(result, topics)
        self.assertEqual(
            [topic["slice_anchor_source"] for topic in topics],
            ["弹幕峰值", "语义复核", "人工高星时间轴"],
        )
        self.assertEqual([topic["slice_anchor"] for topic in topics], [150, 530, 920])
        self.assertEqual(
            [(topic["slice_start"], topic["slice_end"]) for topic in topics],
            [(100, 180), (500, 560), (900, 960)],
        )

    def test_same_peak_can_select_only_one_reviewed_topic(self):
        topics = [
            self._reviewed_topic(100, 190, "高价值峰值事件", ["弹幕峰值"], score=90),
            self._reviewed_topic(110, 180, "低价值峰值事件", ["弹幕峰值"], score=80),
        ]

        slice_decisions.apply_reviewed_slice_decisions(
            topics,
            [(120, 100)],
            avg_density=20,
        )

        self.assertEqual([topic["can_slice"] for topic in topics], [True, False])

    def test_default_has_no_hourly_quota_and_explicit_limit_stays_compatible(self):
        topics = [
            {
                "start": start,
                "end": start + 80,
                "title": f"完整事件{index}",
                "body": [f"·完整事件{index}有明确结果"],
            }
            for index, start in enumerate((100, 500, 900), start=1)
        ]
        peaks = [(120, 100), (520, 90), (920, 80)]
        default_topics = copy.deepcopy(topics)
        limited_topics = copy.deepcopy(topics)

        slice_decisions.apply_danmaku_slice_decisions(
            default_topics,
            peaks,
            avg_density=20,
        )
        slice_decisions.apply_danmaku_slice_decisions(
            limited_topics,
            peaks,
            avg_density=20,
            max_per_hour=2,
        )

        self.assertEqual(sum(topic["can_slice"] for topic in default_topics), 3)
        self.assertEqual(sum(topic["can_slice"] for topic in limited_topics), 2)

    def test_unreviewed_high_star_only_adds_review_candidate(self):
        topic = {
            "start": 500,
            "end": 620,
            "title": "人工记录的完整反转",
            "manual_stars": clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS,
            "body": ["●人工时间轴⭐⭐⭐⭐：完整反转和结果"],
        }

        result = slice_decisions.apply_danmaku_slice_decisions(
            [topic],
            peaks=[],
            avg_density=80,
        )

        self.assertIs(result[0], topic)
        self.assertFalse(topic["can_slice"])
        self.assertTrue(topic["clip_review_candidate"])
        self.assertEqual(topic["clip_candidate_sources"], ["人工高星时间轴"])
        self.assertEqual(slice_decisions.clip_marks_from_topics([topic]), [])

    def test_danmaku_content_alignment_breaks_same_peak_tie(self):
        windows = [(start, 10) for start in range(0, 601, 15)]
        windows[20] = (300, 150)
        messages = [(301 + index * 0.1, "草莓蛋糕烤糊冒烟") for index in range(20)]
        series = danmaku_analysis.DanmakuDensitySeries(
            windows,
            average_density=30,
            duration=660,
            messages=messages,
        )
        topics = [
            {
                "start": 250,
                "end": 400,
                "title": "草莓蛋糕烤糊",
                "body": ["·主播关闭冒烟烤箱"],
            },
            {
                "start": 300,
                "end": 360,
                "title": "讨论游戏布局",
                "body": ["·主播解释第五层布局"],
                "ai_focus_validated": True,
            },
        ]

        slice_decisions.apply_danmaku_slice_decisions(topics, series, avg_density=30)

        self.assertTrue(topics[0]["can_slice"])
        self.assertFalse(topics[1]["can_slice"])
        self.assertGreater(
            topics[0]["danmaku_topic_alignment"],
            topics[1]["danmaku_topic_alignment"],
        )


class ClipMarksTests(unittest.TestCase):
    def test_clip_marks_keep_full_contract_filter_sources_and_dedupe(self):
        valid = {
            "start": 100,
            "end": 200,
            "slice_start": 110,
            "slice_end": 180,
            "title": "回应SC里的草莓蛋糕翻车",
            "publish_title": "投稿标题：草莓蛋糕烤糊后立刻关烤箱",
            "title_hook": "蛋糕冒烟",
            "body": ["·主播回应SC后发现蛋糕冒烟并关闭烤箱"],
            "can_slice": True,
            "slice_anchor": 145,
            "slice_anchor_source": "语义复核",
            "clip_candidate_sources": ["语义复核", "弹幕峰值"],
            "ai_focus_validated": True,
            "clip_interest_score": 88,
            "clip_interest_reason": "诱因和结果完整",
            "clip_timeline_star_bonus": 5,
            "reference_start": 95,
            "reference_end": 205,
        }
        duplicate = {**valid, "slice_anchor_source": "弹幕峰值"}
        invalid_source = {
            **valid,
            "start": 240,
            "end": 300,
            "slice_start": 240,
            "slice_end": 300,
            "slice_anchor": 270,
            "slice_anchor_source": "普通人工星标",
        }

        marks = slice_decisions.clip_marks_from_topics([valid, duplicate, invalid_source])

        self.assertEqual(len(marks), 1)
        mark = marks[0]
        expected = {
            "start",
            "end",
            "title",
            "publish_title",
            "title_hook",
            "report_start",
            "report_end",
            "slice_anchor",
            "slice_anchor_source",
            "clip_candidate_sources",
            "semantic_focus_validated",
            "editorial_interest_score",
            "editorial_interest_reason",
            "timeline_star_bonus",
            "reference_start",
            "reference_end",
            "context_requires_trigger",
            "boundary_evidence",
            "next_report_topic_start",
        }
        self.assertEqual(set(mark), expected)
        self.assertEqual((mark["start"], mark["end"]), (110, 180))
        self.assertEqual((mark["report_start"], mark["report_end"]), (100, 200))
        self.assertTrue(mark["semantic_focus_validated"])
        self.assertTrue(mark["context_requires_trigger"])
        self.assertEqual(mark["next_report_topic_start"], 240)
        self.assertNotIn("投稿标题", mark["publish_title"])
        self.assertIn("草莓蛋糕", mark["publish_title"])


if __name__ == "__main__":
    unittest.main()
