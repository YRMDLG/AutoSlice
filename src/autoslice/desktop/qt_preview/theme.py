"""Qt 视觉基线。后续桌面页面复用这里的尺寸、颜色和控件状态。"""

from dataclasses import dataclass

from .glyphs import icon_file


@dataclass(frozen=True)
class Palette:
    """碳黑·钴蓝：暖中性碳黑衬冷钴蓝；层级靠明度与顶边高光，强调色只落在焦点上。"""

    # 表面层级：由深到浅，暖中性极低彩度，不给视频染色
    canvas: str = "#0A0A09"
    nav: str = "#0D0D0C"
    rail: str = "#11110F"
    panel: str = "#141412"
    raised: str = "#1C1C1A"
    raised_top: str = "#232320"
    highlight: str = "#302F2C"
    overlay: str = "#1E1E1C"
    well: str = "#0C0C0B"
    player: str = "#050505"
    # 强调色：实心用 accent，暗底上的文字/图标用 accent_text
    accent: str = "#3370E6"
    accent_top: str = "#4580EE"
    accent_edge: str = "#6A9AF2"
    accent_medium: str = "#2B5DBE"
    accent_subtle: str = "#161B24"
    accent_hover: str = "#4A82EC"
    accent_pressed: str = "#2A62D2"
    accent_tint: str = "#192335"
    accent_text: str = "#8FB1F7"
    on_accent: str = "#FFFFFF"
    # 文字：暖白与暖灰
    text: str = "#F0EEE9"
    muted: str = "#A6A39C"
    subtle: str = "#7C7973"
    divider: str = "#22221F"
    border: str = "#2A2A27"
    disabled: str = "#4F4D49"
    # 交互状态
    hover: str = "#1A1A18"
    button_hover: str = "#252522"
    button_pressed: str = "#161614"
    button_hover_border: str = "#383733"
    project_hover: str = "#191917"
    project_selected: str = "#1D1D1B"
    row_selected: str = "#171D28"
    # 播放态用暖中性，与冷蓝选中态区分
    playback: str = "#1E1E1B"
    playback_border: str = "#4D4C47"
    focus_ring: str = "#5B8FF0"
    scrollbar: str = "#2A2A27"
    scrollbar_hover: str = "#3F3E3A"
    # AI 差异：柔珊瑚删除、薄荷新增
    ai_removed: str = "#F08A80"
    ai_added: str = "#5CD6A0"


@dataclass(frozen=True)
class Metrics:
    nav_width: int = 72
    appbar_height: int = 56
    project_width: int = 252
    project_min_width: int = 188
    ai_width: int = 324
    ai_min_width: int = 280
    ai_collapsed_width: int = 48
    timeline_height: int = 92
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
    subtitle_row_height: int = 48
    subtitle_number_width: int = 42
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


COLORS = Palette()
SIZES = Metrics()
# 随程序分发的源流黑体（思源黑体圆角版）为主，依次回退思源黑体、雅黑；时间码等宽
UI_FAMILIES = ("Resource Han Rounded CN", "Noto Sans SC", "Microsoft YaHei UI", "Segoe UI")
MONO_FAMILIES = ("Cascadia Mono", "Consolas")


