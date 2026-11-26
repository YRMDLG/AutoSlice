"""字幕压制样式（D-048）：默认那套不能改、自建几套可切换、面板边改边刷新预览、压制用当前那套。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None

from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.subtitle_styles import DEFAULT_ID, SubtitleStyleStore
from autoslice.subtitle_workflow import DEFAULT_SUBTITLE_STYLE


class StyleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = DesktopStorage(Path(self.temp.name))

    def test_default_is_the_original_config_and_cannot_change(self):
        store = SubtitleStyleStore(self.storage)
        self.assertEqual(store.active().id, DEFAULT_ID)
        self.assertEqual(store.active().style, DEFAULT_SUBTITLE_STYLE)
        with self.assertRaises(ValueError):
            store.update(DEFAULT_ID, {"font_size": 12})
        with self.assertRaises(ValueError):
            store.delete(DEFAULT_ID)

    def test_create_switch_rename_delete_persist(self):
        store = SubtitleStyleStore(self.storage)
        mine = store.create(store.suggest_name(), {**DEFAULT_SUBTITLE_STYLE, "font_size": 16})
        self.assertEqual(mine.name, "我的样式")
        self.assertEqual(store.active().id, mine.id)
        store.update(mine.id, {**mine.style, "font_color": "#FFEE00", "y": -700})
        renamed = store.rename(mine.id, "游戏实况")
        second = store.create("游戏实况", DEFAULT_SUBTITLE_STYLE)
        self.assertEqual(second.name, "游戏实况 2")
        store.set_active(renamed.id)
        # 重新打开：几套都在，当前用的那套也记住了
        reopened = SubtitleStyleStore(self.storage)
        self.assertEqual([item.name for item in reopened.presets()], ["默认", "游戏实况", "游戏实况 2"])
        self.assertEqual(reopened.active().id, mine.id)
        self.assertEqual((reopened.active().style["font_color"], reopened.active().style["y"]), ("ffee00", -700.0))
        # 删掉正在用的就回到默认
        self.assertEqual(reopened.delete(mine.id).id, DEFAULT_ID)
        self.assertEqual(SubtitleStyleStore(self.storage).active().id, DEFAULT_ID)

    def test_broken_entries_are_skipped_not_fatal(self):
        self.storage.root.mkdir(parents=True, exist_ok=True)
        (self.storage.root / "subtitle_styles.json").write_text(json.dumps({
            "schema_version": 1, "active": "bad",
            "presets": [
                {"id": "ok", "name": "好的", "style": {"font_size": 18}},
                {"id": "bad", "name": "坏的", "style": {"font_size": 999}},
                {"id": "ok", "name": "重复", "style": {}},
                "不是对象",
            ],
        }), encoding="utf-8")
        store = SubtitleStyleStore(self.storage)
        self.assertEqual([item.id for item in store.presets()], [DEFAULT_ID, "ok"])
        self.assertEqual(store.active().id, DEFAULT_ID)
        (self.storage.root / "subtitle_styles.json").write_text("{坏掉的", encoding="utf-8")
        self.assertEqual([item.id for item in SubtitleStyleStore(self.storage).presets()], [DEFAULT_ID])


SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\n第一句\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\n第二句比较长一些\n\n"
    "3\n00:00:05,000 --> 00:00:06,000\n第三句\n"
)


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class StylePanelWindowTests(unittest.TestCase):
    def setUp(self):
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
        # 字体检查要跑 FFmpeg：测试里直接说“装了”
        font_patch = patch("autoslice.desktop.qt_app.window_style.subtitle_font_problem", return_value="")
        font_patch.start()
        self.addCleanup(font_patch.stop)
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

    def test_panel_replaces_right_column_and_lands_on_a_cue(self):
        window = self.window
        window._seek_to(2.5)  # 两句之间：看不到字幕
        window.style_toggle.click()
        self.assertIs(window.side_stack.currentWidget(), window.style_card)
        self.assertTrue(window.style_toggle.isChecked())
        # 停到选中句（没选就第一句）中间，预览上才有字
        self.assertAlmostEqual(window._player_position, 1.5, delta=0.01)
        window.style_panel.next_button.click()
        self.assertAlmostEqual(window._player_position, 3.5, delta=0.01)
        window.style_panel.done_button.click()
        self.assertIs(window.side_stack.currentWidget(), window.side_splitter)
        self.assertFalse(window.style_toggle.isChecked())

    def test_default_is_locked_new_copy_is_editable_and_drives_preview(self):
        window = self.window
        panel = window.style_panel
        window._toggle_style_panel(True)
        self.assertFalse(panel.numbers["font_size"].isEnabled())
        self.assertTrue(panel.default_note.isVisibleTo(panel))
        panel.new_button.click()
        self.assertTrue(panel.numbers["font_size"].isEnabled())
        self.assertNotEqual(window.style_store.active().id, DEFAULT_ID)
        # 改字号：预览按新样式重新生成（没有播放器时预览不挂，但生效样式立即变）
        panel.numbers["font_size"].spin.setValue(14.0)
        # 用户实机反馈“我要实时变化”：原来预览读的是存盘值，存盘有 400ms 缓冲，预览总慢一拍
        self.assertEqual(window._subtitle_style()["font_size"], 14.0)
        self.assertEqual(window.style_store.active().style["font_size"], 20.0)
        with patch.object(window.preview_service, "render", return_value=Path("x.ass")) as render:
            window.player = SimpleNamespace(set_subtitle=lambda path: None)
            window.preview_toggle.setChecked(True)
            window._refresh_subtitle_preview()
            self.assertEqual(render.call_args.kwargs["style"]["font_size"], 14.0)
            # 改颜色同理：不等存盘
            panel._change("outline_color", "ffffff")
            window._refresh_subtitle_preview()
            self.assertEqual(render.call_args.kwargs["style"]["outline_color"], "ffffff")
        window.player = None
        panel.flush()
        self.assertEqual(window.style_store.active().style["font_size"], 14.0)
        # 切回默认：生效样式回到原来的配置
        panel.preset_combo.setCurrentIndex(0)
        panel._choose_preset(0)
        self.assertEqual(window._subtitle_style(), DEFAULT_SUBTITLE_STYLE)
        self.assertIn("默认", window.render_button.toolTip())

    def test_delete_asks_and_falls_back_to_default(self):
        window = self.window
        panel = window.style_panel
        panel.new_button.click()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            panel.delete_button.click()
        self.assertNotEqual(window.style_store.active().id, DEFAULT_ID)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            panel.delete_button.click()
        self.assertEqual(window.style_store.active().id, DEFAULT_ID)
        self.assertEqual(panel.preset_combo.count(), 1)

    def test_settings_shortcut_opens_panel_on_subtitle_page(self):
        window = self.window
        window._select_page(2)
        self.assertIn("当前样式：默认", window.render_style_note.text())
        window._open_style_from_settings()
        self.assertEqual(window.pages.currentIndex(), 0)
        self.assertTrue(window._style_panel_open())

    # ── 视频上的字幕选框（D-049）──
    def test_video_box_follows_panel_and_writes_back(self):
        window = self.window
        panel = window.style_panel
        # 没有播放器（缺 libmpv）时不出选框
        window._toggle_style_panel(True)
        self.assertIsNone(window.style_overlay_state())
        window.player = SimpleNamespace(set_subtitle=lambda path: None, seek=lambda *a, **k: None,
                                        clear_subtitle=lambda: None)
        window._media_ready = True
        window._player_duration = 10.0
        self.addCleanup(setattr, window, "player", None)
        with patch.object(window.preview_service, "canvas_size", return_value=(1920, 1080)):
            window._seek_to(1.5)
            state = window.style_overlay_state()
            self.assertEqual((state["text"], state["video_size"], state["editable"]), ("第一句", (1920, 1080), False))
            panel.new_button.click()
            self.assertTrue(window.style_overlay_state()["editable"])
            # 选框拖动发出的剪映坐标直接写进面板：滑块、数字和预览用的样式一起变
            window.style_overlay.changed.emit({"x": 120.4, "y": -500.2})
            self.assertEqual((panel.numbers["x"].value(), panel.numbers["y"].value()), (120.0, -500.0))
            self.assertEqual(window.style_overlay_state()["style"]["y"], -500.0)
            # 收起面板或去别的页：选框不出
            window._toggle_style_panel(False)
            self.assertIsNone(window.style_overlay_state())


if __name__ == "__main__":
    unittest.main()
