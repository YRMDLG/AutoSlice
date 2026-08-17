"""FunASR 转录、SRT 规范化与可恢复检查点的唯一实现。"""

from __future__ import annotations

import bisect
import difflib
import hashlib
import html
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime

from autoslice.transcription import model_runtime, results as result_contracts
from autoslice.transcription.contracts import (
    DEFAULT_SUBTITLE_MAX_CHARS,
)
from autoslice.streamer_profiles import (
    current_streamer_profile,
    infer_streamer_name,
    profile_identity_names,
    profile_matches_streamer,
)


FACADE_EXPORTS = {
    '_infer_streamer_name': 'infer_streamer_name',
    'FUNASR_CHECKPOINT_VERSION': 'FUNASR_CHECKPOINT_VERSION',
    'FUNASR_CHUNK_PRE_CONTEXT_SEC': 'FUNASR_CHUNK_PRE_CONTEXT_SEC',
    'FUNASR_CHUNK_SEC': 'FUNASR_CHUNK_SEC',
    'SRT_ABNORMAL_CHARS_PER_SEC': 'SRT_ABNORMAL_CHARS_PER_SEC',
    'SRT_MAX_ESTIMATED_SEG_SEC': 'SRT_MAX_ESTIMATED_SEG_SEC',
    'SRT_REPEAT_REPAIR_MIN_ENTRIES': 'SRT_REPEAT_REPAIR_MIN_ENTRIES',
    'SUBTITLE_LEGACY_REPAIR_MAX_CHARS': 'SUBTITLE_LEGACY_REPAIR_MAX_CHARS',
    'SUBTITLE_MAX_CHARS': 'SUBTITLE_MAX_CHARS',
    'SUBTITLE_MAX_DURATION_SEC': 'SUBTITLE_MAX_DURATION_SEC',
    'SUBTITLE_PAUSE_BREAK_SEC': 'SUBTITLE_PAUSE_BREAK_SEC',
    'SUBTITLE_TARGET_CHARS': 'SUBTITLE_TARGET_CHARS',
    '_repair_srt_end_time': '_repair_srt_end_time',
    '_join_asr_tokens': '_join_asr_tokens',
    '_strip_asr_subtitle_punctuation': '_strip_asr_subtitle_punctuation',
    '_normalise_asr_text': '_normalise_asr_text',
    '_split_subtitle_text_for_display': '_split_subtitle_text_for_display',
    '_split_timed_subtitle_segment': '_split_timed_subtitle_segment',
    '_should_hold_subtitle_for_short_clause': '_should_hold_subtitle_for_short_clause',
    '_segment_timed_tokens': '_segment_timed_tokens',
    '_segments_from_funasr_result': '_segments_from_funasr_result',
    '_read_srt_entries': '_read_srt_entries',
    'export_corrected_srt': 'export_corrected_srt',
    '_probe_video_duration': 'probe_video_duration',
    '_funasr_model_runtime_signature': '_funasr_model_runtime_signature',
    '_funasr_checkpoint_path': 'funasr_checkpoint_path',
    '_funasr_source_fingerprint': '_funasr_source_fingerprint',
    '_funasr_chunk_fingerprint': '_funasr_chunk_fingerprint',
    '_funasr_chunk_input_window': '_funasr_chunk_input_window',
    '_is_close_number': '_is_close_number',
    '_prepare_funasr_checkpoint': '_prepare_funasr_checkpoint',
    '_write_funasr_checkpoint': 'write_funasr_checkpoint',
    '_dedupe_overlapping_funasr_segments': '_dedupe_overlapping_funasr_segments',
    '_is_funasr_punctuation': '_is_funasr_punctuation',
    '_attach_funasr_punctuation_to_tokens': '_attach_funasr_punctuation_to_tokens',
    '_align_funasr_tokens': '_align_funasr_tokens',
    '_trim_funasr_tokens_to_core': '_trim_funasr_tokens_to_core',
    'ensure_srt': 'ensure_srt',
    '_srt_time': 'srt_time',
    '_parse_srt_timestamp': 'parse_srt_timestamp',
    'SRT_ESTIMATED_CHARS_PER_SEC': 'SRT_ESTIMATED_CHARS_PER_SEC',
    'TOPIC_CONTEXT_GAP': 'TOPIC_CONTEXT_GAP',
    '_load_repaired_srt_segments': '_load_repaired_srt_segments',
    '_normalise_streamer_terms': '_normalise_streamer_terms',
    '_profile_identity_names': 'profile_identity_names',
    '_profile_matches_streamer': 'profile_matches_streamer',
    '_subtitle_text_size': '_subtitle_text_size',
    '_text_len_for_timing': '_text_len_for_timing',
}


# 公开原子替换 seam：测试可替换该对象，而不再 patch 高层 façade。
replace_file_atomically = os.replace

# 旧调用方仍可从 service 导入这些对象；唯一实现位于 transcription.results。
_normalise_funasr_result = result_contracts.normalise_funasr_result
_is_valid_funasr_result = result_contracts.is_valid_funasr_result
_primary_speaker_segments = result_contracts.primary_speaker_segments

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
resolve_funasr_speaker_model_source = (
    model_runtime.resolve_funasr_speaker_model_source
)
_funasr_hotwords = model_runtime.funasr_hotwords
_funasr_generate_kwargs = model_runtime.funasr_generate_kwargs
resolve_funasr_device = model_runtime.resolve_funasr_device
funasr_public_status = model_runtime.funasr_public_status
load_funasr_model = model_runtime.load_funasr_model
clear_funasr_cuda_cache = model_runtime.clear_funasr_cuda_cache

FUNASR_CHUNK_SEC = 120.0

FUNASR_CHUNK_PRE_CONTEXT_SEC = 20.0

FUNASR_CHECKPOINT_VERSION = 3

TOPIC_CONTEXT_GAP = 4.0         # SRT 语句间隔边界

