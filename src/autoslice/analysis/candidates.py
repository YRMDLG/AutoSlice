"""候选发现、价值复核、上下文边界与不重叠决策的唯一实现。"""

from __future__ import annotations

import bisect
import difflib
import html
import json
import math
import os
import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta
from functools import partial

from autoslice.analysis import checkpoints as checkpoint_store
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis import timeline as timeline_analysis
from autoslice.analysis import titles as title_analysis
from autoslice.llm import transport as llm_gateway
from autoslice.llm.prompts import (
    ClipCandidatePromptEvidence as _ClipCandidatePromptEvidence,
    ManualTopicPromptEvidence as _ManualTopicPromptEvidence,
    SYSTEM_PROMPT,
    TITLE_HOOK_PROMPT_GUIDE,
    TopicAnalysisPromptEvidence as _TopicAnalysisPromptEvidence,
    build_clip_candidate_review_prompt as _render_clip_candidate_review_prompt,
    build_manual_topic_enrichment_prompt as _render_manual_topic_enrichment_prompt,
    build_system_prompt as _render_system_prompt,
    build_title_hook_guide as _render_title_hook_guide,
    build_topic_analysis_prompt as _render_topic_analysis_prompt,
)
from autoslice.transcription import service as transcription_service
from autoslice.transcription.contracts import (
    DEFAULT_SUBTITLE_GLOSSARY,
    DEFAULT_SUBTITLE_MAX_CHARS,
    SubtitleTitleServices,
)
from autoslice.streamer_profiles import (
    current_streamer_profile,
    streamer_profile_context,
)


FACADE_EXPORTS = {
    'CHUNK_SEC': 'CHUNK_SEC',
    'CLIP_MANUAL_REVIEW_MIN_STARS': 'CLIP_MANUAL_REVIEW_MIN_STARS',
    'CLIP_MIN_INTEREST_SCORE': 'CLIP_MIN_INTEREST_SCORE',
    'CLIP_REVIEW_BATCH_SIZE': 'CLIP_REVIEW_BATCH_SIZE',
    'CLIP_REVIEW_RETRY_BATCH_SIZE': 'CLIP_REVIEW_RETRY_BATCH_SIZE',
    'LLMProviderUnavailableError': 'LLMProviderUnavailableError',
    'LLMStructuredOutputError': 'LLMStructuredOutputError',
    'LLM_ANALYSIS_MODEL': 'LLM_ANALYSIS_MODEL',
    'LLM_COMPACT_MAX_TOKENS': 'LLM_COMPACT_MAX_TOKENS',
    'LLM_COMPACT_TEXT_CHARS': 'LLM_COMPACT_TEXT_CHARS',
    'LLM_DEFAULT_CONCURRENCY': 'LLM_DEFAULT_CONCURRENCY',
    'LLM_FULL_TEXT_CHARS': 'LLM_FULL_TEXT_CHARS',
    'LLM_MAX_CONCURRENCY': 'LLM_MAX_CONCURRENCY',
    'LLM_MAX_TOKENS': 'LLM_MAX_TOKENS',
    'MAX_INITIAL_FAILED_CHUNKS': 'MAX_INITIAL_FAILED_CHUNKS',
    'OUTRO_TRIGGER_JOIN_GAP_SEC': 'OUTRO_TRIGGER_JOIN_GAP_SEC',
    'OUTRO_VARIANT_FAREWELL_AFTER_SEC': 'OUTRO_VARIANT_FAREWELL_AFTER_SEC',
    'OUTRO_VARIANT_FAREWELL_BEFORE_SEC': 'OUTRO_VARIANT_FAREWELL_BEFORE_SEC',
    'SC_CONTEXT_LOOKBACK_SEC': 'SC_CONTEXT_LOOKBACK_SEC',
    'SC_FALLBACK_GIFT_LOOKBACK_SEC': 'SC_FALLBACK_GIFT_LOOKBACK_SEC',
    'SC_TRIGGER_KEYWORDS': 'SC_TRIGGER_KEYWORDS',
    'THANKS_TRIGGER_RE': 'THANKS_TRIGGER_RE',
    'TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC': 'TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC',
    'TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC': 'TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC',
    'TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC': 'TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC',
    'TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC': 'TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC',
    'TOPIC_AI_FOCUS_POST_CONTEXT_SEC': 'TOPIC_AI_FOCUS_POST_CONTEXT_SEC',
    'TOPIC_AI_FOCUS_PRE_CONTEXT_SEC': 'TOPIC_AI_FOCUS_PRE_CONTEXT_SEC',
    'TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC': 'TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC',
    'TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC': 'TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC',
    'TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC': 'TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC',
    'TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE': 'TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE',
    'TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC': 'TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC',
    'TOPIC_DIRECT_SLICE_MAX_SEC': 'TOPIC_DIRECT_SLICE_MAX_SEC',
    'TOPIC_FOCUS_POST_SEC': 'TOPIC_FOCUS_POST_SEC',
    'TOPIC_FOCUS_PRE_SEC': 'TOPIC_FOCUS_PRE_SEC',
    'TOPIC_HARD_TRANSITION_GAP_SEC': 'TOPIC_HARD_TRANSITION_GAP_SEC',
    'TOPIC_LEAD_IN_LOOKBACK_SEC': 'TOPIC_LEAD_IN_LOOKBACK_SEC',
    'TOPIC_LEAD_IN_RECOVERY_MIN_SEC': 'TOPIC_LEAD_IN_RECOVERY_MIN_SEC',
    'TOPIC_MAX_CLIP_SEC': 'TOPIC_MAX_CLIP_SEC',
    'TOPIC_MAX_REPAIRED_REPORT_SEC': 'TOPIC_MAX_REPAIRED_REPORT_SEC',
    'TOPIC_MIN_CLIP_SEC': 'TOPIC_MIN_CLIP_SEC',
    'TOPIC_MIN_REPORT_SEC': 'TOPIC_MIN_REPORT_SEC',
    'TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC': 'TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC',
    'TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC': 'TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC',
    'TOPIC_POST_CONTEXT_SEC': 'TOPIC_POST_CONTEXT_SEC',
    'TOPIC_PRE_CONTEXT_SEC': 'TOPIC_PRE_CONTEXT_SEC',
    'TOPIC_REFERENCE_END_TOLERANCE_SEC': 'TOPIC_REFERENCE_END_TOLERANCE_SEC',
    'TOPIC_RELEVANT_CONTINUATION_GAP_SEC': 'TOPIC_RELEVANT_CONTINUATION_GAP_SEC',
    'TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC': 'TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC',
    'TOPIC_REVIEW_FOCUS_MAX_SEC': 'TOPIC_REVIEW_FOCUS_MAX_SEC',
    '_BOUNDARY_EVIDENCE_STOP_TERMS': '_BOUNDARY_EVIDENCE_STOP_TERMS',
    '_CIRCLED_NUMBERS': '_CIRCLED_NUMBERS',
    '_DANMAKU_META_KEYWORDS': '_DANMAKU_META_KEYWORDS',
    '_FRAGMENT_BODY_LINES': '_FRAGMENT_BODY_LINES',
    '_HEADING_RE': '_HEADING_RE',
    '_MANUAL_AI_PLACEHOLDER_PHRASES': '_MANUAL_AI_PLACEHOLDER_PHRASES',
    '_META_BODY_KEYWORDS': '_META_BODY_KEYWORDS',
    '_NEXT_CASE_ASR_TRIGGER_RE': '_NEXT_CASE_ASR_TRIGGER_RE',
    '_NO_SLICE_HINTS': '_NO_SLICE_HINTS',
    '_OUTRO_ACTIVITY_VARIANT_RE': '_OUTRO_ACTIVITY_VARIANT_RE',
    '_OUTRO_FAREWELL_EVIDENCE': '_OUTRO_FAREWELL_EVIDENCE',
    '_OUTRO_TRIGGER_NORMALISE_RE': '_OUTRO_TRIGGER_NORMALISE_RE',
    '_TOPIC_CONCLUSION_RE': '_TOPIC_CONCLUSION_RE',
    '_TOPIC_DECISION_EVIDENCE_RE': '_TOPIC_DECISION_EVIDENCE_RE',
    '_TOPIC_DISCOURSE_CONTINUATION_RE': '_TOPIC_DISCOURSE_CONTINUATION_RE',
    '_TOPIC_LEAD_IN_TRIGGER_RE': '_TOPIC_LEAD_IN_TRIGGER_RE',
    '_TOPIC_REFUND_RE': '_TOPIC_REFUND_RE',
    '_TRIGGER_CONTEXT_TOPIC_RE': '_TRIGGER_CONTEXT_TOPIC_RE',
    '_UNCUTTABLE_CONTENT_KEYWORDS': '_UNCUTTABLE_CONTENT_KEYWORDS',
    '_UNSUPPORTED_AI_AUDIENCE_REACTION_RE': '_UNSUPPORTED_AI_AUDIENCE_REACTION_RE',
    '_VISUAL_CASE_SHIFT_RE': '_VISUAL_CASE_SHIFT_RE',
    '_VISUAL_REACTION_LEAD_IN_RE': '_VISUAL_REACTION_LEAD_IN_RE',
    '_VISUAL_REVIEW_TOPIC_RE': '_VISUAL_REVIEW_TOPIC_RE',
    '_analyze_topic_chunks': 'analyze_topic_chunks',
    '_append_clip_candidate_source': '_append_clip_candidate_source',
    '_apply_danmaku_slice_decisions': '_apply_danmaku_slice_decisions',
    '_apply_reviewed_slice_decisions': '_apply_reviewed_slice_decisions',
    '_assign_reviewed_semantic_slice_window': '_assign_reviewed_semantic_slice_window',
    '_assign_topic_slice_window': '_assign_topic_slice_window',
    '_boundary_context_has_speech': '_boundary_context_has_speech',
    '_boundary_context_is_relevant': '_boundary_context_is_relevant',
    '_boundary_evidence_term_counts': '_boundary_evidence_term_counts',
    '_boundary_evidence_text_is_relevant': '_boundary_evidence_text_is_relevant',
    '_build_chunk_prompt': '_build_chunk_prompt',
    '_build_clip_candidate_review_audit': '_build_clip_candidate_review_audit',
    '_build_clip_candidate_review_prompt': '_build_clip_candidate_review_prompt',
    '_build_manual_topic_enrichment_prompt': '_build_manual_topic_enrichment_prompt',
    '_cap_expanded_clip_mark': '_cap_expanded_clip_mark',
    '_capped_speech_chain_start': '_capped_speech_chain_start',
    '_clean_body_content': '_clean_body_content',
    '_clean_topics_for_report': '_clean_topics_for_report',
    '_clip_context_requires_trigger': '_clip_context_requires_trigger',
    '_clip_interest_reason': '_clip_interest_reason',
    '_clip_manual_star_count': '_clip_manual_star_count',
    '_clip_marks_from_topics': '_clip_marks_from_topics',
    '_clip_review_candidate': '_clip_review_candidate',
    '_clip_star_bonus_cap': '_clip_star_bonus_cap',
    '_configured_llm_concurrency': '_configured_llm_concurrency',
    '_danmaku_topic_alignment': '_danmaku_topic_alignment',
    '_dedupe_clip_marks': '_dedupe_clip_marks',
    '_detect_stream_outro_clip': '_detect_stream_outro_clip',
    '_enrich_manual_topics_in_batches': 'enrich_manual_topics_in_batches',
    '_enrich_manual_topics_with_llm': 'enrich_manual_topics_with_llm',
    '_enriched_manual_topic_from_item': '_enriched_manual_topic_from_item',
    '_expand_clip_mark_with_context': '_expand_clip_mark_with_context',
    '_expand_clip_marks_with_context': '_expand_clip_marks_with_context',
    '_extract_json_payload': '_extract_json_payload',
    '_filter_unsupported_ai_points': '_filter_unsupported_ai_points',
    '_find_next_topic_hard_end': '_find_next_topic_hard_end',
    '_find_relevant_topic_context_end': '_find_relevant_topic_context_end',
    '_find_relevant_topic_context_start': '_find_relevant_topic_context_start',
    '_find_sc_context_start': '_find_sc_context_start',
    '_find_topic_lead_in_start': '_find_topic_lead_in_start',
    '_find_visual_reaction_context_start': '_find_visual_reaction_context_start',
    '_fit_final_clip_to_safe_srt_boundaries': '_fit_final_clip_to_safe_srt_boundaries',
    '_format_report_time': '_format_report_time',
    '_format_topic_block': '_format_topic_block',
    '_fresh_manual_topic_evidence': '_fresh_manual_topic_evidence',
    '_gift_trigger_has_question_followup': '_gift_trigger_has_question_followup',
    '_has_high_star_manual_evidence': '_has_high_star_manual_evidence',
    '_has_outro_farewell_evidence': '_has_outro_farewell_evidence',
    '_integer_clip_bounds_outside_subtitles': '_integer_clip_bounds_outside_subtitles',
    '_is_content_cuttable_topic': '_is_content_cuttable_topic',
    '_is_duplicate_topic': '_is_duplicate_topic',
    '_is_explicit_sc_topic': '_is_explicit_sc_topic',
    '_is_explicit_sc_trigger': '_is_explicit_sc_trigger',
    '_is_manual_ai_placeholder': '_is_manual_ai_placeholder',
    '_is_manual_merge_target': '_is_manual_merge_target',
    '_is_meta_body_line': '_is_meta_body_line',
    '_is_retryable_llm_error': '_is_retryable_llm_error',
    '_is_slice_marked': '_is_slice_marked',
    '_is_topic_in_chunk': '_is_topic_in_chunk',
    '_json_can_slice': '_json_can_slice',
    '_json_points_to_body': '_json_points_to_body',
    '_load_topic_analysis_checkpoint': '_load_topic_analysis_checkpoint',
    '_looks_like_delayed_topic_conclusion': '_looks_like_delayed_topic_conclusion',
    '_looks_like_discourse_continuation': '_looks_like_discourse_continuation',
    '_looks_like_low_score_visual_case_shift': '_looks_like_low_score_visual_case_shift',
    '_looks_like_next_case_transition': '_looks_like_next_case_transition',
    '_looks_like_sc_or_gift_trigger': '_looks_like_sc_or_gift_trigger',
    '_make_chunk': '_make_chunk',
    '_make_fallback_topic_from_chunk': '_make_fallback_topic_from_chunk',
    '_manual_entry_matches_topic': '_manual_entry_matches_topic',
    '_manual_entry_meaningfully_overlaps_topic': '_manual_entry_meaningfully_overlaps_topic',
    '_manual_evidence_line': '_manual_evidence_line',
    '_manual_review_anchor': '_manual_review_anchor',
    '_merge_expanded_clip_marks': '_merge_expanded_clip_marks',
    '_merge_manual_timeline_topics': 'merge_manual_timeline_topics',
    '_nearest_safe_srt_boundary': '_nearest_safe_srt_boundary',
    '_next_report_topic_safe_boundary': '_next_report_topic_safe_boundary',
    '_normalise_body_line': '_normalise_body_line',
    '_normalise_boundary_evidence_text': '_normalise_boundary_evidence_text',
    '_normalise_outro_trigger_text': '_normalise_outro_trigger_text',
    '_optimized_entry_semantic_text': '_optimized_entry_semantic_text',
    '_outro_topic_from_mark': '_outro_topic_from_mark',
    '_overlap_ratio': '_overlap_ratio',
    '_parse_clip_interest_score': '_parse_clip_interest_score',
    '_parse_clip_star_bonus': '_parse_clip_star_bonus',
    '_parse_json_topics_response': '_parse_json_topics_response',
    '_parse_llm_response': '_parse_llm_response',
    '_reconcile_topic_manual_evidence': '_reconcile_topic_manual_evidence',
    '_refresh_natural_boundary_metadata': '_refresh_natural_boundary_metadata',
    '_refresh_topic_danmaku_evidence': '_refresh_topic_danmaku_evidence',
    '_repair_short_topic_end': '_repair_short_topic_end',
    '_report_fact_lines': '_report_fact_lines',
    '_resolve_reviewed_report_overlaps': '_resolve_reviewed_report_overlaps',
    '_review_peak_selected_topics': '_review_peak_selected_topics',
    '_reviewed_topic_has_required_interest': '_reviewed_topic_has_required_interest',
    '_sanitize_optimized_manual_entry': '_sanitize_optimized_manual_entry',
    '_score_boundary_evidence_text': '_score_boundary_evidence_text',
    '_serialized_progress_callback': '_serialized_progress_callback',
    '_short_llm_error': '_short_llm_error',
    '_snap_clip_to_srt_segments': '_snap_clip_to_srt_segments',
    '_split_chain_crossing_topic_end': '_split_chain_crossing_topic_end',
    '_srt_video_duration': '_srt_video_duration',
    '_strip_code_fence': '_strip_code_fence',
    '_strip_prompt_time_labels': '_strip_prompt_time_labels',
    '_subtitle_speech_chains': '_subtitle_speech_chains',
    '_topic_analysis_prompt_fingerprint': '_topic_analysis_prompt_fingerprint',
    '_topic_danmaku_reference_lines': '_topic_danmaku_reference_lines',
    '_topic_index_label': '_topic_index_label',
    '_topic_peak_candidates': '_topic_peak_candidates',
    '_topic_peak_focus_window': '_topic_peak_focus_window',
    '_topic_semantic_text': '_topic_semantic_text',
    '_topic_srt_summary_lines': '_topic_srt_summary_lines',
    '_topics_from_manual_timeline': '_topics_from_manual_timeline',
    '_trim_report_topic_around_reviewed_topic': '_trim_report_topic_around_reviewed_topic',
    '_validate_unmatched_manual_topics': '_validate_unmatched_manual_topics',
    '_validated_ai_focus_range': '_validated_ai_focus_range',
    '_write_topic_analysis_checkpoint': '_write_topic_analysis_checkpoint',
    'chunk_srt': 'chunk_srt',
    'fmt_time': 'fmt_time',
    'parse_srt_segments': 'parse_srt_segments',
    'parse_srt_text': 'parse_srt_text',
}


