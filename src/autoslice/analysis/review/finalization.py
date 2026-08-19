"""切片最终边界、时长上限与重叠合并的唯一实现。"""

from __future__ import annotations

import math

from autoslice.analysis.review import deduplication as clip_deduplication
from autoslice.analysis.review import policy as clip_policy
from autoslice.transcription import segments as transcription_segments

FACADE_EXPORTS = {
    "_nearest_safe_srt_boundary": "_nearest_safe_srt_boundary",
    "_merge_expanded_clip_marks": "_merge_expanded_clip_marks",
    "_refresh_natural_boundary_metadata": "_refresh_natural_boundary_metadata",
    "_cap_expanded_clip_mark": "_cap_expanded_clip_mark",
    "_snap_clip_to_srt_segments": "_snap_clip_to_srt_segments",
    "_integer_clip_bounds_outside_subtitles": "_integer_clip_bounds_outside_subtitles",
    "_fit_final_clip_to_safe_srt_boundaries": "_fit_final_clip_to_safe_srt_boundaries",
    "_capped_speech_chain_start": "_capped_speech_chain_start",
}


TOPIC_CONTEXT_GAP = transcription_segments.TOPIC_CONTEXT_GAP

TOPIC_PRE_CONTEXT_SEC = clip_policy.TOPIC_PRE_CONTEXT_SEC
TOPIC_MAX_CLIP_SEC = clip_policy.TOPIC_MAX_CLIP_SEC
TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC = clip_policy.TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC = clip_policy.TOPIC_NATURAL_BOUNDARY_PRE_MAX_SEC
TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC = clip_policy.TOPIC_NATURAL_BOUNDARY_POST_MAX_SEC
TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC = (
    clip_policy.TOPIC_AI_FOCUS_NATURAL_POST_BOUNDARY_SEC
)

_overlap_ratio = clip_deduplication._overlap_ratio
_dedupe_clip_marks = clip_deduplication._dedupe_clip_marks


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
