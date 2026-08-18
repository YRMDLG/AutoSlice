"""人工时间轴提示、候选优化与低权重重试的唯一领域实现。"""

from __future__ import annotations

from autoslice import timecode
from autoslice.analysis import evidence as candidate_evidence
from autoslice.analysis import manual_candidates
from autoslice.analysis import manual_enrichment
from autoslice.analysis import manual_review
from autoslice.analysis import timeline as timeline_analysis
from autoslice.analysis import topic_analysis
from autoslice.analysis import titles as title_analysis
from autoslice.llm import transport as llm_gateway

FACADE_EXPORTS = {
    "_attach_manual_timeline_to_chunks": "attach_manual_timeline_to_chunks",
    "_batch_warning_text": "batch_warning_text",
    "_format_manual_entry_for_prompt": "format_manual_entry_for_prompt",
    "_manual_timeline_info_for_chunk": "manual_timeline_info_for_chunk",
    "_optimize_manual_timeline": "optimize_manual_timeline",
    "_optimized_entry_needs_retry": "optimized_entry_needs_retry",
    "_optimized_manual_entries_from_topics": "optimized_manual_entries_from_topics",
    "_retry_optimized_timeline_entries": "retry_optimized_timeline_entries",
    "_topic_from_optimized_entry": "topic_from_optimized_entry",
    "_try_enrich_manual_topics": "try_enrich_manual_topics",
}


def format_manual_entry_for_prompt(entry):
    stars = "⭐" * min(int(entry.get("stars", 0)), 5)
    prefix = f"{stars} " if stars else ""
    clock = entry.get("clock")
    elapsed_label = timecode.format_elapsed(entry["start"])
    if entry.get("end") is not None and int(entry["end"]) > int(entry["start"]):
        elapsed_label = (
            f"{elapsed_label}-{timecode.format_elapsed(entry['end'])}"
        )
    time_label = f"{elapsed_label} / {clock}" if clock else elapsed_label
    summary = "；".join(
        title_analysis._strip_body_prefix(item)
        for item in (entry.get("summary") or [])[:2]
        if title_analysis._strip_body_prefix(item)
    )
    summary_suffix = f" | {summary}" if summary else ""
    return f"- [{time_label}] {prefix}{entry.get('text', '')}{summary_suffix}"


def manual_timeline_info_for_chunk(entries, chunk_start, chunk_end, limit=12):
    """取当前分块附近的人工时间轴，供 LLM 参考。"""
    margin = timeline_analysis.MANUAL_TIMELINE_CHUNK_MARGIN_SEC
    nearby = [
        item
        for item in entries or []
        if chunk_start - margin <= item["start"] <= chunk_end + margin
    ]
    if not nearby:
        return "无"
    starred = [item for item in nearby if item.get("stars", 0) > 0]
    selected = starred[:limit]
    if len(selected) < limit:
        selected.extend(
            item for item in nearby if item not in selected
        )
        selected = selected[:limit]
    selected.sort(key=lambda item: item["start"])
    return "\n".join(format_manual_entry_for_prompt(item) for item in selected)


def attach_manual_timeline_to_chunks(chunks, entries):
    """把人工时间轴摘要挂到每个 SRT 分块上。"""
    for chunk in chunks:
        chunk["manual_timeline_info"] = manual_timeline_info_for_chunk(
            entries,
            int(chunk["start"]),
            int(chunk.get("end", chunk["start"] + topic_analysis.CHUNK_SEC)),
        )
    return chunks


def try_enrich_manual_topics(topics, streamer_name=None, progress_callback=None):
    """AI 复核失败时保留规则候选，返回适合写入报告的警告。"""
    try:
        manual_review.enrich_manual_topics_with_llm(
            topics,
            streamer_name=streamer_name,
            progress_callback=progress_callback,
        )
        return None
    except Exception as exc:
        error = llm_gateway.short_llm_error(exc)
        return f"人工时间轴 AI 复核失败，已保留字幕/弹幕规则结果：{error}"


def optimized_manual_entries_from_topics(topics):
    """把字幕复核话题转换成供后续分块分析使用的简洁时间轴。"""
    entries = []
    for topic in topics or []:
        original_entries = [
            {
                "start": int(item.get("start", 0)),
                "original_start": int(
                    item.get("original_start", item.get("start", 0))
                ),
                "clock": item.get("clock"),
                "text": item.get("text", ""),
                "stars": int(item.get("stars", 0)),
                "alignment_score": item.get("alignment_score"),
                "alignment_shift_sec": int(item.get("alignment_shift_sec", 0)),
            }
            for item in topic.get("manual_timeline") or []
        ]
        stars = max(
            [int(topic.get("manual_stars", 0))]
            + [item["stars"] for item in original_entries]
        )
        summary = []
        for line in topic.get("body") or []:
            clean = title_analysis._strip_body_prefix(line)
            if not clean:
                continue
            if str(line).startswith(
                ("·弹幕依据：", "·字幕核查：", "·时间轴：", "●人工时间轴")
            ):
                continue
            if clean not in summary:
                summary.append(clean)
            if len(summary) >= 4:
                break
        entry = {
            "start": int(topic["start"]),
            "end": int(topic["end"]),
            "clock": original_entries[0].get("clock") if original_entries else None,
            "text": topic.get("title", "人工时间轴重点"),
            "summary": summary,
            "stars": stars,
            "highlight": stars > 0,
            "source": "optimized_manual_timeline",
            "ai_enriched": bool(topic.get("ai_enriched")),
            "ai_focus_validated": bool(topic.get("ai_focus_validated")),
            "reference_only": bool(topic.get("reference_only")),
            "publish_title": topic.get("publish_title"),
            "evidence": [
                line
                for line in topic.get("body") or []
                if str(line).startswith(
                    ("·字幕核查：", "·弹幕依据：", "●人工时间轴", "·时间轴：")
                )
            ],
            "original_entries": original_entries,
        }
        sanitized = manual_candidates.sanitize_optimized_manual_entry(entry)
        if sanitized:
            entries.append(sanitized)
    return entries


