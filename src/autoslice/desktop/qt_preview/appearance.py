"""外观：主题与强调色的读写和应用；换主题后即时刷新样式表、调色板、图标和自绘控件。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from . import icons
from .theme import (
    COLORS,
    DEFAULT_THEME,
    STYLE,
    THEMES,
    apply_theme,
    current_theme,
    normalize,
    stylesheet,
)


@dataclass(frozen=True)
class Appearance:
    theme: str = DEFAULT_THEME
    accent: str = ""
    ground: str = ""

    @classmethod
    def of(cls, theme: str | None, accent: str | None = None, ground: str | None = None) -> Appearance:
        return cls(*normalize(theme, accent, ground))

    @classmethod
    def from_payload(cls, payload: object) -> Appearance:
        payload = payload if isinstance(payload, dict) else {}
        return cls.of(payload.get("theme"), payload.get("accent"), payload.get("ground"))

    def payload(self) -> dict[str, str]:
        return {"theme": self.theme, "accent": self.accent, "ground": self.ground}

    @property
    def label(self) -> str:
        return THEMES[self.theme].label


def load(storage) -> Appearance:
    """读不到或被改坏时回到夜航默认。"""

    return Appearance.from_payload((storage.read_preferences() or {}).get("appearance"))


def save(storage, appearance: Appearance) -> None:
    preferences = dict(storage.read_preferences() or {})
    preferences["appearance"] = appearance.payload()
    storage.save_preferences(preferences)


def current() -> Appearance:
    return Appearance(*current_theme())


class _Hub(QObject):
    changed = Signal()


_hub: _Hub | None = None


def hub() -> _Hub:
    global _hub
    if _hub is None:
        _hub = _Hub()
    return _hub


class _Hook(QObject):
    """挂在控件下的换主题回调；控件销毁时随之销毁，连接自动断开。"""

    def __init__(self, owner: QObject, apply) -> None:
        super().__init__(owner)
        self._apply = apply
        hub().changed.connect(self.run)

    @Slot()
    def run(self) -> None:
        self._apply()


def themed(owner: QObject, apply) -> None:
    """立即执行 apply，之后每次换主题再执行；用于局部样式、位图图标等创建时写死颜色的地方。"""

    apply()
    _Hook(owner, apply)


def palette() -> QPalette:
    """Qt 自带对话框、文件框和少数原生部件用的调色板，与当前主题一致。"""

    c = COLORS
    result = QPalette()
    role = QPalette.ColorRole
    for name, value in (
        (role.Window, c.ground), (role.WindowText, c.text), (role.Base, c.well), (role.AlternateBase, c.card),
        (role.Text, c.text), (role.Button, c.card), (role.ButtonText, c.text), (role.BrightText, c.text),
        (role.Highlight, c.accent_line if STYLE.dark else c.accent_soft),
        (role.HighlightedText, c.text), (role.ToolTipBase, c.overlay), (role.ToolTipText, c.text),
        (role.PlaceholderText, c.placeholder), (role.Link, c.accent_ink), (role.LinkVisited, c.accent_ink),
        (role.Light, c.card_highlight), (role.Midlight, c.raised), (role.Mid, c.control_border),
        (role.Dark, c.divider), (role.Shadow, c.shadow),
    ):
        result.setColor(name, QColor(value))
    for name in (role.WindowText, role.Text, role.ButtonText):
        result.setColor(QPalette.ColorGroup.Disabled, name, QColor(c.disabled))
    return result


def apply(app: QApplication | None = None, appearance: Appearance | None = None) -> Appearance:
    """切到指定外观（不给就重刷当前外观）；有 QApplication 时同步刷新整个界面。"""

    if appearance is not None:
        apply_theme(appearance.theme, appearance.accent, appearance.ground)
    result = current()
    app = app or QApplication.instance()
    if app is None:
        return result
    # 整个界面重新套样式表约 1 秒（随控件数增长）：期间显示等待光标
    busy = isinstance(app, QApplication) and bool(app.topLevelWidgets())
    if busy:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        app.setPalette(palette())
        app.setStyleSheet(stylesheet())
        widgets = app.allWidgets()
        icons.retheme(widgets)
        hub().changed.emit()
        for widget in widgets:
            widget.update()
    finally:
        if busy:
            QApplication.restoreOverrideCursor()
    return result
