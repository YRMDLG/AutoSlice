import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.streamer_profiles import streamer_profile_context

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src" / "autoslice"
OWNER_PATH = SRC_ROOT / "analysis" / "topic" / "titles.py"
FACADE_PATH = SRC_ROOT / "analysis" / "titles.py"
TOPIC_INIT_PATH = SRC_ROOT / "analysis" / "topic" / "__init__.py"

TITLE_CONSUMERS = (
    "src/autoslice/analysis/review/reconciliation.py",
    "src/autoslice/analysis/topic/analysis.py",
    "src/autoslice/analysis/candidates.py",
    "src/autoslice/analysis/review/prompt.py",
    "src/autoslice/analysis/review/candidates.py",
    "src/autoslice/analysis/review/decisions.py",
    "src/autoslice/analysis/report/formatting.py",
    "src/autoslice/analysis/topic/normalization.py",
    "src/autoslice/analysis/report/cleanup.py",
    "src/autoslice/analysis/manual/workflow.py",
    "src/autoslice/analysis/manual/enrichment.py",
    "src/autoslice/analysis/manual/candidates.py",
    "src/autoslice/analysis/manual/review.py",
    "src/autoslice/pipeline.py",
    "src/autoslice/reporting.py",
    "src/autoslice/topic_engine.py",
)


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _legacy_title_imports(tree):
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                alias.name
                for alias in node.names
                if alias.name == "autoslice.analysis.titles"
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module == "autoslice.analysis.titles":
                imports.append(node.module)
            elif node.module == "autoslice.analysis":
                imports.extend(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name == "titles"
                )
    return imports


class TopicTitlesOwnershipTests(unittest.TestCase):
    def test_topic_owner_has_all_42_functions_and_facade_has_no_definitions(self):
        owner_tree = _parse(OWNER_PATH)
        facade_tree = _parse(FACADE_PATH)
        definition_types = (ast.FunctionDef, ast.AsyncFunctionDef)
        owner_functions = [
            node.name for node in owner_tree.body if isinstance(node, definition_types)
        ]

        self.assertEqual(len(owner_functions), 42)
        self.assertEqual(len(owner_functions), len(set(owner_functions)))
        self.assertIn("_configured_title_review_concurrency", owner_functions)
        self.assertIn("_serialized_title_review_progress_callback", owner_functions)
        self.assertIn("load_title_style_profile", owner_functions)
        self.assertIn("review_selected_publish_titles", owner_functions)
        self.assertFalse(
            any(isinstance(node, definition_types) for node in facade_tree.body)
        )
        self.assertFalse(
            any(isinstance(node, ast.ClassDef) for node in facade_tree.body)
        )

    def test_legacy_facade_reexports_every_owner_object_by_identity(self):
        from autoslice.analysis import titles as compatibility
        from autoslice.analysis.topic import titles as owner

        self.assertIs(compatibility.FACADE_EXPORTS, owner.FACADE_EXPORTS)
        for name, value in vars(owner).items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertIs(getattr(compatibility, name), value)

        owner_function_names = [
            node.name
            for node in _parse(OWNER_PATH).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for name in owner_function_names:
            with self.subTest(function=name):
                self.assertIs(getattr(compatibility, name), getattr(owner, name))

    def test_production_consumers_import_topic_owner_directly(self):
        for relative_path in TITLE_CONSUMERS:
            path = ROOT / relative_path
            tree = _parse(path)
            direct_import = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "autoslice.analysis.topic"
                and any(alias.name == "titles" for alias in node.names)
                for node in ast.walk(tree)
            )
            with self.subTest(path=relative_path):
                self.assertTrue(direct_import)
                self.assertEqual(_legacy_title_imports(tree), [])

        violations = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            imports = _legacy_title_imports(_parse(path))
            if imports:
                violations.append((path.relative_to(ROOT).as_posix(), imports))
        self.assertEqual(violations, [])

    def test_topic_package_is_lazy_and_owner_has_no_facade_dependency(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import sys; import autoslice.analysis.topic; "
                    "assert 'autoslice.analysis.topic.titles' not in sys.modules"
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

        topic_init_tree = _parse(TOPIC_INIT_PATH)
        all_assignment = next(
            node
            for node in topic_init_tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
        )
        self.assertIn("titles", ast.literal_eval(all_assignment.value))
        self.assertEqual(_legacy_title_imports(_parse(OWNER_PATH)), [])


class TopicTitlesBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.profile_context = streamer_profile_context("zeyin")
        self.profile_context.__enter__()
        self.addCleanup(self.profile_context.__exit__, None, None, None)

    def test_prefix_fallback_transport_cleanup_and_streamer_name_stay_stable(self):
        from autoslice.analysis.topic import titles

        self.assertEqual(
            titles._normalise_publish_title(
                "【旧账号】抢到最后一张高铁票",
                "抢票经历",
            ),
            "【泽音】抢到最后一张高铁票",
        )
        self.assertEqual(
            titles._sanitize_transport_claims(
                "【泽音】抢到最后一张高铁票却差点误机",
                ["字幕核查：音音说抢到最后一张高铁票"],
            ),
            "【泽音】抢到最后一张高铁票却差点赶高铁惊魂",
        )
        self.assertEqual(
            titles._fallback_title_from_text("她开始挑战节奏天国关卡"),
            "节奏天国游戏",
        )
        self.assertEqual(
            titles._replace_streamer_role(
                "主播与泽音Melody向观众道晚安",
                "泽音Melody",
            ),
            "音音与音音向观众道晚安",
        )

    def test_style_examples_and_title_review_helpers_stay_stable(self):
        from autoslice.analysis.topic import titles

        profile = {
            "rules": ["保留具体结果"],
            "examples": [
                {"title": "【泽音】念完红SC当场反问", "tags": ["SC"]},
                {"title": "【泽音】关卡失败后发出悲鸣", "tags": ["游戏"]},
            ],
        }
        selected = titles._select_title_style_examples(
            "音音开始念一条红SC",
            profile=profile,
            limit=1,
        )
        self.assertEqual(selected, [profile["examples"][0]])

        with patch.dict(os.environ, {"AUTOSLICE_LLM_CONCURRENCY": "99"}):
            self.assertEqual(titles._configured_title_review_concurrency(), 4)
        with patch.dict(os.environ, {"AUTOSLICE_LLM_CONCURRENCY": "invalid"}):
            self.assertEqual(titles._configured_title_review_concurrency(), 3)

        events = []
        callback = titles._serialized_title_review_progress_callback(
            lambda message, step, total: events.append((message, step, total))
        )
        callback("投稿标题独立终审", 96, 100)
        self.assertEqual(events, [("投稿标题独立终审", 96, 100)])
        self.assertIsNone(titles._serialized_title_review_progress_callback(None))


if __name__ == "__main__":
    unittest.main()
