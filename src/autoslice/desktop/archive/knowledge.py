"""用户确认过的“学到的知识”：运行时给字幕 AI 检查、投稿文案、字幕质量提醒取用。

和证据库分开：库里是海量原始证据，这里只是几个小 JSON（术语、标题范例、字幕习惯），读起来是毫秒级，
工作台日常运行不需要打开数据库，也不依赖扫描过档案。取用一律按“和当前视频相关”检索，不整份塞给大模型。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage

from .extractors.terms import grams

SCHEMA_VERSION = 1
_LOCK = threading.Lock()


def archive_dir(storage: DesktopStorage) -> Path:
    return storage.root / "learning" / "archive"


def profile_id_for(video_path: str, title: str = "") -> str:
    """这个视频属于哪个主播档案；认不出（或是通用档案）返回空串，空串取不到任何学到的东西。"""

    try:
        from autoslice.streamer_profiles import resolve_streamer_profile

        profile = resolve_streamer_profile("auto", video_path, context_hint=title)
    except (ImportError, OSError, ValueError):
        return ""
    return "" if profile.id == "generic" else profile.id


class Knowledge:
    def __init__(self, storage: DesktopStorage) -> None:
        self.dir = archive_dir(storage) / "knowledge"
        self._cache: dict[str, tuple[int, dict]] = {}

    def _read(self, name: str) -> dict:
        path = self.dir / f"{name}.json"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return {}
        cached = self._cache.get(name)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        streamers = payload.get("streamers") if isinstance(payload, dict) else None
        data = streamers if isinstance(streamers, dict) else {}
        self._cache[name] = (stamp, data)
        return data

    def _write(self, name: str, update) -> None:
        with _LOCK:
            data = dict(self._read(name))
            update(data)
            DesktopStorage._write_json(self.dir / f"{name}.json", {"schema_version": SCHEMA_VERSION, "streamers": data})
            self._cache.pop(name, None)

    # ── 术语 ──
    def add_glossary(self, streamer: str, terms: list[str]) -> int:
        added = 0

        def update(data: dict) -> None:
            nonlocal added
            current = list(data.get(streamer, []))
            for term in terms:
                if term and term not in current:
                    current.append(term)
                    added += 1
            data[streamer] = current[:2000]

        self._write("glossary", update)
        return added

    def glossary(self, streamer: str) -> list[str]:
        value = self._read("glossary").get(streamer, [])
        return [item for item in value if isinstance(item, str)]

    def relevant_glossary(self, streamer: str, text: str, limit: int = 40) -> list[str]:
        """和这段字幕有关的学到的术语：字幕里出现的优先（越长越优先），再补几个最靠前的。"""

        terms = self.glossary(streamer)
        if not terms:
            return []
        present = [term for term in terms if term in text]
        present.sort(key=lambda term: (-len(term), terms.index(term)))
        chosen = present[:limit]
        for term in terms[: max(0, min(10, limit - len(chosen)))]:
            if term not in chosen:
                chosen.append(term)
        return chosen[:limit]

    # ── 标题范例 ──
    def set_titles(self, streamer: str, titles: list[str]) -> None:
        self._write("titles", lambda data: data.__setitem__(streamer, [item for item in titles if item][:400]))

    def title_examples(self, streamer: str, query: str = "", k: int = 12) -> list[str]:
        """你以前的标题里和 query（现在的标题或字幕摘要）最像的 k 个；没有 query 就取靠后（较新）的。"""

        titles = [item for item in self._read("titles").get(streamer, []) if isinstance(item, str)]
        if not titles:
            return []
        if not query.strip():
            return titles[-k:]
        wanted = set(grams(query))
        scored = []
        for position, title in enumerate(titles):
            overlap = len(wanted & set(grams(title)))
            scored.append((overlap, position, title))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        picked = [title for overlap, _position, title in scored[:k] if overlap > 0]
        # 相关的不够就拿较新的补齐
        for title in reversed(titles):
            if len(picked) >= k:
                break
            if title not in picked:
                picked.append(title)
        return picked[:k]

    # ── 字幕习惯 ──
    def set_habits(self, streamer: str, habits: dict) -> None:
        self._write("habits", lambda data: data.__setitem__(streamer, dict(habits)))

    def subtitle_limits(self, streamer: str) -> dict:
        value = self._read("habits").get(streamer, {})
        return value if isinstance(value, dict) else {}
