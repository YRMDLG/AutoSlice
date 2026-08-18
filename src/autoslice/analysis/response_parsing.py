"""旧话题响应解析路径的同对象兼容 façade。"""

from autoslice.analysis.topic import response as _owner

FACADE_EXPORTS = _owner.FACADE_EXPORTS
NO_SLICE_HINTS = _owner.NO_SLICE_HINTS
is_slice_marked = _owner.is_slice_marked
json_can_slice = _owner.json_can_slice
