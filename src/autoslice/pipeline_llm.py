"""完整流水线 Step 4 的 LLM 分块分析 owner。"""

from __future__ import annotations


def analyze_pipeline_llm_chunks(
    chunks,
    streamer_display_name,
    topic_analysis_checkpoint_path,
    legacy_topic_analysis_checkpoint_path,
    progress_callback=None,
    *,
    seed_artifact_from_legacy,
    analyze_topic_chunks,
):
    """迁移旧检查点并执行首轮 LLM 分块分析。

    检查点迁移和模型调用都通过显式依赖传入，避免 owner 反向依赖
    ``pipeline``，同时保留旧流水线对检查点路径、进度回调和模型调用的
    原始参数及异常/重试语义。
    """
    seed_artifact_from_legacy(
        topic_analysis_checkpoint_path,
        legacy_topic_analysis_checkpoint_path,
    )
    return analyze_topic_chunks(
        chunks,
        streamer_display_name,
        progress_callback=progress_callback,
        checkpoint_path=topic_analysis_checkpoint_path,
    )
