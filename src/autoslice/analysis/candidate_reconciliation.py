"""候选语义对齐与人工证据重挂接的唯一实现。"""

from __future__ import annotations

import math

from autoslice.analysis import clip_policy
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis.manual import candidates as manual_candidates
from autoslice.analysis.manual import timebase as timeline_analysis
from autoslice.analysis import titles as title_analysis

FACADE_EXPORTS = {
    "_danmaku_topic_alignment": "danmaku_topic_alignment",
    "_manual_entry_meaningfully_overlaps_topic": (
        "manual_entry_meaningfully_overlaps_topic"
    ),
    "_reconcile_topic_manual_evidence": "reconcile_topic_manual_evidence",
    "_topic_semantic_text": "topic_semantic_text",
}


def topic_semantic_text(topic):
    parts = [str(topic.get("title", ""))]
    for line in topic.get("body") or []:
        value = str(line)
        if value.startswith((
                "●人工时间轴", "·时间轴", "·弹幕依据：", "·切片核心：",
                "·参考投稿标题",
        )):
            continue
        clean = title_analysis._strip_body_prefix(value)
        if clean:
            parts.append(clean)
    return " ".join(parts)


def danmaku_topic_alignment(topic, evidence):
    """衡量代表弹幕与话题事实是否一致，避免峰值挂到相邻话题。"""
    if not isinstance(evidence, dict):
        return 0.0
    semantic_text = topic_semantic_text(topic)
    if not semantic_text:
        return 0.0
    messages = evidence.get("representative_messages") or []
    scored = []
    for item in messages:
        text = danmaku_analysis._clean_ass_danmaku_text(item.get("text", ""))
        if not text or danmaku_analysis._is_generic_danmaku_reaction(text):
            continue
        score = timeline_analysis.manual_alignment_score(text, semantic_text)
        if score <= 0:
            continue
        weight = 1.0 + math.log1p(max(1, int(item.get("count", 1) or 1)))
        scored.append((score, weight))
    if not scored:
        return 0.0
    scored.sort(key=lambda item: item[0], reverse=True)
    strongest = scored[0][0]
    weighted_average = sum(score * weight for score, weight in scored[:3]) / sum(
        weight for _, weight in scored[:3]
    )
    return round(strongest * 0.70 + weighted_average * 0.30, 4)


def manual_entry_meaningfully_overlaps_topic(entry, topic):
    topic_start = int(topic.get("start", 0))
    topic_end = max(topic_start + 1, int(topic.get("end", topic_start + 1)))
    entry_start = int(entry.get("start", 0))
    entry_end = max(entry_start + 1, int(entry.get("end", entry_start + 1)))
    overlap = max(0, min(topic_end, entry_end) - max(topic_start, entry_start))
    if overlap <= 0:
        return False
    entry_duration = max(1, entry_end - entry_start)
    topic_duration = max(1, topic_end - topic_start)
    return (
        overlap >= 20
        or overlap / entry_duration >= 0.5
        or overlap / topic_duration >= 0.25
    )


def reconcile_topic_manual_evidence(topic):
    """按 AI 最终语义边界重新挂接人工证据，移除相邻事件和误星标。"""
    fixed = dict(topic)
    manual_entries = [
        entry for entry in fixed.get("manual_timeline") or []
        if isinstance(entry, dict)
    ]
    if not manual_entries:
        return fixed

    semantic_text = topic_semantic_text(fixed)
    retained_entries = []
    retained_evidence = []
    seen_evidence = set()
    for raw_entry in manual_entries:
        entry = (
            manual_candidates.sanitize_optimized_manual_entry(raw_entry)
            if raw_entry.get("source") == "optimized_manual_timeline"
            or raw_entry.get("original_entries")
            else dict(raw_entry)
        )
        if not entry or not manual_entry_meaningfully_overlaps_topic(entry, fixed):
            continue
        entry_supports_topic = timeline_analysis.manual_text_supports_candidate(
            manual_candidates.optimized_entry_semantic_text(entry), semantic_text
        )
        if not entry_supports_topic:
            continue

        original_entries = [
            dict(item)
            for item in entry.get("original_entries") or []
            if isinstance(item, dict)
        ]
        if original_entries:
            relevant_originals = []
            for item in original_entries:
                if timeline_analysis.manual_text_supports_candidate(
                        item.get("text", ""), semantic_text):
                    relevant_originals.append(item)
                    continue

                # 高星原句经常是整段对话的收尾、反问或梗点，AI 摘要会把
                # 它改写成概述，逐字匹配不足不代表属于相邻话题。仅当优化后
                # 的整条时间轴已和当前话题语义相符、原句明确落在话题内部时
                # 保留，仍由后续 Terra 独立复核决定是否可切。
                try:
                    stars = int(item.get("stars", 0) or 0)
                    item_start = int(item.get("start", 0) or 0)
                except (TypeError, ValueError):
                    continue
                topic_start = int(fixed.get("start", 0) or 0)
                topic_end = max(
                    topic_start + 1,
                    int(fixed.get("end", topic_start + 1) or topic_start + 1),
                )
                if (
                    entry_supports_topic
                    and stars >= clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS
                    and topic_start + 5 <= item_start <= topic_end - 5
                ):
                    relevant_originals.append(item)
            if not relevant_originals:
                continue
            entry["original_entries"] = relevant_originals
            evidence_entries = relevant_originals
            entry_stars = max(
                int(item.get("stars", 0) or 0)
                for item in relevant_originals
            )
            entry["stars"] = entry_stars
            entry["highlight"] = entry_stars > 0
        else:
            evidence_entries = [entry]

        retained_entries.append(entry)
        for evidence_entry in evidence_entries:
            key = (
                int(evidence_entry.get("start", 0) or 0),
                str(evidence_entry.get("text", "")).strip(),
            )
            if not key[1] or key in seen_evidence:
                continue
            seen_evidence.add(key)
            retained_evidence.append(
                manual_candidates.manual_evidence_line(evidence_entry)
            )

    body = [
        str(line)
        for line in fixed.get("body") or []
        if not str(line).startswith(("●人工时间轴", "·时间轴"))
    ]
    body.extend(line for line in retained_evidence if line not in body)
    fixed["body"] = body
    fixed["manual_timeline"] = retained_entries
    fixed["manual_stars"] = max(
        [0]
        + [int(entry.get("stars", 0) or 0) for entry in retained_entries]
    )
    return fixed