_profile_identity_names = transcription_service.profile_identity_names


_profile_matches_streamer = transcription_service.profile_matches_streamer


SRT_ESTIMATED_CHARS_PER_SEC = transcription_service.SRT_ESTIMATED_CHARS_PER_SEC


TOPIC_CONTEXT_GAP = transcription_service.TOPIC_CONTEXT_GAP


_text_len_for_timing = transcription_service._text_len_for_timing


_normalise_streamer_terms = transcription_service._normalise_streamer_terms


_subtitle_text_size = transcription_service._subtitle_text_size


_load_repaired_srt_segments = transcription_service._load_repaired_srt_segments


DANMAKU_WINDOW = danmaku_analysis.DANMAKU_WINDOW


DANMAKU_WINDOW_STEP = danmaku_analysis.DANMAKU_WINDOW_STEP


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


_parse_hms = timeline_analysis.parse_hms


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


LLM_DEFAULT_CONCURRENCY = 3


LLM_MAX_CONCURRENCY = 4


TOPIC_ANALYSIS_CHECKPOINT_VERSION = checkpoint_store.TOPIC_ANALYSIS_CHECKPOINT_VERSION


CLIP_REVIEW_POLICY_VERSION = checkpoint_store.CLIP_REVIEW_POLICY_VERSION


CLIP_MIN_INTEREST_SCORE = 75  # 独立候选达到投稿价值门槛才值得投入二次剪辑


CLIP_MANUAL_REVIEW_MIN_STARS = 4  # 高星时间轴只负责补充复核候选，绝不直接切片


CLIP_REVIEW_BATCH_SIZE = 3      # 小批复核可显著降低模型漏项和 JSON 截断概率


CLIP_REVIEW_RETRY_BATCH_SIZE = 2


TOPIC_PRE_CONTEXT_SEC = 45      # 通用候选向前保留前因；AI 复核片段另用更紧的 20 秒


TOPIC_POST_CONTEXT_SEC = 60     # 通用候选向后保留收尾；AI 复核片段另用更紧的 20 秒


TOPIC_MIN_CLIP_SEC = 75         # 未经语义复核的短候选至少保留 1.25 分钟上下文


TOPIC_MAX_CLIP_SEC = 240        # 单个实际切片严格不超过 4 分钟


TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC = 60  # 仅证据确认的必要前后文可放宽到 5 分钟


TOPIC_REVIEW_FOCUS_MAX_SEC = 180  # AI 语义核心最多 3 分钟，给前因和收尾预留扩展空间


TOPIC_DIRECT_SLICE_MAX_SEC = TOPIC_REVIEW_FOCUS_MAX_SEC


TOPIC_FOCUS_PRE_SEC = 0         # 长话题核心从弹幕峰值窗口开始，前因由 TOPIC_PRE_CONTEXT_SEC 补


TOPIC_FOCUS_POST_SEC = DANMAKU_WINDOW  # 长话题核心覆盖完整弹幕峰值窗口


TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC = 30


TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC = 45


TOPIC_AI_FOCUS_PRE_CONTEXT_SEC = 20


TOPIC_AI_FOCUS_POST_CONTEXT_SEC = 20


TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC = 45


TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC = 60


TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC = 5


TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC = 10


TOPIC_LEAD_IN_RECOVERY_MIN_SEC = 90


TOPIC_LEAD_IN_LOOKBACK_SEC = 180


TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC = 90


TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC = 60


TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC = 30


TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE = 10


TOPIC_HARD_TRANSITION_GAP_SEC = 10


TOPIC_RELEVANT_CONTINUATION_GAP_SEC = 50


TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC = 180


TOPIC_REFERENCE_END_TOLERANCE_SEC = 90


SC_CONTEXT_LOOKBACK_SEC = 180   # 话题前 3 分钟内的 SC/礼物触发点会纳入切片


SC_FALLBACK_GIFT_LOOKBACK_SEC = 15  # 仅凭“感谢礼物”推断 SC 时避免回溯到无关互动


TOPIC_MIN_REPORT_SEC = 60       # 正文较多但模型给出几秒时，报告至少扩到 1 分钟


TOPIC_MAX_REPAIRED_REPORT_SEC = 180


SC_TRIGGER_KEYWORDS = (
    "sc", "s c", "super chat", "superchat", "醒目留言", "醒目", "付费留言",
    "舰长", "上舰", "总督", "提督", "舰团", "礼物", "打赏", "投喂",
    "爱心抱枕", "告白花束", "棉花糖", "牛哇牛哇", "充电",
)


THANKS_TRIGGER_RE = re.compile(r'(谢谢|感谢|谢[谢了]?|多谢).{0,24}(送|的|老板|老公|礼物|留言|支持)')
















