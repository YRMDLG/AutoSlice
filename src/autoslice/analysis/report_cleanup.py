"""报告话题正文清理、去重与局部重叠修复的唯一实现。"""

from __future__ import annotations

from autoslice import timecode
from autoslice.analysis import boundaries as boundary_analysis
from autoslice.analysis import candidate_reconciliation, content_normalization
from autoslice.analysis import timeline as timeline_analysis
from autoslice.analysis import titles as title_analysis

FACADE_EXPORTS = {
    "_clean_topics_for_report": "clean_topics_for_report",
    "_report_fact_lines": "report_fact_lines",
    "_resolve_reviewed_report_overlaps": "resolve_reviewed_report_overlaps",
    "_trim_report_topic_around_reviewed_topic": ("trim_report_topic_around_reviewed_topic"),
}


def report_fact_lines(topic):
    """返回用于识别报告重复事件的正文事实，排除密度和人工证据标签。"""
    facts = []
    for line in topic.get("body") or []:
        value = str(line)
        if value.startswith(
            (
                "●人工时间轴",
                "·时间轴",
                "·弹幕依据：",
                "·切片核心：",
                "·参考投稿标题",
            )
        ):
            continue
        clean = title_analysis._strip_body_prefix(value)
        if clean:
            facts.append(clean)
    return facts


def trim_report_topic_around_reviewed_topic(topic, reviewed_topic, trim_start):
    """让普通报告话题避开已复核核心，并移除被核心重复覆盖的事实。"""
    fixed = dict(topic)
    if trim_start:
        fixed["start"] = int(reviewed_topic["end"])
    else:
        fixed["end"] = int(reviewed_topic["start"])
    if int(fixed["end"]) - int(fixed["start"]) < 30:
        return None

    reviewed_facts = report_fact_lines(reviewed_topic)
    body = []
    removed_fact = False
    for line in fixed.get("body") or []:
        clean = title_analysis._strip_body_prefix(str(line))
        is_fact = clean and not str(line).startswith(
            (
                "●人工时间轴",
                "·时间轴",
                "·弹幕依据：",
                "·切片核心：",
                "·参考投稿标题",
            )
        )
        if is_fact and any(
            timeline_analysis._manual_alignment_score(clean, reviewed) >= 0.20
            for reviewed in reviewed_facts
        ):
            removed_fact = True
            continue
        body.append(line)
    fixed["body"] = body
    fixed["start_str"] = timecode.format_elapsed(fixed["start"])
    fixed["end_str"] = timecode.format_elapsed(fixed["end"])
    fixed = candidate_reconciliation.reconcile_topic_manual_evidence(fixed)

    if removed_fact:
        remaining_facts = report_fact_lines(fixed)
        rebuilt_title = title_analysis._derive_topic_title(
            "",
            [f"·{fact}" for fact in remaining_facts],
        )
        if rebuilt_title:
            fixed["title"] = rebuilt_title
            fixed["publish_title"] = title_analysis._fallback_publish_title(rebuilt_title)
    return fixed if report_fact_lines(fixed) else None


def resolve_reviewed_report_overlaps(topics, max_overlap_sec=120):
    """具体复核话题优先，修正相邻普通话题在报告中的局部重叠。"""
    resolved = sorted(
        [dict(topic) for topic in topics or []],
        key=lambda item: (item.get("start", 0), item.get("end", 0)),
    )
    index = 0
    while index + 1 < len(resolved):
        current = resolved[index]
        following = resolved[index + 1]
        overlap = min(int(current["end"]), int(following["end"])) - max(
            int(current["start"]), int(following["start"])
        )
        if overlap <= 0 or overlap > max_overlap_sec:
            index += 1
            continue
        current_reviewed = current.get("clip_review_validated") is True
        following_reviewed = following.get("clip_review_validated") is True
        if current_reviewed == following_reviewed:
            index += 1
            continue

        if current_reviewed:
            trimmed = trim_report_topic_around_reviewed_topic(
                following,
                current,
                trim_start=True,
            )
            if trimmed is None:
                resolved.pop(index + 1)
            else:
                resolved[index + 1] = trimmed
                index += 1
            continue

        if int(current["end"]) <= int(following["end"]):
            trimmed = trim_report_topic_around_reviewed_topic(
                current,
                following,
                trim_start=False,
            )
            if trimmed is None:
                resolved.pop(index)
            else:
                resolved[index] = trimmed
                index += 1
            continue
        index += 1
    return resolved


def clean_topics_for_report(topics):
    """生成报告/切片前做最后一道清洗，防止坏标题或提示残留漏网。"""
    prepared = []
    for topic in topics or []:
        if topic.get("fallback"):
            prepared.append(topic)
            continue
        topic = candidate_reconciliation.reconcile_topic_manual_evidence(topic)
        body_lines = [
            content_normalization.normalise_body_line(line) for line in topic.get("body") or []
        ]
        body_lines = [line for line in body_lines if line]
        if not body_lines:
            continue
        title = title_analysis._derive_topic_title(topic.get("title", ""), body_lines)
        if not title:
            continue
        fact_lines = report_fact_lines({"body": body_lines})
        title_rebuilt = False
        if (
            fact_lines
            and max(timeline_analysis._manual_alignment_score(title, fact) for fact in fact_lines)
            == 0
        ):
            rebuilt_title = title_analysis._derive_topic_title(
                "",
                [f"·{fact}" for fact in fact_lines],
            )
            if rebuilt_title:
                title = rebuilt_title
                title_rebuilt = True
        fixed = dict(topic)
        fixed["title"] = title
        fixed["body"] = body_lines
        publish_title = (
            title_analysis._fallback_publish_title(title)
            if title_rebuilt
            else title_analysis._normalise_publish_title(fixed.get("publish_title"), title)
        )
        fixed["publish_title"] = title_analysis._sanitize_transport_claims(
            publish_title,
            body_lines,
        )
        prepared.append(fixed)

    # 具体 AI/字幕话题优先去重。十分钟兜底段最后处理，避免它先占住
    # 整个范围后把内部已经二次复核的短话题误判为重复项。
    cleaned = []
    for fixed in sorted(
        prepared,
        key=lambda item: (
            bool(item.get("fallback")),
            item.get("start", 0),
            item.get("end", 0),
        ),
    ):
        if boundary_analysis._is_duplicate_topic(fixed, cleaned):
            continue
        cleaned.append(fixed)
    cleaned = resolve_reviewed_report_overlaps(cleaned)
    meaningful_hours = {
        int(topic.get("start", 0)) // 3600 for topic in cleaned if not topic.get("fallback")
    }
    cleaned = [
        topic
        for topic in cleaned
        if not (topic.get("fallback") and int(topic.get("start", 0)) // 3600 in meaningful_hours)
    ]
    return sorted(
        cleaned,
        key=lambda item: (item.get("start", 0), item.get("end", 0)),
    )
