"""扫描档案：遍历根目录、认出投稿文件夹、跑抽取器、把事实写进证据库。

设计上面对的是“从第一条切片到现在都留着”的海量旧数据：
- 只读：不修改、不移动、不删除档案里的任何文件，也不往档案里写任何东西；
- 不打开视频：只看文件名、大小、修改时间；字幕、小图才会被读；
- 增量：文件夹的指纹和抽取版本都没变就整个跳过，第二次扫描几乎是即时的；
- 可中断续跑：随时取消，已经写进库的不丢，下次接着扫；定期提交，崩溃最多丢一小批；
- 容错：单个文件夹出错（坏字幕、没权限、名字太长）记一笔继续，不中断整次扫描；
- 省内存：边遍历边处理，不一次性把几十万个文件名读进内存。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Sequence

from .extractors import DEFAULT_EXTRACTORS, ExtractContext, Extractor, fact_tables, schemas
from .layout import StreamerMatcher, classify_folder
from .model import (
    EXTRACT_VERSION,
    MAX_DEPTH,
    SKIP_DIRS,
    ArchiveEntry,
    ScanOptions,
    ScanProgress,
    ScanResult,
)
from .store import ArchiveStore

ProgressCallback = Callable[[ScanProgress], None]
# 每处理这么多个变化的文件夹提交一次。
_COMMIT_EVERY = 100


def open_store(path: str | Path, extractors: Sequence[Extractor] = DEFAULT_EXTRACTORS) -> ArchiveStore:
    return ArchiveStore(path, schemas(extractors))


def walk_folders(root: str, cancel: threading.Event | None = None) -> Iterator[tuple[str, str, list[tuple[str, int, int]]]]:
    """依次给出（文件夹路径, 文件夹名, [(文件名, 大小, 修改时间 ns)]）；只看常规文件，不跟符号链接和联接点。"""

    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        if cancel is not None and cancel.is_set():
            return
        folder, depth = stack.pop()
        files: list[tuple[str, int, int]] = []
        subfolders: list[str] = []
        try:
            with os.scandir(folder) as scanner:
                for item in scanner:
                    try:
                        if item.is_symlink():
                            continue
                        if item.is_dir(follow_symlinks=False):
                            if depth < MAX_DEPTH and item.name.casefold() not in SKIP_DIRS and not item.name.startswith("."):
                                subfolders.append(item.path)
                        elif item.is_file(follow_symlinks=False):
                            stat = item.stat(follow_symlinks=False)
                            files.append((item.name, stat.st_size, stat.st_mtime_ns))
                    except OSError:
                        continue
        except OSError:
            continue
        # 路径统一用正斜杠，数据库里和报告里更好认；排序后遍历顺序稳定。
        yield folder.replace("\\", "/"), os.path.basename(folder.rstrip("\\/")) or folder, files
        stack.extend((path, depth + 1) for path in sorted(subfolders, reverse=True))


def analyze_entry(
    store: ArchiveStore, root: str, entry: ArchiveEntry, streamer: str, options: ScanOptions,
    extractors: Sequence[Extractor] = DEFAULT_EXTRACTORS,
) -> None:
    """把一个文件夹的条目和各抽取器的事实写进库（调用方负责提交）。"""

    context = ExtractContext(options=options, streamer=streamer)
    results: dict[str, Sequence[Sequence]] = {}
    errors: list[str] = []
    for extractor in extractors:
        if not extractor.enabled(options):
            continue
        try:
            results.update(extractor.extract(entry, context))
        except Exception as exc:  # 抽取器的毛病不能中断整次扫描
            errors.append(f"{extractor.name}: {type(exc).__name__}: {exc}"[:200])
    entry_id = store.upsert_entry(root, entry, streamer, 0 if errors else EXTRACT_VERSION, "; ".join(errors))
    for extractor in extractors:
        for table, columns in extractor.tables.items():
            if extractor.enabled(options) or table in results:
                store.replace_rows(table, entry_id, columns, results.get(table, ()))
    if errors:
        raise RuntimeError("; ".join(errors))


def scan_roots(
    store: ArchiveStore,
    roots: Sequence[str],
    options: ScanOptions | None = None,
    *,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    extractors: Sequence[Extractor] = DEFAULT_EXTRACTORS,
    matcher: StreamerMatcher | None = None,
) -> ScanResult:
    """扫描若干根目录；没变的文件夹跳过。返回汇总（取消时 cancelled 为真，已写入的数据保留）。"""

    options = options or ScanOptions()
    matcher = matcher or StreamerMatcher.from_profiles()
    result = ScanResult()
    state = ScanProgress()
    started = time.monotonic()
    last_report = 0.0
    run_id = store.start_run()
    pending = 0

    def report(force: bool = False) -> None:
        nonlocal last_report
        now = time.monotonic()
        if progress is not None and (force or now - last_report >= options.progress_interval):
            last_report = now
            progress(state)

    for raw_root in roots:
        root = str(Path(raw_root).expanduser()).replace("\\", "/")
        store.add_root(root)
        state.root = root
        seen_folders: set[str] = set()
        completed = True
        if not os.path.isdir(root):
            result.errors.append(f"目录不存在或打不开：{root}")
            state.errors += 1
            continue
        for folder, name, files in walk_folders(root, cancel):
            state.folders += 1
            state.current = folder
            if cancel is not None and cancel.is_set():
                completed = False
                break
            if options.limit is not None and state.entries >= options.limit:
                completed = False
                break
            entry = classify_folder(folder, name, files)
            if entry is None:
                report()
                continue
            state.entries += 1
            seen_folders.add(folder)
            known = store.entry_state(folder)
            if known is not None and not options.force and known[1] == entry.signature and known[2] == EXTRACT_VERSION:
                state.skipped += 1
                result.skipped += 1
                report()
                continue
            try:
                analyze_entry(store, root, entry, matcher.infer(folder, name), options, extractors)
            except Exception as exc:
                state.errors += 1
                result.errors.append(f"{folder}: {exc}"[:300])
            state.changed += 1
            result.changed += 1
            pending += 1
            if pending >= _COMMIT_EVERY:
                store.commit()
                pending = 0
            report()
        if completed and options.cleanup_missing and not (cancel is not None and cancel.is_set()):
            for folder, entry_id in store.folders_of_root(root).items():
                if folder not in seen_folders:
                    store.delete_entry(entry_id, fact_tables(extractors))
                    result.removed += 1
        store.mark_scanned(root)
        if not completed:
            break
    store.commit()
    result.entries = state.entries
    result.cancelled = bool(cancel is not None and cancel.is_set())
    result.seconds = round(time.monotonic() - started, 2)
    state.finished = True
    state.cancelled = result.cancelled
    report(force=True)
    store.finish_run(
        run_id, "cancelled" if result.cancelled else "done", result.entries, result.changed, result.skipped,
        len(result.errors), "; ".join(result.errors[:5]),
    )
    return result
