"""离线界面截图：三页（字幕、封面、设置）× 两套主题（夜航、晨雾）。

    python scripts/manual/ui_screenshots.py [输出目录] [--size 1440x900]

只用临时目录里合成的示例视频和字幕，不读本机 AI 配置、投稿目录和应用数据；
截图默认写到仓库 work/ui-shots（已被 git 忽略）。offscreen 截图只能对照布局和配色，
不能代替真实 Windows 字体、mpv 播放、GPU 和动效手感的实机验收。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_LINES = (
    "先别急，我把画面暂停一下。", "好，现在应该能看清楚了。", "这个地图我昨天其实偷偷练过一编",
    "但是那个时候根本没有遇到这个机关", "你们看左上角，是不是有个小门", "我试一下能不能从这边饶过去",
    "诶？诶诶诶？", "等一下，它为什么再追我", "弹幕说往右跑，往右跑是吧", "好好好，跑出来了跑出来了",
    "刚才那个真的差一点就没了", "我宣布今天这把是我打得最好的一把", "下一关我们换个打法试试",
    "这个道具到底是干什么用的", "原来要先把开关全部打开", "终于通关了，休息一下喝口水",
)


def _isolate(temp: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # 离线平台默认只认程序自带的一种字体；给它系统字体目录，时间码等宽字和中文回退才接近实机
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if os.environ["QT_QPA_PLATFORM"] == "offscreen" and windows_fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(windows_fonts))
    os.environ["AUTOSLICE_REDUCED_MOTION"] = "1"
    os.environ["AUTOSLICE_DESKTOP_DATA_DIR"] = str(temp / "app-data")
    # 环境变量优先于 api_config.json：置空即“未配置”，截图过程不会调用真实接口
    for name in ("AUTOSLICE_API_BASE_URL", "AUTOSLICE_API_TOKEN", "AUTOSLICE_API_TYPE"):
        os.environ[name] = ""
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _timestamp(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _make_project(root: Path) -> Path:
    project = root / "投稿" / "半夜直播的意外名场面"
    project.mkdir(parents=True)
    video = project / "示例回放.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=40",
             # 音量起伏的示例音轨：时间轴上的波形像说话而不是一条平带
             "-f", "lavfi", "-i",
             "aevalsrc='0.7*sin(2*PI*220*t)*abs(sin(1.7*t))*(0.35+0.65*abs(sin(0.43*t+1)))':s=22050:d=40",
             "-shortest",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)],
            check=True,
        )
    else:
        video.write_bytes(b"")
    lines = []
    start = 1.0
    for index, text in enumerate(SAMPLE_LINES, 1):
        end = start + 1.6 + (index % 3) * 0.4
        lines.append(f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n")
        start = end + 0.15
    (project / "示例回放.srt").write_text("\n".join(lines), encoding="utf-8")
    return root / "投稿"


def _demo_suggestions(window) -> None:
    """本地示例建议（不调 AI、不落盘）：让截图里能看到选中行下面展开的“建议改为”。"""

    from autoslice.desktop.ai_review import AIReviewSession, Suggestion, document_hash

    entries = window.document.entries
    picks = ((2, "编", "遍", "错字"), (5, "饶", "绕", "同音字"), (7, "再", "在", "错字"))
    suggestions = [
        Suggestion(f"demo-{index}", entries[index].index, entries[index].text,
                   entries[index].text.replace(old, new), reason, 0.9)
        for index, old, new, reason in picks if index < len(entries) and old in entries[index].text
    ]
    window.ai_session = AIReviewSession(suggestions, "demo", document_hash(entries), local_only=True)
    window._render_ai()
    window._select_ai(suggestions[1].suggestion_id if len(suggestions) > 1 else suggestions[0].suggestion_id)


def _settle(app, seconds: float = 0.6) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default=str(ROOT / "work" / "ui-shots"))
    parser.add_argument("--size", default="1440x900")
    parser.add_argument("--themes", default="night,morning")
    args = parser.parse_args(argv)
    width, height = (int(value) for value in args.size.lower().split("x"))
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="autoslice-shots-") as directory:
        temp = Path(directory)
        _isolate(temp)
        from PySide6.QtWidgets import QApplication

        from autoslice.desktop import ai_settings
        from autoslice.desktop.foundation import DesktopStorage
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_preview import appearance, fonts, motion

        app = QApplication.instance() or QApplication([])
        app.setStyle("Fusion")
        fonts.load()
        appearance.apply(app, appearance.Appearance.of("night"))
        motion.install_app(app)
        submissions = _make_project(temp)
        storage = DesktopStorage(temp / "app-data")
        with patch.object(ai_settings, "ai_config_path", return_value=temp / "api_config.json"), \
                patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("截图模式不加载播放器")):
            from autoslice.desktop.qt_app.window import DesktopWindow

            window = DesktopWindow(SubmissionProjectService(submissions), storage)
            window.resize(width, height)
            window.show()
            deadline = time.monotonic() + 20
            while not window.project_buttons and time.monotonic() < deadline:
                _settle(app, 0.2)
            if window.project_buttons:
                next(iter(window.project_buttons.values())).click()
            while window.document is None and time.monotonic() < deadline:
                _settle(app, 0.2)
            _settle(app, 1.5)
            if window.document is not None and window.document.entries:
                _demo_suggestions(window)
                window._select_cue(window.document.entries[5].index)
            for theme in args.themes.split(","):
                appearance.apply(app, appearance.Appearance.of(theme.strip()))
                for page, name in ((0, "subtitles"), (1, "cover"), (2, "settings")):
                    window._select_page(page)
                    _settle(app, 4.0 if page == 1 else 0.8)
                    path = output / f"{theme.strip()}-{name}-{width}x{height}.png"
                    window.grab().save(str(path))
                    print(path)
            window.close()
            _settle(app, 0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
