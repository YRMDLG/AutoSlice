"""录播尾部收播口令检测与报告话题转换的唯一实现。"""

from __future__ import annotations

import math
import re

from autoslice.streamer_profiles import current_streamer_profile

from . import policy as clip_policy

FACADE_EXPORTS = {
    "_OUTRO_ACTIVITY_VARIANT_RE": "_OUTRO_ACTIVITY_VARIANT_RE",
    "_OUTRO_FAREWELL_EVIDENCE": "_OUTRO_FAREWELL_EVIDENCE",
    "_OUTRO_TRIGGER_NORMALISE_RE": "_OUTRO_TRIGGER_NORMALISE_RE",
    "_detect_stream_outro_clip": "_detect_stream_outro_clip",
    "_has_outro_farewell_evidence": "_has_outro_farewell_evidence",
    "_normalise_outro_trigger_text": "_normalise_outro_trigger_text",
    "_outro_topic_from_mark": "_outro_topic_from_mark",
}


OUTRO_TRIGGER_JOIN_GAP_SEC = clip_policy.OUTRO_TRIGGER_JOIN_GAP_SEC
OUTRO_VARIANT_FAREWELL_BEFORE_SEC = clip_policy.OUTRO_VARIANT_FAREWELL_BEFORE_SEC
OUTRO_VARIANT_FAREWELL_AFTER_SEC = clip_policy.OUTRO_VARIANT_FAREWELL_AFTER_SEC


_OUTRO_TRIGGER_NORMALISE_RE = re.compile(r"[\s,，。！？!?、…~～\-_—]+")


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
