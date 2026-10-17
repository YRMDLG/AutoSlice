"""图标矢量数据与 SVG 拼装；不依赖 Qt，主题与图标渲染共用。"""

import math
import tempfile
from functools import lru_cache
from pathlib import Path


def _gear() -> str:
    """八齿齿轮：齿顶收窄、齿根放宽，圆角连接后不显生硬。"""

    points = []
    for tooth in range(8):
        base = math.radians(tooth * 45)
        for radius, offset in ((5.6, -14), (7.6, -8), (7.6, 8), (5.6, 14)):
            angle = base + math.radians(offset)
            points.append(f"{10 + radius * math.cos(angle):.2f} {10 + radius * math.sin(angle):.2f}")
    return f'<path d="M{" L".join(points)}Z"/><circle cx="10" cy="10" r="2.25"/>'


def _burst() -> str:
    """八角爆炸星：尖角与凹角交替，圆角连接后不扎手。"""

    points = []
    for step in range(16):
        radius = 7.4 if step % 2 == 0 else 4.4
        angle = math.radians(step * 22.5 - 90)
        points.append(f"{10 + radius * math.cos(angle):.2f} {10 + radius * math.sin(angle):.2f}")
    return f'<path d="M{" L".join(points)}Z"/>'


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
    "chevron_up": '<path d="m5 12.5 5-5 5 5"/>',
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
    "undo": '<path d="M7.25 4.75 3.75 8.25l3.5 3.5"/><path d="M4 8.25h7a4 4 0 0 1 0 8H8.5"/>',
    "redo": '<path d="m12.75 4.75 3.5 3.5-3.5 3.5"/><path d="M16 8.25H9a4 4 0 0 0 0 8h2.5"/>',
    "text": '<path d="M4.5 6V4.75h11V6M10 4.75v10.5M7.5 15.25h5"/>',
    "image": ('<rect x="3.75" y="6.25" width="12.5" height="10" rx="2"/>'
              '<path d="M6.5 3.75h8.25a2.5 2.5 0 0 1 2.5 2.5v6.5"/>'
              '<circle cx="8.25" cy="9.75" r="1.1"/><path d="m4.25 15 3.5-2.75 2.5 1.75 2.25-2.25 3.5 3"/>'),
    "shapes": '<circle cx="7.25" cy="7.5" r="3.75"/><rect x="10.25" y="9.75" width="6.5" height="6.5" rx="1.25"/>',
    "history": ('<path d="M3.75 10a6.25 6.25 0 1 0 1.83-4.42L3.75 7.4"/><path d="M3.75 3.75V7.4h3.65"/>'
                '<path d="M10 6.75V10l2.25 1.5"/>'),
    "layers": ('<path d="m10 3.75 6.75 3.5-6.75 3.5-6.75-3.5z"/>'
               '<path d="m3.25 10.25 6.75 3.5 6.75-3.5"/><path d="m3.25 13.25 6.75 3.5 6.75-3.5"/>'),
    "download": '<path d="M10 3.75v8.5M6.5 8.75l3.5 3.5 3.5-3.5M4 16.25h12"/>',
    "lock": '<rect x="4.75" y="9" width="10.5" height="7.5" rx="2"/><path d="M7 9V6.75a3 3 0 0 1 6 0V9"/><circle cx="10" cy="12.75" r="0.9" fill="{c}" stroke="none"/>',
    "burst": _burst(),
    "highlight": '<path d="m11.75 3.75 4.5 4.5-6.25 6.25H5.5V10z"/><path d="M4 17h12"/>',
    "template": '<rect x="3.25" y="3.75" width="13.5" height="12.5" rx="2"/><path d="M3.25 8h13.5M8 8v8.25"/>',
    "copy": '<rect x="7.25" y="7.25" width="9" height="9" rx="2"/><path d="M12.75 7.25V5.5A1.75 1.75 0 0 0 11 3.75H5.5A1.75 1.75 0 0 0 3.75 5.5V11A1.75 1.75 0 0 0 5.5 12.75h1.75"/>',
    "trash": '<path d="M4 6h12M8 6V4.5a1.25 1.25 0 0 1 1.25-1.25h1.5A1.25 1.25 0 0 1 12 4.5V6"/><path d="m5.5 6 .6 9.1a1.75 1.75 0 0 0 1.75 1.65h4.3a1.75 1.75 0 0 0 1.75-1.65L14.5 6M8.5 9v4.5M11.5 9v4.5"/>',
    "arrow_up": '<path d="M10 16.25V4.25M5.25 9 10 4.25 14.75 9"/>',
    "arrow_down": '<path d="M10 3.75v12M5.25 11 10 15.75 14.75 11"/>',
    "eye_off": '<path d="M8.1 5.1A8 8 0 0 1 10 4.75c5.1 0 7.75 5.25 7.75 5.25a13 13 0 0 1-2.3 3M12.1 12.1A2.25 2.25 0 0 1 7.9 7.9"/><path d="M5.1 6.6A13 13 0 0 0 2.25 10S4.9 15.25 10 15.25a8 8 0 0 0 3.4-.75M3.5 3.5l13 13"/>',
    "person": '<circle cx="10" cy="6.75" r="3"/><path d="M4.25 16.5a5.75 5.75 0 0 1 11.5 0"/>',
    "camera": '<path d="M3 7.5a2 2 0 0 1 2-2h1.25l1-1.5h5.5l1 1.5H15a2 2 0 0 1 2 2v6.75a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><circle cx="10" cy="10.75" r="2.75"/>',
    "upload": '<path d="M10 13.25V3.75M6.5 7.25 10 3.75l3.5 3.5M4 16.25h12"/>',
    "expand": '<path d="M3.75 7V5.25a1.5 1.5 0 0 1 1.5-1.5H7M13 3.75h1.75a1.5 1.5 0 0 1 1.5 1.5V7M16.25 13v1.75a1.5 1.5 0 0 1-1.5 1.5H13M7 16.25H5.25a1.5 1.5 0 0 1-1.5-1.5V13"/>',
    "contain": '<rect x="3.75" y="3.75" width="12.5" height="12.5" rx="2"/><rect x="6.75" y="7.25" width="6.5" height="5.5" rx="1"/>',
    "close": '<path d="m5.5 5.5 9 9M14.5 5.5l-9 9"/>',
    "bolt": '<path d="m11.25 3.25-5.75 8h4l-.75 5.5 5.75-8h-4z"/>',
}
# 旧代码里用的是复数写法
_PATHS["sparkles"] = _PATHS["sparkle"]


def svg_markup(name: str, color: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20" '
            f'fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" '
            f'stroke-linejoin="round">{_PATHS[name].replace("{c}", color)}</svg>')


@lru_cache(maxsize=16)
def icon_file(name: str, color: str) -> str:
    """样式表 url() 需要文件路径；写入临时目录并返回正斜杠路径。"""

    folder = Path(tempfile.gettempdir()) / "autoslice-icons"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{color.lstrip('#')}.svg"
    path.write_text(svg_markup(name, color), encoding="utf-8")
    return path.as_posix()
