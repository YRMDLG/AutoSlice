import ast
import copy
import hashlib
import unittest
from pathlib import Path

from autoslice import topic_engine
from autoslice.analysis import boundaries, candidates
from autoslice.analysis import review as review_package
from autoslice.analysis.review import context_evidence
from autoslice.streamer_profiles import streamer_profile_context

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.context_evidence"
OWNER_PATH = SRC_ROOT / "autoslice/analysis/review/context_evidence.py"
BOUNDARIES_PATH = SRC_ROOT / "autoslice/analysis/boundaries.py"
CONTEXT_EVIDENCE_NAMES = (
    "_BOUNDARY_SEMANTIC_SIGNAL_PATTERNS",
    "_BOUNDARY_EVIDENCE_STOP_TERMS",
    "_normalise_boundary_evidence_text",
    "_boundary_evidence_term_counts",
    "_score_boundary_evidence_text",
    "_boundary_evidence_text_is_relevant",
    "_boundary_semantic_signals",
    "_boundary_text_has_semantic_signal",
    "_subtitle_speech_chains",
    "_split_chain_crossing_topic_end",
    "_boundary_context_has_speech",
    "_boundary_context_is_relevant",
)
FUNCTION_NAMES = CONTEXT_EVIDENCE_NAMES[2:]
EXPECTED_HASHES = {
    "_BOUNDARY_SEMANTIC_SIGNAL_PATTERNS": (
        "9f4637b1a23549bb53fffb05c779427dfbbe306ac5ac402188ef438137d2e920",
        "f209081ba033e4e0791974a61c7654c7d110d29d81b9d469241c0571123ef869",
    ),
    "_BOUNDARY_EVIDENCE_STOP_TERMS": (
        "39ede367b72a3b8a6dfe350dcc7eec9cdd12076c808d125f40ab7a2d0d3a4e23",
        "5ff7cf4015b061ca034433498a2bf0ba6c8a442a5f1047070425fe0bd9435824",
    ),
    "_normalise_boundary_evidence_text": (
        "70a8f815b077e9337c452ee0132a3451bf665c58e9d559932094ef67a98c4ea6",
        "f3c1998d2f13bdaa109490d96c3f5ab0c8c79f2e1f2c04675eedf965a2faa432",
    ),
    "_boundary_evidence_term_counts": (
        "d71bd28d3363c7992c50f8128bb6485f6d9236282cd6e75cda3c1a92af12f6dc",
        "a562c1ef06675f04d59917067e0705914d1a70ff81c75718b5c1500655a9c17d",
    ),
    "_score_boundary_evidence_text": (
        "487cc438f3d2d3a0c638d1369f6780883f726b9581e1747490c5713d0d42308b",
        "9798ebfb62c4b77cc647b572127351ad71963e27c61aac1d625db9fd4b1214ba",
    ),
    "_boundary_evidence_text_is_relevant": (
        "3f1da625f3de9e5cf546554a8fdeeca9fa4fc6f3e3a929972a85d20cd1bfaf90",
        "ab6ae356b3aa3cf1aaa0159addf9fc6996a370dfb90b4b1a7c5adf5475b65300",
    ),
    "_boundary_semantic_signals": (
        "9d08847dd2ddfccdc22ad0f631d53b5e557027d602a3622943fd5d2e5d4efab9",
        "1b41e99f1e63118aca1de2659a765b1850b8280e394e0171ab3f2ce0d331e54c",
    ),
    "_boundary_text_has_semantic_signal": (
        "86b9ca1718b46ed10bba6e8346b7b3b4a1f5e84fa95d568dd28e46cf22c7659a",
        "2652ddb37bf9af8388af192dcf5c18bcc1cc9b6b630836eb945d439d4bda871c",
    ),
    "_subtitle_speech_chains": (
        "e9fb854a87b12b6ec2517dcf5b36f36dc153913c8e5909e1f9fc0361fa63df23",
        "50560934eeddae714f6d6e2490f97fa6f724ba3471e7725755251765b8efc72f",
    ),
    "_split_chain_crossing_topic_end": (
        "d370e3a6c848b77e645bf142c92976c5467fd33e7b62a754efc06488e005bd85",
        "d8706ed73ee00439eb327d21148211c8577abb9f3db2bab5a59fbdf736c727a9",
    ),
    "_boundary_context_has_speech": (
        "7fa0d17a5117b8bdbd133695c924a725ce04c78ba1808202aabd6194c584a2f2",
        "b0bb78417ad8854cea382582859cffa28ff5a668eba0e96fb94b1d8fed0a1b27",
    ),
    "_boundary_context_is_relevant": (
        "e7325123c9eecfb808466f3bd7446b7e3b95b9577ee6c2e815de40081b0df3c3",
        "0313a8c5b72f57d785db738cecf147cb95ade25046f93cfe08ea4b6f5d782e8f",
    ),
}
OWNER_DEPENDENCIES = {
    "autoslice.analysis.review.policy",
    "autoslice.streamer_profiles.current_streamer_profile",
    "autoslice.transcription.segments",
    "autoslice.transcription.service",
}
PRODUCTION_IMPORTERS = {
    "autoslice.analysis.boundaries",
    "autoslice.analysis.candidates",
    "autoslice.analysis.review.context_ranges",
    "autoslice.analysis.review.transitions",
    "autoslice.topic_engine",
}
BOUNDARY_OWNER_CALLS = {
    "_boundary_context_has_speech",
    "_boundary_context_is_relevant",
    "_boundary_evidence_term_counts",
    "_boundary_evidence_text_is_relevant",
    "_boundary_semantic_signals",
    "_boundary_text_has_semantic_signal",
    "_score_boundary_evidence_text",
    "_split_chain_crossing_topic_end",
    "_subtitle_speech_chains",
}
BOUNDARY_ORCHESTRATION_FUNCTIONS = {
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
                if isinstance(target, ast.Name) and target.id in CONTEXT_EVIDENCE_NAMES:
                    definitions[target.id] = node
    return definitions


class ReviewContextEvidenceOwnershipTests(unittest.TestCase):
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
            for name in CONTEXT_EVIDENCE_NAMES
        }
        self.assertEqual(actual, EXPECTED_HASHES)

    def test_context_evidence_is_unique_owner_and_facades_keep_identity(self):
        implementations = []
        literal_constants = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_NAMES:
                    implementations.append((_module_name(path), node.name))
                if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Dict, ast.Set)):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and target.id in CONTEXT_EVIDENCE_NAMES[:2]
                        ):
                            literal_constants.append((_module_name(path), target.id))

        self.assertEqual(
            set(implementations),
            {(OWNER_MODULE, name) for name in FUNCTION_NAMES},
        )
        self.assertEqual(
            set(literal_constants),
            {(OWNER_MODULE, name) for name in CONTEXT_EVIDENCE_NAMES[:2]},
        )
        for name in CONTEXT_EVIDENCE_NAMES:
            owner = getattr(context_evidence, name)
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
        expected = {name: name for name in CONTEXT_EVIDENCE_NAMES}
        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.Dict)
        self.assertEqual(ast.literal_eval(assignments[0].value), expected)
        self.assertEqual(context_evidence.FACADE_EXPORTS, expected)

    def test_owner_dependencies_and_production_importers_are_exact(self):
        owner_tree = ast.parse(OWNER_PATH.read_text(encoding="utf-8"))
        direct_imports = _direct_imports(owner_tree)
        self.assertEqual(
            {name for name in direct_imports if name.startswith("autoslice.")},
            OWNER_DEPENDENCIES,
        )
        self.assertEqual(
            direct_imports - OWNER_DEPENDENCIES,
            {"__future__.annotations", "collections.defaultdict", "re"},
        )

        importers = set()
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if OWNER_MODULE in _direct_imports(tree):
                importers.add(_module_name(path))
        self.assertEqual(importers, PRODUCTION_IMPORTERS)

    def test_boundaries_calls_owner_directly_and_keeps_orchestration(self):
        trees = [
            ast.parse(BOUNDARIES_PATH.read_text(encoding="utf-8")),
            ast.parse(
                (SRC_ROOT / "autoslice/analysis/review/context_ranges.py").read_text(
                    encoding="utf-8"
                )
            ),
        ]
        boundary_functions = {
            node.name
            for node in trees[0].body
            if isinstance(node, ast.FunctionDef)
        }
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
                        and node.func.value.id == "context_evidence"
                        and node.func.attr in FUNCTION_NAMES
                    ):
                        owner_calls.add(node.func.attr)

        self.assertEqual(local_calls, [])
        self.assertEqual(owner_calls, BOUNDARY_OWNER_CALLS)
        self.assertTrue(BOUNDARY_ORCHESTRATION_FUNCTIONS <= boundary_functions)

    def test_review_package_remains_lazy_with_static_complete_all(self):
        source = (OWNER_PATH.parent / "__init__.py").read_text(encoding="utf-8")
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
        expected = [
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
        self.assertEqual(imports, [])
        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, (ast.List, ast.Tuple))
        self.assertEqual(ast.literal_eval(assignments[0].value), expected)
        self.assertEqual(review_package.__all__, expected)


