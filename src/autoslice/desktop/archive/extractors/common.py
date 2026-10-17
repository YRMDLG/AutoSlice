"""几个抽取器共用的小东西：一个文件夹“最终发布的字幕”是哪份、每份只读一次。"""

from __future__ import annotations

from ..model import ArchiveEntry
from ..srt import Cue, load_cues
from .base import ExtractContext


def final_cues(entry: ArchiveEntry, context: ExtractContext) -> tuple[list[Cue], str]:
    """校对稿优先、没有就用初稿；多个成片的字幕按顺序接起来。返回（条目, 来源：corrected / raw / mixed / 空串）。"""

    cached = context.cache.get("final_cues")
    if cached is not None:
        return cached
    cues: list[Cue] = []
    sources: set[str] = set()
    for video in entry.videos:
        path, kind = (video.corrected, "corrected") if video.corrected else (video.srt, "raw")
        loaded = load_cues(path) if path else []
        if loaded:
            cues.extend(loaded)
            sources.add(kind)
    source = "" if not sources else (next(iter(sources)) if len(sources) == 1 else "mixed")
    context.cache["final_cues"] = (cues, source)
    return cues, source
