"""人工时间轴的北京时间换算、多段录播过滤与字幕模糊校准。"""

from __future__ import annotations

import difflib
import html
import math
import os
import re
import zipfile
from datetime import datetime, timedelta

from runtime_config import TIMELINE_DIR
from streamer_profiles import current_streamer_profile, profile_identity_names


FACADE_EXPORTS = {
    'MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE': 'MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE',
    'MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC': 'MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC',
    'MANUAL_TIMELINE_ALIGNMENT_STEP_SEC': 'MANUAL_TIMELINE_ALIGNMENT_STEP_SEC',
    'MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC': 'MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC',
    'MANUAL_TIMELINE_CHUNK_MARGIN_SEC': 'MANUAL_TIMELINE_CHUNK_MARGIN_SEC',
    'MANUAL_TIMELINE_DIR': 'MANUAL_TIMELINE_DIR',
    'MANUAL_TIMELINE_END_MARGIN_SEC': 'MANUAL_TIMELINE_END_MARGIN_SEC',
    'MANUAL_TIMELINE_GROUNDING_MIN_SCORE': 'MANUAL_TIMELINE_GROUNDING_MIN_SCORE',
    'MANUAL_TIMELINE_OPTIMIZATION_VERSION': 'MANUAL_TIMELINE_OPTIMIZATION_VERSION',
    'MANUAL_TIMELINE_OPTIMIZE_GAP_SEC': 'MANUAL_TIMELINE_OPTIMIZE_GAP_SEC',
    'MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC': 'MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC',
    '_MANUAL_SEMANTIC_BIGRAM_STOPWORDS': '_MANUAL_SEMANTIC_BIGRAM_STOPWORDS',
    '_MANUAL_SEMANTIC_GENERIC_TERMS': '_MANUAL_SEMANTIC_GENERIC_TERMS',
    '_extract_video_start_datetime': '_extract_video_start_datetime',
    '_manual_timeline_doc_candidates': '_manual_timeline_doc_candidates',
    '_find_manual_timeline_doc': '_find_manual_timeline_doc',
    '_read_docx_lines': 'read_docx_lines',
    '_parse_manual_timeline_lines': '_parse_manual_timeline_lines',
    '_parse_elapsed_timeline_report_lines': '_parse_elapsed_timeline_report_lines',
    '_filter_manual_timeline_entries': '_filter_manual_timeline_entries',
    'load_manual_timeline': 'load_manual_timeline',
    '_manual_timeline_summary': '_manual_timeline_summary',
    '_manual_alignment_text': '_manual_alignment_text',
    '_manual_semantic_core': '_manual_semantic_core',
    '_srt_alignment_windows': '_srt_alignment_windows',
    '_align_manual_timeline_entries_to_srt': '_align_manual_timeline_entries_to_srt',
    'MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE': 'MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE',
    'MANUAL_TIMELINE_TOPIC_POST_SEC': 'MANUAL_TIMELINE_TOPIC_POST_SEC',
    'MANUAL_TIMELINE_TOPIC_PRE_SEC': 'MANUAL_TIMELINE_TOPIC_PRE_SEC',
    '_manual_alignment_score': '_manual_alignment_score',
    '_manual_text_supports_candidate': '_manual_text_supports_candidate',
    '_parse_hms': 'parse_hms',
}


_TIMELINE_ACCOUNT_PREFIX_RE = re.compile(
    r"^\s*[【\[][^】\]\r\n]{1,32}[】\]]\s*",
    re.IGNORECASE,
)


MANUAL_TIMELINE_DIR = str(TIMELINE_DIR)

MANUAL_TIMELINE_CHUNK_MARGIN_SEC = 180

MANUAL_TIMELINE_TOPIC_PRE_SEC = 30

MANUAL_TIMELINE_TOPIC_POST_SEC = 150

MANUAL_TIMELINE_END_MARGIN_SEC = 15

MANUAL_TIMELINE_OPTIMIZE_GAP_SEC = 180

MANUAL_TIMELINE_OPTIMIZE_MAX_GROUP_SEC = 600

MANUAL_TIMELINE_OPTIMIZE_BATCH_SIZE = 3

MANUAL_TIMELINE_OPTIMIZATION_VERSION = 3

MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC = 600

MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC = 80

MANUAL_TIMELINE_ALIGNMENT_STEP_SEC = 20

MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE = 0.12

MANUAL_TIMELINE_GROUNDING_MIN_SCORE = 0.15

_MANUAL_SEMANTIC_GENERIC_TERMS = (
    "人工时间轴", "时间轴", "主播", "观众", "弹幕",
    "这个视频", "视频", "这个话题", "话题", "内容", "片段", "直播",
    "正在", "进行", "相关", "分享", "讨论", "聊天", "互动", "看到", "看了",
    "观看", "提到", "表示", "回应", "吐槽", "评价", "评论",
)

_MANUAL_SEMANTIC_BIGRAM_STOPWORDS = {
    "这个", "那个", "然后", "就是", "一个", "一下", "时候", "自己", "大家",
    "怎么", "什么", "还是", "感觉", "真的", "已经", "今天", "昨天", "现在",
}


def _extract_video_start_datetime(video_path):
    """从录播文件名/目录名提取视频起始墙钟时间，用于换算人工时间轴。"""
    basename = os.path.basename(video_path or "")
    candidates = [basename] + re.split(r'[\\/]+', video_path or "")
    patterns = (
        r'(?P<y>\d{4})年(?P<m>\d{1,2})月(?P<d>\d{1,2})[日号][-_\s]*'
        r'(?P<h>\d{1,2})点(?P<mi>\d{1,2})分(?:(?P<s>\d{1,2})秒)?',
        r'(?P<y>\d{4})[-.](?P<m>\d{1,2})[-.](?P<d>\d{1,2})[-_\s]+'
        r'(?P<h>\d{1,2})[-点:](?P<mi>\d{1,2})[-分:](?P<s>\d{1,2})',
    )
    for text in candidates:
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            parts = {
                key: int(value) if value is not None else 0
                for key, value in match.groupdict().items()
            }
            try:
                return datetime(parts["y"], parts["m"], parts["d"], parts["h"], parts["mi"], parts["s"])
            except ValueError:
                continue
    return None


def _manual_timeline_doc_candidates(video_start, timeline_dir=MANUAL_TIMELINE_DIR):
    """按录播日期生成可能的人工时间轴 docx 路径。"""
    if not video_start:
        return []
    compact = video_start.strftime("%Y%m%d")
    dotted = f"{video_start.year}.{video_start.month}.{video_start.day}"
    names = [
        f"{compact}.docx",
        f"{compact}切片文档.docx",
        f"{dotted}.docx",
        f"{dotted}切片文档.docx",
    ]
    return [os.path.join(timeline_dir, name) for name in names]


def _find_manual_timeline_doc(video_path, timeline_dir=MANUAL_TIMELINE_DIR):
    """自动查找时间轴目录中和录播日期匹配的 docx。"""
    video_start = _extract_video_start_datetime(video_path)
    for path in _manual_timeline_doc_candidates(video_start, timeline_dir):
        if os.path.exists(path):
            return path
    if not video_start or not os.path.isdir(timeline_dir):
        return None
    compact = video_start.strftime("%Y%m%d")
    dotted = f"{video_start.year}.{video_start.month}.{video_start.day}"
    matches = []
    for name in os.listdir(timeline_dir):
        if not name.lower().endswith(".docx"):
            continue
        if name.startswith(compact) or name.startswith(dotted):
            matches.append(os.path.join(timeline_dir, name))
    if not matches:
        return None
    matches.sort(key=lambda p: ("切片文档" in os.path.basename(p), os.path.basename(p)))
    return matches[0]


def read_docx_lines(docx_path):
    """读取 docx 段落文本；优先 python-docx，失败时用 zip XML 兜底。"""
    try:
        import docx  # type: ignore

        document = docx.Document(docx_path)
        return [p.text.strip() for p in document.paragraphs if p.text.strip()]
    except Exception:
        try:
            with zipfile.ZipFile(docx_path) as zf:
                xml = zf.read("word/document.xml").decode("utf-8", "ignore")
            texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml)
            merged = []
            current = []
            for item in texts:
                text = html.unescape(item).strip()
                if not text:
                    continue
                current.append(text)
                if re.search(r'[。！？!?]$', text) or re.match(r'^\d{1,2}:\d{2}', text):
                    merged.append("".join(current).strip())
                    current = []
            if current:
                merged.append("".join(current).strip())
            return [line for line in merged if line]
        except Exception:
            return []


