"""旧切片候选落片决策路径的同对象兼容 façade。"""

from autoslice.analysis.review import decisions as _owner

FACADE_EXPORTS = _owner.FACADE_EXPORTS
globals().update({
    name: value
    for name, value in vars(_owner).items()
    if not name.startswith("__") and name != "FACADE_EXPORTS"
})
