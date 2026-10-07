"""桌面端后台任务：耗时动作在线程池里跑，结果回到界面线程。"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal


class JobSignals(QObject):
    finished = Signal(object, object)


class BackgroundJob(QRunnable):
    """动作在工作线程执行；结果或异常经信号回到界面线程。发起方已销毁时静默丢弃。"""

    def __init__(self, action):
        super().__init__()
        self.action = action
        self.signals = JobSignals()

    def run(self):
        try:
            result, error = self.action(), None
        except Exception as exc:  # noqa: BLE001 - 任务异常须回到界面显示，不能让工作线程消失
            result, error = None, exc
        try:
            self.signals.finished.emit(result, error)
        except RuntimeError:
            # 发起任务的页面或窗口已经关闭，没有接收方，结果直接丢弃。
            pass
