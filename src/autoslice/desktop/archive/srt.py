"""宽容的 SRT 读取：旧数据里什么编码、什么毛病的字幕都可能有，读不出来就跳过，不抛到上层。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 超过这个大小的字幕文件多半不是字幕，直接放弃。
MAX_BYTES = 4_000_000
_TIME = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})")
_BLOCK_SPLIT = re.compile(r"\n[ \t]*\n+")
_TAGS = re.compile(r"</?[a-zA-Z][^>]*>|\{\\[^}]*\}")


@dataclass(frozen=True, slots=True)
class Cue:
    index: int
    start: float
    end: float
    text: str


def read_text(path: str | Path) -> str:
    """按 UTF-8（含 BOM）、UTF-16、GB18030 依次尝试；读不出来返回空串。"""

    try:
        data = Path(path).read_bytes()[:MAX_BYTES]
    except OSError:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings = ("utf-16", "utf-8-sig", "gb18030")
    else:
        encodings = ("utf-8-sig", "gb18030", "utf-16")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")[:3]) / 1000


def parse_srt(text: str) -> list[Cue]:
    """解析 SRT；序号乱、缺块、带格式标签都不影响，只留下读得懂的条目。"""

    cues: list[Cue] = []
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    for position, block in enumerate(_BLOCK_SPLIT.split(text.strip("\n")), 1):
        lines = block.split("\n")
        for offset, line in enumerate(lines[:3]):
            match = _TIME.search(line)
            if match:
                break
        else:
            continue
        start = _seconds(*match.group(1, 2, 3, 4))
        end = _seconds(*match.group(5, 6, 7, 8))
        body = "\n".join(item.strip() for item in lines[offset + 1:] if item.strip())
        body = _TAGS.sub("", body).strip()
        if not body or end <= start:
            continue
        try:
            index = int(lines[0].strip()) if offset >= 1 else position
        except ValueError:
            index = position
        cues.append(Cue(index, start, end, body))
    return cues


def load_cues(path: str | Path) -> list[Cue]:
    """读文件并解析；任何问题都返回空列表。"""

    return parse_srt(read_text(path)) if path else []
