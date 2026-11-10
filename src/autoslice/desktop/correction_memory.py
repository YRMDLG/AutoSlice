"""字幕纠错记忆：从每次保存的校对结果里学常见的识别错词。

同一改动在多个视频出现、且在已保存语料中精度足够时，作为待确认校对建议。
日常学习不再写入识别阶段的固定替换，避免特定语境的改字影响其它句子。
"""

from __future__ import annotations

import difflib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from autoslice.streamer_profiles import remove_streamer_profile_replacement

from .foundation import DesktopStorage

# 同一对改动出现在几个不同视频里才记住；只改过一次的可能只是这句话的语境。
MIN_VIDEOS = 2
MIN_PRECISION = 0.8
_WORD_RE = re.compile(r"[一-鿿A-Za-z0-9]")
_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class LearnedCorrection:
    wrong: str
    right: str
    videos: int
    promoted: bool
    note: str = ""
    kept: int = 0
    precision: float = 0.0


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
    """按主播积累完整校对样本，成熟的记忆只供校对确认。"""

    def __init__(
        self,
        storage: DesktopStorage,
        *,
        promote: Callable[[str, str, str], object] | None = None,
        demote: Callable[[str, str, str], object] = remove_streamer_profile_replacement,
    ) -> None:
        self.path = storage.root / "learning" / "subtitle-corrections.json"
        self._promote = promote
        self._demote = demote
        with _LOCK:
            data = self._data()
            if data.get("schema_version") == 1:
                for profile, bucket in data.get("profiles", {}).items():
                    for item in bucket.values():
                        if item.get("promoted"):
                            try:
                                self._demote(profile, item["wrong"], item["right"])
                            except ValueError:
                                pass  # 已删除的主播或不存在的覆盖规则无需再次移除。
                        item.update(promoted=False, note="旧记忆已转为校对参考，等待完整样本")
                self._write(data)

    def _data(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"profiles": {}, "samples": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
            return {"profiles": {}, "samples": {}}
        payload.setdefault("samples", {})
        return payload

    def _write(self, data: dict) -> None:
        DesktopStorage._write_json(self.path, {**data, "schema_version": 2})

    def learn(
        self, profile_id: str, video_key: str, corrections: Iterable[tuple[str, str]],
        *, final_text: str | None = None,
    ) -> tuple[tuple[str, str], ...]:
        """记住当前视频的最终版本，返回本次新增的校对参考。"""
        with _LOCK:
            return self._learn(profile_id, video_key, list(corrections), final_text)

    def _learn(self, profile_id, video_key, corrections, final_text):
        data = self._data()
        profiles = data["profiles"]
        bucket = profiles.setdefault(profile_id, {})
        samples = data["samples"].setdefault(profile_id, {})
        pairs = [pair for original, corrected in corrections
                 for pair in correction_pairs(original, corrected)]
        samples[video_key] = {"text": final_text if final_text is not None else
                             "\n".join(corrected for _, corrected in corrections),
                             "pairs": [list(pair) for pair in pairs]}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        promoted: list[tuple[str, str]] = []
        for wrong, right in pairs:
            bucket.setdefault(f"{wrong}\t{right}", {"wrong": wrong, "right": right})
        for item in bucket.values():
            wrong, right = item["wrong"], item["right"]
            pair = [wrong, right]
            videos = [key for key, sample in samples.items() if pair in sample["pairs"]]
            count = sum(sample["pairs"].count(pair) for sample in samples.values())
            kept = sum(sample["text"].count(wrong) for sample in samples.values())
            precision = count / (count + kept) if count + kept else 0.0
            ready = not item.get("ignored") and len(videos) >= MIN_VIDEOS and precision >= MIN_PRECISION
            if ready and not item.get("promoted"):
                try:
                    if self._promote is not None:
                        self._promote(profile_id, wrong, right)
                except ValueError as exc:
                    ready = False
                    item["note"] = str(exc)
                else:
                    item["note"] = ""
                    promoted.append((wrong, right))
            item.update(videos=videos, n=count, kept=kept, precision=precision,
                        promoted=bool(ready), last=now)
        self._write(data)
        return tuple(promoted)

    def entries(self, profile_id: str | None = None) -> tuple[tuple[str, LearnedCorrection], ...]:
        """（主播, 记录），已记住的在前，其次按出现的视频数。"""

        rows = []
        for profile, bucket in self._data()["profiles"].items():
            if profile_id is not None and profile != profile_id:
                continue
            for item in bucket.values():
                if item.get("ignored"):
                    continue
                rows.append((profile, LearnedCorrection(
                    wrong=str(item.get("wrong", "")), right=str(item.get("right", "")),
                    videos=len(item.get("videos", ())), promoted=bool(item.get("promoted")),
                    note=str(item.get("note", "")), kept=int(item.get("kept", 0)),
                    precision=float(item.get("precision", 0)),
                )))
        rows.sort(key=lambda row: (not row[1].promoted, -row[1].videos, row[1].wrong))
        return tuple(rows)

    def replacements(self, profile_id: str) -> tuple[tuple[str, str], ...]:
        """仅返回可供人工确认的校对建议，不用于识别阶段自动替换。"""
        return tuple((item.wrong, item.right) for _, item in self.entries(profile_id) if item.promoted)

    def forget(self, profile_id: str, wrong: str, right: str) -> None:
        """忽略一条校对参考，以后同样的改动也不再提供建议。"""

        with _LOCK:
            self._forget(profile_id, wrong, right)

    def _forget(self, profile_id, wrong, right):
        data = self._data()
        profiles = data["profiles"]
        item = profiles.get(profile_id, {}).get(f"{wrong}\t{right}")
        if item is None:
            return
        if item.get("promoted") and self._promote is not None:
            try:
                self._demote(profile_id, wrong, right)
            except ValueError:
                pass
        item.update(promoted=False, ignored=True)
        self._write(data)
