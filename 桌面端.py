"""从源码克隆目录直接启动 AutoSlice Desktop vNext。"""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from autoslice.desktop.app import main  # noqa: E402, I001


if __name__ == "__main__":
    main()
