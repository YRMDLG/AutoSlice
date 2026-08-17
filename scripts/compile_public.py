"""编译检查公开仓库中的 Python 源码，不写入 pyc 文件。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def discover_public_python_files(root: Path = ROOT) -> list[Path]:
    """返回 Git 会纳入公开发布候选的 Python 文件。"""

    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    names = result.stdout.decode("utf-8", errors="strict").split("\0")
    return sorted(
        root / name
        for name in names
        if (
            name
            and Path(name).suffix.casefold() == ".py"
            and (root / name).is_file()
        )
    )


def main() -> int:
    files = discover_public_python_files()
    failures: list[str] = []
    for path in files:
        try:
            compile(path.read_bytes(), str(path), "exec")
        except (OSError, SyntaxError, UnicodeError) as exc:
            failures.append(f"{path.relative_to(ROOT)}：{exc}")
    if failures:
        for failure in failures:
            print(f"编译失败：{failure}", file=sys.stderr)
        return 1
    print(f"Python 源码编译检查通过：{len(files)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
