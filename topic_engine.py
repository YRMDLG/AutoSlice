"""
话题分析 + 智能切片引擎

流水线: FunASR转录 → 弹幕密度分析 → SRT分块 → DeepSeek Pro分析 → 报告 + 切片标记

用法:
  from topic_engine import run_pipeline
  result = run_pipeline(flv_path, ass_path, progress_callback=cb)
  # result: {"report": "...", "clip_marks": [...], "json_path": "..."}
"""

import html
import hashlib
import math
import bisect
import difflib
import os, re, json, time, zipfile, requests, threading, shutil
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta

from autoslice.analysis import candidates as candidate_analysis
from autoslice.analysis import danmaku as danmaku_analysis
from autoslice.analysis import timeline as timeline_analysis
from autoslice.analysis import titles as title_analysis
from autoslice.llm import transport as llm_gateway
from autoslice.llm.prompts import (
    ClipCandidatePromptEvidence as _ClipCandidatePromptEvidence,
    ManualTopicPromptEvidence as _ManualTopicPromptEvidence,
    PromptContext as _PromptContext,
    SYSTEM_PROMPT,
    TITLE_HOOK_PROMPT_GUIDE,
    TopicAnalysisPromptEvidence as _TopicAnalysisPromptEvidence,
    build_clip_candidate_review_prompt as _render_clip_candidate_review_prompt,
    build_final_title_generation_prompt as _render_final_title_generation_prompt,
    build_final_title_judge_prompt as _render_final_title_judge_prompt,
    build_manual_topic_enrichment_prompt as _render_manual_topic_enrichment_prompt,
    build_system_prompt as _render_system_prompt,
    build_title_hook_guide as _render_title_hook_guide,
    build_title_style_prompt as _render_title_style_prompt,
    build_topic_analysis_prompt as _render_topic_analysis_prompt,
)
from autoslice.transcription import service as transcription_service
from autoslice.transcription.contracts import (
    DEFAULT_MAX_PUBLISH_TITLE_CHARS,
    DEFAULT_SUBTITLE_GLOSSARY,
    DEFAULT_SUBTITLE_MAX_CHARS,
    SubtitleTitleServices,
)
from media_formats import (
    SUPPORTED_VIDEO_EXTENSIONS,
    compatible_output_extensions,
    is_analyzable_video,
    is_sliceable_video,
    normalise_video_extension,
    preferred_output_extension,
)

from artifact_store import (
    ARTIFACT_BUNDLE_SUFFIX,
    ARTIFACT_DATA_DIRNAME,
    ARTIFACT_LAYOUT_VERSION,
    ARTIFACT_QUEUE_DIRNAME,
    UNIFIED_REFINEMENT_QUEUE_JSON,
    UNIFIED_REFINEMENT_QUEUE_MD,
    artifact_bundle_layout as _calculate_artifact_bundle_layout,
    artifact_bundle_stem as _artifact_bundle_stem,
    copy_artifact_file as _copy_artifact_file,
    first_existing_artifact_path as _first_existing_artifact_path,
    load_artifact_json as _load_artifact_json,
    markdown_relative_artifact_link as _markdown_relative_artifact_link,
    rewrite_organized_report_links as _rewrite_organized_report_links,
    seed_artifact_from_legacy as _seed_artifact_from_legacy,
    write_artifact_json as _write_artifact_json,
    write_artifact_text as _write_artifact_text,
)
from streamer_profiles import (
    active_streamer_profile,
    current_streamer_profile,
    resolve_streamer_profile,
    streamer_profile_context,
)
from runtime_config import OUTPUT_DIR, TIMELINE_DIR


# 转录兼容 façade：所有符号直接绑定唯一 service 对象，禁止本地再实现。
_infer_streamer_name = transcription_service.infer_streamer_name
FUNASR_BATCH_SIZE_SEC = transcription_service.FUNASR_BATCH_SIZE_SEC
FUNASR_CACHE_MODEL_DIR = transcription_service.FUNASR_CACHE_MODEL_DIR
FUNASR_CHECKPOINT_VERSION = transcription_service.FUNASR_CHECKPOINT_VERSION
FUNASR_CHUNK_PRE_CONTEXT_SEC = transcription_service.FUNASR_CHUNK_PRE_CONTEXT_SEC
FUNASR_CHUNK_SEC = transcription_service.FUNASR_CHUNK_SEC
FUNASR_CONTEXTUAL_CACHE_MODEL_DIR = transcription_service.FUNASR_CONTEXTUAL_CACHE_MODEL_DIR
FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC = transcription_service.FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC
FUNASR_CONTEXTUAL_MODEL = transcription_service.FUNASR_CONTEXTUAL_MODEL
FUNASR_CPU_RETRY_DELAY_SEC = transcription_service.FUNASR_CPU_RETRY_DELAY_SEC
FUNASR_DEFAULT_DEVICE = transcription_service.FUNASR_DEFAULT_DEVICE
FUNASR_FOREGROUND_AUDIO_FILTER = transcription_service.FUNASR_FOREGROUND_AUDIO_FILTER
FUNASR_HOTWORD_MAX_CHARS = transcription_service.FUNASR_HOTWORD_MAX_CHARS
FUNASR_HOTWORD_MAX_COUNT = transcription_service.FUNASR_HOTWORD_MAX_COUNT
FUNASR_MODEL = transcription_service.FUNASR_MODEL
FUNASR_NANO_CACHE_ROOTS = transcription_service.FUNASR_NANO_CACHE_ROOTS
FUNASR_NANO_MODEL = transcription_service.FUNASR_NANO_MODEL
FUNASR_PUNC_CACHE_MODEL_DIR = transcription_service.FUNASR_PUNC_CACHE_MODEL_DIR
FUNASR_PUNC_MODEL = transcription_service.FUNASR_PUNC_MODEL
FUNASR_SPK_CACHE_MODEL_DIR = transcription_service.FUNASR_SPK_CACHE_MODEL_DIR
FUNASR_SPK_MODEL = transcription_service.FUNASR_SPK_MODEL
FUNASR_SPK_WEIGHT_FILES = transcription_service.FUNASR_SPK_WEIGHT_FILES
FUNASR_VAD_CACHE_MODEL_DIR = transcription_service.FUNASR_VAD_CACHE_MODEL_DIR
FUNASR_VAD_MODEL = transcription_service.FUNASR_VAD_MODEL
SRT_ABNORMAL_CHARS_PER_SEC = transcription_service.SRT_ABNORMAL_CHARS_PER_SEC
SRT_MAX_ESTIMATED_SEG_SEC = transcription_service.SRT_MAX_ESTIMATED_SEG_SEC
SRT_REPEAT_REPAIR_MIN_ENTRIES = transcription_service.SRT_REPEAT_REPAIR_MIN_ENTRIES
SUBTITLE_LEGACY_REPAIR_MAX_CHARS = transcription_service.SUBTITLE_LEGACY_REPAIR_MAX_CHARS
SUBTITLE_MAX_CHARS = transcription_service.SUBTITLE_MAX_CHARS
SUBTITLE_MAX_DURATION_SEC = transcription_service.SUBTITLE_MAX_DURATION_SEC
SUBTITLE_PAUSE_BREAK_SEC = transcription_service.SUBTITLE_PAUSE_BREAK_SEC
SUBTITLE_TARGET_CHARS = transcription_service.SUBTITLE_TARGET_CHARS
_repair_srt_end_time = transcription_service._repair_srt_end_time
_join_asr_tokens = transcription_service._join_asr_tokens
_strip_asr_subtitle_punctuation = transcription_service._strip_asr_subtitle_punctuation
_normalise_asr_text = transcription_service._normalise_asr_text
_split_subtitle_text_for_display = transcription_service._split_subtitle_text_for_display
_split_timed_subtitle_segment = transcription_service._split_timed_subtitle_segment
_should_hold_subtitle_for_short_clause = transcription_service._should_hold_subtitle_for_short_clause
_segment_timed_tokens = transcription_service._segment_timed_tokens
_segments_from_funasr_result = transcription_service._segments_from_funasr_result
_read_srt_entries = transcription_service._read_srt_entries
export_corrected_srt = transcription_service.export_corrected_srt
_probe_video_duration = transcription_service.probe_video_duration
_prepare_funasr_environment = transcription_service._prepare_funasr_environment
_funasr_model_cache_candidates = transcription_service.funasr_model_cache_candidates
_funasr_nano_cache_candidates = transcription_service._funasr_nano_cache_candidates
_resolve_funasr_model_source = transcription_service.resolve_funasr_model_source
_resolve_funasr_aux_model_source = transcription_service.resolve_funasr_aux_model_source
_resolve_funasr_speaker_model_source = transcription_service.resolve_funasr_speaker_model_source
_funasr_model_runtime_signature = transcription_service._funasr_model_runtime_signature
_funasr_hotwords = transcription_service._funasr_hotwords
_funasr_generate_kwargs = transcription_service._funasr_generate_kwargs
_resolve_funasr_device = transcription_service.resolve_funasr_device
funasr_public_status = transcription_service.funasr_public_status
_load_funasr_model = transcription_service.load_funasr_model
_funasr_checkpoint_path = transcription_service.funasr_checkpoint_path
_funasr_source_fingerprint = transcription_service._funasr_source_fingerprint
_funasr_chunk_fingerprint = transcription_service._funasr_chunk_fingerprint
_funasr_chunk_input_window = transcription_service._funasr_chunk_input_window
_normalise_funasr_result = transcription_service._normalise_funasr_result
_is_valid_funasr_result = transcription_service._is_valid_funasr_result
_primary_speaker_segments = transcription_service._primary_speaker_segments
_is_close_number = transcription_service._is_close_number
_prepare_funasr_checkpoint = transcription_service._prepare_funasr_checkpoint
_write_funasr_checkpoint = transcription_service.write_funasr_checkpoint
_clear_funasr_cuda_cache = transcription_service.clear_funasr_cuda_cache
_dedupe_overlapping_funasr_segments = transcription_service._dedupe_overlapping_funasr_segments
_is_funasr_punctuation = transcription_service._is_funasr_punctuation
_attach_funasr_punctuation_to_tokens = transcription_service._attach_funasr_punctuation_to_tokens
_align_funasr_tokens = transcription_service._align_funasr_tokens
_trim_funasr_tokens_to_core = transcription_service._trim_funasr_tokens_to_core
ensure_srt = transcription_service.ensure_srt
_srt_time = transcription_service.srt_time
_parse_srt_timestamp = transcription_service.parse_srt_timestamp



# 弹幕兼容 façade：解析、峰值和刷屏降权只保留唯一实现。
CLIP_DENSITY_PERCENTILE = danmaku_analysis.CLIP_DENSITY_PERCENTILE
CLIP_DENSITY_RATIO = danmaku_analysis.CLIP_DENSITY_RATIO
CLIP_LOCAL_PEAK_RADIUS_SEC = danmaku_analysis.CLIP_LOCAL_PEAK_RADIUS_SEC
DANMAKU_EVIDENCE_MAX_ITEMS = danmaku_analysis.DANMAKU_EVIDENCE_MAX_ITEMS
DANMAKU_LOCAL_BASELINE_EXCLUSION_SEC = danmaku_analysis.DANMAKU_LOCAL_BASELINE_EXCLUSION_SEC
DANMAKU_LOCAL_BASELINE_RADIUS_SEC = danmaku_analysis.DANMAKU_LOCAL_BASELINE_RADIUS_SEC
DANMAKU_MESSAGE_MAX_CHARS = danmaku_analysis.DANMAKU_MESSAGE_MAX_CHARS
_ASS_OVERRIDE_TAG_RE = danmaku_analysis._ASS_OVERRIDE_TAG_RE
_DANMAKU_BRACKET_EMOTE_RE = danmaku_analysis._DANMAKU_BRACKET_EMOTE_RE
_DANMAKU_GENERIC_REACTIONS = danmaku_analysis._DANMAKU_GENERIC_REACTIONS
_DANMAKU_PROMPT_INSTRUCTION_RE = danmaku_analysis._DANMAKU_PROMPT_INSTRUCTION_RE
_DANMAKU_TITLE_CUE_GROUPS = danmaku_analysis._DANMAKU_TITLE_CUE_GROUPS
_DANMAKU_TITLE_CUE_PRIORITY_PATTERNS = danmaku_analysis._DANMAKU_TITLE_CUE_PRIORITY_PATTERNS
_DANMAKU_UPOWER_RE = danmaku_analysis._DANMAKU_UPOWER_RE
DanmakuDensitySeries = danmaku_analysis.DanmakuDensitySeries
_normalise_danmaku_message = danmaku_analysis._normalise_danmaku_message
_display_danmaku_message = danmaku_analysis._display_danmaku_message
_is_question_only_danmaku = danmaku_analysis._is_question_only_danmaku
_danmaku_title_cue_messages = danmaku_analysis._danmaku_title_cue_messages
_danmaku_title_cue_groups_for_context = danmaku_analysis._danmaku_title_cue_groups_for_context
_danmaku_peak_content_evidence = danmaku_analysis._danmaku_peak_content_evidence
_format_danmaku_peak_content = danmaku_analysis._format_danmaku_peak_content
_danmaku_prompt_message_items = danmaku_analysis._danmaku_prompt_message_items
_density_percentile = danmaku_analysis._density_percentile
_danmaku_clip_threshold = danmaku_analysis._danmaku_clip_threshold
_median_number = danmaku_analysis._median_number
_danmaku_content_quality = danmaku_analysis._danmaku_content_quality
analyze_danmaku = danmaku_analysis.analyze_danmaku


# 人工时间轴兼容 façade：墙钟换算、分段过滤与 SRT 校准使用唯一实现。
MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE = timeline_analysis.MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE
MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC = timeline_analysis.MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC
MANUAL_TIMELINE_ALIGNMENT_STEP_SEC = timeline_analysis.MANUAL_TIMELINE_ALIGNMENT_STEP_SEC
MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC = timeline_analysis.MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC
MANUAL_TIMELINE_CHUNK_MARGIN_SEC = timeline_analysis.MANUAL_TIMELINE_CHUNK_MARGIN_SEC
MANUAL_TIMELINE_DIR = timeline_analysis.MANUAL_TIMELINE_DIR
MANUAL_TIMELINE_END_MARGIN_SEC = timeline_analysis.MANUAL_TIMELINE_END_MARGIN_SEC
MANUAL_TIMELINE_GROUNDING_MIN_SCORE = timeline_analysis.MANUAL_TIMELINE_GROUNDING_MIN_SCORE
MANUAL_TIMELINE_OPTIMIZATION_VERSION = timeline_analysis.MANUAL_TIMELINE_OPTIMIZATION_VERSION
MANUAL_TIMELINE_OPTIMIZE_GAP_SEC = timeline_analysis.MANUAL_TIMELINE_OPTIMIZE_GAP_SEC
MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC = timeline_analysis.MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC
_MANUAL_SEMANTIC_BIGRAM_STOPWORDS = timeline_analysis._MANUAL_SEMANTIC_BIGRAM_STOPWORDS
_MANUAL_SEMANTIC_GENERIC_TERMS = timeline_analysis._MANUAL_SEMANTIC_GENERIC_TERMS
_extract_video_start_datetime = timeline_analysis._extract_video_start_datetime
_manual_timeline_doc_candidates = timeline_analysis._manual_timeline_doc_candidates
_find_manual_timeline_doc = timeline_analysis._find_manual_timeline_doc
_read_docx_lines = timeline_analysis.read_docx_lines
_parse_manual_timeline_lines = timeline_analysis._parse_manual_timeline_lines
_parse_elapsed_timeline_report_lines = timeline_analysis._parse_elapsed_timeline_report_lines
_filter_manual_timeline_entries = timeline_analysis._filter_manual_timeline_entries
load_manual_timeline = timeline_analysis.load_manual_timeline
_manual_timeline_summary = timeline_analysis._manual_timeline_summary
_manual_alignment_text = timeline_analysis._manual_alignment_text
_manual_semantic_core = timeline_analysis._manual_semantic_core
_srt_alignment_windows = timeline_analysis._srt_alignment_windows
_align_manual_timeline_entries_to_srt = timeline_analysis._align_manual_timeline_entries_to_srt


