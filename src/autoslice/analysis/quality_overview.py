"""最终切片与边缘候选质量概览的唯一确定性实现。"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from autoslice import timecode
from autoslice.analysis.review import policy as clip_policy
from autoslice.analysis.review import scoring as clip_scoring

EDGE_CANDIDATE_MIN_SCORE = 60.0
EDGE_CANDIDATE_MAX_SCORE = clip_policy.CLIP_MIN_INTEREST_SCORE
MAX_CLIP_ROWS = 60
MAX_EDGE_CANDIDATE_ROWS = 30
MAX_ANCHOR_SOURCE_ROWS = 20
MAX_TITLE_CHARS = 120
MAX_REASON_CHARS = 240
MAX_ANCHOR_SOURCE_CHARS = 40
MAX_OVERVIEW_BYTES = 48 * 1024

_LOCAL_PATH_PATTERN = re.compile(
    r"(?i)(?:file:" r"//|[a-z]:[\\/]|\\\\[^\\/\s]+[\\/]"
    r"|(?:^|[\s(（])/(?:home|users|var|tmp|etc|opt|mnt|srv|root)(?:/|$)"
    r"|(?:^|[\s(（])\.{1,2}[\\/])"
)


def _finite_non_negative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 1)


def _clean_text(value: Any, fallback: str, maximum: int) -> str:
    if value is None or isinstance(value, bool):
        return fallback
    if isinstance(value, float) and not math.isfinite(value):
        return fallback
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    text = re.sub(r"\s+", " ", text)
    if not text or _LOCAL_PATH_PATTERN.search(text):
        return fallback
    if len(text) <= maximum:
        return text
    return text[:maximum - 1].rstrip() + "…"


def _duration_seconds(item: Mapping[str, Any]) -> int:
    start = _finite_non_negative_number(item.get("start"))
    end = _finite_non_negative_number(item.get("end"))
    if start is None or end is None or end <= start:
        return 0
    return max(0, int(round(end - start)))


def _score_sort_key(row: Mapping[str, Any], field: str) -> tuple[Any, ...]:
    score = row.get(field)
    start = row.get("start")
    return (
        score is None,
        -(float(score) if score is not None else 0.0),
        float(start) if start is not None else math.inf,
        str(row.get("title") or ""),
    )


def _clip_row(mark: Mapping[str, Any]) -> dict[str, Any]:
    peak_density = mark.get("peak_density")
    if peak_density is None:
        peak_density = mark.get("slice_peak_density")
    raw_score = mark.get("editorial_interest_score")
    return {
        "title": _clean_text(
            mark.get("publish_title") or mark.get("title"),
            "未命名切片",
            MAX_TITLE_CHARS,
        ),
        "start": _finite_non_negative_number(mark.get("start")),
        "end": _finite_non_negative_number(mark.get("end")),
        "duration": _duration_seconds(mark),
        "score": (
            None
            if isinstance(raw_score, bool)
            else clip_scoring.parse_clip_interest_score(raw_score)
        ),
        "peak_density": _finite_non_negative_number(peak_density),
        "anchor_source": _clean_text(
            mark.get("slice_anchor_source"),
            "锚点未记录",
            MAX_ANCHOR_SOURCE_CHARS,
        ),
        "reason": _clean_text(
            mark.get("editorial_interest_reason"),
            "投稿价值理由未记录",
            MAX_REASON_CHARS,
        ),
    }


def _edge_candidate_row(candidate: Mapping[str, Any]) -> dict[str, Any]:
    start = _finite_non_negative_number(candidate.get("start"))
    end = _finite_non_negative_number(candidate.get("end"))
    fallback_range = "时间未记录"
    if start is not None and end is not None and end >= start:
        try:
            fallback_range = (
                f"{timecode.format_elapsed(start)}－"
                f"{timecode.format_elapsed(end)}"
            )
        except (OverflowError, ValueError):
            fallback_range = "时间未记录"
    raw_score = candidate.get("interest_score")
    return {
        "title": _clean_text(
            candidate.get("title"),
            "未命名候选",
            MAX_TITLE_CHARS,
        ),
        "time_range": _clean_text(
            candidate.get("time_range"),
            fallback_range,
            48,
        ),
        "score": (
            None
            if isinstance(raw_score, bool)
            else clip_scoring.parse_clip_interest_score(raw_score)
        ),
        "reason": _clean_text(
            candidate.get("interest_reason"),
            "投稿价值理由未记录",
            MAX_REASON_CHARS,
        ),
    }


def _bounded_overview(overview: dict[str, Any]) -> dict[str, Any]:
    while len(json.dumps(
            overview,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
    ).encode("utf-8")) > MAX_OVERVIEW_BYTES:
        clips = overview["clips"]
        edges = overview["edge_candidates"]
        if len(clips) >= len(edges) and clips:
            clips.pop()
            overview["clips_truncated_count"] += 1
        elif edges:
            edges.pop()
            overview["edge_candidates_truncated_count"] += 1
        else:
            break
    return overview


def build_quality_overview(
        clip_marks: Any,
        candidate_review_audit: Any,
) -> dict[str, Any]:
    """生成仅用于结果排序和选择、不参与配额或切片决策的概览。"""

    marks = clip_marks if isinstance(clip_marks, Sequence) and not isinstance(
        clip_marks, (str, bytes, bytearray)
    ) else ()
    all_clips = [_clip_row(mark) for mark in marks if isinstance(mark, Mapping)]
    all_clips.sort(key=lambda row: _score_sort_key(row, "score"))

    durations = [row["duration"] for row in all_clips]
    scored = [row["score"] for row in all_clips if row["score"] is not None]
    duration_buckets = {
        "under_90_seconds": sum(value < 90 for value in durations),
        "from_90_to_179_seconds": sum(90 <= value < 180 for value in durations),
        "at_least_180_seconds": sum(value >= 180 for value in durations),
    }
    anchor_counts = Counter(row["anchor_source"] for row in all_clips)
    ordered_anchor_counts = sorted(
        anchor_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )
    compact_anchor_counts = dict(sorted(
        ordered_anchor_counts[:MAX_ANCHOR_SOURCE_ROWS]
    ))
    omitted_anchor_count = sum(
        count for _source, count in ordered_anchor_counts[MAX_ANCHOR_SOURCE_ROWS:]
    )
    if omitted_anchor_count:
        compact_anchor_counts["其他"] = (
            compact_anchor_counts.get("其他", 0) + omitted_anchor_count
        )

    audit = candidate_review_audit if isinstance(candidate_review_audit, Mapping) else {}
    candidates = audit.get("candidates")
    candidates = candidates if isinstance(candidates, Sequence) and not isinstance(
        candidates, (str, bytes, bytearray)
    ) else ()
    edge_candidates = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        row = _edge_candidate_row(candidate)
        if (
                row["score"] is not None
                and EDGE_CANDIDATE_MIN_SCORE
                <= row["score"]
                < EDGE_CANDIDATE_MAX_SCORE):
            edge_candidates.append(row)
    edge_candidates.sort(key=lambda row: _score_sort_key(row, "score"))

    overview = {
        "overview_version": 1,
        "final_slice_count": len(all_clips),
        "duration_distribution": {
            "total_seconds": sum(durations),
            "minimum_seconds": min(durations, default=0),
            "maximum_seconds": max(durations, default=0),
            "average_seconds": round(sum(durations) / len(durations), 1)
            if durations else 0.0,
            "buckets": duration_buckets,
        },
        "score_distribution": {
            "scored_count": len(scored),
            "missing_count": len(all_clips) - len(scored),
            "minimum": min(scored, default=None),
            "maximum": max(scored, default=None),
            "average": round(sum(scored) / len(scored), 1) if scored else None,
        },
        "anchor_source_counts": compact_anchor_counts,
        "danmaku_peak_count": sum(
            row["anchor_source"] == "弹幕峰值"
            or (row["peak_density"] or 0) > 0
            for row in all_clips
        ),
        "edge_candidate_count": len(edge_candidates),
        "edge_candidates": edge_candidates[:MAX_EDGE_CANDIDATE_ROWS],
        "edge_candidates_truncated_count": max(
            0, len(edge_candidates) - MAX_EDGE_CANDIDATE_ROWS
        ),
        "clips": all_clips[:MAX_CLIP_ROWS],
        "clips_truncated_count": max(0, len(all_clips) - MAX_CLIP_ROWS),
    }
    return _bounded_overview(overview)


__all__ = [
    "EDGE_CANDIDATE_MIN_SCORE",
    "EDGE_CANDIDATE_MAX_SCORE",
    "MAX_OVERVIEW_BYTES",
    "build_quality_overview",
]
