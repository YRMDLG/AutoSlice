"""AutoCover Web 服务与无界面批量命令。"""

from __future__ import annotations

import argparse
import io
import json
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Sequence

from . import API_VERSION, SERVICE_ID
from .renderer import render_cover
from .workspace import CoverWorkspace


def _configure_text_stream(stream: io.TextIOBase) -> None:
    """让 Windows 控制台可安全输出中文、文件名和 emoji。"""

    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="autocover",
        description="AutoCover 泽音切片封面工作台",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动本地 Web 工作台")
    serve.add_argument("--port", type=int, default=5010, help="首选端口，默认 5010")
    serve.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")

    batch = subparsers.add_parser("batch", help="批量生成双比例封面")
    batch.add_argument("root", help="切片目录")
    batch.add_argument("--title-file", help="投稿标题 Markdown 文件")
    batch.add_argument("--output-dir", help="封面输出目录")
    batch.add_argument("--cache-dir", help="候选帧缓存目录")
    batch.add_argument("--count", type=int, default=12, help="每个视频抽取的候选帧数，默认 12")
    batch.add_argument("--force", action="store_true", help="忽略已有候选帧缓存")
    batch.add_argument(
        "--canvas",
        choices=("both", "4x3", "16x9"),
        default="both",
        help="输出比例，默认 both",
    )
    batch.add_argument("--no-recursive", action="store_true", help="不扫描子目录")
    return parser


def find_available_port(preferred: int, attempts: int = 20) -> int:
    """从首选端口开始查找本机可用端口。"""

    if not 1 <= preferred <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    for port in range(preferred, min(65536, preferred + attempts)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"端口 {preferred} 起连续 {attempts} 个端口均被占用")


def _probe_service(port: int, timeout: float = 0.8) -> dict[str, object] | None:
    """读取本机端口的 AutoCover 服务契约，非 JSON 服务返回 None。"""

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/options",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_compatible_service(payload: dict[str, object] | None) -> bool:
    """判断端口上的服务能否被当前前端直接复用。"""

    return bool(
        payload
        and payload.get("service") == SERVICE_ID
        and payload.get("api_version") == API_VERSION
    )


def _open_browser_later(url: str) -> None:
    def open_browser() -> None:
        webbrowser.open(url)

    timer = threading.Timer(0.8, open_browser)
    timer.daemon = True
    timer.start()


def run_server(port: int, *, open_browser: bool = True) -> int:
    """仅在本机启动 Web 工作台。"""

    existing_service = _probe_service(port)
    if _is_compatible_service(existing_service):
        url = f"http://127.0.0.1:{port}"
        print(f"检测到兼容的 AutoCover 服务，直接打开：{url}")
        if open_browser:
            _open_browser_later(url)
        return 0
    if existing_service is not None:
        print(f"端口 {port} 上的服务版本过旧或不兼容，将尝试后续端口")

    selected_port = find_available_port(port)
    try:
        from ..app import create_app
    except ImportError:
        # 兼容在 autocover_tool 目录执行 `python -m autocover.cli`。
        from app import create_app

    url = f"http://127.0.0.1:{selected_port}"
    if selected_port != port:
        print(f"端口 {port} 已被占用，已改用 {selected_port}")
    print(f"AutoCover 已启动：{url}")
    print("按 Ctrl+C 停止服务")
    if open_browser:
        _open_browser_later(url)
    create_app().run(host="127.0.0.1", port=selected_port, debug=False, use_reloader=False)
    return 0


def _canvas_keys(value: str) -> tuple[str, ...]:
    return ("4x3", "16x9") if value == "both" else (value,)


def run_batch(args: argparse.Namespace) -> int:
    """扫描切片、选择最高分帧并批量渲染封面。"""

    if not 1 <= args.count <= 30:
        raise ValueError("候选帧数量必须在 1 到 30 之间")
    workspace = CoverWorkspace(
        args.root,
        title_file=args.title_file,
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
        recursive=not args.no_recursive,
    )
    tasks = workspace.scan()
    if not tasks:
        print("目录中没有找到可处理的视频")
        return 0

    print(f"找到 {len(tasks)} 个切片，开始生成封面")
    failed: list[tuple[str, str]] = []
    generated = 0
    for index, task in enumerate(tasks, start=1):
        print(f"[{index}/{len(tasks)}] {task.filename}")
        try:
            workspace.generate_candidates(task.id, count=args.count, force=args.force)
            candidate = workspace.selected_candidate(task.id)
            for canvas_key in _canvas_keys(args.canvas):
                result = render_cover(
                    candidate.path,
                    task.title,
                    Path(task.output_paths[canvas_key]),
                    canvas_key=canvas_key,
                    template_key=task.template_key,
                    palette_key=task.palette_key,
                )
                generated += 1
                print(f"  {canvas_key}: {result.output_path}")
        except Exception as exc:
            failed.append((task.filename, str(exc)))
            print(f"  失败：{exc}", file=sys.stderr)

    print(f"完成：生成 {generated} 张封面，失败 {len(failed)} 个切片")
    if failed:
        for filename, message in failed:
            print(f"- {filename}：{message}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""

    _configure_text_stream(sys.stdout)
    _configure_text_stream(sys.stderr)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return run_server(args.port, open_browser=not args.no_browser)
    if args.command == "batch":
        try:
            return run_batch(args)
        except (FileNotFoundError, NotADirectoryError, RuntimeError, ValueError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
