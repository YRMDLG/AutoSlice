"""话题引擎与字幕工作流共享的中立 SRT、ASR 与标题契约。

本模块不导入 ``topic_engine``、``subtitle_workflow`` 或 ``app``。需要执行
转录或应用账号标题策略时，由高层调用方显式注入服务。
"""

import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional, Protocol

DEFAULT_SUBTITLE_MAX_CHARS = 16
DEFAULT_MAX_PUBLISH_TITLE_CHARS = 80
DEFAULT_SUBTITLE_GLOSSARY = (
    "提督",
    "舰长",
    "SC",
    "娃衣",
    "bangumi",
)


class TranscriptionService(Protocol):
    """字幕工作流可注入的 ASR 服务签名。"""

    def __call__(
            self, video_path: str, progress_callback: Optional[Callable] = None,
            checkpoint_path: Optional[str] = None,
            foreground_only: bool | None = None,
            background_filter_mode: str | None = None) -> Optional[str]:
        ...


@dataclass(frozen=True)
class SubtitleTitleServices:
    """字幕标题阶段需要的账号策略，避免反向导入话题引擎。"""

    max_publish_title_chars: int
    build_title_style_prompt: Callable[..., str]
    build_title_hook_prompt_guide: Callable[[], str]
    normalise_publish_title: Callable[[Any, str], str]

    def __post_init__(self) -> None:
        if self.max_publish_title_chars <= 0:
            raise ValueError("投稿标题长度上限必须是正整数")
        for name in (
                "build_title_style_prompt",
                "build_title_hook_prompt_guide",
                "normalise_publish_title"):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} 必须可调用")


@dataclass(frozen=True)
class SubtitleCue:
    """一条严格保留序号、时间轴、设置和正文的 SRT 字幕。"""

    index: int
    start: str
    end: str
    settings: str
    text: str

    @property
    def start_seconds(self) -> float:
        return srt_timestamp_seconds(self.start)

    @property
    def end_seconds(self) -> float:
        return srt_timestamp_seconds(self.end)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["start_seconds"] = self.start_seconds
        result["end_seconds"] = self.end_seconds
        return result


def srt_timestamp_seconds(value: str) -> float:
    """把 SRT 时间戳转换为视频内秒数。"""
    parts = str(value).replace(".", ",").split(":")
    if len(parts) != 3 or "," not in parts[2]:
        raise ValueError(f"无效 SRT 时间: {value}")
    second, millisecond = parts[2].split(",", 1)
    try:
        return (
            int(parts[0]) * 3600
            + int(parts[1]) * 60
            + int(second)
            + int(millisecond) / 1000.0
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效 SRT 时间: {value}") from exc


def normalise_generic_publish_title(
        raw_title: Any, topic_title: str,
        max_chars: int = DEFAULT_MAX_PUBLISH_TITLE_CHARS) -> str:
    """无账号假设的安全标题归一化，供未注入 profile 时降级使用。"""
    title = "" if raw_title is None else str(raw_title)
    title = title.replace("**", "").replace("`", "")
    title = re.sub(
        r"^\s*(?:publish_title|投稿标题(?:建议)?)\s*[：:]\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s+", " ", title).strip(" \t\r\n-—")
    title = re.sub(r"^【[^】]{1,40}】", "", title, count=1).strip()
    fallback = re.sub(r"\s+", " ", str(topic_title or "视频片段")).strip()
    if (
            not title
            or len(title) > max_chars
            or len(re.sub(r"\s+", "", title)) < 4
            or any(token in title for token in ('{"topics"', "```", "\\n"))):
        title = fallback or "视频片段"
    return title[:max_chars]


__all__ = [
    "DEFAULT_MAX_PUBLISH_TITLE_CHARS",
    "DEFAULT_SUBTITLE_GLOSSARY",
    "DEFAULT_SUBTITLE_MAX_CHARS",
    "SubtitleCue",
    "SubtitleTitleServices",
    "TranscriptionService",
    "normalise_generic_publish_title",
    "srt_timestamp_seconds",
]