SRT_ABNORMAL_CHARS_PER_SEC = 18 # 超过该语速视为 ASR 时间戳异常

SRT_ESTIMATED_CHARS_PER_SEC = 7 # 异常长字幕按该语速估算结束时间

SRT_MAX_ESTIMATED_SEG_SEC = 300 # 单条异常字幕最多估算 5 分钟

SRT_REPEAT_REPAIR_MIN_ENTRIES = 8  # 旧版把整段全文按每个字重复写入时的识别下限

SUBTITLE_TARGET_CHARS = 10

SUBTITLE_MAX_CHARS = DEFAULT_SUBTITLE_MAX_CHARS

SUBTITLE_LEGACY_REPAIR_MAX_CHARS = 28

SUBTITLE_MAX_DURATION_SEC = 7.0

SUBTITLE_PAUSE_BREAK_SEC = 0.65


def _text_len_for_timing(text):
    """估算语速用长度：去掉空白，保留中文/数字/字母。"""
    return len(re.sub(r'\s+', '', text or ""))


def _repair_srt_end_time(start_s, end_s, text):
    """修复 FunASR 偶发的“几百字压到零点几秒”时间戳。"""
    duration = max(0.001, end_s - start_s)
    text_len = _text_len_for_timing(text)
    if text_len < 80:
        return end_s
    if text_len / duration <= SRT_ABNORMAL_CHARS_PER_SEC:
        return end_s
    estimated = min(SRT_MAX_ESTIMATED_SEG_SEC, max(duration, text_len / SRT_ESTIMATED_CHARS_PER_SEC))
    return start_s + estimated


def _join_asr_tokens(tokens):
    """拼接 FunASR 字/词 token；中文不加空格，连续英文词保留分隔。"""
    result = ""
    for token in (str(item).strip() for item in tokens):
        if not token:
            continue
        if result and re.search(r'[A-Za-z0-9]$', result) and re.match(r'^[A-Za-z0-9]', token):
            result += " "
        result += token
    return result.strip()


def _strip_asr_subtitle_punctuation(text):
    """按剪辑习惯移除 ASR 字幕标点；逗号类保留为单个分隔空格。"""
    result = []
    comma_like = {"，", ",", "、"}
    for char in str(text or ""):
        if char in comma_like:
            result.append(" ")
        elif unicodedata.category(char).startswith("P"):
            continue
        else:
            result.append(char)
    return re.sub(r"\s+", " ", "".join(result)).strip()


def _normalise_asr_text(text, streamer_name="主播"):
    """清理 ASR 分词空格，并应用当前主播配置的低歧义专名纠错。"""
    if isinstance(text, (list, tuple)):
        tokens = text
    else:
        tokens = re.split(r'\s+', str(text or "").replace("\n", " ").strip())
    clean = _join_asr_tokens(tokens)
    clean = _normalise_streamer_terms(clean, streamer_name=streamer_name)
    return _strip_asr_subtitle_punctuation(clean)


def _normalise_streamer_terms(text, streamer_name="主播"):
    """统一字幕和 AI 报告中的主播/粉丝专名，不改动其它排版。"""
    clean = str(text or "")
    profile = current_streamer_profile()
    if not profile_matches_streamer(profile, streamer_name):
        return clean
    for source, target in profile.asr_replacements:
        clean = clean.replace(source, target)
    return clean


def _subtitle_text_size(text):
    return len(re.sub(r'\s+', '', text or ""))


def _split_subtitle_text_for_display(text, max_chars=SUBTITLE_MAX_CHARS):
    """按可读长度拆分字幕正文，优先在空格处断开，必要时按字硬切。"""
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        max_chars = SUBTITLE_MAX_CHARS
    remaining = re.sub(r"\s+", " ", str(text or "")).strip()
    parts = []
    while _subtitle_text_size(remaining) > max_chars:
        visible_count = 0
        hard_cut = len(remaining)
        for index, char in enumerate(remaining):
            if not char.isspace():
                visible_count += 1
            if visible_count >= max_chars:
                hard_cut = index + 1
                break
        preferred_cut = remaining.rfind(" ", 0, hard_cut)
        if (
                preferred_cut > 0
                and _subtitle_text_size(remaining[:preferred_cut])
                >= max(2, int(max_chars * 0.55))):
            cut = preferred_cut
        else:
            cut = hard_cut
        part = remaining[:cut].strip()
        if not part:
            part = remaining[:hard_cut].strip()
            cut = hard_cut
        if not part:
            break
        parts.append(part)
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def _split_timed_subtitle_segment(start_s, end_s, text, max_chars=SUBTITLE_MAX_CHARS):
    """将过长字幕按正文比例分配为连续时间段，避免单条字幕越界。"""
    parts = _split_subtitle_text_for_display(text, max_chars=max_chars)
    if not parts:
        return []
    start_s = float(start_s)
    end_s = float(end_s)
    if end_s <= start_s:
        # 仅修复无效时间戳；合法的极短字幕仍必须保留原始时间范围。
        end_s = start_s + 0.1
    if len(parts) == 1:
        return [(start_s, end_s, parts[0])]

    weights = [max(1, _subtitle_text_size(part)) for part in parts]
    total_weight = sum(weights)
    duration = end_s - start_s
    minimum_duration = min(0.05, duration / len(parts))
    cursor = start_s
    segments = []
    for index, (part, weight) in enumerate(zip(parts, weights)):
        if index == len(parts) - 1:
            next_cursor = end_s
        else:
            ideal_cursor = start_s + duration * sum(weights[:index + 1]) / total_weight
            remaining_parts = len(parts) - index - 1
            earliest_cursor = cursor + minimum_duration
            latest_cursor = end_s - minimum_duration * remaining_parts
            next_cursor = min(latest_cursor, max(earliest_cursor, ideal_cursor))
        segments.append((cursor, next_cursor, part))
        cursor = next_cursor
    return segments


