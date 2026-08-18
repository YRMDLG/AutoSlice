"""人工时间轴候选构造、合并与优化结果清理的唯一实现。"""

from __future__ import annotations

import re

from autoslice import timecode
from autoslice.analysis import boundaries as boundary_analysis
from autoslice.analysis import clip_policy
from autoslice.analysis import evidence as candidate_evidence
from autoslice.analysis import timeline as timeline_analysis
from autoslice.analysis import titles as title_analysis

FACADE_EXPORTS = {
    "_is_manual_merge_target": "is_manual_merge_target",
    "_manual_entry_matches_topic": "manual_entry_matches_topic",
    "_manual_evidence_line": "manual_evidence_line",
    "_merge_manual_timeline_topics": "merge_manual_timeline_topics",
    "_optimized_entry_semantic_text": "optimized_entry_semantic_text",
    "_sanitize_optimized_manual_entry": "sanitize_optimized_manual_entry",
    "_topics_from_manual_timeline": "topics_from_manual_timeline",
}


def manual_entry_matches_topic(entry, topic, margin=0):
    """判断人工记录范围是否与话题范围相交。"""
    start = int(topic["start"]) - margin
    end = int(topic["end"]) + margin
    entry_start = int(entry["start"])
    entry_end = max(entry_start + 1, int(entry.get("end", entry_start + 1)))
    return entry_start < end and entry_end > start


def is_manual_merge_target(topic):
    """人工重点只合并到真实话题，避免兜底或泛话题吞掉重点。"""
    if topic.get("fallback"):
        return False
    if topic.get("source") == "manual_timeline":
        return True
    if title_analysis._is_bad_topic_title(topic.get("title", "")):
        return False
    if topic.get("title") in title_analysis._GENERIC_TOPIC_TITLES:
        return False
    text = " ".join([topic.get("title", "")] + list(topic.get("body") or []))
    compact = re.sub(r"\s+", "", text)
    return not any(
        keyword in compact
        for keyword in clip_policy.UNCUTTABLE_CONTENT_KEYWORDS
    )


def merge_manual_timeline_topics(topics, entries):
    """后置对照优化时间轴；命中只附证据，遗漏候选必须再次复核。"""
    if not entries:
        return topics
    for topic in topics:
        if not is_manual_merge_target(topic):
            continue
        matched = [
            entry
            for entry in entries
            if manual_entry_matches_topic(entry, topic)
        ]
        if not matched:
            continue
        existing_entries = list(topic.get("manual_timeline") or [])
        for entry in matched:
            if entry not in existing_entries:
                existing_entries.append(entry)
        topic["manual_stars"] = max(
            [topic.get("manual_stars", 0)]
            + [entry.get("stars", 0) for entry in existing_entries]
        )
        topic["manual_timeline"] = existing_entries
        body = list(topic.get("body") or [])
        for entry in matched:
            if entry.get("stars", 0) <= 0:
                continue
            stars = "⭐" * min(entry.get("stars", 0), 5)
            line = (
                f"●人工时间轴{stars}："
                f"{timecode.format_elapsed(entry['start'])} {entry['text']}"
            )
            if line not in body:
                body.append(line)
        topic["body"] = body

    for entry in entries:
        optimized = entry.get("source") == "optimized_manual_timeline"
        if entry.get("stars", 0) <= 0 and not optimized:
            continue
        if any(entry in (topic.get("manual_timeline") or []) for topic in topics):
            continue
        if any(
            is_manual_merge_target(topic)
            and manual_entry_matches_topic(entry, topic)
            for topic in topics
        ):
            continue
        topic_start = (
            max(0, int(entry["start"]))
            if optimized
            else max(
                0,
                int(entry["start"])
                - timeline_analysis.MANUAL_TIMELINE_TOPIC_PRE_SEC,
            )
        )
        topic_end = (
            max(topic_start + 1, int(entry.get("end", topic_start + 1)))
            if optimized
            else int(entry["start"])
            + timeline_analysis.MANUAL_TIMELINE_TOPIC_POST_SEC
        )
        topic = {
            "start": topic_start,
            "end": topic_end,
            "start_str": timecode.format_elapsed(topic_start),
            "end_str": timecode.format_elapsed(topic_end),
            "title": title_analysis._manual_title_from_text(entry["text"]),
            "can_slice": False,
            "body": list(entry.get("summary") or [])
            + [
                f"●人工时间轴{'⭐' * min(entry.get('stars', 0), 5)}："
                f"{timecode.format_elapsed(entry['start'])} {entry['text']}"
            ],
            "manual_stars": entry.get("stars", 0),
            "manual_timeline": [entry],
            "source": entry.get("source", "manual_timeline"),
            # 时间轴优化阶段只负责整理候选。首轮遗漏后必须再做一次独立复核，
            # 成功前不能把优化阶段的 ai_enriched 当作切片许可。
            "ai_enriched": False if optimized else bool(entry.get("ai_enriched")),
            "ai_focus_validated": (
                False if optimized else bool(entry.get("ai_focus_validated"))
            ),
            "postcheck_pending": optimized,
            "reference_only": optimized,
            "publish_title": entry.get("publish_title"),
        }
        existing_topics = [
            old for old in topics if is_manual_merge_target(old)
        ]
        if not boundary_analysis._is_duplicate_topic(topic, existing_topics):
            topics.append(topic)
    topics.sort(key=lambda item: (item["start"], item["end"]))
    return topics


