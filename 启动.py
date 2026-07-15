"""
AutoSlice 一键启动
用法: python 启动.py
Ctrl+C 完全停止
"""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys


PROJECT_DIR = Path(__file__).resolve().parent
GPU_RUNTIME_RELATIVE_PATH = Path("AutoSlice") / "gpu-py310-cu130" / "Scripts" / "python.exe"
REQUIRED_IMPORTS = ("flask", "funasr", "docx")


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


def _run_gpu_child(runtime_python, argv=None, environ=None, runner=subprocess.run):
    child_env = dict(environ if environ is not None else os.environ)
    child_env["AUTOSLICE_GPU_RUNTIME_ACTIVE"] = "1"
    child_env["AUTOSLICE_FUNASR_DEVICE"] = "cuda:0"
    command = [
        str(runtime_python),
        str(Path(__file__).resolve()),
        *(list(argv) if argv is not None else sys.argv[1:]),
    ]
    try:
        return runner(command, env=child_env).returncode
    except KeyboardInterrupt:
        return 130


def _missing_dependencies(find_spec=importlib.util.find_spec):
    return [name for name in REQUIRED_IMPORTS if find_spec(name) is None]


def _install_dependencies(runner=subprocess.run):
    env = {**os.environ, "HTTP_PROXY": "", "HTTPS_PROXY": ""}
    result = runner(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
        cwd=str(PROJECT_DIR),
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError("依赖安装失败，请检查网络后重新启动。")


def main():
    os.chdir(PROJECT_DIR)
    print("=" * 50)
    print("  AutoSlice - 泽音Melody 智能切片")
    print("=" * 50)

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

    print("\n[1/2] 检查依赖...")
    missing = _missing_dependencies()
    if missing:
        print(f"  缺少依赖: {', '.join(missing)}，正在安装...")
        _install_dependencies()

    print("[2/2] 启动 Web 服务...")
    print("\n  浏览器打开: http://localhost:5002")
    print("  按 Ctrl+C 停止\n")
    print("=" * 50 + "\n")

    sys.path.insert(0, str(PROJECT_DIR))
    from app import app

    device = os.environ.get("AUTOSLICE_FUNASR_DEVICE", "auto")
    print(f"AutoSlice Web 已启动: http://localhost:5002（FunASR: {device}）")
    print("控制台将实时显示所有任务进度")
    app.run(host="0.0.0.0", port=5002, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
