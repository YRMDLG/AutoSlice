import ast
import copy
import hashlib
import unittest
from pathlib import Path

from autoslice import topic_engine
from autoslice.analysis import boundaries, candidates
from autoslice.analysis import review as review_package
from autoslice.analysis.review import transitions
from autoslice.transcription import segments as transcription_segments
from scripts import architecture_snapshot

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.transitions"
OWNER_PATH = SRC_ROOT / "autoslice/analysis/review/transitions.py"
BOUNDARIES_PATH = SRC_ROOT / "autoslice/analysis/boundaries.py"
TRANSITION_NAMES = (
    "_NEXT_CASE_ASR_TRIGGER_RE",
    "_TOPIC_DECISION_EVIDENCE_RE",
    "_TOPIC_CONCLUSION_RE",
    "_TOPIC_REFUND_RE",
    "_TOPIC_DISCOURSE_CONTINUATION_RE",
    "_VISUAL_CASE_SHIFT_RE",
    "_looks_like_next_case_transition",
    "_looks_like_delayed_topic_conclusion",
    "_looks_like_discourse_continuation",
    "_looks_like_low_score_visual_case_shift",
    "_next_report_topic_safe_boundary",
)
CONSTANT_NAMES = TRANSITION_NAMES[:6]
FUNCTION_NAMES = TRANSITION_NAMES[6:]
EXPECTED_SOURCE_HASHES = {
    "_NEXT_CASE_ASR_TRIGGER_RE": (
        "04709c4e087e129f54209b9259cdfc873bfd390b0f70d67886b73bbb9feea352"
    ),
    "_TOPIC_DECISION_EVIDENCE_RE": (
        "dfd6d7ebdfd1920a9cf7af6729cede6b3b0e1c9f24a62818364a2427196a69f3"
    ),
    "_TOPIC_CONCLUSION_RE": (
        "6bd04327fe6ac1be46d6d55b2f8d3850d4c721e146862493ac021d68a74bab78"
    ),
    "_TOPIC_REFUND_RE": (
        "4177a1797620678fd5df9d6243dac3b3efb313458e67364afc3efa523b859d20"
    ),
    "_TOPIC_DISCOURSE_CONTINUATION_RE": (
        "cc973ebef52a0c5a2299fcc9f3705ff3dd87fc82c5ad5fca91a34a4845c65dbb"
    ),
    "_VISUAL_CASE_SHIFT_RE": (
        "1685f9e73ceeff44edc1c3791635efada6dea9bb62e1422ecaa50788eda6dc5e"
    ),
    "_looks_like_next_case_transition": (
        "a281426febb635e8d4fa16e20a3d1417ca80eb44ab7c1c8a01d3f5bec26dc8e7"
    ),
    "_looks_like_delayed_topic_conclusion": (
        "a93ee8875fd67fc91c18a5048ced53329ec59dea647829f59284fd252678b4a1"
    ),
    "_looks_like_discourse_continuation": (
        "ffc7e4f544c40b29a6f00041136c246e0663a8342a47ded7b72aeb8fdc651c8e"
    ),
    "_looks_like_low_score_visual_case_shift": (
        "c3a153199c42cb26eee80ea57003ebbe67119afaeb5597761a6ae9a452c8f53b"
    ),
    "_next_report_topic_safe_boundary": (
        "079bfa1da32e469278dcd5ccefd0e480638db272764d7b14898396abd14d388d"
    ),
}
EXPECTED_AST_HASHES = {
    "_NEXT_CASE_ASR_TRIGGER_RE": (
        "ad8a0250a50ea8d529e9c7942e85249a551d6b024956a569c7957b13d2cf068e"
    ),
    "_TOPIC_DECISION_EVIDENCE_RE": (
        "ad89fbdd05bf7c0313b4a762a0227cf46c8135e32b15c3ee61215a8c924708e0"
    ),
    "_TOPIC_CONCLUSION_RE": (
        "19829737424906bc787026638132bb07184de2d57eea04c2ec199b048e277068"
    ),
    "_TOPIC_REFUND_RE": (
        "80a273f289a5cbf3f4d490e2a002aaa6b3283cdaac0609ebd83451f3128863cd"
    ),
    "_TOPIC_DISCOURSE_CONTINUATION_RE": (
        "9f0243638a6664d72d2c00406c3e0344042eca7bd372f8b95495b95be49865fa"
    ),
    "_VISUAL_CASE_SHIFT_RE": (
        "f16495180e9adcdeaf77e6f33d7d38c3a5791915b5f6f31934cb7ee7bcdef148"
    ),
    "_looks_like_next_case_transition": (
        "64752c01455914b4420aa83ededb0efdb4eac27bec88bf6bd1a66d8780f69ac2"
    ),
    "_looks_like_delayed_topic_conclusion": (
        "7f7efa01002090136cf703137fe9a2981c0ec5322ed7aaa93c24eabcf98a1e08"
    ),
    "_looks_like_discourse_continuation": (
        "54500a2dfafb78512cf5a0cb21e2f8a36abb732acdcb2342ef608288cea11c5c"
    ),
    "_looks_like_low_score_visual_case_shift": (
        "7bf894d097ee664b0a2fd19a585bbdb7c802d9823eb71db44791f4a2b8d465aa"
    ),
    "_next_report_topic_safe_boundary": (
        "b76027c82bf61f29d3696fe170bb490da8024288eb1ddc25e951b7a5ef651e22"
    ),
}
OWNER_DEPENDENCIES = {
    "autoslice.analysis.review.context_evidence",
    "autoslice.analysis.review.policy",
}
PRODUCTION_IMPORTERS = {
    "autoslice.analysis.boundaries",
    "autoslice.analysis.candidates",
    "autoslice.analysis.review.context_ranges",
    "autoslice.topic_engine",
}
RETAINED_BOUNDARY_FUNCTIONS = {
    "_expand_clip_mark_with_context",
    "_expand_clip_marks_with_context",
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


def _ast_hash(node):
    dumped = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


class ReviewTransitionOwnershipTests(unittest.TestCase):
    def test_migrated_source_and_ast_hashes_are_exact(self):
        source = OWNER_PATH.read_text(encoding="utf-8")
        definitions = _definition_nodes(source)
        source_hashes = {
            name: hashlib.sha256(
                ast.get_source_segment(source, definitions[name]).encode("utf-8")
            ).hexdigest()
            for name in TRANSITION_NAMES
        }
        ast_hashes = {
            name: _ast_hash(definitions[name])
            for name in TRANSITION_NAMES
        }

        self.assertEqual(source_hashes, EXPECTED_SOURCE_HASHES)
        self.assertEqual(ast_hashes, EXPECTED_AST_HASHES)

    def test_transitions_is_unique_owner_and_facades_keep_identity(self):
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
        for name in TRANSITION_NAMES:
            owner = getattr(transitions, name)
            with self.subTest(name=name):
                self.assertNotIn(name, boundaries.FACADE_EXPORTS)
                self.assertIs(getattr(boundaries, name), owner)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)

    def test_facade_exports_are_static_and_exact(self):
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
        expected = {name: name for name in TRANSITION_NAMES}

        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.Dict)
        self.assertEqual(ast.literal_eval(assignments[0].value), expected)
        self.assertEqual(transitions.FACADE_EXPORTS, expected)

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
                and node.attr in TRANSITION_NAMES
            ]
            self.assertEqual(stale, [], relative_path)

        trees = [
            ast.parse(BOUNDARIES_PATH.read_text(encoding="utf-8")),
            ast.parse(
                (SRC_ROOT / "autoslice/analysis/review/context_ranges.py").read_text(
                    encoding="utf-8"
                )
            ),
        ]
        local_calls = []
        owner_calls = set()
        for tree in trees:
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
                        and node.func.value.id == "transition_analysis"
                        and node.func.attr in FUNCTION_NAMES
                    ):
                        owner_calls.add(node.func.attr)

        self.assertEqual(local_calls, [])
        self.assertEqual(owner_calls, set(FUNCTION_NAMES))

    def test_boundaries_keeps_orchestration_and_compatibility_entries(self):
        tree = ast.parse(BOUNDARIES_PATH.read_text(encoding="utf-8"))
        functions = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertTrue(RETAINED_BOUNDARY_FUNCTIONS <= functions)
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
        expected = sorted(
            path.stem
            for path in OWNER_PATH.parent.glob("*.py")
            if path.name != "__init__.py"
        )

        self.assertEqual(imports, [])
        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.List)
        self.assertEqual(ast.literal_eval(assignments[0].value), expected)
        self.assertEqual(review_package.__all__, expected)

    def test_architecture_has_no_cycles_duplicates_or_patch_growth(self):
        current = architecture_snapshot.build_snapshot(ROOT)

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)


