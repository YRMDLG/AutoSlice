"""AutoCover-01 桌面服务和 Qt 工作区的最小回归。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None

from autoslice.desktop.cover_service import CoverDraft, CoverService
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject


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
        self.assertIn("从当前视频取帧", self.widget.frame_button.text())

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
        window._select_page(1)
        self.assertIs(window.pages.currentWidget(), window.cover_editor.parentWidget())


if __name__ == "__main__":
    unittest.main()
