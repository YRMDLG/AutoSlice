"""剪映字幕校对、样式预览与视频压制工作流。"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path


SUBTITLE_REVIEW_VERSION = 1
SUBTITLE_REVIEW_BATCH_SIZE = 80
SUBTITLE_REVIEW_CONTEXT_CUES = 3

DEFAULT_SUBTITLE_GLOSSARY = (
    "泽音Melody",
    "音音",
    "音姐",
    "麻麻",
    "音悦生",
    "提督",
    "舰长",
    "SC",
    "娃衣",
    "雷欧奥特曼",
    "bangumi",
)

_TIME_LINE_RE = re.compile(
    r"^(?P<start>\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[,.]\d{3})(?P<settings>.*)$"
)
_TIMESTAMP_IN_TEXT_RE = re.compile(
    r"\d{1,3}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
    r"\d{1,3}:\d{2}:\d{2}[,.]\d{3}"
)
_PUNCTUATION_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_GENERATED_SUBTITLE_SUFFIXES = (
    "_校对",
    "_校对字幕",
    "_字幕版",
    "_字幕预览",
)


@dataclass(frozen=True)
class SubtitleCue:
    """一条严格保留序号和时间轴的 SRT 字幕。"""

    index: int
    start: str
    end: str
    settings: str
    text: str

    @property
    def start_seconds(self):
        return _srt_timestamp_seconds(self.start)

    @property
    def end_seconds(self):
        return _srt_timestamp_seconds(self.end)

    def to_dict(self):
        result = asdict(self)
        result["start_seconds"] = self.start_seconds
        result["end_seconds"] = self.end_seconds
        return result


def _read_subtitle_text(path):
    raw = Path(path).read_bytes()
    encodings = ("utf-8-sig", "utf-8", "gb18030")
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f"字幕编码无法识别: {path}")


def _srt_timestamp_seconds(value):
    parts = value.replace(".", ",").split(":")
    if len(parts) != 3:
        raise ValueError(f"无效 SRT 时间: {value}")
    second, millisecond = parts[2].split(",", 1)
    return (
        int(parts[0]) * 3600
        + int(parts[1]) * 60
        + int(second)
        + int(millisecond) / 1000.0
    )


def parse_srt_document(path):
    """解析完整 SRT；不清洗原文，不修正时间轴。"""
    text, _ = _read_subtitle_text(path)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n\ufeff")
    if not text.strip():
        raise ValueError("SRT 字幕为空")

    blocks = re.split(r"\n[ \t]*\n+", text)
    cues = []
    seen_indices = set()
    previous_start = -1.0
    for block_number, block in enumerate(blocks, 1):
        lines = block.split("\n")
        if len(lines) < 3:
            raise ValueError(f"SRT 第 {block_number} 块格式不完整")
        try:
            index = int(lines[0].strip())
        except ValueError as exc:
            raise ValueError(f"SRT 第 {block_number} 块序号无效") from exc
        if index in seen_indices:
            raise ValueError(f"SRT 序号重复: {index}")
        seen_indices.add(index)

        timing = _TIME_LINE_RE.match(lines[1].strip())
        if not timing:
            raise ValueError(f"SRT 第 {index} 条时间轴无效")
        start = timing.group("start").replace(".", ",")
        end = timing.group("end").replace(".", ",")
        start_seconds = _srt_timestamp_seconds(start)
        end_seconds = _srt_timestamp_seconds(end)
        if end_seconds <= start_seconds:
            raise ValueError(f"SRT 第 {index} 条结束时间不晚于开始时间")
        if start_seconds < previous_start:
            raise ValueError(f"SRT 第 {index} 条时间轴倒序")
        previous_start = start_seconds

        cue_text = "\n".join(lines[2:]).strip()
        if not cue_text:
            raise ValueError(f"SRT 第 {index} 条字幕为空")
        cues.append(
            SubtitleCue(
                index=index,
                start=start,
                end=end,
                settings=timing.group("settings") or "",
                text=cue_text,
            )
        )
    return cues


def serialise_srt(cues, text_updates=None):
    """生成 UTF-8 SRT，仅替换明确指定的字幕正文。"""
    updates = text_updates or {}
    blocks = []
    for cue in cues:
        text = str(updates.get(cue.index, cue.text)).strip()
        if not text:
            raise ValueError(f"SRT 第 {cue.index} 条修正后为空")
        blocks.append(
            f"{cue.index}\n{cue.start} --> {cue.end}{cue.settings}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _corrected_srt_path(source_srt_path):
    source = Path(source_srt_path)
    return source.with_name(f"{source.stem}_校对.srt")


def save_corrected_srt(source_srt_path, corrections, output_path=None):
    """校验并保存已确认修正；原 SRT 保持只读。"""
    cues = parse_srt_document(source_srt_path)
    cue_by_index = {cue.index: cue for cue in cues}
    updates = {}
    for item in corrections or []:
        if not isinstance(item, dict):
            raise ValueError("字幕修正项必须是对象")
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError) as exc:
            raise ValueError("字幕修正项缺少有效序号") from exc
        cue = cue_by_index.get(index)
        if cue is None:
            raise ValueError(f"字幕修正序号不存在: {index}")
        original = item.get("original")
        if original is not None and str(original) != cue.text:
            raise ValueError(f"第 {index} 条原文已变化，请重新检查")
        corrected = str(item.get("corrected", "")).strip()
        if not corrected:
            raise ValueError(f"第 {index} 条修正文为空")
        if _TIMESTAMP_IN_TEXT_RE.search(corrected):
            raise ValueError(f"第 {index} 条修正文包含时间轴")
        updates[index] = corrected

    destination = Path(output_path) if output_path else _corrected_srt_path(source_srt_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(destination.name + ".tmp")
    temp_path.write_text(serialise_srt(cues, updates), encoding="utf-8")
    os.replace(temp_path, destination)
    return str(destination)


def _is_generated_stem(stem):
    return any(stem.endswith(suffix) for suffix in _GENERATED_SUBTITLE_SUFFIXES)


def _pair_result(video_path, srt_path):
    directory = video_path.parent
    corrected_srt = _corrected_srt_path(srt_path)
    output_video = video_path.with_name(f"{video_path.stem}_字幕版.mp4")
    pair_key = "\n".join(
        (os.path.normcase(str(video_path.resolve())), os.path.normcase(str(srt_path.resolve())))
    )
    try:
        cue_count = len(parse_srt_document(srt_path))
        subtitle_error = ""
    except (OSError, ValueError) as exc:
        cue_count = 0
        subtitle_error = str(exc)
    return {
        "id": hashlib.sha256(pair_key.encode("utf-8")).hexdigest()[:16],
        "title": directory.name,
        "directory": str(directory),
        "video_name": video_path.name,
        "video_path": str(video_path),
        "srt_name": srt_path.name,
        "srt_path": str(srt_path),
        "cue_count": cue_count,
        "subtitle_error": subtitle_error,
        "corrected_srt_path": str(corrected_srt),
        "has_corrected_srt": corrected_srt.is_file(),
        "output_video_path": str(output_video),
        "has_output_video": output_video.is_file(),
    }


def scan_submission_pairs(root_dir):
    """递归扫描投稿目录，优先同名配对，单视频/单字幕目录允许异名配对。"""
    root = Path(root_dir)
    if not root.is_dir():
        raise ValueError("投稿目录不存在")

    pairs = []
    for directory, _, names in os.walk(root):
        folder = Path(directory)
        videos = sorted(
            path
            for path in (folder / name for name in names)
            if path.suffix.lower() in {".mp4", ".mov", ".mkv"}
            and not _is_generated_stem(path.stem)
        )
        subtitles = sorted(
            path
            for path in (folder / name for name in names)
            if path.suffix.lower() == ".srt" and not _is_generated_stem(path.stem)
        )
        if not videos or not subtitles:
            continue

        unmatched_videos = list(videos)
        unmatched_subtitles = list(subtitles)
        by_video_stem = {path.stem.casefold(): path for path in videos}
        by_srt_stem = {path.stem.casefold(): path for path in subtitles}
        for stem in sorted(set(by_video_stem) & set(by_srt_stem)):
            video = by_video_stem[stem]
            subtitle = by_srt_stem[stem]
            pairs.append(_pair_result(video, subtitle))
            unmatched_videos.remove(video)
            unmatched_subtitles.remove(subtitle)

        if len(unmatched_videos) == 1 and len(unmatched_subtitles) == 1:
            pairs.append(_pair_result(unmatched_videos[0], unmatched_subtitles[0]))

    return sorted(pairs, key=lambda item: (item["directory"].casefold(), item["video_name"].casefold()))


def _subtitle_source_fingerprint(srt_path, context_title, glossary):
    digest = hashlib.sha256()
    digest.update(Path(srt_path).read_bytes())
    digest.update(str(context_title or "").encode("utf-8"))
    digest.update(json.dumps(list(glossary), ensure_ascii=False).encode("utf-8"))
    digest.update(str(SUBTITLE_REVIEW_VERSION).encode("ascii"))
    return digest.hexdigest()


def _review_cache_path(srt_path):
    source = Path(srt_path)
    return source.with_name(f"{source.stem}_字幕校对建议.json")


def _review_prompt(cues, target_indices, context_title, glossary, compact=False):
    cue_rows = [
        {"index": cue.index, "text": cue.text}
        for cue in cues
    ]
    rules = (
        "只修正能从上下文确认的错别字、同音误识别、专名和断词错误。"
        "禁止润色、改写语气、删除口头重复、增补标点或猜测听不清内容。"
        "没有错误的字幕不要放入 corrections。original 必须逐字复制输入原文。"
    )
    if compact:
        rules = "只改确定错字和专名；不润色、不改标点、不删口癖；original 必须与输入完全一致。"
    return (
        "你是泽音Melody直播切片的字幕校对员。"
        f"视频标题：{context_title or '未提供'}\n"
        f"优先词表：{'、'.join(glossary)}\n"
        f"待检查序号：{json.dumps(target_indices, ensure_ascii=False)}\n"
        f"规则：{rules}\n"
        "必须只输出一个 JSON 对象，格式为："
        '{"reviewed_indices":[1,2],"corrections":['
        '{"index":1,"original":"原文","corrected":"修正文",'
        '"reason":"依据","confidence":0.95}]}。'
        "reviewed_indices 必须完整照抄全部待检查序号，即使没有任何修正。"
        "confidence 范围为 0 到 1。\n"
        f"字幕上下文：{json.dumps(cue_rows, ensure_ascii=False)}"
    )


def _default_llm_runner(prompt, compact_prompt):
    from topic_engine import _call_llm_with_retry, _extract_json_payload

    response = _call_llm_with_retry(
        prompt,
        compact_prompt=compact_prompt,
        max_tokens=4096,
        compact_max_tokens=3072,
        attempts=3,
        progress_label="字幕 AI 校对",
        require_json=True,
    )
    return _extract_json_payload(response)


def _normalise_review_payload(payload):
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            from topic_engine import _extract_json_payload
            payload = _extract_json_payload(payload)
    return payload if isinstance(payload, dict) else None


def _semantic_text(text):
    return _PUNCTUATION_RE.sub("", text or "")


def _normalise_suggestion(item, cue_by_index, target_indices):
    if not isinstance(item, dict):
        return None
    try:
        index = int(item.get("index"))
    except (TypeError, ValueError):
        return None
    if index not in target_indices or index not in cue_by_index:
        return None
    cue = cue_by_index[index]
    original = str(item.get("original", ""))
    corrected = str(item.get("corrected", "")).strip()
    if original != cue.text or not corrected or corrected == original:
        return None
    if _TIMESTAMP_IN_TEXT_RE.search(corrected):
        return None
    if _semantic_text(original) == _semantic_text(corrected):
        return None
    semantic_original = _semantic_text(original)
    semantic_corrected = _semantic_text(corrected)
    if not semantic_original or not semantic_corrected:
        return None
    length_delta = abs(len(semantic_original) - len(semantic_corrected))
    if length_delta > max(6, int(len(semantic_original) * 0.35)):
        return None
    similarity = difflib.SequenceMatcher(None, semantic_original, semantic_corrected).ratio()
    if similarity < 0.55:
        return None
    try:
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(item.get("reason", "")).strip() or "上下文错字修正"
    return {
        "index": index,
        "original": original,
        "corrected": corrected,
        "reason": reason,
        "confidence": round(confidence, 3),
        "start": cue.start,
        "end": cue.end,
    }


def _review_batch(cues, target_indices, context_title, glossary, llm_runner):
    cue_by_index = {cue.index: cue for cue in cues}
    prompt = _review_prompt(cues, target_indices, context_title, glossary, compact=False)
    compact_prompt = _review_prompt(cues, target_indices, context_title, glossary, compact=True)
    last_error = None
    for attempt in range(2):
        active_prompt = compact_prompt if attempt else prompt
        payload = _normalise_review_payload(llm_runner(active_prompt, compact_prompt))
        if not payload:
            last_error = "AI 未返回 JSON 对象"
            continue
        try:
            reviewed = sorted({int(value) for value in payload.get("reviewed_indices", [])})
        except (TypeError, ValueError):
            reviewed = []
        if reviewed != sorted(target_indices):
            last_error = "AI 未确认完整检查本批字幕"
            continue
        suggestions = []
        for item in payload.get("corrections", []) or []:
            suggestion = _normalise_suggestion(item, cue_by_index, set(target_indices))
            if suggestion:
                suggestions.append(suggestion)
        return suggestions
    raise RuntimeError(last_error or "字幕 AI 校对结果无效")


def suggest_subtitle_corrections(
        srt_path, context_title="", glossary=None, llm_runner=None,
        use_cache=True, progress_callback=None):
    """逐批检查字幕并返回建议；不修改原始字幕。"""
    cues = parse_srt_document(srt_path)
    active_glossary = tuple(
        dict.fromkeys(str(item).strip() for item in (glossary or DEFAULT_SUBTITLE_GLOSSARY) if str(item).strip())
    )
    fingerprint = _subtitle_source_fingerprint(srt_path, context_title, active_glossary)
    cache_path = _review_cache_path(srt_path)
    if use_cache and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("source_fingerprint") == fingerprint:
                cached["cache_hit"] = True
                return cached
        except (OSError, ValueError, TypeError):
            pass

    runner = llm_runner or _default_llm_runner
    suggestions_by_index = {}
    total_batches = max(1, (len(cues) + SUBTITLE_REVIEW_BATCH_SIZE - 1) // SUBTITLE_REVIEW_BATCH_SIZE)
    for batch_number, target_start in enumerate(
            range(0, len(cues), SUBTITLE_REVIEW_BATCH_SIZE), 1):
        target_cues = cues[target_start:target_start + SUBTITLE_REVIEW_BATCH_SIZE]
        context_start = max(0, target_start - SUBTITLE_REVIEW_CONTEXT_CUES)
        context_end = min(
            len(cues),
            target_start + SUBTITLE_REVIEW_BATCH_SIZE + SUBTITLE_REVIEW_CONTEXT_CUES,
        )
        context_cues = cues[context_start:context_end]
        target_indices = [cue.index for cue in target_cues]
        if progress_callback:
            progress_callback(
                f"字幕 AI 校对 ({batch_number}/{total_batches})...",
                batch_number - 1,
                total_batches,
            )
        batch_suggestions = _review_batch(
            context_cues,
            target_indices,
            context_title,
            active_glossary,
            runner,
        )
        for suggestion in batch_suggestions:
            current = suggestions_by_index.get(suggestion["index"])
            if current is None or suggestion["confidence"] > current["confidence"]:
                suggestions_by_index[suggestion["index"]] = suggestion

    result = {
        "version": SUBTITLE_REVIEW_VERSION,
        "source_srt_path": str(Path(srt_path)),
        "source_fingerprint": fingerprint,
        "context_title": context_title,
        "cue_count": len(cues),
        "glossary": list(active_glossary),
        "suggestions": [suggestions_by_index[index] for index in sorted(suggestions_by_index)],
        "cache_path": str(cache_path),
        "cache_hit": False,
    }
    temp_path = cache_path.with_name(cache_path.name + ".tmp")
    temp_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, cache_path)
    if progress_callback:
        progress_callback("字幕 AI 校对完成", total_batches, total_batches)
    return result


def high_confidence_corrections(review_result, minimum_confidence=0.88):
    """返回默认勾选的高置信度修正，最终仍由用户确认。"""
    return [
        item
        for item in (review_result or {}).get("suggestions", [])
        if float(item.get("confidence", 0)) >= float(minimum_confidence)
    ]