def fmt_time(seconds):
    return str(timedelta(seconds=int(seconds)))




















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
            line = f"●人工时间轴{stars}：{fmt_time(entry['start'])} {entry['text']}"
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
            "start_str": fmt_time(topic_start),
            "end_str": fmt_time(topic_end),
            "title": _manual_title_from_text(entry["text"]),
            "can_slice": False,
            "body": list(entry.get("summary") or []) + [
                f"●人工时间轴{'⭐' * min(entry.get('stars', 0), 5)}："
                f"{fmt_time(entry['start'])} {entry['text']}"
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


def _topic_srt_summary_lines(start, end, srt_segments, limit=12, bucket_sec=30):
    """把碎片字幕聚成带时间范围的短窗口，供 AI 核对事件与边界。"""
    if not srt_segments:
        return []
    related = [
        (seg_start, seg_end, text)
        for seg_start, seg_end, text in srt_segments
        if seg_end >= start and seg_start <= end
    ]
    if not related:
        return []

    buckets = {}
    for seg_start, seg_end, text in related:
        key = max(0, int((max(start, seg_start) - start) // bucket_sec))
        bucket = buckets.setdefault(key, {
            "start": max(start, seg_start),
            "end": min(end, seg_end),
            "texts": [],
        })
        bucket["start"] = min(bucket["start"], max(start, seg_start))
        bucket["end"] = max(bucket["end"], min(end, seg_end))
        compact = re.sub(r'\s+', '', text or '')
        if compact and (not bucket["texts"] or bucket["texts"][-1] != compact):
            bucket["texts"].append(compact)

    windows = [buckets[key] for key in sorted(buckets)]
    if len(windows) <= limit:
        selected = windows
    elif limit <= 1:
        selected = [windows[len(windows) // 2]]
    else:
        indexes = sorted({round(i * (len(windows) - 1) / (limit - 1)) for i in range(limit)})
        selected = [windows[index] for index in indexes]

    lines = []
    seen = set()
    for window in selected:
        compact = "".join(window["texts"])
        if not compact or compact in seen:
            continue
        seen.add(compact)
        if len(compact) > 180:
            compact = compact[:180] + "…"
        lines.append(
            f"·字幕核查：{fmt_time(window['start'])}-{fmt_time(window['end'])} {compact}"
        )
    return lines


def _topic_danmaku_reference_lines(start, end, peaks, limit=3):
    """保留相隔较远的多个峰值，让 AI 能识别人工记录中的并列事件。"""
    candidates = [
        (peak_start, density)
        for peak_start, density in peaks or []
        if peak_start + DANMAKU_WINDOW >= start and peak_start <= end
    ]
    selected = []
    for peak_start, density in sorted(candidates, key=lambda item: item[1], reverse=True):
        if any(abs(peak_start - old_start) < DANMAKU_WINDOW for old_start, _ in selected):
            continue
        selected.append((peak_start, density))
        if len(selected) >= limit:
            break
    return [
        f"·弹幕依据：{fmt_time(peak_start)} 附近峰值约 {int(density)} 条/分钟"
        for peak_start, density in sorted(selected)
    ]


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
            time_label = fmt_time(item["start"])
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
            "start_str": fmt_time(start),
            "end_str": fmt_time(end),
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
        time_label = fmt_time(start_s) if end_s <= start_s + 1 else f"{fmt_time(start_s)}－{fmt_time(end_s)}"
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
            start_label=fmt_time(chunk_start),
            end_label=fmt_time(chunk_end),
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


_NO_SLICE_HINTS = ("不切", "不加标记", "不建议切", "不要切", "不适合切")








_META_BODY_KEYWORDS = (
    "但注意", "注意：", "注意:", "我们需要", "我们应该", "我应该", "我倾向", "是否应该",
    "输出格式", "输出如下", "不要输出", "程序会自动", "允许时间范围", "当前分块",
    "时间范围", "时间戳必须", "格式：", "格式`", "根据原则", "指令说", "题目说", "不能假设",
    "只需输出", "只需要输出", "最后，检查", "Markdown代码块", "Markdown 代码块",
    "这里有一段字幕", "后面没有字幕", "所以我们", "因此，输出", "考虑一下",
    "例如：", "例如:", "由于弹幕密度", "因为弹幕密度", "弹幕密度", "全场平均", "本段无峰值",
    "所以输出", "现在写输出", "要点要写", "所以整理信息", "所以话题标题", "目标风格",
    "但具体", "具体有哪些点", "从字幕中提取", "再看弹幕信息", "弹幕反应？", "没有具体弹幕内容",
    "不加✂️", "加✂️", "可能是", "似乎", "或许", "我们可以", "最好合并", "时间太短",
    "内容要点", "我们还需要考虑", "其他可能性", "可以提一句", "由于字幕", "所以只有一个话题",
    "根据格式", "如果有礼物", "才用●", "不用写", "如果无明显话题", "没有弹幕爆点",
    "弹幕爆点信息", "无爆点", "弹幕高能", "密度达", "峰值", "弹幕信息", "低于平均",
    "高于平均", "不活跃", "这里有明显话题", "最后，如果", "尽量简洁",
    "只能写基于字幕", "基于字幕", "标题可以", "标题更简洁", "优先简洁",
    "现在写", "我决定", "决定输出", "注意起始时间", "弹幕高密度",
    "要点用", "没有特别弹幕爆点", "这里没有明显", "需要确保", "有依据",
    "很好地覆盖", "再检查", "写要点", "最终答案", "规则要求", "按照示例",
    "很难分开", "检查要求", "要点2", "可以考虑更具体", "也可以分", "也许可以写",
    "更符合实际", "原字幕没有说完", "忠实于数据", "不符合常识", "每条对应真实时间",
    "字幕未显示", "我们谨慎", "我们写", "可以不用●",
    "看第二段", "第一段", "第二段", "第三段", "第四段", "同样，", "同样，1:",
    "我们说", "这显然", "时间重叠", "重新组织", "按时间顺序梳理", "接着在",
    "从“", "开始到", "我们取到", "最好重新", "约4:", "约3:", "约2:", "约1:",
    "根据字幕", "话题可以", "话题划分", "可能的划分", "通常做法", "首先，决定",
    "分析字幕内容", "从内容看", "关键词", "不能输出", "建议3个话题", "建议3个",
    "考虑时间顺序", "考虑实际讲话内容", "第五段", "时间轴整合", "自然分段",
    "注意1:", "允许时间", "超出范围", "我们尽量", "有很多讲话",
    "考虑分成", "考虑输出", "输出两个话题", "更好的方式", "更合理", "更合理的是", "更合理地", "然后紧接着",
    "我们仔细分析", "仔细分析每个时间段", "提取可理解", "这里明显", "我可以这样",
    "可以这样", "整体上，这是", "第二个话题", "第一个话题", "标题：", "标题:",
    "整个分块", "前部分", "我们只能", "不能用", "超过", "最后一段开始",
    "首先，覆盖", "覆盖从", "要注意", "直接输出最终条目", "最好基于时间顺序",
    "基于时间顺序整理", "建议这样划分", "子部分：", "子部分:", "字幕原文",
    "但内容不确定", "写具体", "从语义看", "可以作为一个整体话题", "为了简洁",
    "注意，我们", "分话题", "建议分成以下", "字幕分析", "总体来说",
    "比较好的做法", "我建议", "我考虑", "我们也可以", "但中间有间隔",
    "我们还需要写出具体要点", "让我们详细解析", "提取关键点", "可能游戏相关",
    "先理解字幕", "基于此", "要点要具体", "要点内容要具体", "思考如何写",
    "输出中不要", "更精确", "我们可用", "话题一", "话题二", "可能的话题",
    "大致内容", "评论文本", "原文：", "原文:", "整体来看", "注意时间戳",
    "可能的整理", "不合要求", "第三个短", "主要内容:", "主要内容：",
    "第一part", "第二part", "部分:", "部分：",
    "最佳方式", "我们仔细看", "时间线变化", "我们分析", "有哪些连续讲话",
    "规划话题结构", "输出时不要写Part", "现在我们来组织", "字幕内容:",
    "字幕内容：", "一个合理的方法", "合理的方法", "实际上，看字幕文本",
    "观察事件",
    "现在规划", "可能的最佳划分", "最佳划分", "这样就", "具体分段",
    "梳理字幕", "连续意思", "输出最终条目", "让我们仔细整理", "读懂字幕",
    "具体要点", "比如：", "比如:", "我认为合理的划分", "我们可能还需要涵盖",
    "然后要点", "话题A", "话题B",
    "这部分明显", "继续讨论这个视频", "继续这段剧情", "總結話題",
    "总结话题", "根據字幕", "根据字幕", "我認為", "我认为", "可以劃分",
    "可以划分", "劃分為", "划分为", "输出内容要严格按照格式", "严格按照格式",
    "标题加emoji", "最终输出", "礼物、弹幕爆点", "确保时间戳",
    "让我们仔细构建", "最终输出示例", "注意称呼", "如果有）",
    "points:", "points：", "title:", "title：", "重新考虑分块内容",
    "我们先把内容分几个话题", "那么我们定义", "整体时间段",
    "让我们尝试提取话题", "我们确保每个话题",
    "我们仔细阅读字幕", "整体看", "我们试着划分", "可能乱码",
    "后面还有", "这些时间段有重叠", "观察内容", "更仔细看",
    "划分建议", "我们还须注意", "先构思", "topic1", "topic2",
    "我们规划话题", "仔细看字幕", "先考虑can", "建议分成两个话题",
    "最终JSON", "最终 JSON", "先整理出具体的时间段", "查看字幕时间戳",
    "注意时间有重叠", "根据人工时间轴", "再分析字幕", "我们尝试解读字幕",
    "can_slice", "points", "\"topics\"", "\"start\"", "\"end\"", "\"title\"",
    "人工时间轴参考", "观察时间戳", "需要写点", "我们看内容",
    "我们来看内容", "对于话题", "根据内容推断边界", "我们看字幕的时间戳",
    "这些人工时间轴", "与上一段有重叠", "其他话题", "另一个思路",
    "我计划", "虽然弹幕低", "必须整理", "考虑话题", "提示说",
    "不需要特别重视", "可以作为参考", "所以生成JSON", "我们整理一下",
    "根据要求", "我们考虑", "先仔细解析字幕", "一个合理的划分",
    "我们来做分析", "我们来确定话题", "从人工时间轴和字幕",
    "输出JSON模板", "可能的切分", "或者：",
)


_FRAGMENT_BODY_LINES = {
    "要点", "补充细节", "具体要点", "另一个事件", "例如", "例如：", "例如:", "等等。", "等等",
    "内容要点", "内容要点：", "内容要点:", "输出", "主播", "加盟商", "店主", "连麦者",
    "但", "但是", "然后", "因为", "所以", "因此", "不过", "最后", "另外", "同时", "继续",
    "现在规划", "具体要点", "具体要点：", "具体要点:", "比如", "比如：", "比如:",
    "points", "points:", "points：", "title", "title:", "title：", "要点", "要点：", "要点:",
    "更好的划分", "更好的划分：", "那么我们定义", "那么我们定义：", "整体时间段", "整体时间段：",
    "观察内容", "观察内容：", "更仔细看", "更仔细看：", "划分建议", "划分建议：",
    "整体看", "整体看，内容涉及：", "我们试着划分", "我们试着划分：",
    "我们规划话题", "我们规划话题：", "仔细看字幕", "仔细看字幕：",
    "先考虑can", "先考虑can：", "最终JSON", "最终 JSON", "最终 JSON：",
    "根据人工时间轴", "根据人工时间轴：", "再分析字幕详细内容", "再分析字幕详细内容：",
    "人工时间轴参考", "人工时间轴参考：", "观察时间戳", "观察时间戳：",
    "需要写点", "需要写点：", "我们看内容", "我们看内容：",
    "我们来看内容", "我们来看内容：", "我们看字幕的时间戳", "我们看字幕的时间戳：",
    "其他话题", "其他话题：", "另一个思路", "另一个思路：",
    "我计划", "我计划：", "所以生成JSON", "所以生成JSON：",
    "根据要求", "根据要求，", "我们考虑", "我们考虑：",
    "先仔细解析字幕", "先仔细解析字幕：", "一个合理的划分", "一个合理的划分：",
    "我们来做分析", "我们来做分析：", "我们来确定话题", "我们来确定话题。",
    "输出JSON模板", "输出JSON模板：", "可能的切分", "可能的切分：",
    "或者", "或者：",
    "弹幕/礼物高光", "弹幕礼物高光", "…", "...", "……",
}


_DANMAKU_META_KEYWORDS = (
    "弹幕反应平静", "无爆点", "弹幕高能", "密度达", "峰值", "全场平均", "低于平均", "高于平均",
    "弹幕倍数", "弹幕信息", "弹幕爆点信息", "没有弹幕爆点", "不活跃", "反应不活跃",
    "弹幕高密度", "反应活跃", "可能弹幕", "字幕未显示", "我们谨慎",
    "弹幕互动平淡", "观众反应较少", "弹幕较少", "观众活跃度不高",
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
















def _is_slice_marked(raw_title):
    """判断标题是否显式标记为可切。"""
    if any(hint in raw_title for hint in _NO_SLICE_HINTS):
        return False
    return "✂" in raw_title


def _is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end, tolerance=90):
    """只接受当前分块时间范围附近的话题，过滤模型复读旧示例。"""
    if end_s <= start_s:
        return False
    if start_s < chunk_start - tolerance:
        return False
    if end_s > chunk_end + tolerance:
        return False
    return True


def _overlap_ratio(a_start, a_end, b_start, b_end):
    """按较短区间计算重叠比例。"""
    overlap = max(0, min(a_end, b_end) - max(a_start, b_start))
    shorter = max(1, min(a_end - a_start, b_end - b_start))
    return overlap / shorter


def _is_duplicate_topic(topic, existing_topics):
    """按时间范围去重；同一段被模型换标题复述时只保留第一条。"""
    for old in existing_topics:
        same_range = abs(topic["start"] - old["start"]) <= 3 and abs(topic["end"] - old["end"]) <= 3
        high_overlap = _overlap_ratio(topic["start"], topic["end"], old["start"], old["end"]) >= 0.85
        if same_range or high_overlap:
            return True
    return False






def _is_meta_body_line(line):
    """过滤模型思考过程、规则复述、弹幕密度解释和占位半句。"""
    raw = line.strip()
    clean = _strip_body_prefix(line)
    if not clean:
        return True
    if "```" in clean:
        return True

    normalized = clean.strip(' （）()[]【】「」『』：:。；;，,、.!！?？')
    if re.fullmatch(r'\[?\d{1,2}:\d{2}(?::\d{2})?\s*', clean):
        return True
    if clean.startswith(("字幕核查：", "字幕核查:", "弹幕依据：", "弹幕依据:", "切片核心：", "切片核心:")):
        return False
    if clean.startswith(("“", "”", "\"", "‘", "'")) and ("– 说" in clean or "- 说" in clean or len(clean) > 80):
        return True
    if "->" in clean:
        return True
    if clean in _FRAGMENT_BODY_LINES or normalized in _FRAGMENT_BODY_LINES:
        return True
    if clean.startswith((
        "标题：", "标题:", "第一个话题", "第二个话题", "第三个话题", "字幕原文",
        "话题一", "话题二", "话题三", "話題1", "話題2", "話題3",
        "第一part", "第二part", "第三个短", "{", "}", '"topics"',
        '"start"', '"end"', '"title"', '"can_slice"', '"points"',
    )):
        return True
    if re.match(r'^(points|title)\s*[:：]', clean, re.IGNORECASE):
        return True
    if clean.startswith((
        "首先，覆盖", "覆盖从", "要注意", "注意字幕", "然后从", "另外，前部分", "整个分块",
        "注意最后一段", "更好的方式", "更合理", "其实我们最好", "建议这样",
        "子部分", "从语义看", "为了简洁", "注意，我们", "字幕分析", "总体来说",
        "比较好的做法", "我建议", "我考虑", "我们也可以", "但中间有间隔",
        "让我们详细解析", "先理解字幕", "基于此", "要点要具体", "要点内容",
        "思考如何写", "输出中不要", "更精确", "我们可用", "可能的话题",
        "大致内容", "从字幕看", "整体来看", "注意时间戳", "可能的整理",
        "主要内容", "部分:", "最佳方式", "我们仔细看", "我们分析", "输出时不要写Part",
        "现在我们来组织", "字幕内容", "一个合理的方法", "实际上，看字幕文本",
        "观察事件", "现在规划", "可能的最佳划分", "具体分段", "梳理字幕",
        "输出最终条目", "让我们仔细整理", "读懂字幕", "具体要点", "比如",
        "我认为合理的划分", "我们可能还需要涵盖", "然后要点",
        "这部分明显", "继续讨论这个视频", "继续这段剧情", "總結話題",
        "根據字幕", "根据字幕", "输出内容要严格按照格式", "标题加emoji",
        "最终输出", "礼物、弹幕爆点", "确保时间戳", "让我们仔细构建",
        "最终输出示例", "注意称呼", "由于是主播自言自语",
        "重新考虑分块内容", "我们先把内容分几个话题", "那么我们定义",
        "整体时间段", "让我们尝试提取话题", "我们确保每个话题",
        "我们仔细阅读字幕", "整体看", "我们试着划分", "这些时间段有重叠",
        "观察内容", "更仔细看", "划分建议", "我们还须注意", "先构思",
        "我们规划话题", "仔细看字幕", "先考虑can", "建议分成两个话题",
        "或者可以合并", "最终 JSON", "最终JSON", "先整理出具体的时间段",
        "查看字幕时间戳", "注意时间有重叠", "根据人工时间轴", "再分析字幕",
        "我们尝试解读字幕",
        "人工时间轴参考", "观察时间戳", "需要写点", "我们看内容",
        "我们来看内容", "对于话题", "根据内容推断边界", "我们看字幕的时间戳",
        "这些人工时间轴", "与上一段有重叠", "其他话题", "另一个思路",
        "我计划", "虽然弹幕低", "必须整理", "考虑话题", "提示说",
        "不需要特别重视", "可以作为参考", "所以生成JSON", "我们整理一下",
        "根据要求", "我们考虑", "先仔细解析字幕", "一个合理的划分",
        "我们来做分析", "我们来确定话题", "从人工时间轴和字幕",
        "输出JSON模板", "可能的切分", "或者",
    )):
        return True
    if re.match(r'^topic\d+\s*[:：]', clean, re.IGNORECASE):
        return True
    if re.match(r'^\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*["“”]', clean):
        return True
    if re.match(r'^["“”]?(start|end|title|can_slice|points|topics)["“”]?\s*[:：]', clean, re.IGNORECASE):
        return True
    if clean in {"{", "}", "[", "]", "},", "],", "{"}:
        return True
    if re.match(r'^\[\d{1,2}:\d{2}(?::\d{2})?\s*/\s*\d{4}-\d{2}-\d{2}', clean):
        return True
    if re.match(r'^\d+[.)、]\s*(聊|讨论|观看|感谢|游戏|生日)', clean):
        return True
    if re.match(r'^\d+\.\s*\d{1,2}:\d{2}', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*[（(]', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*[：:]', clean):
        return True
    if re.match(r'^\d+[.)、]\s*', clean) and re.search(r'(表演|评论|讨论|话题|話題|游戏|感谢|朗读|观看|吐槽|礼物)', clean):
        return True
    if clean.startswith(("然后", "从")) and re.search(r'\d{1,2}:\d{2}(?::\d{2})?', clean):
        return True
    if "##" in clean or "规划话题结构" in clean:
        return True
    if clean.startswith("[开始") or clean.startswith("开始－结束") or clean.startswith("开始-结束"):
        return True
    if re.match(r'^\d+[.、]\s*', clean) and re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean):
        return True
    if re.match(r'^\d+[.、]\s*', clean) and re.search(
            rf'(话题|話題|关于|讨论|{_streamer_role_pattern("主播")}|弹幕|感谢|游戏|时间|内容)',
            clean):
        return True
    if re.match(r'^(话题|話題|第[一二三四五六七八九十]+段|第\d+段)\s*\d*[:：]', clean):
        return True
    if re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean) and re.search(r'(话题|时间|开始|结束|取到|部分|阶段)', clean):
        return True
    if re.search(r'\d{1,2}:\d{2}(?::\d{2})?\s*[-－]\s*\d{1,2}:\d{2}', clean) and re.search(r'(注意|但是|但|我们|考虑|更好|更合理|然后|这里|标题|划分|输出|合并)', clean):
        return True
    if re.match(r'^\d{1,2}:\d{2}(?::\d{2})?\s*(?:开始|继续)', clean) and re.search(r'(评论文本|讨论|抱怨|感谢|开始)', clean):
        return True
    # 被 max_tokens 截断时常出现“·主播”“·加盟商”“·但”这类无法独立理解的半句。
    if len(normalized) <= 3 and normalized in {"主播", "观众", "弹幕", "店主", "对方", "加盟商", "但", "输出"}:
        return True
    # ● 只保留礼物、观众金句等具体事件；泛泛的弹幕强弱/密度判断不进报告。
    if raw.startswith("●") and any(keyword in clean for keyword in _DANMAKU_META_KEYWORDS):
        return True
    if any(keyword in clean for keyword in _META_BODY_KEYWORDS):
        return True
    if re.search(r'(应该|不应该|可以只输出|是否|格式|指令|原则|分块|代码块|我们可以|所以输出|要点要写|具体有哪些点)', clean) and (
        clean.startswith(("但", "另外", "所以", "因此", "这里", "如果", "最后", "检查", "考虑"))
        or "我们" in clean
    ):
        return True
    if clean.startswith(("但", "但是", "不过", "所以", "因此", "此外", "按照", "检查", "现在", "这里", "因为", "另外", "也许", "也可以", "为了")) and re.search(
        r'(规则|要求|字幕|依据|输出|话题|标题|要点|检查|示例|时间|数据|写|分成|可以|覆盖|常识)',
        clean,
    ):
        return True
    if clean.startswith(("所以", "另外", "因此", "现在", "再看")) and re.search(r'(输出|整理|标题|弹幕|要点|具体|密度)', clean):
        return True
    if re.match(r'^(弹幕|密度|由于弹幕|因为弹幕)[:：]', clean):
        return True
    return False


def _clean_body_content(line):
    """保留有效信息，同时去掉模型常见的总结式开头。"""
    clean = _strip_body_prefix(line)
    clean = re.sub(r'^(?:所以整体是|大致内容[:：]?|主要内容[:：]?|首先[，,]\s*)', '', clean).strip()
    clean = re.sub(r'^[\"“”](.*?)[\"”]?\s*,?$', r'\1', clean).strip()
    clean = re.sub(r'^内容有些混乱[，,。；;：:但是\s]*', '', clean).strip()
    clean = re.sub(r'^但是可以归纳出话题[:：]?', '', clean).strip()
    clean = re.sub(r'^可以归纳出话题[:：]?', '', clean).strip()
    clean = re.sub(r'^要点\s*[:：]\s*', '', clean).strip()
    clean = re.sub(r'^这段(?:讨论|继续解释|继续)?', '', clean).strip()
    return _normalise_obvious_report_terms(clean)


def _normalise_body_line(line):
    """规范正文要点前缀，让报告接近人工时间轴。"""
    raw = line.strip()
    line = _clean_body_content(raw)
    if not line or _is_meta_body_line(line):
        return ""
    if raw.startswith("●"):
        return "●" + line
    return "·" + line


def _json_points_to_body(points):
    """把 JSON points/body 字段转换成报告正文要点。"""
    if points is None:
        return []
    if isinstance(points, str):
        raw_items = [line for line in re.split(r'[\r\n]+', points) if line.strip()]
    elif isinstance(points, (list, tuple)):
        raw_items = []
        for item in points:
            if isinstance(item, (list, tuple)):
                raw_items.extend(str(sub) for sub in item)
            else:
                raw_items.append(str(item))
    else:
        raw_items = [str(points)]
    body_lines = [_normalise_body_line(item) for item in raw_items]
    return [line for line in body_lines if line]


def _json_can_slice(value, raw_title):
    """解析 JSON 里的 can_slice 字段；兼容字符串布尔值。"""
    if any(hint in str(raw_title) for hint in _NO_SLICE_HINTS):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "可切", "切", "是"}
    return "✂" in str(raw_title)








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
            start_s = _parse_hms(start_str)
            end_s = _parse_hms(end_str)
        except Exception:
            continue
        if not _is_topic_in_chunk(start_s, end_s, chunk_start, chunk_end):
            continue
        raw_title = str(item.get("title", "")).strip()
        if _is_placeholder_title(raw_title):
            continue
        body_lines = _filter_unsupported_ai_points(_json_points_to_body(
            item.get("points", item.get("body", item.get("summary", item.get("details"))))
        ))
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
            "end_str": fmt_time(end_s),
            "title": title,
            "publish_title": _normalise_publish_title(item.get("publish_title"), title),
            "can_slice": _json_can_slice(item.get("can_slice", False), raw_title),
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
            "start": fmt_time(topic["start"]),
            "end": fmt_time(topic["end"]),
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


_UNSUPPORTED_AI_AUDIENCE_REACTION_RE = re.compile(
    r'(?:'
    r'(?:观众|弹幕).{0,18}(?:刷屏|刷|直呼|调侃|笑称|齐刷|赞叹|赞|起哄|反应活跃|疯狂|热闹|沸腾|炸锅)'
    r'|(?:现场|全场|气氛).{0,10}(?:热烈|活跃|沸腾|炸锅|爆笑|高涨)'
    r')'
)






_MANUAL_AI_PLACEHOLDER_PHRASES = (
    "5-15字具体短标题",
    "具体发生了什么",
    "主播如何回应",
    "具体事件钩子",
    "结果或原话",
)


def _is_manual_ai_placeholder(value):
    compact = re.sub(r'\s+', '', str(value or ""))
    dynamic_placeholder = f"{_prompt_streamer_name()}如何回应"
    return (
        not compact
        or dynamic_placeholder in compact
        or any(phrase in compact for phrase in _MANUAL_AI_PLACEHOLDER_PHRASES)
    )




def _filter_unsupported_ai_points(points):
    """弹幕密度不能证明具体弹幕内容，过滤模型自行补写的观众反应。"""
    return [
        line for line in points or []
        if not _UNSUPPORTED_AI_AUDIENCE_REACTION_RE.search(_strip_body_prefix(line))
    ]


def _validated_ai_focus_range(item, topic):
    """校验 AI 建议的语义核心范围；越界或过长时忽略，继续使用程序候选范围。"""
    try:
        focus_start = _parse_hms(str(item.get("focus_start", "")))
        focus_end = _parse_hms(str(item.get("focus_end", "")))
    except (TypeError, ValueError):
        return None
    source_start = int(topic["start"])
    source_end = int(topic["end"])
    duration = focus_end - focus_start
    if not source_start <= focus_start < focus_end <= source_end:
        return None
    if duration < 10 or duration > TOPIC_REVIEW_FOCUS_MAX_SEC:
        return None
    return focus_start, focus_end


def _enriched_manual_topic_from_item(topic, item):
    """把一项 AI 复核结果应用到候选副本；无有效正文时返回 None。"""
    points = [
        point
        for point in _filter_unsupported_ai_points(_json_points_to_body(item.get("points")))
        if not _is_manual_ai_placeholder(_strip_body_prefix(point))
    ]
    if not points:
        return None
    enriched = dict(topic)
    raw_title = _clean_topic_title(str(item.get("title", topic.get("title", ""))))
    if _is_manual_ai_placeholder(raw_title) or _is_incomplete_ai_title(raw_title):
        raw_title = ""
    title = _derive_topic_title(
        raw_title,
        points,
    )
    if not title or _is_manual_ai_placeholder(title) or _is_incomplete_ai_title(title):
        return None
    preserved_evidence = [
        line
        for line in topic.get("body") or []
        if line.startswith("·弹幕依据：") or line.startswith("●人工时间轴")
    ]
    body = list(points)
    for line in preserved_evidence:
        if line not in body:
            body.append(line)
    evidence_lines = list(topic.get("body") or []) + points
    title = _sanitize_transport_claims(title, evidence_lines)
    enriched["title"] = title
    publish_title = item.get("publish_title")
    if _is_incomplete_ai_title(publish_title):
        publish_title = None
    enriched["publish_title"] = _sanitize_transport_claims(
        _normalise_publish_title(publish_title, title),
        evidence_lines,
    )
    if topic.get("publish_title_locked"):
        enriched["publish_title"] = _normalise_publish_title(
            topic.get("publish_title"),
            topic.get("title", title),
        )
        enriched["publish_title_locked"] = True
        enriched["publish_title_source"] = (
            topic.get("publish_title_source") or "human_review"
        )
    title_hook = _normalise_title_hook(item.get("title_hook"))
    if title_hook:
        enriched["title_hook"] = title_hook
    enriched["body"] = body
    enriched["ai_enriched"] = True
    enriched["postcheck_pending"] = False
    enriched["postcheck_validated"] = True
    enriched.pop("reference_only", None)
    focus_range = _validated_ai_focus_range(item, topic)
    if focus_range:
        source_start = int(topic["start"])
        source_end = int(topic["end"])
        enriched["reference_start"] = source_start
        enriched["reference_end"] = source_end
        enriched["start"], enriched["end"] = focus_range
        enriched["start_str"] = fmt_time(enriched["start"])
        enriched["end_str"] = fmt_time(enriched["end"])
        enriched["ai_focus_validated"] = True
    return enriched


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
            enriched = _enriched_manual_topic_from_item(topic, item)
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
    report_progress = _serialized_progress_callback(progress_callback)
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
    concurrency = min(_configured_llm_concurrency(), max(1, len(jobs)))
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
    return f"{prefix}：{fmt_time(int(entry.get('start', 0)))} {entry.get('text', '')}"


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
        body_lines = [_normalise_body_line(line) for line in current["body"]]
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
            "end_str": fmt_time(end_s),
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
                "start": _parse_hms(start_str),
                "end": _parse_hms(end_str),
                "title": _clean_topic_title(raw_title),
                "can_slice": _is_slice_marked(raw_title),
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
        "start_str": fmt_time(ch["start"]),
        "end_str": fmt_time(ch.get("end", ch["start"] + CHUNK_SEC)),
        "title": title,
        "can_slice": False,
        "body": [
            f"·本段为{streamer_name}的连续聊天/互动，字幕识别较碎，已保留在时间轴中",
            "·该段未形成稳定可切片主题，暂不标记为自动切片",
        ],
        "fallback": True,
    }
    return topic


def _dedupe_clip_marks(marks):
    """对 clip_marks 做最终去重，避免旧 JSON 或异常响应导致重复切片。"""
    deduped = []
    seen_topics = []
    for mark in sorted(
            marks,
            key=lambda m: (
                0 if m.get("clip_type") == "stream_outro" else 1,
                int(m.get("topic_start", m.get("start", 0))),
                int(m.get("topic_end", m.get("end", 0))),
                m.get("title", ""),
            )):
        try:
            topic_start = int(float(mark.get("topic_start", mark["start"])))
            topic_end = int(float(mark.get("topic_end", mark["end"])))
            item = dict(mark)
            item["start"] = int(float(mark["start"]))
            item["end"] = int(float(mark["end"]))
            item["title"] = str(mark.get("title", "未命名片段")).strip() or "未命名片段"
        except (KeyError, TypeError, ValueError):
            continue
        if item["end"] <= item["start"] or topic_end <= topic_start:
            continue
        dedupe_topic = {"start": topic_start, "end": topic_end, "title": item["title"]}
        if _is_duplicate_topic(dedupe_topic, seen_topics):
            continue
        if any(
            old.get("title") == item["title"]
            and _overlap_ratio(item["start"], item["end"], old["start"], old["end"]) >= 0.5
            for old in deduped
        ):
            continue
        seen_topics.append(dedupe_topic)
        deduped.append(item)
    # 去重阶段让收播片先参与比较，是为了在尾部范围冲突时优先保留用户
    # 指定的系列片；对外返回仍必须按视频时间排列，否则收播片会变成 01，
    # 还会迫使所有既有切片无意义地整体改号。
    return sorted(
        deduped,
        key=lambda item: (
            int(item.get("start", 0)),
            int(item.get("end", 0)),
            item.get("title", ""),
        ),
    )


def _nearest_safe_srt_boundary(candidate, minimum, maximum, srt_segments):
    """在允许范围内寻找最接近候选点、且不落在任何字幕句内部的整数秒。"""
    minimum = math.ceil(minimum)
    maximum = math.floor(maximum)
    if minimum > maximum:
        return None
    candidate = max(minimum, min(int(candidate), maximum))
    if not srt_segments:
        return candidate

    def is_safe(point):
        return not any(start < point < end for start, end, _ in srt_segments)

    max_distance = max(candidate - minimum, maximum - candidate)
    for distance in range(max_distance + 1):
        options = [candidate - distance]
        if distance:
            options.append(candidate + distance)
        for point in options:
            if minimum <= point <= maximum and is_safe(point):
                return point
    return None


def _merge_expanded_clip_marks(marks, srt_segments=None):
    """处理扩展后的重叠：核心重叠才合并，仅上下文相碰则按语义边界拆开。"""
    def titles_of(mark):
        titles = mark.get("merged_titles") or [mark.get("title", "")]
        result = []
        for title in titles:
            title = str(title).strip()
            if title and title not in result:
                result.append(title)
        return result

    merged = []
    for mark in sorted(_dedupe_clip_marks(marks), key=lambda m: (m["start"], m["end"])):
        item = _cap_expanded_clip_mark(dict(mark))
        if not merged:
            merged.append(item)
            continue
        prev = merged[-1]
        if item["start"] >= prev["end"]:
            merged.append(item)
            continue

        prev_topic_start = prev.get("topic_start", prev["start"])
        prev_topic_end = prev.get("topic_end", prev["end"])
        item_topic_start = item.get("topic_start", item["start"])
        item_topic_end = item.get("topic_end", item["end"])
        core_overlap = _overlap_ratio(
            prev_topic_start, prev_topic_end, item_topic_start, item_topic_end
        )
        same_title = prev.get("title") == item.get("title")
        if not same_title and core_overlap < 0.5:
            actual_overlap_start = max(int(prev["start"]), int(item["start"]))
            actual_overlap_end = min(int(prev["end"]), int(item["end"]))
            if prev_topic_end <= item_topic_start:
                boundary_min = max(actual_overlap_start, int(prev_topic_end))
                boundary_max = min(actual_overlap_end, int(item_topic_start))
            else:
                overlap_start = max(prev_topic_start, item_topic_start)
                overlap_end = min(prev_topic_end, item_topic_end)
                boundary_min = max(actual_overlap_start, int(overlap_start))
                boundary_max = min(actual_overlap_end, int(overlap_end))
            boundary_min = max(boundary_min, int(prev["start"]) + 1)
            boundary_max = min(boundary_max, int(item["end"]) - 1)
            preferred_boundary = item.get("required_context_start")
            if preferred_boundary is None:
                preferred_boundary = item.get("topic_start")
            if preferred_boundary is None:
                boundary_candidate = int((boundary_min + boundary_max) / 2)
            else:
                boundary_candidate = max(
                    math.ceil(boundary_min),
                    min(int(preferred_boundary), math.floor(boundary_max)),
                )
            boundary = _nearest_safe_srt_boundary(
                boundary_candidate,
                boundary_min,
                boundary_max,
                srt_segments or [],
            )
            if boundary is None:
                blocking_segments = [
                    segment
                    for segment in (srt_segments or [])
                    if segment[0] < boundary_max and segment[1] > boundary_min
                ]
                reliable_continuous_sentence = (
                    blocking_segments
                    and not prev.get("merged_context")
                    and all(
                        segment[1] - segment[0] <= TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC
                        for segment in blocking_segments
                    )
                )
                if not reliable_continuous_sentence:
                    boundary = max(
                        math.ceil(boundary_min),
                        min(boundary_candidate, math.floor(boundary_max)),
                    )
            if boundary is not None:
                prev["end"] = min(int(prev["end"]), boundary)
                item["start"] = max(int(item["start"]), boundary)
                merged[-1] = _cap_expanded_clip_mark(prev)
                merged.append(_cap_expanded_clip_mark(item))
                continue

        prev["end"] = max(prev["end"], item["end"])
        prev["topic_start"] = min(prev.get("topic_start", prev["start"]), item.get("topic_start", item["start"]))
        prev["topic_end"] = max(prev.get("topic_end", prev["end"]), item.get("topic_end", item["end"]))
        prev["context_expanded"] = bool(prev.get("context_expanded") or item.get("context_expanded"))
        prev["merged_context"] = True
        if "context_start_before_natural" in item:
            prev["context_start_before_natural"] = min(
                prev.get("context_start_before_natural", item["context_start_before_natural"]),
                item["context_start_before_natural"],
            )
        if "context_end_before_natural" in item:
            prev["context_end_before_natural"] = max(
                prev.get("context_end_before_natural", item["context_end_before_natural"]),
                item["context_end_before_natural"],
            )
        titles = titles_of(prev)
        for title in titles_of(item):
            if title not in titles:
                titles.append(title)
        if titles:
            prev["title"] = " / ".join(titles)[:60]
            prev["merged_titles"] = titles
        merged[-1] = _cap_expanded_clip_mark(prev)
    return [_refresh_natural_boundary_metadata(item) for item in merged]


def _refresh_natural_boundary_metadata(mark):
    """在限长、合并或去重后刷新实际保留下来的自然边界延伸量。"""
    item = dict(mark)
    context_start = item.get("context_start_before_natural")
    context_end = item.get("context_end_before_natural")
    if context_start is not None:
        item["natural_boundary_pre_sec"] = int(max(0, context_start - item["start"]))
    if context_end is not None:
        item["natural_boundary_post_sec"] = int(max(0, item["end"] - context_end))
    return item


def _cap_expanded_clip_mark(mark):
    """在字幕吸附和重叠处理后再次限长，优先保留话题核心或弹幕峰值。"""
    item = dict(mark)
    if item.get("preserve_to_video_end"):
        return item
    start_s = int(item["start"])
    end_s = int(item["end"])
    if end_s - start_s <= TOPIC_MAX_CLIP_SEC:
        return item

    topic_start = max(start_s, int(item.get("topic_start", start_s)))
    topic_end = min(end_s, int(item.get("topic_end", end_s)))
    if topic_end <= topic_start:
        topic_start, topic_end = start_s, end_s

    required_context_start = item.get("required_context_start")
    required_context_end = item.get("required_context_end")
    required_context_overflow_end = item.get("required_context_overflow_end")
    if required_context_end is not None:
        required_context_end = min(
            end_s,
            max(topic_end, int(required_context_end)),
        )
    if required_context_start is not None and required_context_end is not None:
        required_context_start = max(start_s, min(topic_start, int(required_context_start)))
        required_max_duration = TOPIC_MAX_CLIP_SEC + TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
        if required_context_end - required_context_start <= required_max_duration:
            new_start = required_context_start
            new_end = min(end_s, new_start + required_max_duration)
            if new_end < required_context_end:
                new_end = required_context_end
                new_start = max(start_s, new_end - required_max_duration)
            item["start"] = int(new_start)
            item["end"] = int(max(new_start + 1, new_end))
            return item
    if required_context_overflow_end is not None:
        required_max_duration = TOPIC_MAX_CLIP_SEC + TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
        required_context_overflow_end = min(
            end_s,
            max(topic_end, int(required_context_overflow_end)),
        )
        if required_context_overflow_end - topic_start <= required_max_duration:
            # 只为短暂停顿后出现的明确结论放宽到 5 分钟；结论后的
            # 普通延伸最多再留 10 秒，避免把后续案例一起带入。
            new_end = min(
                end_s,
                required_context_overflow_end
                + TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC,
            )
            if (
                    required_context_end is not None
                    and required_context_end - start_s <= required_max_duration):
                new_end = max(new_end, required_context_end)
            new_start = max(start_s, new_end - required_max_duration)
            item["start"] = int(new_start)
            item["end"] = int(max(new_start + 1, new_end))
            return item
    if required_context_start is not None:
        required_context_start = max(start_s, min(topic_start, int(required_context_start)))
        if topic_end - required_context_start <= TOPIC_MAX_CLIP_SEC:
            # 触发语句和语义核心能同时装入上限时，保住触发句，优先裁掉
            # 核心结束后的普通延伸，不再突破 300 秒。
            new_start = required_context_start
            new_end = min(end_s, new_start + TOPIC_MAX_CLIP_SEC)
            if new_end < topic_end:
                new_end = topic_end
                new_start = max(required_context_start, new_end - TOPIC_MAX_CLIP_SEC)
            if new_start > start_s or new_end < end_s:
                item["duration_capped"] = True
            item["start"] = int(new_start)
            item["end"] = int(max(new_start + 1, new_end))
            return item

    core_duration = topic_end - topic_start
    if core_duration >= TOPIC_MAX_CLIP_SEC:
        anchor = int(item.get("slice_anchor") or ((topic_start + topic_end) / 2))
        new_start = anchor - TOPIC_MAX_CLIP_SEC // 2
        new_start = max(start_s, min(new_start, end_s - TOPIC_MAX_CLIP_SEC))
        new_end = new_start + TOPIC_MAX_CLIP_SEC
    else:
        available_context = TOPIC_MAX_CLIP_SEC - core_duration
        pre_context = min(TOPIC_PRE_CONTEXT_SEC, available_context)
        new_start = max(start_s, topic_start - pre_context)
        new_end = min(end_s, new_start + TOPIC_MAX_CLIP_SEC)
        if new_end < topic_end:
            new_end = topic_end
            new_start = max(start_s, new_end - TOPIC_MAX_CLIP_SEC)

    if new_start > start_s or new_end < end_s:
        item["duration_capped"] = True
    item["start"] = int(new_start)
    item["end"] = int(max(new_start + 1, new_end))
    return item


def parse_srt_segments(srt_path):
    """解析 SRT，返回 [(start_s, end_s, text), ...]。时间均为视频内时间，并修复明显异常时间戳。"""
    return _load_repaired_srt_segments(srt_path)


def _srt_video_duration(srt_segments):
    """用最后一句字幕估算可用视频时长。"""
    if not srt_segments:
        return None
    return max(seg_end for _, seg_end, _ in srt_segments)


_OUTRO_TRIGGER_NORMALISE_RE = re.compile(r"[\s,，。！？!?、…~～\-_—]+")


OUTRO_TRIGGER_JOIN_GAP_SEC = 8


OUTRO_VARIANT_FAREWELL_BEFORE_SEC = 30


OUTRO_VARIANT_FAREWELL_AFTER_SEC = 180


_OUTRO_ACTIVITY_VARIANT_RE = re.compile(
    r"(?:那|那么)?(?:我|我们)?今天(?:的)?(?:就)?(?:先)?"
    r"(?:直播|播|玩|聊|唱|看到|看|说|陪大家)"
    r"到这里(?:了|吧)?"
)


_OUTRO_FAREWELL_EVIDENCE = (
    "晚安",
    "拜拜",
    "明天见",
    "好梦",
    "下播",
    "关播",
)


def _normalise_outro_trigger_text(value):
    """只为明确收播口令匹配清理停顿和标点，不把泛化“晚安”当口令。"""

    return _OUTRO_TRIGGER_NORMALISE_RE.sub("", str(value or "")).casefold()


def _has_outro_farewell_evidence(entries, trigger_start):
    """确认“玩/聊/唱到这里”等弹性句式附近确实存在收播告别。"""

    evidence_start = trigger_start - OUTRO_VARIANT_FAREWELL_BEFORE_SEC
    evidence_end = trigger_start + OUTRO_VARIANT_FAREWELL_AFTER_SEC
    for cue_start, cue_end, normalised_text in entries:
        if cue_end < evidence_start or cue_start > evidence_end:
            continue
        if any(token in normalised_text for token in _OUTRO_FAREWELL_EVIDENCE):
            return True
    return False


def _detect_stream_outro_clip(srt_segments, video_duration, streamer_profile=None):
    """在录播尾部识别明确收播口令，并生成保留至真实视频结束的系列切片。"""

    profile = streamer_profile or current_streamer_profile()
    config = getattr(profile, "outro_clip", None)
    if config is None or not srt_segments or not video_duration:
        return None
    try:
        video_end = int(math.ceil(float(video_duration)))
    except (TypeError, ValueError):
        return None
    if video_end <= 0:
        return None

    tail_start = max(0, video_end - config.search_tail_sec)
    normalised_triggers = [
        (trigger, _normalise_outro_trigger_text(trigger))
        for trigger in config.triggers
    ]
    entries = []
    for cue_start, cue_end, text in sorted(srt_segments, key=lambda item: (item[0], item[1])):
        if cue_start < tail_start or cue_start >= video_end:
            continue
        normalised_text = _normalise_outro_trigger_text(text)
        if not normalised_text:
            continue
        entries.append((cue_start, cue_end, normalised_text))

    candidates = []
    joined_text = ""
    cue_starts = []
    previous_end = None
    for cue_start, cue_end, normalised_text in entries:
        if previous_end is not None and cue_start - previous_end > OUTRO_TRIGGER_JOIN_GAP_SEC:
            joined_text = ""
            cue_starts = []
        previous_end = cue_end
        joined_text += normalised_text
        cue_starts.extend([cue_start] * len(normalised_text))
        for trigger, normalised_trigger in normalised_triggers:
            match_at = joined_text.find(normalised_trigger)
            if match_at >= 0:
                candidates.append((cue_starts[match_at], 0, trigger))
        for match in _OUTRO_ACTIVITY_VARIANT_RE.finditer(joined_text):
            trigger_start = cue_starts[match.start()]
            if _has_outro_farewell_evidence(entries, trigger_start):
                candidates.append((trigger_start, 1, match.group(0)))

    if not candidates:
        return None
    cue_start, _, trigger = min(candidates, key=lambda item: (item[0], item[1]))
    start = max(0, min(int(math.floor(cue_start)), video_end - 1))
    series_title = config.series_title
    publish_title = f"{profile.title_prefix}{series_title}".strip()
    return {
        "start": start,
        "end": video_end,
        "topic_start": start,
        "topic_end": video_end,
        "report_start": start,
        "report_end": video_end,
        "slice_anchor": start,
        "slice_anchor_source": "收播口令",
        "title": series_title,
        "publish_title": publish_title or series_title,
        "clip_type": "stream_outro",
        "series_title": series_title,
        "preserve_to_video_end": True,
        "outro_trigger": trigger,
        "time_basis": "video_elapsed_seconds",
    }


def _outro_topic_from_mark(mark):
    """把确定性收播片同步到完整话题报告，不参与普通候选复核。"""

    trigger = str(mark.get("outro_trigger") or "收播口令").strip()
    title = str(mark.get("title") or "收播片段").strip()
    return {
        "start": int(mark["start"]),
        "end": int(mark["end"]),
        "title": title,
        "publish_title": mark.get("publish_title") or title,
        "can_slice": True,
        "slice_anchor": int(mark["start"]),
        "slice_anchor_source": "收播口令",
        "clip_type": "stream_outro",
        "preserve_to_video_end": True,
        "outro_trigger": trigger,
        "body": [
            f"·检测到收播开始语：“{trigger}”",
            "·保留从收播告别到录播实际结束的完整互动",
            f"·系列切片：{title}",
        ],
    }


def _snap_clip_to_srt_segments(
    start_s,
    end_s,
    srt_segments,
    natural_pre_max_sec=TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC,
    natural_post_max_sec=TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC,
):
    """吸附完整字幕句，并沿连续讲话延伸到最近的自然停顿。"""
    if not srt_segments:
        return start_s, end_s
    segments = sorted(srt_segments, key=lambda item: (item[0], item[1]))
    related_indexes = [
        index
        for index, segment in enumerate(segments)
        if segment[1] >= start_s and segment[0] <= end_s
    ]
    if not related_indexes:
        return start_s, end_s

    first_index = related_indexes[0]
    last_index = related_indexes[-1]
    first_segment = segments[first_index]
    last_segment = segments[last_index]
    snapped_start = min(start_s, first_segment[0]) if start_s - first_segment[0] <= 30 else start_s
    snapped_end = max(end_s, last_segment[1]) if last_segment[1] - end_s <= 90 else end_s

    cursor = first_index - 1
    current_start = first_segment[0]
    earliest_start = start_s - natural_pre_max_sec
    while cursor >= 0:
        previous = segments[cursor]
        gap = max(0.0, current_start - previous[1])
        if gap > TOPIC_CONTEXT_GAP or previous[0] < earliest_start:
            break
        snapped_start = min(snapped_start, previous[0])
        current_start = previous[0]
        cursor -= 1

    cursor = last_index + 1
    current_end = last_segment[1]
    latest_start = end_s + natural_post_max_sec
    while cursor < len(segments):
        following = segments[cursor]
        gap = max(0.0, following[0] - current_end)
        if gap > TOPIC_CONTEXT_GAP or following[0] > latest_start:
            break
        snapped_end = max(snapped_end, following[1])
        current_end = following[1]
        cursor += 1

    return snapped_start, snapped_end


def _integer_clip_bounds_outside_subtitles(start_s, end_s, srt_segments):
    """整数化时向外避开字幕句，防止 floor/ceil 反而落进相邻句内部。"""
    start_point = math.floor(max(0, start_s))
    end_point = math.ceil(max(end_s, start_s + 1))
    if not srt_segments:
        return start_point, end_point

    while True:
        blocking = [segment for segment in srt_segments if segment[0] < start_point < segment[1]]
        if not blocking:
            break
        earlier = math.floor(min(segment[0] for segment in blocking))
        if earlier >= start_point:
            earlier = start_point - 1
        start_point = max(0, earlier)

    while True:
        blocking = [segment for segment in srt_segments if segment[0] < end_point < segment[1]]
        if not blocking:
            break
        later = math.ceil(max(segment[1] for segment in blocking))
        if later <= end_point:
            later = end_point + 1
        end_point = later
    return start_point, max(end_point, start_point + 1)


def _fit_final_clip_to_safe_srt_boundaries(mark, srt_segments):
    """限长与去重后向内避开字幕句，避免最终整数边界重新切断一句话。"""
    item = _cap_expanded_clip_mark(mark)
    if item.get("preserve_to_video_end"):
        return _refresh_natural_boundary_metadata(item)
    start_point = int(item["start"])
    end_point = int(item["end"])
    if not srt_segments:
        return _refresh_natural_boundary_metadata(item)

    protect_validated_end = (
            not item.get("duration_capped")
            and (
                item.get("semantic_focus_validated")
                or item.get("required_context_end") is not None
            )
    )
    minimum_safe_end = start_point + 1
    if protect_validated_end:
        required_context_end = int(item.get("required_context_end", minimum_safe_end))
        hard_context_end = int(item.get("hard_context_end", required_context_end))
        minimum_safe_end = max(
            minimum_safe_end,
            int(item.get("topic_end", minimum_safe_end)),
            min(required_context_end, hard_context_end),
        )
        end_point = max(end_point, minimum_safe_end)

    while True:
        blocking = [
            segment for segment in srt_segments
            if segment[0] < start_point < segment[1]
        ]
        if not blocking:
            break
        start_point = math.ceil(max(segment[1] for segment in blocking))

    original_end_point = end_point
    while True:
        blocking = [
            segment for segment in srt_segments
            if segment[0] < end_point < segment[1]
        ]
        if not blocking:
            break
        inward_end = math.floor(min(segment[0] for segment in blocking))
        if protect_validated_end and inward_end < minimum_safe_end:
            end_point = original_end_point
            item["end_boundary_kept_to_avoid_core_loss"] = True
            break
        end_point = inward_end

    if item.get("duration_capped"):
        chain_start = _capped_speech_chain_start(
            end_point,
            int(item.get("topic_end", start_point)),
            srt_segments,
        )
        if chain_start is not None:
            end_point = chain_start

    if end_point <= start_point:
        return _refresh_natural_boundary_metadata(item)
    item["start"] = start_point
    item["end"] = end_point
    return _refresh_natural_boundary_metadata(item)


def _capped_speech_chain_start(boundary, topic_end, srt_segments, max_rewind_sec=30):
    """限长点切进连续语链时，回退到该语链开头，避免半句话结束。"""
    segments = sorted(srt_segments or [], key=lambda item: (item[0], item[1]))
    if not segments:
        return None

    previous_index = None
    following_index = None
    for index, (seg_start, seg_end, _) in enumerate(segments):
        if seg_start < boundary:
            previous_index = index
        if following_index is None and seg_start >= boundary:
            following_index = index
        if seg_start < boundary < seg_end:
            following_index = index
            previous_index = index - 1 if index > 0 else None
            break
        if seg_start > boundary + TOPIC_CONTEXT_GAP:
            break

    if following_index is None:
        return None
    following = segments[following_index]
    if previous_index is None:
        return None
    previous = segments[previous_index]
    if following[0] - previous[1] > TOPIC_CONTEXT_GAP:
        return None

    chain_index = following_index
    while chain_index > 0:
        candidate = segments[chain_index - 1]
        current = segments[chain_index]
        if current[0] - candidate[1] > TOPIC_CONTEXT_GAP:
            break
        if boundary - candidate[0] > max_rewind_sec:
            break
        chain_index -= 1

    chain_start = int(math.floor(segments[chain_index][0]))
    if chain_start < topic_end or boundary - chain_start > max_rewind_sec:
        return None
    return chain_start


def _looks_like_sc_or_gift_trigger(text):
    """判断字幕文本是否像 SC/礼物/付费留言触发点；兼容 ASR 把 SC 漏识别的情况。"""
    compact = re.sub(r'\s+', ' ', (text or "")).strip()
    if not compact:
        return False
    lower = compact.lower()
    if any(keyword in lower for keyword in SC_TRIGGER_KEYWORDS):
        return True
    return bool(THANKS_TRIGGER_RE.search(compact))


def _is_explicit_sc_trigger(text):
    """只有明确识别到 SC/醒目留言时，才允许跨较长时间回溯。"""
    lower = re.sub(r'\s+', ' ', (text or "")).strip().lower()
    return any(keyword in lower for keyword in (
        "sc", "s c", "super chat", "superchat", "醒目留言", "醒目", "付费留言",
    ))


def _gift_trigger_has_question_followup(index, topic_start, srt_segments, window_sec=45):
    """ASR 漏掉 SC 名词时，用紧随礼物感谢后的提问文本确认关联。"""
    if not 0 <= index < len(srt_segments):
        return False
    trigger_start = srt_segments[index][0]
    texts = []
    for seg_start, _, text in srt_segments[index:index + 12]:
        if seg_start > topic_start or seg_start > trigger_start + window_sec:
            break
        texts.append(text or "")
    compact = re.sub(r'\s+', '', "".join(texts))
    return bool(re.search(
        r'(?:他说|她说|音悦生说|观众说|问|留言).{0,50}'
        r'(?:吗|呢|怎么|为何|为什么|能不能|可不可以|怎么办|[？?])',
        compact,
    ))


def _find_sc_context_start(topic_start, srt_segments, lookback_sec=SC_CONTEXT_LOOKBACK_SEC):
    """在话题前回溯 SC/礼物触发字幕，返回应纳入切片的更早起点。"""
    if not srt_segments:
        return None
    window_start = max(0, topic_start - lookback_sec)
    candidates = [
        (idx, seg)
        for idx, seg in enumerate(srt_segments)
        if window_start <= seg[0] <= topic_start and _looks_like_sc_or_gift_trigger(seg[2])
    ]
    if not candidates:
        return None

    eligible = []
    for idx, seg in candidates:
        distance = topic_start - seg[0]
        if (
            distance <= SC_FALLBACK_GIFT_LOOKBACK_SEC
            or _is_explicit_sc_trigger(seg[2])
            or _gift_trigger_has_question_followup(idx, topic_start, srt_segments)
        ):
            eligible.append((idx, seg))
    if not eligible:
        return None

    idx, seg = eligible[-1]  # 用离话题最近的触发点，避免把更早无关礼物也切进来。
    start_s = seg[0]
    # SC 文本可能被 ASR 切成几句，向前吸附很近的连续字幕，保留完整提问/感谢。
    cursor = idx - 1
    while cursor >= 0:
        prev_start, prev_end, _ = srt_segments[cursor]
        if start_s - prev_end > TOPIC_CONTEXT_GAP or topic_start - prev_start > lookback_sec:
            break
        start_s = prev_start
        cursor -= 1
    return start_s


_TRIGGER_CONTEXT_TOPIC_RE = re.compile(
    r'(?:\bSC\b|s\s*c|super\s*chat|醒目留言|付费留言|'
    r'观众.{0,10}(?:留言|提问|问题|投稿|来信)|'
    r'(?:念|读|回应|回答).{0,10}(?:留言|提问|问题|投稿|来信)|'
    r'感谢.{0,12}(?:礼物|舰长|提督|总督)|(?:礼物|舰长|提督|总督).{0,10}(?:感谢|回应))',
    re.IGNORECASE,
)


def _clip_context_requires_trigger(mark):
    """判断话题是否确实由 SC、留言或礼物触发，避免普通话题回溯无关感谢。"""
    if "context_requires_trigger" in mark:
        return bool(mark.get("context_requires_trigger"))
    text = " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
        *[str(line) for line in mark.get("body") or []],
    ])
    return bool(_TRIGGER_CONTEXT_TOPIC_RE.search(text))


