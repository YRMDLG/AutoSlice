"""命令行：自检、装法提示、识别一个视频、对比引擎。

    python -m autoslice.desktop.asr doctor [--probe]
    python -m autoslice.desktop.asr setup
    python -m autoslice.desktop.asr run 视频.mp4 [--overwrite]
    python -m autoslice.desktop.asr bench --root D:\\旧切片 [--engines qwen3-asr,funasr]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage

from . import bench, doctor
from .engines import ENGINES
from .hotwords import build_hotwords
from .jobs import JobSpec
from .settings import AsrSettings, load_settings
from .setup_text import SETUP_TEXT
from .worker import run_spec


def _settings(args, storage: DesktopStorage) -> AsrSettings:
    settings = load_settings(storage)
    overrides = {key: value for key in ("engine", "model", "aligner", "device")
                 if (value := getattr(args, key, None))}
    return dataclasses.replace(settings, **overrides) if overrides else settings


def _add_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--engine", choices=sorted(ENGINES))
    parser.add_argument("--model")
    parser.add_argument("--aligner")
    parser.add_argument("--device")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoslice.desktop.asr", description="字幕识别")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("doctor", help="检查环境")
    _add_overrides(check)
    check.add_argument("--probe", action="store_true", help="真的加载模型并试跑一小段（会占显存、可能联网下载）")
    check.add_argument("--json", action="store_true")
    sub.add_parser("setup", help="打印安装步骤")
    run = sub.add_parser("run", help="识别一个视频，写同名 .srt")
    run.add_argument("video")
    run.add_argument("--output")
    run.add_argument("--overwrite", action="store_true")
    _add_overrides(run)
    compare = sub.add_parser("bench", help="用你校对过的旧字幕对比引擎")
    compare.add_argument("--root", action="append", required=True, help="旧切片文件夹，可重复")
    compare.add_argument("--engines", default="", help="逗号分隔，默认只测设置里的引擎")
    compare.add_argument("--hotwords", choices=("on", "off", "both"), default="both")
    compare.add_argument("--videos", type=int, default=6, help="最多用几个视频")
    compare.add_argument("--windows", type=int, default=2, help="每个视频取几段")
    compare.add_argument("--window-seconds", type=float, default=180.0)
    compare.add_argument("--out", help="报告写到这个 .md（同名 .json 一起写）")
    _add_overrides(compare)
    args = parser.parse_args(argv)
    storage = DesktopStorage()

    if args.command == "setup":
        print(SETUP_TEXT)
        return 0
    settings = _settings(args, storage)

    if args.command == "doctor":
        checks = doctor.run_checks(settings, storage_root=storage.root)
        if args.probe:
            checks += doctor.probe(settings)
        print(json.dumps([item.to_dict() for item in checks], ensure_ascii=False) if args.json
              else doctor.format_checks(checks))
        return 1 if any(item.status == "fail" for item in checks) else 0

    if args.command == "run":
        video = Path(args.video).expanduser().resolve()
        output = Path(args.output) if args.output else video.with_suffix(".srt")
        spec = JobSpec(str(video), str(output), str(storage.root), dataclasses.asdict(settings),
                       build_hotwords(str(video), storage=storage, title=video.parent.name) if settings.hotwords else [],
                       overwrite=args.overwrite)
        return run_spec(spec, print)

    names = [item for item in args.engines.split(",") if item] or [settings.engine]
    unknown = [item for item in names if item not in ENGINES]
    if unknown:
        parser.error(f"不认识的引擎：{', '.join(unknown)}（可选：{', '.join(sorted(ENGINES))}）")
    modes = {"on": (True,), "off": (False,), "both": (False, True)}[args.hotwords]
    variants = [bench.Variant(f"{ENGINES[name].label.split('（')[0]} · {'带热词' if hot else '无热词'}",
                              dataclasses.replace(settings, engine=name,
                                                  **({} if name == "qwen3-asr" else {"model": "", "aligner": ""})),
                              hot)
                for name in names for hot in modes]
    cases = bench.discover_cases(args.root, window_seconds=args.window_seconds, windows_per_video=args.windows,
                                 max_videos=args.videos)
    if not cases:
        print("没找到可用的片段：需要“有视频 + 有 _校对 字幕”的文件夹。", file=sys.stderr)
        return 2
    print(f"{len(cases)} 个片段，{len(variants)} 个方案，开始…", flush=True)
    scores = bench.run_bench(cases, variants, storage=storage, progress=lambda message: print(message, flush=True))
    report = bench.render_report(scores, cases)
    print(report)
    if args.out:
        out = Path(args.out)
        out.write_text(report, encoding="utf-8")
        out.with_suffix(".json").write_text(json.dumps(bench.to_json(scores), ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n报告已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
