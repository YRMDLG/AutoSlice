"""旧首轮话题分析路径的同对象兼容 facade。"""

from autoslice.analysis.topic import analysis as _owner

FACADE_EXPORTS = _owner.FACADE_EXPORTS
globals().update({
    name: value
    for name, value in vars(_owner).items()
    if not name.startswith("__") and name != "FACADE_EXPORTS"
})
