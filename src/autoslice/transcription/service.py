"""旧版转录入口的同对象兼容 façade；业务实现位于职责模块。"""

from __future__ import annotations

from autoslice import media_probe
from autoslice.streamer_profiles import (
    infer_streamer_name,
    profile_identity_names,
    profile_matches_streamer,
)
from autoslice.transcription import (
    checkpoints as checkpoint_store,
    model_runtime,
    recognition,
    results as result_contracts,
    segments as subtitle_segments,
    srt_io,
    workflow,
)

FACADE_EXPORTS = {
    "_infer_streamer_name": "infer_streamer_name",
    "FUNASR_CHECKPOINT_VERSION": "FUNASR_CHECKPOINT_VERSION",
    "FUNASR_CHUNK_PRE_CONTEXT_SEC": "FUNASR_CHUNK_PRE_CONTEXT_SEC",
    "FUNASR_CHUNK_SEC": "FUNASR_CHUNK_SEC",
    "_funasr_model_runtime_signature": "_funasr_model_runtime_signature",
    "_funasr_checkpoint_path": "funasr_checkpoint_path",
    "_funasr_source_fingerprint": "_funasr_source_fingerprint",
    "_funasr_chunk_fingerprint": "_funasr_chunk_fingerprint",
    "_funasr_chunk_input_window": "_funasr_chunk_input_window",
    "_is_close_number": "_is_close_number",
    "_prepare_funasr_checkpoint": "_prepare_funasr_checkpoint",
    "_write_funasr_checkpoint": "write_funasr_checkpoint",
    "_profile_identity_names": "profile_identity_names",
}

# 结果契约兼容别名。
_normalise_funasr_result = result_contracts.normalise_funasr_result
_is_valid_funasr_result = result_contracts.is_valid_funasr_result
_primary_speaker_segments = result_contracts.primary_speaker_segments

# 检查点兼容别名。
FUNASR_CHECKPOINT_VERSION = checkpoint_store.FUNASR_CHECKPOINT_VERSION
FUNASR_CHUNK_PRE_CONTEXT_SEC = checkpoint_store.FUNASR_CHUNK_PRE_CONTEXT_SEC
FUNASR_CHUNK_SEC = checkpoint_store.FUNASR_CHUNK_SEC
replace_file_atomically = checkpoint_store.replace_file_atomically
_funasr_model_runtime_signature = checkpoint_store.funasr_model_runtime_signature
funasr_checkpoint_path = checkpoint_store.funasr_checkpoint_path
_funasr_source_fingerprint = checkpoint_store.funasr_source_fingerprint
_funasr_chunk_fingerprint = checkpoint_store.funasr_chunk_fingerprint
_funasr_chunk_input_window = checkpoint_store.funasr_chunk_input_window
_is_close_number = checkpoint_store.is_close_number
_prepare_funasr_checkpoint = checkpoint_store.prepare_funasr_checkpoint
write_funasr_checkpoint = checkpoint_store.write_funasr_checkpoint
_existing_srt_is_reusable = checkpoint_store.existing_srt_is_reusable
_quarantine_incomplete_srt = checkpoint_store.quarantine_incomplete_srt

# 模型运行时兼容别名。
FUNASR_MODEL = model_runtime.FUNASR_MODEL
FUNASR_CONTEXTUAL_MODEL = model_runtime.FUNASR_CONTEXTUAL_MODEL
FUNASR_VAD_MODEL = model_runtime.FUNASR_VAD_MODEL
FUNASR_PUNC_MODEL = model_runtime.FUNASR_PUNC_MODEL
FUNASR_SPK_MODEL = model_runtime.FUNASR_SPK_MODEL
FUNASR_DEFAULT_DEVICE = model_runtime.FUNASR_DEFAULT_DEVICE
FUNASR_BATCH_SIZE_SEC = model_runtime.FUNASR_BATCH_SIZE_SEC
FUNASR_CPU_RETRY_DELAY_SEC = model_runtime.FUNASR_CPU_RETRY_DELAY_SEC
FUNASR_CACHE_MODEL_DIR = model_runtime.FUNASR_CACHE_MODEL_DIR
FUNASR_CONTEXTUAL_CACHE_MODEL_DIR = model_runtime.FUNASR_CONTEXTUAL_CACHE_MODEL_DIR
FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC = (
    model_runtime.FUNASR_CONTEXTUAL_CACHE_MODEL_DIR_IIC
)
FUNASR_NANO_MODEL = model_runtime.FUNASR_NANO_MODEL
FUNASR_NANO_CACHE_ROOTS = model_runtime.FUNASR_NANO_CACHE_ROOTS
FUNASR_VAD_CACHE_MODEL_DIR = model_runtime.FUNASR_VAD_CACHE_MODEL_DIR
FUNASR_PUNC_CACHE_MODEL_DIR = model_runtime.FUNASR_PUNC_CACHE_MODEL_DIR
FUNASR_SPK_CACHE_MODEL_DIR = model_runtime.FUNASR_SPK_CACHE_MODEL_DIR
FUNASR_SPK_WEIGHT_FILES = model_runtime.FUNASR_SPK_WEIGHT_FILES
FUNASR_FOREGROUND_AUDIO_FILTER = model_runtime.FUNASR_FOREGROUND_AUDIO_FILTER
FUNASR_HOTWORD_MAX_COUNT = model_runtime.FUNASR_HOTWORD_MAX_COUNT
FUNASR_HOTWORD_MAX_CHARS = model_runtime.FUNASR_HOTWORD_MAX_CHARS
_prepare_funasr_environment = model_runtime.prepare_funasr_environment
funasr_model_cache_candidates = model_runtime.funasr_model_cache_candidates
_funasr_nano_cache_candidates = model_runtime.funasr_nano_cache_candidates
resolve_funasr_model_source = model_runtime.resolve_funasr_model_source
resolve_funasr_aux_model_source = model_runtime.resolve_funasr_aux_model_source
resolve_funasr_speaker_model_source = model_runtime.resolve_funasr_speaker_model_source
_funasr_hotwords = model_runtime.funasr_hotwords
_funasr_generate_kwargs = model_runtime.funasr_generate_kwargs
resolve_funasr_device = model_runtime.resolve_funasr_device
funasr_public_status = model_runtime.funasr_public_status
load_funasr_model = model_runtime.load_funasr_model
clear_funasr_cuda_cache = model_runtime.clear_funasr_cuda_cache

# 字幕分段兼容别名。
for _facade_name, _owner_name in subtitle_segments.FACADE_EXPORTS.items():
    globals()[_facade_name] = getattr(subtitle_segments, _owner_name)
srt_time = subtitle_segments.srt_time
parse_srt_timestamp = subtitle_segments.parse_srt_timestamp

# SRT、媒体探测和完整转录工作流兼容别名。
_read_srt_entries = srt_io.read_srt_entries
_load_repaired_srt_segments = srt_io.load_repaired_srt_segments
export_corrected_srt = srt_io.export_corrected_srt
probe_video_duration = media_probe.probe_video_duration
ensure_srt = workflow.ensure_srt
