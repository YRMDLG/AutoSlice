import ast
import copy
import hashlib
import unittest
from pathlib import Path

from autoslice import topic_engine
from autoslice.analysis import boundaries, candidates
from autoslice.analysis import review as review_package
from autoslice.analysis.review import context_edges
from autoslice.transcription import segments as transcription_segments
from scripts import architecture_snapshot

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.context_edges"
OWNER_PATH = SRC_ROOT / "autoslice/analysis/review/context_edges.py"
BOUNDARIES_PATH = SRC_ROOT / "autoslice/analysis/boundaries.py"
CONTEXT_EDGE_NAMES = (
    "_TOPIC_LEAD_IN_TRIGGER_RE",
    "_VISUAL_REVIEW_TOPIC_RE",
    "_VISUAL_REACTION_LEAD_IN_RE",
    "_find_topic_lead_in_start",
    "_find_visual_reaction_context_start",
    "_find_next_topic_hard_end",
)
CONSTANT_NAMES = CONTEXT_EDGE_NAMES[:3]
FUNCTION_NAMES = CONTEXT_EDGE_NAMES[3:]
EXPECTED_HASHES = {
    "_TOPIC_LEAD_IN_TRIGGER_RE": (
        "7f180ace34f5e8209ca0c398c8bd0c5c170211ce40f9953873b57023b04b8151",
        "78011cd20438480bc18f60a2fbabbcade73ec516072dce2f135fbac763a5fbe9",
    ),
    "_VISUAL_REVIEW_TOPIC_RE": (
        "51521015a27f1c4a08cda4f22b422f6ad137b411028b974f0a9de940d79726fe",
        "9da3951830db09e5cd84c22aa0b6e520e8f43f7144497fbb337d3a06dda9cc95",
    ),
    "_VISUAL_REACTION_LEAD_IN_RE": (
        "6ce487ea6184c602becee5d8a1e4cf306dfa5a5e1607360469b816721ab087e3",
        "794a212146fe7f3313003e389895f2a279b3de970d93c63cf7e276612605093b",
    ),
    "_find_topic_lead_in_start": (
        "ea6829ac50c7c37be4a6648329fffe500da84cf90e18e22e81b3b9499f8a1b73",
        "c4aeed3527cdec8aff60a36c12cdb82c8b9d13a8262ab2d3e00130c104137393",
    ),
    "_find_visual_reaction_context_start": (
        "c885ec60ff61db4082190b442cfec1b4180d51d423cf71f3c841ac7f8ac53d96",
        "ccd9cd61c4cbd3e324c3b96b0a646c75e7aee79e87be39f1b73a9d8ea997b078",
    ),
    "_find_next_topic_hard_end": (
        "99408758fe25a5af44d38ec314d64f22ff0bd9b7518d79ffdd5341973020bead",
        "4110c21be68e4377331323593ed2d67237f4ffb3883a102cbe4c0bcff801b604",
    ),
}
OWNER_DEPENDENCIES = {
    "autoslice.analysis.review.finalization",
    "autoslice.analysis.review.policy",
    "autoslice.analysis.review.triggers",
}
PRODUCTION_IMPORTERS = {
    "autoslice.analysis.boundaries",
    "autoslice.analysis.candidates",
    "autoslice.topic_engine",
}
RETAINED_BOUNDARY_FUNCTIONS = {
    "_expand_clip_mark_with_context",
    "_expand_clip_marks_with_context",
}
EXPECTED_REVIEW_ALL = [
    "candidates",
    "context_edges",
    "context_evidence",
    "context_ranges",
    "decisions",
    "deduplication",
    "finalization",
    "outro",
    "policy",
    "prompt",
    "reconciliation",
    "scoring",
    "transitions",
    "triggers",
    "workflow",
]


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


def _definition_nodes(source):
    definitions = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_NAMES:
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in CONSTANT_NAMES:
                    definitions[target.id] = node
    return definitions


