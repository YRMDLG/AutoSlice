"""Qt 视觉令牌：夜航（深色）与晨雾（浅色）两套主题共用布局和组件，只换令牌与动效档。

- COLORS / STYLE 是“当前主题”：切换时原地改字段，各模块持有的引用随之生效，不用逐个改调用点。
- 颜色值是带 role 的字符串（ColorToken）：图标、局部样式据此在换主题时重新取色。
- 设计稿 CSS 里的 color-mix 在这里用 Python 预先混成具体色值。
- 本模块不依赖 Qt；切换主题后刷新界面的工作在 appearance 模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

from .glyphs import icon_file


class ColorToken(str):
    """颜色字符串，记着自己是哪个令牌；换主题时按 role 重新取色。"""

    role: str

    def __new__(cls, value: str, role: str = "") -> ColorToken:
        token = super().__new__(cls, value)
        token.role = role
        return token


def _channels(value: str) -> tuple[int, int, int]:
    text = value.lstrip("#")
    if len(text) == 8:  # #AARRGGBB：只取颜色部分
        text = text[2:]
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def color_mix(a: str, weight: float, b: str) -> str:
    """等价 CSS color-mix(in srgb, a weight, b)：a 占 weight，b 占其余。"""

    weight = max(0.0, min(1.0, weight))
    first, second = _channels(a), _channels(b)
    mixed = (round(x * weight + y * (1 - weight)) for x, y in zip(first, second))
    return "#" + "".join(f"{value:02X}" for value in mixed)


def rgba(value: str, alpha: float) -> str:
    """样式表用的 rgba()；alpha 取 0~1，换算成 Qt 样式表要的 0~255。"""

    r, g, b = _channels(value)
    return f"rgba({r}, {g}, {b}, {round(max(0.0, min(1.0, alpha)) * 255)})"


def luminance(value: str) -> float:
    """WCAG 相对亮度，用于对比度自检。"""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _channels(value)
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@dataclass
class Palette:
    """当前主题的全部颜色。默认值是夜航；旧字段名保留，供各模块沿用。"""

    # ── 表面 ──
    ground: str = "#0A0A0A"          # 窗口底色
    canvas: str = "#0A0A0A"          # 旧名：同 ground
    card: str = "#141413"            # 悬浮卡片
    card_border: str = "#292826"
    card_highlight: str = "#1C1C1B"  # 卡片顶边极淡高光
    nav: str = "#141413"
    rail: str = "#141413"
    panel: str = "#141413"
    raised: str = "#1D1D1C"
    raised_top: str = "#232322"
    highlight: str = "#302F2C"
    overlay: str = "#1A1A19"         # 下拉菜单、弹层
    overlay_border: str = "#363432"
    dialog: str = "#171716"
    toast: str = "#1E1E1D"
    toast_border: str = "#3A3835"
    well: str = "#111111"            # 输入框、下拉框
    player: str = "#050505"
    scrim: str = "#050505"           # 对话框遮罩（配合 STYLE.scrim_alpha）
    shadow: str = "#000000"          # 投影颜色（配合 STYLE.shadow_alpha）
    # ── 强调色 ──
    accent: str = "#D8E6F7"
    accent_line: str = "#656B72"     # 选中描边、细线
    accent_soft: str = "#303336"     # 强调色 14% 混卡片色
    accent_ink: str = "#D8E6F7"      # 卡片上的强调文字/图标
    accent_fill: str = "#D8E6F7"     # 进度、播放中填充
    primary_text: str = "#D8E6F7"    # 主按钮文字与图标
    on_accent: str = "#0E0E0D"       # 实心强调色上的文字
    accent_text: str = "#D8E6F7"     # 旧名：同 accent_ink
    accent_top: str = "#D8E6F7"
    accent_edge: str = "#D8E6F7"
    accent_medium: str = "#656B72"
    accent_subtle: str = "#303336"
    accent_hover: str = "#E4EEFA"
    accent_pressed: str = "#9DA6B1"
    accent_tint: str = "#3F4449"
    focus_ring: str = "#D8E6F7"
    # ── 文字 ──
    text: str = "#ECEAE6"
    text_secondary: str = "#D8D5CF"
    muted: str = "#A3A09A"
    subtle: str = "#8C8984"
    placeholder: str = "#6E6B66"
    disabled: str = "#575552"
    # ── 线 ──
    divider: str = "#232221"
    border: str = "#292826"
    control_border: str = "#363432"
    control_hover_border: str = "#4E4C48"
    # ── 交互状态 ──
    hover: str = "#191918"
    button_hover: str = "#1F1F1E"
    button_pressed: str = "#111110"
    button_hover_border: str = "#4E4C48"
    project_hover: str = "#1D1D1C"
    project_selected: str = "#222221"
    row_selected: str = "#181817"
    playback: str = "#1A1A19"
    playback_border: str = "#46443F"
    scrollbar: str = "#33312E"
    scrollbar_hover: str = "#4A4844"
    segment_track: str = "#141413"
    segment_track_border: str = "#292826"
    segment_thumb: str = "#141413"
    segment_thumb_border: str = "#656B72"
    # ── 语义 ──
    success: str = "#8FD4AE"
    success_bg: str = "#1D2A23"
    warning: str = "#E8B07A"
    warning_border: str = "#4A3F30"
    danger: str = "#E88A80"
    danger_bg: str = "#2E1D1B"
    ai_removed: str = "#F2958C"
    # 要删的字的底色：套上应用样式表后 Qt 富文本的删除线几乎画不出来，靠底色标出
    ai_removed_bg: str = "#3D2523"
    ai_added: str = "#8FD4AE"
    ai_added_bg: str = "#202C26"
    # ── 时间轴 ──
    timeline_bg: str = "#0F0F0E"
    timeline_border: str = "#21201F"
    tick: str = "#232221"
    wave: str = "#292826"
    block: str = "#1D1D1C"
    block_border: str = "#2C2B29"
    block_text: str = "#A3A09A"
    block_playing: str = "#282725"
    block_playing_border: str = "#46443F"
    block_playing_text: str = "#D8D5CF"
    block_selected: str = "#303336"
    block_selected_border: str = "#D8E6F7"
    block_selected_text: str = "#ECEAE6"
    playhead: str = "#D8E6F7"


@dataclass
class ThemeStyle:
    """当前主题的非颜色令牌：圆角、按钮形态、投影和动效档。"""

    name: str = "night"
    label: str = "夜航"
    dark: bool = True
    card_radius: int = 14
    control_radius: int = 8
    menu_radius: int = 12
    dialog_radius: int = 16
    block_radius: int = 8
    row_radius: int = 10
    gap: int = 10
    # 夜航所有按钮只描边；晨雾次按钮白底、主按钮实心强调色
    filled_buttons: bool = False
    filled_primary: bool = False
    card_shadow: bool = False
    shadow_alpha: float = 0.7
    scrim_alpha: float = 0.55
    press_scale: float = 0.96


@dataclass(frozen=True)
class ThemeSpec:
    name: str
    label: str
    accents: tuple[str, ...]
    grounds: tuple[str, ...] = field(default_factory=tuple)

    @property
    def default_accent(self) -> str:
        return self.accents[0]

    @property
    def default_ground(self) -> str:
        return self.grounds[0] if self.grounds else ""


# 强调色、底色选项取自设计稿 data-props
THEMES: dict[str, ThemeSpec] = {
    "night": ThemeSpec("night", "夜航", ("#D8E6F7", "#E4EEFA", "#CCDDF3", "#EEF3F9"),
                       ("#0A0A0A", "#000000", "#0E0D0C")),
    "morning": ThemeSpec("morning", "晨雾", ("#D5DEE8", "#DCE3EA", "#CBD6E2", "#E1E6EB")),
}
DEFAULT_THEME = "night"


def _night(accent: str, ground: str) -> dict[str, str]:
    card = "#141413"
    line = color_mix(accent, 0.45, card)
    soft = color_mix(accent, 0.14, card)
    return {
        "ground": ground, "canvas": ground, "card": card, "card_border": "#292826",
        "card_highlight": "#1C1C1B", "nav": card, "rail": card, "panel": card,
        "raised": "#1D1D1C", "raised_top": "#232322", "highlight": "#302F2C",
        "overlay": "#1A1A19", "overlay_border": "#363432", "dialog": "#171716",
        "toast": "#1E1E1D", "toast_border": "#3A3835", "well": "#111111", "player": "#050505",
        "scrim": "#050505", "shadow": "#000000",
        "accent": accent, "accent_line": line, "accent_soft": soft, "accent_ink": accent,
        "accent_fill": accent, "primary_text": accent, "on_accent": "#0E0E0D", "accent_text": accent,
        "accent_top": accent, "accent_edge": accent, "accent_medium": line, "accent_subtle": soft,
        "accent_hover": color_mix(accent, 0.7, "#FFFFFF"), "accent_pressed": color_mix(accent, 0.62, card),
        "accent_tint": color_mix(accent, 0.22, card), "focus_ring": accent,
        "text": "#ECEAE6", "text_secondary": "#D8D5CF", "muted": "#A3A09A", "subtle": "#8C8984",
        "placeholder": "#6E6B66", "disabled": "#575552",
        "divider": "#232221", "border": "#292826", "control_border": "#363432",
        "control_hover_border": "#4E4C48",
        "hover": "#191918", "button_hover": "#1F1F1E", "button_pressed": "#111110",
        "button_hover_border": "#4E4C48", "project_hover": "#1D1D1C", "project_selected": "#222221",
        "row_selected": "#181817", "playback": "#1A1A19", "playback_border": "#46443F",
        "scrollbar": "#33312E", "scrollbar_hover": "#4A4844",
        "segment_track": card, "segment_track_border": "#292826", "segment_thumb": card,
        "segment_thumb_border": line,
        "success": "#8FD4AE", "success_bg": "#1D2A23", "warning": "#E8B07A", "warning_border": "#4A3F30",
        "danger": "#E88A80", "danger_bg": "#2E1D1B",
        "ai_removed": "#F2958C", "ai_removed_bg": "#3D2523", "ai_added": "#8FD4AE",
        "ai_added_bg": color_mix("#8FD4AE", 0.12, card),
        "timeline_bg": "#0F0F0E", "timeline_border": "#21201F", "tick": "#232221", "wave": "#292826",
        "block": "#1D1D1C", "block_border": "#2C2B29", "block_text": "#A3A09A",
        "block_playing": "#282725", "block_playing_border": "#46443F", "block_playing_text": "#D8D5CF",
        "block_selected": soft, "block_selected_border": accent, "block_selected_text": "#ECEAE6",
        "playhead": accent,
    }


def _morning(accent: str, _ground: str) -> dict[str, str]:
    card = "#FFFEFB"
    line = color_mix(accent, 0.70, "#5B6B80")
    soft = color_mix(accent, 0.50, card)
    ink = color_mix(accent, 0.20, "#1F2A38")
    return {
        "ground": "#F4F3F0", "canvas": "#F4F3F0", "card": card, "card_border": "#E7E4DE",
        "card_highlight": card, "nav": card, "rail": card, "panel": card,
        "raised": "#ECEAE5", "raised_top": "#F8F7F3", "highlight": "#FFFFFF",
        "overlay": card, "overlay_border": "#E7E4DE", "dialog": card,
        "toast": card, "toast_border": "#E7E4DE", "well": card, "player": "#121212",
        "scrim": "#F4F3F0", "shadow": "#3C301E",
        "accent": accent, "accent_line": line, "accent_soft": soft, "accent_ink": ink,
        "accent_fill": color_mix(accent, 0.92, "#5B6B80"), "primary_text": "#1C2633",
        "on_accent": "#1C2633", "accent_text": ink,
        "accent_top": accent, "accent_edge": line, "accent_medium": line, "accent_subtle": soft,
        "accent_hover": color_mix(accent, 0.85, "#5B6B80"), "accent_pressed": color_mix(accent, 0.80, "#5B6B80"),
        "accent_tint": soft, "focus_ring": line,
        "text": "#1C1B19", "text_secondary": "#1C1B19", "muted": "#5F5C56", "subtle": "#6E6A63",
        "placeholder": "#A9A59D", "disabled": "#B5B1A9",
        "divider": "#EEECE7", "border": "#E7E4DE", "control_border": "#DAD6CF",
        "control_hover_border": "#C9C4BB",
        "hover": "#F7F6F2", "button_hover": "#F8F7F3", "button_pressed": "#EFEDE8",
        "button_hover_border": "#C9C4BB", "project_hover": "#EFEDE8", "project_selected": "#EAE8E3",
        "row_selected": soft, "playback": "#F4F3F0", "playback_border": "#C9C4BB",
        "scrollbar": "#DAD6CF", "scrollbar_hover": "#BDB8AF",
        "segment_track": "#EFEDE8", "segment_track_border": "#EFEDE8", "segment_thumb": card,
        "segment_thumb_border": card,
        "success": "#1F7A4D", "success_bg": "#E3F1E8", "warning": "#A1561A", "warning_border": "#E8D4BC",
        "danger": "#B42318", "danger_bg": "#FBEDEA",
        "ai_removed": "#B42318", "ai_removed_bg": "#FBEDEA", "ai_added": "#0F6B3E", "ai_added_bg": "#DDF1E4",
        "timeline_bg": "#F7F6F2", "timeline_border": "#EFEDE8", "tick": "#E3E0DA", "wave": "#E2DFD8",
        "block": "#ECEAE5", "block_border": "#E3E0DA", "block_text": "#4D4A45",
        "block_playing": "#DEDBD4", "block_playing_border": "#C9C4BB", "block_playing_text": "#1C2633",
        "block_selected": accent, "block_selected_border": line, "block_selected_text": "#1C2633",
        "playhead": "#1C1B19",
    }


_STYLES = {
    "night": ThemeStyle(),
    "morning": ThemeStyle(
        name="morning", label="晨雾", dark=False, card_radius=12, control_radius=8, menu_radius=12,
        dialog_radius=16, block_radius=8, row_radius=8, gap=12, filled_buttons=True, filled_primary=True,
        card_shadow=True, shadow_alpha=0.3, scrim_alpha=0.55, press_scale=0.94,
    ),
}
_BUILDERS = {"night": _night, "morning": _morning}

COLORS = Palette()
STYLE = ThemeStyle()
_current = {"theme": DEFAULT_THEME, "accent": THEMES[DEFAULT_THEME].default_accent,
            "ground": THEMES[DEFAULT_THEME].default_ground}


def normalize(theme: str | None, accent: str | None = None, ground: str | None = None) -> tuple[str, str, str]:
    """不认识的主题、不在选项里的强调色/底色一律回到默认，配置文件被改坏也不出怪色。"""

    name = theme if theme in THEMES else DEFAULT_THEME
    spec = THEMES[name]
    accent = (accent or "").upper()
    ground = (ground or "").upper()
    accent = accent if accent in spec.accents else spec.default_accent
    ground = ground if ground in spec.grounds else spec.default_ground
    return name, accent, ground


def apply_theme(theme: str | None = DEFAULT_THEME, accent: str | None = None,
                ground: str | None = None) -> tuple[str, str, str]:
    """原地改写 COLORS 与 STYLE；返回规范化后的 (主题, 强调色, 底色)。"""

    name, accent, ground = normalize(theme, accent, ground)
    values = _BUILDERS[name](accent, ground)
    for item in fields(Palette):
        setattr(COLORS, item.name, ColorToken(values[item.name], item.name))
    style = _STYLES[name]
    for item in fields(ThemeStyle):
        setattr(STYLE, item.name, getattr(style, item.name))
    _current.update(theme=name, accent=accent, ground=ground)
    return name, accent, ground


def current_theme() -> tuple[str, str, str]:
    return _current["theme"], _current["accent"], _current["ground"]


@dataclass(frozen=True)
class Metrics:
    nav_width: int = 64
    appbar_height: int = 56
    project_width: int = 252
    project_min_width: int = 188
    ai_width: int = 324
    ai_min_width: int = 280
    ai_collapsed_width: int = 48
    timeline_height: int = 92
    timeline_card_height: int = 164
    button_height: int = 32
    row_height: int = 78
    radius: int = 8
    space_1: int = 4
    space_2: int = 8
    space_3: int = 12
    space_4: int = 16
    space_5: int = 24
    space_6: int = 32
    project_compact_height: int = 62
    # 设计稿字幕行 57px + 行间距 2px；序号列 = 左内边距 12 + 序号 24 + 间距 12
    subtitle_row_height: int = 59
    subtitle_number_width: int = 48
    subtitle_time_width: int = 204
    playhead_hit_width: int = 14
    icon_size: int = 20
    tool_icon_size: int = 16
    control_radius: int = 6
    text_small: int = 11
    text_base: int = 12
    text_body: int = 14
    text_heading: int = 17
    compact_height: int = 850
    # split is calculated after the timeline reservation; these values keep the
    # video around 42–45% and leave a readable subtitle list at common sizes.
    compact_video_fraction: float = 0.48
    wide_video_fraction: float = 0.54


SIZES = Metrics()
# 随程序分发的源流黑体（思源黑体圆角版）为主，依次回退思源黑体、雅黑；时间码等宽
UI_FAMILIES = ("Resource Han Rounded CN", "Noto Sans SC", "Microsoft YaHei UI", "Segoe UI", "Segoe UI Emoji")
MONO_FAMILIES = ("Cascadia Mono", "Consolas")

# 模块加载即落到默认主题，字段全部换成带 role 的 ColorToken
apply_theme(DEFAULT_THEME)


def stylesheet() -> str:
    """当前主题的共享控件样式；局部绘制（按钮底、时间轴、弹层）在 motion / popups 等模块。"""

    c = COLORS
    s = STYLE
    m = SIZES
    ui_font = ", ".join(f"'{name}'" for name in UI_FAMILIES)
    mono_font = ", ".join(f"'{name}'" for name in MONO_FAMILIES)
    arrow = icon_file("chevron_down", c.muted)
    arrow_up = icon_file("chevron_up", c.muted)
    check = icon_file("check", c.on_accent if s.filled_primary else c.accent)
    radius = s.control_radius
    # 标题字重：夜航 500，晨雾 600（设计稿）
    heading_weight = 500 if s.dark else 600
    # 夜航按钮只描边；晨雾次按钮白底。按钮底色实际由 motion.ButtonFx 绘制，这里只管文字与兜底
    button_bg = c.card if s.filled_buttons else "transparent"
    primary_bg = c.accent if s.filled_primary else "transparent"
    primary_border = c.accent_line if s.filled_primary else c.accent
    weight = 600 if s.filled_primary else 500
    selection = c.accent_soft if not s.dark else c.accent_pressed
    selection_text = c.text if not s.dark else c.on_accent
    checkbox_on_bg = c.accent if s.filled_primary else "transparent"
    checkbox_on_border = c.accent_line if s.filled_primary else c.accent
    return f"""
        QWidget {{ color: {c.text}; background: transparent; font-family: {ui_font}; font-size: {m.text_base}px; }}
        QWidget#root {{ background: {c.ground}; }}
        QDialog {{ background: {c.dialog}; }}
        QMessageBox {{ background: {c.dialog}; }}
        QPlainTextEdit#inlineTextEdit {{ background: {c.well}; color: {c.text}; border: 1px solid {c.accent}; border-radius: {radius}px; padding: 4px 6px; font-size: 15px; }}
        QListWidget {{ background: {c.well}; border: 1px solid {c.control_border}; border-radius: {radius}px; padding: 4px; outline: 0; }}
        QListWidget::item {{ padding: 4px 2px; border-radius: 6px; }}
        QListWidget::item:hover {{ background: {c.hover}; }}
        QListWidget::item:selected {{ background: {c.row_selected}; color: {c.text}; border: 1px solid {c.accent_line}; }}
        QListWidget::indicator {{ width: 13px; height: 13px; border: 1px solid {c.control_border}; border-radius: 4px; background: {c.well}; }}
        QListWidget::indicator:checked {{ background: {checkbox_on_bg}; border-color: {checkbox_on_border}; image: url("{check}"); }}
        QWidget#appbar {{ background: transparent; border: none; }}
        QWidget#navRail {{ background: {c.nav}; border-right: 1px solid {c.divider}; }}
        QWidget#projectRail {{ background: transparent; }}
        QWidget#workArea {{ background: transparent; }}
        QWidget#videoSurface {{ background: transparent; }}
        QStackedWidget#videoFrame {{ background: {c.player}; border-radius: 9px; }}
        QWidget#timeline, QWidget#subtitleList, QWidget#aiPanel {{ background: transparent; }}
        QWidget#workToolbar {{ background: {c.ground}; }}
        QWidget#workflowActions {{ background: transparent; border: none; }}
        QWidget#playerControls {{ background: transparent; }}
        QWidget#coverSurface {{ background: {c.player}; border: 1px solid {c.divider}; border-radius: {m.radius}px; }}
        QWidget#settingsSurface {{ background: {c.ground}; }}
        QWidget#card {{ background: {c.card}; border: 1px solid {c.card_border}; border-radius: {s.card_radius}px; }}
        QFrame#hairline {{ background: {c.divider}; max-height: 1px; min-height: 1px; margin: 0 16px; }}
        QLabel#brand {{ font-size: 15px; font-weight: 600; }}
        QLabel#pageTitle {{ font-size: {m.text_heading}px; font-weight: 600; }}
        QLabel#projectTitle {{ font-size: 14px; font-weight: 500; }}
        QLabel#videoFile {{ color: {c.subtle}; font-size: {m.text_small}px; }}
        QLabel#statusLabel {{ color: {c.muted}; font-size: {m.text_small}px; padding: 0 4px; }}
        QLabel#timecode {{ color: {c.text}; font-family: {mono_font}; font-size: 12px; }}
        QLabel#timecodeTotal {{ color: {c.subtle}; font-family: {mono_font}; font-size: 12px; }}
        QLabel#videoInfo {{ color: {c.muted}; font-family: {mono_font}; font-size: 11px; background: transparent; }}
        QLabel#videoPlaceholder {{ color: {c.disabled}; font-size: 12px; background: transparent; }}
        QLabel#mono {{ color: {c.subtle}; font-family: {mono_font}; font-size: 11px; }}
        QLabel#aiDiff {{ font-size: {m.text_body}px; }}
        QWidget#aiSuggestionCard {{ background: {c.card}; border: 1px solid {c.divider}; border-radius: {m.radius}px; }}
        QLabel#aiFieldLabel {{ color: {c.subtle}; font-size: {m.text_small}px; }}
        QLabel#aiMeta {{ color: {c.subtle}; font-size: {m.text_small}px; font-weight: 500; }}
        QLabel#aiOriginal {{ color: {c.muted}; }}
        QLabel#aiReason {{ color: {c.subtle}; }}
        QLabel#aiQueueHeader {{ color: {c.subtle}; font-size: {m.text_small}px; font-weight: 500; }}
        QLabel#sectionTitle {{ font-size: 13px; font-weight: {heading_weight}; }}
        QLabel#cardHeading {{ font-size: 14px; font-weight: {heading_weight}; }}
        QLabel#hintLine {{ color: {c.subtle}; font-size: 11px; }}
        QLabel#navBadge {{ background: {c.accent if s.dark else c.accent_line}; color: {c.on_accent if s.dark else '#FFFFFF'}; border-radius: 7px; font-size: 10px; font-weight: 600; padding: 0 4px; }}
        QLabel#fieldLabel {{ color: {c.muted}; font-size: 12px; background: transparent; }}
        QFrame#canvasWell {{ background: {"#0E0E0D" if s.dark else "#EFEDE8"}; border: 1px solid {c.timeline_border if s.dark else "#EFEDE8"}; border-radius: 9px; }}
        QFrame#valueRow {{ background: {c.well}; border: 1px solid {c.control_border}; border-radius: 8px; }}
        QPushButton#fieldButton {{ text-align: left; padding: 0 30px 0 10px; font-size: 13px; color: {c.text}; }}
        QPushButton#inRow {{ text-align: left; padding: 0 6px; font-size: 12px; color: {c.text}; min-height: 0; }}
        QSpinBox#inRow {{ background: transparent; border: none; font-family: {mono_font}; font-size: 12px; color: {c.muted}; padding: 0 4px; }}
        QCheckBox#switch::indicator {{ width: 34px; height: 20px; border-radius: 10px; image: none; border: 1px solid {c.control_border}; background: {c.well}; }}
        QCheckBox#switch::indicator:checked {{ border-color: {c.accent if s.dark else c.accent_line}; background: {c.accent_soft}; image: none; }}
        QLabel#keyHint {{ color: {c.subtle}; font-family: {mono_font}; font-size: 11px; background: transparent; }}
        QPushButton[keyHint="true"] {{ text-align: left; padding-left: 14px; padding-right: 62px; }}
        QPushButton#chip {{ min-height: 30px; max-height: 30px; padding: 0 10px; font-size: 12px; color: {c.subtle}; }}
        QPushButton#chip:hover {{ color: {c.text}; }}
        QPushButton#chip[warn="true"] {{ color: {c.text}; }}
        QWidget#aiInline {{ background: transparent; }}
        QWidget#findBar {{ background: {c.well}; border: 1px solid {c.card_border}; border-radius: 10px; }}
        QFrame#vline {{ background: {c.card_border}; border: none; }}
        QLabel#settingsHeading {{ font-size: 20px; font-weight: 600; }}
        QPushButton#tocItem {{ text-align: left; padding: 0 12px; min-height: 34px; max-height: 34px; border-radius: 8px; color: {c.muted}; background: transparent; border: 1px solid transparent; }}
        QPushButton#tocItem:hover {{ color: {c.text}; }}
        QPushButton#tocItem:checked {{ color: {c.text}; }}
        QPushButton#aiFix {{ min-height: 0; padding: 0 12px 0 10px; color: {c.text}; }}
        QWidget#coverToolbar {{ background: transparent; border: none; }}
        QWidget#coverPanel {{ background: transparent; border: none; }}
        QWidget#frameStrip {{ background: transparent; border: none; }}
        QWidget#findBar QLineEdit {{ background: {c.card}; padding: 6px 10px; }}
        QLabel#bodyTitle {{ font-size: 20px; font-weight: 600; }}
        QLabel#muted, QLabel#hint {{ color: {c.muted}; }}
        QLabel#subtle {{ color: {c.subtle}; }}
        QLabel#danger {{ color: {c.danger}; }}
        QLabel#success {{ color: {c.success}; }}
        QLabel#badge {{ color: {c.accent_ink}; background: {c.accent_soft}; border: 1px solid {c.accent_line}; border-radius: 4px; padding: 2px 6px; font-size: 11px; }}
        QLabel#quietBadge {{ color: {c.muted}; background: transparent; border: 1px solid {c.control_border}; border-radius: 4px; padding: 2px 6px; font-size: 11px; }}
        QPushButton {{ background: {button_bg}; border: 1px solid {c.control_border}; border-radius: {radius}px; padding: 0 12px; min-height: {m.button_height}px; color: {c.text_secondary}; }}
        QPushButton:hover {{ border-color: {c.control_hover_border}; color: {c.text}; }}
        QPushButton:pressed {{ background: {c.button_pressed}; }}
        QPushButton:checked {{ color: {c.text}; border-color: {c.accent_line}; }}
        QPushButton:disabled {{ color: {c.disabled}; border-color: {c.divider}; }}
        QPushButton#primary {{ color: {c.primary_text}; background: {primary_bg}; border-color: {primary_border}; font-weight: {weight}; }}
        QPushButton#primary:hover {{ color: {c.primary_text}; }}
        QPushButton#primary:disabled, QPushButton#primary:disabled:hover {{ color: {c.disabled}; background: transparent; border-color: {c.divider}; }}
        QPushButton#secondary {{ font-weight: {400 if s.dark else 500}; }}
        QPushButton#secondary:disabled, QPushButton#secondary:disabled:hover {{ color: {c.disabled}; border-color: {c.divider}; }}
        QPushButton#quiet, QPushButton#navButton, QPushButton#projectItem, QPushButton#projectItemCompact {{ background: transparent; border: none; }}
        QPushButton#quiet {{ min-height: 26px; color: {c.muted}; }}
        QPushButton#quiet:hover {{ color: {c.text}; }}
        QPushButton#tool {{ background: transparent; border: 1px solid transparent; border-radius: {radius}px; min-height: 28px; padding: 0 10px; color: {c.muted}; }}
        QPushButton#tool:hover {{ color: {c.text}; }}
        QPushButton#tool:checked {{ color: {c.text}; border-color: transparent; }}
        QPushButton#tool[checkable="true"] {{ min-height: 26px; max-height: 26px; font-size: 12px; color: {c.subtle}; }}
        QPushButton#tool[checkable="true"]:hover, QPushButton#tool[checkable="true"]:checked {{ color: {c.text}; }}
        QPushButton#tool:disabled {{ color: {c.disabled}; background: transparent; border-color: transparent; }}
        QPushButton#tool[square="true"], QPushButton#square {{ padding: 0; min-height: 0; }}
        QPushButton#projectSwitch {{ padding: 0; min-height: 0; text-align: left; }}
        QPushButton#taskRing {{ padding: 0; min-height: 0; border: none; background: transparent; }}
        QPushButton[hasMenu="true"] {{ padding-right: 22px; }}
        QPushButton::menu-indicator {{ image: url("{arrow}"); width: 10px; height: 10px; subcontrol-origin: padding; subcontrol-position: center right; right: 7px; }}
        QWidget#settingsCard {{ background: {c.card}; border: 1px solid {c.card_border}; border-radius: {s.card_radius}px; }}
        QWidget#settingsCard[section="true"] {{ background: transparent; border: none; }}
        QWidget#settingsBox {{ background: transparent; border: 1px solid {c.block_border}; border-radius: 12px; }}
        QWidget#settingsRow {{ background: transparent; }}
        QFrame#rowLine {{ background: {c.divider}; border: none; min-height: 1px; max-height: 1px; }}
        QLabel#sectionHeading {{ font-size: 14px; font-weight: {"500" if STYLE.dark else "600"}; }}
        QLabel#sectionNote {{ color: {c.muted}; font-size: 12px; }}
        QLabel#monoPath {{ font-family: {mono_font}; font-size: 12px; color: {c.text}; }}
        QLabel#cardTitle {{ font-size: 14px; font-weight: 600; }}
        QLabel#formLabel {{ color: {c.muted}; }}
        QWidget#segmented {{ background: transparent; border: none; }}
        QPushButton#segment {{ background: transparent; border: 1px solid transparent; border-radius: 6px; min-height: 24px; padding: 0 12px; color: {c.muted}; }}
        QPushButton#segment:hover {{ color: {c.text}; }}
        QPushButton#segment:checked {{ color: {c.text}; background: transparent; border-color: transparent; }}
        QPushButton#segment:disabled {{ color: {c.disabled}; }}
        QPushButton#quiet:checked {{ color: {c.text}; border: 1px solid {c.accent_line}; }}
        QPushButton#frameThumb {{ padding: 0; min-height: 82px; max-height: 82px; border: none; background: transparent; }}
        QPushButton#schemeThumb {{ padding: 2px; border-radius: 8px; }}
        QPushButton#schemeThumb:checked {{ border: 2px solid {c.accent}; }}
        QLabel#coverNoticeInfo {{ color: {c.muted}; background: {c.card}; border-bottom: 1px solid {c.divider}; padding: 4px 12px; }}
        QLabel#coverNoticeWarning {{ color: {c.text}; background: {c.card}; border-bottom: 1px solid {c.warning_border}; padding: 4px 12px; }}
        QLabel#coverNoticeError {{ color: {c.danger}; background: {c.card}; border-bottom: 1px solid {c.danger}; padding: 4px 12px; }}
        QPushButton#aiQueueItem {{ text-align: left; padding: 0 8px; min-height: 30px; max-height: 30px; color: {c.muted}; background: transparent; border: 1px solid transparent; border-radius: 6px; }}
        QPushButton#aiQueueItem:hover {{ color: {c.text}; }}
        QPushButton#aiQueueItem:checked {{ color: {c.text}; border-color: {c.accent_line}; }}
        QPushButton#navButton {{ border-radius: 10px; color: {c.subtle}; padding: 0; min-height: 46px; max-height: 46px; min-width: 46px; max-width: 46px; }}
        QPushButton#navButton:hover {{ color: {c.text}; }}
        QPushButton#navButton:checked {{ color: {c.accent_ink}; }}
        QPushButton#projectItem, QPushButton#projectItemCompact {{ text-align: left; border-radius: 8px; padding: 0; }}
        QPushButton#projectItem {{ min-height: {m.row_height}px; max-height: {m.row_height}px; }}
        QPushButton#projectItemCompact {{ min-height: {m.project_compact_height}px; max-height: {m.project_compact_height}px; }}
        QCheckBox {{ spacing: 8px; }}
        QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {c.control_border}; border-radius: 4px; background: {c.well}; }}
        QCheckBox::indicator:hover {{ border-color: {c.control_hover_border}; }}
        QCheckBox::indicator:checked {{ background: {checkbox_on_bg}; border-color: {checkbox_on_border}; image: url("{check}"); }}
        QCheckBox::indicator:disabled {{ border-color: {c.divider}; background: transparent; }}
        QRadioButton {{ spacing: 8px; }}
        QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid {c.control_border}; border-radius: 7px; background: {c.well}; }}
        QRadioButton::indicator:checked {{ border: 4px solid {c.accent if s.dark else c.accent_line}; background: {c.well}; }}
        QProgressBar {{ background: {c.divider}; border: none; border-radius: 2px; min-height: 4px; max-height: 4px; text-align: center; color: transparent; }}
        QProgressBar::chunk {{ background: {c.accent_fill}; border-radius: 2px; }}
        QWidget#asrBanner {{ background: {c.well}; border: 1px solid {c.card_border}; border-radius: 10px; }}
        QPlainTextEdit {{ background: {c.well}; border: 1px solid {c.control_border}; border-radius: {radius}px; padding: 5px 8px; selection-background-color: {selection}; selection-color: {selection_text}; }}
        QSpinBox, QDoubleSpinBox {{ background: {c.well}; border: 1px solid {c.control_border}; border-radius: {radius}px; padding: 0 22px 0 8px; selection-background-color: {selection}; selection-color: {selection_text}; font-family: {mono_font}; }}
        QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {c.control_hover_border}; }}
        QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 18px; border: none; border-top-right-radius: {radius}px; background: transparent; }}
        QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 18px; border: none; border-bottom-right-radius: {radius}px; background: transparent; }}
        QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {c.hover}; }}
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{arrow_up}"); width: 9px; height: 9px; }}
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{arrow}"); width: 9px; height: 9px; }}
        QLineEdit, QTextEdit, QComboBox {{ background: {c.well}; border: 1px solid {c.control_border}; border-radius: {radius}px; padding: 7px 10px; selection-background-color: {selection}; selection-color: {selection_text}; }}
        QComboBox:hover {{ border-color: {c.control_hover_border}; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox::down-arrow {{ image: url("{arrow}"); width: 12px; height: 12px; }}
        QComboBox QAbstractItemView {{ background: {c.overlay}; border: 1px solid {c.overlay_border}; padding: 4px; outline: 0; selection-background-color: {c.button_hover}; selection-color: {c.text}; }}
        QSlider::groove:horizontal {{ height: 4px; background: {c.control_border}; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {c.accent if s.dark else c.accent_line}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -5px 0; background: {c.text if s.dark else c.card}; border: none; border-radius: 7px; }}
        QTableView {{ background: {c.card}; alternate-background-color: {c.card}; gridline-color: transparent; selection-background-color: {c.row_selected}; selection-color: {c.text}; border: none; font-size: {m.text_body}px; }}
        QTableView::item {{ background: transparent; }}
        QTableView::item:hover {{ background: transparent; }}
        QTableView::item:selected {{ background: transparent; color: {c.text}; }}
        QTableView::item:selected:hover {{ background: transparent; color: {c.text}; }}
        QHeaderView::section {{ background: {c.card}; color: {c.subtle}; border: none; border-bottom: 1px solid {c.divider}; padding: 4px 3px; font-size: {m.text_small}px; font-weight: 500; }}
        QAbstractSpinBox QLineEdit {{ background: transparent; border: none; padding: 0; border-radius: 0; }}
        QComboBox#quietCombo {{ background: transparent; border: 1px solid transparent; color: {c.muted}; font-size: 12px; padding: 4px 8px; }}
        QComboBox#quietCombo:hover {{ background: {c.hover}; border-color: transparent; color: {c.text}; }}
        QPushButton#menuRow, QPushButton#projectOption {{ background: transparent; border: none; text-align: left; }}
        QPushButton#projectOption {{ padding: 0; min-height: 48px; max-height: 48px; }}
        QPushButton#menuRow {{ color: {c.muted}; font-size: 12px; padding: 0 10px; border-radius: 8px; }}
        QPushButton#menuRow:hover {{ background: {c.hover}; color: {c.text}; }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {c.focus_ring}; }}
        QLineEdit:disabled, QTextEdit:disabled {{ color: {c.disabled}; background: transparent; }}
        QMenu {{ background: {c.overlay}; border: 1px solid {c.overlay_border}; padding: 6px; }}
        QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 6px; background: transparent; color: {c.text_secondary}; }}
        QMenu::item:selected {{ background: {c.button_hover}; color: {c.text}; }}
        QMenu::item:disabled {{ color: {c.disabled}; }}
        QMenu::separator {{ height: 1px; background: {c.divider}; margin: 4px 10px; }}
        QToolTip {{ background: {c.overlay}; color: {c.text}; border: 1px solid {c.overlay_border}; padding: 5px 8px; }}
        QScrollArea {{ border: none; background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
        QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {c.scrollbar}; border-radius: 3px; min-height: 28px; }}
        QScrollBar::handle:horizontal {{ background: {c.scrollbar}; border-radius: 3px; min-width: 28px; }}
        QScrollBar::handle:hover {{ background: {c.scrollbar_hover}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QScrollBar#timelineScroll:horizontal {{ background: transparent; height: 10px; margin: 1px 0; border-radius: 5px; }}
        QScrollBar#timelineScroll::handle:horizontal {{ background: {c.scrollbar}; min-width: 36px; border-radius: 4px; }}
        QScrollBar#timelineScroll::handle:horizontal:hover {{ background: {c.scrollbar_hover}; }}
        QScrollBar#timelineScroll::handle:horizontal:pressed {{ background: {c.accent_line}; }}
        QScrollBar#timelineScroll::add-line:horizontal, QScrollBar#timelineScroll::sub-line:horizontal {{ width: 0; }}
        QScrollBar#timelineScroll::add-page:horizontal, QScrollBar#timelineScroll::sub-page:horizontal {{ background: transparent; }}
        QSplitter::handle {{ background: transparent; }}
        QSplitter::handle:horizontal {{ width: 4px; }}
        QSplitter::handle:vertical {{ height: 4px; }}
    """
