"""字幕纠错记忆：从每次保存的校对结果里学常见的识别错词。

同一对“错词 → 正确词”在两个不同视频里都被改过，就写进该主播的本机覆盖词库
（asr_replacements）。之后 AI 校对会把它列成建议，仍由用户采纳或跳过；
公开的 streamer_profiles.json 不会被改动。
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from autoslice.streamer_profiles import (
    add_streamer_profile_replacement,
    remove_streamer_profile_replacement,
)

from .foundation import DesktopStorage

# 同一对改动出现在几个不同视频里才记住；只改过一次的可能只是这句话的语境。
MIN_VIDEOS = 2
_WORD_RE = re.compile(r"[一-鿿A-Za-z0-9]")
_MAX_VIDEOS_KEPT = 20


@dataclass(frozen=True, slots=True)
class LearnedCorrection:
    wrong: str
    right: str
    videos: int
    promoted: bool
    note: str = ""


def _learnable(wrong: str, right: str) -> bool:
    """只学短的替换：长度相近、都有文字；只是加减字（语气词、标点）不算错词。"""

    return (
        2 <= len(wrong) <= 8
        and 2 <= len(right) <= 8
        and abs(len(wrong) - len(right)) <= 2
        and wrong != right
        and bool(_WORD_RE.search(wrong))
        and bool(_WORD_RE.search(right))
        and wrong not in right
        and right not in wrong
    )


def correction_pairs(original: str, corrected: str) -> tuple[tuple[str, str], ...]:
    """一条字幕的改动里能学的“错词 → 正确词”。

    单字替换太泛，向两边各扩一个没改的字（“乐→悦”记成“音乐生→音悦生”）。
    """

    original, corrected = str(original or ""), str(corrected or "")
    matcher = difflib.SequenceMatcher(None, original, corrected, autojunk=False)
    pairs: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        # 被改的部分本身要是文字；只改标点不算错词。
        if tag != "replace" or not (_WORD_RE.search(original[i1:i2]) and _WORD_RE.search(corrected[j1:j2])):
            continue
        if max(i2 - i1, j2 - j1) < 2:
            if i1 > 0 and j1 > 0:
                i1, j1 = i1 - 1, j1 - 1
            if i2 < len(original) and j2 < len(corrected):
                i2, j2 = i2 + 1, j2 + 1
        wrong, right = original[i1:i2].strip(), corrected[j1:j2].strip()
        if _learnable(wrong, right):
            pairs.append((wrong, right))
    return tuple(dict.fromkeys(pairs))


class CorrectionMemory:
    """按主播记录改过的错词；达到门槛后写进本机覆盖词库。"""

    def __init__(
        self,
        storage: DesktopStorage,
        *,
        promote: Callable[[str, str, str], object] = add_streamer_profile_replacement,
        demote: Callable[[str, str, str], object] = remove_streamer_profile_replacement,
    ) -> None:
        self.path = storage.root / "learning" / "subtitle-corrections.json"
        self._promote = promote
        self._demote = demote

    def _read(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        profiles = payload.get("profiles") if isinstance(payload, dict) else None
        return profiles if isinstance(profiles, dict) else {}

    def _write(self, profiles: dict) -> None:
        DesktopStorage._write_json(self.path, {"schema_version": 1, "profiles": profiles})

    def learn(
        self, profile_id: str, video_key: str, corrections: Iterable[tuple[str, str]],
    ) -> tuple[tuple[str, str], ...]:
        """记一次保存的改动；返回本次新写进词库的错词对。"""

        profiles = self._read()
        bucket = profiles.setdefault(profile_id, {})
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        promoted: list[tuple[str, str]] = []
        for original, corrected in corrections:
            for wrong, right in correction_pairs(original, corrected):
                item = bucket.setdefault(f"{wrong}\t{right}", {"wrong": wrong, "right": right, "videos": []})
                if video_key not in item["videos"]:
                    item["videos"] = [*item["videos"], video_key][-_MAX_VIDEOS_KEPT:]
                item["last"] = now
                if item.get("promoted") or item.get("ignored") or item.get("note") or len(item["videos"]) < MIN_VIDEOS:
                    continue
                try:
                    self._promote(profile_id, wrong, right)
                except ValueError as exc:
                    # 与已有固定纠错冲突、超过上限或主播未配置：不写词库，原因留给设置页显示。
                    item["note"] = str(exc)
                    continue
                item["promoted"] = True
                promoted.append((wrong, right))
        self._write(profiles)
        return tuple(promoted)

    def entries(self, profile_id: str | None = None) -> tuple[tuple[str, LearnedCorrection], ...]:
        """（主播, 记录），已记住的在前，其次按出现的视频数。"""

        rows = []
        for profile, bucket in self._read().items():
            if profile_id is not None and profile != profile_id:
                continue
            for item in bucket.values():
                if item.get("ignored"):
                    continue
                rows.append((profile, LearnedCorrection(
                    wrong=str(item.get("wrong", "")), right=str(item.get("right", "")),
                    videos=len(item.get("videos", ())), promoted=bool(item.get("promoted")),
                    note=str(item.get("note", "")),
                )))
        rows.sort(key=lambda row: (not row[1].promoted, -row[1].videos, row[1].wrong))
        return tuple(rows)

    def forget(self, profile_id: str, wrong: str, right: str) -> None:
        """忽略一条：从本机覆盖词库删除，以后同样的改动也不再自动记住。"""

        profiles = self._read()
        item = profiles.get(profile_id, {}).get(f"{wrong}\t{right}")
        if item is None:
            return
        if item.get("promoted"):
            try:
                self._demote(profile_id, wrong, right)
            except ValueError:
                pass
        item.update(promoted=False, ignored=True)
        self._write(profiles)
