"""Desktop 字幕文档沿用旧 SRT 契约的回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.desktop.projects import SubmissionProjectService
from autoslice.desktop.subtitles import SubtitleDocument
from autoslice.subtitle_workflow import parse_srt_document, save_corrected_srt

SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\n第一条\n\n"
    "2\n00:00:02,500 --> 00:00:03,000\n第二条\n\n"
    "3\n00:00:04,000 --> 00:00:05,000\n第三条\n"
)


class DesktopSubtitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "标题"
        self.folder.mkdir()
        (self.folder / "成片.mp4").touch()
        self.source = self.folder / "成片.srt"
        self.source.write_text(SRT, encoding="utf-8")

    def video(self):
        return SubmissionProjectService(self.root).refresh().projects[0].videos[0]

    def test_load_edit_delete_undo_redo_save_reload(self):
        document = SubtitleDocument.load(self.video())
        self.assertEqual([entry.text for entry in document.entries], ["第一条", "第二条", "第三条"])
        document.edit_text(1, "改好\n下一行")
        self.assertTrue(document.dirty)
        self.assertFalse((self.folder / "成片_校对.srt").exists())
        document.delete(2)
        self.assertEqual([entry.index for entry in document.entries], [1, 3])
        self.assertTrue(document.undo())
        self.assertEqual([entry.index for entry in document.entries], [1, 2, 3])
        self.assertTrue(document.undo())
        self.assertFalse(document.dirty)
        self.assertTrue(document.redo())
        self.assertTrue(document.redo())
        output = document.save()
        self.assertFalse(document.dirty)
        self.assertEqual(Path(output).name, "成片_校对.srt")
        self.assertEqual(self.source.read_text(encoding="utf-8"), SRT)
        self.assertTrue((self.folder / "成片_校对状态.json").exists())
        cues = parse_srt_document(output)
        self.assertEqual([(cue.index, cue.start, cue.end, cue.text) for cue in cues], [
            (1, "00:00:01,000", "00:00:02,000", "改好\n下一行"),
            (3, "00:00:04,000", "00:00:05,000", "第三条"),
        ])
        reloaded = SubtitleDocument.load(self.video())
        self.assertEqual([entry.text for entry in reloaded.entries], ["改好\n下一行", "第三条"])
        self.assertFalse(reloaded.dirty)
        self.assertTrue(document.undo())
        self.assertTrue(document.dirty)
        self.assertTrue(document.redo())
        self.assertFalse(document.dirty)

    def test_missing_corrupt_and_last_entry(self):
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, "缺少 SRT"):
            SubtitleDocument.load(self.video())
        self.source.write_text("损坏文件", encoding="utf-8")
        with self.assertRaises(ValueError):
            SubtitleDocument.load(self.video())
        self.source.write_text(SRT, encoding="utf-8")
        document = SubtitleDocument.load(self.video())
        document.delete(1)
        document.delete(2)
        with self.assertRaisesRegex(ValueError, "至少保留一条"):
            document.delete(3)

    def test_write_failure_keeps_dirty_and_source(self):
        document = SubtitleDocument.load(self.video())
        document.edit_text(1, "修改")
        with patch("autoslice.desktop.subtitles.save_corrected_srt", side_effect=PermissionError("拒绝写入")):
            with self.assertRaises(PermissionError):
                document.save()
        self.assertTrue(document.dirty)
        self.assertEqual(self.source.read_text(encoding="utf-8"), SRT)

    def test_external_source_or_corrected_change_blocks_overwrite(self):
        document = SubtitleDocument.load(self.video())
        document.edit_text(1, "修改")
        self.source.write_text(SRT.replace("第一条", "外部修改"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "源字幕已在外部变化"):
            document.save()
        self.source.write_text(SRT, encoding="utf-8")
        (self.folder / "成片_校对.srt").write_text("外部创建", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "校对字幕已在外部变化"):
            document.save()

    def test_saved_merge_and_timing_survive_text_edit(self):
        save_corrected_srt(
            self.source, [], merge_pairs=[{"first": 1, "second": 2}],
            time_overrides={"1": {"start": 0.5, "end": 3.5}},
        )
        document = SubtitleDocument.load(self.video())
        self.assertEqual([entry.source_indices for entry in document.entries], [(1, 2), (3,)])
        document.edit_text(1, "合并后修改")
        document.save()
        reloaded = SubtitleDocument.load(self.video())
        self.assertEqual(reloaded.entries[0].text, "合并后修改")
        self.assertEqual((reloaded.entries[0].start, reloaded.entries[0].end), (
            "00:00:00,500", "00:00:03,500"
        ))
        self.assertEqual(reloaded.entries[0].source_indices, (1, 2))

    def test_legacy_corrected_without_state_recovers_simple_edit(self):
        corrected = self.folder / "成片_校对.srt"
        corrected.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n旧版修改\n\n"
            "3\n00:00:04,000 --> 00:00:05,000\n第三条\n",
            encoding="utf-8",
        )
        document = SubtitleDocument.load(self.video())
        self.assertEqual([entry.index for entry in document.entries], [1, 3])
        self.assertEqual(document.entries[0].text, "旧版修改")

    def test_timeline_time_changes_undo_save_reload(self):
        document = SubtitleDocument.load(self.video())
        document.change_time(1, 0.5, 3.5)
        self.assertTrue(document.dirty)
        self.assertEqual((document.entries[0].start, document.entries[0].end),
                         ("00:00:00,500", "00:00:03,500"))
        self.assertTrue(document.undo())
        self.assertFalse(document.dirty)
        self.assertTrue(document.redo())
        self.assertTrue(document.dirty)

        before = tuple(document.entries)
        document.change_time(2, 3.0, 3.8, remember=False)
        document.commit_time_preview(before)
        self.assertTrue(document.undo())
        self.assertEqual(document.entries[1].start, "00:00:02,500")
        self.assertTrue(document.redo())
        self.assertEqual(document.entries[1].start, "00:00:03,000")
        output = document.save()
        cues = parse_srt_document(output)
        self.assertEqual((cues[0].start, cues[0].end, cues[1].start, cues[1].end),
                         ("00:00:00,500", "00:00:03,500", "00:00:03,000", "00:00:03,800"))
        reloaded = SubtitleDocument.load(self.video())
        self.assertEqual(reloaded.entries, document.entries)
        self.assertFalse(reloaded.dirty)

    def test_timeline_rejects_reversed_order_but_allows_overlap(self):
        document = SubtitleDocument.load(self.video())
        document.change_time(1, 1.0, 3.2)
        with self.assertRaisesRegex(ValueError, "下一条"):
            document.change_time(1, 3.0, 4.0)
        with self.assertRaisesRegex(ValueError, "晚于开始"):
            document.change_time(2, 3.0, 2.0)


if __name__ == "__main__":
    unittest.main()
