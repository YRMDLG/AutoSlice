"""首轮字幕/弹幕话题分析的提示词、响应解析与分块编排。"""

from __future__ import annotations

import os
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from functools import partial

from autoslice import timecode
from autoslice.analysis import checkpoints as checkpoint_store
from autoslice.analysis import llm_execution
from autoslice.analysis.report import formatting as topic_formatting
from autoslice.analysis.review import deduplication as clip_deduplication
from autoslice.analysis.review import policy as clip_policy
from autoslice.analysis.topic import normalization
from autoslice.analysis.topic import response as topic_response
from autoslice.analysis.topic import titles as title_analysis
from autoslice.llm import transport as llm_gateway
from autoslice.llm.prompts import TopicAnalysisPromptEvidence
from autoslice.llm.prompts import (
    build_topic_analysis_prompt as render_topic_analysis_prompt,
)
from autoslice.transcription import segments as transcription_segments

FACADE_EXPORTS = {
    "CHUNK_SEC": "CHUNK_SEC",
    "LLM_ANALYSIS_MODEL": "LLM_ANALYSIS_MODEL",
    "LLM_COMPACT_MAX_TOKENS": "LLM_COMPACT_MAX_TOKENS",
    "LLM_COMPACT_TEXT_CHARS": "LLM_COMPACT_TEXT_CHARS",
    "LLM_FULL_TEXT_CHARS": "LLM_FULL_TEXT_CHARS",
    "LLM_MAX_TOKENS": "LLM_MAX_TOKENS",
    "MAX_INITIAL_FAILED_CHUNKS": "MAX_INITIAL_FAILED_CHUNKS",
    "_HEADING_RE": "HEADING_RE",
    "_analyze_topic_chunks": "analyze_topic_chunks",
    "_build_chunk_prompt": "build_chunk_prompt",
    "_is_topic_in_chunk": "is_topic_in_chunk",
    "_load_topic_analysis_checkpoint": "load_topic_analysis_checkpoint",
    "_make_fallback_topic_from_chunk": "make_fallback_topic_from_chunk",
    "_parse_json_topics_response": "parse_json_topics_response",
    "_parse_llm_response": "parse_llm_response",
    "_repair_short_topic_end": "repair_short_topic_end",
    "_strip_code_fence": "strip_code_fence",
    "_strip_prompt_time_labels": "strip_prompt_time_labels",
    "_topic_analysis_prompt_fingerprint": "topic_analysis_prompt_fingerprint",
    "_write_topic_analysis_checkpoint": "write_topic_analysis_checkpoint",
}


CHUNK_SEC = 600
LLM_ANALYSIS_MODEL = (
    os.environ.get("AUTOSLICE_ANALYSIS_MODEL", "").strip()
    or "gpt-5.6-luna"
)
LLM_MAX_TOKENS = 16000
LLM_COMPACT_MAX_TOKENS = 12000
LLM_FULL_TEXT_CHARS = 8000
LLM_COMPACT_TEXT_CHARS = 2200
MAX_INITIAL_FAILED_CHUNKS = 3
TOPIC_ANALYSIS_CHECKPOINT_VERSION = (
    checkpoint_store.TOPIC_ANALYSIS_CHECKPOINT_VERSION
)

HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|\d+[.)、])?\s*\["
    r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—－~～至]+\s*"
    r"(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+?)\s*$"
)

topic_analysis_prompt_fingerprint = partial(
    checkpoint_store.topic_analysis_prompt_fingerprint,
    schema_version=TOPIC_ANALYSIS_CHECKPOINT_VERSION,
    model=LLM_ANALYSIS_MODEL,
    max_tokens=LLM_MAX_TOKENS,
    compact_max_tokens=LLM_COMPACT_MAX_TOKENS,
)
load_topic_analysis_checkpoint = partial(
    checkpoint_store.load_topic_analysis_checkpoint,
    schema_version=TOPIC_ANALYSIS_CHECKPOINT_VERSION,
)
write_topic_analysis_checkpoint = partial(
    checkpoint_store.write_topic_analysis_checkpoint,
    schema_version=TOPIC_ANALYSIS_CHECKPOINT_VERSION,
    model=LLM_ANALYSIS_MODEL,
)


