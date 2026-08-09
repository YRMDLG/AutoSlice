"""在独立 AutoCover 工作区和公开仓库内置副本之间做白名单同步。"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPOSITORY_ROOT.parent / "AutoCover"
DEFAULT_DESTINATION = REPOSITORY_ROOT / "autocover_tool"

MANAGED_ROOT_FILES = ("app.py",)
MANAGED_DIRECTORY_SUFFIXES = {
    "autocover": frozenset({".py"}),
    "static": frozenset({".css", ".js"}),
    "templates": frozenset({".html"}),
    "tests": frozenset({".py"}),
}


@dataclass(frozen=True)
class SyncDifference:
    """一项待同步差异。"""

    state: str
    relative_path: Path


def _validate_source(source: Path) -> None:
    required = (
        source / "app.py",
        source / "autocover" / "__init__.py",
        source / "static" / "app.js",
        source / "templates" / "index.html",
    )
    missing = [path for path in required if not path.is_file()]
    if missing:
        rendered = "、".join(str(path) for path in missing)
        raise ValueError(f"AutoCover 源目录不完整：{rendered}")


def managed_files(root: Path) -> dict[Path, Path]:
    """返回允许同步的相对路径及实际文件，不包含本机资源和项目元数据。"""

    root = root.resolve()
    files: dict[Path, Path] = {}
    for filename in MANAGED_ROOT_FILES:
        path = root / filename
        if path.is_file():
            files[Path(filename)] = path

    for directory, suffixes in MANAGED_DIRECTORY_SUFFIXES.items():
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if (
                path.is_file()
                and path.suffix.casefold() in suffixes
                and "__pycache__" not in path.parts
            ):
                files[path.relative_to(root)] = path
    return dict(sorted(files.items(), key=lambda item: str(item[0]).casefold()))


def compare_trees(source: Path, destination: Path) -> list[SyncDifference]:
    """比较两棵白名单文件树。"""

    _validate_source(source)
    source_files = managed_files(source)
    destination_files = managed_files(destination)
    differences: list[SyncDifference] = []
    for relative_path in sorted(
        set(source_files) | set(destination_files),
        key=lambda path: str(path).casefold(),
    ):
        source_path = source_files.get(relative_path)
        destination_path = destination_files.get(relative_path)
        if source_path is None:
            differences.append(SyncDifference("仅公开副本", relative_path))
        elif destination_path is None:
            differences.append(SyncDifference("仅本地源", relative_path))
        elif source_path.read_bytes() != destination_path.read_bytes():
            differences.append(SyncDifference("内容不同", relative_path))
    return differences


def apply_sync(source: Path, destination: Path) -> list[SyncDifference]:
    """把白名单文件同步到公开副本，并移除已经从源代码删除的白名单文件。"""

    differences = compare_trees(source, destination)
    source_files = managed_files(source)
    destination_files = managed_files(destination)
    destination.mkdir(parents=True, exist_ok=True)

    for relative_path, source_path in source_files.items():
        destination_path = destination / relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if (
            not destination_path.is_file()
            or source_path.read_bytes() != destination_path.read_bytes()
        ):
            shutil.copy2(source_path, destination_path)

    for relative_path, destination_path in destination_files.items():
        if relative_path not in source_files:
            destination_path.unlink()
    return differences


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查或更新公开仓库中的 AutoCover 白名单源码副本。",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="独立 AutoCover 工作区，默认是公开仓库相邻的 AutoCover 目录。",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_DESTINATION,
        help="内置 AutoCover 目录，默认是当前仓库的 autocover_tool。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="只检查差异（默认）。",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="把白名单差异复制到内置目录。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.source.expanduser().resolve()
    destination = args.destination.expanduser().resolve()
    try:
        differences = (
            apply_sync(source, destination)
            if args.apply
            else compare_trees(source, destination)
        )
    except (OSError, ValueError) as exc:
        print(f"AutoCover 同步失败：{exc}", file=sys.stderr)
        return 2

    if args.apply:
        if differences:
            print(f"AutoCover 同步完成：处理 {len(differences)} 项差异")
        else:
            print("AutoCover 已经同步，无需修改")
        return 0

    if differences:
        print("AutoCover 尚未同步：")
        for item in differences:
            print(f"- {item.state}: {item.relative_path}")
        print("确认差异后执行：python scripts/sync_autocover.py --apply")
        return 1

    print("AutoCover 白名单源码已同步")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
