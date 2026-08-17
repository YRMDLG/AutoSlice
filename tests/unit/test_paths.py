import tempfile
import unittest
from pathlib import Path

from autoslice import runtime_config, task_store
from autoslice import paths


class RuntimePathContractTests(unittest.TestCase):
    def test_root_compatibility_modules_share_the_runtime_path_contract(self):
        self.assertEqual(runtime_config.PROJECT_DIR, paths.APPLICATION_DATA_ROOT)
        self.assertEqual(task_store.DEFAULT_TASK_STATE_DIR, paths.state_dir())

    def test_explicit_workspace_has_priority_without_repository_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "独立工作区"
            resolved = paths.discover_workspace_root(
                Path(temporary) / "installed" / "autoslice" / "paths.py",
                environ={paths.WORKSPACE_ENV_NAME: str(workspace)},
            )

        self.assertEqual(resolved, workspace.resolve())

    def test_source_workspace_is_discovered_from_public_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            (root / "api_config.example.json").write_text("{}\n", encoding="utf-8")
            module = root / "src" / "autoslice" / "paths.py"
            module.parent.mkdir(parents=True)
            module.write_text("", encoding="utf-8")

            resolved = paths.discover_workspace_root(module, environ={})

        self.assertEqual(resolved, root.resolve())

    def test_installed_mode_uses_explicit_user_data_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            user_data = Path(temporary) / "user-data"
            module = Path(temporary) / "site-packages" / "autoslice" / "paths.py"

            resolved = paths.application_data_root(
                start=module,
                environ={paths.USER_DATA_ENV_NAME: str(user_data)},
            )

            self.assertEqual(resolved, user_data.resolve())
            self.assertFalse(user_data.exists())

    def test_state_directory_can_be_isolated_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "state"

            resolved = paths.state_dir(
                environ={paths.STATE_DIR_ENV_NAME: str(configured)},
            )

        self.assertEqual(resolved, configured.resolve())

    def test_packaged_resources_win_over_legacy_workspace_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "src" / "autoslice"
            packaged = package / "resources" / "templates"
            legacy = root / "templates"
            packaged.mkdir(parents=True)
            legacy.mkdir()

            resolved = paths.resource_directory(
                "templates",
                package_dir=package,
                workspace_root=root,
            )

        self.assertEqual(resolved, packaged.resolve())

    def test_legacy_workspace_resources_are_supported_during_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "autoslice"
            package.mkdir()
            legacy = root / "static"
            legacy.mkdir()

            resolved = paths.resource_directory(
                "static",
                package_dir=package,
                workspace_root=root,
            )

        self.assertEqual(resolved, legacy.resolve())


if __name__ == "__main__":
    unittest.main()
