"""
AutoSlice 一键启动
用法: python 启动.py
Ctrl+C 完全停止
"""

import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from runtime_config import (
    AUTOCOVER_DIR,
    AUTOCOVER_INPUT_DIR,
    COVER_OUTPUT_DIR,
    STICKER_DIR,
    apply_local_environment,
)
from security_policy import SecurityConfigurationError, SecurityPolicy
from autocover_tool.autocover import (
    API_VERSION as AUTOCOVER_API_VERSION,
    SERVICE_ID as AUTOCOVER_SERVICE_ID,
)


PROJECT_DIR = Path(__file__).resolve().parent
GPU_RUNTIME_RELATIVE_PATH = Path("AutoSlice") / "gpu-py310-cu130" / "Scripts" / "python.exe"
REQUIRED_IMPORTS = ("flask", "funasr", "soxr", "docx", "requests")
MINIMUM_PACKAGE_VERSIONS = {"funasr": "1.4.1"}
AUTOCOVER_PROJECT_DIR = AUTOCOVER_DIR
AUTOCOVER_PREFERRED_PORT = 5010
AUTOCOVER_START_TIMEOUT = 20.0
AUTOSLICE_SERVICE_ID = "autoslice"
AUTOSLICE_API_VERSION = 1
# 与 app.py/subtitle_workflow.py 保持一致；旧进程缺少该字段时不得静默复用。
AUTOSLICE_SUBTITLE_REVIEW_VERSION = 4
AUTOSLICE_SUBTITLE_ASR_VERSION = 2
AUTOSLICE_PORT = 5002


def _gpu_runtime_python(local_app_data=None):
    base_dir = local_app_data or os.environ.get("LOCALAPPDATA")
    if not base_dir:
        return None
    return Path(base_dir) / GPU_RUNTIME_RELATIVE_PATH


