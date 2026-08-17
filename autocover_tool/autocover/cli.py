"""兼容旧导入；唯一实现位于 ``autoslice_cover.cli``。"""

try:
    from autocover_tool._compat import alias_module
except ModuleNotFoundError:
    from _compat import alias_module

_implementation = alias_module(__name__, "autoslice_cover.cli")
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
