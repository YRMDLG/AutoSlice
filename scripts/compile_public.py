"""编译检查公开仓库中的 Python 源码，不写入 pyc 文件。"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".venv", "venv", "__pycache__"}


def main() -> int:
    files = sorted(
        path
        for path in ROOT.rglob("*.py")
        if not EXCLUDED_PARTS.intersection(path.relative_to(ROOT).parts)
    )
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
