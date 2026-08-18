"""高能切片候选的独立 LLM 复核编排。"""

import math
from concurrent.futures import ThreadPoolExecutor

from autoslice.analysis import (
    clip_policy,
    clip_review_candidates,
    clip_review_prompt,
    clip_scoring,
    llm_execution,
    manual_enrichment,
    response_parsing,
)
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.llm import transport as llm_gateway

FACADE_EXPORTS = {
    "_review_peak_selected_topics": "review_peak_selected_topics",
}


def review_peak_selected_topics(
    topics,
    srt_segments,
    peaks,
    streamer_name=None,
    progress_callback=None,
    checkpoint_callback=None,
    resume=False,
):
    """对峰值候选做独立字幕复核；缺项会逐步缩小批次重试。"""
    if resume:
        selected = [
            topic
            for topic in topics
            if (
                (topic.get("can_slice") or topic.get("clip_review_candidate"))
                and not topic.get("clip_review_validated")
                and topic.get("clip_review_rejection") == "等待独立字幕复核"
            )
        ]
    else:
        selected = [
            topic
            for topic in topics
            if topic.get("can_slice") or topic.get("clip_review_candidate")
        ]
    if not selected:
        return None

    high_energy_peaks = danmaku_analysis._high_energy_danmaku_peaks(
        peaks,
        danmaku_analysis._average_danmaku_density(peaks),
    )
    for original in selected:
        original["clip_review_validated"] = False
        original["clip_review_rejection"] = "等待独立字幕复核"
        if not resume:
            original["clip_review_attempts"] = 0

    unresolved = list(selected)
    last_errors = {}
    report_progress = llm_execution.serialized_progress_callback(progress_callback)
    provider_retry_coordinator = llm_gateway.LLMProviderRetryCoordinator()
    review_rounds = (
        (
            (clip_policy.CLIP_REVIEW_RETRY_BATCH_SIZE, "检查点补充"),
            (1, "检查点逐项兜底"),
        )
        if resume
        else (
            (clip_policy.CLIP_REVIEW_BATCH_SIZE, "首轮"),
            (clip_policy.CLIP_REVIEW_RETRY_BATCH_SIZE, "缺项补充"),
            (1, "逐项兜底"),
        )
    )
    for batch_size, round_label in review_rounds:
        if not unresolved:
            break
        retry_items = []
        total_batches = math.ceil(len(unresolved) / batch_size)
        jobs = []
        for batch_index, offset in enumerate(
            range(0, len(unresolved), batch_size),
            1,
        ):
            originals = unresolved[offset : offset + batch_size]
            candidates = [
                clip_review_candidates.build_clip_review_candidate(
                    topic,
                    srt_segments,
                    high_energy_peaks,
                    density_series=peaks,
                )
                for topic in originals
            ]
            if report_progress:
                report_progress(
                    f"高能切片字幕复核 {round_label} ({batch_index}/{total_batches})...",
                    95,
                    100,
                )
            prompt = clip_review_prompt.build_clip_candidate_review_prompt(
                candidates,
                streamer_name=streamer_name,
                compact=False,
            )
            compact_prompt = clip_review_prompt.build_clip_candidate_review_prompt(
                candidates,
                streamer_name=streamer_name,
                compact=True,
            )
            for original in originals:
                original["clip_review_attempts"] = int(
                    original.get("clip_review_attempts", 0)
                ) + 1
            jobs.append(
                {
                    "batch_index": batch_index,
                    "originals": originals,
                    "candidates": candidates,
                    "prompt": prompt,
                    "compact_prompt": compact_prompt,
                }
            )

        def review_job(job):
            return llm_gateway.call_llm_with_retry(
                job["prompt"],
                compact_prompt=job["compact_prompt"],
                require_json=True,
                progress_callback=report_progress,
                progress_label="高能切片字幕复核",
                progress_step=95,
                retry_coordinator=provider_retry_coordinator,
                reasoning_stage="review",
            )

        concurrency = min(
            llm_execution.configured_llm_concurrency(),
            max(1, len(jobs)),
        )
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="autoslice-review",
        ) as executor:
            futures = [executor.submit(review_job, job) for job in jobs]
            for job, future in zip(jobs, futures):
                batch_index = job["batch_index"]
                originals = job["originals"]
                candidates = job["candidates"]
                try:
                    response = future.result()
                except Exception as exc:
                    error = f"API复核失败：{llm_gateway.short_llm_error(exc)}"
                    for original in originals:
                        last_errors[id(original)] = error
                        retry_items.append(original)
                else:
                    response_payload = llm_gateway.extract_json_payload(response)
                    raw_items = (
                        response_payload.get("topics", [])
                        if isinstance(response_payload, dict)
                        else []
                    )
                    items_by_id = {}
                    for item in raw_items if isinstance(raw_items, list) else []:
                        if not isinstance(item, dict):
                            continue
                        try:
                            item_id = int(item.get("id"))
                        except (TypeError, ValueError):
                            continue
                        if (
                            1 <= item_id <= len(candidates)
                            and item_id not in items_by_id
                        ):
                            items_by_id[item_id] = item

                    for item_id, (original, candidate) in enumerate(
                        zip(originals, candidates),
                        1,
                    ):
                        item = items_by_id.get(item_id)
                        if item is None or "valid" not in item:
                            last_errors[id(original)] = "模型未返回该候选的有效结构"
                            retry_items.append(original)
                            continue
                        base_interest_score = clip_scoring.parse_clip_interest_score(
                            item.get("base_interest_score")
                        )
                        timeline_star_bonus = clip_scoring.parse_clip_star_bonus(
                            item.get("timeline_star_bonus")
                        )
                        manual_star_count = max(
                            0,
                            int(candidate.get("manual_stars", 0) or 0),
                        )
                        timeline_star_bonus_cap = clip_scoring.clip_star_bonus_cap(
                            manual_star_count
                        )
                        interest_reason = clip_scoring.clip_interest_reason(item)
                        if not response_parsing.json_can_slice(
                            item.get("valid"),
                            "",
                        ):
                            original["clip_review_validated"] = False
                            original["clip_review_rejection"] = str(
                                item.get("reason", "字幕证据不足")
                            ).strip() or "字幕证据不足"
                            if base_interest_score is not None:
                                original["clip_interest_base_score"] = (
                                    base_interest_score
                                )
                            if timeline_star_bonus is not None:
                                original["clip_timeline_star_bonus"] = min(
                                    timeline_star_bonus,
                                    timeline_star_bonus_cap,
                                )
                            if interest_reason:
                                original["clip_interest_reason"] = interest_reason
                            last_errors.pop(id(original), None)
                            continue
                        if base_interest_score is None or timeline_star_bonus is None:
                            last_errors[id(original)] = "模型未返回有效投稿价值评分"
                            retry_items.append(original)
                            continue
                        if timeline_star_bonus > timeline_star_bonus_cap:
                            last_errors[id(original)] = (
                                f"{manual_star_count} 星人工记录最多只能增加 "
                                f"{timeline_star_bonus_cap:g} 分"
                            )
                            retry_items.append(original)
                            continue
                        interest_score = round(
                            min(
                                100.0,
                                base_interest_score + timeline_star_bonus,
                            ),
                            1,
                        )
                        if interest_score < clip_policy.CLIP_MIN_INTEREST_SCORE:
                            original["clip_review_validated"] = False
                            original["clip_interest_base_score"] = base_interest_score
                            original["clip_timeline_star_bonus"] = timeline_star_bonus
                            original["clip_interest_score"] = interest_score
                            original["clip_interest_reason"] = interest_reason
                            detail = (
                                interest_reason
                                or "内容完整但投稿钩子或反应强度不足"
                            )
                            original["clip_review_rejection"] = (
                                f"投稿价值 {interest_score:g} 分，低于 "
                                f"{clip_policy.CLIP_MIN_INTEREST_SCORE} 分：{detail}"
                            )
                            last_errors.pop(id(original), None)
                            continue
                        enriched = manual_enrichment.enrich_manual_topic_from_item(
                            candidate,
                            item,
                        )
                        if not enriched or not enriched.get("ai_focus_validated"):
                            last_errors[id(original)] = "复核边界或正文无效"
                            retry_items.append(original)
                            continue
                        enriched["clip_review_validated"] = True
                        enriched["clip_review_rejection"] = None
                        enriched["clip_review_attempts"] = original[
                            "clip_review_attempts"
                        ]
                        enriched["clip_interest_base_score"] = base_interest_score
                        enriched["clip_timeline_star_bonus"] = timeline_star_bonus
                        enriched["clip_interest_score"] = interest_score
                        enriched["clip_interest_reason"] = interest_reason
                        enriched["can_slice"] = False
                        original.clear()
                        original.update(enriched)
                        last_errors.pop(id(original), None)

                if checkpoint_callback:
                    checkpoint_callback(
                        topics,
                        retry_items,
                        round_label,
                        batch_index,
                        total_batches,
                    )
        unresolved = retry_items

    for original in unresolved:
        original["clip_review_validated"] = False
        original["clip_review_rejection"] = last_errors.get(
            id(original),
            "独立字幕复核未完成",
        )

    if not unresolved:
        return None
    details = "；".join(
        f"{topic.get('title', '未命名候选')}：{topic.get('clip_review_rejection')}"
        for topic in unresolved[:5]
    )
    if len(unresolved) > 5:
        details += f"；另有 {len(unresolved) - 5} 项"
    return (
        f"高能切片候选仍有 {len(unresolved)} 项在全部复核轮次后缺少有效结构，"
        f"未通过项不会自动切片：{details}"
    )
