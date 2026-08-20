"""流水线 Step 2/3 的纯数据准备 owner。

本模块只负责把弹幕、修复后的 SRT 和视频时长准备成首轮分析需要的
中间结果。具体的弹幕解析、SRT 解析/分块和视频探测仍由各自模块实现，
调用方通过显式依赖注入保留可替换 seam。
"""

from __future__ import annotations


def prepare_pipeline_analysis(
    flv_path,
    ass_path,
    srt_path,
    progress_callback=None,
    *,
    analyze_danmaku,
    empty_danmaku_series,
    average_danmaku_density,
    high_energy_danmaku_peaks,
    parse_srt_text,
    chunk_srt,
    probe_video_duration,
):
    """准备完整流水线首轮分析所需的弹幕、分块和时长数据。

    ``analyze_danmaku``、``parse_srt_text``、``chunk_srt`` 和
    ``probe_video_duration`` 都是已有模块的实现；本 owner 不复制这些
    解析或探测逻辑。其余弹幕统计依赖也显式传入，以便旧 pipeline seam
    和单元测试继续替换它们。
    """
    if progress_callback:
        progress_callback("Step 2/5: 弹幕密度分析...", 15, 100)
    peaks = (
        analyze_danmaku(ass_path)
        if ass_path
        else empty_danmaku_series()
    )
    avg_den = average_danmaku_density(peaks)
    if peaks:
        high_energy_peaks = high_energy_danmaku_peaks(peaks, avg_den)
        peak_info = (
            f"弹幕密度 {len(peaks)} 个滑动窗口, "
            f"独立高能峰值 {len(high_energy_peaks)} 个, "
            f"全场平均密度 {avg_den:.0f}条/分钟"
        )
    else:
        peak_info = "无弹幕数据"

    if progress_callback:
        progress_callback("Step 3/5: SRT 分块中...", 20, 100)
    segs = parse_srt_text(srt_path)
    chunks = chunk_srt(segs, peaks)
    srt_duration = max((end for _, end, _ in segs), default=None)
    probed_video_duration = probe_video_duration(flv_path)
    video_duration = probed_video_duration or srt_duration

    return {
        "peaks": peaks,
        "avg_den": avg_den,
        "peak_info": peak_info,
        "segs": segs,
        "chunks": chunks,
        "srt_duration": srt_duration,
        "probed_video_duration": probed_video_duration,
        "video_duration": video_duration,
    }
