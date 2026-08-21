import ast
import copy
import unittest
from pathlib import Path

from autoslice import pipeline, slicing, topic_engine
from autoslice.analysis import boundaries, candidates

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.boundaries"
OWNER_PATH = SRC_ROOT / "autoslice/analysis/boundaries.py"
BOUNDARY_FUNCTIONS = (
    "_expand_clip_mark_with_context",
    "_expand_clip_marks_with_context",
)
OWNER_DEPENDENCIES = {
    "autoslice.analysis.review.context_edges",
    "autoslice.analysis.review.context_evidence",
    "autoslice.analysis.review.context_ranges",
    "autoslice.analysis.review.deduplication",
    "autoslice.analysis.review.finalization",
    "autoslice.analysis.review.outro",
    "autoslice.analysis.review.policy",
    "autoslice.analysis.review.transitions",
    "autoslice.analysis.review.triggers",
    "autoslice.transcription.segments",
    "autoslice.transcription.srt_io",
}
PRODUCTION_IMPORTERS = {
    "autoslice.analysis.candidates",
    "autoslice.pipeline",
    "autoslice.slicing",
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


class ClipBoundaryOwnershipTests(unittest.TestCase):
    def test_unique_owner_and_compatibility_identity(self):
        implementations = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if (
                    isinstance(node, ast.FunctionDef)
                    and node.name in BOUNDARY_FUNCTIONS
                ):
                    implementations.append((_module_name(path), node.name))

        self.assertEqual(
            set(implementations),
            {(OWNER_MODULE, name) for name in BOUNDARY_FUNCTIONS},
        )
        self.assertEqual(
            boundaries.FACADE_EXPORTS,
            {
                "_expand_clip_mark_with_context": "_expand_clip_mark_with_context",
                "_expand_clip_marks_with_context": "_expand_clip_marks_with_context",
                "_srt_video_duration": "_srt_video_duration",
                "parse_srt_segments": "parse_srt_segments",
            },
        )
        for name in BOUNDARY_FUNCTIONS:
            owner = getattr(boundaries, name)
            with self.subTest(name=name):
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)
        self.assertIs(
            pipeline._expand_clip_marks_with_context,
            boundaries._expand_clip_marks_with_context,
        )
        self.assertIs(
            slicing._expand_clip_marks_with_context,
            boundaries._expand_clip_marks_with_context,
        )

    def test_dependencies_and_direct_consumers_are_exact(self):
        owner_tree = ast.parse(OWNER_PATH.read_text(encoding="utf-8"))
        direct_imports = _direct_imports(owner_tree)
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
            candidate_tree = ast.parse(path.read_text(encoding="utf-8"))
            if OWNER_MODULE in _direct_imports(candidate_tree):
                importers.add(_module_name(path))
        self.assertEqual(importers, PRODUCTION_IMPORTERS)

    def test_single_mark_expansion_does_not_mutate_inputs(self):
        mark = {
            "start": 200,
            "end": 245,
            "title": "SC 回答观众问题",
            "body": ["观众通过 SC 提问，主播完整回答"],
            "semantic_focus_validated": True,
            "reference_start": 180,
            "reference_end": 260,
        }
        segments = [
            (150, 158, "谢谢老板的醒目留言"),
            (158, 172, "观众问为什么不直播那个游戏"),
            (200, 245, "主播给出完整原因和结论"),
        ]
        original_mark = copy.deepcopy(mark)
        original_segments = copy.deepcopy(segments)

        expanded = boundaries._expand_clip_mark_with_context(
            mark,
            srt_segments=segments,
            video_duration=300,
        )

        self.assertEqual(expanded["start"], 150)
        self.assertEqual(expanded["topic_start"], 200)
        self.assertEqual(expanded["topic_end"], 245)
        self.assertEqual(expanded["time_basis"], "video_elapsed_seconds")
        self.assertTrue(expanded["context_expanded"])
        self.assertEqual(mark, original_mark)
        self.assertEqual(segments, original_segments)

    def test_batch_expansion_keeps_context_without_overlap(self):
        marks = [
            {"start": 100, "end": 150, "title": "第一个事件"},
            {"start": 170, "end": 220, "title": "第二个事件"},
        ]
        segments = [
            (60, 95, "第一个事件起因"),
            (100, 150, "第一个事件结果"),
            (155, 165, "自然停顿"),
            (170, 220, "第二个事件结果"),
            (225, 250, "第二个事件收尾"),
        ]

        expanded = boundaries._expand_clip_marks_with_context(
            marks,
            srt_segments=segments,
            video_duration=300,
        )

        self.assertEqual(len(expanded), 2)
        self.assertLessEqual(expanded[0]["end"], expanded[1]["start"])
        self.assertLessEqual(expanded[0]["start"], 100)
        self.assertGreaterEqual(expanded[1]["end"], 220)

    def test_stream_outro_wins_overlap_and_extends_to_video_end(self):
        marks = [
            {"start": 85, "end": 110, "title": "普通尾部话题"},
            {
                "start": 90,
                "end": 100,
                "topic_start": 90,
                "topic_end": 100,
                "report_start": 90,
                "report_end": 100,
                "title": "晚安小音音",
                "clip_type": "stream_outro",
                "preserve_to_video_end": True,
            },
        ]
        original = copy.deepcopy(marks)

        expanded = boundaries._expand_clip_marks_with_context(
            marks,
            srt_segments=[],
            video_duration=121.3,
        )

        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded[0]["title"], "晚安小音音")
        self.assertEqual(expanded[0]["start"], 90)
        self.assertEqual(expanded[0]["end"], 122)
        self.assertEqual(expanded[0]["topic_end"], 122)
        self.assertEqual(expanded[0]["report_end"], 122)
        self.assertEqual(marks, original)


if __name__ == "__main__":
    unittest.main()
