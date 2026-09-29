"""Qt 视觉基线。后续桌面页面复用这里的尺寸、颜色和控件状态。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    """石墨·靛蓝：近中性石墨底不染色视频，唯一强调色为靛蓝。"""

    # 背景层级：由深到浅，色相极弱偏冷
    canvas: str = "#0D0E11"
    nav: str = "#101114"
    rail: str = "#141519"
    panel: str = "#17181D"
    raised: str = "#202127"
    player: str = "#08090B"
    # 强调色：实心用 accent，暗底上的文字/图标用 accent_text
    accent: str = "#5E63F2"
    accent_medium: str = "#4A4FD0"
    accent_subtle: str = "#1D1F35"
    accent_hover: str = "#7074F5"
    accent_pressed: str = "#4C51DE"
    accent_tint: str = "#24264A"
    accent_text: str = "#A3A6FF"
    on_accent: str = "#FFFFFF"
    # 文字
    text: str = "#EDEDF0"
    muted: str = "#A1A2AC"
    subtle: str = "#787985"
    divider: str = "#25262D"
    disabled: str = "#565761"
    # 交互状态
    hover: str = "#1E1F25"
    button_hover: str = "#282930"
    button_pressed: str = "#1A1B20"
    button_hover_border: str = "#34353E"
    project_hover: str = "#1B1C22"
    project_selected: str = "#1E2036"
    row_selected: str = "#20223A"
    # 播放态用中性灰，与靛蓝选中态区分
    playback: str = "#24252C"
    playback_border: str = "#5C5E6B"
    focus_ring: str = "#8387FF"
    scrollbar: str = "#2E2F37"
    scrollbar_hover: str = "#45464F"
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
    button_height: int = 36
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
    subtitle_time_width: int = 184
    playhead_hit_width: int = 14
    icon_size: int = 20
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


def stylesheet() -> str:
    """共享控件样式；局部绘制只处理时间轴和图标等图形。"""

    c = COLORS
    m = SIZES
    return f"""
        QWidget {{ color: {c.text}; background: transparent; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: {m.text_base}px; }}
        QWidget#root {{ background: {c.canvas}; }}
        QWidget#appbar {{ background: {c.nav}; border-bottom: 1px solid {c.divider}; }}
        QWidget#navRail {{ background: {c.nav}; border-right: 1px solid {c.divider}; }}
        QWidget#projectRail, QWidget#aiPanel {{ background: {c.rail}; border-right: 1px solid {c.divider}; }}
        QWidget#workArea {{ background: {c.canvas}; }}
        QWidget#videoSurface {{ background: {c.player}; }}
        QWidget#timeline, QWidget#subtitleList {{ background: {c.panel}; }}
        QWidget#workToolbar {{ background: {c.rail}; }}
        QWidget#workflowActions {{ background: {c.panel}; border: 1px solid {c.divider}; border-radius: 8px; padding: 2px; }}
        QWidget#playerControls {{ background: {c.rail}; }}
        QWidget#coverSurface {{ background: {c.player}; border: 1px solid {c.divider}; border-radius: {m.radius}px; }}
        QWidget#settingsSurface {{ background: {c.canvas}; }}
        QFrame#hairline {{ background: {c.divider}; max-height: 1px; min-height: 1px; }}
        QLabel#brand {{ font-size: 16px; font-weight: 700; }}
        QLabel#pageTitle {{ font-size: {m.text_heading}px; font-weight: 600; }}
        QLabel#projectTitle {{ font-size: 14px; font-weight: 600; }}
        QLabel#videoFile {{ color: {c.muted}; font-size: {m.text_small}px; }}
        QLabel#statusLabel {{ color: {c.muted}; font-size: {m.text_small}px; padding: 0 4px; }}
        QLabel#timecode {{ color: {c.muted}; font-size: 12px; }}
        QLabel#aiDiff {{ font-size: {m.text_body}px; }}
        QLabel#sectionTitle {{ font-size: 13px; font-weight: 600; }}
        QLabel#bodyTitle {{ font-size: 20px; font-weight: 600; }}
        QLabel#muted, QLabel#hint {{ color: {c.muted}; }}
        QLabel#subtle {{ color: {c.subtle}; }}
        QLabel#badge {{ color: {c.accent_text}; background: {c.accent_tint}; border-radius: 5px; padding: 3px 7px; font-size: 11px; }}
        QLabel#quietBadge {{ color: {c.muted}; background: {c.raised}; border-radius: 5px; padding: 3px 7px; font-size: 11px; }}
        QPushButton {{ background: {c.raised}; border: 1px solid {c.divider}; border-radius: {m.control_radius}px; padding: 0 12px; min-height: {m.button_height}px; color: {c.text}; }}
        QPushButton:hover {{ background: {c.button_hover}; border-color: {c.button_hover_border}; }}
        QPushButton:pressed {{ background: {c.button_pressed}; }}
        QPushButton:disabled {{ color: {c.disabled}; background: {c.panel}; border-color: {c.divider}; }}
        QPushButton#primary {{ color: {c.on_accent}; background: {c.accent}; border-color: {c.accent}; font-weight: 600; }}
        QPushButton#primary:hover {{ background: {c.accent_hover}; border-color: {c.accent_hover}; }}
        QPushButton#primary:pressed {{ background: {c.accent_pressed}; border-color: {c.accent_pressed}; }}
        QPushButton#primary:disabled {{ color: {c.disabled}; background: {c.panel}; border-color: {c.divider}; }}
        QPushButton#primary:disabled:hover {{ color: {c.disabled}; background: {c.panel}; border-color: {c.divider}; }}
        QPushButton#secondary {{ background: {c.raised}; border-color: {c.button_hover_border}; font-weight: 600; }}
        QPushButton#secondary:hover {{ background: {c.button_hover}; border-color: {c.accent_medium}; }}
        QPushButton#secondary:pressed {{ background: {c.button_pressed}; border-color: {c.accent_pressed}; }}
        QPushButton#secondary:disabled {{ color: {c.disabled}; background: {c.panel}; border-color: {c.divider}; }}
        QPushButton#secondary:disabled:hover {{ color: {c.disabled}; background: {c.panel}; border-color: {c.divider}; }}
        QPushButton#quiet, QPushButton#navButton, QPushButton#projectItem, QPushButton#projectItemCompact {{ background: transparent; border: none; }}
        QPushButton#quiet:hover {{ background: {c.raised}; }}
        QPushButton#quiet:pressed {{ background: {c.accent_tint}; }}
        QPushButton#navButton {{ border-radius: 8px; color: {c.muted}; padding: 0; min-height: 60px; max-height: 60px; min-width: 56px; max-width: 56px; }}
        QPushButton#navButton:hover {{ background: {c.raised}; color: {c.text}; }}
        QPushButton#navButton:checked {{ color: {c.accent_text}; background: {c.project_selected}; border-left: 2px solid {c.accent}; }}
        QPushButton#projectItem, QPushButton#projectItemCompact {{ text-align: left; border-radius: 5px; padding: 0; }}
        QPushButton#projectItem {{ min-height: {m.row_height}px; max-height: {m.row_height}px; }}
        QPushButton#projectItemCompact {{ min-height: {m.project_compact_height}px; max-height: {m.project_compact_height}px; }}
        QPushButton#projectItem:hover, QPushButton#projectItemCompact:hover {{ background: {c.project_hover}; }}
        QPushButton#projectItem:checked, QPushButton#projectItemCompact:checked {{ background: {c.project_selected}; border-left: 2px solid {c.accent}; }}
        QLineEdit, QTextEdit, QComboBox {{ background: {c.player}; border: 1px solid {c.divider}; border-radius: 6px; padding: 8px 10px; selection-background-color: {c.accent_pressed}; }}
        QComboBox:hover {{ border-color: {c.button_hover_border}; background: {c.raised}; }}
        QComboBox::drop-down {{ border: none; width: 20px; }}
        QSlider::groove:horizontal {{ height: 4px; background: {c.divider}; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {c.accent}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -4px 0; background: {c.text}; border: none; border-radius: 6px; }}
        QSlider::handle:horizontal:hover {{ background: {c.accent_text}; }}
        QTableView {{ background: {c.panel}; alternate-background-color: {c.panel}; gridline-color: transparent; selection-background-color: {c.row_selected}; selection-color: {c.text}; border: none; font-size: {m.text_body}px; }}
        QTableView::item:hover {{ background: {c.hover}; }}
        QTableView::item:selected {{ background: {c.row_selected}; color: {c.text}; }}
        QTableView::item:selected:hover {{ background: {c.row_selected}; color: {c.text}; }}
        QHeaderView::section {{ background: {c.panel}; color: {c.subtle}; border: none; border-bottom: 1px solid {c.divider}; padding: 4px 6px; font-size: {m.text_small}px; font-weight: 500; }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QPushButton:focus {{ border-color: {c.focus_ring}; }}
        QLineEdit:disabled, QTextEdit:disabled {{ color: {c.disabled}; background: {c.panel}; }}
        QScrollArea {{ border: none; background: transparent; }}
        QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {c.scrollbar}; border-radius: 4px; min-height: 28px; }}
        QScrollBar::handle:vertical:hover {{ background: {c.scrollbar_hover}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QSplitter::handle {{ background: {c.divider}; }}
        QSplitter::handle:hover {{ background: {c.button_hover_border}; }}
        QSplitter::handle:pressed {{ background: {c.accent_medium}; }}
        QSplitter::handle:horizontal {{ width: 4px; }}
        QSplitter::handle:vertical {{ height: 4px; }}
    """
