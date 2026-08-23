"""ASR 文本规范化、字幕分段与时间戳对齐的唯一实现。"""

from __future__ import annotations

import bisect
import difflib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum, auto

from autoslice.streamer_profiles import (
    current_streamer_profile,
    profile_matches_streamer,
)
from autoslice.transcription.contracts import DEFAULT_SUBTITLE_MAX_CHARS

TOPIC_CONTEXT_GAP = 4.0
SRT_ABNORMAL_CHARS_PER_SEC = 18
SRT_ESTIMATED_CHARS_PER_SEC = 7
SRT_MAX_ESTIMATED_SEG_SEC = 300
SRT_REPEAT_REPAIR_MIN_ENTRIES = 8
SUBTITLE_TARGET_CHARS = 10
SUBTITLE_MAX_CHARS = DEFAULT_SUBTITLE_MAX_CHARS
SUBTITLE_LEGACY_REPAIR_MAX_CHARS = 28
SUBTITLE_MAX_DURATION_SEC = 7.0
SUBTITLE_PAUSE_BREAK_SEC = 0.65

NATURAL_SENTENCE_CONNECTORS = (
    "所以",
    "但是",
    "然后",
    "不过",
    "其实",
    "反正",
    "因为",
    "如果",
)
_CLAUSE_TAIL_PREFIXES = (
    "越来越",
    "解释得",
    "说得",
    "变得",
    "已经",
    "正在",
    "还是",
    "真的",
    "感觉",
    "应该",
    "可能",
    "不会",
    "不要",
    "想要",
)
_SENTENCE_END_PUNCTUATION = frozenset("。！？!?；;")
_CLAUSE_PUNCTUATION = frozenset("，,、")


class SubtitleBoundaryReason(Enum):
    """结构化记录字幕边界的实际触发来源。"""

    TARGET_GAP = auto()
    PAUSE = auto()
    SENTENCE_PUNCTUATION = auto()
    CLAUSE_PUNCTUATION = auto()
    TARGET_DURATION = auto()
    MAX_DURATION = auto()
    MAX_CHARS = auto()
    ALIGNED_TEXT_LIMIT = auto()
    UNALIGNED_TEXT_LIMIT = auto()
    END_OF_INPUT = auto()


@dataclass(frozen=True)
class SubtitleBoundaryDecision:
    """一条输出字幕之后的边界诊断，不进入旧 SRT/API 返回契约。"""

    segment_index: int
    reason: SubtitleBoundaryReason
    at_seconds: float
    source_token_index: int | None
    gap_seconds: float
    duration_seconds: float
    visible_chars: int


@dataclass(frozen=True)
class SubtitleSegmentationTrace:
    """测试和诊断使用的分段结果；旧入口仍只返回三元组列表。"""

    segments: tuple[tuple[float, float, str], ...]
    boundaries: tuple[SubtitleBoundaryDecision, ...]


