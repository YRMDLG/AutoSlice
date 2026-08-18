"""候选发现、证据整理与投稿价值复核的唯一实现。"""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from functools import partial

from autoslice.analysis import checkpoints as checkpoint_store
from autoslice.analysis import boundaries as boundary_analysis
from autoslice.analysis import clip_scoring
from autoslice.analysis import clip_policy
from autoslice.analysis import clip_review_candidates
from autoslice.analysis import clip_review_prompt
from autoslice.analysis import content_normalization
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis import evidence as candidate_evidence
from autoslice.analysis import llm_execution
from autoslice.analysis import manual_enrichment
from autoslice.analysis import response_parsing
from autoslice.analysis import timeline as timeline_analysis
from autoslice.analysis import titles as title_analysis
from autoslice import timecode
from autoslice.llm import transport as llm_gateway
from autoslice.llm.prompts import (
    ManualTopicPromptEvidence as _ManualTopicPromptEvidence,
    TopicAnalysisPromptEvidence as _TopicAnalysisPromptEvidence,
    build_manual_topic_enrichment_prompt as _render_manual_topic_enrichment_prompt,
    build_topic_analysis_prompt as _render_topic_analysis_prompt,
)
from autoslice.transcription import service as transcription_service
from autoslice.transcription import segments as transcription_segments
from autoslice.transcription import srt_io as transcription_srt_io
from autoslice.streamer_profiles import (
    current_streamer_profile,
    streamer_profile_context,
)


FACADE_EXPORTS = {
    'CHUNK_SEC': 'CHUNK_SEC',
    'LLMProviderUnavailableError': 'LLMProviderUnavailableError',
    'LLMStructuredOutputError': 'LLMStructuredOutputError',
    'LLM_ANALYSIS_MODEL': 'LLM_ANALYSIS_MODEL',
    'LLM_COMPACT_MAX_TOKENS': 'LLM_COMPACT_MAX_TOKENS',
    'LLM_COMPACT_TEXT_CHARS': 'LLM_COMPACT_TEXT_CHARS',
    'LLM_FULL_TEXT_CHARS': 'LLM_FULL_TEXT_CHARS',
    'LLM_MAX_TOKENS': 'LLM_MAX_TOKENS',
    'MAX_INITIAL_FAILED_CHUNKS': 'MAX_INITIAL_FAILED_CHUNKS',
    '_CIRCLED_NUMBERS': '_CIRCLED_NUMBERS',
    '_HEADING_RE': '_HEADING_RE',
    '_MANUAL_AI_PLACEHOLDER_PHRASES': '_MANUAL_AI_PLACEHOLDER_PHRASES',
    '_UNCUTTABLE_CONTENT_KEYWORDS': '_UNCUTTABLE_CONTENT_KEYWORDS',
    '_analyze_topic_chunks': 'analyze_topic_chunks',
    '_append_clip_candidate_source': '_append_clip_candidate_source',
    '_apply_danmaku_slice_decisions': '_apply_danmaku_slice_decisions',
    '_apply_reviewed_slice_decisions': '_apply_reviewed_slice_decisions',
    '_assign_reviewed_semantic_slice_window': '_assign_reviewed_semantic_slice_window',
    '_assign_topic_slice_window': '_assign_topic_slice_window',
    '_build_chunk_prompt': '_build_chunk_prompt',
    '_build_clip_candidate_review_prompt': '_build_clip_candidate_review_prompt',
    '_build_manual_topic_enrichment_prompt': '_build_manual_topic_enrichment_prompt',
    '_clean_topics_for_report': '_clean_topics_for_report',
    '_clip_marks_from_topics': '_clip_marks_from_topics',
    '_clip_review_candidate': '_clip_review_candidate',
    '_danmaku_topic_alignment': '_danmaku_topic_alignment',
    '_enrich_manual_topics_in_batches': 'enrich_manual_topics_in_batches',
    '_enrich_manual_topics_with_llm': 'enrich_manual_topics_with_llm',
    '_enriched_manual_topic_from_item': '_enriched_manual_topic_from_item',
    '_extract_json_payload': '_extract_json_payload',
    '_format_report_time': '_format_report_time',
    '_format_topic_block': '_format_topic_block',
    '_fresh_manual_topic_evidence': '_fresh_manual_topic_evidence',
    '_has_high_star_manual_evidence': '_has_high_star_manual_evidence',
    '_is_content_cuttable_topic': '_is_content_cuttable_topic',
    '_is_manual_ai_placeholder': '_is_manual_ai_placeholder',
    '_is_manual_merge_target': '_is_manual_merge_target',
    '_is_retryable_llm_error': '_is_retryable_llm_error',
    '_is_topic_in_chunk': '_is_topic_in_chunk',
    '_load_topic_analysis_checkpoint': '_load_topic_analysis_checkpoint',
    '_make_chunk': '_make_chunk',
    '_make_fallback_topic_from_chunk': '_make_fallback_topic_from_chunk',
    '_manual_entry_matches_topic': '_manual_entry_matches_topic',
    '_manual_entry_meaningfully_overlaps_topic': '_manual_entry_meaningfully_overlaps_topic',
    '_manual_evidence_line': '_manual_evidence_line',
    '_manual_review_anchor': '_manual_review_anchor',
    '_merge_manual_timeline_topics': 'merge_manual_timeline_topics',
    '_optimized_entry_semantic_text': '_optimized_entry_semantic_text',
    '_parse_json_topics_response': '_parse_json_topics_response',
    '_parse_llm_response': '_parse_llm_response',
    '_reconcile_topic_manual_evidence': '_reconcile_topic_manual_evidence',
    '_refresh_topic_danmaku_evidence': '_refresh_topic_danmaku_evidence',
    '_repair_short_topic_end': '_repair_short_topic_end',
    '_report_fact_lines': '_report_fact_lines',
    '_resolve_reviewed_report_overlaps': '_resolve_reviewed_report_overlaps',
    '_review_peak_selected_topics': '_review_peak_selected_topics',
    '_reviewed_topic_has_required_interest': '_reviewed_topic_has_required_interest',
    '_sanitize_optimized_manual_entry': '_sanitize_optimized_manual_entry',
    '_short_llm_error': '_short_llm_error',
    '_strip_code_fence': '_strip_code_fence',
    '_strip_prompt_time_labels': '_strip_prompt_time_labels',
    '_topic_analysis_prompt_fingerprint': '_topic_analysis_prompt_fingerprint',
    '_topic_index_label': '_topic_index_label',
    '_topic_peak_focus_window': '_topic_peak_focus_window',
    '_topic_semantic_text': '_topic_semantic_text',
    '_topics_from_manual_timeline': '_topics_from_manual_timeline',
    '_trim_report_topic_around_reviewed_topic': '_trim_report_topic_around_reviewed_topic',
    '_validate_unmatched_manual_topics': '_validate_unmatched_manual_topics',
    '_validated_ai_focus_range': '_validated_ai_focus_range',
    '_write_topic_analysis_checkpoint': '_write_topic_analysis_checkpoint',
    'chunk_srt': 'chunk_srt',
    'parse_srt_text': 'parse_srt_text',
}


_profile_identity_names = transcription_service.profile_identity_names


_profile_matches_streamer = transcription_service.profile_matches_streamer


SRT_ESTIMATED_CHARS_PER_SEC = transcription_segments.SRT_ESTIMATED_CHARS_PER_SEC


TOPIC_CONTEXT_GAP = transcription_segments.TOPIC_CONTEXT_GAP


_text_len_for_timing = transcription_segments.text_len_for_timing


_normalise_streamer_terms = transcription_segments.normalise_streamer_terms


_subtitle_text_size = transcription_segments.subtitle_text_size


_load_repaired_srt_segments = transcription_srt_io.load_repaired_srt_segments


DANMAKU_WINDOW = danmaku_analysis.DANMAKU_WINDOW


_topic_srt_summary_lines = candidate_evidence.topic_srt_summary_lines
_topic_danmaku_reference_lines = candidate_evidence.topic_danmaku_reference_lines
_topic_peak_candidates = candidate_evidence.topic_peak_candidates


_clip_review_candidate = clip_review_candidates.build_clip_review_candidate
_fresh_manual_topic_evidence = clip_review_candidates.fresh_manual_topic_evidence
_build_clip_candidate_review_prompt = (
    clip_review_prompt.build_clip_candidate_review_prompt
)


LLM_DEFAULT_CONCURRENCY = llm_execution.LLM_DEFAULT_CONCURRENCY
LLM_MAX_CONCURRENCY = llm_execution.LLM_MAX_CONCURRENCY
_configured_llm_concurrency = llm_execution.configured_llm_concurrency
_serialized_progress_callback = llm_execution.serialized_progress_callback


_NO_SLICE_HINTS = response_parsing.NO_SLICE_HINTS
_is_slice_marked = response_parsing.is_slice_marked
_json_can_slice = response_parsing.json_can_slice


_DANMAKU_META_KEYWORDS = content_normalization.DANMAKU_META_KEYWORDS
_FRAGMENT_BODY_LINES = content_normalization.FRAGMENT_BODY_LINES
_META_BODY_KEYWORDS = content_normalization.META_BODY_KEYWORDS
_UNSUPPORTED_AI_AUDIENCE_REACTION_RE = (
    content_normalization.UNSUPPORTED_AI_AUDIENCE_REACTION_RE
)
_clean_body_content = content_normalization.clean_body_content
_filter_unsupported_ai_points = content_normalization.filter_unsupported_ai_points
_is_meta_body_line = content_normalization.is_meta_body_line
_json_points_to_body = content_normalization.json_points_to_body
_normalise_body_line = content_normalization.normalise_body_line


_MANUAL_AI_PLACEHOLDER_PHRASES = (
    manual_enrichment.MANUAL_AI_PLACEHOLDER_PHRASES
)
_enriched_manual_topic_from_item = manual_enrichment.enrich_manual_topic_from_item
_is_manual_ai_placeholder = manual_enrichment.is_manual_ai_placeholder
_validated_ai_focus_range = manual_enrichment.validated_ai_focus_range


_clean_ass_danmaku_text = danmaku_analysis._clean_ass_danmaku_text


_is_generic_danmaku_reaction = danmaku_analysis._is_generic_danmaku_reaction


_danmaku_prompt_evidence = danmaku_analysis._danmaku_prompt_evidence


