import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock


LAUNCHER_PATH = Path(__file__).with_name("启动.py")
SPEC = importlib.util.spec_from_file_location("autoslice_launcher", LAUNCHER_PATH)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_gpu_runtime_path_stays_outside_repository(self):
        runtime = launcher._gpu_runtime_python(r"C:\Users\测试\AppData\Local")

        self.assertEqual(
            runtime,
            Path(r"C:\Users\测试\AppData\Local\AutoSlice\gpu-py310-cu130\Scripts\python.exe"),
        )

    def test_gpu_runtime_health_check_requires_file_and_cuda_probe(self):
        with TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "python.exe"
            runtime.write_bytes(b"runtime")
            successful_runner = Mock(return_value=Mock(returncode=0))
            failed_runner = Mock(return_value=Mock(returncode=1))

            self.assertTrue(launcher._gpu_runtime_is_healthy(runtime, successful_runner))
            self.assertFalse(launcher._gpu_runtime_is_healthy(runtime, failed_runner))
            self.assertFalse(
                launcher._gpu_runtime_is_healthy(Path(tmp) / "missing.exe", successful_runner)
            )

        command = successful_runner.call_args.args[0]
        self.assertEqual(command[0], str(runtime))
        self.assertIn("torch.cuda.is_available", command[2])

    def test_gpu_runtime_selection_respects_cpu_and_active_child(self):
        base_env = {"LOCALAPPDATA": r"C:\Runtime"}
        healthy = Mock(return_value=True)

        selected = launcher._select_gpu_runtime(
            environ=base_env,
            current_executable=r"C:\Python310\python.exe",
            health_check=healthy,
        )
        self.assertEqual(
            selected,
            Path(r"C:\Runtime\AutoSlice\gpu-py310-cu130\Scripts\python.exe"),
        )

        for extra in (
            {"AUTOSLICE_FUNASR_DEVICE": "cpu"},
            {"AUTOSLICE_GPU_RUNTIME_ACTIVE": "1"},
            {"AUTOSLICE_DISABLE_GPU": "1"},
        ):
            env = {**base_env, **extra}
            self.assertIsNone(
                launcher._select_gpu_runtime(
                    environ=env,
                    current_executable=r"C:\Python310\python.exe",
                    health_check=healthy,
                )
            )

    def test_gpu_child_receives_cuda_device_without_mutating_parent_env(self):
        captured = {}

        def fake_runner(command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return Mock(returncode=0)

        parent_env = {"LOCALAPPDATA": r"C:\Runtime", "KEEP_ME": "yes"}
        runtime = Path(r"C:\Runtime\AutoSlice\gpu-py310-cu130\Scripts\python.exe")
        code = launcher._run_gpu_child(
            runtime,
            argv=["--test"],
            environ=parent_env,
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(captured["command"][0], str(runtime))
        self.assertEqual(captured["command"][-1], "--test")
        self.assertEqual(captured["env"]["AUTOSLICE_FUNASR_DEVICE"], "cuda:0")
        self.assertEqual(captured["env"]["AUTOSLICE_GPU_RUNTIME_ACTIVE"], "1")
        self.assertNotIn("AUTOSLICE_FUNASR_DEVICE", parent_env)

    def test_dependency_check_uses_module_specs_without_importing_funasr(self):
        available = {"flask": object(), "funasr": None, "docx": object()}

        missing = launcher._missing_dependencies(lambda name: available[name])

        self.assertEqual(missing, ["funasr"])


if __name__ == "__main__":
    unittest.main()
