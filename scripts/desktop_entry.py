"""安装版入口。

    AutoSlice.exe                  正常启动桌面端
    AutoSlice.exe --smoke 结果.txt  打包后的自检：关键库、资源、识别子进程用的源码是否都带上了（不打开窗口）
"""

from __future__ import annotations

import sys
from pathlib import Path

# 源码运行本脚本时（调试用）把 src 放进路径；打包后 autoslice 已在包里
_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def smoke(output: str) -> int:
    lines: list[str] = []
    failed = False

    def check(name: str, probe) -> None:
        nonlocal failed
        try:
            lines.append(f"ok    {name}: {probe()}")
        except Exception as exc:  # noqa: BLE001 - 自检要把每一项失败原因都记下来
            failed = True
            lines.append(f"FAIL  {name}: {type(exc).__name__}: {exc}")

    def resource(relative: str) -> str:
        import autoslice

        path = Path(autoslice.__file__).resolve().parent / relative
        if not path.exists():
            raise FileNotFoundError(path)
        return str(path)

    check("PySide6", lambda: __import__("PySide6").__version__)
    check("requests", lambda: __import__("requests").__version__)
    check("Pillow", lambda: __import__("PIL").__version__)
    check("pypinyin(同音检查，可选)", lambda: __import__("pypinyin").__version__)
    check("界面字体", lambda: resource("resources/fonts/ResourceHanRoundedCN-Regular.ttf"))
    check("主播配置", lambda: resource("streamer_profiles.json"))
    check("窗口类", lambda: __import__("autoslice.desktop.qt_app.window", fromlist=["DesktopWindow"]).DesktopWindow.__name__)
    check("识别模块", lambda: __import__("autoslice.desktop.asr.worker", fromlist=["main"]).main.__name__)
    if getattr(sys, "frozen", False):
        from autoslice.desktop.asr.jobs import source_root

        check("识别子进程用的源码", lambda: str(next(Path(source_root()).glob("autoslice/desktop/asr/worker.py"))))
    for optional in ("cv2", "numpy", "onnxruntime"):
        try:
            module = __import__(optional)
            lines.append(f"ok    {optional}(抠图用，可选): {getattr(module, '__version__', '')}")
        except ImportError:
            lines.append(f"skip  {optional}(抠图用，可选): 没带，抠图会不可用或退回普通模式")
    Path(output).write_text("\n".join(lines) + ("\nSMOKE FAILED\n" if failed else "\nSMOKE OK\n"), encoding="utf-8")
    return 1 if failed else 0


def main() -> int:
    if "--smoke" in sys.argv:
        index = sys.argv.index("--smoke")
        return smoke(sys.argv[index + 1] if index + 1 < len(sys.argv) else "smoke.txt")
    from autoslice.desktop.qt_app.__main__ import main as run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