FACADE_EXPORTS = {
    "TOPIC_CONTEXT_GAP": "TOPIC_CONTEXT_GAP",
    "SRT_ABNORMAL_CHARS_PER_SEC": "SRT_ABNORMAL_CHARS_PER_SEC",
    "SRT_ESTIMATED_CHARS_PER_SEC": "SRT_ESTIMATED_CHARS_PER_SEC",
    "SRT_MAX_ESTIMATED_SEG_SEC": "SRT_MAX_ESTIMATED_SEG_SEC",
    "SRT_REPEAT_REPAIR_MIN_ENTRIES": "SRT_REPEAT_REPAIR_MIN_ENTRIES",
    "SUBTITLE_TARGET_CHARS": "SUBTITLE_TARGET_CHARS",
    "SUBTITLE_MAX_CHARS": "SUBTITLE_MAX_CHARS",
    "SUBTITLE_LEGACY_REPAIR_MAX_CHARS": "SUBTITLE_LEGACY_REPAIR_MAX_CHARS",
    "SUBTITLE_MAX_DURATION_SEC": "SUBTITLE_MAX_DURATION_SEC",
    "SUBTITLE_PAUSE_BREAK_SEC": "SUBTITLE_PAUSE_BREAK_SEC",
    "_text_len_for_timing": "text_len_for_timing",
    "_repair_srt_end_time": "repair_srt_end_time",
    "_join_asr_tokens": "join_asr_tokens",
    "_strip_asr_subtitle_punctuation": "strip_asr_subtitle_punctuation",
    "_normalise_asr_text": "normalise_asr_text",
    "_normalise_streamer_terms": "normalise_streamer_terms",
    "_subtitle_text_size": "subtitle_text_size",
    "_split_subtitle_text_for_display": "split_subtitle_text_for_display",
    "_split_timed_subtitle_segment": "split_timed_subtitle_segment",
    "_should_hold_subtitle_for_short_clause": "should_hold_subtitle_for_short_clause",
    "_segment_timed_tokens": "segment_timed_tokens",
    "_segments_from_funasr_result": "segments_from_funasr_result",
    "_dedupe_overlapping_funasr_segments": "dedupe_overlapping_funasr_segments",
    "_is_funasr_punctuation": "is_funasr_punctuation",
    "_attach_funasr_punctuation_to_tokens": "attach_funasr_punctuation_to_tokens",
    "_align_funasr_tokens": "align_funasr_tokens",
    "_trim_funasr_tokens_to_core": "trim_funasr_tokens_to_core",
    "_srt_time": "srt_time",
    "_parse_srt_timestamp": "parse_srt_timestamp",
    "_srt_video_duration": "srt_video_duration",
}


def text_len_for_timing(text):
    """估算语速用长度：去掉空白，保留中文、数字和字母。"""
    return len(re.sub(r"\s+", "", text or ""))


def repair_srt_end_time(start_s, end_s, text):
    """修复 FunASR 偶发的“几百字压到零点几秒”时间戳。"""
    duration = max(0.001, end_s - start_s)
    text_len = text_len_for_timing(text)
    if text_len < 80:
        return end_s
    if text_len / duration <= SRT_ABNORMAL_CHARS_PER_SEC:
        return end_s
    estimated = min(
        SRT_MAX_ESTIMATED_SEG_SEC,
        max(duration, text_len / SRT_ESTIMATED_CHARS_PER_SEC),
    )
    return start_s + estimated


def join_asr_tokens(tokens):
    """拼接 FunASR 字词 token；中文不加空格，连续英文词保留分隔。"""
    result = ""
    for token in (str(item).strip() for item in tokens):
        if not token:
            continue
        if (
            result
            and re.search(r"[A-Za-z0-9]$", result)
            and re.match(r"^[A-Za-z0-9]", token)
        ):
            result += " "
        result += token
    return result.strip()


def strip_asr_subtitle_punctuation(text):
    """按剪辑习惯移除 ASR 标点，逗号类保留为单个分隔空格。"""
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


def normalise_asr_text(text, streamer_name="主播"):
    """清理 ASR 分词空格，并应用当前主播配置的低歧义专名纠错。"""
    if isinstance(text, (list, tuple)):
        tokens = text
    else:
        tokens = re.split(r"\s+", str(text or "").replace("\n", " ").strip())
    clean = join_asr_tokens(tokens)
    clean = normalise_streamer_terms(clean, streamer_name=streamer_name)
    return strip_asr_subtitle_punctuation(clean)


def normalise_streamer_terms(text, streamer_name="主播"):
    """统一字幕和 AI 报告中的主播及粉丝专名，不改动其它排版。"""
    clean = str(text or "")
    profile = current_streamer_profile()
    if not profile_matches_streamer(profile, streamer_name):
        return clean
    for source, target in profile.asr_replacements:
        clean = clean.replace(source, target)
    return clean


def subtitle_text_size(text):
    return len(re.sub(r"\s+", "", text or ""))