# 标题兼容别名：候选模块消费 titles 的唯一实现，不保留本地副本。
MAX_PUBLISH_TITLE_CHARS = title_analysis.MAX_PUBLISH_TITLE_CHARS
MAX_TOPIC_TITLE_CHARS = title_analysis.MAX_TOPIC_TITLE_CHARS
TITLE_STYLE_EXAMPLE_LIMIT = title_analysis.TITLE_STYLE_EXAMPLE_LIMIT
TITLE_STYLE_PROFILE_PATH = title_analysis.TITLE_STYLE_PROFILE_PATH
_GENERIC_PUBLISH_TITLES = title_analysis._GENERIC_PUBLISH_TITLES
_GENERIC_TOPIC_TITLES = title_analysis._GENERIC_TOPIC_TITLES
_LEADING_ACCOUNT_PREFIX_RE = title_analysis._LEADING_ACCOUNT_PREFIX_RE
_META_TITLE_KEYWORDS = title_analysis._META_TITLE_KEYWORDS
_PLACEHOLDER_TITLES = title_analysis._PLACEHOLDER_TITLES
_PUBLISH_TITLE_META_KEYWORDS = title_analysis._PUBLISH_TITLE_META_KEYWORDS
_SUCCESSFUL_RAIL_EVIDENCE_RE = title_analysis._SUCCESSFUL_RAIL_EVIDENCE_RE
_TITLE_STYLE_TAG_KEYWORDS = title_analysis._TITLE_STYLE_TAG_KEYWORDS
_active_streamer_aliases = title_analysis._active_streamer_aliases
_build_title_style_prompt = title_analysis._build_title_style_prompt
_clean_topic_title = title_analysis._clean_topic_title
_clip_candidate_danmaku_prompt_evidence = title_analysis._clip_candidate_danmaku_prompt_evidence
_clip_candidate_reference_publish_titles = title_analysis._clip_candidate_reference_publish_titles
_compact_topic_phrase = title_analysis._compact_topic_phrase
_derive_topic_title = title_analysis._derive_topic_title
_fallback_publish_title = title_analysis._fallback_publish_title
_fallback_title_from_text = title_analysis._fallback_title_from_text
_is_bad_topic_title = title_analysis._is_bad_topic_title
_is_generic_topic_title = title_analysis._is_generic_topic_title
_is_incomplete_ai_title = title_analysis._is_incomplete_ai_title
_is_placeholder_title = title_analysis._is_placeholder_title
_manual_title_from_text = title_analysis._manual_title_from_text
_normalise_obvious_report_terms = title_analysis._normalise_obvious_report_terms
_normalise_publish_title = title_analysis._normalise_publish_title
_normalise_title_hook = title_analysis._normalise_title_hook
_profile_formal_names = title_analysis._profile_formal_names
_prompt_context = title_analysis._prompt_context
_prompt_streamer_name = title_analysis._prompt_streamer_name
_publish_title_example = title_analysis._publish_title_example
_publish_title_instruction = title_analysis._publish_title_instruction
_publish_title_prefix = title_analysis._publish_title_prefix
_replace_streamer_role = title_analysis._replace_streamer_role
_sanitize_transport_claims = title_analysis._sanitize_transport_claims
_select_title_style_examples = title_analysis._select_title_style_examples
_specific_topic_phrase = title_analysis._specific_topic_phrase
_streamer_report_name = title_analysis._streamer_report_name
_streamer_role_pattern = title_analysis._streamer_role_pattern
_strip_body_prefix = title_analysis._strip_body_prefix
_strip_title_meta = title_analysis._strip_title_meta
_title_style_profile_path = title_analysis._title_style_profile_path
load_title_style_profile = title_analysis.load_title_style_profile


_average_danmaku_density = danmaku_analysis._average_danmaku_density


_high_energy_danmaku_peaks = danmaku_analysis._high_energy_danmaku_peaks


_danmaku_peak_features = danmaku_analysis._danmaku_peak_features


_reviewed_danmaku_ranking_score = danmaku_analysis._reviewed_danmaku_ranking_score


MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE = timeline_analysis.MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE


MANUAL_TIMELINE_TOPIC_POST_SEC = timeline_analysis.MANUAL_TIMELINE_TOPIC_POST_SEC


MANUAL_TIMELINE_TOPIC_PRE_SEC = timeline_analysis.MANUAL_TIMELINE_TOPIC_PRE_SEC


_manual_alignment_score = timeline_analysis._manual_alignment_score


_manual_text_supports_candidate = timeline_analysis._manual_text_supports_candidate


_parse_hms = timecode.parse_hms


fmt_time = timecode.format_elapsed


LLMStructuredOutputError = llm_gateway.LLMStructuredOutputError


LLMProviderUnavailableError = llm_gateway.LLMProviderUnavailableError


_short_llm_error = llm_gateway.short_llm_error


_is_retryable_llm_error = llm_gateway.is_retryable_llm_error


_extract_json_payload = llm_gateway.extract_json_payload


CHUNK_SEC = 600          # 每块 10 分钟：减少 API 调用，降低话题被硬切碎的概率


LLM_ANALYSIS_MODEL = (
    os.environ.get("AUTOSLICE_ANALYSIS_MODEL", "").strip()
    or "gpt-5.6-luna"
)


LLM_MAX_TOKENS = 16000


LLM_COMPACT_MAX_TOKENS = 12000


LLM_FULL_TEXT_CHARS = 8000


LLM_COMPACT_TEXT_CHARS = 2200


MAX_INITIAL_FAILED_CHUNKS = 3


TOPIC_ANALYSIS_CHECKPOINT_VERSION = checkpoint_store.TOPIC_ANALYSIS_CHECKPOINT_VERSION


CLIP_REVIEW_POLICY_VERSION = checkpoint_store.CLIP_REVIEW_POLICY_VERSION


CLIP_MIN_INTEREST_SCORE = clip_policy.CLIP_MIN_INTEREST_SCORE
CLIP_MANUAL_REVIEW_MIN_STARS = clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS
CLIP_REVIEW_BATCH_SIZE = clip_policy.CLIP_REVIEW_BATCH_SIZE
CLIP_REVIEW_RETRY_BATCH_SIZE = clip_policy.CLIP_REVIEW_RETRY_BATCH_SIZE
_parse_clip_interest_score = clip_scoring.parse_clip_interest_score
_parse_clip_star_bonus = clip_scoring.parse_clip_star_bonus
_clip_star_bonus_cap = clip_scoring.clip_star_bonus_cap
_clip_manual_star_count = clip_scoring.clip_manual_star_count
_clip_interest_reason = clip_scoring.clip_interest_reason
_build_clip_candidate_review_audit = (
    clip_scoring.build_clip_candidate_review_audit
)
TOPIC_PRE_CONTEXT_SEC = clip_policy.TOPIC_PRE_CONTEXT_SEC
TOPIC_POST_CONTEXT_SEC = clip_policy.TOPIC_POST_CONTEXT_SEC
TOPIC_MIN_CLIP_SEC = clip_policy.TOPIC_MIN_CLIP_SEC
TOPIC_MAX_CLIP_SEC = clip_policy.TOPIC_MAX_CLIP_SEC
TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC = clip_policy.TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
TOPIC_REVIEW_FOCUS_MAX_SEC = clip_policy.TOPIC_REVIEW_FOCUS_MAX_SEC
TOPIC_DIRECT_SLICE_MAX_SEC = clip_policy.TOPIC_DIRECT_SLICE_MAX_SEC
TOPIC_FOCUS_PRE_SEC = clip_policy.TOPIC_FOCUS_PRE_SEC
TOPIC_FOCUS_POST_SEC = clip_policy.TOPIC_FOCUS_POST_SEC
TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC = clip_policy.TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC
TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC = clip_policy.TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC
TOPIC_AI_FOCUS_PRE_CONTEXT_SEC = clip_policy.TOPIC_AI_FOCUS_PRE_CONTEXT_SEC
TOPIC_AI_FOCUS_POST_CONTEXT_SEC = clip_policy.TOPIC_AI_FOCUS_POST_CONTEXT_SEC
TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC = clip_policy.TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC
TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC = clip_policy.TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC
TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC = (
    clip_policy.TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC
)
TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC = (
    clip_policy.TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
)
TOPIC_LEAD_IN_RECOVERY_MIN_SEC = clip_policy.TOPIC_LEAD_IN_RECOVERY_MIN_SEC
TOPIC_LEAD_IN_LOOKBACK_SEC = clip_policy.TOPIC_LEAD_IN_LOOKBACK_SEC
TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC = clip_policy.TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC
TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC = clip_policy.TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC
TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC = clip_policy.TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC
TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE = clip_policy.TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
TOPIC_HARD_TRANSITION_GAP_SEC = clip_policy.TOPIC_HARD_TRANSITION_GAP_SEC
TOPIC_RELEVANT_CONTINUATION_GAP_SEC = clip_policy.TOPIC_RELEVANT_CONTINUATION_GAP_SEC
TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC = (
    clip_policy.TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC
)
TOPIC_REFERENCE_END_TOLERANCE_SEC = clip_policy.TOPIC_REFERENCE_END_TOLERANCE_SEC
SC_CONTEXT_LOOKBACK_SEC = clip_policy.SC_CONTEXT_LOOKBACK_SEC
SC_FALLBACK_GIFT_LOOKBACK_SEC = clip_policy.SC_FALLBACK_GIFT_LOOKBACK_SEC
TOPIC_MIN_REPORT_SEC = clip_policy.TOPIC_MIN_REPORT_SEC
TOPIC_MAX_REPAIRED_REPORT_SEC = clip_policy.TOPIC_MAX_REPAIRED_REPORT_SEC
SC_TRIGGER_KEYWORDS = clip_policy.SC_TRIGGER_KEYWORDS
THANKS_TRIGGER_RE = clip_policy.THANKS_TRIGGER_RE




































def _repair_short_topic_end(start_s, end_s, body_lines, chunk_end):
    """模型给出极短时间但正文很多时，修正报告话题结束时间。"""
    duration = end_s - start_s
    body_len = sum(_text_len_for_timing(line) for line in body_lines)
    if duration >= 10 or body_len < 40:
        return end_s
    estimated = min(TOPIC_MAX_REPAIRED_REPORT_SEC, max(TOPIC_MIN_REPORT_SEC, body_len / SRT_ESTIMATED_CHARS_PER_SEC))
    return int(min(chunk_end, start_s + estimated))




def _manual_entry_matches_topic(entry, topic, margin=0):
    start = int(topic["start"]) - margin
    end = int(topic["end"]) + margin
    entry_start = int(entry["start"])
    entry_end = max(entry_start + 1, int(entry.get("end", entry_start + 1)))
    return entry_start < end and entry_end > start


def _is_manual_merge_target(topic):
    """人工重点只合并到真实话题；兜底/泛话题会吞掉重点，必须单独补话题。"""
    if topic.get("fallback"):
        return False
    if topic.get("source") == "manual_timeline":
        return True
    if _is_bad_topic_title(topic.get("title", "")):
        return False
    if topic.get("title") in _GENERIC_TOPIC_TITLES:
        return False
    text = " ".join([topic.get("title", "")] + list(topic.get("body") or []))
    compact = re.sub(r'\s+', '', text)
    if any(keyword in compact for keyword in _UNCUTTABLE_CONTENT_KEYWORDS):
        return False
    return True


