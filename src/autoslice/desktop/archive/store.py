"""档案证据库（SQLite）：每个文件夹一行，抽取出来的事实按文件夹挂在各自的表里。

按文件夹存事实，好处是某个文件夹变了只重抽它自己（先删它的旧事实再写新的），汇总全靠 SQL 现算，
不需要“增量更新汇总表”这种容易算错的逻辑。库只存在本机应用数据目录，绝不写进档案文件夹。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Sequence

from .model import SCHEMA_VERSION, ArchiveEntry

_CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS roots (path TEXT PRIMARY KEY, added REAL, last_scan REAL);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    root TEXT NOT NULL,
    folder TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    streamer TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    year INTEGER NOT NULL DEFAULT 0,
    video_count INTEGER NOT NULL DEFAULT 0,
    video_bytes INTEGER NOT NULL DEFAULT 0,
    srt_count INTEGER NOT NULL DEFAULT 0,
    corrected_count INTEGER NOT NULL DEFAULT 0,
    burned_count INTEGER NOT NULL DEFAULT 0,
    cover_count INTEGER NOT NULL DEFAULT 0,
    signature TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0,
    scanned REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS entries_root ON entries(root);
CREATE INDEX IF NOT EXISTS entries_streamer ON entries(streamer);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started REAL, finished REAL, status TEXT NOT NULL DEFAULT 'running',
    seen INTEGER NOT NULL DEFAULT 0, changed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT ''
);
"""


class ArchiveStore:
    """一个线程里用一个实例；读报告的线程自己再开一个（WAL 允许一边写一边读）。"""

    def __init__(self, path: str | Path, schemas: Sequence[str] = ()) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA foreign_keys=OFF")
        self._db.executescript(_CORE_SCHEMA)
        previous = self.scalar("SELECT value FROM meta WHERE key = 'schema_version'", default="1")
        if int(previous) > SCHEMA_VERSION:
            self._db.close()
            raise ValueError("档案数据库版本较新，请使用更新版本的 AutoSlice")
        for ddl in schemas:
            self._db.executescript(ddl)
        # v1 → v2 只新增 final_text 表；保留原始事实，抽取版本负责重扫补齐语料。
        self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        self._db.commit()

    def __enter__(self) -> "ArchiveStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._db.commit()
        finally:
            self._db.close()

    def commit(self) -> None:
        self._db.commit()

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self._db.execute(sql, tuple(params)).fetchall()

    def scalar(self, sql: str, params: Iterable = (), default=0):
        row = self._db.execute(sql, tuple(params)).fetchone()
        return default if row is None or row[0] is None else row[0]

    # ── 根目录 ──
    def add_root(self, path: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO roots(path, added) VALUES(?, ?)", (path, time.time()))
        self._db.commit()

    def remove_root(self, path: str, *, forget_data: bool = False) -> None:
        self._db.execute("DELETE FROM roots WHERE path = ?", (path,))
        if forget_data:
            for row in self.query("SELECT id FROM entries WHERE root = ?", (path,)):
                self.delete_entry(row["id"])
        self._db.commit()

    def roots(self) -> list[str]:
        return [row["path"] for row in self.query("SELECT path FROM roots ORDER BY added")]

    def mark_scanned(self, path: str) -> None:
        self._db.execute("UPDATE roots SET last_scan = ? WHERE path = ?", (time.time(), path))
        self._db.commit()

    # ── 条目 ──
    def entry_state(self, folder: str) -> tuple[int, str, int] | None:
        row = self._db.execute("SELECT id, signature, version FROM entries WHERE folder = ?", (folder,)).fetchone()
        return (row["id"], row["signature"], row["version"]) if row else None

    def upsert_entry(self, root: str, entry: ArchiveEntry, streamer: str, version: int, error: str = "") -> int:
        year = int(entry.date[:4]) if entry.date[:4].isdigit() else 0
        values = (
            root, entry.title, streamer, entry.date, year, len(entry.videos),
            sum(video.size for video in entry.videos), sum(1 for video in entry.videos if video.srt),
            sum(1 for video in entry.videos if video.corrected), sum(1 for video in entry.videos if video.burned),
            len(entry.covers), entry.signature, version, time.time(), error, entry.folder,
        )
        existing = self.entry_state(entry.folder)
        if existing is None:
            cursor = self._db.execute(
                "INSERT INTO entries(root, title, streamer, date, year, video_count, video_bytes, srt_count,"
                " corrected_count, burned_count, cover_count, signature, version, scanned, error, folder)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values,
            )
            return int(cursor.lastrowid)
        self._db.execute(
            "UPDATE entries SET root=?, title=?, streamer=?, date=?, year=?, video_count=?, video_bytes=?,"
            " srt_count=?, corrected_count=?, burned_count=?, cover_count=?, signature=?, version=?, scanned=?,"
            " error=? WHERE folder=?", values,
        )
        return existing[0]

    def replace_rows(self, table: str, entry_id: int, columns: Sequence[str], rows: Iterable[Sequence]) -> None:
        """换掉某个文件夹在某张事实表里的全部行。"""

        self._db.execute(f"DELETE FROM {table} WHERE entry_id = ?", (entry_id,))
        marks = ",".join("?" for _ in range(len(columns) + 1))
        names = ",".join(("entry_id", *columns))
        self._db.executemany(f"INSERT INTO {table}({names}) VALUES({marks})", ((entry_id, *row) for row in rows))

    def delete_entry(self, entry_id: int, tables: Sequence[str] = ()) -> None:
        for table in tables:
            self._db.execute(f"DELETE FROM {table} WHERE entry_id = ?", (entry_id,))
        self._db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

    def folders_of_root(self, root: str) -> dict[str, int]:
        return {row["folder"]: row["id"] for row in self.query("SELECT folder, id FROM entries WHERE root = ?", (root,))}

    # ── 运行记录 ──
    def start_run(self) -> int:
        cursor = self._db.execute("INSERT INTO runs(started) VALUES(?)", (time.time(),))
        self._db.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, seen: int, changed: int, skipped: int, errors: int, note: str = "") -> None:
        self._db.execute(
            "UPDATE runs SET finished=?, status=?, seen=?, changed=?, skipped=?, errors=?, note=? WHERE id=?",
            (time.time(), status, seen, changed, skipped, errors, note[:2000], run_id),
        )
        self._db.commit()

    def last_run(self) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None
