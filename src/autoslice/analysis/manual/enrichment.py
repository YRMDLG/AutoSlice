"""人工时间轴 AI 富化结果的校验与应用。"""

import re

from autoslice import timecode
from autoslice.analysis import clip_policy, content_normalization
from autoslice.analysis import titles as title_analysis

FACADE_EXPORTS = {
    "_MANUAL_AI_PLACEHOLDER_PHRASES": "MANUAL_AI_PLACEHOLDER_PHRASES",
    "_enriched_manual_topic_from_item": "enrich_manual_topic_from_item",
    "_is_manual_ai_placeholder": "is_manual_ai_placeholder",
    "_validated_ai_focus_range": "validated_ai_focus_range",
}


MANUAL_AI_PLACEHOLDER_PHRASES = (
    "5-15字具体短标题",
    "具体发生了什么",
    "主播如何回应",
    "具体事件钩子",
    "结果或原话",
)


def is_manual_ai_placeholder(value):
    """识别人工时间轴富化响应中的模板占位文本。"""
    compact = re.sub(r"\s+", "", str(value or ""))
    dynamic_placeholder = f"{title_analysis._prompt_streamer_name()}如何回应"
    return (
        not compact
        or dynamic_placeholder in compact
        or any(phrase in compact for phrase in MANUAL_AI_PLACEHOLDER_PHRASES)
    )


def validated_ai_focus_range(item, topic):
    """校验 AI 建议的语义核心范围；无效时继续使用程序候选范围。"""
    try:
        focus_start = timecode.parse_hms(str(item.get("focus_start", "")))
        focus_end = timecode.parse_hms(str(item.get("focus_end", "")))
    except (TypeError, ValueError):
        return None
    source_start = int(topic["start"])
    source_end = int(topic["end"])
    duration = focus_end - focus_start
    if not source_start <= focus_start < focus_end <= source_end:
        return None
    if duration < 10 or duration > clip_policy.TOPIC_REVIEW_FOCUS_MAX_SEC:
        return None
    return focus_start, focus_end


def enrich_manual_topic_from_item(topic, item):
    """把一项 AI 复核结果应用到候选副本；无有效正文时返回 ``None``。"""
    points = [
        point
        for point in content_normalization.filter_unsupported_ai_points(
            content_normalization.json_points_to_body(item.get("points"))
        )
        if not is_manual_ai_placeholder(title_analysis._strip_body_prefix(point))
    ]
    if not points:
        return None

    enriched = dict(topic)
    raw_title = title_analysis._clean_topic_title(str(item.get("title", topic.get("title", ""))))
    if is_manual_ai_placeholder(raw_title) or title_analysis._is_incomplete_ai_title(raw_title):
        raw_title = ""
    title = title_analysis._derive_topic_title(raw_title, points)
    if (
        not title
        or is_manual_ai_placeholder(title)
        or title_analysis._is_incomplete_ai_title(title)
    ):
        return None

    preserved_evidence = [
        line
        for line in topic.get("body") or []
        if line.startswith("·弹幕依据：") or line.startswith("●人工时间轴")
    ]
    body = list(points)
    for line in preserved_evidence:
        if line not in body:
            body.append(line)

    evidence_lines = list(topic.get("body") or []) + points
    title = title_analysis._sanitize_transport_claims(title, evidence_lines)
    enriched["title"] = title
    publish_title = item.get("publish_title")
    if title_analysis._is_incomplete_ai_title(publish_title):
        publish_title = None
    enriched["publish_title"] = title_analysis._sanitize_transport_claims(
        title_analysis._normalise_publish_title(publish_title, title),
        evidence_lines,
    )
    if topic.get("publish_title_locked"):
        enriched["publish_title"] = title_analysis._normalise_publish_title(
            topic.get("publish_title"),
            topic.get("title", title),
        )
        enriched["publish_title_locked"] = True
        enriched["publish_title_source"] = topic.get("publish_title_source") or "human_review"

    title_hook = title_analysis._normalise_title_hook(item.get("title_hook"))
    if title_hook:
        enriched["title_hook"] = title_hook
    enriched["body"] = body
    enriched["ai_enriched"] = True
    enriched["postcheck_pending"] = False
    enriched["postcheck_validated"] = True
    enriched.pop("reference_only", None)

    focus_range = validated_ai_focus_range(item, topic)
    if focus_range:
        source_start = int(topic["start"])
        source_end = int(topic["end"])
        enriched["reference_start"] = source_start
        enriched["reference_end"] = source_end
        enriched["start"], enriched["end"] = focus_range
        enriched["start_str"] = timecode.format_elapsed(enriched["start"])
        enriched["end_str"] = timecode.format_elapsed(enriched["end"])
        enriched["ai_focus_validated"] = True
    return enriched
