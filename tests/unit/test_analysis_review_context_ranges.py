import ast
import copy
import hashlib
import unittest
from pathlib import Path

from autoslice import topic_engine
from autoslice.analysis import boundaries, candidates
from autoslice.analysis import review as review_package
from autoslice.analysis.review import context_ranges

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.context_ranges"
OWNER_PATH = SRC_ROOT / "autoslice/analysis/review/context_ranges.py"
BOUNDARIES_PATH = SRC_ROOT / "autoslice/analysis/boundaries.py"

RANGE_NAMES = (
    "_find_relevant_topic_context_start",
    "_find_relevant_topic_context_end",
)
EXPECTED_HASHES = {
    "_find_relevant_topic_context_start": (
        "93a50855d30ae89cf42d7d818e9670491088c70f4334564aa52da8fc0577f7bb",
        "f66d564573cf2f992b8546673cd3f5aa466be7bd6625e308455bbf02683b27ff",
    ),
    "_find_relevant_topic_context_end": (
        "f6a9095c0f2b4d2e975a1f6967e1e8d41e32fdb8da73e8890aa4052e8ba8dd01",
        "11fea959ca4f071362a4337aea7ac5f39b2deb351f32afbc2f6f2df745b74717",
    ),
}
OWNER_DEPENDENCIES = {
    "autoslice.analysis.review.context_evidence",
    "autoslice.analysis.review.policy",
    "autoslice.analysis.review.transitions",
}
PRODUCTION_IMPORTERS = {
    "autoslice.analysis.boundaries",
    "autoslice.analysis.candidates",
    "autoslice.topic_engine",
}


def _module_name(path):
    parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _direct_imports(tree):
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.update(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if alias.name != "*"
            )
    return imported


def _definitions(source):
    return {
        node.name: node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name in RANGE_NAMES
    }


class ReviewContextRangeOwnershipTests(unittest.TestCase):
    def test_migrated_source_and_ast_hashes_are_exact(self):
        source = OWNER_PATH.read_text(encoding="utf-8")
        definitions = _definitions(source)
        actual = {}
        for name in RANGE_NAMES:
            node = definitions[name]
            actual[name] = (
                hashlib.sha256(
                    ast.get_source_segment(source, node).encode("utf-8")
                ).hexdigest(),
                hashlib.sha256(
                    ast.dump(
                        node,
                        annotate_fields=True,
                        include_attributes=False,
                    ).encode("utf-8")
                ).hexdigest(),
            )
        self.assertEqual(actual, EXPECTED_HASHES)

    def test_unique_owner_and_compatibility_identity(self):
        implementations = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in RANGE_NAMES:
                    implementations.append((_module_name(path), node.name))

        self.assertEqual(
            set(implementations),
            {(OWNER_MODULE, name) for name in RANGE_NAMES},
        )
        for name in RANGE_NAMES:
            with self.subTest(name=name):
                owner = getattr(context_ranges, name)
                self.assertNotIn(name, boundaries.FACADE_EXPORTS)
                self.assertIs(getattr(boundaries, name), owner)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)

    def test_facade_exports_and_dependencies_are_exact(self):
        source = OWNER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "FACADE_EXPORTS"
                for target in node.targets
            )
        ]
        expected_exports = {name: name for name in RANGE_NAMES}
        self.assertEqual(len(assignments), 1)
        self.assertEqual(ast.literal_eval(assignments[0].value), expected_exports)
        self.assertEqual(context_ranges.FACADE_EXPORTS, expected_exports)

        direct_imports = _direct_imports(tree)
        self.assertEqual(
            {name for name in direct_imports if name.startswith("autoslice.")},
            OWNER_DEPENDENCIES,
        )
        self.assertEqual(
            direct_imports - OWNER_DEPENDENCIES,
            {"__future__.annotations", "math"},
        )

        importers = set()
        for path in SRC_ROOT.rglob("*.py"):
            candidate = ast.parse(path.read_text(encoding="utf-8"))
            if OWNER_MODULE in _direct_imports(candidate):
                importers.add(_module_name(path))
        self.assertEqual(importers, PRODUCTION_IMPORTERS)

    def test_boundaries_calls_owner_directly_and_keeps_only_orchestration(self):
        tree = ast.parse(BOUNDARIES_PATH.read_text(encoding="utf-8"))
        local_calls = [
            (node.lineno, node.func.id)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in RANGE_NAMES
        ]
        owner_calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "context_ranges"
            and node.func.attr in RANGE_NAMES
        }
        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(local_calls, [])
        self.assertEqual(owner_calls, set(RANGE_NAMES))
        self.assertEqual(
            functions,
            {
                "_expand_clip_mark_with_context",
                "_expand_clip_marks_with_context",
            },
        )

    def test_review_package_is_lazy_and_complete(self):
        init_path = OWNER_PATH.parent / "__init__.py"
        source = init_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertEqual(
            [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ],
            [],
        )
        expected = sorted(
            path.stem
            for path in OWNER_PATH.parent.glob("*.py")
            if path.name != "__init__.py"
        )
        self.assertEqual(review_package.__all__, expected)

    def test_context_ranges_does_not_mutate_inputs(self):
        mark = {
            "title": "低血糖眩晕",
            "publish_title": "身体突然受不了",
            "boundary_evidence": ["低血糖", "眩晕"],
            "reference_start": 30,
        }
        segments = [
            (25.0, 29.0, "低血糖眩晕"),
            (35.0, 39.0, "低血糖眩晕"),
            (60.0, 64.0, "下一个话题"),
        ]
        original_mark = copy.deepcopy(mark)
        original_segments = copy.deepcopy(segments)

        self.assertEqual(
            context_ranges._find_relevant_topic_context_start(
                mark, 30, 35, segments
            ),
            (25, 34),
        )
        self.assertEqual(
            context_ranges._find_relevant_topic_context_end(
                mark, 35, 100, segments
            ),
            (39, 60, None),
        )
        self.assertEqual(mark, original_mark)
        self.assertEqual(segments, original_segments)

    def test_empty_or_invalid_ranges_fail_safe(self):
        mark = {"title": "", "publish_title": "", "boundary_evidence": []}
        self.assertEqual(
            context_ranges._find_relevant_topic_context_start(mark, 10, 20, []),
            (None, 0),
        )
        self.assertEqual(
            context_ranges._find_relevant_topic_context_end(mark, 20, 10, []),
            (20, None, None),
        )


if __name__ == "__main__":
    unittest.main()
