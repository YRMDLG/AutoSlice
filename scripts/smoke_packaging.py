"""验证 editable 安装的元数据、包导入和命令行入口。"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import sys

EXPECTED_DISTRIBUTION = "autoslice"
EXPECTED_ENTRY_POINT = "autoslice.launcher:main"
EXPECTED_COVER_ENTRY_POINT = "autoslice_cover.cli:main"


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
    if entry_points.get("autoslice-cover") != EXPECTED_COVER_ENTRY_POINT:
        print(
            "打包冒烟失败：autoslice-cover 命令行入口缺失或指向错误",
            file=sys.stderr,
        )
        return 1

    import autoslice
    import autoslice.launcher
    import autoslice.runtime_config
    import autoslice.web.app
    import autoslice_cover
    import autoslice_cover.app

    if (
        not autoslice.__file__
        or not autoslice.launcher.__file__
        or not autoslice.web.app.__file__
    ):
        print("打包冒烟失败：AutoSlice 包来源不可定位", file=sys.stderr)
        return 1
    if not autoslice_cover.__file__ or not autoslice_cover.app.__file__:
        print("打包冒烟失败：AutoCover 包来源不可定位", file=sys.stderr)
        return 1

    package_root = importlib.resources.files("autoslice")
    required_resources = (
        package_root / "resources" / "templates" / "topic_v2.html",
        package_root / "resources" / "static" / "workbench.css",
        package_root / "streamer_profiles.json",
        package_root / "title_style_profile.example.json",
    )
    if any(not resource.is_file() for resource in required_resources):
        print("打包冒烟失败：AutoSlice 包资源不完整", file=sys.stderr)
        return 1

    cover_root = importlib.resources.files("autoslice_cover")
    required_cover_resources = (
        cover_root / "resources" / "templates" / "index.html",
        cover_root / "resources" / "static" / "app.js",
        cover_root / "resources" / "static" / "styles.css",
    )
    if any(not resource.is_file() for resource in required_cover_resources):
        print("打包冒烟失败：AutoCover 包资源不完整", file=sys.stderr)
        return 1

    print(
        "editable 安装冒烟通过："
        f"{distribution.metadata['Name']} {distribution.version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