def split_subtitle_text_for_display(text, max_chars=SUBTITLE_MAX_CHARS):
    """按可读长度拆分字幕正文，优先在空格处断开，必要时按字硬切。"""
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        max_chars = SUBTITLE_MAX_CHARS
    remaining = re.sub(r"\s+", " ", str(text or "")).strip()
    parts = []
    while subtitle_text_size(remaining) > max_chars:
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
            and subtitle_text_size(remaining[:preferred_cut])
            >= max(2, int(max_chars * 0.55))
        ):
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


def split_timed_subtitle_segment(
    start_s,
    end_s,
    text,
    max_chars=SUBTITLE_MAX_CHARS,
):
    """将过长字幕按正文比例分配为连续时间段。"""
    parts = split_subtitle_text_for_display(text, max_chars=max_chars)
    if not parts:
        return []
    start_s = float(start_s)
    end_s = float(end_s)
    if end_s <= start_s:
        end_s = start_s + 0.1
    if len(parts) == 1:
        return [(start_s, end_s, parts[0])]

    weights = [max(1, subtitle_text_size(part)) for part in parts]
    total_weight = sum(weights)
    duration = end_s - start_s
    minimum_duration = min(0.05, duration / len(parts))
    cursor = start_s
    segments = []
    elapsed_weight = 0
    for index, (part, weight) in enumerate(zip(parts, weights)):
        elapsed_weight += weight
        if index == len(parts) - 1:
            next_cursor = end_s
        else:
            ideal_cursor = start_s + duration * elapsed_weight / total_weight
            remaining_parts = len(parts) - index - 1
            earliest_cursor = cursor + minimum_duration
            latest_cursor = end_s - minimum_duration * remaining_parts
            next_cursor = min(latest_cursor, max(earliest_cursor, ideal_cursor))
        segments.append((cursor, next_cursor, part))
        cursor = next_cursor
    return segments


def should_hold_subtitle_for_short_clause(
    timed_tokens,
    index,
    current_chars,
    max_chars,
):
    """句末只剩一两个字时延后软截断，避免短语尾巴落到下一条。"""
    if current_chars >= max_chars:
        return False
    trailing_chars = 0
    for _start_s, _end_s, future_token in timed_tokens[index + 1 :]:
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


