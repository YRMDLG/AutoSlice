"""旧候选语义对齐与人工证据重挂接路径的同对象兼容 façade。"""

from autoslice.analysis.review import reconciliation as _owner

FACADE_EXPORTS = _owner.FACADE_EXPORTS
globals().update({
    name: value
    for name, value in vars(_owner).items()
    if not name.startswith("__") and name != "FACADE_EXPORTS"
})
