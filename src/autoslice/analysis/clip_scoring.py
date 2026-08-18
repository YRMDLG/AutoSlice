"""切片候选投稿价值评分与复核审计的唯一实现。"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any

from autoslice.analysis import checkpoints as checkpoint_store

FACADE_EXPORTS = {
    "_build_clip_candidate_review_audit": "build_clip_candidate_review_audit",
    "_clip_interest_reason": "clip_interest_reason",
    "_clip_manual_star_count": "clip_manual_star_count",
    "_clip_star_bonus_cap": "clip_star_bonus_cap",
    "_parse_clip_interest_score": "parse_clip_interest_score",
    "_parse_clip_star_bonus": "parse_clip_star_bonus",
}


def parse_clip_interest_score(value: Any) -> float | None:
    """解析 Terra 投稿价值分；缺失、越界或非有限值均视为无效。"""

    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or not 0 <= score <= 100:
        return None
    return round(score, 1)


def parse_clip_star_bonus(value: Any) -> float | None:
    """解析强人工星标的有限加分；模型绝不能返回超过八分。"""

    bonus = parse_clip_interest_score(value)
    if bonus is None or bonus > 8:
        return None
    return bonus


def clip_star_bonus_cap(manual_star_count: Any) -> float:
    """按单条人工记录的星标强度限制加分，普通标记不左右筛选。"""

    try:
        star_count = max(0, int(manual_star_count or 0))
    except (TypeError, ValueError):
        star_count = 0
    if star_count < 3:
        return 0.0
    if star_count == 3:
        return 2.0
    if star_count == 4:
        return 5.0
    return 8.0


def clip_manual_star_count(topic: Any) -> int:
    """读取人工星标数量；异常旧数据按零处理，不能阻断审计写入。"""

    try:
        return max(0, int((topic or {}).get("manual_stars", 0) or 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def clip_interest_reason(item: Any) -> str:
    """清理投稿价值说明，供检查点审计和拒绝原因使用。"""

    source = item if isinstance(item, dict) else {}
    reason = re.sub(
        r"\s+",
        " ",
        str(source.get("interest_reason", source.get("reason", ""))),
    ).strip()
    return reason[:240]


def build_clip_candidate_review_audit(topics: Any) -> dict[str, Any]:
    """生成面向人工排查的候选明细，不污染概览或话题报告。"""

    rows = []
    for topic in topics or []:
        sources = [
            str(value).strip()
            for value in topic.get("clip_candidate_sources") or []
            if str(value).strip()
        ]
        reviewed = topic.get("clip_review_validated")
        has_review_state = (
            topic.get("clip_review_attempts") is not None
            or topic.get("clip_review_rejection") is not None
            or reviewed is not None
        )
        if not sources and not has_review_state:
            continue
        if topic.get("can_slice"):
            status = "已通过并生成切片"
        elif reviewed is True:
            status = "已通过复核但未生成切片"
        elif reviewed is False:
            status = "未通过复核"
        else:
            status = "复核未完成"
        start = int(topic.get("start", 0) or 0)
        end = int(topic.get("end", 0) or 0)
        title = str(topic.get("title", "未命名候选")).strip() or "未命名候选"
        rows.append({
            "start": start,
            "end": end,
            "time_range": f"{_format_time(start)}－{_format_time(end)}",
            "title": title,
            "candidate_sources": sources,
            "manual_stars": clip_manual_star_count(topic),
            "clip_review_validated": reviewed,
            "interest_score": parse_clip_interest_score(
                topic.get("clip_interest_score")
            ),
            "interest_reason": topic.get("clip_interest_reason"),
            "rejection_reason": topic.get("clip_review_rejection"),
            "final_slice": bool(topic.get("can_slice")),
            "final_slice_anchor_source": topic.get("slice_anchor_source"),
            "status": status,
        })
    return {
        "review_policy_version": checkpoint_store.CLIP_REVIEW_POLICY_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(rows),
        "approved_count": sum(row["final_slice"] for row in rows),
        "candidates": sorted(
            rows,
            key=lambda row: (row["start"], row["end"], row["title"]),
        ),
    }


def _format_time(seconds: int) -> str:
    return str(timedelta(seconds=int(seconds)))
