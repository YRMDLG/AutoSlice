"""把修复后的 SRT 与弹幕峰值整理成首轮 LLM 分析分块。"""

from __future__ import annotations

from autoslice import timecode
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis.topic import analysis as topic_analysis
from autoslice.transcription import segments as transcription_segments
from autoslice.transcription import srt_io as transcription_srt_io

FACADE_EXPORTS = {
    "_make_chunk": "make_chunk",
    "chunk_srt": "chunk_srt",
    "parse_srt_text": "parse_srt_text",
}


def parse_srt_text(srt_path):
    """读取修复后的 SRT，过滤不足两个可见字符的字幕。"""
    return [
        (start_s, end_s, text)
        for start_s, end_s, text in (
            transcription_srt_io.load_repaired_srt_segments(srt_path)
        )
        if transcription_segments.subtitle_text_size(text) >= 2
    ]


def chunk_srt(
    segs,
    peaks,
    chunk_sec=topic_analysis.CHUNK_SEC,
    context_sec=90,
):
    """按半开核心区间分块字幕，并附加前后只读上下文。"""
    if not segs:
        return []
    if chunk_sec <= 0:
        raise ValueError("chunk_sec 必须大于 0")
    if context_sec < 0:
        raise ValueError("context_sec 不能小于 0")
    avg_density = danmaku_analysis._average_danmaku_density(peaks)
    independent_peaks = danmaku_analysis._high_energy_danmaku_peaks(
        peaks,
        avg_density,
    )

    rows = []
    for order, item in enumerate(segs):
        if len(item) == 3:
            start_s, end_s, text = item
        else:
            start_s, text = item
            end_s = start_s
        time_label = (
            timecode.format_elapsed(start_s)
            if end_s <= start_s + 1
            else (
                f"{timecode.format_elapsed(start_s)}－"
                f"{timecode.format_elapsed(end_s)}"
            )
        )
        rows.append((start_s, end_s, order, f"[{time_label}] {text}"))
    rows.sort(key=lambda row: (row[0], row[1], row[2]))

    first_start = rows[0][0]
    core_anchor = (first_start // chunk_sec) * chunk_sec
    owned_rows = {}
    for row in rows:
        bucket_index = int((row[0] - core_anchor) // chunk_sec)
        core_start = core_anchor + bucket_index * chunk_sec
        owned_rows.setdefault(core_start, []).append(row)

    timeline_end = max(
        end_s if end_s > start_s else start_s + 1
        for start_s, end_s, _, _ in rows
    )
    chunks = []
    for chunk_start in sorted(owned_rows):
        chunk_end = chunk_start + chunk_sec
        before_start = max(0, chunk_start - context_sec)
        after_start = min(chunk_end, timeline_end)
        after_end = min(chunk_end + context_sec, timeline_end)
        before_texts = [
            row[3]
            for row in rows
            if (
                row[0] < chunk_start
                and (row[1] if row[1] > row[0] else row[0] + 1)
                > before_start
            )
        ]
        after_texts = [
            row[3]
            for row in rows
            if (
                row[0] >= chunk_end
                and row[0] < after_end
                and (row[1] if row[1] > row[0] else row[0] + 1)
                > chunk_end
            )
        ]
        chunks.append(
            make_chunk(
                chunk_start,
                [row[3] for row in owned_rows[chunk_start]],
                peaks,
                avg_density,
                independent_peaks=independent_peaks,
                chunk_end=chunk_end,
                context={
                    "before": {
                        "start": before_start,
                        "end": chunk_start,
                        "text": "\n".join(before_texts),
                    },
                    "after": {
                        "start": after_start,
                        "end": max(after_start, after_end),
                        "text": "\n".join(after_texts),
                    },
                },
            )
        )

    return chunks


def make_chunk(
    chunk_start,
    texts,
    peaks,
    avg_density=0,
    independent_peaks=None,
    *,
    chunk_end=None,
    context=None,
):
    """构造一个固定时长的字幕/弹幕分析分块。"""
    text_block = "\n".join(texts)
    chunk_end = (
        chunk_start + topic_analysis.CHUNK_SEC
        if chunk_end is None
        else chunk_end
    )
    context = context or {
        "before": {
            "start": max(0, chunk_start - 90),
            "end": chunk_start,
            "text": "",
        },
        "after": {
            "start": chunk_end,
            "end": chunk_end + 90,
            "text": "",
        },
    }
    nearby_peaks = [
        (start, density)
        for start, density in peaks
        if chunk_start - 60 <= start <= chunk_end + 60
    ]
    if nearby_peaks:
        max_density = max(density for _, density in nearby_peaks)
        ratio = max_density / avg_density if avg_density > 0 else 1.0
        danmaku_info = (
            f"[弹幕: 本段峰值{max_density}条/分钟 = {ratio:.1f}倍平均 | "
            f"全场平均={avg_density:.0f}]"
        )
    else:
        danmaku_info = (
            f"[弹幕: 本段无峰值, 远低于全场平均{avg_density:.0f}]"
        )
    independent_peaks = (
        danmaku_analysis._high_energy_danmaku_peaks(peaks, avg_density)
        if independent_peaks is None
        else independent_peaks
    )
    evidence_rows = []
    for peak_start, density in independent_peaks:
        if not (
            chunk_start - danmaku_analysis.DANMAKU_WINDOW
            <= peak_start
            <= chunk_end + danmaku_analysis.DANMAKU_WINDOW
        ):
            continue
        features = danmaku_analysis._danmaku_peak_features(
            peaks,
            peak_start,
            density,
            avg_density=avg_density,
        )
        evidence_rows.append(
            (
                float(features["selection_score"]),
                int(peak_start),
                danmaku_analysis._danmaku_prompt_evidence(features),
            )
        )
    evidence_rows.sort(key=lambda row: (-row[0], row[1]))
    return {
        "start": chunk_start,
        "end": chunk_end,
        "text": text_block,
        "core": {
            "start": chunk_start,
            "end": chunk_end,
            "text": text_block,
        },
        "context": context,
        "danmaku_info": danmaku_info,
        "danmaku_evidence": [row[2] for row in evidence_rows[:4]],
        "has_peaks": bool(nearby_peaks),
    }
