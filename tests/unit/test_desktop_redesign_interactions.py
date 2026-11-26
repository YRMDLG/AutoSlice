"""新界面（D-042）加的字幕页交互：查找替换、空格从选中句开头播放、右键补项、带撤销的提示条、行内建议。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None

SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\n先别急 ABC\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\n好abc，现在看清楚了\n\n"
    "3\n00:00:05,000 --> 00:00:06,000\n这个地图练过一编\n\n"
    "4\n00:00:07,000 --> 00:00:08,000\n最后一句\n"
)


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class RedesignInteractionTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.foundation import DesktopStorage
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        folder = root / "示例项目"
        folder.mkdir()
        (folder / "视频.mp4").touch()
        (folder / "视频.srt").write_text(SRT, encoding="utf-8")
        self.app = QApplication.instance() or QApplication([])
        with patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("测试无播放器")):
            self.window = DesktopWindow(SubmissionProjectService(root), DesktopStorage(root / "app-data"))
        self.window.resize(1440, 900)
        self.window.show()

        def close_window():
            self.wait_for(lambda: not self.window._jobs and not self.window.cover_editor._jobs)
            self.window._resolve_unsaved = lambda: True
            self.window.close()

        self.addCleanup(close_window)
        self.wait_for(lambda: bool(self.window.project_buttons))
        project = next(item for item in self.window.service.snapshot.projects if item.title == "示例项目")
        self.window._select_real_project(project)
        self.wait_for(lambda: self.window.document is not None)

    def wait_for(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.01)
        self.fail("等待界面状态完成超时")

    def texts(self):
        return [entry.text for entry in self.window.document.entries]

    # ── 查找替换 ──
    def test_find_is_case_insensitive_by_default_and_jumps_to_hit(self):
        window = self.window
        window._open_find()
        self.assertTrue(window.find_bar.isVisibleTo(window))
        window.find_input.setText("abc")
        self.assertEqual(len(window._find_hits), 2)
        self.assertEqual(window.find_count.text(), "1 / 2")
        self.assertEqual(window.selected_index, 1)
        window._find_step(1)
        self.assertEqual(window.selected_index, 2)
        window.find_case.setChecked(True)
        self.assertEqual(window._find_hits, [(2, 1)])

    def test_replace_all_is_one_undo_step_and_only_touches_the_document(self):
        window = self.window
        window._open_find()
        window.find_input.setText("abc")
        window.replace_input.setText("嗯")
        window._replace_all()
        self.assertEqual(self.texts()[:2], ["先别急 嗯", "好嗯，现在看清楚了"])
        self.assertTrue(window.document.dirty)
        self.assertFalse(window.find_bar.isHidden())
        self.assertTrue(window.toast.isVisible())
        self.assertIn("替换 2 处", window.toast.message.text())
        window._undo()
        self.assertEqual(self.texts()[:2], ["先别急 ABC", "好abc，现在看清楚了"])

    def test_replace_one_moves_to_next_hit(self):
        window = self.window
        window._open_find()
        window.find_input.setText("abc")
        window.replace_input.setText("x")
        window._replace_one()
        self.assertEqual(self.texts()[0], "先别急 x")
        self.assertEqual(window._find_hits, [(2, 1)])
        self.assertEqual(window.selected_index, 2)

    def test_find_only_in_pending_uses_ai_queue(self):
        from autoslice.desktop.ai_review import AIReviewSession, Suggestion, document_hash

        window = self.window
        content = document_hash(window.document.entries)
        window.ai_session = AIReviewSession([
            Suggestion("s3", 3, "这个地图练过一编", "这个地图练过一遍", "错字", None),
        ], content[:20], content)
        window._render_ai()
        window._open_find()
        window.find_pending_only.setChecked(True)
        window.find_input.setText("一")
        self.assertEqual({cue for cue, _ in window._find_hits}, {3})

    def test_escape_in_find_box_closes_it(self):
        window = self.window
        window._open_find()
        QTest.keyClick(window.find_input, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertTrue(window.find_bar.isHidden())
        self.assertFalse(window.find_toggle.isChecked())

    # ── 空格 ──
    def test_space_plays_from_selected_cue_start_when_paused(self):
        window = self.window
        player = window.player = Mock()
        window._media_ready = True
        window._player_duration = 30.0
        window._player_paused = True
        window._player_position = 12.0
        window.selection.choose(3, window._cue_order())
        window.selected_index = 3
        window._space_play()
        player.seek.assert_called_with(5.0, pause=True)
        player.play.assert_called_once_with()
        window._space_play()
        player.pause.assert_called_once_with()

    def test_space_without_selection_just_toggles(self):
        window = self.window
        player = window.player = Mock()
        window._media_ready = True
        window._player_duration = 30.0
        window._player_paused = True
        window.selected_index = None
        window._space_play()
        player.seek.assert_not_called()
        player.play.assert_called_once_with()

    # ── 右键补项与提示条 ──
    def test_merge_with_next_and_toast_undo_restores(self):
        window = self.window
        window._merge_with_next(1)
        self.assertEqual(len(window.document.entries), 3)
        self.assertTrue(window.toast.isVisible())
        self.assertFalse(window.toast.undo_button.isHidden())
        window.toast.undo_button.click()
        self.assertEqual(len(window.document.entries), 4)

    def test_delete_shows_undoable_toast(self):
        window = self.window
        window.selection.choose(4, window._cue_order())
        window.selected_index = 4
        window._delete()
        self.assertEqual(len(window.document.entries), 3)
        self.assertIn("已删除第 4 条", window.toast.message.text())
        window.toast.undo_button.click()
        self.assertEqual(len(window.document.entries), 4)

    def test_text_edit_toast_undo_reverts_whole_edit(self):
        window = self.window
        window._edit_cue_text(2)
        editor = window._active_subtitle_editor()
        self.assertIsNotNone(editor)
        editor.insertPlainText("嗯嗯")
        window._commit_editor()
        self.assertIn("嗯嗯", self.texts()[1])
        self.assertIn("已修改第 2 条", window.toast.message.text())
        window.toast.undo_button.click()
        self.assertEqual(self.texts()[1], "好abc，现在看清楚了")

    # ── 行内建议 ──
    def test_inline_suggestion_sits_under_its_row_and_collapses_while_editing(self):
        from autoslice.desktop.ai_review import AIReviewSession, Suggestion, document_hash
        from autoslice.desktop.qt_preview.theme import SIZES

        window = self.window
        content = document_hash(window.document.entries)
        window.ai_session = AIReviewSession([
            Suggestion("s3", 3, "这个地图练过一编", "这个地图练过一遍", "错字", None),
        ], content[:20], content)
        window._render_ai()
        window._select_ai("s3")
        self.app.processEvents()
        row = window._row_of_cue(3)
        header = window.table.verticalHeader()
        self.assertGreater(header.sectionSize(row), SIZES.subtitle_row_height)
        self.assertIs(window.ai_card.parentWidget(), window.table.viewport())
        top = window.table.visualRect(window.model.index(row, 0)).top()
        self.assertEqual(window.ai_card.geometry().top(), top + SIZES.subtitle_row_height)
        self.assertEqual(window.model.tag_for(3), "待确认")
        # 改字时建议收起，这一行换成编辑态的高度（设计稿 edrow，D-045）；改完建议再展开
        from autoslice.desktop.qt_app.window_widgets import EDIT_ROW_HEIGHT

        with_suggestion = header.sectionSize(row)
        window._edit_cue_text(3)
        self.assertEqual(header.sectionSize(row), EDIT_ROW_HEIGHT)
        self.assertFalse(window.ai_card.isVisible() and window._ai_inline_row == row)
        window._commit_editor()
        self.assertEqual(header.sectionSize(row), with_suggestion)

    # ── 实际操作走查第四轮（D-046）──
    def test_edit_menu_submenus_keep_their_effects_and_menu_is_freed(self):
        # 原来用 menu.addMenu("标题")：返回的包装一创建就失效，子菜单一弹就报“C++ 对象已删除”
        import shiboken6
        from PySide6.QtCore import QEvent, QPoint
        from PySide6.QtWidgets import QMenu

        from autoslice.desktop.qt_preview import popups

        window = self.window
        opened = []
        with patch.object(popups.MenuFx, "popup", lambda fx, _position: opened.append(fx.menu)):
            window._show_edit_menu(QPoint(10, 10))
        menu = opened[0]
        subs = [child for child in menu.children() if isinstance(child, QMenu)]
        self.assertEqual({sub.title() for sub in subs}, {"裁剪与贴合", "试听"})
        for sub in subs:
            fx = next(child for child in sub.children() if isinstance(child, popups.MenuFx))
            self.assertTrue(shiboken6.isValid(fx.menu))
            self.assertIs(fx.menu, sub)
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(shiboken6.isValid(menu), "菜单关掉后应删除，不随右键次数累积")

    def test_cover_warning_goes_to_toast_without_pushing_the_page(self):
        window = self.window
        editor = window.cover_editor
        window.app_status.setText("")
        editor.status_changed.emit("AI 正在保留你的文字、检查画面并优化排版…")
        self.assertIn("AI 正在", window.app_status.text())
        editor._set_notice("还没配置 AI 接口：到设置页填写地址、密钥和模型", "error")
        # 文字照记（诊断和测试读它），但不占布局；改由顶部提示条弹出，顶栏上过时的进度撤掉
        self.assertIn("设置页", editor.notice_label.text())
        self.assertFalse(editor.notice_label.isVisible())
        self.assertTrue(window.toast.isVisible())
        self.assertIn("设置页", window.toast.message.text())
        self.assertEqual(window.app_status.text(), "")
        # 一行放得下就不折行
        metrics = window.toast.message.fontMetrics()
        self.assertLess(window.toast.message.height(), metrics.lineSpacing() * 2)
        # 同一句再走状态栏时不重复
        editor.status_changed.emit("还没配置 AI 接口：到设置页填写地址、密钥和模型")
        self.assertEqual(window.app_status.text(), "")

    def test_ime_cursor_rect_points_at_the_caret(self):
        # QPlainTextEdit 原样回答的是视口坐标，Qt 按整个输入框换算：候选窗偏左上一个内边距（10, 8）
        from PySide6.QtCore import QPoint

        window = self.window
        window._edit_cue_text(2)
        editor = window._active_subtitle_editor()
        self.assertNotEqual(editor.viewport().pos(), QPoint(0, 0))
        rect = editor.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle)
        caret = editor.viewport().mapTo(editor, editor.cursorRect().topLeft())
        self.assertEqual(rect.toRect().topLeft(), caret)

    # ── 右键在定位线新增字幕（D-048）──
    def test_insert_at_playhead_from_menu_opens_editor_and_undoes(self):
        from PySide6.QtCore import QPoint

        from autoslice.desktop.qt_preview import popups

        window = self.window
        menus = []

        def open_menu():
            with patch.object(popups.MenuFx, "popup", lambda fx, _position: menus.append(fx.menu)):
                window._show_edit_menu(QPoint(10, 10))
            return next(action for action in menus[-1].actions() if action.text() == "在定位线新增字幕")

        # 定位线在第 1 句里：置灰，悬停说原因
        window._player_position = 1.5
        item = open_menu()
        self.assertFalse(item.isEnabled())
        self.assertIn("第 1 条字幕里", item.toolTip())
        # 两句之间：新增一条 2.2~3.0，选中并直接进入改字
        window._player_position = 2.2
        item = open_menu()
        self.assertTrue(item.isEnabled())
        item.trigger()
        self.app.processEvents()
        added = window.document.entries[1]
        self.assertEqual((added.start, added.end, added.text), ("00:00:02,200", "00:00:03,000", ""))
        self.assertEqual(window.selection.active, added.index)
        self.assertIsNotNone(window._active_subtitle_editor())
        self.assertIn("新增", window.toast.message.text())
        window._commit_editor()
        window.toast.undo_button.click()
        self.assertEqual(len(window.document.entries), 4)

    def test_skipped_draft_explains_in_words_not_status_codes(self):
        from autoslice.desktop.qt_app.window_projects import _DRAFT_SKIPPED

        for status in ("source_changed", "source_missing", "incompatible", "invalid"):
            self.assertNotIn(status, _DRAFT_SKIPPED[status])
            self.assertIn("草稿", _DRAFT_SKIPPED[status])


if __name__ == "__main__":
    unittest.main()
