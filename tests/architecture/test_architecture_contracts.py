import ast
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from scripts import (
    architecture_snapshot,
    compile_public,
    scan_public_release,
    validate_public_docs,
)

ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = ROOT / "architecture_baseline.json"

LEGACY_COMPATIBILITY_PATHS = frozenset({
    "_autoslice_compat.py",
    "app.py",
    "artifact_store.py",
    "core.py",
    "llm_client.py",
    "media_formats.py",
    "runtime_config.py",
    "security_policy.py",
    "setup_asr_model.py",
    "setup_gpu_runtime.py",
    "streamer_profiles.py",
    "subtitle_workflow.py",
    "task_registry.py",
    "task_store.py",
    "topic_engine.py",
    "启动.py",
    "autocover_tool/__init__.py",
    "autocover_tool/_compat.py",
    "autocover_tool/app.py",
    "autocover_tool/启动.py",
    "autocover_tool/autocover/__init__.py",
    "autocover_tool/autocover/cli.py",
    "autocover_tool/autocover/drafts.py",
    "autocover_tool/autocover/emoji.py",
    "autocover_tool/autocover/fonts.py",
    "autocover_tool/autocover/paths.py",
    "autocover_tool/autocover/renderer.py",
    "autocover_tool/autocover/stickers.py",
    "autocover_tool/autocover/style.py",
    "autocover_tool/autocover/titles.py",
    "autocover_tool/autocover/video.py",
    "autocover_tool/autocover/workspace.py",
})

DEFINITION_FREE_COMPATIBILITY_MODULES = frozenset({
    "app",
    "artifact_store",
    "core",
    "llm_client",
    "media_formats",
    "runtime_config",
    "security_policy",
    "setup_asr_model",
    "setup_gpu_runtime",
    "streamer_profiles",
    "subtitle_workflow",
    "task_registry",
    "task_store",
    "topic_engine",
    "启动",
    "autoslice.analysis.candidate_reconciliation",
    "autoslice.analysis.chunking",
    "autoslice.analysis.clip_policy",
    "autoslice.analysis.clip_review",
    "autoslice.analysis.clip_review_candidates",
    "autoslice.analysis.clip_review_prompt",
    "autoslice.analysis.clip_scoring",
    "autoslice.analysis.content_normalization",
    "autoslice.analysis.manual_enrichment",
    "autoslice.analysis.manual_review",
    "autoslice.analysis.manual_timeline",
    "autoslice.analysis.response_parsing",
    "autoslice.analysis.titles",
    "autoslice.analysis.topic_analysis",
    "autocover_tool",
    "autocover_tool.app",
    "autocover_tool.启动",
    "autocover_tool.autocover",
    "autocover_tool.autocover.cli",
    "autocover_tool.autocover.drafts",
    "autocover_tool.autocover.emoji",
    "autocover_tool.autocover.fonts",
    "autocover_tool.autocover.paths",
    "autocover_tool.autocover.renderer",
    "autocover_tool.autocover.stickers",
    "autocover_tool.autocover.style",
    "autocover_tool.autocover.titles",
    "autocover_tool.autocover.video",
    "autocover_tool.autocover.workspace",
})

LEGACY_IMPORT_ROOTS = frozenset({
    "app",
    "artifact_store",
    "autocover",
    "autocover_tool",
    "core",
    "llm_client",
    "media_formats",
    "runtime_config",
    "security_policy",
    "setup_asr_model",
    "setup_gpu_runtime",
    "streamer_profiles",
    "subtitle_workflow",
    "task_registry",
    "task_store",
    "topic_engine",
})

# Phase 4 已把以下实现全部迁入唯一 gateway；债务记录保留为回归护栏，
# 后续不得在 topic_engine 中重新引入同名定义。
CURRENT_LLM_IDENTITY_DEBT = {
    "status": "resolved",
    "owner_module": "autoslice.topic_engine",
    "local_definitions": (
        "_LLMProviderRetryCoordinator",
        "_call_llm_with_retry",
        "_decode_llm_response_json",
        "_parse_anthropic_response",
        "_parse_openai_response",
        "call_llm",
    ),
}


class ArchitectureSnapshotTests(unittest.TestCase):

    def test_unified_dependency_manifests_match_public_contract(self):
        errors = []

        validate_public_docs._validate_dependency_contract(errors)

        self.assertEqual(errors, [])

    def test_private_video_topic_analyzer_is_outside_architecture_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
            private_dir = root / "video-topic-analyzer" / "scripts"
            private_dir.mkdir(parents=True)
            (private_dir / "private.py").write_text(
                "raise RuntimeError('不得扫描')\n",
                encoding="utf-8",
            )

            snapshot = architecture_snapshot.build_snapshot(root)

        self.assertEqual(snapshot["scope"]["production_files"], ["product.py"])
        self.assertIn(
            "video-topic-analyzer",
            snapshot["scope"]["excluded_directory_names"],
        )

    def test_public_compile_uses_git_release_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tracked = root / "tracked.py"
            ignored = root / "video-topic-analyzer" / "private.py"
            tracked.write_text("VALUE = 1\n", encoding="utf-8")
            ignored.parent.mkdir(parents=True)
            ignored.write_text("VALUE = 2\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=b"tracked.py\0",
            )

            with patch.object(
                compile_public.subprocess,
                "run",
                return_value=completed,
            ) as run_mock:
                files = compile_public.discover_public_python_files(root)

        self.assertEqual(files, [tracked])
        command = run_mock.call_args.args[0]
        self.assertIn("--exclude-standard", command)
        self.assertEqual(run_mock.call_args.kwargs["cwd"], root)

    def test_release_scan_recognises_nested_test_packages_as_fixtures(self):
        fixture_path = ROOT / "tests" / "integration" / "test_fixture.py"
        fixture_text = (
            'VIDEO = r"X:\\personal\\recording.flv"\n'
            'URL = "file:///X:/personal/recording.flv"\n'
        )
        product_path = ROOT / "autoslice" / "unsafe_fixture.py"

        self.assertEqual(
            scan_public_release._text_errors(fixture_path, fixture_text),
            [],
        )
        self.assertTrue(
            scan_public_release._text_errors(product_path, fixture_text),
        )

    def test_ast_snapshot_reports_source_facts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "alpha.py").write_text(
                "import beta\n"
                "\n"
                "def duplicate():\n"
                "    return 1\n"
                "\n"
                "def duplicate():\n"
                "    return 2\n",
                encoding="utf-8",
            )
            (root / "beta.py").write_text(
                "class Worker:\n"
                "    def run(self):\n"
                "        return 'ok'\n",
                encoding="utf-8",
            )
            (root / "test_alpha.py").write_text(
                "from unittest.mock import patch\n"
                "\n"
                "def test_private_seam():\n"
                "    with patch('alpha._private_helper'):\n"
                "        pass\n",
                encoding="utf-8",
            )

            snapshot = architecture_snapshot.build_snapshot(root)

        modules = {item["module"]: item for item in snapshot["modules"]}
        self.assertEqual(snapshot["summary"]["production_module_count"], 2)
        self.assertEqual(modules["alpha"]["line_count"], 7)
        self.assertEqual(
            [item["name"] for item in modules["alpha"]["top_level_functions"]],
            ["duplicate", "duplicate"],
        )
        self.assertEqual(
            snapshot["duplicate_top_level_definitions"][0]["name"],
            "duplicate",
        )
        self.assertEqual(
            [(edge["from"], edge["to"]) for edge in snapshot["import_edges"]],
            [("alpha", "beta")],
        )
        self.assertEqual(snapshot["test_private_patches"]["total"], 1)
        self.assertEqual(
            snapshot["test_private_patches"]["targets"],
            [{"target": "alpha._private_helper", "count": 1}],
        )

    def test_snapshot_generation_is_repeatable_and_offline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source"
            source_dir.mkdir()
            (source_dir / "sample.py").write_text(
                "def sample():\n    return 1\n",
                encoding="utf-8",
            )
            first_output = root / "first.json"
            second_output = root / "second.json"
            with (
                patch.object(Path, "home", side_effect=AssertionError("不得读取用户目录")),
                patch.object(socket, "create_connection", side_effect=AssertionError("不得访问网络")),
                patch.object(urllib.request, "urlopen", side_effect=AssertionError("不得访问网络")),
            ):
                first = architecture_snapshot.build_snapshot(source_dir)
                second = architecture_snapshot.build_snapshot(source_dir)
                architecture_snapshot.write_snapshot(first, first_output)
                architecture_snapshot.write_snapshot(second, second_output)

            self.assertEqual(first, second)
            self.assertEqual(first_output.read_bytes(), second_output.read_bytes())
            self.assertNotIn(str(Path.home()), first_output.read_text(encoding="utf-8"))

    def test_committed_baseline_has_consistent_key_metrics(self):
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(baseline["schema_version"], 1)
        self.assertEqual(baseline["generator"], "scripts/architecture_snapshot.py")
        self.assertEqual(baseline["scope"]["root"], ".")
        self.assertIn("topic_engine.py", baseline["scope"]["production_files"])
        self.assertIn(
            "tests/integration/test_topic_engine.py",
            baseline["scope"]["test_files"],
        )
        self.assertGreater(baseline["summary"]["production_module_count"], 0)
        self.assertEqual(
            baseline["summary"]["duplicate_top_level_definition_count"],
            len(baseline["duplicate_top_level_definitions"]),
        )
        self.assertEqual(
            baseline["summary"]["import_edge_count"],
            len(baseline["import_edges"]),
        )
        self.assertEqual(
            baseline["summary"]["test_private_patch_count"],
            baseline["test_private_patches"]["total"],
        )
        self.assertEqual(
            baseline["summary"]["production_line_count"],
            sum(module["line_count"] for module in baseline["modules"]),
        )


class ArchitectureDependencyTests(unittest.TestCase):

    def test_cycle_detection_is_derived_from_import_edges(self):
        edges = [
            {"from": "alpha", "to": "beta"},
            {"from": "beta", "to": "alpha"},
            {"from": "beta", "to": "gamma"},
        ]

        cycles = architecture_snapshot.find_dependency_cycles(
            {"alpha", "beta", "gamma"},
            edges,
        )

        self.assertEqual(cycles, [{
            "modules": ["alpha", "beta"],
            "internal_edges": [
                {"from": "alpha", "to": "beta"},
                {"from": "beta", "to": "alpha"},
            ],
            "debt_status": "present",
        }])

    def test_current_architecture_has_no_dependency_cycles_or_reverse_edges(self):
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        current = architecture_snapshot.build_snapshot(ROOT)

        violations = architecture_snapshot.dependency_contract_violations(
            current,
            baseline,
        )

        self.assertEqual(violations, [])
        self.assertEqual(baseline["dependency_cycles"], [])
        self.assertEqual(current["dependency_cycles"], [])

        edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertNotIn(("subtitle_workflow", "topic_engine"), edges)
        self.assertNotIn(("topic_engine", "subtitle_workflow"), edges)
        for high_level_module in architecture_snapshot.FORBIDDEN_LOW_LEVEL_TARGETS:
            self.assertNotIn(
                ("autoslice.transcription.contracts", high_level_module),
                edges,
            )

    def test_new_cycle_is_rejected_without_expanding_debt_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "new_left.py").write_text("import new_right\n", encoding="utf-8")
            (root / "new_right.py").write_text("import new_left\n", encoding="utf-8")
            current = architecture_snapshot.build_snapshot(root)

        violations = architecture_snapshot.dependency_contract_violations(
            current,
            {"dependency_cycles": []},
        )

        self.assertEqual(
            violations,
            ["检测到基线外模块依赖环：new_left -> new_right"],
        )

    def test_autoslice_module_cannot_import_high_level_facades(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "topic_engine.py").write_text("VALUE = 1\n", encoding="utf-8")
            transport_dir = root / "autoslice" / "llm"
            transport_dir.mkdir(parents=True)
            (root / "autoslice" / "__init__.py").write_text("", encoding="utf-8")
            (transport_dir / "__init__.py").write_text("", encoding="utf-8")
            (transport_dir / "transport.py").write_text(
                "from topic_engine import VALUE\n",
                encoding="utf-8",
            )
            current = architecture_snapshot.build_snapshot(root)

        violations = architecture_snapshot.dependency_contract_violations(
            current,
            {"dependency_cycles": []},
        )

        self.assertEqual(
            violations,
            ["底层模块 autoslice.llm.transport 禁止反向导入高层模块 topic_engine"],
        )

    def test_all_autoslice_modules_avoid_high_level_reverse_imports(self):
        current = architecture_snapshot.build_snapshot(ROOT)
        forbidden = set(architecture_snapshot.FORBIDDEN_LOW_LEVEL_TARGETS)

        reverse_edges = [
            edge
            for edge in current["import_edges"]
            if edge["from"].startswith("autoslice.")
            and not any(
                edge["from"] == module
                or edge["from"].startswith(f"{module}.")
                for module in architecture_snapshot.HIGH_LEVEL_SOURCE_MODULES
            )
            and edge["to"] in forbidden
        ]

        self.assertEqual(reverse_edges, [])

    def test_low_level_autoslice_modules_do_not_import_autocover_implementation(self):
        current = architecture_snapshot.build_snapshot(ROOT)

        reverse_edges = [
            edge
            for edge in current["import_edges"]
            if edge["from"].startswith("autoslice.")
            and not any(
                edge["from"] == module
                or edge["from"].startswith(f"{module}.")
                for module in architecture_snapshot.HIGH_LEVEL_SOURCE_MODULES
            )
            and (
                edge["to"] == "autoslice_cover"
                or edge["to"].startswith("autoslice_cover.")
            )
        ]

        self.assertEqual(reverse_edges, [])


