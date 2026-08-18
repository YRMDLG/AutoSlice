"""旧报告清理路径的同对象兼容 façade。"""

from autoslice.analysis.report import cleanup as _owner

FACADE_EXPORTS = _owner.FACADE_EXPORTS
report_fact_lines = _owner.report_fact_lines
trim_report_topic_around_reviewed_topic = _owner.trim_report_topic_around_reviewed_topic
resolve_reviewed_report_overlaps = _owner.resolve_reviewed_report_overlaps
clean_topics_for_report = _owner.clean_topics_for_report
