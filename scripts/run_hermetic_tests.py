"""先安装外部副作用护栏，再发现并运行整个 unittest 测试集。"""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_external_boundaries import install_test_external_boundary_guard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-q", "--quiet", action="store_true", help="减少 unittest 输出")
    args = parser.parse_args(argv)

    previous_task_database = os.environ.get("AUTOSLICE_TASK_DB")
    with TemporaryDirectory(prefix="autoslice-hermetic-tasks-") as directory:
        os.environ["AUTOSLICE_TASK_DB"] = str(Path(directory) / "tasks.sqlite3")
        try:
            install_test_external_boundary_guard()
            suite = unittest.defaultTestLoader.discover(
                start_dir=str(ROOT),
                pattern="test*.py",
                top_level_dir=str(ROOT),
            )
            result = unittest.TextTestRunner(
                verbosity=0 if args.quiet else 2
            ).run(suite)
        finally:
            if previous_task_database is None:
                os.environ.pop("AUTOSLICE_TASK_DB", None)
            else:
                os.environ["AUTOSLICE_TASK_DB"] = previous_task_database
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
