"""各引擎共用的运行环境小工具（设备选择、显存峰值）。单独成模块，避免引擎和注册表互相导入。"""

from __future__ import annotations


def resolve_device(requested: str) -> str:
    """auto → 有 CUDA 用 cuda:0，否则 cpu。"""

    value = (requested or "auto").strip().lower()
    if value in ("cuda", "gpu"):
        return "cuda:0"
    if value != "auto":
        return value
    try:
        import torch

        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except (ImportError, OSError, RuntimeError):
        return "cpu"


def cuda_peak_mb() -> float:
    """本进程到目前为止的显存峰值（MB）；没有 CUDA 返回 0。"""

    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.max_memory_allocated() / (1024 * 1024)
    except (ImportError, OSError, RuntimeError):
        pass
    return 0.0
