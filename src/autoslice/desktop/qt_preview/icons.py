"""桌面工作台共用图标：20px 网格、1.5 线宽、圆角端点，按屏幕缩放比渲染。"""

import math
import tempfile
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .theme import COLORS, SIZES


def _gear() -> str:
    """八齿齿轮：齿顶收窄、齿根放宽，圆角连接后不显生硬。"""

    points = []
    for tooth in range(8):
        base = math.radians(tooth * 45)
        for radius, offset in ((5.6, -14), (7.6, -8), (7.6, 8), (5.6, 14)):
            angle = base + math.radians(offset)
            points.append(f"{10 + radius * math.cos(angle):.2f} {10 + radius * math.sin(angle):.2f}")
    return f'<path d="M{" L".join(points)}Z"/><circle cx="10" cy="10" r="2.25"/>'


# {c} 为填充色占位；描边色由外层 svg 统一给出
_PATHS = {
    "subtitles": ('<rect x="2.75" y="4.25" width="14.5" height="11.5" rx="2.5"/>'
                  '<path d="M6 9.25h4M12.25 9.25H14M6 12.25h1.75M10 12.25h4"/>'),
    "cover": ('<rect x="2.75" y="3.75" width="14.5" height="12.5" rx="2.5"/>'
              '<circle cx="7.25" cy="8" r="1.25"/>'
              '<path d="m3 14.5 3.75-3.25 2.75 2.25 3-3.25 4.5 4.75"/>'),
    "settings": _gear(),
    "play": '<path d="M6.75 4.75v10.5L15.5 10z" fill="{c}"/>',
    "pause": ('<rect x="5.25" y="4.75" width="2.5" height="10.5" rx="0.75" fill="{c}" stroke="none"/>'
              '<rect x="12.25" y="4.75" width="2.5" height="10.5" rx="0.75" fill="{c}" stroke="none"/>'),
    "volume": ('<path d="M3.75 7.75h2.5L10 4.75v10.5l-3.75-3h-2.5z"/>'
               '<path d="M13 7.5a3.5 3.5 0 0 1 0 5M15.25 5.25a6.75 6.75 0 0 1 0 9.5"/>'),
    "save": ('<path d="M5 3.25h8.2l3.55 3.55V15a1.75 1.75 0 0 1-1.75 1.75H5A1.75 1.75 0 0 1 3.25 15V5A1.75 1.75 0 0 1 5 3.25z"/>'
             '<path d="M6.75 3.5v3.25h5V3.5M6.25 16.5v-4.25h7.5v4.25"/>'),
    "render": ('<path d="M11 3.75h5.25V9M16.25 3.75 9.5 10.5"/>'
               '<path d="M14.25 11.75v2.75a1.75 1.75 0 0 1-1.75 1.75h-7a1.75 1.75 0 0 1-1.75-1.75v-7'
               'A1.75 1.75 0 0 1 5.5 5.75h2.75"/>'),
    "folder": ('<path d="M2.75 6A1.75 1.75 0 0 1 4.5 4.25h3.3l1.7 1.75h6A1.75 1.75 0 0 1 17.25 7.75v6.75'
               'a1.75 1.75 0 0 1-1.75 1.75h-11A1.75 1.75 0 0 1 2.75 14.5z"/>'),
    "chevron_left": '<path d="M12 5 7 10l5 5"/>',
    "chevron_right": '<path d="m8 5 5 5-5 5"/>',
    "chevron_down": '<path d="m5 7.5 5 5 5-5"/>',
    "more": ('<circle cx="4.75" cy="10" r="1.25" fill="{c}" stroke="none"/>'
             '<circle cx="10" cy="10" r="1.25" fill="{c}" stroke="none"/>'
             '<circle cx="15.25" cy="10" r="1.25" fill="{c}" stroke="none"/>'),
    "refresh": ('<path d="M16.25 10a6.25 6.25 0 1 1-1.83-4.42L16.25 7.4"/>'
                '<path d="M16.25 3.75V7.4h-3.65"/>'),
    "magnet": ('<path d="M4.25 3.75h3.5V10a2.25 2.25 0 0 0 4.5 0V3.75h3.5V10a5.75 5.75 0 0 1-11.5 0z"/>'
               '<path d="M4.25 7h3.5M12.25 7h3.5"/>'),
    "waveform": '<path d="M3.75 8.5v3M6.9 6v8M10 3.75v12.5M13.1 6.5v7M16.25 8.75v2.5"/>',
    "fit": '<path d="M2.75 5v10M17.25 5v10M5.75 10h8.5M7.75 8 5.75 10l2 2M12.25 8l2 2-2 2"/>',
    "sparkle": ('<path d="M9 3.25c.4 3.1 1.65 4.35 4.75 4.75-3.1.4-4.35 1.65-4.75 4.75'
                '-.4-3.1-1.65-4.35-4.75-4.75 3.1-.4 4.35-1.65 4.75-4.75z"/>'
                '<path d="M15 12.25v4M13 14.25h4"/>'),
    "eye": ('<path d="M2.25 10S4.9 4.75 10 4.75 17.75 10 17.75 10 15.1 15.25 10 15.25 2.25 10 2.25 10z"/>'
            '<circle cx="10" cy="10" r="2.25"/>'),
}


def svg_markup(name: str, color: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20" '
            f'fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" '
            f'stroke-linejoin="round">{_PATHS[name].replace("{c}", color)}</svg>')


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


@lru_cache(maxsize=16)
def icon_file(name: str, color: str) -> str:
    """样式表 url() 需要文件路径；写入临时目录并返回正斜杠路径。"""

    folder = Path(tempfile.gettempdir()) / "autoslice-icons"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{color.lstrip('#')}.svg"
    path.write_text(svg_markup(name, color), encoding="utf-8")
    return path.as_posix()
