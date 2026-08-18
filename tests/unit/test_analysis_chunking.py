import ast
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.analysis.topic import chunking

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src" / "autoslice"
OWNER_PATH = SRC_ROOT / "analysis" / "topic" / "chunking.py"
FACADE_PATH = SRC_ROOT / "analysis" / "chunking.py"
TOPIC_INIT_PATH = SRC_ROOT / "analysis" / "topic" / "__init__.py"

CHUNKING_CONSUMERS = (
    "src/autoslice/analysis/candidates.py",
    "src/autoslice/pipeline.py",
    "src/autoslice/topic_engine.py",
)

PRE_MIGRATION_NON_DUNDER_NAMES = {
    "FACADE_EXPORTS",
    "annotations",
    "chunk_srt",
    "danmaku_analysis",
    "make_chunk",
    "parse_srt_text",
    "timecode",
    "topic_analysis",
    "transcription_segments",
    "transcription_srt_io",
}


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _legacy_chunking_imports(tree):
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                alias.name
                for alias in node.names
                if alias.name == "autoslice.analysis.chunking"
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module == "autoslice.analysis.chunking":
                imports.append(node.module)
            elif node.module == "autoslice.analysis":
                imports.extend(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name == "chunking"
                )
    return imports


def _import_targets(tree):
    targets = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.update(
                f"{node.module}.{alias.name}" for alias in node.names
            )
    return targets