def _same_executable(first, second):
    if not first or not second:
        return False
    try:
        return Path(first).resolve() == Path(second).resolve()
    except OSError:
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _gpu_runtime_is_healthy(runtime_python, runner=subprocess.run):
    if not runtime_python or not Path(runtime_python).is_file():
        return False
    probe = (
        "import torch; "
        "raise SystemExit(0 if torch.cuda.is_available() else 1)"
    )
    try:
        result = runner(
            [str(runtime_python), "-c", probe],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _select_gpu_runtime(
        environ=None, current_executable=None, health_check=_gpu_runtime_is_healthy):
    env = environ if environ is not None else os.environ
    requested_device = str(env.get("AUTOSLICE_FUNASR_DEVICE", "auto")).strip().lower()
    if (
            env.get("AUTOSLICE_GPU_RUNTIME_ACTIVE") == "1"
            or env.get("AUTOSLICE_DISABLE_GPU") == "1"
            or requested_device == "cpu"):
        return None
    runtime_python = _gpu_runtime_python(env.get("LOCALAPPDATA"))
    executable = current_executable or sys.executable
    if _same_executable(executable, runtime_python):
        return None
    return runtime_python if health_check(runtime_python) else None


def _run_gpu_child(
        runtime_python, argv=None, environ=None, runner=subprocess.run,
        current_executable=None):
    child_env = dict(environ if environ is not None else os.environ)
    child_env["AUTOSLICE_GPU_RUNTIME_ACTIVE"] = "1"
    child_env["AUTOSLICE_FUNASR_DEVICE"] = "cuda:0"
    child_env.setdefault(
        "AUTOSLICE_HOST_PYTHON",
        str(current_executable or sys.executable),
    )
    command = [
        str(runtime_python),
        str(Path(__file__).resolve()),
        *(list(argv) if argv is not None else sys.argv[1:]),
    ]
    try:
        return runner(command, env=child_env).returncode
    except KeyboardInterrupt:
        return 130


def _version_tuple(value):
    numbers = [int(item) for item in re.findall(r"\d+", str(value or ""))[:3]]
    return tuple((numbers + [0, 0, 0])[:3])


def _missing_dependencies(
        find_spec=importlib.util.find_spec,
        version_reader=importlib.metadata.version):
    missing = [name for name in REQUIRED_IMPORTS if find_spec(name) is None]
    for package_name, minimum in MINIMUM_PACKAGE_VERSIONS.items():
        if package_name in missing:
            continue
        try:
            installed = version_reader(package_name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(package_name)
            continue
        if _version_tuple(installed) < _version_tuple(minimum):
            missing.append(f"{package_name}>={minimum}")
    return missing


def _install_dependencies(runner=subprocess.run):
    env = {**os.environ}
    for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy"):
        env[key] = ""
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    result = runner(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
        cwd=str(PROJECT_DIR),
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError("依赖安装失败，请检查网络后重新启动。")


def _is_compatible_autocover_service(payload):
    return bool(
        payload
        and payload.get("service") == AUTOCOVER_SERVICE_ID
        and payload.get("api_version") == AUTOCOVER_API_VERSION
    )


def _probe_autocover_service(port, timeout=0.8):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/options",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


def _probe_autoslice_service(port=AUTOSLICE_PORT, timeout=0.8):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/service",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_compatible_autoslice_service(payload):
    return bool(
        payload
        and payload.get("service") == AUTOSLICE_SERVICE_ID
        and payload.get("api_version") == AUTOSLICE_API_VERSION
        and payload.get("subtitle_review_version")
        == AUTOSLICE_SUBTITLE_REVIEW_VERSION
        and payload.get("subtitle_asr_version")
        == AUTOSLICE_SUBTITLE_ASR_VERSION
    )


def _existing_unified_services(autoslice_probe=None, autocover_probe=None):
    probe_slice = autoslice_probe or _probe_autoslice_service
    slice_payload = probe_slice(AUTOSLICE_PORT)
    if (
            isinstance(slice_payload, dict)
            and slice_payload.get("service") == AUTOSLICE_SERVICE_ID
            and slice_payload.get("api_version") == AUTOSLICE_API_VERSION
            and (
                slice_payload.get("subtitle_review_version")
                != AUTOSLICE_SUBTITLE_REVIEW_VERSION
                or slice_payload.get("subtitle_asr_version")
                != AUTOSLICE_SUBTITLE_ASR_VERSION
            )
    ):
        raise RuntimeError(
            "检测到旧版 AutoSlice 服务仍在运行；请先在旧窗口按 Ctrl+C，"
            "再重新执行 python 启动.py，以加载最新字幕校对与背景音过滤规则。"
        )
    if not _is_compatible_autoslice_service(slice_payload):
        return None

    cover_url = str(slice_payload.get("autocover_url", "")).strip().rstrip("/")
    parsed = None
    try:
        parsed = urllib.parse.urlsplit(cover_url)
        cover_port = parsed.port
    except ValueError:
        cover_port = None
    if (
            parsed is None
            or parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or cover_port is None):
        raise RuntimeError("已运行的 AutoSlice 没有有效的 AutoCover 地址，请关闭旧窗口后重启。")

    probe_cover = autocover_probe or _probe_autocover_service
    if not _is_compatible_autocover_service(probe_cover(cover_port)):
        raise RuntimeError("AutoSlice 已在运行，但 AutoCover 未就绪，请关闭旧窗口后重启。")
    return {
        "autoslice_url": f"http://127.0.0.1:{AUTOSLICE_PORT}",
        "autocover_url": cover_url,
    }


def _find_available_local_port(preferred, attempts=20):
    if not 1 <= int(preferred) <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    for port in range(int(preferred), min(65536, int(preferred) + attempts)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"端口 {preferred} 起连续 {attempts} 个端口均被占用")


def _autocover_python(environ=None, current_executable=None):
    env = environ if environ is not None else os.environ
    return Path(
        env.get("AUTOSLICE_HOST_PYTHON")
        or current_executable
        or sys.executable
    )


def _ensure_autocover_dependencies(python_executable, project_dir, runner=subprocess.run):
    probe = runner(
        [str(python_executable), "-c", "import flask; from PIL import Image"],
        cwd=str(project_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode == 0:
        return
    requirements = Path(project_dir) / "requirements.txt"
    if not requirements.is_file():
        raise RuntimeError("AutoCover 缺少 requirements.txt")
    env = {**os.environ, "HTTP_PROXY": "", "HTTPS_PROXY": ""}
    result = runner(
        [
            str(python_executable), "-m", "pip", "install",
            "-r", str(requirements), "-q",
        ],
        cwd=str(project_dir),
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError("AutoCover 依赖安装失败，请检查网络后重新启动。")


def _wait_for_autocover(port, process, timeout=AUTOCOVER_START_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_compatible_autocover_service(_probe_autocover_service(port)):
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.1)
    return False


def _stop_autocover(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _autoslice_bind_host(environ=None):
    """使用与 Flask 请求边界相同的 LAN 配置决定监听地址。"""

    env = environ if environ is not None else os.environ
    policy = SecurityPolicy(
        env_prefix="AUTOSLICE",
        cookie_name="autoslice_local_session",
        access_header="X-AutoSlice-Token",
        environ=env,
    )
    try:
        return policy.bind_host()
    except SecurityConfigurationError as exc:
        raise RuntimeError(str(exc)) from exc


def _start_autocover(
        environ=None, project_dir=None, preferred_port=AUTOCOVER_PREFERRED_PORT,
        service_probe=None, port_finder=None, dependency_setup=None,
        process_factory=None, service_waiter=None):
    env = environ if environ is not None else os.environ
    cover_dir = Path(project_dir or AUTOCOVER_PROJECT_DIR)
    probe = service_probe or _probe_autocover_service
    existing = probe(preferred_port)
    if _is_compatible_autocover_service(existing):
        url = f"http://127.0.0.1:{preferred_port}"
        env["AUTOCOVER_URL"] = url
        return None, url, True
    if not cover_dir.is_dir():
        raise RuntimeError(f"AutoCover 项目不存在: {cover_dir}")

    select_port = port_finder or _find_available_local_port
    selected_port = select_port(preferred_port)
    python_executable = _autocover_python(env)
    prepare_dependencies = dependency_setup or _ensure_autocover_dependencies
    prepare_dependencies(python_executable, cover_dir)

    factory = process_factory or subprocess.Popen
    child_env = dict(env)
    child_env["PYTHONUTF8"] = "1"
    child_env.setdefault("AUTOCOVER_INPUT_DIR", str(AUTOCOVER_INPUT_DIR))
    child_env.setdefault("AUTOCOVER_OUTPUT_DIR", str(COVER_OUTPUT_DIR))
    child_env.setdefault("AUTOCOVER_STICKER_DIR", str(STICKER_DIR))
    process = factory(
        [
            str(python_executable), "-m", "autocover.cli", "serve",
            "--port", str(selected_port), "--no-browser",
        ],
        cwd=str(cover_dir),
        env=child_env,
    )
    waiter = service_waiter or _wait_for_autocover
    if not waiter(selected_port, process):
        _stop_autocover(process)
        raise RuntimeError("AutoCover 启动失败，请检查上方错误信息。")

    url = f"http://127.0.0.1:{selected_port}"
    env["AUTOCOVER_URL"] = url
    return process, url, False


def main():
    apply_local_environment()
    os.chdir(PROJECT_DIR)
    print("=" * 50)
    print("  AutoSlice - 智能切片")
    print("=" * 50)

    try:
        existing_services = _existing_unified_services()
    except RuntimeError as exc:
        print(f"\n启动失败: {exc}")
        return 1
    if existing_services:
        print("\n检测到统一服务已经运行，本次不再重复启动。")
        print(f"  AutoSlice: {existing_services['autoslice_url']}")
        print(f"  AutoCover: {existing_services['autocover_url']}")
        return 0

    runtime_python = _select_gpu_runtime()
    if runtime_python:
        print("\n检测到隔离 CUDA 运行时，正在切换 RTX 语音转录...")
        return _run_gpu_child(runtime_python)

    os.environ.setdefault("MODELSCOPE_LOCAL_ONLY", "1")
    local_gpu_python = _gpu_runtime_python()
    if _same_executable(sys.executable, local_gpu_python):
        os.environ.setdefault("AUTOSLICE_FUNASR_DEVICE", "cuda:0")
    else:
        os.environ.setdefault("AUTOSLICE_FUNASR_DEVICE", "auto")

    print("\n[1/3] 检查依赖...")
    missing = _missing_dependencies()
    if missing:
        print(f"  缺少依赖: {', '.join(missing)}，正在安装...")
        _install_dependencies()

    print("[2/3] 启动 AutoCover 封面服务...")
    cover_process = None
    try:
        cover_process, cover_url, reused_cover = _start_autocover()
        cover_state = "复用已有服务" if reused_cover else "已随 AutoSlice 启动"
        print(f"  AutoCover: {cover_url}（{cover_state}）")

        print("[3/3] 启动 AutoSlice Web 服务...")
        print("\n  浏览器打开: http://localhost:5002")
        print("  自动封面入口: http://localhost:5002/autocover")
        print("  按 Ctrl+C 同时停止本次启动的服务\n")
        print("=" * 50 + "\n")

        sys.path.insert(0, str(PROJECT_DIR))
        from app import app
        from topic_engine import funasr_public_status

        asr_status = funasr_public_status()
        print(
            "AutoSlice Web 已启动: http://localhost:5002"
            f"（FunASR: {asr_status['display_name']} · {asr_status['device']}）"
        )
        if asr_status["needs_setup"]:
            print(f"  FunASR 提示: {asr_status['recommendation']}")
        print(
            "  识别调整: autoslice.local.json 的 AUTOSLICE_FUNASR_HOTWORDS；"
            "固定纠错见 streamer_profiles.json 的 asr_replacements"
        )
        print("控制台将实时显示所有任务进度")
        app.run(
            host=_autoslice_bind_host(),
            port=5002,
            debug=False,
            threaded=True,
        )
    finally:
        _stop_autocover(cover_process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
