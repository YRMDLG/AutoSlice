"""兼容旧导入；唯一实现位于 :mod:`autoslice.runtime_config`。"""

from _autoslice_compat import alias_module

alias_module(__name__, "autoslice.runtime_config")
