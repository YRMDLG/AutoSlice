"""兼容旧导入；唯一实现位于 ``autoslice_cover.drafts``。"""

try:
    from autocover_tool._compat import alias_module
except ModuleNotFoundError:
    from _compat import alias_module

alias_module(__name__, "autoslice_cover.drafts")
