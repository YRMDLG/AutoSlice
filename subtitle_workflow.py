"""剪映字幕校对、样式预览与视频压制工作流。"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path

from autoslice.llm import transport as llm_gateway
from autoslice.llm.prompts import PromptContext, build_title_hook_guide
from autoslice.transcription.contracts import (
    DEFAULT_MAX_PUBLISH_TITLE_CHARS,
    DEFAULT_SUBTITLE_GLOSSARY,
    DEFAULT_SUBTITLE_MAX_CHARS,
    SubtitleCue,
    SubtitleTitleServices,
    normalise_generic_publish_title,
    srt_timestamp_seconds as _srt_timestamp_seconds,
)
from streamer_profiles import StreamerProfile, merge_profile_subtitle_glossary


SUBTITLE_REVIEW_VERSION = 5
SUBTITLE_ASR_VERSION = 2
SUBTITLE_EDIT_STATE_VERSION = 1
SUBTITLE_REVIEW_BATCH_SIZE = 30
SUBTITLE_REVIEW_CONTEXT_CUES = 3
SUBTITLE_REVIEW_CONCURRENCY = 2
SUBTITLE_TITLE_EVIDENCE_CHARS = 32000

_ATOMIC_WRITE_LOCK = threading.Lock()

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
    "_排版",
    "_校对",
    "_校对字幕",
    "_字幕版",
    "_字幕预览",
)
_SUBMISSION_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv"}

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


def _generic_title_style_prompt(_context_text="", compact=False):
    """未注入账号 profile 时不读取任何标题样本。"""
    return ""


def _generic_title_hook_prompt_guide():
    """以显式通用身份构造标题规则，不猜测账号前缀。"""
    context = PromptContext(
        streamer_display_name="主播",
        prompt_streamer_name="主播",
        editor_subject="所选主播",
        title_prefix_rule="不要添加账号专属方括号前缀",
        title_prefix_rule_quoted="不要添加账号专属方括号前缀",
        publish_title_example="具体事件钩子👀结果或反差",
    )
    return build_title_hook_guide(context)


DEFAULT_SUBTITLE_TITLE_SERVICES = SubtitleTitleServices(
    max_publish_title_chars=DEFAULT_MAX_PUBLISH_TITLE_CHARS,
    build_title_style_prompt=_generic_title_style_prompt,
    build_title_hook_prompt_guide=_generic_title_hook_prompt_guide,
    normalise_publish_title=normalise_generic_publish_title,
)


def _resolve_title_services(title_services=None):
    if title_services is None:
        return DEFAULT_SUBTITLE_TITLE_SERVICES
    if not isinstance(title_services, SubtitleTitleServices):
        raise TypeError("title_services 必须是 SubtitleTitleServices")
    return title_services


def _read_subtitle_text(path):
    raw = Path(path).read_bytes()
    encodings = ("utf-8-sig", "utf-8", "gb18030")
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError(f"字幕编码无法识别: {path}")


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


def _normalise_deleted_indices(cues, deleted_indices):
    """校验删除请求，并返回存在于源字幕中的序号集合。"""
    if deleted_indices is None:
        return set()
    if not isinstance(deleted_indices, (list, tuple, set, frozenset)):
        raise ValueError("删除字幕序号必须是数组")

    cue_indices = {cue.index for cue in cues}
    deleted = set()
    for index in deleted_indices:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("删除字幕序号必须是整数")
        if index in deleted:
            raise ValueError(f"删除字幕序号重复: {index}")
        if index not in cue_indices:
            raise ValueError(f"删除字幕序号不存在: {index}")
        deleted.add(index)
    if cues and len(deleted) == len(cues):
        raise ValueError("不能删除全部字幕")
    return deleted


def _normalise_merge_pairs(cues, merge_pairs, deleted_indices):
    """校验相邻字幕合并关系，并返回“后一条 -> 前一条”的映射。"""
    if merge_pairs is None:
        return {}
    if not isinstance(merge_pairs, (list, tuple)):
        raise ValueError("字幕合并关系必须是数组")

    cue_indices = [cue.index for cue in cues]
    cue_positions = {index: position for position, index in enumerate(cue_indices)}
    previous_by_child = {}
    child_by_previous = {}
    for item in merge_pairs:
        if not isinstance(item, dict):
            raise ValueError("字幕合并项必须是对象")
        try:
            first = int(item.get("first"))
            second = int(item.get("second"))
        except (TypeError, ValueError) as exc:
            raise ValueError("字幕合并项缺少有效序号") from exc
        if first not in cue_positions or second not in cue_positions:
            raise ValueError("字幕合并序号不存在")
        if first in deleted_indices or second in deleted_indices:
            raise ValueError("已删除字幕不能参与合并")
        if cue_positions[second] != cue_positions[first] + 1:
            raise ValueError("只能合并时间轴中相邻的字幕")
        if second in previous_by_child or first in child_by_previous:
            raise ValueError("同一字幕不能同时参与多个合并关系")
        previous_by_child[second] = first
        child_by_previous[first] = second
    return previous_by_child


def _normalise_merge_overrides(cues, previous_by_child, merge_overrides):
    """校验合并后的整段正文覆盖值，仅允许写在合并组首条。"""
    if merge_overrides is None:
        return {}
    if not isinstance(merge_overrides, dict):
        raise ValueError("合并字幕正文必须是对象")

    cue_indices = {cue.index for cue in cues}
    child_by_previous = {previous: child for child, previous in previous_by_child.items()}
    result = {}
    for raw_index, raw_text in merge_overrides.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("合并字幕正文序号无效") from exc
        if index not in cue_indices or index in previous_by_child:
            raise ValueError("合并字幕正文必须对应合并组首条")
        if index not in child_by_previous:
            raise ValueError("合并字幕正文没有对应的合并关系")
        text = str(raw_text or "").strip()
        if not text:
            raise ValueError("合并字幕正文不能为空")
        if _TIMESTAMP_IN_TEXT_RE.search(text):
            raise ValueError("合并字幕正文包含时间轴")
        result[index] = text
    return result


def _normalise_time_overrides(
        cues, previous_by_child, deleted_indices, time_overrides):
    """校验合并组首条的手工时间覆盖，并统一为 SRT 时间码。"""
    if time_overrides is None:
        return {}
    if not isinstance(time_overrides, dict):
        raise ValueError("字幕时间调整必须是对象")

    cue_by_index = {cue.index: cue for cue in cues}
    child_by_previous = {
        previous: child for child, previous in previous_by_child.items()
    }
    result = {}
    for raw_index, raw_value in time_overrides.items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("字幕时间调整序号无效") from exc
        if index not in cue_by_index:
            raise ValueError(f"字幕时间调整序号不存在: {index}")
        if index in previous_by_child:
            raise ValueError("字幕时间调整必须对应合并组首条")
        if index in deleted_indices:
            raise ValueError("已删除字幕不能调整时间")
        if not isinstance(raw_value, dict):
            raise ValueError(f"第 {index} 条字幕时间调整必须是对象")
        start_value = raw_value.get("start")
        end_value = raw_value.get("end")
        if isinstance(start_value, bool) or isinstance(end_value, bool):
            raise ValueError(f"第 {index} 条字幕时间必须是秒数")
        try:
            start_seconds = float(start_value)
            end_seconds = float(end_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {index} 条字幕时间必须是秒数") from exc
        if not math.isfinite(start_seconds) or not math.isfinite(end_seconds):
            raise ValueError(f"第 {index} 条字幕时间必须是有限数字")
        if start_seconds < 0:
            raise ValueError(f"第 {index} 条字幕开始时间不能小于 0")
        if end_seconds <= start_seconds:
            raise ValueError(f"第 {index} 条字幕结束时间必须晚于开始时间")

        members = [cue_by_index[index]]
        cursor = index
        while cursor in child_by_previous:
            cursor = child_by_previous[cursor]
            members.append(cue_by_index[cursor])
        result[index] = {
            "start": _srt_timestamp(start_seconds),
            "end": _srt_timestamp(end_seconds),
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "default_start": members[0].start,
            "default_end": members[-1].end,
        }
    return result


def _join_subtitle_text(left, right):
    """合并两条正文，保留原有停顿空格，避免把中文词组强行断开。"""
    raw_left = str(left or "")
    raw_right = str(right or "")
    left_text = raw_left.rstrip()
    right_text = raw_right.lstrip()
    if not left_text:
        return right_text
    if not right_text:
        return left_text
    if raw_left[-1:].isspace() or raw_right[:1].isspace():
        return f"{left_text} {right_text}"
    return left_text + right_text


def _merged_subtitle_cues(
        cues, text_updates, deleted_indices, previous_by_child, merge_overrides,
        time_overrides):
    """应用修改、删除和合并，生成新的连续 SRT cue 列表。"""
    cue_by_index = {cue.index: cue for cue in cues}
    child_by_previous = {previous: child for child, previous in previous_by_child.items()}
    output = []
    for cue in cues:
        if cue.index in deleted_indices or cue.index in previous_by_child:
            continue
        members = [cue]
        cursor = cue.index
        while cursor in child_by_previous:
            cursor = child_by_previous[cursor]
            members.append(cue_by_index[cursor])
        text = ""
        for member in members:
            text = _join_subtitle_text(
                text,
                text_updates.get(member.index, member.text),
            )
        text = merge_overrides.get(cue.index, text).strip()
        if not text:
            raise ValueError(f"第 {cue.index} 条合并后字幕为空")
        timing = time_overrides.get(cue.index, {})
        output.append(SubtitleCue(
            index=cue.index,
            start=timing.get("start", cue.start),
            end=timing.get("end", members[-1].end),
            settings=cue.settings,
            text=text,
        ))
    if cues and not output:
        raise ValueError("不能删除全部字幕")
    previous_start = -1.0
    for cue in output:
        if cue.start_seconds < previous_start:
            raise ValueError(f"第 {cue.index} 条字幕开始时间早于上一条，时间轴会倒序")
        previous_start = cue.start_seconds
    return output


def serialise_srt(
        cues, text_updates=None, deleted_indices=None, *, merge_pairs=None,
        merge_overrides=None, time_overrides=None):
    """生成 UTF-8 SRT，支持正文、删除、合并及时间调整。"""
    updates = text_updates or {}
    deleted = _normalise_deleted_indices(cues, deleted_indices)
    previous_by_child = _normalise_merge_pairs(cues, merge_pairs, deleted)
    overrides = _normalise_merge_overrides(
        cues,
        previous_by_child,
        merge_overrides,
    )
    timings = _normalise_time_overrides(
        cues,
        previous_by_child,
        deleted,
        time_overrides,
    )
    blocks = []
    for cue in _merged_subtitle_cues(
            cues, updates, deleted, previous_by_child, overrides, timings):
        blocks.append(
            f"{cue.index}\n{cue.start} --> {cue.end}{cue.settings}\n{cue.text}"
        )
    return "\n\n".join(blocks) + "\n"


def _corrected_srt_path(source_srt_path):
    source = Path(source_srt_path)
    return source.with_name(f"{source.stem}_校对.srt")


def _reflowed_srt_path(source_srt_path):
    source = Path(source_srt_path)
    return source.with_name(f"{source.stem}_排版.srt")


def _subtitle_edit_state_path(source_srt_path):
    source = Path(source_srt_path)
    return source.with_name(f"{source.stem}_校对状态.json")


def _subtitle_content_fingerprint(source_srt_path):
    return hashlib.sha256(Path(source_srt_path).read_bytes()).hexdigest()


def _srt_timestamp(seconds):
    total_milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


_SHORT_CLAUSE_INTERJECTIONS = frozenset({
    "啊", "呀", "哎", "唉", "哦", "嗯", "呃",
    "所以", "但是", "然后", "不过", "其实", "反正", "因为", "如果",
})
_CONTINUATION_SUFFIXES = (
    "越来越", "这个", "那个", "一个", "什么", "怎么", "不是", "就是",
    "可以", "没有", "我们", "你们", "他们", "然后", "因为", "所以",
    "但是", "已经", "正在", "还是", "真的", "感觉", "应该", "可能",
    "不会", "不要", "想要",
)
_SHORT_FRAGMENT_MAX_GAP_SECONDS = 1.0


def _visible_subtitle_char_count(text):
    return len(re.sub(r"\s+", "", str(text or "")))


def _short_clause_prefix(text):
    match = re.match(r"^([^\s]{1,2})\s+", str(text or "").lstrip())
    if not match:
        return ""
    prefix = match.group(1)
    return "" if prefix in _SHORT_CLAUSE_INTERJECTIONS else prefix


def _continuation_suffix_to_shift(text, prefix=""):
    stripped = str(text or "").rstrip()
    if not stripped:
        return ""
    last_space = max(stripped.rfind(" "), stripped.rfind("\t"))
    if last_space >= 0:
        tail = stripped[last_space + 1:]
        if 1 <= _visible_subtitle_char_count(tail) <= 3:
            return tail
    for suffix in _CONTINUATION_SUFFIXES:
        if stripped.endswith(suffix):
            return suffix
    # 没有命中词表时，仅对接近正常行长的上一条做保守回收：一字短头通常
    # 需要带回前三字，两字短头带回前两字，组成常见的四字左右短语。
    visible_chars = _visible_subtitle_char_count(stripped)
    generic_size = 3 if len(prefix) == 1 else 2
    if visible_chars >= 8 and len(stripped) >= generic_size:
        candidate = stripped[-generic_size:]
        if not re.search(r"\s", candidate):
            return candidate
    return ""


def _rebalance_short_leading_subtitle_fragments(cues, max_chars):
    """修复“短尾 + 空格 + 新句”式断句，保持所有 cue 的原时间范围。"""
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        return list(cues or [])
    result = list(cues or [])
    for index in range(1, len(result)):
        previous = result[index - 1]
        current = result[index]
        if (
                current.start_seconds - previous.end_seconds
                > _SHORT_FRAGMENT_MAX_GAP_SECONDS):
            continue
        prefix = _short_clause_prefix(current.text)
        movable = _continuation_suffix_to_shift(previous.text, prefix)
        if not prefix or not movable:
            continue
        previous_text = previous.text.rstrip()
        if not previous_text.endswith(movable):
            continue
        revised_previous = previous_text[:-len(movable)].rstrip()
        revised_current = movable + current.text.lstrip()
        if (
                _visible_subtitle_char_count(revised_previous) < 2
                or _visible_subtitle_char_count(revised_current) > max_chars):
            continue
        result[index - 1] = SubtitleCue(
            index=previous.index,
            start=previous.start,
            end=previous.end,
            settings=previous.settings,
            text=revised_previous,
        )
        result[index] = SubtitleCue(
            index=current.index,
            start=current.start,
            end=current.end,
            settings=current.settings,
            text=revised_current,
        )
    return result


def reflow_subtitle_srt_for_display(
        source_srt_path, output_path=None, max_chars=None):
    """无损拆分超长字幕，生成供校对和压制使用的排版副本。"""
    source = Path(source_srt_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("源字幕文件不存在")
    if source.suffix.casefold() != ".srt":
        raise ValueError("源字幕必须是 SRT")
    destination = (
        Path(output_path).expanduser().resolve()
        if output_path else _reflowed_srt_path(source)
    )
    if destination == source:
        raise ValueError("排版字幕不能覆盖源字幕")
    if (
            _corrected_srt_path(source).is_file()
            or _corrected_srt_path(destination).is_file()):
        raise ValueError("已有校对字幕，不能重新整理源字幕")

    if max_chars is None:
        # 与 FunASR 新生成工作字幕使用同一上限，避免旧字幕在校对页面再次过长。
        max_chars = DEFAULT_SUBTITLE_MAX_CHARS
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError) as exc:
        raise ValueError("字幕单行字数上限无效") from exc

    source_cues = parse_srt_document(source)
    reflowed_cues = []
    next_index = 1
    for cue in source_cues:
        for start, end, text in _split_cue_for_ass(cue, max_chars):
            reflowed_cues.append(SubtitleCue(
                index=next_index,
                start=_srt_timestamp(start),
                end=_srt_timestamp(end),
                settings=cue.settings,
                text=text,
            ))
            next_index += 1

    reflowed_cues = _rebalance_short_leading_subtitle_fragments(
        reflowed_cues,
        max_chars,
    )
    if not reflowed_cues:
        raise ValueError("源字幕没有可整理的内容")
    _atomic_write_text(destination, serialise_srt(reflowed_cues))
    return {
        "source_srt_path": str(source),
        "srt_path": str(destination),
        "corrected_srt_path": str(_corrected_srt_path(destination)),
        "cue_count": len(reflowed_cues),
        "split_count": len(reflowed_cues) - len(source_cues),
        "max_chars": max_chars,
    }


def save_corrected_srt(
        source_srt_path, corrections, output_path=None, *, deleted_indices=None,
        merge_pairs=None, merge_overrides=None, time_overrides=None):
    """校验并保存正文、删除、合并和时间调整；原 SRT 保持只读。"""
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

    deleted = _normalise_deleted_indices(cues, deleted_indices)
    conflicting_indices = sorted(set(updates) & deleted)
    if conflicting_indices:
        raise ValueError(
            f"第 {conflicting_indices[0]} 条字幕不能同时修改和删除"
        )

    previous_by_child = _normalise_merge_pairs(cues, merge_pairs, deleted)
    normalized_merge_overrides = _normalise_merge_overrides(
        cues,
        previous_by_child,
        merge_overrides,
    )
    normalized_time_overrides = _normalise_time_overrides(
        cues,
        previous_by_child,
        deleted,
        time_overrides,
    )
    destination = Path(output_path) if output_path else _corrected_srt_path(source_srt_path)
    _atomic_write_text(
        destination,
        serialise_srt(
            cues,
            updates,
            deleted_indices=deleted,
            merge_pairs=merge_pairs,
            merge_overrides=merge_overrides,
            time_overrides=time_overrides,
        ),
    )
    state_payload = {
        "version": SUBTITLE_EDIT_STATE_VERSION,
        "source_srt_path": str(Path(source_srt_path).resolve()),
        "source_fingerprint": _subtitle_content_fingerprint(source_srt_path),
        "corrected_srt_path": str(destination.resolve()),
        "corrected_fingerprint": _subtitle_content_fingerprint(destination),
        "corrections": [
            {
                "index": index,
                "original": cue_by_index[index].text,
                "corrected": corrected,
            }
            for index, corrected in sorted(updates.items())
        ],
        "deleted_indices": sorted(deleted),
        "merge_pairs": [
            {"first": first, "second": second}
            for second, first in sorted(previous_by_child.items())
        ],
        "merge_overrides": {
            str(index): text
            for index, text in sorted(normalized_merge_overrides.items())
        },
        "time_overrides": {
            str(index): {
                "start": timing["start_seconds"],
                "end": timing["end_seconds"],
            }
            for index, timing in sorted(normalized_time_overrides.items())
        },
    }
    _atomic_write_text(
        _subtitle_edit_state_path(source_srt_path),
        json.dumps(state_payload, ensure_ascii=False, indent=2),
    )
    return str(destination)


def load_subtitle_edit_state(source_srt_path):
    """读取与源字幕完全匹配的校对状态；旧产物或过期状态返回 None。"""

    source = Path(source_srt_path).resolve()
    state_path = _subtitle_edit_state_path(source)
    if not state_path.is_file():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version", 0)) != SUBTITLE_EDIT_STATE_VERSION:
            return None
        saved_source = Path(str(payload.get("source_srt_path", ""))).resolve()
        if os.path.normcase(str(saved_source)) != os.path.normcase(str(source)):
            return None
        if payload.get("source_fingerprint") != _subtitle_content_fingerprint(source):
            return None
        corrected = Path(str(payload.get("corrected_srt_path", ""))).resolve()
        if not corrected.is_file():
            return None
        if payload.get("corrected_fingerprint") != _subtitle_content_fingerprint(corrected):
            return None
        cues = parse_srt_document(source)
        deleted = _normalise_deleted_indices(cues, payload.get("deleted_indices", []))
        previous_by_child = _normalise_merge_pairs(
            cues,
            payload.get("merge_pairs", []),
            deleted,
        )
        normalized_merge_overrides = _normalise_merge_overrides(
            cues,
            previous_by_child,
            payload.get("merge_overrides", {}),
        )
        normalized_time_overrides = _normalise_time_overrides(
            cues,
            previous_by_child,
            deleted,
            payload.get("time_overrides", {}),
        )
        cue_by_index = {cue.index: cue for cue in cues}
        corrections = []
        for item in payload.get("corrections", []):
            if not isinstance(item, dict):
                return None
            index = int(item.get("index"))
            if index not in cue_by_index or index in deleted:
                return None
            if str(item.get("original", "")) != cue_by_index[index].text:
                return None
            corrected_text = str(item.get("corrected", "")).strip()
            if not corrected_text or _TIMESTAMP_IN_TEXT_RE.search(corrected_text):
                return None
            corrections.append({
                "index": index,
                "original": cue_by_index[index].text,
                "corrected": corrected_text,
            })
        return {
            "state_path": str(state_path),
            "corrected_srt_path": str(corrected),
            "corrections": corrections,
            "deleted_indices": sorted(deleted),
            "merge_pairs": [
                {"first": first, "second": second}
                for second, first in sorted(previous_by_child.items())
            ],
            "merge_overrides": {
                str(index): text
                for index, text in sorted(normalized_merge_overrides.items())
            },
            "time_overrides": {
                str(index): {
                    "start": timing["start_seconds"],
                    "end": timing["end_seconds"],
                }
                for index, timing in sorted(normalized_time_overrides.items())
            },
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _is_generated_stem(stem):
    return (
        stem.endswith(".part")
        or any(stem.endswith(suffix) for suffix in _GENERATED_SUBTITLE_SUFFIXES)
    )


def _path_timestamps(path):
    try:
        stat = Path(path).stat()
    except OSError:
        return 0.0, 0.0
    created_at = getattr(stat, "st_birthtime", stat.st_ctime)
    return float(created_at), float(stat.st_mtime)


def _pair_result(video_path, srt_path=None):
    directory = video_path.parent
    folder_created_at, folder_modified_at = _path_timestamps(directory)
    source_created_at, source_modified_at = _path_timestamps(video_path)
    raw_srt = srt_path or video_path.with_suffix(".srt")
    reflowed_srt = _reflowed_srt_path(raw_srt)
    expected_srt = reflowed_srt if reflowed_srt.is_file() else raw_srt
    has_source_srt = expected_srt.is_file()
    corrected_srt = _corrected_srt_path(expected_srt)
    output_video = video_path.with_name(f"{video_path.stem}_字幕版.mp4")
    pair_key = os.path.normcase(str(video_path.resolve()))
    cue_count = 0
    subtitle_error = ""
    if has_source_srt:
        try:
            cue_count = len(parse_srt_document(expected_srt))
        except (OSError, ValueError) as exc:
            subtitle_error = str(exc)
    return {
        "id": hashlib.sha256(pair_key.encode("utf-8")).hexdigest()[:16],
        "title": directory.name,
        "directory": str(directory),
        "folder_created_at": folder_created_at,
        "folder_modified_at": folder_modified_at,
        "source_created_at": source_created_at,
        "source_modified_at": source_modified_at,
        "video_name": video_path.name,
        "video_path": str(video_path),
        "raw_srt_path": str(raw_srt),
        "srt_name": expected_srt.name,
        "srt_path": str(expected_srt),
        "is_reflowed_srt": expected_srt == reflowed_srt,
        "has_source_srt": has_source_srt,
        "needs_transcription": not has_source_srt,
        "can_reflow_srt": (
            raw_srt.is_file()
            and not _corrected_srt_path(expected_srt).is_file()
            and not _corrected_srt_path(raw_srt).is_file()
        ),
        "cue_count": cue_count,
        "subtitle_error": subtitle_error,
        "corrected_srt_path": str(corrected_srt),
        "has_corrected_srt": corrected_srt.is_file(),
        "output_video_path": str(output_video),
        "has_output_video": output_video.is_file(),
    }


def scan_submission_pairs(root_dir):
    """递归扫描投稿视频；已有字幕优先配对，无字幕视频保留为待识别项。"""
    root = Path(root_dir)
    if not root.is_dir():
        raise ValueError("投稿目录不存在")

    pairs = []
    for directory, _, names in os.walk(root):
        folder = Path(directory)
        videos = sorted(
            path
            for path in (folder / name for name in names)
            if path.suffix.lower() in _SUBMISSION_VIDEO_SUFFIXES
            and not _is_generated_stem(path.stem)
        )
        subtitles = sorted(
            path
            for path in (folder / name for name in names)
            if path.suffix.lower() == ".srt" and not _is_generated_stem(path.stem)
        )
        if not videos:
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
            unmatched_videos.clear()

        pairs.extend(_pair_result(video) for video in unmatched_videos)

    return sorted(pairs, key=lambda item: (item["directory"].casefold(), item["video_name"].casefold()))


def transcribe_submission_video(
        video_path, progress_callback=None, foreground_only=True,
        transcription_service=None):
    """为精剪成片生成同名 SRT；成功后清理检查点，失败时保留续跑数据。"""
    video = Path(video_path).expanduser().resolve()
    if not video.is_file():
        raise ValueError("投稿视频文件不存在")
    if video.suffix.casefold() not in _SUBMISSION_VIDEO_SUFFIXES:
        raise ValueError("投稿视频格式不受支持")

    if transcription_service is None or not callable(transcription_service):
        raise ValueError("字幕转录必须显式注入 transcription_service")

    expected_srt = video.with_suffix(".srt")
    checkpoint_path = video.with_name(f"{video.stem}_asr_checkpoint.json")
    srt_path = transcription_service(
        str(video),
        progress_callback=progress_callback,
        checkpoint_path=str(checkpoint_path),
        foreground_only=bool(foreground_only),
    )
    if not srt_path or not Path(srt_path).is_file():
        raise RuntimeError("未识别到有效语音，没有生成 SRT 字幕")
    generated_srt = Path(srt_path).resolve()
    if generated_srt != expected_srt.resolve():
        raise RuntimeError("FunASR 返回了非预期的字幕路径")
    cues = parse_srt_document(generated_srt)
    if not cues:
        raise RuntimeError("生成的 SRT 没有有效字幕")
    filter_result = {
        "enabled": bool(foreground_only),
        "mode": "off",
        "speaker_filtered_segment_count": 0,
        "speaker_filtered_chunk_count": 0,
    }
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        checkpoint = {}
    if isinstance(checkpoint, dict):
        filter_result.update({
            "mode": str(checkpoint.get("foreground_filter_mode") or "off"),
            "speaker_filtered_segment_count": int(
                checkpoint.get("speaker_filtered_segment_count") or 0
            ),
            "speaker_filtered_chunk_count": int(
                checkpoint.get("speaker_filtered_chunk_count") or 0
            ),
        })
    checkpoint_path.unlink(missing_ok=True)
    return {
        "video_path": str(video),
        "srt_path": str(generated_srt),
        "cue_count": len(cues),
        "background_filter": filter_result,
    }


def normalise_subtitle_review_dictionary(glossary=None, replacements=None):
    """合并默认专名、额外词条和固定纠错映射，保持顺序稳定。"""

    active_glossary = tuple(dict.fromkeys(
        str(item).strip()
        for item in (*DEFAULT_SUBTITLE_GLOSSARY, *(glossary or ()))
        if str(item).strip()
    ))
    active_replacements = []
    for item in replacements or ():
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("固定纠错映射必须由两个字符串组成")
        source, target = (str(part).strip() for part in item)
        if not source or not target:
            raise ValueError("固定纠错映射不能包含空字符串")
        pair = (source, target)
        if source != target and pair not in active_replacements:
            active_replacements.append(pair)
    active_glossary = tuple(dict.fromkeys((
        *active_glossary,
        *(target for _, target in active_replacements),
    )))
    return active_glossary, tuple(active_replacements)


def subtitle_review_profile_rules(streamer_profile, extra_glossary=None):
    """合并通用词表、冻结 profile 词表、身份词、映射目标与追加词条。"""

    if not isinstance(streamer_profile, StreamerProfile):
        raise TypeError("字幕校对主播配置必须是冻结的 StreamerProfile")
    profile_terms = merge_profile_subtitle_glossary(
        streamer_profile,
        (
            streamer_profile.canonical_name,
            streamer_profile.report_name,
            *streamer_profile.aliases,
            *(target for _, target in streamer_profile.asr_replacements),
        ),
    )
    return normalise_subtitle_review_dictionary(
        (*profile_terms, *(extra_glossary or ())),
        streamer_profile.asr_replacements,
    )


def _subtitle_source_fingerprint(
        srt_path, context_title, glossary, replacements=(),
        streamer_profile_id="", streamer_profile_label="",
        streamer_profile_fingerprint=""):
    digest = hashlib.sha256()
    digest.update(Path(srt_path).read_bytes())
    digest.update(str(context_title or "").encode("utf-8"))
    digest.update(json.dumps(list(glossary), ensure_ascii=False).encode("utf-8"))
    digest.update(json.dumps(list(replacements), ensure_ascii=False).encode("utf-8"))
    digest.update(str(streamer_profile_id or "").encode("utf-8"))
    digest.update(str(streamer_profile_label or "").encode("utf-8"))
    digest.update(str(streamer_profile_fingerprint or "").encode("utf-8"))
    digest.update(str(SUBTITLE_REVIEW_VERSION).encode("ascii"))
    return digest.hexdigest()


def _review_cache_path(srt_path):
    source = Path(srt_path)
    return source.with_name(f"{source.stem}_字幕校对建议.json")


def _validated_cached_review(
        cached, srt_path, cues, fingerprint, context_title, glossary,
        replacements, streamer_profile_id, streamer_profile_label,
        streamer_profile_fingerprint, cache_path):
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
    if cached.get("replacements") != [list(pair) for pair in replacements]:
        return None
    if str(cached.get("streamer_profile_id", "")) != str(streamer_profile_id or ""):
        return None
    if str(cached.get("streamer_profile_label", "")) != str(streamer_profile_label or ""):
        return None
    if str(cached.get("streamer_profile_fingerprint", "")) != str(
            streamer_profile_fingerprint or ""):
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
        "replacements": [list(pair) for pair in replacements],
        "streamer_profile_id": str(streamer_profile_id or ""),
        "streamer_profile_label": str(streamer_profile_label or ""),
        "streamer_profile_fingerprint": str(streamer_profile_fingerprint or ""),
        "glossary_count": len(glossary),
        "replacement_count": len(replacements),
        "suggestions": sorted(suggestions, key=lambda item: item["index"]),
        "cache_path": str(cache_path),
        "cache_hit": True,
    }


def _review_prompt(
        cues, target_indices, context_title, glossary, replacements=(),
        compact=False):
    cue_rows = [
        {"index": cue.index, "text": cue.text}
        for cue in cues
    ]
    rules = (
        "只修正能从上下文确认的错别字、同音误识别、专名和断词错误。"
        "禁止润色、改写语气、删除口头重复、增补标点或猜测听不清内容。"
        "必须主动检查与优先词表发音相近或被错误断开的文字；能从人物、团体、粉丝称呼等语境确认时改成词表中的专名。"
        "原文若是语义成立的常用词，不能只因视频标题或优先词表就替换成同主题词。"
        "固定纠错映射来自当前已识别主播的用户配置；原文精确出现左侧错词时必须改为右侧专名。"
        "没有错误的字幕不要放入 corrections。original 必须逐字复制输入原文。"
    )
    if compact:
        rules = (
            "只改确定错字和专名；不润色、不改标点、不删口癖；"
            "主动核对与优先词表同音或错误断开的专名；"
            "不能仅凭标题或词表替换语义成立的常用词；original 必须与输入完全一致。"
            "精确命中当前主播固定纠错映射时必须修正。"
        )
    replacement_rows = [
        {"错误词": source, "正确词": target}
        for source, target in replacements
    ]
    return (
        "你是直播切片的字幕校对员。"
        f"视频标题：{context_title or '未提供'}\n"
        f"优先词表：{'、'.join(glossary)}\n"
        f"当前主播固定纠错映射：{json.dumps(replacement_rows, ensure_ascii=False)}\n"
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
    response = llm_gateway.call_llm_with_retry(
        prompt,
        **call_kwargs,
    )
    return llm_gateway.extract_json_payload(response)


def _build_default_llm_runner():
    retry_coordinator = llm_gateway.LLMProviderRetryCoordinator()

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
            payload = llm_gateway.extract_json_payload(payload)
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


def _review_batch(
        cues, target_indices, context_title, glossary, replacements, llm_runner):
    cue_by_index = {cue.index: cue for cue in cues}
    prompt = _review_prompt(
        cues, target_indices, context_title, glossary, replacements, compact=False)
    compact_prompt = _review_prompt(
        cues, target_indices, context_title, glossary, replacements, compact=True)
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


def _apply_fixed_replacements(text, replacements):
    corrected = str(text or "")
    applied = []
    # 先替换长短语，避免“音乐声们”先命中“音乐声”后漏掉更精确规则。
    ordered_replacements = sorted(
        replacements or (),
        key=lambda pair: len(str(pair[0])),
        reverse=True,
    )
    for source, target in ordered_replacements:
        if source in corrected:
            corrected = corrected.replace(source, target)
            applied.append(f"{source} → {target}")
    return corrected, applied


def _fixed_replacement_suggestions(cues, replacements):
    """把当前主播的固定错词映射转成确定性建议，避免依赖模型记忆。"""

    suggestions = []
    for cue in cues:
        corrected, applied = _apply_fixed_replacements(cue.text, replacements)
        if corrected == cue.text:
            continue
        suggestions.append({
            "index": cue.index,
            "original": cue.text,
            "corrected": corrected,
            "reason": f"当前主播固定纠错：{'、'.join(applied)}",
            "confidence": 1.0,
            "source": "fixed_replacement",
            "start": cue.start,
            "end": cue.end,
        })
    return suggestions


def suggest_subtitle_corrections(
        srt_path, context_title="", glossary=None, llm_runner=None,
        use_cache=True, progress_callback=None, replacements=None,
        streamer_profile_id="", streamer_profile_label="",
        streamer_profile_fingerprint="", streamer_profile=None):
    """逐批检查字幕并返回建议；不修改原始字幕。"""
    cues = parse_srt_document(srt_path)
    if streamer_profile is not None:
        if replacements:
            raise ValueError("指定主播 profile 时固定纠错只能来自该 profile")
        active_glossary, active_replacements = subtitle_review_profile_rules(
            streamer_profile,
            glossary,
        )
        streamer_profile_id = streamer_profile.id
        streamer_profile_label = streamer_profile.label
        streamer_profile_fingerprint = streamer_profile.subtitle_review_fingerprint()
    else:
        active_glossary, active_replacements = normalise_subtitle_review_dictionary(
            glossary,
            replacements,
        )
    fingerprint = _subtitle_source_fingerprint(
        srt_path,
        context_title,
        active_glossary,
        active_replacements,
        streamer_profile_id,
        streamer_profile_label,
        streamer_profile_fingerprint,
    )
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
                active_replacements,
                streamer_profile_id,
                streamer_profile_label,
                streamer_profile_fingerprint,
                cache_path,
            )
            if validated:
                return validated
        except (OSError, ValueError, TypeError):
            pass

    runner = llm_runner if llm_runner is not None else _build_default_llm_runner()
    suggestions_by_index = {
        item["index"]: item
        for item in _fixed_replacement_suggestions(cues, active_replacements)
    }
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
            active_replacements,
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
        "replacements": [list(pair) for pair in active_replacements],
        "streamer_profile_id": str(streamer_profile_id or ""),
        "streamer_profile_label": str(streamer_profile_label or ""),
        "streamer_profile_fingerprint": str(streamer_profile_fingerprint or ""),
        "glossary_count": len(active_glossary),
        "replacement_count": len(active_replacements),
        "suggestions": [suggestions_by_index[index] for index in sorted(suggestions_by_index)],
        "cache_path": str(cache_path),
        "cache_hit": False,
    }
    if _subtitle_source_fingerprint(
            srt_path, context_title, active_glossary, active_replacements,
            streamer_profile_id, streamer_profile_label,
            streamer_profile_fingerprint) != fingerprint:
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
    replacements = tuple(
        (str(item[0]), str(item[1]))
        for item in (review_result or {}).get("replacements", [])
        if isinstance(item, (list, tuple)) and len(item) == 2
    )
    for item in (review_result or {}).get("suggestions", []):
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        original = _semantic_text(str(item.get("original", "")))
        corrected = _semantic_text(str(item.get("corrected", "")))
        fixed_corrected, fixed_applied = _apply_fixed_replacements(
            str(item.get("original", "")),
            replacements,
        )
        if (
                fixed_applied
                and confidence >= float(minimum_confidence)
                and fixed_corrected == str(item.get("corrected", ""))):
            selected.append(item)
            continue
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


def _subtitle_title_evidence(cues, maximum_chars=SUBTITLE_TITLE_EVIDENCE_CHARS):
    """保留整段字幕的时间顺序；超长成片按均匀采样压缩，避免只看开头。"""
    rows = []
    for cue in cues:
        clean_text = re.sub(r"\s+", " ", str(cue.text or "")).strip()
        if clean_text:
            rows.append(f"[{cue.start} - {cue.end}] {clean_text}")
    if not rows:
        raise ValueError("字幕没有可用于生成标题的正文")
    full_text = "\n".join(rows)
    if len(full_text) <= maximum_chars:
        return full_text, False

    target_count = max(3, int(len(rows) * maximum_chars / len(full_text)))
    if target_count >= len(rows):
        return full_text[:maximum_chars], True
    indices = {
        round(position * (len(rows) - 1) / (target_count - 1))
        for position in range(target_count)
    }
    sampled = "\n".join(rows[index] for index in sorted(indices))
    return sampled[:maximum_chars], True


def _subtitle_title_generation_prompt(
        evidence, context_title, *, sampled=False, compact=False,
        title_services=None):
    title_services = _resolve_title_services(title_services)
    style_prompt = title_services.build_title_style_prompt(
        evidence,
        compact=compact,
    )
    evidence_limit = 12000 if compact else SUBTITLE_TITLE_EVIDENCE_CHARS
    sampling_note = (
        "字幕过长，下面是覆盖开头、中段和结尾的等距样本；不得把缺失内容脑补为事实。"
        if sampled else
        "下面是当前成片按时间顺序排列的完整字幕。"
    )
    return (
        "你负责根据一条已经精剪完成的视频字幕生成B站投稿标题。"
        "先还原这段视频完整发生了什么：触发点、对话推进、真正结果或收尾原话分别是什么；"
        "再找陌生观众第一眼最想追问‘为什么’的具体矛盾、反差、误会、视觉细节或社死后果。"
        "字幕是ASR文本，可能缺少标点或有少量同音错字，应结合上下文理解，但绝不能补写字幕没有的事件。"
        "生成3个真正不同角度的标题：优先尝试原话反差、结果前置、口语吐槽；"
        "每个标题都应把具体诱因与真正结果、反转或代价写完整，不能只写‘聊到、介绍、发现、讨论、看到’。"
        "现有视频/目录名称只用于识别人物和主题，不能因为它已经像标题就照抄。"
        + title_services.build_title_hook_prompt_guide()
        + "\n只输出JSON对象："
        '{"content_summary":"一句话还原事件全过程",'
        '"hook":"最值得点击且有字幕证据的诱因+结果",'
        '"candidates":[{"title":"标题A","angle":"原话反差"},'
        '{"title":"标题B","angle":"结果前置"},'
        '{"title":"标题C","angle":"口语吐槽"}]}。'
        "不得输出分析过程或Markdown。\n\n"
        f"当前视频名称：{context_title or '未提供'}\n"
        f"字幕说明：{sampling_note}\n\n"
        f"账号历史标题风格（只学习语气和结构，不得照抄旧事件）：\n{style_prompt or '无'}\n\n"
        "当前成片字幕：\n" + evidence[:evidence_limit]
    )


def _normalise_subtitle_title_payload(
        payload, context_title, title_services=None):
    title_services = _resolve_title_services(title_services)
    if isinstance(payload, str):
        payload = llm_gateway.extract_json_payload(payload)
    if not isinstance(payload, dict):
        raise RuntimeError("标题生成没有返回有效 JSON 对象")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise RuntimeError("标题生成结果缺少 candidates 数组")
    titles = []
    for item in raw_candidates:
        raw_title = item.get("title") if isinstance(item, dict) else item
        raw_title = re.sub(r"\s+", " ", str(raw_title or "")).strip()
        if not 4 <= len(raw_title) <= title_services.max_publish_title_chars:
            continue
        title = title_services.normalise_publish_title(
            raw_title,
            context_title or "视频片段",
        )
        if title not in titles:
            titles.append(title)
    if len(titles) < 3:
        raise RuntimeError("AI 未返回 3 个不同且有效的参考标题")
    return {
        "content_summary": re.sub(
            r"\s+", " ", str(payload.get("content_summary") or "")
        ).strip()[:500],
        "hook": re.sub(r"\s+", " ", str(payload.get("hook") or "")).strip()[:300],
        "candidates": titles[:3],
    }


def _subtitle_title_judge_prompt(
        evidence, context_title, generated, *, sampled=False, compact=False,
        title_services=None):
    title_services = _resolve_title_services(title_services)
    style_prompt = title_services.build_title_style_prompt(
        evidence,
        compact=compact,
    )
    evidence_limit = 10000 if compact else SUBTITLE_TITLE_EVIDENCE_CHARS
    return (
        "你是独立的B站切片标题终审，不参与上一轮候选生成。"
        "请逐字核对字幕，比较3个候选；如果都遗漏最强后果、反转或收尾原话，可以重写。"
        "最终标题必须具体、口语化、有好奇点，同时完全受字幕证据支持。"
        "不要偏爱排在第一的标题，不要写成内容摘要，不要为博眼球编造事实。"
        + title_services.build_title_hook_prompt_guide()
        + "\n只输出JSON对象："
        '{"recommended_title":"最终推荐标题",'
        '"reason":"一句话说明保留了什么诱因和爆点",'
        '"alternatives":["备选标题1","备选标题2"]}。'
        "不要输出分析过程或Markdown。\n\n"
        f"当前视频名称：{context_title or '未提供'}\n"
        f"上一轮内容还原：{generated['content_summary']}\n"
        f"上一轮爆点：{generated['hook']}\n"
        f"候选标题：{json.dumps(generated['candidates'], ensure_ascii=False)}\n"
        f"账号历史标题风格：\n{style_prompt or '无'}\n\n"
        f"字幕{'（等距样本）' if sampled else '（完整）'}：\n"
        + evidence[:evidence_limit]
    )


def _normalise_subtitle_title_judgement(
        payload, generated, context_title, title_services=None):
    title_services = _resolve_title_services(title_services)
    if isinstance(payload, str):
        payload = llm_gateway.extract_json_payload(payload)
    if not isinstance(payload, dict):
        raise RuntimeError("标题终审没有返回有效 JSON 对象")
    raw_recommended = re.sub(
        r"\s+", " ", str(payload.get("recommended_title") or "")
    ).strip()
    if not 4 <= len(raw_recommended) <= title_services.max_publish_title_chars:
        raise RuntimeError("标题终审没有返回有效推荐标题")
    recommended = title_services.normalise_publish_title(
        raw_recommended,
        context_title or "视频片段",
    )
    titles = [recommended]
    for value in payload.get("alternatives") or []:
        raw = re.sub(r"\s+", " ", str(value or "")).strip()
        if not 4 <= len(raw) <= title_services.max_publish_title_chars:
            continue
        title = title_services.normalise_publish_title(
            raw,
            context_title or "视频片段",
        )
        if title not in titles:
            titles.append(title)
    for title in generated["candidates"]:
        if title not in titles:
            titles.append(title)
    return {
        "recommended_title": recommended,
        "reason": re.sub(
            r"\s+", " ", str(payload.get("reason") or "")
        ).strip()[:300],
        "candidates": titles[:3],
        "content_summary": generated["content_summary"],
        "hook": generated["hook"],
    }


def _default_subtitle_title_runner(prompt, compact_prompt, progress_label):
    response = llm_gateway.call_llm_with_retry(
        prompt,
        compact_prompt=compact_prompt,
        max_tokens=6000,
        compact_max_tokens=4500,
        attempts=3,
        progress_label=progress_label,
        require_json=True,
        reasoning_stage="review",
    )
    return llm_gateway.extract_json_payload(response)


def generate_subtitle_reference_titles(
        srt_path, context_title="", llm_runner=None, progress_callback=None,
        title_services=None):
    """根据当前成片字幕生成三个参考投稿标题，并独立终审推荐一个。"""
    title_services = _resolve_title_services(title_services)
    cues = parse_srt_document(srt_path)
    evidence, sampled = _subtitle_title_evidence(cues)
    runner = llm_runner or _default_subtitle_title_runner

    if progress_callback:
        progress_callback("根据校对字幕理解片段并生成标题候选...", 25, 100)
    generation_prompt = _subtitle_title_generation_prompt(
        evidence,
        context_title,
        sampled=sampled,
        title_services=title_services,
    )
    generated = _normalise_subtitle_title_payload(
        runner(
            generation_prompt,
            _subtitle_title_generation_prompt(
                evidence,
                context_title,
                sampled=sampled,
                compact=True,
                title_services=title_services,
            ),
            "字幕参考标题候选生成",
        ),
        context_title,
        title_services=title_services,
    )

    if progress_callback:
        progress_callback("独立复核标题爆点与字幕证据...", 65, 100)
    judgement = _normalise_subtitle_title_judgement(
        runner(
            _subtitle_title_judge_prompt(
                evidence,
                context_title,
                generated,
                sampled=sampled,
                title_services=title_services,
            ),
            _subtitle_title_judge_prompt(
                evidence,
                context_title,
                generated,
                sampled=sampled,
                compact=True,
                title_services=title_services,
            ),
            "字幕参考标题独立终审",
        ),
        generated,
        context_title,
        title_services=title_services,
    )
    judgement.update({
        "source_srt_path": str(Path(srt_path).resolve()),
        "context_title": str(context_title or ""),
        "cue_count": len(cues),
        "sampled": sampled,
    })
    if progress_callback:
        progress_callback("参考标题生成完成", 100, 100)
    return judgement


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


def _subtitle_display_text_size(text):
    return len(re.sub(r"\s+", "", str(text or "")))


def _subtitle_display_char_limit(width, geometry):
    """按输出画布、字号和描边估算一行字幕的安全字数。"""
    usable_width = max(1.0, float(width) - geometry["margin"] * 2)
    glyph_width = max(
        1.0,
        geometry["font_size"] * 0.92 + geometry["outline"] * 2,
    )
    return max(6, min(32, int(usable_width / glyph_width)))


def _split_subtitle_text_for_ass(text, max_chars):
    """将显示过长的单条字幕拆为安全行，优先保留词间空格。"""
    remaining = re.sub(r"\s+", " ", str(text or "")).strip()
    parts = []
    while _subtitle_display_text_size(remaining) > max_chars:
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
                and _subtitle_display_text_size(remaining[:preferred_cut])
                >= max(2, int(max_chars * 0.55))):
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


def _split_cue_for_ass(cue, max_chars):
    """把一条 SRT cue 拆成连续 ASS 事件，避免靠自动换行撑出画面。"""
    parts = _split_subtitle_text_for_ass(cue.text, max_chars)
    if not parts:
        return []
    start = cue.start_seconds
    end = cue.end_seconds
    if end <= start:
        # SRT 时间轴本身无效时才给渲染器一个最小可显示区间。
        end = start + 0.01
    if len(parts) == 1:
        return [(start, end, parts[0])]

    weights = [max(1, _subtitle_display_text_size(part)) for part in parts]
    total_weight = sum(weights)
    duration = end - start
    minimum_duration = min(0.05, duration / len(parts))
    cursor = start
    events = []
    for index, (part, weight) in enumerate(zip(parts, weights)):
        if index == len(parts) - 1:
            next_cursor = end
        else:
            ideal_cursor = start + duration * sum(weights[:index + 1]) / total_weight
            remaining_parts = len(parts) - index - 1
            earliest_cursor = cursor + minimum_duration
            latest_cursor = end - minimum_duration * remaining_parts
            next_cursor = min(latest_cursor, max(earliest_cursor, ideal_cursor))
        events.append((cursor, next_cursor, part))
        cursor = next_cursor
    return events


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
    max_chars = _subtitle_display_char_limit(width, geometry)
    for cue in cues:
        for start, end, text in _split_cue_for_ass(cue, max_chars):
            events.append(
                "Dialogue: 0,"
                f"{_ass_timestamp(start)},{_ass_timestamp(end)},"
                f"Default,,0,0,0,,{position}{_escape_ass_text(text)}"
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
    with _ATOMIC_WRITE_LOCK:
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
