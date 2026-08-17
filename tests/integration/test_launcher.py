import importlib.util
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_PATH = REPOSITORY_ROOT / "启动.py"
SPEC = importlib.util.spec_from_file_location("autoslice_launcher", LAUNCHER_PATH)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_autoslice_binds_loopback_unless_secured_lan_mode_is_explicit(self):
        self.assertEqual(launcher._autoslice_bind_host({}), "127.0.0.1")
        with self.assertRaisesRegex(RuntimeError, "强随机令牌"):
            launcher._autoslice_bind_host({"AUTOSLICE_LAN_MODE": "1"})
        with self.assertRaisesRegex(RuntimeError, "Host、Origin"):
            launcher._autoslice_bind_host({
                "AUTOSLICE_LAN_MODE": "1",
                "AUTOSLICE_LAN_TOKEN": "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6",
            })
        self.assertEqual(
            launcher._autoslice_bind_host({
                "AUTOSLICE_LAN_MODE": "1",
                "AUTOSLICE_LAN_TOKEN": "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6",
                "AUTOSLICE_LAN_HOSTS": "192.168.1.20",
                "AUTOSLICE_LAN_ORIGINS": "http://192.168.1.20:5002",
                "AUTOSLICE_ALLOWED_ROOTS": str(Path.cwd()),
            }),
            "0.0.0.0",
        )

    def test_gpu_runtime_path_stays_outside_repository(self):
        runtime = launcher._gpu_runtime_python(r"X:\fixtures\LocalAppData")

        self.assertEqual(
            runtime,
            Path(r"X:\fixtures\LocalAppData\AutoSlice\gpu-py310-cu130\Scripts\python.exe"),
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
            current_executable=r"C:\Python310\python.exe",
        )

        self.assertEqual(code, 0)
        self.assertEqual(captured["command"][0], str(runtime))
        self.assertEqual(captured["command"][-1], "--test")
        self.assertEqual(captured["env"]["AUTOSLICE_FUNASR_DEVICE"], "cuda:0")
        self.assertEqual(captured["env"]["AUTOSLICE_GPU_RUNTIME_ACTIVE"], "1")
        self.assertEqual(
            captured["env"]["AUTOSLICE_HOST_PYTHON"],
            r"C:\Python310\python.exe",
        )
        self.assertNotIn("AUTOSLICE_FUNASR_DEVICE", parent_env)

    def test_dependency_check_uses_module_specs_without_importing_funasr(self):
        available = {
            "flask": object(),
            "funasr": None,
            "soxr": object(),
            "docx": object(),
            "requests": object(),
        }

        missing = launcher._missing_dependencies(lambda name: available[name])

        self.assertEqual(missing, ["funasr"])

    def test_dependency_contract_keeps_root_autocover_and_gpu_packages_separate(self):
        root_requirements = (
            LAUNCHER_PATH.with_name("requirements.txt").read_text(encoding="utf-8")
        )
        autocover_requirements = (
            LAUNCHER_PATH.parent / "autocover_tool" / "requirements.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("requests>=2.31", root_requirements.splitlines())
        self.assertIn("requests", launcher.REQUIRED_IMPORTS)
        self.assertNotIn("Pillow", root_requirements)
        self.assertIn("Pillow", autocover_requirements)
        self.assertNotIn("torch", root_requirements.casefold())
        self.assertNotIn("PIL", launcher.REQUIRED_IMPORTS)
        self.assertNotIn("torch", launcher.REQUIRED_IMPORTS)

    def test_dependency_check_reports_missing_requests(self):
        missing = launcher._missing_dependencies(
            find_spec=lambda name: None if name == "requests" else object(),
            version_reader=lambda _name: "99.0",
        )

        self.assertEqual(missing, ["requests"])

    def test_dependency_check_upgrades_outdated_funasr(self):
        missing = launcher._missing_dependencies(
            find_spec=lambda _name: object(),
            version_reader=lambda name: "1.3.9" if name == "funasr" else "99.0",
        )

        self.assertEqual(missing, ["funasr>=1.4.1"])

    def test_dependency_install_clears_all_proxy_variants(self):
        captured = {}

        def fake_runner(_command, **kwargs):
            captured["env"] = kwargs["env"]
            return Mock(returncode=0)

        launcher._install_dependencies(runner=fake_runner)

        for key in (
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
            self.assertEqual(captured["env"][key], "")
        self.assertEqual(captured["env"]["NO_PROXY"], "*")
        self.assertEqual(captured["env"]["no_proxy"], "*")

    def test_autocover_contract_and_port_selection(self):
        self.assertTrue(launcher._is_compatible_autocover_service({
            "service": "autocover",
            "api_version": launcher.AUTOCOVER_API_VERSION,
        }))
        self.assertFalse(launcher._is_compatible_autocover_service({
            "service": "autocover",
            "api_version": 3,
        }))
        self.assertTrue(launcher._is_compatible_autoslice_service({
            "service": "autoslice",
            "api_version": 1,
            "subtitle_review_version": 4,
            "subtitle_asr_version": launcher.AUTOSLICE_SUBTITLE_ASR_VERSION,
        }))
        self.assertFalse(launcher._is_compatible_autoslice_service({
            "service": "autoslice",
            "api_version": 1,
        }))

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            self.assertEqual(
                launcher._find_available_local_port(port, attempts=2),
                port + 1,
            )

    def test_autocover_reuses_compatible_service_without_starting_process(self):
        env = {}
        process_factory = Mock()

        process, url, reused = launcher._start_autocover(
            environ=env,
            preferred_port=5010,
            service_probe=Mock(return_value={
                "service": "autocover",
                "api_version": launcher.AUTOCOVER_API_VERSION,
            }),
            process_factory=process_factory,
        )

        self.assertIsNone(process)
        self.assertEqual(url, "http://127.0.0.1:5010")
        self.assertTrue(reused)
        self.assertEqual(env["AUTOCOVER_URL"], url)
        process_factory.assert_not_called()

    def test_existing_unified_services_are_reused_and_require_healthy_cover(self):
        slice_payload = {
            "service": "autoslice",
            "api_version": 1,
            "subtitle_review_version": 4,
            "subtitle_asr_version": launcher.AUTOSLICE_SUBTITLE_ASR_VERSION,
            "autocover_url": "http://127.0.0.1:5012",
        }
        cover_payload = {
            "service": "autocover",
            "api_version": launcher.AUTOCOVER_API_VERSION,
        }
        slice_probe = Mock(return_value=slice_payload)
        cover_probe = Mock(return_value=cover_payload)

        result = launcher._existing_unified_services(slice_probe, cover_probe)

        self.assertEqual(result, {
            "autoslice_url": "http://127.0.0.1:5002",
            "autocover_url": "http://127.0.0.1:5012",
        })
        slice_probe.assert_called_once_with(5002)
        cover_probe.assert_called_once_with(5012)

        with self.assertRaisesRegex(RuntimeError, "AutoCover 未就绪"):
            launcher._existing_unified_services(
                Mock(return_value=slice_payload),
                Mock(return_value=None),
            )
        with self.assertRaisesRegex(RuntimeError, "旧版 AutoSlice"):
            launcher._existing_unified_services(
                Mock(return_value={
                    "service": "autoslice",
                    "api_version": 1,
                    "autocover_url": "http://127.0.0.1:5012",
                }),
                Mock(return_value=cover_payload),
            )
        invalid_payload = dict(slice_payload, autocover_url="http://127.0.0.1:bad")
        with self.assertRaisesRegex(RuntimeError, "没有有效"):
            launcher._existing_unified_services(
                Mock(return_value=invalid_payload),
                Mock(),
            )

    def test_autocover_starts_with_host_python_and_selected_port(self):
        with TemporaryDirectory() as directory:
            cover_dir = Path(directory) / "AutoCover"
            cover_dir.mkdir()
            process = Mock()
            process.poll.return_value = None
            process_factory = Mock(return_value=process)
            dependency_setup = Mock()
            waiter = Mock(return_value=True)
            env = {"AUTOSLICE_HOST_PYTHON": r"C:\Python310\python.exe"}

            result_process, url, reused = launcher._start_autocover(
                environ=env,
                project_dir=cover_dir,
                preferred_port=5010,
                service_probe=Mock(return_value=None),
                port_finder=Mock(return_value=5011),
                dependency_setup=dependency_setup,
                process_factory=process_factory,
                service_waiter=waiter,
            )

        self.assertIs(result_process, process)
        self.assertEqual(url, "http://127.0.0.1:5011")
        self.assertFalse(reused)
        dependency_setup.assert_called_once_with(
            Path(r"C:\Python310\python.exe"), cover_dir
        )
        command = process_factory.call_args.args[0]
        self.assertEqual(command, [
            r"C:\Python310\python.exe",
            "-m", "autocover.cli", "serve",
            "--port", "5011", "--no-browser",
        ])
        self.assertEqual(process_factory.call_args.kwargs["cwd"], str(cover_dir))
        self.assertEqual(process_factory.call_args.kwargs["env"]["PYTHONUTF8"], "1")
        waiter.assert_called_once_with(5011, process)
        self.assertEqual(env["AUTOCOVER_URL"], url)

    def test_autocover_start_failure_stops_child_and_exit_stops_only_owned_process(self):
        with TemporaryDirectory() as directory:
            cover_dir = Path(directory) / "AutoCover"
            cover_dir.mkdir()
            failed_process = Mock()
            failed_process.poll.return_value = None

            with self.assertRaisesRegex(RuntimeError, "AutoCover 启动失败"):
                launcher._start_autocover(
                    environ={},
                    project_dir=cover_dir,
                    service_probe=Mock(return_value=None),
                    port_finder=Mock(return_value=5010),
                    dependency_setup=Mock(),
                    process_factory=Mock(return_value=failed_process),
                    service_waiter=Mock(return_value=False),
                )

        failed_process.terminate.assert_called_once_with()
        failed_process.wait.assert_called_once_with(timeout=5)

        exited_process = Mock()
        exited_process.poll.return_value = 0
        launcher._stop_autocover(exited_process)
        exited_process.terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