class ReviewContextEdgeOwnershipTests(unittest.TestCase):
    def test_migrated_source_and_ast_hashes_are_exact(self):
        source = OWNER_PATH.read_text(encoding="utf-8")
        definitions = _definition_nodes(source)
        actual = {
            name: (
                hashlib.sha256(
                    ast.get_source_segment(source, definitions[name]).encode("utf-8")
                ).hexdigest(),
                hashlib.sha256(
                    ast.dump(
                        definitions[name],
                        annotate_fields=True,
                        include_attributes=False,
                    ).encode("utf-8")
                ).hexdigest(),
            )
            for name in CONTEXT_EDGE_NAMES
        }
        self.assertEqual(actual, EXPECTED_HASHES)

    def test_context_edges_is_unique_owner_and_facades_keep_identity(self):
        implementations = []
        compiled_constants = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_NAMES:
                    implementations.append((_module_name(path), node.name))
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in CONSTANT_NAMES:
                            compiled_constants.append((_module_name(path), target.id))

        self.assertEqual(
            set(implementations),
            {(OWNER_MODULE, name) for name in FUNCTION_NAMES},
        )
        self.assertEqual(
            set(compiled_constants),
            {(OWNER_MODULE, name) for name in CONSTANT_NAMES},
        )
        for name in CONTEXT_EDGE_NAMES:
            owner = getattr(context_edges, name)
            with self.subTest(name=name):
                self.assertNotIn(name, boundaries.FACADE_EXPORTS)
                self.assertIs(getattr(boundaries, name), owner)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)

    def test_facade_exports_are_static_and_exact(self):
        tree = ast.parse(OWNER_PATH.read_text(encoding="utf-8"))
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "FACADE_EXPORTS"
                for target in node.targets
            )
        ]
        expected = {name: name for name in CONTEXT_EDGE_NAMES}

        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.Dict)
        self.assertEqual(ast.literal_eval(assignments[0].value), expected)
        self.assertEqual(context_edges.FACADE_EXPORTS, expected)

    def test_owner_dependencies_and_production_importers_are_exact(self):
        owner_tree = ast.parse(OWNER_PATH.read_text(encoding="utf-8"))
        direct_imports = _direct_imports(owner_tree)
        self.assertEqual(
            {name for name in direct_imports if name.startswith("autoslice.")},
            OWNER_DEPENDENCIES,
        )
        self.assertEqual(direct_imports - OWNER_DEPENDENCIES, {"math", "re"})

        importers = set()
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if OWNER_MODULE in _direct_imports(tree):
                importers.add(_module_name(path))
        self.assertEqual(importers, PRODUCTION_IMPORTERS)

    def test_consumers_bind_owner_directly_and_boundaries_calls_owner(self):
        for relative_path in (
            "autoslice/analysis/candidates.py",
            "autoslice/topic_engine.py",
        ):
            tree = ast.parse((SRC_ROOT / relative_path).read_text(encoding="utf-8"))
            stale = [
                (node.attr, node.lineno)
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "boundary_analysis"
                and node.attr in CONTEXT_EDGE_NAMES
            ]
            self.assertEqual(stale, [], relative_path)

        tree = ast.parse(BOUNDARIES_PATH.read_text(encoding="utf-8"))
        local_calls = []
        owner_calls = set()
        for function in (
            node for node in tree.body if isinstance(node, ast.FunctionDef)
        ):
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id in FUNCTION_NAMES:
                    local_calls.append((function.name, node.func.id, node.lineno))
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "context_edges"
                    and node.func.attr in FUNCTION_NAMES
                ):
                    owner_calls.add(node.func.attr)

        self.assertEqual(local_calls, [])
        self.assertEqual(owner_calls, set(FUNCTION_NAMES))

    def test_boundaries_keeps_only_required_orchestration_and_compatibility(self):
        tree = ast.parse(BOUNDARIES_PATH.read_text(encoding="utf-8"))
        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }

        self.assertEqual(functions, RETAINED_BOUNDARY_FUNCTIONS)
        self.assertIs(boundaries.transcription_segments, transcription_segments)
        self.assertIs(
            boundaries.TOPIC_CONTEXT_GAP,
            transcription_segments.TOPIC_CONTEXT_GAP,
        )

    def test_review_package_is_lazy_with_one_static_complete_all(self):
        init_path = OWNER_PATH.parent / "__init__.py"
        source = init_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        ]

        self.assertEqual(imports, [])
        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.List)
        self.assertEqual(ast.literal_eval(assignments[0].value), EXPECTED_REVIEW_ALL)
        self.assertEqual(review_package.__all__, EXPECTED_REVIEW_ALL)

    def test_architecture_has_no_cycles_duplicates_or_patch_growth(self):
        current = architecture_snapshot.build_snapshot(ROOT)

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)