_TOPIC_LEAD_IN_TRIGGER_RE = re.compile(
    r'(?:对了|说到这个|说起来|你们猜|有个音悦生|有位音悦生|看到一条|念一条|刚才有|'
    r'昨天.{0,20}(?:发|送|问)|今天发的|接下来(?:看|玩)|'
    r'下一个(?:视频|话题|游戏|评价|差评|案例|商家|商品)?)'
)


_NEXT_CASE_ASR_TRIGGER_RE = re.compile(
    r'(?:再(?:看|能|给你)|来看|出给|给你|接着看|继续看)下(?:一(?:个)?|个)|'
    r'下一个(?:视频|话题|游戏|评价|差评|案例|商家|商品)?|'
    r'(?:看看|看一下)(?:他|她|商家|顾客|用户).{0,6}(?:说|写)(?:了)?什么|'
    r'(?:谁|有谁)记得(?:上次|之前).{0,20}(?:吗|嘛)?'
)


_TOPIC_DECISION_EVIDENCE_RE = re.compile(
    r'(?:判断|如何|是否|怎么办|怎么处理|结论|退款|退钱|退回|赔偿|补偿|换货)'
)


_TOPIC_CONCLUSION_RE = re.compile(
    r'(?:我觉得|所以|那就|这样(?:的话)?|应该|最终|最后|结论|总之|看来|结果|决定)'
    r'.{0,40}(?:可以|不可以|不行|不用|展示|通过|驳回|解决|处理|算了|'
    r'退款|退钱|退回|退掉|退了|赔偿|补偿|换货|保留|删除)|'
    r'(?:把|给).{0,20}(?:钱|款).{0,8}退(?:回|掉|了)|'
    r'(?:退款|退钱|退回|退掉|返钱)'
)


