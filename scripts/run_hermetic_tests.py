"""先安装外部副作用护栏，再发现并运行整个 unittest 测试集。"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.external_boundary_guard import (
    install_test_external_boundary_guard,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-q", "--quiet", action="store_true", help="减少 unittest 输出")
    args = parser.parse_args(argv)

    # Windows CI 必须实际执行 AutoCover 的 Node 行为测试；不能因为 runner
    # 没有 Node.js 而让 unittest 的 skipUnless 静默掩盖测试缺失。开发者在
    # 本地没有 Node 时仍可直接运行此脚本，保持原有的友好行为。
    if os.environ.get("AUTOSLICE_REQUIRE_NODE_TESTS") == "1" and not shutil.which("node"):
        print(
            "AUTOSLICE_REQUIRE_NODE_TESTS=1，但当前环境找不到 node；"
            "拒绝以跳过 Node 行为测试的方式通过。",
            file=sys.stderr,
        )
        return 2

    with TemporaryDirectory(prefix="autoslice-hermetic-tasks-") as directory:
        isolated_root = Path(directory)
        environment = {
            "AUTOSLICE_LOCAL_CONFIG": str(isolated_root / "autoslice.local.json"),
            "AUTOSLICE_TASK_DB": str(isolated_root / "tasks.sqlite3"),
            "AUTOSLICE_TITLE_STYLE_PROFILE": str(
                ROOT / "title_style_profile.example.json"
            ),
        }
        previous = {name: os.environ.get(name) for name in environment}
        os.environ.update(environment)
        try:
            install_test_external_boundary_guard()
            suite = unittest.defaultTestLoader.discover(
                start_dir=str(ROOT / "tests"),
                pattern="test*.py",
                top_level_dir=str(ROOT),
            )
            result = unittest.TextTestRunner(
                verbosity=0 if args.quiet else 2
            ).run(suite)
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