def stylesheet() -> str:
    """共享控件样式；局部绘制只处理时间轴和图标等图形。"""

    c = COLORS
    m = SIZES
    ui_font = ", ".join(f"'{name}'" for name in UI_FAMILIES)
    mono_font = ", ".join(f"'{name}'" for name in MONO_FAMILIES)
    arrow = icon_file("chevron_down", c.muted)
    # 凸起控件：自上而下微渐变 + 顶边高光，模拟顶光
    lift = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {c.raised_top}, stop:1 {c.raised})"
    lift_hover = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2B2B28, stop:1 {c.button_hover})"
    solid = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {c.accent_top}, stop:1 {c.accent})"
    solid_hover = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #5A8FF1, stop:1 {c.accent_hover})"
    return f"""
        QWidget {{ color: {c.text}; background: transparent; font-family: {ui_font}; font-size: {m.text_base}px; }}
        QWidget#root {{ background: {c.canvas}; }}
        QWidget#appbar {{ background: {c.nav}; border-bottom: 1px solid {c.divider}; }}
        QWidget#navRail {{ background: {c.nav}; border-right: 1px solid {c.divider}; }}
        QWidget#projectRail, QWidget#aiPanel {{ background: {c.rail}; }}
        QWidget#workArea {{ background: {c.canvas}; }}
        QWidget#videoSurface {{ background: {c.player}; }}
        QWidget#timeline, QWidget#subtitleList {{ background: {c.panel}; }}
        QWidget#workToolbar {{ background: {c.rail}; }}
        QWidget#workflowActions {{ background: transparent; border: none; }}
        QWidget#playerControls {{ background: transparent; }}
        QWidget#coverSurface {{ background: {c.player}; border: 1px solid {c.divider}; border-radius: {m.radius}px; }}
        QWidget#settingsSurface {{ background: {c.canvas}; }}
        QFrame#hairline {{ background: {c.divider}; max-height: 1px; min-height: 1px; }}
        QLabel#brand {{ font-size: 15px; font-weight: 500; }}
        QLabel#pageTitle {{ font-size: {m.text_heading}px; font-weight: 500; }}
        QLabel#projectTitle {{ font-size: 14px; font-weight: 500; }}
        QLabel#videoFile {{ color: {c.muted}; font-size: {m.text_small}px; }}
        QLabel#statusLabel {{ color: {c.muted}; font-size: {m.text_small}px; padding: 0 4px; }}
        QLabel#timecode {{ color: {c.muted}; font-family: {mono_font}; font-size: 12px; }}
        QLabel#aiDiff {{ font-size: {m.text_body}px; }}
        QLabel#sectionTitle {{ font-size: 13px; font-weight: 500; }}
        QLabel#bodyTitle {{ font-size: 20px; font-weight: 500; }}
        QLabel#muted, QLabel#hint {{ color: {c.muted}; }}
        QLabel#subtle {{ color: {c.subtle}; }}
        QLabel#badge {{ color: {c.accent_text}; background: {c.accent_subtle}; border: 1px solid {c.accent_tint}; border-radius: 4px; padding: 2px 6px; font-size: 11px; }}
        QLabel#quietBadge {{ color: {c.muted}; background: {c.raised}; border: 1px solid {c.border}; border-radius: 4px; padding: 2px 6px; font-size: 11px; }}
        QPushButton {{ background: {lift}; border: 1px solid {c.border}; border-top-color: {c.highlight}; border-radius: {m.control_radius}px; padding: 0 12px; min-height: {m.button_height}px; color: {c.text}; }}
        QPushButton:hover {{ background: {lift_hover}; border-color: {c.button_hover_border}; border-top-color: #45443F; }}
        QPushButton:pressed {{ background: {c.button_pressed}; border-color: {c.border}; border-top-color: {c.canvas}; }}
        QPushButton:checked {{ color: {c.accent_text}; background: {c.accent_subtle}; border-color: {c.accent_tint}; border-top-color: #24375A; }}
        QPushButton:checked:hover {{ background: #1A2130; border-color: #24375A; }}
        QPushButton:disabled {{ color: {c.disabled}; background: {c.panel}; border-color: {c.divider}; }}
        QPushButton#primary {{ color: {c.on_accent}; background: {solid}; border-color: {c.accent_pressed}; border-top-color: {c.accent_edge}; font-weight: 500; }}
        QPushButton#primary:hover {{ background: {solid_hover}; border-color: {c.accent}; border-top-color: #86ADF6; }}
        QPushButton#primary:pressed {{ background: {c.accent_pressed}; border-color: {c.accent_medium}; border-top-color: {c.accent_medium}; }}
        QPushButton#primary:disabled, QPushButton#primary:disabled:hover {{ color: {c.disabled}; background: {c.panel}; border-color: {c.divider}; }}
        QPushButton#secondary {{ font-weight: 500; }}
        QPushButton#secondary:disabled, QPushButton#secondary:disabled:hover {{ color: {c.disabled}; background: {c.panel}; border-color: {c.divider}; }}
        QPushButton#quiet, QPushButton#navButton, QPushButton#projectItem, QPushButton#projectItemCompact {{ background: transparent; border: none; }}
        QPushButton#quiet:hover {{ background: {c.hover}; }}
        QPushButton#quiet:pressed {{ background: {c.button_pressed}; }}
        QPushButton#tool {{ background: transparent; border: 1px solid transparent; border-radius: {m.control_radius}px; min-height: 28px; padding: 0 10px; color: {c.muted}; }}
        QPushButton#tool:hover {{ background: {c.hover}; color: {c.text}; }}
        QPushButton#tool:pressed {{ background: {c.button_pressed}; }}
        QPushButton#tool:checked {{ color: {c.accent_text}; background: {c.accent_subtle}; border-color: {c.accent_tint}; }}
        QPushButton#tool:checked:hover {{ background: #1A2130; }}
        QPushButton#tool:disabled {{ color: {c.disabled}; background: transparent; border-color: transparent; }}
        QWidget#segmented {{ background: {c.well}; border: 1px solid {c.border}; border-top-color: {c.canvas}; border-radius: 7px; }}
        QPushButton#segment {{ background: transparent; border: 1px solid transparent; border-radius: 5px; min-height: 24px; padding: 0 12px; color: {c.muted}; }}
        QPushButton#segment:hover {{ color: {c.text}; }}
        QPushButton#segment:checked {{ color: {c.text}; background: {lift}; border-color: {c.border}; border-top-color: {c.highlight}; }}
        QPushButton#segment:disabled {{ color: {c.disabled}; }}
        QPushButton#navButton {{ border-radius: 8px; color: {c.muted}; padding: 0; min-height: 60px; max-height: 60px; min-width: 56px; max-width: 56px; }}
        QPushButton#navButton:hover {{ background: {c.hover}; color: {c.text}; }}
        QPushButton#navButton:checked {{ color: {c.text}; background: {c.project_selected}; }}
        QPushButton#projectItem, QPushButton#projectItemCompact {{ text-align: left; border-radius: 6px; padding: 0; }}
        QPushButton#projectItem {{ min-height: {m.row_height}px; max-height: {m.row_height}px; }}
        QPushButton#projectItemCompact {{ min-height: {m.project_compact_height}px; max-height: {m.project_compact_height}px; }}
        QPushButton#projectItem:hover, QPushButton#projectItemCompact:hover {{ background: {c.project_hover}; }}
        QPushButton#projectItem:checked, QPushButton#projectItemCompact:checked {{ background: {c.project_selected}; }}
        QLineEdit, QTextEdit, QComboBox {{ background: {c.well}; border: 1px solid {c.border}; border-top-color: {c.canvas}; border-radius: {m.control_radius}px; padding: 7px 10px; selection-background-color: {c.accent_pressed}; }}
        QComboBox:hover {{ border-color: {c.button_hover_border}; border-top-color: {c.border}; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox::down-arrow {{ image: url("{arrow}"); width: 12px; height: 12px; }}
        QComboBox QAbstractItemView {{ background: {c.overlay}; border: 1px solid {c.border}; padding: 4px; outline: 0; selection-background-color: {c.accent}; selection-color: {c.on_accent}; }}
        QSlider::groove:horizontal {{ height: 3px; background: {c.border}; border-radius: 1px; }}
        QSlider::sub-page:horizontal {{ background: {c.accent}; border-radius: 1px; }}
        QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -5px 0; background: {c.text}; border: none; border-radius: 6px; }}
        QSlider::handle:horizontal:hover {{ background: #FFFFFF; }}
        QTableView {{ background: {c.panel}; alternate-background-color: {c.panel}; gridline-color: transparent; selection-background-color: {c.row_selected}; selection-color: {c.text}; border: none; font-size: {m.text_body}px; }}
        QTableView::item {{ background: transparent; }}
        QTableView::item:hover {{ background: transparent; }}
        QTableView::item:selected {{ background: transparent; color: {c.text}; }}
        QTableView::item:selected:hover {{ background: transparent; color: {c.text}; }}
        QHeaderView::section {{ background: {c.panel}; color: {c.subtle}; border: none; border-bottom: 1px solid {c.divider}; padding: 4px 3px; font-size: {m.text_small}px; font-weight: 500; }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QPushButton:focus {{ border-color: {c.focus_ring}; }}
        QLineEdit:disabled, QTextEdit:disabled {{ color: {c.disabled}; background: {c.panel}; }}
        QMenu {{ background: {c.overlay}; border: 1px solid {c.border}; border-top-color: {c.highlight}; padding: 4px; }}
        QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; background: transparent; }}
        QMenu::item:selected {{ background: {c.accent}; color: {c.on_accent}; }}
        QMenu::item:disabled {{ color: {c.disabled}; }}
        QMenu::separator {{ height: 1px; background: {c.divider}; margin: 4px 6px; }}
        QToolTip {{ background: {c.overlay}; color: {c.text}; border: 1px solid {c.border}; padding: 5px 8px; }}
        QScrollArea {{ border: none; background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
        QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {c.scrollbar}; border-radius: 3px; min-height: 28px; }}
        QScrollBar::handle:horizontal {{ background: {c.scrollbar}; border-radius: 3px; min-width: 28px; }}
        QScrollBar::handle:hover {{ background: {c.scrollbar_hover}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        QSplitter::handle {{ background: {c.canvas}; }}
        QSplitter::handle:hover {{ background: {c.hover}; }}
        QSplitter::handle:pressed {{ background: {c.accent_medium}; }}
        QSplitter::handle:horizontal {{ width: 4px; }}
        QSplitter::handle:vertical {{ height: 4px; }}
    """
