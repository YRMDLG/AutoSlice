"""双击启动 AutoCover 本地封面工作台。"""

from __future__ import annotations

from autocover.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["serve"]))
