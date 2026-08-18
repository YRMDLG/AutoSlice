"""旧人工候选路径的同对象兼容 façade。"""

from autoslice.analysis.manual import candidates as _owner

FACADE_EXPORTS = _owner.FACADE_EXPORTS
manual_entry_matches_topic = _owner.manual_entry_matches_topic
is_manual_merge_target = _owner.is_manual_merge_target
merge_manual_timeline_topics = _owner.merge_manual_timeline_topics
topics_from_manual_timeline = _owner.topics_from_manual_timeline
optimized_entry_semantic_text = _owner.optimized_entry_semantic_text
manual_evidence_line = _owner.manual_evidence_line
sanitize_optimized_manual_entry = _owner.sanitize_optimized_manual_entry