class _SubtitleSegmentationPlanner:
    """在单一 owner 内规划字词边界、回退切分和 trace。"""

    def __init__(self, streamer_name, max_chars):
        self.streamer_name = streamer_name
        try:
            self.max_chars = max(1, int(max_chars))
        except (TypeError, ValueError):
            self.max_chars = SUBTITLE_MAX_CHARS
        self.tokens = []
        self.current = []
        self.traced_segments = []
        self.carried_clause_tail = False

    def segment(self, timed_tokens):
        self.tokens = self._normalise_tokens(timed_tokens)
        self.current = []
        self.traced_segments = []
        self.carried_clause_tail = False
        if not self.tokens:
            return SubtitleSegmentationTrace(segments=(), boundaries=())
        for index, token_data in enumerate(self.tokens):
            self._consume_token(index, token_data)
        self._emit(SubtitleBoundaryReason.END_OF_INPUT)
        return self._build_trace(self._polish())

    @staticmethod
    def _normalise_tokens(timed_tokens):
        normalised = []
        for source_index, item in enumerate(timed_tokens or []):
            try:
                start_s, end_s, token = item
                start_s = float(start_s)
                end_s = float(end_s)
            except (TypeError, ValueError):
                continue
            token = str(token).strip()
            if token:
                normalised.append((start_s, end_s, token, source_index))
        return normalised

    @staticmethod
    def _plain_token_text(token):
        return "".join(
            char
            for char in str(token or "")
            if not char.isspace()
            and not unicodedata.category(char).startswith("P")
            and char != "…"
        )

    @classmethod
    def _token_visible_size(cls, token):
        return len(cls._plain_token_text(token))

    def _future_plain_text(self, start_index, max_visible_chars=8):
        result = []
        for item in self.tokens[start_index:]:
            result.extend(self._plain_token_text(item[2]))
            if len(result) >= max_visible_chars:
                break
        return "".join(result[:max_visible_chars])

    @staticmethod
    def _starts_with_connector(text):
        compact = re.sub(r"\s+", "", str(text or ""))
        return any(
            compact.startswith(connector)
            for connector in NATURAL_SENTENCE_CONNECTORS
        )

    def _future_starts_connector(self, start_index):
        future = self._future_plain_text(
            start_index,
            max_visible_chars=max(map(len, NATURAL_SENTENCE_CONNECTORS)),
        )
        return self._starts_with_connector(future)

    def _split_index_for_moved_chars(self, move_chars):
        if move_chars <= 0 or not self.current:
            return None
        collected = 0
        split_index = len(self.current)
        for item_index in range(len(self.current) - 1, -1, -1):
            collected += self._token_visible_size(self.current[item_index][2])
            split_index = item_index
            if collected >= move_chars:
                break
        if split_index <= 0:
            return None
        prefix_chars = sum(
            self._token_visible_size(item[2])
            for item in self.current[:split_index]
        )
        return split_index if prefix_chars >= 2 else None

    def _connector_rewind_split_index(self, next_index):
        current_plain = "".join(
            self._plain_token_text(item[2]) for item in self.current
        )
        if not current_plain:
            return None
        future_plain = self._future_plain_text(
            next_index,
            max_visible_chars=max(map(len, NATURAL_SENTENCE_CONNECTORS)),
        )
        for connector in NATURAL_SENTENCE_CONNECTORS:
            for left_size in range(1, len(connector)):
                if (
                    current_plain.endswith(connector[:left_size])
                    and future_plain.startswith(connector[left_size:])
                ):
                    return self._split_index_for_moved_chars(left_size)
            if (
                future_plain
                and current_plain.endswith(connector)
                and len(current_plain) > len(connector)
            ):
                return self._split_index_for_moved_chars(len(connector))
        return None

    def _lookahead_clause_tail(self, start_index, previous_end_s):
        trailing_chars = 0
        cursor_end = float(previous_end_s)
        for start_s, end_s, token, _source_index in self.tokens[start_index:]:
            if float(start_s) - cursor_end > 0.4:
                return None
            for char in str(token or ""):
                if char.isspace():
                    continue
                if (
                    unicodedata.category(char).startswith("P")
                    or char == "…"
                ):
                    return trailing_chars, char
                trailing_chars += 1
                if trailing_chars > 2:
                    return None
            cursor_end = float(end_s)
        return None

    def _clause_tail_rewind_split_index(self, trailing_chars):
        current_plain = "".join(
            self._plain_token_text(item[2]) for item in self.current
        )
        if not current_plain:
            return None
        candidates = []
        search_start = max(2, len(current_plain) - 8)
        for prefix in _CLAUSE_TAIL_PREFIXES:
            position = current_plain.rfind(prefix, search_start)
            if position >= 4:
                tail_size = len(current_plain) - position + trailing_chars
                if 3 <= tail_size <= self.max_chars:
                    candidates.append(position)
        move_chars = (
            len(current_plain) - max(candidates)
            if candidates
            else max(2, 4 - trailing_chars)
        )
        if move_chars + trailing_chars > self.max_chars:
            return None
        return self._split_index_for_moved_chars(move_chars)

    @staticmethod
    def _traced_segment(
        start_s,
        end_s,
        text,
        reason,
        source_token_index,
        gap_seconds,
    ):
        return {
            "start_s": start_s,
            "end_s": end_s,
            "text": text,
            "reason": reason,
            "source_token_index": source_token_index,
            "gap_seconds": gap_seconds,
            "duration_seconds": end_s - start_s,
            "visible_chars": subtitle_text_size(text),
        }

    def _emit(self, reason, *, count=None, gap_seconds=0.0):
        if not self.current or count == 0:
            return
        prefix = self.current if count is None else self.current[:count]
        self.current = [] if count is None else self.current[count:]
        text = normalise_asr_text(
            [item[2] for item in prefix],
            streamer_name=self.streamer_name,
        )
        if not text:
            self._extend_previous(prefix, reason, gap_seconds)
            return
        parts = split_timed_subtitle_segment(
            prefix[0][0],
            prefix[-1][1],
            text,
            max_chars=self.max_chars,
        )
        for part_index, (part_start, part_end, part_text) in enumerate(parts):
            part_reason = (
                reason
                if part_index == len(parts) - 1
                else SubtitleBoundaryReason.ALIGNED_TEXT_LIMIT
            )
            self.traced_segments.append(self._traced_segment(
                part_start,
                part_end,
                part_text,
                part_reason,
                prefix[-1][3],
                gap_seconds if part_index == len(parts) - 1 else 0.0,
            ))
        if not self.current:
            self.carried_clause_tail = False

    def _extend_previous(self, prefix, reason, gap_seconds):
        if not self.traced_segments:
            return
        previous = self.traced_segments[-1]
        self.traced_segments[-1] = self._traced_segment(
            previous["start_s"],
            max(previous["end_s"], prefix[-1][1]),
            previous["text"],
            reason,
            prefix[-1][3],
            gap_seconds,
        )

    def _consume_token(self, index, token_data):
        start_s, end_s, token, source_index = token_data
        self.current.append((start_s, end_s, token, source_index))
        current_chars = sum(
            self._token_visible_size(item[2]) for item in self.current
        )
        duration = self.current[-1][1] - self.current[0][0]
        next_gap = self._next_gap(index, end_s)

        if self._break_for_punctuation(index, token, current_chars, next_gap):
            return
        if next_gap >= SUBTITLE_PAUSE_BREAK_SEC and current_chars >= 2:
            self._emit(
                SubtitleBoundaryReason.PAUSE,
                count=self._connector_rewind_split_index(index + 1),
                gap_seconds=next_gap,
            )
            return
        if current_chars >= self.max_chars:
            self._break_for_max_chars(index, end_s, current_chars, next_gap)
            return
        if duration >= SUBTITLE_MAX_DURATION_SEC:
            self._emit(
                SubtitleBoundaryReason.MAX_DURATION,
                count=self._connector_rewind_split_index(index + 1),
                gap_seconds=next_gap,
            )
            return
        self._break_for_target(index, end_s, current_chars, duration, next_gap)

    def _next_gap(self, index, end_s):
        if index + 1 >= len(self.tokens):
            return 0.0
        return max(0.0, self.tokens[index + 1][0] - end_s)

    def _break_for_punctuation(self, index, token, current_chars, next_gap):
        has_sentence_end = any(
            char in _SENTENCE_END_PUNCTUATION for char in token
        )
        if has_sentence_end and current_chars >= 2:
            self._emit(
                SubtitleBoundaryReason.SENTENCE_PUNCTUATION,
                gap_seconds=next_gap,
            )
            return True
        has_clause_end = any(char in _CLAUSE_PUNCTUATION for char in token)
        if (
            has_clause_end
            and current_chars >= 2
            and (
                self._future_starts_connector(index + 1)
                or self.carried_clause_tail
            )
        ):
            self._emit(
                SubtitleBoundaryReason.CLAUSE_PUNCTUATION,
                gap_seconds=next_gap,
            )
            return True
        return False

    def _break_for_max_chars(self, index, end_s, current_chars, next_gap):
        tail = self._lookahead_clause_tail(index + 1, end_s)
        if current_chars == self.max_chars and tail and tail[0] == 0:
            return
        split_index = self._connector_rewind_split_index(index + 1)
        if split_index is None and tail and 0 < tail[0] <= 2:
            split_index = self._clause_tail_rewind_split_index(tail[0])
            if split_index is not None:
                self._emit(
                    SubtitleBoundaryReason.MAX_CHARS,
                    count=split_index,
                )
                self.carried_clause_tail = True
                return
        self._emit(
            SubtitleBoundaryReason.MAX_CHARS,
            count=split_index,
            gap_seconds=next_gap,
        )

    def _break_for_target(
        self,
        index,
        end_s,
        current_chars,
        duration,
        next_gap,
    ):
        target_reason = None
        if current_chars >= SUBTITLE_TARGET_CHARS and next_gap >= 0.15:
            target_reason = SubtitleBoundaryReason.TARGET_GAP
        elif current_chars >= SUBTITLE_TARGET_CHARS and duration >= 4.5:
            target_reason = SubtitleBoundaryReason.TARGET_DURATION
        if target_reason is None:
            return
        tail = self._lookahead_clause_tail(index + 1, end_s)
        if tail and current_chars + tail[0] <= self.max_chars:
            return
        self._emit(
            target_reason,
            count=self._connector_rewind_split_index(index + 1),
            gap_seconds=next_gap,
        )

    def _polish(self):
        polished = []
        for segment in self.traced_segments:
            if polished and self._can_merge_short_segment(polished[-1], segment):
                previous = polished[-1]
                polished[-1] = self._traced_segment(
                    previous["start_s"],
                    segment["end_s"],
                    previous["text"] + segment["text"],
                    segment["reason"],
                    segment["source_token_index"],
                    segment["gap_seconds"],
                )
                continue
            polished.append(segment)
        return polished

    def _can_merge_short_segment(self, previous, segment):
        combined_text = previous["text"] + segment["text"]
        return (
            segment["end_s"] - segment["start_s"] < 0.5
            and max(0.0, segment["start_s"] - previous["end_s"]) <= 0.4
            and subtitle_text_size(combined_text) <= self.max_chars
            and not self._starts_with_connector(segment["text"])
        )

    @staticmethod
    def _build_trace(items):
        return SubtitleSegmentationTrace(
            segments=tuple(
                (item["start_s"], item["end_s"], item["text"])
                for item in items
            ),
            boundaries=tuple(
                SubtitleBoundaryDecision(
                    segment_index=index,
                    reason=item["reason"],
                    at_seconds=item["end_s"],
                    source_token_index=item["source_token_index"],
                    gap_seconds=item["gap_seconds"],
                    duration_seconds=item["duration_seconds"],
                    visible_chars=item["visible_chars"],
                )
                for index, item in enumerate(items[:-1])
            ),
        )