# 标题兼容 façade：样式、证据绑定和多阶段复核只保留唯一实现。
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
_load_title_style_profile = title_analysis.load_title_style_profile
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
_final_title_review_payload = title_analysis._final_title_review_payload
_build_final_title_generation_prompt = title_analysis._build_final_title_generation_prompt
_normalise_final_title_option = title_analysis._normalise_final_title_option
_parse_final_title_candidates = title_analysis._parse_final_title_candidates
_build_final_title_judge_prompt = title_analysis._build_final_title_judge_prompt
_parse_final_title_judgement = title_analysis._parse_final_title_judgement
_review_selected_publish_titles = title_analysis.review_selected_publish_titles


# 候选兼容 façade：评分、价值复核和边界决策只保留唯一实现。
CHUNK_SEC = candidate_analysis.CHUNK_SEC
CLIP_MANUAL_REVIEW_MIN_STARS = candidate_analysis.CLIP_MANUAL_REVIEW_MIN_STARS
CLIP_MIN_INTEREST_SCORE = candidate_analysis.CLIP_MIN_INTEREST_SCORE
CLIP_REVIEW_BATCH_SIZE = candidate_analysis.CLIP_REVIEW_BATCH_SIZE
CLIP_REVIEW_POLICY_VERSION = candidate_analysis.CLIP_REVIEW_POLICY_VERSION
CLIP_REVIEW_RETRY_BATCH_SIZE = candidate_analysis.CLIP_REVIEW_RETRY_BATCH_SIZE
DANMAKU_WINDOW = candidate_analysis.DANMAKU_WINDOW
DANMAKU_WINDOW_STEP = candidate_analysis.DANMAKU_WINDOW_STEP
LLMProviderUnavailableError = candidate_analysis.LLMProviderUnavailableError
LLMStructuredOutputError = candidate_analysis.LLMStructuredOutputError
LLM_ANALYSIS_MODEL = candidate_analysis.LLM_ANALYSIS_MODEL
LLM_COMPACT_MAX_TOKENS = candidate_analysis.LLM_COMPACT_MAX_TOKENS
LLM_COMPACT_TEXT_CHARS = candidate_analysis.LLM_COMPACT_TEXT_CHARS
LLM_DEFAULT_CONCURRENCY = candidate_analysis.LLM_DEFAULT_CONCURRENCY
LLM_FULL_TEXT_CHARS = candidate_analysis.LLM_FULL_TEXT_CHARS
LLM_MAX_CONCURRENCY = candidate_analysis.LLM_MAX_CONCURRENCY
LLM_MAX_TOKENS = candidate_analysis.LLM_MAX_TOKENS
MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE = candidate_analysis.MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE
MANUAL_TIMELINE_TOPIC_POST_SEC = candidate_analysis.MANUAL_TIMELINE_TOPIC_POST_SEC
MANUAL_TIMELINE_TOPIC_PRE_SEC = candidate_analysis.MANUAL_TIMELINE_TOPIC_PRE_SEC
MAX_INITIAL_FAILED_CHUNKS = candidate_analysis.MAX_INITIAL_FAILED_CHUNKS
OUTRO_TRIGGER_JOIN_GAP_SEC = candidate_analysis.OUTRO_TRIGGER_JOIN_GAP_SEC
OUTRO_VARIANT_FAREWELL_AFTER_SEC = candidate_analysis.OUTRO_VARIANT_FAREWELL_AFTER_SEC
OUTRO_VARIANT_FAREWELL_BEFORE_SEC = candidate_analysis.OUTRO_VARIANT_FAREWELL_BEFORE_SEC
SC_CONTEXT_LOOKBACK_SEC = candidate_analysis.SC_CONTEXT_LOOKBACK_SEC
SC_FALLBACK_GIFT_LOOKBACK_SEC = candidate_analysis.SC_FALLBACK_GIFT_LOOKBACK_SEC
SC_TRIGGER_KEYWORDS = candidate_analysis.SC_TRIGGER_KEYWORDS
SRT_ESTIMATED_CHARS_PER_SEC = candidate_analysis.SRT_ESTIMATED_CHARS_PER_SEC
THANKS_TRIGGER_RE = candidate_analysis.THANKS_TRIGGER_RE
TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC = candidate_analysis.TOPIC_AI_FOCUS_EDGE_POST_CONTEXT_SEC
TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC = candidate_analysis.TOPIC_AI_FOCUS_EDGE_PRE_CONTEXT_SEC
TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC = candidate_analysis.TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC = candidate_analysis.TOPIC_AI_FOCUS_NATURAL_PRE_BOUNDARY_SEC
TOPIC_AI_FOCUS_POST_CONTEXT_SEC = candidate_analysis.TOPIC_AI_FOCUS_POST_CONTEXT_SEC
TOPIC_AI_FOCUS_PRE_CONTEXT_SEC = candidate_analysis.TOPIC_AI_FOCUS_PRE_CONTEXT_SEC
TOPIC_ANALYSIS_CHECKPOINT_VERSION = candidate_analysis.TOPIC_ANALYSIS_CHECKPOINT_VERSION
TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC = candidate_analysis.TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEARCH_SEC
TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC = candidate_analysis.TOPIC_BOUNDARY_EVIDENCE_FORWARD_SEC
TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC = candidate_analysis.TOPIC_BOUNDARY_EVIDENCE_LOOKBACK_SEC
TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE = candidate_analysis.TOPIC_BOUNDARY_EVIDENCE_MIN_SCORE
TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC = candidate_analysis.TOPIC_BOUNDARY_FORWARD_SHIFT_MAX_SEC
TOPIC_CONTEXT_GAP = candidate_analysis.TOPIC_CONTEXT_GAP
TOPIC_DIRECT_SLICE_MAX_SEC = candidate_analysis.TOPIC_DIRECT_SLICE_MAX_SEC
TOPIC_FOCUS_POST_SEC = candidate_analysis.TOPIC_FOCUS_POST_SEC
TOPIC_FOCUS_PRE_SEC = candidate_analysis.TOPIC_FOCUS_PRE_SEC
TOPIC_HARD_TRANSITION_GAP_SEC = candidate_analysis.TOPIC_HARD_TRANSITION_GAP_SEC
TOPIC_LEAD_IN_LOOKBACK_SEC = candidate_analysis.TOPIC_LEAD_IN_LOOKBACK_SEC
TOPIC_LEAD_IN_RECOVERY_MIN_SEC = candidate_analysis.TOPIC_LEAD_IN_RECOVERY_MIN_SEC
TOPIC_MAX_CLIP_SEC = candidate_analysis.TOPIC_MAX_CLIP_SEC
TOPIC_MAX_REPAIRED_REPORT_SEC = candidate_analysis.TOPIC_MAX_REPAIRED_REPORT_SEC
TOPIC_MIN_CLIP_SEC = candidate_analysis.TOPIC_MIN_CLIP_SEC
TOPIC_MIN_REPORT_SEC = candidate_analysis.TOPIC_MIN_REPORT_SEC
TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC = candidate_analysis.TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC
TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC = candidate_analysis.TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC
TOPIC_POST_CONTEXT_SEC = candidate_analysis.TOPIC_POST_CONTEXT_SEC
TOPIC_PRE_CONTEXT_SEC = candidate_analysis.TOPIC_PRE_CONTEXT_SEC
TOPIC_REFERENCE_END_TOLERANCE_SEC = candidate_analysis.TOPIC_REFERENCE_END_TOLERANCE_SEC
TOPIC_RELEVANT_CONTINUATION_GAP_SEC = candidate_analysis.TOPIC_RELEVANT_CONTINUATION_GAP_SEC
TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC = candidate_analysis.TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
TOPIC_REVIEW_FOCUS_MAX_SEC = candidate_analysis.TOPIC_REVIEW_FOCUS_MAX_SEC
_BOUNDARY_EVIDENCE_STOP_TERMS = candidate_analysis._BOUNDARY_EVIDENCE_STOP_TERMS
_CIRCLED_NUMBERS = candidate_analysis._CIRCLED_NUMBERS
_DANMAKU_META_KEYWORDS = candidate_analysis._DANMAKU_META_KEYWORDS
_FRAGMENT_BODY_LINES = candidate_analysis._FRAGMENT_BODY_LINES
_HEADING_RE = candidate_analysis._HEADING_RE
_MANUAL_AI_PLACEHOLDER_PHRASES = candidate_analysis._MANUAL_AI_PLACEHOLDER_PHRASES
_META_BODY_KEYWORDS = candidate_analysis._META_BODY_KEYWORDS
_NEXT_CASE_ASR_TRIGGER_RE = candidate_analysis._NEXT_CASE_ASR_TRIGGER_RE
_NO_SLICE_HINTS = candidate_analysis._NO_SLICE_HINTS
_OUTRO_ACTIVITY_VARIANT_RE = candidate_analysis._OUTRO_ACTIVITY_VARIANT_RE
_OUTRO_FAREWELL_EVIDENCE = candidate_analysis._OUTRO_FAREWELL_EVIDENCE
_OUTRO_TRIGGER_NORMALISE_RE = candidate_analysis._OUTRO_TRIGGER_NORMALISE_RE
_TOPIC_CONCLUSION_RE = candidate_analysis._TOPIC_CONCLUSION_RE
_TOPIC_DECISION_EVIDENCE_RE = candidate_analysis._TOPIC_DECISION_EVIDENCE_RE
_TOPIC_DISCOURSE_CONTINUATION_RE = candidate_analysis._TOPIC_DISCOURSE_CONTINUATION_RE
_TOPIC_LEAD_IN_TRIGGER_RE = candidate_analysis._TOPIC_LEAD_IN_TRIGGER_RE
_TOPIC_REFUND_RE = candidate_analysis._TOPIC_REFUND_RE
_TOPIC_REVIEW_TRANSIENT_KEYS = candidate_analysis._TOPIC_REVIEW_TRANSIENT_KEYS
_TRIGGER_CONTEXT_TOPIC_RE = candidate_analysis._TRIGGER_CONTEXT_TOPIC_RE
_UNCUTTABLE_CONTENT_KEYWORDS = candidate_analysis._UNCUTTABLE_CONTENT_KEYWORDS
_UNSUPPORTED_AI_AUDIENCE_REACTION_RE = candidate_analysis._UNSUPPORTED_AI_AUDIENCE_REACTION_RE
_VISUAL_CASE_SHIFT_RE = candidate_analysis._VISUAL_CASE_SHIFT_RE
_VISUAL_REACTION_LEAD_IN_RE = candidate_analysis._VISUAL_REACTION_LEAD_IN_RE
_VISUAL_REVIEW_TOPIC_RE = candidate_analysis._VISUAL_REVIEW_TOPIC_RE
_analysis_topics_snapshot = candidate_analysis._analysis_topics_snapshot
_analyze_topic_chunks = candidate_analysis.analyze_topic_chunks
_append_clip_candidate_source = candidate_analysis._append_clip_candidate_source
_apply_danmaku_slice_decisions = candidate_analysis._apply_danmaku_slice_decisions
_apply_reviewed_slice_decisions = candidate_analysis._apply_reviewed_slice_decisions
_assign_reviewed_semantic_slice_window = candidate_analysis._assign_reviewed_semantic_slice_window
_assign_topic_slice_window = candidate_analysis._assign_topic_slice_window
_average_danmaku_density = candidate_analysis._average_danmaku_density
_boundary_context_has_speech = candidate_analysis._boundary_context_has_speech
_boundary_context_is_relevant = candidate_analysis._boundary_context_is_relevant
_boundary_evidence_term_counts = candidate_analysis._boundary_evidence_term_counts
_boundary_evidence_text_is_relevant = candidate_analysis._boundary_evidence_text_is_relevant
_build_chunk_prompt = candidate_analysis._build_chunk_prompt
_build_clip_candidate_review_audit = candidate_analysis._build_clip_candidate_review_audit
_build_clip_candidate_review_prompt = candidate_analysis._build_clip_candidate_review_prompt
_build_manual_topic_enrichment_prompt = candidate_analysis._build_manual_topic_enrichment_prompt
_cap_expanded_clip_mark = candidate_analysis._cap_expanded_clip_mark
_capped_speech_chain_start = candidate_analysis._capped_speech_chain_start
_clean_ass_danmaku_text = candidate_analysis._clean_ass_danmaku_text
_clean_body_content = candidate_analysis._clean_body_content
_clean_topics_for_report = candidate_analysis._clean_topics_for_report
_clip_context_requires_trigger = candidate_analysis._clip_context_requires_trigger
_clip_interest_reason = candidate_analysis._clip_interest_reason
_clip_manual_star_count = candidate_analysis._clip_manual_star_count
_clip_marks_from_topics = candidate_analysis._clip_marks_from_topics
_clip_review_candidate = candidate_analysis._clip_review_candidate
_clip_review_checkpoint_is_complete = candidate_analysis._clip_review_checkpoint_is_complete
_clip_review_checkpoint_matches_policy = candidate_analysis._clip_review_checkpoint_matches_policy
_clip_star_bonus_cap = candidate_analysis._clip_star_bonus_cap
_configured_llm_concurrency = candidate_analysis._configured_llm_concurrency
_danmaku_peak_features = candidate_analysis._danmaku_peak_features
_danmaku_prompt_evidence = candidate_analysis._danmaku_prompt_evidence
_danmaku_topic_alignment = candidate_analysis._danmaku_topic_alignment
_dedupe_clip_marks = candidate_analysis._dedupe_clip_marks
_detect_stream_outro_clip = candidate_analysis._detect_stream_outro_clip
_enrich_manual_topics_in_batches = candidate_analysis.enrich_manual_topics_in_batches
_enrich_manual_topics_with_llm = candidate_analysis.enrich_manual_topics_with_llm
_enriched_manual_topic_from_item = candidate_analysis._enriched_manual_topic_from_item
_expand_clip_mark_with_context = candidate_analysis._expand_clip_mark_with_context
_expand_clip_marks_with_context = candidate_analysis._expand_clip_marks_with_context
_extract_json_payload = candidate_analysis._extract_json_payload
_filter_unsupported_ai_points = candidate_analysis._filter_unsupported_ai_points
_find_next_topic_hard_end = candidate_analysis._find_next_topic_hard_end
_find_relevant_topic_context_end = candidate_analysis._find_relevant_topic_context_end
_find_relevant_topic_context_start = candidate_analysis._find_relevant_topic_context_start
_find_sc_context_start = candidate_analysis._find_sc_context_start
_find_topic_lead_in_start = candidate_analysis._find_topic_lead_in_start
_find_visual_reaction_context_start = candidate_analysis._find_visual_reaction_context_start
_fit_final_clip_to_safe_srt_boundaries = candidate_analysis._fit_final_clip_to_safe_srt_boundaries
_format_report_time = candidate_analysis._format_report_time
_format_topic_block = candidate_analysis._format_topic_block
_fresh_manual_topic_evidence = candidate_analysis._fresh_manual_topic_evidence
_gift_trigger_has_question_followup = candidate_analysis._gift_trigger_has_question_followup
_has_high_star_manual_evidence = candidate_analysis._has_high_star_manual_evidence
_has_outro_farewell_evidence = candidate_analysis._has_outro_farewell_evidence
_high_energy_danmaku_peaks = candidate_analysis._high_energy_danmaku_peaks
_integer_clip_bounds_outside_subtitles = candidate_analysis._integer_clip_bounds_outside_subtitles
_is_content_cuttable_topic = candidate_analysis._is_content_cuttable_topic
_is_duplicate_topic = candidate_analysis._is_duplicate_topic
_is_explicit_sc_topic = candidate_analysis._is_explicit_sc_topic
_is_explicit_sc_trigger = candidate_analysis._is_explicit_sc_trigger
_is_generic_danmaku_reaction = candidate_analysis._is_generic_danmaku_reaction
_is_manual_ai_placeholder = candidate_analysis._is_manual_ai_placeholder
_is_manual_merge_target = candidate_analysis._is_manual_merge_target
_is_meta_body_line = candidate_analysis._is_meta_body_line
_is_retryable_llm_error = candidate_analysis._is_retryable_llm_error
_is_slice_marked = candidate_analysis._is_slice_marked
_is_topic_in_chunk = candidate_analysis._is_topic_in_chunk
_json_can_slice = candidate_analysis._json_can_slice
_json_points_to_body = candidate_analysis._json_points_to_body
_load_repaired_srt_segments = candidate_analysis._load_repaired_srt_segments
_load_topic_analysis_checkpoint = candidate_analysis._load_topic_analysis_checkpoint
_looks_like_delayed_topic_conclusion = candidate_analysis._looks_like_delayed_topic_conclusion
_looks_like_discourse_continuation = candidate_analysis._looks_like_discourse_continuation
_looks_like_low_score_visual_case_shift = candidate_analysis._looks_like_low_score_visual_case_shift
_looks_like_next_case_transition = candidate_analysis._looks_like_next_case_transition
_looks_like_sc_or_gift_trigger = candidate_analysis._looks_like_sc_or_gift_trigger
_make_chunk = candidate_analysis._make_chunk
_make_fallback_topic_from_chunk = candidate_analysis._make_fallback_topic_from_chunk
_manual_alignment_score = candidate_analysis._manual_alignment_score
_manual_entry_matches_topic = candidate_analysis._manual_entry_matches_topic
_manual_entry_meaningfully_overlaps_topic = candidate_analysis._manual_entry_meaningfully_overlaps_topic
_manual_evidence_line = candidate_analysis._manual_evidence_line
_manual_review_anchor = candidate_analysis._manual_review_anchor
_manual_text_supports_candidate = candidate_analysis._manual_text_supports_candidate
_merge_expanded_clip_marks = candidate_analysis._merge_expanded_clip_marks
_merge_manual_timeline_topics = candidate_analysis.merge_manual_timeline_topics
_nearest_safe_srt_boundary = candidate_analysis._nearest_safe_srt_boundary
_next_report_topic_safe_boundary = candidate_analysis._next_report_topic_safe_boundary
_normalise_body_line = candidate_analysis._normalise_body_line
_normalise_boundary_evidence_text = candidate_analysis._normalise_boundary_evidence_text
_normalise_outro_trigger_text = candidate_analysis._normalise_outro_trigger_text
_normalise_streamer_terms = candidate_analysis._normalise_streamer_terms
_optimized_entry_semantic_text = candidate_analysis._optimized_entry_semantic_text
_outro_topic_from_mark = candidate_analysis._outro_topic_from_mark
_overlap_ratio = candidate_analysis._overlap_ratio
_parse_clip_interest_score = candidate_analysis._parse_clip_interest_score
_parse_clip_star_bonus = candidate_analysis._parse_clip_star_bonus
_parse_hms = candidate_analysis._parse_hms
_parse_json_topics_response = candidate_analysis._parse_json_topics_response
_parse_llm_response = candidate_analysis._parse_llm_response
_profile_identity_names = candidate_analysis._profile_identity_names
_profile_matches_streamer = candidate_analysis._profile_matches_streamer
_reconcile_topic_manual_evidence = candidate_analysis._reconcile_topic_manual_evidence
_refresh_natural_boundary_metadata = candidate_analysis._refresh_natural_boundary_metadata
_refresh_topic_danmaku_evidence = candidate_analysis._refresh_topic_danmaku_evidence
_repair_short_topic_end = candidate_analysis._repair_short_topic_end
_report_fact_lines = candidate_analysis._report_fact_lines
_resolve_reviewed_report_overlaps = candidate_analysis._resolve_reviewed_report_overlaps
_review_peak_selected_topics = candidate_analysis._review_peak_selected_topics
_reviewed_danmaku_ranking_score = candidate_analysis._reviewed_danmaku_ranking_score
_reviewed_topic_has_required_interest = candidate_analysis._reviewed_topic_has_required_interest
_sanitize_optimized_manual_entry = candidate_analysis._sanitize_optimized_manual_entry
_score_boundary_evidence_text = candidate_analysis._score_boundary_evidence_text
_serialized_progress_callback = candidate_analysis._serialized_progress_callback
_short_llm_error = candidate_analysis._short_llm_error
_snap_clip_to_srt_segments = candidate_analysis._snap_clip_to_srt_segments
_split_chain_crossing_topic_end = candidate_analysis._split_chain_crossing_topic_end
_srt_video_duration = candidate_analysis._srt_video_duration
_strip_code_fence = candidate_analysis._strip_code_fence
_strip_prompt_time_labels = candidate_analysis._strip_prompt_time_labels
_subtitle_speech_chains = candidate_analysis._subtitle_speech_chains
_subtitle_text_size = candidate_analysis._subtitle_text_size
_text_len_for_timing = candidate_analysis._text_len_for_timing
_topic_analysis_prompt_fingerprint = candidate_analysis._topic_analysis_prompt_fingerprint
_topic_danmaku_reference_lines = candidate_analysis._topic_danmaku_reference_lines
_topic_index_label = candidate_analysis._topic_index_label
_topic_peak_candidates = candidate_analysis._topic_peak_candidates
_topic_peak_focus_window = candidate_analysis._topic_peak_focus_window
_topic_semantic_text = candidate_analysis._topic_semantic_text
_topic_srt_summary_lines = candidate_analysis._topic_srt_summary_lines
_topics_from_manual_timeline = candidate_analysis._topics_from_manual_timeline
_trim_report_topic_around_reviewed_topic = candidate_analysis._trim_report_topic_around_reviewed_topic
_validate_unmatched_manual_topics = candidate_analysis._validate_unmatched_manual_topics
_validated_ai_focus_range = candidate_analysis._validated_ai_focus_range
_write_clip_review_checkpoint = candidate_analysis.write_clip_review_checkpoint
_write_completed_clip_review_checkpoint = candidate_analysis._write_completed_clip_review_checkpoint
_write_topic_analysis_checkpoint = candidate_analysis._write_topic_analysis_checkpoint
chunk_srt = candidate_analysis.chunk_srt
fmt_time = candidate_analysis.fmt_time
parse_srt_segments = candidate_analysis.parse_srt_segments
parse_srt_text = candidate_analysis.parse_srt_text


