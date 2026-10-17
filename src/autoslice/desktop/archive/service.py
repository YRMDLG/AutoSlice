"""学习旧切片的统一入口：界面和命令行都走这里。

所有产物都在应用数据目录 learning/archive/ 下（证据库、报告、待确认建议、学到的知识），
绝不写进档案文件夹。每个方法在调用它的线程里自己开数据库连接，可以放心丢进后台线程。
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Sequence

from autoslice.desktop.foundation import DesktopStorage

from .knowledge import Knowledge, archive_dir
from .layout import StreamerMatcher
from .model import ScanOptions, ScanResult
from .proposals import (
    PENDING,
    Proposal,
    apply_proposals,
    build_proposals,
    load_status,
    set_status,
    with_status,
)
from .report import write_report
from .scanner import ProgressCallback, open_store, scan_roots
from .store import ArchiveStore


class ArchiveService:
    def __init__(self, storage: DesktopStorage) -> None:
        self.storage = storage
        self.dir = archive_dir(storage)
        self.db_path = self.dir / "archive.db"
        self.report_dir = self.dir / "reports"
        self.knowledge = Knowledge(storage)

    def _store(self) -> ArchiveStore:
        return open_store(self.db_path)

    def has_data(self) -> bool:
        return self.db_path.is_file()

    # ── 档案根目录 ──
    def roots(self) -> list[str]:
        if not self.has_data():
            return []
        with self._store() as store:
            return store.roots()

    def add_root(self, path: str) -> None:
        with self._store() as store:
            store.add_root(str(Path(path).expanduser()).replace("\\", "/"))

    def remove_root(self, path: str, *, forget_data: bool = False) -> None:
        with self._store() as store:
            store.remove_root(path, forget_data=forget_data)

    # ── 扫描 ──
    def scan(
        self, roots: Sequence[str] | None = None, options: ScanOptions | None = None, *,
        progress: ProgressCallback | None = None, cancel: threading.Event | None = None,
    ) -> ScanResult:
        """扫描（默认所有已登记的根目录）；没变的文件夹跳过，所以反复点“开始”是安全的。"""

        with self._store() as store:
            targets = list(roots) if roots else store.roots()
            return scan_roots(store, targets, options, progress=progress, cancel=cancel)

    def summary(self) -> dict:
        """界面上一行摘要要用的几个数。"""

        if not self.has_data():
            return {"entries": 0, "with_both": 0, "corrections": 0, "last_scan": 0.0}
        with self._store() as store:
            last = store.last_run()
            return {
                "entries": store.scalar("SELECT COUNT(*) FROM entries"),
                "with_both": store.scalar("SELECT COUNT(DISTINCT entry_id) FROM edits"),
                "corrections": store.scalar("SELECT COUNT(DISTINCT wrong || right) FROM corrections"),
                "last_scan": float(last["finished"] or 0.0) if last else 0.0,
            }

    # ── 报告 ──
    def report(self, matcher: StreamerMatcher | None = None) -> dict[str, Path]:
        with self._store() as store:
            return write_report(store, self.report_dir, matcher)

    # ── 建议 ──
    def proposals(self, matcher: StreamerMatcher | None = None, *, include_done: bool = False) -> list[Proposal]:
        if not self.has_data():
            return []
        with self._store() as store:
            found = with_status(self.storage, build_proposals(store, matcher))
        return found if include_done else [item for item in found if item.status == PENDING]

    def apply(self, proposals: Sequence[Proposal]) -> tuple[list[str], list[str]]:
        return apply_proposals(self.storage, list(proposals))

    def reject(self, proposals: Sequence[Proposal]) -> None:
        set_status(self.storage, [item.id for item in proposals], "rejected")

    def pending_count(self) -> int:
        return len(self.proposals())

    # ── 隐私：一键清掉学到的全部东西 ──
    def forget_everything(self) -> None:
        """删掉证据库、报告、建议状态和学到的知识；不碰档案文件夹，也不撤销已写进本机覆盖词库的固定纠错。"""

        if self.dir.is_dir():
            shutil.rmtree(self.dir, ignore_errors=True)

    def status(self) -> dict[str, str]:
        return load_status(self.storage)
