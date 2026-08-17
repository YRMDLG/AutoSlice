"""AutoCover 旧源码入口到 ``src`` owner 的临时模块别名工具。"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from types import ModuleType

SOURCE_DIR = Path(__file__).resolve().parent.parent / "src"


def alias_module(legacy_name: str, target_name: str) -> ModuleType:
    """让旧模块名与 ``autoslice_cover`` owner 指向同一个模块对象。"""

    if str(SOURCE_DIR) not in sys.path:
        sys.path.insert(0, str(SOURCE_DIR))
    implementation = import_module(target_name)
    sys.modules[legacy_name] = implementation
    return implementation
