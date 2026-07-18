"""剪映字幕校对、样式预览与视频压制工作流。"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path


SUBTITLE_REVIEW_VERSION = 3
SUBTITLE_REVIEW_BATCH_SIZE = 30
SUBTITLE_REVIEW_CONTEXT_CUES = 3
SUBTITLE_REVIEW_CONCURRENCY = 2

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

EXACT_SUBTITLE_FONT = "Noto Sans S Chinese Black"
EXACT_SUBTITLE_FONT_RESOLVED = "NotoSansHans-Black"
DEFAULT_SUBTITLE_STYLE = {
    "font_name": EXACT_SUBTITLE_FONT,
    "font_size": 20.0,
    "font_color": "ffffff",
    "outline_color": "d06e95",
    "outline_width": 100.0,
    "x": 0.0,
    "y": -788.0,
    "shadow": 0.0,
}
DEFAULT_VIDEO_EXPORT = {
    "width": 1920,
    "height": 1080,
    "bitrate_kbps": 8000,
    "rate_control": "vbr",
    "codec": "h264",
    "container": "mp4",
    "fps": 60.0,
    "color_space": "bt709",
    "color_range": "tv",
    "audio": "copy",
}
_JIANYING_FONT_TO_1080_ASS = 6.75
_JIANYING_OUTLINE_TO_1080_ASS = 0.0533333333


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
    _atomic_write_text(destination, serialise_srt(cues, updates))
    return str(destination)


def _is_generated_stem(stem):
    return (
        stem.endswith(".part")
        or any(stem.endswith(suffix) for suffix in _GENERATED_SUBTITLE_SUFFIXES)
    )


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


def _validated_cached_review(
        cached, srt_path, cues, fingerprint, context_title, glossary, cache_path):
    """只接受由当前规则和当前字幕生成的完整缓存。"""
    if not isinstance(cached, dict):
        return None
    try:
        version = int(cached.get("version"))
        cue_count = int(cached.get("cue_count"))
    except (TypeError, ValueError):
        return None
    if version != SUBTITLE_REVIEW_VERSION or cue_count != len(cues):
        return None
    if cached.get("source_fingerprint") != fingerprint:
        return None
    if str(cached.get("context_title", "")) != str(context_title or ""):
        return None
    if cached.get("glossary") != list(glossary):
        return None
    cached_source = cached.get("source_srt_path")
    if not cached_source or os.path.normcase(os.path.abspath(cached_source)) != os.path.normcase(
            os.path.abspath(srt_path)):
        return None

    raw_suggestions = cached.get("suggestions")
    if not isinstance(raw_suggestions, list):
        return None
    cue_by_index = {cue.index: cue for cue in cues}
    target_indices = set(cue_by_index)
    suggestions = []
    seen_indices = set()
    for item in raw_suggestions:
        suggestion = _normalise_suggestion(item, cue_by_index, target_indices)
        if suggestion is None or suggestion["index"] in seen_indices:
            return None
        seen_indices.add(suggestion["index"])
        suggestions.append(suggestion)

    return {
        "version": SUBTITLE_REVIEW_VERSION,
        "source_srt_path": str(Path(srt_path)),
        "source_fingerprint": fingerprint,
        "context_title": str(context_title or ""),
        "cue_count": len(cues),
        "glossary": list(glossary),
        "suggestions": sorted(suggestions, key=lambda item: item["index"]),
        "cache_path": str(cache_path),
        "cache_hit": True,
    }


def _review_prompt(cues, target_indices, context_title, glossary, compact=False):
    cue_rows = [
        {"index": cue.index, "text": cue.text}
        for cue in cues
    ]
    rules = (
        "只修正能从上下文确认的错别字、同音误识别、专名和断词错误。"
        "禁止润色、改写语气、删除口头重复、增补标点或猜测听不清内容。"
        "原文若是语义成立的常用词，不能只因视频标题或优先词表就替换成同主题词。"
        "没有错误的字幕不要放入 corrections。original 必须逐字复制输入原文。"
    )
    if compact:
        rules = (
            "只改确定错字和专名；不润色、不改标点、不删口癖；"
            "不能仅凭标题或词表替换语义成立的常用词；original 必须与输入完全一致。"
        )
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


def _default_llm_runner(prompt, compact_prompt, retry_coordinator=None):
    from topic_engine import _call_llm_with_retry, _extract_json_payload

    call_kwargs = {
        "compact_prompt": compact_prompt,
        "max_tokens": 12000,
        "compact_max_tokens": 12000,
        "attempts": 3,
        "progress_label": "字幕 AI 校对",
        "require_json": True,
    }
    if retry_coordinator is not None:
        call_kwargs["retry_coordinator"] = retry_coordinator
    response = _call_llm_with_retry(
        prompt,
        **call_kwargs,
    )
    return _extract_json_payload(response)


def _build_default_llm_runner():
    from topic_engine import _LLMProviderRetryCoordinator

    retry_coordinator = _LLMProviderRetryCoordinator()

    def run(prompt, compact_prompt):
        return _default_llm_runner(
            prompt,
            compact_prompt,
            retry_coordinator=retry_coordinator,
        )

    return run


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
        raw_reviewed = payload.get("reviewed_indices")
        raw_corrections = payload.get("corrections")
        if not isinstance(raw_reviewed, list):
            last_error = "AI 返回的 reviewed_indices 不是数组"
            continue
        if not isinstance(raw_corrections, list) or any(
                not isinstance(item, dict) for item in raw_corrections):
            last_error = "AI 返回的 corrections 不是对象数组"
            continue
        try:
            reviewed_values = [int(value) for value in raw_reviewed]
        except (TypeError, ValueError):
            reviewed_values = []
        reviewed = sorted(set(reviewed_values))
        if len(reviewed_values) != len(reviewed):
            last_error = "AI 返回了重复或无效的已检查序号"
            continue
        if reviewed != sorted(target_indices):
            last_error = "AI 未确认完整检查本批字幕"
            continue
        suggestions = []
        correction_indices = set()
        malformed_correction = False
        for item in raw_corrections:
            if not {"index", "original", "corrected"}.issubset(item):
                malformed_correction = True
                break
            if not isinstance(item.get("original"), str) or not isinstance(
                    item.get("corrected"), str):
                malformed_correction = True
                break
            try:
                correction_index = int(item.get("index"))
            except (TypeError, ValueError):
                malformed_correction = True
                break
            if correction_index in correction_indices:
                malformed_correction = True
                break
            correction_indices.add(correction_index)
            suggestion = _normalise_suggestion(item, cue_by_index, set(target_indices))
            if suggestion:
                suggestions.append(suggestion)
        if malformed_correction:
            last_error = "AI 返回了缺字段、类型错误或重复的修正项"
            continue
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
            validated = _validated_cached_review(
                cached,
                srt_path,
                cues,
                fingerprint,
                context_title,
                active_glossary,
                cache_path,
            )
            if validated:
                return validated
        except (OSError, ValueError, TypeError):
            pass

    runner = llm_runner if llm_runner is not None else _build_default_llm_runner()
    suggestions_by_index = {}
    batch_specs = []
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
        batch_specs.append((batch_number, context_cues, target_indices))

    total_batches = len(batch_specs)
    batch_results = [None] * total_batches

    def review_spec(spec):
        batch_number, context_cues, target_indices = spec
        return batch_number, _review_batch(
            context_cues,
            target_indices,
            context_title,
            active_glossary,
            runner,
        )

    if total_batches == 1:
        if progress_callback:
            progress_callback("字幕 AI 校对 (1/1)...", 0, 1)
        _, batch_results[0] = review_spec(batch_specs[0])
    else:
        worker_count = min(SUBTITLE_REVIEW_CONCURRENCY, total_batches)
        if progress_callback:
            progress_callback(
                f"字幕 AI 校对并行处理中 (0/{total_batches})...",
                0,
                total_batches,
            )
        with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="autoslice-subtitle-review") as executor:
            futures = {
                executor.submit(review_spec, spec): spec[0]
                for spec in batch_specs
            }
            completed = 0
            for future in as_completed(futures):
                batch_number, batch_suggestions = future.result()
                batch_results[batch_number - 1] = batch_suggestions
                completed += 1
                if progress_callback:
                    progress_callback(
                        f"字幕 AI 校对并行处理中 ({completed}/{total_batches})...",
                        completed,
                        total_batches,
                    )

    for batch_suggestions in batch_results:
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
    if _subtitle_source_fingerprint(srt_path, context_title, active_glossary) != fingerprint:
        raise RuntimeError("源字幕在 AI 检查期间已变化，请重新检查")
    _atomic_write_text(
        cache_path,
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    if progress_callback:
        progress_callback("字幕 AI 校对完成", total_batches, total_batches)
    return result


def high_confidence_corrections(review_result, minimum_confidence=0.95):
    """返回可默认勾选的保守修正；增删字符的建议必须人工确认。"""
    selected = []
    for item in (review_result or {}).get("suggestions", []):
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        original = _semantic_text(str(item.get("original", "")))
        corrected = _semantic_text(str(item.get("corrected", "")))
        if confidence < float(minimum_confidence) or len(original) != len(corrected):
            continue
        matcher = difflib.SequenceMatcher(None, original, corrected)
        changed_chars = 0
        safe_replacements_only = True
        for tag, start_a, end_a, start_b, end_b in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag != "replace" or end_a - start_a != end_b - start_b:
                safe_replacements_only = False
                break
            changed_chars += end_a - start_a
        maximum_changes = 1 if len(original) <= 8 else min(
            3,
            max(2, (len(original) + 6) // 7),
        )
        if safe_replacements_only and 0 < changed_chars <= maximum_changes:
            selected.append(item)
    return selected


def normalise_subtitle_style(style=None):
    """校验剪映参数；指定字体固定为用户确认的精确字体。"""
    values = dict(DEFAULT_SUBTITLE_STYLE)
    values.update(style or {})
    if str(values.get("font_name", "")).strip() != EXACT_SUBTITLE_FONT:
        raise ValueError(f"字幕字体必须是 {EXACT_SUBTITLE_FONT}")
    for key, minimum, maximum in (
        ("font_size", 1.0, 30.0),
        ("outline_width", 0.0, 100.0),
        ("x", -1000.0, 1000.0),
        ("y", -1000.0, 1000.0),
        ("shadow", 0.0, 100.0),
    ):
        try:
            values[key] = float(values[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"字幕样式 {key} 必须是数字") from exc
        if not minimum <= values[key] <= maximum:
            raise ValueError(f"字幕样式 {key} 超出范围")
    for key in ("font_color", "outline_color"):
        color = str(values.get(key, "")).strip().lstrip("#").lower()
        if not re.fullmatch(r"[0-9a-f]{6}", color):
            raise ValueError(f"字幕样式 {key} 必须是 6 位十六进制颜色")
        values[key] = color
    values["font_name"] = EXACT_SUBTITLE_FONT
    return values


def normalise_video_export(settings=None):
    """校验剪映视频导出参数。"""
    values = dict(DEFAULT_VIDEO_EXPORT)
    values.update(settings or {})
    for key, minimum, maximum in (
        ("width", 320, 7680),
        ("height", 180, 4320),
        ("bitrate_kbps", 500, 100000),
    ):
        try:
            values[key] = int(values[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"视频导出参数 {key} 必须是整数") from exc
        if not minimum <= values[key] <= maximum:
            raise ValueError(f"视频导出参数 {key} 超出范围")
    try:
        values["fps"] = float(values["fps"])
    except (TypeError, ValueError) as exc:
        raise ValueError("视频导出参数 fps 必须是数字") from exc
    if not 1 <= values["fps"] <= 240:
        raise ValueError("视频导出参数 fps 超出范围")
    fixed_values = {
        "rate_control": "vbr",
        "codec": "h264",
        "container": "mp4",
        "color_space": "bt709",
        "color_range": "tv",
        "audio": "copy",
    }
    for key, expected in fixed_values.items():
        if str(values.get(key, "")).lower() != expected:
            raise ValueError(f"视频导出参数 {key} 必须是 {expected}")
        values[key] = expected
    return values


def _probe_video_info(video_path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(video_path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    if not video_stream:
        raise ValueError("视频文件没有画面流")
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    duration = float(
        payload.get("format", {}).get("duration")
        or video_stream.get("duration")
        or 0
    )
    if width <= 0 or height <= 0 or duration <= 0:
        raise ValueError("无法读取视频分辨率或时长")
    return {
        "width": width,
        "height": height,
        "duration": duration,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        "video_codec": video_stream.get("codec_name", ""),
        "fps": _parse_frame_rate(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        "bit_rate": int(video_stream.get("bit_rate") or 0),
        "pixel_format": video_stream.get("pix_fmt", ""),
        "color_range": video_stream.get("color_range", ""),
        "color_space": video_stream.get("color_space", ""),
        "color_transfer": video_stream.get("color_transfer", ""),
        "color_primaries": video_stream.get("color_primaries", ""),
    }


def _parse_frame_rate(value):
    if not value:
        return 0.0
    if "/" in str(value):
        numerator, denominator = str(value).split("/", 1)
        try:
            return float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _html_color_to_ass(color):
    value = color.lstrip("#")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H00{blue}{green}{red}".upper()


def _ass_timestamp(seconds):
    centiseconds = max(0, int(round(float(seconds) * 100)))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def _escape_ass_text(text):
    return (
        str(text)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\r", r"\N")
        .replace("\n", r"\N")
    )


def _style_geometry(style, width, height):
    scale = float(height) / 1080.0
    return {
        "font_size": round(style["font_size"] * _JIANYING_FONT_TO_1080_ASS * scale, 2),
        "outline": round(style["outline_width"] * _JIANYING_OUTLINE_TO_1080_ASS * scale, 2),
        "shadow": round(style["shadow"] * _JIANYING_OUTLINE_TO_1080_ASS * scale, 2),
        "x": int(round(width / 2.0 + style["x"] / 1000.0 * width / 2.0)),
        "y": int(round(height / 2.0 - style["y"] / 1000.0 * height / 2.0)),
        "margin": max(10, int(round(width * 0.04))),
    }


def build_ass_document(cues, width, height, style=None):
    """把 SRT 内容和剪映样式参数转换为分辨率自适应 ASS。"""
    active_style = normalise_subtitle_style(style)
    geometry = _style_geometry(active_style, width, height)
    primary = _html_color_to_ass(active_style["font_color"])
    outline = _html_color_to_ass(active_style["outline_color"])
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {int(width)}
PlayResY: {int(height)}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{EXACT_SUBTITLE_FONT},{geometry['font_size']},{primary},{primary},{outline},&H00000000,-1,0,0,0,100,100,0,0,1,{geometry['outline']},{geometry['shadow']},5,{geometry['margin']},{geometry['margin']},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    position = rf"{{\an5\pos({geometry['x']},{geometry['y']})}}"
    events = []
    for cue in cues:
        events.append(
            "Dialogue: 0,"
            f"{_ass_timestamp(cue.start_seconds)},{_ass_timestamp(cue.end_seconds)},"
            f"Default,,0,0,0,,{position}{_escape_ass_text(cue.text)}"
        )
    return header + "\n".join(events) + "\n"


def _ass_output_path(srt_path):
    source = Path(srt_path)
    return source.with_name(f"{source.stem}_字幕样式.ass")


def _style_output_path(srt_path):
    source = Path(srt_path)
    return source.with_name(f"{source.stem}_字幕样式.json")


def _atomic_write_text(path, text):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_ass_from_srt(
        srt_path, video_path, style=None, output_path=None,
        canvas_width=None, canvas_height=None):
    """保存可复用 ASS 和样式 JSON，不覆盖 SRT。"""
    cues = parse_srt_document(srt_path)
    video_info = _probe_video_info(video_path)
    active_style = normalise_subtitle_style(style)
    render_width = int(canvas_width or video_info["width"])
    render_height = int(canvas_height or video_info["height"])
    ass_path = Path(output_path) if output_path else _ass_output_path(srt_path)
    style_path = _style_output_path(srt_path)
    _atomic_write_text(
        ass_path,
        build_ass_document(cues, render_width, render_height, active_style),
    )
    _atomic_write_text(
        style_path,
        json.dumps(active_style, ensure_ascii=False, indent=2) + "\n",
    )
    return {
        "ass_path": str(ass_path),
        "style_path": str(style_path),
        "style": active_style,
        "video_info": video_info,
        "canvas_width": render_width,
        "canvas_height": render_height,
    }


def _ffmpeg_filter_path(path):
    value = str(Path(path).resolve()).replace("\\", "/")
    value = value.replace(":", r"\:").replace("'", r"\'")
    return f"'{value}'"


def _ass_filter(ass_path):
    return f"ass={_ffmpeg_filter_path(ass_path)}"


@lru_cache(maxsize=1)
def verify_exact_subtitle_font():
    """让 libass 实际选字并确认没有回退到相近字体。"""
    with tempfile.TemporaryDirectory(prefix="autoslice_font_probe_") as td:
        ass_path = Path(td) / "font_probe.ass"
        cue = SubtitleCue(1, "00:00:00,000", "00:00:00,500", "", "字体检查")
        ass_path.write_text(build_ass_document([cue], 320, 180), encoding="utf-8")
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "verbose",
                "-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.5",
                "-vf", _ass_filter(ass_path), "-frames:v", "1",
                "-f", "null", os.devnull,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    log = result.stderr
    match = re.search(
        rf"fontselect:\s*\({re.escape(EXACT_SUBTITLE_FONT)},[^\n]+?\)\s*->\s*([^,\r\n]+)",
        log,
        re.IGNORECASE,
    )
    resolved = match.group(1).strip() if match else ""
    available = result.returncode == 0 and resolved.casefold() == EXACT_SUBTITLE_FONT_RESOLVED.casefold()
    return {
        "available": available,
        "requested": EXACT_SUBTITLE_FONT,
        "resolved": resolved,
        "expected_resolved": EXACT_SUBTITLE_FONT_RESOLVED,
    }


def _ensure_exact_subtitle_font():
    result = verify_exact_subtitle_font()
    if not result["available"]:
        raise RuntimeError(
            f"无法精确加载字幕字体 {EXACT_SUBTITLE_FONT}，"
            f"实际解析为 {result['resolved'] or '未知字体'}"
        )
    return result


def _video_filter_chain(ass_path, export_settings):
    width = export_settings["width"]
    height = export_settings["height"]
    fps = export_settings["fps"]
    return ",".join((
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
        f"fps={fps:g}",
        _ass_filter(ass_path),
        "format=yuv420p",
    ))


def render_subtitle_preview(
        video_path, srt_path, style=None, preview_time=None,
        export_settings=None):
    """渲染一张带真实字幕样式的视频帧，返回 JPEG 字节。"""
    _ensure_exact_subtitle_font()
    cues = parse_srt_document(srt_path)
    if not cues:
        raise ValueError("字幕文件没有有效内容")
    video_info = _probe_video_info(video_path)
    active_export = normalise_video_export(export_settings)
    selected_time = (
        float(preview_time)
        if preview_time is not None
        else (cues[0].start_seconds + cues[0].end_seconds) / 2.0
    )
    selected_time = max(0.0, min(selected_time, max(0.0, video_info["duration"] - 0.05)))
    with tempfile.TemporaryDirectory(prefix="autoslice_subtitle_preview_") as td:
        ass_path = Path(td) / "preview.ass"
        ass_path.write_text(
            build_ass_document(
                cues,
                active_export["width"],
                active_export["height"],
                style,
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(video_path), "-ss", f"{selected_time:.3f}",
                "-vf", _video_filter_chain(ass_path, active_export), "-frames:v", "1",
                "-q:v", "2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    if result.returncode != 0 or not result.stdout.startswith(b"\xff\xd8"):
        message = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"字幕预览生成失败: {message}")
    return result.stdout, selected_time


@lru_cache(maxsize=1)
def _nvenc_available():
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.2",
            "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", os.devnull,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _output_video_path(video_path):
    source = Path(video_path)
    return source.with_name(f"{source.stem}_字幕版.mp4")


def _encoder_arguments(encoder, export_settings):
    bitrate = f"{export_settings['bitrate_kbps']}k"
    maxrate = f"{int(round(export_settings['bitrate_kbps'] * 1.5))}k"
    buffer_size = f"{int(round(export_settings['bitrate_kbps'] * 2))}k"
    if encoder == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
            "-rc", "vbr", "-b:v", bitrate,
            "-maxrate", maxrate, "-bufsize", buffer_size,
        ]
    return [
        "-c:v", "libx264", "-preset", "medium", "-b:v", bitrate,
        "-maxrate", maxrate, "-bufsize", buffer_size,
    ]


def _run_subtitle_encode(command, duration, progress_callback=None):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line.startswith(("out_time_us=", "out_time_ms=")):
                    continue
                try:
                    elapsed = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    continue
                if progress_callback and duration > 0:
                    percent = min(99, max(0, int(elapsed / duration * 100)))
                    progress_callback(f"字幕压制中 ({percent}%)...", percent, 100)
        stderr = process.stderr.read() if process.stderr is not None else ""
        return_code = process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    if return_code != 0:
        raise RuntimeError(stderr.strip()[-1000:] or f"FFmpeg 返回 {return_code}")


def burn_subtitles(
        video_path, srt_path, style=None, output_path=None, encoder="auto",
        progress_callback=None, export_settings=None):
    """把校对字幕压制到新 MP4；优先 NVENC，失败自动回退 libx264。"""
    font_result = _ensure_exact_subtitle_font()
    video_info = _probe_video_info(video_path)
    active_export = normalise_video_export(export_settings)
    artifacts = write_ass_from_srt(
        srt_path,
        video_path,
        style,
        canvas_width=active_export["width"],
        canvas_height=active_export["height"],
    )
    destination = Path(output_path) if output_path else _output_video_path(video_path)
    if destination.suffix.lower() != ".mp4":
        raise ValueError("字幕版输出文件必须是 MP4")
    if destination.resolve() == Path(video_path).resolve():
        raise ValueError("字幕版输出不能覆盖原视频")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_name(destination.stem + ".part.mp4")
    if part_path.exists():
        part_path.unlink()

    selected_encoder = encoder
    if selected_encoder == "auto":
        selected_encoder = "h264_nvenc" if _nvenc_available() else "libx264"
    if selected_encoder not in {"h264_nvenc", "libx264"}:
        raise ValueError("不支持的字幕压制编码器")

    def make_command(active_encoder):
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-vf", _video_filter_chain(artifacts["ass_path"], active_export),
            "-map", "0:v:0", "-map", "0:a:0?",
        ]
        command.extend(_encoder_arguments(active_encoder, active_export))
        command.extend([
            "-r", f"{active_export['fps']:g}",
            "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv",
            "-bsf:v",
            "h264_metadata=colour_primaries=1:transfer_characteristics=1:"
            "matrix_coefficients=1:video_full_range_flag=0",
            "-c:a", "copy", "-movflags", "+faststart",
            "-max_muxing_queue_size", "4096", "-progress", "pipe:1", "-nostats",
            str(part_path),
        ])
        return command

    used_encoder = selected_encoder
    try:
        try:
            _run_subtitle_encode(
                make_command(selected_encoder),
                video_info["duration"],
                progress_callback,
            )
        except RuntimeError:
            if selected_encoder != "h264_nvenc":
                raise
            if part_path.exists():
                part_path.unlink()
            used_encoder = "libx264"
            if progress_callback:
                progress_callback("NVENC 压制失败，自动改用软件编码...", 0, 100)
            _run_subtitle_encode(
                make_command("libx264"),
                video_info["duration"],
                progress_callback,
            )
        output_info = _probe_video_info(part_path)
        if output_info["has_audio"] != video_info["has_audio"]:
            raise RuntimeError("字幕版视频音频流与原视频不一致")
        if abs(output_info["duration"] - video_info["duration"]) > 0.5:
            raise RuntimeError("字幕版视频时长误差超过 0.5 秒")
        if output_info["width"] != active_export["width"] or output_info["height"] != active_export["height"]:
            raise RuntimeError("字幕版视频分辨率不符合导出参数")
        if abs(output_info["fps"] - active_export["fps"]) > 0.05:
            raise RuntimeError("字幕版视频帧率不符合导出参数")
        if (
            output_info["color_space"] != "bt709"
            or output_info["color_transfer"] != "bt709"
            or output_info["color_primaries"] != "bt709"
        ):
            raise RuntimeError("字幕版视频不是 Rec.709 SDR")
        os.replace(part_path, destination)
    finally:
        if part_path.exists():
            part_path.unlink()
    if progress_callback:
        progress_callback("字幕压制完成", 100, 100)
    return {
        "output_video_path": str(destination),
        "ass_path": artifacts["ass_path"],
        "style_path": artifacts["style_path"],
        "style": artifacts["style"],
        "font": font_result,
        "encoder": used_encoder,
        "export_settings": active_export,
        "source_video_info": video_info,
        "output_video_info": output_info,
    }
