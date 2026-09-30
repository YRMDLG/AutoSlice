"""AutoCover-01 桌面服务和 Qt 工作区的最小回归。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QRectF, Qt
    from PySide6.QtGui import QPixmap
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None

from autoslice.desktop.cover_service import (
    CoverDraft,
    CoverService,
    text_transforms_for,
    wrap_cover_title,
)
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject


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

    def test_page_inherits_current_project_and_exposes_editor_controls(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.assertEqual(self.widget.project_label.text(), "项目甲")
        self.assertEqual(self.widget.video_label.text(), "视频.mp4")
        self.assertEqual(self.widget.title_edit.text(), "项目甲")
        self.assertFalse(self.widget.export_button.isEnabled())
        self.assertTrue(self.widget.frame_button.isEnabled())
        self.assertIn("从当前视频取帧", self.widget.frame_button.text())

    def test_title_drag_updates_normalized_position_and_current_playhead_is_read_only(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.widget.timestamp.setValue(3.0)
        self.widget.set_current_playhead(8.5)
        self.widget._title_position_changed(0.42, 0.37)
        self.assertAlmostEqual(self.widget.draft.text_x, 0.42)
        self.assertAlmostEqual(self.widget.draft.text_y, 0.37)
        self.assertAlmostEqual(self.widget.timestamp.value(), 3.0)
        self.assertAlmostEqual(self.widget._current_playhead, 8.5)

    def test_canvas_mouse_gestures_emit_title_and_background_changes(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        canvas = CoverCanvas()
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
        self.addCleanup(lambda: (setattr(window, "_resolve_unsaved", lambda: True), window.close()))
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

    def test_real_frame_click_updates_draft_canvas_and_export(self):
        self.widget.set_context(self.project, self.project.videos[0])
        self.assertTrue(self.widget.frame_button.isEnabled())
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

    def test_real_png_import_updates_preview_canvas(self):
        from unittest.mock import patch

        source = Path(self.temp.name) / "中文底图【测试】.png"
        Image.new("RGB", (320, 180), "#d97706").save(source)
        self.widget.set_context(self.project, self.project.videos[0])
        with patch(
            "autoslice.desktop.cover.QFileDialog.getOpenFileName",
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