class ArchitectureDefinitionTests(unittest.TestCase):

    def test_editable_install_imports_product_packages_from_src_layout(self):
        import autoslice
        import autoslice_cover

        for package in (autoslice, autoslice_cover):
            with self.subTest(package=package.__name__):
                package_path = Path(package.__file__).resolve()
                self.assertTrue(package_path.is_relative_to(ROOT / "src"))

    def test_all_automated_tests_live_under_the_root_tests_package(self):
        _production_files, test_files = architecture_snapshot.discover_python_files(ROOT)

        outside_root_tests = sorted(
            path.relative_to(ROOT).as_posix()
            for path in test_files
            if not path.is_relative_to(ROOT / "tests")
        )

        self.assertEqual(outside_root_tests, [])

    def test_product_owners_live_under_src_or_explicit_script_boundaries(self):
        current = architecture_snapshot.build_snapshot(ROOT)
        production_paths = set(current["scope"]["production_files"])
        outside_owner_boundaries = sorted(
            path
            for path in production_paths
            if not path.startswith("src/")
            and not path.startswith("scripts/")
            and path not in LEGACY_COMPATIBILITY_PATHS
        )

        self.assertEqual(outside_owner_boundaries, [])

    def test_legacy_compatibility_modules_are_definition_free(self):
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}

        for module_name in sorted(DEFINITION_FREE_COMPATIBILITY_MODULES):
            with self.subTest(module=module_name):
                module = modules[module_name]
                self.assertEqual(module["top_level_functions"], [])
                self.assertEqual(module["top_level_classes"], [])
                self.assertLessEqual(module["line_count"], 20)

    def test_non_architecture_tests_import_product_owners_directly(self):
        violations = []
        for path in sorted((ROOT / "tests").rglob("*.py")):
            if "architecture" in path.relative_to(ROOT / "tests").parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported_roots = []
                if isinstance(node, ast.Import):
                    imported_roots = [alias.name.split(".", 1)[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_roots = [node.module.split(".", 1)[0]]
                for imported_root in imported_roots:
                    if imported_root in LEGACY_IMPORT_ROOTS:
                        violations.append(
                            f"{path.relative_to(ROOT).as_posix()}:{node.lineno} "
                            f"导入旧入口 {imported_root}"
                        )

                if not isinstance(node, ast.Call) or not node.args:
                    continue
                is_patch_call = (
                    isinstance(node.func, ast.Name) and node.func.id == "patch"
                ) or (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "patch"
                )
                target = node.args[0]
                if (
                    is_patch_call
                    and isinstance(target, ast.Constant)
                    and isinstance(target.value, str)
                    and "." in target.value
                    and target.value.split(".", 1)[0] in LEGACY_IMPORT_ROOTS
                ):
                    violations.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno} "
                        f"patch 旧入口 {target.value}"
                    )

        self.assertEqual(violations, [])

    def test_legacy_top_level_modules_alias_the_installed_owners(self):
        aliases = {
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
        script = (
            "import importlib\n"
            f"aliases = {aliases!r}\n"
            "for legacy_name, owner_name in aliases.items():\n"
            "    legacy = importlib.import_module(legacy_name)\n"
            "    owner = importlib.import_module(owner_name)\n"
            "    if legacy is not owner:\n"
            "        raise AssertionError(f'{legacy_name} is not {owner_name}')\n"
        )

        # ``autoslice.web.app`` 会在导入时创建生产任务后端。兼容身份检查必须
        # 在独立进程和临时数据库中完成，避免单独运行架构测试时污染工作区，
        # 也避免把已导入的临时 TaskStore 留给后续集成测试。
        with tempfile.TemporaryDirectory(prefix="autoslice-alias-test-") as directory:
            environment = os.environ.copy()
            environment["AUTOSLICE_TASK_DB"] = str(
                Path(directory) / "tasks.sqlite3"
            )
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    def test_phase6_facades_keep_every_owner_object_identity(self):
        import topic_engine
        from autoslice import (
            media_probe,
            pipeline,
            reporting,
            slice_encoding,
            slice_reuse,
            slicing,
            timecode,
        )
        from autoslice.analysis import (
            boundaries,
            candidates,
            checkpoints,
            danmaku,
            evidence,
            llm_execution,
            slice_decisions as slice_decisions_compatibility,
            timeline,
            titles as title_compatibility,
        )
        from autoslice.analysis.report import cleanup as report_cleanup
        from autoslice.analysis.report import formatting as topic_formatting
        from autoslice.analysis.manual import workflow as manual_workflow
        from autoslice.analysis.review import context_edges, context_evidence
        from autoslice.analysis.review import context_ranges
        from autoslice.analysis.review import decisions as slice_decisions
        from autoslice.analysis.review import deduplication as clip_deduplication
        from autoslice.analysis.review import finalization
        from autoslice.analysis.review import outro
        from autoslice.analysis.review import policy as clip_policy
        from autoslice.analysis.review import (
            reconciliation as candidate_reconciliation,
        )
        from autoslice.analysis.review import scoring as clip_scoring
        from autoslice.analysis.review import transitions
        from autoslice.analysis.review import triggers as trigger_analysis
        from autoslice.analysis.topic import normalization, response, titles
        from autoslice.transcription import model_runtime as transcription_model_runtime
        from autoslice.transcription import results as transcription_results
        from autoslice.transcription import service as transcription
        from autoslice.transcription import workflow as transcription_workflow

        owners = (
            timecode,
            media_probe,
            transcription_model_runtime,
            transcription_results,
            transcription,
            transcription_workflow,
            danmaku,
            evidence,
            llm_execution,
            response,
            timeline,
            manual_workflow,
            checkpoints,
            clip_scoring,
            normalization,
            context_edges,
            context_evidence,
            context_ranges,
            clip_deduplication,
            finalization,
            outro,
            transitions,
            trigger_analysis,
            boundaries,
            clip_policy,
            candidate_reconciliation,
            report_cleanup,
            topic_formatting,
            slice_decisions,
            candidates,
            titles,
            reporting,
            slice_reuse,
            slice_encoding,
            slicing,
            pipeline,
        )
        self.assertIs(title_compatibility.FACADE_EXPORTS, titles.FACADE_EXPORTS)
        for name, value in vars(titles).items():
            if not name.startswith("__"):
                self.assertIs(getattr(title_compatibility, name), value)
        self.assertIs(
            slice_decisions_compatibility.FACADE_EXPORTS,
            slice_decisions.FACADE_EXPORTS,
        )
        for name, value in vars(slice_decisions).items():
            if not name.startswith("__"):
                self.assertIs(getattr(slice_decisions_compatibility, name), value)
        facade_owners = {}
        for owner in owners:
            for facade_name in owner.FACADE_EXPORTS:
                previous_owner = facade_owners.setdefault(
                    facade_name,
                    owner.__name__,
                )
                self.assertEqual(
                    previous_owner,
                    owner.__name__,
                    msg=(
                        f"topic_engine.{facade_name} 同时由 "
                        f"{previous_owner} 和 {owner.__name__} 声明所有权"
                    ),
                )
        self.assertGreater(
            len(facade_owners),
            450,
        )
        for owner in owners:
            self.assertTrue(owner.FACADE_EXPORTS, owner.__name__)
            for facade_name, implementation_name in owner.FACADE_EXPORTS.items():
                with self.subTest(
                        owner=owner.__name__, facade_name=facade_name):
                    self.assertIs(
                        getattr(topic_engine, facade_name),
                        getattr(owner, implementation_name),
                        msg=(
                            f"topic_engine.{facade_name} 未直接绑定 "
                            f"{owner.__name__}.{implementation_name}"
                        ),
                    )

    def test_candidate_facade_is_definition_free_and_not_imported_by_production(self):
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_module = modules["autoslice.analysis.candidates"]

        self.assertEqual(candidate_module["top_level_functions"], [])
        self.assertEqual(candidate_module["top_level_classes"], [])

        production_src_modules = {
            module["module"]
            for module in current["modules"]
            if module["path"].startswith("src/")
            and module["module"] != "autoslice.analysis.candidates"
        }
        candidate_imports = [
            edge
            for edge in current["import_edges"]
            if edge["from"] in production_src_modules
            and edge["to"] == "autoslice.analysis.candidates"
        ]
        self.assertEqual(candidate_imports, [])

    def test_candidate_facade_consumers_bind_unique_owner_objects(self):
        from autoslice import pipeline, reporting, topic_engine
        from autoslice.analysis import danmaku
        from autoslice.analysis.topic import analysis as topic_analysis
        from autoslice.analysis.topic import normalization, titles
        from autoslice.llm import transport

        bindings = {
            reporting: {
                "_clean_topic_title": titles._clean_topic_title,
                "_filter_unsupported_ai_points": (
                    normalization.filter_unsupported_ai_points
                ),
                "_replace_streamer_role": titles._replace_streamer_role,
                "_normalise_publish_title": titles._normalise_publish_title,
                "LLM_ANALYSIS_MODEL": topic_analysis.LLM_ANALYSIS_MODEL,
            },
            pipeline: {
                "_average_danmaku_density": danmaku._average_danmaku_density,
                "_high_energy_danmaku_peaks": danmaku._high_energy_danmaku_peaks,
            },
            topic_engine: {
                "LLMProviderUnavailableError": transport.LLMProviderUnavailableError,
                "LLMStructuredOutputError": transport.LLMStructuredOutputError,
                "_extract_json_payload": transport.extract_json_payload,
                "_is_retryable_llm_error": transport.is_retryable_llm_error,
                "_short_llm_error": transport.short_llm_error,
            },
        }
        for consumer, aliases in bindings.items():
            for compatibility_name, owner in aliases.items():
                with self.subTest(
                        consumer=consumer.__name__, name=compatibility_name):
                    self.assertIs(getattr(consumer, compatibility_name), owner)

    def test_analysis_checkpoints_have_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline
        from autoslice.analysis import candidates, checkpoints

        aliases = {
            "analysis_topics_snapshot": candidates._analysis_topics_snapshot,
            "clip_review_checkpoint_matches_policy": (
                candidates._clip_review_checkpoint_matches_policy
            ),
            "clip_review_checkpoint_is_complete": (
                candidates._clip_review_checkpoint_is_complete
            ),
            "write_completed_clip_review_checkpoint": (
                candidates._write_completed_clip_review_checkpoint
            ),
        }
        for name, compatibility_alias in aliases.items():
            with self.subTest(name=name):
                owner = getattr(checkpoints, name)
                self.assertIs(compatibility_alias, owner)
                self.assertIs(getattr(pipeline, f"_{name}"), owner)

        self.assertIs(
            candidates.write_clip_review_checkpoint,
            checkpoints.write_clip_review_checkpoint,
        )
        self.assertIs(pipeline.checkpoint_store, checkpoints)
        self.assertIs(
            topic_engine._write_clip_review_checkpoint,
            checkpoints.write_clip_review_checkpoint,
        )
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"]["top_level_functions"]
        }
        self.assertTrue({
            "analysis_topics_snapshot",
            "write_clip_review_checkpoint",
            "clip_review_checkpoint_matches_policy",
            "clip_review_checkpoint_is_complete",
            "write_completed_clip_review_checkpoint",
        }.isdisjoint(candidate_functions))

    def test_clip_scoring_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline
        from autoslice.analysis import candidates
        from autoslice.analysis import clip_scoring as legacy_clip_scoring
        from autoslice.analysis.review import scoring as clip_scoring

        aliases = {
            "_build_clip_candidate_review_audit": (
                "build_clip_candidate_review_audit"
            ),
            "_clip_interest_reason": "clip_interest_reason",
            "_clip_manual_star_count": "clip_manual_star_count",
            "_clip_star_bonus_cap": "clip_star_bonus_cap",
            "_parse_clip_interest_score": "parse_clip_interest_score",
            "_parse_clip_star_bonus": "parse_clip_star_bonus",
        }
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(clip_scoring, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)
        self.assertIs(legacy_clip_scoring.FACADE_EXPORTS, clip_scoring.FACADE_EXPORTS)
        for name, value in vars(clip_scoring).items():
            if not name.startswith("__"):
                self.assertIs(getattr(legacy_clip_scoring, name), value)
        self.assertIs(
            pipeline._build_clip_candidate_review_audit,
            clip_scoring.build_clip_candidate_review_audit,
        )
        self.assertIs(pipeline.clip_scoring, clip_scoring)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.review.scoring"][
                "top_level_functions"
            ]
        }
        self.assertEqual(len(owner_functions), 7)
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.review.scoring"
        legacy_module = "autoslice.analysis.clip_scoring"
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.analysis.review.decisions",
            "autoslice.analysis.review.workflow",
            "autoslice.pipeline",
            "autoslice.topic_engine",
        }
        for consumer in consumers:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        production_modules = {
            module["module"]
            for module in current["modules"]
            if module["path"].startswith("src/")
            and module["module"] != legacy_module
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == legacy_module
            },
            set(),
        )
        for forbidden_target in consumers | {legacy_module}:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

    def test_candidate_evidence_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice.analysis import candidates, evidence

        aliases = {
            "_topic_danmaku_reference_lines": "topic_danmaku_reference_lines",
            "_topic_peak_candidates": "topic_peak_candidates",
            "_topic_srt_summary_lines": "topic_srt_summary_lines",
        }
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(evidence, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.evidence"][
                "top_level_functions"
            ]
        }
        self.assertTrue(owner_functions)
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))

    def test_candidate_llm_execution_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice.analysis import candidates, llm_execution

        aliases = {
            "LLM_DEFAULT_CONCURRENCY": "LLM_DEFAULT_CONCURRENCY",
            "LLM_MAX_CONCURRENCY": "LLM_MAX_CONCURRENCY",
            "_configured_llm_concurrency": "configured_llm_concurrency",
            "_serialized_progress_callback": "serialized_progress_callback",
        }
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(llm_execution, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.llm_execution"][
                "top_level_functions"
            ]
        }
        self.assertTrue(owner_functions)
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))

    def test_topic_response_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice.analysis import candidates
        from autoslice.analysis import response_parsing as legacy_response
        from autoslice.analysis.topic import response

        aliases = {
            "_NO_SLICE_HINTS": "NO_SLICE_HINTS",
            "_is_slice_marked": "is_slice_marked",
            "_json_can_slice": "json_can_slice",
        }
        self.assertEqual(response.FACADE_EXPORTS, aliases)
        self.assertIs(legacy_response.FACADE_EXPORTS, response.FACADE_EXPORTS)
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(response, owner_name)
                self.assertIs(getattr(legacy_response, owner_name), owner)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = "autoslice.analysis.topic.response"
        legacy_module = "autoslice.analysis.response_parsing"
        self.assertEqual(
            modules[legacy_module]["top_level_functions"],
            [],
        )
        self.assertEqual(modules[legacy_module]["top_level_classes"], [])
        self.assertEqual(
            {
                item["name"]
                for item in modules[owner_module]["top_level_functions"]
            },
            {"is_slice_marked", "json_can_slice"},
        )
        self.assertEqual(modules[owner_module]["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.analysis.review.workflow",
            "autoslice.analysis.topic.analysis",
            "autoslice.topic_engine",
        }
        for consumer in consumers:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        for forbidden_target in {
            "autoslice.analysis.candidates",
            "autoslice.analysis.content_normalization",
            legacy_module,
            "autoslice.pipeline",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

        direct_calls = set()
        for path in (
            ROOT / "src/autoslice/analysis/review/workflow.py",
            ROOT / "src/autoslice/analysis/topic/analysis.py",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            direct_calls.update(
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"response", "topic_response"}
            )
        self.assertGreaterEqual(
            direct_calls,
            {"is_slice_marked", "json_can_slice"},
        )

    def test_topic_normalization_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice.analysis import candidates
        from autoslice.analysis import content_normalization as legacy_normalization
        from autoslice.analysis.topic import normalization

        aliases = {
            "_DANMAKU_META_KEYWORDS": "DANMAKU_META_KEYWORDS",
            "_FRAGMENT_BODY_LINES": "FRAGMENT_BODY_LINES",
            "_META_BODY_KEYWORDS": "META_BODY_KEYWORDS",
            "_UNSUPPORTED_AI_AUDIENCE_REACTION_RE": (
                "UNSUPPORTED_AI_AUDIENCE_REACTION_RE"
            ),
            "_clean_body_content": "clean_body_content",
            "_filter_unsupported_ai_points": "filter_unsupported_ai_points",
            "_is_meta_body_line": "is_meta_body_line",
            "_json_points_to_body": "json_points_to_body",
            "_normalise_body_line": "normalise_body_line",
        }
        self.assertEqual(normalization.FACADE_EXPORTS, aliases)
        self.assertIs(
            legacy_normalization.FACADE_EXPORTS,
            normalization.FACADE_EXPORTS,
        )
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(normalization, owner_name)
                self.assertIs(getattr(legacy_normalization, owner_name), owner)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = "autoslice.analysis.topic.normalization"
        legacy_module = "autoslice.analysis.content_normalization"
        self.assertEqual(modules[legacy_module]["top_level_functions"], [])
        self.assertEqual(modules[legacy_module]["top_level_classes"], [])
        owner_functions = {
            item["name"]
            for item in modules[owner_module][
                "top_level_functions"
            ]
        }
        self.assertEqual(
            owner_functions,
            {
                "clean_body_content",
                "filter_unsupported_ai_points",
                "is_meta_body_line",
                "json_points_to_body",
                "normalise_body_line",
            },
        )
        self.assertEqual(modules[owner_module]["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual.enrichment",
            "autoslice.analysis.report.cleanup",
            "autoslice.analysis.topic.analysis",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }
        for consumer in consumers:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        for forbidden_target in {
            "autoslice.analysis.candidates",
            legacy_module,
            "autoslice.analysis.response_parsing",
            "autoslice.pipeline",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

        direct_calls = set()
        for path in (
            ROOT / "src/autoslice/analysis/manual/enrichment.py",
            ROOT / "src/autoslice/analysis/report/cleanup.py",
            ROOT / "src/autoslice/analysis/topic/analysis.py",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            direct_calls.update(
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "normalization"
            )
        self.assertGreaterEqual(
            direct_calls,
            {
                "filter_unsupported_ai_points",
                "json_points_to_body",
                "normalise_body_line",
            },
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_topic_domain_package_has_no_eager_imports(self):
        package_path = ROOT / "src/autoslice/analysis/topic/__init__.py"
        tree = ast.parse(package_path.read_text(encoding="utf-8"))

        self.assertEqual(
            [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ],
            [],
        )
        all_assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        ]
        self.assertEqual(len(all_assignments), 1)
        self.assertEqual(
            ast.literal_eval(all_assignments[0].value),
            [
                "analysis",
                "chunking",
                "normalization",
                "reconciliation",
                "response",
                "titles",
            ],
        )

    def test_manual_enrichment_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice.analysis import candidates
        from autoslice.analysis import manual_enrichment as legacy_enrichment
        from autoslice.analysis.manual import enrichment, review, workflow
        from autoslice.analysis.review import workflow as review_workflow

        aliases = {
            "_MANUAL_AI_PLACEHOLDER_PHRASES": "MANUAL_AI_PLACEHOLDER_PHRASES",
            "_enriched_manual_topic_from_item": "enrich_manual_topic_from_item",
            "_is_manual_ai_placeholder": "is_manual_ai_placeholder",
            "_validated_ai_focus_range": "validated_ai_focus_range",
        }
        self.assertEqual(enrichment.FACADE_EXPORTS, aliases)
        self.assertIs(legacy_enrichment.FACADE_EXPORTS, enrichment.FACADE_EXPORTS)
        for owner_name in aliases.values():
            with self.subTest(legacy=owner_name):
                self.assertIs(
                    getattr(legacy_enrichment, owner_name),
                    getattr(enrichment, owner_name),
                )
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(enrichment, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)

        for consumer in (candidates, review_workflow, review, workflow, topic_engine):
            with self.subTest(consumer=consumer.__name__):
                self.assertIs(consumer.manual_enrichment, enrichment)

        review_source = (
            ROOT / "src/autoslice/analysis/manual/review.py"
        ).read_text(encoding="utf-8")
        workflow_source = (
            ROOT / "src/autoslice/analysis/manual/workflow.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "manual_enrichment.enrich_manual_topic_from_item(",
            review_source,
        )
        self.assertIn(
            "manual_enrichment.is_manual_ai_placeholder(",
            workflow_source,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        self.assertEqual(
            modules["autoslice.analysis.manual_enrichment"]["top_level_functions"],
            [],
        )
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.manual.enrichment"][
                "top_level_functions"
            ]
        }
        self.assertEqual(
            owner_functions,
            {
                "enrich_manual_topic_from_item",
                "is_manual_ai_placeholder",
                "validated_ai_focus_range",
            },
        )
        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.manual.enrichment"
        legacy_module = "autoslice.analysis.manual_enrichment"
        for consumer in {
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual.review",
            "autoslice.analysis.manual.workflow",
            "autoslice.analysis.review.workflow",
            "autoslice.topic_engine",
        }:
            with self.subTest(direct_consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        for forbidden_target in {
            legacy_module,
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual_review",
            "autoslice.analysis.manual_timeline",
            "autoslice.pipeline",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

    def test_clip_review_candidates_have_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice.analysis import candidates
        from autoslice.analysis import (
            clip_review_candidates as legacy_clip_review_candidates,
        )
        from autoslice.analysis.manual import review as manual_review
        from autoslice.analysis.review import candidates as review_candidates
        from autoslice.analysis.review import workflow as clip_review

        aliases = {
            "_clip_review_candidate": "build_clip_review_candidate",
            "_fresh_manual_topic_evidence": "fresh_manual_topic_evidence",
        }
        self.assertEqual(review_candidates.FACADE_EXPORTS, aliases)
        self.assertIs(
            legacy_clip_review_candidates.FACADE_EXPORTS,
            review_candidates.FACADE_EXPORTS,
        )
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(review_candidates, owner_name)
                self.assertIs(
                    getattr(legacy_clip_review_candidates, owner_name),
                    owner,
                )
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)

        self.assertIs(candidates.clip_review_candidates, review_candidates)
        self.assertIs(clip_review.clip_review_candidates, review_candidates)
        self.assertIs(manual_review.clip_review_candidates, review_candidates)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.review.candidates"][
                "top_level_functions"
            ]
        }
        self.assertEqual(
            owner_functions,
            {"build_clip_review_candidate", "fresh_manual_topic_evidence"},
        )
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.review.candidates"
        legacy_module = "autoslice.analysis.clip_review_candidates"
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual.review",
            "autoslice.analysis.review.workflow",
            "autoslice.topic_engine",
        }
        production_modules = {
            module["module"]
            for module in current["modules"]
            if module["path"].startswith("src/")
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == owner_module
            },
            consumers | {legacy_module},
        )
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == legacy_module
            },
            set(),
        )
        for forbidden_target in consumers | {
            legacy_module,
            "autoslice.pipeline",
        }:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

    def test_clip_review_prompt_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice.analysis import candidates
        from autoslice.analysis import (
            clip_review_prompt as legacy_clip_review_prompt,
        )
        from autoslice.analysis.review import prompt as review_prompt
        from autoslice.analysis.review import workflow as clip_review

        aliases = {
            "_build_clip_candidate_review_prompt": (
                "build_clip_candidate_review_prompt"
            ),
        }
        self.assertEqual(review_prompt.FACADE_EXPORTS, aliases)
        self.assertIs(
            legacy_clip_review_prompt.FACADE_EXPORTS,
            review_prompt.FACADE_EXPORTS,
        )
        owner = review_prompt.build_clip_candidate_review_prompt
        self.assertIs(
            legacy_clip_review_prompt.build_clip_candidate_review_prompt,
            owner,
        )
        self.assertIs(candidates._build_clip_candidate_review_prompt, owner)
        self.assertIs(topic_engine._build_clip_candidate_review_prompt, owner)
        self.assertIs(candidates.clip_review_prompt, review_prompt)
        self.assertIs(clip_review.clip_review_prompt, review_prompt)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.review.prompt"][
                "top_level_functions"
            ]
        }
        self.assertEqual(owner_functions, {"build_clip_candidate_review_prompt"})
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.review.prompt"
        legacy_module = "autoslice.analysis.clip_review_prompt"
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.analysis.review.workflow",
            "autoslice.topic_engine",
        }
        production_modules = {
            module["module"]
            for module in current["modules"]
            if module["path"].startswith("src/")
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == owner_module
            },
            consumers | {legacy_module},
        )
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == legacy_module
            },
            set(),
        )
        for forbidden_target in consumers | {
            legacy_module,
            "autoslice.pipeline",
        }:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

    def test_clip_review_orchestration_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline, pipeline_retry_review
        from autoslice.analysis import candidates
        from autoslice.analysis import clip_review as legacy_clip_review
        from autoslice.analysis.review import workflow as clip_review

        owner = clip_review.review_peak_selected_topics
        self.assertIs(legacy_clip_review.FACADE_EXPORTS, clip_review.FACADE_EXPORTS)
        for name, value in vars(clip_review).items():
            if name.startswith("__"):
                continue
            with self.subTest(facade_name=name):
                self.assertIs(getattr(legacy_clip_review, name), value)
        self.assertIs(candidates._review_peak_selected_topics, owner)
        self.assertIs(pipeline._review_peak_selected_topics, owner)
        self.assertIs(topic_engine._review_peak_selected_topics, owner)
        self.assertIs(candidates.clip_review, clip_review)
        self.assertIs(pipeline.clip_review, clip_review)
        self.assertIs(topic_engine.clip_review, clip_review)

        candidate_source = (
            ROOT / "src/autoslice/analysis/candidates.py"
        ).read_text(encoding="utf-8")
        pipeline_source = (ROOT / "src/autoslice/pipeline.py").read_text(
            encoding="utf-8"
        )
        retry_review_source = (
            ROOT / "src/autoslice/pipeline_retry_review.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("\ndef _review_peak_selected_topics(", candidate_source)
        self.assertIn(
            "review_peak_selected_topics(",
            retry_review_source,
        )
        self.assertNotIn("_review_peak_selected_topics(", pipeline_source)
        self.assertEqual(
            pipeline_retry_review.review_retry_candidates_and_titles.__name__,
            "review_retry_candidates_and_titles",
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.review.workflow"][
                "top_level_functions"
            ]
        }
        self.assertEqual(
            owner_functions,
            {
                "_build_review_jobs",
                "_apply_review_response",
                "review_peak_selected_topics",
            },
        )
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))
        self.assertEqual(
            modules["autoslice.analysis.review.workflow"]["top_level_classes"],
            [],
        )
        self.assertEqual(
            modules["autoslice.analysis.clip_review"]["top_level_functions"],
            [],
        )
        self.assertEqual(
            modules["autoslice.analysis.clip_review"]["top_level_classes"],
            [],
        )

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.review.workflow"
        legacy_module = "autoslice.analysis.clip_review"
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.pipeline",
            "autoslice.topic_engine",
        }
        production_modules = {
            module["module"]
            for module in current["modules"]
            if module["path"].startswith("src/")
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == owner_module
            },
            consumers | {legacy_module},
        )
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == legacy_module
            },
            set(),
        )
        self.assertIn((legacy_module, owner_module), import_edges)
        self.assertEqual(
            {
                (source, target)
                for source, target in import_edges
                if source.startswith("autoslice.analysis.review.")
                and target == legacy_module
            },
            set(),
        )
        for forbidden_target in consumers | {legacy_module}:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

    def test_manual_review_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline, pipeline_review
        from autoslice.analysis import candidates
        from autoslice.analysis import manual_review as legacy_review
        from autoslice.analysis.manual import review, workflow

        owner_aliases = {
            "_build_manual_topic_enrichment_prompt": (
                "build_manual_topic_enrichment_prompt"
            ),
            "_enrich_manual_topics_with_llm": "enrich_manual_topics_with_llm",
            "_enrich_manual_topics_in_batches": "enrich_manual_topics_in_batches",
            "_validate_unmatched_manual_topics": (
                "validate_unmatched_manual_topics"
            ),
        }
        self.assertEqual(review.FACADE_EXPORTS, owner_aliases)
        self.assertIs(legacy_review.FACADE_EXPORTS, review.FACADE_EXPORTS)
        for owner_name in owner_aliases.values():
            with self.subTest(legacy=owner_name):
                self.assertIs(
                    getattr(legacy_review, owner_name),
                    getattr(review, owner_name),
                )
        candidate_aliases = dict(owner_aliases)
        candidate_aliases["enrich_manual_topics_with_llm"] = (
            candidate_aliases.pop("_enrich_manual_topics_with_llm")
        )
        candidate_aliases["enrich_manual_topics_in_batches"] = (
            candidate_aliases.pop("_enrich_manual_topics_in_batches")
        )
        for compatibility_name, owner_name in candidate_aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(review, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                topic_engine_name = (
                    compatibility_name
                    if compatibility_name.startswith("_")
                    else f"_{compatibility_name}"
                )
                self.assertIs(getattr(topic_engine, topic_engine_name), owner)

        self.assertIs(
            pipeline._validate_unmatched_manual_topics,
            review.validate_unmatched_manual_topics,
        )
        for consumer in (candidates, workflow, pipeline, topic_engine):
            with self.subTest(consumer=consumer.__name__):
                self.assertIs(consumer.manual_review, review)

        candidate_source = (
            ROOT / "src/autoslice/analysis/candidates.py"
        ).read_text(encoding="utf-8")
        manual_timeline_source = (
            ROOT / "src/autoslice/analysis/manual/workflow.py"
        ).read_text(encoding="utf-8")
        pipeline_source = (ROOT / "src/autoslice/pipeline.py").read_text(
            encoding="utf-8"
        )
        pipeline_review_source = (
            ROOT / "src/autoslice/pipeline_review.py"
        ).read_text(encoding="utf-8")
        for old_definition in (
            "_build_manual_topic_enrichment_prompt",
            "enrich_manual_topics_with_llm",
            "enrich_manual_topics_in_batches",
            "_validate_unmatched_manual_topics",
        ):
            self.assertNotIn(f"\ndef {old_definition}(", candidate_source)
        self.assertIn(
            "manual_review.enrich_manual_topics_with_llm(",
            manual_timeline_source,
        )
        self.assertIn(
            "manual_review.enrich_manual_topics_in_batches(",
            manual_timeline_source,
        )
        self.assertNotIn(
            "manual_review.validate_unmatched_manual_topics(",
            pipeline_source,
        )
        self.assertIn(
            "validate_unmatched_manual_topics(",
            pipeline_review_source,
        )
        self.assertIs(
            pipeline.review_pipeline_candidates,
            pipeline_review.review_pipeline_candidates,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        self.assertEqual(
            modules["autoslice.analysis.manual_review"]["top_level_functions"],
            [],
        )
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.manual.review"][
                "top_level_functions"
            ]
        }
        self.assertEqual(
            owner_functions,
            {
                "build_manual_topic_enrichment_prompt",
                "enrich_manual_topics_in_batches",
                "enrich_manual_topics_with_llm",
                "validate_unmatched_manual_topics",
            },
        )
        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.manual.review"
        legacy_module = "autoslice.analysis.manual_review"
        for consumer in {
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual.workflow",
            "autoslice.pipeline",
            "autoslice.topic_engine",
        }:
            with self.subTest(direct_consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        self.assertIn(
            (owner_module, "autoslice.analysis.manual.enrichment"),
            import_edges,
        )
        self.assertIn(
            (owner_module, "autoslice.analysis.manual.timebase"),
            import_edges,
        )
        for forbidden_target in {
            legacy_module,
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual_enrichment",
            "autoslice.analysis.manual_timeline",
            "autoslice.pipeline",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

    def test_timecode_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline, reporting, timecode
        from autoslice.analysis import candidates, timeline

        self.assertIs(candidates.fmt_time, timecode.format_elapsed)
        self.assertIs(candidates._parse_hms, timecode.parse_hms)
        self.assertIs(timeline.parse_hms, timecode.parse_hms)
        self.assertIs(pipeline.fmt_time, timecode.format_elapsed)
        self.assertIs(reporting.fmt_time, timecode.format_elapsed)
        self.assertIs(reporting._parse_hms, timecode.parse_hms)
        self.assertIs(topic_engine.fmt_time, timecode.format_elapsed)
        self.assertIs(topic_engine._parse_hms, timecode.parse_hms)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_functions = {
            item["name"]
            for item in modules["autoslice.timecode"]["top_level_functions"]
        }
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        timeline_functions = {
            item["name"]
            for item in modules["autoslice.analysis.timeline"][
                "top_level_functions"
            ]
        }
        self.assertEqual(owner_functions, {"format_elapsed", "parse_hms"})
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))
        self.assertTrue(owner_functions.isdisjoint(timeline_functions))

    def test_topic_formatting_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import reporting
        from autoslice.analysis import candidates
        from autoslice.analysis import topic_formatting as legacy_topic_formatting
        from autoslice.analysis.report import formatting as topic_formatting
        from autoslice.analysis.topic import analysis as topic_analysis

        aliases = {
            "_CIRCLED_NUMBERS": "CIRCLED_NUMBERS",
            "_format_report_time": "format_report_time",
            "_format_topic_block": "format_topic_block",
            "_topic_index_label": "topic_index_label",
        }
        self.assertEqual(topic_formatting.FACADE_EXPORTS, aliases)
        self.assertIs(
            legacy_topic_formatting.FACADE_EXPORTS,
            topic_formatting.FACADE_EXPORTS,
        )
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(topic_formatting, owner_name)
                self.assertIs(
                    getattr(legacy_topic_formatting, owner_name),
                    owner,
                )
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)
                self.assertNotIn(
                    compatibility_name,
                    candidates.FACADE_EXPORTS,
                )

        self.assertIs(
            reporting._format_report_time,
            topic_formatting.format_report_time,
        )
        self.assertIs(
            reporting._format_topic_block,
            topic_formatting.format_topic_block,
        )
        self.assertIs(candidates.topic_formatting, topic_formatting)
        self.assertIs(reporting.topic_formatting, topic_formatting)
        self.assertIs(topic_analysis.topic_formatting, topic_formatting)
        self.assertIs(topic_engine.topic_formatting, topic_formatting)

        candidate_source = (
            ROOT / "src/autoslice/analysis/candidates.py"
        ).read_text(encoding="utf-8")
        reporting_source = (ROOT / "src/autoslice/reporting.py").read_text(
            encoding="utf-8"
        )
        topic_analysis_source = (
            ROOT / "src/autoslice/analysis/topic/analysis.py"
        ).read_text(encoding="utf-8")
        for old_definition in (
            "_format_report_time",
            "_format_topic_block",
            "_topic_index_label",
        ):
            self.assertNotIn(f"\ndef {old_definition}(", candidate_source)
        self.assertNotIn("_CIRCLED_NUMBERS = \"①", candidate_source)
        self.assertNotIn(
            "topic_formatting.format_topic_block(",
            candidate_source,
        )
        self.assertIn(
            "topic_formatting.format_topic_block(",
            topic_analysis_source,
        )
        self.assertIn(
            "topic_formatting.format_report_time(",
            reporting_source,
        )
        self.assertIn(
            "topic_formatting.format_topic_block(",
            reporting_source,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        self.assertEqual(
            modules["autoslice.analysis.topic_formatting"][
                "top_level_functions"
            ],
            [],
        )
        owner_functions = {
            item["name"]
            for item in modules[
                "autoslice.analysis.report.formatting"
            ]["top_level_functions"]
        }
        self.assertEqual(
            owner_functions,
            {"format_report_time", "format_topic_block", "topic_index_label"},
        )
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.report.formatting"
        legacy_module = "autoslice.analysis.topic_formatting"
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.analysis.topic.analysis",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }
        for consumer in consumers:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        for forbidden_target in {
            "autoslice.analysis.report_cleanup",
            legacy_module,
            "autoslice.analysis.candidates",
            "autoslice.pipeline",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }:
            self.assertNotIn(
                (owner_module, forbidden_target),
                import_edges,
            )

    def test_topic_analysis_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline, pipeline_llm, reporting
        from autoslice.analysis import candidates
        from autoslice.analysis import topic_analysis as legacy_topic_analysis
        from autoslice.analysis.manual import workflow as manual_workflow
        from autoslice.analysis.topic import analysis as topic_analysis
        from autoslice.analysis.topic import chunking

        aliases = {
            "_HEADING_RE": "HEADING_RE",
            "_analyze_topic_chunks": "analyze_topic_chunks",
            "_build_chunk_prompt": "build_chunk_prompt",
            "_is_topic_in_chunk": "is_topic_in_chunk",
            "_load_topic_analysis_checkpoint": (
                "load_topic_analysis_checkpoint"
            ),
            "_make_fallback_topic_from_chunk": (
                "make_fallback_topic_from_chunk"
            ),
            "_parse_json_topics_response": "parse_json_topics_response",
            "_parse_llm_response": "parse_llm_response",
            "_repair_short_topic_end": "repair_short_topic_end",
            "_strip_code_fence": "strip_code_fence",
            "_strip_prompt_time_labels": "strip_prompt_time_labels",
            "_topic_analysis_prompt_fingerprint": (
                "topic_analysis_prompt_fingerprint"
            ),
            "_write_topic_analysis_checkpoint": (
                "write_topic_analysis_checkpoint"
            ),
        }
        self.assertIs(
            legacy_topic_analysis.FACADE_EXPORTS,
            topic_analysis.FACADE_EXPORTS,
        )
        for name, value in vars(topic_analysis).items():
            if name.startswith("__"):
                continue
            with self.subTest(legacy_name=name):
                self.assertIs(getattr(legacy_topic_analysis, name), value)

        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(topic_analysis, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)

        for constant_name in (
            "CHUNK_SEC",
            "LLM_ANALYSIS_MODEL",
            "LLM_COMPACT_MAX_TOKENS",
            "LLM_COMPACT_TEXT_CHARS",
            "LLM_FULL_TEXT_CHARS",
            "LLM_MAX_TOKENS",
            "MAX_INITIAL_FAILED_CHUNKS",
        ):
            with self.subTest(constant=constant_name):
                owner = getattr(topic_analysis, constant_name)
                self.assertIs(getattr(candidates, constant_name), owner)
                self.assertIs(getattr(topic_engine, constant_name), owner)

        self.assertIs(chunking.topic_analysis, topic_analysis)
        self.assertIs(candidates.topic_analysis, topic_analysis)
        self.assertIs(manual_workflow.topic_analysis, topic_analysis)
        self.assertIs(pipeline.topic_analysis, topic_analysis)
        self.assertIs(reporting.topic_analysis, topic_analysis)
        self.assertIs(topic_engine.topic_analysis, topic_analysis)
        self.assertIs(
            pipeline.LLM_ANALYSIS_MODEL,
            topic_analysis.LLM_ANALYSIS_MODEL,
        )

        candidate_source = (
            ROOT / "src/autoslice/analysis/candidates.py"
        ).read_text(encoding="utf-8")
        pipeline_llm_source = (
            ROOT / "src/autoslice/pipeline_llm.py"
        ).read_text(encoding="utf-8")
        topic_engine_source = (
            ROOT / "src/autoslice/topic_engine.py"
        ).read_text(encoding="utf-8")
        for old_definition in (
            "_repair_short_topic_end",
            "_build_chunk_prompt",
            "_strip_code_fence",
            "_is_topic_in_chunk",
            "_parse_json_topics_response",
            "_parse_llm_response",
            "_strip_prompt_time_labels",
            "_make_fallback_topic_from_chunk",
            "analyze_topic_chunks",
        ):
            self.assertNotIn(f"\ndef {old_definition}(", candidate_source)
        self.assertNotIn("_HEADING_RE = re.compile(", candidate_source)
        self.assertIs(pipeline.topic_analysis, topic_analysis)
        self.assertIs(
            pipeline.analyze_pipeline_llm_chunks,
            pipeline_llm.analyze_pipeline_llm_chunks,
        )
        self.assertIn("analyze_topic_chunks(", pipeline_llm_source)
        self.assertIn(
            "_analyze_topic_chunks = topic_analysis.analyze_topic_chunks",
            topic_engine_source,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.topic.analysis"][
                "top_level_functions"
            ]
        }
        self.assertEqual(
            modules["autoslice.analysis.topic_analysis"]["top_level_functions"],
            [],
        )
        self.assertEqual(
            modules["autoslice.analysis.topic_analysis"]["top_level_classes"],
            [],
        )
        self.assertEqual(
            owner_functions,
            {
                "analyze_topic_chunks",
                "build_chunk_prompt",
                "is_topic_in_chunk",
                "make_fallback_topic_from_chunk",
                "parse_json_topics_response",
                "parse_llm_response",
                "repair_short_topic_end",
                "strip_code_fence",
                "strip_prompt_time_labels",
            },
        )
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.topic.analysis"
        legacy_module = "autoslice.analysis.topic_analysis"
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.analysis.topic.chunking",
            "autoslice.analysis.manual.workflow",
            "autoslice.pipeline",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }
        for consumer in consumers:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        for forbidden_target in consumers | {legacy_module}:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_analysis_chunking_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline
        from autoslice.analysis import candidates
        from autoslice.analysis import chunking as legacy_chunking
        from autoslice.analysis.topic import chunking

        aliases = {
            "_make_chunk": "make_chunk",
            "chunk_srt": "chunk_srt",
            "parse_srt_text": "parse_srt_text",
        }
        self.assertEqual(chunking.FACADE_EXPORTS, aliases)
        self.assertIs(
            legacy_chunking.FACADE_EXPORTS,
            chunking.FACADE_EXPORTS,
        )
        for name, value in vars(chunking).items():
            if name.startswith("__"):
                continue
            with self.subTest(legacy_name=name):
                self.assertIs(getattr(legacy_chunking, name), value)

        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(chunking, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)

        self.assertIs(candidates.chunking, chunking)
        self.assertIs(pipeline.analysis_chunking, chunking)
        self.assertIs(topic_engine.analysis_chunking, chunking)
        self.assertIs(pipeline.parse_srt_text, chunking.parse_srt_text)
        self.assertIs(pipeline.chunk_srt, chunking.chunk_srt)

        candidate_source = (
            ROOT / "src/autoslice/analysis/candidates.py"
        ).read_text(encoding="utf-8")
        pipeline_source = (ROOT / "src/autoslice/pipeline.py").read_text(
            encoding="utf-8"
        )
        topic_engine_source = (
            ROOT / "src/autoslice/topic_engine.py"
        ).read_text(encoding="utf-8")
        for old_definition in ("parse_srt_text", "chunk_srt", "_make_chunk"):
            self.assertNotIn(f"\ndef {old_definition}(", candidate_source)
        self.assertIn(
            "parse_srt_text = analysis_chunking.parse_srt_text",
            pipeline_source,
        )
        self.assertIn(
            "chunk_srt = analysis_chunking.chunk_srt",
            pipeline_source,
        )
        self.assertIn(
            "_make_chunk = analysis_chunking.make_chunk",
            topic_engine_source,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"][
                "top_level_functions"
            ]
        }
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.topic.chunking"][
                "top_level_functions"
            ]
        }
        self.assertEqual(
            modules["autoslice.analysis.chunking"]["top_level_functions"],
            [],
        )
        self.assertEqual(
            modules["autoslice.analysis.chunking"]["top_level_classes"],
            [],
        )
        self.assertEqual(
            owner_functions,
            {"chunk_srt", "make_chunk", "parse_srt_text"},
        )
        self.assertEqual(
            modules["autoslice.analysis.topic.chunking"]["top_level_classes"],
            [],
        )
        self.assertTrue(owner_functions.isdisjoint(candidate_functions))

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.topic.chunking"
        legacy_module = "autoslice.analysis.chunking"
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.pipeline",
            "autoslice.topic_engine",
        }
        for consumer in consumers:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)

        production_modules = {
            module["module"]
            for module in current["modules"]
            if module["path"].startswith("src/")
            and module["module"] != legacy_module
        }
        legacy_importers = {
            source
            for source, target in import_edges
            if source in production_modules and target == legacy_module
        }
        self.assertEqual(legacy_importers, set())
        for forbidden_target in consumers | {legacy_module}:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_manual_domain_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline
        from autoslice.analysis import (
            candidates,
            manual_candidates as legacy_manual_candidates,
            timeline as legacy_timeline,
        )
        from autoslice.analysis.manual import candidates as manual_candidates
        from autoslice.analysis.manual import review as manual_review
        from autoslice.analysis.manual import timebase
        from autoslice.analysis.manual import workflow as manual_timeline
        from autoslice.analysis.report import cleanup as report_cleanup
        from autoslice.analysis.review import policy as clip_policy
        from autoslice.analysis.review import (
            reconciliation as candidate_reconciliation,
        )

        timebase_aliases = {
            "MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE": (
                "MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE"
            ),
            "MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC": (
                "MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC"
            ),
            "MANUAL_TIMELINE_ALIGNMENT_STEP_SEC": (
                "MANUAL_TIMELINE_ALIGNMENT_STEP_SEC"
            ),
            "MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC": (
                "MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC"
            ),
            "MANUAL_TIMELINE_CHUNK_MARGIN_SEC": (
                "MANUAL_TIMELINE_CHUNK_MARGIN_SEC"
            ),
            "MANUAL_TIMELINE_DIR": "MANUAL_TIMELINE_DIR",
            "MANUAL_TIMELINE_END_MARGIN_SEC": (
                "MANUAL_TIMELINE_END_MARGIN_SEC"
            ),
            "MANUAL_TIMELINE_GROUNDING_MIN_SCORE": (
                "MANUAL_TIMELINE_GROUNDING_MIN_SCORE"
            ),
            "MANUAL_TIMELINE_OPTIMIZATION_VERSION": (
                "MANUAL_TIMELINE_OPTIMIZATION_VERSION"
            ),
            "MANUAL_TIMELINE_OPTIMIZE_GAP_SEC": (
                "MANUAL_TIMELINE_OPTIMIZE_GAP_SEC"
            ),
            "MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC": (
                "MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC"
            ),
            "_MANUAL_SEMANTIC_BIGRAM_STOPWORDS": (
                "_MANUAL_SEMANTIC_BIGRAM_STOPWORDS"
            ),
            "_MANUAL_SEMANTIC_GENERIC_TERMS": (
                "_MANUAL_SEMANTIC_GENERIC_TERMS"
            ),
            "_extract_video_start_datetime": "extract_video_start_datetime",
            "_manual_timeline_doc_candidates": (
                "manual_timeline_doc_candidates"
            ),
            "_find_manual_timeline_doc": "find_manual_timeline_doc",
            "_read_docx_lines": "read_docx_lines",
            "_parse_manual_timeline_lines": "parse_manual_timeline_lines",
            "_parse_elapsed_timeline_report_lines": (
                "parse_elapsed_timeline_report_lines"
            ),
            "_filter_manual_timeline_entries": (
                "filter_manual_timeline_entries"
            ),
            "load_manual_timeline": "load_manual_timeline",
            "_manual_timeline_summary": "manual_timeline_summary",
            "_manual_alignment_text": "manual_alignment_text",
            "_manual_semantic_core": "manual_semantic_core",
            "_srt_alignment_windows": "srt_alignment_windows",
            "_align_manual_timeline_entries_to_srt": (
                "align_manual_timeline_entries_to_srt"
            ),
            "MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE": (
                "MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE"
            ),
            "MANUAL_TIMELINE_TOPIC_POST_SEC": (
                "MANUAL_TIMELINE_TOPIC_POST_SEC"
            ),
            "MANUAL_TIMELINE_TOPIC_PRE_SEC": (
                "MANUAL_TIMELINE_TOPIC_PRE_SEC"
            ),
            "_manual_alignment_score": "manual_alignment_score",
            "_manual_text_supports_candidate": (
                "manual_text_supports_candidate"
            ),
        }
        self.assertEqual(timebase.FACADE_EXPORTS, timebase_aliases)
        self.assertIs(legacy_timeline.FACADE_EXPORTS, timebase.FACADE_EXPORTS)
        for compatibility_name, owner_name in timebase_aliases.items():
            with self.subTest(topic_engine_timebase=compatibility_name):
                self.assertIs(
                    getattr(topic_engine, compatibility_name),
                    getattr(timebase, owner_name),
                )

        timebase_same_name_compatibility = {
            "TIMELINE_DIR",
            "MANUAL_TIMELINE_DIR",
            "MANUAL_TIMELINE_CHUNK_MARGIN_SEC",
            "MANUAL_TIMELINE_TOPIC_PRE_SEC",
            "MANUAL_TIMELINE_TOPIC_POST_SEC",
            "MANUAL_TIMELINE_END_MARGIN_SEC",
            "MANUAL_TIMELINE_OPTIMIZE_GAP_SEC",
            "MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC",
            "MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE",
            "MANUAL_TIMELINE_OPTIMIZATION_VERSION",
            "MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC",
            "MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC",
            "MANUAL_TIMELINE_ALIGNMENT_STEP_SEC",
            "MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE",
            "MANUAL_TIMELINE_GROUNDING_MIN_SCORE",
            "parse_hms",
            "_TIMELINE_ACCOUNT_PREFIX_RE",
            "_MANUAL_SEMANTIC_GENERIC_TERMS",
            "_MANUAL_SEMANTIC_BIGRAM_STOPWORDS",
            "read_docx_lines",
            "load_manual_timeline",
        }
        for name in timebase_same_name_compatibility:
            with self.subTest(legacy_timebase=name):
                self.assertIs(getattr(legacy_timeline, name), getattr(timebase, name))

        legacy_timebase_functions = {
            compatibility_name: owner_name
            for compatibility_name, owner_name in timebase_aliases.items()
            if owner_name
            in {
                "extract_video_start_datetime",
                "manual_timeline_doc_candidates",
                "find_manual_timeline_doc",
                "parse_manual_timeline_lines",
                "parse_elapsed_timeline_report_lines",
                "filter_manual_timeline_entries",
                "manual_timeline_summary",
                "manual_alignment_text",
                "manual_alignment_score",
                "manual_semantic_core",
                "manual_text_supports_candidate",
                "srt_alignment_windows",
                "align_manual_timeline_entries_to_srt",
            }
        }
        legacy_timebase_functions["read_docx_lines"] = "read_docx_lines"
        legacy_timebase_functions["load_manual_timeline"] = "load_manual_timeline"
        for compatibility_name, owner_name in legacy_timebase_functions.items():
            with self.subTest(legacy_timebase_function=compatibility_name):
                self.assertIs(
                    getattr(legacy_timeline, compatibility_name),
                    getattr(timebase, owner_name),
                )

        manual_candidate_aliases = {
            "_is_manual_merge_target": "is_manual_merge_target",
            "_manual_entry_matches_topic": "manual_entry_matches_topic",
            "_manual_evidence_line": "manual_evidence_line",
            "_merge_manual_timeline_topics": "merge_manual_timeline_topics",
            "_optimized_entry_semantic_text": "optimized_entry_semantic_text",
            "_sanitize_optimized_manual_entry": (
                "sanitize_optimized_manual_entry"
            ),
            "_topics_from_manual_timeline": "topics_from_manual_timeline",
        }
        self.assertEqual(manual_candidates.FACADE_EXPORTS, manual_candidate_aliases)
        self.assertIs(
            legacy_manual_candidates.FACADE_EXPORTS,
            manual_candidates.FACADE_EXPORTS,
        )
        for owner_name in manual_candidate_aliases.values():
            with self.subTest(legacy_manual_candidates=owner_name):
                self.assertIs(
                    getattr(legacy_manual_candidates, owner_name),
                    getattr(manual_candidates, owner_name),
                )

        candidate_aliases = dict(manual_candidate_aliases)
        candidate_aliases["merge_manual_timeline_topics"] = (
            candidate_aliases.pop("_merge_manual_timeline_topics")
        )
        for compatibility_name, owner_name in candidate_aliases.items():
            with self.subTest(candidate=compatibility_name):
                owner = getattr(manual_candidates, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)

        topic_engine_aliases = dict(manual_candidate_aliases)
        for compatibility_name, owner_name in topic_engine_aliases.items():
            with self.subTest(topic_engine_candidate=compatibility_name):
                self.assertIs(
                    getattr(topic_engine, compatibility_name),
                    getattr(manual_candidates, owner_name),
                )

        candidate_consumers = (
            candidate_reconciliation,
            candidates,
            manual_timeline,
            pipeline,
            topic_engine,
        )
        for consumer in candidate_consumers:
            with self.subTest(candidate_consumer=consumer.__name__):
                self.assertIs(consumer.manual_candidates, manual_candidates)
        timebase_consumers = (
            candidate_reconciliation,
            candidates,
            manual_review,
            manual_timeline,
            report_cleanup,
            pipeline,
            topic_engine,
        )
        for consumer in timebase_consumers:
            with self.subTest(timebase_consumer=consumer.__name__):
                self.assertIs(consumer.timeline_analysis, timebase)

        self.assertIs(
            candidates._UNCUTTABLE_CONTENT_KEYWORDS,
            clip_policy.UNCUTTABLE_CONTENT_KEYWORDS,
        )
        self.assertIs(
            topic_engine._UNCUTTABLE_CONTENT_KEYWORDS,
            clip_policy.UNCUTTABLE_CONTENT_KEYWORDS,
        )
        self.assertIs(
            pipeline._sanitize_optimized_manual_entry,
            manual_candidates.sanitize_optimized_manual_entry,
        )

        manual_package_tree = ast.parse(
            (ROOT / "src/autoslice/analysis/manual/__init__.py").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(
            any(
                isinstance(node, (ast.Import, ast.ImportFrom))
                for node in manual_package_tree.body
            )
        )
        self.assertEqual(
            {
                node.value
                for node in ast.walk(manual_package_tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            },
            {
                "人工时间轴的时间基准、候选、富化、复核与优化工作流。",
                "artifacts",
                "candidates",
                "enrichment",
                "review",
                "timebase",
                "workflow",
            },
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        self.assertEqual(
            modules["autoslice.analysis.timeline"]["top_level_functions"],
            [],
        )
        self.assertEqual(
            modules["autoslice.analysis.manual_candidates"][
                "top_level_functions"
            ],
            [],
        )
        timebase_functions = {
            item["name"]
            for item in modules["autoslice.analysis.manual.timebase"][
                "top_level_functions"
            ]
        }
        self.assertEqual(
            timebase_functions,
            {
                "extract_video_start_datetime",
                "manual_timeline_doc_candidates",
                "find_manual_timeline_doc",
                "read_docx_lines",
                "parse_manual_timeline_lines",
                "parse_elapsed_timeline_report_lines",
                "filter_manual_timeline_entries",
                "load_manual_timeline",
                "manual_timeline_summary",
                "manual_alignment_text",
                "manual_alignment_score",
                "manual_semantic_core",
                "manual_text_supports_candidate",
                "srt_alignment_windows",
                "align_manual_timeline_entries_to_srt",
            },
        )
        self.assertTrue(all(not name.startswith("_") for name in timebase_functions))
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.manual.candidates"][
                "top_level_functions"
            ]
        }
        self.assertEqual(candidate_functions, set(manual_candidate_aliases.values()))

        legacy_private_timebase_functions = {
            "_extract_video_start_datetime",
            "_manual_timeline_doc_candidates",
            "_find_manual_timeline_doc",
            "_parse_manual_timeline_lines",
            "_parse_elapsed_timeline_report_lines",
            "_filter_manual_timeline_entries",
            "_manual_timeline_summary",
            "_manual_alignment_text",
            "_manual_alignment_score",
            "_manual_semantic_core",
            "_manual_text_supports_candidate",
            "_srt_alignment_windows",
            "_align_manual_timeline_entries_to_srt",
        }
        expected_public_timebase_calls = {
            "src/autoslice/analysis/manual/candidates.py": {
                "manual_text_supports_candidate",
            },
            "src/autoslice/analysis/review/reconciliation.py": {
                "manual_alignment_score",
                "manual_text_supports_candidate",
            },
            "src/autoslice/analysis/manual/workflow.py": {
                "align_manual_timeline_entries_to_srt",
            },
            "src/autoslice/analysis/report/cleanup.py": {
                "manual_alignment_score",
            },
            "src/autoslice/analysis/manual/artifacts.py": {
                "extract_video_start_datetime",
            },
            "src/autoslice/pipeline.py": {
                "filter_manual_timeline_entries",
                "manual_timeline_summary",
            },
        }
        for relative_path, expected_calls in expected_public_timebase_calls.items():
            consumer_tree = ast.parse(
                (ROOT / relative_path).read_text(encoding="utf-8")
            )
            private_calls = {
                node.func.attr
                for node in ast.walk(consumer_tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in legacy_private_timebase_functions
            } | {
                node.func.id
                for node in ast.walk(consumer_tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in legacy_private_timebase_functions
            }
            direct_owner_calls = {
                node.func.attr
                for node in ast.walk(consumer_tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "timeline_analysis"
            }
            with self.subTest(public_timebase_consumer=relative_path):
                self.assertEqual(private_calls, set())
                self.assertGreaterEqual(direct_owner_calls, expected_calls)

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        timebase_owner = "autoslice.analysis.manual.timebase"
        candidate_owner = "autoslice.analysis.manual.candidates"
        legacy_timebase = "autoslice.analysis.timeline"
        legacy_candidates = "autoslice.analysis.manual_candidates"
        timebase_consumer_names = {
            "autoslice.analysis.manual.artifacts",
            "autoslice.analysis.review.reconciliation",
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual.review",
            "autoslice.analysis.manual.workflow",
            "autoslice.analysis.report.cleanup",
            "autoslice.pipeline",
            "autoslice.topic_engine",
        }
        candidate_consumer_names = {
            "autoslice.analysis.manual.artifacts",
            "autoslice.analysis.review.reconciliation",
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual.workflow",
            "autoslice.pipeline",
            "autoslice.topic_engine",
        }
        for consumer in timebase_consumer_names:
            with self.subTest(timebase_import=consumer):
                self.assertIn((consumer, timebase_owner), import_edges)
                self.assertNotIn((consumer, legacy_timebase), import_edges)
        for consumer in candidate_consumer_names:
            with self.subTest(candidate_import=consumer):
                self.assertIn((consumer, candidate_owner), import_edges)
                self.assertNotIn((consumer, legacy_candidates), import_edges)
        self.assertIn((candidate_owner, timebase_owner), import_edges)
        self.assertIn((legacy_timebase, timebase_owner), import_edges)
        self.assertIn((legacy_candidates, candidate_owner), import_edges)

        forbidden_owner_targets = {
            legacy_timebase,
            legacy_candidates,
            "autoslice.analysis.candidates",
            "autoslice.pipeline",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }
        for owner_module in (timebase_owner, candidate_owner):
            for forbidden_target in forbidden_owner_targets:
                with self.subTest(
                    owner=owner_module,
                    forbidden_target=forbidden_target,
                ):
                    self.assertNotIn(
                        (owner_module, forbidden_target),
                        import_edges,
                    )

        self.assertEqual(current["summary"]["top_level_function_count"], 969)
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_manual_timeline_optimization_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline
        from autoslice.analysis import manual_timeline as legacy_workflow
        from autoslice.analysis.manual import workflow

        pipeline_aliases = {
            "_format_manual_entry_for_prompt": "format_manual_entry_for_prompt",
            "_manual_timeline_info_for_chunk": "manual_timeline_info_for_chunk",
            "attach_manual_timeline_to_chunks": "attach_manual_timeline_to_chunks",
            "try_enrich_manual_topics": "try_enrich_manual_topics",
            "optimized_manual_entries_from_topics": (
                "optimized_manual_entries_from_topics"
            ),
            "optimized_entry_needs_retry": "optimized_entry_needs_retry",
            "topic_from_optimized_entry": "topic_from_optimized_entry",
            "_batch_warning_text": "batch_warning_text",
            "retry_optimized_timeline_entries": "retry_optimized_timeline_entries",
            "optimize_manual_timeline": "optimize_manual_timeline",
        }
        workflow_aliases = {
            "_attach_manual_timeline_to_chunks": "attach_manual_timeline_to_chunks",
            "_batch_warning_text": "batch_warning_text",
            "_format_manual_entry_for_prompt": "format_manual_entry_for_prompt",
            "_manual_timeline_info_for_chunk": "manual_timeline_info_for_chunk",
            "_optimize_manual_timeline": "optimize_manual_timeline",
            "_optimized_entry_needs_retry": "optimized_entry_needs_retry",
            "_optimized_manual_entries_from_topics": (
                "optimized_manual_entries_from_topics"
            ),
            "_retry_optimized_timeline_entries": (
                "retry_optimized_timeline_entries"
            ),
            "_topic_from_optimized_entry": "topic_from_optimized_entry",
            "_try_enrich_manual_topics": "try_enrich_manual_topics",
        }
        self.assertEqual(workflow.FACADE_EXPORTS, workflow_aliases)
        self.assertIs(legacy_workflow.FACADE_EXPORTS, workflow.FACADE_EXPORTS)
        for owner_name in workflow.FACADE_EXPORTS.values():
            with self.subTest(legacy=owner_name):
                self.assertIs(
                    getattr(legacy_workflow, owner_name),
                    getattr(workflow, owner_name),
                )
        for compatibility_name, owner_name in pipeline_aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(workflow, owner_name)
                self.assertIs(getattr(pipeline, compatibility_name), owner)

        for facade_name, owner_name in workflow.FACADE_EXPORTS.items():
            with self.subTest(facade=facade_name):
                self.assertIs(
                    getattr(topic_engine, facade_name),
                    getattr(workflow, owner_name),
                )

        self.assertIs(pipeline.manual_timeline_analysis, workflow)
        self.assertIs(topic_engine.manual_timeline_analysis, workflow)
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        self.assertEqual(
            modules["autoslice.analysis.manual_timeline"]["top_level_functions"],
            [],
        )
        owner_functions = {
            item["name"]
            for item in modules["autoslice.analysis.manual.workflow"][
                "top_level_functions"
            ]
        }
        self.assertEqual(owner_functions, set(workflow.FACADE_EXPORTS.values()))
        pipeline_functions = {
            item["name"]
            for item in modules["autoslice.pipeline"]["top_level_functions"]
        }
        self.assertTrue(
            set(workflow.FACADE_EXPORTS.values()).isdisjoint(
                pipeline_functions
            )
        )
        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.manual.workflow"
        legacy_module = "autoslice.analysis.manual_timeline"
        for consumer in {"autoslice.pipeline", "autoslice.topic_engine"}:
            with self.subTest(direct_consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        for dependency in {
            "autoslice.analysis.manual.candidates",
            "autoslice.analysis.manual.enrichment",
            "autoslice.analysis.manual.review",
            "autoslice.analysis.manual.timebase",
        }:
            self.assertIn((owner_module, dependency), import_edges)
        for forbidden_target in {
            legacy_module,
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual_enrichment",
            "autoslice.analysis.manual_review",
            "autoslice.pipeline",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

    def test_manual_artifacts_have_one_owner_and_preserve_pipeline_patch_seam(self):
        from autoslice import pipeline, topic_engine
        from autoslice.analysis import manual as manual_package
        from autoslice.analysis.manual import artifacts

        aliases = {
            "optimized_timeline_paths": "_optimized_timeline_paths",
            "write_optimized_timeline_files": "_write_optimized_timeline_files",
            "load_optimized_timeline_artifact": (
                "_load_optimized_timeline_artifact"
            ),
        }
        owner_aliases = {
            **aliases,
            "manual_timeline_for_rebuilt_report": (
                "_manual_timeline_for_rebuilt_report"
            ),
        }
        self.assertEqual(
            artifacts.FACADE_EXPORTS,
            {
                topic_engine_name: owner_name
                for owner_name, topic_engine_name in owner_aliases.items()
            },
        )
        for owner_name, topic_engine_name in aliases.items():
            with self.subTest(owner=owner_name):
                owner = getattr(artifacts, owner_name)
                self.assertIs(getattr(pipeline, owner_name), owner)
                self.assertIs(getattr(topic_engine, topic_engine_name), owner)

        rebuilt_report_owner = artifacts.manual_timeline_for_rebuilt_report
        self.assertIs(
            pipeline._manual_timeline_for_rebuilt_report,
            rebuilt_report_owner,
        )
        self.assertIs(
            topic_engine._manual_timeline_for_rebuilt_report,
            rebuilt_report_owner,
        )

        self.assertEqual(manual_package.__all__, sorted(manual_package.__all__))
        self.assertIn("artifacts", manual_package.__all__)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = "autoslice.analysis.manual.artifacts"
        self.assertEqual(
            [
                item["name"]
                for item in modules[owner_module]["top_level_functions"]
            ],
            list(owner_aliases),
        )
        pipeline_functions = {
            item["name"]
            for item in modules["autoslice.pipeline"]["top_level_functions"]
        }
        self.assertTrue(set(aliases).isdisjoint(pipeline_functions))
        self.assertNotIn("_manual_timeline_for_rebuilt_report", pipeline_functions)
        self.assertNotIn("manual_timeline_for_rebuilt_report", pipeline_functions)
        self.assertIn("prepare_optimized_manual_timeline", pipeline_functions)

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if target == owner_module
            },
            {"autoslice.pipeline", "autoslice.topic_engine"},
        )
        self.assertEqual(
            {
                target
                for source, target in import_edges
                if source == owner_module
            },
            {
                "autoslice.analysis.manual.candidates",
                "autoslice.analysis.manual.timebase",
                "autoslice.streamer_profiles",
                "autoslice.timecode",
            },
        )
        self.assertNotIn((owner_module, "autoslice.pipeline"), import_edges)
        self.assertNotIn((owner_module, "autoslice.topic_engine"), import_edges)

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "manual_timeline_for_rebuilt_report"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "manual_artifacts"
                and node.value.attr == "manual_timeline_for_rebuilt_report"
                for node in pipeline_tree.body
            )
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "_manual_timeline_for_rebuilt_report"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Name)
                and node.value.id == "manual_timeline_for_rebuilt_report"
                for node in pipeline_tree.body
            )
        )
        topic_tree = ast.parse(
            (ROOT / "src/autoslice/topic_engine.py").read_text(encoding="utf-8")
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "_manual_timeline_for_rebuilt_report"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "manual_artifacts"
                and node.value.attr == "manual_timeline_for_rebuilt_report"
                for node in topic_tree.body
            )
        )
        retry_impl = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "retry_clip_review_from_artifacts_impl"
        )
        retry_global_calls = {
            node.func.id
            for node in ast.walk(retry_impl)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertNotIn("_manual_timeline_for_rebuilt_report", retry_global_calls)
        retry_owner_call = next(
            node
            for node in ast.walk(retry_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "prepare_retry_pipeline_state"
        )
        self.assertIn(
            "manual_timeline_for_rebuilt_report",
            {keyword.arg for keyword in retry_owner_call.keywords},
        )

        prepare = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "prepare_optimized_manual_timeline"
        )
        global_calls = {
            node.func.id
            for node in ast.walk(prepare)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(set(aliases).issubset(global_calls))

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_media_probe_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import media_probe, pipeline, slice_reuse, slicing
        from autoslice.transcription import service

        owner = media_probe.probe_video_duration
        self.assertIs(service.probe_video_duration, owner)
        self.assertIs(pipeline.probe_video_duration, owner)
        self.assertIs(slice_reuse.probe_video_duration, owner)
        self.assertIs(slicing.probe_video_duration, owner)
        self.assertIs(topic_engine._probe_video_duration, owner)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_functions = {
            item["name"]
            for item in modules["autoslice.media_probe"]["top_level_functions"]
        }
        self.assertEqual(owner_functions, {"probe_video_duration"})
        for module_name in (
            "autoslice.pipeline",
            "autoslice.slice_reuse",
            "autoslice.slicing",
            "autoslice.transcription.service",
        ):
            functions = {
                item["name"]
                for item in modules[module_name]["top_level_functions"]
            }
            self.assertTrue(owner_functions.isdisjoint(functions))

    def test_funasr_results_have_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice.transcription import results, service

        aliases = {
            "_normalise_funasr_result": "normalise_funasr_result",
            "_is_valid_funasr_result": "is_valid_funasr_result",
            "_primary_speaker_segments": "primary_speaker_segments",
        }
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(results, owner_name)
                self.assertIs(getattr(service, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)

        self.assertIs(service.result_contracts, results)
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        service_functions = {
            item["name"]
            for item in modules["autoslice.transcription.service"][
                "top_level_functions"
            ]
        }
        self.assertTrue(
            set(results.FACADE_EXPORTS.values()).isdisjoint(service_functions)
        )

    def test_funasr_model_runtime_has_one_owner_and_direct_service_consumer(self):
        import topic_engine
        from autoslice.transcription import model_runtime, service

        self.assertIs(service.model_runtime, model_runtime)
        aliases = {
            "_prepare_funasr_environment": "prepare_funasr_environment",
            "funasr_model_cache_candidates": "funasr_model_cache_candidates",
            "_funasr_nano_cache_candidates": "funasr_nano_cache_candidates",
            "resolve_funasr_model_source": "resolve_funasr_model_source",
            "resolve_funasr_aux_model_source": "resolve_funasr_aux_model_source",
            "resolve_funasr_speaker_model_source": (
                "resolve_funasr_speaker_model_source"
            ),
            "_funasr_hotwords": "funasr_hotwords",
            "_funasr_generate_kwargs": "funasr_generate_kwargs",
            "resolve_funasr_device": "resolve_funasr_device",
            "funasr_public_status": "funasr_public_status",
            "load_funasr_model": "load_funasr_model",
            "clear_funasr_cuda_cache": "clear_funasr_cuda_cache",
        }
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                self.assertIs(
                    getattr(service, compatibility_name),
                    getattr(model_runtime, owner_name),
                )

        for facade_name, owner_name in model_runtime.FACADE_EXPORTS.items():
            with self.subTest(facade=facade_name):
                self.assertIs(
                    getattr(topic_engine, facade_name),
                    getattr(model_runtime, owner_name),
                )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        service_functions = {
            item["name"]
            for item in modules["autoslice.transcription.service"][
                "top_level_functions"
            ]
        }
        self.assertTrue(
            {
                "prepare_funasr_environment",
                "funasr_model_cache_candidates",
                "funasr_nano_cache_candidates",
                "resolve_funasr_model_source",
                "resolve_funasr_aux_model_source",
                "resolve_funasr_speaker_model_source",
                "funasr_hotwords",
                "funasr_generate_kwargs",
                "resolve_funasr_device",
                "funasr_public_status",
                "load_funasr_model",
                "clear_funasr_cuda_cache",
            }.isdisjoint(service_functions)
        )

    def test_background_filter_has_one_owner_and_safe_dependency_direction(self):
        from autoslice import subtitle_workflow
        from autoslice.transcription import (
            background_filter,
            checkpoints,
            model_runtime,
            recognition,
            workflow,
        )
        from autoslice.web import app as web_app

        self.assertIs(model_runtime.background_filter, background_filter)
        self.assertIs(checkpoints.background_filter, background_filter)
        self.assertIs(recognition.background_filter, background_filter)
        self.assertIs(workflow.background_filter, background_filter)
        self.assertIs(
            subtitle_workflow.background_filter_contract,
            background_filter,
        )
        self.assertIs(web_app.background_filter, background_filter)

        current = architecture_snapshot.build_snapshot(ROOT)
        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        owner_module = "autoslice.transcription.background_filter"
        direct_consumers = {
            "autoslice.subtitle_workflow",
            "autoslice.transcription.checkpoints",
            "autoslice.transcription.model_runtime",
            "autoslice.transcription.recognition",
            "autoslice.transcription.workflow",
            "autoslice.web.app",
        }
        for consumer in direct_consumers:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((owner_module, consumer), import_edges)

    def test_funasr_checkpoints_have_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline
        from autoslice.transcription import checkpoints, service

        aliases = {
            "FUNASR_CHECKPOINT_VERSION": "FUNASR_CHECKPOINT_VERSION",
            "FUNASR_CHUNK_PRE_CONTEXT_SEC": "FUNASR_CHUNK_PRE_CONTEXT_SEC",
            "FUNASR_CHUNK_SEC": "FUNASR_CHUNK_SEC",
            "_funasr_model_runtime_signature": "funasr_model_runtime_signature",
            "funasr_checkpoint_path": "funasr_checkpoint_path",
            "_funasr_source_fingerprint": "funasr_source_fingerprint",
            "_funasr_chunk_fingerprint": "funasr_chunk_fingerprint",
            "_funasr_chunk_input_window": "funasr_chunk_input_window",
            "_is_close_number": "is_close_number",
            "_prepare_funasr_checkpoint": "prepare_funasr_checkpoint",
            "write_funasr_checkpoint": "write_funasr_checkpoint",
            "_existing_srt_is_reusable": "existing_srt_is_reusable",
            "_quarantine_incomplete_srt": "quarantine_incomplete_srt",
        }
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                self.assertIs(
                    getattr(service, compatibility_name),
                    getattr(checkpoints, owner_name),
                )

        for facade_name, owner_name in checkpoints.FACADE_EXPORTS.items():
            with self.subTest(facade=facade_name):
                self.assertIs(
                    getattr(topic_engine, facade_name),
                    getattr(checkpoints, owner_name),
                )
        self.assertIs(service.checkpoint_store, checkpoints)
        self.assertIs(
            pipeline._funasr_checkpoint_path,
            checkpoints.funasr_checkpoint_path,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        service_functions = {
            item["name"]
            for item in modules["autoslice.transcription.service"][
                "top_level_functions"
            ]
        }
        owner_functions = set(checkpoints.FACADE_EXPORTS.values()) | {
            "commit_file_atomically",
            "existing_srt_is_reusable",
            "quarantine_incomplete_srt",
        }
        self.assertTrue(owner_functions.isdisjoint(service_functions))

    def test_funasr_recognition_has_one_owner_and_direct_service_consumer(self):
        from autoslice.transcription import checkpoints, model_runtime, recognition, service
        from autoslice.transcription import results

        self.assertIs(service.recognition, recognition)
        self.assertIs(recognition.checkpoint_store, checkpoints)
        self.assertIs(recognition.model_runtime, model_runtime)
        self.assertIs(recognition.result_contracts, results)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        service_functions = {
            item["name"]
            for item in modules["autoslice.transcription.service"][
                "top_level_functions"
            ]
        }
        recognition_functions = {
            item["name"]
            for item in modules["autoslice.transcription.recognition"][
                "top_level_functions"
            ]
        }
        self.assertTrue(recognition_functions)
        self.assertTrue(recognition_functions.isdisjoint(service_functions))

    def test_funasr_segments_have_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice.analysis import boundaries, candidates, titles
        from autoslice.transcription import segments, service

        self.assertIs(service.subtitle_segments, segments)
        self.assertIs(candidates.transcription_segments, segments)
        self.assertIs(titles.transcription_segments, segments)
        self.assertIs(boundaries.transcription_segments, segments)
        self.assertIs(
            topic_engine._segment_timed_tokens,
            segments.segment_timed_tokens,
        )
        self.assertIs(
            topic_engine._segments_from_funasr_result,
            segments.segments_from_funasr_result,
        )
        self.assertEqual(
            segments.segment_timed_tokens_with_trace.__module__,
            segments.__name__,
        )
        self.assertEqual(
            segments.segments_from_funasr_result_with_trace.__module__,
            segments.__name__,
        )
        self.assertIs(
            segments.segment_timed_tokens.__globals__["segment_timed_tokens_with_trace"],
            segments.segment_timed_tokens_with_trace,
        )
        self.assertIs(
            segments.segments_from_funasr_result.__globals__[
                "segments_from_funasr_result_with_trace"
            ],
            segments.segments_from_funasr_result_with_trace,
        )

        for facade_name, owner_name in segments.FACADE_EXPORTS.items():
            with self.subTest(name=facade_name):
                owner = getattr(segments, owner_name)
                self.assertIs(getattr(service, facade_name), owner)
                self.assertIs(getattr(topic_engine, facade_name), owner)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        service_functions = {
            item["name"]
            for item in modules["autoslice.transcription.service"][
                "top_level_functions"
            ]
        }
        owner_module = modules["autoslice.transcription.segments"]
        owner_functions = {
            item["name"]
            for item in owner_module["top_level_functions"]
        }
        self.assertEqual(owner_functions, {
            "align_funasr_tokens",
            "attach_funasr_punctuation_to_tokens",
            "dedupe_overlapping_funasr_segments",
            "is_funasr_punctuation",
            "join_asr_tokens",
            "normalise_asr_text",
            "normalise_streamer_terms",
            "parse_srt_timestamp",
            "repair_srt_end_time",
            "segment_timed_tokens",
            "segment_timed_tokens_with_trace",
            "segments_from_funasr_result",
            "segments_from_funasr_result_with_trace",
            "should_hold_subtitle_for_short_clause",
            "split_subtitle_text_for_display",
            "split_timed_subtitle_segment",
            "srt_time",
            "srt_video_duration",
            "strip_asr_subtitle_punctuation",
            "subtitle_text_size",
            "text_len_for_timing",
            "trim_funasr_tokens_to_core",
        })
        self.assertEqual(
            {item["name"] for item in owner_module["top_level_classes"]},
            {
                "_SubtitleSegmentationPlanner",
                "SubtitleBoundaryDecision",
                "SubtitleBoundaryReason",
                "SubtitleSegmentationTrace",
            },
        )
        self.assertTrue(owner_functions.isdisjoint(service_functions))

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_name = "autoslice.transcription.segments"
        for consumer in {
            "autoslice.analysis.boundaries",
            "autoslice.analysis.candidates",
            "autoslice.analysis.topic.titles",
            "autoslice.topic_engine",
            "autoslice.transcription.service",
        }:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_name), import_edges)
        for forbidden_target in {
            "autoslice.analysis.boundaries",
            "autoslice.analysis.candidates",
            "autoslice.analysis.topic.titles",
            "autoslice.topic_engine",
            "autoslice.transcription.service",
        }:
            with self.subTest(forbidden_target=forbidden_target):
                self.assertNotIn((owner_name, forbidden_target), import_edges)

    def test_srt_io_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline
        from autoslice.analysis import boundaries, candidates
        from autoslice.transcription import service, srt_io

        self.assertIs(service.srt_io, srt_io)
        self.assertIs(pipeline.transcription_srt_io, srt_io)
        self.assertIs(candidates.transcription_srt_io, srt_io)
        self.assertIs(boundaries.transcription_srt_io, srt_io)

        for facade_name, owner_name in srt_io.FACADE_EXPORTS.items():
            with self.subTest(name=facade_name):
                owner = getattr(srt_io, owner_name)
                self.assertIs(getattr(service, facade_name), owner)
                self.assertIs(getattr(topic_engine, facade_name), owner)

        self.assertIs(pipeline.export_corrected_srt, srt_io.export_corrected_srt)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        service_functions = {
            item["name"]
            for item in modules["autoslice.transcription.service"][
                "top_level_functions"
            ]
        }
        owner_functions = {
            item["name"]
            for item in modules["autoslice.transcription.srt_io"][
                "top_level_functions"
            ]
        }
        self.assertTrue(owner_functions)
        self.assertTrue(owner_functions.isdisjoint(service_functions))

    def test_transcription_workflow_has_one_owner_and_pure_service_facade(self):
        import topic_engine
        from autoslice import pipeline
        from autoslice.transcription import service, workflow

        owner = workflow.ensure_srt
        self.assertIs(service.ensure_srt, owner)
        self.assertIs(pipeline.ensure_srt, owner)
        self.assertIs(topic_engine.ensure_srt, owner)
        self.assertIs(workflow.checkpoint_store, service.checkpoint_store)
        self.assertIs(workflow.model_runtime, service.model_runtime)
        self.assertIs(workflow.recognition, service.recognition)
        self.assertIs(workflow.result_contracts, service.result_contracts)
        self.assertIs(workflow.subtitle_segments, service.subtitle_segments)
        self.assertIs(workflow.srt_io, service.srt_io)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        service_functions = {
            item["name"]
            for item in modules["autoslice.transcription.service"][
                "top_level_functions"
            ]
        }
        workflow_functions = {
            item["name"]
            for item in modules["autoslice.transcription.workflow"][
                "top_level_functions"
            ]
        }
        self.assertFalse(service_functions)
        self.assertIn("ensure_srt", workflow_functions)
        self.assertNotIn("ensure_srt", service.FACADE_EXPORTS)

    def test_pipeline_transcription_has_one_owner_and_preserves_pipeline_seams(self):
        from autoslice import pipeline, pipeline_transcription

        self.assertIs(
            pipeline.prepare_pipeline_subtitles,
            pipeline_transcription.prepare_pipeline_subtitles,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_transcription"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["prepare_pipeline_subtitles"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_transcription"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_transcription", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_transcription", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        pipeline_definitions = {
            node.name
            for node in pipeline_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        self.assertTrue(
            {
                "ensure_srt",
                "export_corrected_srt",
                "_seed_artifact_from_legacy",
            }.isdisjoint(pipeline_definitions)
        )
        run_impl = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_pipeline_impl"
        )
        direct_calls = {
            node.func.id
            for node in ast.walk(run_impl)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("prepare_pipeline_subtitles", direct_calls)
        self.assertTrue(
            {
                "ensure_srt",
                "export_corrected_srt",
            }.isdisjoint(direct_calls)
        )

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertEqual(
            current["test_private_patches"]["total"],
            17,
        )

    def test_pipeline_analysis_has_one_owner_and_direct_consumer(self):
        from autoslice import pipeline, pipeline_analysis

        self.assertIs(
            pipeline.prepare_pipeline_analysis,
            pipeline_analysis.prepare_pipeline_analysis,
        )
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_analysis"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["prepare_pipeline_analysis"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_analysis"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_analysis", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_analysis", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        run_impl = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_pipeline_impl"
        )
        direct_calls = {
            node.func.id
            for node in ast.walk(run_impl)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("prepare_pipeline_analysis", direct_calls)
        self.assertTrue(
            {
                "analyze_danmaku",
                "parse_srt_text",
                "chunk_srt",
                "probe_video_duration",
            }.isdisjoint(direct_calls)
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_manual_has_one_owner_and_preserves_explicit_dependency_seams(self):
        from autoslice import pipeline, pipeline_manual

        self.assertIs(
            pipeline.prepare_pipeline_manual_timeline,
            pipeline_manual.prepare_pipeline_manual_timeline,
        )
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_manual"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["prepare_pipeline_manual_timeline"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_manual"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_manual", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_manual", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        run_impl = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_pipeline_impl"
        )
        direct_calls = {
            node.func.id
            for node in ast.walk(run_impl)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("prepare_pipeline_manual_timeline", direct_calls)
        self.assertTrue(
            {
                "_copy_artifact_file",
                "load_optimized_timeline_artifact",
                "prepare_optimized_manual_timeline",
            }.isdisjoint(direct_calls)
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_llm_has_one_owner_and_preserves_explicit_dependency_seams(self):
        from autoslice import pipeline, pipeline_llm

        self.assertIs(
            pipeline.analyze_pipeline_llm_chunks,
            pipeline_llm.analyze_pipeline_llm_chunks,
        )
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_llm"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["analyze_pipeline_llm_chunks"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_llm"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_llm", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_llm", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        run_impl = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_pipeline_impl"
        )
        direct_calls = {
            node.func.id
            for node in ast.walk(run_impl)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("analyze_pipeline_llm_chunks", direct_calls)
        self.assertTrue(
            {
                "analyze_topic_chunks",
            }.isdisjoint(direct_calls)
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_review_has_one_owner_and_direct_consumer(self):
        from autoslice import pipeline, pipeline_review

        self.assertIs(
            pipeline.review_pipeline_candidates,
            pipeline_review.review_pipeline_candidates,
        )
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_review"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["review_pipeline_candidates"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_review"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_review", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_review", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        run_impl = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_pipeline_impl"
        )
        direct_calls = {
            node.func.id
            for node in ast.walk(run_impl)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("review_pipeline_candidates", direct_calls)
        self.assertTrue(
            {
                "merge_manual_timeline_topics",
                "validate_unmatched_manual_topics",
                "clean_topics_for_report",
                "analysis_topics_snapshot",
                "write_clip_review_checkpoint",
                "apply_danmaku_slice_decisions",
                "review_peak_selected_topics",
            }.isdisjoint(direct_calls)
        )

        owner_source = (
            ROOT / "src/autoslice/pipeline_review.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("autoslice.pipeline", owner_source)
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_titles_has_one_owner_and_direct_consumer(self):
        from autoslice import pipeline, pipeline_titles
        from autoslice.analysis.topic import titles as title_analysis

        self.assertIs(
            pipeline.review_pipeline_publish_titles,
            pipeline_titles.review_pipeline_publish_titles,
        )
        self.assertIs(
            pipeline._review_selected_publish_titles,
            title_analysis.review_selected_publish_titles,
        )
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_titles"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["review_pipeline_publish_titles"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_titles"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_titles", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_titles", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        run_impl = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_pipeline_impl"
        )
        direct_calls = {
            node.func.id
            for node in ast.walk(run_impl)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("review_pipeline_publish_titles", direct_calls)
        self.assertNotIn("_review_selected_publish_titles", direct_calls)

        owner_source = (
            ROOT / "src/autoslice/pipeline_titles.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("autoslice.pipeline", owner_source)
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_decisions_has_one_owner_and_direct_consumer(self):
        from autoslice import pipeline, pipeline_decisions
        from autoslice.analysis import checkpoints
        from autoslice.analysis.review import outro, scoring
        from autoslice.transcription import srt_io

        self.assertIs(
            pipeline.prepare_pipeline_decisions,
            pipeline_decisions.prepare_pipeline_decisions,
        )
        self.assertIs(
            pipeline._build_clip_candidate_review_audit,
            scoring.build_clip_candidate_review_audit,
        )
        self.assertIs(
            pipeline.parse_srt_segments,
            srt_io.load_repaired_srt_segments,
        )
        self.assertIs(
            pipeline._detect_stream_outro_clip,
            outro._detect_stream_outro_clip,
        )
        self.assertIs(
            pipeline._outro_topic_from_mark,
            outro._outro_topic_from_mark,
        )
        self.assertIs(
            pipeline._analysis_topics_snapshot,
            checkpoints.analysis_topics_snapshot,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_decisions"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["prepare_pipeline_decisions"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_decisions"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_decisions", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_decisions", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        run_impl = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_pipeline_impl"
        )
        direct_calls = {
            node.func.id
            for node in ast.walk(run_impl)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("prepare_pipeline_decisions", direct_calls)
        self.assertTrue(
            {
                "_build_clip_candidate_review_audit",
                "parse_srt_segments",
                "_analysis_topics_snapshot",
            }.isdisjoint(direct_calls)
        )
        target_module_calls = {
            (node.func.value.id, node.func.attr)
            for node in ast.walk(run_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"slice_decisions", "outro_analysis"}
        }
        self.assertTrue(
            {
                ("slice_decisions", "clip_marks_from_topics"),
                ("outro_analysis", "_detect_stream_outro_clip"),
                ("outro_analysis", "_outro_topic_from_mark"),
            }.isdisjoint(target_module_calls)
        )
        owner_call = next(
            node
            for node in ast.walk(run_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "prepare_pipeline_decisions"
        )
        self.assertEqual(
            {keyword.arg for keyword in owner_call.keywords},
            {
                "filter_topics",
                "clip_marks_from_topics",
                "build_clip_candidate_review_audit",
                "write_artifact_json",
                "parse_srt_segments",
                "detect_stream_outro_clip",
                "outro_topic_from_mark",
                "analysis_topics_snapshot",
            },
        )

        owner_source = (
            ROOT / "src/autoslice/pipeline_decisions.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("autoslice.pipeline", owner_source)
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_artifacts_has_one_owner_direct_consumers_and_hard_limits(self):
        from autoslice import pipeline, pipeline_artifacts

        self.assertIs(
            pipeline.persist_pipeline_artifacts,
            pipeline_artifacts.persist_pipeline_artifacts,
        )
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_artifacts"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["persist_pipeline_artifacts"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_artifacts"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_artifacts", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_artifacts", "autoslice.topic_engine"),
            import_edges,
        )

        owner_source = (
            ROOT / "src/autoslice/pipeline_artifacts.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("autoslice.pipeline", owner_source)
        self.assertNotIn("topic_engine", owner_source)
        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        for implementation_name in (
            "run_pipeline_impl",
            "retry_clip_review_from_artifacts_impl",
        ):
            implementation = next(
                node for node in pipeline_tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == implementation_name
            )
            owner_calls = [
                node for node in ast.walk(implementation)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "persist_pipeline_artifacts"
            ]
            self.assertEqual(len(owner_calls), 1)
            self.assertTrue(
                {
                    "_write_artifact_text",
                    "_write_completed_clip_review_checkpoint",
                }.isdisjoint({
                    node.func.id for node in ast.walk(implementation)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                })
            )

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"], 0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_retry_has_one_owner_direct_consumer_and_safe_direction(self):
        from autoslice import pipeline, pipeline_retry

        self.assertIs(
            pipeline.prepare_retry_pipeline_state,
            pipeline_retry.prepare_retry_pipeline_state,
        )
        owner_source = (
            ROOT / "src/autoslice/pipeline_retry.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("autoslice.pipeline", owner_source)
        self.assertNotIn("autoslice.topic_engine", owner_source)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_retry"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["prepare_retry_pipeline_state"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        retry_impl = next(
            node for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "retry_clip_review_from_artifacts_impl"
        )
        owner_calls = [
            node for node in ast.walk(retry_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "prepare_retry_pipeline_state"
        ]
        self.assertEqual(len(owner_calls), 1)
        self.assertTrue(
            {
                "artifact_bundle_layout",
                "organize_existing_artifacts",
                "seed_artifact_from_legacy",
                "manual_timeline_for_rebuilt_report",
                "parse_generated_topic_report",
                "clean_topics_for_report",
                "analysis_topics_snapshot",
                "merge_manual_timeline_topics",
                "clip_review_checkpoint_matches_policy",
                "clip_review_checkpoint_is_complete",
                "topic_review_focus_max_sec",
            }.issubset({keyword.arg for keyword in owner_calls[0].keywords})
        )

    def test_pipeline_retry_analysis_has_one_owner_direct_consumer_and_safe_direction(self):
        from autoslice import pipeline, pipeline_retry_analysis

        self.assertIs(
            pipeline.prepare_retry_analysis_state,
            pipeline_retry_analysis.prepare_retry_analysis_state,
        )
        owner_source = (
            ROOT / "src/autoslice/pipeline_retry_analysis.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("autoslice.pipeline", owner_source)
        self.assertNotIn("autoslice.topic_engine", owner_source)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_retry_analysis"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["prepare_retry_analysis_state"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry_analysis"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry_analysis", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry_analysis", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        retry_impl = next(
            node for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "retry_clip_review_from_artifacts_impl"
        )
        owner_calls = [
            node for node in ast.walk(retry_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "prepare_retry_analysis_state"
        ]
        self.assertEqual(len(owner_calls), 1)
        self.assertEqual(
            {keyword.arg for keyword in owner_calls[0].keywords},
            {
                "parse_srt_segments",
                "analyze_danmaku",
                "empty_danmaku_series",
                "average_danmaku_density",
                "high_energy_danmaku_peaks",
            },
        )
        self.assertTrue(
            {
                "parse_srt_segments",
                "analyze_danmaku",
                "DanmakuDensitySeries",
                "_average_danmaku_density",
                "_high_energy_danmaku_peaks",
            }.isdisjoint({
                node.func.id for node in ast.walk(retry_impl)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
            } - {"prepare_retry_analysis_state"})
        )

    def test_pipeline_retry_review_has_one_owner_direct_consumer_and_safe_direction(self):
        from autoslice import pipeline, pipeline_retry_review

        self.assertIs(
            pipeline.review_retry_candidates_and_titles,
            pipeline_retry_review.review_retry_candidates_and_titles,
        )
        owner_source = (
            ROOT / "src/autoslice/pipeline_retry_review.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("autoslice.pipeline", owner_source)
        self.assertNotIn("autoslice.topic_engine", owner_source)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_retry_review"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["review_retry_candidates_and_titles"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry_review"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry_review", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry_review", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        retry_impl = next(
            node for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "retry_clip_review_from_artifacts_impl"
        )
        owner_calls = [
            node for node in ast.walk(retry_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "review_retry_candidates_and_titles"
        ]
        self.assertEqual(len(owner_calls), 1)
        self.assertTrue(
            {
                "srt_segments",
                "peaks",
                "avg_den",
                "streamer_name",
                "clip_review_checkpoint_path",
                "resume_review",
                "reuse_completed_review",
                "stale_review_keys",
                "clean_topics_for_report",
                "apply_danmaku_slice_decisions",
                "append_clip_candidate_source",
                "review_peak_selected_topics",
                "review_selected_publish_titles",
                "write_clip_review_checkpoint",
            }.issubset({keyword.arg for keyword in owner_calls[0].keywords})
        )
        self.assertTrue(
            {
                "write_clip_review_checkpoint",
                "apply_danmaku_slice_decisions",
                "append_clip_candidate_source",
                "review_peak_selected_topics",
                "_review_selected_publish_titles",
            }.isdisjoint({
                node.func.id
                for node in ast.walk(retry_impl)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
            } - {"review_retry_candidates_and_titles"})
        )

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_boundaries_has_one_owner_direct_consumers_and_hard_limits(self):
        from autoslice import pipeline, pipeline_boundaries, reporting
        from autoslice.analysis import boundaries

        self.assertIs(
            pipeline.prepare_pipeline_boundaries,
            pipeline_boundaries.prepare_pipeline_boundaries,
        )
        self.assertIs(
            pipeline.pipeline_boundaries,
            pipeline_boundaries,
        )
        self.assertIs(pipeline.reporting_service, reporting)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_boundaries"]

        # 三项硬指标：唯一顶层函数、无顶层类、owner 体量受限。
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["prepare_pipeline_boundaries"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])
        self.assertLessEqual(owner_module["line_count"], 35)

        import_edges = {
            (edge["from"], edge["to"]) for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_boundaries"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_boundaries", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_boundaries", "autoslice.topic_engine"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_boundaries", "autoslice.reporting"),
            import_edges,
        )

        owner_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline_boundaries.py").read_text(
                encoding="utf-8"
            )
        )
        owner_function = next(
            node
            for node in owner_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "prepare_pipeline_boundaries"
        )
        owner_calls = [
            node
            for node in ast.walk(owner_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
        ]
        self.assertEqual(
            [node.func.id for node in owner_calls],
            [
                "expand_clip_marks_with_context",
                "synchronise_selected_topic_ranges",
            ],
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        for implementation_name in ("run_pipeline_impl",):
            implementation = next(
                node
                for node in pipeline_tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == implementation_name
            )
            owner_calls = [
                node
                for node in ast.walk(implementation)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "prepare_pipeline_boundaries"
            ]
            self.assertEqual(len(owner_calls), 1)
            owner_call = owner_calls[0]
            self.assertEqual(
                {keyword.arg for keyword in owner_call.keywords},
                {
                    "expand_clip_marks_with_context",
                    "synchronise_selected_topic_ranges",
                },
            )
            self.assertTrue(
                {
                    "_expand_clip_marks_with_context",
                    "synchronise_selected_topic_ranges",
                }.isdisjoint(
                    {
                        node.func.id
                        for node in ast.walk(implementation)
                        if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                    }
                )
            )

        self.assertIs(
            pipeline.boundary_analysis._expand_clip_marks_with_context,
            boundaries._expand_clip_marks_with_context,
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_slice_reuse_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import slice_reuse, slicing

        aliases = {
            "_GENERATED_VIDEO_SUFFIX_PATTERN": "_GENERATED_VIDEO_SUFFIX_PATTERN",
            "_GENERATED_TOPIC_TEMP_RE": "_GENERATED_TOPIC_TEMP_RE",
            "SLICE_DURATION_TOLERANCE_SEC": "SLICE_DURATION_TOLERANCE_SEC",
            "_GENERATED_TOPIC_ARTIFACT_RE": "_GENERATED_TOPIC_ARTIFACT_RE",
            "cleanup_stale_topic_clips": "cleanup_stale_topic_clips",
            "is_reusable_topic_clip": "is_reusable_topic_clip",
            "reuse_compatible_topic_clip": "reuse_compatible_topic_clip",
            "reuse_topic_clip_after_title_change": (
                "reuse_topic_clip_after_title_change"
            ),
        }
        for compatibility_name, owner_name in aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(slice_reuse, owner_name)
                self.assertIs(getattr(slicing, compatibility_name), owner)

        for facade_name, owner_name in slice_reuse.FACADE_EXPORTS.items():
            with self.subTest(facade=facade_name):
                self.assertIs(
                    getattr(topic_engine, facade_name),
                    getattr(slice_reuse, owner_name),
                )

        self.assertIs(slicing.slice_reuse, slice_reuse)
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        slicing_functions = {
            item["name"]
            for item in modules["autoslice.slicing"]["top_level_functions"]
        }
        self.assertTrue(
            {
                "cleanup_stale_topic_clips",
                "is_reusable_topic_clip",
                "reuse_compatible_topic_clip",
                "reuse_topic_clip_after_title_change",
            }.isdisjoint(slicing_functions)
        )

    def test_slice_planning_has_one_owner_and_direct_slicing_consumer(self):
        from autoslice import slice_planning, slicing

        self.assertIs(slicing.slice_planning, slice_planning)
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        slicing_functions = {
            item["name"]
            for item in modules["autoslice.slicing"]["top_level_functions"]
        }
        self.assertTrue(
            {
                "build_slice_jobs",
                "partition_slice_jobs",
            }.isdisjoint(slicing_functions)
        )

    def test_slice_encoding_has_one_owner_and_direct_slicing_consumer(self):
        import topic_engine
        from autoslice import slice_encoding, slicing

        self.assertIs(slicing.slice_encoding, slice_encoding)
        compatibility_aliases = {
            "SLICE_DEFAULT_CONCURRENCY": "SLICE_DEFAULT_CONCURRENCY",
            "SLICE_EXACT_SEEK_PREROLL_SEC": "SLICE_EXACT_SEEK_PREROLL_SEC",
            "SLICE_MAX_CONCURRENCY": "SLICE_MAX_CONCURRENCY",
            "SLICE_INDEX_MIN_CLIPS": "SLICE_INDEX_MIN_CLIPS",
            "format_ffmpeg_seconds": "format_ffmpeg_seconds",
            "preferred_slice_video_encoder_args": (
                "preferred_slice_video_encoder_args"
            ),
            "software_slice_video_encoder_args": (
                "software_slice_video_encoder_args"
            ),
            "configured_slice_concurrency": "configured_slice_concurrency",
            "build_precise_slice_ffmpeg_command": (
                "build_precise_slice_ffmpeg_command"
            ),
            "prepare_seekable_slice_source": "prepare_seekable_slice_source",
        }
        for compatibility_name, owner_name in compatibility_aliases.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(slice_encoding, owner_name)
                self.assertIs(getattr(slicing, compatibility_name), owner)

        for facade_name, owner_name in slice_encoding.FACADE_EXPORTS.items():
            with self.subTest(facade=facade_name):
                self.assertIs(
                    getattr(topic_engine, facade_name),
                    getattr(slice_encoding, owner_name),
                )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        slicing_functions = {
            item["name"]
            for item in modules["autoslice.slicing"]["top_level_functions"]
        }
        self.assertTrue(
            {
                "format_ffmpeg_seconds",
                "preferred_slice_video_encoder_args",
                "software_slice_video_encoder_args",
                "configured_slice_concurrency",
                "build_precise_slice_ffmpeg_command",
                "prepare_seekable_slice_source",
                "encode_slice_job",
                "execute_slice_jobs",
            }.isdisjoint(slicing_functions)
        )

    def test_clip_deduplication_has_one_owner_and_exact_direct_consumers(self):
        import topic_engine
        from autoslice import reporting, slicing
        from autoslice.analysis import boundaries, candidates
        from autoslice.analysis.review import deduplication as clip_deduplication

        aliases = {
            "_overlap_ratio": "_overlap_ratio",
            "_is_duplicate_topic": "_is_duplicate_topic",
            "_dedupe_clip_marks": "_dedupe_clip_marks",
        }
        self.assertEqual(clip_deduplication.FACADE_EXPORTS, aliases)
        for name in aliases:
            owner = getattr(clip_deduplication, name)
            with self.subTest(name=name):
                self.assertNotIn(name, boundaries.FACADE_EXPORTS)
                self.assertIs(getattr(boundaries, name), owner)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)
        self.assertIs(
            reporting._dedupe_clip_marks,
            clip_deduplication._dedupe_clip_marks,
        )
        self.assertIs(
            slicing._dedupe_clip_marks,
            clip_deduplication._dedupe_clip_marks,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.analysis.review.deduplication"]
        self.assertEqual(owner_module["line_count"], 72)
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            list(aliases.values()),
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        boundary_module = modules["autoslice.analysis.boundaries"]
        self.assertEqual(boundary_module["line_count"], 454)
        self.assertEqual(len(boundary_module["top_level_functions"]), 2)
        self.assertTrue(
            set(aliases).isdisjoint(
                item["name"] for item in boundary_module["top_level_functions"]
            )
        )

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module_name = "autoslice.analysis.review.deduplication"
        direct_consumers = {
            "autoslice.analysis.boundaries",
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual.candidates",
            "autoslice.analysis.manual.review",
            "autoslice.analysis.report.cleanup",
            "autoslice.analysis.review.decisions",
            "autoslice.analysis.review.finalization",
            "autoslice.analysis.topic.analysis",
            "autoslice.reporting",
            "autoslice.slicing",
            "autoslice.topic_engine",
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if target == owner_module_name
            },
            direct_consumers,
        )
        self.assertEqual(
            {
                target
                for source, target in import_edges
                if source == owner_module_name
            },
            set(),
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_clip_finalization_has_one_owner_and_exact_direct_consumers(self):
        import topic_engine
        from autoslice.analysis import boundaries, candidates
        from autoslice.analysis.review import finalization

        aliases = {
            "_nearest_safe_srt_boundary": "_nearest_safe_srt_boundary",
            "_merge_expanded_clip_marks": "_merge_expanded_clip_marks",
            "_refresh_natural_boundary_metadata": (
                "_refresh_natural_boundary_metadata"
            ),
            "_cap_expanded_clip_mark": "_cap_expanded_clip_mark",
            "_snap_clip_to_srt_segments": "_snap_clip_to_srt_segments",
            "_integer_clip_bounds_outside_subtitles": (
                "_integer_clip_bounds_outside_subtitles"
            ),
            "_fit_final_clip_to_safe_srt_boundaries": (
                "_fit_final_clip_to_safe_srt_boundaries"
            ),
            "_capped_speech_chain_start": "_capped_speech_chain_start",
        }
        self.assertEqual(finalization.FACADE_EXPORTS, aliases)
        for name in aliases:
            owner = getattr(finalization, name)
            with self.subTest(name=name):
                self.assertNotIn(name, boundaries.FACADE_EXPORTS)
                self.assertIs(getattr(boundaries, name), owner)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module_name = "autoslice.analysis.review.finalization"
        owner_module = modules[owner_module_name]
        self.assertEqual(owner_module["line_count"], 470)
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            list(aliases.values()),
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        boundary_functions = {
            item["name"]
            for item in modules["autoslice.analysis.boundaries"][
                "top_level_functions"
            ]
        }
        self.assertTrue(set(aliases).isdisjoint(boundary_functions))

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if target == owner_module_name
            },
            {
                "autoslice.analysis.boundaries",
                "autoslice.analysis.candidates",
                "autoslice.analysis.review.context_edges",
                "autoslice.topic_engine",
            },
        )
        self.assertEqual(
            {
                target
                for source, target in import_edges
                if source == owner_module_name
            },
            {
                "autoslice.analysis.review.deduplication",
                "autoslice.analysis.review.policy",
                "autoslice.transcription.segments",
            },
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_clip_boundaries_have_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline, reporting, slicing
        from autoslice.analysis import boundaries, candidates
        from autoslice.transcription import segments as transcription_segments
        from autoslice.transcription import srt_io as transcription_srt_io

        self.assertEqual(
            boundaries.FACADE_EXPORTS,
            {
                "_expand_clip_mark_with_context": "_expand_clip_mark_with_context",
                "_expand_clip_marks_with_context": "_expand_clip_marks_with_context",
                "_srt_video_duration": "_srt_video_duration",
                "parse_srt_segments": "parse_srt_segments",
            },
        )
        compatibility_names = (
            "_expand_clip_mark_with_context",
            "_expand_clip_marks_with_context",
            "_fit_final_clip_to_safe_srt_boundaries",
        )
        for name in compatibility_names:
            with self.subTest(name=name):
                owner = getattr(boundaries, name)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)

        self.assertIs(
            boundaries.parse_srt_segments,
            transcription_srt_io.load_repaired_srt_segments,
        )
        self.assertIs(
            boundaries._srt_video_duration,
            transcription_segments.srt_video_duration,
        )
        for consumer in (pipeline, slicing, candidates, topic_engine):
            with self.subTest(consumer=consumer.__name__):
                self.assertIs(
                    consumer.parse_srt_segments,
                    transcription_srt_io.load_repaired_srt_segments,
                )
                self.assertIs(
                    consumer._srt_video_duration,
                    transcription_segments.srt_video_duration,
                )

        self.assertIs(pipeline.boundary_analysis, boundaries)
        self.assertIs(slicing.boundary_analysis, boundaries)
        self.assertFalse(hasattr(reporting, "boundary_analysis"))

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"]["top_level_functions"]
        }
        self.assertTrue(
            set(boundaries.FACADE_EXPORTS.values()).isdisjoint(candidate_functions)
        )
        boundary_module = modules["autoslice.analysis.boundaries"]
        self.assertEqual(
            [item["name"] for item in boundary_module["top_level_functions"]],
            [
                "_expand_clip_mark_with_context",
                "_expand_clip_marks_with_context",
            ],
        )
        self.assertEqual(boundary_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module_name = "autoslice.analysis.boundaries"
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if target == owner_module_name
            },
            {
                "autoslice.analysis.candidates",
                "autoslice.pipeline",
                "autoslice.slicing",
                "autoslice.topic_engine",
            },
        )
        self.assertEqual(
            {
                target
                for source, target in import_edges
                if source == owner_module_name
            },
            {
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
            },
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_stream_outro_has_one_owner_and_exact_direct_consumers(self):
        from autoslice import pipeline, topic_engine
        from autoslice.analysis import boundaries, candidates
        from autoslice.analysis import review as review_package
        from autoslice.analysis.review import outro

        aliases = {
            "_OUTRO_ACTIVITY_VARIANT_RE": "_OUTRO_ACTIVITY_VARIANT_RE",
            "_OUTRO_FAREWELL_EVIDENCE": "_OUTRO_FAREWELL_EVIDENCE",
            "_OUTRO_TRIGGER_NORMALISE_RE": "_OUTRO_TRIGGER_NORMALISE_RE",
            "_detect_stream_outro_clip": "_detect_stream_outro_clip",
            "_has_outro_farewell_evidence": "_has_outro_farewell_evidence",
            "_normalise_outro_trigger_text": "_normalise_outro_trigger_text",
            "_outro_topic_from_mark": "_outro_topic_from_mark",
        }
        self.assertEqual(outro.FACADE_EXPORTS, aliases)
        for name in aliases:
            owner = getattr(outro, name)
            with self.subTest(name=name):
                self.assertNotIn(name, boundaries.FACADE_EXPORTS)
                self.assertIs(getattr(boundaries, name), owner)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)
        self.assertIs(pipeline.outro_analysis, outro)
        self.assertIs(
            pipeline._detect_stream_outro_clip,
            outro._detect_stream_outro_clip,
        )
        self.assertIs(
            pipeline._outro_topic_from_mark,
            outro._outro_topic_from_mark,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module_name = "autoslice.analysis.review.outro"
        owner_module = modules[owner_module_name]
        self.assertEqual(owner_module["line_count"], 161)
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            [
                "_normalise_outro_trigger_text",
                "_has_outro_farewell_evidence",
                "_detect_stream_outro_clip",
                "_outro_topic_from_mark",
            ],
        )
        self.assertEqual(owner_module["top_level_classes"], [])
        boundary_functions = {
            item["name"]
            for item in modules["autoslice.analysis.boundaries"]["top_level_functions"]
        }
        self.assertTrue(
            {
                "_normalise_outro_trigger_text",
                "_has_outro_farewell_evidence",
                "_detect_stream_outro_clip",
                "_outro_topic_from_mark",
            }.isdisjoint(boundary_functions)
        )

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if target == owner_module_name
            },
            {
                "autoslice.analysis.boundaries",
                "autoslice.analysis.candidates",
                "autoslice.pipeline",
                "autoslice.topic_engine",
            },
        )
        self.assertEqual(
            {
                target
                for source, target in import_edges
                if source == owner_module_name
            },
            {
                "autoslice.analysis.review.policy",
                "autoslice.streamer_profiles",
            },
        )

        outro_names = set(aliases)
        stale_boundary_references = []
        for relative_path in (
            "src/autoslice/analysis/candidates.py",
            "src/autoslice/pipeline.py",
            "src/autoslice/topic_engine.py",
        ):
            tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
            stale_boundary_references.extend(
                (relative_path, node.lineno, node.attr)
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "boundary_analysis"
                and node.attr in outro_names
            )
        self.assertEqual(stale_boundary_references, [])

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        decision_callback_sets = []
        for node in ast.walk(pipeline_tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {
                    "prepare_pipeline_decisions",
                    "prepare_retry_decisions",
                }
            ):
                continue
            decision_callback_sets.append({
                keyword.arg: keyword.value.attr
                for keyword in node.keywords
                if keyword.arg in {
                    "detect_stream_outro_clip",
                    "outro_topic_from_mark",
                }
                and isinstance(keyword.value, ast.Attribute)
                and isinstance(keyword.value.value, ast.Name)
                and keyword.value.value.id == "outro_analysis"
            })
        self.assertEqual(
            decision_callback_sets,
            [
                {
                    "detect_stream_outro_clip": "_detect_stream_outro_clip",
                    "outro_topic_from_mark": "_outro_topic_from_mark",
                },
                {
                    "detect_stream_outro_clip": "_detect_stream_outro_clip",
                    "outro_topic_from_mark": "_outro_topic_from_mark",
                },
            ],
        )

        review_init_tree = ast.parse(
            (ROOT / "src/autoslice/analysis/review/__init__.py").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            for node in ast.walk(review_init_tree)
        ))
        self.assertEqual(review_package.__all__, sorted(review_package.__all__))
        self.assertIn("outro", review_package.__all__)

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_clip_policy_has_one_owner_and_direct_consumers(self):
        import topic_engine
        from autoslice import pipeline
        from autoslice.analysis import candidates
        from autoslice.analysis import clip_policy as legacy_clip_policy
        from autoslice.analysis.review import policy as clip_policy

        compatibility_names = (
            "CLIP_MANUAL_REVIEW_MIN_STARS",
            "CLIP_MIN_INTEREST_SCORE",
            "SC_TRIGGER_KEYWORDS",
            "THANKS_TRIGGER_RE",
            "TOPIC_MAX_CLIP_SEC",
            "TOPIC_PRE_CONTEXT_SEC",
            "TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC",
        )
        for name in compatibility_names:
            with self.subTest(name=name):
                owner = getattr(clip_policy, name)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)

        self.assertIs(legacy_clip_policy.FACADE_EXPORTS, clip_policy.FACADE_EXPORTS)
        for name, value in vars(clip_policy).items():
            if not name.startswith("__"):
                self.assertIs(getattr(legacy_clip_policy, name), value)
        self.assertIs(pipeline.clip_policy, clip_policy)
        self.assertIs(
            pipeline.CLIP_MIN_INTEREST_SCORE,
            clip_policy.CLIP_MIN_INTEREST_SCORE,
        )
        self.assertIs(
            pipeline.TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC,
            clip_policy.TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.review.policy"
        legacy_module = "autoslice.analysis.clip_policy"
        consumers = {
            "autoslice.analysis.boundaries",
            "autoslice.analysis.review.context_edges",
            "autoslice.analysis.review.decisions",
            "autoslice.analysis.review.reconciliation",
            "autoslice.analysis.candidates",
            "autoslice.analysis.manual.candidates",
            "autoslice.analysis.manual.enrichment",
            "autoslice.analysis.review.candidates",
            "autoslice.analysis.review.finalization",
            "autoslice.analysis.review.prompt",
            "autoslice.analysis.review.transitions",
            "autoslice.analysis.review.triggers",
            "autoslice.analysis.review.workflow",
            "autoslice.analysis.topic.analysis",
            "autoslice.pipeline",
            "autoslice.topic_engine",
        }
        for consumer in consumers:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        production_modules = {
            module["module"]
            for module in current["modules"]
            if module["path"].startswith("src/")
            and module["module"] != legacy_module
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == legacy_module
            },
            set(),
        )
        for forbidden_target in consumers | {legacy_module}:
            self.assertNotIn((owner_module, forbidden_target), import_edges)

    def test_candidate_reconciliation_has_one_owner_and_direct_consumers(self):
        from autoslice import topic_engine
        from autoslice.analysis import (
            candidate_reconciliation as legacy_candidate_reconciliation,
        )
        from autoslice.analysis import candidates
        from autoslice.analysis.report import cleanup as report_cleanup
        from autoslice.analysis.review import decisions as slice_decisions
        from autoslice.analysis.review import reconciliation as candidate_reconciliation

        compatibility_owners = {
            "_topic_semantic_text": "topic_semantic_text",
            "_danmaku_topic_alignment": "danmaku_topic_alignment",
            "_manual_entry_meaningfully_overlaps_topic": (
                "manual_entry_meaningfully_overlaps_topic"
            ),
            "_reconcile_topic_manual_evidence": "reconcile_topic_manual_evidence",
        }
        self.assertIs(
            legacy_candidate_reconciliation.FACADE_EXPORTS,
            candidate_reconciliation.FACADE_EXPORTS,
        )
        for name, value in vars(candidate_reconciliation).items():
            if name.startswith("__"):
                continue
            with self.subTest(facade_name=name):
                self.assertIs(
                    getattr(legacy_candidate_reconciliation, name),
                    value,
                )
        for compatibility_name, owner_name in compatibility_owners.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(candidate_reconciliation, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)
                self.assertEqual(
                    candidate_reconciliation.FACADE_EXPORTS[compatibility_name],
                    owner_name,
                )
                self.assertNotIn(
                    compatibility_name,
                    candidates.FACADE_EXPORTS,
                )

        direct_consumers = {
            candidates,
            report_cleanup,
            slice_decisions,
            topic_engine,
        }
        for consumer in direct_consumers:
            with self.subTest(direct_consumer=consumer.__name__):
                self.assertIs(
                    consumer.candidate_reconciliation,
                    candidate_reconciliation,
                )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"]["top_level_functions"]
        }
        owner_functions = [
            item["name"]
            for item in modules[
                "autoslice.analysis.review.reconciliation"
            ]["top_level_functions"]
        ]
        self.assertEqual(owner_functions, list(compatibility_owners.values()))
        legacy_module_record = modules[
            "autoslice.analysis.candidate_reconciliation"
        ]
        self.assertEqual(legacy_module_record["line_count"], 10)
        self.assertEqual(legacy_module_record["top_level_functions"], [])
        self.assertEqual(legacy_module_record["top_level_classes"], [])
        self.assertTrue(
            set(compatibility_owners).isdisjoint(candidate_functions)
        )
        self.assertTrue(
            set(compatibility_owners.values()).isdisjoint(candidate_functions)
        )

        owner_tree = ast.parse(
            (
                ROOT
                / "src/autoslice/analysis/review/reconciliation.py"
            ).read_text(encoding="utf-8")
        )
        owner_local_calls = {
            node.func.id
            for node in ast.walk(owner_tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertGreaterEqual(
            owner_local_calls,
            {
                "topic_semantic_text",
                "manual_entry_meaningfully_overlaps_topic",
            },
        )
        self.assertTrue(
            set(compatibility_owners).isdisjoint(owner_local_calls)
        )

        slice_decisions_tree = ast.parse(
            (ROOT / "src/autoslice/analysis/review/decisions.py").read_text(
                encoding="utf-8"
            )
        )
        direct_slice_decision_calls = {
            node.func.attr
            for node in ast.walk(slice_decisions_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "candidate_reconciliation"
        }
        compatibility_calls = {
            node.func.id
            for node in ast.walk(slice_decisions_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in compatibility_owners
        }
        self.assertGreaterEqual(
            direct_slice_decision_calls,
            {
                "danmaku_topic_alignment",
            },
        )
        self.assertEqual(compatibility_calls, set())

        candidates_tree = ast.parse(
            (ROOT / "src/autoslice/analysis/candidates.py").read_text(
                encoding="utf-8"
            )
        )
        candidate_bindings = {
            target.id: node.value.attr
            for node in candidates_tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "candidate_reconciliation"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for compatibility_name, owner_name in compatibility_owners.items():
            self.assertEqual(candidate_bindings[compatibility_name], owner_name)

        topic_engine_tree = ast.parse(
            (ROOT / "src/autoslice/topic_engine.py").read_text(encoding="utf-8")
        )
        direct_bindings = {
            target.id: node.value.attr
            for node in topic_engine_tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "candidate_reconciliation"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for compatibility_name, owner_name in compatibility_owners.items():
            self.assertEqual(direct_bindings[compatibility_name], owner_name)

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.review.reconciliation"
        legacy_module = "autoslice.analysis.candidate_reconciliation"
        direct_consumer_names = {
            "autoslice.analysis.candidates",
            "autoslice.analysis.report.cleanup",
            "autoslice.analysis.review.decisions",
            "autoslice.topic_engine",
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if target == owner_module
            },
            direct_consumer_names | {legacy_module},
        )
        production_modules = {
            module["module"]
            for module in current["modules"]
            if module["path"].startswith("src/")
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == legacy_module
            },
            set(),
        )
        self.assertEqual(
            {
                target
                for source, target in import_edges
                if source == owner_module
            },
            {
                "autoslice.analysis.danmaku",
                "autoslice.analysis.manual.candidates",
                "autoslice.analysis.manual.timebase",
                "autoslice.analysis.review.policy",
                "autoslice.analysis.topic.titles",
            },
        )
        self.assertFalse(
            any(
                source.startswith("autoslice.analysis.review")
                and target == legacy_module
                for source, target in import_edges
            )
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_report_cleanup_has_one_owner_and_direct_consumers(self):
        from autoslice import pipeline, topic_engine
        from autoslice.analysis import candidates
        from autoslice.analysis import report_cleanup as legacy_report_cleanup
        from autoslice.analysis.report import cleanup as report_cleanup

        compatibility_owners = {
            "_report_fact_lines": "report_fact_lines",
            "_trim_report_topic_around_reviewed_topic": (
                "trim_report_topic_around_reviewed_topic"
            ),
            "_resolve_reviewed_report_overlaps": (
                "resolve_reviewed_report_overlaps"
            ),
            "_clean_topics_for_report": "clean_topics_for_report",
        }
        self.assertEqual(report_cleanup.FACADE_EXPORTS, compatibility_owners)
        self.assertIs(
            legacy_report_cleanup.FACADE_EXPORTS,
            report_cleanup.FACADE_EXPORTS,
        )
        for compatibility_name, owner_name in compatibility_owners.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(report_cleanup, owner_name)
                self.assertIs(
                    getattr(legacy_report_cleanup, owner_name),
                    owner,
                )
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)
                self.assertNotIn(
                    compatibility_name,
                    candidates.FACADE_EXPORTS,
                )
        self.assertIs(candidates.report_cleanup, report_cleanup)
        self.assertIs(pipeline.report_cleanup, report_cleanup)
        self.assertIs(topic_engine.report_cleanup, report_cleanup)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        candidate_functions = {
            item["name"]
            for item in modules["autoslice.analysis.candidates"]["top_level_functions"]
        }
        owner_functions = {
            item["name"]
            for item in modules[
                "autoslice.analysis.report.cleanup"
            ]["top_level_functions"]
        }
        self.assertEqual(
            modules["autoslice.analysis.report_cleanup"][
                "top_level_functions"
            ],
            [],
        )
        self.assertEqual(owner_functions, set(compatibility_owners.values()))
        self.assertTrue(set(compatibility_owners).isdisjoint(candidate_functions))
        self.assertTrue(
            set(compatibility_owners.values()).isdisjoint(candidate_functions)
        )

        owner_path = ROOT / "src/autoslice/analysis/report/cleanup.py"
        owner_tree = ast.parse(owner_path.read_text(encoding="utf-8"))
        imported_targets = set()
        for node in ast.walk(owner_tree):
            if isinstance(node, ast.Import):
                imported_targets.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_targets.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
        self.assertNotIn("autoslice.analysis.candidates", imported_targets)
        self.assertNotIn("autoslice.pipeline", imported_targets)

        owner_direct_calls = {
            (node.func.value.id, node.func.attr)
            for node in ast.walk(owner_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        }
        self.assertGreaterEqual(
            owner_direct_calls,
            {
                ("clip_deduplication", "_is_duplicate_topic"),
                (
                    "candidate_reconciliation",
                    "reconcile_topic_manual_evidence",
                ),
                ("normalization", "normalise_body_line"),
                ("timeline_analysis", "manual_alignment_score"),
                ("timecode", "format_elapsed"),
                ("title_analysis", "_derive_topic_title"),
                ("title_analysis", "_fallback_publish_title"),
                ("title_analysis", "_normalise_publish_title"),
                ("title_analysis", "_sanitize_transport_claims"),
                ("title_analysis", "_strip_body_prefix"),
            },
        )

        candidates_tree = ast.parse(
            (ROOT / "src/autoslice/analysis/candidates.py").read_text(
                encoding="utf-8"
            )
        )
        candidate_compatibility_calls = {
            node.func.id
            for node in ast.walk(candidates_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in compatibility_owners
        }
        self.assertEqual(candidate_compatibility_calls, set())
        candidate_bindings = {
            target.id: node.value.attr
            for node in candidates_tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "report_cleanup"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for compatibility_name, owner_name in compatibility_owners.items():
            self.assertEqual(candidate_bindings[compatibility_name], owner_name)

        topic_engine_tree = ast.parse(
            (ROOT / "src/autoslice/topic_engine.py").read_text(encoding="utf-8")
        )
        topic_engine_bindings = {
            target.id: node.value.attr
            for node in topic_engine_tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "report_cleanup"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for compatibility_name, owner_name in compatibility_owners.items():
            self.assertEqual(topic_engine_bindings[compatibility_name], owner_name)

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        pipeline_direct_calls = [
            node
            for node in ast.walk(pipeline_tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "report_cleanup"
            and node.attr == "clean_topics_for_report"
        ]
        pipeline_compatibility_calls = [
            node
            for node in ast.walk(pipeline_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_clean_topics_for_report"
        ]
        self.assertGreater(len(pipeline_direct_calls), 0)
        self.assertEqual(pipeline_compatibility_calls, [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.report.cleanup"
        legacy_module = "autoslice.analysis.report_cleanup"
        consumers = {
            "autoslice.analysis.candidates",
            "autoslice.pipeline",
            "autoslice.topic_engine",
        }
        for consumer in consumers:
            with self.subTest(consumer=consumer):
                self.assertIn((consumer, owner_module), import_edges)
                self.assertNotIn((consumer, legacy_module), import_edges)
        self.assertIn((legacy_module, owner_module), import_edges)
        for forbidden_target in {
            legacy_module,
            "autoslice.analysis.topic_formatting",
            "autoslice.analysis.candidates",
            "autoslice.pipeline",
            "autoslice.reporting",
            "autoslice.topic_engine",
        }:
            self.assertNotIn(
                (owner_module, forbidden_target),
                import_edges,
            )

        self.assertEqual(
            current["summary"]["top_level_function_count"],
            969,
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_slice_decisions_has_one_owner_and_direct_consumers(self):
        from autoslice import pipeline, topic_engine
        from autoslice.analysis import candidates
        from autoslice.analysis import slice_decisions as legacy_slice_decisions
        from autoslice.analysis.review import decisions as slice_decisions

        compatibility_owners = {
            "_topic_peak_focus_window": "topic_peak_focus_window",
            "_assign_topic_slice_window": "assign_topic_slice_window",
            "_is_content_cuttable_topic": "is_content_cuttable_topic",
            "_refresh_topic_danmaku_evidence": "refresh_topic_danmaku_evidence",
            "_append_clip_candidate_source": "append_clip_candidate_source",
            "_has_high_star_manual_evidence": "has_high_star_manual_evidence",
            "_manual_review_anchor": "manual_review_anchor",
            "_reviewed_topic_has_required_interest": (
                "reviewed_topic_has_required_interest"
            ),
            "_assign_reviewed_semantic_slice_window": (
                "assign_reviewed_semantic_slice_window"
            ),
            "_apply_reviewed_slice_decisions": "apply_reviewed_slice_decisions",
            "_apply_danmaku_slice_decisions": "apply_danmaku_slice_decisions",
            "_clip_marks_from_topics": "clip_marks_from_topics",
        }
        self.assertIs(
            legacy_slice_decisions.FACADE_EXPORTS,
            slice_decisions.FACADE_EXPORTS,
        )
        self.assertEqual(slice_decisions.FACADE_EXPORTS, compatibility_owners)
        for name, value in vars(slice_decisions).items():
            if name.startswith("__"):
                continue
            with self.subTest(facade_name=name):
                self.assertIs(getattr(legacy_slice_decisions, name), value)
        for compatibility_name, owner_name in compatibility_owners.items():
            with self.subTest(name=compatibility_name):
                owner = getattr(slice_decisions, owner_name)
                self.assertIs(getattr(candidates, compatibility_name), owner)
                self.assertIs(getattr(topic_engine, compatibility_name), owner)
                self.assertNotIn(compatibility_name, candidates.FACADE_EXPORTS)

        self.assertIs(candidates.slice_decisions, slice_decisions)
        self.assertIs(pipeline.slice_decisions, slice_decisions)
        self.assertIs(topic_engine.slice_decisions, slice_decisions)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        self.assertEqual(
            modules["autoslice.analysis.candidates"]["top_level_functions"],
            [],
        )
        facade_module = modules["autoslice.analysis.slice_decisions"]
        self.assertEqual(facade_module["line_count"], 10)
        self.assertEqual(facade_module["top_level_functions"], [])
        self.assertEqual(facade_module["top_level_classes"], [])
        owner_module = modules["autoslice.analysis.review.decisions"]
        self.assertEqual(owner_module["line_count"], 551)
        self.assertEqual(owner_module["top_level_classes"], [])
        self.assertEqual(
            [
                item["name"]
                for item in owner_module["top_level_functions"]
            ],
            list(compatibility_owners.values()),
        )

        candidates_tree = ast.parse(
            (ROOT / "src/autoslice/analysis/candidates.py").read_text(
                encoding="utf-8"
            )
        )
        candidate_bindings = {
            target.id: node.value.attr
            for node in candidates_tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "slice_decisions"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(
            {
                name: candidate_bindings[name]
                for name in compatibility_owners
            },
            compatibility_owners,
        )
        candidate_local_calls = {
            node.func.id
            for node in ast.walk(candidates_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id
            in set(compatibility_owners) | set(compatibility_owners.values())
        }
        self.assertEqual(candidate_local_calls, set())

        topic_engine_tree = ast.parse(
            (ROOT / "src/autoslice/topic_engine.py").read_text(encoding="utf-8")
        )
        topic_engine_bindings = {
            target.id: node.value.attr
            for node in topic_engine_tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "slice_decisions"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(
            {
                name: topic_engine_bindings[name]
                for name in compatibility_owners
            },
            compatibility_owners,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        production_owners = {
            "_apply_danmaku_slice_decisions": "apply_danmaku_slice_decisions",
            "_append_clip_candidate_source": "append_clip_candidate_source",
            "_clip_marks_from_topics": "clip_marks_from_topics",
        }
        direct_pipeline_calls = {
            node.attr
            for node in ast.walk(pipeline_tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "slice_decisions"
        }
        self.assertGreaterEqual(
            direct_pipeline_calls,
            set(production_owners.values()),
        )
        pipeline_compatibility_calls = {
            node.func.id
            for node in ast.walk(pipeline_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in production_owners
        }
        self.assertEqual(pipeline_compatibility_calls, set())
        pipeline_compatibility_bindings = {
            target.id
            for node in pipeline_tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id in production_owners
        }
        self.assertEqual(pipeline_compatibility_bindings, set())

        owner_tree = ast.parse(
            (ROOT / "src/autoslice/analysis/review/decisions.py").read_text(
                encoding="utf-8"
            )
        )
        imported_targets = set()
        for node in ast.walk(owner_tree):
            if isinstance(node, ast.Import):
                imported_targets.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_targets.update(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
        self.assertTrue(
            {
                "autoslice.analysis.candidates",
                "autoslice.pipeline",
                "autoslice.topic_engine",
            }.isdisjoint(imported_targets)
        )

        owner_direct_calls = {
            (node.func.value.id, node.func.attr)
            for node in ast.walk(owner_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        }
        self.assertGreaterEqual(
            owner_direct_calls,
            {
                ("trigger_analysis", "_clip_context_requires_trigger"),
                ("clip_deduplication", "_dedupe_clip_marks"),
                ("candidate_evidence", "topic_peak_candidates"),
                ("candidate_reconciliation", "danmaku_topic_alignment"),
                ("clip_scoring", "parse_clip_interest_score"),
                ("danmaku_analysis", "_danmaku_peak_features"),
                ("danmaku_analysis", "_high_energy_danmaku_peaks"),
                ("danmaku_analysis", "_reviewed_danmaku_ranking_score"),
                ("timecode", "format_elapsed"),
                ("title_analysis", "_is_bad_topic_title"),
                ("title_analysis", "_normalise_publish_title"),
                ("title_analysis", "_sanitize_transport_claims"),
            },
        )

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module_name = "autoslice.analysis.review.decisions"
        legacy_module_name = "autoslice.analysis.slice_decisions"
        direct_consumers = {
            "autoslice.analysis.candidates",
            "autoslice.pipeline",
            "autoslice.topic_engine",
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if target == owner_module_name
            },
            direct_consumers | {legacy_module_name},
        )
        production_modules = {
            module["module"]
            for module in current["modules"]
            if module["path"].startswith("src/")
        }
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if source in production_modules and target == legacy_module_name
            },
            set(),
        )
        self.assertEqual(
            {
                target
                for source, target in import_edges
                if source == legacy_module_name
            },
            {owner_module_name},
        )
        self.assertEqual(
            {
                target
                for source, target in import_edges
                if source == owner_module_name
            },
            {
                "autoslice.analysis.danmaku",
                "autoslice.analysis.evidence",
                "autoslice.analysis.review.deduplication",
                "autoslice.analysis.review.policy",
                "autoslice.analysis.review.reconciliation",
                "autoslice.analysis.review.scoring",
                "autoslice.analysis.review.triggers",
                "autoslice.analysis.topic.titles",
                "autoslice.timecode",
            },
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_topic_engine_compatibility_constants_follow_unique_owner(self):
        import topic_engine
        from autoslice.llm import transport

        self.assertIs(
            topic_engine.LLM_RETRY_DELAYS,
            transport.DEFAULT_RETRY_DELAYS,
        )
        self.assertIs(
            topic_engine.LLM_PROVIDER_UNAVAILABLE_RETRY_DELAYS,
            transport.DEFAULT_PROVIDER_UNAVAILABLE_RETRY_DELAYS,
        )
        self.assertIs(
            topic_engine.LLM_REQUEST_TIMEOUT,
            transport.DEFAULT_REQUEST_TIMEOUT,
        )

    def test_topic_engine_is_a_thin_definition_free_facade(self):
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        topic_engine = modules["autoslice.topic_engine"]

        self.assertEqual(
            [item["name"] for item in topic_engine["top_level_functions"]],
            ["main"],
        )
        self.assertEqual(topic_engine["top_level_classes"], [])
        self.assertLess(topic_engine["line_count"], 1000)

    def test_task_result_summary_has_one_owner_and_direct_web_consumer(self):
        from autoslice import task_results
        from autoslice.web import app as web_app

        self.assertIs(
            web_app.build_pipeline_result_summary,
            task_results.build_pipeline_result_summary,
        )
        self.assertIs(
            web_app.normalize_task_result,
            task_results.normalize_task_result,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner = modules["autoslice.task_results"]
        self.assertEqual(
            [item["name"] for item in owner["top_level_functions"]],
            [
                "normalize_task_result",
                "_non_negative_int",
                "_failed_chunk_count",
                "build_pipeline_result_summary",
            ],
        )
        self.assertEqual(owner["top_level_classes"], [])
        self.assertLessEqual(owner["line_count"], 120)

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.web.app", "autoslice.task_results"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.task_results", "autoslice.web.app"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.task_results", "autoslice.task_store"),
            import_edges,
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_current_modules_have_no_duplicate_top_level_definitions(self):
        current = architecture_snapshot.build_snapshot(ROOT)

        self.assertEqual(current["duplicate_top_level_definitions"], [])

    def test_duplicate_function_or_class_name_is_detected_from_ast(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "duplicate.py").write_text(
                "def repeated():\n"
                "    return 1\n"
                "\n"
                "class repeated:\n"
                "    pass\n",
                encoding="utf-8",
            )
            snapshot = architecture_snapshot.build_snapshot(root)

        self.assertEqual(
            snapshot["duplicate_top_level_definitions"],
            [{
                "module": "duplicate",
                "path": "duplicate.py",
                "name": "repeated",
                "definitions": [
                    {
                        "name": "repeated",
                        "kind": "function",
                        "line": 1,
                        "end_line": 2,
                    },
                    {
                        "name": "repeated",
                        "kind": "class",
                        "line": 4,
                        "end_line": 5,
                    },
                ],
            }],
        )

    def test_topic_engine_llm_imports_keep_transport_object_identity(self):
        import llm_client
        import topic_engine
        from autoslice.llm import transport

        expected_identities = {
            "_LLMApiConfig": "LLMApiConfig",
            "_call_compatible_api": "call_compatible_api",
            "_infer_api_type": "infer_api_type",
            "_infer_llm_api_type": "infer_api_type",
            "_load_llm_api_config": "load_api_config",
            "load_api_config": "load_api_config",
            "_normalise_api_config": "normalise_api_config",
            "_normalise_llm_api_config": "normalise_api_config",
            "_read_llm_json_config": "read_json_config",
            "_read_json_config": "read_json_config",
            "LLMResponseTruncatedError": "LLMResponseTruncatedError",
            "LLMStructuredOutputError": "LLMStructuredOutputError",
            "LLMResponseFormatError": "LLMResponseFormatError",
            "LLMProviderUnavailableError": "LLMProviderUnavailableError",
            "_LLMProviderRetryCoordinator": "LLMProviderRetryCoordinator",
            "_llm_response_has_complete_json": "response_has_complete_json",
            "_decode_llm_response_json": "decode_response_json",
            "_parse_openai_response": "parse_openai_response",
            "_parse_anthropic_response": "parse_anthropic_response",
            "call_llm": "call_llm",
            "_short_llm_error": "short_llm_error",
            "_llm_http_status": "llm_http_status",
            "_is_provider_service_unavailable": "is_provider_service_unavailable",
            "_is_retryable_llm_error": "is_retryable_llm_error",
            "_call_llm_with_retry": "call_llm_with_retry",
            "_extract_json_payload": "extract_json_payload",
        }
        for facade_name, transport_name in expected_identities.items():
            with self.subTest(facade_name=facade_name):
                self.assertIs(
                    getattr(topic_engine, facade_name),
                    getattr(transport, transport_name),
                    msg=f"{facade_name} 被后定义覆盖，不再指向唯一传输符号",
                )
                self.assertIs(
                    getattr(llm_client, transport_name),
                    getattr(transport, transport_name),
                    msg=f"llm_client.{transport_name} 不是唯一实现的兼容导出",
                )

    def test_resolved_llm_implementation_debt_cannot_reappear(self):
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner = modules[CURRENT_LLM_IDENTITY_DEBT["owner_module"]]
        local_names = {
            definition["name"]
            for definition in (
                owner["top_level_functions"] + owner["top_level_classes"]
            )
        }

        self.assertEqual(CURRENT_LLM_IDENTITY_DEBT["status"], "resolved")
        self.assertEqual(
            set(),
            set(CURRENT_LLM_IDENTITY_DEBT["local_definitions"]) & local_names,
            msg="topic_engine 不得重新定义已迁入唯一 gateway 的 LLM 实现",
        )

    def test_pipeline_reporting_has_one_owner_direct_consumers_and_safe_direction(self):
        from autoslice import pipeline, pipeline_reporting, pipeline_retry_reporting

        self.assertIs(
            pipeline.prepare_pipeline_report,
            pipeline_reporting.prepare_pipeline_report,
        )
        self.assertIs(
            pipeline.build_context_policy,
            pipeline_reporting.build_context_policy,
        )
        self.assertIs(
            pipeline_retry_reporting.build_danmaku_selection_policy,
            pipeline_reporting.build_danmaku_selection_policy,
        )

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_reporting"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            [
                "build_context_policy",
                "build_danmaku_selection_policy",
                "prepare_pipeline_report",
            ],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_reporting"),
            import_edges,
        )
        self.assertIn(
            (
                "autoslice.pipeline_retry_reporting",
                "autoslice.pipeline_reporting",
            ),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_reporting", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_reporting", "autoslice.topic_engine"),
            import_edges,
        )

        owner_source = (
            ROOT / "src/autoslice/pipeline_reporting.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("autoslice.pipeline", owner_source)
        self.assertNotIn("topic_engine", owner_source)
        self.assertNotIn("open(", owner_source)
        self.assertNotIn("write_text", owner_source)
        self.assertNotIn("write_bytes", owner_source)

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        run_impl = next(
            node for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "run_pipeline_impl"
        )
        report_calls = [
            node for node in ast.walk(run_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "prepare_pipeline_report"
        ]
        self.assertEqual(len(report_calls), 1)
        self.assertTrue({
            "video_path",
            "artifact_layout",
            "source_srt_path",
            "corrected_srt_path",
            "topic_analysis_checkpoint_path",
            "clip_review_checkpoint_path",
            "candidate_review_audit_path",
            "accepted_topics",
            "analysis_topics",
            "clip_marks",
            "peak_info",
            "failed_chunks",
            "api_precheck_warning",
            "clip_review_warning",
            "manual_timeline",
            "streamer_profile",
            "average_density",
            "density_threshold",
            "local_peak_radius_sec",
            "manual_review_min_stars",
            "min_editorial_interest_score",
            "context_policy",
            "topic_analysis_model",
            "review_model",
            "clip_review_completed_at",
            "artifact_layout_version",
            "build_timeline_report",
            "manual_timeline_summary",
        }.issubset({keyword.arg for keyword in report_calls[0].keywords}))
        direct_report_calls = [
            node for node in ast.walk(run_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "build_timeline_report"
        ]
        self.assertEqual(direct_report_calls, [])

        retry_impl = next(
            node for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "retry_clip_review_from_artifacts_impl"
        )
        context_policy_calls = [
            node for node in ast.walk(retry_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_context_policy"
        ]
        self.assertEqual(len(context_policy_calls), 1)
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_retry_reporting_has_one_owner_direct_consumer_and_safe_direction(self):
        from autoslice import pipeline, pipeline_retry_reporting

        self.assertIs(
            pipeline.prepare_retry_report,
            pipeline_retry_reporting.prepare_retry_report,
        )
        owner_source = (
            ROOT / "src/autoslice/pipeline_retry_reporting.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("from autoslice.pipeline import", owner_source)
        self.assertNotIn("import autoslice.pipeline\n", owner_source)
        self.assertNotIn("autoslice.topic_engine", owner_source)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_retry_reporting"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["prepare_retry_report"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry_reporting"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry_reporting", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry_reporting", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        retry_impl = next(
            node for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "retry_clip_review_from_artifacts_impl"
        )
        owner_calls = [
            node for node in ast.walk(retry_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "prepare_retry_report"
        ]
        self.assertEqual(len(owner_calls), 1)
        self.assertTrue(
            {
                "data",
                "video_path",
                "report_path",
                "artifact_layout",
                "source_srt_path",
                "corrected_srt_path",
                "clip_review_checkpoint_path",
                "candidate_review_audit_path",
                "accepted_topics",
                "clip_marks",
                "peak_info",
                "failed_chunks",
                "clip_review_warning",
                "rebuilt_manual_timeline",
                "streamer_profile",
                "average_density",
                "density_threshold",
                "local_peak_radius_sec",
                "manual_review_min_stars",
                "min_editorial_interest_score",
                "context_policy",
                "clip_review_completed_at",
                "artifact_layout_version",
                "build_timeline_report",
                "analysis_topics_snapshot",
                "manual_timeline_summary",
                "warning_without_previous_clip_review",
            }.issubset({keyword.arg for keyword in owner_calls[0].keywords})
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_pipeline_retry_decisions_has_one_owner_direct_consumer_and_safe_direction(self):
        from autoslice import pipeline, pipeline_retry_decisions

        self.assertIs(
            pipeline.prepare_retry_decisions,
            pipeline_retry_decisions.prepare_retry_decisions,
        )
        owner_source = (
            ROOT / "src/autoslice/pipeline_retry_decisions.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("autoslice.pipeline", owner_source)
        self.assertNotIn("autoslice.topic_engine", owner_source)

        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner_module = modules["autoslice.pipeline_retry_decisions"]
        self.assertEqual(
            [item["name"] for item in owner_module["top_level_functions"]],
            ["prepare_retry_decisions"],
        )
        self.assertEqual(owner_module["top_level_classes"], [])

        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        self.assertIn(
            ("autoslice.pipeline", "autoslice.pipeline_retry_decisions"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry_decisions", "autoslice.pipeline"),
            import_edges,
        )
        self.assertNotIn(
            ("autoslice.pipeline_retry_decisions", "autoslice.topic_engine"),
            import_edges,
        )

        pipeline_tree = ast.parse(
            (ROOT / "src/autoslice/pipeline.py").read_text(encoding="utf-8")
        )
        retry_impl = next(
            node for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "retry_clip_review_from_artifacts_impl"
        )
        owner_calls = [
            node for node in ast.walk(retry_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "prepare_retry_decisions"
        ]
        self.assertEqual(len(owner_calls), 1)
        self.assertTrue(
            {
                "filter_topics",
                "probe_video_duration",
                "clip_marks_from_topics",
                "build_clip_candidate_review_audit",
                "write_artifact_json",
                "parse_srt_segments",
                "detect_stream_outro_clip",
                "outro_topic_from_mark",
                "analysis_topics_snapshot",
                "prepare_pipeline_decisions",
                "prepare_pipeline_boundaries",
                "expand_clip_marks_with_context",
                "synchronise_selected_topic_ranges",
                "srt_video_duration",
            }.issubset({keyword.arg for keyword in owner_calls[0].keywords})
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertLessEqual(current["test_private_patches"]["total"], 17)


if __name__ == "__main__":
    unittest.main()
