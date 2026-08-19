import ast
import hashlib
import unittest
from pathlib import Path

from autoslice import topic_engine
from autoslice.analysis import boundaries, candidates
from autoslice.analysis.review import finalization
from scripts import architecture_snapshot

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.finalization"
OWNER_PATH = SRC_ROOT / "autoslice/analysis/review/finalization.py"
REVIEW_INIT_PATH = SRC_ROOT / "autoslice/analysis/review/__init__.py"
FUNCTION_NAMES = (
    "_nearest_safe_srt_boundary",
    "_merge_expanded_clip_marks",
    "_refresh_natural_boundary_metadata",
    "_cap_expanded_clip_mark",
    "_snap_clip_to_srt_segments",
    "_integer_clip_bounds_outside_subtitles",
    "_fit_final_clip_to_safe_srt_boundaries",
    "_capped_speech_chain_start",
)
EXPECTED_SOURCE_HASHES = {
    "_nearest_safe_srt_boundary": (
        "ec4c586d065ce7c8beddbe1b3fb2922f87859ca136711272d3117632c0bd963c"
    ),
    "_merge_expanded_clip_marks": (
        "147891b1366074eb9863793ef9d22868dd125897d49915f9dd9dcbdd02f887bf"
    ),
    "_refresh_natural_boundary_metadata": (
        "27164cd00deb6ee24f0184308eaa65564769ec13df90e1f1a033c749a6ff0ee3"
    ),
    "_cap_expanded_clip_mark": (
        "acdd6ec506332eaac2afc382699becf263e85051be39263c40b5fad645a936df"
    ),
    "_snap_clip_to_srt_segments": (
        "e544ba6f16c14f6a70276c67c46b168991f8e88b6a31b3ba4b453421ea935e5c"
    ),
    "_integer_clip_bounds_outside_subtitles": (
        "b43df6bdade84df0800930e45d6299d8722d3ea32044e251274c220c5165af05"
    ),
    "_fit_final_clip_to_safe_srt_boundaries": (
        "d2a6c497d15b2bce7894dfbc6473db6a6ee02da56cbfb5a171eca326127e6f87"
    ),
    "_capped_speech_chain_start": (
        "12b02295ec678be5b9623a69eb73068d113d034b889a59915d2ac81d701ed9d1"
    ),
}
OWNER_DEPENDENCIES = {
    "autoslice.analysis.review.deduplication",
    "autoslice.analysis.review.policy",
    "autoslice.transcription.segments",
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


class ReviewFinalizationOwnershipTests(unittest.TestCase):
    def test_migrated_function_source_hashes_are_exact(self):
        source = OWNER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        definitions = {
            node.name: ast.get_source_segment(source, node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        actual = {
            name: hashlib.sha256(definitions[name].encode("utf-8")).hexdigest()
            for name in FUNCTION_NAMES
        }

        self.assertEqual(actual, EXPECTED_SOURCE_HASHES)

    def test_finalization_is_unique_owner_and_facades_keep_object_identity(self):
        implementations = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in FUNCTION_NAMES
                ):
                    implementations.append((_module_name(path), node.name))

        self.assertEqual(
            set(implementations),
            {(OWNER_MODULE, name) for name in FUNCTION_NAMES},
        )
        for name in FUNCTION_NAMES:
            owner = getattr(finalization, name)
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

        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.Dict)
        self.assertEqual(
            ast.literal_eval(assignments[0].value),
            {name: name for name in FUNCTION_NAMES},
        )
        self.assertEqual(
            finalization.FACADE_EXPORTS,
            {name: name for name in FUNCTION_NAMES},
        )

    def test_owner_dependencies_and_production_importers_are_exact(self):
        owner_tree = ast.parse(OWNER_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                name
                for name in _direct_imports(owner_tree)
                if name.startswith("autoslice.")
            },
            OWNER_DEPENDENCIES,
        )

        importers = set()
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if OWNER_MODULE in _direct_imports(tree):
                importers.add(_module_name(path))
        self.assertEqual(importers, PRODUCTION_IMPORTERS)

    def test_boundaries_implementation_calls_owner_not_local_aliases(self):
        tree = ast.parse(
            (SRC_ROOT / "autoslice/analysis/boundaries.py").read_text(
                encoding="utf-8"
            )
        )
        local_calls = []
        owner_calls = []
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
                    and node.func.value.id == "finalization"
                    and node.func.attr in FUNCTION_NAMES
                ):
                    owner_calls.append((function.name, node.func.attr, node.lineno))

        self.assertEqual(local_calls, [])
        self.assertEqual(
            {name for _, name, _ in owner_calls},
            set(FUNCTION_NAMES) - {"_capped_speech_chain_start"},
        )

    def test_review_package_stays_lazy_with_one_sorted_static_all(self):
        source = REVIEW_INIT_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [
            node
            for node in tree.body
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
        declared = ast.literal_eval(assignments[0].value)
        self.assertEqual(declared, sorted(declared))
        self.assertEqual(
            declared,
            [
                "candidates",
                "context_evidence",
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

    def test_architecture_metrics_remain_within_contract(self):
        current = architecture_snapshot.build_snapshot(ROOT)
        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }

        self.assertEqual(
            {source for source, target in import_edges if target == OWNER_MODULE},
            PRODUCTION_IMPORTERS,
        )
        self.assertEqual(
            {target for source, target in import_edges if source == OWNER_MODULE},
            OWNER_DEPENDENCIES,
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)


class ReviewFinalizationBehaviorTests(unittest.TestCase):
    def test_duration_limits_and_preserve_to_video_end(self):
        ordinary = finalization._cap_expanded_clip_mark(
            {
                "start": 0,
                "end": 500,
                "topic_start": 100,
                "topic_end": 200,
                "title": "普通片段",
            }
        )
        required_context = finalization._cap_expanded_clip_mark(
            {
                "start": 0,
                "end": 500,
                "topic_start": 200,
                "topic_end": 250,
                "required_context_start": 50,
                "required_context_end": 350,
                "title": "必要上下文片段",
            }
        )
        outro_mark = {
            "start": 0,
            "end": 500,
            "title": "收播片段",
            "preserve_to_video_end": True,
        }

        self.assertLessEqual(ordinary["end"] - ordinary["start"], 300)
        self.assertTrue(ordinary["duration_capped"])
        self.assertLessEqual(
            required_context["end"] - required_context["start"],
            330,
        )
        self.assertEqual(
            finalization._cap_expanded_clip_mark(outro_mark),
            outro_mark,
        )

    def test_subtitle_safe_points_integer_bounds_and_final_fit(self):
        self.assertEqual(
            finalization._snap_clip_to_srt_segments(
                100,
                120,
                [
                    (96, 99, "连续开头"),
                    (100, 120, "核心字幕"),
                    (121, 125, "连续结尾"),
                ],
            ),
            (96, 125),
        )
        self.assertEqual(
            finalization._nearest_safe_srt_boundary(
                10,
                8,
                12,
                [(9.5, 10.5, "候选点处于句中")],
            ),
            9,
        )
        self.assertEqual(
            finalization._integer_clip_bounds_outside_subtitles(
                10.4,
                20.1,
                [
                    (9.5, 11.2, "起点句"),
                    (20.5, 22.2, "终点句"),
                ],
            ),
            (9, 23),
        )

        fixed = finalization._fit_final_clip_to_safe_srt_boundaries(
            {
                "start": 100,
                "end": 340,
                "topic_start": 120,
                "topic_end": 300,
                "title": "最终边界避开字幕句",
            },
            [(338.4, 345.2, "不能切断的收尾句")],
        )
        self.assertEqual(fixed["end"], 338)
        self.assertFalse(338.4 < fixed["end"] < 345.2)

    def test_capped_continuous_speech_chain_rewinds_to_safe_start(self):
        chain_start = finalization._capped_speech_chain_start(
            314,
            250,
            [
                (290.2, 299.0, "连续收尾第一句"),
                (300.2, 305.0, "连续收尾第二句"),
                (307.0, 312.0, "连续收尾第三句"),
                (314.0, 345.0, "限长点切进的最后一句"),
            ],
        )

        self.assertEqual(chain_start, 290)

    def test_context_overlap_splits_safely_and_refreshes_metadata(self):
        marks = [
            {
                "start": 0,
                "end": 120,
                "topic_start": 20,
                "topic_end": 60,
                "title": "话题 A",
                "context_end_before_natural": 100,
            },
            {
                "start": 100,
                "end": 220,
                "topic_start": 140,
                "topic_end": 180,
                "title": "话题 B",
                "required_context_start": 140,
                "context_start_before_natural": 130,
            },
        ]

        result = finalization._merge_expanded_clip_marks(
            marks,
            [(118, 122, "重叠区内的完整字幕句")],
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["end"], result[1]["start"])
        self.assertEqual(result[0]["end"], 118)
        self.assertEqual(result[0]["natural_boundary_post_sec"], 18)
        self.assertEqual(result[1]["natural_boundary_pre_sec"], 12)
        self.assertEqual(marks[0]["end"], 120)
        self.assertEqual(marks[1]["start"], 100)

    def test_core_overlap_merges_titles_ranges_and_context_flags(self):
        result = finalization._merge_expanded_clip_marks(
            [
                {
                    "start": 0,
                    "end": 120,
                    "topic_start": 20,
                    "topic_end": 100,
                    "title": "话题 A",
                },
                {
                    "start": 60,
                    "end": 180,
                    "topic_start": 60,
                    "topic_end": 140,
                    "title": "话题 B",
                },
            ]
        )

        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["start"], result[0]["end"]), (0, 180))
        self.assertEqual(
            (result[0]["topic_start"], result[0]["topic_end"]),
            (20, 140),
        )
        self.assertEqual(result[0]["title"], "话题 A / 话题 B")
        self.assertEqual(result[0]["merged_titles"], ["话题 A", "话题 B"])
        self.assertTrue(result[0]["merged_context"])

    def test_metadata_refresh_copies_input_and_keeps_required_fields(self):
        mark = {
            "start": 90,
            "end": 220,
            "context_start_before_natural": 100,
            "context_end_before_natural": 200,
            "required_context_start": 95,
            "required_context_end": 210,
            "semantic_focus_validated": True,
            "duration_capped": True,
        }

        refreshed = finalization._refresh_natural_boundary_metadata(mark)

        self.assertIsNot(refreshed, mark)
        self.assertNotIn("natural_boundary_pre_sec", mark)
        self.assertNotIn("natural_boundary_post_sec", mark)
        self.assertEqual(refreshed["natural_boundary_pre_sec"], 10)
        self.assertEqual(refreshed["natural_boundary_post_sec"], 20)
        for field in (
            "required_context_start",
            "required_context_end",
            "semantic_focus_validated",
            "duration_capped",
        ):
            self.assertEqual(refreshed[field], mark[field])


if __name__ == "__main__":
    unittest.main()