def merge_manual_timeline_topics(topics, entries):
    """后置对照优化时间轴；命中只附证据，遗漏候选必须再次复核。"""
    if not entries:
        return topics
    for topic in topics:
        if not _is_manual_merge_target(topic):
            continue
        matched = [entry for entry in entries if _manual_entry_matches_topic(entry, topic)]
        if not matched:
            continue
        existing_entries = list(topic.get("manual_timeline") or [])
        for entry in matched:
            if entry not in existing_entries:
                existing_entries.append(entry)
        topic["manual_stars"] = max(
            [topic.get("manual_stars", 0)]
            + [entry.get("stars", 0) for entry in existing_entries]
        )
        topic["manual_timeline"] = existing_entries
        body = list(topic.get("body") or [])
        for entry in matched:
            if entry.get("stars", 0) <= 0:
                continue
            stars = "⭐" * min(entry.get("stars", 0), 5)
            line = f"●人工时间轴{stars}：{timecode.format_elapsed(entry['start'])} {entry['text']}"
            if line not in body:
                body.append(line)
        topic["body"] = body

    for entry in entries:
        optimized = entry.get("source") == "optimized_manual_timeline"
        if entry.get("stars", 0) <= 0 and not optimized:
            continue
        if any(entry in (topic.get("manual_timeline") or []) for topic in topics):
            continue
        if any(_is_manual_merge_target(topic) and _manual_entry_matches_topic(entry, topic) for topic in topics):
            continue
        topic_start = (
            max(0, int(entry["start"]))
            if optimized
            else max(0, int(entry["start"]) - MANUAL_TIMELINE_TOPIC_PRE_SEC)
        )
        topic_end = (
            max(topic_start + 1, int(entry.get("end", topic_start + 1)))
            if optimized
            else int(entry["start"]) + MANUAL_TIMELINE_TOPIC_POST_SEC
        )
        topic = {
            "start": topic_start,
            "end": topic_end,
            "start_str": timecode.format_elapsed(topic_start),
            "end_str": timecode.format_elapsed(topic_end),
            "title": _manual_title_from_text(entry["text"]),
            "can_slice": False,
            "body": list(entry.get("summary") or []) + [
                f"●人工时间轴{'⭐' * min(entry.get('stars', 0), 5)}："
                f"{timecode.format_elapsed(entry['start'])} {entry['text']}"
            ],
            "manual_stars": entry.get("stars", 0),
            "manual_timeline": [entry],
            "source": entry.get("source", "manual_timeline"),
            # 时间轴优化阶段只负责整理候选。首轮遗漏后必须再做一次独立复核，
            # 成功前不能把优化阶段的 ai_enriched 当作切片许可。
            "ai_enriched": False if optimized else bool(entry.get("ai_enriched")),
            "ai_focus_validated": False if optimized else bool(entry.get("ai_focus_validated")),
            "postcheck_pending": optimized,
            "reference_only": optimized,
            "publish_title": entry.get("publish_title"),
        }
        if not _is_duplicate_topic(topic, [old for old in topics if _is_manual_merge_target(old)]):
            topics.append(topic)
    topics.sort(key=lambda item: (item["start"], item["end"]))
    return topics


def _topics_from_manual_timeline(
        entries, srt_segments=None, peaks=None, max_gap_sec=240,
        max_group_duration_sec=None):
    """基于字幕/弹幕生成话题，人工时间轴只作为辅助参考和校准。"""
    sorted_entries = sorted(entries or [], key=lambda item: item["start"])
    groups = []
    current = []
    for entry in sorted_entries:
        if entry.get("explicit_range"):
            if current:
                groups.append(current)
                current = []
            groups.append([entry])
            continue
        if not current:
            current = [entry]
            continue
        same_hour = int(entry["start"] // 3600) == int(current[-1]["start"] // 3600)
        within_group_duration = (
            max_group_duration_sec is None
            or entry["start"] - current[0]["start"] <= max_group_duration_sec
        )
        if (
            same_hour
            and within_group_duration
            and entry["start"] - current[-1]["start"] <= max_gap_sec
        ):
            current.append(entry)
        else:
            groups.append(current)
            current = [entry]
    if current:
        groups.append(current)

    topics = []
    for group in groups:
        starred_entries = [item for item in group if item.get("stars", 0) > 0]
        if starred_entries and any(item.get("alignment_score") is not None for item in starred_entries):
            title_entry = max(
                starred_entries,
                key=lambda item: (
                    float(item.get("alignment_score") or 0),
                    len(str(item.get("text", ""))),
                ),
            )
        else:
            title_entry = starred_entries[0] if starred_entries else group[0]
        explicit_end = group[0].get("end") if len(group) == 1 and group[0].get("explicit_range") else None
        if explicit_end is not None:
            start = max(0, int(group[0]["start"]))
            end = max(start + 1, int(explicit_end))
        else:
            start = max(0, int(group[0]["start"]) - (MANUAL_TIMELINE_TOPIC_PRE_SEC if title_entry.get("stars", 0) else 0))
            end = int(group[-1]["start"]) + (MANUAL_TIMELINE_TOPIC_POST_SEC if title_entry.get("stars", 0) else 120)
        body = []
        body.extend(_topic_danmaku_reference_lines(start, end, peaks or []))
        body.extend(_topic_srt_summary_lines(start, end, srt_segments or []))
        for item in group:
            time_label = timecode.format_elapsed(item["start"])
            if item.get("stars", 0) > 0:
                stars = "⭐" * min(item.get("stars", 0), 5)
                body.append(f"●人工时间轴{stars}：{time_label} {item['text']}")
            else:
                body.append(f"·时间轴：{time_label} {item['text']}")
            if item.get("reference_publish_title"):
                body.append(f"·参考投稿标题（仅供核对）：{item['reference_publish_title']}")
        topic = {
            "start": start,
            "end": end,
            "start_str": timecode.format_elapsed(start),
            "end_str": timecode.format_elapsed(end),
            "title": _manual_title_from_text(title_entry["text"]),
            "can_slice": False,
            "body": body,
            "manual_stars": max(item.get("stars", 0) for item in group),
            "manual_timeline": group,
            "source": "subtitle_danmaku_with_manual_reference",
        }
        topics.append(topic)
    return topics


def parse_srt_text(srt_path):
    """解析 SRT，去空格，返回 [(start_s, end_s, text), ...]，并修复明显异常时间戳。"""
    return [
        (start_s, end_s, text)
        for start_s, end_s, text in _load_repaired_srt_segments(srt_path)
        if _subtitle_text_size(text) >= 2
    ]


def chunk_srt(segs, peaks, chunk_sec=CHUNK_SEC):
    """将 SRT 按时间分块，每块附带弹幕密度信息"""
    if not segs:
        return []
    avg_density = _average_danmaku_density(peaks)
    independent_peaks = _high_energy_danmaku_peaks(peaks, avg_density)

    chunks = []
    chunk_start = segs[0][0]
    current_texts = []

    for item in segs:
        if len(item) == 3:
            start_s, end_s, text = item
        else:
            start_s, text = item
            end_s = start_s
        if start_s - chunk_start > chunk_sec:
            if current_texts:
                chunks.append(_make_chunk(
                    chunk_start,
                    current_texts,
                    peaks,
                    avg_density,
                    independent_peaks=independent_peaks,
                ))
            chunk_start = start_s
            current_texts = []
        time_label = timecode.format_elapsed(start_s) if end_s <= start_s + 1 else f"{timecode.format_elapsed(start_s)}－{timecode.format_elapsed(end_s)}"
        current_texts.append(f"[{time_label}] {text}")

    if current_texts:
        chunks.append(_make_chunk(
            chunk_start,
            current_texts,
            peaks,
            avg_density,
            independent_peaks=independent_peaks,
        ))

    return chunks


def _make_chunk(
        chunk_start, texts, peaks, avg_density=0, independent_peaks=None):
    text_block = "\n".join(texts)
    chunk_end = chunk_start + CHUNK_SEC
    nearby_peaks = [(s, d) for s, d in peaks if chunk_start - 60 <= s <= chunk_end + 60]
    if nearby_peaks:
        max_d = max(d for _, d in nearby_peaks)
        ratio = max_d / avg_density if avg_density > 0 else 1.0
        danmaku_info = f"[弹幕: 本段峰值{max_d}条/分钟 = {ratio:.1f}倍平均 | 全场平均={avg_density:.0f}]"
    else:
        danmaku_info = f"[弹幕: 本段无峰值, 远低于全场平均{avg_density:.0f}]"
    independent_peaks = (
        _high_energy_danmaku_peaks(peaks, avg_density)
        if independent_peaks is None
        else independent_peaks
    )
    evidence_rows = []
    for peak_start, density in independent_peaks:
        if not chunk_start - DANMAKU_WINDOW <= peak_start <= chunk_end + DANMAKU_WINDOW:
            continue
        features = _danmaku_peak_features(
            peaks,
            peak_start,
            density,
            avg_density=avg_density,
        )
        evidence_rows.append((
            float(features["selection_score"]),
            int(peak_start),
            _danmaku_prompt_evidence(features),
        ))
    evidence_rows.sort(key=lambda row: (-row[0], row[1]))
    danmaku_evidence = [row[2] for row in evidence_rows[:4]]
    return {
        "start": chunk_start,
        "end": chunk_end,
        "text": text_block,
        "danmaku_info": danmaku_info,
        "danmaku_evidence": danmaku_evidence,
        "has_peaks": len(nearby_peaks) > 0,
    }










def _build_chunk_prompt(ch, index, total, compact=False, streamer_name=None):
    """构造字幕/弹幕首轮 prompt；人工时间轴不得参与这一轮。"""
    chunk_start = ch["start"]
    chunk_end = ch.get("end", ch["start"] + CHUNK_SEC)
    text_limit = LLM_COMPACT_TEXT_CHARS if compact else LLM_FULL_TEXT_CHARS
    context = _prompt_context(
        streamer_name,
        context_text=ch.get("text") or "",
        compact=compact,
    )
    prompt = _render_topic_analysis_prompt(
        _TopicAnalysisPromptEvidence(
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


_HEADING_RE = re.compile(
    r'^\s*(?:#{1,6}\s*)?(?:[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]|\d+[.)、])?\s*\['
    r'(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—－~～至]+\s*'
    r'(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+?)\s*$'
)
















_UNCUTTABLE_CONTENT_KEYWORDS = (
    "未发言", "仅播放", "只是播放", "游戏角色对话语音", "背景语音", "游戏画面/语音",
    "具体内容不清晰", "字幕识别较碎", "未形成稳定可切片主题", "暂不标记为自动切片",
    "无有效讲话", "全是沉默", "全是音乐", "机械复读", "游戏开头动画",
)


def _strip_code_fence(response):
    """去掉 LLM 可能包裹的 Markdown 代码块。"""
    response = (response or "").strip()
    if response.startswith("```"):
        response = re.sub(r'^```\w*\n?', '', response)
        response = re.sub(r'\n?```$', '', response)
    return response.strip()
















def _is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end, tolerance=90):
    """只接受当前分块时间范围附近的话题，过滤模型复读旧示例。"""
    if end_s <= start_s:
        return False
    if start_s < chunk_start - tolerance:
        return False
    if end_s > chunk_end + tolerance:
        return False
    return True


_overlap_ratio = boundary_analysis._overlap_ratio
_is_duplicate_topic = boundary_analysis._is_duplicate_topic






















def _parse_json_topics_response(response, chunk_start, chunk_end, accepted_topics):
    """优先解析结构化 JSON 响应；不是 JSON 时返回 None，由旧 Markdown 解析兜底。"""
    payload = _extract_json_payload(response)
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
        if not _is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end):
            continue
        raw_title = str(item.get("title", "")).strip()
        if _is_placeholder_title(raw_title):
            continue
        body_lines = content_normalization.filter_unsupported_ai_points(
            content_normalization.json_points_to_body(
            item.get("points", item.get("body", item.get("summary", item.get("details"))))
            )
        )
        if not body_lines:
            continue
        end_s = _repair_short_topic_end(start_s, end_s, body_lines, chunk_end)
        title = _derive_topic_title(_clean_topic_title(raw_title), body_lines)
        if not title:
            continue
        topic = {
            "start": start_s,
            "end": end_s,
            "start_str": start_str,
            "end_str": timecode.format_elapsed(end_s),
            "title": title,
            "publish_title": _normalise_publish_title(item.get("publish_title"), title),
            "can_slice": response_parsing.json_can_slice(
                item.get("can_slice", False),
                raw_title,
            ),
            "body": body_lines,
        }
        title_hook = _normalise_title_hook(item.get("title_hook"))
        if title_hook:
            topic["title_hook"] = title_hook
        if _is_duplicate_topic(topic, accepted_topics):
            continue
        accepted_topics.append(topic)
        parsed_topics.append(topic)
        if topic["can_slice"]:
            clip_marks.append({"start": topic["start"], "end": topic["end"], "title": topic["title"]})

    report_blocks = [_format_topic_block(topic, idx + 1) for idx, topic in enumerate(parsed_topics)]
    return report_blocks, _dedupe_clip_marks(clip_marks)


def _build_manual_topic_enrichment_prompt(topics, streamer_name=None, compact=False):
    """把规则聚合候选压缩成一次批量 AI 复核请求。"""
    candidates = []
    for index, topic in enumerate(topics or [], 1):
        body_limit = 8 if compact else 18
        evidence = [
            _strip_body_prefix(line)
            for line in (topic.get("body") or [])[:body_limit]
            if _strip_body_prefix(line)
        ]
        subtitle_evidence = [
            line for line in evidence if line.startswith("字幕核查：")
        ]
        manual_evidence = [
            line for line in evidence
            if line.startswith(("人工时间轴", "时间轴："))
        ]
        density_evidence = [
            line for line in evidence if line.startswith("弹幕依据：")
        ]
        candidates.append({
            "id": index,
            "start": timecode.format_elapsed(topic["start"]),
            "end": timecode.format_elapsed(topic["end"]),
            "current_title": topic.get("title", "未命名片段"),
            "evidence": evidence,
            "subtitle_evidence": subtitle_evidence,
            "manual_evidence": manual_evidence,
            "density_evidence": density_evidence,
            "reference_publish_title": topic.get("publish_title"),
        })
    context = _prompt_context(
        streamer_name,
        context_text=json.dumps(candidates, ensure_ascii=False),
        compact=compact,
        publish_title_example_text="具体事件钩子👀结果或原话",
    )
    return _render_manual_topic_enrichment_prompt(
        _ManualTopicPromptEvidence(
            context=context,
            candidates=tuple(candidates),
        )
    )








def enrich_manual_topics_with_llm(
        topics, streamer_name=None, progress_callback=None,
        retry_coordinator=None, progress_label="人工时间轴 AI 复核",
        progress_step=75):
    """用一次 DeepSeek 请求批量复核人工候选，并允许并列事件拆成两项。"""
    if not topics:
        return 0
    prompt = _build_manual_topic_enrichment_prompt(topics, streamer_name=streamer_name)
    compact_prompt = _build_manual_topic_enrichment_prompt(
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
    payload = _extract_json_payload(response)
    raw_topics = payload.get("topics", []) if isinstance(payload, dict) else []
    if not isinstance(raw_topics, list):
        raise LLMStructuredOutputError("人工时间轴 AI 复核未返回 topics 数组")

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
            if _is_duplicate_topic(enriched, replacements):
                continue
            replacements.append(enriched)
        if replacements:
            replacements.sort(key=lambda value: (value["start"], value["end"]))
            enriched_topics.extend(replacements)
            updated += len(replacements)
        else:
            enriched_topics.append(topic)
    if not updated:
        raise LLMStructuredOutputError("人工时间轴 AI 复核没有返回可用话题")
    topics[:] = sorted(enriched_topics, key=lambda value: (value["start"], value["end"]))
    return updated


def enrich_manual_topics_in_batches(
        topics, streamer_name=None, progress_callback=None,
        batch_size=MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE,
        batch_result_callback=None, progress_start=22, progress_end=24,
        progress_label="字幕校准人工时间轴"):
    """分批优化复杂人工时间轴，避免一次请求塞入整场证据。"""
    optimized_topics = []
    warnings = []
    safe_batch_size = max(1, batch_size)
    total_batches = max(1, math.ceil(len(topics or []) / safe_batch_size))
    report_progress = llm_execution.serialized_progress_callback(progress_callback)
    jobs = []
    for batch_index, offset in enumerate(
            range(0, len(topics or []), safe_batch_size), 1):
        batch = list(topics[offset:offset + safe_batch_size])
        jobs.append({
            "batch_index": batch_index,
            "offset": offset,
            "batch": batch,
        })

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
            thread_name_prefix="autoslice-manual") as executor:
        futures = [executor.submit(enrich_job, job) for job in jobs]
        for job, future in zip(jobs, futures):
            batch_index = job["batch_index"]
            offset = job["offset"]
            batch = job["batch"]
            try:
                future.result()
                unresolved = [topic for topic in batch if not topic.get("ai_enriched")]
                if unresolved:
                    for topic in unresolved:
                        topic["reference_only"] = True
                    warnings.append(
                        f"第 {batch_index}/{total_batches} 批仅复核 "
                        f"{len(batch) - len(unresolved)}/{len(batch)} 项，"
                        f"其余 {len(unresolved)} 项未返回"
                    )
            except Exception as exc:
                warning = f"第 {batch_index}/{total_batches} 批优化失败：{_short_llm_error(exc)}"
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
                    list(topics[offset + safe_batch_size:]),
                    list(warnings),
                )
    topics[:] = sorted(optimized_topics, key=lambda item: (item["start"], item["end"]))
    if not warnings:
        return None
    return "人工时间轴部分未完成字幕校准，相关条目仅作低权重参考：" + "；".join(warnings)


