"""先安装外部副作用护栏，再发现并运行整个 unittest 测试集。"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from test_external_boundaries import install_test_external_boundary_guard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-q", "--quiet", action="store_true", help="减少 unittest 输出")
    args = parser.parse_args(argv)

    install_test_external_boundary_guard()
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(ROOT),
        pattern="test*.py",
        top_level_dir=str(ROOT),
    )
    result = unittest.TextTestRunner(verbosity=0 if args.quiet else 2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