_TOPIC_REFUND_RE = re.compile(r'(?:退款|退钱|退回|退掉|退了|返钱|把.{0,20}钱.{0,8}退)')


_TOPIC_DISCOURSE_CONTINUATION_RE = re.compile(
    r'^(?:主要是|而且|然后|所以|但是|不过|就是|对(?:啊|呀|的)|确实|其实|'
    r'我想说|可怜|恭喜)|^.{0,16}(?:还(?:要|会|真|拿|点|给|说|有|在|数|是)|再补充)'
)


_BOUNDARY_SEMANTIC_SIGNAL_PATTERNS = {
    # 直播口语常用不同词重复同一种身体反应；只靠二字以上字面重合会把
    # “想吐/发晕”之后的“低血糖/眩晕”误判成新话题并提前截断。
    "physical_distress": re.compile(
        r'(?:想吐|恶心|反胃|发晕|晕眩|眩晕|头晕|低血糖|站不稳|'
        r'身体受不住|身体受不了|吃不消)'
    ),
}


_VISUAL_CASE_SHIFT_RE = re.compile(
    r'(?:左边|右边).{0,30}(?:赠品|原厂|非原装|遥控器|商品|图片)|'
    r'(?:原厂|非原装).{0,20}(?:遥控器|商品)|'
    r'这两个.{0,12}(?:遥控器|商品|图片)'
)


