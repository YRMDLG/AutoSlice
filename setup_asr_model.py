"""AutoSlice ASR 模型安装兼容入口。"""

from _autoslice_compat import alias_module

_implementation = alias_module(__name__, "autoslice.setup_asr_model")
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
