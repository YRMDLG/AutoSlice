"""弹幕解析、密度峰值发现与结构化互动证据的唯一实现。"""

from __future__ import annotations

import bisect
import html
import math
import os
import re
from collections import Counter
from datetime import timedelta
from xml.etree import ElementTree


FACADE_EXPORTS = {
    'CLIP_DENSITY_PERCENTILE': 'CLIP_DENSITY_PERCENTILE',
    'CLIP_DENSITY_RATIO': 'CLIP_DENSITY_RATIO',
    'CLIP_LOCAL_PEAK_RADIUS_SEC': 'CLIP_LOCAL_PEAK_RADIUS_SEC',
    'DANMAKU_EVIDENCE_MAX_ITEMS': 'DANMAKU_EVIDENCE_MAX_ITEMS',
    'DANMAKU_LOCAL_BASELINE_EXCLUSION_SEC': 'DANMAKU_LOCAL_BASELINE_EXCLUSION_SEC',
    'DANMAKU_LOCAL_BASELINE_RADIUS_SEC': 'DANMAKU_LOCAL_BASELINE_RADIUS_SEC',
    'DANMAKU_MESSAGE_MAX_CHARS': 'DANMAKU_MESSAGE_MAX_CHARS',
    '_ASS_OVERRIDE_TAG_RE': '_ASS_OVERRIDE_TAG_RE',
    '_DANMAKU_BRACKET_EMOTE_RE': '_DANMAKU_BRACKET_EMOTE_RE',
    '_DANMAKU_GENERIC_REACTIONS': '_DANMAKU_GENERIC_REACTIONS',
    '_DANMAKU_PROMPT_INSTRUCTION_RE': '_DANMAKU_PROMPT_INSTRUCTION_RE',
    '_DANMAKU_TITLE_CUE_GROUPS': '_DANMAKU_TITLE_CUE_GROUPS',
    '_DANMAKU_TITLE_CUE_PRIORITY_PATTERNS': '_DANMAKU_TITLE_CUE_PRIORITY_PATTERNS',
    '_DANMAKU_UPOWER_RE': '_DANMAKU_UPOWER_RE',
    'DanmakuDensitySeries': 'DanmakuDensitySeries',
    '_normalise_danmaku_message': '_normalise_danmaku_message',
    '_display_danmaku_message': '_display_danmaku_message',
    '_is_question_only_danmaku': '_is_question_only_danmaku',
    '_danmaku_title_cue_messages': '_danmaku_title_cue_messages',
    '_danmaku_title_cue_groups_for_context': '_danmaku_title_cue_groups_for_context',
    '_danmaku_peak_content_evidence': '_danmaku_peak_content_evidence',
    '_format_danmaku_peak_content': '_format_danmaku_peak_content',
    '_danmaku_prompt_message_items': '_danmaku_prompt_message_items',
    '_density_percentile': '_density_percentile',
    '_danmaku_clip_threshold': '_danmaku_clip_threshold',
    '_median_number': '_median_number',
    '_danmaku_content_quality': '_danmaku_content_quality',
    'analyze_danmaku': 'analyze_danmaku',
    'DANMAKU_WINDOW': 'DANMAKU_WINDOW',
    'DANMAKU_WINDOW_STEP': 'DANMAKU_WINDOW_STEP',
    '_average_danmaku_density': '_average_danmaku_density',
    '_clean_ass_danmaku_text': '_clean_ass_danmaku_text',
    '_danmaku_peak_features': '_danmaku_peak_features',
    '_danmaku_prompt_evidence': '_danmaku_prompt_evidence',
    '_high_energy_danmaku_peaks': '_high_energy_danmaku_peaks',
    '_is_generic_danmaku_reaction': '_is_generic_danmaku_reaction',
    '_reviewed_danmaku_ranking_score': '_reviewed_danmaku_ranking_score',
}


DANMAKU_WINDOW = 60

DANMAKU_WINDOW_STEP = 15  # 每 15 秒采样一个 60 秒窗口，兼顾峰值定位和全场覆盖

DANMAKU_MESSAGE_MAX_CHARS = 120

DANMAKU_EVIDENCE_MAX_ITEMS = 6