class ReviewTransitionBehaviorTests(unittest.TestCase):
    def test_next_case_regular_and_asr_variants_are_recognised(self):
        for text in (
            "下一个案例",
            "接着看下个商品",
            "再能下一个",
            "看看商家写了什么",
        ):
            with self.subTest(text=text):
                self.assertTrue(transitions._looks_like_next_case_transition(text))
        self.assertFalse(transitions._looks_like_next_case_transition("继续说当前退款"))

    def test_delayed_refund_and_decision_conclusions_are_recognised(self):
        refund_mark = {
            "title": "商家退款争议",
            "publish_title": "客服最后会不会退钱",
            "boundary_evidence": ["顾客要求退款"],
        }
        decision_mark = {
            "title": "判断商品是否能展示",
            "boundary_evidence": ["最后怎么处理"],
        }

        self.assertTrue(transitions._looks_like_delayed_topic_conclusion(
            refund_mark,
            "所以把钱退回去",
            {},
        ))
        self.assertTrue(transitions._looks_like_delayed_topic_conclusion(
            decision_mark,
            "我觉得这样不可以展示",
            {},
        ))
        self.assertFalse(transitions._looks_like_delayed_topic_conclusion(
            decision_mark,
            "接下来看看另一个商品",
            {},
        ))

    def test_discourse_continuation_and_visual_case_shift_are_recognised(self):
        self.assertTrue(transitions._looks_like_discourse_continuation(
            "主要是这个问题还没有解决"
        ))
        self.assertTrue(transitions._looks_like_discourse_continuation(
            "这个商家还要再补充一句"
        ))
        self.assertFalse(transitions._looks_like_discourse_continuation(
            "下一个案例"
        ))
        self.assertTrue(transitions._looks_like_low_score_visual_case_shift(
            "左边是赠品右边是原厂遥控器",
            {"退款": 1},
        ))
        self.assertFalse(transitions._looks_like_low_score_visual_case_shift(
            "还是讨论刚才的退款",
            {"退款": 1},
        ))

    def test_next_report_topic_inside_subtitle_keeps_sentence_end(self):
        segments = [
            (90.0, 99.5, "上一句"),
            (105.25, 114.2, "跨越报告话题起点的一整句"),
            (115.0, 120.0, "下一句"),
        ]

        self.assertEqual(
            transitions._next_report_topic_safe_boundary(110, 100, segments),
            (115, 105.25),
        )
        self.assertEqual(
            transitions._next_report_topic_safe_boundary(130, 100, segments),
            (130, None),
        )

    def test_mark_term_counts_and_segments_are_not_mutated(self):
        mark = {
            "title": "退款判断",
            "publish_title": "最后决定退钱",
            "boundary_evidence": ["顾客申请退款"],
        }
        term_counts = {"退款": 2, "商品": 1}
        segments = [(100.0, 112.5, "所以最后决定退款")]
        expected_mark = copy.deepcopy(mark)
        expected_term_counts = copy.deepcopy(term_counts)
        expected_segments = copy.deepcopy(segments)

        transitions._looks_like_delayed_topic_conclusion(
            mark,
            "所以最后决定退款",
            term_counts,
        )
        transitions._looks_like_low_score_visual_case_shift(
            "左边是赠品右边是商品",
            term_counts,
        )
        transitions._next_report_topic_safe_boundary(110, 100, segments)

        self.assertEqual(mark, expected_mark)
        self.assertEqual(term_counts, expected_term_counts)
        self.assertEqual(segments, expected_segments)


if __name__ == "__main__":
    unittest.main()
