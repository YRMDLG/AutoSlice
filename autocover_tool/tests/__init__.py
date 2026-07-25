"""AutoCover 自动化测试。"""

from __future__ import annotations

import sys
from pathlib import Path


# AutoCover 仍支持在自身目录用 `python -m autocover.cli` 独立运行。
# 从统一仓库根目录发现测试时只补充独立包路径，不抢占 AutoSlice 的
# 根模块；需要访问 Web 应用的测试会显式使用 autocover_tool.app。
AUTOCOVER_ROOT = str(Path(__file__).resolve().parents[1])
if AUTOCOVER_ROOT not in sys.path:
    sys.path.append(AUTOCOVER_ROOT)
