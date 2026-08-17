"""AutoSlice 隔离 GPU 运行时安装兼容入口。"""

from _autoslice_compat import alias_module

_implementation = alias_module(__name__, "autoslice.setup_gpu_runtime")
if __name__ == "__main__":
    try:
        _implementation.setup_gpu_runtime()
    except (OSError, RuntimeError, _implementation.subprocess.SubprocessError) as exc:
        print(f"GPU 运行时安装失败: {exc}", file=_implementation.sys.stderr)
        raise SystemExit(1) from exc
