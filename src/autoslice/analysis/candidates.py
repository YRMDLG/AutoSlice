"""候选发现、证据整理与投稿价值复核的唯一实现。"""

from __future__ import annotations

from autoslice.analysis import boundaries as boundary_analysis
from autoslice.analysis import checkpoints as checkpoint_store
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis import evidence as candidate_evidence
from autoslice.analysis import llm_execution
from autoslice.analysis.manual import candidates as manual_candidates
from autoslice.analysis.manual import enrichment as manual_enrichment
from autoslice.analysis.manual import review as manual_review
from autoslice.analysis.manual import timebase as timeline_analysis
from autoslice.analysis.report import cleanup as report_cleanup
from autoslice.analysis.report import formatting as topic_formatting
from autoslice.analysis.review import candidates as clip_review_candidates
from autoslice.analysis.review import policy as clip_policy
from autoslice.analysis.review import prompt as clip_review_prompt
from autoslice.analysis.review import reconciliation as candidate_reconciliation
from autoslice.analysis.review import scoring as clip_scoring
from autoslice.analysis.review import workflow as clip_review
from autoslice.analysis import slice_decisions
from autoslice.analysis.topic import analysis as topic_analysis
from autoslice.analysis.topic import chunking
from autoslice.analysis.topic import titles as title_analysis
from autoslice.analysis.topic import normalization, response
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
    '_HEADING_RE': '_HEADING_RE',
    '_MANUAL_AI_PLACEHOLDER_PHRASES': '_MANUAL_AI_PLACEHOLDER_PHRASES',
    '_analyze_topic_chunks': 'analyze_topic_chunks',
    '_build_chunk_prompt': '_build_chunk_prompt',
    '_build_clip_candidate_review_prompt': '_build_clip_candidate_review_prompt',
    '_build_manual_topic_enrichment_prompt': '_build_manual_topic_enrichment_prompt',
    '_clip_review_candidate': '_clip_review_candidate',
    '_enrich_manual_topics_in_batches': 'enrich_manual_topics_in_batches',
    '_enrich_manual_topics_with_llm': 'enrich_manual_topics_with_llm',
    '_enriched_manual_topic_from_item': '_enriched_manual_topic_from_item',
    '_extract_json_payload': '_extract_json_payload',
    '_fresh_manual_topic_evidence': '_fresh_manual_topic_evidence',
    '_is_manual_ai_placeholder': '_is_manual_ai_placeholder',
    '_is_manual_merge_target': '_is_manual_merge_target',
    '_is_retryable_llm_error': '_is_retryable_llm_error',
    '_is_topic_in_chunk': '_is_topic_in_chunk',
    '_load_topic_analysis_checkpoint': '_load_topic_analysis_checkpoint',
    '_make_chunk': '_make_chunk',
    '_make_fallback_topic_from_chunk': '_make_fallback_topic_from_chunk',
    '_manual_entry_matches_topic': '_manual_entry_matches_topic',
    '_manual_evidence_line': '_manual_evidence_line',
    '_merge_manual_timeline_topics': 'merge_manual_timeline_topics',
    '_optimized_entry_semantic_text': '_optimized_entry_semantic_text',
    '_parse_json_topics_response': '_parse_json_topics_response',
    '_parse_llm_response': '_parse_llm_response',
    '_repair_short_topic_end': '_repair_short_topic_end',
    '_review_peak_selected_topics': '_review_peak_selected_topics',
    '_sanitize_optimized_manual_entry': '_sanitize_optimized_manual_entry',
    '_short_llm_error': '_short_llm_error',
    '_strip_code_fence': '_strip_code_fence',
    '_strip_prompt_time_labels': '_strip_prompt_time_labels',
    '_topic_analysis_prompt_fingerprint': '_topic_analysis_prompt_fingerprint',
    '_topics_from_manual_timeline': '_topics_from_manual_timeline',
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


_NO_SLICE_HINTS = response.NO_SLICE_HINTS
_is_slice_marked = response.is_slice_marked
_json_can_slice = response.json_can_slice


_DANMAKU_META_KEYWORDS = normalization.DANMAKU_META_KEYWORDS
_FRAGMENT_BODY_LINES = normalization.FRAGMENT_BODY_LINES
_META_BODY_KEYWORDS = normalization.META_BODY_KEYWORDS
_UNSUPPORTED_AI_AUDIENCE_REACTION_RE = (
    normalization.UNSUPPORTED_AI_AUDIENCE_REACTION_RE
)
_clean_body_content = normalization.clean_body_content
_filter_unsupported_ai_points = normalization.filter_unsupported_ai_points
_is_meta_body_line = normalization.is_meta_body_line
_json_points_to_body = normalization.json_points_to_body
_normalise_body_line = normalization.normalise_body_line


_clean_topics_for_report = report_cleanup.clean_topics_for_report
_report_fact_lines = report_cleanup.report_fact_lines
_resolve_reviewed_report_overlaps = report_cleanup.resolve_reviewed_report_overlaps
_trim_report_topic_around_reviewed_topic = (
    report_cleanup.trim_report_topic_around_reviewed_topic
)


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


_manual_alignment_score = timeline_analysis.manual_alignment_score


_manual_text_supports_candidate = timeline_analysis.manual_text_supports_candidate


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


_topic_peak_focus_window = slice_decisions.topic_peak_focus_window
_assign_topic_slice_window = slice_decisions.assign_topic_slice_window
_is_content_cuttable_topic = slice_decisions.is_content_cuttable_topic
_refresh_topic_danmaku_evidence = slice_decisions.refresh_topic_danmaku_evidence
_append_clip_candidate_source = slice_decisions.append_clip_candidate_source
_has_high_star_manual_evidence = slice_decisions.has_high_star_manual_evidence
_manual_review_anchor = slice_decisions.manual_review_anchor
_reviewed_topic_has_required_interest = (
    slice_decisions.reviewed_topic_has_required_interest
)
_assign_reviewed_semantic_slice_window = (
    slice_decisions.assign_reviewed_semantic_slice_window
)
_apply_reviewed_slice_decisions = slice_decisions.apply_reviewed_slice_decisions
_apply_danmaku_slice_decisions = slice_decisions.apply_danmaku_slice_decisions
_clip_marks_from_topics = slice_decisions.clip_marks_from_topics


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