# LLM 兼容 façade 必须与唯一 gateway 保持对象身份；本模块不得重新定义。
_LLMApiConfig = llm_gateway.LLMApiConfig
_call_compatible_api = llm_gateway.call_compatible_api
_infer_api_type = llm_gateway.infer_api_type
_infer_llm_api_type = llm_gateway.infer_api_type
_load_llm_api_config = llm_gateway.load_api_config
_normalise_api_config = llm_gateway.normalise_api_config
_normalise_llm_api_config = llm_gateway.normalise_api_config
_read_llm_json_config = llm_gateway.read_json_config
_read_json_config = llm_gateway.read_json_config
load_api_config = llm_gateway.load_api_config
LLMResponseTruncatedError = llm_gateway.LLMResponseTruncatedError
LLMResponseFormatError = llm_gateway.LLMResponseFormatError
_LLMProviderRetryCoordinator = llm_gateway.LLMProviderRetryCoordinator
_llm_response_has_complete_json = llm_gateway.response_has_complete_json
_decode_llm_response_json = llm_gateway.decode_response_json
_parse_openai_response = llm_gateway.parse_openai_response
_parse_anthropic_response = llm_gateway.parse_anthropic_response
call_llm = llm_gateway.call_llm
_llm_http_status = llm_gateway.llm_http_status
_is_provider_service_unavailable = llm_gateway.is_provider_service_unavailable
_call_llm_with_retry = llm_gateway.call_llm_with_retry


# ============================================================
# 配置
# ============================================================
LLM_MODEL = (
    os.environ.get("AUTOSLICE_LLM_MODEL", "").strip()
    or "gpt-5.6-terra"
)
LLM_RETRY_DELAYS = (3, 8, 20, 45)
LLM_PROVIDER_UNAVAILABLE_RETRY_DELAYS = (3, 8)
LLM_REQUEST_TIMEOUT = (30, 300)
# 修改候选复核提示、标题证据或通过规则时必须递增，防止旧标题检查点被继续复用。
# 字幕工作台的“排除背景音”只处理用于识别的临时 WAV，不修改源视频音轨。
# 先抑制稳定噪声和低音量背景，再由可选 CAM++ 说话人聚类保留主要说话人。
SLICE_EXACT_SEEK_PREROLL_SEC = 10
SLICE_DURATION_TOLERANCE_SEC = 0.5
SLICE_INDEX_MIN_CLIPS = 4
SLICE_DEFAULT_CONCURRENCY = 2
SLICE_MAX_CONCURRENCY = 2
# 当前默认字幕为剪映字号 20、描边 100。28 字会在 16:9 成片中直接越界，
# 因此工作 SRT 以 13 字为硬上限；最终 ASS 仍会按实际画布再做一次保险拆分。
# 只供修复历史“全文重复到逐字时间戳”的旧 SRT 使用。该路径需要较完整的
# 上下文来纠正跨词专名；最终成片仍会在 ASS 层按画布宽度安全拆分。



# 兼容旧测试和外部脚本对该变量的临时覆盖；生产任务优先读取当前主播配置。
DEFAULT_REFINEMENT_QUEUE_DIR = os.environ.get(
    "AUTOSLICE_REFINEMENT_QUEUE_DIR",
    str(OUTPUT_DIR),
)
_UNIFIED_REFINEMENT_QUEUE_LOCK = threading.Lock()


REFINEMENT_WORKFLOW_STEPS = (
    ("verify_context", "核查前后文"),
    ("trim_breath", "剪气口与停顿"),
    ("correct_subtitles", "精剪导出后自动识别、校对并压制字幕"),
    ("add_intro_outro", "添加片头片尾"),
    ("export_video", "导出精调成片"),
    ("make_cover", "用 AutoCover 制作封面"),
    ("publish_bilibili", "在 B 站网页投稿"),
)


def _validated_video_path(path, *, for_slicing=False):
    """校验公开工作流的视频输入，并保留兼容的 ``flv_path`` 参数名。"""

    normalized_path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(normalized_path):
        raise FileNotFoundError(f"录播文件不存在: {normalized_path}")
    supported = (
        is_sliceable_video(normalized_path)
        if for_slicing
        else is_analyzable_video(normalized_path)
    )
    if not supported:
        extensions = "/".join(SUPPORTED_VIDEO_EXTENSIONS)
        raise ValueError(f"不支持的视频格式；支持：{extensions}")
    return normalized_path








































































_GENERATED_REPORT_TOPIC_RE = re.compile(
    r'^\s*(?:[①-⑳㉑-㊿]|\d+[.、)])\s*'
    rf'\[\s*(?P<start>\d{{1,3}}:\d{{2}}(?::\d{{2}})?)\s*[－—–~-]\s*'
    rf'(?P<end>\d{{1,3}}:\d{{2}}(?::\d{{2}})?)\s*\]\s*(?P<title>.+?)\s*$'
)


