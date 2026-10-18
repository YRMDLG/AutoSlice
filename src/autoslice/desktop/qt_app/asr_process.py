"""在子进程里跑识别相关命令：识别任务、环境自检。界面线程只收事件，不阻塞。

用 subprocess 而不是 QProcess：Windows 上从界面程序起 python.exe 会弹出黑色控制台窗口
（用户一关就把识别杀了），PySide6 没有暴露 CREATE_NO_WINDOW，subprocess 可以。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from autoslice.desktop.asr.jobs import JobSpec, parse_event, worker_command

_TAIL_LINES = 12
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class ModuleProcess(QObject):
    """启动 `python -u -m 模块 ...`，逐行读输出。finished(退出码, 最后几行输出, 是否被取消)。"""

    line = Signal(str)
    finished = Signal(int, str, bool)
    # 读线程 → 界面线程：进程结束（由绑定在本对象上的槽接收，自动排队到界面线程）
    _ended = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._process: subprocess.Popen | None = None
        self._tail: list[str] = []
        self._cancelled = False
        self._ended.connect(self._finish)

    @property
    def running(self) -> bool:
        return self._process is not None

    def start(self, program: str, arguments: list[str], environment: dict[str, str]) -> None:
        if self._process is not None:
            raise RuntimeError("已有任务在运行")
        self._tail, self._cancelled = [], False
        try:
            process = subprocess.Popen(
                [program, *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**os.environ, **environment}, creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            self._tail = [f"没法启动 {program}：{exc}（检查设置里“识别用的 Python”）"]
            self._process = None
            QTimer.singleShot(0, lambda: self.finished.emit(-1, "\n".join(self._tail), False))
            return
        self._process = process
        threading.Thread(target=self._pump, args=(process,), daemon=True).start()

    def cancel(self) -> None:
        """直接结束进程（模型推理中途没法温和停）；检查点已经落盘，下次接着跑。"""

        self._cancelled = True
        if self._process is not None:
            try:
                self._process.kill()
            except OSError:
                pass

    def _pump(self, process: subprocess.Popen) -> None:
        """读线程：只读输出、发信号，不碰界面对象。"""

        try:
            for raw in process.stdout:
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not text.strip():
                    continue
                if parse_event(text) is None:
                    self._tail = [*self._tail, text][-_TAIL_LINES:]
                self.line.emit(text)
        except (OSError, ValueError):
            pass
        finally:
            code = process.wait()
            try:
                self._ended.emit(code)
            except RuntimeError:
                pass  # 窗口已销毁，没有接收方

    def _finish(self, code: int) -> None:
        self._process = None
        self.finished.emit(code, "\n".join(self._tail), self._cancelled)


class RecognizeJob(QObject):
    """一次识别任务：progress(消息, 0~1)，finished(状态, 内容)。状态是 done / error / cancelled。"""

    progress = Signal(str, float)
    finished = Signal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._runner = ModuleProcess(self)
        self._runner.line.connect(self._on_line)
        self._runner.finished.connect(self._on_finished)
        self._result: dict | None = None

    @property
    def running(self) -> bool:
        return self._runner.running

    def start(self, spec: JobSpec, spec_path: Path, python: str = "") -> None:
        spec.write(spec_path)
        program, arguments, environment = worker_command(spec_path, python)
        self._result = None
        self._runner.start(program, arguments, environment)

    def cancel(self) -> None:
        self._runner.cancel()

    def _on_line(self, text: str) -> None:
        event = parse_event(text)
        if event is None:
            return
        if event["type"] == "progress":
            self.progress.emit(str(event.get("message", "")), float(event.get("fraction", 0.0)))
        elif event["type"] in ("done", "error", "cancelled"):
            self._result = event

    def _on_finished(self, code: int, tail: str, cancelled: bool) -> None:
        if cancelled:
            self.finished.emit("cancelled", {})
        elif self._result is not None and self._result["type"] == "done":
            self.finished.emit("done", self._result)
        elif self._result is not None and self._result["type"] == "error":
            self.finished.emit("error", {"message": str(self._result.get("message", "识别失败"))})
        elif self._result is not None:
            self.finished.emit("cancelled", {})
        else:
            # 没收到任何结论就退出了：多半是缺库、显存或驱动问题，把最后几行输出给用户看
            detail = tail.strip().splitlines()[-1] if tail.strip() else f"退出码 {code}"
            self.finished.emit("error", {"message": f"识别进程意外退出：{detail}", "tail": tail})
