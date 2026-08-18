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


def chunk_srt(segs, peaks, chunk_sec=topic_analysis.CHUNK_SEC):
    """按视频时间分块字幕，并附加弹幕密度和内容证据。"""
    if not segs:
        return []
    avg_density = danmaku_analysis._average_danmaku_density(peaks)
    independent_peaks = danmaku_analysis._high_energy_danmaku_peaks(
        peaks,
        avg_density,
    )

    chunks = []
    chunk_start = segs[0][0]
    current_texts = []

    for item in segs:
        if len(item) == 3:
            start_s, end_s, text = item
        else:
            start_s, text = item
            end_s = start_s
        if start_s - chunk_start > chunk_sec:
            if current_texts:
                chunks.append(
                    make_chunk(
                        chunk_start,
                        current_texts,
                        peaks,
                        avg_density,
                        independent_peaks=independent_peaks,
                    )
                )
            chunk_start = start_s
            current_texts = []
        time_label = (
            timecode.format_elapsed(start_s)
            if end_s <= start_s + 1
            else (
                f"{timecode.format_elapsed(start_s)}－"
                f"{timecode.format_elapsed(end_s)}"
            )
        )
        current_texts.append(f"[{time_label}] {text}")

    if current_texts:
        chunks.append(
            make_chunk(
                chunk_start,
                current_texts,
                peaks,
                avg_density,
                independent_peaks=independent_peaks,
            )
        )

    return chunks


def make_chunk(
    chunk_start,
    texts,
    peaks,
    avg_density=0,
    independent_peaks=None,
):
    """构造一个固定时长的字幕/弹幕分析分块。"""
    text_block = "\n".join(texts)
    chunk_end = chunk_start + topic_analysis.CHUNK_SEC
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
        "danmaku_info": danmaku_info,
        "danmaku_evidence": [row[2] for row in evidence_rows[:4]],
        "has_peaks": bool(nearby_peaks),
    }
