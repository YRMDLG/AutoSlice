"""准备从既有产物重做切片复核所需的字幕与弹幕分析状态。"""

from __future__ import annotations

import os


def prepare_retry_analysis_state(
        flv_path, ass_path, data, *, parse_srt_segments, analyze_danmaku,
        empty_danmaku_series, average_danmaku_density,
        high_energy_danmaku_peaks, isfile=None):
    """准备 retry 阶段后续复核所需的字幕、弹幕和峰值摘要。

    字幕路径的优先级、文件存在性检查以及默认弹幕文件的推导属于本阶段
    的编排职责；SRT 和弹幕领域实现全部通过显式依赖注入。``isfile`` 只
    是测试替换点，默认仍使用标准库的文件存在性判断。
    """
    isfile = isfile or os.path.isfile
    base, _ = os.path.splitext(os.path.abspath(flv_path))
    corrected_srt_path = data.get("corrected_srt_path")
    source_srt_path = data.get("source_srt_path") or base + ".srt"
    srt_path = (
        corrected_srt_path
        if corrected_srt_path and isfile(corrected_srt_path)
        else source_srt_path
    )
    if not srt_path or not isfile(srt_path):
        raise FileNotFoundError(f"复核字幕不存在: {srt_path or '未记录'}")

    srt_segments = parse_srt_segments(srt_path)
    if not srt_segments:
        raise ValueError("复核字幕中没有有效句段")

    if ass_path is None:
        ass_candidate = base + ".ass"
        ass_path = ass_candidate if isfile(ass_candidate) else None
    peaks = analyze_danmaku(ass_path) if ass_path else empty_danmaku_series()
    avg_den = average_danmaku_density(peaks)
    high_energy_peaks = high_energy_danmaku_peaks(peaks, avg_den)
    peak_info = (
        f"弹幕密度 {len(peaks)} 个滑动窗口, "
        f"独立高能峰值 {len(high_energy_peaks)} 个, "
        f"全场平均密度 {avg_den:.0f}条/分钟"
        if peaks else "无弹幕数据"
    )

    return {
        "srt_path": srt_path,
        "srt_segments": srt_segments,
        "peaks": peaks,
        "avg_den": avg_den,
        "high_energy_peaks": high_energy_peaks,
        "peak_info": peak_info,
        "source_srt_path": source_srt_path,
        "corrected_srt_path": corrected_srt_path,
        "ass_path": ass_path,
    }
