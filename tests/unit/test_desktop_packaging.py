"""离线验证 spec 的收集顺序和缺失资源门禁，不调用真实打包器。"""

import os
import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = Path(__file__).resolve().parents[2] / "packaging" / "autoslice-desktop.spec"


class PackagingContractTests(unittest.TestCase):
    def run_spec(self, *, missing_resource="", missing_module=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "build" / "worker_src" / "autoslice").mkdir(parents=True)
            modules = ["autoslice.desktop.qt_app.window", "autoslice.desktop.asr.worker", "autoslice_cover.document_render"]
            data = []
            for name in ("ResourceHanRoundedCN-Regular.ttf", "ResourceHanRoundedCN-Medium.ttf", "streamer_profiles.json", "seto-bilibili.ttf"):
                path = root / name
                path.touch()
                if name != missing_resource:
                    data.append((str(path), "resources"))
            hooks = types.ModuleType("PyInstaller.utils.hooks")

            def collect(package, **kwargs):
                self.assertEqual(sys.path[0], str(root / "src"))
                self.assertEqual(os.environ["PYTHONPATH"].split(os.pathsep)[0], str(root / "src"))
                return [name for name in modules if name.startswith(package + ".") and name != missing_module]

            hooks.collect_submodules = collect
            hooks.collect_data_files = lambda package: data if package == "autoslice" else []
            previous_path = list(sys.path)
            try:
                with patch.dict(sys.modules, {"PyInstaller.utils.hooks": hooks}), patch.dict(os.environ, {"PYTHONPATH": ""}):
                    return runpy.run_path(str(SPEC), init_globals={
                        "SPECPATH": str(root / "packaging"),
                        "Analysis": lambda *args, **kwargs: types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=kwargs["datas"]),
                        "PYZ": lambda *args, **kwargs: None,
                        "EXE": lambda *args, **kwargs: None,
                        "COLLECT": lambda *args, **kwargs: None,
                    })
            finally:
                sys.path[:] = previous_path

    def test_source_path_is_available_before_collection(self):
        self.assertIn("autoslice.desktop.asr.worker", self.run_spec()["hiddenimports"])

    def test_missing_resource_fails_before_analysis(self):
        with self.assertRaisesRegex(SystemExit, "seto-bilibili.ttf"):
            self.run_spec(missing_resource="seto-bilibili.ttf")

    def test_fixed_hidden_imports_cannot_mask_missing_project_modules(self):
        with self.assertRaisesRegex(SystemExit, "autoslice.desktop.asr.worker"):
            self.run_spec(missing_module="autoslice.desktop.asr.worker")