def _should_hold_subtitle_for_short_clause(timed_tokens, index, current_chars, max_chars):
    """句末只剩一两个字时延后软截断，避免把短语尾巴单独丢到下一条。"""
    if current_chars >= max_chars:
        return False
    trailing_chars = 0
    for _start_s, _end_s, future_token in timed_tokens[index + 1:]:
        for char in str(future_token or ""):
            if char.isspace():
                continue
            if unicodedata.category(char).startswith("P") or char == "…":
                return (
                    0 < trailing_chars <= 2
                    and current_chars + trailing_chars <= max_chars
                )
            trailing_chars += 1
            if trailing_chars > 2 or current_chars + trailing_chars > max_chars:
                return False
    return False


def _segment_timed_tokens(
        timed_tokens, streamer_name="主播", max_chars=SUBTITLE_MAX_CHARS):
    """把字/词时间戳整理成适合阅读和边界吸附的短句字幕。"""
    if not timed_tokens:
        return []
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        max_chars = SUBTITLE_MAX_CHARS
    segments = []
    current = []
    current_chars = 0
    sentence_end_tokens = "。！？!?；;"

    def flush():
        nonlocal current, current_chars
        if not current:
            return
        text = _normalise_asr_text([item[2] for item in current], streamer_name=streamer_name)
        if text:
            segments.extend(_split_timed_subtitle_segment(
                current[0][0], current[-1][1], text, max_chars=max_chars,
            ))
        current = []
        current_chars = 0

    for index, (start_s, end_s, token) in enumerate(timed_tokens):
        token = str(token).strip()
        if not token:
            continue
        current.append((float(start_s), float(end_s), token))
        current_chars += _subtitle_text_size(token)
        duration = current[-1][1] - current[0][0]
        next_gap = 0.0
        if index + 1 < len(timed_tokens):
            next_gap = max(0.0, float(timed_tokens[index + 1][0]) - float(end_s))
        target_break = (
            current_chars >= SUBTITLE_TARGET_CHARS
            and (next_gap >= 0.15 or duration >= 4.5)
        )
        hold_for_short_clause = (
            target_break
            and _should_hold_subtitle_for_short_clause(
                timed_tokens,
                index,
                current_chars,
                max_chars,
            )
        )
        should_break = (
            (token[-1] in sentence_end_tokens and current_chars >= 4)
            or (next_gap >= SUBTITLE_PAUSE_BREAK_SEC and current_chars >= 2)
            or (target_break and not hold_for_short_clause)
            or current_chars >= max_chars
            or duration >= SUBTITLE_MAX_DURATION_SEC
        )
        if should_break:
            flush()
    flush()

    # Nano 的 VAD 分段有时会把句末标点放到下一段的开头。标点没有独立
    # 语义，应回挂到上一条；极短尾字也在时间连续且不超长时并回上一条。
    closing_punctuation = "，。！？；：、,.!?;:）)]}》】”’"
    polished = []
    for start_s, end_s, text in segments:
        leading = ""
        while text and text[0] in closing_punctuation:
            leading += text[0]
            text = text[1:]
        if leading and polished:
            previous = polished[-1]
            polished[-1] = (previous[0], previous[1], previous[2] + leading)
        if not text:
            if polished:
                previous = polished[-1]
                polished[-1] = (
                    previous[0], max(previous[1], end_s), previous[2]
                )
            continue
        if polished:
            previous = polished[-1]
            gap = max(0.0, start_s - previous[1])
            combined_text = previous[2] + text
            if (
                    end_s - start_s < 0.5
                    and gap <= 0.4
                    and _subtitle_text_size(combined_text) <= max_chars):
                polished[-1] = (previous[0], end_s, combined_text)
                continue
        polished.append((start_s, end_s, text))
    return polished


def _segments_from_funasr_result(
        text, timestamps, offset=0.0, streamer_name="主播", raw_text=None,
        max_chars=SUBTITLE_MAX_CHARS):
    """把单个 FunASR 结果转成短句，避免把整段全文复制到每个字时间戳。"""
    timestamps = [item for item in (timestamps or []) if isinstance(item, (list, tuple)) and len(item) == 2]
    if not text or not timestamps:
        return []
    tokens, aligned = _align_funasr_tokens(text, timestamps, raw_text=raw_text)
    if not aligned:
        start_s = offset + float(timestamps[0][0]) / 1000.0
        end_s = offset + float(timestamps[-1][1]) / 1000.0
        clean = _normalise_asr_text(text, streamer_name=streamer_name)
        return _split_timed_subtitle_segment(
            start_s,
            max(end_s, start_s + 0.1),
            clean,
            max_chars=max_chars,
        )
    timed_tokens = [
        (
            offset + float(timestamp[0]) / 1000.0,
            offset + float(timestamp[1]) / 1000.0,
            token,
        )
        for token, timestamp in zip(tokens, timestamps)
    ]
    return _segment_timed_tokens(
        timed_tokens,
        streamer_name=streamer_name,
        max_chars=max_chars,
    )


def _read_srt_entries(srt_path):
    """读取原始 SRT 条目，不提前修正时间，供异常结构识别使用。"""
    if not srt_path or not os.path.exists(srt_path):
        return []
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    pattern = r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)'
    entries = []
    for start_str, end_str, text in re.findall(pattern, content, re.DOTALL):
        clean_text = text.strip().replace("\n", " ").strip()
        if not clean_text:
            continue
        entries.append((
            parse_srt_timestamp(start_str),
            parse_srt_timestamp(end_str),
            clean_text,
        ))
    return entries


