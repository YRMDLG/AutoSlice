"""主播专属称呼、标题和 ASR 规则的可配置注册表。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Iterator

from autoslice.paths import SOURCE_WORKSPACE_ROOT, state_dir

PROFILE_SCHEMA_VERSION = 1
PROFILE_OVERRIDE_SCHEMA_VERSION = 1
AUTO_PROFILE_ID = "auto"
GENERIC_PROFILE_ID = "generic"
PACKAGE_PROFILE_PATH = Path(__file__).with_name("streamer_profiles.json")
DEFAULT_PROFILE_PATH = (
    SOURCE_WORKSPACE_ROOT / "streamer_profiles.json"
    if SOURCE_WORKSPACE_ROOT is not None
    and (SOURCE_WORKSPACE_ROOT / "streamer_profiles.json").is_file()
    else PACKAGE_PROFILE_PATH
)
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
_CONTEXT_TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:【(?P<book>[^【】\r\n]{1,32})】|\[(?P<bracket>[^\[\]\r\n]{1,32})\])"
)
_ACTIVE_PROFILE: ContextVar["StreamerProfile | None"] = ContextVar(
    "autoslice_streamer_profile",
    default=None,
)
_PROFILE_OVERRIDE_LOCK = threading.RLock()


@dataclass(frozen=True)
class OutroClipConfig:
    """主播可选的收播系列切片规则。"""

    series_title: str
    search_tail_sec: int
    triggers: tuple[str, ...]


@dataclass(frozen=True)
class CoverSeriesRule:
    """主播专属系列标题对应的封面模板规则。"""

    keywords: tuple[str, ...]
    template_key: str
    palette_key: str
    reason: str


@dataclass(frozen=True)
class CoverRulesConfig:
    """主播专属封面文案替换与系列推荐规则。"""

    copy_replacements: tuple[tuple[str, str], ...] = ()
    series_rules: tuple[CoverSeriesRule, ...] = ()

    @staticmethod
    def _copy_replacements(payload: dict[str, object]) -> tuple[tuple[str, str], ...]:
        value = payload.get("copy_replacements", [])
        if not isinstance(value, list):
            raise ValueError("主播配置 cover_rules.copy_replacements 必须是二维字符串数组")
        if len(value) > 50:
            raise ValueError("主播配置 cover_rules.copy_replacements 最多包含 50 项")
        pairs: list[tuple[str, str]] = []
        for item in value:
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError("主播配置 cover_rules.copy_replacements 每项必须包含两个字符串")
            source, target = item
            if not isinstance(source, str) or not isinstance(target, str):
                raise ValueError("主播配置 cover_rules.copy_replacements 每项必须包含两个字符串")
            pair = (
                re.sub(r"\s+", " ", source).strip(),
                re.sub(r"\s+", " ", target).strip(),
            )
            if not pair[0] or not pair[1] or pair[0] == pair[1]:
                raise ValueError("主播配置 cover_rules.copy_replacements 必须替换为不同的非空文本")
            if len(pair[0]) > 80 or len(pair[1]) > 80:
                raise ValueError("主播配置 cover_rules.copy_replacements 单项不能超过 80 个字符")
            if pair not in pairs:
                pairs.append(pair)
        return tuple(pairs)

    @classmethod
    def from_profile_payload(cls, payload: dict[str, object]) -> "CoverRulesConfig":
        """严格校验可选封面规则；旧配置缺少该字段时使用通用默认。"""

        value = payload.get("cover_rules")
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("主播配置 cover_rules 必须是对象或 null")
        unknown = set(value) - {"copy_replacements", "series_rules"}
        if unknown:
            raise ValueError(f"主播配置 cover_rules 包含未知字段: {', '.join(sorted(unknown))}")

        raw_rules = value.get("series_rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError("主播配置 cover_rules.series_rules 必须是对象数组")
        if len(raw_rules) > 12:
            raise ValueError("主播配置 cover_rules.series_rules 最多包含 12 项")
        series_rules: list[CoverSeriesRule] = []
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                raise ValueError("主播配置 cover_rules.series_rules 每项必须是对象")
            unknown_rule = set(raw_rule) - {
                "keywords",
                "template_key",
                "palette_key",
                "reason",
            }
            if unknown_rule:
                raise ValueError(
                    "主播配置 cover_rules.series_rules 包含未知字段: "
                    f"{', '.join(sorted(unknown_rule))}"
                )
            raw_keywords = raw_rule.get("keywords", [])
            if isinstance(raw_keywords, list) and len(raw_keywords) > 20:
                raise ValueError("主播配置 cover_rules.series_rules.keywords 最多包含 20 项")
            keywords = _string_list(raw_rule, "keywords", maximum=20)
            if not keywords:
                raise ValueError("主播配置 cover_rules.series_rules.keywords 至少包含一项")
            if any(len(keyword) > 80 for keyword in keywords):
                raise ValueError("主播配置 cover_rules.series_rules.keywords 单项不能超过 80 个字符")
            template_key = _required_text(raw_rule, "template_key", maximum=64)
            palette_key = _required_text(raw_rule, "palette_key", maximum=64)
            reason = _required_text(raw_rule, "reason", maximum=160)
            series_rules.append(CoverSeriesRule(
                keywords=keywords,
                template_key=template_key,
                palette_key=palette_key,
                reason=reason,
            ))
        return cls(
            copy_replacements=cls._copy_replacements(value),
            series_rules=tuple(series_rules),
        )


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
    cover_rules: CoverRulesConfig = CoverRulesConfig()

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

    def subtitle_review_fingerprint(self) -> str:
        """返回字幕复核相关配置的稳定摘要，供字幕缓存隔离。"""

        outro_clip = None
        if self.outro_clip is not None:
            outro_clip = {
                "series_title": self.outro_clip.series_title,
                "search_tail_sec": self.outro_clip.search_tail_sec,
                "triggers": list(self.outro_clip.triggers),
            }
        payload = {
            "id": self.id,
            "label": self.label,
            "canonical_name": self.canonical_name,
            "report_name": self.report_name,
            "title_prefix": self.title_prefix,
            "aliases": list(self.aliases),
            "path_keywords": list(self.path_keywords),
            "subtitle_glossary": list(self.subtitle_glossary),
            "asr_replacements": [list(pair) for pair in self.asr_replacements],
            "title_style_profile": str(self.title_style_profile or ""),
            "outro_clip": outro_clip,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _config_path(path: str | os.PathLike[str] | None = None) -> Path:
    configured = path or os.environ.get("AUTOSLICE_STREAMER_PROFILES")
    return Path(configured or DEFAULT_PROFILE_PATH).expanduser().resolve()


def streamer_profile_override_path() -> Path:
    """返回本机覆盖词库路径，不默认指向仓库公开配置。"""

    configured = str(os.environ.get("AUTOSLICE_STREAMER_PROFILE_OVERRIDES", "")).strip()
    if configured:
        path = Path(configured).expanduser().resolve()
    else:
        path = (state_dir() / "streamer_profile_overrides.json").resolve()
    if path.name.casefold() == "streamer_profiles.json":
        raise ValueError("主播覆盖词库不能覆盖公开 streamer_profiles.json")
    return path


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


def _load_profile_overrides(path: Path) -> dict[str, tuple[tuple[str, str], ...]]:
    """读取只允许包含错词映射的本机覆盖文件。"""

    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"主播覆盖词库 JSON 无效: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"无法读取主播覆盖词库: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != PROFILE_OVERRIDE_SCHEMA_VERSION:
        raise ValueError(
            f"主播覆盖词库 schema_version 必须为 {PROFILE_OVERRIDE_SCHEMA_VERSION}"
        )
    raw_profiles = payload.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ValueError("主播覆盖词库 profiles 必须是对象")
    overrides: dict[str, tuple[tuple[str, str], ...]] = {}
    for profile_id, raw_profile in raw_profiles.items():
        profile_id = str(profile_id).strip().casefold()
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError(f"主播覆盖词库 id 格式无效: {profile_id}")
        if not isinstance(raw_profile, dict):
            raise ValueError("主播覆盖词库 profile 必须是对象")
        unknown = set(raw_profile) - {"asr_replacements"}
        if unknown:
            raise ValueError("主播覆盖词库只能包含 asr_replacements，不能保存封面规则")
        overrides[profile_id] = _replacement_pairs(raw_profile)
    return overrides


def _merge_profile_overrides(
        payload: dict[str, object],
        overrides: dict[str, tuple[tuple[str, str], ...]],
) -> dict[str, object]:
    """把本机新增映射追加到公开 profile，不允许覆盖已有映射。"""

    if not overrides:
        return payload
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, list):
        return payload
    merged_payload = dict(payload)
    merged_profiles = []
    known_ids = set()
    for raw_profile in raw_profiles:
        if not isinstance(raw_profile, dict):
            merged_profiles.append(raw_profile)
            continue
        profile = dict(raw_profile)
        profile_id = str(profile.get("id") or "").strip().casefold()
        known_ids.add(profile_id)
        additions = overrides.get(profile_id, ())
        if additions:
            existing = list(_replacement_pairs(profile))
            for pair in additions:
                if pair not in existing:
                    if any(source == pair[0] and target != pair[1] for source, target in existing):
                        raise ValueError(f"主播覆盖词库与默认映射冲突: {profile_id}/{pair[0]}")
                    existing.append(pair)
            if len(existing) > 100:
                raise ValueError(f"主播覆盖词库 {profile_id} 的固定纠错超过 100 项")
            profile["asr_replacements"] = [list(pair) for pair in existing]
        merged_profiles.append(profile)
    unknown_ids = sorted(set(overrides) - known_ids)
    if unknown_ids:
        raise ValueError(f"主播覆盖词库包含未知 profile: {', '.join(unknown_ids)}")
    merged_payload["profiles"] = merged_profiles
    return merged_payload


def _atomic_write_profile_overrides(
        path: Path,
        overrides: dict[str, tuple[tuple[str, str], ...]],
) -> None:
    """原子保存本机覆盖词库，避免中断留下半个 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PROFILE_OVERRIDE_SCHEMA_VERSION,
        "profiles": {
            profile_id: {"asr_replacements": [list(pair) for pair in pairs]}
            for profile_id, pairs in sorted(overrides.items())
            if pairs
        },
    }
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def add_streamer_profile_replacement(
        profile_id: str,
        source: str,
        target: str,
        *,
        expected_fingerprint: str = "",
) -> dict[str, object]:
    """把用户确认后的映射写入本机覆盖词库并返回新 profile 摘要。"""

    profile_id = str(profile_id or "").strip().casefold()
    source = str(source or "").strip()
    target = str(target or "").strip()
    if not _PROFILE_ID_RE.fullmatch(profile_id) or profile_id == AUTO_PROFILE_ID:
        raise ValueError("只能为已配置的具体主播保存固定纠错")
    if not source or not target or source == target:
        raise ValueError("错词和正确词必须是不同的非空文本")
    if len(source) > 100 or len(target) > 100:
        raise ValueError("错词和正确词不能超过 100 个字符")

    with _PROFILE_OVERRIDE_LOCK:
        profiles, _ = load_streamer_profiles()
        profile = profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"未知主播配置: {profile_id}")
        if expected_fingerprint and profile.subtitle_review_fingerprint() != expected_fingerprint:
            raise ValueError("主播配置已变化，请重新执行 AI 检查后再保存")
        existing = tuple(profile.asr_replacements)
        conflicting = next(
            (pair for pair in existing if pair[0] == source and pair[1] != target),
            None,
        )
        if conflicting:
            raise ValueError(f"错词“{source}”已有固定纠错“{conflicting[1]}”，请先确认替换目标")
        override_path = streamer_profile_override_path()
        overrides = _load_profile_overrides(override_path)
        additions = list(overrides.get(profile_id, ()))
        pair = (source, target)
        already_present = pair in existing
        if pair not in additions and not already_present:
            if len(existing) + 1 > 100:
                raise ValueError("该主播固定纠错最多保存 100 条")
            additions.append(pair)
            overrides[profile_id] = tuple(additions)
            _atomic_write_profile_overrides(override_path, overrides)
        updated = resolve_streamer_profile(profile_id)
    return {
        "profile_id": updated.id,
        "profile_label": updated.label,
        "profile_fingerprint": updated.subtitle_review_fingerprint(),
        "replacement_count": len(updated.asr_replacements),
        "added": not already_present and pair in additions,
        "storage_scope": "本机用户覆盖词库，不修改仓库默认配置",
    }


