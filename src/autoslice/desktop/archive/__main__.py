"""命令行：在有旧切片的那台机器上跑，``python -m autoslice.desktop.archive --help``。

典型流程：
  inspect  先看看文件夹是怎么被认出来的（不写任何东西）；
  scan     扫描并生成报告（可以随时 Ctrl+C，下次接着扫）；
  proposals / apply  看学到了什么、确认后应用；
  report   用库里已有的数据重新生成报告；digest.json 可以整份交给 GPT。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage

from .layout import StreamerMatcher, classify_folder
from .model import ScanOptions, ScanProgress
from .scanner import walk_folders
from .service import ArchiveService


def _service(args) -> ArchiveService:
    return ArchiveService(DesktopStorage(args.data_dir) if args.data_dir else DesktopStorage())


def _cmd_inspect(args) -> int:
    """只读：看文件夹怎么被认出来，以及没被认出来的文件夹里都是什么文件。"""

    matcher = StreamerMatcher.from_profiles()
    folders = entries = 0
    features: Counter = Counter()
    streamers: Counter = Counter()
    extensions: Counter = Counter()
    unrecognized: Counter = Counter()
    samples = []
    for root in args.roots:
        for folder, name, files in walk_folders(root):
            folders += 1
            entry = classify_folder(folder, name, files)
            for file_name, _size, _mtime in files:
                suffix = Path(file_name).suffix.lower() or "(无后缀)"
                extensions[suffix] += 1
                if entry is None:
                    unrecognized[suffix] += 1
            if entry is None:
                continue
            entries += 1
            streamers[matcher.label(matcher.infer(folder, name))] += 1
            features["有字幕初稿"] += any(video.srt for video in entry.videos)
            features["有校对稿"] += any(video.corrected for video in entry.videos)
            features["初稿+校对稿成对"] += entry.has_pairs
            features["有字幕版成片"] += any(video.burned for video in entry.videos)
            features["有封面候选"] += bool(entry.covers)
            features["有文字笔记"] += bool(entry.notes)
            if len(samples) < args.samples:
                samples.append(entry)
            if args.limit and entries >= args.limit:
                break
    print(f"走过 {folders} 个文件夹，认出 {entries} 个投稿文件夹")
    for key, count in features.items():
        print(f"  {key}：{count}")
    print("按主播：" + "、".join(f"{label} {n}" for label, n in streamers.most_common(8)))
    print("文件后缀（前 12）：" + "、".join(f"{suffix} {n}" for suffix, n in extensions.most_common(12)))
    if unrecognized:
        print("没被认成投稿文件夹里的后缀（前 8）：" + "、".join(f"{suffix} {n}" for suffix, n in unrecognized.most_common(8)))
    for entry in samples:
        flags = [
            "初稿" if any(video.srt for video in entry.videos) else "",
            "校对" if any(video.corrected for video in entry.videos) else "",
            "字幕版" if any(video.burned for video in entry.videos) else "",
            f"封面×{len(entry.covers)}" if entry.covers else "",
        ]
        print(f"  · {entry.title[:40]}  [{entry.date or '?'}]  {' '.join(flag for flag in flags if flag)}")
    return 0


def _cmd_scan(args) -> int:
    service = _service(args)
    cancel = threading.Event()
    last = [0.0]

    def progress(state: ScanProgress) -> None:
        now = time.monotonic()
        if state.finished or now - last[0] >= 2.0:
            last[0] = now
            print(f"\r已走 {state.folders} 个文件夹 · 条目 {state.entries} · 新抽取 {state.changed} · 跳过 {state.skipped}"
                  f" · 出错 {state.errors}   ", end="", file=sys.stderr, flush=True)

    options = ScanOptions(covers=not args.no_covers, limit=args.limit or None, force=args.force)
    try:
        result = service.scan(args.roots or None, options, progress=progress, cancel=cancel)
    except KeyboardInterrupt:
        cancel.set()
        print("\n已请求停止，已写入的不会丢，下次接着扫。", file=sys.stderr)
        return 130
    print(file=sys.stderr)
    print(f"完成：{result.entries} 个条目，新抽取 {result.changed}，跳过 {result.skipped}，清理 {result.removed}，"
          f"用时 {result.seconds} 秒{'（被中断）' if result.cancelled else ''}")
    for error in result.errors[:5]:
        print(f"  ! {error}")
    paths = service.report()
    print(f"报告：{paths['markdown']}\n可以交给 GPT 的摘要：{paths['digest']}")
    return 0


def _cmd_report(args) -> int:
    service = _service(args)
    if not service.has_data():
        print("还没有扫描过：先运行 scan。")
        return 1
    paths = service.report()
    print(f"报告：{paths['markdown']}\n可以交给 GPT 的摘要：{paths['digest']}")
    return 0


def _cmd_proposals(args) -> int:
    proposals = _service(args).proposals(include_done=args.all)
    if not proposals:
        print("没有待确认的建议。")
        return 0
    for item in proposals[: args.limit]:
        print(f"{item.id}  [{item.kind}] {item.label} · {item.title}  ({item.evidence})")
    if len(proposals) > args.limit:
        print(f"…… 还有 {len(proposals) - args.limit} 条，用 --limit 调大。")
    return 0


def _cmd_apply(args) -> int:
    service = _service(args)
    proposals = service.proposals()
    kinds = set(args.kinds.split(",")) if args.kinds else set()
    ids = set(args.ids or ())
    chosen = [item for item in proposals if (not kinds or item.kind in kinds) and (not ids or item.id in ids)]
    if not (kinds or ids or args.all):
        print("请用 --kinds、--ids 或 --all 指定要应用哪些建议。")
        return 2
    applied, failures = service.apply(chosen)
    print(f"已应用 {len(applied)} 条。")
    for failure in failures[:10]:
        print(f"  ! {failure}")
    return 0


def _cmd_forget(args) -> int:
    if not args.yes:
        print("这会删掉证据库、报告和学到的知识（不碰你的档案文件夹）；确认请加 --yes。")
        return 2
    _service(args).forget_everything()
    print("已清除。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m autoslice.desktop.archive", description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", help="应用数据目录（默认 %%LOCALAPPDATA%%/AutoSlice）")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="只读：看文件夹怎么被认出来")
    inspect.add_argument("roots", nargs="+")
    inspect.add_argument("--limit", type=int, default=0, help="最多认这么多个条目（0 为不限）")
    inspect.add_argument("--samples", type=int, default=12)
    inspect.set_defaults(handler=_cmd_inspect)
    scan = sub.add_parser("scan", help="扫描并生成报告")
    scan.add_argument("roots", nargs="*", help="不写就扫已登记的目录")
    scan.add_argument("--limit", type=int, default=0, help="只扫前 N 个条目（试跑）")
    scan.add_argument("--no-covers", action="store_true", help="不分析旧封面")
    scan.add_argument("--force", action="store_true", help="全部重新抽取")
    scan.set_defaults(handler=_cmd_scan)
    sub.add_parser("report", help="用库里已有的数据重新生成报告").set_defaults(handler=_cmd_report)
    proposals = sub.add_parser("proposals", help="列出待确认的建议")
    proposals.add_argument("--all", action="store_true", help="也列出已处理的")
    proposals.add_argument("--limit", type=int, default=60)
    proposals.set_defaults(handler=_cmd_proposals)
    apply = sub.add_parser("apply", help="应用建议")
    apply.add_argument("--kinds", help="replacement,glossary,titles,subtitle_limits（逗号分隔）")
    apply.add_argument("--ids", nargs="*")
    apply.add_argument("--all", action="store_true")
    apply.set_defaults(handler=_cmd_apply)
    forget = sub.add_parser("forget", help="清除学到的全部内容")
    forget.add_argument("--yes", action="store_true")
    forget.set_defaults(handler=_cmd_forget)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