def topics_from_manual_timeline(
    entries,
    srt_segments=None,
    peaks=None,
    max_gap_sec=240,
    max_group_duration_sec=None,
):
    """基于字幕和弹幕生成话题，人工时间轴只作为辅助参考和校准。"""
    sorted_entries = sorted(entries or [], key=lambda item: item["start"])
    groups = []
    current = []
    for entry in sorted_entries:
        if entry.get("explicit_range"):
            if current:
                groups.append(current)
                current = []
            groups.append([entry])
            continue
        if not current:
            current = [entry]
            continue
        same_hour = int(entry["start"] // 3600) == int(
            current[-1]["start"] // 3600
        )
        within_group_duration = (
            max_group_duration_sec is None
            or entry["start"] - current[0]["start"] <= max_group_duration_sec
        )
        if (
            same_hour
            and within_group_duration
            and entry["start"] - current[-1]["start"] <= max_gap_sec
        ):
            current.append(entry)
        else:
            groups.append(current)
            current = [entry]
    if current:
        groups.append(current)

    topics = []
    for group in groups:
        starred_entries = [item for item in group if item.get("stars", 0) > 0]
        if starred_entries and any(
            item.get("alignment_score") is not None for item in starred_entries
        ):
            title_entry = max(
                starred_entries,
                key=lambda item: (
                    float(item.get("alignment_score") or 0),
                    len(str(item.get("text", ""))),
                ),
            )
        else:
            title_entry = starred_entries[0] if starred_entries else group[0]
        explicit_end = (
            group[0].get("end")
            if len(group) == 1 and group[0].get("explicit_range")
            else None
        )
        if explicit_end is not None:
            start = max(0, int(group[0]["start"]))
            end = max(start + 1, int(explicit_end))
        else:
            start = max(
                0,
                int(group[0]["start"])
                - (
                    timeline_analysis.MANUAL_TIMELINE_TOPIC_PRE_SEC
                    if title_entry.get("stars", 0)
                    else 0
                ),
            )
            end = int(group[-1]["start"]) + (
                timeline_analysis.MANUAL_TIMELINE_TOPIC_POST_SEC
                if title_entry.get("stars", 0)
                else 120
            )
        body = []
        body.extend(
            candidate_evidence.topic_danmaku_reference_lines(
                start,
                end,
                peaks or [],
            )
        )
        body.extend(
            candidate_evidence.topic_srt_summary_lines(
                start,
                end,
                srt_segments or [],
            )
        )
        for item in group:
            time_label = timecode.format_elapsed(item["start"])
            if item.get("stars", 0) > 0:
                stars = "⭐" * min(item.get("stars", 0), 5)
                body.append(f"●人工时间轴{stars}：{time_label} {item['text']}")
            else:
                body.append(f"·时间轴：{time_label} {item['text']}")
            if item.get("reference_publish_title"):
                body.append(
                    "·参考投稿标题（仅供核对）："
                    f"{item['reference_publish_title']}"
                )
        topic = {
            "start": start,
            "end": end,
            "start_str": timecode.format_elapsed(start),
            "end_str": timecode.format_elapsed(end),
            "title": title_analysis._manual_title_from_text(title_entry["text"]),
            "can_slice": False,
            "body": body,
            "manual_stars": max(item.get("stars", 0) for item in group),
            "manual_timeline": group,
            "source": "subtitle_danmaku_with_manual_reference",
        }
        topics.append(topic)
    return topics


def optimized_entry_semantic_text(entry):
    """汇总优化时间轴条目的标题和摘要语义。"""
    return " ".join(
        [
            str(entry.get("text", "")),
            *[str(point) for point in entry.get("summary") or []],
        ]
    ).strip()


def manual_evidence_line(entry):
    """以真实星标和原始时间生成一行人工证据。"""
    stars = max(0, int(entry.get("stars", 0) or 0))
    prefix = f"●人工时间轴{'⭐' * min(stars, 5)}" if stars else "·时间轴"
    return (
        f"{prefix}：{timecode.format_elapsed(int(entry.get('start', 0)))} "
        f"{entry.get('text', '')}"
    )


def sanitize_optimized_manual_entry(entry):
    """过滤与原人工记录无关的 AI 改写，并移除误并入的原始星标。"""
    fixed = dict(entry or {})
    original_entries = [
        dict(item)
        for item in fixed.get("original_entries") or []
        if isinstance(item, dict)
    ]
    if not original_entries:
        return fixed

    semantic_text = optimized_entry_semantic_text(fixed)
    grounded_entries = [
        item
        for item in original_entries
        if timeline_analysis._manual_text_supports_candidate(
            item.get("text", ""),
            semantic_text,
        )
    ]
    if not grounded_entries:
        return None

    fixed["original_entries"] = grounded_entries
    stars = max(int(item.get("stars", 0) or 0) for item in grounded_entries)
    fixed["stars"] = stars
    fixed["highlight"] = stars > 0
    if grounded_entries[0].get("clock"):
        fixed["clock"] = grounded_entries[0]["clock"]

    evidence = [
        str(line)
        for line in fixed.get("evidence") or []
        if not str(line).startswith(("●人工时间轴", "·时间轴"))
    ]
    for item in grounded_entries:
        line = manual_evidence_line(item)
        if line not in evidence:
            evidence.append(line)
    fixed["evidence"] = evidence
    return fixed