_VISUAL_REVIEW_TOPIC_RE = re.compile(
    r'(?:评价|差评|评论|照片|图片|视频|投稿|商品|外卖|美团|手套|画面)'
)


_VISUAL_REACTION_LEAD_IN_RE = re.compile(
    r'(?:这是在干|这到底是|谁.{0,8}(?:弄|放|干)|哪一个环节|'
    r'怎么回事|放大看|看一下.{0,8}(?:规格|图片)|这是什么)'
)


_BOUNDARY_EVIDENCE_STOP_TERMS = {
    "主播", "观众", "商家", "外卖", "评价", "差评", "这个", "那个",
    "真的", "然后", "开始", "继续", "感谢", "觉得", "表示", "看到", "观看",
    "内容", "话题", "视频", "弹幕", "回应", "一个", "没有", "怎么", "什么",
    "就是", "还是", "可以", "不是", "因为", "所以", "一下", "自己", "进行",
    "发现", "游戏", "关系",
    "默认",
}


def _normalise_boundary_evidence_text(value):
    return re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+', '', str(value or "")).lower()


def _boundary_evidence_term_counts(mark):
    """从标题和复核要点提取边界关键词；短词重复出现时权重更高。"""
    evidence = [
        mark.get("title", ""),
        mark.get("publish_title", ""),
        *(mark.get("boundary_evidence") or []),
    ]
    stop_terms = set(_BOUNDARY_EVIDENCE_STOP_TERMS)
    stop_terms.update(_profile_identity_names(current_streamer_profile()))
    counts = defaultdict(int)
    for value in evidence:
        normalised = _normalise_boundary_evidence_text(value)
        for run in re.findall(r'[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}', normalised):
            if re.fullmatch(r'[a-z0-9]+', run):
                counts[run] += 2
                continue
            for size in range(2, min(6, len(run)) + 1):
                for offset in range(len(run) - size + 1):
                    term = run[offset:offset + size]
                    if not any(
                        stop_term in term
                        for stop_term in stop_terms
                        if len(stop_term) >= 2
                    ):
                        counts[term] += 1
    return counts


def _score_boundary_evidence_text(text, term_counts):
    normalised = _normalise_boundary_evidence_text(text)
    score = 0
    for term, count in term_counts.items():
        if term not in normalised:
            continue
        length_weight = 1 if len(term) == 2 else 3 if len(term) == 3 else 5 if len(term) == 4 else 7
        score += length_weight + min(3, count - 1)
    return score


def _boundary_evidence_text_is_relevant(text, term_counts):
    """短句命中多次出现在标题/要点中的核心词时，也视为同话题证据。"""
    if _score_boundary_evidence_text(text, term_counts) >= TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE:
        return True
    normalised = _normalise_boundary_evidence_text(text)
    return any(
        len(term) >= 2 and count >= 4 and term in normalised
        for term, count in term_counts.items()
    )


def _boundary_semantic_signals(mark):
    """提取少量高置信同义概念，弥补 ASR 边界处的字面词形变化。"""

    evidence = " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
        *[str(item) for item in mark.get("boundary_evidence") or []],
    ])
    return {
        name
        for name, pattern in _BOUNDARY_SEMANTIC_SIGNAL_PATTERNS.items()
        if pattern.search(evidence)
    }


def _boundary_text_has_semantic_signal(text, semantic_signals):
    """判断后续语链是否延续候选已经明确出现的高置信概念。"""

    return any(
        _BOUNDARY_SEMANTIC_SIGNAL_PATTERNS[name].search(str(text or ""))
        for name in semantic_signals
    )


def _subtitle_speech_chains(srt_segments, minimum, maximum):
    selected = [
        segment for segment in srt_segments or []
        if segment[1] >= minimum and segment[0] <= maximum
    ]
    chains = []
    for segment in selected:
        if not chains or segment[0] - chains[-1][-1][1] > TOPIC_CONTEXT_GAP:
            chains.append([segment])
        else:
            chains[-1].append(segment)
    return chains


def _split_chain_crossing_topic_end(chain, topic_end):
    """拆开跨过核心终点的语链，避免后续新话题被连续语音整体吸入。"""
    if not chain or chain[0][0] > topic_end + 1 or chain[-1][1] <= topic_end:
        return [chain]

    core = [segment for segment in chain if segment[0] < topic_end]
    trailing = [segment for segment in chain if segment[0] >= topic_end]
    split = [core] if core else []
    split.extend([[segment] for segment in trailing])
    return split or [chain]


def _find_relevant_topic_context_start(mark, topic_start, topic_end, srt_segments):
    """用标题/要点匹配离核心最近的连续语链，识别真正案由起点。"""
    term_counts = _boundary_evidence_term_counts(mark)
    if not term_counts or not srt_segments:
        return None, 0
    reference_start = int(mark.get("reference_start", topic_start))
    # AI 参考起点也可能落在一句话中间，额外回看 15 秒恢复完整引子。
    search_start = max(
        0,
        reference_start - 15,
        topic_start - TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC,
    )
    search_end = min(topic_end, topic_start + TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC)
    chains = _subtitle_speech_chains(srt_segments, search_start, search_end)
    candidates = []
    for chain_index, chain in enumerate(chains):
        score = _score_boundary_evidence_text(
            " ".join(segment[2] for segment in chain),
            term_counts,
        )
        if score < TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE:
            continue
        chain_start = max(float(search_start), float(chain[0][0]))
        chain_end = min(float(search_end), float(chain[-1][1]))
        midpoint_distance = abs((chain_start + chain_end) / 2 - topic_start)
        candidates.append((midpoint_distance, -score, chain_index, chain_start, score))
    if not candidates:
        return None, 0
    _, _, chain_index, chain_start, score = min(candidates)
    while chain_index > 0:
        previous_chain = chains[chain_index - 1]
        gap = chain_start - previous_chain[-1][1]
        previous_score = _score_boundary_evidence_text(
            " ".join(segment[2] for segment in previous_chain),
            term_counts,
        )
        if gap > TOPIC_HARD_TRANSITION_GAP_SEC or previous_score <= 0:
            break
        chain_start = max(float(search_start), float(previous_chain[0][0]))
        chain_index -= 1
    return int(math.floor(chain_start)), int(score)


def _boundary_context_has_speech(start_s, end_s, srt_segments):
    if end_s <= start_s:
        return False
    return any(
        seg_end > start_s and seg_start < end_s
        for seg_start, seg_end, _ in srt_segments or []
    )


def _boundary_context_is_relevant(mark, start_s, end_s, srt_segments):
    texts = [
        text for seg_start, seg_end, text in srt_segments or []
        if seg_end > start_s and seg_start < end_s
    ]
    if not texts:
        return False
    return _score_boundary_evidence_text(
        " ".join(texts),
        _boundary_evidence_term_counts(mark),
    ) >= TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE


def _looks_like_next_case_transition(text):
    """识别“再看下一个”及常见 ASR 误识别，避免吞入下一案例。"""
    compact = re.sub(r'\s+', '', text or "")
    return bool(_NEXT_CASE_ASR_TRIGGER_RE.search(compact))


def _looks_like_delayed_topic_conclusion(mark, text, term_counts):
    """识别与案由相符、但在短暂停顿后才说出的判断或退款结论。"""
    compact = re.sub(r'\s+', '', text or "")
    if not compact or not _TOPIC_CONCLUSION_RE.search(compact):
        return False
    evidence = re.sub(r'\s+', '', " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
        *[str(item) for item in mark.get("boundary_evidence") or []],
    ]))
    if _TOPIC_REFUND_RE.search(evidence) and _TOPIC_REFUND_RE.search(compact):
        return True
    if _TOPIC_DECISION_EVIDENCE_RE.search(evidence):
        return True
    return _score_boundary_evidence_text(compact, term_counts) > 0


def _looks_like_discourse_continuation(text):
    """识别短暂停顿后以“主要是/还……”承接上一话题的补充句。"""
    return bool(_TOPIC_DISCOURSE_CONTINUATION_RE.search(
        re.sub(r'\s+', '', text or "")
    ))


def _looks_like_low_score_visual_case_shift(text, term_counts):
    """识别未说“下一个”、但画面和对象已明显切换的新案例。"""
    compact = re.sub(r'\s+', '', text or "")
    return bool(
        _VISUAL_CASE_SHIFT_RE.search(compact)
        and _score_boundary_evidence_text(compact, term_counts)
        < TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
    )


def _next_report_topic_safe_boundary(next_topic_start, topic_end, srt_segments):
    """下一话题时间落在字幕句内时，允许当前片段保留该句到句末。"""
    next_topic_start = int(next_topic_start)
    for seg_start, seg_end, _ in srt_segments or []:
        if seg_end <= topic_end:
            continue
        if seg_start >= next_topic_start:
            break
        if seg_start < next_topic_start < seg_end and seg_end - next_topic_start <= 10:
            return int(math.ceil(seg_end)), float(seg_start)
    return next_topic_start, None


