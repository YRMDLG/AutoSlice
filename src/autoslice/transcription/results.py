"""FunASR 原始结果规范化、结构校验与主要说话人选择的唯一实现。"""

from __future__ import annotations

import math
import re
from collections import defaultdict

FACADE_EXPORTS = {
    "_is_valid_funasr_result": "is_valid_funasr_result",
    "_normalise_funasr_result": "normalise_funasr_result",
    "_primary_speaker_segments": "primary_speaker_segments",
}


def normalise_funasr_result(result):
    """只保存恢复 SRT 所需字段，并把 numpy 标量转成 JSON 基础类型。"""
    normalised = []
    for item in result or []:
        if not isinstance(item, dict):
            continue
        # Fun-ASR-Nano 同时返回 LLM 文本和 CTC 字级对齐结果。Nano 的
        # LLM 文本在部分硬件/精度组合下可能是占位符，但 CTC 文本与
        # ctc_timestamps 仍然可用；统一转换为项目内部的毫秒时间戳契约。
        llm_text = str(item.get("text", ""))
        llm_timestamps = item.get("timestamps")
        ctc_text = item.get("ctc_text")
        ctc_timestamps = item.get("ctc_timestamps")
        llm_text_is_meaningful = bool(
            re.search(r"[\w\u3400-\u9fff]", llm_text, flags=re.UNICODE)
        )
        use_nano_llm = llm_text_is_meaningful and bool(llm_timestamps)
        use_nano_ctc = (
            not use_nano_llm
            and isinstance(ctc_text, str)
            and bool(ctc_timestamps)
        )
        if use_nano_llm:
            text_value = llm_text
            source_timestamps = llm_timestamps
        elif use_nano_ctc:
            text_value = ctc_text
            source_timestamps = ctc_timestamps
        else:
            text_value = llm_text
            source_timestamps = item.get("timestamp", [])
        nano_timestamp_tokens = []
        timestamps = []
        for pair in source_timestamps or []:
            if isinstance(pair, dict):
                token = str(pair.get("token", "")).strip()
                if not token:
                    # Nano 会为部分停顿返回纯空白 token。空白没有字幕内容，
                    # 保留其时间戳反而会破坏 token 与时间戳的一一对应关系。
                    continue
                values_source = (pair.get("start_time"), pair.get("end_time"))
                nano_timestamp_tokens.append(token)
            elif isinstance(pair, (list, tuple)) and len(pair) >= 2:
                values_source = pair[:2]
            else:
                continue
            values = []
            for value in values_source:
                if hasattr(value, "item"):
                    value = value.item()
                if value is not None and not isinstance(value, (int, float)):
                    value = float(value)
                # Nano 的 ctc_timestamps 单位是秒；旧 FunASR timestamp
                # 单位是毫秒。内部始终使用毫秒，避免字幕整体缩短 1000 倍。
                if (use_nano_llm or use_nano_ctc) and isinstance(
                    value, (int, float)
                ):
                    value *= 1000.0
                values.append(value)
            timestamps.append(values)
        entry = {
            "text": str(text_value),
            "timestamp": timestamps,
        }
        raw_text = item.get("raw_text")
        if (
            (use_nano_llm or use_nano_ctc)
            and nano_timestamp_tokens
            and len(nano_timestamp_tokens) == len(timestamps)
        ):
            # Nano 的 token 字段与时间戳一一对应。保留带空格的 token 序列，
            # 让后续标点对齐既能使用 LLM 校正后的正文，又不会新增时间 token。
            raw_text = " ".join(nano_timestamp_tokens)
        if isinstance(raw_text, str):
            entry["raw_text"] = raw_text
        speaker_segments = []
        for sentence in item.get("sentence_info") or []:
            if not isinstance(sentence, dict):
                continue
            sentence_text = str(
                sentence.get("sentence", sentence.get("text", "")) or ""
            ).strip()
            speaker = sentence.get("spk")
            if hasattr(speaker, "item"):
                speaker = speaker.item()
            try:
                sentence_start = float(sentence.get("start"))
                sentence_end = float(sentence.get("end"))
            except (TypeError, ValueError):
                continue
            if (
                not sentence_text
                or speaker is None
                or not math.isfinite(sentence_start)
                or not math.isfinite(sentence_end)
                or sentence_end <= sentence_start
            ):
                continue
            sentence_timestamps = []
            for pair in sentence.get("timestamp") or []:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                values = []
                for value in pair[:2]:
                    if hasattr(value, "item"):
                        value = value.item()
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        value = None
                    values.append(value)
                if all(
                    value is not None and math.isfinite(value)
                    for value in values
                ):
                    sentence_timestamps.append(values)
            speaker_segments.append(
                {
                    "speaker": str(speaker),
                    "start": sentence_start,
                    "end": sentence_end,
                    "text": sentence_text,
                    "timestamp": sentence_timestamps,
                }
            )
        if speaker_segments:
            entry["speaker_segments"] = speaker_segments
        normalised.append(entry)
    return normalised


def is_valid_funasr_result(result):
    if not isinstance(result, list):
        return False
    for item in result:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("text"), str)
            or not isinstance(item.get("timestamp"), list)
        ):
            return False
        if "raw_text" in item and not isinstance(item.get("raw_text"), str):
            return False
        speaker_segments = item.get("speaker_segments", [])
        if not isinstance(speaker_segments, list):
            return False
        for segment in speaker_segments:
            if (
                not isinstance(segment, dict)
                or not isinstance(segment.get("speaker"), str)
                or not isinstance(segment.get("text"), str)
                or not isinstance(segment.get("timestamp", []), list)
                or not isinstance(segment.get("start"), (int, float))
                or not isinstance(segment.get("end"), (int, float))
            ):
                return False
        for pair in item["timestamp"]:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or any(
                    value is not None and not isinstance(value, (int, float))
                    for value in pair
                )
            ):
                return False
    return True


def primary_speaker_segments(result):
    """从一个识别块中保留持续时间和发言次数占优的主要说话人。"""
    segments = []
    stats = defaultdict(lambda: {"duration": 0.0, "count": 0, "first": math.inf})
    for item in result or []:
        if not isinstance(item, dict):
            continue
        for segment in item.get("speaker_segments") or []:
            if not isinstance(segment, dict):
                continue
            speaker = str(segment.get("speaker", "")).strip()
            try:
                start = float(segment.get("start"))
                end = float(segment.get("end"))
            except (TypeError, ValueError):
                continue
            if (
                not speaker
                or not math.isfinite(start)
                or not math.isfinite(end)
                or end <= start
            ):
                continue
            segments.append(segment)
            stat = stats[speaker]
            stat["duration"] += max(0.0, end - start) / 1000.0
            stat["count"] += 1
            stat["first"] = min(stat["first"], start)
    if len(stats) < 2:
        return None, 0, None

    primary = max(
        stats,
        key=lambda speaker: (
            stats[speaker]["duration"] + stats[speaker]["count"] * 0.75,
            stats[speaker]["count"],
            -stats[speaker]["first"],
        ),
    )
    selected = [
        segment
        for segment in segments
        if str(segment.get("speaker", "")).strip() == primary
    ]
    removed_count = len(segments) - len(selected)
    if not selected or removed_count <= 0:
        return None, 0, None
    return selected, removed_count, primary