def segment_timed_tokens_with_trace(
    timed_tokens,
    streamer_name="主播",
    max_chars=SUBTITLE_MAX_CHARS,
):
    """由私有 planner 生成字幕及结构化边界原因。"""
    return _SubtitleSegmentationPlanner(
        streamer_name,
        max_chars,
    ).segment(timed_tokens)


def segment_timed_tokens(
    timed_tokens,
    streamer_name="主播",
    max_chars=SUBTITLE_MAX_CHARS,
):
    """兼容旧调用：返回 ``(start, end, text)`` 三元组列表。"""
    return list(segment_timed_tokens_with_trace(
        timed_tokens,
        streamer_name=streamer_name,
        max_chars=max_chars,
    ).segments)


def segments_from_funasr_result_with_trace(
    text,
    timestamps,
    offset=0.0,
    streamer_name="主播",
    raw_text=None,
    max_chars=SUBTITLE_MAX_CHARS,
):
    """转换 FunASR 结果并暴露对齐/比例拆分的结构化边界原因。"""
    timestamps = [
        item
        for item in (timestamps or [])
        if isinstance(item, (list, tuple)) and len(item) == 2
    ]
    if not text or not timestamps:
        return SubtitleSegmentationTrace(segments=(), boundaries=())
    tokens, aligned = align_funasr_tokens(text, timestamps, raw_text=raw_text)
    if not aligned:
        start_s = offset + float(timestamps[0][0]) / 1000.0
        end_s = offset + float(timestamps[-1][1]) / 1000.0
        clean = normalise_asr_text(text, streamer_name=streamer_name)
        parts = split_timed_subtitle_segment(
            start_s,
            max(end_s, start_s + 0.1),
            clean,
            max_chars=max_chars,
        )
        return SubtitleSegmentationTrace(
            segments=tuple(parts),
            boundaries=tuple(
                SubtitleBoundaryDecision(
                    segment_index=index,
                    reason=SubtitleBoundaryReason.UNALIGNED_TEXT_LIMIT,
                    at_seconds=part_end,
                    source_token_index=None,
                    gap_seconds=0.0,
                    duration_seconds=part_end - part_start,
                    visible_chars=subtitle_text_size(part_text),
                )
                for index, (part_start, part_end, part_text)
                in enumerate(parts[:-1])
            ),
        )
    timed_tokens = [
        (
            offset + float(timestamp[0]) / 1000.0,
            offset + float(timestamp[1]) / 1000.0,
            token,
        )
        for token, timestamp in zip(tokens, timestamps)
    ]
    return segment_timed_tokens_with_trace(
        timed_tokens,
        streamer_name=streamer_name,
        max_chars=max_chars,
    )


