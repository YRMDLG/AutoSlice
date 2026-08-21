"""公开兼容入口的符号、身份和依赖方向清单。"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

from scripts import architecture_snapshot

ROOT = Path(__file__).resolve().parents[2]

ROOT_ALIASES = {
    "app": "autoslice.web.app",
    "artifact_store": "autoslice.artifact_store",
    "core": "autoslice.core",
    "llm_client": "autoslice.llm_client",
    "media_formats": "autoslice.media_formats",
    "runtime_config": "autoslice.runtime_config",
    "security_policy": "autoslice.security_policy",
    "streamer_profiles": "autoslice.streamer_profiles",
    "subtitle_workflow": "autoslice.subtitle_workflow",
    "task_registry": "autoslice.task_registry",
    "task_store": "autoslice.task_store",
    "topic_engine": "autoslice.topic_engine",
}

ANALYSIS_FACADES = {
    "autoslice.analysis.candidate_reconciliation": (
        "autoslice.analysis.review.reconciliation"
    ),
    "autoslice.analysis.chunking": "autoslice.analysis.topic.chunking",
    "autoslice.analysis.clip_policy": "autoslice.analysis.review.policy",
    "autoslice.analysis.clip_review": "autoslice.analysis.review.workflow",
    "autoslice.analysis.clip_review_candidates": (
        "autoslice.analysis.review.candidates"
    ),
    "autoslice.analysis.clip_review_prompt": (
        "autoslice.analysis.review.prompt"
    ),
    "autoslice.analysis.clip_scoring": "autoslice.analysis.review.scoring",
    "autoslice.analysis.content_normalization": (
        "autoslice.analysis.topic.normalization"
    ),
    "autoslice.analysis.manual_candidates": (
        "autoslice.analysis.manual.candidates"
    ),
    "autoslice.analysis.manual_enrichment": (
        "autoslice.analysis.manual.enrichment"
    ),
    "autoslice.analysis.manual_review": "autoslice.analysis.manual.review",
    "autoslice.analysis.manual_timeline": "autoslice.analysis.manual.workflow",
    "autoslice.analysis.report_cleanup": "autoslice.analysis.report.cleanup",
    "autoslice.analysis.response_parsing": "autoslice.analysis.topic.response",
    "autoslice.analysis.slice_decisions": "autoslice.analysis.review.decisions",
    "autoslice.analysis.titles": "autoslice.analysis.topic.titles",
    "autoslice.analysis.topic_analysis": "autoslice.analysis.topic.analysis",
    "autoslice.analysis.topic_formatting": (
        "autoslice.analysis.report.formatting"
    ),
}

AUTOCOVER_ALIASES = {
    "autocover_tool.app": "autoslice_cover.app",
    "autocover_tool.autocover.cli": "autoslice_cover.cli",
    "autocover_tool.autocover.drafts": "autoslice_cover.drafts",
    "autocover_tool.autocover.emoji": "autoslice_cover.emoji",
    "autocover_tool.autocover.fonts": "autoslice_cover.fonts",
    "autocover_tool.autocover.paths": "autoslice_cover.paths",
    "autocover_tool.autocover.renderer": "autoslice_cover.renderer",
    "autocover_tool.autocover.stickers": "autoslice_cover.stickers",
    "autocover_tool.autocover.style": "autoslice_cover.style",
    "autocover_tool.autocover.titles": "autoslice_cover.titles",
    "autocover_tool.autocover.video": "autoslice_cover.video",
    "autocover_tool.autocover.workspace": "autoslice_cover.workspace",
}


class CompatibilityInventoryTests(unittest.TestCase):
    def test_root_aliases_are_same_module_objects(self):
        for legacy_name, owner_name in ROOT_ALIASES.items():
            with self.subTest(legacy_name=legacy_name):
                self.assertIs(
                    importlib.import_module(legacy_name),
                    importlib.import_module(owner_name),
                )

    def test_analysis_facades_export_only_owner_objects(self):
        for facade_name, owner_name in ANALYSIS_FACADES.items():
            facade = importlib.import_module(facade_name)
            owner = importlib.import_module(owner_name)
            with self.subTest(facade=facade_name):
                self.assertIs(facade.FACADE_EXPORTS, owner.FACADE_EXPORTS)
                self.assertTrue(facade.FACADE_EXPORTS)
                for facade_symbol, owner_symbol in facade.FACADE_EXPORTS.items():
                    with self.subTest(symbol=facade_symbol):
                        self.assertTrue(hasattr(owner, owner_symbol))
                        facade_value = getattr(facade, facade_symbol, None)
                        if facade_symbol.startswith("_") and facade_value is None:
                            # façade 会保留私有符号的 owner 清单，但只把
                            # 非私有名字复制到模块命名空间。
                            continue
                        self.assertIsNotNone(facade_value)
                        self.assertIs(
                            facade_value,
                            getattr(owner, owner_symbol),
                        )

    def test_compatibility_files_have_no_second_top_level_implementation(self):
        relative_paths = [
            *(name.replace(".", "/") + ".py" for name in ANALYSIS_FACADES),
            *(
                "autocover_tool/" + name.split(".", 1)[1].replace(".", "/") + ".py"
                for name in AUTOCOVER_ALIASES
            ),
        ]
        for relative_path in relative_paths:
            path = ROOT / "src" / relative_path if relative_path.startswith("autoslice/") else ROOT / relative_path
            if not path.exists():
                # autoslice package names include the ``autoslice/`` prefix;
                # root-relative AutoCover names already point at the shim tree.
                path = ROOT / relative_path
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), str(path))
            definitions = [
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ]
            with self.subTest(path=str(path)):
                self.assertEqual(definitions, [])

    def test_production_consumers_do_not_import_analysis_facades(self):
        snapshot = architecture_snapshot.build_snapshot(ROOT)
        import_edges = {
            (edge["from"], edge["to"])
            for edge in snapshot["import_edges"]
        }
        facade_names = set(ANALYSIS_FACADES)
        production_modules = {
            item["module"]
            for item in snapshot["modules"]
            if item["path"].startswith("src/")
        }
        for consumer in production_modules - facade_names:
            for facade_name in facade_names:
                with self.subTest(consumer=consumer, facade=facade_name):
                    self.assertNotIn((consumer, facade_name), import_edges)

        for facade_name, owner_name in ANALYSIS_FACADES.items():
            with self.subTest(facade=facade_name):
                self.assertIn((facade_name, owner_name), import_edges)
                self.assertNotIn((owner_name, facade_name), import_edges)

    def test_autocover_compatibility_inventory_matches_unique_owner(self):
        for legacy_name, owner_name in AUTOCOVER_ALIASES.items():
            with self.subTest(legacy_name=legacy_name):
                self.assertIs(
                    importlib.import_module(legacy_name),
                    importlib.import_module(owner_name),
                )

        snapshot = architecture_snapshot.build_snapshot(ROOT)
        import_edges = {
            (edge["from"], edge["to"])
            for edge in snapshot["import_edges"]
        }
        for legacy_name, owner_name in AUTOCOVER_ALIASES.items():
            with self.subTest(edge=(legacy_name, owner_name)):
                self.assertIn((legacy_name, "autocover_tool._compat"), import_edges)
                self.assertNotIn((owner_name, legacy_name), import_edges)


if __name__ == "__main__":
    unittest.main()
