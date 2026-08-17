"""兼容旧 AutoCover Web 导入；唯一实现位于 ``autoslice_cover.app``。"""

try:
    from ._compat import alias_module
except ImportError:  # 兼容在 autocover_tool 目录直接执行 ``python app.py``
    from _compat import alias_module

_implementation = alias_module(__name__, "autoslice_cover.app")
if __name__ == "__main__":
    _implementation.app.run(host="127.0.0.1", port=5010, debug=False)
