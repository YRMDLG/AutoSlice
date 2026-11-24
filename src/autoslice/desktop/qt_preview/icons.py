"""桌面工作台共用图标：20px 网格、1.5 线宽、圆角端点，按屏幕缩放比渲染。

颜色传入的是主题令牌（ColorToken）时，记下令牌名；换主题后 retheme() 按令牌重新着色。
"""

from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QWidget

from .glyphs import svg_markup
from .theme import COLORS, SIZES

# QIcon.cacheKey → (图标名, 颜色令牌, 尺寸, 勾选态颜色令牌)
_THEMED: dict[int, tuple[str, str, int, str | None]] = {}


def _ratio() -> float:
    app = QGuiApplication.instance()
    screen = app.primaryScreen() if app else None
    return screen.devicePixelRatio() if screen else 1.0


def _pixmap(name: str, color: str, size: int, ratio: float) -> QPixmap:
    # 按物理像素渲染，避免低分屏发虚、高分屏被放大
    physical = max(1, round(size * ratio))
    image = QPixmap(physical, physical)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(QByteArray(svg_markup(name, color).encode("utf-8"))).render(painter)
    painter.end()
    image.setDevicePixelRatio(physical / size)
    return image


@lru_cache(maxsize=512)
def _build(name: str, color: str, size: int, on_color: str | None, role: str, on_role: str) -> QIcon:
    # role 进缓存键：同一色值在另一套主题里可能分属不同令牌
    ratio = _ratio()
    result = QIcon()
    result.addPixmap(_pixmap(name, color, size, ratio), QIcon.Mode.Normal, QIcon.State.Off)
    if on_color:
        result.addPixmap(_pixmap(name, on_color, size, ratio), QIcon.Mode.Normal, QIcon.State.On)
    if role and (not on_color or on_role):
        _THEMED[result.cacheKey()] = (name, role, size, on_role or None)
    return result


def icon(name: str, color: str | None = None, size: int = SIZES.icon_size,
         on_color: str | None = None) -> QIcon:
    """on_color 为可勾选按钮开启态的颜色；不给 color 时用当前主题的次要文字色。"""

    color = COLORS.muted if color is None else color
    return _build(name, str(color), size, str(on_color) if on_color else None,
                  getattr(color, "role", ""), getattr(on_color, "role", "") if on_color else "")


def themed_icon(name: str, role: str, size: int = SIZES.icon_size, on_role: str | None = None) -> QIcon:
    """按令牌名取当前主题下的图标。"""

    return icon(name, getattr(COLORS, role), size, getattr(COLORS, on_role) if on_role else None)


def spec_of(value: QIcon) -> tuple[str, str, int, str | None] | None:
    return _THEMED.get(value.cacheKey()) if value is not None and not value.isNull() else None


def retheme(widgets) -> int:
    """把按钮上用主题令牌着色的图标换成当前主题的颜色；返回换了几个。"""

    changed = 0
    for widget in widgets:
        if isinstance(widget, QAbstractButton):
            spec = spec_of(widget.icon())
            if spec:
                widget.setIcon(themed_icon(*spec))
                changed += 1
        if isinstance(widget, QWidget):
            for action in widget.actions():
                spec = spec_of(action.icon())
                if spec:
                    action.setIcon(themed_icon(*spec))
                    changed += 1
    return changed
