"""兼容旧导入；唯一实现位于 :mod:`autoslice.artifact_store`。"""

from _autoslice_compat import alias_module

alias_module(__name__, "autoslice.artifact_store")