def _find_relevant_topic_context_end(mark, topic_end, search_end, srt_segments):
    """保留静默后的同话题回应，并返回首个确认无关的后续语链起点。"""
    if not mark.get("boundary_evidence") or search_end <= topic_end:
        return topic_end, None, None

    term_counts = _boundary_evidence_term_counts(mark)
    semantic_signals = _boundary_semantic_signals(mark)
    chains = _subtitle_speech_chains(srt_segments, topic_end, search_end)
    if not chains:
        return topic_end, None, None

    records = []
    review_chains = [
        split_chain
        for chain in chains
        for split_chain in _split_chain_crossing_topic_end(chain, topic_end)
    ]
    for chain in review_chains:
        transition_start = next(
            (
                seg_start for seg_start, _, text in chain
                if (
                    seg_start >= topic_end
                    and (
                        _looks_like_next_case_transition(text)
                        or _looks_like_low_score_visual_case_shift(text, term_counts)
                    )
                )
            ),
            None,
        )
        evidence_text = " ".join(
            text for seg_start, _, text in chain
            if transition_start is None or seg_start < transition_start
        )
        records.append({
            "chain": chain,
            "start": max(float(topic_end), float(chain[0][0])),
            "end": max(float(topic_end), float(chain[-1][1])),
            "score": _score_boundary_evidence_text(evidence_text, term_counts),
            "conclusion": _looks_like_delayed_topic_conclusion(
                mark,
                evidence_text,
                term_counts,
            ),
            "transition_start": transition_start,
        })

    nearby_transition = next(
        (
            record["transition_start"] for record in records
            if (
                record["transition_start"] is not None
                and record["transition_start"] - topic_end <= 90
            )
        ),
        None,
    )
    if nearby_transition is not None:
        prior_segments = [
            segment
            for record in records
            for segment in record["chain"]
            if segment[0] < nearby_transition
        ]
        context_end = max(
            [float(topic_end), *[segment[1] for segment in prior_segments]]
        )
        return int(math.ceil(context_end)), int(math.floor(nearby_transition)), None

    context_end = float(topic_end)
    natural_grace_used = False
    relevant_context_seen = False
    delayed_conclusion_end = None
    for index, record in enumerate(records):
        transition_start = record["transition_start"]
        if transition_start is not None:
            before_transition = [
                segment for segment in record["chain"]
                if segment[0] < transition_start
            ]
            if before_transition:
                context_end = max(context_end, before_transition[-1][1])
            return (
                int(math.ceil(context_end)),
                int(math.floor(transition_start)),
                delayed_conclusion_end,
            )

        starts_inside_core = record["chain"][0][0] <= topic_end + 1
        record_text = " ".join(segment[2] for segment in record["chain"])
        evidence_relevant = (
            (
                _boundary_evidence_text_is_relevant(record_text, term_counts)
                or _boundary_text_has_semantic_signal(
                    record_text,
                    semantic_signals,
                )
            )
            and record["start"] - context_end <= TOPIC_RELEVANT_CONTINUATION_GAP_SEC
        )
        delayed_conclusion = (
            relevant_context_seen
            and record["conclusion"]
            and record["start"] - context_end <= TOPIC_RELEVANT_CONTINUATION_GAP_SEC
        )
        discourse_continuation = (
            record["start"] - context_end <= TOPIC_HARD_TRANSITION_GAP_SEC
            and _looks_like_discourse_continuation(
                " ".join(segment[2] for segment in record["chain"])
            )
        )
        natural_closure = (
            not natural_grace_used
            and record["start"] - context_end
            <= TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
        )
        if (
                starts_inside_core
                or evidence_relevant
                or delayed_conclusion
                or discourse_continuation
                or natural_closure):
            context_end = max(context_end, record["end"])
            if delayed_conclusion:
                delayed_conclusion_end = max(
                    delayed_conclusion_end or topic_end,
                    int(math.ceil(record["end"])),
                )
            relevant_context_seen = (
                relevant_context_seen or starts_inside_core or evidence_relevant
            )
            if record["chain"][0][0] >= topic_end and natural_closure:
                natural_grace_used = True
            continue

        future_relevant = False
        for future in records[index + 1:]:
            if future["transition_start"] is not None:
                break
            if future["start"] - context_end > TOPIC_RELEVANT_CONTINUATION_GAP_SEC:
                break
            future_text = " ".join(segment[2] for segment in future["chain"])
            if (
                    _boundary_evidence_text_is_relevant(future_text, term_counts)
                    or _boundary_text_has_semantic_signal(
                        future_text,
                        semantic_signals,
                    )):
                future_relevant = True
                break
            if relevant_context_seen and future["conclusion"]:
                future_relevant = True
                break
        if future_relevant:
            continue
        return (
            int(math.ceil(context_end)),
            int(math.floor(record["start"])),
            delayed_conclusion_end,
        )

    return int(math.ceil(context_end)), None, delayed_conclusion_end


def _find_topic_lead_in_start(reference_start, topic_start, srt_segments):
    """长话题的 AI 核心偏晚时，从参考范围内恢复明确的新话题触发语句。"""
    if not srt_segments or topic_start - reference_start < 30:
        return None
    search_start = max(reference_start, topic_start - TOPIC_LEAD_IN_LOOKBACK_SEC)
    triggers = []
    for seg_start, _, text in srt_segments:
        if seg_start < search_start:
            continue
        if seg_start >= topic_start:
            break
        if _TOPIC_LEAD_IN_TRIGGER_RE.search(re.sub(r'\s+', '', text or "")):
            triggers.append(seg_start)
    if not triggers:
        return None

    # 同一引子可能连续拆成“对了 / 你们猜”等数句。取离核心最近的
    # 一组触发词，但保留该组第一句。
    cluster_start = triggers[0]
    previous = triggers[0]
    for trigger in triggers[1:]:
        if trigger - previous > 20:
            cluster_start = trigger
        previous = trigger
    return cluster_start


def _find_visual_reaction_context_start(mark, topic_start, srt_segments):
    """为看图/评价类话题保留尚未说出主体名称时的第一反应。"""
    evidence_text = " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
        *[str(line) for line in mark.get("boundary_evidence") or []],
    ])
    if not _VISUAL_REVIEW_TOPIC_RE.search(evidence_text):
        return None

    search_start = max(0, topic_start - TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC)
    triggers = [
        seg_start
        for seg_start, _, text in srt_segments or []
        if (
            search_start <= seg_start < topic_start
            and _VISUAL_REACTION_LEAD_IN_RE.search(re.sub(r'\s+', '', text or ""))
        )
    ]
    if not triggers:
        return None

    cluster_start = triggers[0]
    previous = triggers[0]
    for trigger in triggers[1:]:
        if trigger - previous > 30:
            cluster_start = trigger
        previous = trigger
    return int(math.floor(cluster_start))


def _is_explicit_sc_topic(mark):
    text = " ".join([
        str(mark.get("title", "")),
        str(mark.get("publish_title", "")),
    ]).lower()
    return bool(re.search(r'(?:\bsc\b|s\s*c|super\s*chat|醒目留言|付费留言)', text))


def _find_next_topic_hard_end(
        topic_end, reference_end, search_end, srt_segments,
        stop_at_gift_trigger=False):
    """核心已到参考范围末尾时，用明确转场阻止固定后文吞入下一话题。"""
    if (
        not srt_segments
        or (not stop_at_gift_trigger and reference_end - topic_end > 5)
    ):
        return None
    for index, (seg_start, _, text) in enumerate(srt_segments):
        if seg_start < topic_end:
            continue
        if seg_start > search_end:
            break
        compact = re.sub(r'\s+', '', text or "")
        if not (
            _TOPIC_LEAD_IN_TRIGGER_RE.search(compact)
            or _is_explicit_sc_trigger(compact)
            or (
                _looks_like_sc_or_gift_trigger(compact)
                and (
                    stop_at_gift_trigger
                    or _gift_trigger_has_question_followup(
                        index,
                        search_end,
                        srt_segments,
                    )
                )
            )
        ):
            continue
        latest_boundary = math.floor(seg_start)
        boundary = _nearest_safe_srt_boundary(
            latest_boundary,
            math.ceil(topic_end),
            latest_boundary,
            srt_segments,
        )
        return boundary if boundary is not None else latest_boundary
    return None


def _expand_clip_mark_with_context(mark, srt_segments=None, video_duration=None):
    """把 LLM 标记的话题范围扩展为真正用于 ffmpeg 的前后文切片范围。"""
    topic_start = int(float(mark.get("topic_start", mark["start"])))
    topic_end = int(float(mark.get("topic_end", mark["end"])))
    if topic_end <= topic_start:
        topic_end = topic_start + 1

    relevant_context_start, relevant_context_score = _find_relevant_topic_context_start(
        mark,
        topic_start,
        topic_end,
        srt_segments or [],
    )
    if (
            relevant_context_score >= TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
            and not _clip_context_requires_trigger(mark)
            and topic_start + 5 < relevant_context_start
            and relevant_context_start - topic_start <= TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC):
        topic_start = relevant_context_start

    raw_duration = topic_end - topic_start
    semantic_focus = bool(mark.get("semantic_focus_validated"))
    if semantic_focus:
        reference_start = int(mark.get("reference_start", topic_start))
        reference_end = int(mark.get("reference_end", topic_end))
        pre_context_sec = (
            TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC
            if topic_start - reference_start <= 5
            else TOPIC_AI_FOCUS_PRE_CONTEXT_SEC
        )
        post_context_sec = (
            TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC
            if reference_end - topic_end <= 5
            else TOPIC_AI_FOCUS_POST_CONTEXT_SEC
        )
        natural_pre_max_sec = TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC
        natural_post_max_sec = TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
    else:
        pre_context_sec = TOPIC_PRE_CONTEXT_SEC
        post_context_sec = TOPIC_POST_CONTEXT_SEC
        natural_pre_max_sec = TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC
        natural_post_max_sec = TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC

    start_s = max(0, topic_start - pre_context_sec)
    end_s = topic_end + post_context_sec
    hard_context_end = None
    relevant_context_end = topic_end
    delayed_conclusion_end = None
    if semantic_focus:
        next_topic_start = mark.get("next_report_topic_start")
        next_topic_boundary = None
        next_topic_crossing_start = None
        if next_topic_start is not None and int(next_topic_start) >= topic_end:
            next_topic_start = int(next_topic_start)
            next_topic_boundary, next_topic_crossing_start = (
                _next_report_topic_safe_boundary(
                    next_topic_start,
                    topic_end,
                    srt_segments or [],
                )
            )
        boundary_search_end = min(
            topic_end + TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC,
            topic_start + TOPIC_MAX_CLIP_SEC + TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC,
            int(mark.get("reference_end", topic_end))
            + TOPIC_REFERENCE_END_TOLERANCE_SEC,
        )
        if next_topic_boundary is not None:
            boundary_search_end = min(boundary_search_end, next_topic_boundary)
        (
            relevant_context_end,
            unrelated_next_start,
            delayed_conclusion_end,
        ) = _find_relevant_topic_context_end(
            mark,
            topic_end,
            boundary_search_end,
            srt_segments or [],
        )
        if (
                next_topic_crossing_start is not None
                and next_topic_start - topic_end
                <= TOPIC_RELEVANT_CONTINUATION_GAP_SEC
                and (
                    unrelated_next_start is None
                    or unrelated_next_start >= next_topic_crossing_start
                )):
            # 报告边界偶尔落在“说完这一项”这类跨界字幕中。保留整句，
            # 但若更早已有明确“下一个”，不能跨过它追到报告边界。
            relevant_context_end = max(relevant_context_end, next_topic_boundary)
            if (
                    unrelated_next_start is not None
                    and unrelated_next_start <= next_topic_crossing_start):
                unrelated_next_start = next_topic_boundary
        end_s = max(end_s, relevant_context_end)
        hard_context_end = _find_next_topic_hard_end(
            topic_end,
            int(mark.get("reference_end", topic_end)),
            end_s,
            srt_segments or [],
            stop_at_gift_trigger=_clip_context_requires_trigger(mark),
        )
        if hard_context_end is not None:
            end_s = min(end_s, hard_context_end)
        if next_topic_start is not None:
            if (
                    topic_end - 5 <= next_topic_start <= end_s
                    and (
                        next_topic_start >= relevant_context_end
                        or next_topic_crossing_start is not None
                    )):
                next_topic_hard_end = next_topic_boundary or next_topic_start
                hard_context_end = (
                    min(hard_context_end, next_topic_hard_end)
                    if hard_context_end is not None
                    else next_topic_hard_end
                )
                end_s = min(end_s, hard_context_end)
        if unrelated_next_start is not None:
            hard_context_end = (
                min(hard_context_end, unrelated_next_start)
                if hard_context_end is not None
                else unrelated_next_start
            )
            end_s = min(end_s, hard_context_end)
    sc_context_start = None
    if _clip_context_requires_trigger(mark):
        sc_context_start = _find_sc_context_start(topic_start, srt_segments or [])
    if sc_context_start is not None:
        start_s = min(start_s, sc_context_start)
    lead_in_start = None
    visual_lead_in_start = None
    if (
        semantic_focus
        and raw_duration >= TOPIC_LEAD_IN_RECOVERY_MIN_SEC
        and not _clip_context_requires_trigger(mark)
    ):
        lead_in_start = _find_topic_lead_in_start(
            int(mark.get("reference_start", topic_start)),
            topic_start,
            srt_segments or [],
        )
        if lead_in_start is not None:
            if (
                    mark.get("boundary_evidence")
                    and relevant_context_start is not None
                    and lead_in_start < relevant_context_start
                    and not _boundary_context_is_relevant(
                        mark,
                        lead_in_start,
                        relevant_context_start,
                        srt_segments or [],
                    )):
                lead_in_start = None
    if semantic_focus and not _clip_context_requires_trigger(mark):
        visual_lead_in_start = _find_visual_reaction_context_start(
            mark,
            topic_start,
            srt_segments or [],
        )

    boundary_trimmed_context = False
    if (
            _clip_context_requires_trigger(mark)
            and sc_context_start is None
            and _is_explicit_sc_topic(mark)):
        # 无法在字幕中找到明确 SC 名词时，AI 复核核心起点就是最可信的提问起点；
        # 不再机械带入前一话题的固定 20 秒。
        start_s = topic_start
        boundary_trimmed_context = True
    else:
        semantic_context_starts = [
            value for value in (
                sc_context_start,
                lead_in_start,
                visual_lead_in_start,
                relevant_context_start,
            )
            if value is not None and value <= topic_start
        ]
        if semantic_context_starts:
            semantic_context_start = min(semantic_context_starts)
            if semantic_context_start < start_s:
                start_s = semantic_context_start
            elif (
                    semantic_context_start > start_s
                    and _boundary_context_has_speech(
                        start_s,
                        semantic_context_start,
                        srt_segments or [],
                    )):
                # 语义案由前已有另一段讲话时裁掉；纯静默/画面铺垫仍保留。
                start_s = semantic_context_start
                boundary_trimmed_context = True

    if end_s - start_s < TOPIC_MIN_CLIP_SEC and not boundary_trimmed_context:
        deficit = TOPIC_MIN_CLIP_SEC - (end_s - start_s)
        left = deficit if hard_context_end is not None else int(deficit * 0.4)
        right = 0 if hard_context_end is not None else deficit - left
        start_s = max(0, start_s - left)
        end_s += right

    context_duration_limit = TOPIC_MAX_CLIP_SEC
    if relevant_context_end > topic_end:
        context_duration_limit += TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
    if raw_duration < TOPIC_MAX_CLIP_SEC and end_s - start_s > context_duration_limit:
        end_s = start_s + context_duration_limit
        if end_s < topic_end:
            end_s = topic_end
            start_s = max(0, end_s - context_duration_limit)

    context_start_s = start_s
    context_end_s = end_s
    start_s, end_s = _snap_clip_to_srt_segments(
        start_s,
        end_s,
        srt_segments or [],
        natural_pre_max_sec=natural_pre_max_sec,
        natural_post_max_sec=natural_post_max_sec,
    )
    if hard_context_end is not None:
        end_s = min(end_s, hard_context_end)

    if video_duration:
        end_s = min(end_s, video_duration)
        if end_s - start_s < TOPIC_MIN_CLIP_SEC and start_s > 0:
            start_s = max(0, end_s - TOPIC_MIN_CLIP_SEC)

    expanded = dict(mark)
    expanded["topic_start"] = topic_start
    expanded["topic_end"] = topic_end
    if int(expanded.get("report_start", topic_start)) < topic_start:
        expanded["report_start"] = topic_start
    expanded["start"], expanded["end"] = _integer_clip_bounds_outside_subtitles(
        start_s,
        end_s,
        srt_segments or [],
    )
    if hard_context_end is not None:
        expanded["end"] = min(expanded["end"], int(hard_context_end))
        expanded["hard_context_end"] = int(hard_context_end)
    expanded["time_basis"] = "video_elapsed_seconds"
    expanded["context_expanded"] = True
    expanded["context_pre_sec"] = pre_context_sec
    expanded["context_post_sec"] = post_context_sec
    expanded["context_start_before_natural"] = int(context_start_s)
    expanded["context_end_before_natural"] = int(context_end_s)
    required_context_starts = [
        value for value in (
            sc_context_start,
            lead_in_start,
            visual_lead_in_start,
            relevant_context_start,
        )
        if value is not None and value < topic_start
    ]
    if required_context_starts:
        expanded["required_context_start"] = int(min(required_context_starts))
    if relevant_context_end > topic_end:
        expanded["required_context_end"] = int(relevant_context_end)
    if delayed_conclusion_end is not None:
        expanded["required_context_overflow_end"] = int(delayed_conclusion_end)
    expanded = _cap_expanded_clip_mark(expanded)
    return _refresh_natural_boundary_metadata(expanded)


