"""已安装命令行入口；实际启动逻辑暂由根目录兼容启动器持有。"""

from __future__ import annotations

from importlib import import_module


def main() -> int:
    """调用现有一键启动器，保持 ``python 启动.py`` 与 ``autoslice`` 一致。"""

    launcher = import_module("启动")
    return int(launcher.main())