def _parse_manual_timeline_lines(lines, video_start):
    """解析朋友整理的时间轴文档，把墙钟时间换算成视频内秒数。"""
    if not video_start:
        return []
    entries = []
    period_start = None
    period_end = None
    header_re = re.compile(
        r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})'
        r'\s*至\s*'
        r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})'
    )
    event_re = re.compile(r'^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s+(.+?)\s*$')
    expanded_lines = []
    event_marker_re = re.compile(r'(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?=\s)')
    for raw in lines or []:
        line = re.sub(r'\s+', ' ', str(raw or "")).strip()
        if not line:
            continue
        header = header_re.search(line)
        if header and "记录如下" in line:
            try:
                values = list(map(int, header.groups()))
                period_start = datetime(*values[:6])
                period_end = datetime(*values[6:])
            except ValueError:
                period_start = None
                period_end = None
            continue
        markers = list(event_marker_re.finditer(line))
        if not markers:
            continue
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(line)
            expanded_lines.append((
                line[marker.start():end].strip(),
                period_start,
                period_end,
            ))

    for line, line_period_start, line_period_end in expanded_lines:
        match = event_re.match(line)
        if not match:
            continue
        h, minute, sec, text = match.groups()
        second = int(sec or 0)
        event_time = (int(h), int(minute), second)
        try:
            if line_period_start and line_period_end:
                event_dt = datetime(
                    line_period_start.year, line_period_start.month, line_period_start.day,
                    *event_time,
                )
                if event_dt < line_period_start:
                    event_dt += timedelta(days=1)
            else:
                candidates = [
                    datetime.combine(video_start.date() + timedelta(days=offset), datetime.min.time()).replace(
                        hour=event_time[0], minute=event_time[1], second=event_time[2]
                    )
                    for offset in (-1, 0, 1)
                ]
                event_dt = min(candidates, key=lambda item: abs((item - video_start).total_seconds()))
        except ValueError:
            continue
        elapsed = int((event_dt - video_start).total_seconds())
        if elapsed < 0:
            continue
        stars = text.count("⭐") + text.count("★")
        clean_text = re.sub(r'[⭐★]+', '', text).strip()
        clean_text = clean_text.strip(" -—，,。")
        if not clean_text:
            continue
        entries.append({
            "start": elapsed,
            "clock": f"{event_dt:%Y-%m-%d %H:%M:%S}",
            "text": clean_text,
            "stars": stars,
            "highlight": stars > 0,
            "source": "manual_timeline",
        })
    return entries


def _parse_elapsed_timeline_report_lines(lines):
    """解析明确声明为视频内时间的旧报告，作为低权重参考候选。"""
    normalized_lines = [str(line or "").strip() for line in lines or []]
    has_elapsed_time_basis = any(
        "时间基准" in line and ("视频内时间" in line or "播放进度" in line)
        for line in normalized_lines
    )
    if not has_elapsed_time_basis:
        return []

    time_pattern = r'\d{1,3}:\d{2}(?::\d{2})?'
    topic_re = re.compile(
        rf'^\s*(?:[①-⑳㉑-㊿]|\d+[.、)])?\s*'
        rf'\[\s*(?P<start>{time_pattern})\s*[－—–~-]\s*'
        rf'(?P<end>{time_pattern})\s*\]\s*(?P<title>.+?)\s*$'
    )
    entries = []
    current = None
    for line in normalized_lines:
        match = topic_re.match(line)
        if match:
            try:
                start = parse_hms(match.group("start"))
                end = parse_hms(match.group("end"))
            except (TypeError, ValueError):
                current = None
                continue
            if end <= start:
                current = None
                continue
            raw_title = match.group("title")
            stars = raw_title.count("⭐") + raw_title.count("★")
            if "✂" in raw_title:
                stars = max(stars, 1)
            title = re.sub(r'[✂⭐★]\ufe0f?', '', raw_title).strip(" -—，,。")
            if not title:
                current = None
                continue
            current = {
                "start": start,
                "end": end,
                "clock": "视频内时间",
                "text": title,
                "stars": stars,
                "highlight": stars > 0,
                "source": "elapsed_report_reference",
                "time_basis": "video_elapsed_seconds",
                "explicit_range": True,
            }
            entries.append(current)
            continue
        if current and _TIMELINE_ACCOUNT_PREFIX_RE.match(line):
            current["reference_publish_title"] = line
    return entries


