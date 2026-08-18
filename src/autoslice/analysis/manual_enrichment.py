"""旧人工时间轴富化路径的同对象兼容 façade。"""

from autoslice.analysis.manual import enrichment as _owner

FACADE_EXPORTS = _owner.FACADE_EXPORTS
MANUAL_AI_PLACEHOLDER_PHRASES = _owner.MANUAL_AI_PLACEHOLDER_PHRASES
is_manual_ai_placeholder = _owner.is_manual_ai_placeholder
validated_ai_focus_range = _owner.validated_ai_focus_range
enrich_manual_topic_from_item = _owner.enrich_manual_topic_from_item
