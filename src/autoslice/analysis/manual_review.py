"""旧人工时间轴复核路径的同对象兼容 façade。"""

from autoslice.analysis.manual import review as _owner

FACADE_EXPORTS = _owner.FACADE_EXPORTS
build_manual_topic_enrichment_prompt = _owner.build_manual_topic_enrichment_prompt
enrich_manual_topics_with_llm = _owner.enrich_manual_topics_with_llm
enrich_manual_topics_in_batches = _owner.enrich_manual_topics_in_batches
validate_unmatched_manual_topics = _owner.validate_unmatched_manual_topics