def _load_repaired_srt_segments(srt_path):
    """加载健康 SRT，并无损还原旧版 FunASR 的“全文逐字重复”异常文件。"""
    entries = _read_srt_entries(srt_path)
    if not entries:
        return []
    streamer_name = infer_streamer_name(srt_path)
    segments = []
    index = 0
    while index < len(entries):
        raw_text = entries[index][2]
        group_end = index + 1
        while group_end < len(entries) and entries[group_end][2] == raw_text:
            group_end += 1
        group = entries[index:group_end]
        tokens = raw_text.split()
        is_repeated_funasr_block = (
            len(group) >= SRT_REPEAT_REPAIR_MIN_ENTRIES
            and len(tokens) == len(group)
            and _subtitle_text_size(raw_text) >= 20
        )
        if is_repeated_funasr_block:
            timed_tokens = [
                (entry[0], entry[1], token)
                for entry, token in zip(group, tokens)
            ]
            segments.extend(_segment_timed_tokens(
                timed_tokens,
                streamer_name=streamer_name,
                max_chars=SUBTITLE_LEGACY_REPAIR_MAX_CHARS,
            ))
        else:
            for start_s, end_s, text in group:
                clean_text = _normalise_asr_text(text, streamer_name=streamer_name)
                if not clean_text:
                    continue
                repaired_end = _repair_srt_end_time(start_s, end_s, clean_text)
                if (
                    segments
                    and clean_text == segments[-1][2]
                    and start_s - segments[-1][1] <= TOPIC_CONTEXT_GAP
                ):
                    segments[-1] = (
                        segments[-1][0],
                        max(segments[-1][1], repaired_end),
                        clean_text,
                    )
                else:
                    segments.append((start_s, repaired_end, clean_text))
        index = group_end
    return sorted(segments, key=lambda item: (item[0], item[1]))


def export_corrected_srt(source_srt_path, output_path=None):
    """生成可导入剪映的校对版，不覆盖原始 SRT。"""
    segments = _load_repaired_srt_segments(source_srt_path)
    if not segments:
        return None
    output_path = os.path.abspath(
        output_path or os.path.splitext(source_srt_path)[0] + "_校对字幕.srt"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for index, (start_s, end_s, text) in enumerate(segments, 1):
            f.write(
                f"{index}\n{srt_time(start_s)} --> {srt_time(max(end_s, start_s + 0.1))}\n"
                f"{text}\n\n"
            )
    return output_path


def probe_video_duration(video_path):
    """用 ffprobe 获取当前分段视频的精确时长；失败时返回 None。"""
    if not video_path or not os.path.isfile(video_path):
        return None
    import subprocess as sp
    try:
        result = sp.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", video_path,
            ],
            check=True,
            stdout=sp.PIPE,
            stderr=sp.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except (OSError, ValueError, sp.CalledProcessError):
        return None


def _funasr_model_runtime_signature(foreground_only=False):
    """让旧 ASR 检查点在模型/标点配置变化后自动失效。"""
    model_source = model_runtime.resolve_funasr_model_source()
    contextual_active = "contextual" in str(model_source).casefold()
    vad_source = model_runtime.resolve_funasr_aux_model_source(
        model_runtime.FUNASR_VAD_MODEL,
        model_runtime.FUNASR_VAD_CACHE_MODEL_DIR,
    )
    punc_source = model_runtime.resolve_funasr_aux_model_source(
        model_runtime.FUNASR_PUNC_MODEL,
        model_runtime.FUNASR_PUNC_CACHE_MODEL_DIR,
    )
    speaker_source = (
        model_runtime.resolve_funasr_speaker_model_source()
        if foreground_only else None
    )
    return {
        "asr_model": os.path.normcase(os.path.abspath(model_source)),
        "contextual_hotwords": contextual_active,
        "vad_model": os.path.normcase(os.path.abspath(vad_source)) if vad_source else None,
        "punc_model": os.path.normcase(os.path.abspath(punc_source)) if punc_source else None,
        "speaker_model": (
            os.path.normcase(os.path.abspath(speaker_source))
            if speaker_source else None
        ),
        "foreground_only": bool(foreground_only),
        "foreground_audio_filter": (
            model_runtime.FUNASR_FOREGROUND_AUDIO_FILTER
            if foreground_only else None
        ),
        "funasr_chunk_sec": FUNASR_CHUNK_SEC,
        "funasr_chunk_pre_context_sec": FUNASR_CHUNK_PRE_CONTEXT_SEC,
    }


def funasr_checkpoint_path(video_path):
    return os.path.splitext(video_path)[0] + "_asr_checkpoint.json"


