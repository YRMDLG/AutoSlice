"""AutoCover 的画布规格和历史封面风格定义。"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class CanvasSpec:
    """描述一种 B 站封面画布及文字安全区。"""

    key: str
    label: str
    width: int
    height: int
    margin_x: int
    text_top: int
    text_bottom: int
    focus_x: float = 0.5

    @property
    def aspect_ratio(self) -> float:
        """返回画布宽高比。"""

        return self.width / self.height

    def to_dict(self) -> dict[str, int | float | str]:
        """返回适合 API 输出的字典。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class CoverPalette:
    """描述一套按文字语义使用的历史封面调色板。"""

    key: str
    label: str
    context_color: str
    quote_color: str
    emphasis_color: str
    neutral_color: str
    stroke_color: str = "#111111"
    shadow_color: str = "#000000B8"
    context_stroke_color: str | None = None
    quote_stroke_color: str | None = None
    emphasis_stroke_color: str | None = None
    neutral_stroke_color: str | None = None

    @property
    def line_colors(self) -> tuple[str, ...]:
        """保留旧渲染器使用的颜色序列。"""

        return (
            self.context_color,
            self.quote_color,
            self.emphasis_color,
            self.neutral_color,
        )

    def color_for_line(self, index: int) -> str:
        """返回指定行的兼容颜色；新代码应优先使用语义角色。"""

        return self.line_colors[index % len(self.line_colors)]

    def color_for_role(self, role: str) -> str:
        """按背景、原话、爆点或中性角色返回颜色。"""

        role_colors = {
            "context": self.context_color,
            "quote": self.quote_color,
            "emphasis": self.emphasis_color,
            "neutral": self.neutral_color,
        }
        try:
            return role_colors[role]
        except KeyError as exc:
            supported = "、".join(role_colors)
            raise ValueError(f"不支持的文字角色：{role}；可选值为 {supported}") from exc

    def stroke_for_role(self, role: str) -> str:
        """按语义角色返回描边色；未单独设置时使用模板通用描边。"""

        role_colors = {
            "context": self.context_stroke_color,
            "quote": self.quote_stroke_color,
            "emphasis": self.emphasis_stroke_color,
            "neutral": self.neutral_stroke_color,
        }
        try:
            return role_colors[role] or self.stroke_color
        except KeyError as exc:
            supported = "、".join(role_colors)
            raise ValueError(f"不支持的文字角色：{role}；可选值为 {supported}") from exc

    def to_dict(self) -> dict[str, str | None]:
        """返回适合 API 输出的字典。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class CoverTemplate:
    """描述文字构图模板。"""

    key: str
    label: str
    default_palette_key: str
    max_lines: int
    max_line_units: int
    layout: str
    text_top_ratio: float
    text_bottom_ratio: float
    background_mode: str = "frame"
    subject_anchor: str = "right"
    text_anchor: str = "left"
    supports_sticker: bool = True

    def to_dict(self) -> dict[str, bool | int | float | str]:
        """返回适合 API 输出的字典。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class VisualStyleRecommendation:
    """封面模板与调色板的自动推荐结果。"""

    template_key: str
    palette_key: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        """返回适合 API 输出的字典。"""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class CoverStyle:
    """描述账号封面的整体视觉语言。"""

    key: str
    label: str
    default_template_key: str
    default_palette_key: str
    background_dim: float
    max_lines: int
    max_line_units: int

    def color_for_line(self, index: int, palette_key: str | None = None) -> str:
        """按指定调色板返回第 index 行的颜色。"""

        return get_palette(palette_key or self.default_palette_key).color_for_line(index)

    def color_for_role(self, role: str, palette_key: str | None = None) -> str:
        """按文字语义角色获取颜色，避免机械地逐行轮换。"""

        return get_palette(palette_key or self.default_palette_key).color_for_role(role)


PERSONAL_16_9 = CanvasSpec(
    key="16x9",
    label="个人空间 16:9",
    width=1920,
    height=1080,
    margin_x=72,
    text_top=36,
    text_bottom=700,
)

HOME_4_3 = CanvasSpec(
    key="4x3",
    label="首页推荐 4:3",
    width=1440,
    height=1080,
    margin_x=58,
    text_top=36,
    text_bottom=710,
)

