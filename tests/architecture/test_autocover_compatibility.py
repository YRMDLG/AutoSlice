"""AutoCover 旧导入路径必须绑定到 ``src`` 中的唯一 owner。"""

from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class AutoCoverCompatibilityTests(unittest.TestCase):
    def test_manifest_contract_owner_does_not_import_autoslice(self):
        path = REPOSITORY_ROOT / "src" / "autoslice_cover" / "manifest_contract.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        self.assertFalse(
            any(name == "autoslice" or name.startswith("autoslice.") for name in imported),
            imported,
        )

    def test_autocover_tool_modules_alias_owner_objects(self):
        aliases = {
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

        for legacy_name, owner_name in aliases.items():
            with self.subTest(legacy_name=legacy_name):
                self.assertIs(
                    importlib.import_module(legacy_name),
                    importlib.import_module(owner_name),
                )

    def test_standalone_autocover_imports_alias_owner_objects(self):
        script = (
            "import autocover.renderer\n"
            "import autocover.workspace\n"
            "import autoslice_cover.renderer\n"
            "import autoslice_cover.workspace\n"
            "assert autocover.renderer is autoslice_cover.renderer\n"
            "assert autocover.workspace is autoslice_cover.workspace\n"
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(REPOSITORY_ROOT / "autocover_tool"),
                str(REPOSITORY_ROOT / "src"),
            )
        )
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=REPOSITORY_ROOT,
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


if __name__ == "__main__":
    unittest.main()