def _optimized_entry_semantic_text(entry):
    return " ".join([
        str(entry.get("text", "")),
        *[str(point) for point in entry.get("summary") or []],
    ]).strip()


def _manual_evidence_line(entry):
    stars = max(0, int(entry.get("stars", 0) or 0))
    prefix = f"●人工时间轴{'⭐' * min(stars, 5)}" if stars else "·时间轴"
    return f"{prefix}：{timecode.format_elapsed(int(entry.get('start', 0)))} {entry.get('text', '')}"


def _sanitize_optimized_manual_entry(entry):
    """过滤与原人工记录无关的 AI 改写，并移除误并入的原始星标。"""
    fixed = dict(entry or {})
    original_entries = [
        dict(item)
        for item in fixed.get("original_entries") or []
        if isinstance(item, dict)
    ]
    if not original_entries:
        return fixed

    semantic_text = _optimized_entry_semantic_text(fixed)
    grounded_entries = [
        item for item in original_entries
        if _manual_text_supports_candidate(item.get("text", ""), semantic_text)
    ]
    if not grounded_entries:
        return None

    fixed["original_entries"] = grounded_entries
    stars = max(int(item.get("stars", 0) or 0) for item in grounded_entries)
    fixed["stars"] = stars
    fixed["highlight"] = stars > 0
    if grounded_entries[0].get("clock"):
        fixed["clock"] = grounded_entries[0]["clock"]

    evidence = [
        str(line)
        for line in fixed.get("evidence") or []
        if not str(line).startswith(("●人工时间轴", "·时间轴"))
    ]
    for item in grounded_entries:
        line = _manual_evidence_line(item)
        if line not in evidence:
            evidence.append(line)
    fixed["evidence"] = evidence
    return fixed


def _parse_llm_response(response, chunk_start, chunk_end, accepted_topics=None, allow_markdown_fallback=True):
    """
    解析单个分块的 LLM 输出。

    返回: (report_blocks, clip_marks)
    - report_blocks: 单话题时间轴块，主要用于测试和调试
    - clip_marks: 去重后的可切片段列表
    """
    accepted_topics = accepted_topics if accepted_topics is not None else []
    json_result = _parse_json_topics_response(response, chunk_start, chunk_end, accepted_topics)
    if json_result is not None:
        return json_result
    if not allow_markdown_fallback:
        return [], []

    response = _strip_code_fence(response)
    if not response or response.strip() == "无明显话题":
        return [], []

    parsed_topics = []
    current = None

    def flush_current():
        if not current:
            return
        start_s = current["start"]
        end_s = current["end"]
        if not _is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end):
            return

        if _is_placeholder_title(current["title"]):
            return
        body_lines = [
            content_normalization.normalise_body_line(line)
            for line in current["body"]
        ]
        body_lines = [line for line in body_lines if line]
        if not body_lines:
            return
        end_s = _repair_short_topic_end(start_s, end_s, body_lines, chunk_end)
        title = _derive_topic_title(current["title"], body_lines)
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
        if _is_duplicate_topic(topic, accepted_topics):
            return
        accepted_topics.append(topic)
        parsed_topics.append(topic)

    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r'^Part\s*\d+\s*[:：]', line, re.IGNORECASE):
            # 分块分析不接受 LLM 自己输出 Part，避免最终报告 Part 重复。
            continue
        match = _HEADING_RE.match(line)
        if match:
            flush_current()
            start_str, end_str, raw_title = match.groups()
            current = {
                "start_str": start_str,
                "end_str": end_str,
                "start": timecode.parse_hms(start_str),
                "end": timecode.parse_hms(end_str),
                "title": _clean_topic_title(raw_title),
                "can_slice": response_parsing.is_slice_marked(raw_title),
                "body": [],
            }
        elif current:
            current["body"].append(line)

    flush_current()

    report_blocks = [_format_topic_block(topic, idx + 1) for idx, topic in enumerate(parsed_topics)]
    clip_marks = [
        {"start": topic["start"], "end": topic["end"], "title": topic["title"]}
        for topic in parsed_topics
        if topic["can_slice"]
    ]
    return report_blocks, clip_marks


def _strip_prompt_time_labels(text):
    """去掉分块字幕里的 [time] 标签，生成兜底摘要时使用。"""
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r'^\[[^\]]+\]\s*', '', raw).strip()
        if line:
            lines.append(line)
    return " ".join(lines)




def _make_fallback_topic_from_chunk(ch, streamer_name=None):
    """当 LLM 对分块没有有效输出时，生成非切片兜底时间轴，避免整场直播空白。"""
    text = _strip_prompt_time_labels(ch.get("text", ""))
    text = re.sub(r'\s+', '', text)
    if len(text) < 20:
        return None
    title = _fallback_title_from_text(text)
    topic = {
        "start": int(ch["start"]),
        "end": int(ch.get("end", ch["start"] + CHUNK_SEC)),
        "start_str": timecode.format_elapsed(ch["start"]),
        "end_str": timecode.format_elapsed(ch.get("end", ch["start"] + CHUNK_SEC)),
        "title": title,
        "can_slice": False,
        "body": [
            f"·本段为{streamer_name}的连续聊天/互动，字幕识别较碎，已保留在时间轴中",
            "·该段未形成稳定可切片主题，暂不标记为自动切片",
        ],
        "fallback": True,
    }
    return topic

