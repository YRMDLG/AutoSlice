"""完整流水线投稿标题复核阶段 owner。"""

from __future__ import annotations


def review_pipeline_publish_titles(
    accepted_topics,
    streamer_display_name,
    clip_review_checkpoint_path,
    api_precheck_warning=None,
    progress_callback=None,
    *,
    review_selected_publish_titles,
    write_clip_review_checkpoint,
    clean_topics_for_report,
):
    """复核投稿标题、写入检查点并准备后续边界阶段输入。

    标题算法、检查点存储和报告清洗均通过显式参数注入，使旧
    ``pipeline`` patch seam 继续生效，同时避免本 owner 反向依赖
    ``pipeline`` 或标题领域实现。
    """

    def write_title_checkpoint(current, batch_index, total_batches):
        write_clip_review_checkpoint(
            clip_review_checkpoint_path,
            current,
            stage="title_reviewing",
            batch_index=batch_index,
            total_batches=total_batches,
        )

    title_review_warning = review_selected_publish_titles(
        accepted_topics,
        streamer_name=streamer_display_name,
        progress_callback=progress_callback,
        checkpoint_callback=write_title_checkpoint,
    )
    if title_review_warning:
        api_precheck_warning = "；".join(
            item for item in (api_precheck_warning, title_review_warning) if item
        )

    accepted_topics = clean_topics_for_report(accepted_topics)
    return {
        "accepted_topics": accepted_topics,
        "api_precheck_warning": api_precheck_warning,
        "title_review_warning": title_review_warning,
    }