DANMAKU_LOCAL_BASELINE_RADIUS_SEC = 300

DANMAKU_LOCAL_BASELINE_EXCLUSION_SEC = 90

CLIP_DENSITY_RATIO = 1.20  # 话题切片至少需要达到全场平均的 1.2 倍

CLIP_DENSITY_PERCENTILE = 0.85  # 同时达到整场较高分位，避免把普通波动当爆点

CLIP_LOCAL_PEAK_RADIUS_SEC = 150  # 只保留前后 2.5 分钟内最高的独立峰值

_ASS_OVERRIDE_TAG_RE = re.compile(r"\{[^{}]*\}")

_DANMAKU_BRACKET_EMOTE_RE = re.compile(r"(?:\[[^\[\]\r\n]{1,64}\])+$")

_DANMAKU_UPOWER_RE = re.compile(r"^\[UPOWER_[^\]_]+_(?P<text>[^\]]+)\]$", re.IGNORECASE)

_DANMAKU_GENERIC_REACTIONS = {
    "?", "??", "???", "？", "？？", "？？？", "!", "!!", "!!!",
    "！", "！！", "！！！", "疑问", "震惊", "爱你", "贴贴", "摸头",
    "可爱", "好看", "打call", "哈哈", "哈哈哈", "哈哈哈哈", "哈哈哈哈哈",
    "草", "笑", "哇", "啊", "我去", "卧槽",
}

_DANMAKU_TITLE_CUE_GROUPS = (
    (
        "颜色造型",
        re.compile(
            r"(?:紫|蓝|青|红|黄|绿|白|黑|金|银)"
            r"(?:色|发|毛|框|边|瞳|眼|衣|裙|袜|丝)|"
            r"粉(?:色|发|毛|框|边|瞳|眼|衣|裙|袜)|染色|挑染|应援色",
            re.IGNORECASE,
        ),
    ),
    (
        "服装或视觉细节",
        re.compile(
            r"虾线|鼓包|挂钩|吊袜|破洞|划破|撕破|刮破|战损|黑丝|白丝|丝袜|"
            r"蓝框|篮筐|双层|连体|反光|光环|南半球|北半球|裤|皮裙",
            re.IGNORECASE,
        ),
    ),
    (
        "身份或关系反转",
        re.compile(
            r"ai音|女王音|天使音|换人|你是|你谁|初登场|第一次|不认识|谁啊",
            re.IGNORECASE,
        ),
    ),
    (
        "目标或难度反差",
        re.compile(
            r"五十万|50万|一百万|100万|百万粉|百大|游戏高手|更难|最难|太难|"
            r"有点难|做不到|完蛋|聊.{0,8}(?:万|粉)",
            re.IGNORECASE,
        ),
    ),
    (
        "原话或结果反应",
        re.compile(
            r"居然|原来|没想到|竟然|不可能|回不去|笑死|破了|坏了|得逞|真相",
            re.IGNORECASE,
        ),
    ),
)

_DANMAKU_TITLE_CUE_PRIORITY_PATTERNS = {
    "颜色造型": re.compile(
        r"头发|发色|紫发|蓝发|粉发|紫毛|蓝毛|粉毛|蓝框|篮筐|衣服|裙|袜|黑丝|白丝",
        re.IGNORECASE,
    ),
    "服装或视觉细节": re.compile(
        r"虾线|鼓包|挂钩|破洞|划破|撕破|刮破|战损|蓝框|篮筐|双层",
        re.IGNORECASE,
    ),
    "身份或关系反转": re.compile(
        r"ai音|女王音|天使音|换人|初登场|第一次",
        re.IGNORECASE,
    ),
    "目标或难度反差": re.compile(
        r"五十万|50万|一百万|100万|百万粉|百大|游戏高手|更难|最难",
        re.IGNORECASE,
    ),
}

_DANMAKU_PROMPT_INSTRUCTION_RE = re.compile(
    r'(?:忽略|无视|覆盖|绕过).{0,12}(?:指令|规则|提示词|系统提示)'
    r'|(?:输出|泄露|显示|告诉我).{0,12}(?:密钥|秘密|api.?key|token|系统提示)',
    re.IGNORECASE,
)


