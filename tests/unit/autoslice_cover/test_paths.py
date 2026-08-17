"""AutoCover 资源与可写目录契约测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autoslice_cover import paths


class AutoCoverPathContractTests(unittest.TestCase):
    def test_source_checkout_preserves_existing_tool_data_root(self):
        repository_root = Path(__file__).resolve().parents[3]
        expected = (repository_root / "autocover_tool").resolve()

        self.assertEqual(paths.DATA_ROOT, expected)
        self.assertEqual(
            paths.TEMPLATE_DIR,
            paths.PACKAGE_ROOT / "resources" / "templates",
        )
        self.assertEqual(
            paths.STATIC_DIR,
            paths.PACKAGE_ROOT / "resources" / "static",
        )
        self.assertTrue(paths.TEMPLATE_DIR.is_dir())
        self.assertTrue(paths.STATIC_DIR.is_dir())

    def test_installed_mode_uses_explicit_user_data_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary) / "site-packages" / "autoslice_cover"
            package_root.mkdir(parents=True)
            data_root = Path(temporary) / "cover-data"

            resolved = paths.application_data_root(
                package_root=package_root,
                environ={paths.DATA_DIR_ENV_NAME: str(data_root)},
            )

            self.assertEqual(resolved, data_root.resolve())
            self.assertFalse(data_root.exists())

    def test_workspace_discovery_requires_both_public_markers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = root / "src" / "autoslice_cover"
            package_root.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

            self.assertIsNone(paths.discover_workspace_root(package_root))

            (root / "api_config.example.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual(paths.discover_workspace_root(package_root), root.resolve())


if __name__ == "__main__":
    unittest.main()
