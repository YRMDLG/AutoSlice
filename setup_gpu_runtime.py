"""为 AutoSlice 安装隔离的 CUDA FunASR 运行时。"""

import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


TORCH_WHEEL_NAME = "torch-2.12.1+cu130-cp310-cp310-win_amd64.whl"
TORCH_WHEEL_URL = (
    "https://download-r2.pytorch.org/whl/cu130/"
    "torch-2.12.1%2Bcu130-cp310-cp310-win_amd64.whl"
)
TORCH_WHEEL_SHA256 = "3b6e6e3ce55c3ebd688b00001cd44ff1a43fa30823f0394d20c8fd9910fb7087"


def _runtime_root():
    configured = os.environ.get("AUTOSLICE_GPU_RUNTIME_DIR")
    if configured:
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("找不到 LOCALAPPDATA，无法创建隔离运行时。")
    return Path(local_app_data) / "AutoSlice"


def _runtime_python(root):
    return root / "gpu-py310-cu130" / "Scripts" / "python.exe"


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _gpu_runtime_is_healthy(python_path):
    if not python_path.is_file():
        return False
    result = subprocess.run(
        [
            str(python_path),
            "-c",
            "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    return result.returncode == 0


def _download_wheel(wheel_path):
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("找不到 curl，无法下载官方 CUDA PyTorch wheel。")
    wheel_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 CUDA PyTorch（约 1.9 GB，可断点续传）: {wheel_path}")
    subprocess.run(
        [
            curl,
            "-L",
            "--fail",
            "--retry", "3",
            "--continue-at", "-",
            "--output", str(wheel_path),
            TORCH_WHEEL_URL,
        ],
        check=True,
    )


def setup_gpu_runtime():
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("当前安装脚本仅支持 64 位 Windows。")
    if sys.version_info[:2] != (3, 10):
        raise RuntimeError("当前 CUDA wheel 需要 Python 3.10。")

    root = _runtime_root()
    runtime_python = _runtime_python(root)
    if _gpu_runtime_is_healthy(runtime_python):
        print(f"隔离 CUDA 运行时已经可用: {runtime_python}")
        return runtime_python

    wheel_path = root / "downloads" / TORCH_WHEEL_NAME
    if not wheel_path.is_file():
        _download_wheel(wheel_path)
    print("校验官方 SHA-256...")
    if _sha256_file(wheel_path) != TORCH_WHEEL_SHA256:
        wheel_path.unlink(missing_ok=True)
        print("已有 wheel 校验失败，正在重新完整下载...")
        _download_wheel(wheel_path)
        if _sha256_file(wheel_path) != TORCH_WHEEL_SHA256:
            raise RuntimeError("CUDA PyTorch wheel 的 SHA-256 校验失败，已停止安装。")

    runtime_dir = runtime_python.parent.parent
    if not runtime_python.is_file():
        print(f"创建隔离 Python 环境: {runtime_dir}")
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(runtime_dir)],
            check=True,
        )

    print("安装 CUDA PyTorch 到隔离环境...")
    subprocess.run(
        [str(runtime_python), "-m", "pip", "install", "--no-deps", str(wheel_path)],
        check=True,
    )
    if not _gpu_runtime_is_healthy(runtime_python):
        raise RuntimeError(
            "CUDA 运行时安装完成但显卡健康检查失败；AutoSlice 会继续使用 CPU。"
        )
    print(f"安装完成: {runtime_python}")
    print("以后运行 `python 启动.py` 会自动使用 GPU 转录。")
    return runtime_python


if __name__ == "__main__":
    try:
        setup_gpu_runtime()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"GPU 运行时安装失败: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
