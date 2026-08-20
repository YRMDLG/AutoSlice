"""自动切片的薄编排层：连接转录、证据、复核、报告与切片服务。"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from autoslice.artifact_store import ARTIFACT_LAYOUT_VERSION
from autoslice.artifact_store import copy_artifact_file as _copy_artifact_file
from autoslice.artifact_store import seed_artifact_from_legacy as _seed_artifact_from_legacy
from autoslice.artifact_store import write_artifact_json as _write_artifact_json
from autoslice.artifact_store import write_artifact_text as _write_artifact_text
from autoslice import media_probe
from autoslice import pipeline_analysis
from autoslice import pipeline_llm
from autoslice import pipeline_manual
from autoslice import pipeline_review
from autoslice import pipeline_titles
from autoslice import pipeline_transcription
from autoslice import reporting as reporting_service
from autoslice import slicing as slicing_service
from autoslice import timecode
from autoslice.analysis import boundaries as boundary_analysis
from autoslice.analysis import checkpoints as checkpoint_store
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis.manual import artifacts as manual_artifacts
from autoslice.analysis.manual import candidates as manual_candidates
from autoslice.analysis.manual import review as manual_review
from autoslice.analysis.manual import timebase as timeline_analysis
from autoslice.analysis.manual import workflow as manual_timeline_analysis
from autoslice.analysis.report import cleanup as report_cleanup
from autoslice.analysis.review import outro as outro_analysis
from autoslice.analysis.review import policy as clip_policy
from autoslice.analysis.review import scoring as clip_scoring
from autoslice.analysis.review import workflow as clip_review
from autoslice.analysis.review import decisions as slice_decisions
from autoslice.analysis.topic import chunking as analysis_chunking
from autoslice.analysis.topic import titles as title_analysis
from autoslice.analysis.topic import analysis as topic_analysis
from autoslice.llm.prompts import build_system_prompt as _render_system_prompt
from autoslice.llm.prompts import build_title_hook_guide as _render_title_hook_guide
from autoslice.transcription import checkpoints as transcription_checkpoints
from autoslice.transcription import segments as transcription_segments
from autoslice.transcription import srt_io as transcription_srt_io
from autoslice.transcription import workflow as transcription_workflow
from autoslice.transcription.contracts import SubtitleTitleServices
from autoslice.streamer_profiles import (
    current_streamer_profile,
    streamer_profile_context,
)

FACADE_EXPORTS = {
    '_title_hook_prompt_guide': 'title_hook_prompt_guide',
    '_build_system_prompt': 'build_system_prompt',
    'subtitle_title_services': 'subtitle_title_services',
    '_optimized_timeline_paths': 'optimized_timeline_paths',
    '_write_optimized_timeline_files': 'write_optimized_timeline_files',
    '_load_optimized_timeline_artifact': 'load_optimized_timeline_artifact',
    '_prepare_optimized_manual_timeline': 'prepare_optimized_manual_timeline',
    'optimize_manual_timeline_for_video': 'optimize_manual_timeline_for_video',
    '_optimize_manual_timeline_for_video_impl': 'optimize_manual_timeline_for_video_impl',
    '_scaled_progress_callback': '_scaled_progress_callback',
    '_monotonic_progress_callback': '_monotonic_progress_callback',
    'run_pipeline': 'run_pipeline',
    '_run_pipeline_impl': 'run_pipeline_impl',
    '_manual_timeline_for_rebuilt_report': 'manual_timeline_for_rebuilt_report',
    '_warning_without_previous_clip_review': '_warning_without_previous_clip_review',
    'retry_clip_review_from_artifacts': 'retry_clip_review_from_artifacts',
    '_retry_clip_review_from_artifacts_impl': 'retry_clip_review_from_artifacts_impl',
}


# 可替换的公开依赖 seam；默认对象均直接来自各自唯一 owner。
ensure_srt = transcription_workflow.ensure_srt
export_corrected_srt = transcription_srt_io.export_corrected_srt
prepare_pipeline_subtitles = pipeline_transcription.prepare_pipeline_subtitles
prepare_pipeline_analysis = pipeline_analysis.prepare_pipeline_analysis
analyze_pipeline_llm_chunks = pipeline_llm.analyze_pipeline_llm_chunks
prepare_pipeline_manual_timeline = pipeline_manual.prepare_pipeline_manual_timeline
review_pipeline_candidates = pipeline_review.review_pipeline_candidates
review_pipeline_publish_titles = pipeline_titles.review_pipeline_publish_titles
probe_video_duration = media_probe.probe_video_duration
analyze_danmaku = danmaku_analysis.analyze_danmaku
parse_srt_text = analysis_chunking.parse_srt_text
parse_srt_segments = transcription_srt_io.load_repaired_srt_segments
chunk_srt = analysis_chunking.chunk_srt
fmt_time = timecode.format_elapsed
load_manual_timeline = timeline_analysis.load_manual_timeline

DanmakuDensitySeries = danmaku_analysis.DanmakuDensitySeries
MANUAL_TIMELINE_OPTIMIZATION_VERSION = timeline_analysis.MANUAL_TIMELINE_OPTIMIZATION_VERSION
MAX_PUBLISH_TITLE_CHARS = title_analysis.MAX_PUBLISH_TITLE_CHARS
CLIP_LOCAL_PEAK_RADIUS_SEC = danmaku_analysis.CLIP_LOCAL_PEAK_RADIUS_SEC
CLIP_MANUAL_REVIEW_MIN_STARS = clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS
CLIP_MIN_INTEREST_SCORE = clip_policy.CLIP_MIN_INTEREST_SCORE
LLM_ANALYSIS_MODEL = topic_analysis.LLM_ANALYSIS_MODEL
LLM_MODEL = reporting_service.LLM_REVIEW_MODEL
TOPIC_MAX_CLIP_SEC = clip_policy.TOPIC_MAX_CLIP_SEC
TOPIC_MIN_CLIP_SEC = clip_policy.TOPIC_MIN_CLIP_SEC
TOPIC_POST_CONTEXT_SEC = clip_policy.TOPIC_POST_CONTEXT_SEC
TOPIC_PRE_CONTEXT_SEC = clip_policy.TOPIC_PRE_CONTEXT_SEC
TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC = clip_policy.TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
TOPIC_REVIEW_FOCUS_MAX_SEC = clip_policy.TOPIC_REVIEW_FOCUS_MAX_SEC

_prompt_context = title_analysis._prompt_context
_build_title_style_prompt = title_analysis._build_title_style_prompt
_normalise_publish_title = title_analysis._normalise_publish_title
_review_selected_publish_titles = title_analysis.review_selected_publish_titles
_sanitize_optimized_manual_entry = (
    manual_candidates.sanitize_optimized_manual_entry
)
_extract_video_start_datetime = timeline_analysis.extract_video_start_datetime
_filter_manual_timeline_entries = timeline_analysis.filter_manual_timeline_entries
_manual_timeline_summary = timeline_analysis.manual_timeline_summary
_funasr_checkpoint_path = transcription_checkpoints.funasr_checkpoint_path

_analysis_topics_snapshot = checkpoint_store.analysis_topics_snapshot
_average_danmaku_density = danmaku_analysis._average_danmaku_density
_build_clip_candidate_review_audit = clip_scoring.build_clip_candidate_review_audit
_danmaku_clip_threshold = danmaku_analysis._danmaku_clip_threshold
_detect_stream_outro_clip = outro_analysis._detect_stream_outro_clip
_expand_clip_marks_with_context = boundary_analysis._expand_clip_marks_with_context
_high_energy_danmaku_peaks = danmaku_analysis._high_energy_danmaku_peaks
_outro_topic_from_mark = outro_analysis._outro_topic_from_mark
_review_peak_selected_topics = clip_review.review_peak_selected_topics
_srt_video_duration = transcription_segments.srt_video_duration
_validate_unmatched_manual_topics = manual_review.validate_unmatched_manual_topics
_write_completed_clip_review_checkpoint = checkpoint_store.write_completed_clip_review_checkpoint
_clip_review_checkpoint_is_complete = checkpoint_store.clip_review_checkpoint_is_complete
_clip_review_checkpoint_matches_policy = checkpoint_store.clip_review_checkpoint_matches_policy

# 旧调用方仍可从 pipeline 导入这些对象；唯一实现位于 analysis.manual.workflow。
_format_manual_entry_for_prompt = manual_timeline_analysis.format_manual_entry_for_prompt
_manual_timeline_info_for_chunk = manual_timeline_analysis.manual_timeline_info_for_chunk
attach_manual_timeline_to_chunks = manual_timeline_analysis.attach_manual_timeline_to_chunks
try_enrich_manual_topics = manual_timeline_analysis.try_enrich_manual_topics
optimized_manual_entries_from_topics = (
    manual_timeline_analysis.optimized_manual_entries_from_topics
)
optimized_entry_needs_retry = manual_timeline_analysis.optimized_entry_needs_retry
topic_from_optimized_entry = manual_timeline_analysis.topic_from_optimized_entry
_batch_warning_text = manual_timeline_analysis.batch_warning_text
retry_optimized_timeline_entries = (
    manual_timeline_analysis.retry_optimized_timeline_entries
)
optimize_manual_timeline = manual_timeline_analysis.optimize_manual_timeline


def title_hook_prompt_guide(streamer_name=None):
    """兼容 façade：把显式主播上下文交给唯一 prompt 实现。"""
    return _render_title_hook_guide(_prompt_context(streamer_name))


def build_system_prompt(streamer_name=None):
    """兼容 façade：构造显式上下文后调用唯一 prompt 实现。"""
    return _render_system_prompt(_prompt_context(streamer_name))


def subtitle_title_services():
    """向高层调用方提供显式标题服务，字幕模块无需反向导入本 façade。"""
    return SubtitleTitleServices(
        max_publish_title_chars=MAX_PUBLISH_TITLE_CHARS,
        build_title_style_prompt=_build_title_style_prompt,
        build_title_hook_prompt_guide=title_hook_prompt_guide,
        normalise_publish_title=_normalise_publish_title,
    )


optimized_timeline_paths = manual_artifacts.optimized_timeline_paths
write_optimized_timeline_files = manual_artifacts.write_optimized_timeline_files
load_optimized_timeline_artifact = (
    manual_artifacts.load_optimized_timeline_artifact
)
manual_timeline_for_rebuilt_report = (
    manual_artifacts.manual_timeline_for_rebuilt_report
)
_manual_timeline_for_rebuilt_report = manual_timeline_for_rebuilt_report


def prepare_optimized_manual_timeline(
        flv_path, video_base, srt_segments, peaks, video_duration,
        manual_timeline_path, streamer_name=None, progress_callback=None,
        retry_incomplete_artifact=True, artifact_layout=None):
    """加载、过滤并优化人工时间轴，返回后续可直接使用的结构。"""
    manual_timeline = load_manual_timeline(
        flv_path,
        manual_timeline_path=manual_timeline_path,
    )
    all_entries = manual_timeline.get("entries") or []
    raw_entries = timeline_analysis.filter_manual_timeline_entries(
        all_entries,
        video_duration,
    )
    manual_timeline["source_entry_count"] = len(all_entries)
    manual_timeline["raw_entry_count"] = len(raw_entries)
    manual_timeline["entries"] = raw_entries
    if not raw_entries:
        return manual_timeline

    optimized_json_path, optimized_md_path = optimized_timeline_paths(
        video_base, artifact_layout=artifact_layout
    )
    if artifact_layout:
        legacy_json_path, legacy_md_path = optimized_timeline_paths(video_base)
        _seed_artifact_from_legacy(optimized_json_path, legacy_json_path)
        _seed_artifact_from_legacy(optimized_md_path, legacy_md_path)

    def write_checkpoint(entries, warning):
        write_optimized_timeline_files(
            video_base,
            manual_timeline.get("path"),
            raw_entries,
            entries,
            warning=warning,
            artifact_layout=artifact_layout,
            video_path=flv_path,
        )

    reusable_artifact = None
    source_path = manual_timeline.get("path")
    if os.path.isfile(optimized_json_path) and source_path and os.path.isfile(source_path):
        try:
            artifact_is_current = (
                os.path.getmtime(optimized_json_path) >= os.path.getmtime(source_path)
            )
            if artifact_is_current:
                candidate = load_optimized_timeline_artifact(
                    optimized_json_path,
                    flv_path,
                    source_path,
                )
                if (
                    candidate.get("raw_entry_count") == len(raw_entries)
                    and candidate.get("optimization_version")
                    == MANUAL_TIMELINE_OPTIMIZATION_VERSION
                    and (
                        candidate.get("streamer_profile_id")
                        == current_streamer_profile().id
                        or (
                            not candidate.get("streamer_profile_id")
                            and current_streamer_profile().id == "zeyin"
                        )
                    )
                ):
                    reusable_artifact = candidate
        except (OSError, ValueError, TypeError):
            reusable_artifact = None

    if reusable_artifact:
        retry_count = sum(
            manual_timeline_analysis.optimized_entry_needs_retry(entry)
            for entry in reusable_artifact.get("entries") or []
        )
        passed_count = len(reusable_artifact["entries"]) - retry_count
        if retry_incomplete_artifact:
            if progress_callback:
                progress_callback(
                    f"复用 {passed_count} 个已通过候选，"
                    f"仅重试 {retry_count} 个低权重候选...",
                    20,
                    100,
                )
            optimized_entries, warning = (
                manual_timeline_analysis.retry_optimized_timeline_entries(
                    reusable_artifact.get("entries") or [],
                    srt_segments=srt_segments,
                    peaks=peaks,
                    streamer_name=streamer_name,
                    progress_callback=progress_callback,
                    checkpoint_callback=write_checkpoint,
                )
            )
        else:
            optimized_entries = reusable_artifact.get("entries") or []
            warning = reusable_artifact.get("optimization_warning")
            if retry_count:
                reuse_warning = (
                    f"为缩短整场分析耗时，复用 {passed_count} 个已验证候选；"
                    f"{retry_count} 个未验证候选仅作辅助参考"
                )
                warning = "；".join(
                    item for item in (warning, reuse_warning) if item
                )
            if progress_callback:
                progress_callback(
                    f"复用人工时间轴检查点：{passed_count} 个已验证，"
                    f"{retry_count} 个仅作参考",
                    22,
                    100,
                )
    else:
        def save_fresh_checkpoint(processed_topics, remaining_topics, warnings):
            pending_topics = []
            for topic in remaining_topics:
                pending = dict(topic)
                pending["reference_only"] = True
                pending_topics.append(pending)
            checkpoint_entries = (
                manual_timeline_analysis.optimized_manual_entries_from_topics(
                    list(processed_topics) + pending_topics
                )
            )
            write_checkpoint(
                checkpoint_entries,
                manual_timeline_analysis.batch_warning_text(
                    warnings, pending_count=len(remaining_topics)
                ),
            )

        optimized_entries, warning = manual_timeline_analysis.optimize_manual_timeline(
            raw_entries,
            srt_segments=srt_segments,
            peaks=peaks,
            streamer_name=streamer_name,
            progress_callback=progress_callback,
            batch_result_callback=save_fresh_checkpoint,
        )

    optimized_json_path, optimized_md_path = write_optimized_timeline_files(
        video_base,
        source_path,
        raw_entries,
        optimized_entries,
        warning=warning,
        artifact_layout=artifact_layout,
        video_path=flv_path,
    )
    manual_timeline["entries"] = optimized_entries
    manual_timeline["optimized_entry_count"] = len(optimized_entries)
    manual_timeline["optimized_json_path"] = optimized_json_path
    manual_timeline["optimized_md_path"] = optimized_md_path
    manual_timeline["optimization_warning"] = warning
    return manual_timeline


def optimize_manual_timeline_for_video(
        flv_path, manual_timeline_path, ass_path=None, progress_callback=None,
        output_dir=None, artifact_dir=None, streamer_profile_id="auto"):
    """在隔离的主播配置上下文中优化人工时间轴。"""
    flv_path = slicing_service.validate_video_path(flv_path)
    with streamer_profile_context(streamer_profile_id, flv_path) as profile:
        result = optimize_manual_timeline_for_video_impl(
            flv_path,
            manual_timeline_path,
            ass_path=ass_path,
            progress_callback=progress_callback,
            output_dir=output_dir,
            artifact_dir=artifact_dir,
        )
        result["streamer_profile_id"] = profile.id
        return result


def optimize_manual_timeline_for_video_impl(
        flv_path, manual_timeline_path, ass_path=None, progress_callback=None,
        output_dir=None, artifact_dir=None):
    """仅优化人工时间轴，不启动整场话题分析或自动切片。"""
    if not os.path.isfile(flv_path):
        raise FileNotFoundError(f"录播文件不存在: {flv_path}")
    if not manual_timeline_path or not os.path.isfile(manual_timeline_path):
        raise FileNotFoundError(f"人工时间轴文件不存在: {manual_timeline_path or '未选择'}")

    if output_dir is None and artifact_dir is None:
        output_dir = os.path.dirname(os.path.abspath(flv_path))
    artifact_layout = reporting_service.artifact_bundle_layout(
        flv_path,
        output_dir=output_dir,
        artifact_dir=artifact_dir,
    )
    os.makedirs(artifact_layout["data_dir"], exist_ok=True)
    _seed_artifact_from_legacy(
        artifact_layout["asr_checkpoint_path"],
        _funasr_checkpoint_path(flv_path),
    )

    if progress_callback:
        progress_callback("检查完整版字幕...", 0, 100)
    source_srt_path = ensure_srt(
        flv_path,
        progress_callback,
        checkpoint_path=artifact_layout["asr_checkpoint_path"],
    )
    if not source_srt_path:
        raise RuntimeError("无法生成 SRT 字幕")
    corrected_srt_path = export_corrected_srt(
        source_srt_path,
        output_path=artifact_layout["corrected_srt_path"],
    )
    srt_path = corrected_srt_path or source_srt_path
    srt_segments = parse_srt_text(srt_path)
    if not srt_segments:
        raise RuntimeError("字幕文件没有可用于校时的有效内容")

    if ass_path is None:
        candidate_ass_path = os.path.splitext(flv_path)[0] + ".ass"
        ass_path = candidate_ass_path if os.path.isfile(candidate_ass_path) else None
    if progress_callback:
        progress_callback("计算人工时间轴附近弹幕依据...", 15, 100)
    peaks = analyze_danmaku(ass_path) if ass_path and os.path.isfile(ass_path) else DanmakuDensitySeries()
    srt_duration = max((end for _, end, _ in srt_segments), default=None)
    probed_video_duration = probe_video_duration(flv_path)
    video_duration = probed_video_duration or srt_duration
    streamer_name = current_streamer_profile().report_name
    video_base = os.path.splitext(flv_path)[0]
    manual_timeline = prepare_optimized_manual_timeline(
        flv_path,
        video_base,
        srt_segments,
        peaks,
        video_duration,
        manual_timeline_path,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
        artifact_layout=artifact_layout,
    )
    if not manual_timeline.get("raw_entry_count"):
        raise ValueError("所选人工时间轴没有落在当前完整版录播范围内的记录")
    if progress_callback:
        progress_callback(
            f"完成! 原始 {manual_timeline['raw_entry_count']} 条 → "
            f"优化 {manual_timeline.get('optimized_entry_count', 0)} 个候选",
            100,
            100,
        )
    organized = reporting_service.organize_existing_artifacts(
        flv_path,
        output_dir=output_dir,
        artifact_dir=artifact_layout["artifact_dir"],
    )
    return {
        "video_path": flv_path,
        "source_srt_path": source_srt_path,
        "corrected_srt_path": corrected_srt_path,
        "srt_path": srt_path,
        "optimized_json_path": manual_timeline.get("optimized_json_path"),
        "optimized_md_path": manual_timeline.get("optimized_md_path"),
        "warning": manual_timeline.get("optimization_warning"),
        "manual_timeline": timeline_analysis.manual_timeline_summary(manual_timeline),
        "artifact_dir": artifact_layout["artifact_dir"],
        "overview_path": organized["overview_path"],
    }


def _scaled_progress_callback(progress_callback, start_step, end_step):
    """把子任务百分比映射到完整流水线的固定阶段区间。"""
    if not progress_callback:
        return None
    start_step = int(start_step)
    end_step = max(start_step, int(end_step))

    def report(message, step, total):
        try:
            ratio = float(step) / max(1.0, float(total))
        except (TypeError, ValueError):
            ratio = 0.0
        ratio = min(1.0, max(0.0, ratio))
        mapped = start_step + int(round((end_step - start_step) * ratio))
        progress_callback(message, mapped, 100)

    return report


def _monotonic_progress_callback(progress_callback):
    """并发阶段可乱序完成，但单次分析任务的百分比不得倒退。"""
    if not progress_callback:
        return None
    lock = threading.Lock()
    highest_step = 0

    def report(message, step, total):
        nonlocal highest_step
        try:
            normalised = int(round(float(step) / max(1.0, float(total)) * 100))
        except (TypeError, ValueError):
            normalised = highest_step
        normalised = min(100, max(0, normalised))
        with lock:
            highest_step = max(highest_step, normalised)
            progress_callback(message, highest_step, 100)

    return report


def run_pipeline(
        flv_path, ass_path=None, progress_callback=None, manual_timeline_path=None,
        optimized_timeline_path=None, output_dir=None, artifact_dir=None,
        streamer_profile_id="auto"):
    """在隔离的主播配置上下文中执行完整分析流水线。"""
    flv_path = slicing_service.validate_video_path(flv_path)
    with streamer_profile_context(streamer_profile_id, flv_path) as profile:
        result = run_pipeline_impl(
            flv_path,
            ass_path=ass_path,
            progress_callback=progress_callback,
            manual_timeline_path=manual_timeline_path,
            optimized_timeline_path=optimized_timeline_path,
            output_dir=output_dir,
            artifact_dir=artifact_dir,
        )
        result["streamer_profile_id"] = profile.id
        return result


def run_pipeline_impl(
        flv_path, ass_path=None, progress_callback=None, manual_timeline_path=None,
        optimized_timeline_path=None, output_dir=None, artifact_dir=None):
    """
    完整流水线：SRT → 弹幕 → LLM分析 → 报告 + 切片标记

    返回: {
        "report": str (Markdown),
        "clip_marks": [{"start": s, "end": s, "title": str}, ...],
        "json_path": str,
        "md_path": str,
    }
    """
    progress_callback = _monotonic_progress_callback(progress_callback)
    flv_path = os.path.abspath(flv_path)
    video_name = os.path.basename(flv_path)
    base = os.path.splitext(flv_path)[0]
    if output_dir is None and artifact_dir is None:
        output_dir = os.path.dirname(flv_path)
    artifact_layout = reporting_service.artifact_bundle_layout(
        flv_path,
        output_dir=output_dir,
        artifact_dir=artifact_dir,
    )
    os.makedirs(artifact_layout["data_dir"], exist_ok=True)
    os.makedirs(artifact_layout["unified_queue_dir"], exist_ok=True)
    streamer_profile = current_streamer_profile()
    streamer_name = streamer_profile.canonical_name
    streamer_display_name = streamer_profile.report_name
    unified_queue_json_path = artifact_layout["unified_queue_json_path"]
    unified_queue_md_path = artifact_layout["unified_queue_md_path"]
    prepared_subtitles = prepare_pipeline_subtitles(
        flv_path,
        artifact_layout,
        _funasr_checkpoint_path(flv_path),
        progress_callback=progress_callback,
        ensure_progress_callback=_scaled_progress_callback(
            progress_callback, 0, 14
        ),
        seed_artifact_from_legacy=_seed_artifact_from_legacy,
        ensure_srt=ensure_srt,
        export_corrected_srt=export_corrected_srt,
    )
    source_srt_path = prepared_subtitles["source_srt_path"]
    corrected_srt_path = prepared_subtitles["corrected_srt_path"]
    srt_path = prepared_subtitles["srt_path"]

    analysis_preparation = prepare_pipeline_analysis(
        flv_path,
        ass_path,
        srt_path,
        progress_callback=progress_callback,
        analyze_danmaku=analyze_danmaku,
        empty_danmaku_series=DanmakuDensitySeries,
        average_danmaku_density=_average_danmaku_density,
        high_energy_danmaku_peaks=_high_energy_danmaku_peaks,
        parse_srt_text=parse_srt_text,
        chunk_srt=chunk_srt,
        probe_video_duration=probe_video_duration,
    )
    peaks = analysis_preparation["peaks"]
    avg_den = analysis_preparation["avg_den"]
    peak_info = analysis_preparation["peak_info"]
    segs = analysis_preparation["segs"]
    chunks = analysis_preparation["chunks"]
    probed_video_duration = analysis_preparation["probed_video_duration"]
    video_duration = analysis_preparation["video_duration"]
    manual_preparation = prepare_pipeline_manual_timeline(
        flv_path,
        base,
        segs,
        peaks,
        video_duration,
        manual_timeline_path,
        optimized_timeline_path,
        streamer_display_name,
        artifact_layout,
        progress_callback=progress_callback,
        copy_artifact_file=_copy_artifact_file,
        load_optimized_timeline_artifact=load_optimized_timeline_artifact,
        prepare_optimized_manual_timeline=prepare_optimized_manual_timeline,
    )
    manual_timeline = manual_preparation["manual_timeline"]
    manual_entries = manual_preparation["manual_entries"]
    optimization_warning = manual_preparation["optimization_warning"]
    # Step 4: 首轮只分析字幕和弹幕，避免人工措辞锚定标题与语义边界。
    topic_analysis_checkpoint_path = artifact_layout[
        "topic_analysis_checkpoint_path"
    ]
    accepted_topics, failed_chunks, api_precheck_warning = (
        analyze_pipeline_llm_chunks(
            chunks,
            streamer_display_name,
            topic_analysis_checkpoint_path,
            base + "_topic_analysis_checkpoint.json",
            progress_callback=progress_callback,
            seed_artifact_from_legacy=_seed_artifact_from_legacy,
            analyze_topic_chunks=topic_analysis.analyze_topic_chunks,
        )
    )
    if optimization_warning:
        api_precheck_warning = "；".join(
            item for item in (optimization_warning, api_precheck_warning) if item
        )

    clip_review_checkpoint_path = artifact_layout["clip_review_checkpoint_path"]
    review_preparation = review_pipeline_candidates(
        accepted_topics,
        manual_entries,
        streamer_display_name,
        segs,
        peaks=peaks,
        avg_den=avg_den,
        clip_review_checkpoint_path=clip_review_checkpoint_path,
        legacy_clip_review_checkpoint_path=base + "_clip_review_checkpoint.json",
        api_precheck_warning=api_precheck_warning,
        progress_callback=progress_callback,
        merge_manual_timeline_topics=(
            manual_candidates.merge_manual_timeline_topics
        ),
        validate_unmatched_manual_topics=(
            manual_review.validate_unmatched_manual_topics
        ),
        clean_topics_for_report=report_cleanup.clean_topics_for_report,
        analysis_topics_snapshot=_analysis_topics_snapshot,
        seed_artifact_from_legacy=_seed_artifact_from_legacy,
        write_clip_review_checkpoint=checkpoint_store.write_clip_review_checkpoint,
        apply_danmaku_slice_decisions=(
            slice_decisions.apply_danmaku_slice_decisions
        ),
        review_peak_selected_topics=clip_review.review_peak_selected_topics,
    )
    accepted_topics = review_preparation["accepted_topics"]
    analysis_topics = review_preparation["analysis_topics"]
    api_precheck_warning = review_preparation["api_precheck_warning"]
    clip_review_warning = review_preparation["clip_review_warning"]
    title_preparation = review_pipeline_publish_titles(
        accepted_topics,
        streamer_display_name,
        clip_review_checkpoint_path,
        api_precheck_warning=api_precheck_warning,
        progress_callback=progress_callback,
        review_selected_publish_titles=_review_selected_publish_titles,
        write_clip_review_checkpoint=checkpoint_store.write_clip_review_checkpoint,
        clean_topics_for_report=report_cleanup.clean_topics_for_report,
    )
    accepted_topics = title_preparation["accepted_topics"]
    api_precheck_warning = title_preparation["api_precheck_warning"]
    # 复核旧产物时 analysis_topics 可能已带上一轮生成的收播片；收播片由
    # 当前字幕和真实视频时长重新判定，避免报告和队列出现重复的系列任务。
    accepted_topics = [
        topic for topic in accepted_topics
        if topic.get("clip_type") != "stream_outro"
    ]
    raw_clip_marks = slice_decisions.clip_marks_from_topics(accepted_topics)
    candidate_review_audit = _build_clip_candidate_review_audit(accepted_topics)
    candidate_review_audit_path = artifact_layout["candidate_review_audit_path"]
    _write_artifact_json(candidate_review_audit_path, candidate_review_audit)
    srt_segments_for_context = parse_srt_segments(srt_path)
    outro_mark = outro_analysis._detect_stream_outro_clip(
        srt_segments_for_context,
        probed_video_duration,
        streamer_profile=streamer_profile,
    )
    if outro_mark:
        accepted_topics.append(outro_analysis._outro_topic_from_mark(outro_mark))
        raw_clip_marks.append(outro_mark)
    clip_marks = _expand_clip_marks_with_context(
        raw_clip_marks,
        srt_segments=srt_segments_for_context,
        video_duration=video_duration or _srt_video_duration(srt_segments_for_context),
    )
    reporting_service.synchronise_selected_topic_ranges(accepted_topics, clip_marks)
    analysis_topics = _analysis_topics_snapshot(accepted_topics)
    if progress_callback:
        progress_callback("Step 5/5: 生成报告...", 97, 100)
    report = reporting_service.build_timeline_report(
        video_name, peak_info, accepted_topics,
        failed_chunks=failed_chunks, api_warning=api_precheck_warning,
        streamer_name=streamer_display_name,
        group_by_hour=True,
        manual_timeline=manual_timeline,
        clip_marks=clip_marks,
        corrected_srt_path=corrected_srt_path,
        unified_queue_md_path=unified_queue_md_path,
        report_dir=artifact_layout["artifact_dir"],
    )

    # 保存
    md_path = artifact_layout["report_path"]
    json_path = artifact_layout["clip_marks_path"]
    task_manifest_json_path = artifact_layout["task_manifest_json_path"]
    task_manifest_md_path = artifact_layout["task_manifest_md_path"]
    clip_review_completed_at = datetime.now().isoformat(timespec="seconds")

    _write_artifact_text(md_path, report)
    _write_artifact_json(
        json_path,
        {
            "video": video_name,
            "streamer_profile_id": streamer_profile.id,
            "streamer_name": streamer_name,
            "streamer_display_name": streamer_display_name,
            "artifact_layout_version": ARTIFACT_LAYOUT_VERSION,
            "artifact_dir": artifact_layout["artifact_dir"],
            "overview_path": artifact_layout["overview_path"],
            "analysis_report_path": md_path,
            "model_policy": {
                "topic_analysis": LLM_ANALYSIS_MODEL,
                "manual_timeline_review": LLM_MODEL,
                "clip_candidate_review": LLM_MODEL,
            },
            "source_srt_path": source_srt_path,
            "corrected_srt_path": corrected_srt_path,
            "task_manifest_json_path": task_manifest_json_path,
            "task_manifest_md_path": task_manifest_md_path,
            "unified_queue_json_path": unified_queue_json_path,
            "unified_queue_md_path": unified_queue_md_path,
            "clip_review_checkpoint_path": clip_review_checkpoint_path,
            "candidate_review_audit_path": candidate_review_audit_path,
            "topic_analysis_checkpoint_path": topic_analysis_checkpoint_path,
            "time_basis": "video_elapsed_seconds",
            "time_basis_note": "start/end 均为视频内秒数，不是真实钟点；topic_start/topic_end 为原话题范围，start/end 为含前后文的实际切片范围。",
            "expanded_with_context": True,
            "context_policy": {
                "pre_context_sec": TOPIC_PRE_CONTEXT_SEC,
                "post_context_sec": TOPIC_POST_CONTEXT_SEC,
                "min_clip_sec": TOPIC_MIN_CLIP_SEC,
                "max_clip_sec": TOPIC_MAX_CLIP_SEC,
                "required_context_overflow_sec": TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC,
            },
            "danmaku_selection_policy": {
                "average_density": round(avg_den, 3),
                "density_threshold": round(_danmaku_clip_threshold(peaks, avg_den), 3),
                "local_peak_radius_sec": CLIP_LOCAL_PEAK_RADIUS_SEC,
                "max_clips_per_hour": None,
                "fixed_hourly_quota": False,
                "min_editorial_interest_score": CLIP_MIN_INTEREST_SCORE,
                "manual_star_can_force_slice": False,
                "manual_star_review_min_stars": CLIP_MANUAL_REVIEW_MIN_STARS,
                "semantic_review_can_keep_peak_moved_focus": True,
                "independent_subtitle_review_required": True,
            },
            "api_precheck_warning": api_precheck_warning,
            "clip_review_warning": clip_review_warning,
            "clip_review_completed_at": clip_review_completed_at,
            "failed_chunks": failed_chunks,
            "manual_timeline": timeline_analysis.manual_timeline_summary(manual_timeline),
            "analysis_topics": analysis_topics,
            "clip_marks": clip_marks,
        },
    )

    refinement_manifest = reporting_service.build_refinement_manifest(
        flv_path,
        source_srt_path,
        corrected_srt_path,
        md_path,
        json_path,
        clip_marks,
        task_manifest_json_path,
        task_manifest_md_path,
    )
    refinement_manifest["unified_queue_json_path"] = unified_queue_json_path
    refinement_manifest["unified_queue_md_path"] = unified_queue_md_path
    refinement_manifest["artifact_layout_version"] = ARTIFACT_LAYOUT_VERSION
    refinement_manifest["artifact_dir"] = artifact_layout["artifact_dir"]
    refinement_manifest["overview_path"] = artifact_layout["overview_path"]
    reporting_service.write_refinement_manifest_files(refinement_manifest)
    unified_queue_warning = None
    try:
        reporting_service.upsert_unified_refinement_queue(
            refinement_manifest,
            queue_json_path=unified_queue_json_path,
            queue_md_path=unified_queue_md_path,
        )
    except (OSError, ValueError, TypeError) as e:
        unified_queue_warning = f"精调总清单更新失败: {e}"
        if progress_callback:
            progress_callback(unified_queue_warning, 99, 100)

    _write_completed_clip_review_checkpoint(
        clip_review_checkpoint_path,
        accepted_topics,
        warning=clip_review_warning,
        source="pipeline",
        completed_at=clip_review_completed_at,
    )

    organized = reporting_service.organize_existing_artifacts(
        flv_path,
        output_dir=artifact_layout["output_root"],
        json_path=json_path,
        report_path=md_path,
        slice_dir=artifact_layout["slice_dir"],
        artifact_dir=artifact_layout["artifact_dir"],
    )

    if progress_callback:
        progress_callback(
            f"完成! {len(clip_marks)} 个可切片段 → {organized['overview_path']}",
            100, 100
        )

    return {
        "report": report,
        "topic_count": len(accepted_topics),
        "clip_marks": clip_marks,
        "json_path": json_path,
        "md_path": md_path,
        "srt_path": srt_path,
        "source_srt_path": source_srt_path,
        "corrected_srt_path": corrected_srt_path,
        "task_manifest_json_path": task_manifest_json_path,
        "task_manifest_md_path": task_manifest_md_path,
        "artifact_dir": artifact_layout["artifact_dir"],
        "overview_path": organized["overview_path"],
        "slice_dir": artifact_layout["slice_dir"],
        "unified_queue_json_path": unified_queue_json_path,
        "unified_queue_md_path": unified_queue_md_path,
        "unified_queue_warning": unified_queue_warning,
        "topic_analysis_checkpoint_path": topic_analysis_checkpoint_path,
        "clip_review_checkpoint_path": clip_review_checkpoint_path,
        "candidate_review_audit_path": candidate_review_audit_path,
        "failed_chunks": failed_chunks,
        "api_precheck_warning": api_precheck_warning,
        "manual_timeline": timeline_analysis.manual_timeline_summary(manual_timeline),
    }


def _warning_without_previous_clip_review(data):
    """保留首轮/人工时间轴警告，移除上一次候选复核失败说明。"""
    warning = str(data.get("api_precheck_warning") or "").strip()
    clip_warning = str(data.get("clip_review_warning") or "").strip()
    if clip_warning and clip_warning in warning:
        warning = warning.replace(clip_warning, "").strip("； ")
    marker_index = warning.find("高能切片候选")
    if marker_index >= 0:
        warning = warning[:marker_index].strip("； ")
    return warning or None


def retry_clip_review_from_artifacts(
        flv_path, ass_path=None, json_path=None, report_path=None,
        progress_callback=None, output_dir=None, artifact_dir=None,
        streamer_profile_id="auto"):
    """在隔离的主播配置上下文中只重做切片候选复核。"""
    flv_path = slicing_service.validate_video_path(flv_path)
    with streamer_profile_context(streamer_profile_id, flv_path) as profile:
        result = retry_clip_review_from_artifacts_impl(
            flv_path,
            ass_path=ass_path,
            json_path=json_path,
            report_path=report_path,
            progress_callback=progress_callback,
            output_dir=output_dir,
            artifact_dir=artifact_dir,
        )
        result["streamer_profile_id"] = profile.id
        return result


def retry_clip_review_from_artifacts_impl(
        flv_path, ass_path=None, json_path=None, report_path=None,
        progress_callback=None, output_dir=None, artifact_dir=None):
    """复用已有逐话题报告，只重做弹幕候选筛选、字幕复核和最终产物。"""
    streamer_profile = current_streamer_profile()
    flv_path = os.path.abspath(flv_path)
    base, _ = os.path.splitext(flv_path)
    if output_dir is None and artifact_dir is None:
        output_dir = os.path.dirname(flv_path)
    artifact_layout = reporting_service.artifact_bundle_layout(
        flv_path,
        output_dir=output_dir,
        artifact_dir=artifact_dir,
    )
    os.makedirs(artifact_layout["data_dir"], exist_ok=True)
    if json_path is None and not os.path.isfile(artifact_layout["clip_marks_path"]):
        legacy_json_path = base + "_clip_marks.json"
        if os.path.isfile(legacy_json_path):
            reporting_service.organize_existing_artifacts(
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
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("切片标记 JSON 根节点必须是对象")

    rebuilt_manual_timeline = _manual_timeline_for_rebuilt_report(
        data.get("manual_timeline"),
        flv_path,
    )
    rebuilt_manual_entries = rebuilt_manual_timeline.get("entries") or []
    recovered_topics = data.get("analysis_topics")
    if not isinstance(recovered_topics, list) or not recovered_topics:
        recovered_topics = reporting_service.parse_generated_topic_report(report_path)
    baseline_topics = report_cleanup.clean_topics_for_report(
        _analysis_topics_snapshot(recovered_topics)
    )
    if rebuilt_manual_entries:
        manual_candidates.merge_manual_timeline_topics(
            baseline_topics,
            rebuilt_manual_entries,
        )
        baseline_topics = report_cleanup.clean_topics_for_report(baseline_topics)
    analysis_topics = _analysis_topics_snapshot(baseline_topics)

    clip_review_checkpoint_path = (
        data.get("clip_review_checkpoint_path")
        or artifact_layout["clip_review_checkpoint_path"]
    )
    _seed_artifact_from_legacy(
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
            with open(clip_review_checkpoint_path, encoding="utf-8") as f:
                checkpoint = json.load(f)
            if not _clip_review_checkpoint_matches_policy(checkpoint):
                checkpoint_policy_stale = True
                checkpoint_topics = checkpoint.get("topics")
            else:
                checkpoint_topics = checkpoint.get("topics")
            resume_stages = {"reviewing", "resuming", "completed_with_warning"}
            if isinstance(checkpoint_topics, list) and checkpoint_topics:
                if checkpoint_policy_stale:
                    # 旧策略的已通过项可能已被收缩到峰值之外；把它们重新
                    # 送入本版规则复核，同时把最新优化时间轴重新挂回话题。
                    accepted_topics = report_cleanup.clean_topics_for_report(
                        checkpoint_topics
                    )
                    if rebuilt_manual_entries:
                        manual_candidates.merge_manual_timeline_topics(
                            accepted_topics,
                            rebuilt_manual_entries,
                        )
                        accepted_topics = report_cleanup.clean_topics_for_report(
                            accepted_topics
                        )
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
                            and int(topic.get("end", 0)) - int(topic.get("start", 0))
                            > TOPIC_REVIEW_FOCUS_MAX_SEC
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
                        accepted_topics = report_cleanup.clean_topics_for_report(
                            checkpoint_topics
                        )
                        resume_review = True
                    elif _clip_review_checkpoint_is_complete(
                            checkpoint, checkpoint_topics):
                        accepted_topics = report_cleanup.clean_topics_for_report(
                            checkpoint_topics
                        )
                        reuse_completed_review = True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            resume_review = False
    if not accepted_topics:
        raise ValueError("已有产物中没有可用于复核的话题")

    corrected_srt_path = data.get("corrected_srt_path")
    source_srt_path = data.get("source_srt_path") or base + ".srt"
    srt_path = (
        corrected_srt_path
        if corrected_srt_path and os.path.isfile(corrected_srt_path)
        else source_srt_path
    )
    if not srt_path or not os.path.isfile(srt_path):
        raise FileNotFoundError(f"复核字幕不存在: {srt_path or '未记录'}")
    srt_segments = parse_srt_segments(srt_path)
    if not srt_segments:
        raise ValueError("复核字幕中没有有效句段")

    if ass_path is None:
        ass_candidate = base + ".ass"
        ass_path = ass_candidate if os.path.isfile(ass_candidate) else None
    peaks = analyze_danmaku(ass_path) if ass_path else DanmakuDensitySeries()
    avg_den = _average_danmaku_density(peaks)
    high_energy_peaks = _high_energy_danmaku_peaks(peaks, avg_den)
    peak_info = (
        f"弹幕密度 {len(peaks)} 个滑动窗口, "
        f"独立高能峰值 {len(high_energy_peaks)} 个, "
        f"全场平均密度 {avg_den:.0f}条/分钟"
        if peaks else "无弹幕数据"
    )
    if progress_callback:
        pending_count = sum(
            1 for topic in accepted_topics
            if topic.get("can_slice")
            and topic.get("clip_review_rejection") == "等待独立字幕复核"
        )
        if resume_review:
            resume_note = f"，从检查点续跑 {pending_count} 项"
        elif reuse_completed_review:
            resume_note = "，复用已完成的独立字幕复核"
        elif checkpoint_policy_stale:
            resume_note = "，检测到旧版复核规则，使用当前规则重新复核"
        else:
            resume_note = ""
        progress_callback(
            f"已恢复 {len(accepted_topics)} 个话题，仅重做高能候选复核{resume_note}",
            90,
            100,
        )

    checkpoint_store.write_clip_review_checkpoint(
        clip_review_checkpoint_path,
        accepted_topics if (resume_review or reuse_completed_review) else analysis_topics,
        stage=(
            "resuming" if resume_review
            else "rebuilding" if reuse_completed_review
            else "ready"
        ),
        source="artifact_retry",
    )
    if not resume_review and not reuse_completed_review:
        slice_decisions.apply_danmaku_slice_decisions(
            accepted_topics,
            peaks,
            avg_den,
        )
        for topic in accepted_topics:
            key = (
                int(topic.get("start", 0) or 0),
                int(topic.get("end", 0) or 0),
                str(topic.get("title", "")),
            )
            if key in stale_review_keys:
                slice_decisions.append_clip_candidate_source(topic, "语义复核")
    if reuse_completed_review:
        clip_review_warning = None
    else:
        clip_review_warning = clip_review.review_peak_selected_topics(
            accepted_topics,
            srt_segments=srt_segments,
            peaks=peaks,
            streamer_name=streamer_profile.report_name,
            progress_callback=progress_callback,
            checkpoint_callback=lambda current, pending, round_label, batch_index, total_batches: (
                checkpoint_store.write_clip_review_checkpoint(
                    clip_review_checkpoint_path,
                    current,
                    stage="reviewing",
                    source="artifact_retry",
                    pending_count=len(pending),
                    round=round_label,
                    batch_index=batch_index,
                    total_batches=total_batches,
                )
            ),
            resume=resume_review,
        )
    accepted_topics = report_cleanup.clean_topics_for_report(accepted_topics)
    slice_decisions.apply_danmaku_slice_decisions(
        accepted_topics,
        peaks,
        avg_den,
        require_clip_review=True,
    )
    title_review_warning = _review_selected_publish_titles(
        accepted_topics,
        streamer_name=streamer_profile.report_name,
        progress_callback=progress_callback,
        checkpoint_callback=lambda current, batch_index, total_batches: (
            checkpoint_store.write_clip_review_checkpoint(
                clip_review_checkpoint_path,
                current,
                stage="title_reviewing",
                source="artifact_retry",
                batch_index=batch_index,
                total_batches=total_batches,
            )
        ),
    )
    if title_review_warning:
        clip_review_warning = "；".join(
            item for item in (clip_review_warning, title_review_warning) if item
        )
    accepted_topics = report_cleanup.clean_topics_for_report(accepted_topics)
    accepted_topics = [
        topic for topic in accepted_topics
        if topic.get("clip_type") != "stream_outro"
    ]
    raw_clip_marks = slice_decisions.clip_marks_from_topics(accepted_topics)
    candidate_review_audit = _build_clip_candidate_review_audit(accepted_topics)
    candidate_review_audit_path = artifact_layout["candidate_review_audit_path"]
    _write_artifact_json(candidate_review_audit_path, candidate_review_audit)
    probed_video_duration = probe_video_duration(flv_path)
    video_duration = probed_video_duration or _srt_video_duration(srt_segments)
    outro_mark = outro_analysis._detect_stream_outro_clip(
        srt_segments,
        probed_video_duration,
        streamer_profile=streamer_profile,
    )
    if outro_mark:
        accepted_topics.append(outro_analysis._outro_topic_from_mark(outro_mark))
        raw_clip_marks.append(outro_mark)
    clip_marks = _expand_clip_marks_with_context(
        raw_clip_marks,
        srt_segments=srt_segments,
        video_duration=video_duration,
    )
    reporting_service.synchronise_selected_topic_ranges(accepted_topics, clip_marks)

    base_warning = _warning_without_previous_clip_review(data)
    api_warning = "；".join(
        item for item in (base_warning, clip_review_warning) if item
    ) or None
    manual_timeline = rebuilt_manual_timeline
    unified_queue_json_path = data.get("unified_queue_json_path")
    unified_queue_md_path = data.get("unified_queue_md_path")
    if not unified_queue_json_path or not unified_queue_md_path:
        unified_queue_json_path = artifact_layout["unified_queue_json_path"]
        unified_queue_md_path = artifact_layout["unified_queue_md_path"]
    video_name = os.path.basename(flv_path)
    streamer_name = streamer_profile.canonical_name
    streamer_display_name = streamer_profile.report_name
    report = reporting_service.build_timeline_report(
        video_name,
        peak_info,
        accepted_topics,
        failed_chunks=data.get("failed_chunks") or [],
        api_warning=api_warning,
        streamer_name=streamer_display_name,
        group_by_hour=True,
        manual_timeline=manual_timeline,
        clip_marks=clip_marks,
        corrected_srt_path=corrected_srt_path,
        unified_queue_md_path=unified_queue_md_path,
        report_dir=artifact_layout["artifact_dir"],
    )
    analysis_topics = _analysis_topics_snapshot(accepted_topics)

    clip_review_completed_at = datetime.now().isoformat(timespec="seconds")
    data.update({
        "video": video_name,
        "streamer_profile_id": streamer_profile.id,
        "streamer_name": streamer_name,
        "streamer_display_name": streamer_display_name,
        "artifact_layout_version": ARTIFACT_LAYOUT_VERSION,
        "artifact_dir": artifact_layout["artifact_dir"],
        "overview_path": artifact_layout["overview_path"],
        "analysis_report_path": report_path,
        "source_srt_path": source_srt_path,
        "corrected_srt_path": corrected_srt_path,
        "unified_queue_json_path": unified_queue_json_path,
        "unified_queue_md_path": unified_queue_md_path,
        "clip_review_checkpoint_path": clip_review_checkpoint_path,
        "candidate_review_audit_path": candidate_review_audit_path,
        "expanded_with_context": True,
        "context_policy": {
            "pre_context_sec": TOPIC_PRE_CONTEXT_SEC,
            "post_context_sec": TOPIC_POST_CONTEXT_SEC,
            "min_clip_sec": TOPIC_MIN_CLIP_SEC,
            "max_clip_sec": TOPIC_MAX_CLIP_SEC,
            "required_context_overflow_sec": TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC,
        },
        "danmaku_selection_policy": {
            "average_density": round(avg_den, 3),
            "density_threshold": round(_danmaku_clip_threshold(peaks, avg_den), 3),
            "local_peak_radius_sec": CLIP_LOCAL_PEAK_RADIUS_SEC,
            "max_clips_per_hour": None,
            "fixed_hourly_quota": False,
            "min_editorial_interest_score": CLIP_MIN_INTEREST_SCORE,
            "manual_star_can_force_slice": False,
            "manual_star_review_min_stars": CLIP_MANUAL_REVIEW_MIN_STARS,
            "semantic_review_can_keep_peak_moved_focus": True,
            "independent_subtitle_review_required": True,
        },
        "api_precheck_warning": api_warning,
        "clip_review_warning": clip_review_warning,
        "manual_timeline": timeline_analysis.manual_timeline_summary(manual_timeline),
        "analysis_topics": analysis_topics,
        "clip_marks": clip_marks,
        "clip_review_completed_at": clip_review_completed_at,
    })

    _write_artifact_text(report_path, report)
    _write_artifact_json(json_path, data)

    task_manifest_json_path = data.get("task_manifest_json_path")
    task_manifest_md_path = data.get("task_manifest_md_path")
    if not task_manifest_json_path or not task_manifest_md_path:
        task_manifest_json_path = artifact_layout["task_manifest_json_path"]
        task_manifest_md_path = artifact_layout["task_manifest_md_path"]
    refinement_manifest = reporting_service.build_refinement_manifest(
        flv_path,
        source_srt_path,
        corrected_srt_path,
        report_path,
        json_path,
        clip_marks,
        task_manifest_json_path,
        task_manifest_md_path,
    )
    refinement_manifest["unified_queue_json_path"] = unified_queue_json_path
    refinement_manifest["unified_queue_md_path"] = unified_queue_md_path
    refinement_manifest["artifact_layout_version"] = ARTIFACT_LAYOUT_VERSION
    refinement_manifest["artifact_dir"] = artifact_layout["artifact_dir"]
    refinement_manifest["overview_path"] = artifact_layout["overview_path"]
    reporting_service.write_refinement_manifest_files(refinement_manifest)
    try:
        reporting_service.upsert_unified_refinement_queue(
            refinement_manifest,
            queue_json_path=unified_queue_json_path,
            queue_md_path=unified_queue_md_path,
        )
    except (OSError, ValueError, TypeError) as exc:
        refinement_manifest["unified_queue_warning"] = f"精调总清单更新失败: {exc}"
        reporting_service.write_refinement_manifest_files(refinement_manifest)

    _write_completed_clip_review_checkpoint(
        clip_review_checkpoint_path,
        accepted_topics,
        warning=clip_review_warning,
        source="artifact_retry",
        completed_at=clip_review_completed_at,
    )
    organized = reporting_service.organize_existing_artifacts(
        flv_path,
        output_dir=artifact_layout["output_root"],
        json_path=json_path,
        report_path=report_path,
        slice_dir=artifact_layout["slice_dir"],
        artifact_dir=artifact_layout["artifact_dir"],
    )
    if progress_callback:
        progress_callback(
            f"候选复核完成：{len(clip_marks)} 个可切片段 → {json_path}",
            100,
            100,
        )
    return {
        "report": report,
        "topic_count": len(accepted_topics),
        "clip_marks": clip_marks,
        "json_path": json_path,
        "md_path": report_path,
        "artifact_dir": artifact_layout["artifact_dir"],
        "overview_path": organized["overview_path"],
        "srt_path": srt_path,
        "failed_chunks": data.get("failed_chunks") or [],
        "api_precheck_warning": api_warning,
        "clip_review_warning": clip_review_warning,
        "candidate_review_audit_path": candidate_review_audit_path,
    }
