import ast
import unittest
from pathlib import Path

from autoslice.analysis import (
    clip_review_candidates as legacy_clip_review_candidates,
)
from autoslice.analysis.review import candidates as clip_review_candidates

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.candidates"
LEGACY_MODULE = "autoslice.analysis.clip_review_candidates"
CANDIDATE_CONSUMERS = {
    "autoslice.analysis.candidates",
    "autoslice.analysis.manual.review",
    "autoslice.analysis.review.workflow",
    "autoslice.topic_engine",
}


def _module_name(path):
    parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


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


class ClipReviewCandidateTests(unittest.TestCase):
    def test_legacy_facade_is_definition_free_and_forwards_owner_by_identity(self):
        facade_path = SRC_ROOT / "autoslice/analysis/clip_review_candidates.py"
        tree = ast.parse(facade_path.read_text(encoding="utf-8"))
        definitions = [
            node
            for node in tree.body
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            )
        ]

        self.assertEqual(definitions, [])
        self.assertIs(
            legacy_clip_review_candidates.FACADE_EXPORTS,
            clip_review_candidates.FACADE_EXPORTS,
        )
        for name, value in vars(clip_review_candidates).items():
            if not name.startswith("__"):
                with self.subTest(name=name):
                    self.assertIs(
                        getattr(legacy_clip_review_candidates, name),
                        value,
                    )

    def test_owner_has_exact_consumers_and_no_reverse_dependencies(self):
        owner_importers = set()
        legacy_importers = set()
        for path in SRC_ROOT.rglob("*.py"):
            module_name = _module_name(path)
            imported = _imported_names(path)
            if OWNER_MODULE in imported and module_name != LEGACY_MODULE:
                owner_importers.add(module_name)
            if LEGACY_MODULE in imported:
                legacy_importers.add(module_name)

        self.assertEqual(owner_importers, CANDIDATE_CONSUMERS)
        self.assertEqual(legacy_importers, set())

        owner_imports = _imported_names(
            SRC_ROOT / "autoslice/analysis/review/candidates.py"
        )
        forbidden = CANDIDATE_CONSUMERS | {
            LEGACY_MODULE,
            "autoslice.pipeline",
        }
        self.assertTrue(owner_imports.isdisjoint(forbidden))

    def test_fresh_evidence_rebuilds_subtitle_danmaku_and_manual_sources(self):
        topic = {
            "start": 100,
            "end": 160,
            "body": ["·上一轮 AI 自行总结的内容"],
            "manual_timeline": [
                {
                    "stars": 2,
                    "original_entries": [
                        {"start": 110, "text": "第一条人工记录", "stars": 2},
                        {"start": 120, "text": "五星以上记录", "stars": 7},
                        {"start": 110, "text": "第一条人工记录", "stars": 2},
                    ],
                },
                {"start": 130, "text": "普通人工记录", "stars": 0},
            ],
        }

        evidence = clip_review_candidates.fresh_manual_topic_evidence(
            topic,
            srt_segments=[
                (90, 110, "前情"),
                (120, 150, "核心"),
                (200, 210, "范围外"),
            ],
            peaks=[(100, 30), (300, 50)],
        )

        self.assertIn("·弹幕依据：0:01:40 附近峰值约 30 条/分钟", evidence)
        self.assertTrue(
            any(line.startswith("·字幕核查：") and "前情核心" in line for line in evidence)
        )
        self.assertIn("●人工时间轴⭐⭐：0:01:50 第一条人工记录", evidence)
        self.assertIn("●人工时间轴⭐⭐⭐⭐⭐：0:02:00 五星以上记录", evidence)
        self.assertIn("·时间轴：0:02:10 普通人工记录", evidence)
        self.assertEqual(
            evidence.count("●人工时间轴⭐⭐：0:01:50 第一条人工记录"),
            1,
        )
        self.assertFalse(any("上一轮 AI" in line for line in evidence))

    def test_review_candidate_expands_context_and_adds_fresh_peak_features(self):
        topic = {
            "start": 100,
            "end": 160,
            "title": "袜子破洞引发吐槽",
            "body": ["·上一轮 AI 摘要"],
            "manual_timeline": [
                {"start": 115, "text": "袜子破了", "stars": 3},
            ],
        }
        srt_segments = [
            (50, 70, "前置说明"),
            (105, 130, "核心字幕"),
            (150, 170, "收尾字幕"),
            (230, 240, "范围外字幕"),
        ]
        peaks = [(110, 60)]
        density_series = [(0, 10), (60, 12), (110, 60), (170, 10), (230, 9)]

        candidate = clip_review_candidates.build_clip_review_candidate(
            topic,
            srt_segments,
            peaks,
            density_series=density_series,
        )

        self.assertEqual((candidate["start"], candidate["end"]), (55, 220))
        self.assertEqual(
            (candidate["start_str"], candidate["end_str"]),
            ("0:00:55", "0:03:40"),
        )
        self.assertEqual(
            (candidate["review_original_start"], candidate["review_original_end"]),
            (100, 160),
        )
        self.assertTrue(
            any("核心字幕" in line for line in candidate["core_subtitle_evidence"])
        )
        self.assertIn("袜子破洞引发吐槽", candidate["title_cue_context"])
        self.assertIn("核心字幕", candidate["title_cue_context"])
        self.assertFalse(any("上一轮 AI" in line for line in candidate["body"]))
        self.assertTrue(any("前置说明" in line for line in candidate["body"]))
        self.assertEqual(candidate["danmaku_peak_start"], 110)
        self.assertGreater(candidate["danmaku_selection_score"], 0)
        self.assertGreater(candidate["danmaku_local_surge_ratio"], 1)
        self.assertEqual(candidate["danmaku_interaction_signal"], "无原文")
        self.assertIsNone(candidate["danmaku_content_evidence"])
        self.assertEqual((topic["start"], topic["end"]), (100, 160))
        self.assertNotIn("review_original_start", topic)

    def test_existing_danmaku_content_is_preserved_without_recomputation(self):
        evidence = {"representative_messages": [{"text": "具体互动"}]}
        topic = {
            "start": 100,
            "end": 160,
            "title": "已有弹幕原文",
            "danmaku_content_evidence": evidence,
        }

        candidate = clip_review_candidates.build_clip_review_candidate(
            topic,
            [(105, 130, "核心字幕")],
            [(110, 60)],
            density_series=[(0, 10), (110, 60), (220, 9)],
        )

        self.assertIs(candidate["danmaku_content_evidence"], evidence)
        self.assertNotIn("danmaku_peak_start", candidate)
        self.assertNotIn("danmaku_selection_score", candidate)

    def test_review_context_clamps_at_zero_and_handles_missing_optional_evidence(self):
        candidate = clip_review_candidates.build_clip_review_candidate(
            {"start": 20, "end": 30, "title": "开场片段"},
            [],
            [],
        )

        self.assertEqual((candidate["start"], candidate["end"]), (0, 90))
        self.assertEqual(candidate["core_subtitle_evidence"], [])
        self.assertEqual(candidate["body"], [])
        self.assertNotIn("danmaku_peak_start", candidate)


if __name__ == "__main__":
    unittest.main()
