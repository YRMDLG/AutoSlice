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

from .cover_model import DEFAULT_STROKE_RATIO, TextStyle, stroke_for


@dataclass(frozen=True, slots=True)
class CoverStyleMemory:
    font_family: str = str(DEFAULT_TEXT_STYLE["font_family"])
    font_weight: int = 900
    headline_size: int = 104
    context_size_ratio: float = 0.70
    fill_color: str = "#FFE438"
    stroke_color: str = "#111111"
    stroke_width: int = 8
    shadow: bool = True
    line_spacing: float = 1.12
    preferred_asset_ids: tuple[str, ...] = ()
    outer_stroke: str = ""
    outer_stroke_width: int = 0
    backdrop: str = ""
    # A 的颜色；为空时与 B 相同（黄青、黄红等双色风格靠它延续到下一个封面）。
    context_fill: str = ""
    context_stroke: str = ""
    # 强调词颜色；为空时自动配色。
    accent: str = ""

    def text_style(self, *, role: str = "B") -> TextStyle:
        size = self.headline_size if role == "B" else max(24, round(self.headline_size * self.context_size_ratio))
        context = role == "A"
        # 描边按字号比例：A 字小，描边跟着细一点。
        ratio = size / max(1, self.headline_size)
        return TextStyle(
            font_family=self.font_family,
            font_weight=self.font_weight if role == "B" else max(100, self.font_weight - 200),
            font_size=size,
            fill=self.context_fill if context and self.context_fill else self.fill_color,
            stroke=self.context_stroke if context and self.context_stroke else self.stroke_color,
            stroke_width=max(1, round(self.stroke_width * ratio)) if self.stroke_width else 0,
            shadow=self.shadow,
            line_spacing=self.line_spacing,
            outer_stroke=self.outer_stroke,
            outer_stroke_width=self.outer_stroke_width,
            backdrop=self.backdrop,
            accent=self.accent,
        )


def _rgb(color: str) -> tuple[int, int, int] | None:
    value = str(color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(character * 2 for character in value)
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except (ValueError, IndexError):
        return None


def _luminance(color: str) -> float:
    rgb = _rgb(color)
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255 if rgb else 0.5


def accent_for(fill: str, stroke: str) -> str:
    """没指定强调色时按填充和描边挑一个对比色：黑边黄字配青、黑边白字配黄，白边彩字配黑。"""

    if _luminance(stroke) > 0.6:
        return "#111111" if _luminance(fill) > 0.25 else "#F44336"
    rgb = _rgb(fill)
    if rgb and rgb[0] > 200 and rgb[1] > 170 and rgb[2] < 130:
        return "#16D8ED"
    return "#FFE438"


_TAG_RE = re.compile(r"^\s*[〖【\[](.{1,32}?)[〗】\]]")


def streamer_key(title: str | None) -> str | None:
    """投稿标题前缀里的主播名；风格按主播继承，而不是按单个项目。"""

    match = _TAG_RE.match(str(title or ""))
    return (match.group(1).strip() or None) if match else None


@dataclass(frozen=True, slots=True)
class StylePreset:
    """一键样式：只改颜色与效果，不动字号、字体和位置；描边按字号比例。"""

    key: str
    label: str
    fill: str
    stroke: str
    stroke_ratio: float
    shadow: bool
    outer_stroke: str = ""
    outer_stroke_ratio: float = 0.0
    backdrop: str = ""
    # A 的填充与描边；为空时与 B 相同（双色预设用）。
    context_fill: str = ""
    context_stroke: str = ""

    def apply(self, style: TextStyle, role: str = "B") -> TextStyle:
        context = role == "A"
        return replace(
            style,
            fill=self.context_fill if context and self.context_fill else self.fill,
            stroke=self.context_stroke if context and self.context_stroke else self.stroke,
            stroke_width=stroke_for(style.font_size, self.stroke_ratio),
            shadow=self.shadow,
            outer_stroke=self.outer_stroke,
            outer_stroke_width=stroke_for(style.font_size, self.outer_stroke_ratio) if self.outer_stroke else 0,
            backdrop=self.backdrop,
            # 换配色时强调色回到自动，跟着新配色走。
            accent="",
        )


# 配色参考 B 站“绝对忠诚的Y”的切片封面：黄字粗黑边为主，第二句用青、红（白边）或紫（白边）。
_YELLOW, _CYAN, _RED, _PURPLE, _INK = "#FFE438", "#16D8ED", "#F44336", "#6739C6", "#111111"
STYLE_PRESETS: tuple[StylePreset, ...] = (
    StylePreset("classic", "全黄黑边", _YELLOW, _INK, DEFAULT_STROKE_RATIO, True),
    StylePreset("duo", "黄青", _CYAN, _INK, DEFAULT_STROKE_RATIO, True, context_fill=_YELLOW),
    StylePreset("yellow-red", "黄红", _RED, "#FFFFFF", DEFAULT_STROKE_RATIO, True, context_fill=_YELLOW, context_stroke=_INK),
    StylePreset("yellow-purple", "黄紫", _PURPLE, "#FFFFFF", DEFAULT_STROKE_RATIO, True, context_fill=_YELLOW, context_stroke=_INK),
    StylePreset("white", "白字黑边", "#FFFFFF", _INK, DEFAULT_STROKE_RATIO, True),
    StylePreset("double", "双层描边", _YELLOW, _INK, 0.06, True, "#FFFFFF", 0.05),
    StylePreset("dark-bar", "暗底白字", "#FFFFFF", "#000000", 0.02, False, backdrop="#000000B0"),
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

