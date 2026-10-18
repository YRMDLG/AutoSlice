# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir 打包 AutoSlice 桌面端（Windows）。用 packaging/build_windows.ps1 运行，不要直接手敲。
#
# 刻意不打进去的：torch、funasr、modelscope（识别在设置里指定的另一个 Python 里跑）、flask（旧网页版）、tkinter（旧 Tk 界面）。
# libmpv-2.dll、ffmpeg.exe、ffprobe.exe 由 build_windows.ps1 复制到程序目录旁边（见 packaging/bin）。

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.environ["PYTHONPATH"] = str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")
WORKER_SRC = ROOT / "build" / "worker_src"  # build_windows.ps1 先把纯源码整理到这里
if not (WORKER_SRC / "autoslice").is_dir():
    raise SystemExit("缺少 build/worker_src，请用 packaging/build_windows.ps1 打包")

SKIP_PREFIXES = ("autoslice.web", "autoslice.desktop.app", "autoslice.setup_gpu_runtime", "autoslice.setup_asr_model")


def keep(name: str) -> bool:
    return not name.startswith(SKIP_PREFIXES)


hiddenimports = (
    collect_submodules("autoslice", filter=keep)
    + collect_submodules("autoslice_cover")
    + ["pypinyin", "PySide6.QtNetwork"]
)
datas = (
    collect_data_files("autoslice")
    + collect_data_files("autoslice_cover")
    + [(str(WORKER_SRC), "worker_src")]
)
for required in ("ResourceHanRoundedCN-Regular.ttf", "ResourceHanRoundedCN-Medium.ttf",
                 "streamer_profiles.json", "seto-bilibili.ttf"):
    if not any(Path(source).name == required and Path(source).is_file() for source, _ in datas):
        raise SystemExit(f"打包资源收集失败：缺少 {required}，检查源码路径和资源文件")
for required in ("autoslice.desktop.qt_app.window", "autoslice.desktop.asr.worker", "autoslice_cover.document_render"):
    if required not in hiddenimports:
        raise SystemExit(f"打包模块收集失败：缺少 {required}，检查源码路径")
icon = ROOT / "packaging" / "autoslice.ico"

analysis = Analysis(
    [str(ROOT / "scripts" / "desktop_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["torch", "torchaudio", "funasr", "modelscope", "flask", "tkinter", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="AutoSlice",
    console=False,
    icon=str(icon) if icon.is_file() else None,
)
coll = COLLECT(exe, analysis.binaries, analysis.datas, name="AutoSlice")
