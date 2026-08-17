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
        from autoslice import pipeline, reporting, slicing
        from autoslice.analysis import candidates, checkpoints, danmaku, timeline, titles
        from autoslice.transcription import service as transcription

        owners = (
            transcription,
            danmaku,
            timeline,
            checkpoints,
            candidates,
            titles,
            reporting,
            slicing,
            pipeline,
        )
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


if __name__ == "__main__":
    unittest.main()
