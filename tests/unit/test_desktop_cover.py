"""AutoCover-01 桌面服务和 Qt 工作区的最小回归。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QRectF, Qt, QThreadPool
    from PySide6.QtGui import QFont, QFontDatabase, QFontMetrics, QPixmap
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QGroupBox
except ImportError:
    QApplication = None
    QGroupBox = None

from autoslice.desktop.cover_draft import CoverDraft, text_transforms_for, wrap_cover_title
from autoslice.desktop.cover_layout import canvas_size, text_layout
from autoslice.desktop.cover_model import CoverDocument, TextObject, object_for_profile
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject
from autoslice_cover.fonts import resolve_font_selection


def _make_real_video(path: Path) -> None:
    """用本机 FFmpeg 生成可被真实取帧链路读取的短 MP4。"""

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise unittest.SkipTest("本机没有 ffmpeg")
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=10",
            "-t",
            "1.2",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or not path.is_file():
        raise RuntimeError(f"测试视频生成失败：{result.stderr}")


class CoverServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project_dir = root / "投稿项目"
        self.project_dir.mkdir()
        self.video_path = self.project_dir / "成片.mp4"
        self.video_path.write_bytes(b"video")
        self.image_path = root / "底图.png"
        Image.new("RGB", (640, 360), "#23a6a8").save(self.image_path)
        self.project = SubmissionProject(
            "project-1", "投稿项目", str(self.project_dir),
            (ProjectVideo("成片.mp4", str(self.video_path), "", "", False, False, ""),),
        )
        self.video = self.project.videos[0]
        self.storage = DesktopStorage(root / "app-data")
        self.service = CoverService(self.storage)

    def test_draft_roundtrip_stays_in_private_storage(self):
        image = self.service.import_image(self.image_path)
        draft = CoverDraft("标题", str(image), 1.25, 0.2, 0.3, 96)
        path = self.service.save(self.project, self.video, draft)
        loaded, read = self.service.load(self.project, self.video)
        self.assertEqual(read.status, "ready")
        self.assertEqual(loaded.title, "标题")
        self.assertEqual(loaded.image_path, str(image))
        self.assertTrue(path.is_relative_to(self.storage.drafts))
        self.assertTrue(image.is_relative_to(self.storage.thumbnails))
        self.assertFalse((self.project_dir / "封面草稿.json").exists())

    def test_export_increments_without_overwriting_existing_cover(self):
        image = self.service.import_image(self.image_path)
        draft = CoverDraft("导出标题", str(image), font_size=72)
        first = self.service.export(self.project, self.video, draft)
        first_bytes = first.read_bytes()
        second = self.service.export(self.project, self.video, draft)
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertTrue(first.name.startswith("AutoCover-成片"))

    def test_real_mp4_extracts_openable_cached_frame(self):
        _make_real_video(self.video_path)
        frame, timestamp = self.service.extract_frame(self.video, 0.4)
        self.assertTrue(frame.is_file())
        self.assertTrue(frame.is_relative_to(self.storage.thumbnails))
        self.assertAlmostEqual(timestamp, 0.4, places=3)
        with Image.open(frame) as image:
            image.verify()
            self.assertGreater(image.width, 0)
            self.assertGreater(image.height, 0)

    def test_nearby_frames_are_extracted_around_center_without_blocking_ui_contract(self):
        _make_real_video(self.video_path)
        frames = self.service.extract_nearby_frames(
            self.video,
            0.4,
            (-0.4, 0.0, 0.4),
        )
        self.assertEqual(len(frames), 3)
        self.assertEqual([round(item[1], 1) for item in frames], [0.0, 0.4, 0.8])
        self.assertTrue(all(path.is_file() for path, _timestamp in frames))

    def test_background_transform_roundtrip_stays_in_private_storage(self):
        image = self.service.import_image(self.image_path)
        draft = CoverDraft(
            "构图",
            str(image),
            background_x=0.21,
            background_y=0.77,
            background_scale=1.6,
        )
        self.service.save(self.project, self.video, draft)
        loaded, _ = self.service.load(self.project, self.video)
        self.assertAlmostEqual(loaded.background_x, 0.21)
        self.assertAlmostEqual(loaded.background_y, 0.77)
        self.assertAlmostEqual(loaded.background_scale, 1.6)

    def test_legacy_auto_copy_compacts_long_a_b_into_short_b(self):
        source_title = "〖泽音〗音姐今天要给沐霂点男模👀“哎呀我还没见过男模啥样呢😋”那种事情不要啊😭"
        project = replace(self.project, title=source_title)
        document = CoverDraft(
            "音姐今天要给沐霂点男模\n哎呀我还没见过男模啥样呢",
        ).to_document()
        self.service.save_document(project, self.video, document)

        loaded, _ = self.service.load_document(project, self.video)
        text = next(item for item in loaded.objects if isinstance(item, TextObject))
        self.assertEqual(text.text, "哎呀我还没见过男模啥样呢")
        self.assertLess(text.rect.width, 0.8)

    def test_export_matches_preview_for_same_transform_state(self):
        image = self.service.import_image(self.image_path)
        draft = CoverDraft(
            "拖动后的标题",
            str(image),
            text_x=0.29,
            text_y=0.18,
            background_x=0.18,
            background_y=0.82,
            background_scale=1.35,
        )
        preview = self.service.render_preview(self.video, draft)
        exported = self.service.export(self.project, self.video, draft)
        self.assertEqual(preview.read_bytes(), exported.read_bytes())

    def test_document_preview_and_export_share_text_style(self):
        image = self.service.import_image(self.image_path)
        document = CoverDraft("文档样式", str(image), font_size=88).to_document()
        text = next(item for item in document.objects if isinstance(item, TextObject))
        styled = replace(
            text,
            style=replace(
                text.style,
                fill="#00E5FF",
                stroke="#111111",
                stroke_width=8,
                font_weight=900,
            ),
        )
        document = replace(document, objects=tuple(styled if item.id == text.id else item for item in document.objects))
        preview = self.service.render_preview_document(self.video, document)
        exported = self.service.export_document(self.project, self.video, document)
        self.assertEqual(preview.read_bytes(), exported.read_bytes())
        with Image.open(preview) as rendered:
            cyan_pixels = sum(
                1
                for red, green, blue in rendered.convert("RGB").getdata()
                if blue > 180 and green > 130 and red < 100
            )
        self.assertGreater(cyan_pixels, 20)

    def test_document_renderer_uses_resolved_font_path(self):
        image = self.service.import_image(self.image_path)
        document = CoverDraft("中文字体宽度", str(image), font_size=96).to_document()
        text = next(item for item in document.objects if isinstance(item, TextObject))
        resolution = resolve_font_selection(text.style.font_family)
        layout = text_layout(text, canvas_size("4x3"))
        fonts = {run.font_path for line in layout.lines for run in line.runs}
        self.assertEqual(
            {Path(path).resolve() for path in fonts if path},
            {resolution.path.resolve()} if resolution.path else set(),
        )

    def test_preview_and_export_default_to_four_by_three_canvas(self):
        image = self.service.import_image(self.image_path)
        draft = CoverDraft("4:3 主画布", str(image), font_size=72)
        preview = self.service.render_preview(self.video, draft)
        exported = self.service.export(self.project, self.video, draft)
        with Image.open(preview) as preview_image:
            self.assertEqual(preview_image.size, (1440, 1080))
        with Image.open(exported) as exported_image:
            self.assertEqual(exported_image.size, (1440, 1080))

    def test_preview_and_export_can_use_sixteen_by_nine_main_canvas(self):
        image = self.service.import_image(self.image_path)
        draft = CoverDraft("横版主画布", str(image), font_size=72)
        preview = self.service.render_preview(self.video, draft, canvas_key="16x9")
        exported = self.service.export(
            self.project, self.video, draft, canvas_key="16x9"
        )
        with Image.open(preview) as preview_image:
            self.assertEqual(preview_image.size, (1920, 1080))
        with Image.open(exported) as exported_image:
            self.assertEqual(exported_image.size, (1920, 1080))
        self.assertIn("-16x9", exported.name)

    def test_text_position_prefers_quieter_side_of_frame(self):
        image_path = Path(self.temp.name) / "layout.jpg"
        image = Image.new("RGB", (1440, 1080), (180, 180, 180))
        draw = ImageDraw.Draw(image)
        for x in range(0, 700, 14):
            draw.line((x, 0, x, 1080), fill=(20 if x % 28 else 245,) * 3, width=7)
        image.save(image_path)
        x, y = self.service.suggest_text_position(
            image_path,
            CoverDraft("短标题", font_size=96),
        )
        self.assertGreater(x, 0.4)
        self.assertLess(y, 0.5)

    def test_long_multiline_title_is_wrapped_and_transforms_stay_in_bounds(self):
        title = "这是一个很长很长的中文标题，用来验证自动换行不会横穿画布\n第二行 😀"
        lines = wrap_cover_title(title, 104)
        self.assertGreater(len(lines), 2)
        transforms = text_transforms_for(CoverDraft(title, text_y=0.1), lines)
        self.assertEqual(len(lines), len(transforms))
        self.assertTrue(all(0.0 <= transform.x <= 1.0 for transform in transforms))
        self.assertTrue(all(0.0 <= transform.y <= 1.0 for transform in transforms))


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class CoverEditorQtSmokeTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover import CoverEditorWidget
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        project_dir = root / "项目甲"
        project_dir.mkdir()
        video_path = project_dir / "视频.mp4"
        video_path.write_bytes(b"video")
        self.project = SubmissionProject(
            "project-1", "项目甲", str(project_dir),
            (ProjectVideo("视频.mp4", str(video_path), "", "", False, False, ""),),
        )
        self.app = QApplication.instance() or QApplication([])
        self.widget = CoverEditorWidget(DesktopStorage(root / "data"))
        self.addCleanup(self.widget.deleteLater)
        # 单元测试没有事件循环，deleteLater 不会执行；显示过的窗口必须关掉，
        # 否则它会留在屏幕上截走后续 Qt 测试的悬停事件。
        self.addCleanup(self.widget.close)

    def test_page_inherits_current_project_and_exposes_editor_controls(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.assertEqual(self.widget.project_label.text(), "项目甲")
        self.assertEqual(self.widget.video_label.text(), "视频.mp4")
        self.assertEqual(self.widget.title_edit.text(), "项目甲")
        self.assertIn("4:3", self.widget.export_button.text())
        self.assertIn("1440×1080", self.widget.export_summary.text())
        self.assertIs(self.widget.panel_stack.currentWidget(), self.widget.copy_controls)
        self.assertFalse(self.widget.export_button.isEnabled())
        self.assertTrue(self.widget.extract_button.isEnabled())
        self.assertIn("取帧", self.widget.extract_button.text())

    def test_context_panel_switches_between_copy_and_background_controls(self):
        self.widget._canvas_selection_changed(False)
        self.assertIs(self.widget.panel_stack.currentWidget(), self.widget.bg_controls)
        self.assertEqual(self.widget.panel_title.text(), "底图取景")
        self.widget._canvas_selection_changed(True)
        self.assertIs(self.widget.panel_stack.currentWidget(), self.widget.copy_controls)
        self.assertEqual(self.widget.panel_title.text(), "封面文案")

    def test_main_canvas_ratio_switch_updates_preview_and_export_contract(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.widget.canvas_ratio_buttons["16x9"].click()
        self.assertEqual(self.widget._canvas_key, "16x9")
        self.assertIn("16:9", self.widget.canvas_hint.text())
        self.assertIn("1920×1080", self.widget.export_summary.text())
        self.assertIn("16:9", self.widget.export_button.text())
        self.widget.canvas_ratio_buttons["4x3"].click()
        self.assertEqual(self.widget._canvas_key, "4x3")
        self.assertIn("1440×1080", self.widget.export_summary.text())

    def test_cleanup_exposes_output_contract(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.assertIn("项目目录", self.widget.export_summary.text())
        self.assertIn("4:3", self.widget.export_both_button.text())
        self.assertIn("16:9", self.widget.export_both_button.text())
        # 已删除：AI 三个方案、另一比例小预览、X/Y 与旋转数值。
        for name in ("ai_button", "check_preview", "check_preview_toggle", "x_spin", "y_spin", "rotation_spin"):
            self.assertFalse(hasattr(self.widget, name), name)

    def test_nearby_frame_strip_marks_current_source(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.widget._selected_frame_timestamp = 10.0
        self.widget._refresh_nearby_frame_strip(10.0)
        checked = [button for button in self.widget.nearby_frame_buttons if button.isChecked()]
        self.assertEqual(len(checked), 1)
        self.assertAlmostEqual(float(checked[0].property("timestamp")), 10.0, places=2)
        self.assertEqual(checked[0].objectName(), "frameThumb")
        self.widget._selected_frame_timestamp = None
        self.widget._refresh_nearby_frame_strip(0.0)
        self.assertEqual(
            len([button for button in self.widget.nearby_frame_buttons if button.isChecked()]),
            1,
        )

    def _text(self, object_id: str, profile: str = "4x3"):
        from autoslice.desktop.cover_model import object_for_profile

        return object_for_profile(self.widget.document, object_id, profile)

    def test_copy_variant_switch_only_swaps_text_and_keeps_boxes(self):
        from dataclasses import replace

        from autoslice.desktop.cover_copy import BasicCoverCopy
        from autoslice.desktop.cover_model import update_text_object

        self.widget.set_context(self.project, self.project.videos[0])
        moved = replace(
            self._text("copy-b"), transform=replace(self._text("copy-b").transform, x=0.31, y=0.55),
            style=replace(self._text("copy-b").style, font_size=150),
        )
        self.widget.document = update_text_object(self.widget.document, moved, profile_key="4x3")
        self.widget._copy_variants = (
            BasicCoverCopy("旧上下文", "旧主文案"),
            BasicCoverCopy("", "第二版主文案"),
        )
        self.widget._copy_variant_index = 0
        self.widget._cycle_copy()
        current = self._text("copy-b")
        self.assertEqual(current.text, "第二版主文案")
        self.assertEqual((current.transform.x, current.transform.y), (0.31, 0.55))
        self.assertEqual(current.style.font_size, 150)
        # 空 A 两个比例一起隐藏；换回有 A 的版本再显示。
        if any(item.id == "copy-a" for item in self.widget.document.objects):
            self.assertFalse(self._text("copy-a").visible)
            self.assertFalse(self._text("copy-a", "16x9").visible)
            self.widget._cycle_copy()
            self.assertTrue(self._text("copy-a", "16x9").visible)
            self.assertEqual(self._text("copy-a").text, "旧上下文")

    def test_duplicate_button_makes_independent_text_box_and_delete_removes_it(self):
        from autoslice.desktop.cover_copy import BasicCoverCopy
        from autoslice.desktop.cover_model import TextObject

        self.widget.set_context(self.project, self.project.videos[0])
        before = {item.id for item in self.widget.document.objects}
        self.widget._duplicate_object("copy-b")
        added = [item for item in self.widget.document.objects if item.id not in before]
        self.assertEqual(len(added), 1)
        copy_id = added[0].id
        self.assertIsInstance(added[0], TextObject)
        self.assertEqual(self.widget.document.selected_object_id, copy_id)
        for profile in ("4x3", "16x9"):
            source, duplicate = self._text("copy-b", profile), self._text(copy_id, profile)
            self.assertTrue(duplicate.visible)
            self.assertEqual(duplicate.text, source.text)
            self.assertGreater(duplicate.transform.y, source.transform.y)
        # 换一版只改 A/B 主文案，复制出来的文本框保持不变。
        self.widget._copy_variants = (BasicCoverCopy("", "甲"), BasicCoverCopy("", "乙"))
        self.widget._copy_variant_index = 0
        original = self._text(copy_id).text
        self.widget._cycle_copy()
        self.assertEqual(self._text("copy-b").text, "乙")
        self.assertEqual(self._text(copy_id).text, original)
        self.widget._delete_object(copy_id)
        self.assertNotIn(copy_id, {item.id for item in self.widget.document.objects})
        self.assertNotIn(copy_id, self.widget.document.profiles["16x9"].overrides)
        self.widget._delete_object("copy-b")
        self.assertFalse(self._text("copy-b").visible)
        self.assertIn("copy-b", {item.id for item in self.widget.document.objects})

    def test_nearby_offsets_never_repeat_the_first_frame(self):
        for center in (0.0, 0.3, 1.2, 8.0):
            offsets = self.widget._nearby_offsets(center)
            stamps = [round(center + offset, 2) for offset in offsets]
            self.assertEqual(len(offsets), 7)
            self.assertEqual(len(set(stamps)), 7)
            self.assertGreaterEqual(min(stamps), 0.0)
            self.assertIn(round(center, 2), stamps)

    def test_batch_dialog_preselects_unexported_and_reports_failures(self):
        from autoslice.desktop.cover_batch_dialog import BatchTarget, CoverBatchDialog

        video = self.project.videos[0]
        other = SubmissionProject("project-2", "项目乙", self.project.directory, (video,))
        targets = (
            BatchTarget(self.project, video, exported=False, has_draft=False),
            BatchTarget(other, video, exported=True, has_draft=True),
        )
        calls = []

        def action(project, _video):
            calls.append(project.id)
            if project.id == "project-2":
                raise ValueError("坏了")
            return (Path("a.jpg"), Path("b.jpg"))

        def run(work, callback):
            try:
                callback(work(), None)
            except Exception as exc:  # noqa: BLE001 - 与真实后台任务一致，错误交给回调
                callback(None, exc)

        dialog = CoverBatchDialog(targets, action, run)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.checked_indices(), [0])
        results = []
        dialog.finished_batch.connect(lambda done, failed: results.append((done, failed)))
        dialog.all_button.click()
        dialog.start_button.click()
        self.assertEqual(calls, ["project-1", "project-2"])
        self.assertEqual(results, [(1, 1)])
        self.assertIn("已导出 2 张", dialog.list.item(0).text())
        self.assertIn("坏了", dialog.list.item(1).text())
        self.assertFalse(dialog._running)

    def test_routine_progress_goes_to_status_bar_without_shifting_canvas(self):
        messages = []
        self.widget.status_changed.connect(messages.append)
        self.widget.show()
        self.widget._set_notice("正在从视频取帧…", "info")
        self.assertFalse(self.widget.notice_label.isVisible())
        self.assertEqual(messages, ["正在从视频取帧…"])
        self.widget._set_notice("没有找到附近可用画面", "warning")
        self.assertTrue(self.widget.notice_label.isVisible())
        self.widget._set_notice("")
        self.assertFalse(self.widget.notice_label.isVisible())

    def test_inline_edit_types_on_canvas_and_escape_restores(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.widget.show()
        self.app.processEvents()
        original = self._text("copy-b").text
        self.widget._edit_text("copy-b")
        self.assertTrue(self.widget.inline_edit.isVisible())
        self.assertEqual(self.widget.inline_edit.toPlainText(), original)
        self.widget.inline_edit.setPlainText("画布上直接打字")
        self.assertEqual(self._text("copy-b").text, "画布上直接打字")
        self.assertEqual(self._text("copy-b", "16x9").text, "画布上直接打字")
        QTest.keyClick(self.widget.inline_edit, Qt.Key.Key_Escape)
        self.assertFalse(self.widget.inline_edit.isVisible())
        self.assertEqual(self._text("copy-b").text, original)
        # 再进一次，Ctrl+回车保留。
        self.widget._edit_text("copy-b")
        self.widget.inline_edit.setPlainText("保留这句")
        QTest.keyClick(self.widget.inline_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.assertFalse(self.widget.inline_edit.isVisible())
        self.assertEqual(self._text("copy-b").text, "保留这句")
        self.widget.close()

    def test_frame_slider_and_overview_choose_frames(self):
        from autoslice.desktop.cover_frames import CoverFrame

        self.widget.set_context(self.project, self.project.videos[0])
        chosen = []
        self.widget._choose_nearby_frame = chosen.append
        self.widget._video_duration_ready(self.widget._context_generation, 125.0, None)
        self.assertTrue(self.widget.frame_slider.isEnabled())
        self.assertEqual(self.widget.frame_slider.maximum(), 1250)
        self.assertEqual(self.widget.duration_label.text(), "2:05")
        self.widget.frame_slider.setValue(423)
        self.widget._slider_frame()
        self.assertEqual(chosen, [42.3])
        frames = tuple(CoverFrame(Path(f"{t}.jpg"), t, 50.0 + t) for t in range(0, 120, 10))
        self.widget._nearby_request_generation += 1
        self.widget._wider_frames_ready(self.widget._nearby_request_generation, frames, None, scope="全片")
        stamps = [float(button.property("timestamp")) for button in self.widget.nearby_frame_buttons]
        self.assertEqual(stamps, [50.0, 60.0, 70.0, 80.0, 90.0, 100.0, 110.0])

    def test_sync_button_follows_ratio(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.assertTrue(self.widget.sync_ratio_button.isEnabled())
        self.assertEqual(self.widget.sync_ratio_button.text(), "同步到 16:9")
        self.widget.canvas_ratio_buttons["16x9"].click()
        self.assertEqual(self.widget.sync_ratio_button.text(), "同步到 4:3")
        before = self.widget.document
        self.widget._sync_other_ratio()
        self.assertNotEqual(self.widget.document.profiles["4x3"], before.profiles["4x3"])
        self.widget._undo()
        self.assertEqual(self.widget.document.profiles["4x3"], before.profiles["4x3"])

    def test_hidden_text_can_be_restored_from_toolbar(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.assertFalse(self.widget.hidden_button.isVisibleTo(self.widget))
        self.widget._delete_object("copy-b")
        self.assertTrue(self.widget.hidden_button.isVisibleTo(self.widget))
        self.assertEqual(self.widget.hidden_button.text(), "已隐藏 1")
        self.widget._fill_hidden_menu()
        labels = [action.text() for action in self.widget.hidden_menu.actions()]
        self.assertTrue(any(label.startswith("主文案 B") for label in labels))
        self.widget._restore_object("copy-b")
        self.assertTrue(self._text("copy-b").visible)
        self.assertTrue(self._text("copy-b", "16x9").visible)
        self.assertFalse(self.widget.hidden_button.isVisibleTo(self.widget))

    def test_empty_primary_text_gets_candidate_back_when_restored(self):
        from autoslice.desktop.cover_copy import BasicCoverCopy

        self.widget.set_context(self.project, self.project.videos[0])
        self.widget._copy_variants = (BasicCoverCopy("上下文候选", "主文案候选"),)
        self.widget._copy_variant_index = 0
        self.widget._select_copy_role("B")
        self.widget.title_edit.setPlainText("")
        self.assertFalse(self._text("copy-b").visible)
        self.widget._restore_object("copy-b")
        self.assertEqual(self._text("copy-b").text, "主文案候选")

    def test_lock_toggle_and_locked_delete_is_refused(self):
        messages = []
        self.widget.status_changed.connect(messages.append)
        self.widget.set_context(self.project, self.project.videos[0])
        self.widget._select_copy_role("B")
        self.widget.lock_text_button.click()
        self.assertTrue(next(item for item in self.widget.document.objects if item.id == "copy-b").locked)
        self.widget._delete_selected_object()
        self.assertTrue(self._text("copy-b").visible)
        self.assertIn("锁定", messages[-1])
        self.widget._undo()
        self.assertFalse(next(item for item in self.widget.document.objects if item.id == "copy-b").locked)

    def test_overlay_opacity_and_shape_style_apply_to_both_ratios(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.widget._add_shape("circle")
        shape_id = self.widget.document.selected_object_id
        self.widget.shape_stroke_spin.setValue(14)
        self.widget.shape_stroke_button.set_color("#FF0000")
        self.widget._store_overlay_style()
        for key in ("4x3", "16x9"):
            shape = self._text(shape_id, key)
            self.assertEqual((shape.stroke, shape.stroke_width), ("#FF0000", 14))
        self.widget._hide_selected_object()
        self.assertFalse(self._text(shape_id).visible)
        self.assertEqual(self.widget.hidden_button.text(), "已隐藏 1")

    def test_next_button_and_export_history_dialog(self):
        from autoslice.desktop.cover_export_dialog import CoverExportDialog

        self.widget.set_context(self.project, self.project.videos[0])
        requested = []
        self.widget.next_video_requested.connect(lambda: requested.append(True))
        self.assertTrue(self.widget.next_button.isEnabled())
        self.widget.next_button.click()
        self.assertEqual(requested, [True])
        exported = Path(self.temp.name) / "AutoCover-视频.jpg"
        Image.new("RGB", (64, 48), "red").save(exported)
        dialog = CoverExportDialog((
            {"title": "项目甲", "canvas_key": "4x3", "output": str(exported), "timestamp": "2026-10-08T10:00:00+00:00"},
            {"video": "旧.mp4", "canvas_key": "16x9", "output": str(exported.with_name("不存在.jpg")), "timestamp": "2026-10-08T11:00:00+00:00"},
        ))
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.list.count(), 2)
        self.assertIn("不存在", dialog.list.item(0).text())
        self.assertIn("16:9", dialog.list.item(0).text())
        dialog.list.setCurrentRow(1)
        self.assertEqual(dialog.selected_path(), exported)

    def test_text_dragged_past_left_and_top_edges_stays_there_after_commit(self):
        self.widget.set_context(self.project, self.project.videos[0])
        current = self._text("copy-b")
        moved = replace(current, transform=replace(current.transform, x=-0.3, y=-0.1))
        # 走真实的松手提交路径：画布 object_changed → 比例覆盖 → 重新读出。
        self.widget._canvas_object_changed(moved, "4x3")
        self.assertAlmostEqual(self._text("copy-b").transform.x, -0.3)
        self.assertAlmostEqual(self._text("copy-b").transform.y, -0.1)
        self.assertAlmostEqual(self.widget.canvas._find_text().transform.x, -0.3)
        reloaded = CoverDocument.from_payload(self.widget.document.to_payload())
        self.assertAlmostEqual(object_for_profile(reloaded, "copy-b", "4x3").transform.x, -0.3)

    def test_batch_button_follows_project_list(self):
        self.assertFalse(self.widget.batch_button.isEnabled())
        self.widget.set_project_list((self.project,))
        self.assertTrue(self.widget.batch_button.isEnabled())
        self.widget.set_project_list(())
        self.assertFalse(self.widget.batch_button.isEnabled())

    def test_stale_frame_callback_is_ignored_after_context_reset(self):
        self.widget.set_context(self.project, self.project.videos[0])
        old_generation = self.widget._frame_request_generation
        self.widget.set_context(None, None)
        self.widget._frame_ready(
            old_generation,
            True,
            (Path(self.temp.name) / "late-frame.jpg", 1.0),
            None,
        )
        self.assertIsNone(self.widget.project)
        self.assertIsNone(self.widget.video)
        self.assertIsNone(self.widget.draft.image_path)
        self.assertIn("字幕页", self.widget.canvas.text())

    def test_title_drag_updates_normalized_position_and_current_playhead_is_read_only(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.widget.timestamp_edit.setValue(3.0)
        self.widget.set_current_playhead(8.5)
        self.widget._title_position_changed(0.42, 0.37)
        self.assertAlmostEqual(self.widget.draft.text_x, 0.42)
        self.assertAlmostEqual(self.widget.draft.text_y, 0.37)
        self.assertAlmostEqual(self.widget.timestamp_edit.value(), 3.0)
        self.assertAlmostEqual(self.widget._current_playhead, 8.5)

    def test_document_change_survives_debounced_preview_and_undo_redo(self):
        image = Path(self.temp.name) / "font-and-ai.png"
        Image.new("RGB", (640, 480), "#334155").save(image)
        base = CoverDraft("上下文说明\n主视觉标题", str(image), font_size=96).to_document()
        self.widget.document = base
        self.widget.draft = CoverDraft.from_document(base)
        self.widget.history.reset(base)
        self.widget._apply_draft()
        from autoslice.desktop.cover_model import update_text_object

        b_id = next(item.id for item in base.objects if isinstance(item, TextObject) and item.copy_role == "B")
        current = object_for_profile(base, b_id, "4x3")
        candidate = update_text_object(
            base, replace(current, transform=replace(current.transform, x=0.66)), profile_key="4x3",
        )
        before_generation = self.widget._preview_request_generation
        self.widget.document = candidate
        self.widget._commit_document_change(base)
        applied = object_for_profile(self.widget.document, b_id, "4x3")
        self.assertIsInstance(applied, TextObject)
        self.assertAlmostEqual(applied.transform.x, 0.66, places=3)
        self.assertGreater(self.widget._preview_request_generation, before_generation)

        # 让自动保存、预览和旧回调都有机会执行，再检查文档没有被旧控件回写。
        for _ in range(90):
            self.app.processEvents()
            time.sleep(0.03)
        stable = object_for_profile(self.widget.document, b_id, "4x3")
        self.assertIsInstance(stable, TextObject)
        self.assertAlmostEqual(stable.transform.x, applied.transform.x, places=3)
        self.assertAlmostEqual(stable.transform.y, applied.transform.y, places=3)

        self.widget._undo()
        undone = object_for_profile(self.widget.document, b_id, "4x3")
        self.assertNotAlmostEqual(undone.transform.x, applied.transform.x, places=3)
        self.widget._redo()
        redone = object_for_profile(self.widget.document, b_id, "4x3")
        self.assertAlmostEqual(redone.transform.x, applied.transform.x, places=3)
        self.widget._set_canvas_key("16x9")
        self.widget._set_canvas_key("4x3")
        restored = object_for_profile(self.widget.document, b_id, "4x3")
        self.assertAlmostEqual(restored.transform.x, applied.transform.x, places=3)

    def test_canvas_and_renderer_report_same_resolved_chinese_font(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        image = Path(self.temp.name) / "font.png"
        Image.new("RGB", (640, 480), "#475569").save(image)
        document = CoverDraft("中文字体宽度", str(image), font_size=96).to_document()
        text = next(item for item in document.objects if isinstance(item, TextObject))
        resolution = resolve_font_selection(text.style.font_family)
        canvas = CoverCanvas()
        self.addCleanup(canvas.close)
        canvas.resize(900, 600)
        canvas.set_preview(QPixmap(str(image)))
        canvas.set_document(document, "4x3")
        canvas.show()
        self.app.processEvents()
        self.assertIsNotNone(canvas.resolved_font)
        self.assertEqual(canvas.resolved_font.path, resolution.path)
        family, font_id = canvas.resolved_qt_font
        self.assertTrue(family)
        if resolution.path is not None:
            self.assertGreaterEqual(font_id, 0)
            self.assertIn(family, QFontDatabase.applicationFontFamilies(font_id))
        qt_font = QFont(family)
        qt_font.setPixelSize(96)
        metrics = QFontMetrics(qt_font)
        # 画布实际字体和 Pillow 来源至少都能测量同一条中文文案；具体抗锯齿
        # 像素会随 Qt/Pillow 后端略有差异，这里断言字形宽度处于同一数量级。
        from PIL import ImageFont

        qt_width = metrics.horizontalAdvance("中文字体宽度")
        pillow_font = ImageFont.truetype(str(resolution.path), size=96) if resolution.path else None
        self.assertGreater(qt_width, 0)
        if pillow_font is not None:
            pillow_width = float(pillow_font.getlength("中文字体宽度"))
            self.assertGreater(pillow_width, 0)
            self.assertGreater(qt_width / pillow_width, 0.45)
            self.assertLess(qt_width / pillow_width, 2.2)

    def test_canvas_mouse_gestures_emit_title_and_background_changes(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        canvas = CoverCanvas()
        self.addCleanup(canvas.close)
        canvas.resize(900, 600)
        canvas.set_preview(QPixmap(900, 506))
        canvas.set_title_rect(QRectF(0.1, 0.1, 0.3, 0.2))
        canvas.show()
        self.app.processEvents()
        title_positions = []
        background_positions = []
        canvas.title_position_changed.connect(lambda x, y: title_positions.append((x, y)))
        canvas.background_position_changed.connect(lambda x, y: background_positions.append((x, y)))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(220, 100))
        QTest.mouseMove(canvas, QPoint(320, 150))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(320, 150))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(700, 400))
        QTest.mouseMove(canvas, QPoint(650, 350))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(650, 350))
        self.assertTrue(title_positions)
        self.assertTrue(background_positions)
        self.assertGreater(title_positions[-1][0], 0.1)

    def test_title_drag_preserves_mouse_offset_without_forced_safe_margin(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        canvas = CoverCanvas()
        self.addCleanup(canvas.close)
        canvas.resize(900, 600)
        canvas.set_preview(QPixmap(900, 506))
        canvas.set_title_rect(QRectF(0.1, 0.1, 0.3, 0.2))
        canvas.show()
        self.app.processEvents()
        positions = []
        canvas.title_position_changed.connect(lambda x, y: positions.append((x, y)))

        # 图片在 QLabel 中上下留白约 47 px；按下点位于标题内部，
        # 移动后标题左上角应跟随同一按下偏移，而不是跳到鼠标位置。
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(220, 140))
        QTest.mouseMove(canvas, QPoint(320, 190))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(320, 190))

        self.assertTrue(positions)
        x, y = positions[-1]
        self.assertAlmostEqual(x, 0.2111, places=2)
        self.assertAlmostEqual(y, 0.1988, places=2)
        self.assertGreater(x, 0.1)
        self.assertGreater(y, 0.1)

    def test_title_drag_can_reach_edge_without_safety_lock(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        canvas = CoverCanvas()
        self.addCleanup(canvas.close)
        canvas.resize(900, 600)
        canvas.set_preview(QPixmap(900, 506))
        canvas.set_title_rect(QRectF(0.2, 0.2, 0.3, 0.2))
        canvas.show()
        self.app.processEvents()
        positions = []
        canvas.title_position_changed.connect(lambda x, y: positions.append((x, y)))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(300, 170))
        QTest.mouseMove(canvas, QPoint(80, 80))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 80))
        self.assertTrue(positions)
        self.assertLess(positions[-1][0], 0.06)
        self.assertLess(positions[-1][1], 0.06)

    def test_canvas_selection_feedback_distinguishes_title_and_background(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        canvas = CoverCanvas()
        self.addCleanup(canvas.close)
        canvas.resize(900, 600)
        canvas.set_preview(QPixmap(900, 506))
        canvas.set_title_rect(QRectF(0.1, 0.1, 0.3, 0.2))
        canvas.show()
        self.app.processEvents()
        selected = []
        canvas.selected_object_changed.connect(selected.append)
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(220, 140))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(220, 140))
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(700, 400))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(700, 400))
        self.assertEqual(selected[-2:], ["title", "background"])

    def test_qt_navigation_shares_selected_project_with_cover_page(self):
        from unittest.mock import patch

        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        root = Path(self.temp.name) / "投稿根目录"
        folder = root / "项目乙"
        folder.mkdir(parents=True)
        video = folder / "视频.mp4"
        video.write_bytes(b"video")
        (folder / "视频.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n测试字幕\n", encoding="utf-8"
        )
        service = SubmissionProjectService(root)
        storage = DesktopStorage(Path(self.temp.name) / "window-data")
        with patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("smoke")):
            window = DesktopWindow(service, storage)
        window.show()
        # close 可能因后台任务被窗口拒绝；hide 兜底，避免残留窗口影响后续 Qt 测试。
        self.addCleanup(lambda: (setattr(window, "_resolve_unsaved", lambda: True), window.close(), window.hide()))
        deadline = 200
        while not window.project_buttons and deadline:
            self.app.processEvents()
            self.app.processEvents()
            deadline -= 1
        self.assertTrue(window.project_buttons)
        project = service.snapshot.projects[0]
        window._select_real_project(project)
        for _ in range(300):
            self.app.processEvents()
            if window.cover_editor.video is not None:
                break
        self.assertEqual(window.cover_editor.project.id, project.id)
        self.assertEqual(window.cover_editor.video.path, project.videos[0].path)
        window._player_position = 7.5
        window._select_page(1)
        self.assertAlmostEqual(window.cover_editor._current_playhead, 7.5)
        self.assertAlmostEqual(window._player_position, 7.5)
        window._select_page(1)
        self.assertIs(window.pages.currentWidget(), window.cover_editor.parentWidget())


    def test_next_from_cover_moves_to_following_project(self):
        from unittest.mock import patch

        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        root = Path(self.temp.name) / "投稿根目录"
        for name in ("项目一", "项目二"):
            folder = root / name
            folder.mkdir(parents=True)
            (folder / "视频.mp4").write_bytes(b"video")
        service = SubmissionProjectService(root)
        storage = DesktopStorage(Path(self.temp.name) / "next-data")
        with patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("smoke")):
            window = DesktopWindow(service, storage)
        window.show()
        self.addCleanup(lambda: (setattr(window, "_resolve_unsaved", lambda: True), window.close(), window.hide()))
        for _ in range(200):
            self.app.processEvents()
            if window.project_buttons:
                break
        projects = service.snapshot.projects
        window._select_real_project(projects[0])
        for _ in range(300):
            self.app.processEvents()
            if window.cover_editor.video is not None and not window._loading:
                break
        window.cover_editor.next_button.click()
        for _ in range(300):
            self.app.processEvents()
            if window.project is projects[1]:
                break
        self.assertIs(window.project, projects[1])
        window.cover_editor.next_button.click()
        self.assertIn("最后一个", window.app_status.text())


@unittest.skipIf(QApplication is None, "PySide6 不在当前解释器中")
class CoverEditorQtMediaIntegrationTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover import CoverEditorWidget

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        project_dir = root / "真实项目"
        project_dir.mkdir()
        video_path = project_dir / "真实视频.mp4"
        _make_real_video(video_path)
        self.project = SubmissionProject(
            "real-project",
            "真实项目",
            str(project_dir),
            (ProjectVideo("真实视频.mp4", str(video_path), "", "", False, False, ""),),
        )
        self.app = QApplication.instance() or QApplication([])
        self.widget = CoverEditorWidget(DesktopStorage(root / "data"))
        self.widget.resize(900, 600)
        self.addCleanup(self.widget.deleteLater)
        # 导出完成后后台仍可能在写导出历史；先等线程池结束再删临时目录。
        self.addCleanup(QThreadPool.globalInstance().waitForDone)

    def test_real_frame_click_updates_draft_canvas_and_export(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.assertTrue(self.widget.extract_button.isEnabled())
        self.widget._extract_frame()
        for _ in range(240):
            self.app.processEvents()
            time.sleep(0.05)
            if self.widget._preview_path is not None:
                break
        self.assertIsNotNone(self.widget._preview_path)
        self.assertTrue(Path(self.widget.draft.image_path).is_file())
        pixmap = self.widget.canvas.pixmap()
        self.assertIsNotNone(pixmap)
        self.assertFalse(pixmap.isNull())
        self.assertTrue(self.widget.export_button.isEnabled())
        self.widget._export()
        for _ in range(160):
            self.app.processEvents()
            time.sleep(0.05)
            if list(Path(self.project.directory).glob("AutoCover-*.jpg")):
                break
        exported = list(Path(self.project.directory).glob("AutoCover-*.jpg"))
        self.assertEqual(len(exported), 1)
        with Image.open(exported[0]) as image:
            image.verify()

    def _wait(self, condition, seconds=12.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.03)
        return False

    def test_zero_playhead_picks_overview_frame_and_offers_schemes(self):
        from autoslice.desktop.cover_frames import CoverFrame

        picked = Path(self.temp.name) / "picked.png"
        Image.new("RGB", (640, 360), "#335577").save(picked)
        calls = []
        self.widget.service.overview_candidates = lambda video: (
            CoverFrame(picked, 0.3, 50.0), CoverFrame(picked, 0.9, 70.0),
        )
        original = self.widget.service.extract_frame
        self.widget.service.extract_frame = lambda video, timestamp: (calls.append(timestamp), original(video, timestamp))[1]
        self.widget.show()
        self.widget.set_current_playhead(0.0)
        self.widget.set_context(self.project, self.project.videos[0])
        self.assertTrue(self._wait(lambda: self.widget.draft.image_path and self.widget._schemes))
        self.assertAlmostEqual(calls[0], 0.9)
        before = self.widget.document
        self.widget.scheme_buttons[1].click()
        self.assertTrue(self.widget.scheme_buttons[1].isChecked())
        self.assertNotEqual(self.widget.document, before)
        self.widget._undo()
        self.assertEqual(self.widget.document.profiles, before.profiles)
        self.widget.close()

    def test_add_text_creates_selected_box_ready_for_typing(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.assertTrue(self.widget.add_text_button.isEnabled())
        before = {item.id for item in self.widget.document.objects}
        self.widget.add_text_button.click()
        added = [item for item in self.widget.document.objects if item.id not in before]
        self.assertEqual(len(added), 1)
        self.assertEqual(self.widget.document.selected_object_id, added[0].id)
        self.assertEqual(self.widget.copy_role_label.text(), "自建文本框")
        self.widget.title_edit.setPlainText("新的文字")
        current = [item for item in self.widget.document.objects if item.id == added[0].id][0]
        self.assertEqual(current.text, "新的文字")
        # 主文案不受影响。
        self.assertNotEqual(self.widget._primary_copy_ids().get("B"), added[0].id)

    def test_auto_cover_exports_both_ratios_saves_draft_and_never_overwrites(self):
        service = self.widget.service
        project, video = self.project, self.project.videos[0]
        self.assertEqual(service.existing_exports(project, video), ())
        self.assertFalse(service.has_draft(project, video))
        first = service.auto_cover(project, video)
        sizes = []
        for path in first:
            with Image.open(path) as image:
                sizes.append(image.size)
        self.assertEqual(sizes, [(1440, 1080), (1920, 1080)])
        self.assertTrue(service.has_draft(project, video))
        self.assertEqual(len(service.existing_exports(project, video)), 2)
        # 第二次按已存草稿导出，新文件带序号，不覆盖。
        second = service.auto_cover(project, video)
        self.assertTrue(all(path.exists() for path in first + second))
        self.assertEqual(len(set(first + second)), 4)

    def test_reexported_video_keeps_layout_and_retakes_frame_at_same_time(self):
        from autoslice.desktop.cover import CoverEditorWidget
        from autoslice.desktop.cover_model import update_text_object

        project, video = self.project, self.project.videos[0]
        self.widget.show()
        self.widget.set_current_playhead(0.6)
        self.widget.set_context(project, video)
        self.assertTrue(self._wait(lambda: self.widget.draft.image_path and not self.widget._jobs, 20))
        current = object_for_profile(self.widget.document, "copy-b", "4x3")
        moved = replace(current, transform=replace(current.transform, x=0.33, y=0.44))
        self.widget.document = update_text_object(self.widget.document, moved, profile_key="4x3")
        self.widget._save_draft()
        self.widget.close()
        _make_real_video(Path(video.path))
        widget = CoverEditorWidget(self.widget.service.storage)
        self.addCleanup(widget.close)
        widget.show()
        widget.set_current_playhead(0.0)
        widget.set_context(project, video)
        self.assertEqual(widget.draft_status.text(), "视频已更新 · 沿用原排版")
        self.assertTrue(self._wait(lambda: widget.draft.image_path and not widget._jobs, 20))
        kept = object_for_profile(widget.document, "copy-b", "4x3")
        self.assertAlmostEqual(kept.transform.x, 0.33, places=3)
        self.assertAlmostEqual(kept.transform.y, 0.44, places=3)
        self.assertAlmostEqual(widget.document.source.selected_timestamp, 0.6, delta=0.15)

    def test_real_png_import_updates_preview_canvas(self):
        from unittest.mock import patch

        source = Path(self.temp.name) / "中文底图【测试】.png"
        Image.new("RGB", (320, 180), "#d97706").save(source)
        self.widget.set_context(self.project, self.project.videos[0])
        with patch(
            "PySide6.QtWidgets.QFileDialog.getOpenFileName",
            return_value=(str(source), "图片 (*.png *.jpg *.jpeg *.webp *.bmp)"),
        ):
            self.widget._import_image()
        for _ in range(160):
            self.app.processEvents()
            time.sleep(0.05)
            if self.widget._preview_path is not None:
                break
        self.assertIsNotNone(self.widget.draft.image_path)
        self.assertTrue(Path(self.widget.draft.image_path).is_file())
        self.assertIsNotNone(self.widget._preview_path)
        pixmap = self.widget.canvas.pixmap()
        self.assertIsNotNone(pixmap)
        self.assertFalse(pixmap.isNull())


if __name__ == "__main__":
    unittest.main()
