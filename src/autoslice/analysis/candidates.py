"""候选发现、证据整理与投稿价值复核的唯一实现。"""

from __future__ import annotations

import re
from collections import defaultdict

from autoslice.analysis import boundaries as boundary_analysis
from autoslice.analysis import candidate_reconciliation
from autoslice.analysis import checkpoints as checkpoint_store
from autoslice.analysis import chunking
from autoslice.analysis import clip_scoring
from autoslice.analysis import clip_policy
from autoslice.analysis import clip_review
from autoslice.analysis import clip_review_candidates
from autoslice.analysis import clip_review_prompt
from autoslice.analysis import content_normalization
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis import evidence as candidate_evidence
from autoslice.analysis import llm_execution
from autoslice.analysis import manual_enrichment
from autoslice.analysis import manual_candidates
from autoslice.analysis import manual_review
from autoslice.analysis import response_parsing
from autoslice.analysis import timeline as timeline_analysis
from autoslice.analysis import topic_analysis
from autoslice.analysis import topic_formatting
from autoslice.analysis import titles as title_analysis
from autoslice import timecode
from autoslice.llm import transport as llm_gateway
from autoslice.transcription import service as transcription_service
from autoslice.transcription import segments as transcription_segments
from autoslice.transcription import srt_io as transcription_srt_io


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
    '_manual_evidence_line': '_manual_evidence_line',
    '_manual_review_anchor': '_manual_review_anchor',
    '_merge_manual_timeline_topics': 'merge_manual_timeline_topics',
    '_optimized_entry_semantic_text': '_optimized_entry_semantic_text',
    '_parse_json_topics_response': '_parse_json_topics_response',
    '_parse_llm_response': '_parse_llm_response',
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

_topic_semantic_text = candidate_reconciliation.topic_semantic_text
_danmaku_topic_alignment = candidate_reconciliation.danmaku_topic_alignment
_manual_entry_meaningfully_overlaps_topic = (
    candidate_reconciliation.manual_entry_meaningfully_overlaps_topic
)
_reconcile_topic_manual_evidence = (
    candidate_reconciliation.reconcile_topic_manual_evidence
)


_clip_review_candidate = clip_review_candidates.build_clip_review_candidate
_fresh_manual_topic_evidence = clip_review_candidates.fresh_manual_topic_evidence
_build_clip_candidate_review_prompt = (
    clip_review_prompt.build_clip_candidate_review_prompt
)
_review_peak_selected_topics = clip_review.review_peak_selected_topics

_UNCUTTABLE_CONTENT_KEYWORDS = clip_policy.UNCUTTABLE_CONTENT_KEYWORDS
_manual_entry_matches_topic = manual_candidates.manual_entry_matches_topic
_is_manual_merge_target = manual_candidates.is_manual_merge_target
merge_manual_timeline_topics = manual_candidates.merge_manual_timeline_topics
_topics_from_manual_timeline = manual_candidates.topics_from_manual_timeline
_optimized_entry_semantic_text = manual_candidates.optimized_entry_semantic_text
_manual_evidence_line = manual_candidates.manual_evidence_line
_sanitize_optimized_manual_entry = (
    manual_candidates.sanitize_optimized_manual_entry
)


_build_manual_topic_enrichment_prompt = (
    manual_review.build_manual_topic_enrichment_prompt
)
enrich_manual_topics_with_llm = manual_review.enrich_manual_topics_with_llm
enrich_manual_topics_in_batches = manual_review.enrich_manual_topics_in_batches
_validate_unmatched_manual_topics = manual_review.validate_unmatched_manual_topics


_CIRCLED_NUMBERS = topic_formatting.CIRCLED_NUMBERS
_format_report_time = topic_formatting.format_report_time
_format_topic_block = topic_formatting.format_topic_block
_topic_index_label = topic_formatting.topic_index_label


CHUNK_SEC = topic_analysis.CHUNK_SEC
LLM_ANALYSIS_MODEL = topic_analysis.LLM_ANALYSIS_MODEL
LLM_MAX_TOKENS = topic_analysis.LLM_MAX_TOKENS
LLM_COMPACT_MAX_TOKENS = topic_analysis.LLM_COMPACT_MAX_TOKENS
LLM_FULL_TEXT_CHARS = topic_analysis.LLM_FULL_TEXT_CHARS
LLM_COMPACT_TEXT_CHARS = topic_analysis.LLM_COMPACT_TEXT_CHARS
MAX_INITIAL_FAILED_CHUNKS = topic_analysis.MAX_INITIAL_FAILED_CHUNKS
TOPIC_ANALYSIS_CHECKPOINT_VERSION = (
    topic_analysis.TOPIC_ANALYSIS_CHECKPOINT_VERSION
)
_HEADING_RE = topic_analysis.HEADING_RE
_repair_short_topic_end = topic_analysis.repair_short_topic_end
_build_chunk_prompt = topic_analysis.build_chunk_prompt
_strip_code_fence = topic_analysis.strip_code_fence
_is_topic_in_chunk = topic_analysis.is_topic_in_chunk
_parse_json_topics_response = topic_analysis.parse_json_topics_response
_parse_llm_response = topic_analysis.parse_llm_response
_strip_prompt_time_labels = topic_analysis.strip_prompt_time_labels
_make_fallback_topic_from_chunk = topic_analysis.make_fallback_topic_from_chunk
_topic_analysis_prompt_fingerprint = (
    topic_analysis.topic_analysis_prompt_fingerprint
)
_load_topic_analysis_checkpoint = topic_analysis.load_topic_analysis_checkpoint
_write_topic_analysis_checkpoint = topic_analysis.write_topic_analysis_checkpoint
analyze_topic_chunks = topic_analysis.analyze_topic_chunks
_analyze_topic_chunks = topic_analysis.analyze_topic_chunks


parse_srt_text = chunking.parse_srt_text
chunk_srt = chunking.chunk_srt
_make_chunk = chunking.make_chunk


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




































_overlap_ratio = boundary_analysis._overlap_ratio
_is_duplicate_topic = boundary_analysis._is_duplicate_topic






















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
    if any(
        keyword in compact
        for keyword in clip_policy.UNCUTTABLE_CONTENT_KEYWORDS
    ):
        return False
    return True


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
    fixed = candidate_reconciliation.reconcile_topic_manual_evidence(fixed)

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
        topic = candidate_reconciliation.reconcile_topic_manual_evidence(topic)
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
        alignment = candidate_reconciliation.danmaku_topic_alignment(
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
