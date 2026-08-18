"""人工时间轴候选的 LLM 复核、分批执行与遗漏项补查。"""

import json
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from autoslice import timecode
from autoslice.analysis import (
    boundaries,
    clip_review_candidates,
    llm_execution,
    manual_enrichment,
)
from autoslice.analysis import (
    timeline as timeline_analysis,
)
from autoslice.analysis import (
    titles as title_analysis,
)
from autoslice.llm import transport as llm_gateway
from autoslice.llm.prompts import (
    ManualTopicPromptEvidence,
)
from autoslice.llm.prompts import (
    build_manual_topic_enrichment_prompt as render_manual_topic_enrichment_prompt,
)
from autoslice.streamer_profiles import (
    current_streamer_profile,
    streamer_profile_context,
)

FACADE_EXPORTS = {
    "_build_manual_topic_enrichment_prompt": (
        "build_manual_topic_enrichment_prompt"
    ),
    "_enrich_manual_topics_in_batches": "enrich_manual_topics_in_batches",
    "_enrich_manual_topics_with_llm": "enrich_manual_topics_with_llm",
    "_validate_unmatched_manual_topics": "validate_unmatched_manual_topics",
}


def build_manual_topic_enrichment_prompt(
    topics,
    streamer_name=None,
    compact=False,
):
    """把规则聚合候选压缩成一次批量 AI 复核请求。"""
    candidates = []
    for index, topic in enumerate(topics or [], 1):
        body_limit = 8 if compact else 18
        evidence = [
            title_analysis._strip_body_prefix(line)
            for line in (topic.get("body") or [])[:body_limit]
            if title_analysis._strip_body_prefix(line)
        ]
        subtitle_evidence = [
            line for line in evidence if line.startswith("字幕核查：")
        ]
        manual_evidence = [
            line
            for line in evidence
            if line.startswith(("人工时间轴", "时间轴："))
        ]
        density_evidence = [
            line for line in evidence if line.startswith("弹幕依据：")
        ]
        candidates.append(
            {
                "id": index,
                "start": timecode.format_elapsed(topic["start"]),
                "end": timecode.format_elapsed(topic["end"]),
                "current_title": topic.get("title", "未命名片段"),
                "evidence": evidence,
                "subtitle_evidence": subtitle_evidence,
                "manual_evidence": manual_evidence,
                "density_evidence": density_evidence,
                "reference_publish_title": topic.get("publish_title"),
            }
        )
    context = title_analysis._prompt_context(
        streamer_name,
        context_text=json.dumps(candidates, ensure_ascii=False),
        compact=compact,
        publish_title_example_text="具体事件钩子👀结果或原话",
    )
    return render_manual_topic_enrichment_prompt(
        ManualTopicPromptEvidence(
            context=context,
            candidates=tuple(candidates),
        )
    )


def enrich_manual_topics_with_llm(
    topics,
    streamer_name=None,
    progress_callback=None,
    retry_coordinator=None,
    progress_label="人工时间轴 AI 复核",
    progress_step=75,
):
    """用一次 LLM 请求批量复核人工候选，并允许并列事件拆成两项。"""
    if not topics:
        return 0
    prompt = build_manual_topic_enrichment_prompt(
        topics,
        streamer_name=streamer_name,
    )
    compact_prompt = build_manual_topic_enrichment_prompt(
        topics,
        streamer_name=streamer_name,
        compact=True,
    )
    response = llm_gateway.call_llm_with_retry(
        prompt,
        compact_prompt=compact_prompt,
        require_json=True,
        progress_callback=progress_callback,
        progress_label=progress_label,
        progress_step=progress_step,
        retry_coordinator=retry_coordinator,
        reasoning_stage="review",
    )
    payload = llm_gateway.extract_json_payload(response)
    raw_topics = payload.get("topics", []) if isinstance(payload, dict) else []
    if not isinstance(raw_topics, list):
        raise llm_gateway.LLMStructuredOutputError(
            "人工时间轴 AI 复核未返回 topics 数组"
        )

    grouped_items = defaultdict(list)
    for item in raw_topics:
        if not isinstance(item, dict):
            continue
        try:
            topic_index = int(item.get("id")) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= topic_index < len(topics):
            continue
        if len(grouped_items[topic_index]) < 2:
            grouped_items[topic_index].append(item)

    updated = 0
    enriched_topics = []
    for topic_index, topic in enumerate(topics):
        items = grouped_items.get(topic_index, [])
        replacements = []
        for item in items:
            enriched = manual_enrichment.enrich_manual_topic_from_item(topic, item)
            if not enriched:
                continue
            if len(items) > 1 and not enriched.get("ai_focus_validated"):
                continue
            if boundaries._is_duplicate_topic(enriched, replacements):
                continue
            replacements.append(enriched)
        if replacements:
            replacements.sort(key=lambda value: (value["start"], value["end"]))
            enriched_topics.extend(replacements)
            updated += len(replacements)
        else:
            enriched_topics.append(topic)
    if not updated:
        raise llm_gateway.LLMStructuredOutputError(
            "人工时间轴 AI 复核没有返回可用话题"
        )
    topics[:] = sorted(
        enriched_topics,
        key=lambda value: (value["start"], value["end"]),
    )
    return updated


