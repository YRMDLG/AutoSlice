"""AutoCover 的主播/近期视觉风格记忆。

只记忆可迁移的视觉语言，不保存精确坐标、背景帧或文案。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from autoslice_cover.text_layout import DEFAULT_TEXT_STYLE

from .cover_model import TextStyle


@dataclass(frozen=True, slots=True)
class CoverStyleMemory:
    font_family: str = str(DEFAULT_TEXT_STYLE["font_family"])
    font_weight: int = 900
    headline_size: int = 104
    context_size_ratio: float = 0.70
    fill_color: str = "#FFE438"
    stroke_color: str = "#111111"
    stroke_width: int = 6
    shadow: bool = True
    line_spacing: float = 1.12
    preferred_asset_ids: tuple[str, ...] = ()
    outer_stroke: str = ""
    outer_stroke_width: int = 0
    backdrop: str = ""

    def text_style(self, *, role: str = "B") -> TextStyle:
        size = self.headline_size if role == "B" else max(24, round(self.headline_size * self.context_size_ratio))
        return TextStyle(
            font_family=self.font_family,
            font_weight=self.font_weight if role == "B" else max(100, self.font_weight - 200),
            font_size=size,
            fill=self.fill_color,
            stroke=self.stroke_color,
            stroke_width=self.stroke_width if role == "B" else max(1, self.stroke_width - 2),
            shadow=self.shadow,
            line_spacing=self.line_spacing,
            outer_stroke=self.outer_stroke,
            outer_stroke_width=self.outer_stroke_width,
            backdrop=self.backdrop,
        )


_TAG_RE = re.compile(r"^\s*[〖【\[](.{1,32}?)[〗】\]]")


def streamer_key(title: str | None) -> str | None:
    """投稿标题前缀里的主播名；风格按主播继承，而不是按单个项目。"""

    match = _TAG_RE.match(str(title or ""))
    return (match.group(1).strip() or None) if match else None


@dataclass(frozen=True, slots=True)
class StylePreset:
    """一键样式：只改颜色与效果，不动字号、字体和位置。"""

    key: str
    label: str
    fill: str
    stroke: str
    stroke_width: int
    shadow: bool
    outer_stroke: str = ""
    outer_stroke_width: int = 0
    backdrop: str = ""
    # A 的填充色；为空时与 B 相同（双色预设用）。
    context_fill: str = ""

    def apply(self, style: TextStyle, role: str = "B") -> TextStyle:
        return replace(
            style,
            fill=self.context_fill if role == "A" and self.context_fill else self.fill,
            stroke=self.stroke,
            stroke_width=self.stroke_width,
            shadow=self.shadow,
            outer_stroke=self.outer_stroke,
            outer_stroke_width=self.outer_stroke_width,
            backdrop=self.backdrop,
        )


# 直播切片封面的常用组合；第一项即默认样式。
STYLE_PRESETS: tuple[StylePreset, ...] = (
    StylePreset("classic", "黄字黑边", "#FFE438", "#111111", 6, True),
    StylePreset("duo", "黄青双色", "#12D8E6", "#111111", 8, True, context_fill="#FFE438"),
    StylePreset("white", "白字黑边", "#FFFFFF", "#111111", 6, True),
    StylePreset("double", "双层描边", "#FFE438", "#111111", 6, True, "#FFFFFF", 6),
    StylePreset("red-bar", "红底白字", "#FFFFFF", "#7A0A10", 2, False, backdrop="#E3262FF0"),
    StylePreset("yellow-bar", "黄底黑字", "#111111", "#111111", 0, False, backdrop="#FFE438F5"),
    StylePreset("dark-bar", "暗底白字", "#FFFFFF", "#000000", 2, False, backdrop="#000000B0"),
)


class CoverStyleMemoryStore:
    def __init__(self, root: Path | str) -> None:
        self.path = Path(root).expanduser().resolve() / "cover-style-memory.json"

    @staticmethod
    def _key(streamer: str | None, recent_titles: tuple[str, ...] = ()) -> str:
        value = "\0".join([streamer or "通用", *recent_titles[:8]])
        return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:24]

    def load(self, streamer: str | None = None, recent_titles: tuple[str, ...] = ()) -> CoverStyleMemory:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return CoverStyleMemory()
        records = payload.get("records") if isinstance(payload, dict) else None
        raw = records.get(self._key(streamer, recent_titles)) if isinstance(records, dict) else None
        if not isinstance(raw, dict):
            raw = records.get(self._key(streamer)) if isinstance(records, dict) else None
        if not isinstance(raw, dict) and streamer:
            # 新主播还没有记忆时沿用最近一次确认的通用风格。
            raw = records.get(self._key(None)) if isinstance(records, dict) else None
        if not isinstance(raw, dict):
            return CoverStyleMemory()
        try:
            values = dict(raw)
            values["preferred_asset_ids"] = tuple(values.get("preferred_asset_ids", ()))
            return CoverStyleMemory(**values)
        except (TypeError, ValueError):
            return CoverStyleMemory()

    def save(
        self,
        memory: CoverStyleMemory,
        *,
        streamer: str | None = None,
        recent_titles: tuple[str, ...] = (),
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        records = payload.setdefault("records", {})
        if not isinstance(records, dict):
            records = {}
            payload["records"] = records
        records[self._key(streamer, recent_titles)] = asdict(memory)
        if streamer:
            records[self._key(None)] = asdict(memory)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

