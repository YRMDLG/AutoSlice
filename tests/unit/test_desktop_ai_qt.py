"""Qt AI 侧栏的 fake 后台任务与 Desktop-06.5 状态回归。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class DesktopAIQtTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.ai_review import AIReviewService
        from autoslice.desktop.foundation import DesktopStorage
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        folder = self.root / "中文项目"
        folder.mkdir()
        (folder / "视频.mp4").touch()
        (folder / "视频.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n错字一\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n错字二\n", encoding="utf-8"
        )
        self.app = QApplication.instance() or QApplication([])
        self.calls = []
        config = SimpleNamespace(base_url="https://example.invalid", api_type="openai",
                                 model="existing-model", review_reasoning_effort="high")
        profile = SimpleNamespace(id="test", label="测试",
                                  subtitle_review_fingerprint=lambda: "rules-v1")
        def checker(path, **_kwargs):
            self.calls.append(str(path))
            time.sleep(.05)
            return {"suggestions": [
                {"index": 1, "original": "错字一", "corrected": "正字一", "reason": "错别字", "confidence": .9},
                {"index": 2, "original": "错字二", "corrected": "正字二", "reason": "错别字", "confidence": .8},
            ]}
        storage = DesktopStorage(self.root / "app-data")
        ai_service = AIReviewService(storage, checker=checker, config_loader=lambda: config)
        self.storage = storage
        self.ai_service = ai_service
        profile_patch = patch("autoslice.desktop.ai_review.resolve_streamer_profile", return_value=profile)
        profile_patch.start()
        self.addCleanup(profile_patch.stop)
        with patch("autoslice.desktop.qt_app.window.AIReviewService", return_value=ai_service), \
             patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("测试无播放器")):
            self.window = DesktopWindow(SubmissionProjectService(self.root), storage)
        self.window.show()
        def close_window():
            self.window._resolve_unsaved = lambda: True
            self.window.close()
        self.addCleanup(close_window)
        self.wait_for(lambda: bool(self.window.project_buttons))
        project = next(p for p in self.window.service.snapshot.projects if p.title == "中文项目")
        self.window._select_real_project(project)
        self.wait_for(lambda: self.window.document is not None)

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail(f"Qt 后台任务未完成：{self.window.scan_status.text()} / {self.window.subtitle_status.text()}")

    def test_background_queue_accept_skip_conflict_and_navigation(self):
        self.window._start_ai_check()
        self.window._select_page(1)
        self.wait_for(lambda: self.window.ai_session is not None)
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.assertTrue(self.window.ai_return.isVisible())
        self.assertTrue(self.window.nav_badge.isVisible())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.window.ai_session.pending), 2)
        self.window._set_filter(True)
        self.assertEqual(self.window.model.rowCount(), 2)
        self.window._set_filter(False)
        self.window._return_to_ai()
        self.assertEqual(self.window.pages.currentIndex(), 0)
        first = self.window.ai_session.pending[0]
        self.window._select_ai(first.suggestion_id)
        self.assertEqual(self.window.selected_index, 1)
        self.assertIn("line-through", self.window.ai_diff.text())
        self.window._accept_ai()
        self.assertEqual(self.window.document.entries[0].text, "正字一")
        self.assertTrue(self.window.document.dirty)
        self.assertEqual(self.window._ai_selected, self.window.ai_session.pending[0].suggestion_id)
        self.window._undo()
        self.assertEqual(self.window.document.entries[0].text, "错字一")
        self.assertEqual(len(self.window.ai_session.pending), 2)
        self.window._redo()
        self.assertEqual(self.window.document.entries[0].text, "正字一")
        second = self.window.ai_session.pending[0]
        self.window.document.edit_text(second.cue_id, "人工修改")
        self.window._edited()
        self.window._select_ai(second.suggestion_id)
        self.assertFalse(self.window.ai_accept.isEnabled())
        self.window._skip_ai()
        self.assertEqual(self.window.document.entries[1].text, "人工修改")
        self.assertEqual(len(self.window.ai_session.pending), 0)
        self.window._undo()
        self.assertEqual(len(self.window.ai_session.pending), 1)
        self.window._redo()
        self.assertEqual(len(self.window.ai_session.pending), 0)
        self.assertEqual(len(self.calls), 1)
        self.window._draft_timer.stop()

    def test_missing_config_and_cancel_leave_editor_available(self):
        self.window.ai_service.config_loader = lambda: (_ for _ in ()).throw(ValueError("未配置 LLM API"))
        self.window._start_ai_check()
        self.wait_for(lambda: not self.window._ai_running)
        self.assertIn("未配置 AI", self.window.ai_note.text())
        self.assertTrue(self.window.table.isEnabled())

    def test_playhead_full_height_blank_scrub_and_cue_priority(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        timeline = self.window.timeline
        timeline.set_duration(24)
        timeline.center = 2.5
        timeline.set_playhead(2.5, playing=False)
        self.app.processEvents()
        selected = self.window.selected_index
        emitted = []
        timeline.seek_requested.connect(emitted.append)
        marker = int(timeline._x(2.5))
        for y in (20, 38, timeline.height() - 2):
            QTest.mouseMove(timeline, QPoint(marker, y))
            self.assertEqual(timeline.cursor().shape(), Qt.CursorShape.SizeHorCursor)
            QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=QPoint(marker, y))
            self.assertIsNotNone(timeline._scrub)
            QTest.mouseMove(timeline, QPoint(marker + 16, y))
            QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton,
                               pos=QPoint(marker + 16, y))
            timeline.set_playhead(2.5, playing=False)
        blank = int(timeline._x(5))
        QTest.mousePress(timeline, Qt.MouseButton.LeftButton,
                         pos=QPoint(blank, timeline.height() - 2))
        self.assertIsNotNone(timeline._scrub)
        QTest.mouseMove(timeline, QPoint(blank + 20, timeline.height() - 2))
        QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton,
                           pos=QPoint(blank + 20, timeline.height() - 2))
        # 空白拖动已经进入矩形框选，不再把释放位置解释为 scrub seek。
        self.assertEqual(len(emitted), 3)
        self.assertEqual(self.window.selected_index, selected)
        self.assertFalse(self.window.document.dirty)
        block = next(item for item in timeline._visible_blocks() if item[0].index == 1)[3]
        for x, mode in ((block.left() + 1, "left"), (block.center().x(), "move"),
                        (block.right() - 1, "right")):
            point = QPoint(int(x), 38)
            QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point)
            self.assertEqual(timeline._drag["mode"], mode)
            self.assertIsNone(timeline._scrub)
            QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point)
        self.window._draft_timer.stop()
        self.assertEqual(self.calls, [])
        self.window.ai_service.config_loader = lambda: SimpleNamespace(
            base_url="https://example.invalid", api_type="openai", model="existing-model",
            review_reasoning_effort="high"
        )
        self.window._start_ai_check()
        self.window._start_ai_check()  # 第二次点击运行中的按钮表示取消。
        self.wait_for(lambda: not self.window._ai_running)
        self.assertIn("取消", self.window.ai_note.text())
        self.assertIsNone(self.window.ai_session)
        self.assertTrue(self.window.table.isEnabled())

    def test_restart_restores_progress_without_checker_call(self):
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        self.window._start_ai_check()
        self.wait_for(lambda: self.window.ai_session is not None)
        first = self.window.ai_session.pending[0]
        self.window._select_ai(first.suggestion_id)
        self.window._skip_ai()
        self.assertEqual(len(self.calls), 1)
        self.window.close()
        with patch("autoslice.desktop.qt_app.window.AIReviewService", return_value=self.ai_service), \
             patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("测试无播放器")):
            reopened = DesktopWindow(SubmissionProjectService(self.root), self.storage)
        reopened.show()
        self.addCleanup(reopened.close)
        self.wait_for(lambda: bool(reopened.project_buttons))
        project = next(p for p in reopened.service.snapshot.projects if p.title == "中文项目")
        reopened._select_real_project(project)
        self.wait_for(lambda: reopened.document is not None)
        self.assertEqual([item.status for item in reopened.ai_session.suggestions], ["skipped", "pending"])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
