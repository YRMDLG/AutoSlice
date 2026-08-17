"""已弃用的 ``autocover`` 包名；生产实现位于 ``autoslice_cover``。"""

try:
    from autocover_tool._compat import alias_module
except ModuleNotFoundError:  # 兼容把 autocover_tool 加入 sys.path 的旧调用方
    from _compat import alias_module

_implementation = alias_module("_autoslice_cover_owner", "autoslice_cover")

__version__ = _implementation.__version__
SERVICE_ID = _implementation.SERVICE_ID
API_VERSION = _implementation.API_VERSION

__all__ = ["API_VERSION", "SERVICE_ID", "__version__"]
