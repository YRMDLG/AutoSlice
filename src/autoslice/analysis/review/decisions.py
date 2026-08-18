"""切片候选落片决策与 clip_marks 生成的唯一实现。"""

from __future__ import annotations

import re
from collections import defaultdict

from autoslice import timecode
from autoslice.analysis import boundaries as boundary_analysis
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis import evidence as candidate_evidence
from autoslice.analysis.review import deduplication as clip_deduplication
from autoslice.analysis.review import policy as clip_policy
from autoslice.analysis.review import reconciliation as candidate_reconciliation
from autoslice.analysis.review import scoring as clip_scoring
from autoslice.analysis.topic import titles as title_analysis

FACADE_EXPORTS = {
    "_topic_peak_focus_window": "topic_peak_focus_window",
    "_assign_topic_slice_window": "assign_topic_slice_window",
    "_is_content_cuttable_topic": "is_content_cuttable_topic",
    "_refresh_topic_danmaku_evidence": "refresh_topic_danmaku_evidence",
    "_append_clip_candidate_source": "append_clip_candidate_source",
    "_has_high_star_manual_evidence": "has_high_star_manual_evidence",
    "_manual_review_anchor": "manual_review_anchor",
    "_reviewed_topic_has_required_interest": "reviewed_topic_has_required_interest",
    "_assign_reviewed_semantic_slice_window": "assign_reviewed_semantic_slice_window",
    "_apply_reviewed_slice_decisions": "apply_reviewed_slice_decisions",
    "_apply_danmaku_slice_decisions": "apply_danmaku_slice_decisions",
    "_clip_marks_from_topics": "clip_marks_from_topics",
}


def topic_peak_focus_window(
    topic,
    peaks,
    window_sec=danmaku_analysis.DANMAKU_WINDOW,
):
    """返回话题内最高弹幕峰值窗口；实际切片优先围绕该窗口扩前后文。"""
    start = int(topic["start"])
    end = int(topic["end"])
    candidates = candidate_evidence.topic_peak_candidates(topic, peaks, window_sec)
    if not candidates:
        return None
    peak_start, density = max(candidates, key=lambda item: item[1])
    focus_start = max(start, int(peak_start) - clip_policy.TOPIC_FOCUS_PRE_SEC)
    focus_end = min(end, int(peak_start) + clip_policy.TOPIC_FOCUS_POST_SEC)
    if focus_end <= focus_start:
        focus_end = min(end, focus_start + window_sec)
    return {
        "start": int(focus_start),
        "end": int(max(focus_end, focus_start + 1)),
        "anchor": int(peak_start + window_sec / 2),
        "density": density,
    }


def assign_topic_slice_window(topic, peaks):
    """为话题分配较短的实际切片核心范围；报告范围仍保留完整话题。"""
    topic_start = int(topic["start"])
    topic_end = int(topic["end"])
    if topic_end <= topic_start:
        return topic

    duration = topic_end - topic_start
    fixed = topic
    peak_focus = topic_peak_focus_window(topic, peaks)
    if not peak_focus:
        fixed["can_slice"] = False
        return fixed

    fixed["slice_anchor"] = peak_focus["anchor"]
    fixed["slice_anchor_source"] = "弹幕峰值"
    fixed["slice_peak_density"] = peak_focus["density"]
    if duration <= clip_policy.TOPIC_DIRECT_SLICE_MAX_SEC:
        fixed["slice_start"] = topic_start
        fixed["slice_end"] = topic_end
        return fixed

    fixed["slice_start"] = peak_focus["start"]
    fixed["slice_end"] = peak_focus["end"]
    body = list(fixed.get("body") or [])
    note = (
        f"·切片核心：完整话题较长，实际切片围绕弹幕峰值"
        f"{timecode.format_elapsed(peak_focus['anchor'])}截取，保留峰值前后完整反应"
    )
    if note not in body:
        body.append(note)
    fixed["body"] = body
    return fixed


