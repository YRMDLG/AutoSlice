"""从既有产物恢复切片复核所需状态的流水线阶段 owner。"""

from __future__ import annotations

import json
import os


def prepare_retry_pipeline_state(
        flv_path, json_path=None, report_path=None, output_dir=None,
        artifact_dir=None, *, artifact_bundle_layout,
        organize_existing_artifacts, seed_artifact_from_legacy,
        manual_timeline_for_rebuilt_report, parse_generated_topic_report,
        clean_topics_for_report, analysis_topics_snapshot,
        merge_manual_timeline_topics, clip_review_checkpoint_matches_policy,
        clip_review_checkpoint_is_complete, topic_review_focus_max_sec):
    """整理并恢复 retry 阶段在字幕解析前需要的全部状态。

    该阶段只负责已有 JSON、报告、人工时间轴和复核检查点的恢复准备。
    领域清洗、人工时间轴和检查点规则均由调用方显式注入，避免 owner
    复制实现或反向依赖高层 façade。
    """
    def topic_range_key(topic):
        return (
            int(topic.get("start", 0) or 0),
            int(topic.get("end", 0) or 0),
        )

    def merge_completed_review_with_baseline(baseline_topics, checkpoint_topics):
        """把已复核状态覆盖回最新分析，保留检查点之外的新候选。"""
        has_new_manual_candidates = any(
            topic.get("manual_timeline_review")
            or topic.get("postcheck_pending")
            or topic.get("source") in {"manual_timeline", "optimized_manual_timeline"}
            or any(
                entry.get("source") == "optimized_manual_timeline"
                for entry in topic.get("manual_timeline") or []
                if isinstance(entry, dict)
            )
            for topic in baseline_topics or []
            if isinstance(topic, dict)
        )
        if not has_new_manual_candidates:
            return list(checkpoint_topics or [])
        checkpoint_by_range = {
            topic_range_key(topic): topic
            for topic in checkpoint_topics or []
            if isinstance(topic, dict)
        }
        merged = []
        matched_ranges = set()
        for baseline in baseline_topics or []:
            key = topic_range_key(baseline)
            reviewed = checkpoint_by_range.get(key)
            if reviewed is not None:
                reviewed = dict(reviewed)
                existing_manual_entries = list(
                    reviewed.get("manual_timeline") or []
                )
                baseline_manual_entries = list(
                    baseline.get("manual_timeline") or []
                )
                new_manual_entries = [
                    entry
                    for entry in baseline_manual_entries
                    if entry not in existing_manual_entries
                ]
                optimized_manual_entries = [
                    entry
                    for entry in existing_manual_entries + new_manual_entries
                    if isinstance(entry, dict)
                    and entry.get("source") == "optimized_manual_timeline"
                ]
                unreviewed_manual_candidate = bool(
                    optimized_manual_entries
                    and reviewed.get("clip_review_validated") is None
                    and not reviewed.get("clip_review_rejection")
                )
                if new_manual_entries:
                    # 检查点可能已经有同范围的旧复核结果，但最新分析才
                    # 挂上人工时间轴。不能因为范围相同就把这条新证据吞掉；
                    # 合并后必须重新进入 Terra 独立复核。
                    merge_manual_timeline_topics(
                        [reviewed],
                        new_manual_entries,
                    )
                if new_manual_entries or unreviewed_manual_candidate:
                    if any(
                        entry.get("source") == "optimized_manual_timeline"
                        for entry in new_manual_entries
                    ) or unreviewed_manual_candidate:
                        reviewed["manual_timeline_review"] = True
                        reviewed["clip_review_candidate"] = True
                        reviewed["clip_candidate_sources"] = [
                            "人工时间轴语义复核"
                        ]
                        reviewed["clip_review_validated"] = False
                        reviewed["clip_review_rejection"] = "等待独立字幕复核"
                        reviewed["clip_review_attempts"] = 0
                merged.append(reviewed)
                matched_ranges.add(key)
                continue
            topic = dict(baseline)
            if topic.get("manual_timeline_review"):
                topic["clip_review_candidate"] = True
                topic["clip_candidate_sources"] = ["人工时间轴语义复核"]
                topic["clip_review_validated"] = False
                topic["clip_review_rejection"] = "等待独立字幕复核"
                topic["clip_review_attempts"] = 0
            merged.append(topic)

        # 报告恢复不完整时，仍保留检查点中的已复核话题，避免续跑丢结果。
        merged.extend(
            topic
            for topic in checkpoint_topics or []
            if isinstance(topic, dict)
            and topic_range_key(topic) not in matched_ranges
            and not any(
                topic_range_key(existing) == topic_range_key(topic)
                for existing in merged
            )
        )
        return merged

    flv_path = os.path.abspath(flv_path)
    base, _ = os.path.splitext(flv_path)
    if output_dir is None and artifact_dir is None:
        output_dir = os.path.dirname(flv_path)
    artifact_layout = artifact_bundle_layout(
        flv_path,
        output_dir=output_dir,
        artifact_dir=artifact_dir,
    )
    os.makedirs(artifact_layout["data_dir"], exist_ok=True)
    if json_path is None and not os.path.isfile(artifact_layout["clip_marks_path"]):
        legacy_json_path = base + "_clip_marks.json"
        if os.path.isfile(legacy_json_path):
            organize_existing_artifacts(
                flv_path,
                output_dir=artifact_layout["output_root"],
                json_path=legacy_json_path,
                report_path=base + "_话题分析.md",
                artifact_dir=artifact_layout["artifact_dir"],
            )
    json_path = json_path or artifact_layout["clip_marks_path"]
    report_path = report_path or artifact_layout["report_path"]
    if not os.path.isfile(json_path):
        raise FileNotFoundError(f"切片标记 JSON 不存在: {json_path}")
    with open(json_path, encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    if not isinstance(data, dict):
        raise ValueError("切片标记 JSON 根节点必须是对象")

    rebuilt_manual_timeline = manual_timeline_for_rebuilt_report(
        data.get("manual_timeline"),
        flv_path,
    )
    rebuilt_manual_entries = rebuilt_manual_timeline.get("entries") or []
    recovered_topics = data.get("analysis_topics")
    if not isinstance(recovered_topics, list) or not recovered_topics:
        recovered_topics = parse_generated_topic_report(report_path)
    baseline_topics = clean_topics_for_report(
        analysis_topics_snapshot(recovered_topics)
    )
    if rebuilt_manual_entries:
        merge_manual_timeline_topics(
            baseline_topics,
            rebuilt_manual_entries,
        )
        baseline_topics = clean_topics_for_report(baseline_topics)
    analysis_topics = analysis_topics_snapshot(baseline_topics)

    clip_review_checkpoint_path = (
        data.get("clip_review_checkpoint_path")
        or artifact_layout["clip_review_checkpoint_path"]
    )
    seed_artifact_from_legacy(
        clip_review_checkpoint_path,
        base + "_clip_review_checkpoint.json",
    )
    resume_review = False
    reuse_completed_review = False
    checkpoint_policy_stale = False
    stale_review_keys = set()
    accepted_topics = baseline_topics
    if os.path.isfile(clip_review_checkpoint_path):
        try:
            with open(clip_review_checkpoint_path, encoding="utf-8") as file_obj:
                checkpoint = json.load(file_obj)
            checkpoint_policy_stale = not clip_review_checkpoint_matches_policy(
                checkpoint
            )
            checkpoint_topics = checkpoint.get("topics")
            resume_stages = {"reviewing", "resuming", "completed_with_warning"}
            if isinstance(checkpoint_topics, list) and checkpoint_topics:
                if checkpoint_policy_stale:
                    # 旧策略的已通过项可能已被收缩到峰值之外；把它们重新
                    # 送入本版规则复核，同时把最新优化时间轴重新挂回话题。
                    accepted_topics = clean_topics_for_report(checkpoint_topics)
                    if rebuilt_manual_entries:
                        merge_manual_timeline_topics(
                            accepted_topics,
                            rebuilt_manual_entries,
                        )
                        accepted_topics = clean_topics_for_report(accepted_topics)
                    stale_review_keys = {
                        (
                            int(topic.get("start", 0) or 0),
                            int(topic.get("end", 0) or 0),
                            str(topic.get("title", "")),
                        )
                        for topic in accepted_topics
                        if (
                            topic.get("clip_review_attempts") is not None
                            or topic.get("clip_review_validated") is not None
                        )
                    }
                    reuse_completed_review = False
                    resume_review = False
                    checkpoint_topics = None
                else:
                    for topic in checkpoint_topics:
                        if (
                            topic.get("clip_review_validated") is True
                            and int(topic.get("end", 0))
                            - int(topic.get("start", 0))
                            > topic_review_focus_max_sec
                        ):
                            topic["clip_review_validated"] = False
                            topic["clip_review_rejection"] = "等待独立字幕复核"
                            topic["can_slice"] = True
                    pending_topics = [
                        topic for topic in checkpoint_topics
                        if (
                            topic.get("can_slice")
                            and not topic.get("clip_review_validated")
                            and topic.get("clip_review_rejection") == "等待独立字幕复核"
                        )
                    ]
                    if pending_topics and checkpoint.get("stage") in resume_stages:
                        accepted_topics = clean_topics_for_report(checkpoint_topics)
                        resume_review = True
                    elif clip_review_checkpoint_is_complete(
                            checkpoint, checkpoint_topics):
                        if rebuilt_manual_entries:
                            latest_topics = list(baseline_topics)
                            accepted_topics = clean_topics_for_report(
                                merge_completed_review_with_baseline(
                                    latest_topics,
                                    checkpoint_topics,
                                )
                            )
                            has_pending_manual_review = any(
                                topic.get("manual_timeline_review")
                                and not topic.get("clip_review_validated")
                                for topic in accepted_topics
                            )
                            resume_review = has_pending_manual_review
                            reuse_completed_review = not has_pending_manual_review
                        else:
                            accepted_topics = clean_topics_for_report(checkpoint_topics)
                            reuse_completed_review = True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            resume_review = False
    if not accepted_topics:
        raise ValueError("已有产物中没有可用于复核的话题")

    return {
        "data": data,
        "artifact_layout": artifact_layout,
        "json_path": json_path,
        "report_path": report_path,
        "rebuilt_manual_timeline": rebuilt_manual_timeline,
        "analysis_topics": analysis_topics,
        "accepted_topics": accepted_topics,
        "clip_review_checkpoint_path": clip_review_checkpoint_path,
        "resume_review": resume_review,
        "reuse_completed_review": reuse_completed_review,
        "checkpoint_policy_stale": checkpoint_policy_stale,
        "stale_review_keys": stale_review_keys,
    }
