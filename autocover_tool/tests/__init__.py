"""AutoCover 自动化测试。"""

from __future__ import annotations

import sys
from pathlib import Path


# AutoCover 仍支持在自身目录用 `python -m autocover.cli` 独立运行。
# 从统一仓库根目录发现测试时，把该目录放到导入路径最前，避免误导入
# AutoSlice 根目录中同名的 app.py。
AUTOCOVER_ROOT = str(Path(__file__).resolve().parents[1])
if sys.path[0] != AUTOCOVER_ROOT:
    sys.path.insert(0, AUTOCOVER_ROOT)
