"""验证 editable 安装的元数据、包导入和命令行入口。"""

from __future__ import annotations

import importlib.metadata
import sys

EXPECTED_DISTRIBUTION = "autoslice"
EXPECTED_ENTRY_POINT = "autoslice.launcher:main"


def main() -> int:
    distribution = importlib.metadata.distribution(EXPECTED_DISTRIBUTION)
    entry_points = {
        item.name: item.value
        for item in distribution.entry_points
        if item.group == "console_scripts"
    }
    if entry_points.get("autoslice") != EXPECTED_ENTRY_POINT:
        print(
            "打包冒烟失败：autoslice 命令行入口缺失或指向错误",
            file=sys.stderr,
        )
        return 1

    import autocover_tool.autocover
    import autoslice
    import autoslice.launcher

    if not autoslice.__file__ or not autoslice.launcher.__file__:
        print("打包冒烟失败：AutoSlice 包来源不可定位", file=sys.stderr)
        return 1
    if not autocover_tool.autocover.__file__:
        print("打包冒烟失败：AutoCover 包来源不可定位", file=sys.stderr)
        return 1

    print(
        "editable 安装冒烟通过："
        f"{distribution.metadata['Name']} {distribution.version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
