"""旧切片复核评分路径的同对象兼容 façade。"""

from autoslice.analysis.review import scoring as _owner

FACADE_EXPORTS = _owner.FACADE_EXPORTS
globals().update({
    name: value
    for name, value in vars(_owner).items()
    if not name.startswith("__") and name != "FACADE_EXPORTS"
})
