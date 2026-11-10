"""完整字幕编辑快照：保留混合拆分、合并后的条目与源字幕关系。"""

from __future__ import annotations

import math
import re

from autoslice.transcription.contracts import srt_timestamp_seconds

_TIME = re.compile(r"^\d{1,3}:\d{2}:\d{2},\d{3}$")
_TIMELINE = re.compile(r"\d{1,3}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,3}:\d{2}:\d{2}[,.]\d{3}")


def normalise_document_entries(cues, entries, *, allow_empty_text=False):
    """校验快照，不要求其分条方式与上次保存相同，也不重写正文。"""
    if not isinstance(entries, (list, tuple)) or not entries:
        raise ValueError("字幕编辑快照至少需要一条字幕")
    order = {cue.index: position for position, cue in enumerate(cues)}
    seen = set()
    previous_start = -1.0
    previous_source = -1
    result = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("字幕编辑条目无效")
        index = item.get("index")
        if type(index) is not int or index < 0 or index in seen:
            raise ValueError("字幕编辑序号重复或无效")
        members = item.get("source_indices")
        if not isinstance(members, (list, tuple)) or not members or any(
            type(value) is not int or value not in order for value in members
        ):
            raise ValueError("字幕编辑快照包含未知源字幕")
        positions = [order[value] for value in members]
        if positions != sorted(set(positions)) or positions[0] < previous_source:
            raise ValueError("字幕编辑快照的源字幕顺序无效")
        if not all(isinstance(item.get(key), str) for key in ("start", "end", "text")):
            raise ValueError("字幕编辑字段无效")
        if not _TIME.fullmatch(item["start"]) or not _TIME.fullmatch(item["end"]):
            raise ValueError("字幕时间格式无效")
        start, end = (srt_timestamp_seconds(item[key]) for key in ("start", "end"))
        if not all(math.isfinite(value) for value in (start, end)) or start < 0 or end <= start:
            raise ValueError("字幕结束时间必须晚于开始时间")
        if start < previous_start:
            raise ValueError("字幕编辑快照的时间顺序无效")
        if not allow_empty_text and not item["text"].strip():
            raise ValueError("字幕文字不能为空，请补全后保存")
        if not allow_empty_text and (_TIMELINE.search(item["text"]) or re.search(r"\n[ \t]*\n", item["text"])):
            raise ValueError("字幕正文不能包含时间轴或空白分隔行")
        stable_id = item.get("stable_id")
        if stable_id is not None and not isinstance(stable_id, str):
            raise ValueError("字幕编辑标识无效")
        result.append({"index": index, "start": item["start"], "end": item["end"],
                       "text": item["text"], "source_indices": list(members),
                       "stable_id": stable_id})
        seen.add(index)
        previous_start, previous_source = start, positions[0]
    return result


def serialise_document_entries(cues, entries):
    """按当前编辑结果输出，保留序号、正文、换行和时间。"""
    sources = {cue.index: cue for cue in cues}
    return "\n\n".join(
        f"{item['index']}\n{item['start']} --> {item['end']}"
        f"{sources[item['source_indices'][0]].settings}\n{item['text']}"
        for item in entries
    ) + "\n"