def repair_short_topic_end(start_s, end_s, body_lines, chunk_end):
    """模型给出极短时间但正文很多时，修正报告话题结束时间。"""
    duration = end_s - start_s
    body_len = sum(
        transcription_segments.text_len_for_timing(line)
        for line in body_lines
    )
    if duration >= 10 or body_len < 40:
        return end_s
    estimated = min(
        clip_policy.TOPIC_MAX_REPAIRED_REPORT_SEC,
        max(
            clip_policy.TOPIC_MIN_REPORT_SEC,
            body_len / transcription_segments.SRT_ESTIMATED_CHARS_PER_SEC,
        ),
    )
    return int(min(chunk_end, start_s + estimated))


def build_chunk_prompt(ch, index, total, compact=False, streamer_name=None):
    """构造字幕/弹幕首轮 prompt；人工时间轴不得参与这一轮。"""
    chunk_start = ch["start"]
    chunk_end = ch.get("end", ch["start"] + CHUNK_SEC)
    text_limit = LLM_COMPACT_TEXT_CHARS if compact else LLM_FULL_TEXT_CHARS
    context = title_analysis._prompt_context(
        streamer_name,
        context_text=ch.get("text") or "",
        compact=compact,
    )
    prompt = render_topic_analysis_prompt(
        TopicAnalysisPromptEvidence(
            context=context,
            compact=bool(compact),
            chunk_index=index + 1,
            chunk_total=total,
            start_label=timecode.format_elapsed(chunk_start),
            end_label=timecode.format_elapsed(chunk_end),
            danmaku_info=str(ch["danmaku_info"]),
            danmaku_evidence=tuple(ch.get("danmaku_evidence") or ()),
            subtitle_text=str(ch["text"])[:text_limit],
        )
    )
    return prompt, chunk_start, chunk_end