def remove_streamer_profile_replacement(
        profile_id: str,
        source: str,
        target: str,
        *,
        expected_fingerprint: str = "",
) -> dict[str, object]:
    """只删除本机新增映射；公开默认 profile 的映射不能被误删。"""

    profile_id = str(profile_id or "").strip().casefold()
    pair = (str(source or "").strip(), str(target or "").strip())
    with _PROFILE_OVERRIDE_LOCK:
        profiles, _ = load_streamer_profiles()
        profile = profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"未知主播配置: {profile_id}")
        if expected_fingerprint and profile.subtitle_review_fingerprint() != expected_fingerprint:
            raise ValueError("主播配置已变化，请重新执行 AI 检查后再操作")
        override_path = streamer_profile_override_path()
        overrides = _load_profile_overrides(override_path)
        additions = list(overrides.get(profile_id, ()))
        if pair not in additions:
            if pair in profile.asr_replacements:
                raise ValueError("默认主播配置中的固定纠错不能通过页面删除")
            raise ValueError("本机覆盖词库中没有这条映射")
        additions.remove(pair)
        if additions:
            overrides[profile_id] = tuple(additions)
        else:
            overrides.pop(profile_id, None)
        _atomic_write_profile_overrides(override_path, overrides)
        updated = resolve_streamer_profile(profile_id)
    return {
        "profile_id": updated.id,
        "profile_label": updated.label,
        "profile_fingerprint": updated.subtitle_review_fingerprint(),
        "replacement_count": len(updated.asr_replacements),
        "storage_scope": "本机用户覆盖词库，不修改仓库默认配置",
    }


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
        private_candidates = [resolved.with_name("title_style_profile.json")]
        if SOURCE_WORKSPACE_ROOT is not None:
            private_candidates.insert(
                0,
                SOURCE_WORKSPACE_ROOT / "title_style_profile.json",
            )
        for private_profile in private_candidates:
            if private_profile.is_file():
                return private_profile.resolve()
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
    payload = _merge_profile_overrides(
        payload,
        _load_profile_overrides(streamer_profile_override_path()),
    )
    raw_profiles = payload.get("profiles")

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
            cover_rules=CoverRulesConfig.from_profile_payload(item),
        )
        profiles[profile_id] = profile

    default_profile_id = _required_text(
        payload,
        "default_profile_id",
        maximum=32,
    ).casefold()
    if default_profile_id not in profiles:
        raise ValueError("主播配置 default_profile_id 不存在")

    generic_profile = profiles.get(GENERIC_PROFILE_ID)
    if generic_profile is None:
        raise ValueError("主播配置必须包含 generic profile")
    for profile_id, profile in tuple(profiles.items()):
        if profile_id == generic_profile.id:
            continue
        profiles[profile_id] = replace(
            profile,
            subtitle_glossary=tuple(dict.fromkeys((
                *generic_profile.subtitle_glossary,
                *profile.subtitle_glossary,
            ))),
            cover_rules=CoverRulesConfig(
                copy_replacements=tuple(dict.fromkeys((
                    *generic_profile.cover_rules.copy_replacements,
                    *profile.cover_rules.copy_replacements,
                ))),
                series_rules=tuple(dict.fromkeys((
                    *generic_profile.cover_rules.series_rules,
                    *profile.cover_rules.series_rules,
                ))),
            ),
        )
    return profiles, default_profile_id


