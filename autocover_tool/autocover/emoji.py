"""Windows Emoji 字形检测和彩色透明图层生成。"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path

from PIL import Image


BROWSER_PATH_ENV = "AUTOCOVER_BROWSER_PATH"
CLEANUP_DELAY_SECONDS = 2.0
SCREENSHOT_WAIT_SECONDS = 5.0
_PENDING_CLEANUPS: set[Path] = set()
_PENDING_CLEANUPS_LOCK = threading.Lock()
_CLEANUP_THREADS: set[threading.Thread] = set()


def _forget_pending_cleanup(path: Path) -> None:
    with _PENDING_CLEANUPS_LOCK:
        _PENDING_CLEANUPS.discard(path)


def _cleanup_pending_trees() -> None:
    with _PENDING_CLEANUPS_LOCK:
        paths = tuple(_PENDING_CLEANUPS)
        threads = tuple(_CLEANUP_THREADS)
    for thread in threads:
        thread.join(timeout=12)
    if paths:
        time.sleep(0.75)
    for path in paths:
        if _remove_temporary_tree(path):
            _forget_pending_cleanup(path)


atexit.register(_cleanup_pending_trees)


def _remove_temporary_tree(path: Path) -> bool:
    """清理 Chromium 临时目录，不让后台进程的短暂占用中断封面导出。"""

    for attempt in range(40):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt < 39:
                time.sleep(0.25)
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def _schedule_temporary_tree_cleanup(path: Path) -> None:
    """等待 Chromium 子进程退出后再删目录，避免删除后又被重建。"""

    with _PENDING_CLEANUPS_LOCK:
        _PENDING_CLEANUPS.add(path)

    def cleanup() -> None:
        try:
            time.sleep(CLEANUP_DELAY_SECONDS / 2)
            _remove_temporary_tree(path)
            time.sleep(CLEANUP_DELAY_SECONDS / 2)
            if _remove_temporary_tree(path):
                _forget_pending_cleanup(path)
        finally:
            with _PENDING_CLEANUPS_LOCK:
                _CLEANUP_THREADS.discard(threading.current_thread())

    thread = threading.Thread(
        target=cleanup,
        daemon=True,
        name="autocover-emoji-cleanup",
    )
    with _PENDING_CLEANUPS_LOCK:
        _CLEANUP_THREADS.add(thread)
    thread.start()


def get_emoji_font_path() -> str | None:
    """返回 Windows Emoji 字体路径；其他系统返回 None。"""

    windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    path = windows_dir / "Fonts" / "seguiemj.ttf"
    return str(path.resolve()) if path.is_file() else None


def is_emoji_character(character: str) -> bool:
    """判断字符是否应优先交给 Emoji 字体绘制。"""

    codepoint = ord(character)
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x1FC00 <= codepoint <= 0x1FFFD
        or 0x2300 <= codepoint <= 0x23FF
        or 0x2600 <= codepoint <= 0x27BF
        or 0x2B00 <= codepoint <= 0x2BFF
        or 0xFE0E <= codepoint <= 0xFE0F
        or 0x1F3FB <= codepoint <= 0x1F3FF
        or codepoint == 0x200D
    )


def get_chromium_path() -> str | None:
    """查找可用的 Chrome、Edge 或 Chromium。"""

    configured = os.environ.get(BROWSER_PATH_ENV, "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft"
        / "Edge"
        / "Application"
        / "msedge.exe",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return str(candidate.resolve())
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe", "chromium"):
        executable = shutil.which(name)
        if executable:
            return executable
    return None


@lru_cache(maxsize=128)
def render_emoji_image(text: str, size: int) -> Image.Image | None:
    """通过 Chromium 使用 Segoe UI Emoji 生成彩色透明图层。"""

    if not text or size <= 0 or get_emoji_font_path() is None:
        return None
    executable = get_chromium_path()
    if executable is None:
        return None

    root = Path(tempfile.mkdtemp(prefix="autocover-emoji-"))
    try:
        html_path = root / "emoji.html"
        output = root / "emoji.png"
        profile = root / "profile"
        width = max(128, round(size * max(2.0, len(text) * 1.65)))
        height = max(96, round(size * 1.8))
        entities = "".join(f"&#x{ord(character):X};" for character in text)
        html = (
            '<!doctype html><meta charset="utf-8"><style>'
            "html,body{margin:0;background:transparent;overflow:hidden}"
            "span{display:inline-block;white-space:nowrap;"
            f'font-family:"Segoe UI Emoji";font-size:{int(size)}px;line-height:1.35}}'
            f"</style><span>{entities}</span>"
        )
        html_path.write_text(html, encoding="utf-8")
        try:
            completed = subprocess.run(
                [
                    executable,
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--no-first-run",
                    "--disable-background-mode",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-crash-reporter",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--no-service-autorun",
                    "--noerrdialogs",
                    "--default-background-color=00000000",
                    "--force-device-scale-factor=1",
                    f"--window-size={width},{height}",
                    f"--screenshot={output}",
                    f"--user-data-dir={profile}",
                    html_path.as_uri(),
                ],
                capture_output=True,
                timeout=20,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode not in {0, None}:
            return None
        deadline = time.monotonic() + SCREENSHOT_WAIT_SECONDS
        while time.monotonic() < deadline:
            if output.is_file():
                try:
                    with Image.open(output) as image:
                        rgba = image.convert("RGBA")
                        bbox = rgba.getchannel("A").getbbox()
                        if bbox is not None:
                            return rgba.crop(bbox).copy()
                except (OSError, ValueError):
                    pass
            time.sleep(0.05)
        return None
    finally:
        _schedule_temporary_tree_cleanup(root)