def _expand_clip_marks_with_context(marks, srt_segments=None, video_duration=None):
    """批量扩展切片上下文；输入/输出时间均为视频内秒数。"""
    outro_marks = [
        dict(mark) for mark in (marks or [])
        if mark.get("clip_type") == "stream_outro" and mark.get("preserve_to_video_end")
    ]
    ordinary_marks = [
        mark for mark in (marks or [])
        if mark.get("clip_type") != "stream_outro"
    ]

    def overlaps_outro(mark):
        try:
            start = float(mark["start"])
            end = float(mark["end"])
        except (KeyError, TypeError, ValueError):
            return False
        return any(
            start < float(outro["end"]) and end > float(outro["start"])
            for outro in outro_marks
        )

    # 收播片是用户明确指定的独立系列。普通尾部候选与它重叠时直接让位，
    # 既不合并成超长杂项，也不会输出两条内容高度重复的视频。
    ordinary_marks = [mark for mark in ordinary_marks if not overlaps_outro(mark)]
    expanded = [
        _expand_clip_mark_with_context(mark, srt_segments=srt_segments, video_duration=video_duration)
        for mark in _dedupe_clip_marks(ordinary_marks)
    ]
    merged = _merge_expanded_clip_marks(expanded, srt_segments=srt_segments)
    ordinary_final = [
        _fit_final_clip_to_safe_srt_boundaries(mark, srt_segments or [])
        for mark in merged
    ]
    ordinary_final = [mark for mark in ordinary_final if not overlaps_outro(mark)]
    outro_final = []
    for mark in _dedupe_clip_marks(outro_marks):
        item = dict(mark)
        if video_duration:
            item["end"] = int(math.ceil(float(video_duration)))
            item["topic_end"] = item["end"]
            item["report_end"] = item["end"]
        outro_final.append(_fit_final_clip_to_safe_srt_boundaries(item, srt_segments or []))
    return sorted(
        [*ordinary_final, *outro_final],
        key=lambda item: (int(item.get("start", 0)), int(item.get("end", 0)), item.get("title", "")),
    )


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


def _topic_peak_candidates(topic, peaks, window_sec=DANMAKU_WINDOW):
    """峰值中心允许一个采样步长误差，兼容字幕边界校正后的重复重建。"""
    if not peaks:
        return []
    start = int(topic["start"])
    end = int(topic["end"])
    return [
        (peak_start, density)
        for peak_start, density in peaks
        if (
            start - DANMAKU_WINDOW_STEP
            <= peak_start + window_sec / 2
            <= end + DANMAKU_WINDOW_STEP
        )
    ]


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
        f"{fmt_time(peak_focus['anchor'])}截取，保留峰值前后完整反应"
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
    fixed["start_str"] = fmt_time(fixed["start"])
    fixed["end_str"] = fmt_time(fixed["end"])
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
        body_lines = [_normalise_body_line(line) for line in topic.get("body") or []]
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
        evidence = f"·弹幕依据：{fmt_time(peak_start)} 附近峰值约 {density:.0f} 条/分钟"
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


def _configured_llm_concurrency():
    """读取受控并发数；默认 3 路，避免过度请求上游服务。"""
    raw_value = os.environ.get(
        "AUTOSLICE_LLM_CONCURRENCY",
        str(LLM_DEFAULT_CONCURRENCY),
    )
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = LLM_DEFAULT_CONCURRENCY
    return max(1, min(LLM_MAX_CONCURRENCY, value))


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


def _serialized_progress_callback(progress_callback):
    """让并发重试日志按完整消息写入 SSE 和控制台。"""
    if not progress_callback:
        return None
    lock = threading.Lock()

    def report(message, step, total):
        with lock:
            progress_callback(message, step, total)

    return report


def analyze_topic_chunks(
        chunks, streamer_display_name, progress_callback=None,
        checkpoint_path=None):
    """逐块独立分析字幕和弹幕；请求并行，结果仍按视频顺序合并。"""
    if not chunks:
        return [], [], None

    total = len(chunks)
    report_progress = _serialized_progress_callback(progress_callback)
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

        concurrency = min(_configured_llm_concurrency(), len(pending))
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
                "time": fmt_time(chunk_start),
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


def _fresh_manual_topic_evidence(topic, srt_segments=None, peaks=None):
    """为后置复核重建原始证据，避免沿用上一轮 AI 摘要造成错误自证。"""
    start = int(topic.get("start", 0))
    end = max(start + 1, int(topic.get("end", start + 1)))
    body = []
    if peaks:
        body.extend(_topic_danmaku_reference_lines(start, end, peaks))
    if srt_segments:
        body.extend(_topic_srt_summary_lines(start, end, srt_segments))

    for entry in topic.get("manual_timeline") or []:
        source_entries = entry.get("original_entries") or [entry]
        for source_entry in source_entries:
            stars = int(source_entry.get("stars", entry.get("stars", 0)))
            prefix = f"●人工时间轴{'⭐' * min(stars, 5)}" if stars else "·时间轴"
            line = (
                f"{prefix}：{fmt_time(int(source_entry.get('start', start)))} "
                f"{source_entry.get('text', '')}"
            )
            if line not in body:
                body.append(line)
    return body


def _clip_review_candidate(
        topic, srt_segments, peaks, density_series=None):
    """用原字幕重新构造高能候选，首轮标题和摘要不作为复核证据。"""
    source_start = int(topic.get("start", 0))
    source_end = max(source_start + 1, int(topic.get("end", source_start + 1)))
    review_start = max(0, source_start - TOPIC_PRE_CONTEXT_SEC)
    review_end = source_end + TOPIC_POST_CONTEXT_SEC
    candidate = dict(topic)
    candidate["start"] = review_start
    candidate["end"] = review_end
    candidate["start_str"] = fmt_time(review_start)
    candidate["end_str"] = fmt_time(review_end)
    core_subtitle_evidence = _topic_srt_summary_lines(
        source_start,
        source_end,
        srt_segments,
    )
    candidate["core_subtitle_evidence"] = [
        _strip_body_prefix(line) for line in core_subtitle_evidence
        if _strip_body_prefix(line)
    ]
    candidate["title_cue_context"] = " ".join([
        str(topic.get("title", "")),
        *candidate["core_subtitle_evidence"],
    ])
    candidate["body"] = _fresh_manual_topic_evidence(
        candidate,
        srt_segments=srt_segments,
        peaks=peaks,
    )
    candidate["review_original_start"] = source_start
    candidate["review_original_end"] = source_end
    density_source = density_series if density_series is not None else peaks
    if not candidate.get("danmaku_content_evidence") and density_source:
        peak_candidates = _topic_peak_candidates(topic, peaks)
        if peak_candidates:
            peak_start, density = max(peak_candidates, key=lambda item: item[1])
            features = _danmaku_peak_features(
                density_source,
                peak_start,
                density,
                avg_density=_average_danmaku_density(density_source),
            )
            candidate["danmaku_peak_start"] = int(peak_start)
            candidate["danmaku_selection_score"] = features["selection_score"]
            candidate["danmaku_local_surge_ratio"] = features["local_surge_ratio"]
            candidate["danmaku_density_percentile"] = features["density_percentile"]
            candidate["danmaku_content_quality"] = features["content_quality"]
            candidate["danmaku_interaction_signal"] = features["interaction_signal"]
            candidate["danmaku_content_evidence"] = features["content_evidence"]
    return candidate




def _parse_clip_interest_score(value):
    """解析 Terra 的投稿价值分；缺失、越界或非有限值都视为结构无效。"""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or not 0 <= score <= 100:
        return None
    return round(score, 1)


def _parse_clip_star_bonus(value):
    """解析强人工星标的有限加分；普通星标不允许产生加分。"""
    bonus = _parse_clip_interest_score(value)
    if bonus is None or bonus > 8:
        return None
    return bonus


def _clip_star_bonus_cap(manual_star_count):
    """按单条人工记录的星标强度限制加分，避免普通标记左右筛选。"""
    try:
        star_count = max(0, int(manual_star_count or 0))
    except (TypeError, ValueError):
        star_count = 0
    if star_count < 3:
        return 0.0
    if star_count == 3:
        return 2.0
    if star_count == 4:
        return 5.0
    return 8.0


def _clip_manual_star_count(topic):
    """读取人工星标数量；异常旧数据按 0 处理，不能阻断审计文件写入。"""
    try:
        return max(0, int((topic or {}).get("manual_stars", 0) or 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def _clip_interest_reason(item):
    """清理投稿价值说明，供检查点审计和拒绝原因使用。"""
    reason = re.sub(r'\s+', ' ', str(
        item.get("interest_reason", item.get("reason", ""))
    )).strip()
    return reason[:240]




def _build_clip_candidate_review_prompt(candidates, streamer_name=None, compact=False):
    """构造切片候选独立复核提示；只把原字幕、峰值和原始人工记录作为证据。"""
    payload = []
    for index, candidate in enumerate(candidates, 1):
        evidence_limit = 10 if compact else 24
        evidence = [
            _strip_body_prefix(line)
            for line in (candidate.get("body") or [])[:evidence_limit]
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
        payload.append({
            "id": index,
            "reference_start": fmt_time(candidate["start"]),
            "reference_end": fmt_time(candidate["end"]),
            "candidate_anchor": fmt_time(candidate.get("slice_anchor", candidate["start"])),
            "candidate_sources": list(candidate.get("clip_candidate_sources") or []),
            "provisional_title": candidate.get("title", "待核查高能片段"),
            "reference_publish_titles": _clip_candidate_reference_publish_titles(candidate),
            "publish_title_locked": bool(candidate.get("publish_title_locked")),
            "manual_star_count": max(0, int(candidate.get("manual_stars", 0) or 0)),
            "danmaku_evidence": _clip_candidate_danmaku_prompt_evidence(candidate),
            "evidence": evidence,
            "subtitle_evidence": subtitle_evidence,
            "core_subtitle_evidence": candidate.get("core_subtitle_evidence") or [],
            "manual_evidence": manual_evidence,
            "density_evidence": density_evidence,
        })
    context = _prompt_context(
        streamer_name,
        context_text=json.dumps(payload, ensure_ascii=False),
        compact=compact,
        publish_title_example_text="具体事件与原话",
    )
    return _render_clip_candidate_review_prompt(
        _ClipCandidatePromptEvidence(
            context=context,
            candidates=tuple(payload),
            focus_max_seconds=TOPIC_REVIEW_FOCUS_MAX_SEC,
            minimum_interest_score=CLIP_MIN_INTEREST_SCORE,
        )
    )


_TOPIC_REVIEW_TRANSIENT_KEYS = checkpoint_store.TOPIC_REVIEW_TRANSIENT_KEYS


def _build_clip_candidate_review_audit(topics):
    """生成面向人工排查的候选明细，不把冗长原因塞进概览或话题报告。"""
    rows = []
    for topic in topics or []:
        sources = [
            str(value).strip()
            for value in topic.get("clip_candidate_sources") or []
            if str(value).strip()
        ]
        reviewed = topic.get("clip_review_validated")
        has_review_state = (
            topic.get("clip_review_attempts") is not None
            or topic.get("clip_review_rejection") is not None
            or reviewed is not None
        )
        if not sources and not has_review_state:
            continue
        if topic.get("can_slice"):
            status = "已通过并生成切片"
        elif reviewed is True:
            status = "已通过复核但未生成切片"
        elif reviewed is False:
            status = "未通过复核"
        else:
            status = "复核未完成"
        rows.append({
            "start": int(topic.get("start", 0) or 0),
            "end": int(topic.get("end", 0) or 0),
            "time_range": (
                f"{fmt_time(int(topic.get('start', 0) or 0))}－"
                f"{fmt_time(int(topic.get('end', 0) or 0))}"
            ),
            "title": str(topic.get("title", "未命名候选")).strip() or "未命名候选",
            "candidate_sources": sources,
            "manual_stars": _clip_manual_star_count(topic),
            "clip_review_validated": reviewed,
            "interest_score": _parse_clip_interest_score(topic.get("clip_interest_score")),
            "interest_reason": topic.get("clip_interest_reason"),
            "rejection_reason": topic.get("clip_review_rejection"),
            "final_slice": bool(topic.get("can_slice")),
            "final_slice_anchor_source": topic.get("slice_anchor_source"),
            "status": status,
        })
    return {
        "review_policy_version": CLIP_REVIEW_POLICY_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(rows),
        "approved_count": sum(row["final_slice"] for row in rows),
        "candidates": sorted(rows, key=lambda row: (row["start"], row["end"], row["title"])),
    }


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
    report_progress = _serialized_progress_callback(progress_callback)
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
                _clip_review_candidate(
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
            prompt = _build_clip_candidate_review_prompt(
                candidates,
                streamer_name=streamer_name,
                compact=False,
            )
            compact_prompt = _build_clip_candidate_review_prompt(
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

        concurrency = min(_configured_llm_concurrency(), max(1, len(jobs)))
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
                        if not _json_can_slice(item.get("valid"), ""):
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
                        enriched = _enriched_manual_topic_from_item(candidate, item)
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
            fresh_evidence = _fresh_manual_topic_evidence(
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