def _funasr_source_fingerprint(
        video_path, duration, hotwords="", foreground_only=False):
    stat = os.stat(video_path)
    payload = {
        "path": os.path.normcase(os.path.abspath(video_path)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration": round(float(duration), 3),
        "sample_rate": 16000,
        "channels": 1,
        "chunk_sec": FUNASR_CHUNK_SEC,
        "chunk_pre_context_sec": FUNASR_CHUNK_PRE_CONTEXT_SEC,
        "runtime_signature": _funasr_model_runtime_signature(
            foreground_only=foreground_only
        ),
        "hotword_digest": hashlib.sha256(
            str(hotwords or "").encode("utf-8")
        ).hexdigest(),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _funasr_chunk_fingerprint(source_fingerprint, index, start, duration):
    value = f"{source_fingerprint}:{index}:{start:.3f}:{duration:.3f}"
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _funasr_chunk_input_window(index, duration):
    """返回主体时间段及带前置语境的实际识别时间段。"""
    core_start = index * FUNASR_CHUNK_SEC
    core_duration = min(FUNASR_CHUNK_SEC, max(0.0, duration - core_start))
    pre_context = min(FUNASR_CHUNK_PRE_CONTEXT_SEC, core_start)
    input_start = core_start - pre_context
    input_duration = core_duration + pre_context
    return core_start, core_duration, input_start, input_duration


def _is_close_number(value, expected):
    try:
        return math.isclose(float(value), expected, abs_tol=0.001)
    except (TypeError, ValueError):
        return False


def _prepare_funasr_checkpoint(
        video_path, duration, chunk_count, checkpoint_path=None, hotwords="",
        foreground_only=False):
    checkpoint_path = os.path.abspath(
        checkpoint_path or funasr_checkpoint_path(video_path)
    )
    source_fingerprint = _funasr_source_fingerprint(
        video_path,
        duration,
        hotwords=hotwords,
        foreground_only=foreground_only,
    )
    payload = {
        "version": FUNASR_CHECKPOINT_VERSION,
        "source_fingerprint": source_fingerprint,
        "runtime_signature": _funasr_model_runtime_signature(
            foreground_only=foreground_only
        ),
        "video_path": os.path.abspath(video_path),
        "duration": float(duration),
        "chunk_sec": FUNASR_CHUNK_SEC,
        "chunk_pre_context_sec": FUNASR_CHUNK_PRE_CONTEXT_SEC,
        "chunk_count": int(chunk_count),
        "status": "pending",
        "foreground_only": bool(foreground_only),
        "foreground_filter_mode": (
            "speaker_diarization"
            if foreground_only
            and model_runtime.resolve_funasr_speaker_model_source()
            else "adaptive_gate" if foreground_only
            else "off"
        ),
        "chunks": {},
    }
    try:
        with open(checkpoint_path, encoding="utf-8") as handle:
            existing = json.load(handle)
    except (OSError, ValueError, TypeError):
        existing = None
    if not isinstance(existing, dict):
        return checkpoint_path, payload
    if (
            existing.get("version") != FUNASR_CHECKPOINT_VERSION
            or existing.get("source_fingerprint") != source_fingerprint
            or existing.get("chunk_count") != chunk_count):
        return checkpoint_path, payload

    existing_chunks = existing.get("chunks")
    if not isinstance(existing_chunks, dict):
        return checkpoint_path, payload
    if isinstance(existing.get("last_failure"), dict):
        payload["last_failure"] = existing["last_failure"]
    for index in range(chunk_count):
        start, chunk_duration, input_start, input_duration = (
            _funasr_chunk_input_window(index, duration)
        )
        expected_fingerprint = _funasr_chunk_fingerprint(
            source_fingerprint,
            index,
            start,
            chunk_duration,
        )
        entry = existing_chunks.get(str(index))
        if (
                isinstance(entry, dict)
                and entry.get("fingerprint") == expected_fingerprint
                and _is_close_number(entry.get("input_start"), input_start)
                and _is_close_number(entry.get("input_duration"), input_duration)
                and result_contracts.is_valid_funasr_result(entry.get("result"))):
            payload["chunks"][str(index)] = entry
    return checkpoint_path, payload


def write_funasr_checkpoint(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        replace_file_atomically(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _existing_srt_is_reusable(srt_path, checkpoint_path):
    """只复用结构完整且未被失败 ASR 检查点标记为残缺的正式字幕。"""

    entries = _read_srt_entries(srt_path)
    if not entries:
        return False
    try:
        with open(checkpoint_path, encoding="utf-8") as handle:
            checkpoint = json.load(handle)
    except (OSError, ValueError, TypeError):
        # 没有 ASR 检查点时视为用户提供的完整 SRT，保持旧入口兼容。
        return not os.path.exists(checkpoint_path)
    if not isinstance(checkpoint, dict):
        return False

    if checkpoint.get("status") != "completed":
        # 用户可在失败后自行提供字幕；只有比失败检查点更新的文件才可复用。
        try:
            return os.stat(srt_path).st_mtime_ns > os.stat(checkpoint_path).st_mtime_ns
        except OSError:
            return False

    chunks = checkpoint.get("chunks")
    chunk_count = checkpoint.get("chunk_count")
    coverage = checkpoint.get("coverage")
    duration = checkpoint.get("duration")
    try:
        complete_chunks = (
            isinstance(chunks, dict)
            and int(chunk_count) > 0
            and len(chunks) == int(chunk_count)
            and all(
                result_contracts.is_valid_funasr_result(
                    chunks[str(index)].get("result")
                )
                for index in range(int(chunk_count))
            )
        )
        complete_coverage = (
            isinstance(coverage, dict)
            and _is_close_number(coverage.get("start"), 0.0)
            and _is_close_number(coverage.get("end"), float(duration))
        )
        expected_segments = int(checkpoint.get("segment_count"))
    except (KeyError, TypeError, ValueError):
        return False
    return (
        complete_chunks
        and complete_coverage
        and expected_segments == len(entries)
    )


def _quarantine_incomplete_srt(srt_path):
    """把失败检查点对应的旧正式字幕移出可复用路径。"""

    quarantine_path = srt_path + ".incomplete"
    replace_file_atomically(srt_path, quarantine_path)
    return quarantine_path


def _dedupe_overlapping_funasr_segments(segments):
    """合并分块边界处“半句 + 完整句”的重叠识别结果。"""
    deduped = []
    for segment in sorted(segments, key=lambda item: (item[0], item[1])):
        if not deduped:
            deduped.append(segment)
            continue
        previous = deduped[-1]
        overlap = min(previous[1], segment[1]) - max(previous[0], segment[0])
        shorter_duration = min(
            max(0.001, previous[1] - previous[0]),
            max(0.001, segment[1] - segment[0]),
        )
        previous_text = re.sub(r'\s+', '', previous[2])
        segment_text = re.sub(r'\s+', '', segment[2])
        contains = (
            previous_text
            and segment_text
            and (previous_text in segment_text or segment_text in previous_text)
        )
        if overlap > 0.1 and overlap / shorter_duration >= 0.6 and contains:
            preferred = segment if len(segment_text) >= len(previous_text) else previous
            deduped[-1] = (
                min(previous[0], segment[0]),
                max(previous[1], segment[1]),
                preferred[2],
            )
            continue
        deduped.append(segment)
    return deduped


def _is_funasr_punctuation(char):
    return unicodedata.category(char).startswith("P") or char in "…"


def _attach_funasr_punctuation_to_tokens(tokens, text):
    """在 token 文本有少量识别差异时，仍把正文标点映射回字级时间戳。"""
    tokens = [str(token) for token in (tokens or [])]
    if not tokens or not isinstance(text, str):
        return tokens

    raw_text = "".join(tokens)
    plain_chars = []
    punctuation = defaultdict(str)
    position = 0
    for char in text:
        if char.isspace():
            continue
        if _is_funasr_punctuation(char):
            punctuation[position] += char
        else:
            plain_chars.append(char)
            position += 1
    if not punctuation:
        return tokens

    target_text = "".join(plain_chars)
    matcher = difflib.SequenceMatcher(None, raw_text, target_text, autojunk=False)
    target_to_source = {}
    for tag, source_start, source_end, target_start, target_end in matcher.get_opcodes():
        source_len = max(0, source_end - source_start)
        target_len = max(0, target_end - target_start)
        if tag == "equal":
            for offset in range(target_len):
                target_to_source[target_start + offset] = source_start + offset
        elif tag in {"replace", "insert"}:
            for offset in range(target_len):
                target_to_source[target_start + offset] = source_start + min(
                    offset, max(0, source_len - 1)
                )

    boundaries = []
    boundary = 0
    for token in tokens:
        boundary += len(token)
        boundaries.append(boundary)
    result = list(tokens)
    for target_position, marks in punctuation.items():
        source_position = target_to_source.get(target_position)
        if source_position is None:
            known = [pos for pos in target_to_source if pos <= target_position]
            source_position = target_to_source[max(known)] if known else 0
        if source_position <= 0:
            result[0] = marks + result[0]
            continue
        token_index = bisect.bisect_left(boundaries, source_position)
        token_index = min(token_index, len(result) - 1)
        result[token_index] += marks
    return result


def _align_funasr_tokens(text, timestamps, raw_text=None):
    """把标点模型新增的字符挂回原始 ASR token，保持时间戳数量一致。"""
    timestamps = [
        item for item in (timestamps or [])
        if isinstance(item, (list, tuple)) and len(item) == 2
    ]
    if not timestamps:
        return [], False

    source_text = raw_text if isinstance(raw_text, str) and raw_text.strip() else text
    tokens = str(source_text).strip().split()
    if len(tokens) != len(timestamps):
        compact_source = re.sub(r"\s+", "", str(source_text))
        if len(compact_source) != len(timestamps):
            return [], False
        tokens = list(compact_source)

    if not isinstance(raw_text, str) or not raw_text.strip():
        return tokens, True

    raw_compact = "".join(tokens)
    punct_by_position = defaultdict(str)
    plain_chars = []
    position = 0
    for char in str(text):
        if char.isspace():
            continue
        if _is_funasr_punctuation(char):
            punct_by_position[position] += char
        else:
            plain_chars.append(char)
            position += 1
    raw_plain = "".join(
        char for char in raw_compact if not _is_funasr_punctuation(char)
    )
    if "".join(plain_chars) != raw_plain:
        # 标点模型偶尔会连同正文一起归一化；此时保留原始 token，至少不破坏时间轴。
        if raw_plain != raw_compact:
            return tokens, True
        return _attach_funasr_punctuation_to_tokens(tokens, text), True

    if raw_plain != raw_compact:
        # Nano 的 timestamps 已经包含原生标点 token，无需重复挂载。
        return tokens, True

    aligned = []
    consumed = 0
    for token in tokens:
        token_end = consumed + len(token)
        prefix = punct_by_position.get(0, "") if consumed == 0 else ""
        suffix = punct_by_position.get(token_end, "")
        aligned.append(prefix + token + suffix)
        consumed = token_end
    return aligned, True


def _trim_funasr_tokens_to_core(
        text, timestamps, input_start, core_start, core_end, raw_text=None):
    """先按字词时间归属主体区间，避免重叠输入在边界生成重复半句。"""
    tokens, aligned = _align_funasr_tokens(text, timestamps, raw_text=raw_text)
    if not aligned:
        return str(text or ""), timestamps, False

    selected_tokens = []
    selected_timestamps = []
    for token, timestamp in zip(tokens, timestamps):
        try:
            midpoint = input_start + (
                float(timestamp[0]) + float(timestamp[1])
            ) / 2000.0
        except (TypeError, ValueError):
            continue
        if core_start <= midpoint < core_end:
            selected_tokens.append(token)
            selected_timestamps.append(timestamp)
    return " ".join(selected_tokens), selected_timestamps, True


def ensure_srt(
        video_path, progress_callback=None, checkpoint_path=None,
        foreground_only=False):
    """确保 SRT 存在；分块检查点可恢复，全部成功后才原子写入正式字幕。"""
    import subprocess as sp
    import uuid

    srt_path = os.path.splitext(video_path)[0] + ".srt"
    srt_temp_path = srt_path + ".tmp"
    checkpoint_path = os.path.abspath(
        checkpoint_path or funasr_checkpoint_path(video_path)
    )
    if os.path.exists(srt_path) and os.path.getsize(srt_path) > 0:
        if _existing_srt_is_reusable(srt_path, checkpoint_path):
            if progress_callback:
                progress_callback("SRT 已存在，跳过转录", 5, 100)
            return srt_path
        _quarantine_incomplete_srt(srt_path)
        if progress_callback:
            progress_callback("检测到残缺正式 SRT，已隔离并继续恢复转录", 5, 100)

    if progress_callback:
        progress_callback("FunASR 转录中...", 5, 100)

    duration = probe_video_duration(video_path)
    if not duration:
        raise RuntimeError("无法读取录播时长，FunASR 转录未启动。")
    streamer_name = infer_streamer_name(video_path)
    hotwords = model_runtime.funasr_hotwords(
        video_path,
        streamer_name=streamer_name,
    )
    chunk_count = max(1, int(math.ceil(duration / FUNASR_CHUNK_SEC)))
    checkpoint_path, checkpoint = _prepare_funasr_checkpoint(
        video_path,
        duration,
        chunk_count,
        checkpoint_path=checkpoint_path,
        hotwords=hotwords,
        foreground_only=foreground_only,
    )
    checkpoint["status"] = "running"
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    checkpoint.pop("last_failure", None)
    write_funasr_checkpoint(checkpoint_path, checkpoint)
    missing_indices = [
        index for index in range(chunk_count)
        if str(index) not in checkpoint["chunks"]
    ]
    if progress_callback and len(missing_indices) < chunk_count:
        progress_callback(
            f"已复用 FunASR 检查点 {chunk_count - len(missing_indices)}/{chunk_count} 块",
            10,
            100,
        )

    wav_path = None
    active_chunk_path = None
    model = None
    current_device = None
    active_chunk_index = None
    try:
        if missing_indices:
            try:
                from funasr import AutoModel
            except ImportError as exc:
                raise RuntimeError("FunASR 未安装，无法生成字幕。") from exc

            wav_path = os.path.splitext(video_path)[0] + f"_asr_{uuid.uuid4().hex[:6]}.wav"
            audio_extract_command = [
                "ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
                "-ar", "16000", "-ac", "1",
            ]
            if foreground_only:
                audio_extract_command.extend([
                    "-af", model_runtime.FUNASR_FOREGROUND_AUDIO_FILTER,
                ])
            audio_extract_command.extend(["-y", wav_path])
            sp.run(
                audio_extract_command,
                check=True,
                stdout=sp.PIPE,
                stderr=sp.DEVNULL,
                encoding="utf-8",
                errors="replace",
            )
            requested_device = model_runtime.resolve_funasr_device()
            if progress_callback:
                progress_callback(f"加载 FunASR 模型({requested_device})...", 10, 100)
            model_load_options = {"foreground_only": True} if foreground_only else {}
            model = model_runtime.load_funasr_model(
                AutoModel,
                progress_callback=progress_callback,
                device=requested_device,
                **model_load_options,
            )
            current_device = getattr(model, "_autoslice_device", requested_device)
            foreground_filter_mode = getattr(
                model,
                "_autoslice_foreground_filter",
                "adaptive_gate" if foreground_only else "off",
            )
            checkpoint["foreground_filter_mode"] = foreground_filter_mode
            if progress_callback and foreground_only:
                progress_callback(
                    (
                        "已启用主要说话人识别，将排除其他说话人与低音量背景声"
                        if foreground_filter_mode == "speaker_diarization"
                        else "未安装 CAM++，已启用基础背景音门限；仍可能保留较响的背景对白"
                    ),
                    12,
                    100,
                )
            generate_kwargs = model_runtime.funasr_generate_kwargs(
                model,
                hotwords=hotwords,
            )

            for index in missing_indices:
                active_chunk_index = index
                start, chunk_duration, input_start, input_duration = (
                    _funasr_chunk_input_window(index, duration)
                )
                if progress_callback:
                    pct = 10 + int((index / chunk_count) * 80)
                    progress_callback(
                        f"转录中 ({index + 1}/{chunk_count})...",
                        pct,
                        100,
                    )

                if chunk_count == 1:
                    active_chunk_path = wav_path
                else:
                    active_chunk_path = (
                        os.path.splitext(video_path)[0] + f"_chunk_{index}.wav"
                    )
                    sp.run(
                        [
                            "ffmpeg", "-y", "-ss", str(input_start), "-i", wav_path,
                            "-t", str(input_duration), "-acodec", "pcm_s16le",
                            "-ar", "16000", "-ac", "1", active_chunk_path,
                        ],
                        check=True,
                        stdout=sp.PIPE,
                        stderr=sp.DEVNULL,
                        encoding="utf-8",
                        errors="replace",
                    )

                try:
                    result = model.generate(
                        input=active_chunk_path,
                        **generate_kwargs,
                    )
                except Exception as first_error:
                    if str(current_device).startswith("cuda"):
                        if progress_callback:
                            progress_callback(
                                f"第 {index + 1} 块 GPU 转录失败，改用 CPU 重试: {first_error}",
                                10 + int((index / chunk_count) * 80),
                                100,
                            )
                        model = None
                        model_runtime.clear_funasr_cuda_cache()
                        model = model_runtime.load_funasr_model(
                            AutoModel,
                            progress_callback=progress_callback,
                            device="cpu",
                            **model_load_options,
                        )
                        current_device = "cpu"
                        generate_kwargs = model_runtime.funasr_generate_kwargs(
                            model,
                            hotwords=hotwords,
                        )
                    else:
                        if model_runtime.FUNASR_CPU_RETRY_DELAY_SEC:
                            time.sleep(model_runtime.FUNASR_CPU_RETRY_DELAY_SEC)
                    try:
                        result = model.generate(
                            input=active_chunk_path,
                            **generate_kwargs,
                        )
                    except Exception as retry_error:
                        raise RuntimeError(
                            f"FunASR 第 {index + 1}/{chunk_count} 块连续失败，"
                            "已保留此前检查点，未生成残缺 SRT。"
                        ) from retry_error

                normalised_result = result_contracts.normalise_funasr_result(result)
                chunk_fingerprint = _funasr_chunk_fingerprint(
                    checkpoint["source_fingerprint"],
                    index,
                    start,
                    chunk_duration,
                )
                checkpoint["chunks"][str(index)] = {
                    "fingerprint": chunk_fingerprint,
                    "start": start,
                    "duration": chunk_duration,
                    "input_start": input_start,
                    "input_duration": input_duration,
                    "result": normalised_result,
                    "completed_at": datetime.now().isoformat(timespec="seconds"),
                }
                checkpoint["completed_chunk_count"] = len(checkpoint["chunks"])
                checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
                write_funasr_checkpoint(checkpoint_path, checkpoint)
                if active_chunk_path != wav_path and os.path.exists(active_chunk_path):
                    os.remove(active_chunk_path)
                active_chunk_path = None
                active_chunk_index = None

        all_segments = []
        speaker_filtered_count = 0
        speaker_filtered_chunks = 0
        for index in range(chunk_count):
            entry = checkpoint["chunks"].get(str(index))
            if not entry:
                raise RuntimeError(
                    f"FunASR 第 {index + 1}/{chunk_count} 块缺失，未生成残缺 SRT。"
                )
            start, chunk_duration, input_start, _ = _funasr_chunk_input_window(
                index, duration
            )
            core_end = start + chunk_duration
            result_items = entry.get("result") or []
            primary_segments, removed_count, _ = (
                result_contracts.primary_speaker_segments(result_items)
                if foreground_only
                else (None, 0, None)
            )
            if primary_segments:
                speaker_filtered_count += removed_count
                speaker_filtered_chunks += 1
                for speaker_segment in primary_segments:
                    text_value = str(speaker_segment.get("text", "")).strip()
                    timestamps = speaker_segment.get("timestamp") or []
                    if timestamps:
                        chunk_segments = _segments_from_funasr_result(
                            text_value,
                            timestamps,
                            offset=input_start,
                            streamer_name=streamer_name,
                        )
                    else:
                        try:
                            sentence_start = input_start + float(
                                speaker_segment.get("start")
                            ) / 1000.0
                            sentence_end = input_start + float(
                                speaker_segment.get("end")
                            ) / 1000.0
                        except (TypeError, ValueError):
                            continue
                        chunk_segments = _split_timed_subtitle_segment(
                            sentence_start,
                            max(sentence_end, sentence_start + 0.1),
                            _normalise_asr_text(
                                text_value,
                                streamer_name=streamer_name,
                            ),
                        )
                    for segment in chunk_segments:
                        segment_midpoint = (segment[0] + segment[1]) / 2.0
                        if segment_midpoint < start or segment_midpoint >= core_end:
                            continue
                        bounded_start = max(0.0, start, segment[0])
                        bounded_end = min(duration, core_end, segment[1])
                        if bounded_end > bounded_start:
                            all_segments.append(
                                (bounded_start, bounded_end, segment[2])
                            )
                continue

            for item in result_items:
                text_value = str(item.get("text", "")).strip()
                raw_text_value = item.get("raw_text")
                timestamps = item.get("timestamp", [])
                if text_value and timestamps:
                    core_text, core_timestamps, token_aligned = (
                        _trim_funasr_tokens_to_core(
                            text_value,
                            timestamps,
                            input_start,
                            start,
                            core_end,
                            raw_text=raw_text_value,
                        )
                    )
                    if not core_text or not core_timestamps:
                        continue
                    chunk_segments = _segments_from_funasr_result(
                        core_text,
                        core_timestamps,
                        offset=input_start,
                        streamer_name=streamer_name,
                    )
                    for segment in chunk_segments:
                        segment_midpoint = (segment[0] + segment[1]) / 2.0
                        if (
                                not token_aligned
                                and (segment_midpoint < start or segment_midpoint >= core_end)):
                            continue
                        bounded_start = max(0.0, start, segment[0])
                        bounded_end = min(duration, core_end, segment[1])
                        if bounded_end > bounded_start:
                            all_segments.append(
                                (bounded_start, bounded_end, segment[2])
                            )

        if not all_segments:
            checkpoint["status"] = "completed_empty"
            checkpoint["segment_count"] = 0
            checkpoint["completed_at"] = datetime.now().isoformat(timespec="seconds")
            write_funasr_checkpoint(checkpoint_path, checkpoint)
            if progress_callback:
                progress_callback("未识别到有效语音，未生成空 SRT", 0, 100)
            return None

        all_segments = _dedupe_overlapping_funasr_segments(all_segments)
        checkpoint["speaker_filtered_segment_count"] = speaker_filtered_count
        checkpoint["speaker_filtered_chunk_count"] = speaker_filtered_chunks
        written_count = 0
        with open(srt_temp_path, "w", encoding="utf-8") as handle:
            for start, end, text_value in all_segments:
                if len(text_value) < 2:
                    continue
                written_count += 1
                handle.write(
                    f"{written_count}\n{srt_time(start)} --> {srt_time(end)}\n"
                    f"{text_value}\n\n"
                )
        if not written_count:
            os.remove(srt_temp_path)
            checkpoint["status"] = "completed_empty"
            checkpoint["segment_count"] = 0
            checkpoint["completed_at"] = datetime.now().isoformat(timespec="seconds")
            write_funasr_checkpoint(checkpoint_path, checkpoint)
            return None
        replace_file_atomically(srt_temp_path, srt_path)
        checkpoint["status"] = "completed"
        checkpoint["segment_count"] = written_count
        checkpoint["coverage"] = {"start": 0.0, "end": float(duration)}
        checkpoint["completed_at"] = datetime.now().isoformat(timespec="seconds")
        write_funasr_checkpoint(checkpoint_path, checkpoint)
        if progress_callback:
            progress_callback(f"转录完成 ({written_count} 条)", 90, 100)
        return srt_path
    except Exception as exc:
        checkpoint["status"] = "failed"
        checkpoint["last_failure"] = {
            "chunk_index": active_chunk_index,
            "message": str(exc),
            "failed_at": datetime.now().isoformat(timespec="seconds"),
        }
        checkpoint["completed_chunk_count"] = len(checkpoint.get("chunks") or {})
        try:
            write_funasr_checkpoint(checkpoint_path, checkpoint)
        except OSError:
            pass
        raise
    finally:
        if active_chunk_path and active_chunk_path != wav_path and os.path.exists(active_chunk_path):
            os.remove(active_chunk_path)
        if wav_path and os.path.exists(wav_path):
            os.remove(wav_path)
        if os.path.exists(srt_temp_path):
            os.remove(srt_temp_path)


def srt_time(s):
    h, m = divmod(int(s), 3600)
    m, sec = divmod(m, 60)
    ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def parse_srt_timestamp(value):
    """解析 SRT 时间戳，返回视频内秒数。"""
    h, m, rest = value.strip().split(":")
    s, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