def infer_streamer_name_from_filename(
        video_path: str | os.PathLike[str] | None,
) -> str | None:
    """从“主播名-日期”格式的录播文件名提取主播名。"""

    if not video_path:
        return None
    raw_path = str(video_path)
    path_parser = PureWindowsPath if "\\" in raw_path else Path
    filename = path_parser(raw_path).stem.strip()
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
    prefix_match = _CONTEXT_TITLE_PREFIX_RE.match(str(context_hint or ""))
    if prefix_match is not None:
        prefix_name = prefix_match.group("book") or prefix_match.group("bracket") or ""
        matched_prefix = _match_profile_by_filename_name(profiles, prefix_name)
        return matched_prefix or profiles.get(GENERIC_PROFILE_ID)
    matches: list[tuple[int, int, str, StreamerProfile]] = []
    for profile in profiles.values():
        prefix_name = profile.title_prefix.strip("【】[] \t\r\n")
        candidates = (
            (500, profile.title_prefix, 2),
            (450, prefix_name, 2),
            (400, profile.canonical_name, 2),
            (300, profile.report_name, 2),
            *((200, alias, 2) for alias in profile.aliases),
            *(
                (150, keyword, 2)
                for rule in profile.cover_rules.series_rules
                for keyword in rule.keywords
            ),
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
        cover_rules=base_profile.cover_rules,
    )


