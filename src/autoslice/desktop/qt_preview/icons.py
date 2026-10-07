"""桌面工作台共用的轻量线性图标。"""

from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .theme import COLORS, SIZES

_PATHS = {
    "subtitles": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M6 10h5m-5 4h12m-4-4h4"/>',
    "cover": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="1"/><path d="m4 17 5-5 4 3 3-4 4 5"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "play": '<path d="m8 5 11 7-11 7z"/>',
    "pause": '<path d="M8 5v14m8-14v14"/>',
    "volume": '<path d="M4 9v6h4l5 4V5L8 9H4Zm12-1a6 6 0 0 1 0 8m2-11a10 10 0 0 1 0 14"/>',
    "save": '<path d="M5 3h11l3 3v15H5z"/><path d="M8 3v6h8V3M8 21v-7h8v7"/>',
    "render": '<path d="m4 4 16 8-16 8z"/><path d="M4 4v16"/>',
    "folder": '<path d="M3 6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "undo": '<path d="M9 7 4 12l5 5"/><path d="M5 12h8a6 6 0 0 1 6 6"/>',
    "redo": '<path d="m15 7 5 5-5 5"/><path d="M19 12h-8a6 6 0 0 0-6 6"/>',
    "chevron_left": '<path d="m15 6-6 6 6 6"/>',
    "chevron_right": '<path d="m9 6 6 6-6 6"/>',
    "chevron_down": '<path d="m6 9 6 6 6-6"/>',
    "text": '<path d="M5 7V5h14v2M12 5v14M9 19h6"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-10 9"/>',
    "shapes": '<circle cx="8" cy="8" r="4.5"/><rect x="12" y="12" width="8.5" height="8.5" rx="1"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8m0-4v4h4M4 13a8 8 0 0 0 14.6 4.5L20 16m0 4v-4h-4"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
    "download": '<path d="M12 4v11m-5-5 5 5 5-5M5 20h14"/>',
    "sparkles": '<path d="m12 3 1.1 3.1L16 7.2l-2.9 1.1L12 11l-1.1-2.7L8 7.2l2.9-1.1L12 3Z"/><path d="m18 13 .8 2.2L21 16l-2.2.8L18 19l-.8-2.2L15 16l2.2-.8L18 13Z"/><path d="m6 13 .7 1.8L8.5 15.5l-1.8.7L6 18l-.7-1.8-1.8-.7 1.8-.7L6 13Z"/>',
}


@lru_cache(maxsize=32)
def icon(name: str, color: str = COLORS.muted, size: int = SIZES.icon_size) -> QIcon:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
           f'viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" '
           f'stroke-linecap="round" stroke-linejoin="round">{_PATHS[name]}</svg>')
    image = QPixmap(size, size)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(painter)
    painter.end()
    return QIcon(image)
