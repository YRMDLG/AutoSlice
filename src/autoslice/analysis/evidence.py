"""话题候选的字幕窗口与弹幕峰值证据唯一实现。"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from autoslice.analysis import danmaku as danmaku_analysis

FACADE_EXPORTS = {
    "_topic_danmaku_reference_lines": "topic_danmaku_reference_lines",
    "_topic_peak_candidates": "topic_peak_candidates",
    "_topic_srt_summary_lines": "topic_srt_summary_lines",
}


def _format_time(seconds: float) -> str:
    return str(timedelta(seconds=int(seconds)))


def topic_srt_summary_lines(
    start: float,
    end: float,
    srt_segments: Any,
    limit: int = 12,
    bucket_sec: float = 30,
) -> list[str]:
    """把碎片字幕聚成带时间范围的短窗口，供 AI 核对事件与边界。"""

    if not srt_segments:
        return []
    related = [
        (seg_start, seg_end, text)
        for seg_start, seg_end, text in srt_segments
        if seg_end >= start and seg_start <= end
    ]
    if not related:
        return []

    buckets = {}
    for seg_start, seg_end, text in related:
        key = max(0, int((max(start, seg_start) - start) // bucket_sec))
        bucket = buckets.setdefault(key, {
            "start": max(start, seg_start),
            "end": min(end, seg_end),
            "texts": [],
        })
        bucket["start"] = min(bucket["start"], max(start, seg_start))
        bucket["end"] = max(bucket["end"], min(end, seg_end))
        compact = re.sub(r"\s+", "", text or "")
        if compact and (not bucket["texts"] or bucket["texts"][-1] != compact):
            bucket["texts"].append(compact)

    windows = [buckets[key] for key in sorted(buckets)]
    if len(windows) <= limit:
        selected = windows
    elif limit <= 1:
        selected = [windows[len(windows) // 2]]
    else:
        indexes = sorted({
            round(index * (len(windows) - 1) / (limit - 1))
            for index in range(limit)
        })
        selected = [windows[index] for index in indexes]

    lines = []
    seen = set()
    for window in selected:
        compact = "".join(window["texts"])
        if not compact or compact in seen:
            continue
        seen.add(compact)
        if len(compact) > 180:
            compact = compact[:180] + "…"
        lines.append(
            f"·字幕核查：{_format_time(window['start'])}-"
            f"{_format_time(window['end'])} {compact}"
        )
    return lines


def topic_danmaku_reference_lines(
    start: float,
    end: float,
    peaks: Any,
    limit: int = 3,
) -> list[str]:
    """保留相隔较远的多个峰值，让 AI 能识别人工记录中的并列事件。"""

    candidates = [
        (peak_start, density)
        for peak_start, density in peaks or []
        if peak_start + danmaku_analysis.DANMAKU_WINDOW >= start
        and peak_start <= end
    ]
    selected = []
    for peak_start, density in sorted(
        candidates,
        key=lambda item: item[1],
        reverse=True,
    ):
        if any(
            abs(peak_start - old_start) < danmaku_analysis.DANMAKU_WINDOW
            for old_start, _ in selected
        ):
            continue
        selected.append((peak_start, density))
        if len(selected) >= limit:
            break
    return [
        f"·弹幕依据：{_format_time(peak_start)} 附近峰值约 {int(density)} 条/分钟"
        for peak_start, density in sorted(selected)
    ]


def topic_peak_candidates(
    topic: dict[str, Any],
    peaks: Any,
    window_sec: float = danmaku_analysis.DANMAKU_WINDOW,
) -> list[tuple[float, float]]:
    """匹配话题内峰值，并允许一个弹幕采样步长的边界误差。"""

    if not peaks:
        return []
    start = int(topic["start"])
    end = int(topic["end"])
    return [
        (peak_start, density)
        for peak_start, density in peaks
        if (
            start - danmaku_analysis.DANMAKU_WINDOW_STEP
            <= peak_start + window_sec / 2
            <= end + danmaku_analysis.DANMAKU_WINDOW_STEP
        )
    ]
