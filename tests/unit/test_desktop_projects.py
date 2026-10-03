"""Desktop vNext 的统一投稿项目扫描测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice import runtime_config
from autoslice.desktop.projects import (
    SubmissionProjectService,
    configured_submission_root,
)


class SubmissionProjectServiceTests(unittest.TestCase):
    def test_default_root_and_existing_configuration(self):
        with patch.dict(os.environ, {"AUTOSLICE_SUBMISSION_DIR": ""}):
            with patch("autoslice.desktop.projects.runtime_config.LOCAL_ENVIRONMENT", {}):
                # 未配置时与 Web 端一致回退到仓库内 submissions，不写死本机路径
                self.assertEqual(
                    configured_submission_root(),
                    (runtime_config.PROJECT_DIR / "submissions").resolve(),
                )
        with patch.dict(os.environ, {"AUTOSLICE_SUBMISSION_DIR": r"D:\投稿"}):
            self.assertEqual(configured_submission_root(), Path(r"D:\投稿").resolve())

    def test_refresh_groups_existing_scan_pairs_by_title_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "标题甲"
            second = root / "标题乙"
            first.mkdir()
            second.mkdir()
            (first / "成片.mp4").touch()
            (first / "成片.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8"
            )
            (first / "另一条.mkv").touch()
            (second / "待补.mp4").touch()

            service = SubmissionProjectService(root)
            snapshot = service.refresh()
            projects = {item.title: item for item in snapshot.projects}
            self.assertEqual(set(projects), {"标题甲", "标题乙"})
            self.assertEqual(len(projects["标题甲"].videos), 2)
            self.assertEqual(projects["标题甲"].status, "缺少字幕")
            self.assertEqual(projects["标题乙"].status, "缺少字幕")

            (second / "待补.mp4").unlink()
            (root / "标题丙").mkdir()
            refreshed = service.refresh()
            self.assertIs(service.snapshot, refreshed)
            projects = {item.title: item for item in refreshed.projects}
            self.assertEqual(set(projects), {"标题甲", "标题乙", "标题丙"})
            self.assertEqual(projects["标题乙"].status, "缺少视频")
            self.assertEqual(projects["标题丙"].status, "缺少视频")

            (root / "标题丙").rmdir()
            self.assertNotIn(
                "标题丙", {item.title for item in service.refresh().projects}
            )

    def test_missing_and_empty_roots_are_displayable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "投稿"
            service = SubmissionProjectService(root)
            self.assertEqual(service.refresh().status, "目录不存在")
            root.mkdir()
            self.assertEqual(service.refresh().status, "目录为空")

    def test_unreadable_root_is_displayable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = SubmissionProjectService(root)
            with patch.object(Path, "iterdir", side_effect=PermissionError("拒绝访问")):
                snapshot = service.refresh()
            self.assertEqual(snapshot.status, "目录不可读")
            self.assertEqual(snapshot.projects, ())
            self.assertIn("拒绝访问", snapshot.message)

    def test_scan_uses_legacy_pairing_and_ignores_generated_video(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "标题"
            folder.mkdir()
            (folder / "片段.mp4").touch()
            (folder / "字幕.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n你好\n", encoding="utf-8"
            )
            (folder / "字幕_排版.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n排版后\n", encoding="utf-8"
            )
            (folder / "片段_字幕版.mp4").touch()
            (folder / "片段_字幕版 (2).mp4").touch()
            project = SubmissionProjectService(directory).refresh().projects[0]
            self.assertEqual([video.name for video in project.videos], ["片段.mp4"])
            self.assertTrue(project.videos[0].has_source_srt)
            self.assertEqual(Path(project.videos[0].srt_path).name, "字幕_排版.srt")
            self.assertEqual(project.status, "素材已识别")


if __name__ == "__main__":
    unittest.main()
