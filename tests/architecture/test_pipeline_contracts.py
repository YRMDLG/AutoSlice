"""主流水线与 retry 阶段的唯一 owner 和编排边界护栏。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from scripts import architecture_snapshot

ROOT = Path(__file__).resolve().parents[2]


class PipelineArchitectureContractTests(unittest.TestCase):
    def _function_tree(self, relative_path: str, function_name: str):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
        return next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        )

    @staticmethod
    def _direct_call_names(function_node):
        return [
            node.func.id
            for node in ast.walk(function_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
        ]

    def test_main_pipeline_stage_bindings_have_one_owner(self):
        from autoslice import (
            pipeline,
            pipeline_analysis,
            pipeline_artifacts,
            pipeline_boundaries,
            pipeline_decisions,
            pipeline_llm,
            pipeline_manual,
            pipeline_reporting,
            pipeline_review,
            pipeline_titles,
            pipeline_transcription,
        )

        bindings = {
            "prepare_pipeline_subtitles": pipeline_transcription,
            "prepare_pipeline_analysis": pipeline_analysis,
            "prepare_pipeline_manual_timeline": pipeline_manual,
            "analyze_pipeline_llm_chunks": pipeline_llm,
            "review_pipeline_candidates": pipeline_review,
            "review_pipeline_publish_titles": pipeline_titles,
            "prepare_pipeline_decisions": pipeline_decisions,
            "prepare_pipeline_boundaries": pipeline_boundaries,
            "prepare_pipeline_report": pipeline_reporting,
            "persist_pipeline_artifacts": pipeline_artifacts,
        }
        for name, owner in bindings.items():
            with self.subTest(stage=name):
                self.assertIs(getattr(pipeline, name), getattr(owner, name))

        run_pipeline = self._function_tree(
            "src/autoslice/pipeline.py",
            "run_pipeline_impl",
        )
        calls = self._direct_call_names(run_pipeline)
        for name in bindings:
            with self.subTest(production_consumer=name):
                self.assertEqual(calls.count(name), 1)

    def test_retry_stage_bindings_have_one_owner_and_reuse_existing_domains(self):
        from autoslice import (
            pipeline,
            pipeline_artifacts,
            pipeline_retry,
            pipeline_retry_analysis,
            pipeline_retry_decisions,
            pipeline_retry_reporting,
            pipeline_retry_review,
        )

        bindings = {
            "prepare_retry_pipeline_state": pipeline_retry,
            "prepare_retry_analysis_state": pipeline_retry_analysis,
            "review_retry_candidates_and_titles": pipeline_retry_review,
            "prepare_retry_decisions": pipeline_retry_decisions,
            "prepare_retry_report": pipeline_retry_reporting,
            "persist_pipeline_artifacts": pipeline_artifacts,
        }
        for name, owner in bindings.items():
            with self.subTest(stage=name):
                self.assertIs(getattr(pipeline, name, None), getattr(owner, name, None))

        retry_pipeline = self._function_tree(
            "src/autoslice/pipeline.py",
            "retry_clip_review_from_artifacts_impl",
        )
        calls = self._direct_call_names(retry_pipeline)
        for name in (
            "prepare_retry_pipeline_state",
            "prepare_retry_analysis_state",
            "review_retry_candidates_and_titles",
            "prepare_retry_decisions",
            "prepare_retry_report",
            "persist_pipeline_artifacts",
        ):
            with self.subTest(retry_consumer=name):
                self.assertEqual(calls.count(name), 1)

        # retry 只能连接已有 owner，不能反向回到 pipeline 或 topic_engine。
        snapshot = architecture_snapshot.build_snapshot(ROOT)
        import_edges = {
            (edge["from"], edge["to"])
            for edge in snapshot["import_edges"]
        }
        retry_modules = {
            "autoslice.pipeline_retry",
            "autoslice.pipeline_retry_analysis",
            "autoslice.pipeline_retry_review",
            "autoslice.pipeline_retry_decisions",
            "autoslice.pipeline_retry_reporting",
        }
        for module in retry_modules:
            with self.subTest(retry_module=module):
                self.assertNotIn((module, "autoslice.pipeline"), import_edges)
                self.assertNotIn((module, "autoslice.topic_engine"), import_edges)

        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry"),
            import_edges,
        )
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry_analysis"),
            import_edges,
        )
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry_review"),
            import_edges,
        )
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry_decisions"),
            import_edges,
        )
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry_reporting"),
            import_edges,
        )

    def test_pipeline_and_retry_do_not_define_duplicate_stage_functions(self):
        snapshot = architecture_snapshot.build_snapshot(ROOT)
        modules = {
            item["module"]: item for item in snapshot["modules"]
        }
        expected = {
            "autoslice.pipeline": {
                "run_pipeline_impl",
                "retry_clip_review_from_artifacts_impl",
            },
            "autoslice.pipeline_retry": {"prepare_retry_pipeline_state"},
            "autoslice.pipeline_retry_analysis": {
                "prepare_retry_analysis_state",
            },
            "autoslice.pipeline_retry_review": {
                "review_retry_candidates_and_titles",
            },
            "autoslice.pipeline_retry_decisions": {"prepare_retry_decisions"},
            "autoslice.pipeline_retry_reporting": {"prepare_retry_report"},
        }
        for module_name, owner_functions in expected.items():
            with self.subTest(module=module_name):
                actual = {
                    item["name"]
                    for item in modules[module_name]["top_level_functions"]
                }
                self.assertTrue(owner_functions.issubset(actual))

        self.assertEqual(snapshot["dependency_cycles"], [])
        self.assertEqual(snapshot["duplicate_top_level_definitions"], [])
        self.assertLessEqual(snapshot["test_private_patches"]["total"], 17)


if __name__ == "__main__":
    unittest.main()