CANVAS_SPECS = {
    PERSONAL_16_9.key: PERSONAL_16_9,
    HOME_4_3.key: HOME_4_3,
}

PALETTES = {
    "latest_cyan": CoverPalette(
        key="latest_cyan",
        label="秩序式黄青高对比",
        context_color="#FFE438",
        quote_color="#16D8ED",
        emphasis_color="#16D8ED",
        neutral_color="#FFFFFF",
    ),
    "latest_conflict": CoverPalette(
        key="latest_conflict",
        label="秩序式黄红紫反差",
        context_color="#FFE438",
        quote_color="#F44336",
        emphasis_color="#6739C6",
        neutral_color="#FFFFFF",
        quote_stroke_color="#FFFFFF",
        emphasis_stroke_color="#FFFFFF",
    ),
    "latest_secret": CoverPalette(
        key="latest_secret",
        label="秩序式秘密黄红青",
        context_color="#FFE438",
        quote_color="#F44336",
        emphasis_color="#16D8ED",
        neutral_color="#FFFFFF",
        quote_stroke_color="#FFFFFF",
    ),
    "latest_soft": CoverPalette(
        key="latest_soft",
        label="秩序式黄紫青对话",
        context_color="#FFE438",
        quote_color="#6739C6",
        emphasis_color="#16D8ED",
        neutral_color="#FFFFFF",
        quote_stroke_color="#FFFFFF",
    ),
    "latest_yellow": CoverPalette(
        key="latest_yellow",
        label="秩序式全黄短标题",
        context_color="#FFE438",
        quote_color="#FFE438",
        emphasis_color="#FFE438",
        neutral_color="#FFFFFF",
    ),
    "classic": CoverPalette(
        key="classic",
        label="直播黄青粉白",
        context_color="#FFE34D",
        quote_color="#32DDF2",
        emphasis_color="#FF76A8",
        neutral_color="#FFFFFF",
    ),
    "conflict": CoverPalette(
        key="conflict",
        label="冲突黄青红粉",
        context_color="#FFE34D",
        quote_color="#27DDF2",
        emphasis_color="#FF4E43",
        neutral_color="#FF8EBC",
    ),
    "soft": CoverPalette(
        key="soft",
        label="温柔黄青粉白",
        context_color="#FFE96A",
        quote_color="#69E4F2",
        emphasis_color="#FF9ABD",
        neutral_color="#FFFFFF",
    ),
    "night_purple": CoverPalette(
        key="night_purple",
        label="晚安紫黄",
        context_color="#FFE34D",
        quote_color="#FFFFFF",
        emphasis_color="#FFD85A",
        neutral_color="#E9D8FF",
    ),
    "golden_room": CoverPalette(
        key="golden_room",
        label="旧版晚安金黄",
        context_color="#FFE34D",
        quote_color="#FFF0A3",
        emphasis_color="#FFFFFF",
        neutral_color="#FFD86C",
    ),
    "warning": CoverPalette(
        key="warning",
        label="警告黑黄红",
        context_color="#171717",
        quote_color="#FFD83D",
        emphasis_color="#FF493D",
        neutral_color="#171717",
        stroke_color="#F5F2E8",
        shadow_color="#00000050",
    ),
    "dark_stage": CoverPalette(
        key="dark_stage",
        label="舞台黑金白",
        context_color="#FFE34D",
        quote_color="#FFFFFF",
        emphasis_color="#FFF2A0",
        neutral_color="#E6D5FF",
    ),
    "media": CoverPalette(
        key="media",
        label="视频反应黄白青",
        context_color="#FFE34D",
        quote_color="#FFFFFF",
        emphasis_color="#34DDF2",
        neutral_color="#FF8FB8",
    ),
    "minimal": CoverPalette(
        key="minimal",
        label="极简黑白红",
        context_color="#171717",
        quote_color="#FFFFFF",
        emphasis_color="#EF493F",
        neutral_color="#FFE34D",
        stroke_color="#F7F4EC",
        shadow_color="#00000040",
    ),
}

