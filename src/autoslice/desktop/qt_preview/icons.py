"""桌面工作台共用图标：20px 网格、1.5 线宽、圆角端点，按屏幕缩放比渲染。"""

from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .glyphs import svg_markup
from .theme import COLORS, SIZES


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


@lru_cache(maxsize=128)
def icon(name: str, color: str = COLORS.muted, size: int = SIZES.icon_size,
         on_color: str | None = None) -> QIcon:
    """on_color 为可勾选按钮开启态的颜色。"""

    ratio = _ratio()
    result = QIcon()
    result.addPixmap(_pixmap(name, color, size, ratio), QIcon.Mode.Normal, QIcon.State.Off)
    if on_color:
        result.addPixmap(_pixmap(name, on_color, size, ratio), QIcon.Mode.Normal, QIcon.State.On)
    return result