# 边界兼容别名：候选模块消费 boundaries 的唯一实现，不保留本地副本。
_BOUNDARY_EVIDENCE_STOP_TERMS = boundary_analysis._BOUNDARY_EVIDENCE_STOP_TERMS
_NEXT_CASE_ASR_TRIGGER_RE = boundary_analysis._NEXT_CASE_ASR_TRIGGER_RE
_OUTRO_ACTIVITY_VARIANT_RE = boundary_analysis._OUTRO_ACTIVITY_VARIANT_RE
_OUTRO_FAREWELL_EVIDENCE = boundary_analysis._OUTRO_FAREWELL_EVIDENCE
_OUTRO_TRIGGER_NORMALISE_RE = boundary_analysis._OUTRO_TRIGGER_NORMALISE_RE
_TOPIC_CONCLUSION_RE = boundary_analysis._TOPIC_CONCLUSION_RE
_TOPIC_DECISION_EVIDENCE_RE = boundary_analysis._TOPIC_DECISION_EVIDENCE_RE
_TOPIC_DISCOURSE_CONTINUATION_RE = boundary_analysis._TOPIC_DISCOURSE_CONTINUATION_RE
_TOPIC_LEAD_IN_TRIGGER_RE = boundary_analysis._TOPIC_LEAD_IN_TRIGGER_RE
_TOPIC_REFUND_RE = boundary_analysis._TOPIC_REFUND_RE
_TRIGGER_CONTEXT_TOPIC_RE = boundary_analysis._TRIGGER_CONTEXT_TOPIC_RE
_VISUAL_CASE_SHIFT_RE = boundary_analysis._VISUAL_CASE_SHIFT_RE
_VISUAL_REACTION_LEAD_IN_RE = boundary_analysis._VISUAL_REACTION_LEAD_IN_RE
_VISUAL_REVIEW_TOPIC_RE = boundary_analysis._VISUAL_REVIEW_TOPIC_RE
_boundary_context_has_speech = boundary_analysis._boundary_context_has_speech
_boundary_context_is_relevant = boundary_analysis._boundary_context_is_relevant
_boundary_evidence_term_counts = boundary_analysis._boundary_evidence_term_counts
_boundary_evidence_text_is_relevant = boundary_analysis._boundary_evidence_text_is_relevant
_cap_expanded_clip_mark = boundary_analysis._cap_expanded_clip_mark
_capped_speech_chain_start = boundary_analysis._capped_speech_chain_start
_clip_context_requires_trigger = boundary_analysis._clip_context_requires_trigger
_dedupe_clip_marks = boundary_analysis._dedupe_clip_marks
_detect_stream_outro_clip = boundary_analysis._detect_stream_outro_clip
_expand_clip_mark_with_context = boundary_analysis._expand_clip_mark_with_context
_expand_clip_marks_with_context = boundary_analysis._expand_clip_marks_with_context
_find_next_topic_hard_end = boundary_analysis._find_next_topic_hard_end
_find_relevant_topic_context_end = boundary_analysis._find_relevant_topic_context_end
_find_relevant_topic_context_start = boundary_analysis._find_relevant_topic_context_start
_find_sc_context_start = boundary_analysis._find_sc_context_start
_find_topic_lead_in_start = boundary_analysis._find_topic_lead_in_start
_find_visual_reaction_context_start = boundary_analysis._find_visual_reaction_context_start
_fit_final_clip_to_safe_srt_boundaries = boundary_analysis._fit_final_clip_to_safe_srt_boundaries
_gift_trigger_has_question_followup = boundary_analysis._gift_trigger_has_question_followup
_has_outro_farewell_evidence = boundary_analysis._has_outro_farewell_evidence
_integer_clip_bounds_outside_subtitles = boundary_analysis._integer_clip_bounds_outside_subtitles
_is_duplicate_topic = boundary_analysis._is_duplicate_topic
_is_explicit_sc_topic = boundary_analysis._is_explicit_sc_topic
_is_explicit_sc_trigger = boundary_analysis._is_explicit_sc_trigger
_looks_like_delayed_topic_conclusion = boundary_analysis._looks_like_delayed_topic_conclusion
_looks_like_discourse_continuation = boundary_analysis._looks_like_discourse_continuation
_looks_like_low_score_visual_case_shift = boundary_analysis._looks_like_low_score_visual_case_shift
_looks_like_next_case_transition = boundary_analysis._looks_like_next_case_transition
_looks_like_sc_or_gift_trigger = boundary_analysis._looks_like_sc_or_gift_trigger
_merge_expanded_clip_marks = boundary_analysis._merge_expanded_clip_marks
_nearest_safe_srt_boundary = boundary_analysis._nearest_safe_srt_boundary
_next_report_topic_safe_boundary = boundary_analysis._next_report_topic_safe_boundary
_normalise_boundary_evidence_text = boundary_analysis._normalise_boundary_evidence_text
_normalise_outro_trigger_text = boundary_analysis._normalise_outro_trigger_text
_outro_topic_from_mark = boundary_analysis._outro_topic_from_mark
_overlap_ratio = boundary_analysis._overlap_ratio
_refresh_natural_boundary_metadata = boundary_analysis._refresh_natural_boundary_metadata
_score_boundary_evidence_text = boundary_analysis._score_boundary_evidence_text
_snap_clip_to_srt_segments = boundary_analysis._snap_clip_to_srt_segments
_split_chain_crossing_topic_end = boundary_analysis._split_chain_crossing_topic_end
_srt_video_duration = boundary_analysis._srt_video_duration
_subtitle_speech_chains = boundary_analysis._subtitle_speech_chains
parse_srt_segments = boundary_analysis.parse_srt_segments
_BOUNDARY_SEMANTIC_SIGNAL_PATTERNS = boundary_analysis._BOUNDARY_SEMANTIC_SIGNAL_PATTERNS
_boundary_semantic_signals = boundary_analysis._boundary_semantic_signals
_boundary_text_has_semantic_signal = boundary_analysis._boundary_text_has_semantic_signal


_CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺㊻㊼㊽㊾㊿"


def _format_report_time(seconds):
    """报告展示用时间：1小时内用 MM:SS，超过 1 小时用 H:MM:SS。"""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _topic_index_label(index):
    if 1 <= index <= len(_CIRCLED_NUMBERS):
        return _CIRCLED_NUMBERS[index - 1]
    return f"{index}."




def _format_topic_block(topic, index, streamer_name=None):
    """格式化单个话题块，贴近用户给出的逐话题时间轴样式。"""
    label = _topic_index_label(index) if index else ""
    start = _format_report_time(topic["start"])
    end = _format_report_time(topic["end"])
    marker = " ✂️" if topic.get("can_slice") else ""
    title = _replace_streamer_role(topic["title"], streamer_name)
    lines = [f"{label}[{start}－{end}]{title}{marker}"]
    body = topic.get("body") or []
    lines.extend(_replace_streamer_role(line, streamer_name) for line in body)
    return "\n".join(lines)


def _topic_peak_focus_window(topic, peaks, window_sec=DANMAKU_WINDOW):
    """返回话题内最高弹幕峰值窗口；实际切片优先围绕该窗口扩前后文。"""
    start = int(topic["start"])
    end = int(topic["end"])
    candidates = _topic_peak_candidates(topic, peaks, window_sec)
    if not candidates:
        return None
    peak_start, density = max(candidates, key=lambda item: item[1])
    focus_start = max(start, int(peak_start) - TOPIC_FOCUS_PRE_SEC)
    focus_end = min(end, int(peak_start) + TOPIC_FOCUS_POST_SEC)
    if focus_end <= focus_start:
        focus_end = min(end, focus_start + window_sec)
    return {
        "start": int(focus_start),
        "end": int(max(focus_end, focus_start + 1)),
        "anchor": int(peak_start + window_sec / 2),
        "density": density,
    }


def _assign_topic_slice_window(topic, peaks):
    """为话题分配较短的实际切片核心范围；报告范围仍保留完整话题。"""
    topic_start = int(topic["start"])
    topic_end = int(topic["end"])
    if topic_end <= topic_start:
        return topic

    duration = topic_end - topic_start
    fixed = topic
    peak_focus = _topic_peak_focus_window(topic, peaks)
    if not peak_focus:
        fixed["can_slice"] = False
        return fixed

    fixed["slice_anchor"] = peak_focus["anchor"]
    fixed["slice_anchor_source"] = "弹幕峰值"
    fixed["slice_peak_density"] = peak_focus["density"]
    if duration <= TOPIC_DIRECT_SLICE_MAX_SEC:
        fixed["slice_start"] = topic_start
        fixed["slice_end"] = topic_end
        return fixed

    fixed["slice_start"] = peak_focus["start"]
    fixed["slice_end"] = peak_focus["end"]
    body = list(fixed.get("body") or [])
    note = (
        f"·切片核心：完整话题较长，实际切片围绕弹幕峰值"
        f"{timecode.format_elapsed(peak_focus['anchor'])}截取，保留峰值前后完整反应"
    )
    if note not in body:
        body.append(note)
    fixed["body"] = body
    return fixed


def _is_content_cuttable_topic(topic):
    """判断话题内容本身是否适合切片，避免只有背景语音/兜底说明被高弹幕误切。"""
    if topic.get("fallback"):
        return False
    if topic.get("reference_only"):
        return False
    if topic.get("source") in {"manual_timeline", "optimized_manual_timeline"} and not topic.get("ai_enriched"):
        return False
    if _is_bad_topic_title(topic.get("title", "")):
        return False
    text = " ".join([topic.get("title", "")] + list(topic.get("body") or []))
    compact = re.sub(r'\s+', '', text)
    if not compact:
        return False
    if any(keyword in compact for keyword in _UNCUTTABLE_CONTENT_KEYWORDS):
        return False
    return True


def _topic_semantic_text(topic):
    parts = [str(topic.get("title", ""))]
    for line in topic.get("body") or []:
        value = str(line)
        if value.startswith((
                "●人工时间轴", "·时间轴", "·弹幕依据：", "·切片核心：",
                "·参考投稿标题",
        )):
            continue
        clean = _strip_body_prefix(value)
        if clean:
            parts.append(clean)
    return " ".join(parts)


def _danmaku_topic_alignment(topic, evidence):
    """衡量代表弹幕与话题事实是否一致，避免峰值挂到相邻话题。"""
    if not isinstance(evidence, dict):
        return 0.0
    semantic_text = _topic_semantic_text(topic)
    if not semantic_text:
        return 0.0
    messages = evidence.get("representative_messages") or []
    scored = []
    for item in messages:
        text = _clean_ass_danmaku_text(item.get("text", ""))
        if not text or _is_generic_danmaku_reaction(text):
            continue
        score = _manual_alignment_score(text, semantic_text)
        if score <= 0:
            continue
        weight = 1.0 + math.log1p(max(1, int(item.get("count", 1) or 1)))
        scored.append((score, weight))
    if not scored:
        return 0.0
    scored.sort(key=lambda item: item[0], reverse=True)
    strongest = scored[0][0]
    weighted_average = sum(score * weight for score, weight in scored[:3]) / sum(
        weight for _, weight in scored[:3]
    )
    return round(strongest * 0.70 + weighted_average * 0.30, 4)


def _manual_entry_meaningfully_overlaps_topic(entry, topic):
    topic_start = int(topic.get("start", 0))
    topic_end = max(topic_start + 1, int(topic.get("end", topic_start + 1)))
    entry_start = int(entry.get("start", 0))
    entry_end = max(entry_start + 1, int(entry.get("end", entry_start + 1)))
    overlap = max(0, min(topic_end, entry_end) - max(topic_start, entry_start))
    if overlap <= 0:
        return False
    entry_duration = max(1, entry_end - entry_start)
    topic_duration = max(1, topic_end - topic_start)
    return (
        overlap >= 20
        or overlap / entry_duration >= 0.5
        or overlap / topic_duration >= 0.25
    )


