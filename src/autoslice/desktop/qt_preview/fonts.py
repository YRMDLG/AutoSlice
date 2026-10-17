"""随程序分发的界面字体（源流黑体，SIL OFL 1.1）；缺失时由主题字体栈回退系统字体。"""

from functools import lru_cache
from pathlib import Path

from PySide6.QtGui import QFontDatabase

FONT_DIR = Path(__file__).resolve().parents[2] / "resources" / "fonts"


@lru_cache(maxsize=1)
def load() -> tuple[str, ...]:
    """注册字体目录下的 ttf；须在 QApplication 创建后调用，可重复调用。"""

    families: list[str] = []
    for path in sorted(FONT_DIR.glob("*.ttf")):
        font_id = QFontDatabase.addApplicationFont(str(path))
        if font_id >= 0:
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
    return tuple(dict.fromkeys(families))