def _filter_manual_timeline_entries(entries, video_duration, end_margin_sec=MANUAL_TIMELINE_END_MARGIN_SEC):
    """只保留当前分段视频范围内的人工时间轴记录。"""
    if not video_duration or video_duration <= 0:
        return list(entries or [])
    max_start = float(video_duration) + max(0, end_margin_sec)
    return [item for item in entries or [] if 0 <= float(item.get("start", -1)) <= max_start]


def load_manual_timeline(video_path, timeline_dir=MANUAL_TIMELINE_DIR, manual_timeline_path=None):
    """加载人工时间轴 docx；manual_timeline_path=None 自动匹配，'__none__' 禁用。"""
    video_start = _extract_video_start_datetime(video_path)
    if manual_timeline_path == "__none__":
        return {"path": None, "entries": [], "video_start": video_start, "mode": "disabled"}
    if manual_timeline_path:
        doc_path = manual_timeline_path if os.path.isfile(manual_timeline_path) else None
    else:
        doc_path = _find_manual_timeline_doc(video_path, timeline_dir)
    if not doc_path:
        return {"path": None, "entries": [], "video_start": video_start, "mode": "manual" if manual_timeline_path else "auto"}
    lines = read_docx_lines(doc_path)
    entries = _parse_manual_timeline_lines(lines, video_start)
    time_basis = "wall_clock_converted_to_video_elapsed_seconds" if entries else None
    if not entries:
        entries = _parse_elapsed_timeline_report_lines(lines)
        if entries:
            time_basis = "video_elapsed_seconds"
    return {
        "path": doc_path,
        "entries": entries,
        "video_start": video_start,
        "mode": "manual" if manual_timeline_path else "auto",
        "time_basis": time_basis,
    }


def _manual_timeline_summary(manual_timeline):
    """返回可 JSON 序列化的人工时间轴摘要，避免 Web SSE 返回 datetime。"""
    manual_timeline = manual_timeline or {}
    entries = manual_timeline.get("entries") or []
    video_start = manual_timeline.get("video_start")
    if isinstance(video_start, datetime):
        video_start = video_start.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "path": manual_timeline.get("path"),
        "entry_count": len(entries),
        "source_entry_count": manual_timeline.get("source_entry_count", len(entries)),
        "raw_entry_count": manual_timeline.get("raw_entry_count", len(entries)),
        "optimized_entry_count": manual_timeline.get("optimized_entry_count"),
        "optimized_json_path": manual_timeline.get("optimized_json_path"),
        "optimized_md_path": manual_timeline.get("optimized_md_path"),
        "optimization_warning": manual_timeline.get("optimization_warning"),
        "star_count": sum(1 for item in entries if item.get("stars", 0) > 0),
        "video_start": video_start,
        "time_basis": manual_timeline.get("time_basis") or (
            entries[0].get("time_basis", "wall_clock_converted_to_video_elapsed_seconds")
            if entries else None
        ),
    }


def _manual_alignment_text(text):
    return "".join(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]+', str(text or ""))).lower()


def _manual_alignment_score(reference, candidate):
    """用二元字组和最长公共片段匹配人工概述与噪声 ASR。"""
    reference = _manual_alignment_text(reference)
    candidate = _manual_alignment_text(candidate)
    if len(reference) < 2 or len(candidate) < 2:
        return 0.0
    reference_grams = {reference[index:index + 2] for index in range(len(reference) - 1)}
    candidate_grams = {candidate[index:index + 2] for index in range(len(candidate) - 1)}
    overlap = len(reference_grams & candidate_grams)
    if not overlap:
        return 0.0
    f1 = 2 * overlap / (len(reference_grams) + len(candidate_grams))
    recall = overlap / len(reference_grams)
    longest = difflib.SequenceMatcher(
        None,
        reference,
        candidate,
        autojunk=False,
    ).find_longest_match().size
    longest_ratio = longest / max(8, min(len(reference), 40))
    return 0.35 * f1 + 0.35 * recall + 0.30 * min(1.0, longest_ratio)


