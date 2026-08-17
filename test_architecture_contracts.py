import json
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from scripts import architecture_snapshot


ROOT = Path(__file__).resolve().parent
BASELINE_PATH = ROOT / "architecture_baseline.json"

# Phase 1 只冻结身份契约，不冒充 Phase 4 已完成。以下符号仍由单体拥有，
# 必须在后续迁移真正删除旧实现时连同这份债务记录一起更新。
CURRENT_LLM_IDENTITY_DEBT = {
    "status": "present",
    "owner_module": "topic_engine",
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
        self.assertIn("test_topic_engine.py", baseline["scope"]["test_files"])
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

    def test_current_cycles_are_limited_to_recorded_debt(self):
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        current = architecture_snapshot.build_snapshot(ROOT)

        violations = architecture_snapshot.dependency_contract_violations(
            current,
            baseline,
        )

        self.assertEqual(violations, [])
        self.assertTrue(baseline["dependency_cycles"])
        for cycle in baseline["dependency_cycles"]:
            self.assertEqual(cycle["debt_status"], "present")
            self.assertTrue(cycle["modules"])
            self.assertFalse(any("*" in module for module in cycle["modules"]))

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


class ArchitectureDefinitionTests(unittest.TestCase):

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

        expected_identities = {
            "_LLMApiConfig": "LLMApiConfig",
            "_call_compatible_api": "call_compatible_api",
            "_infer_api_type": "infer_api_type",
            "_load_llm_api_config": "load_api_config",
            "_normalise_api_config": "normalise_api_config",
            "_read_llm_json_config": "read_json_config",
        }
        for facade_name, transport_name in expected_identities.items():
            with self.subTest(facade_name=facade_name):
                self.assertIs(
                    getattr(topic_engine, facade_name),
                    getattr(llm_client, transport_name),
                    msg=f"{facade_name} 被后定义覆盖，不再指向唯一传输符号",
                )

    def test_legacy_llm_implementation_debt_is_not_reported_as_resolved(self):
        current = architecture_snapshot.build_snapshot(ROOT)
        modules = {module["module"]: module for module in current["modules"]}
        owner = modules[CURRENT_LLM_IDENTITY_DEBT["owner_module"]]
        local_names = {
            definition["name"]
            for definition in (
                owner["top_level_functions"] + owner["top_level_classes"]
            )
        }

        self.assertEqual(CURRENT_LLM_IDENTITY_DEBT["status"], "present")
        self.assertEqual(
            set(CURRENT_LLM_IDENTITY_DEBT["local_definitions"]),
            set(CURRENT_LLM_IDENTITY_DEBT["local_definitions"]) & local_names,
            msg="债务记录必须与 topic_engine 中仍存在的 LLM 实现完全对应",
        )


if __name__ == "__main__":
    unittest.main()