def is_content_cuttable_topic(topic):
    """判断话题内容本身是否适合切片，避免只有背景语音/兜底说明被高弹幕误切。"""
    if topic.get("fallback"):
        return False
    if topic.get("reference_only"):
        return False
    if topic.get("source") in {"manual_timeline", "optimized_manual_timeline"} and not topic.get(
        "ai_enriched"
    ):
        return False
    if title_analysis._is_bad_topic_title(topic.get("title", "")):
        return False
    text = " ".join([topic.get("title", "")] + list(topic.get("body") or []))
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    if any(keyword in compact for keyword in clip_policy.UNCUTTABLE_CONTENT_KEYWORDS):
        return False
    return True


def refresh_topic_danmaku_evidence(topic, peaks):
    """AI 缩小语义核心后重新计算弹幕依据，移除已落在核心外的旧峰值说明。"""
    candidates = candidate_evidence.topic_peak_candidates(topic, peaks)
    best = max(candidates, key=lambda item: item[1]) if candidates else None
    evidence = None
    if best:
        peak_start, density = best
        evidence = (
            f"·弹幕依据：{timecode.format_elapsed(peak_start)} 附近峰值约 {density:.0f} 条/分钟"
        )
    body = []
    inserted = False
    for line in topic.get("body") or []:
        if str(line).startswith("·切片核心："):
            continue
        if str(line).startswith("·弹幕依据："):
            if evidence and not inserted:
                body.append(evidence)
                inserted = True
            continue
        if evidence and not inserted and str(line).startswith("●人工时间轴"):
            body.append(evidence)
            inserted = True
        body.append(line)
    if evidence and not inserted:
        body.append(evidence)
    topic["body"] = body
    return best


def append_clip_candidate_source(topic, source):
    """记录候选的发现来源，避免峰值、语义和人工时间轴互相覆盖。"""
    sources = [
        str(value).strip()
        for value in topic.get("clip_candidate_sources") or []
        if str(value).strip()
    ]
    if source not in sources:
        sources.append(source)
    topic["clip_candidate_sources"] = sources
    topic["clip_review_candidate"] = True


def has_high_star_manual_evidence(topic):
    """高星人工时间轴只提供复核入口，必须有真实的人工证据可追溯。"""
    try:
        if int(topic.get("manual_stars", 0) or 0) < clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS:
            return False
    except (TypeError, ValueError):
        return False
    if any(str(line).startswith("●人工时间轴") for line in topic.get("body") or []):
        return True
    for entry in topic.get("manual_timeline") or []:
        if not isinstance(entry, dict):
            continue
        try:
            if int(entry.get("stars", 0) or 0) >= clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS:
                return True
        except (TypeError, ValueError):
            pass
        for item in entry.get("original_entries") or []:
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("stars", 0) or 0) >= clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def manual_review_anchor(topic):
    """为无峰值的高星候选提供审计锚点，不把它伪装成弹幕峰值。"""
    topic_start = int(topic.get("start", 0) or 0)
    topic_end = max(
        topic_start + 1,
        int(topic.get("end", topic_start + 1) or topic_start + 1),
    )
    choices = []
    for entry in topic.get("manual_timeline") or []:
        if not isinstance(entry, dict):
            continue
        originals = entry.get("original_entries") or [entry]
        for item in originals:
            if not isinstance(item, dict):
                continue
            try:
                stars = int(item.get("stars", entry.get("stars", 0)) or 0)
                start = int(item.get("start", entry.get("start", topic_start)) or topic_start)
            except (TypeError, ValueError):
                continue
            if (
                stars >= clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS
                and topic_start <= start <= topic_end
            ):
                choices.append((stars, start))
    if choices:
        return max(
            choices,
            key=lambda item: (
                item[0],
                -abs(item[1] - (topic_start + topic_end) / 2),
            ),
        )[1]
    return (topic_start + topic_end) // 2


def reviewed_topic_has_required_interest(topic):
    """新策略严格使用 Terra 分数；旧调用仅在仍有真实峰值时保持兼容。"""
    if not topic.get("clip_review_validated"):
        return False
    score = clip_scoring.parse_clip_interest_score(topic.get("clip_interest_score"))
    return score is None or score >= clip_policy.CLIP_MIN_INTEREST_SCORE