class ReviewContextEvidenceBehaviorTests(unittest.TestCase):
    def test_stop_terms_and_current_streamer_identity_terms_are_filtered(self):
        mark = {
            "title": "",
            "publish_title": "",
            "boundary_evidence": ["主播", "观众", "默认", "音音", "低血糖"],
        }
        with streamer_profile_context("zeyin"):
            counts = context_evidence._boundary_evidence_term_counts(mark)

        self.assertNotIn("主播", counts)
        self.assertNotIn("观众", counts)
        self.assertNotIn("默认", counts)
        self.assertNotIn("音音", counts)
        self.assertGreater(counts["低血糖"], 0)

    def test_keyword_length_weights_threshold_and_repeated_short_terms(self):
        weighted_terms = {
            "甲乙": 1,
            "丙丁戊": 1,
            "己庚辛壬": 1,
            "癸子丑寅卯": 1,
        }
        text = "甲乙丙丁戊己庚辛壬癸子丑寅卯"
        self.assertEqual(
            context_evidence._score_boundary_evidence_text(text, weighted_terms),
            16,
        )
        threshold_terms = {"身体受不了": 1, "低血糖": 1}
        self.assertEqual(
            context_evidence._score_boundary_evidence_text(
                "身体受不了，可能是低血糖",
                threshold_terms,
            ),
            context_evidence.TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE,
        )
        self.assertTrue(context_evidence._boundary_evidence_text_is_relevant(
            "身体受不了，可能是低血糖",
            threshold_terms,
        ))
        self.assertFalse(context_evidence._boundary_evidence_text_is_relevant(
            "只是有点低血糖",
            threshold_terms,
        ))
        self.assertTrue(context_evidence._boundary_evidence_text_is_relevant(
            "还是恶心",
            {"恶心": 4},
        ))
        self.assertFalse(context_evidence._boundary_evidence_text_is_relevant(
            "还是恶心",
            {"恶心": 3},
        ))

    def test_physical_distress_synonyms_share_high_confidence_signal(self):
        signals = context_evidence._boundary_semantic_signals({
            "title": "直播时突然想吐",
            "publish_title": "",
            "boundary_evidence": [],
        })
        self.assertEqual(signals, {"physical_distress"})
        self.assertTrue(context_evidence._boundary_text_has_semantic_signal(
            "她说可能低血糖，现在站不稳",
            signals,
        ))
        self.assertFalse(context_evidence._boundary_text_has_semantic_signal(
            "接下来看看另一个商品",
            signals,
        ))

    def test_topic_context_gap_splits_subtitle_speech_chains(self):
        segments = [
            (0.0, 1.0, "第一句"),
            (5.0, 6.0, "刚好四秒，仍是一链"),
            (10.1, 11.0, "超过四秒，另起一链"),
            (30.0, 31.0, "搜索区间外"),
        ]
        self.assertEqual(
            context_evidence._subtitle_speech_chains(segments, 0, 20),
            [segments[:2], [segments[2]]],
        )

    def test_chain_crossing_topic_end_splits_each_trailing_sentence(self):
        chain = [
            (90.0, 95.0, "核心前句"),
            (98.0, 102.0, "跨越终点"),
            (102.0, 104.0, "终点后第一句"),
            (104.0, 106.0, "终点后第二句"),
        ]
        self.assertEqual(
            context_evidence._split_chain_crossing_topic_end(chain, 100),
            [[chain[0], chain[1]], [chain[2]], [chain[3]]],
        )
        self.assertEqual(
            context_evidence._split_chain_crossing_topic_end(chain[:2], 110),
            [chain[:2]],
        )

    def test_interval_speech_and_relevance_use_strict_overlap(self):
        mark = {
            "title": "身体受不了低血糖",
            "publish_title": "",
            "boundary_evidence": [],
        }
        segments = [
            (0.0, 5.0, "无关开场"),
            (10.0, 14.0, "身体受不了，可能是低血糖"),
            (20.0, 25.0, "另一个话题"),
        ]
        self.assertFalse(context_evidence._boundary_context_has_speech(
            5.0,
            10.0,
            segments,
        ))
        self.assertTrue(context_evidence._boundary_context_has_speech(
            9.9,
            10.1,
            segments,
        ))
        self.assertFalse(context_evidence._boundary_context_has_speech(
            10.0,
            10.0,
            segments,
        ))
        self.assertTrue(context_evidence._boundary_context_is_relevant(
            mark,
            10.0,
            14.0,
            segments,
        ))
        self.assertFalse(context_evidence._boundary_context_is_relevant(
            mark,
            20.0,
            25.0,
            segments,
        ))

    def test_mark_and_segments_are_not_modified(self):
        mark = {
            "title": "想吐低血糖",
            "publish_title": "身体受不了",
            "boundary_evidence": ["眩晕站不稳"],
        }
        segments = [
            (0.0, 2.0, "想吐"),
            (3.0, 5.0, "低血糖站不稳"),
            (10.0, 12.0, "下一个话题"),
        ]
        original_mark = copy.deepcopy(mark)
        original_segments = copy.deepcopy(segments)
        term_counts = context_evidence._boundary_evidence_term_counts(mark)
        semantic_signals = context_evidence._boundary_semantic_signals(mark)
        chains = context_evidence._subtitle_speech_chains(segments, 0, 20)
        context_evidence._boundary_evidence_text_is_relevant(
            segments[1][2],
            term_counts,
        )
        context_evidence._boundary_text_has_semantic_signal(
            segments[1][2],
            semantic_signals,
        )
        context_evidence._split_chain_crossing_topic_end(chains[0], 4)
        context_evidence._boundary_context_has_speech(0, 6, segments)
        context_evidence._boundary_context_is_relevant(mark, 0, 6, segments)

        self.assertEqual(mark, original_mark)
        self.assertEqual(segments, original_segments)


if __name__ == "__main__":
    unittest.main()
