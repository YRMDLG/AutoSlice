"""把一个文件夹里的文件认成“投稿条目”：成片、字幕初稿、校对稿、字幕版成片、封面；推断主播和日期。

全部是纯函数，不读文件内容，方便用合成的文件列表做测试；旧数据命名不统一，所以尽量宽容，认不准宁可少配。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Iterable, Sequence

from .model import (
    BURNED_MARKERS,
    CORRECTED_MARKERS,
    COVER_NAME_HINTS,
    GENERATED_MARKERS,
    IMAGE_SUFFIXES,
    NOTE_SUFFIXES,
    SRT_SUFFIX,
    VIDEO_SUFFIXES,
    ArchiveEntry,
    ArchiveVideo,
)

# (名字, 大小, 修改时间 ns)
FileInfo = tuple[str, int, int]

_DUPLICATE = re.compile(r" \(\d+\)$")
_DATE_FULL = re.compile(r"(20\d{2})[-_.年/ ]?(0?[1-9]|1[0-2])[-_.月/ ]?(0?[1-9]|[12]\d|3[01])")
_DATE_MONTH_DAY = re.compile(r"(0?[1-9]|1[0-2])月(0?[1-9]|[12]\d|3[01])日")
_BRACKET_LABEL = re.compile(r"^\s*[【\[]([^】\]]{1,20})[】\]]")
_MAX_COVERS = 3
_MAX_NOTES = 3


def _split(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    return (name[:dot], name[dot:].lower()) if dot > 0 else (name, "")


def _base(stem: str) -> str:
    """去掉“ (2)”这种避让编号。"""

    return _DUPLICATE.sub("", stem)


def _ends_with(stem: str, markers: Iterable[str]) -> str:
    stem = _base(stem)
    return next((marker for marker in markers if stem.endswith(marker)), "")


def is_generated(stem: str) -> bool:
    """工作台自己生成的文件（校对稿、字幕版成片……）。"""

    return stem.endswith(".part") or bool(_ends_with(stem, GENERATED_MARKERS))


def strip_markers(stem: str) -> str:
    """去掉末尾的生成后缀，得到原始素材的主名。"""

    stem = _base(stem)
    changed = True
    while changed:
        changed = False
        for marker in GENERATED_MARKERS:
            if stem.endswith(marker):
                stem = stem[: -len(marker)]
                changed = True
    return stem


def infer_date(title: str, fallback_ns: int = 0) -> str:
    """从标题里的日期猜，猜不到用文件修改时间；都没有返回空串（YYYY-MM-DD）。"""

    year_hint = 0
    if fallback_ns > 0:
        try:
            year_hint = datetime.fromtimestamp(fallback_ns / 1e9).year
        except (OverflowError, OSError, ValueError):
            year_hint = 0
    match = _DATE_FULL.search(title)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            pass
    match = _DATE_MONTH_DAY.search(title)
    if match and year_hint:
        try:
            return date(year_hint, int(match.group(1)), int(match.group(2))).isoformat()
        except ValueError:
            pass
    if fallback_ns > 0 and year_hint:
        try:
            return datetime.fromtimestamp(fallback_ns / 1e9).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def _cover_rank(name: str, size: int) -> tuple[int, int]:
    lowered = name.lower()
    hinted = any(hint in lowered for hint in COVER_NAME_HINTS)
    return (0 if hinted else 1, -size)


def classify_folder(folder: str, title: str, files: Sequence[FileInfo]) -> ArchiveEntry | None:
    """认一个文件夹；没有成片也没有字幕就不算投稿条目。"""

    videos, srts, images, notes = [], [], [], []
    for name, size, mtime in files:
        stem, suffix = _split(name)
        if suffix in VIDEO_SUFFIXES:
            videos.append((name, stem, size, mtime))
        elif suffix == SRT_SUFFIX:
            srts.append((name, stem, size, mtime))
        elif suffix in IMAGE_SUFFIXES:
            images.append((name, size))
        elif suffix in NOTE_SUFFIXES:
            notes.append(name)
    raw_videos = [item for item in videos if not is_generated(item[1])]
    burned_videos = {strip_markers(item[1]).casefold(): item[0] for item in videos if _ends_with(item[1], BURNED_MARKERS)}
    raw_srts = [item for item in srts if not is_generated(item[1])]
    corrected_by_stem: dict[str, str] = {}
    for name, stem, _size, _mtime in srts:
        if _ends_with(stem, CORRECTED_MARKERS):
            corrected_by_stem.setdefault(strip_markers(stem).casefold(), name)
    if not raw_videos and not raw_srts and not corrected_by_stem:
        return None

    srt_by_stem = {_base(item[1]).casefold(): item[0] for item in raw_srts}
    # 只有一个成片和一份初稿时，名字对不上也认为是一对（旧数据常这样）。
    lone_srt = raw_srts[0][0] if len(raw_videos) == 1 and len(raw_srts) == 1 else ""
    sources: list[ArchiveVideo] = []
    used_srts: set[str] = set()
    for name, stem, size, mtime in raw_videos:
        key = _base(stem).casefold()
        srt = srt_by_stem.get(key, "") or lone_srt
        used_srts.add(srt)
        corrected = corrected_by_stem.get(key, "")
        if not corrected and srt:
            corrected = corrected_by_stem.get(_base(_split(srt)[0]).casefold(), "")
        sources.append(ArchiveVideo(
            name=name, path=f"{folder}/{name}", size=size, mtime_ns=mtime,
            srt=f"{folder}/{srt}" if srt else "", corrected=f"{folder}/{corrected}" if corrected else "",
            burned=f"{folder}/{burned_videos[key]}" if key in burned_videos else "",
        ))
    # 没有成片、只留了字幕的旧文件夹：用字幕当“视频”记下来，字幕里的信息照样能学。
    for name, stem, size, mtime in raw_srts:
        if name in used_srts:
            continue
        key = _base(stem).casefold()
        corrected = corrected_by_stem.get(key, "")
        sources.append(ArchiveVideo(
            name=name, path="", size=0, mtime_ns=mtime, srt=f"{folder}/{name}",
            corrected=f"{folder}/{corrected}" if corrected else "",
        ))
    for key, corrected in corrected_by_stem.items():
        if not any(video.corrected.endswith(corrected) for video in sources):
            sources.append(ArchiveVideo(name=corrected, path="", size=0, mtime_ns=0, corrected=f"{folder}/{corrected}"))

    covers = tuple(f"{folder}/{name}" for name, _size in sorted(images, key=lambda item: _cover_rank(*item))[:_MAX_COVERS])
    note_files = tuple(f"{folder}/{name}" for name in sorted(notes)[:_MAX_NOTES])
    earliest = min((video.mtime_ns for video in sources if video.mtime_ns > 0), default=0)
    digest = hashlib.sha1()
    for item in sorted(files):
        digest.update(f"{item[0]}|{item[1]}|{item[2]}\n".encode("utf-8", "replace"))
    return ArchiveEntry(
        folder=folder, title=title or sources[0].name, videos=tuple(sources), covers=covers, notes=note_files,
        date=infer_date(title, earliest), signature=digest.hexdigest()[:20],
    )


class StreamerMatcher:
    """按主播档案（标题前缀、称呼、路径关键词）认出一个文件夹属于谁；没有档案的主播按标题开头的【名字】归类。"""

    def __init__(self, profiles: Iterable[tuple[str, str, str, Sequence[str], Sequence[str]]] = ()) -> None:
        # (id, 名称, 标题前缀, 标题里出现就算的称呼, 路径关键词)
        self._profiles = [item for item in profiles if item[0] != "generic"]

    @classmethod
    def from_profiles(cls) -> "StreamerMatcher":
        try:
            from autoslice.streamer_profiles import load_streamer_profiles

            profiles, _default = load_streamer_profiles()
        except (ImportError, OSError, ValueError):
            return cls()
        return cls(
            (
                profile.id, profile.label, profile.title_prefix,
                tuple(dict.fromkeys((profile.canonical_name, profile.report_name, *profile.aliases))),
                tuple(profile.path_keywords),
            )
            for profile in profiles.values()
        )

    def infer(self, folder: str, title: str) -> str:
        """返回主播 id；认不出返回空串。"""

        for profile_id, _label, prefix, _names, _keywords in self._profiles:
            if prefix and title.startswith(prefix):
                return profile_id
        for profile_id, _label, _prefix, names, _keywords in self._profiles:
            if any(name and name in title for name in names):
                return profile_id
        lowered = folder.casefold().replace("\\", "/")
        best = ("", 0)
        for profile_id, _label, _prefix, _names, keywords in self._profiles:
            for keyword in keywords:
                if keyword and keyword.casefold() in lowered and len(keyword) > best[1]:
                    best = (profile_id, len(keyword))
        if best[0]:
            return best[0]
        match = _BRACKET_LABEL.match(title)
        return f"ext:{match.group(1).strip()}" if match else ""

    def label(self, streamer: str) -> str:
        for profile_id, label, *_rest in self._profiles:
            if profile_id == streamer:
                return label
        return streamer.removeprefix("ext:") if streamer else "未识别"
