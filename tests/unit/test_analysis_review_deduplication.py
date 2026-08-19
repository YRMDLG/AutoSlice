import ast
import unittest
from pathlib import Path

from autoslice import topic_engine
from autoslice.analysis import boundaries, candidates
from autoslice.analysis.review import deduplication as clip_deduplication

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.deduplication"
OWNER_PATH = SRC_ROOT / "autoslice/analysis/review/deduplication.py"
REVIEW_INIT_PATH = SRC_ROOT / "autoslice/analysis/review/__init__.py"
FUNCTION_NAMES = (
    "_overlap_ratio",
    "_is_duplicate_topic",
    "_dedupe_clip_marks",
)
PRODUCTION_IMPORTERS = {
    "autoslice.analysis.boundaries",
    "autoslice.analysis.candidates",
    "autoslice.analysis.manual.candidates",
    "autoslice.analysis.manual.review",
    "autoslice.analysis.report.cleanup",
    "autoslice.analysis.review.decisions",
    "autoslice.analysis.review.finalization",
    "autoslice.analysis.topic.analysis",
    "autoslice.reporting",
    "autoslice.slicing",
    "autoslice.topic_engine",
}


def _module_name(path):
    parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_names(tree):
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


class ReviewDeduplicationBehaviorTests(unittest.TestCase):
    def test_overlap_uses_shorter_interval_and_duplicate_thresholds(self):
        self.assertEqual(
            clip_deduplication._overlap_ratio(0, 100, 50, 60),
            1.0,
        )
        self.assertEqual(
            clip_deduplication._overlap_ratio(0, 100, 15, 115),
            0.85,
        )

        existing = [{"start": 100, "end": 200, "title": "原话题"}]
        self.assertTrue(
            clip_deduplication._is_duplicate_topic(
                {"start": 103, "end": 203, "title": "换标题"},
                existing,
            )
        )
        self.assertTrue(
            clip_deduplication._is_duplicate_topic(
                {"start": 115, "end": 215, "title": "换标题"},
                existing,
            )
        )
        self.assertFalse(
            clip_deduplication._is_duplicate_topic(
                {"start": 116, "end": 216, "title": "独立话题"},
                existing,
            )
        )

    def test_same_title_with_half_clip_overlap_is_deduplicated(self):
        marks = [
            {
                "start": 0,
                "end": 100,
                "topic_start": 0,
                "topic_end": 40,
                "title": "同标题",
            },
            {
                "start": 50,
                "end": 150,
                "topic_start": 100,
                "topic_end": 140,
                "title": "同标题",
            },
        ]

        self.assertEqual(
            clip_deduplication._dedupe_clip_marks(marks),
            [marks[0]],
        )

    def test_outro_compares_first_but_results_stay_in_time_order(self):
        marks = [
            {"start": 10, "end": 40, "title": "开场"},
            {"start": 100, "end": 190, "title": "普通尾部候选"},
            {
                "start": 102,
                "end": 200,
                "title": "系列收播",
                "clip_type": "stream_outro",
            },
            {"start": 300, "end": 360, "title": "后续片段"},
        ]

        result = clip_deduplication._dedupe_clip_marks(marks)

        self.assertEqual(
            [item["title"] for item in result],
            ["开场", "系列收播", "后续片段"],
        )
        self.assertEqual(
            [(item["start"], item["end"]) for item in result],
            [(10, 40), (102, 200), (300, 360)],
        )

    def test_invalid_marks_are_skipped_and_valid_values_are_normalised(self):
        result = clip_deduplication._dedupe_clip_marks(
            [
                {"start": 10},
                {"start": 20, "end": 20},
                {"start": 30, "end": 40, "topic_start": 35, "topic_end": 35},
                {"start": 50.9, "end": 70.2, "title": "  "},
            ]
        )

        self.assertEqual(
            result,
            [{"start": 50, "end": 70, "title": "未命名片段"}],
        )


class ReviewDeduplicationOwnershipTests(unittest.TestCase):
    def test_owner_has_only_facade_exports_and_three_unique_functions(self):
        owner_tree = ast.parse(OWNER_PATH.read_text(encoding="utf-8"))
        definitions = [
            node
            for node in owner_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        assignments = [
            node
            for node in owner_tree.body
            if isinstance(node, ast.Assign)
        ]

        self.assertEqual([node.name for node in definitions], list(FUNCTION_NAMES))
        self.assertEqual(len(assignments), 1)
        self.assertEqual(
            [target.id for target in assignments[0].targets],
            ["FACADE_EXPORTS"],
        )
        self.assertFalse(any(isinstance(node, ast.ClassDef) for node in owner_tree.body))
        self.assertEqual(
            clip_deduplication.FACADE_EXPORTS,
            {name: name for name in FUNCTION_NAMES},
        )

        implementations = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in FUNCTION_NAMES:
                        implementations.append((_module_name(path), node.name))
        self.assertEqual(
            set(implementations),
            {(OWNER_MODULE, name) for name in FUNCTION_NAMES},
        )

    def test_boundaries_candidates_and_topic_engine_keep_object_identity(self):
        for name in FUNCTION_NAMES:
            owner = getattr(clip_deduplication, name)
            with self.subTest(name=name):
                self.assertNotIn(name, boundaries.FACADE_EXPORTS)
                self.assertIs(getattr(boundaries, name), owner)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)

    def test_owner_has_exact_production_importers_and_no_dependencies(self):
        owner_importers = set()
        old_indirect_references = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            module_name = _module_name(path)
            if OWNER_MODULE in _imported_names(tree):
                owner_importers.add(module_name)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in FUNCTION_NAMES
                    and isinstance(node.value, ast.Name)
                    and node.value.id in {"boundaries", "boundary_analysis"}
                ):
                    old_indirect_references.append(
                        (module_name, node.value.id, node.attr, node.lineno)
                    )

        owner_tree = ast.parse(OWNER_PATH.read_text(encoding="utf-8"))
        self.assertEqual(owner_importers, PRODUCTION_IMPORTERS)
        self.assertEqual(_imported_names(owner_tree), set())
        self.assertEqual(old_indirect_references, [])

    def test_review_package_stays_lazy_and_declares_deduplication(self):
        source = REVIEW_INIT_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        all_assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )

        self.assertEqual(imports, [])
        self.assertEqual(
            ast.literal_eval(all_assignment.value),
            [
                "candidates",
                "decisions",
                "deduplication",
                "finalization",
                "outro",
                "policy",
                "prompt",
                "reconciliation",
                "scoring",
                "triggers",
                "workflow",
            ],
        )
        self.assertIn(
            '__all__ = ["candidates", "decisions", "deduplication", "finalization", "outro", "policy", "prompt", "reconciliation", "scoring", "triggers", "workflow"]',
            source.splitlines(),
        )


if __name__ == "__main__":
    unittest.main()
