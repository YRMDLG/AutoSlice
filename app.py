"""兼容旧导入；Web 实现位于 :mod:`autoslice.web.app`。"""

from _autoslice_compat import alias_module

alias_module(__name__, "autoslice.web.app")
