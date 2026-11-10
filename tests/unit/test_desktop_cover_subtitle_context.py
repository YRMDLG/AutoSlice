"""封面 AI 的字幕来源：校对版优先、原字幕回退与中文编码兼容。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autoslice.desktop.cover_ai import _subtitle_lines
from autoslice.desktop.cover_store import CoverStoreService
from autoslice.desktop.projects import ProjectVideo


class CoverSubtitleContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = ProjectVideo("视频.mp4", str(self.root / "视频.mp4"), "", "", False, False, "")

    def write_srt(self, name, text, encoding="utf-8"):
        path = self.root / name
        path.write_text(f"1\n00:00:01,000 --> 00:00:03,000\n{text}\n", encoding=encoding)
        return str(path)

    def test_corrected_file_is_used_even_when_the_inventory_flag_is_stale(self):
        video = replace(self.video, srt_path=self.write_srt("原文.srt", "原字幕"),
                        corrected_srt_path=self.write_srt("校对.srt", "完整校对字幕"))
        self.assertEqual(CoverStoreService.subtitle_cues(video), ((1.0, 3.0, "完整校对字幕"),))

    def test_missing_or_malformed_corrected_file_falls_back_to_source(self):
        source = self.write_srt("原文.srt", "前因与结果都要读")
        corrected = self.root / "校对.srt"
        for exists in (False, True):
            with self.subTest(corrected_exists=exists):
                if exists:
                    corrected.write_text("不是有效字幕", encoding="utf-8")
                video = replace(self.video, srt_path=source, corrected_srt_path=str(corrected), has_corrected_srt=True)
                self.assertEqual(CoverStoreService.subtitle_cues(video)[0][2], "前因与结果都要读")

    def test_legacy_chinese_encoding_is_read_without_rewriting_the_file(self):
        source = self.write_srt("中文字幕.srt", "不能只看选帧附近的一句", "gb18030")
        before = Path(source).read_bytes()
        video = replace(self.video, srt_path=source)
        self.assertEqual(CoverStoreService.subtitle_cues(video)[0][2], "不能只看选帧附近的一句")
        self.assertEqual(Path(source).read_bytes(), before)

    def test_new_sibling_subtitle_is_found_without_rescanning_the_project(self):
        self.write_srt("视频.srt", "新保存的字幕也要看到")
        self.assertEqual(CoverStoreService.subtitle_cues(self.video)[0][2], "新保存的字幕也要看到")

    def test_long_subtitle_context_keeps_the_ending_and_marks_omissions(self):
        cues = [(float(index), float(index + 1), "中间字幕" * 30) for index in range(250)]
        cues[0] = (0.0, 1.0, "事情开始的原因")
        cues[-1] = (249.0, 250.0, "最后才出现的结果")
        text = _subtitle_lines(cues)
        self.assertIn("事情开始的原因", text)
        self.assertIn("最后才出现的结果", text)
        self.assertIn("中段省略", text)
        self.assertLessEqual(len(text), 18000)


if __name__ == "__main__":
    unittest.main()
