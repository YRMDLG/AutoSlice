"""切片前后文、字幕安全边界、收播与非重叠决策的唯一实现。"""

from __future__ import annotations

import math
import re
from collections import defaultdict

from autoslice.analysis import clip_policy
from autoslice.streamer_profiles import current_streamer_profile
from autoslice.transcription import service as transcription_service
from autoslice.transcription import segments as transcription_segments

FACADE_EXPORTS = {
    "_BOUNDARY_EVIDENCE_STOP_TERMS": "_BOUNDARY_EVIDENCE_STOP_TERMS",
    "_NEXT_CASE_ASR_TRIGGER_RE": "_NEXT_CASE_ASR_TRIGGER_RE",
    "_OUTRO_ACTIVITY_VARIANT_RE": "_OUTRO_ACTIVITY_VARIANT_RE",
    "_OUTRO_FAREWELL_EVIDENCE": "_OUTRO_FAREWELL_EVIDENCE",
    "_OUTRO_TRIGGER_NORMALISE_RE": "_OUTRO_TRIGGER_NORMALISE_RE",
    "_TOPIC_CONCLUSION_RE": "_TOPIC_CONCLUSION_RE",
    "_TOPIC_DECISION_EVIDENCE_RE": "_TOPIC_DECISION_EVIDENCE_RE",
    "_TOPIC_DISCOURSE_CONTINUATION_RE": "_TOPIC_DISCOURSE_CONTINUATION_RE",
    "_TOPIC_LEAD_IN_TRIGGER_RE": "_TOPIC_LEAD_IN_TRIGGER_RE",
    "_TOPIC_REFUND_RE": "_TOPIC_REFUND_RE",
    "_TRIGGER_CONTEXT_TOPIC_RE": "_TRIGGER_CONTEXT_TOPIC_RE",
    "_VISUAL_CASE_SHIFT_RE": "_VISUAL_CASE_SHIFT_RE",
    "_VISUAL_REACTION_LEAD_IN_RE": "_VISUAL_REACTION_LEAD_IN_RE",
    "_VISUAL_REVIEW_TOPIC_RE": "_VISUAL_REVIEW_TOPIC_RE",
    "_boundary_context_has_speech": "_boundary_context_has_speech",
    "_boundary_context_is_relevant": "_boundary_context_is_relevant",
    "_boundary_evidence_term_counts": "_boundary_evidence_term_counts",
    "_boundary_evidence_text_is_relevant": "_boundary_evidence_text_is_relevant",
    "_cap_expanded_clip_mark": "_cap_expanded_clip_mark",
    "_capped_speech_chain_start": "_capped_speech_chain_start",
    "_clip_context_requires_trigger": "_clip_context_requires_trigger",
    "_dedupe_clip_marks": "_dedupe_clip_marks",
    "_detect_stream_outro_clip": "_detect_stream_outro_clip",
    "_expand_clip_mark_with_context": "_expand_clip_mark_with_context",
    "_expand_clip_marks_with_context": "_expand_clip_marks_with_context",
    "_find_next_topic_hard_end": "_find_next_topic_hard_end",
    "_find_relevant_topic_context_end": "_find_relevant_topic_context_end",
    "_find_relevant_topic_context_start": "_find_relevant_topic_context_start",
    "_find_sc_context_start": "_find_sc_context_start",
    "_find_topic_lead_in_start": "_find_topic_lead_in_start",
    "_find_visual_reaction_context_start": "_find_visual_reaction_context_start",
    "_fit_final_clip_to_safe_srt_boundaries": "_fit_final_clip_to_safe_srt_boundaries",
    "_gift_trigger_has_question_followup": "_gift_trigger_has_question_followup",
    "_has_outro_farewell_evidence": "_has_outro_farewell_evidence",
    "_integer_clip_bounds_outside_subtitles": "_integer_clip_bounds_outside_subtitles",
    "_is_duplicate_topic": "_is_duplicate_topic",
    "_is_explicit_sc_topic": "_is_explicit_sc_topic",
    "_is_explicit_sc_trigger": "_is_explicit_sc_trigger",
    "_looks_like_delayed_topic_conclusion": "_looks_like_delayed_topic_conclusion",
    "_looks_like_discourse_continuation": "_looks_like_discourse_continuation",
    "_looks_like_low_score_visual_case_shift": "_looks_like_low_score_visual_case_shift",
    "_looks_like_next_case_transition": "_looks_like_next_case_transition",
    "_looks_like_sc_or_gift_trigger": "_looks_like_sc_or_gift_trigger",
    "_merge_expanded_clip_marks": "_merge_expanded_clip_marks",
    "_nearest_safe_srt_boundary": "_nearest_safe_srt_boundary",
    "_next_report_topic_safe_boundary": "_next_report_topic_safe_boundary",
    "_normalise_boundary_evidence_text": "_normalise_boundary_evidence_text",
    "_normalise_outro_trigger_text": "_normalise_outro_trigger_text",
    "_outro_topic_from_mark": "_outro_topic_from_mark",
    "_overlap_ratio": "_overlap_ratio",
    "_refresh_natural_boundary_metadata": "_refresh_natural_boundary_metadata",
    "_score_boundary_evidence_text": "_score_boundary_evidence_text",
    "_snap_clip_to_srt_segments": "_snap_clip_to_srt_segments",
    "_split_chain_crossing_topic_end": "_split_chain_crossing_topic_end",
    "_srt_video_duration": "_srt_video_duration",
    "_subtitle_speech_chains": "_subtitle_speech_chains",
    "parse_srt_segments": "parse_srt_segments",
}


_profile_identity_names = transcription_service.profile_identity_names
_load_repaired_srt_segments = transcription_service._load_repaired_srt_segments
TOPIC_CONTEXT_GAP = transcription_segments.TOPIC_CONTEXT_GAP

TOPIC_PRE_CONTEXT_SEC = clip_policy.TOPIC_PRE_CONTEXT_SEC
TOPIC_POST_CONTEXT_SEC = clip_policy.TOPIC_POST_CONTEXT_SEC
TOPIC_MIN_CLIP_SEC = clip_policy.TOPIC_MIN_CLIP_SEC
TOPIC_MAX_CLIP_SEC = clip_policy.TOPIC_MAX_CLIP_SEC
TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC = clip_policy.TOPIC_REQUIRED_CONTEXT_OVERFLOW_SEC
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
SC_TRIGGER_KEYWORDS = clip_policy.SC_TRIGGER_KEYWORDS
THANKS_TRIGGER_RE = clip_policy.THANKS_TRIGGER_RE
OUTRO_TRIGGER_JOIN_GAP_SEC = clip_policy.OUTRO_TRIGGER_JOIN_GAP_SEC
OUTRO_VARIANT_FAREWELL_BEFORE_SEC = clip_policy.OUTRO_VARIANT_FAREWELL_BEFORE_SEC
OUTRO_VARIANT_FAREWELL_AFTER_SEC = clip_policy.OUTRO_VARIANT_FAREWELL_AFTER_SEC


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


OUTRO_TRIGGER_JOIN_GAP_SEC = clip_policy.OUTRO_TRIGGER_JOIN_GAP_SEC


OUTRO_VARIANT_FAREWELL_BEFORE_SEC = clip_policy.OUTRO_VARIANT_FAREWELL_BEFORE_SEC


OUTRO_VARIANT_FAREWELL_AFTER_SEC = clip_policy.OUTRO_VARIANT_FAREWELL_AFTER_SEC


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