def _reconcile_topic_manual_evidence(topic):
    """按 AI 最终语义边界重新挂接人工证据，移除相邻事件和误星标。"""
    fixed = dict(topic)
    manual_entries = [
        entry for entry in fixed.get("manual_timeline") or []
        if isinstance(entry, dict)
    ]
    if not manual_entries:
        return fixed

    semantic_text = _topic_semantic_text(fixed)
    retained_entries = []
    retained_evidence = []
    seen_evidence = set()
    for raw_entry in manual_entries:
        entry = (
            _sanitize_optimized_manual_entry(raw_entry)
            if raw_entry.get("source") == "optimized_manual_timeline"
            or raw_entry.get("original_entries")
            else dict(raw_entry)
        )
        if not entry or not _manual_entry_meaningfully_overlaps_topic(entry, fixed):
            continue
        entry_supports_topic = _manual_text_supports_candidate(
            _optimized_entry_semantic_text(entry), semantic_text
        )
        if not entry_supports_topic:
            continue

        original_entries = [
            dict(item)
            for item in entry.get("original_entries") or []
            if isinstance(item, dict)
        ]
        if original_entries:
            relevant_originals = []
            for item in original_entries:
                if _manual_text_supports_candidate(
                        item.get("text", ""), semantic_text):
                    relevant_originals.append(item)
                    continue

                # 高星原句经常是整段对话的收尾、反问或梗点，AI 摘要会把
                # 它改写成概述，逐字匹配不足不代表属于相邻话题。仅当优化后
                # 的整条时间轴已和当前话题语义相符、原句明确落在话题内部时
                # 保留，仍由后续 Terra 独立复核决定是否可切。
                try:
                    stars = int(item.get("stars", 0) or 0)
                    item_start = int(item.get("start", 0) or 0)
                except (TypeError, ValueError):
                    continue
                topic_start = int(fixed.get("start", 0) or 0)
                topic_end = max(
                    topic_start + 1,
                    int(fixed.get("end", topic_start + 1) or topic_start + 1),
                )
                if (
                    entry_supports_topic
                    and stars >= CLIP_MANUAL_REVIEW_MIN_STARS
                    and topic_start + 5 <= item_start <= topic_end - 5
                ):
                    relevant_originals.append(item)
            if not relevant_originals:
                continue
            entry["original_entries"] = relevant_originals
            evidence_entries = relevant_originals
            entry_stars = max(
                int(item.get("stars", 0) or 0)
                for item in relevant_originals
            )
            entry["stars"] = entry_stars
            entry["highlight"] = entry_stars > 0
        else:
            evidence_entries = [entry]

        retained_entries.append(entry)
        for evidence_entry in evidence_entries:
            key = (
                int(evidence_entry.get("start", 0) or 0),
                str(evidence_entry.get("text", "")).strip(),
            )
            if not key[1] or key in seen_evidence:
                continue
            seen_evidence.add(key)
            retained_evidence.append(_manual_evidence_line(evidence_entry))

    body = [
        str(line)
        for line in fixed.get("body") or []
        if not str(line).startswith(("●人工时间轴", "·时间轴"))
    ]
    body.extend(line for line in retained_evidence if line not in body)
    fixed["body"] = body
    fixed["manual_timeline"] = retained_entries
    fixed["manual_stars"] = max(
        [0]
        + [int(entry.get("stars", 0) or 0) for entry in retained_entries]
    )
    return fixed


def _report_fact_lines(topic):
    """返回用于识别报告重复事件的正文事实，排除密度和人工证据标签。"""
    facts = []
    for line in topic.get("body") or []:
        value = str(line)
        if value.startswith((
                "●人工时间轴", "·时间轴", "·弹幕依据：", "·切片核心：",
                "·参考投稿标题",
        )):
            continue
        clean = _strip_body_prefix(value)
        if clean:
            facts.append(clean)
    return facts


def _trim_report_topic_around_reviewed_topic(topic, reviewed_topic, trim_start):
    """让普通报告话题避开已复核核心，并移除被核心重复覆盖的事实。"""
    fixed = dict(topic)
    if trim_start:
        fixed["start"] = int(reviewed_topic["end"])
    else:
        fixed["end"] = int(reviewed_topic["start"])
    if int(fixed["end"]) - int(fixed["start"]) < 30:
        return None

    reviewed_facts = _report_fact_lines(reviewed_topic)
    body = []
    removed_fact = False
    for line in fixed.get("body") or []:
        clean = _strip_body_prefix(str(line))
        is_fact = clean and not str(line).startswith((
            "●人工时间轴", "·时间轴", "·弹幕依据：", "·切片核心：",
            "·参考投稿标题",
        ))
        if is_fact and any(
                _manual_alignment_score(clean, reviewed) >= 0.20
                for reviewed in reviewed_facts):
            removed_fact = True
            continue
        body.append(line)
    fixed["body"] = body
    fixed["start_str"] = timecode.format_elapsed(fixed["start"])
    fixed["end_str"] = timecode.format_elapsed(fixed["end"])
    fixed = _reconcile_topic_manual_evidence(fixed)

    if removed_fact:
        remaining_facts = _report_fact_lines(fixed)
        rebuilt_title = _derive_topic_title(
            "",
            [f"·{fact}" for fact in remaining_facts],
        )
        if rebuilt_title:
            fixed["title"] = rebuilt_title
            fixed["publish_title"] = _fallback_publish_title(rebuilt_title)
    return fixed if _report_fact_lines(fixed) else None


def _resolve_reviewed_report_overlaps(topics, max_overlap_sec=120):
    """具体复核话题优先，修正相邻普通话题在报告中的局部重叠。"""
    resolved = sorted(
        [dict(topic) for topic in topics or []],
        key=lambda item: (item.get("start", 0), item.get("end", 0)),
    )
    index = 0
    while index + 1 < len(resolved):
        current = resolved[index]
        following = resolved[index + 1]
        overlap = min(int(current["end"]), int(following["end"])) - max(
            int(current["start"]), int(following["start"])
        )
        if overlap <= 0 or overlap > max_overlap_sec:
            index += 1
            continue
        current_reviewed = current.get("clip_review_validated") is True
        following_reviewed = following.get("clip_review_validated") is True
        if current_reviewed == following_reviewed:
            index += 1
            continue

        if current_reviewed:
            trimmed = _trim_report_topic_around_reviewed_topic(
                following,
                current,
                trim_start=True,
            )
            if trimmed is None:
                resolved.pop(index + 1)
            else:
                resolved[index + 1] = trimmed
                index += 1
            continue

        if int(current["end"]) <= int(following["end"]):
            trimmed = _trim_report_topic_around_reviewed_topic(
                current,
                following,
                trim_start=False,
            )
            if trimmed is None:
                resolved.pop(index)
            else:
                resolved[index] = trimmed
                index += 1
            continue
        index += 1
    return resolved


def _clean_topics_for_report(topics):
    """生成报告/切片前做最后一道清洗，防止坏标题或提示残留漏网。"""
    prepared = []
    for topic in topics or []:
        if topic.get("fallback"):
            prepared.append(topic)
            continue
        topic = _reconcile_topic_manual_evidence(topic)
        body_lines = [
            content_normalization.normalise_body_line(line)
            for line in topic.get("body") or []
        ]
        body_lines = [line for line in body_lines if line]
        if not body_lines:
            continue
        title = _derive_topic_title(topic.get("title", ""), body_lines)
        if not title:
            continue
        fact_lines = _report_fact_lines({"body": body_lines})
        title_rebuilt = False
        if fact_lines and max(
                _manual_alignment_score(title, fact) for fact in fact_lines) == 0:
            rebuilt_title = _derive_topic_title(
                "",
                [f"·{fact}" for fact in fact_lines],
            )
            if rebuilt_title:
                title = rebuilt_title
                title_rebuilt = True
        fixed = dict(topic)
        fixed["title"] = title
        fixed["body"] = body_lines
        publish_title = (
            _fallback_publish_title(title)
            if title_rebuilt
            else _normalise_publish_title(fixed.get("publish_title"), title)
        )
        fixed["publish_title"] = _sanitize_transport_claims(
            publish_title,
            body_lines,
        )
        prepared.append(fixed)

    # 具体 AI/字幕话题优先去重。十分钟兜底段最后处理，避免它先占住
    # 整个范围后把内部已经二次复核的短话题误判为重复项。
    cleaned = []
    for fixed in sorted(
        prepared,
        key=lambda item: (
            bool(item.get("fallback")),
            item.get("start", 0),
            item.get("end", 0),
        ),
    ):
        if _is_duplicate_topic(fixed, cleaned):
            continue
        cleaned.append(fixed)
    cleaned = _resolve_reviewed_report_overlaps(cleaned)
    meaningful_hours = {
        int(topic.get("start", 0)) // 3600
        for topic in cleaned
        if not topic.get("fallback")
    }
    cleaned = [
        topic for topic in cleaned
        if not (
            topic.get("fallback")
            and int(topic.get("start", 0)) // 3600 in meaningful_hours
        )
    ]
    return sorted(cleaned, key=lambda item: (item.get("start", 0), item.get("end", 0)))


def _refresh_topic_danmaku_evidence(topic, peaks):
    """AI 缩小语义核心后重新计算弹幕依据，移除已落在核心外的旧峰值说明。"""
    candidates = _topic_peak_candidates(topic, peaks)
    best = max(candidates, key=lambda item: item[1]) if candidates else None
    evidence = None
    if best:
        peak_start, density = best
        evidence = f"·弹幕依据：{timecode.format_elapsed(peak_start)} 附近峰值约 {density:.0f} 条/分钟"
    body = []
    inserted = False
    for line in topic.get("body") or []:
        if str(line).startswith("·切片核心："):
            continue
        if str(line).startswith("·弹幕依据："):
            if evidence and not inserted:
                body.append(evidence)
                inserted = True
            continue
        if evidence and not inserted and str(line).startswith("●人工时间轴"):
            body.append(evidence)
            inserted = True
        body.append(line)
    if evidence and not inserted:
        body.append(evidence)
    topic["body"] = body
    return best


def _append_clip_candidate_source(topic, source):
    """记录候选的发现来源，避免峰值、语义和人工时间轴互相覆盖。"""
    sources = [
        str(value).strip()
        for value in topic.get("clip_candidate_sources") or []
        if str(value).strip()
    ]
    if source not in sources:
        sources.append(source)
    topic["clip_candidate_sources"] = sources
    topic["clip_review_candidate"] = True


def _has_high_star_manual_evidence(topic):
    """高星人工时间轴只提供复核入口，必须有真实的人工证据可追溯。"""
    try:
        if int(topic.get("manual_stars", 0) or 0) < CLIP_MANUAL_REVIEW_MIN_STARS:
            return False
    except (TypeError, ValueError):
        return False
    if any(
            str(line).startswith("●人工时间轴")
            for line in topic.get("body") or []):
        return True
    for entry in topic.get("manual_timeline") or []:
        if not isinstance(entry, dict):
            continue
        try:
            if int(entry.get("stars", 0) or 0) >= CLIP_MANUAL_REVIEW_MIN_STARS:
                return True
        except (TypeError, ValueError):
            pass
        for item in entry.get("original_entries") or []:
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("stars", 0) or 0) >= CLIP_MANUAL_REVIEW_MIN_STARS:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _manual_review_anchor(topic):
    """为无峰值的高星候选提供审计锚点，不把它伪装成弹幕峰值。"""
    topic_start = int(topic.get("start", 0) or 0)
    topic_end = max(topic_start + 1, int(topic.get("end", topic_start + 1) or topic_start + 1))
    choices = []
    for entry in topic.get("manual_timeline") or []:
        if not isinstance(entry, dict):
            continue
        originals = entry.get("original_entries") or [entry]
        for item in originals:
            if not isinstance(item, dict):
                continue
            try:
                stars = int(item.get("stars", entry.get("stars", 0)) or 0)
                start = int(item.get("start", entry.get("start", topic_start)) or topic_start)
            except (TypeError, ValueError):
                continue
            if stars >= CLIP_MANUAL_REVIEW_MIN_STARS and topic_start <= start <= topic_end:
                choices.append((stars, start))
    if choices:
        return max(choices, key=lambda item: (item[0], -abs(item[1] - (topic_start + topic_end) / 2)))[1]
    return (topic_start + topic_end) // 2


