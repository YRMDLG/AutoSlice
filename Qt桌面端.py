"""AutoSlice Qt vNext 开发入口；桌面端.py 仍启动旧 Tk。"""

import sys
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from autoslice.desktop.qt_app.__main__ import main  # noqa: E402, I001


if __name__ == "__main__":
    raise SystemExit(main())