def resolve_streamer_profile(
        profile_id: str | None = AUTO_PROFILE_ID,
        video_path: str | os.PathLike[str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
        context_hint: str | None = None,
) -> StreamerProfile:
    """解析显式配置，或按录播路径自动匹配最具体的主播。"""

    profiles, _default_profile_id = load_streamer_profiles(config_path)
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
    default_profile = profiles[GENERIC_PROFILE_ID]
    if filename_streamer:
        return _dynamic_filename_profile(default_profile, filename_streamer)
    context_profile = _match_profile_by_context(profiles, context_hint)
    if context_profile is not None:
        return context_profile
    return default_profile


def profile_identity_names(profile: StreamerProfile) -> tuple[str, ...]:
    """返回可用于识别当前主播的正式名和配置称呼。"""

    names = {
        profile.canonical_name,
        profile.report_name,
        *profile.aliases,
        *profile.path_keywords,
    }
    short_name = re.sub(
        r"[A-Za-z][A-Za-z0-9_. -]*$",
        "",
        profile.canonical_name,
    ).strip()
    if len(short_name) >= 2:
        names.add(short_name)
    return tuple(
        sorted((name for name in names if name), key=len, reverse=True)
    )


def profile_matches_streamer(
        profile: StreamerProfile, streamer_name: str | None) -> bool:
    """判断显式主播称呼是否属于给定 profile。"""

    name = str(streamer_name or "").strip()
    return not name or name in profile_identity_names(profile)


def infer_streamer_name(
        video_path: str | os.PathLike[str] | None) -> str:
    """从当前任务快照或录播路径解析主播正式名。"""

    active = active_streamer_profile()
    profile = active or resolve_streamer_profile("auto", video_path)
    return profile.canonical_name


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
    profiles, _default_profile_id = load_streamer_profiles()
    return profiles[GENERIC_PROFILE_ID]


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