def assign_reviewed_semantic_slice_window(topic, source, anchor=None):
    """复核后的语义核心已经有完整边界，不再强行拉回已失配的弹幕峰值。"""
    topic_start = int(topic.get("start", 0) or 0)
    topic_end = max(
        topic_start + 1,
        int(topic.get("end", topic_start + 1) or topic_start + 1),
    )
    topic["slice_start"] = topic_start
    topic["slice_end"] = topic_end
    topic["slice_anchor"] = int(anchor if anchor is not None else (topic_start + topic_end) // 2)
    topic["slice_anchor_source"] = source
    topic["can_slice"] = True
    return topic


def apply_reviewed_slice_decisions(topics, peaks, avg_density, max_per_hour=None):
    """让通过 Terra 的候选按最终语义边界落片，峰值只保留为发现来源。"""
    high_energy_peaks = danmaku_analysis._high_energy_danmaku_peaks(peaks, avg_density)
    peak_features = {
        int(peak_start): danmaku_analysis._danmaku_peak_features(
            peaks,
            peak_start,
            density,
            avg_density=avg_density,
        )
        for peak_start, density in high_energy_peaks
    }
    peak_rows = []
    non_peak_rows = []
    for topic in topics:
        topic["can_slice"] = False
        for key in (
            "slice_start",
            "slice_end",
            "slice_anchor",
            "slice_anchor_source",
            "slice_peak_density",
        ):
            topic.pop(key, None)
        if not is_content_cuttable_topic(topic) or not reviewed_topic_has_required_interest(topic):
            continue

        peak_candidates = candidate_evidence.topic_peak_candidates(topic, high_energy_peaks)
        sources = [
            str(value).strip()
            for value in topic.get("clip_candidate_sources") or []
            if str(value).strip()
        ]
        # 兼容外部脚本和既有测试直接提交的、已经通过复核的峰值候选。
        if peak_candidates and not sources:
            sources = ["弹幕峰值"]
            topic["clip_candidate_sources"] = sources
        if not sources:
            continue

        if peak_candidates:
            peak_start, density = max(peak_candidates, key=lambda item: item[1])
            features = peak_features[int(peak_start)]
            ranking_score = (
                danmaku_analysis._reviewed_danmaku_ranking_score(features)
                if features.get("content_evidence")
                else float(density)
            )
            peak_rows.append(
                {
                    "topic": topic,
                    "peak_start": int(peak_start),
                    "density": float(density),
                    "ranking_score": ranking_score,
                    "anchor": int(peak_start + danmaku_analysis.DANMAKU_WINDOW / 2),
                }
            )
        elif "人工高星时间轴" in sources:
            non_peak_rows.append((topic, "人工高星时间轴", manual_review_anchor(topic)))
        else:
            non_peak_rows.append((topic, "语义复核", None))

    # 同一峰值只能对应一个片段。复核后更重绝对热度，以免局部突增把真正
    # 的全场极高峰挤掉；这也保留了旧接口传入 max_per_hour 时的兼容行为。
    peak_rows.sort(
        key=lambda row: (
            -row["ranking_score"],
            -(clip_scoring.parse_clip_interest_score(row["topic"].get("clip_interest_score")) or 0),
            row["topic"].get("start", 0),
        )
    )
    used_peak_starts = set()
    selected_per_hour = defaultdict(int)
    for row in peak_rows:
        topic = row["topic"]
        peak_start = row["peak_start"]
        hour = max(0, int(row["anchor"] // 3600))
        if peak_start in used_peak_starts:
            continue
        if max_per_hour is not None and selected_per_hour[hour] >= max_per_hour:
            continue
        topic["peak_density"] = row["density"]
        topic["density_ratio"] = round(row["density"] / avg_density, 2) if avg_density else 0
        assign_reviewed_semantic_slice_window(topic, "弹幕峰值", anchor=row["anchor"])
        used_peak_starts.add(peak_start)
        if max_per_hour is not None:
            selected_per_hour[hour] += 1

    for topic, source, anchor in sorted(
        non_peak_rows,
        key=lambda row: (
            -(clip_scoring.parse_clip_interest_score(row[0].get("clip_interest_score")) or 0),
            row[0].get("start", 0),
        ),
    ):
        effective_anchor = int(
            anchor
            if anchor is not None
            else (int(topic.get("start", 0)) + int(topic.get("end", 0))) // 2
        )
        hour = max(0, int(effective_anchor // 3600))
        if max_per_hour is not None and selected_per_hour[hour] >= max_per_hour:
            continue
        assign_reviewed_semantic_slice_window(topic, source, anchor=effective_anchor)
        if max_per_hour is not None:
            selected_per_hour[hour] += 1
    return topics


def apply_danmaku_slice_decisions(
    topics,
    peaks,
    avg_density,
    max_per_hour=None,
    require_clip_review=False,
):
    """按独立局部峰值筛选话题；生产路径不设小时配额。"""
    if not topics:
        return []
    if require_clip_review:
        return apply_reviewed_slice_decisions(
            topics,
            peaks,
            avg_density,
            max_per_hour=max_per_hour,
        )
    high_energy_peaks = danmaku_analysis._high_energy_danmaku_peaks(peaks, avg_density)
    peak_features = {
        int(peak_start): danmaku_analysis._danmaku_peak_features(
            peaks,
            peak_start,
            density,
            avg_density=avg_density,
        )
        for peak_start, density in high_energy_peaks
    }
    candidates = []
    for topic in topics:
        topic["can_slice"] = False
        topic.pop("clip_review_candidate", None)
        topic.pop("clip_candidate_sources", None)
        for key in (
            "slice_start",
            "slice_end",
            "slice_anchor",
            "slice_anchor_source",
            "slice_peak_density",
            "danmaku_peak_start",
            "danmaku_selection_score",
            "danmaku_local_baseline",
            "danmaku_local_surge_ratio",
            "danmaku_density_percentile",
            "danmaku_content_quality",
            "danmaku_interaction_signal",
            "danmaku_topic_alignment",
            "danmaku_content_evidence",
        ):
            topic.pop(key, None)
        refresh_topic_danmaku_evidence(topic, high_energy_peaks)
        peak_candidates = candidate_evidence.topic_peak_candidates(topic, high_energy_peaks)
        best_peak = (
            max(
                peak_candidates,
                key=lambda item: (
                    peak_features[int(item[0])]["selection_score"]
                    if peak_features[int(item[0])]["content_evidence"]
                    else float(item[1])
                ),
            )
            if peak_candidates
            else None
        )
        peak_density = float(best_peak[1]) if best_peak else 0.0
        topic["peak_density"] = peak_density
        topic["density_ratio"] = round(peak_density / avg_density, 2) if avg_density else 0
        if not best_peak or not is_content_cuttable_topic(topic):
            continue
        if require_clip_review and not topic.get("clip_review_validated"):
            continue
        if topic["end"] <= topic["start"]:
            continue
        peak_start, density = best_peak
        features = peak_features[int(peak_start)]
        alignment = candidate_reconciliation.danmaku_topic_alignment(
            topic,
            features.get("content_evidence"),
        )
        anchor = int(peak_start + danmaku_analysis.DANMAKU_WINDOW / 2)
        topic["danmaku_peak_start"] = int(peak_start)
        topic["danmaku_selection_score"] = features["selection_score"]
        topic["danmaku_local_baseline"] = features["local_baseline"]
        topic["danmaku_local_surge_ratio"] = features["local_surge_ratio"]
        topic["danmaku_density_percentile"] = features["density_percentile"]
        topic["danmaku_content_quality"] = features["content_quality"]
        topic["danmaku_interaction_signal"] = features["interaction_signal"]
        topic["danmaku_topic_alignment"] = alignment
        topic["danmaku_content_evidence"] = features["content_evidence"]
        if not features["content_evidence"]:
            ranking_score = float(density)
        elif require_clip_review:
            ranking_score = danmaku_analysis._reviewed_danmaku_ranking_score(features)
        else:
            ranking_score = features["selection_score"]
        candidates.append(
            {
                "topic": topic,
                "peak_start": int(peak_start),
                "density": float(density),
                "anchor": anchor,
                "ranking_score": ranking_score,
                "alignment": alignment,
            }
        )

    # 不同峰值按局部突增和内容质量排序；同一峰值优先匹配弹幕原文的话题。
    candidates.sort(
        key=lambda row: (
            -row["ranking_score"],
            -row["alignment"],
            -int(bool(row["topic"].get("ai_focus_validated"))),
            row["topic"]["end"] - row["topic"]["start"],
            row["topic"]["start"],
        )
    )
    used_peak_starts = set()
    selected_per_hour = defaultdict(int)
    for candidate in candidates:
        topic = candidate["topic"]
        peak_start = candidate["peak_start"]
        anchor = candidate["anchor"]
        hour = max(0, int(anchor // 3600))
        if peak_start in used_peak_starts:
            continue
        if max_per_hour is not None and selected_per_hour[hour] >= max_per_hour:
            continue
        topic["can_slice"] = True
        append_clip_candidate_source(topic, "弹幕峰值")
        assign_topic_slice_window(topic, [(peak_start, topic["peak_density"])])
        if not topic.get("can_slice") or topic.get("slice_anchor_source") != "弹幕峰值":
            topic["can_slice"] = False
            continue
        used_peak_starts.add(peak_start)
        if max_per_hour is not None:
            selected_per_hour[hour] += 1

    # 高星人工时间轴只增加独立字幕复核候选，不在这一阶段直接切片。
    # 这让低密度但有完整事件的片段有一次机会，同时避免按星标机械凑片。
    for topic in topics:
        if is_content_cuttable_topic(topic) and has_high_star_manual_evidence(topic):
            append_clip_candidate_source(topic, "人工高星时间轴")
    return topics


def clip_marks_from_topics(topics):
    """根据已筛选的重点话题生成 clip_marks。"""
    topic_list = list(topics or [])
    marks = []
    for topic in topic_list:
        if not (
            topic.get("can_slice")
            and topic.get("slice_anchor") is not None
            and topic.get("slice_anchor_source") in {"弹幕峰值", "语义复核", "人工高星时间轴"}
        ):
            continue
        next_topic_starts = [
            int(other.get("start", 0))
            for other in topic_list
            if (
                other is not topic
                and int(other.get("start", 0)) > int(topic.get("start", 0))
                and int(other.get("start", 0)) >= int(topic.get("end", 0)) - 5
            )
        ]
        marks.append(
            {
                "start": topic.get("slice_start", topic["start"]),
                "end": topic.get("slice_end", topic["end"]),
                "title": topic["title"],
                "publish_title": title_analysis._sanitize_transport_claims(
                    title_analysis._normalise_publish_title(
                        topic.get("publish_title"), topic["title"]
                    ),
                    topic.get("body") or [],
                ),
                **({"title_hook": topic["title_hook"]} if topic.get("title_hook") else {}),
                "report_start": topic["start"],
                "report_end": topic["end"],
                "slice_anchor": topic.get("slice_anchor"),
                "slice_anchor_source": topic.get("slice_anchor_source"),
                "clip_candidate_sources": list(topic.get("clip_candidate_sources") or []),
                "semantic_focus_validated": bool(topic.get("ai_focus_validated")),
                "editorial_interest_score": topic.get("clip_interest_score"),
                "editorial_interest_reason": topic.get("clip_interest_reason"),
                "timeline_star_bonus": topic.get("clip_timeline_star_bonus", 0),
                "reference_start": topic.get("reference_start"),
                "reference_end": topic.get("reference_end"),
                "context_requires_trigger": boundary_analysis._clip_context_requires_trigger(topic),
                "boundary_evidence": list(topic.get("body") or []),
                "next_report_topic_start": (min(next_topic_starts) if next_topic_starts else None),
            }
        )
    return clip_deduplication._dedupe_clip_marks(marks)
