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
    def run_spec(self, *, missing_resource="", missing_module="", pythonpath=""):
        """pythonpath 是打包前调用者环境里的 PYTHONPATH，可用 {src} 代表临时目录里的源码目录。"""

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
                environment = {"PYTHONPATH": pythonpath.format(src=root / "src")}
                with patch.dict(sys.modules, {"PyInstaller.utils.hooks": hooks}), patch.dict(os.environ, environment):
                    namespace = runpy.run_path(str(SPEC), init_globals={
                        "SPECPATH": str(root / "packaging"),
                        "Analysis": lambda *args, **kwargs: types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=kwargs["datas"]),
                        "PYZ": lambda *args, **kwargs: None,
                        "EXE": lambda *args, **kwargs: None,
                        "COLLECT": lambda *args, **kwargs: None,
                    })
                    namespace["final_pythonpath"] = os.environ["PYTHONPATH"]
                    return namespace
            finally:
                sys.path[:] = previous_path

    def test_source_path_is_available_before_collection(self):
        self.assertIn("autoslice.desktop.asr.worker", self.run_spec()["hiddenimports"])

    def test_pythonpath_puts_source_first_without_empty_or_duplicate_entries(self):
        sep = os.pathsep
        for previous, others in (
            ("", []), (sep, []), ("other", ["other"]), (f"{{src}}{sep}other", ["other"]),
            (f"other{sep}{{src}}{sep}", ["other"]), (f"a{sep}{sep}b", ["a", "b"]),
        ):
            with self.subTest(previous=previous):
                namespace = self.run_spec(pythonpath=previous)
                self.assertEqual(namespace["final_pythonpath"].split(sep), [str(namespace["SRC"]), *others])

    def test_smoke_test_runs_without_the_source_tree_on_pythonpath(self):
        script = (SPEC.parent / "build_windows.ps1").read_text(encoding="utf-8-sig")
        setting = script.index('$env:PYTHONPATH = Join-Path $Root "src"')
        pyinstaller = script.index("-m PyInstaller")
        cleanup = script.index("Remove-Item Env:PYTHONPATH")
        smoke = script.index('"--smoke ')
        # 只在打包阶段设置；自检启动前必须已经清掉，否则缺失的模块会被源码目录补上而假通过
        self.assertLess(setting, pyinstaller)
        self.assertLess(pyinstaller, cleanup)
        self.assertLess(cleanup, smoke)
        self.assertEqual(script.count("$env:PYTHONPATH ="), 1)

    def test_missing_resource_fails_before_analysis(self):
        with self.assertRaisesRegex(SystemExit, "seto-bilibili.ttf"):
            self.run_spec(missing_resource="seto-bilibili.ttf")

    def test_fixed_hidden_imports_cannot_mask_missing_project_modules(self):
        with self.assertRaisesRegex(SystemExit, "autoslice.desktop.asr.worker"):
            self.run_spec(missing_module="autoslice.desktop.asr.worker")