class AnalysisChunkingTests(unittest.TestCase):
    def test_topic_owner_has_exactly_three_functions_and_facade_has_none(self):
        owner_tree = _parse(OWNER_PATH)
        facade_tree = _parse(FACADE_PATH)
        definition_types = (ast.FunctionDef, ast.AsyncFunctionDef)

        self.assertEqual(
            [
                node.name
                for node in owner_tree.body
                if isinstance(node, definition_types)
            ],
            ["parse_srt_text", "chunk_srt", "make_chunk"],
        )
        self.assertFalse(
            any(isinstance(node, ast.ClassDef) for node in owner_tree.body)
        )
        self.assertFalse(
            any(isinstance(node, definition_types) for node in facade_tree.body)
        )
        self.assertFalse(
            any(isinstance(node, ast.ClassDef) for node in facade_tree.body)
        )

    def test_legacy_facade_reexports_every_pre_migration_name_by_identity(self):
        from autoslice.analysis import chunking as compatibility
        from autoslice.analysis.topic import chunking as owner

        owner_names = {
            name for name in vars(owner) if not name.startswith("__")
        }
        self.assertEqual(owner_names, PRE_MIGRATION_NON_DUNDER_NAMES)
        self.assertIs(compatibility.FACADE_EXPORTS, owner.FACADE_EXPORTS)
        for name in sorted(PRE_MIGRATION_NON_DUNDER_NAMES):
            with self.subTest(name=name):
                self.assertIs(getattr(compatibility, name), getattr(owner, name))

    def test_production_consumers_import_topic_owner_directly(self):
        for relative_path in CHUNKING_CONSUMERS:
            path = ROOT / relative_path
            tree = _parse(path)
            direct_import = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "autoslice.analysis.topic"
                and any(alias.name == "chunking" for alias in node.names)
                for node in ast.walk(tree)
            )
            with self.subTest(path=relative_path):
                self.assertTrue(direct_import)
                self.assertEqual(_legacy_chunking_imports(tree), [])

        violations = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            imports = _legacy_chunking_imports(_parse(path))
            if imports:
                violations.append((path.relative_to(ROOT).as_posix(), imports))
        self.assertEqual(violations, [])

    def test_topic_package_is_lazy_and_owner_has_no_reverse_dependency(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import sys; import autoslice.analysis.topic; "
                    "assert 'autoslice.analysis.topic.chunking' "
                    "not in sys.modules"
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

        topic_init_tree = _parse(TOPIC_INIT_PATH)
        all_assignment = next(
            node
            for node in topic_init_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        self.assertIn("chunking", ast.literal_eval(all_assignment.value))

        forbidden_targets = {
            "autoslice.analysis.chunking",
            *(
                path.removeprefix("src/").removesuffix(".py").replace("/", ".")
                for path in CHUNKING_CONSUMERS
            ),
        }
        self.assertTrue(
            _import_targets(_parse(OWNER_PATH)).isdisjoint(forbidden_targets)
        )

    def test_parse_srt_text_filters_only_visibly_too_short_cues(self):
        repaired = [
            (1.0, 2.0, "啊"),
            (2.0, 3.0, "好的"),
            (3.0, 4.0, "A B"),
        ]

        with patch(
            "autoslice.analysis.topic.chunking."
            "transcription_srt_io.load_repaired_srt_segments",
            return_value=repaired,
        ) as load:
            result = chunking.parse_srt_text("测试.srt")

        load.assert_called_once_with("测试.srt")
        self.assertEqual(result, repaired[1:])

    def test_chunk_srt_empty_input_does_not_analyze_danmaku(self):
        self.assertEqual(chunking.chunk_srt([], peaks=[]), [])

    def test_chunk_srt_supports_three_and_two_field_segments_and_splits_by_time(self):
        segments = [
            (0, 2, "第一句"),
            (5, "第二句"),
            (11, 14, "第三句"),
        ]

        chunks = chunking.chunk_srt(segments, peaks=[], chunk_sec=10)

        self.assertEqual(len(chunks), 2)
        self.assertEqual((chunks[0]["start"], chunks[0]["end"]), (0, 600))
        self.assertIn("[0:00:00－0:00:02] 第一句", chunks[0]["text"])
        self.assertIn("[0:00:05] 第二句", chunks[0]["text"])
        self.assertEqual((chunks[1]["start"], chunks[1]["end"]), (11, 611))
        self.assertEqual(chunks[1]["text"], "[0:00:11－0:00:14] 第三句")

    def test_make_chunk_summarizes_peak_ratio_and_has_peak_flag(self):
        result = chunking.make_chunk(
            100,
            ["[0:01:40] 测试字幕"],
            peaks=[(120, 80), (200, 40)],
            avg_density=20,
            independent_peaks=[],
        )

        self.assertEqual(result["start"], 100)
        self.assertEqual(result["end"], 700)
        self.assertTrue(result["has_peaks"])
        self.assertIn("峰值80条/分钟 = 4.0倍平均", result["danmaku_info"])

    def test_make_chunk_without_nearby_peak_uses_low_density_summary(self):
        result = chunking.make_chunk(
            100,
            ["[0:01:40] 测试字幕"],
            peaks=[(900, 40)],
            avg_density=20,
            independent_peaks=[],
        )

        self.assertFalse(result["has_peaks"])
        self.assertIn("本段无峰值", result["danmaku_info"])
        self.assertEqual(result["danmaku_evidence"], [])

    def test_make_chunk_keeps_top_four_evidence_rows_in_stable_score_order(self):
        independent_peaks = [
            (200, 50),
            (100, 40),
            (300, 30),
            (400, 20),
            (500, 10),
        ]
        result = chunking.make_chunk(
            0,
            ["[0:00:00] 测试字幕"],
            peaks=independent_peaks,
            avg_density=10,
            independent_peaks=independent_peaks,
        )
        expected_rows = []
        for peak_start, density in independent_peaks:
            features = chunking.danmaku_analysis._danmaku_peak_features(
                independent_peaks,
                peak_start,
                density,
                avg_density=10,
            )
            expected_rows.append(
                (
                    float(features["selection_score"]),
                    peak_start,
                    chunking.danmaku_analysis._danmaku_prompt_evidence(
                        features
                    ),
                )
            )
        expected_rows.sort(key=lambda row: (-row[0], row[1]))

        self.assertEqual(
            result["danmaku_evidence"],
            [row[2] for row in expected_rows[:4]],
        )


if __name__ == "__main__":
    unittest.main()
