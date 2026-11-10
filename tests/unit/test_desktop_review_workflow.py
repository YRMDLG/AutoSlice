"""字幕质检、局部复查与跨页任务的完整交互回归。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.subtitle_workflow import suggest_subtitle_corrections
from tests.unit import test_desktop_ai_qt as ai_fixture


class ScopedReviewTests(unittest.TestCase):
    def test_scoped_review_keeps_context_and_does_not_reuse_a_partial_cache_as_full(self):
        calls = []
        contexts = []
        def runner(prompt, _compact):
            indices = json.loads(prompt.split("待检查序号：", 1)[1].split("\n", 1)[0])
            calls.append(indices)
            contexts.append(json.loads(prompt.split("字幕上下文：", 1)[1]))
            return {"reviewed_indices": indices, "corrections": []}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "长字幕.srt"
            source.write_text("\n\n".join(
                f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n第{index}条字幕"
                for index in range(1, 36)
            ), encoding="utf-8")
            result = suggest_subtitle_corrections(source, llm_runner=runner, review_indices={31})
            self.assertEqual(calls, [[31]])
            self.assertEqual([cue["index"] for cue in contexts[0]], list(range(28, 35)))
            self.assertEqual(result["review_indices"], [31])
            suggest_subtitle_corrections(source, llm_runner=runner, review_indices={31})
            self.assertEqual(len(calls), 1)
            suggest_subtitle_corrections(source, llm_runner=runner)
            self.assertEqual(sorted(index for batch in calls[1:] for index in batch), list(range(1, 36)))


@unittest.skipIf(ai_fixture.QApplication is None, "PySide6 不可用")
class ReviewWorkflowQtTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ai_fixture.DesktopAIQtTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.window = self.fixture.window

    def wait_for(self, condition):
        self.fixture.wait_for(condition)

    def test_quality_list_locates_removes_duplicate_and_undo_restores_it(self):
        document = self.window.document
        document.edit_text(1, "今天的内容非常有趣选")
        document.edit_text(2, "选秀节目真的很好看")
        document.change_time(1, 1, 3)
        self.window._edited()
        self.window.quality_button.click()
        self.assertEqual(self.window.quality_list.count(), 1)
        self.assertTrue(self.window.quality_fix.isEnabled())
        self.window.quality_locate.click()
        self.assertEqual(self.window.selected_index, 1)
        self.window.quality_fix.click()
        self.assertEqual(document.entries[0].text, "今天的内容非常有趣")
        self.assertEqual(self.window.quality_list.count(), 0)
        self.assertFalse(document.corrected_path.exists())
        self.window._undo()
        self.assertEqual(document.entries[0].text, "今天的内容非常有趣选")
        self.assertEqual(self.window.quality_list.count(), 1)

    def test_partial_recheck_preserves_other_processed_suggestions(self):
        from PySide6.QtWidgets import QMessageBox

        self.window._start_ai_check()
        self.wait_for(lambda: not self.window._ai_running and self.window.ai_session is not None)
        first = self.window.ai_session.pending[0]
        self.window._select_ai(first.suggestion_id)
        self.window._skip_ai()
        self.window.document.edit_text(2, "新错字")
        self.window._edited()
        self.window.selection.choose(2, self.window._cue_order())
        calls = []
        def checker(path, **kwargs):
            calls.append(kwargs["review_indices"])
            return {"suggestions": [{"index": 2, "original": "新错字", "corrected": "新正字"}]}
        self.window.ai_service.checker = checker
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window._start_ai_check("selected")
            self.wait_for(lambda: not self.window._ai_running)
        self.assertEqual(calls, [[2]])
        self.assertEqual(self.window.ai_session.find(first.suggestion_id).status, "skipped")
        self.assertEqual(self.window.ai_session.pending[0].suggested_text, "新正字")
        self.window._start_ai_check("changed")
        self.assertEqual(calls, [[2]])
        self.assertIn("没有新的", self.window.ai_note.text())

    def test_task_panel_keeps_ai_result_on_another_page_and_returns_to_video(self):
        self.window._select_page(1)
        self.window._start_ai_check()
        self.window.tasks_button.click()
        self.wait_for(lambda: not self.window._ai_running)
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.assertEqual(self.window.task_list.count(), 1)
        self.assertIn("已完成", self.window.task_list.item(0).text())
        self.window.task_locate.click()
        self.assertEqual(self.window.pages.currentIndex(), 0)
        stopped = []
        self.window._task_update("test-render", kind="字幕压制", project=self.window.project,
                                 video_path=self.window.document.video.path, cancel=lambda: stopped.append(True))
        self.window.task_list.setCurrentRow(0)
        self.window.task_cancel.click()
        self.assertEqual(stopped, [True])

    def test_local_check_restores_without_ai_config_and_keeps_skip_progress(self):
        from autoslice.desktop.ai_review import Suggestion

        self.window.ai_service.config_loader = lambda: (_ for _ in ()).throw(ValueError("未配置 AI"))
        found = [Suggestion("local-only", 1, "错字一", "正字一", "本地建议", None, source="homophone")]
        with patch.object(self.window.ai_service, "local_suggestions", return_value=found):
            self.window._start_local_check()
        self.assertTrue(self.window.ai_session.local_only)
        self.window._skip_ai()
        restored, status = self.window.ai_service.load(self.window.document, self.window.project.title)
        self.assertEqual(status, "ready")
        self.assertTrue(restored.local_only)
        self.assertEqual(restored.suggestions[0].status, "skipped")

    def test_task_navigation_opens_the_requested_video_in_a_multi_video_project(self):
        folder = self.fixture.root / "多视频项目"
        folder.mkdir()
        for name in ("前一段", "后一段"):
            (folder / f"{name}.mp4").touch()
            (folder / f"{name}.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n测试字幕\n", encoding="utf-8")
        self.window.refresh()
        self.wait_for(lambda: not self.window._jobs and any(
            project.title == "多视频项目" for project in self.window.service.snapshot.projects))
        project = next(p for p in self.window.service.snapshot.projects if p.title == "多视频项目")
        target = project.videos[-1]
        self.window._task_update("target", kind="AI 校对", project=project,
                                 video_path=target.path, state="done", message="检查完成")
        self.window.tasks_button.click()
        self.window.task_locate.click()
        self.wait_for(lambda: self.window.document is not None and not self.window._jobs
                      and not self.window.cover_editor._jobs)
        self.assertEqual(self.window.document.video.path, target.path)


if __name__ == "__main__":
    unittest.main()