TEMPLATES = {
    "dialog": CoverTemplate(
        key="dialog",
        label="直播对话/原话反转",
        default_palette_key="latest_cyan",
        max_lines=4,
        max_line_units=28,
        layout="context_quote_emphasis",
        text_top_ratio=0.025,
        text_bottom_ratio=0.88,
    ),
    "headline": CoverTemplate(
        key="headline",
        label="短标题头条",
        default_palette_key="latest_yellow",
        max_lines=2,
        max_line_units=34,
        layout="top_bottom",
        text_top_ratio=0.035,
        text_bottom_ratio=0.88,
    ),
    "evidence": CoverTemplate(
        key="evidence",
        label="SC/私信/评论证据卡",
        default_palette_key="latest_cyan",
        max_lines=4,
        max_line_units=27,
        layout="evidence_split",
        text_top_ratio=0.025,
        text_bottom_ratio=0.92,
        background_mode="evidence",
    ),
    "reaction": CoverTemplate(
        key="reaction",
        label="看视频/二创反应",
        default_palette_key="media",
        max_lines=3,
        max_line_units=30,
        layout="media_caption",
        text_top_ratio=0.025,
        text_bottom_ratio=0.92,
        background_mode="media",
        subject_anchor="auto",
    ),
    "gameplay": CoverTemplate(
        key="gameplay",
        label="游戏画面爆点",
        default_palette_key="latest_cyan",
        max_lines=3,
        max_line_units=30,
        layout="game_caption",
        text_top_ratio=0.025,
        text_bottom_ratio=0.92,
        background_mode="gameplay",
        subject_anchor="auto",
    ),
    "night": CoverTemplate(
        key="night",
        label="晚安小音音系列",
        default_palette_key="night_purple",
        max_lines=2,
        max_line_units=32,
        layout="series_center",
        text_top_ratio=0.035,
        text_bottom_ratio=0.82,
        background_mode="series_asset",
        subject_anchor="center",
        text_anchor="center",
        supports_sticker=False,
    ),
    "performance": CoverTemplate(
        key="performance",
        label="唱歌/3D/舞台",
        default_palette_key="dark_stage",
        max_lines=2,
        max_line_units=32,
        layout="edge_caption",
        text_top_ratio=0.025,
        text_bottom_ratio=0.94,
        background_mode="full_scene",
        subject_anchor="center",
    ),
    "poster": CoverTemplate(
        key="poster",
        label="纪念/活动海报",
        default_palette_key="dark_stage",
        max_lines=2,
        max_line_units=30,
        layout="poster_minimal",
        text_top_ratio=0.03,
        text_bottom_ratio=0.94,
        background_mode="poster",
        subject_anchor="center",
        text_anchor="center",
        supports_sticker=False,
    ),
    "warning": CoverTemplate(
        key="warning",
        label="警告/极简整活",
        default_palette_key="warning",
        max_lines=3,
        max_line_units=30,
        layout="center",
        text_top_ratio=0.18,
        text_bottom_ratio=0.80,
        background_mode="minimal",
        subject_anchor="center",
        text_anchor="center",
        supports_sticker=False,
    ),
}

MELODY_STYLE = CoverStyle(
    key="melody",
    label="秩序式高冲击 × 音音双比例风格",
    default_template_key="dialog",
    default_palette_key="latest_cyan",
    background_dim=0.0,
    max_lines=4,
    max_line_units=28,
)


def get_canvas_spec(key: str) -> CanvasSpec:
    """根据 key 获取画布规格。

    Raises:
        ValueError: key 不是受支持的画布比例。
    """

    try:
        return CANVAS_SPECS[key]
    except KeyError as exc:
        supported = "、".join(CANVAS_SPECS)
        raise ValueError(f"不支持的封面比例：{key}；可选值为 {supported}") from exc


def get_palette(key: str) -> CoverPalette:
    """根据 key 获取调色板。"""

    try:
        return PALETTES[key]
    except KeyError as exc:
        supported = "、".join(PALETTES)
        raise ValueError(f"不支持的调色板：{key}；可选值为 {supported}") from exc


def get_template(key: str) -> CoverTemplate:
    """根据 key 获取构图模板。"""

    try:
        return TEMPLATES[key]
    except KeyError as exc:
        supported = "、".join(TEMPLATES)
        raise ValueError(f"不支持的封面模板：{key}；可选值为 {supported}") from exc