def _manual_semantic_core(text):
    """移除称呼和叙述套话，只保留可用于核对事件的词面锚点。"""
    value = _manual_alignment_text(text)
    profile = current_streamer_profile()
    generic_terms = (
        *_MANUAL_SEMANTIC_GENERIC_TERMS,
        *profile_identity_names(profile),
    )
    for phrase in generic_terms:
        value = value.replace(_manual_alignment_text(phrase), "")
    return value


def _manual_text_supports_candidate(reference, candidate):
    """保守判断人工原句是否真的支持 AI 改写，而非只处于附近时间。"""
    if _manual_alignment_score(reference, candidate) >= MANUAL_TIMELINE_GROUNDING_MIN_SCORE:
        return True
    reference_core = _manual_semantic_core(reference)
    candidate_core = _manual_semantic_core(candidate)
    if len(reference_core) < 2 or len(candidate_core) < 2:
        return False
    match = difflib.SequenceMatcher(
        None,
        reference_core,
        candidate_core,
        autojunk=False,
    ).find_longest_match()
    if match.size >= 3:
        return True
    reference_grams = {
        reference_core[index:index + 2]
        for index in range(len(reference_core) - 1)
    }
    candidate_grams = {
        candidate_core[index:index + 2]
        for index in range(len(candidate_core) - 1)
    }
    shared = (
        reference_grams & candidate_grams
    ) - _MANUAL_SEMANTIC_BIGRAM_STOPWORDS
    return len(shared) >= 2


def _srt_alignment_windows(srt_segments):
    """把整场字幕预聚合为固定窗口，供人工时间轴做宽范围模糊校时。"""
    segments = sorted(srt_segments or [], key=lambda item: (item[0], item[1]))
    if not segments:
        return []
    duration = int(math.ceil(max(end for _, end, _ in segments)))
    windows = []
    left = 0
    right = 0
    for start in range(0, duration + 1, MANUAL_TIMELINE_ALIGNMENT_STEP_SEC):
        end = start + MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC
        while left < len(segments) and segments[left][1] < start:
            left += 1
        right = max(right, left)
        while right < len(segments) and segments[right][0] <= end:
            right += 1
        text = "".join(item[2] for item in segments[left:right])
        if text:
            windows.append((start, end, text))
    return windows


def _align_manual_timeline_entries_to_srt(entries, srt_segments):
    """在原墙钟点前后十分钟搜索字幕证据，修正人工记录的粗略锚点。"""
    windows = _srt_alignment_windows(srt_segments)
    if not windows:
        return [dict(entry) for entry in entries or []]
    aligned_entries = []
    for entry in entries or []:
        raw_start = int(entry.get("start", 0))
        nearby = [
            window for window in windows
            if abs((window[0] + MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC / 2) - raw_start)
            <= MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC
        ]
        best_window = None
        best_score = 0.0
        for window in nearby:
            content_score = _manual_alignment_score(entry.get("text", ""), window[2])
            distance = abs((window[0] + MANUAL_TIMELINE_ALIGNMENT_WINDOW_SEC / 2) - raw_start)
            proximity_bonus = 0.02 * max(
                0.0,
                1.0 - distance / MANUAL_TIMELINE_ALIGNMENT_SEARCH_SEC,
            )
            score = content_score + proximity_bonus
            if score > best_score:
                best_window = window
                best_score = score
        fixed = dict(entry)
        fixed["original_start"] = raw_start
        fixed["alignment_score"] = round(best_score, 4)
        if best_window and best_score >= MANUAL_TIMELINE_ALIGNMENT_MIN_SCORE:
            fixed["start"] = int(best_window[0])
            fixed["alignment_shift_sec"] = int(best_window[0] - raw_start)
            fixed["alignment_source"] = "subtitle_fuzzy_match"
        else:
            fixed["alignment_shift_sec"] = 0
            fixed["alignment_source"] = "wall_clock_fallback"
        aligned_entries.append(fixed)
    return sorted(aligned_entries, key=lambda item: (item["start"], item.get("original_start", 0)))


def parse_hms(s):
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return int(parts[0]) * 60 + int(parts[1])
