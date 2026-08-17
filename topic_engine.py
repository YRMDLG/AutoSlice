"""兼容旧导入和脚本入口；唯一实现位于 :mod:`autoslice.topic_engine`。"""

from _autoslice_compat import alias_module

_implementation = alias_module(__name__, "autoslice.topic_engine")
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
