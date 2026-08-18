"""SRT 读取、旧字幕修复、序列化与校对导出的唯一实现。"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

from autoslice.streamer_profiles import infer_streamer_name
from autoslice.transcription import checkpoints as checkpoint_store
from autoslice.transcription import segments as subtitle_segments

FACADE_EXPORTS = {
    "_read_srt_entries": "read_srt_entries",
    "_load_repaired_srt_segments": "load_repaired_srt_segments",
    "export_corrected_srt": "export_corrected_srt",
}


def read_srt_entries(srt_path):
    """读取原始 SRT 条目，不提前修正时间，供异常结构识别使用。"""
    resolved_path = os.fspath(srt_path) if srt_path else ""
    if not resolved_path or not os.path.exists(resolved_path):
        return []
    with open(resolved_path, encoding="utf-8") as handle:
        content = handle.read()
    pattern = (
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"
        r"(.*?)(?=\n\n|\Z)"
    )
    entries = []
    for start_text, end_text, text in re.findall(pattern, content, re.DOTALL):
        clean_text = text.strip().replace("\n", " ").strip()
        if not clean_text:
            continue
        entries.append(
            (
                subtitle_segments.parse_srt_timestamp(start_text),
                subtitle_segments.parse_srt_timestamp(end_text),
                clean_text,
            )
        )
    return entries


def load_repaired_srt_segments(srt_path):
    """加载健康 SRT，并还原旧版 FunASR 的全文逐字重复异常。"""
    entries = read_srt_entries(srt_path)
    if not entries:
        return []
    streamer_name = infer_streamer_name(os.fspath(srt_path))
    repaired_segments = []
    index = 0
    while index < len(entries):
        raw_text = entries[index][2]
        group_end = index + 1
        while group_end < len(entries) and entries[group_end][2] == raw_text:
            group_end += 1
        group = entries[index:group_end]
        tokens = raw_text.split()
        is_repeated_funasr_block = (
            len(group) >= subtitle_segments.SRT_REPEAT_REPAIR_MIN_ENTRIES
            and len(tokens) == len(group)
            and subtitle_segments.subtitle_text_size(raw_text) >= 20
        )
        if is_repeated_funasr_block:
            timed_tokens = [
                (entry[0], entry[1], token)
                for entry, token in zip(group, tokens)
            ]
            repaired_segments.extend(
                subtitle_segments.segment_timed_tokens(
                    timed_tokens,
                    streamer_name=streamer_name,
                    max_chars=subtitle_segments.SUBTITLE_LEGACY_REPAIR_MAX_CHARS,
                )
            )
        else:
            for start_s, end_s, text in group:
                clean_text = subtitle_segments.normalise_asr_text(
                    text,
                    streamer_name=streamer_name,
                )
                if not clean_text:
                    continue
                repaired_end = subtitle_segments.repair_srt_end_time(
                    start_s,
                    end_s,
                    clean_text,
                )
                if (
                    repaired_segments
                    and clean_text == repaired_segments[-1][2]
                    and start_s - repaired_segments[-1][1]
                    <= subtitle_segments.TOPIC_CONTEXT_GAP
                ):
                    repaired_segments[-1] = (
                        repaired_segments[-1][0],
                        max(repaired_segments[-1][1], repaired_end),
                        clean_text,
                    )
                else:
                    repaired_segments.append((start_s, repaired_end, clean_text))
        index = group_end
    return sorted(repaired_segments, key=lambda item: (item[0], item[1]))


def write_srt_segments(
    output_path,
    segments: Iterable[tuple[float, float, str]],
    *,
    minimum_text_chars: int = 0,
    minimum_duration: float = 0.0,
) -> int:
    """把内部字幕段写成标准 SRT，并返回实际写入条数。"""
    resolved_path = os.path.abspath(os.fspath(output_path))
    os.makedirs(os.path.dirname(resolved_path), exist_ok=True)
    written_count = 0
    with open(resolved_path, "w", encoding="utf-8") as handle:
        for start_s, end_s, text in segments:
            text_value = str(text or "")
            if len(text_value) < max(0, int(minimum_text_chars)):
                continue
            start_value = float(start_s)
            end_value = max(float(end_s), start_value + float(minimum_duration))
            written_count += 1
            handle.write(
                f"{written_count}\n{subtitle_segments.srt_time(start_value)} --> "
                f"{subtitle_segments.srt_time(end_value)}\n"
                f"{text_value}\n\n"
            )
    return written_count


def export_corrected_srt(source_srt_path, output_path=None):
    """原子生成可导入剪映的校对版，不覆盖原始 SRT。"""
    repaired_segments = load_repaired_srt_segments(source_srt_path)
    if not repaired_segments:
        return None
    source_path = os.fspath(source_srt_path)
    resolved_path = os.path.abspath(
        os.fspath(output_path)
        if output_path is not None
        else os.path.splitext(source_path)[0] + "_校对字幕.srt"
    )
    temporary_path = resolved_path + ".tmp"
    try:
        write_srt_segments(
            temporary_path,
            repaired_segments,
            minimum_duration=0.1,
        )
        checkpoint_store.commit_file_atomically(temporary_path, resolved_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
    return resolved_path


__all__ = [
    "FACADE_EXPORTS",
    "export_corrected_srt",
    "load_repaired_srt_segments",
    "read_srt_entries",
    "write_srt_segments",
]
