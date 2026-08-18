import ast
import math
import unittest
from pathlib import Path

from autoslice.analysis import clip_scoring as legacy_clip_scoring
from autoslice.analysis.review import scoring as clip_scoring

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.scoring"
LEGACY_MODULE = "autoslice.analysis.clip_scoring"
SCORING_CONSUMERS = {
    "autoslice.analysis.candidates",
    "autoslice.analysis.clip_review",
    "autoslice.analysis.slice_decisions",
    "autoslice.pipeline",
    "autoslice.topic_engine",
}


class ClipScoringTests(unittest.TestCase):
    @staticmethod
    def _module_name(path):
        parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    @staticmethod
    def _imported_names(path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
        return imported

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

    def test_scoring_owner_and_legacy_facade_have_expected_definitions(self):
        owner_path = SRC_ROOT / "autoslice/analysis/review/scoring.py"
        legacy_path = SRC_ROOT / "autoslice/analysis/clip_scoring.py"
        owner_tree = ast.parse(owner_path.read_text(encoding="utf-8"))
        legacy_tree = ast.parse(legacy_path.read_text(encoding="utf-8"))
        owner_functions = {
            node.name
            for node in owner_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertEqual(
            owner_functions,
            {
                "_format_time",
                "build_clip_candidate_review_audit",
                "clip_interest_reason",
                "clip_manual_star_count",
                "clip_star_bonus_cap",
                "parse_clip_interest_score",
                "parse_clip_star_bonus",
            },
        )
        self.assertFalse(
            any(isinstance(node, ast.ClassDef) for node in owner_tree.body)
        )
        self.assertFalse(
            any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                for node in legacy_tree.body
            )
        )

    def test_legacy_facade_forwards_every_owner_name_by_identity(self):
        self.assertIs(
            legacy_clip_scoring.FACADE_EXPORTS,
            clip_scoring.FACADE_EXPORTS,
        )
        for name, value in vars(clip_scoring).items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertIs(getattr(legacy_clip_scoring, name), value)

    def test_scoring_consumers_import_owner_and_no_production_uses_facade(self):
        owner_importers = set()
        legacy_importers = set()
        for path in SRC_ROOT.rglob("*.py"):
            module_name = self._module_name(path)
            imported = self._imported_names(path)
            if OWNER_MODULE in imported:
                owner_importers.add(module_name)
            if LEGACY_MODULE in imported:
                legacy_importers.add(module_name)

        self.assertEqual(owner_importers, SCORING_CONSUMERS | {LEGACY_MODULE})
        self.assertEqual(legacy_importers, set())

    def test_scoring_owner_has_no_reverse_dependency_on_consumers_or_facade(self):
        imported = self._imported_names(
            SRC_ROOT / "autoslice/analysis/review/scoring.py"
        )

        self.assertTrue(
            imported.isdisjoint(SCORING_CONSUMERS | {LEGACY_MODULE})
        )


if __name__ == "__main__":
    unittest.main()
