"""流水线的字幕准备阶段：迁移旧检查点并准备可供分析的 SRT。"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping


def prepare_pipeline_subtitles(
        flv_path,
        artifact_layout: Mapping[str, str],
        legacy_checkpoint_path,
        *,
        progress_callback: Callable | None,
        ensure_progress_callback: Callable | None,
        seed_artifact_from_legacy: Callable,
        ensure_srt: Callable,
        export_corrected_srt: Callable):
    """完成流水线 Step 1，并返回源字幕、校对字幕和实际分析字幕路径。

    依赖全部由调用方显式传入，使本阶段可以在不触碰真实媒体或转录服务的
    单元测试中执行；该模块不承载字幕解析、人工时间轴或报告逻辑。
    """
    seed_artifact_from_legacy(
        artifact_layout["asr_checkpoint_path"],
        legacy_checkpoint_path,
    )

    if progress_callback:
        progress_callback("Step 1/5: 检查/生成字幕...", 0, 100)
    source_srt_path = ensure_srt(
        flv_path,
        ensure_progress_callback,
        checkpoint_path=artifact_layout["asr_checkpoint_path"],
    )
    if not source_srt_path:
        raise RuntimeError("无法生成 SRT 字幕")
    corrected_srt_path = export_corrected_srt(
        source_srt_path,
        output_path=artifact_layout["corrected_srt_path"],
    )
    srt_path = corrected_srt_path or source_srt_path
    if corrected_srt_path and progress_callback:
        progress_callback(
            f"已生成剪映校对字幕: {os.path.basename(corrected_srt_path)}",
            14,
            100,
        )

    return {
        "source_srt_path": source_srt_path,
        "corrected_srt_path": corrected_srt_path,
        "srt_path": srt_path,
    }