class ReviewContextEdgeBehaviorTests(unittest.TestCase):
    def test_explicit_lead_in_and_contiguous_cluster_are_recovered(self):
        segments = [
            (30.0, 34.0, "较早的无关内容"),
            (65.5, 68.0, "对了"),
            (78.0, 82.0, "你们猜今天发的是什么"),
            (100.0, 110.0, "话题核心"),
        ]

        self.assertEqual(
            context_edges._find_topic_lead_in_start(20, 100, segments),
            65.5,
        )

    def test_lead_in_uses_nearest_cluster_but_short_topic_does_not_rewind(self):
        segments = [
            (35.0, 38.0, "说到这个"),
            (70.0, 73.0, "对了"),
            (84.0, 87.0, "你们猜是谁发的"),
        ]

        self.assertEqual(
            context_edges._find_topic_lead_in_start(20, 100, segments),
            70.0,
        )
        self.assertIsNone(
            context_edges._find_topic_lead_in_start(75, 100, segments)
        )

    def test_visual_topic_keeps_first_reaction_but_non_visual_topic_does_not(self):
        segments = [
            (61.8, 64.0, "这到底是在干什么"),
            (78.0, 80.0, "放大看一下图片"),
            (100.0, 110.0, "开始评价商品"),
        ]
        visual_mark = {
            "title": "查看商品差评图片",
            "boundary_evidence": ["画面里的手套很奇怪"],
        }
        non_visual_mark = {
            "title": "讨论睡眠安排",
            "boundary_evidence": ["昨晚只睡两个小时"],
        }

        self.assertEqual(
            context_edges._find_visual_reaction_context_start(
                visual_mark,
                100,
                segments,
            ),
            61,
        )
        self.assertIsNone(
            context_edges._find_visual_reaction_context_start(
                non_visual_mark,
                100,
                segments,
            )
        )

    def test_regular_topic_and_explicit_sc_start_hard_end(self):
        cases = (
            [(130.0, 134.0, "对了说下一件事")],
            [(130.0, 134.0, "谢谢这个 SC")],
        )
        for segments in cases:
            with self.subTest(segments=segments):
                self.assertEqual(
                    context_edges._find_next_topic_hard_end(
                        100,
                        100,
                        160,
                        segments,
                    ),
                    130,
                )

    def test_gift_with_question_starts_hard_end_but_ordinary_thanks_does_not(self):
        gift_question = [
            (130.0, 134.0, "谢谢老板送的舰长"),
            (135.0, 140.0, "他说为什么会这样吗"),
        ]
        ordinary_thanks = [(130.0, 134.0, "谢谢大家今天来看直播")]

        self.assertEqual(
            context_edges._find_next_topic_hard_end(
                100,
                100,
                160,
                gift_question,
            ),
            130,
        )
        self.assertIsNone(
            context_edges._find_next_topic_hard_end(
                100,
                100,
                160,
                ordinary_thanks,
            )
        )

    def test_stop_at_gift_trigger_overrides_reference_gap(self):
        segments = [(130.0, 134.0, "谢谢老板送的舰长")]

        self.assertIsNone(
            context_edges._find_next_topic_hard_end(
                100,
                120,
                160,
                segments,
            )
        )
        self.assertEqual(
            context_edges._find_next_topic_hard_end(
                100,
                120,
                160,
                segments,
                stop_at_gift_trigger=True,
            ),
            130,
        )

    def test_hard_end_moves_to_safe_subtitle_boundary(self):
        segments = [
            (129.0, 130.4, "上一句还没有说完"),
            (130.8, 134.0, "对了说下一件事"),
        ]

        boundary = context_edges._find_next_topic_hard_end(
            120,
            120,
            160,
            segments,
        )

        self.assertEqual(boundary, 129)
        self.assertFalse(any(start < boundary < end for start, end, _ in segments))

    def test_inputs_are_not_mutated(self):
        mark = {
            "title": "查看商品差评图片",
            "publish_title": "第一眼看见奇怪手套",
            "boundary_evidence": ["画面里的手套很奇怪"],
        }
        segments = [
            (61.8, 64.0, "这到底是在干什么"),
            (80.0, 84.0, "对了你们猜"),
            (100.0, 110.0, "开始评价商品"),
            (130.0, 134.0, "谢谢这个 SC"),
        ]
        expected_mark = copy.deepcopy(mark)
        expected_segments = copy.deepcopy(segments)

        context_edges._find_topic_lead_in_start(20, 100, segments)
        context_edges._find_visual_reaction_context_start(mark, 100, segments)
        context_edges._find_next_topic_hard_end(110, 110, 150, segments)

        self.assertEqual(mark, expected_mark)
        self.assertEqual(segments, expected_segments)


if __name__ == "__main__":
    unittest.main()
