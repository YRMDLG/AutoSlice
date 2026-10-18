"""识别热词：主播名、提督/舰长这类固定词，加上你确认过的学到的术语。

热词只用来让模型更愿意听成这些词，不会替换识别结果；放得太多反而会带偏，所以有上限。
"""

from __future__ import annotations

from autoslice.desktop.archive.knowledge import Knowledge, profile_id_for
from autoslice.desktop.foundation import DesktopStorage
from autoslice.transcription.contracts import DEFAULT_SUBTITLE_GLOSSARY

MAX_TERMS = 60
_MIN_LEN, _MAX_LEN = 2, 20


def build_hotwords(video_path: str, *, storage: DesktopStorage | None = None, title: str = "",
                   limit: int = MAX_TERMS) -> list[str]:
    """顺序就是优先级：固定词和主播名在前，学到的术语在后；去重、限长、限个数。"""

    values: list[str] = list(DEFAULT_SUBTITLE_GLOSSARY)
    streamer = profile_id_for(video_path, title)
    try:
        from autoslice.streamer_profiles import resolve_streamer_profile

        profile = resolve_streamer_profile("auto", video_path, context_hint=title)
        values += [profile.canonical_name, profile.report_name, *profile.aliases, *profile.subtitle_glossary]
        # 只放正确写法；asr_replacements 的左边是常见误识别，放进去等于鼓励模型写错
        values += [target for _, target in profile.asr_replacements]
    except (ImportError, OSError, ValueError):
        pass
    if storage is not None and streamer:
        values += Knowledge(storage).glossary(streamer)
    result, seen = [], set()
    for value in values:
        value = str(value or "").strip()
        key = value.casefold()
        if not (_MIN_LEN <= len(value) <= _MAX_LEN) or key in seen or value in ("主播", "generic"):
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= limit:
            break
    return result