def segments_from_funasr_result(
    text,
    timestamps,
    offset=0.0,
    streamer_name="主播",
    raw_text=None,
    max_chars=SUBTITLE_MAX_CHARS,
):
    """兼容旧调用：返回 ``(start, end, text)`` 三元组列表。"""
    return list(segments_from_funasr_result_with_trace(
        text,
        timestamps,
        offset=offset,
        streamer_name=streamer_name,
        raw_text=raw_text,
        max_chars=max_chars,
    ).segments)


def dedupe_overlapping_funasr_segments(segments):
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
        previous_text = re.sub(r"\s+", "", previous[2])
        segment_text = re.sub(r"\s+", "", segment[2])
        contains = previous_text and segment_text and (
            previous_text in segment_text or segment_text in previous_text
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


def is_funasr_punctuation(char):
    return unicodedata.category(char).startswith("P") or char in "…"


def attach_funasr_punctuation_to_tokens(tokens, text):
    """在正文有少量识别差异时，仍把标点映射回字级时间戳。"""
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
        if is_funasr_punctuation(char):
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
                    offset,
                    max(0, source_len - 1),
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


def align_funasr_tokens(text, timestamps, raw_text=None):
    """把标点模型新增的字符挂回原始 ASR token，保持时间戳数量一致。"""
    timestamps = [
        item
        for item in (timestamps or [])
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
        if is_funasr_punctuation(char):
            punct_by_position[position] += char
        else:
            plain_chars.append(char)
            position += 1
    raw_plain = "".join(
        char for char in raw_compact if not is_funasr_punctuation(char)
    )
    if "".join(plain_chars) != raw_plain:
        if raw_plain != raw_compact:
            return tokens, True
        return attach_funasr_punctuation_to_tokens(tokens, text), True

    if raw_plain != raw_compact:
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


def trim_funasr_tokens_to_core(
    text,
    timestamps,
    input_start,
    core_start,
    core_end,
    raw_text=None,
):
    """按字词时间归属主体区间，避免重叠输入在边界生成重复半句。"""
    tokens, aligned = align_funasr_tokens(text, timestamps, raw_text=raw_text)
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


def srt_time(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, whole_seconds = divmod(remainder, 60)
    milliseconds = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def parse_srt_timestamp(value):
    """解析 SRT 时间戳，返回视频内秒数。"""
    hours, minutes, remainder = value.strip().split(":")
    seconds, milliseconds = remainder.replace(".", ",").split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(milliseconds) / 1000
    )


def srt_video_duration(srt_segments):
    """用最后一句字幕估算可用视频时长。"""
    if not srt_segments:
        return None
    return max(seg_end for _, seg_end, _ in srt_segments)


__all__ = [
    *FACADE_EXPORTS.values(),
    "FACADE_EXPORTS",
    "NATURAL_SENTENCE_CONNECTORS",
    "SubtitleBoundaryDecision",
    "SubtitleBoundaryReason",
    "SubtitleSegmentationTrace",
    "segment_timed_tokens_with_trace",
    "segments_from_funasr_result_with_trace",
]
