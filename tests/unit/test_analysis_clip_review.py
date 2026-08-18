import ast
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.analysis import clip_review as legacy_clip_review
from autoslice.analysis.review import workflow as clip_review
from autoslice.streamer_profiles import streamer_profile_context

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"


class ClipReviewTests(unittest.TestCase):
    def setUp(self):
        self.profile_context = streamer_profile_context("zeyin")
        self.profile_context.__enter__()
        self.addCleanup(self.profile_context.__exit__, None, None, None)

    @staticmethod
    def _topic(**overrides):
        topic = {
            "start": 100,
            "end": 180,
            "title": "袜子破洞引发吐槽",
            "body": ["·首轮 AI 摘要"],
            "can_slice": True,
            "manual_stars": 4,
        }
        topic.update(overrides)
        return topic

    def test_owner_and_legacy_facade_keep_definition_and_identity_contracts(self):
        owner_path = SRC_ROOT / "autoslice/analysis/review/workflow.py"
        facade_path = SRC_ROOT / "autoslice/analysis/clip_review.py"
        owner_tree = ast.parse(owner_path.read_text(encoding="utf-8"))
        facade_tree = ast.parse(facade_path.read_text(encoding="utf-8"))
        owner_functions = {
            node.name
            for node in owner_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertEqual(owner_functions, {"review_peak_selected_topics"})
        self.assertFalse(
            any(isinstance(node, ast.ClassDef) for node in owner_tree.body)
        )
        self.assertFalse(
            any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                for node in facade_tree.body
            )
        )
        self.assertIs(legacy_clip_review.FACADE_EXPORTS, clip_review.FACADE_EXPORTS)
        for name, value in vars(clip_review).items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertIs(getattr(legacy_clip_review, name), value)

    def test_no_selected_candidate_returns_without_calling_llm(self):
        topics = [self._topic(can_slice=False, clip_review_candidate=False)]

        with patch("autoslice.llm.transport.call_llm_with_retry") as call:
            warning = clip_review.review_peak_selected_topics(
                topics,
                srt_segments=[],
                peaks=[],
            )

        self.assertIsNone(warning)
        call.assert_not_called()

    def test_valid_candidate_applies_focus_score_and_checkpoint(self):
        topics = [self._topic()]
        checkpoints = []
        progress = []
        response = json.dumps(
            {
                "topics": [
                    {
                        "id": 1,
                        "valid": True,
                        "title": "袜子破洞现场找人背锅",
                        "publish_title": "袜子破了还要怪洗衣机😂",
                        "focus_start": "0:01:40",
                        "focus_end": "0:02:50",
                        "points": ["音音发现袜子破了后解释完整经过"],
                        "base_interest_score": 70,
                        "timeline_star_bonus": 5,
                        "interest_reason": "反差和原话都完整",
                    }
                ]
            },
            ensure_ascii=False,
        )

        with patch(
            "autoslice.llm.transport.call_llm_with_retry",
            return_value=response,
        ) as call:
            warning = clip_review.review_peak_selected_topics(
                topics,
                srt_segments=[(80, 190, "音音发现袜子破了后解释完整经过")],
                peaks=[],
                streamer_name="音音",
                progress_callback=lambda *args: progress.append(args),
                checkpoint_callback=lambda *args: checkpoints.append(args),
            )

        self.assertIsNone(warning)
        self.assertEqual(call.call_count, 1)
        self.assertTrue(call.call_args.kwargs["require_json"])
        self.assertEqual(call.call_args.kwargs["reasoning_stage"], "review")
        self.assertTrue(topics[0]["clip_review_validated"])
        self.assertIsNone(topics[0]["clip_review_rejection"])
        self.assertEqual(topics[0]["clip_review_attempts"], 1)
        self.assertEqual(topics[0]["clip_interest_base_score"], 70.0)
        self.assertEqual(topics[0]["clip_timeline_star_bonus"], 5.0)
        self.assertEqual(topics[0]["clip_interest_score"], 75.0)
        self.assertEqual((topics[0]["start"], topics[0]["end"]), (100, 170))
        self.assertFalse(topics[0]["can_slice"])
        self.assertEqual(topics[0]["publish_title"], "【泽音】袜子破了还要怪洗衣机😂")
        self.assertTrue(progress)
        self.assertEqual(len(checkpoints), 1)
        self.assertIs(checkpoints[0][0], topics)
        self.assertEqual(checkpoints[0][1], [])
        self.assertEqual(checkpoints[0][2:], ("首轮", 1, 1))

    def test_explicit_rejection_finishes_without_retry(self):
        topics = [self._topic(manual_stars=5)]
        response = json.dumps(
            {
                "topics": [
                    {
                        "id": 1,
                        "valid": False,
                        "reason": "字幕只有普通闲聊",
                        "base_interest_score": 40,
                        "timeline_star_bonus": 8,
                        "interest_reason": "没有独立事件",
                    }
                ]
            },
            ensure_ascii=False,
        )

        with patch(
            "autoslice.llm.transport.call_llm_with_retry",
            return_value=response,
        ) as call:
            warning = clip_review.review_peak_selected_topics(
                topics,
                srt_segments=[(100, 180, "普通闲聊")],
                peaks=[],
            )

        self.assertIsNone(warning)
        self.assertEqual(call.call_count, 1)
        self.assertFalse(topics[0]["clip_review_validated"])
        self.assertEqual(topics[0]["clip_review_rejection"], "字幕只有普通闲聊")
        self.assertEqual(topics[0]["clip_interest_base_score"], 40.0)
        self.assertEqual(topics[0]["clip_timeline_star_bonus"], 8.0)
        self.assertEqual(topics[0]["clip_interest_reason"], "没有独立事件")

    def test_api_failure_retries_all_rounds_and_returns_safe_warning(self):
        topics = [self._topic()]

        with patch(
            "autoslice.llm.transport.call_llm_with_retry",
            side_effect=RuntimeError("temporary upstream failure"),
        ) as call:
            warning = clip_review.review_peak_selected_topics(
                topics,
                srt_segments=[(100, 180, "完整字幕")],
                peaks=[],
            )

        self.assertEqual(call.call_count, 3)
        self.assertEqual(topics[0]["clip_review_attempts"], 3)
        self.assertFalse(topics[0]["clip_review_validated"])
        self.assertIn("API复核失败", topics[0]["clip_review_rejection"])
        self.assertIn("仍有 1 项", warning)
        self.assertIn("未通过项不会自动切片", warning)


if __name__ == "__main__":
    unittest.main()
