"""主播专属称呼、标题和 ASR 规则的可配置注册表。"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator


PROFILE_SCHEMA_VERSION = 1
AUTO_PROFILE_ID = "auto"
LEGACY_PROFILE_ID = "zeyin"
DEFAULT_PROFILE_PATH = Path(__file__).with_name("streamer_profiles.json")
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_FILENAME_STREAMER_RE = re.compile(
    r"^(?P<name>.+?)[\s_-]+(?="
    r"(?:19|20)\d{2}(?:"
    r"[-_.]\d{1,2}[-_.]\d{1,2}"
    r"|年\d{1,2}月\d{1,2}(?:日|号)?"
    r"|\d{4}(?!\d)"
    r"))",
    re.IGNORECASE,
)
_PROFILE_NAME_SEPARATOR_RE = re.compile(r"[\s._\-·•【】\[\]()（）]+")
_ACTIVE_PROFILE: ContextVar["StreamerProfile | None"] = ContextVar(
    "autoslice_streamer_profile",
    default=None,
)


@dataclass(frozen=True)
class OutroClipConfig:
    """主播可选的收播系列切片规则。"""

    series_title: str
    search_tail_sec: int
    triggers: tuple[str, ...]


@dataclass(frozen=True)
class StreamerProfile:
    """单个主播工作流所需的稳定配置。"""

    id: str
    label: str
    canonical_name: str
    report_name: str
    title_prefix: str
    aliases: tuple[str, ...]
    path_keywords: tuple[str, ...]
    subtitle_glossary: tuple[str, ...]
    asr_replacements: tuple[tuple[str, str], ...]
    title_style_profile: Path | None
    outro_clip: OutroClipConfig | None

    def to_public_dict(self) -> dict[str, object]:
        """只返回前端选择所需字段，不暴露本机配置路径。"""

        return {
            "id": self.id,
            "label": self.label,
            "canonical_name": self.canonical_name,
            "report_name": self.report_name,
            "title_prefix": self.title_prefix,
            "aliases": list(self.aliases),
        }


def _config_path(path: str | os.PathLike[str] | None = None) -> Path:
    configured = path or os.environ.get("AUTOSLICE_STREAMER_PROFILES")
    return Path(configured or DEFAULT_PROFILE_PATH).expanduser().resolve()


def _required_text(payload: dict[str, object], key: str, *, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"主播配置 {key} 必须是非空字符串")
    clean = value.strip()
    if len(clean) > maximum:
        raise ValueError(f"主播配置 {key} 不能超过 {maximum} 个字符")
    return clean


def _string_list(
        payload: dict[str, object], key: str, *, maximum: int) -> tuple[str, ...]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"主播配置 {key} 必须是字符串数组")
    cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    if len(cleaned) > maximum:
        raise ValueError(f"主播配置 {key} 最多包含 {maximum} 项")
    return cleaned


def _replacement_pairs(payload: dict[str, object]) -> tuple[tuple[str, str], ...]:
    value = payload.get("asr_replacements", [])
    if not isinstance(value, list):
        raise ValueError("主播配置 asr_replacements 必须是二维字符串数组")
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
                not isinstance(item, list)
                or len(item) != 2
                or any(not isinstance(part, str) or not part.strip() for part in item)):
            raise ValueError("主播配置 asr_replacements 每项必须包含两个非空字符串")
        pair = (item[0].strip(), item[1].strip())
        if pair not in pairs:
            pairs.append(pair)
    if len(pairs) > 100:
        raise ValueError("主播配置 asr_replacements 最多包含 100 项")
    return tuple(pairs)


def _title_style_path(config_path: Path, payload: dict[str, object]) -> Path | None:
    configured = str(os.environ.get("AUTOSLICE_TITLE_STYLE_PROFILE", "")).strip()
    if configured:
        resolved = Path(configured).expanduser().resolve()
        if not resolved.is_file():
            raise ValueError("AUTOSLICE_TITLE_STYLE_PROFILE 指向的标题样本文件不存在")
        return resolved
    value = payload.get("title_style_profile")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("主播配置 title_style_profile 必须是相对路径或 null")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError("主播配置 title_style_profile 必须使用相对路径")
    resolved = (config_path.parent / relative).resolve()
    try:
        resolved.relative_to(config_path.parent.resolve())
    except ValueError as exc:
        raise ValueError("主播配置 title_style_profile 不能超出配置目录") from exc
    if not resolved.is_file():
        raise ValueError(f"主播标题样本文件不存在: {relative}")
    if resolved.name == "title_style_profile.example.json":
        private_profile = resolved.with_name("title_style_profile.json")
        if private_profile.is_file():
            return private_profile
    return resolved


def _outro_clip_config(payload: dict[str, object]) -> OutroClipConfig | None:
    """校验可选收播片规则；未配置或显式停用时返回 None。"""

    value = payload.get("outro_clip")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("主播配置 outro_clip 必须是对象或 null")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("主播配置 outro_clip.enabled 必须是布尔值")
    if not enabled:
        return None

    series_title = _required_text(value, "series_title", maximum=80)
    search_tail_sec = value.get("search_tail_sec", 1200)
    if (
            isinstance(search_tail_sec, bool)
            or not isinstance(search_tail_sec, int)
            or not 60 <= search_tail_sec <= 7200):
        raise ValueError("主播配置 outro_clip.search_tail_sec 必须是 60-7200 的整数")
    triggers = _string_list(value, "triggers", maximum=30)
    if not triggers:
        raise ValueError("主播配置 outro_clip.triggers 至少包含一条收播口令")
    return OutroClipConfig(
        series_title=series_title,
        search_tail_sec=search_tail_sec,
        triggers=triggers,
    )


def load_streamer_profiles(
        path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, StreamerProfile], str]:
    """读取并严格校验主播配置。"""

    config_path = _config_path(path)
    try:
        with config_path.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"主播配置 JSON 无效: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"无法读取主播配置: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("主播配置根节点必须是对象")
    if payload.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError(f"主播配置 schema_version 必须为 {PROFILE_SCHEMA_VERSION}")
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError("主播配置 profiles 必须是非空数组")

    profiles: dict[str, StreamerProfile] = {}
    for item in raw_profiles:
        if not isinstance(item, dict):
            raise ValueError("主播配置 profiles 每项必须是对象")
        profile_id = _required_text(item, "id", maximum=32).casefold()
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError(f"主播配置 id 格式无效: {profile_id}")
        if profile_id in profiles or profile_id == AUTO_PROFILE_ID:
            raise ValueError(f"主播配置 id 重复或保留: {profile_id}")
        title_prefix = item.get("title_prefix", "")
        if not isinstance(title_prefix, str) or len(title_prefix) > 32:
            raise ValueError("主播配置 title_prefix 必须是不超过 32 字的字符串")
        profile = StreamerProfile(
            id=profile_id,
            label=_required_text(item, "label", maximum=80),
            canonical_name=_required_text(item, "canonical_name", maximum=80),
            report_name=_required_text(item, "report_name", maximum=80),
            title_prefix=title_prefix.strip(),
            aliases=_string_list(item, "aliases", maximum=30),
            path_keywords=_string_list(item, "path_keywords", maximum=30),
            subtitle_glossary=_string_list(
                item,
                "subtitle_glossary",
                maximum=200,
            ),
            asr_replacements=_replacement_pairs(item),
            title_style_profile=_title_style_path(config_path, item),
            outro_clip=_outro_clip_config(item),
        )
        profiles[profile_id] = profile

    default_profile_id = _required_text(
        payload,
        "default_profile_id",
        maximum=32,
    ).casefold()
    if default_profile_id not in profiles:
        raise ValueError("主播配置 default_profile_id 不存在")

    generic_profile = profiles.get("generic")
    if generic_profile is not None:
        for profile_id, profile in tuple(profiles.items()):
            if profile_id == generic_profile.id:
                continue
            profiles[profile_id] = replace(
                profile,
                subtitle_glossary=tuple(dict.fromkeys((
                    *generic_profile.subtitle_glossary,
                    *profile.subtitle_glossary,
                ))),
            )
    return profiles, default_profile_id


def infer_streamer_name_from_filename(
        video_path: str | os.PathLike[str] | None,
) -> str | None:
    """从“主播名-日期”格式的录播文件名提取主播名。"""

    if not video_path:
        return None
    filename = Path(str(video_path)).stem.strip()
    match = _FILENAME_STREAMER_RE.match(filename)
    if not match:
        return None
    name = match.group("name").strip(" \t\r\n-_.")
    if not name or len(name) > 30:
        return None
    if not any(character.isalnum() for character in name):
        return None
    return name


def _normalise_profile_name(value: str) -> str:
    """规范化配置称呼，用于匹配文件名中的主播名。"""

    return _PROFILE_NAME_SEPARATOR_RE.sub("", str(value or "")).casefold()


def _profile_filename_names(profile: StreamerProfile) -> tuple[str, ...]:
    prefix_name = profile.title_prefix.strip("【】[] \t\r\n")
    return (
        profile.canonical_name,
        profile.report_name,
        prefix_name,
        *profile.aliases,
        *profile.path_keywords,
    )


def _match_profile_by_filename_name(
        profiles: dict[str, StreamerProfile],
        streamer_name: str | None,
) -> StreamerProfile | None:
    normalized_name = _normalise_profile_name(streamer_name or "")
    if not normalized_name:
        return None
    for profile in profiles.values():
        if any(
                _normalise_profile_name(candidate) == normalized_name
                for candidate in _profile_filename_names(profile)):
            return profile
    return None


def _match_profile_by_context(
        profiles: dict[str, StreamerProfile],
        context_hint: str | None,
) -> StreamerProfile | None:
    """从投稿目录标题等上下文识别已配置主播，优先采用明确标题前缀。"""

    context = str(context_hint or "").strip().casefold()
    if not context:
        return None
    matches: list[tuple[int, int, str, StreamerProfile]] = []
    for profile in profiles.values():
        prefix_name = profile.title_prefix.strip("【】[] \t\r\n")
        candidates = (
            (500, profile.title_prefix, 2),
            (450, prefix_name, 2),
            (400, profile.canonical_name, 2),
            (300, profile.report_name, 2),
            *((200, alias, 2) for alias in profile.aliases),
        )
        for priority, candidate, minimum_length in candidates:
            normalized = str(candidate or "").strip().casefold()
            if len(normalized) >= minimum_length and normalized in context:
                matches.append((priority, len(normalized), profile.id, profile))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1], item[2]))[3]


def _dynamic_filename_profile(
        base_profile: StreamerProfile,
        streamer_name: str,
) -> StreamerProfile:
    """保留通用配置能力，仅按本次录播文件名补充主播身份。"""

    aliases = tuple(dict.fromkeys((
        streamer_name,
        *(alias for alias in base_profile.aliases if alias != "主播"),
    )))
    return StreamerProfile(
        id=base_profile.id,
        label=f"{streamer_name}（文件名识别）",
        canonical_name=streamer_name,
        report_name=streamer_name,
        title_prefix=f"【{streamer_name}】",
        aliases=aliases,
        path_keywords=base_profile.path_keywords,
        subtitle_glossary=base_profile.subtitle_glossary,
        asr_replacements=base_profile.asr_replacements,
        title_style_profile=base_profile.title_style_profile,
        outro_clip=base_profile.outro_clip,
    )


def resolve_streamer_profile(
        profile_id: str | None = AUTO_PROFILE_ID,
        video_path: str | os.PathLike[str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
        context_hint: str | None = None,
) -> StreamerProfile:
    """解析显式配置，或按录播路径自动匹配最具体的主播。"""

    profiles, default_profile_id = load_streamer_profiles(config_path)
    selected_id = str(profile_id or AUTO_PROFILE_ID).strip().casefold()
    if selected_id != AUTO_PROFILE_ID:
        try:
            return profiles[selected_id]
        except KeyError as exc:
            raise ValueError(f"未知主播配置: {selected_id}") from exc

    filename_streamer = infer_streamer_name_from_filename(video_path)
    filename_profile = _match_profile_by_filename_name(
        profiles,
        filename_streamer,
    )
    if filename_profile is not None:
        return filename_profile

    normalized_path = os.path.normcase(os.path.abspath(str(video_path or ""))).casefold()
    matches: list[tuple[int, str, StreamerProfile]] = []
    for profile in profiles.values():
        for keyword in profile.path_keywords:
            normalized_keyword = keyword.casefold()
            if normalized_keyword and normalized_keyword in normalized_path:
                matches.append((len(normalized_keyword), profile.id, profile))
    if matches:
        return max(matches, key=lambda item: (item[0], item[1]))[2]
    context_profile = _match_profile_by_context(profiles, context_hint)
    if context_profile is not None:
        return context_profile
    default_profile = profiles[default_profile_id]
    if filename_streamer:
        return _dynamic_filename_profile(default_profile, filename_streamer)
    return default_profile


def merge_profile_subtitle_glossary(
        profile: StreamerProfile,
        extra_terms: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...]:
    """在 profile 默认词表后追加本次任务词条，默认项不可被覆盖。"""

    return tuple(dict.fromkeys(
        str(item).strip()
        for item in (*profile.subtitle_glossary, *(extra_terms or ()))
        if str(item).strip()
    ))


def active_streamer_profile() -> StreamerProfile | None:
    """返回当前任务配置；没有任务上下文时返回 None。"""

    return _ACTIVE_PROFILE.get()


def current_streamer_profile() -> StreamerProfile:
    """返回当前任务配置；没有任务上下文时使用通用默认配置。"""

    active = active_streamer_profile()
    if active is not None:
        return active
    profiles, default_profile_id = load_streamer_profiles()
    return profiles[default_profile_id]


@contextmanager
def streamer_profile_context(
        profile_id: str | StreamerProfile | None = AUTO_PROFILE_ID,
        video_path: str | os.PathLike[str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
) -> Iterator[StreamerProfile]:
    """在当前线程/异步上下文内激活主播配置，并在退出时可靠恢复。"""

    profile = (
        profile_id
        if isinstance(profile_id, StreamerProfile)
        else resolve_streamer_profile(
            profile_id,
            video_path,
            config_path=config_path,
        )
    )
    token = _ACTIVE_PROFILE.set(profile)
    try:
        yield profile
    finally:
        _ACTIVE_PROFILE.reset(token)


def public_streamer_profiles() -> list[dict[str, object]]:
    """返回稳定的前端选择列表，自动识别始终排在首位。"""

    profiles, _default_profile_id = load_streamer_profiles()
    return [
        {
            "id": AUTO_PROFILE_ID,
            "label": "自动识别",
            "canonical_name": "",
            "report_name": "",
            "title_prefix": "",
            "aliases": [],
        },
        *(profile.to_public_dict() for profile in profiles.values()),
    ]