def optimized_entry_needs_retry(entry):
    """识别未复核、降级或被模型模板占位污染的优化候选。"""
    if not entry.get("ai_enriched") or entry.get("reference_only"):
        return True
    if manual_enrichment.is_manual_ai_placeholder(entry.get("text")):
        return True
    return any(
        manual_enrichment.is_manual_ai_placeholder(
            title_analysis._strip_body_prefix(point)
        )
        for point in entry.get("summary") or []
    )


def topic_from_optimized_entry(entry, srt_segments, peaks):
    """把优化 JSON 中的低权重候选还原为可重试的 AI 复核话题。"""
    start = int(entry.get("start", 0))
    end = max(start + 1, int(entry.get("end", start + 1)))
    original_entries = list(entry.get("original_entries") or [])
    if not original_entries:
        original_entries = [
            {
                "start": start,
                "original_start": start,
                "text": entry.get("text", "人工时间轴重点"),
                "stars": int(entry.get("stars", 0)),
            }
        ]
    body = list(entry.get("evidence") or [])
    if not any(str(line).startswith("·弹幕依据：") for line in body):
        body[:0] = candidate_evidence.topic_danmaku_reference_lines(
            start, end, peaks or []
        )
    if not any(str(line).startswith("·字幕核查：") for line in body):
        body.extend(
            candidate_evidence.topic_srt_summary_lines(
                start, end, srt_segments or []
            )
        )
    for item in original_entries:
        stars = int(item.get("stars", 0))
        prefix = f"●人工时间轴{'⭐' * min(stars, 5)}" if stars else "·时间轴"
        line = (
            f"{prefix}：{timecode.format_elapsed(int(item.get('start', start)))} "
            f"{item.get('text', '')}"
        )
        if line not in body:
            body.append(line)
    return {
        "start": start,
        "end": end,
        "start_str": timecode.format_elapsed(start),
        "end_str": timecode.format_elapsed(end),
        "title": entry.get("text", "人工时间轴重点"),
        "publish_title": entry.get("publish_title"),
        "body": body,
        "can_slice": False,
        "manual_stars": int(entry.get("stars", 0)),
        "manual_timeline": original_entries,
        "source": "optimized_manual_timeline",
        "reference_only": True,
    }


def batch_warning_text(warnings, pending_count=0):
    details = list(warnings or [])
    if pending_count:
        details.append(f"尚有 {pending_count} 项等待后续批次")
    if not details:
        return None
    return "人工时间轴部分未完成字幕校准，相关条目仅作低权重参考：" + "；".join(details)


def retry_optimized_timeline_entries(
    entries,
    srt_segments,
    peaks,
    streamer_name=None,
    progress_callback=None,
    checkpoint_callback=None,
):
    """保留已通过候选，仅以小批量重试低权重或占位污染项。"""
    accepted_entries = [
        dict(entry)
        for entry in entries or []
        if not optimized_entry_needs_retry(entry)
    ]
    retry_topics = [
        topic_from_optimized_entry(entry, srt_segments, peaks)
        for entry in entries or []
        if optimized_entry_needs_retry(entry)
    ]
    if not retry_topics:
        return sorted(
            accepted_entries, key=lambda item: (item["start"], item["end"])
        ), None

    def save_checkpoint(processed_topics, remaining_topics, warnings):
        if not checkpoint_callback:
            return
        pending_topics = []
        for topic in remaining_topics:
            pending = dict(topic)
            pending["reference_only"] = True
            pending_topics.append(pending)
        checkpoint_entries = accepted_entries + optimized_manual_entries_from_topics(
            list(processed_topics) + pending_topics
        )
        checkpoint_callback(
            sorted(checkpoint_entries, key=lambda item: (item["start"], item["end"])),
            batch_warning_text(warnings, pending_count=len(remaining_topics)),
        )

    warning = manual_review.enrich_manual_topics_in_batches(
        retry_topics,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
        batch_size=timeline_analysis.MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE,
        batch_result_callback=save_checkpoint,
    )
    optimized_entries = accepted_entries + optimized_manual_entries_from_topics(
        retry_topics
    )
    return sorted(
        optimized_entries, key=lambda item: (item["start"], item["end"])
    ), warning


def optimize_manual_timeline(
    entries,
    srt_segments,
    peaks,
    streamer_name=None,
    progress_callback=None,
    batch_result_callback=None,
):
    """先用字幕/弹幕聚合人工记录，再由 AI 改写标题、要点和语义范围。"""
    if not entries:
        return [], None
    aligned_entries = timeline_analysis._align_manual_timeline_entries_to_srt(
        entries, srt_segments
    )
    topics = manual_candidates.topics_from_manual_timeline(
        aligned_entries,
        srt_segments=srt_segments,
        peaks=peaks,
        max_gap_sec=timeline_analysis.MANUAL_TIMELINE_OPTIMIZE_GAP_SEC,
        max_group_duration_sec=(
            timeline_analysis.MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC
        ),
    )
    warning = manual_review.enrich_manual_topics_in_batches(
        topics,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
        batch_result_callback=batch_result_callback,
    )
    return optimized_manual_entries_from_topics(topics), warning