def _reviewed_topic_has_required_interest(topic):
    """新策略严格使用 Terra 分数；旧调用仅在仍有真实峰值时保持兼容。"""
    if not topic.get("clip_review_validated"):
        return False
    score = _parse_clip_interest_score(topic.get("clip_interest_score"))
    return score is None or score >= CLIP_MIN_INTEREST_SCORE


def _assign_reviewed_semantic_slice_window(topic, source, anchor=None):
    """复核后的语义核心已经有完整边界，不再强行拉回已失配的弹幕峰值。"""
    topic_start = int(topic.get("start", 0) or 0)
    topic_end = max(topic_start + 1, int(topic.get("end", topic_start + 1) or topic_start + 1))
    topic["slice_start"] = topic_start
    topic["slice_end"] = topic_end
    topic["slice_anchor"] = int(anchor if anchor is not None else (topic_start + topic_end) // 2)
    topic["slice_anchor_source"] = source
    topic["can_slice"] = True
    return topic


def _apply_reviewed_slice_decisions(topics, peaks, avg_density, max_per_hour=None):
    """让通过 Terra 的候选按最终语义边界落片，峰值只保留为发现来源。"""
    high_energy_peaks = _high_energy_danmaku_peaks(peaks, avg_density)
    peak_features = {
        int(peak_start): _danmaku_peak_features(
            peaks,
            peak_start,
            density,
            avg_density=avg_density,
        )
        for peak_start, density in high_energy_peaks
    }
    peak_rows = []
    non_peak_rows = []
    for topic in topics:
        topic["can_slice"] = False
        for key in (
            "slice_start", "slice_end", "slice_anchor", "slice_anchor_source",
            "slice_peak_density",
        ):
            topic.pop(key, None)
        if not _is_content_cuttable_topic(topic) or not _reviewed_topic_has_required_interest(topic):
            continue

        peak_candidates = _topic_peak_candidates(topic, high_energy_peaks)
        sources = [
            str(value).strip()
            for value in topic.get("clip_candidate_sources") or []
            if str(value).strip()
        ]
        # 兼容外部脚本和既有测试直接提交的、已经通过复核的峰值候选。
        if peak_candidates and not sources:
            sources = ["弹幕峰值"]
            topic["clip_candidate_sources"] = sources
        if not sources:
            continue

        if peak_candidates:
            peak_start, density = max(peak_candidates, key=lambda item: item[1])
            features = peak_features[int(peak_start)]
            ranking_score = (
                _reviewed_danmaku_ranking_score(features)
                if features.get("content_evidence")
                else float(density)
            )
            peak_rows.append({
                "topic": topic,
                "peak_start": int(peak_start),
                "density": float(density),
                "ranking_score": ranking_score,
                "anchor": int(peak_start + DANMAKU_WINDOW / 2),
            })
        elif "人工高星时间轴" in sources:
            non_peak_rows.append((
                topic,
                "人工高星时间轴",
                _manual_review_anchor(topic),
            ))
        else:
            non_peak_rows.append((
                topic,
                "语义复核",
                None,
            ))

    # 同一峰值只能对应一个片段。复核后更重绝对热度，以免局部突增把真正
    # 的全场极高峰挤掉；这也保留了旧接口传入 max_per_hour 时的兼容行为。
    peak_rows.sort(key=lambda row: (
        -row["ranking_score"],
        -(_parse_clip_interest_score(row["topic"].get("clip_interest_score")) or 0),
        row["topic"].get("start", 0),
    ))
    used_peak_starts = set()
    selected_per_hour = defaultdict(int)
    for row in peak_rows:
        topic = row["topic"]
        peak_start = row["peak_start"]
        hour = max(0, int(row["anchor"] // 3600))
        if peak_start in used_peak_starts:
            continue
        if max_per_hour is not None and selected_per_hour[hour] >= max_per_hour:
            continue
        topic["peak_density"] = row["density"]
        topic["density_ratio"] = round(row["density"] / avg_density, 2) if avg_density else 0
        _assign_reviewed_semantic_slice_window(topic, "弹幕峰值", anchor=row["anchor"])
        used_peak_starts.add(peak_start)
        if max_per_hour is not None:
            selected_per_hour[hour] += 1

    for topic, source, anchor in sorted(
            non_peak_rows,
            key=lambda row: (
                -(_parse_clip_interest_score(row[0].get("clip_interest_score")) or 0),
                row[0].get("start", 0),
            )):
        effective_anchor = int(anchor if anchor is not None else (
            int(topic.get("start", 0)) + int(topic.get("end", 0))
        ) // 2)
        hour = max(0, int(effective_anchor // 3600))
        if max_per_hour is not None and selected_per_hour[hour] >= max_per_hour:
            continue
        _assign_reviewed_semantic_slice_window(topic, source, anchor=effective_anchor)
        if max_per_hour is not None:
            selected_per_hour[hour] += 1
    return topics


def _apply_danmaku_slice_decisions(
        topics, peaks, avg_density, max_per_hour=None,
        require_clip_review=False):
    """按独立局部峰值筛选话题；生产路径不设小时配额。"""
    if not topics:
        return []
    if require_clip_review:
        return _apply_reviewed_slice_decisions(
            topics,
            peaks,
            avg_density,
            max_per_hour=max_per_hour,
        )
    high_energy_peaks = _high_energy_danmaku_peaks(peaks, avg_density)
    peak_features = {
        int(peak_start): _danmaku_peak_features(
            peaks,
            peak_start,
            density,
            avg_density=avg_density,
        )
        for peak_start, density in high_energy_peaks
    }
    candidates = []
    for topic in topics:
        topic["can_slice"] = False
        topic.pop("clip_review_candidate", None)
        topic.pop("clip_candidate_sources", None)
        for key in (
            "slice_start", "slice_end", "slice_anchor", "slice_anchor_source",
            "slice_peak_density", "danmaku_peak_start", "danmaku_selection_score",
            "danmaku_local_baseline", "danmaku_local_surge_ratio",
            "danmaku_density_percentile", "danmaku_content_quality",
            "danmaku_interaction_signal", "danmaku_topic_alignment",
            "danmaku_content_evidence",
        ):
            topic.pop(key, None)
        _refresh_topic_danmaku_evidence(topic, high_energy_peaks)
        peak_candidates = _topic_peak_candidates(topic, high_energy_peaks)
        best_peak = max(
            peak_candidates,
            key=lambda item: (
                peak_features[int(item[0])]["selection_score"]
                if peak_features[int(item[0])]["content_evidence"]
                else float(item[1])
            ),
        ) if peak_candidates else None
        peak_density = float(best_peak[1]) if best_peak else 0.0
        topic["peak_density"] = peak_density
        topic["density_ratio"] = round(peak_density / avg_density, 2) if avg_density else 0
        if not best_peak or not _is_content_cuttable_topic(topic):
            continue
        if require_clip_review and not topic.get("clip_review_validated"):
            continue
        if topic["end"] <= topic["start"]:
            continue
        peak_start, density = best_peak
        features = peak_features[int(peak_start)]
        alignment = _danmaku_topic_alignment(
            topic,
            features.get("content_evidence"),
        )
        anchor = int(peak_start + DANMAKU_WINDOW / 2)
        topic["danmaku_peak_start"] = int(peak_start)
        topic["danmaku_selection_score"] = features["selection_score"]
        topic["danmaku_local_baseline"] = features["local_baseline"]
        topic["danmaku_local_surge_ratio"] = features["local_surge_ratio"]
        topic["danmaku_density_percentile"] = features["density_percentile"]
        topic["danmaku_content_quality"] = features["content_quality"]
        topic["danmaku_interaction_signal"] = features["interaction_signal"]
        topic["danmaku_topic_alignment"] = alignment
        topic["danmaku_content_evidence"] = features["content_evidence"]
        if not features["content_evidence"]:
            ranking_score = float(density)
        elif require_clip_review:
            ranking_score = _reviewed_danmaku_ranking_score(features)
        else:
            ranking_score = features["selection_score"]
        candidates.append({
            "topic": topic,
            "peak_start": int(peak_start),
            "density": float(density),
            "anchor": anchor,
            "ranking_score": ranking_score,
            "alignment": alignment,
        })

    # 不同峰值按局部突增和内容质量排序；同一峰值优先匹配弹幕原文的话题。
    candidates.sort(key=lambda row: (
        -row["ranking_score"],
        -row["alignment"],
        -int(bool(row["topic"].get("ai_focus_validated"))),
        row["topic"]["end"] - row["topic"]["start"],
        row["topic"]["start"],
    ))
    used_peak_starts = set()
    selected_per_hour = defaultdict(int)
    for candidate in candidates:
        topic = candidate["topic"]
        peak_start = candidate["peak_start"]
        anchor = candidate["anchor"]
        hour = max(0, int(anchor // 3600))
        if peak_start in used_peak_starts:
            continue
        if max_per_hour is not None and selected_per_hour[hour] >= max_per_hour:
            continue
        topic["can_slice"] = True
        _append_clip_candidate_source(topic, "弹幕峰值")
        _assign_topic_slice_window(topic, [(peak_start, topic["peak_density"])])
        if not topic.get("can_slice") or topic.get("slice_anchor_source") != "弹幕峰值":
            topic["can_slice"] = False
            continue
        used_peak_starts.add(peak_start)
        if max_per_hour is not None:
            selected_per_hour[hour] += 1

    # 高星人工时间轴只增加独立字幕复核候选，不在这一阶段直接切片。
    # 这让低密度但有完整事件的片段有一次机会，同时避免按星标机械凑片。
    for topic in topics:
        if _is_content_cuttable_topic(topic) and _has_high_star_manual_evidence(topic):
            _append_clip_candidate_source(topic, "人工高星时间轴")
    return topics


def _clip_marks_from_topics(topics):
    """根据已筛选的重点话题生成 clip_marks。"""
    topic_list = list(topics or [])
    marks = []
    for topic in topic_list:
        if not (
                topic.get("can_slice")
                and topic.get("slice_anchor") is not None
                and topic.get("slice_anchor_source") in {
                    "弹幕峰值", "语义复核", "人工高星时间轴",
                }):
            continue
        next_topic_starts = [
            int(other.get("start", 0))
            for other in topic_list
            if (
                other is not topic
                and int(other.get("start", 0)) > int(topic.get("start", 0))
                and int(other.get("start", 0)) >= int(topic.get("end", 0)) - 5
            )
        ]
        marks.append({
            "start": topic.get("slice_start", topic["start"]),
            "end": topic.get("slice_end", topic["end"]),
            "title": topic["title"],
            "publish_title": _sanitize_transport_claims(
                _normalise_publish_title(
                    topic.get("publish_title"), topic["title"]
                ),
                topic.get("body") or [],
            ),
            **({"title_hook": topic["title_hook"]} if topic.get("title_hook") else {}),
            "report_start": topic["start"],
            "report_end": topic["end"],
            "slice_anchor": topic.get("slice_anchor"),
            "slice_anchor_source": topic.get("slice_anchor_source"),
            "clip_candidate_sources": list(topic.get("clip_candidate_sources") or []),
            "semantic_focus_validated": bool(topic.get("ai_focus_validated")),
            "editorial_interest_score": topic.get("clip_interest_score"),
            "editorial_interest_reason": topic.get("clip_interest_reason"),
            "timeline_star_bonus": topic.get("clip_timeline_star_bonus", 0),
            "reference_start": topic.get("reference_start"),
            "reference_end": topic.get("reference_end"),
            "context_requires_trigger": _clip_context_requires_trigger(topic),
            "boundary_evidence": list(topic.get("body") or []),
            "next_report_topic_start": min(next_topic_starts) if next_topic_starts else None,
        })
    return _dedupe_clip_marks(marks)


_topic_analysis_prompt_fingerprint = partial(
    checkpoint_store.topic_analysis_prompt_fingerprint,
    schema_version=TOPIC_ANALYSIS_CHECKPOINT_VERSION,
    model=LLM_ANALYSIS_MODEL,
    max_tokens=LLM_MAX_TOKENS,
    compact_max_tokens=LLM_COMPACT_MAX_TOKENS,
)


_load_topic_analysis_checkpoint = partial(
    checkpoint_store.load_topic_analysis_checkpoint,
    schema_version=TOPIC_ANALYSIS_CHECKPOINT_VERSION,
)


_write_topic_analysis_checkpoint = partial(
    checkpoint_store.write_topic_analysis_checkpoint,
    schema_version=TOPIC_ANALYSIS_CHECKPOINT_VERSION,
    model=LLM_ANALYSIS_MODEL,
)


def analyze_topic_chunks(
        chunks, streamer_display_name, progress_callback=None,
        checkpoint_path=None):
    """逐块独立分析字幕和弹幕；请求并行，结果仍按视频顺序合并。"""
    if not chunks:
        return [], [], None

    total = len(chunks)
    report_progress = llm_execution.serialized_progress_callback(progress_callback)
    stored_responses = _load_topic_analysis_checkpoint(checkpoint_path)
    active_checkpoint_responses = {}
    prepared_chunks = []
    outcomes = {}
    pending = []

    for index, chunk in enumerate(chunks):
        prompt, chunk_start, chunk_end = _build_chunk_prompt(
            chunk,
            index,
            total,
            compact=False,
            streamer_name=streamer_display_name,
        )
        compact_prompt, _, _ = _build_chunk_prompt(
            chunk,
            index,
            total,
            compact=True,
            streamer_name=streamer_display_name,
        )
        fingerprint = _topic_analysis_prompt_fingerprint(prompt, compact_prompt)
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
                and _extract_json_payload(cached["response"]) is not None):
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
            message = _short_llm_error(exc)
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
                thread_name_prefix="autoslice-llm") as executor:
            # 最高推理强度下，先用一个分块确认上游可用，再扩展到配置并发。
            # 否则服务短暂 503 时，首批多个请求会被误判成多个独立分块失败。
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
                        if isinstance(exc, LLMProviderUnavailableError):
                            for active_future in active_futures:
                                active_future.cancel()
                            raise RuntimeError(
                                "LLM 上游推理服务持续不可用，已暂停本次分析；"
                                "已完成的检查点会保留，稍后直接重试即可。"
                            ) from exc
                        outcomes[index] = {"error": exc}
                        short_error = _short_llm_error(exc)
                        consecutive_failed_chunks = (
                            consecutive_failed_chunks + 1
                            if _is_retryable_llm_error(exc)
                            else MAX_INITIAL_FAILED_CHUNKS
                        )
                        if report_progress:
                            report_progress(
                                f"块 {index + 1} API 连续失败，已跳过: {short_error}",
                                prepared["pct"],
                                100,
                            )
                    else:
                        outcomes[index] = {"response": response, "cached": False}
                        successful_response_count += 1
                        consecutive_failed_chunks = 0
                        active_checkpoint_responses[str(index + 1)] = {
                            "fingerprint": prepared["fingerprint"],
                            "response": response,
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        }
                        checkpoint_saved = _write_topic_analysis_checkpoint(
                            checkpoint_path,
                            active_checkpoint_responses,
                            total,
                        )
                        if (
                                not checkpoint_saved
                                and report_progress
                                and not checkpoint_warning_reported):
                            checkpoint_warning_reported = True
                            report_progress(
                                "首轮分析检查点写入失败，本次分析继续；请检查目录权限",
                                prepared["pct"],
                                100,
                            )
                    completed_pending += 1
                    if report_progress:
                        report_progress(
                            f"Step 4/5: LLM分析完成 "
                            f"({cached_count + completed_pending}/{total}，"
                            f"第 {index + 1} 块)",
                            25 + int(((cached_count + completed_pending) / total) * 68),
                            100,
                        )

                if (
                        consecutive_failed_chunks >= MAX_INITIAL_FAILED_CHUNKS
                        and successful_response_count == 0):
                    for future in active_futures:
                        future.cancel()
                    raise RuntimeError(
                        f"LLM API 连续 {consecutive_failed_chunks} 个分块失败，"
                        "疑似上游服务不可用。"
                    )

                # 首次连败时不补位，确保上游全挂只发送首批请求；已有任一成功后持续补满。
                if successful_response_count > 0 or not active_futures:
                    while len(active_futures) < concurrency and submit_next(executor):
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
            failed_chunks.append({
                "index": index + 1,
                "start": int(chunk_start),
                "end": int(chunk_end),
                "time": timecode.format_elapsed(chunk_start),
                "error": _short_llm_error(error),
            })
            fallback_topic = _make_fallback_topic_from_chunk(
                chunk,
                streamer_name=streamer_display_name,
            )
            if fallback_topic and not _is_duplicate_topic(fallback_topic, accepted_topics):
                accepted_topics.append(fallback_topic)
            continue

        before_topic_count = len(accepted_topics)
        _parse_llm_response(
            outcome["response"],
            chunk_start,
            chunk_end,
            accepted_topics,
            allow_markdown_fallback=False,
        )
        if len(accepted_topics) == before_topic_count:
            fallback_topic = _make_fallback_topic_from_chunk(
                chunk,
                streamer_name=streamer_display_name,
            )
            if fallback_topic and not _is_duplicate_topic(fallback_topic, accepted_topics):
                accepted_topics.append(fallback_topic)

    return accepted_topics, failed_chunks, None


_TOPIC_REVIEW_TRANSIENT_KEYS = checkpoint_store.TOPIC_REVIEW_TRANSIENT_KEYS


_analysis_topics_snapshot = checkpoint_store.analysis_topics_snapshot


write_clip_review_checkpoint = checkpoint_store.write_clip_review_checkpoint


_clip_review_checkpoint_matches_policy = (
    checkpoint_store.clip_review_checkpoint_matches_policy
)


_clip_review_checkpoint_is_complete = (
    checkpoint_store.clip_review_checkpoint_is_complete
)


_write_completed_clip_review_checkpoint = (
    checkpoint_store.write_completed_clip_review_checkpoint
)


def _review_peak_selected_topics(
        topics, srt_segments, peaks, streamer_name=None, progress_callback=None,
        checkpoint_callback=None, resume=False):
    """对峰值候选做独立字幕复核；缺项会逐步缩小批次重试。"""
    if resume:
        selected = [
            topic for topic in topics
            if (
                (topic.get("can_slice") or topic.get("clip_review_candidate"))
                and not topic.get("clip_review_validated")
                and topic.get("clip_review_rejection") == "等待独立字幕复核"
            )
        ]
    else:
        selected = [
            topic for topic in topics
            if topic.get("can_slice") or topic.get("clip_review_candidate")
        ]
    if not selected:
        return None
    high_energy_peaks = _high_energy_danmaku_peaks(
        peaks,
        _average_danmaku_density(peaks),
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
            (CLIP_REVIEW_RETRY_BATCH_SIZE, "检查点补充"),
            (1, "检查点逐项兜底"),
        )
        if resume else (
            (CLIP_REVIEW_BATCH_SIZE, "首轮"),
            (CLIP_REVIEW_RETRY_BATCH_SIZE, "缺项补充"),
            (1, "逐项兜底"),
        )
    )
    for batch_size, round_label in review_rounds:
        if not unresolved:
            break
        retry_items = []
        total_batches = math.ceil(len(unresolved) / batch_size)
        jobs = []
        for batch_index, offset in enumerate(range(0, len(unresolved), batch_size), 1):
            originals = unresolved[offset:offset + batch_size]
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
            jobs.append({
                "batch_index": batch_index,
                "originals": originals,
                "candidates": candidates,
                "prompt": prompt,
                "compact_prompt": compact_prompt,
            })

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
                thread_name_prefix="autoslice-review") as executor:
            futures = [executor.submit(review_job, job) for job in jobs]
            for job, future in zip(jobs, futures):
                batch_index = job["batch_index"]
                originals = job["originals"]
                candidates = job["candidates"]
                try:
                    response = future.result()
                except Exception as exc:
                    error = f"API复核失败：{_short_llm_error(exc)}"
                    for original in originals:
                        last_errors[id(original)] = error
                        retry_items.append(original)
                else:
                    response_payload = _extract_json_payload(response)
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
                        if 1 <= item_id <= len(candidates) and item_id not in items_by_id:
                            items_by_id[item_id] = item

                    for item_id, (original, candidate) in enumerate(
                            zip(originals, candidates), 1):
                        item = items_by_id.get(item_id)
                        if item is None or "valid" not in item:
                            last_errors[id(original)] = "模型未返回该候选的有效结构"
                            retry_items.append(original)
                            continue
                        base_interest_score = _parse_clip_interest_score(
                            item.get("base_interest_score")
                        )
                        timeline_star_bonus = _parse_clip_star_bonus(
                            item.get("timeline_star_bonus")
                        )
                        manual_star_count = max(
                            0,
                            int(candidate.get("manual_stars", 0) or 0),
                        )
                        timeline_star_bonus_cap = _clip_star_bonus_cap(
                            manual_star_count
                        )
                        interest_reason = _clip_interest_reason(item)
                        if not response_parsing.json_can_slice(
                            item.get("valid"),
                            "",
                        ):
                            original["clip_review_validated"] = False
                            original["clip_review_rejection"] = str(
                                item.get("reason", "字幕证据不足")
                            ).strip() or "字幕证据不足"
                            if base_interest_score is not None:
                                original["clip_interest_base_score"] = base_interest_score
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
                        interest_score = round(min(
                            100.0,
                            base_interest_score + timeline_star_bonus,
                        ), 1)
                        if interest_score < CLIP_MIN_INTEREST_SCORE:
                            original["clip_review_validated"] = False
                            original["clip_interest_base_score"] = base_interest_score
                            original["clip_timeline_star_bonus"] = timeline_star_bonus
                            original["clip_interest_score"] = interest_score
                            original["clip_interest_reason"] = interest_reason
                            detail = interest_reason or "内容完整但投稿钩子或反应强度不足"
                            original["clip_review_rejection"] = (
                                f"投稿价值 {interest_score:g} 分，低于 "
                                f"{CLIP_MIN_INTEREST_SCORE} 分：{detail}"
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
                        enriched["clip_review_attempts"] = original["clip_review_attempts"]
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
            id(original), "独立字幕复核未完成"
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


def _validate_unmatched_manual_topics(
        topics, streamer_name=None, progress_callback=None,
        srt_segments=None, peaks=None):
    """后置复核首轮遗漏的时间轴候选；失败时只保留报告线索。"""
    manual_topics = [
        topic for topic in topics
        if topic.get("source") in {"manual_timeline", "optimized_manual_timeline"}
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
        batch_size=MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE,
        progress_start=94,
        progress_end=94,
        progress_label="人工时间轴补充项复核",
    )

    topics[:] = [topic for topic in topics if id(topic) not in original_ids]
    topics.extend(manual_topics)
    topics.sort(key=lambda item: (item["start"], item["end"]))
    if warning:
        return (
            "人工时间轴补充项部分复核失败；未核验条目仅写入报告且不会自动切片："
            f"{warning}"
        )
    return None