def format_elapsed_time(seconds):
    """把视频内秒数格式化为稳定的经过时间文本。"""

    return str(timedelta(seconds=int(seconds)))


class DanmakuDensitySeries(list):
    """等间隔弹幕密度窗口，并保留按整场时长计算的真实平均值。"""

    def __init__(
            self, windows=(), average_density=0.0, message_count=0,
            duration=0.0, messages=()):
        super().__init__(windows)
        self.average_density = float(average_density)
        self.message_count = int(message_count)
        self.duration = float(duration)
        cleaned_messages = []
        for timestamp, text in messages or ():
            value = _clean_ass_danmaku_text(text)
            if not value:
                continue
            cleaned_messages.append((float(timestamp), value))
        cleaned_messages.sort(key=lambda item: item[0])
        self.messages = tuple(cleaned_messages)
        self.message_timestamps = tuple(item[0] for item in cleaned_messages)


def _clean_ass_danmaku_text(value):
    """清理 ASS 样式指令和控制字符，但保留观众实际发送的文字。"""
    text = _ASS_OVERRIDE_TAG_RE.sub("", str(value or ""))
    text = text.replace(r"\N", " ").replace(r"\n", " ").replace(r"\h", " ")
    text = html.unescape(text)
    text = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u2060\ufeff]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > DANMAKU_MESSAGE_MAX_CHARS:
        text = text[:DANMAKU_MESSAGE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _normalise_danmaku_message(value):
    text = re.sub(r"\s+", "", str(value or "")).casefold()
    match = _DANMAKU_UPOWER_RE.fullmatch(text)
    return match.group("text") if match else text


def _display_danmaku_message(value):
    """去掉 UPOWER 平台包装，保留观众实际写的反应内容。"""
    text = _clean_ass_danmaku_text(value)
    match = _DANMAKU_UPOWER_RE.fullmatch(text)
    return match.group("text") if match else text


def _is_generic_danmaku_reaction(value):
    compact = _normalise_danmaku_message(value)
    if not compact:
        return True
    if compact in _DANMAKU_GENERIC_REACTIONS:
        return True
    if _DANMAKU_BRACKET_EMOTE_RE.fullmatch(compact):
        return True
    if len(set(compact)) == 1:
        return True
    if re.fullmatch(r"[？?!！…~～哈啊嘿嗯哼草笑wW]+", compact):
        return True
    return False


def _is_question_only_danmaku(value):
    """问号刷屏只表示困惑，不单独视为有内容的互动证据。"""
    compact = _normalise_danmaku_message(value)
    return bool(compact and re.fullmatch(r"[?？]+", compact))


def _danmaku_title_cue_messages(
        counts, display_by_key, first_index, max_items=8, per_group=2):
    """从完整峰值中按信息类型保留少量标题线索，避免同义高频句垄断名额。"""
    selected = []
    selected_keys = set()
    for cue, pattern in _DANMAKU_TITLE_CUE_GROUPS:
        priority_pattern = _DANMAKU_TITLE_CUE_PRIORITY_PATTERNS.get(cue)
        matches = [
            key for key in counts
            if (
                key not in selected_keys
                and not _is_generic_danmaku_reaction(key)
                and pattern.search(display_by_key[key])
            )
        ]
        if not matches:
            continue
        matches.sort(key=lambda key: (
            -int(bool(
                priority_pattern and priority_pattern.search(display_by_key[key])
            )),
            -counts[key],
            -len(re.sub(r"[^\w\u4e00-\u9fff]+", "", display_by_key[key])),
            first_index[key],
        ))
        for key in matches[:per_group]:
            selected_keys.add(key)
            selected.append({
                "text": display_by_key[key],
                "count": counts[key],
                "cue": cue,
            })
            if len(selected) >= max_items:
                return selected
    return selected


def _danmaku_title_cue_groups_for_context(value):
    """用话题核心字幕确定相关线索类别，避免扩展收尾中的下一话题干扰标题。"""
    text = str(value or "")
    return {
        cue for cue, pattern in _DANMAKU_TITLE_CUE_GROUPS
        if pattern.search(text)
    }


def _danmaku_peak_content_evidence(
        series, peak_start, window_sec=DANMAKU_WINDOW,
        max_items=DANMAKU_EVIDENCE_MAX_ITEMS):
    """摘要峰值窗口的弹幕原文；旧密度列表没有原文时安全降级。"""
    timestamps = tuple(getattr(series, "message_timestamps", ()) or ())
    messages = tuple(getattr(series, "messages", ()) or ())
    if not timestamps or not messages:
        return None
    start = float(peak_start)
    end = start + float(window_sec)
    left = bisect.bisect_left(timestamps, start)
    right = bisect.bisect_left(timestamps, end)
    window_messages = messages[left:right]
    if not window_messages:
        return None

    display_by_key = {}
    first_index = {}
    normalised = []
    for index, (_, text) in enumerate(window_messages):
        key = _normalise_danmaku_message(text)
        if not key:
            continue
        normalised.append(key)
        display_by_key.setdefault(key, _display_danmaku_message(text))
        first_index.setdefault(key, index)
    if not normalised:
        return None

    counts = Counter(normalised)
    generic_count = sum(
        count for key, count in counts.items()
        if _is_generic_danmaku_reaction(key)
    )
    question_count = sum(
        count for key, count in counts.items()
        if _is_question_only_danmaku(key)
    )
    informative_keys = [
        key for key in counts
        if len(key) >= 2 and not _is_generic_danmaku_reaction(key)
    ]
    informative_count = sum(counts[key] for key in informative_keys)
    ranked = sorted(
        counts,
        key=lambda key: (-counts[key], first_index[key]),
    )
    frequent_messages = [
        {"text": display_by_key[key], "count": counts[key]}
        for key in ranked[:max_items]
    ]
    representative_keys = list(informative_keys)
    representative_keys.sort(key=lambda key: (
        -(counts[key] * (1.0 + min(len(key), 24) / 24.0)),
        first_index[key],
    ))
    representative_messages = [
        {"text": display_by_key[key], "count": counts[key]}
        for key in representative_keys[:max_items]
    ]
    title_cue_messages = _danmaku_title_cue_messages(
        counts,
        display_by_key,
        first_index,
    )
    total = len(normalised)
    return {
        "window_start": int(start),
        "window_end": int(end),
        "message_count": total,
        "unique_count": len(counts),
        "unique_ratio": round(len(counts) / total, 3),
        "repeat_ratio": round(max(counts.values()) / total, 3),
        "generic_count": generic_count,
        "generic_ratio": round(generic_count / total, 3),
        "question_count": question_count,
        "question_ratio": round(question_count / total, 3),
        "informative_count": informative_count,
        "informative_unique_count": len(informative_keys),
        "informative_ratio": round(informative_count / total, 3),
        "frequent_messages": frequent_messages,
        "representative_messages": representative_messages,
        "title_cue_messages": title_cue_messages,
    }


def _format_danmaku_peak_content(evidence, max_items=4):
    """生成可嵌入报告或提示的有上限摘要，不对弹幕动机做推断。"""
    if not isinstance(evidence, dict):
        return ""
    selected = []
    seen = set()
    for key in ("representative_messages", "frequent_messages"):
        for item in evidence.get(key) or []:
            text = _clean_ass_danmaku_text(item.get("text", ""))
            normalised = _normalise_danmaku_message(text)
            if not text or normalised in seen:
                continue
            seen.add(normalised)
            count = max(1, int(item.get("count", 1) or 1))
            selected.append(f"“{text}”×{count}")
            if len(selected) >= max_items:
                break
        if len(selected) >= max_items:
            break
    return "峰值弹幕原文：" + "、".join(selected) if selected else ""


def _danmaku_prompt_message_items(evidence, key, limit=4):
    """限制送入模型的弹幕原文，并丢弃明显的提示注入文本。"""
    items = []
    seen = set()
    for item in (evidence or {}).get(key) or []:
        text = _clean_ass_danmaku_text(item.get("text", ""))
        normalised = _normalise_danmaku_message(text)
        if (
            not text
            or normalised in seen
            or _DANMAKU_PROMPT_INSTRUCTION_RE.search(text)
        ):
            continue
        seen.add(normalised)
        prompt_item = {
            "text": text,
            "count": max(1, int(item.get("count", 1) or 1)),
        }
        cue = re.sub(r"\s+", " ", str(item.get("cue", ""))).strip()
        if cue:
            prompt_item["cue"] = cue[:24]
        items.append(prompt_item)
        if len(items) >= limit:
            break
    return items


def _danmaku_prompt_evidence(features, max_items=4, title_context=""):
    """生成有界、可审计的模型弹幕证据，原文只作旁证。"""
    if not isinstance(features, dict):
        return None
    evidence = features.get("content_evidence")
    payload = {
        "window_start": format_elapsed_time(features.get("peak_start", 0)),
        "window_end": format_elapsed_time(
            int(features.get("peak_start", 0)) + DANMAKU_WINDOW
        ),
        "density": features.get("density"),
        "global_ratio": features.get("global_ratio"),
        "local_surge_ratio": features.get("local_surge_ratio"),
        "density_percentile": features.get("density_percentile"),
        "selection_score": features.get("selection_score"),
        "interaction_signal": features.get("interaction_signal"),
        "content_available": bool(evidence),
    }
    if not evidence:
        return payload
    title_cue_messages = list(evidence.get("title_cue_messages") or [])
    relevant_cue_groups = _danmaku_title_cue_groups_for_context(title_context)
    if relevant_cue_groups:
        relevant_title_cues = [
            item for item in title_cue_messages
            if (
                item.get("cue") in relevant_cue_groups
                or int(item.get("count", 0) or 0) >= 2
            )
        ]
        if relevant_title_cues:
            title_cue_messages = relevant_title_cues
    prompt_evidence = dict(evidence)
    prompt_evidence["title_cue_messages"] = title_cue_messages
    payload.update({
        "message_count": int(evidence.get("message_count", 0) or 0),
        "informative_ratio": float(evidence.get("informative_ratio", 0) or 0),
        "generic_ratio": float(evidence.get("generic_ratio", 0) or 0),
        "question_ratio": float(evidence.get("question_ratio", 0) or 0),
        "repeat_ratio": float(evidence.get("repeat_ratio", 0) or 0),
        "unique_ratio": float(evidence.get("unique_ratio", 0) or 0),
        "representative_messages": _danmaku_prompt_message_items(
            evidence,
            "representative_messages",
            limit=max_items,
        ),
        "title_cue_messages": _danmaku_prompt_message_items(
            prompt_evidence,
            "title_cue_messages",
            limit=max_items,
        ),
        "frequent_messages": _danmaku_prompt_message_items(
            evidence,
            "frequent_messages",
            limit=max_items,
        ),
    })
    return payload


def _average_danmaku_density(windows):
    """优先读取整场真实均值；普通列表继续兼容既有测试和调用方。"""
    if hasattr(windows, "average_density"):
        return float(windows.average_density)
    densities = [density for _, density in windows or []]
    return sum(densities) / len(densities) if densities else 0.0


def _density_percentile(windows, percentile):
    """返回密度最近秩分位数；样本为空时返回 0。"""
    densities = sorted(float(density) for _, density in windows or [])
    if not densities:
        return 0.0
    rank = max(0, min(len(densities) - 1, math.ceil(len(densities) * percentile) - 1))
    return densities[rank]


def _danmaku_clip_threshold(peaks, avg_density):
    """计算正式切片门槛；完整滑窗还需达到整场较高分位。"""
    threshold = max(avg_density * CLIP_DENSITY_RATIO, avg_density + 10, 20)
    if isinstance(peaks, DanmakuDensitySeries) and len(peaks) >= 20:
        threshold = max(
            threshold,
            _density_percentile(peaks, CLIP_DENSITY_PERCENTILE),
        )
    return float(threshold)


def _high_energy_danmaku_peaks(peaks, avg_density=None):
    """从滑动窗口中提取互相独立的局部高能峰值。"""
    if not peaks:
        return []
    avg_density = (
        _average_danmaku_density(peaks)
        if avg_density is None
        else float(avg_density)
    )
    threshold = _danmaku_clip_threshold(peaks, avg_density)
    all_windows = [
        (int(start), float(density))
        for start, density in peaks
    ]
    candidates = [
        (int(start), float(density))
        for start, density in all_windows
        if float(density) >= threshold
    ]

    # 必须是邻域内真正最高的窗口。不能只和“已选峰值”比较，否则一个
    # 被更高峰压掉的肩峰仍可能继续放行更外侧的次级肩峰。
    selected = []
    for start, density in candidates:
        if any(
            abs(start - other_start) <= CLIP_LOCAL_PEAK_RADIUS_SEC
            and (
                other_density > density
                or (other_density == density and other_start < start)
            )
            for other_start, other_density in all_windows
        ):
            continue
        selected.append((start, density))
    return sorted(selected, key=lambda item: item[0])


def _median_number(values):
    """计算中位数，避免为一个简单统计额外引入依赖。"""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _danmaku_content_quality(evidence):
    """把有效交流与无意义刷屏压缩为 0-1 内容质量分。"""
    if not isinstance(evidence, dict):
        return None
    informative_ratio = float(evidence.get("informative_ratio", 0) or 0)
    informative_unique = int(evidence.get("informative_unique_count", 0) or 0)
    unique_ratio = float(evidence.get("unique_ratio", 0) or 0)
    repeat_ratio = float(evidence.get("repeat_ratio", 0) or 0)
    generic_ratio = float(evidence.get("generic_ratio", 0) or 0)
    question_ratio = float(evidence.get("question_ratio", 0) or 0)
    positive = (
        informative_ratio * 0.50
        + min(1.0, informative_unique / 8.0) * 0.20
        + unique_ratio * 0.20
        + (1.0 - repeat_ratio) * 0.10
    )
    penalty = question_ratio * 0.35 + max(0.0, generic_ratio - 0.50) * 0.40
    return round(max(0.0, min(1.0, positive - penalty)), 4)


def _danmaku_peak_features(peaks, peak_start, density, avg_density=None):
    """计算峰值的全场强度、局部突增和弹幕内容信号。"""
    avg_density = (
        _average_danmaku_density(peaks)
        if avg_density is None
        else float(avg_density)
    )
    windows = [(int(start), float(value)) for start, value in peaks or []]
    local_values = [
        value for start, value in windows
        if (
            DANMAKU_LOCAL_BASELINE_EXCLUSION_SEC
            < abs(start - int(peak_start))
            <= DANMAKU_LOCAL_BASELINE_RADIUS_SEC
        )
    ]
    if len(local_values) < 3:
        local_values = [
            value for start, value in windows
            if start != int(peak_start)
            and abs(start - int(peak_start)) <= DANMAKU_LOCAL_BASELINE_RADIUS_SEC
        ]
    local_baseline = _median_number(local_values) or avg_density or 1.0
    global_ratio = float(density) / max(avg_density, 1.0)
    local_surge_ratio = float(density) / max(local_baseline, 1.0)
    percentile = (
        sum(1 for _, value in windows if value <= float(density)) / len(windows)
        if windows else 0.0
    )
    evidence = _danmaku_peak_content_evidence(peaks, peak_start)
    content_quality = _danmaku_content_quality(evidence)

    global_strength = min(1.0, global_ratio / 3.0)
    local_strength = min(1.0, max(0.0, local_surge_ratio - 1.0) / 2.0)
    score = 100.0 * (
        global_strength * 0.30
        + local_strength * 0.50
        + percentile * 0.20
    )
    if content_quality is not None:
        score *= 0.75 + content_quality * 0.50

    interaction_signal = "无原文"
    if evidence:
        if (
            float(evidence.get("question_ratio", 0) or 0) >= 0.60
            or float(evidence.get("generic_ratio", 0) or 0) >= 0.80
        ):
            interaction_signal = "无意义刷屏偏高"
        elif (
            float(evidence.get("informative_ratio", 0) or 0) >= 0.35
            and int(evidence.get("informative_unique_count", 0) or 0) >= 3
        ):
            interaction_signal = "具体互动明显"
        else:
            interaction_signal = "混合互动"
    return {
        "peak_start": int(peak_start),
        "peak_center": int(peak_start + DANMAKU_WINDOW / 2),
        "density": round(float(density), 3),
        "global_average": round(float(avg_density), 3),
        "global_ratio": round(global_ratio, 3),
        "local_baseline": round(local_baseline, 3),
        "local_surge_ratio": round(local_surge_ratio, 3),
        "density_percentile": round(percentile, 4),
        "content_quality": content_quality,
        "selection_score": round(score, 4),
        "interaction_signal": interaction_signal,
        "content_evidence": evidence,
    }


def _reviewed_danmaku_ranking_score(features):
    """Terra 已确认内容成立后，兼顾局部突增和全场绝对热度。"""
    selection_score = float(features.get("selection_score", 0) or 0)
    percentile = float(features.get("density_percentile", 0) or 0)
    global_ratio = float(features.get("global_ratio", 0) or 0)
    content_quality = features.get("content_quality")
    quality = 1.0 if content_quality is None else float(content_quality)
    global_strength = min(1.0, max(0.0, global_ratio) / 3.0)
    absolute_strength = 100.0 * (
        percentile * 0.40
        + global_strength * 0.30
    )
    # 复核后的事件已由字幕证明成立；内容质量仍用于压低问号和复读刷屏。
    absolute_strength *= 0.75 + max(0.0, min(1.0, quality)) * 0.25
    return round(selection_score * 0.30 + absolute_strength, 4)


def _parse_ass_messages(ass_path):
    """读取 ASS ``Dialogue`` 行，返回视频内时间与原始弹幕文本。"""

    messages = []
    with open(ass_path, encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.startswith("Dialogue:"):
                continue
            parts = line.rstrip("\r\n").split(",", 9)
            if len(parts) < 2:
                continue
            try:
                h, m, s = parts[1].strip().split(":")
                timestamp = int(h) * 3600 + int(m) * 60 + float(s)
            except (TypeError, ValueError):
                continue
            text = _clean_ass_danmaku_text(parts[9] if len(parts) >= 10 else "")
            messages.append((timestamp, text))
    return messages


def _parse_xml_messages(xml_path):
    """流式读取 Bilibili XML ``<d p=\"秒,...\">`` 弹幕。"""

    messages = []
    try:
        for _event, element in ElementTree.iterparse(xml_path, events=("end",)):
            if element.tag.rsplit("}", 1)[-1] != "d":
                element.clear()
                continue
            position = str(element.attrib.get("p", "")).split(",", 1)[0]
            try:
                timestamp = float(position)
            except (TypeError, ValueError):
                element.clear()
                continue
            messages.append((timestamp, _clean_ass_danmaku_text(element.text or "")))
            element.clear()
    except ElementTree.ParseError:
        return []
    return messages


def parse_danmaku_messages(source_path):
    """按扩展名解析 ASS 或 Bilibili XML，并保留空文本的密度计数。"""

    if not source_path or not os.path.isfile(source_path):
        return []
    if os.path.splitext(os.fspath(source_path))[1].casefold() == ".xml":
        return _parse_xml_messages(source_path)
    return _parse_ass_messages(source_path)


def analyze_danmaku(source_path):
    """按固定步长统计 60 秒滑动窗口，并保留可核对的弹幕原文。"""

    parsed_messages = parse_danmaku_messages(source_path)
    if not parsed_messages:
        return DanmakuDensitySeries()

    timestamps = [timestamp for timestamp, _text in parsed_messages]
    messages = [
        (timestamp, text)
        for timestamp, text in parsed_messages
        if text
    ]

    if not timestamps:
        return DanmakuDensitySeries()

    timestamps.sort()
    duration = max(float(timestamps[-1]), float(DANMAKU_WINDOW))
    average_density = len(timestamps) * 60.0 / duration
    windows = []
    for start in range(0, int(math.floor(timestamps[-1])) + 1, DANMAKU_WINDOW_STEP):
        left = bisect.bisect_left(timestamps, start)
        right = bisect.bisect_left(timestamps, start + DANMAKU_WINDOW)
        windows.append((start, right - left))

    return DanmakuDensitySeries(
        windows,
        average_density=average_density,
        message_count=len(timestamps),
        duration=duration,
        messages=messages,
    )
