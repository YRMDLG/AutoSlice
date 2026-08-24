"""AutoCover 探测必须由共享 owner 提供，生产消费者不得复制实现。"""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER_PATH = ROOT / "src" / "autoslice" / "autocover_service.py"
LAUNCHER_PATH = ROOT / "src" / "autoslice" / "launcher.py"
WEB_APP_PATH = ROOT / "src" / "autoslice" / "web" / "app.py"


class AutoCoverServiceOwnerTests(unittest.TestCase):
    def test_production_consumers_import_the_shared_owner(self):
        owner_source = OWNER_PATH.read_text(encoding="utf-8")
        launcher_source = LAUNCHER_PATH.read_text(encoding="utf-8")
        web_source = WEB_APP_PATH.read_text(encoding="utf-8")

        self.assertIn("def probe_autocover_endpoint(", owner_source)
        self.assertIn("def probe_autocover_service(", owner_source)
        self.assertIn("from autoslice import autocover_service", launcher_source)
        self.assertIn("from autoslice import autocover_service", web_source)
        self.assertIn(
            "_probe_autocover_service = autocover_service.probe_autocover_service",
            launcher_source,
        )
        self.assertIn(
            "probe_autocover_endpoint = autocover_service.probe_autocover_endpoint",
            web_source,
        )

    def test_launcher_has_no_second_autocover_probe_implementation(self):
        launcher_tree = ast.parse(LAUNCHER_PATH.read_text(encoding="utf-8"))
        function_names = {
            node.name
            for node in launcher_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertNotIn("_probe_autocover_service", function_names)
        self.assertNotIn("_is_compatible_autocover_service", function_names)


if __name__ == "__main__":
    unittest.main()