def strip_code_fence(response):
    """去掉 LLM 可能包裹的 Markdown 代码块。"""
    response = (response or "").strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```$", "", response)
    return response.strip()


def is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end, tolerance=90):
    """只接受当前分块时间范围附近的话题，过滤模型复读旧示例。"""
    if end_s <= start_s:
        return False
    if start_s < chunk_start - tolerance:
        return False
    if end_s > chunk_end + tolerance:
        return False
    return True


def parse_json_topics_response(
    response,
    chunk_start,
    chunk_end,
    accepted_topics,
):
    """优先解析结构化 JSON；不是 JSON 时返回 ``None``。"""
    payload = llm_gateway.extract_json_payload(response)
    if payload is None:
        return None
    if isinstance(payload, dict):
        raw_topics = payload.get("topics", [])
    elif isinstance(payload, list):
        raw_topics = payload
    else:
        return [], []
    if not isinstance(raw_topics, list):
        return [], []

    parsed_topics = []
    clip_marks = []
    for item in raw_topics:
        if not isinstance(item, dict):
            continue
        try:
            start_str = str(item.get("start", "")).strip()
            end_str = str(item.get("end", "")).strip()
            start_s = timecode.parse_hms(start_str)
            end_s = timecode.parse_hms(end_str)
        except Exception:
            continue
        if not is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end):
            continue
        raw_title = str(item.get("title", "")).strip()
        if title_analysis._is_placeholder_title(raw_title):
            continue
        body_lines = normalization.filter_unsupported_ai_points(
            normalization.json_points_to_body(
                item.get(
                    "points",
                    item.get("body", item.get("summary", item.get("details"))),
                )
            )
        )
        if not body_lines:
            continue
        end_s = repair_short_topic_end(
            start_s,
            end_s,
            body_lines,
            chunk_end,
        )
        title = title_analysis._derive_topic_title(
            title_analysis._clean_topic_title(raw_title),
            body_lines,
        )
        if not title:
            continue
        topic = {
            "start": start_s,
            "end": end_s,
            "start_str": start_str,
            "end_str": timecode.format_elapsed(end_s),
            "title": title,
            "publish_title": title_analysis._normalise_publish_title(
                item.get("publish_title"),
                title,
            ),
            "can_slice": topic_response.json_can_slice(
                item.get("can_slice", False),
                raw_title,
            ),
            "body": body_lines,
        }
        title_hook = title_analysis._normalise_title_hook(
            item.get("title_hook")
        )
        if title_hook:
            topic["title_hook"] = title_hook
        if clip_deduplication._is_duplicate_topic(topic, accepted_topics):
            continue
        accepted_topics.append(topic)
        parsed_topics.append(topic)
        if topic["can_slice"]:
            clip_marks.append(
                {
                    "start": topic["start"],
                    "end": topic["end"],
                    "title": topic["title"],
                }
            )

    report_blocks = [
        topic_formatting.format_topic_block(topic, idx + 1)
        for idx, topic in enumerate(parsed_topics)
    ]
    return report_blocks, clip_deduplication._dedupe_clip_marks(clip_marks)


def parse_llm_response(
    response,
    chunk_start,
    chunk_end,
    accepted_topics=None,
    allow_markdown_fallback=True,
):
    """解析单个分块的 LLM 输出，返回报告块与切片标记。"""
    accepted_topics = accepted_topics if accepted_topics is not None else []
    json_result = parse_json_topics_response(
        response,
        chunk_start,
        chunk_end,
        accepted_topics,
    )
    if json_result is not None:
        return json_result
    if not allow_markdown_fallback:
        return [], []

    response = strip_code_fence(response)
    if not response or response.strip() == "无明显话题":
        return [], []

    parsed_topics = []
    current = None

    def flush_current():
        if not current:
            return
        start_s = current["start"]
        end_s = current["end"]
        if not is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end):
            return
        if title_analysis._is_placeholder_title(current["title"]):
            return
        body_lines = [
            normalization.normalise_body_line(line)
            for line in current["body"]
        ]
        body_lines = [line for line in body_lines if line]
        if not body_lines:
            return
        end_s = repair_short_topic_end(
            start_s,
            end_s,
            body_lines,
            chunk_end,
        )
        title = title_analysis._derive_topic_title(
            current["title"],
            body_lines,
        )
        if not title:
            return
        topic = {
            "start": start_s,
            "end": end_s,
            "start_str": current["start_str"],
            "end_str": timecode.format_elapsed(end_s),
            "title": title,
            "can_slice": current["can_slice"],
            "body": body_lines,
        }
        if clip_deduplication._is_duplicate_topic(topic, accepted_topics):
            return
        accepted_topics.append(topic)
        parsed_topics.append(topic)

    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^Part\s*\d+\s*[:：]", line, re.IGNORECASE):
            continue
        match = HEADING_RE.match(line)
        if match:
            flush_current()
            start_str, end_str, raw_title = match.groups()
            current = {
                "start_str": start_str,
                "end_str": end_str,
                "start": timecode.parse_hms(start_str),
                "end": timecode.parse_hms(end_str),
                "title": title_analysis._clean_topic_title(raw_title),
                "can_slice": topic_response.is_slice_marked(raw_title),
                "body": [],
            }
        elif current:
            current["body"].append(line)

    flush_current()
    report_blocks = [
        topic_formatting.format_topic_block(topic, idx + 1)
        for idx, topic in enumerate(parsed_topics)
    ]
    clip_marks = [
        {
            "start": topic["start"],
            "end": topic["end"],
            "title": topic["title"],
        }
        for topic in parsed_topics
        if topic["can_slice"]
    ]
    return report_blocks, clip_marks


def strip_prompt_time_labels(text):
    """去掉分块字幕里的 ``[time]`` 标签。"""
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"^\[[^\]]+\]\s*", "", raw).strip()
        if line:
            lines.append(line)
    return " ".join(lines)


def make_fallback_topic_from_chunk(ch, streamer_name=None):
    """无有效输出时生成非切片兜底时间轴，避免整场报告空白。"""
    text = strip_prompt_time_labels(ch.get("text", ""))
    text = re.sub(r"\s+", "", text)
    if len(text) < 20:
        return None
    title = title_analysis._fallback_title_from_text(text)
    topic = {
        "start": int(ch["start"]),
        "end": int(ch.get("end", ch["start"] + CHUNK_SEC)),
        "start_str": timecode.format_elapsed(ch["start"]),
        "end_str": timecode.format_elapsed(
            ch.get("end", ch["start"] + CHUNK_SEC)
        ),
        "title": title,
        "can_slice": False,
        "body": [
            f"·本段为{streamer_name}的连续聊天/互动，字幕识别较碎，已保留在时间轴中",
            "·该段未形成稳定可切片主题，暂不标记为自动切片",
        ],
        "fallback": True,
    }
    return topic


def analyze_topic_chunks(
    chunks,
    streamer_display_name,
    progress_callback=None,
    checkpoint_path=None,
):
    """逐块独立分析字幕和弹幕；请求并行，结果按视频顺序合并。"""
    if not chunks:
        return [], [], None

    total = len(chunks)
    report_progress = llm_execution.serialized_progress_callback(
        progress_callback
    )
    stored_responses = load_topic_analysis_checkpoint(checkpoint_path)
    active_checkpoint_responses = {}
    prepared_chunks = []
    outcomes = {}
    pending = []

    for index, chunk in enumerate(chunks):
        prompt, chunk_start, chunk_end = build_chunk_prompt(
            chunk,
            index,
            total,
            compact=False,
            streamer_name=streamer_display_name,
        )
        compact_prompt, _, _ = build_chunk_prompt(
            chunk,
            index,
            total,
            compact=True,
            streamer_name=streamer_display_name,
        )
        fingerprint = topic_analysis_prompt_fingerprint(
            prompt,
            compact_prompt,
        )
        prepared = {
            "index": index,
            "chunk": chunk,
            "prompt": prompt,
            "compact_prompt": compact_prompt,
            "chunk_start": chunk_start,
            "chunk_end": chunk_end,
            "fingerprint": fingerprint,
            "pct": 25 + int((index / total) * 68),
        }
        prepared_chunks.append(prepared)
        cache_key = str(index + 1)
        cached = stored_responses.get(cache_key)
        if (
            isinstance(cached, dict)
            and cached.get("fingerprint") == fingerprint
            and isinstance(cached.get("response"), str)
            and llm_gateway.extract_json_payload(cached["response"])
            is not None
        ):
            outcomes[index] = {"response": cached["response"], "cached": True}
            active_checkpoint_responses[cache_key] = cached
        else:
            pending.append(prepared)

    cached_count = total - len(pending)
    if report_progress and cached_count:
        report_progress(
            f"Step 4/5: 已复用首轮分析缓存 {cached_count}/{total} 块",
            24,
            100,
        )

    if pending:
        try:
            api_config = llm_gateway.load_api_config()
        except Exception as exc:
            message = llm_gateway.short_llm_error(exc)
            if report_progress:
                report_progress(f"API 配置无效: {message}", 0, 100)
            raise RuntimeError(f"API 配置无效: {message}") from exc

        concurrency = min(
            llm_execution.configured_llm_concurrency(),
            len(pending),
        )
        initial_submission_count = (
            1
            if getattr(api_config, "analysis_reasoning_effort", None) == "xhigh"
            else concurrency
        )
        if report_progress:
            report_progress(
                f"Step 4/5: {LLM_ANALYSIS_MODEL} 分块分析 "
                f"({len(pending)} 块待处理，{concurrency} 路并行)...",
                25,
                100,
            )

        successful_response_count = cached_count
        consecutive_failed_chunks = 0
        completed_pending = 0
        checkpoint_warning_reported = False
        provider_retry_coordinator = llm_gateway.LLMProviderRetryCoordinator()

        def request_chunk(prepared):
            return llm_gateway.call_llm_with_retry(
                prepared["prompt"],
                compact_prompt=prepared["compact_prompt"],
                require_json=True,
                progress_callback=report_progress,
                progress_label=f"块 {prepared['index'] + 1} API",
                progress_step=prepared["pct"],
                retry_coordinator=provider_retry_coordinator,
                model_override=LLM_ANALYSIS_MODEL,
                reasoning_stage="analysis",
            )

        pending_iterator = iter(pending)
        active_futures = {}

        def submit_next(executor):
            try:
                prepared = next(pending_iterator)
            except StopIteration:
                return False
            future = executor.submit(request_chunk, prepared)
            active_futures[future] = prepared
            return True

        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="autoslice-llm",
        ) as executor:
            for _ in range(initial_submission_count):
                if not submit_next(executor):
                    break

            while active_futures:
                completed, _ = wait(
                    tuple(active_futures),
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    prepared = active_futures.pop(future)
                    index = prepared["index"]
                    try:
                        response = future.result()
                    except Exception as exc:
                        if isinstance(
                            exc,
                            llm_gateway.LLMProviderUnavailableError,
                        ):
                            for active_future in active_futures:
                                active_future.cancel()
                            raise RuntimeError(
                                "LLM 上游推理服务持续不可用，已暂停本次分析；"
                                "已完成的检查点会保留，稍后直接重试即可。"
                            ) from exc
                        outcomes[index] = {"error": exc}
                        short_error = llm_gateway.short_llm_error(exc)
                        consecutive_failed_chunks = (
                            consecutive_failed_chunks + 1
                            if llm_gateway.is_retryable_llm_error(exc)
                            else MAX_INITIAL_FAILED_CHUNKS
                        )
                        if report_progress:
                            report_progress(
                                f"块 {index + 1} API 连续失败，已跳过: "
                                f"{short_error}",
                                prepared["pct"],
                                100,
                            )
                    else:
                        outcomes[index] = {
                            "response": response,
                            "cached": False,
                        }
                        successful_response_count += 1
                        consecutive_failed_chunks = 0
                        active_checkpoint_responses[str(index + 1)] = {
                            "fingerprint": prepared["fingerprint"],
                            "response": response,
                            "updated_at": datetime.now().isoformat(
                                timespec="seconds"
                            ),
                        }
                        checkpoint_saved = write_topic_analysis_checkpoint(
                            checkpoint_path,
                            active_checkpoint_responses,
                            total,
                        )
                        if (
                            not checkpoint_saved
                            and report_progress
                            and not checkpoint_warning_reported
                        ):
                            checkpoint_warning_reported = True
                            report_progress(
                                "首轮分析检查点写入失败，本次分析继续；"
                                "请检查目录权限",
                                prepared["pct"],
                                100,
                            )
                    completed_pending += 1
                    if report_progress:
                        report_progress(
                            "Step 4/5: LLM分析完成 "
                            f"({cached_count + completed_pending}/{total}，"
                            f"第 {index + 1} 块)",
                            25
                            + int(
                                (
                                    (cached_count + completed_pending)
                                    / total
                                )
                                * 68
                            ),
                            100,
                        )

                if (
                    consecutive_failed_chunks >= MAX_INITIAL_FAILED_CHUNKS
                    and successful_response_count == 0
                ):
                    for future in active_futures:
                        future.cancel()
                    raise RuntimeError(
                        f"LLM API 连续 {consecutive_failed_chunks} 个分块失败，"
                        "疑似上游服务不可用。"
                    )

                if successful_response_count > 0 or not active_futures:
                    while (
                        len(active_futures) < concurrency
                        and submit_next(executor)
                    ):
                        pass

    accepted_topics = []
    failed_chunks = []
    for prepared in prepared_chunks:
        index = prepared["index"]
        chunk = prepared["chunk"]
        chunk_start = prepared["chunk_start"]
        chunk_end = prepared["chunk_end"]
        outcome = outcomes[index]
        error = outcome.get("error")
        if error is not None:
            failed_chunks.append(
                {
                    "index": index + 1,
                    "start": int(chunk_start),
                    "end": int(chunk_end),
                    "time": timecode.format_elapsed(chunk_start),
                    "error": llm_gateway.short_llm_error(error),
                }
            )
            fallback_topic = make_fallback_topic_from_chunk(
                chunk,
                streamer_name=streamer_display_name,
            )
            if fallback_topic and not clip_deduplication._is_duplicate_topic(
                fallback_topic,
                accepted_topics,
            ):
                accepted_topics.append(fallback_topic)
            continue

        before_topic_count = len(accepted_topics)
        parse_llm_response(
            outcome["response"],
            chunk_start,
            chunk_end,
            accepted_topics,
            allow_markdown_fallback=False,
        )
        if len(accepted_topics) == before_topic_count:
            fallback_topic = make_fallback_topic_from_chunk(
                chunk,
                streamer_name=streamer_display_name,
            )
            if fallback_topic and not clip_deduplication._is_duplicate_topic(
                fallback_topic,
                accepted_topics,
            ):
                accepted_topics.append(fallback_topic)

    return accepted_topics, failed_chunks, None