def enrich_manual_topics_in_batches(
    topics,
    streamer_name=None,
    progress_callback=None,
    batch_size=timeline_analysis.MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE,
    batch_result_callback=None,
    progress_start=22,
    progress_end=24,
    progress_label="字幕校准人工时间轴",
):
    """分批优化复杂人工时间轴，避免一次请求塞入整场证据。"""
    optimized_topics = []
    warnings = []
    safe_batch_size = max(1, batch_size)
    total_batches = max(1, math.ceil(len(topics or []) / safe_batch_size))
    report_progress = llm_execution.serialized_progress_callback(progress_callback)
    jobs = []
    for batch_index, offset in enumerate(
        range(0, len(topics or []), safe_batch_size),
        1,
    ):
        batch = list(topics[offset : offset + safe_batch_size])
        jobs.append(
            {
                "batch_index": batch_index,
                "offset": offset,
                "batch": batch,
            }
        )

    profile_snapshot = current_streamer_profile()

    def enrich_job(job):
        with streamer_profile_context(profile_snapshot):
            return enrich_manual_topics_with_llm(
                job["batch"],
                streamer_name=streamer_name,
                progress_callback=report_progress,
                retry_coordinator=provider_retry_coordinator,
                progress_label=f"{progress_label} AI 复核",
                progress_step=progress_start,
            )

    provider_retry_coordinator = llm_gateway.LLMProviderRetryCoordinator()
    concurrency = min(
        llm_execution.configured_llm_concurrency(),
        max(1, len(jobs)),
    )
    if report_progress:
        report_progress(
            f"{progress_label}：{total_batches} 批，{concurrency} 路并行...",
            progress_start,
            100,
        )
    completed_batches = 0
    with ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="autoslice-manual",
    ) as executor:
        futures = [executor.submit(enrich_job, job) for job in jobs]
        for job, future in zip(jobs, futures):
            batch_index = job["batch_index"]
            offset = job["offset"]
            batch = job["batch"]
            try:
                future.result()
                unresolved = [
                    topic for topic in batch if not topic.get("ai_enriched")
                ]
                if unresolved:
                    for topic in unresolved:
                        topic["reference_only"] = True
                    warnings.append(
                        f"第 {batch_index}/{total_batches} 批仅复核 "
                        f"{len(batch) - len(unresolved)}/{len(batch)} 项，"
                        f"其余 {len(unresolved)} 项未返回"
                    )
            except Exception as exc:
                warning = (
                    f"第 {batch_index}/{total_batches} 批优化失败："
                    f"{llm_gateway.short_llm_error(exc)}"
                )
                warnings.append(warning)
                for topic in batch:
                    topic["reference_only"] = True
            optimized_topics.extend(batch)
            completed_batches += 1
            if report_progress:
                progress_span = max(0, progress_end - progress_start)
                current_step = progress_start + int(
                    progress_span * completed_batches / total_batches
                )
                report_progress(
                    f"{progress_label}完成 "
                    f"({completed_batches}/{total_batches})",
                    current_step,
                    100,
                )
            if batch_result_callback:
                batch_result_callback(
                    list(optimized_topics),
                    list(topics[offset + safe_batch_size :]),
                    list(warnings),
                )
    topics[:] = sorted(
        optimized_topics,
        key=lambda item: (item["start"], item["end"]),
    )
    if not warnings:
        return None
    return (
        "人工时间轴部分未完成字幕校准，相关条目仅作低权重参考："
        + "；".join(warnings)
    )


def validate_unmatched_manual_topics(
    topics,
    streamer_name=None,
    progress_callback=None,
    srt_segments=None,
    peaks=None,
):
    """后置复核首轮遗漏的时间轴候选；失败时只保留报告线索。"""
    manual_topics = [
        topic
        for topic in topics
        if topic.get("source")
        in {"manual_timeline", "optimized_manual_timeline"}
        and (not topic.get("ai_enriched") or topic.get("postcheck_pending"))
    ]
    if not manual_topics:
        return None

    if srt_segments:
        for topic in manual_topics:
            fresh_evidence = clip_review_candidates.fresh_manual_topic_evidence(
                topic,
                srt_segments=srt_segments,
                peaks=peaks,
            )
            if fresh_evidence:
                topic["body"] = fresh_evidence

    original_ids = {id(topic) for topic in manual_topics}
    warning = enrich_manual_topics_in_batches(
        manual_topics,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
        batch_size=timeline_analysis.MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE,
        progress_start=94,
        progress_end=94,
        progress_label="人工时间轴补充项复核",
    )

    topics[:] = [topic for topic in topics if id(topic) not in original_ids]
    topics.extend(manual_topics)
    topics.sort(key=lambda item: (item["start"], item["end"]))
    if warning:
        return (
            "人工时间轴补充项部分复核失败；"
            "未核验条目仅写入报告且不会自动切片："
            f"{warning}"
        )
    return None