def _parse_generated_topic_report(report_path):
    """从已有逐话题报告恢复首轮话题，供仅重做候选复核使用。"""
    if not report_path or not os.path.isfile(report_path):
        raise FileNotFoundError(f"话题报告不存在: {report_path or '未指定'}")
    with open(report_path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    if not any("时间基准" in line and "视频内时间" in line for line in lines):
        raise ValueError("话题报告未声明视频内时间基准，不能安全恢复候选")

    topics = []
    current = None
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("## 投稿标题建议") or line.startswith("## 分析警告"):
            current = None
            break
        match = _GENERATED_REPORT_TOPIC_RE.match(line)
        if match:
            try:
                start = _parse_hms(match.group("start"))
                end = _parse_hms(match.group("end"))
            except (TypeError, ValueError):
                current = None
                continue
            title = re.sub(r'[✂⭐★]\ufe0f?', '', match.group("title")).strip()
            title = _clean_topic_title(title)
            if end <= start or not title:
                current = None
                continue
            current = {
                "start": start,
                "end": end,
                "start_str": fmt_time(start),
                "end_str": fmt_time(end),
                "title": title,
                "body": [],
                "can_slice": False,
                "source": "recovered_report",
                "recovered_from_report": True,
            }
            topics.append(current)
            continue
        if current and line.startswith(("·", "●")):
            if line.startswith("·切片核心："):
                continue
            current["body"].append(line)

    for topic in topics:
        topic["body"] = _filter_unsupported_ai_points(topic.get("body") or [])
        body_text = " ".join(topic.get("body") or [])
        if "未形成稳定可切片主题" in body_text or "暂不标记为自动切片" in body_text:
            topic["fallback"] = True
    if not topics:
        raise ValueError("话题报告中没有可恢复的逐话题条目")
    return topics










def _format_manual_entry_for_prompt(entry):
    stars = "⭐" * min(int(entry.get("stars", 0)), 5)
    prefix = f"{stars} " if stars else ""
    clock = entry.get("clock")
    elapsed_label = fmt_time(entry["start"])
    if entry.get("end") is not None and int(entry["end"]) > int(entry["start"]):
        elapsed_label = f"{elapsed_label}-{fmt_time(entry['end'])}"
    time_label = f"{elapsed_label} / {clock}" if clock else elapsed_label
    summary = "；".join(
        _strip_body_prefix(item)
        for item in (entry.get("summary") or [])[:2]
        if _strip_body_prefix(item)
    )
    summary_suffix = f" | {summary}" if summary else ""
    return f"- [{time_label}] {prefix}{entry.get('text', '')}{summary_suffix}"


def _manual_timeline_info_for_chunk(entries, chunk_start, chunk_end, limit=12):
    """取当前分块附近的人工时间轴，供 LLM 参考。"""
    nearby = [
        item for item in entries or []
        if chunk_start - MANUAL_TIMELINE_CHUNK_MARGIN_SEC <= item["start"] <= chunk_end + MANUAL_TIMELINE_CHUNK_MARGIN_SEC
    ]
    if not nearby:
        return "无"
    starred = [item for item in nearby if item.get("stars", 0) > 0]
    selected = starred[:limit]
    if len(selected) < limit:
        selected.extend([item for item in nearby if item not in selected][:limit - len(selected)])
    selected.sort(key=lambda item: item["start"])
    return "\n".join(_format_manual_entry_for_prompt(item) for item in selected)


def _attach_manual_timeline_to_chunks(chunks, entries):
    """把人工时间轴摘要挂到每个 SRT 分块上。"""
    for ch in chunks:
        ch["manual_timeline_info"] = _manual_timeline_info_for_chunk(
            entries, int(ch["start"]), int(ch.get("end", ch["start"] + CHUNK_SEC))
        )
    return chunks
















# ============================================================
# Step 1: FunASR 自动转录 (复用 core.py 逻辑，降级到 CPU)
# ============================================================





























































# ============================================================
# Step 2: 弹幕密度分析
# ============================================================




# 高频问号或同义短句很容易占满代表弹幕，导致真正能解释爆点的视觉细节
# 到不了标题模型。这里按标题常见的信息类型各保留少量原文旁证。













































# ============================================================
# Step 3: SRT 解析 + 分块
# ============================================================















def _title_hook_prompt_guide(streamer_name=None):
    """兼容 façade：把显式主播上下文交给唯一 prompt 实现。"""
    return _render_title_hook_guide(_prompt_context(streamer_name))


# ============================================================
# Step 4: LLM 分析
# ============================================================


def _build_system_prompt(streamer_name=None):
    """兼容 façade：构造显式上下文后调用唯一 prompt 实现。"""
    return _render_system_prompt(_prompt_context(streamer_name))



# ============================================================
# LLM 响应解析与去重
# ============================================================
















































def subtitle_title_services():
    """向高层调用方提供显式标题服务，字幕模块无需反向导入本 façade。"""
    return SubtitleTitleServices(
        max_publish_title_chars=MAX_PUBLISH_TITLE_CHARS,
        build_title_style_prompt=_build_title_style_prompt,
        build_title_hook_prompt_guide=_title_hook_prompt_guide,
        normalise_publish_title=_normalise_publish_title,
    )


























def _try_enrich_manual_topics(topics, streamer_name=None, progress_callback=None):
    """AI 复核失败时保留规则候选，返回适合写入报告的警告。"""
    try:
        candidate_analysis.enrich_manual_topics_with_llm(
            topics,
            streamer_name=streamer_name,
            progress_callback=progress_callback,
        )
        return None
    except Exception as exc:
        return f"人工时间轴 AI 复核失败，已保留字幕/弹幕规则结果：{_short_llm_error(exc)}"

























def _optimized_manual_entries_from_topics(topics):
    """把字幕复核话题转换成供后续分块分析使用的简洁时间轴。"""
    entries = []
    for topic in topics or []:
        original_entries = [
            {
                "start": int(item.get("start", 0)),
                "original_start": int(item.get("original_start", item.get("start", 0))),
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
            clean = _strip_body_prefix(line)
            if not clean:
                continue
            if str(line).startswith(("·弹幕依据：", "·字幕核查：", "·时间轴：", "●人工时间轴")):
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
                line for line in topic.get("body") or []
                if str(line).startswith(("·字幕核查：", "·弹幕依据：", "●人工时间轴", "·时间轴："))
            ],
            "original_entries": original_entries,
        }
        sanitized = _sanitize_optimized_manual_entry(entry)
        if sanitized:
            entries.append(sanitized)
    return entries


def _optimized_entry_needs_retry(entry):
    """识别未复核、降级或被模型模板占位污染的优化候选。"""
    if not entry.get("ai_enriched") or entry.get("reference_only"):
        return True
    if _is_manual_ai_placeholder(entry.get("text")):
        return True
    return any(
        _is_manual_ai_placeholder(_strip_body_prefix(point))
        for point in entry.get("summary") or []
    )


def _topic_from_optimized_entry(entry, srt_segments, peaks):
    """把优化 JSON 中的低权重候选还原为可重试的 AI 复核话题。"""
    start = int(entry.get("start", 0))
    end = max(start + 1, int(entry.get("end", start + 1)))
    original_entries = list(entry.get("original_entries") or [])
    if not original_entries:
        original_entries = [{
            "start": start,
            "original_start": start,
            "text": entry.get("text", "人工时间轴重点"),
            "stars": int(entry.get("stars", 0)),
        }]
    body = list(entry.get("evidence") or [])
    if not any(str(line).startswith("·弹幕依据：") for line in body):
        body[:0] = _topic_danmaku_reference_lines(start, end, peaks or [])
    if not any(str(line).startswith("·字幕核查：") for line in body):
        body.extend(_topic_srt_summary_lines(start, end, srt_segments or []))
    for item in original_entries:
        stars = int(item.get("stars", 0))
        prefix = f"●人工时间轴{'⭐' * min(stars, 5)}" if stars else "·时间轴"
        line = f"{prefix}：{fmt_time(int(item.get('start', start)))} {item.get('text', '')}"
        if line not in body:
            body.append(line)
    return {
        "start": start,
        "end": end,
        "start_str": fmt_time(start),
        "end_str": fmt_time(end),
        "title": entry.get("text", "人工时间轴重点"),
        "publish_title": entry.get("publish_title"),
        "body": body,
        "can_slice": False,
        "manual_stars": int(entry.get("stars", 0)),
        "manual_timeline": original_entries,
        "source": "optimized_manual_timeline",
        "reference_only": True,
    }


def _batch_warning_text(warnings, pending_count=0):
    details = list(warnings or [])
    if pending_count:
        details.append(f"尚有 {pending_count} 项等待后续批次")
    if not details:
        return None
    return "人工时间轴部分未完成字幕校准，相关条目仅作低权重参考：" + "；".join(details)


def _retry_optimized_timeline_entries(
        entries, srt_segments, peaks, streamer_name=None, progress_callback=None,
        checkpoint_callback=None):
    """保留已通过候选，仅以小批量重试低权重或占位污染项。"""
    accepted_entries = [dict(entry) for entry in entries or [] if not _optimized_entry_needs_retry(entry)]
    retry_topics = [
        _topic_from_optimized_entry(entry, srt_segments, peaks)
        for entry in entries or []
        if _optimized_entry_needs_retry(entry)
    ]
    if not retry_topics:
        return sorted(accepted_entries, key=lambda item: (item["start"], item["end"])), None

    def save_checkpoint(processed_topics, remaining_topics, warnings):
        if not checkpoint_callback:
            return
        pending_topics = []
        for topic in remaining_topics:
            pending = dict(topic)
            pending["reference_only"] = True
            pending_topics.append(pending)
        checkpoint_entries = accepted_entries + _optimized_manual_entries_from_topics(
            list(processed_topics) + pending_topics
        )
        checkpoint_callback(
            sorted(checkpoint_entries, key=lambda item: (item["start"], item["end"])),
            _batch_warning_text(warnings, pending_count=len(remaining_topics)),
        )

    warning = candidate_analysis.enrich_manual_topics_in_batches(
        retry_topics,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
        batch_size=MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE,
        batch_result_callback=save_checkpoint,
    )
    optimized_entries = accepted_entries + _optimized_manual_entries_from_topics(retry_topics)
    return sorted(optimized_entries, key=lambda item: (item["start"], item["end"])), warning


def _optimize_manual_timeline(
        entries, srt_segments, peaks, streamer_name=None, progress_callback=None,
        batch_result_callback=None):
    """先用字幕/弹幕聚合人工记录，再由 AI 改写标题、要点和语义范围。"""
    if not entries:
        return [], None
    aligned_entries = _align_manual_timeline_entries_to_srt(entries, srt_segments)
    topics = _topics_from_manual_timeline(
        aligned_entries,
        srt_segments=srt_segments,
        peaks=peaks,
        max_gap_sec=MANUAL_TIMELINE_OPTIMIZE_GAP_SEC,
        max_group_duration_sec=MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC,
    )
    warning = candidate_analysis.enrich_manual_topics_in_batches(
        topics,
        streamer_name=streamer_name,
        progress_callback=progress_callback,
        batch_result_callback=batch_result_callback,
    )
    return _optimized_manual_entries_from_topics(topics), warning


def _optimized_timeline_paths(video_base, artifact_layout=None):
    if artifact_layout:
        return (
            artifact_layout["optimized_timeline_json_path"],
            artifact_layout["optimized_timeline_md_path"],
        )
    return video_base + "_优化时间轴.json", video_base + "_优化时间轴.md"


def _write_optimized_timeline_files(
        video_base, source_path, raw_entries, optimized_entries, warning=None,
        artifact_layout=None, video_path=None):
    """保存可审阅的优化时间轴，便于判断人工参考如何被字幕校准。"""
    json_path, md_path = _optimized_timeline_paths(
        video_base, artifact_layout=artifact_layout
    )
    os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(md_path)), exist_ok=True)
    payload = {
        "video_path": (
            os.path.abspath(video_path)
            if video_path
            else video_base + ".flv"
        ),
        "source_path": source_path,
        "streamer_profile_id": current_streamer_profile().id,
        "optimization_version": MANUAL_TIMELINE_OPTIMIZATION_VERSION,
        "raw_entry_count": len(raw_entries or []),
        "optimized_entry_count": len(optimized_entries or []),
        "warning": warning,
        "entries": optimized_entries or [],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    lines = [
        "# 字幕校准后的人工时间轴",
        "",
        f"> 原始文件: {source_path or '无'}",
        f"> 原始 {len(raw_entries or [])} 条 → 优化 {len(optimized_entries or [])} 个话题候选",
    ]
    if warning:
        lines.append(f"> 警告: {warning}")
    lines.extend(["", "---", ""])
    for index, entry in enumerate(optimized_entries or [], 1):
        stars = " ⭐" * min(int(entry.get("stars", 0)), 5)
        confidence = (
            "字幕/AI初审（完整分析时再次独立复核）"
            if entry.get("ai_enriched")
            else "低权重参考"
        )
        lines.append(
            f"## {index:02d} [{fmt_time(entry['start'])}－{fmt_time(entry['end'])}] "
            f"{entry.get('text', '未命名话题')}{stars}"
        )
        lines.append(f"- 状态: {confidence}")
        adjustments = [
            f"{fmt_time(item.get('original_start', item.get('start', 0)))}→"
            f"{fmt_time(item.get('start', 0))} ({int(item.get('alignment_shift_sec', 0)):+d}秒)"
            for item in entry.get("original_entries") or []
            if int(item.get("alignment_shift_sec", 0)) != 0
        ]
        if adjustments:
            lines.append(f"- 字幕校时: {'；'.join(adjustments[:4])}")
        for point in entry.get("summary") or []:
            lines.append(f"- {point}")
        lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return json_path, md_path


def _load_optimized_timeline_artifact(
        artifact_path, flv_path, manual_timeline_path=None):
    """加载独立优化产物，并核对录播及原始 DOCX，避免串用时间轴。"""
    if not artifact_path or not os.path.isfile(artifact_path):
        raise FileNotFoundError(f"优化时间轴文件不存在: {artifact_path or '未选择'}")
    try:
        with open(artifact_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"优化时间轴 JSON 无法读取: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise ValueError("优化时间轴 JSON 缺少 entries 数组")

    def normalized(path):
        return os.path.normcase(os.path.abspath(str(path or "")))

    artifact_video_path = payload.get("video_path")
    if not artifact_video_path or normalized(artifact_video_path) != normalized(flv_path):
        raise ValueError("优化时间轴不属于当前选择的录播文件")
    source_path = payload.get("source_path")
    if manual_timeline_path and normalized(source_path) != normalized(manual_timeline_path):
        raise ValueError("优化时间轴与当前选择的人工 DOCX 不一致")

    sanitized_entries = []
    for entry in payload["entries"]:
        if not isinstance(entry, dict):
            continue
        sanitized = _sanitize_optimized_manual_entry(entry)
        if sanitized:
            sanitized_entries.append(sanitized)
    dropped_count = len(payload["entries"]) - len(sanitized_entries)
    warning = str(payload.get("warning") or "").strip()
    if dropped_count:
        grounding_warning = (
            f"已忽略 {dropped_count} 个与原人工记录语义不符的优化候选"
        )
        warning = "；".join(item for item in (warning, grounding_warning) if item)

    return {
        "path": source_path,
        "entries": sanitized_entries,
        "source_entry_count": int(payload.get("raw_entry_count", 0)),
        "raw_entry_count": int(payload.get("raw_entry_count", 0)),
        "optimized_entry_count": len(sanitized_entries),
        "optimized_json_path": artifact_path,
        "optimized_md_path": os.path.splitext(artifact_path)[0] + ".md",
        "optimization_warning": warning or None,
        "optimization_version": int(payload.get("optimization_version", 0)),
        "streamer_profile_id": payload.get("streamer_profile_id"),
        "mode": "optimized_artifact",
        "video_start": _extract_video_start_datetime(flv_path),
    }


def _prepare_optimized_manual_timeline(
        flv_path, video_base, srt_segments, peaks, video_duration,
        manual_timeline_path, streamer_name=None, progress_callback=None,
        retry_incomplete_artifact=True, artifact_layout=None):
    """加载、过滤并优化人工时间轴，返回后续可直接使用的结构。"""
    manual_timeline = load_manual_timeline(
        flv_path,
        manual_timeline_path=manual_timeline_path,
    )
    all_entries = manual_timeline.get("entries") or []
    raw_entries = _filter_manual_timeline_entries(all_entries, video_duration)
    manual_timeline["source_entry_count"] = len(all_entries)
    manual_timeline["raw_entry_count"] = len(raw_entries)
    manual_timeline["entries"] = raw_entries
    if not raw_entries:
        return manual_timeline

    optimized_json_path, optimized_md_path = _optimized_timeline_paths(
        video_base, artifact_layout=artifact_layout
    )
    if artifact_layout:
        legacy_json_path, legacy_md_path = _optimized_timeline_paths(video_base)
        _seed_artifact_from_legacy(optimized_json_path, legacy_json_path)
        _seed_artifact_from_legacy(optimized_md_path, legacy_md_path)

    def write_checkpoint(entries, warning):
        _write_optimized_timeline_files(
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
                candidate = _load_optimized_timeline_artifact(
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
            _optimized_entry_needs_retry(entry)
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
            optimized_entries, warning = _retry_optimized_timeline_entries(
                reusable_artifact.get("entries") or [],
                srt_segments=srt_segments,
                peaks=peaks,
                streamer_name=streamer_name,
                progress_callback=progress_callback,
                checkpoint_callback=write_checkpoint,
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
            checkpoint_entries = _optimized_manual_entries_from_topics(
                list(processed_topics) + pending_topics
            )
            write_checkpoint(
                checkpoint_entries,
                _batch_warning_text(warnings, pending_count=len(remaining_topics)),
            )

        optimized_entries, warning = _optimize_manual_timeline(
            raw_entries,
            srt_segments=srt_segments,
            peaks=peaks,
            streamer_name=streamer_name,
            progress_callback=progress_callback,
            batch_result_callback=save_fresh_checkpoint,
        )

    optimized_json_path, optimized_md_path = _write_optimized_timeline_files(
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
    flv_path = _validated_video_path(flv_path)
    with streamer_profile_context(streamer_profile_id, flv_path) as profile:
        result = _optimize_manual_timeline_for_video_impl(
            flv_path,
            manual_timeline_path,
            ass_path=ass_path,
            progress_callback=progress_callback,
            output_dir=output_dir,
            artifact_dir=artifact_dir,
        )
        result["streamer_profile_id"] = profile.id
        return result


def _optimize_manual_timeline_for_video_impl(
        flv_path, manual_timeline_path, ass_path=None, progress_callback=None,
        output_dir=None, artifact_dir=None):
    """仅优化人工时间轴，不启动整场话题分析或自动切片。"""
    if not os.path.isfile(flv_path):
        raise FileNotFoundError(f"录播文件不存在: {flv_path}")
    if not manual_timeline_path or not os.path.isfile(manual_timeline_path):
        raise FileNotFoundError(f"人工时间轴文件不存在: {manual_timeline_path or '未选择'}")

    if output_dir is None and artifact_dir is None:
        output_dir = os.path.dirname(os.path.abspath(flv_path))
    artifact_layout = _artifact_bundle_layout(
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
    probed_video_duration = _probe_video_duration(flv_path)
    video_duration = probed_video_duration or srt_duration
    streamer_name = current_streamer_profile().report_name
    video_base = os.path.splitext(flv_path)[0]
    manual_timeline = _prepare_optimized_manual_timeline(
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
    organized = organize_existing_artifacts(
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
        "manual_timeline": _manual_timeline_summary(manual_timeline),
        "artifact_dir": artifact_layout["artifact_dir"],
        "overview_path": organized["overview_path"],
    }




















# ============================================================
# 话题切片上下文扩展
# ============================================================




















































































# ============================================================
# 逐话题时间轴报告格式化
# ============================================================









def _strip_emoji_for_title(title):
    """给 Part 标题做轻量清理，避免标题太花。"""
    return re.sub(r'^[^\w\u4e00-\u9fff]+', '', title).strip() or title


def _make_part_title(topics, streamer_name=None):
    """根据 Part 内话题生成阶段标题。"""
    titles = [_strip_emoji_for_title(_replace_streamer_role(t["title"], streamer_name)) for t in topics if t.get("title")]
    if not titles:
        return "阶段话题整理"
    if len(titles) == 1:
        return titles[0]
    first, second = titles[0], titles[1]
    if len(first) + len(second) <= 18:
        return f"{first}与{second}"
    return f"{first}等话题"




def _group_topics_for_parts(topics, part_seconds=900):
    """按约 15 分钟一段聚合话题，生成 Part。"""
    sorted_topics = sorted(topics, key=lambda t: (t["start"], t["end"]))
    groups = []
    current = []
    group_start = None
    for topic in sorted_topics:
        if not current:
            current = [topic]
            group_start = topic["start"]
            continue
        if topic["start"] - group_start >= part_seconds:
            groups.append(current)
            current = [topic]
            group_start = topic["start"]
        else:
            current.append(topic)
    if current:
        groups.append(current)
    return groups


def _group_topics_by_hour(topics):
    """按视频内自然小时聚合话题，生成“每小时重点”。"""
    sorted_topics = sorted(topics, key=lambda t: (t["start"], t["end"]))
    buckets = []
    current_hour = None
    current = []
    for topic in sorted_topics:
        hour = int(topic["start"] // 3600)
        if current_hour is None:
            current_hour = hour
            current = [topic]
            continue
        if hour != current_hour:
            buckets.append((current_hour, current))
            current_hour = hour
            current = [topic]
        else:
            current.append(topic)
    if current:
        buckets.append((current_hour, current))
    return buckets












































def _topic_clip_filename(index, mark, source_path=None):
    """生成自动切片文件名；报告和实际 ffmpeg 输出必须共用此规则。"""
    title = str(mark.get("title", f"片段{index}")).strip() or f"片段{index}"
    safe_title = re.sub(r'[\\/:*?"<>|`]', '', title)
    safe_title = re.sub(r'\s+', ' ', safe_title).strip(' .')[:30]
    if not safe_title:
        safe_title = f"片段{index}"
    start_s = int(float(mark.get("start", 0)))
    output_extension = preferred_output_extension(source_path or ".flv")
    return f"{index:02d}_{start_s}s_{safe_title}{output_extension}"


def _compatible_topic_clip_filenames(index, mark, source_path):
    """返回首选文件名以及读取历史产物时允许回退的文件名。"""

    preferred_name = _topic_clip_filename(index, mark, source_path)
    filename_stem = os.path.splitext(preferred_name)[0]
    return tuple(
        filename_stem + extension
        for extension in compatible_output_extensions(source_path)
    )


def _synchronise_selected_topic_ranges(topics, clip_marks):
    """将字幕证据后移的核心起点同步回报告，避免报告继续显示上一案例时间。"""
    used = set()
    for mark in clip_marks or []:
        candidates = [
            (index, topic)
            for index, topic in enumerate(topics or [])
            if index not in used and topic.get("title") == mark.get("title")
        ]
        if not candidates:
            continue
        report_start = int(mark.get("report_start", mark.get("topic_start", 0)))
        index, topic = min(
            candidates,
            key=lambda item: abs(int(item[1].get("start", 0)) - report_start),
        )
        used.add(index)
        if report_start > int(topic.get("start", report_start)):
            topic["start"] = report_start
            topic["start_str"] = _format_report_time(report_start)


def _clip_subtitle_filename(clip_filename):
    """片段字幕与视频同名，便于剪映成对导入。"""
    return os.path.splitext(clip_filename)[0] + ".srt"


def _resolve_clip_subtitle_source(flv_path, data):
    """优先使用流水线校对字幕，兼容旧 JSON 回退到同名 SRT。"""
    layout = None
    if isinstance(data, dict) and data.get("artifact_dir"):
        layout = _artifact_bundle_layout(
            flv_path,
            artifact_dir=data.get("artifact_dir"),
        )
    video_base = os.path.splitext(flv_path)[0]
    candidates = [
        data.get("corrected_srt_path"),
        layout["corrected_srt_path"] if layout else None,
        video_base + "_校对字幕.srt",
        data.get("srt_path"),
        video_base + ".srt",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


def _publish_title_report_lines(clip_marks, source_path=None):
    """生成 AutoCover 可直接解析的投稿标题区，只包含最终实际切片。"""
    marks = _dedupe_clip_marks(clip_marks or [])
    if not marks:
        return []
    lines = ["## 投稿标题建议", ""]
    for index, mark in enumerate(marks, 1):
        start = _format_report_time(mark["start"])
        end = _format_report_time(mark["end"])
        filename = _topic_clip_filename(index, mark, source_path)
        publish_title = _normalise_publish_title(
            mark.get("publish_title"), mark.get("title", "未命名片段")
        )
        lines.extend([
            f"### {index:02d}（{start}－{end}）",
            "",
            f"原文件：`{filename}`",
            "",
            f"**{publish_title}**",
            "",
        ])
    return lines


def _artifact_bundle_layout(video_path, output_dir=None, artifact_dir=None):
    return _calculate_artifact_bundle_layout(
        video_path,
        output_dir=output_dir,
        artifact_dir=artifact_dir,
        default_output_dir=DEFAULT_REFINEMENT_QUEUE_DIR,
    )


def _render_artifact_overview(layout, clip_data=None, manifest=None, slice_dir=None):
    """渲染面向日常剪辑的短概览，不复制完整话题正文。"""
    clip_data = clip_data if isinstance(clip_data, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    marks = _dedupe_clip_marks(clip_data.get("clip_marks") or [])
    slice_dir = os.path.abspath(
        slice_dir or manifest.get("slice_output_dir") or layout["slice_dir"]
    )
    tasks_by_filename = {
        task.get("clip_filename"): task
        for task in manifest.get("tasks") or []
        if isinstance(task, dict) and task.get("clip_filename")
    }
    lines = [
        f"# {os.path.basename(layout['source_video_path'])} 自动切片概览",
        "",
        f"> 自动生成 | 最终切片 {len(marks)} 个 | "
        f"更新时间: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## 入口",
        "",
        f"- 源录播: `{layout['source_video_path']}`",
        f"- 实际切片目录: `{slice_dir}`",
    ]
    readable_files = (
        ("完整话题分析", layout["report_path"], "01_话题分析.md"),
        ("精调任务清单", layout["task_manifest_md_path"], "02_精调任务.md"),
        ("字幕校准后的人工时间轴", layout["optimized_timeline_md_path"], "03_优化时间轴.md"),
    )
    for label, path, relative_name in readable_files:
        if os.path.isfile(path):
            lines.append(f"- {label}: [{relative_name}](./{relative_name})")
    lines.extend(["", "## 最终切片", ""])
    if not marks:
        lines.extend(["本次没有最终可切片段。", ""])
        return "\n".join(lines)
    for index, mark in enumerate(marks, 1):
        candidate_filenames = _compatible_topic_clip_filenames(
            index,
            mark,
            layout["source_video_path"],
        )
        task = next(
            (
                tasks_by_filename[name]
                for name in candidate_filenames
                if name in tasks_by_filename
            ),
            {},
        )
        existing_filename = next(
            (
                name
                for name in candidate_filenames
                if os.path.isfile(os.path.join(slice_dir, name))
            ),
            None,
        )
        filename = (
            existing_filename
            or task.get("clip_filename")
            or candidate_filenames[0]
        )
        task_slice_path = task.get("slice_path")
        clip_path = (
            task_slice_path
            if task_slice_path and os.path.isfile(task_slice_path)
            else os.path.join(slice_dir, filename)
        )
        title = str(mark.get("title") or f"片段{index}").strip()
        publish_title = _normalise_publish_title(
            mark.get("publish_title") or task.get("publish_title"),
            title,
        )
        start = float(mark.get("start", 0) or 0)
        end = float(mark.get("end", start) or start)
        lines.extend([
            f"### {index:02d} {title}",
            "",
            f"- 视频内时间: {_format_report_time(start)}－{_format_report_time(end)}"
            f"（{max(0, int(round(end - start)))} 秒）",
            f"- 投稿标题: {publish_title}",
            *(
                [f"- 系列收播片: {mark.get('series_title') or title}"]
                if mark.get("clip_type") == "stream_outro" else []
            ),
            f"- 切片文件: `{os.path.abspath(clip_path)}`",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def organize_existing_artifacts(
        flv_path, output_dir=None, json_path=None, report_path=None,
        slice_dir=None, artifact_dir=None):
    """把旧版散落的小型产物复制进整理包，并改写整理包内部引用。"""
    flv_path = os.path.abspath(flv_path)
    if not os.path.isfile(flv_path):
        raise FileNotFoundError(f"录播文件不存在: {flv_path}")
    layout = _artifact_bundle_layout(
        flv_path, output_dir=output_dir, artifact_dir=artifact_dir
    )
    os.makedirs(layout["data_dir"], exist_ok=True)
    legacy_base = os.path.splitext(flv_path)[0]
    legacy_clip_json_path = _first_existing_artifact_path(
        json_path,
        layout["clip_marks_path"],
        legacy_base + "_clip_marks.json",
    )
    legacy_clip_data = _load_artifact_json(legacy_clip_json_path)
    legacy_manual = legacy_clip_data.get("manual_timeline")
    legacy_manual = legacy_manual if isinstance(legacy_manual, dict) else {}
    source_paths = {
        "report_path": _first_existing_artifact_path(
            report_path, layout["report_path"], legacy_base + "_话题分析.md"
        ),
        "task_manifest_md_path": _first_existing_artifact_path(
            legacy_clip_data.get("task_manifest_md_path"),
            layout["task_manifest_md_path"],
            legacy_base + "_精调任务.md",
        ),
        "optimized_timeline_md_path": _first_existing_artifact_path(
            legacy_manual.get("optimized_md_path"),
            layout["optimized_timeline_md_path"],
            legacy_base + "_优化时间轴.md",
        ),
        "clip_marks_path": legacy_clip_json_path,
        "task_manifest_json_path": _first_existing_artifact_path(
            legacy_clip_data.get("task_manifest_json_path"),
            layout["task_manifest_json_path"],
            legacy_base + "_精调任务.json",
        ),
        "optimized_timeline_json_path": _first_existing_artifact_path(
            legacy_manual.get("optimized_json_path"),
            layout["optimized_timeline_json_path"],
            legacy_base + "_优化时间轴.json",
        ),
        "asr_checkpoint_path": _first_existing_artifact_path(
            layout["asr_checkpoint_path"], legacy_base + "_asr_checkpoint.json"
        ),
        "topic_analysis_checkpoint_path": _first_existing_artifact_path(
            legacy_clip_data.get("topic_analysis_checkpoint_path"),
            layout["topic_analysis_checkpoint_path"],
            legacy_base + "_topic_analysis_checkpoint.json",
        ),
        "clip_review_checkpoint_path": _first_existing_artifact_path(
            legacy_clip_data.get("clip_review_checkpoint_path"),
            layout["clip_review_checkpoint_path"],
            legacy_base + "_clip_review_checkpoint.json",
        ),
        "corrected_srt_path": _first_existing_artifact_path(
            legacy_clip_data.get("corrected_srt_path"),
            layout["corrected_srt_path"],
            legacy_base + "_校对字幕.srt",
        ),
    }
    copied_files = []
    for key, source_path in source_paths.items():
        copied = _copy_artifact_file(source_path, layout[key])
        if copied:
            copied_files.append(copied)

    clip_data = _load_artifact_json(layout["clip_marks_path"])
    manifest = _load_artifact_json(layout["task_manifest_json_path"])
    actual_slice_dir = os.path.abspath(
        slice_dir or manifest.get("slice_output_dir") or layout["slice_dir"]
    )
    if manifest:
        manifest.update({
            "artifact_layout_version": ARTIFACT_LAYOUT_VERSION,
            "artifact_dir": layout["artifact_dir"],
            "overview_path": layout["overview_path"],
            "analysis_report_path": (
                layout["report_path"] if os.path.isfile(layout["report_path"]) else None
            ),
            "clip_marks_path": layout["clip_marks_path"],
            "manifest_json_path": layout["task_manifest_json_path"],
            "manifest_md_path": layout["task_manifest_md_path"],
            "corrected_srt_path": (
                layout["corrected_srt_path"]
                if os.path.isfile(layout["corrected_srt_path"])
                else manifest.get("corrected_srt_path")
            ),
            "slice_output_dir": actual_slice_dir,
            "unified_queue_json_path": layout["unified_queue_json_path"],
            "unified_queue_md_path": layout["unified_queue_md_path"],
        })
        for task in manifest.get("tasks") or []:
            if not isinstance(task, dict) or not task.get("clip_filename"):
                continue
            filename_stem = os.path.splitext(task["clip_filename"])[0]
            candidate_paths = [
                os.path.abspath(os.path.join(
                    actual_slice_dir,
                    filename_stem + extension,
                ))
                for extension in compatible_output_extensions(flv_path)
            ]
            candidate = next(
                (path for path in candidate_paths if os.path.isfile(path)),
                candidate_paths[0],
            )
            subtitle = os.path.splitext(candidate)[0] + ".srt"
            if os.path.isfile(candidate):
                task["clip_filename"] = os.path.basename(candidate)
                task["slice_path"] = candidate
            if os.path.isfile(subtitle):
                task["subtitle_path"] = subtitle
        try:
            _upsert_unified_refinement_queue(
                manifest,
                queue_json_path=layout["unified_queue_json_path"],
                queue_md_path=layout["unified_queue_md_path"],
            )
            manifest["unified_queue_warning"] = None
        except (OSError, ValueError, TypeError) as exc:
            manifest["unified_queue_warning"] = f"精调总清单更新失败: {exc}"
        _write_artifact_json(layout["task_manifest_json_path"], manifest)
        _write_artifact_text(
            layout["task_manifest_md_path"],
            _render_refinement_manifest_markdown(manifest),
        )

    if clip_data:
        clip_data.update({
            "artifact_layout_version": ARTIFACT_LAYOUT_VERSION,
            "artifact_dir": layout["artifact_dir"],
            "overview_path": layout["overview_path"],
            "analysis_report_path": (
                layout["report_path"] if os.path.isfile(layout["report_path"]) else None
            ),
            "task_manifest_json_path": layout["task_manifest_json_path"],
            "task_manifest_md_path": layout["task_manifest_md_path"],
            "unified_queue_json_path": layout["unified_queue_json_path"],
            "unified_queue_md_path": layout["unified_queue_md_path"],
            "clip_review_checkpoint_path": layout["clip_review_checkpoint_path"],
            "topic_analysis_checkpoint_path": layout["topic_analysis_checkpoint_path"],
            "corrected_srt_path": (
                layout["corrected_srt_path"]
                if os.path.isfile(layout["corrected_srt_path"])
                else clip_data.get("corrected_srt_path")
            ),
        })
        manual_timeline = clip_data.get("manual_timeline")
        if isinstance(manual_timeline, dict):
            if os.path.isfile(layout["optimized_timeline_json_path"]):
                manual_timeline["optimized_json_path"] = layout[
                    "optimized_timeline_json_path"
                ]
            if os.path.isfile(layout["optimized_timeline_md_path"]):
                manual_timeline["optimized_md_path"] = layout[
                    "optimized_timeline_md_path"
                ]
        _write_artifact_json(layout["clip_marks_path"], clip_data)

    _rewrite_organized_report_links(layout)

    overview = _render_artifact_overview(
        layout,
        clip_data=clip_data,
        manifest=manifest,
        slice_dir=actual_slice_dir,
    )
    _write_artifact_text(layout["overview_path"], overview)
    _write_artifact_text(
        layout["slice_pointer_path"],
        actual_slice_dir + "\n",
    )
    copied_files.extend([layout["overview_path"], layout["slice_pointer_path"]])
    return {
        **layout,
        "slice_dir": actual_slice_dir,
        "clip_count": len(_dedupe_clip_marks(clip_data.get("clip_marks") or [])),
        "copied_files": sorted(set(copied_files)),
    }


def _build_refinement_manifest(video_path, source_srt_path, corrected_srt_path,
                               analysis_report_path, clip_marks_path, clip_marks,
                               manifest_json_path, manifest_md_path):
    """构造一场录播的统一精调任务数据。"""
    tasks = []
    for index, mark in enumerate(_dedupe_clip_marks(clip_marks or []), 1):
        filename = _topic_clip_filename(index, mark, video_path)
        tasks.append({
            "id": f"{index:02d}",
            "status": "等待自动切片",
            "clip_filename": filename,
            "slice_path": None,
            "subtitle_path": None,
            "start": int(mark["start"]),
            "end": int(mark["end"]),
            "duration": int(mark["end"] - mark["start"]),
            "topic_start": int(mark.get("topic_start", mark["start"])),
            "topic_end": int(mark.get("topic_end", mark["end"])),
            "topic_title": mark.get("title", "未命名片段"),
            "clip_type": mark.get("clip_type", "topic"),
            "series_title": mark.get("series_title"),
            "outro_trigger": mark.get("outro_trigger"),
            "preserve_to_video_end": bool(mark.get("preserve_to_video_end")),
            "publish_title": _normalise_publish_title(
                mark.get("publish_title"), mark.get("title", "未命名片段")
            ),
            "natural_boundary_pre_sec": int(mark.get("natural_boundary_pre_sec", 0)),
            "natural_boundary_post_sec": int(mark.get("natural_boundary_post_sec", 0)),
            "steps": [
                {"key": key, "label": label, "status": "待处理"}
                for key, label in REFINEMENT_WORKFLOW_STEPS
            ],
        })
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "schema_version": 1,
        "status": "等待自动切片" if tasks else "无可切片段",
        "generated_at": now,
        "updated_at": now,
        "video_name": os.path.basename(video_path),
        "source_video_path": os.path.abspath(video_path),
        "source_srt_path": os.path.abspath(source_srt_path) if source_srt_path else None,
        "corrected_srt_path": os.path.abspath(corrected_srt_path) if corrected_srt_path else None,
        "analysis_report_path": os.path.abspath(analysis_report_path),
        "clip_marks_path": os.path.abspath(clip_marks_path),
        "manifest_json_path": os.path.abspath(manifest_json_path),
        "manifest_md_path": os.path.abspath(manifest_md_path),
        "slice_output_dir": None,
        "tasks": tasks,
    }


def _render_refinement_manifest_markdown(manifest):
    """把精调任务数据渲染成可直接勾选的 Markdown。"""
    lines = [
        f"# {manifest.get('video_name', '录播')} 精调任务清单",
        f"> 自动生成 | 总状态: {manifest.get('status', '待处理')} | "
        f"更新时间: {manifest.get('updated_at', '')}",
        "",
        "## 文件",
        "",
        f"- 源录播: `{manifest.get('source_video_path') or '无'}`",
        f"- 校对字幕: `{manifest.get('corrected_srt_path') or '无'}`",
        f"- 话题报告: `{manifest.get('analysis_report_path') or '无'}`",
        f"- 切片标记: `{manifest.get('clip_marks_path') or '无'}`",
        f"- 切片目录: `{manifest.get('slice_output_dir') or '等待自动切片'}`",
        f"- 精调总清单: `{manifest.get('unified_queue_md_path') or '未启用'}`",
        "",
        "## 切片队列",
        "",
    ]
    tasks = manifest.get("tasks") or []
    if not tasks:
        lines.append("本次没有可切片段。")
        lines.append("")
        return "\n".join(lines)
    for task in tasks:
        lines.extend([
            f"### {task.get('id')} {task.get('topic_title', '未命名片段')}",
            "",
            f"- 状态: {task.get('status', '待处理')}",
            f"- 视频内时间: {_format_report_time(task.get('start', 0))}－"
            f"{_format_report_time(task.get('end', 0))}（{task.get('duration', 0)} 秒）",
            f"- 切片文件: `{task.get('slice_path') or task.get('clip_filename')}`",
            f"- 片段字幕: `{task.get('subtitle_path') or '精剪导出后在字幕校对页识别'}`",
            f"- 投稿标题: {task.get('publish_title', '')}",
        ])
        if task.get("clip_type") == "stream_outro":
            lines.append(
                f"- 系列收播片: {task.get('series_title') or task.get('topic_title')}"
                f"（触发语：{task.get('outro_trigger') or '收播口令'}）"
            )
        for step in task.get("steps") or []:
            checked = "x" if step.get("status") == "已完成" else " "
            lines.append(f"- [{checked}] {step.get('label')}（{step.get('status', '待处理')}）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _unified_refinement_queue_paths(queue_dir=None):
    root = os.path.abspath(
        queue_dir
        or os.path.join(DEFAULT_REFINEMENT_QUEUE_DIR, ARTIFACT_QUEUE_DIRNAME)
    )
    return (
        os.path.join(root, UNIFIED_REFINEMENT_QUEUE_JSON),
        os.path.join(root, UNIFIED_REFINEMENT_QUEUE_MD),
    )


def _refinement_task_is_completed(task):
    status = str(task.get("status", "")).strip()
    if status in {"已完成", "已发布", "已投稿"}:
        return True
    steps = task.get("steps") or []
    return bool(steps) and all(step.get("status") == "已完成" for step in steps)


def _unified_refinement_record(manifest):
    """从单场清单提取总队列需要的信息，保留剪映阶段的关键路径和首尾依据。"""
    tasks = []
    for task in manifest.get("tasks") or []:
        tasks.append({
            "id": task.get("id"),
            "status": task.get("status", "待处理"),
            "topic_title": task.get("topic_title", "未命名片段"),
            "clip_type": task.get("clip_type", "topic"),
            "series_title": task.get("series_title"),
            "outro_trigger": task.get("outro_trigger"),
            "preserve_to_video_end": bool(task.get("preserve_to_video_end")),
            "publish_title": task.get("publish_title", ""),
            "start": int(task.get("start", 0)),
            "end": int(task.get("end", 0)),
            "duration": int(task.get("duration", 0)),
            "topic_start": int(task.get("topic_start", task.get("start", 0))),
            "topic_end": int(task.get("topic_end", task.get("end", 0))),
            "natural_boundary_pre_sec": int(task.get("natural_boundary_pre_sec", 0)),
            "natural_boundary_post_sec": int(task.get("natural_boundary_post_sec", 0)),
            "clip_filename": task.get("clip_filename"),
            "slice_path": task.get("slice_path"),
            "subtitle_path": task.get("subtitle_path"),
            "steps": [dict(step) for step in task.get("steps") or []],
        })
    completed_count = sum(_refinement_task_is_completed(task) for task in tasks)
    ready_count = sum(
        not _refinement_task_is_completed(task) and task.get("status") == "待精调"
        for task in tasks
    )
    waiting_slice_count = sum(task.get("status") == "等待自动切片" for task in tasks)
    source_video_path = os.path.abspath(manifest.get("source_video_path") or manifest.get("video_name") or "")
    return {
        "recording_key": os.path.normcase(source_video_path),
        "video_name": manifest.get("video_name", os.path.basename(source_video_path)),
        "status": manifest.get("status", "待处理"),
        "updated_at": manifest.get("updated_at", datetime.now().isoformat(timespec="seconds")),
        "source_video_path": source_video_path,
        "corrected_srt_path": manifest.get("corrected_srt_path"),
        "analysis_report_path": manifest.get("analysis_report_path"),
        "manifest_json_path": manifest.get("manifest_json_path"),
        "manifest_md_path": manifest.get("manifest_md_path"),
        "slice_output_dir": manifest.get("slice_output_dir"),
        "task_count": len(tasks),
        "pending_count": len(tasks) - completed_count,
        "ready_count": ready_count,
        "waiting_slice_count": waiting_slice_count,
        "completed_count": completed_count,
        "tasks": tasks,
    }


def _render_unified_refinement_queue_markdown(queue):
    """渲染跨录播总队列，优先展示真正需要进入剪映的任务。"""
    lines = [
        "# AutoSlice 精调任务总清单",
        f"> 自动生成 | {queue.get('recording_count', 0)} 场录播 | "
        f"待处理 {queue.get('pending_count', 0)} 个切片 | "
        f"可进剪映 {queue.get('ready_count', 0)} 个 | "
        f"更新时间: {queue.get('updated_at', '')}",
        "",
        "## 当前队列",
        "",
    ]
    recordings = queue.get("recordings") or []
    if not recordings:
        lines.extend(["目前没有精调任务。", ""])
        return "\n".join(lines)
    for recording in recordings:
        lines.extend([
            f"### {recording.get('video_name', '录播')}",
            "",
            f"- 状态: {recording.get('status', '待处理')}；"
            f"待处理 {recording.get('pending_count', 0)}/{recording.get('task_count', 0)}",
            f"- 校对字幕: `{recording.get('corrected_srt_path') or '无'}`",
            f"- 单场清单: `{recording.get('manifest_md_path') or '无'}`",
            f"- 切片目录: `{recording.get('slice_output_dir') or '等待自动切片'}`",
            "",
        ])
        tasks = recording.get("tasks") or []
        if not tasks:
            lines.extend(["本场没有可切片段。", ""])
            continue
        for task in tasks:
            completed = _refinement_task_is_completed(task)
            checked = "x" if completed else " "
            pre_context = max(0, int(task.get("topic_start", 0)) - int(task.get("start", 0)))
            post_context = max(0, int(task.get("end", 0)) - int(task.get("topic_end", 0)))
            lines.extend([
                f"- [{checked}] {task.get('id', '')} {task.get('topic_title', '未命名片段')}"
                f"（{task.get('status', '待处理')}，{task.get('duration', 0)} 秒）",
                f"  - 视频内时间: {_format_report_time(task.get('start', 0))}－"
                f"{_format_report_time(task.get('end', 0))}",
                f"  - 切片: `{task.get('slice_path') or task.get('clip_filename') or '等待自动切片'}`",
                f"  - 片段字幕: `{task.get('subtitle_path') or '精剪导出后在字幕校对页识别'}`",
                f"  - 首尾: 已在话题核心前保留 {pre_context} 秒、后保留 {post_context} 秒；"
                f"自然停顿额外调整前 {task.get('natural_boundary_pre_sec', 0)} 秒、"
                f"后 {task.get('natural_boundary_post_sec', 0)} 秒",
                f"  - 投稿标题: {task.get('publish_title', '')}",
            ])
            if task.get("clip_type") == "stream_outro":
                lines.append(
                    f"  - 系列收播片: {task.get('series_title') or task.get('topic_title')}"
                    f"（触发语：{task.get('outro_trigger') or '收播口令'}）"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _upsert_unified_refinement_queue(manifest, queue_json_path=None, queue_md_path=None):
    """按源录播更新总队列；并发流水线通过进程内锁避免互相覆盖。"""
    default_json_path, default_md_path = _unified_refinement_queue_paths()
    json_path = os.path.abspath(queue_json_path or default_json_path)
    md_path = os.path.abspath(queue_md_path or default_md_path)
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    record = _unified_refinement_record(manifest)
    with _UNIFIED_REFINEMENT_QUEUE_LOCK:
        queue = {"schema_version": 1, "recordings": []}
        if os.path.isfile(json_path):
            try:
                with open(json_path, encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict) and isinstance(existing.get("recordings"), list):
                    queue = existing
            except (OSError, ValueError, TypeError):
                pass
        recordings = [
            item for item in queue.get("recordings") or []
            if isinstance(item, dict) and item.get("recording_key") != record["recording_key"]
        ]
        recordings.append(record)
        recordings.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        queue.update({
            "schema_version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "recording_count": len(recordings),
            "task_count": sum(int(item.get("task_count", 0)) for item in recordings),
            "pending_count": sum(int(item.get("pending_count", 0)) for item in recordings),
            "ready_count": sum(int(item.get("ready_count", 0)) for item in recordings),
            "waiting_slice_count": sum(int(item.get("waiting_slice_count", 0)) for item in recordings),
            "completed_count": sum(int(item.get("completed_count", 0)) for item in recordings),
            "recordings": recordings,
        })
        json_temp_path = json_path + ".tmp"
        md_temp_path = md_path + ".tmp"
        with open(json_temp_path, "w", encoding="utf-8") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
        with open(md_temp_path, "w", encoding="utf-8") as f:
            f.write(_render_unified_refinement_queue_markdown(queue))
        os.replace(json_temp_path, json_path)
        os.replace(md_temp_path, md_path)
    return json_path, md_path


def _write_refinement_manifest_files(manifest):
    """同步写入 JSON 和 Markdown 两种任务清单。"""
    json_path = manifest["manifest_json_path"]
    md_path = manifest["manifest_md_path"]
    os.makedirs(os.path.dirname(os.path.abspath(json_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(md_path)), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_render_refinement_manifest_markdown(manifest))
    return json_path, md_path


def _update_refinement_manifest_after_slice(manifest_json_path, report_dir, marks):
    """自动切片完成后回写实际文件路径，保留已有人工步骤状态。"""
    if not manifest_json_path or not os.path.isfile(manifest_json_path):
        return False
    with open(manifest_json_path, encoding="utf-8") as f:
        manifest = json.load(f)
    tasks_by_name = {
        task.get("clip_filename"): task
        for task in manifest.get("tasks") or []
        if task.get("clip_filename")
    }
    source_path = (
        manifest.get("source_video_path")
        or manifest.get("video_path")
        or ".flv"
    )
    found_count = 0
    for index, mark in enumerate(_dedupe_clip_marks(marks or []), 1):
        candidate_filenames = _compatible_topic_clip_filenames(
            index,
            mark,
            source_path,
        )
        task = next(
            (
                tasks_by_name[name]
                for name in candidate_filenames
                if name in tasks_by_name
            ),
            None,
        )
        if not task:
            continue
        filename = next(
            (
                name
                for name in candidate_filenames
                if os.path.isfile(os.path.join(report_dir, name))
            ),
            candidate_filenames[0],
        )
        task["clip_filename"] = filename
        output_path = os.path.abspath(os.path.join(report_dir, filename))
        task["slice_path"] = output_path
        subtitle_path = os.path.abspath(
            os.path.join(report_dir, _clip_subtitle_filename(filename))
        )
        task["subtitle_path"] = subtitle_path if os.path.isfile(subtitle_path) else None
        for step in task.get("steps") or []:
            if step.get("key") == "correct_subtitles":
                step["label"] = "精剪导出后自动识别、校对并压制字幕"
        if os.path.isfile(output_path):
            task["status"] = "待精调"
            found_count += 1
        else:
            task["status"] = "切片文件缺失"
    manifest["slice_output_dir"] = os.path.abspath(report_dir)
    manifest["status"] = "待精调" if found_count else "切片文件缺失"
    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    queue_json_path = manifest.get("unified_queue_json_path")
    queue_md_path = manifest.get("unified_queue_md_path")
    if queue_json_path or queue_md_path:
        try:
            _upsert_unified_refinement_queue(
                manifest,
                queue_json_path=queue_json_path,
                queue_md_path=queue_md_path,
            )
            manifest["unified_queue_warning"] = None
        except (OSError, ValueError, TypeError) as e:
            manifest["unified_queue_warning"] = f"精调总清单更新失败: {e}"
    _write_refinement_manifest_files(manifest)
    return True


def _build_timeline_report(
        video_name, peak_info, topics, failed_chunks=None, api_warning=None,
        streamer_name="主播", group_by_hour=False, manual_timeline=None,
        clip_marks=None, corrected_srt_path=None, unified_queue_md_path=None,
        report_dir=None):
    """生成最终 Markdown：逐话题时间轴 + Part 分组。"""
    manual_timeline = manual_timeline or {}
    manual_entries = manual_timeline.get("entries") or []
    lines = [
        f"# {video_name} 话题分析报告",
        f"> 自动生成 | 模型: {LLM_ANALYSIS_MODEL}（整场话题） + "
        f"{LLM_MODEL}（人工时间轴/切片复核） | {peak_info}",
        "> 时间基准：视频内时间/播放进度（不是现实钟点）；实际切片会自动向前后扩展保留上下文",
    ]
    if corrected_srt_path:
        if report_dir:
            lines.append(
                "> 剪映校对字幕: "
                + _markdown_relative_artifact_link(
                    corrected_srt_path,
                    report_dir,
                )
            )
        else:
            lines.append(f"> 剪映校对字幕: {os.path.basename(corrected_srt_path)}")
    if unified_queue_md_path:
        if report_dir:
            queue_link = os.path.relpath(
                unified_queue_md_path, report_dir
            ).replace(os.sep, "/")
            lines.append(
                f"> 精调总清单: [{os.path.basename(unified_queue_md_path)}]"
                f"({queue_link})"
            )
        else:
            lines.append(f"> 精调总清单: {unified_queue_md_path}")
    if manual_timeline.get("path"):
        star_count = sum(1 for item in manual_entries if item.get("stars", 0) > 0)
        source_count = manual_timeline.get("source_entry_count", len(manual_entries))
        raw_count = manual_timeline.get("raw_entry_count", source_count)
        optimized_count = manual_timeline.get("optimized_entry_count")
        if optimized_count is not None:
            count_label = f"当前分段原始 {raw_count} 条 → 字幕优化 {optimized_count} 个候选"
        else:
            count_label = (
                f"当前分段 {len(manual_entries)}/{source_count} 条记录"
                if source_count != len(manual_entries)
                else f"{len(manual_entries)} 条记录"
            )
        lines.append(
            f"> 人工时间轴辅助: {os.path.basename(manual_timeline['path'])} | "
            f"{count_label}, ⭐重点 {star_count} 条"
        )
        if manual_timeline.get("optimized_md_path"):
            optimized_md_path = manual_timeline["optimized_md_path"]
            if report_dir:
                optimized_link = os.path.relpath(
                    optimized_md_path, report_dir
                ).replace(os.sep, "/")
                lines.append(
                    f"> 字幕优化时间轴: [{os.path.basename(optimized_md_path)}]"
                    f"({optimized_link})"
                )
            else:
                lines.append(f"> 字幕优化时间轴: {optimized_md_path}")
    lines.extend(["---", "", "## 逐话题时间轴", ""])

    groups = _group_topics_for_parts(topics)
    if not groups:
        lines.append("本次没有解析到有效话题。")
        lines.append("")
    else:
        topic_index = 1
        if group_by_hour:
            iterable = _group_topics_by_hour(topics)
        else:
            iterable = [(idx - 1, group) for idx, group in enumerate(_group_topics_for_parts(topics), 1)]
        for display_part_index, (part_index, group) in enumerate(iterable, 1):
            part_start = min(t["start"] for t in group)
            part_end = max(t["end"] for t in group)
            if group_by_hour:
                part_title = f"第{part_index + 1}小时重点"
            else:
                part_title = _make_part_title(group, streamer_name=streamer_name)
            lines.append(
                f"Part {display_part_index}: {part_title} "
                f"({_format_report_time(part_start)}－{_format_report_time(part_end)})"
            )
            for topic in group:
                lines.append(_format_topic_block(topic, topic_index, streamer_name=streamer_name))
                topic_index += 1
            lines.append("")

    publish_title_lines = _publish_title_report_lines(
        clip_marks,
        source_path=video_name,
    )
    if publish_title_lines:
        lines.extend(publish_title_lines)

    if api_warning:
        lines.append("## 分析警告")
        lines.append("")
        lines.append(f"- {api_warning}")
        lines.append("")

    if failed_chunks:
        lines.append("## LLM 分块失败记录")
        lines.append("")
        for item in failed_chunks:
            lines.append(
                f"- 块 {item.get('index')} [{item.get('time')}] "
                f"连续失败，已跳过：{item.get('error')}"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"












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
    flv_path = _validated_video_path(flv_path)
    with streamer_profile_context(streamer_profile_id, flv_path) as profile:
        result = _run_pipeline_impl(
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


def _run_pipeline_impl(
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
    artifact_layout = _artifact_bundle_layout(
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
    _seed_artifact_from_legacy(
        artifact_layout["asr_checkpoint_path"],
        _funasr_checkpoint_path(flv_path),
    )

    # Step 1: 确保 SRT 存在
    if progress_callback:
        progress_callback("Step 1/5: 检查/生成字幕...", 0, 100)
    source_srt_path = ensure_srt(
        flv_path,
        _scaled_progress_callback(progress_callback, 0, 14),
        checkpoint_path=artifact_layout["asr_checkpoint_path"],
    )
    if not source_srt_path:
        raise RuntimeError("无法生成 SRT 字幕")
    corrected_srt_path = export_corrected_srt(
        source_srt_path,
        output_path=artifact_layout["corrected_srt_path"],
    )
    srt_path = corrected_srt_path or source_srt_path
    if corrected_srt_path and progress_callback:
        progress_callback(
            f"已生成剪映校对字幕: {os.path.basename(corrected_srt_path)}",
            14,
            100,
        )

    # Step 2: 弹幕分析
    if progress_callback:
        progress_callback("Step 2/5: 弹幕密度分析...", 15, 100)
    peaks = analyze_danmaku(ass_path) if ass_path else DanmakuDensitySeries()
    avg_den = _average_danmaku_density(peaks)
    if peaks:
        high_energy_peaks = _high_energy_danmaku_peaks(peaks, avg_den)
        peak_info = (
            f"弹幕密度 {len(peaks)} 个滑动窗口, "
            f"独立高能峰值 {len(high_energy_peaks)} 个, "
            f"全场平均密度 {avg_den:.0f}条/分钟"
        )
    else:
        peak_info = "无弹幕数据"

    # Step 3: SRT 分块
    if progress_callback:
        progress_callback("Step 3/5: SRT 分块中...", 20, 100)
    segs = parse_srt_text(srt_path)
    chunks = chunk_srt(segs, peaks)
    srt_duration = max((end for _, end, _ in segs), default=None)
    probed_video_duration = _probe_video_duration(flv_path)
    video_duration = probed_video_duration or srt_duration
    if optimized_timeline_path:
        selected_optimized_path = os.path.abspath(optimized_timeline_path)
        _copy_artifact_file(
            selected_optimized_path,
            artifact_layout["optimized_timeline_json_path"],
        )
        selected_optimized_md_path = os.path.splitext(selected_optimized_path)[0] + ".md"
        _copy_artifact_file(
            selected_optimized_md_path,
            artifact_layout["optimized_timeline_md_path"],
        )
        manual_timeline = _load_optimized_timeline_artifact(
            artifact_layout["optimized_timeline_json_path"],
            flv_path,
            manual_timeline_path=(
                manual_timeline_path
                if manual_timeline_path not in (None, "__none__")
                else None
            ),
        )
    else:
        manual_timeline = _prepare_optimized_manual_timeline(
            flv_path,
            base,
            segs,
            peaks,
            video_duration,
            manual_timeline_path,
            streamer_name=streamer_display_name,
            progress_callback=progress_callback,
            retry_incomplete_artifact=False,
            artifact_layout=artifact_layout,
        )
    raw_manual_entry_count = int(manual_timeline.get("raw_entry_count", 0))
    manual_entries = manual_timeline.get("entries") or []
    optimization_warning = manual_timeline.get("optimization_warning")
    if manual_entries:
        if progress_callback:
            count_label = (
                f"原始 {raw_manual_entry_count} 条 → 字幕优化 {len(manual_entries)} 个候选"
            )
            progress_callback(
                f"已加载人工时间轴: {os.path.basename(manual_timeline['path'])}，"
                f"{count_label}",
                24, 100,
            )
    # Step 4: 首轮只分析字幕和弹幕，避免人工措辞锚定标题与语义边界。
    topic_analysis_checkpoint_path = artifact_layout[
        "topic_analysis_checkpoint_path"
    ]
    _seed_artifact_from_legacy(
        topic_analysis_checkpoint_path,
        base + "_topic_analysis_checkpoint.json",
    )
    accepted_topics, failed_chunks, api_precheck_warning = candidate_analysis.analyze_topic_chunks(
        chunks,
        streamer_display_name,
        progress_callback=progress_callback,
        checkpoint_path=topic_analysis_checkpoint_path,
    )
    if optimization_warning:
        api_precheck_warning = "；".join(
            item for item in (optimization_warning, api_precheck_warning) if item
        )

    candidate_analysis.merge_manual_timeline_topics(accepted_topics, manual_entries)
    manual_validation_warning = _validate_unmatched_manual_topics(
        accepted_topics,
        streamer_name=streamer_display_name,
        progress_callback=progress_callback,
        srt_segments=segs,
        peaks=peaks,
    )
    if manual_validation_warning:
        api_precheck_warning = "；".join(
            item for item in (api_precheck_warning, manual_validation_warning) if item
        )
    accepted_topics = _clean_topics_for_report(accepted_topics)
    analysis_topics = _analysis_topics_snapshot(accepted_topics)
    clip_review_checkpoint_path = artifact_layout["clip_review_checkpoint_path"]
    _seed_artifact_from_legacy(
        clip_review_checkpoint_path,
        base + "_clip_review_checkpoint.json",
    )
    candidate_analysis.write_clip_review_checkpoint(
        clip_review_checkpoint_path,
        analysis_topics,
        stage="ready",
    )
    _apply_danmaku_slice_decisions(
        accepted_topics,
        peaks,
        avg_den,
    )
    clip_review_warning = _review_peak_selected_topics(
        accepted_topics,
        srt_segments=segs,
        peaks=peaks,
        streamer_name=streamer_display_name,
        progress_callback=progress_callback,
        checkpoint_callback=lambda current, pending, round_label, batch_index, total_batches: (
            candidate_analysis.write_clip_review_checkpoint(
                clip_review_checkpoint_path,
                current,
                stage="reviewing",
                pending_count=len(pending),
                round=round_label,
                batch_index=batch_index,
                total_batches=total_batches,
            )
        ),
    )
    if clip_review_warning:
        api_precheck_warning = "；".join(
            item for item in (api_precheck_warning, clip_review_warning) if item
        )
    accepted_topics = _clean_topics_for_report(accepted_topics)
    _apply_danmaku_slice_decisions(
        accepted_topics,
        peaks,
        avg_den,
        require_clip_review=True,
    )
    title_review_warning = _review_selected_publish_titles(
        accepted_topics,
        streamer_name=streamer_display_name,
        progress_callback=progress_callback,
        checkpoint_callback=lambda current, batch_index, total_batches: (
            candidate_analysis.write_clip_review_checkpoint(
                clip_review_checkpoint_path,
                current,
                stage="title_reviewing",
                batch_index=batch_index,
                total_batches=total_batches,
            )
        ),
    )
    if title_review_warning:
        api_precheck_warning = "；".join(
            item for item in (api_precheck_warning, title_review_warning) if item
        )
    accepted_topics = _clean_topics_for_report(accepted_topics)
    # 复核旧产物时 analysis_topics 可能已带上一轮生成的收播片；收播片由
    # 当前字幕和真实视频时长重新判定，避免报告和队列出现重复的系列任务。
    accepted_topics = [
        topic for topic in accepted_topics
        if topic.get("clip_type") != "stream_outro"
    ]
    raw_clip_marks = _clip_marks_from_topics(accepted_topics)
    candidate_review_audit = _build_clip_candidate_review_audit(accepted_topics)
    candidate_review_audit_path = artifact_layout["candidate_review_audit_path"]
    _write_artifact_json(candidate_review_audit_path, candidate_review_audit)
    srt_segments_for_context = parse_srt_segments(srt_path)
    outro_mark = _detect_stream_outro_clip(
        srt_segments_for_context,
        probed_video_duration,
        streamer_profile=streamer_profile,
    )
    if outro_mark:
        accepted_topics.append(_outro_topic_from_mark(outro_mark))
        raw_clip_marks.append(outro_mark)
    clip_marks = _expand_clip_marks_with_context(
        raw_clip_marks,
        srt_segments=srt_segments_for_context,
        video_duration=video_duration or _srt_video_duration(srt_segments_for_context),
    )
    _synchronise_selected_topic_ranges(accepted_topics, clip_marks)
    analysis_topics = _analysis_topics_snapshot(accepted_topics)
    if progress_callback:
        progress_callback("Step 5/5: 生成报告...", 97, 100)
    report = _build_timeline_report(
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
            "manual_timeline": _manual_timeline_summary(manual_timeline),
            "analysis_topics": analysis_topics,
            "clip_marks": clip_marks,
        },
    )

    refinement_manifest = _build_refinement_manifest(
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
    _write_refinement_manifest_files(refinement_manifest)
    unified_queue_warning = None
    try:
        _upsert_unified_refinement_queue(
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

    organized = organize_existing_artifacts(
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
        "manual_timeline": _manual_timeline_summary(manual_timeline),
    }


def _manual_timeline_for_rebuilt_report(summary, flv_path):
    """从现有 JSON 恢复报告头所需的人工时间轴元数据。"""
    summary = dict(summary or {})
    optimized_path = summary.get("optimized_json_path")
    source_path = summary.get("path")
    if optimized_path and os.path.isfile(optimized_path):
        try:
            return _load_optimized_timeline_artifact(
                optimized_path,
                flv_path,
                manual_timeline_path=source_path,
            )
        except (OSError, ValueError, TypeError):
            pass
    entry_count = int(summary.get("entry_count", 0) or 0)
    star_count = min(entry_count, int(summary.get("star_count", 0) or 0))
    summary["entries"] = [
        {"stars": 1 if index < star_count else 0}
        for index in range(entry_count)
    ]
    return summary


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
    flv_path = _validated_video_path(flv_path)
    with streamer_profile_context(streamer_profile_id, flv_path) as profile:
        result = _retry_clip_review_from_artifacts_impl(
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


def _retry_clip_review_from_artifacts_impl(
        flv_path, ass_path=None, json_path=None, report_path=None,
        progress_callback=None, output_dir=None, artifact_dir=None):
    """复用已有逐话题报告，只重做弹幕候选筛选、字幕复核和最终产物。"""
    streamer_profile = current_streamer_profile()
    flv_path = os.path.abspath(flv_path)
    base, _ = os.path.splitext(flv_path)
    if output_dir is None and artifact_dir is None:
        output_dir = os.path.dirname(flv_path)
    artifact_layout = _artifact_bundle_layout(
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
        recovered_topics = _parse_generated_topic_report(report_path)
    baseline_topics = _clean_topics_for_report(
        _analysis_topics_snapshot(recovered_topics)
    )
    if rebuilt_manual_entries:
        candidate_analysis.merge_manual_timeline_topics(baseline_topics, rebuilt_manual_entries)
        baseline_topics = _clean_topics_for_report(baseline_topics)
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
                    accepted_topics = _clean_topics_for_report(checkpoint_topics)
                    if rebuilt_manual_entries:
                        candidate_analysis.merge_manual_timeline_topics(
                            accepted_topics,
                            rebuilt_manual_entries,
                        )
                        accepted_topics = _clean_topics_for_report(accepted_topics)
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
                        accepted_topics = _clean_topics_for_report(checkpoint_topics)
                        resume_review = True
                    elif _clip_review_checkpoint_is_complete(
                            checkpoint, checkpoint_topics):
                        accepted_topics = _clean_topics_for_report(checkpoint_topics)
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

    candidate_analysis.write_clip_review_checkpoint(
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
        _apply_danmaku_slice_decisions(
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
                _append_clip_candidate_source(topic, "语义复核")
    if reuse_completed_review:
        clip_review_warning = None
    else:
        clip_review_warning = _review_peak_selected_topics(
            accepted_topics,
            srt_segments=srt_segments,
            peaks=peaks,
            streamer_name=streamer_profile.report_name,
            progress_callback=progress_callback,
            checkpoint_callback=lambda current, pending, round_label, batch_index, total_batches: (
                candidate_analysis.write_clip_review_checkpoint(
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
    accepted_topics = _clean_topics_for_report(accepted_topics)
    _apply_danmaku_slice_decisions(
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
            candidate_analysis.write_clip_review_checkpoint(
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
    accepted_topics = _clean_topics_for_report(accepted_topics)
    accepted_topics = [
        topic for topic in accepted_topics
        if topic.get("clip_type") != "stream_outro"
    ]
    raw_clip_marks = _clip_marks_from_topics(accepted_topics)
    candidate_review_audit = _build_clip_candidate_review_audit(accepted_topics)
    candidate_review_audit_path = artifact_layout["candidate_review_audit_path"]
    _write_artifact_json(candidate_review_audit_path, candidate_review_audit)
    probed_video_duration = _probe_video_duration(flv_path)
    video_duration = probed_video_duration or _srt_video_duration(srt_segments)
    outro_mark = _detect_stream_outro_clip(
        srt_segments,
        probed_video_duration,
        streamer_profile=streamer_profile,
    )
    if outro_mark:
        accepted_topics.append(_outro_topic_from_mark(outro_mark))
        raw_clip_marks.append(outro_mark)
    clip_marks = _expand_clip_marks_with_context(
        raw_clip_marks,
        srt_segments=srt_segments,
        video_duration=video_duration,
    )
    _synchronise_selected_topic_ranges(accepted_topics, clip_marks)

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
    report = _build_timeline_report(
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
        "manual_timeline": _manual_timeline_summary(manual_timeline),
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
    refinement_manifest = _build_refinement_manifest(
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
    _write_refinement_manifest_files(refinement_manifest)
    try:
        _upsert_unified_refinement_queue(
            refinement_manifest,
            queue_json_path=unified_queue_json_path,
            queue_md_path=unified_queue_md_path,
        )
    except (OSError, ValueError, TypeError) as exc:
        refinement_manifest["unified_queue_warning"] = f"精调总清单更新失败: {exc}"
        _write_refinement_manifest_files(refinement_manifest)

    _write_completed_clip_review_checkpoint(
        clip_review_checkpoint_path,
        accepted_topics,
        warning=clip_review_warning,
        source="artifact_retry",
        completed_at=clip_review_completed_at,
    )
    organized = organize_existing_artifacts(
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


_GENERATED_VIDEO_SUFFIX_PATTERN = "|".join(
    re.escape(extension.removeprefix("."))
    for extension in SUPPORTED_VIDEO_EXTENSIONS
)
_GENERATED_TOPIC_ARTIFACT_RE = re.compile(
    rf'^\d{{2,3}}_\d+s_.+\.(?:{_GENERATED_VIDEO_SUFFIX_PATTERN}|srt)$',
    re.IGNORECASE,
)
_GENERATED_TOPIC_TEMP_RE = re.compile(
    rf'^(?:\d{{2,3}}_\d+s_.+\.(?:{_GENERATED_VIDEO_SUFFIX_PATTERN})'
    rf'\.part\.(?:{_GENERATED_VIDEO_SUFFIX_PATTERN})|'
    r'\.autoslice_seek_index_\d+\.mkv)$',
    re.IGNORECASE,
)


def _cleanup_stale_topic_clips(report_dir, preserve_names=None):
    """清理失效自动产物；可保留已通过校验的现有切片视频。"""
    if not os.path.isdir(report_dir):
        return 0
    preserved = {
        str(name).casefold()
        for name in (preserve_names or [])
        if str(name).strip()
    }
    removed = 0
    for name in os.listdir(report_dir):
        if not (
                _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name)
                or _GENERATED_TOPIC_TEMP_RE.fullmatch(name)):
            continue
        if (
                _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name)
                and name.casefold() in preserved):
            continue
        path = os.path.join(report_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        removed += 1
    return removed


def _format_ffmpeg_seconds(value):
    """生成稳定的 ffmpeg 秒数字符串，避免无意义的长浮点尾数。"""
    return f"{float(value):.3f}".rstrip("0").rstrip(".") or "0"


def _is_reusable_topic_clip(output_path, source_path, expected_duration):
    """校验已有切片是否仍对应当前源录播和当前计划时长。"""
    force_rebuild = os.environ.get("AUTOSLICE_FORCE_RESLICE", "").strip().lower()
    if force_rebuild in {"1", "true", "yes", "on"}:
        return False
    try:
        output_stat = os.stat(output_path)
        source_stat = os.stat(source_path)
    except OSError:
        return False
    if output_stat.st_size <= 0 or output_stat.st_mtime_ns < source_stat.st_mtime_ns:
        return False
    actual_duration = _probe_video_duration(output_path)
    return (
        actual_duration is not None
        and abs(float(actual_duration) - float(expected_duration))
        <= SLICE_DURATION_TOLERANCE_SEC
    )


def _reuse_compatible_topic_clip(job, source_path):
    """首选产物不存在时，原路径复用兼容的历史容器产物。"""

    preferred_path = os.path.abspath(job["output_path"])
    output_stem = os.path.splitext(preferred_path)[0]
    for extension in compatible_output_extensions(source_path):
        candidate_path = output_stem + extension
        if os.path.normcase(candidate_path) == os.path.normcase(preferred_path):
            continue
        if not _is_reusable_topic_clip(
                candidate_path, source_path, job["duration"]):
            continue
        job["output_path"] = candidate_path
        job["output_name"] = os.path.basename(candidate_path)
        return True
    return False


def _reuse_topic_clip_after_title_change(job, report_dir, source_path):
    """起点和时长未变时复用旧视频，允许标题或候选编号发生变化。"""
    expected_name = str(job["output_name"])
    start_marker = f'_{int(job["start"])}s_'.casefold()
    try:
        names = os.listdir(report_dir)
    except OSError:
        return False
    candidates = []
    compatible_extensions = set(compatible_output_extensions(source_path))
    for name in names:
        if name.casefold() == expected_name.casefold():
            continue
        if not _GENERATED_TOPIC_ARTIFACT_RE.fullmatch(name):
            continue
        if (
                start_marker not in name.casefold()
                or normalise_video_extension(name) not in compatible_extensions):
            continue
        path = os.path.join(report_dir, name)
        try:
            modified_ns = os.stat(path).st_mtime_ns
        except OSError:
            continue
        candidates.append((modified_ns, path))
    for _modified_ns, candidate_path in sorted(candidates, reverse=True):
        if not _is_reusable_topic_clip(
                candidate_path, source_path, job["duration"]):
            continue
        target_extension = normalise_video_extension(candidate_path)
        target_path = os.path.splitext(job["output_path"])[0] + target_extension
        try:
            os.replace(candidate_path, target_path)
        except OSError:
            try:
                shutil.copy2(candidate_path, target_path)
            except OSError:
                try:
                    os.remove(target_path)
                except OSError:
                    pass
                continue
        job["output_path"] = target_path
        job["output_name"] = os.path.basename(target_path)
        return True
    return False


def _preferred_slice_video_encoder_args():
    """优先使用本机 NVENC；无 NVIDIA 环境时回退到高质量软件编码。"""
    requested = os.environ.get("AUTOSLICE_VIDEO_ENCODER", "auto").strip().lower()
    use_nvenc = requested in {"nvenc", "h264_nvenc"}
    if requested == "auto":
        use_nvenc = shutil.which("nvidia-smi") is not None
    if use_nvenc:
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-profile:v", "high",
            "-rc:v", "vbr", "-cq:v", "23", "-b:v", "0",
        ]
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-crf", "19",
    ]


def _software_slice_video_encoder_args():
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-crf", "19",
    ]


def _configured_slice_concurrency():
    """首次索引切片最多使用 2 路 NVENC，环境变量可主动降为 1。"""
    raw_value = os.environ.get(
        "AUTOSLICE_SLICE_CONCURRENCY",
        str(SLICE_DEFAULT_CONCURRENCY),
    )
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = SLICE_DEFAULT_CONCURRENCY
    return max(1, min(SLICE_MAX_CONCURRENCY, value))


def _build_precise_slice_ffmpeg_command(
        input_path, output_path, start_s, duration, video_encoder_args):
    """双重 seek 丢弃关键帧前置内容，再编码视频得到精确首尾。"""
    coarse_start = max(0.0, float(start_s) - SLICE_EXACT_SEEK_PREROLL_SEC)
    precise_offset = max(0.0, float(start_s) - coarse_start)
    command = [
        "ffmpeg", "-y",
        "-ss", _format_ffmpeg_seconds(coarse_start),
        "-i", input_path,
    ]
    if precise_offset > 0:
        command.extend(["-ss", _format_ffmpeg_seconds(precise_offset)])
    command.extend([
        "-t", _format_ffmpeg_seconds(duration),
        "-map", "0:v:0", "-map", "0:a:0?",
        *video_encoder_args,
        "-c:a", "copy",
        "-avoid_negative_ts", "make_zero",
        output_path,
    ])
    return command


def _prepare_seekable_slice_source(
        flv_path, report_dir, mark_count, subprocess_module, progress_callback=None,
        total_seek_sec=None, source_span_sec=None):
    """多片段 FLV 先临时重封装为带索引的 MKV，避免每段线性扫描整场。"""
    seek_cost_requires_index = (
        mark_count >= 2
        and total_seek_sec is not None
        and source_span_sec is not None
        and float(source_span_sec) > 0
        and float(total_seek_sec) >= float(source_span_sec)
    )
    if (
            (mark_count < SLICE_INDEX_MIN_CLIPS and not seek_cost_requires_index)
            or os.path.splitext(flv_path)[1].lower() != ".flv"):
        return flv_path, None
    try:
        source_size = os.path.getsize(flv_path)
        if shutil.disk_usage(report_dir).free < source_size * 1.2:
            return flv_path, None
    except OSError:
        return flv_path, None

    temp_path = os.path.join(report_dir, f".autoslice_seek_index_{os.getpid()}.mkv")
    if os.path.exists(temp_path):
        os.remove(temp_path)
    if progress_callback:
        progress_callback("正在构建临时快速定位索引...", 0, mark_count)
    try:
        subprocess_module.run([
            "ffmpeg", "-y", "-i", flv_path,
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", temp_path,
        ], check=True, stdout=subprocess_module.DEVNULL, stderr=subprocess_module.DEVNULL)
    except (OSError, subprocess_module.CalledProcessError):
        if os.path.exists(temp_path):
            os.remove(temp_path)
        if progress_callback:
            progress_callback("临时索引构建失败，改用源录播定位", 0, mark_count)
        return flv_path, None
    return temp_path, temp_path


def _slice_from_marks_impl(flv_path, json_path, output_dir, progress_callback=None):
    """
    【新功能】根据话题分析生成的 clip_marks.json 自动切片。
    完全独立于现有的弹幕切片和时间轴切片模式。

    返回: (切片数, 输出子目录)
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"JSON 文件不存在: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    marks = _dedupe_clip_marks(data.get("clip_marks", []))
    if not data.get("expanded_with_context"):
        srt_segments_for_context = parse_srt_segments(
            os.path.splitext(flv_path)[0] + ".srt"
        )
        marks = _expand_clip_marks_with_context(
            marks,
            srt_segments=srt_segments_for_context,
            video_duration=_srt_video_duration(srt_segments_for_context),
        )
    if not marks:
        if progress_callback:
            progress_callback("无切片标记", 0, 1)
        return 0, ""

    import subprocess as sp
    video_name = os.path.basename(flv_path)
    base_name = os.path.splitext(video_name)[0]
    report_dir = os.path.join(output_dir, base_name + "_话题切片")
    os.makedirs(report_dir, exist_ok=True)
    precise_video_end = None
    if any(mark.get("preserve_to_video_end") for mark in marks):
        precise_video_end = _probe_video_duration(flv_path)
    subtitle_source_path = _resolve_clip_subtitle_source(flv_path, data)
    subtitle_segments = (
        parse_srt_segments(subtitle_source_path)
        if subtitle_source_path
        else []
    )

    slice_jobs = []
    for index, mark in enumerate(marks, 1):
        start_s = float(mark["start"])
        end_s = float(mark["end"])
        if mark.get("preserve_to_video_end") and precise_video_end:
            # JSON/报告使用向上取整的整秒作为可读边界；真正编码时必须以
            # ffprobe 的浮点时长收尾，否则计划时长会比源视频多近一秒，
            # 既可能触发时长校验失败，也无法准确表达“保留到关播”。
            end_s = min(end_s, float(precise_video_end))
        duration = end_s - start_s
        if duration <= 0:
            continue
        output_name = _topic_clip_filename(index, mark, flv_path)
        slice_jobs.append({
            "index": index,
            "mark": mark,
            "start": start_s,
            "end": end_s,
            "duration": duration,
            "title": mark.get("title", f"片段{index}"),
            "output_name": output_name,
            "output_path": os.path.join(report_dir, output_name),
        })

    reusable_jobs = []
    pending_jobs = []
    title_renamed_count = 0
    for job in slice_jobs:
        if _is_reusable_topic_clip(
                job["output_path"], flv_path, job["duration"]):
            reusable_jobs.append(job)
        elif _reuse_compatible_topic_clip(job, flv_path):
            reusable_jobs.append(job)
        elif _reuse_topic_clip_after_title_change(job, report_dir, flv_path):
            reusable_jobs.append(job)
            title_renamed_count += 1
        else:
            pending_jobs.append(job)

    removed_count = _cleanup_stale_topic_clips(
        report_dir,
        preserve_names=[job["output_name"] for job in reusable_jobs],
    )

    if progress_callback:
        if removed_count:
            progress_callback(
                f"已清理 {removed_count} 个旧字幕或失效自动产物",
                0,
                len(marks),
            )
        if reusable_jobs and pending_jobs:
            rename_note = (
                f"，其中 {title_renamed_count} 个仅更新标题"
                if title_renamed_count else ""
            )
            progress_callback(
                f"已复用 {len(reusable_jobs)} 个现有切片{rename_note}，"
                f"仅重切 {len(pending_jobs)} 个",
                0,
                len(marks),
            )
        elif reusable_jobs:
            rename_note = (
                f"，其中 {title_renamed_count} 个仅更新标题"
                if title_renamed_count else ""
            )
            progress_callback(
                f"已复用 {len(reusable_jobs)} 个现有切片{rename_note}，无需重新编码",
                0,
                len(marks),
            )
        else:
            progress_callback(f"开始切片 ({len(pending_jobs)} 段)...", 0, len(marks))

    count = len(reusable_jobs)
    slice_source = flv_path
    temporary_seek_source = None
    video_encoder_args = (
        _preferred_slice_video_encoder_args()
        if pending_jobs
        else None
    )
    slice_progress = _serialized_progress_callback(progress_callback)

    def encode_slice_job(job, requested_encoder_args):
        index = job["index"]
        start_s = job["start"]
        duration = job["duration"]
        title = job["title"]
        output_path = job["output_path"]
        output_extension = normalise_video_extension(output_path)
        temporary_output_path = output_path + ".part" + output_extension
        effective_encoder_args = list(requested_encoder_args)

        if slice_progress:
            slice_progress(
                f"切片 {index}/{len(marks)}: {title}",
                index,
                len(marks),
            )

        if os.path.exists(temporary_output_path):
            os.remove(temporary_output_path)
        try:
            command = _build_precise_slice_ffmpeg_command(
                slice_source,
                temporary_output_path,
                start_s,
                duration,
                effective_encoder_args,
            )
            try:
                sp.run(
                    command,
                    check=True,
                    stdout=sp.DEVNULL,
                    stderr=sp.PIPE,
                    encoding="utf-8",
                    errors="replace",
                )
            except sp.CalledProcessError:
                if "h264_nvenc" not in effective_encoder_args:
                    raise
                if os.path.exists(temporary_output_path):
                    os.remove(temporary_output_path)
                effective_encoder_args = _software_slice_video_encoder_args()
                if slice_progress:
                    slice_progress(
                        "NVENC 不可用，已改用 CPU 精确编码",
                        index - 1,
                        len(marks),
                    )
                command = _build_precise_slice_ffmpeg_command(
                    slice_source,
                    temporary_output_path,
                    start_s,
                    duration,
                    effective_encoder_args,
                )
                sp.run(
                    command,
                    check=True,
                    stdout=sp.DEVNULL,
                    stderr=sp.PIPE,
                    encoding="utf-8",
                    errors="replace",
                )

            actual_duration = _probe_video_duration(temporary_output_path)
            if (
                    actual_duration is None
                    or abs(actual_duration - duration) > SLICE_DURATION_TOLERANCE_SEC):
                raise RuntimeError(
                    f"切片 {index} 时长校验失败：计划 {duration:.3f}s，"
                    f"实际 {actual_duration if actual_duration is not None else '无法读取'}"
                )
            os.replace(temporary_output_path, output_path)
            return effective_encoder_args
        except Exception:
            if os.path.exists(temporary_output_path):
                os.remove(temporary_output_path)
            raise

    try:
        if pending_jobs:
            total_seek_sec = sum(
                max(0.0, job["start"] - SLICE_EXACT_SEEK_PREROLL_SEC)
                for job in pending_jobs
            )
            source_span_sec = (
                _srt_video_duration(subtitle_segments)
                or max(job["end"] for job in slice_jobs)
            )
            slice_source, temporary_seek_source = _prepare_seekable_slice_source(
                flv_path,
                report_dir,
                len(pending_jobs),
                sp,
                progress_callback=progress_callback,
                total_seek_sec=total_seek_sec,
                source_span_sec=source_span_sec,
            )
        remaining_jobs = list(pending_jobs)
        can_probe_parallel_nvenc = (
            len(remaining_jobs) >= SLICE_INDEX_MIN_CLIPS
            and temporary_seek_source is not None
            and "h264_nvenc" in (video_encoder_args or [])
            and _configured_slice_concurrency() > 1
        )
        if can_probe_parallel_nvenc:
            probe_job = remaining_jobs.pop(0)
            video_encoder_args = encode_slice_job(probe_job, video_encoder_args)
            count += 1

        can_parallel_encode = (
            can_probe_parallel_nvenc
            and "h264_nvenc" in (video_encoder_args or [])
            and len(remaining_jobs) > 1
        )
        if can_parallel_encode:
            workers = min(_configured_slice_concurrency(), len(remaining_jobs))
            if slice_progress:
                slice_progress(
                    f"NVENC 探针通过，启用 {workers} 路并行切片",
                    count,
                    len(marks),
                )
            with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="autoslice-encode") as executor:
                futures = [
                    executor.submit(encode_slice_job, job, video_encoder_args)
                    for job in remaining_jobs
                ]
                for future in as_completed(futures):
                    future.result()
                    count += 1
        else:
            for job in remaining_jobs:
                video_encoder_args = encode_slice_job(job, video_encoder_args)
                count += 1
    finally:
        if temporary_seek_source and os.path.exists(temporary_seek_source):
            os.remove(temporary_seek_source)

    _update_refinement_manifest_after_slice(
        data.get("task_manifest_json_path"),
        report_dir,
        marks,
    )

    organized = organize_existing_artifacts(
        flv_path,
        output_dir=output_dir,
        json_path=json_path,
        report_path=data.get("analysis_report_path"),
        slice_dir=report_dir,
        artifact_dir=data.get("artifact_dir"),
    )

    if progress_callback:
        progress_callback(
            f"完成! {count} 个片段 → {report_dir}；概览 {organized['overview_path']}",
            len(marks),
            len(marks),
        )

    return count, report_dir


def slice_from_marks(
        flv_path, json_path, output_dir, progress_callback=None,
        streamer_profile_id="auto"):
    """按标记切片，并在当前线程显式激活调用方冻结的主播配置。"""
    flv_path = _validated_video_path(flv_path, for_slicing=True)
    with streamer_profile_context(streamer_profile_id, flv_path):
        return _slice_from_marks_impl(
            flv_path,
            json_path,
            output_dir,
            progress_callback=progress_callback,
        )




# ============================================================
# CLI 测试
# ============================================================
if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1:
        flv = sys.argv[1]
        ass = os.path.splitext(flv)[0] + ".ass"
        if not os.path.exists(ass):
            ass = None
        result = run_pipeline(flv, ass, progress_callback=lambda m, s, t: print(f"[{s}%] {m}"))
        print(f"\n报告: {result['md_path']}")
        print(f"切片标记: {len(result['clip_marks'])} 个")
        for cm in result['clip_marks'][:10]:
            print(f"  [{fmt_time(cm['start'])}-{fmt_time(cm['end'])}] {cm['title']}")
    else:
        print("用法: python topic_engine.py <视频文件>")
