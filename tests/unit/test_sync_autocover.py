"""AutoCover 白名单同步测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.sync_autocover import apply_sync, compare_trees, managed_files


class AutoCoverSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "AutoCover"
        self.destination = self.root / "public" / "autocover_tool"
        required = {
            "app.py": "source app\n",
            "autocover/__init__.py": "SERVICE_ID = 'autocover'\n",
            "autocover/renderer.py": "def render(): pass\n",
            "static/app.js": "const ready = true;\n",
            "templates/index.html": "<main></main>\n",
            "tests/test_app.py": "def test_app(): pass\n",
        }
        for relative_path, content in required.items():
            path = self.source / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_managed_files_exclude_private_assets_and_metadata(self) -> None:
        private_files = {
            "README.md": "本地说明",
            "PLAN.md": "本地计划",
            "local/fonts/private.ttf": "font",
            ".codex/notes.md": "notes",
            "stickers/private.png": "image",
        }
        for relative_path, content in private_files.items():
            path = self.source / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        paths = set(managed_files(self.source))

        self.assertIn(Path("app.py"), paths)
        self.assertIn(Path("autocover/renderer.py"), paths)
        for relative_path in private_files:
            self.assertNotIn(Path(relative_path), paths)

    def test_apply_updates_managed_files_without_touching_public_metadata(self) -> None:
        public_readme = self.destination / "README.md"
        public_readme.parent.mkdir(parents=True)
        public_readme.write_text("公开说明", encoding="utf-8")
        stale = self.destination / "tests" / "test_removed.py"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale", encoding="utf-8")

        before = compare_trees(self.source, self.destination)
        applied = apply_sync(self.source, self.destination)

        self.assertEqual(before, applied)
        self.assertEqual(compare_trees(self.source, self.destination), [])
        self.assertEqual(public_readme.read_text(encoding="utf-8"), "公开说明")
        self.assertFalse(stale.exists())
        self.assertEqual(
            (self.destination / "static" / "app.js").read_text(encoding="utf-8"),
            "const ready = true;\n",
        )


if __name__ == "__main__":
    unittest.main()
