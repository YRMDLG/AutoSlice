"""桌面端后台任务：结果与异常回到界面线程，发起方销毁后静默丢弃。"""

from __future__ import annotations

import unittest

try:
    import shiboken6
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None

if QApplication is not None:
    from autoslice.desktop.jobs import BackgroundJob


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class BackgroundJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_result_and_error_reach_callback(self):
        received = []
        ok = BackgroundJob(lambda: 42)
        ok.signals.finished.connect(lambda result, error: received.append((result, error)))
        ok.run()

        def broken():
            raise ValueError("坏了")

        failing = BackgroundJob(broken)
        failing.signals.finished.connect(lambda result, error: received.append((result, error)))
        failing.run()
        self.assertEqual(received[0], (42, None))
        self.assertIsNone(received[1][0])
        self.assertIsInstance(received[1][1], ValueError)

    def test_finished_after_owner_deleted_is_dropped_silently(self):
        job = BackgroundJob(lambda: "迟到的结果")
        shiboken6.delete(job.signals)
        job.run()


if __name__ == "__main__":
    unittest.main()
