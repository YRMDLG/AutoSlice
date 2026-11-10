"""Desktop 字幕文档沿用旧 SRT 契约的回归测试。"""

from __future__ import annotations

import tempfile
import threading
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

    def test_split_merge_combinations_roundtrip_without_losing_text_or_time(self):
        for operation in ("split_merge", "merge_split", "split_retime", "repeated_split"):
            with self.subTest(operation=operation):
                document = SubtitleDocument.load(self.video())
                if operation == "split_merge":
                    new_index = document.split_at(1, 1.5, left_text="第一")
                    document.merge_adjacent({new_index, 2})
                elif operation == "merge_split":
                    document.merge_adjacent({1, 2})
                    document.split_at(1, 2.0, left_text="第一条")
                elif operation == "split_retime":
                    document.split_at(1, 1.5, left_text="第一")
                    document.change_time(1, .8, 1.5)
                else:
                    document.split_at(1, 1.7, left_text="第一")
                    document.split_at(1, 1.3, left_text="第")
                expected = list(document.entries)
                output = document.save()
                cues = parse_srt_document(output)
                self.assertEqual([(c.index, c.start, c.end, c.text) for c in cues],
                                 [(e.index, e.start, e.end, e.text) for e in expected])
                self.assertEqual(SubtitleDocument.load(self.video()).entries, expected)
                self.assertFalse(document.dirty)
                Path(output).unlink()
                self.source.with_name("成片_校对状态.json").unlink()

    def test_new_split_and_merge_drafts_restore_before_first_save(self):
        for operation in ("split", "merge", "mixed"):
            with self.subTest(operation=operation):
                document = SubtitleDocument.load(self.video())
                if operation == "merge":
                    document.merge_adjacent({1, 2})
                else:
                    new_index = document.split_at(1, 1.5)
                    if operation == "mixed":
                        document.edit_text(new_index, "后半句")
                        document.merge_adjacent({new_index, 2})
                reopened = SubtitleDocument.load(self.video())
                reopened.restore_draft(document.draft_payload())
                self.assertEqual(reopened.entries, document.entries)
                self.assertTrue(reopened.dirty)

    def test_draft_rejects_unknown_sources_duplicate_ids_and_reversed_time(self):
        document = SubtitleDocument.load(self.video())
        for field, value in (("source_indices", [999]), ("index", 2),
                             ("end", "00:00:00,500")):
            payload = document.draft_payload()
            payload["entries"][0][field] = value
            with self.assertRaises(ValueError):
                document.restore_draft(payload)

    def test_save_keeps_new_edits_dirty_when_document_changes_during_write(self):
        document = SubtitleDocument.load(self.video())
        document.edit_text(1, "保存开始时")
        entered, release = threading.Event(), threading.Event()
        failures = []
        def delayed_save(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("测试保存等待超时")
            return save_corrected_srt(*args, **kwargs)
        def save():
            try:
                document.save()
            except Exception as exc:
                failures.append(exc)
        with patch("autoslice.desktop.subtitles.save_corrected_srt", side_effect=delayed_save):
            worker = threading.Thread(target=save)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                document.edit_text(1, "保存期间的新修改")
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(failures)
        self.assertTrue(document.dirty)
        self.assertEqual(parse_srt_document(document.corrected_path)[0].text, "保存开始时")
        document.save()
        self.assertEqual(SubtitleDocument.load(self.video()).entries[0].text, "保存期间的新修改")

    def test_legacy_writer_cannot_overwrite_combined_desktop_edits(self):
        document = SubtitleDocument.load(self.video())
        new_index = document.split_at(1, 1.5, left_text="第一")
        document.merge_adjacent({new_index, 2})
        output = Path(document.save())
        before = output.read_bytes()
        with self.assertRaisesRegex(ValueError, "桌面端"):
            save_corrected_srt(self.source, [])
        self.assertEqual(output.read_bytes(), before)

    def test_deleting_one_split_part_and_editing_after_reopen_remains_saveable(self):
        document = SubtitleDocument.load(self.video())
        right = document.split_at(1, 1.5, left_text="第一")
        document.delete(right)
        document.save()
        reopened = SubtitleDocument.load(self.video())
        self.assertEqual(reopened.entries[0].text, "第一")
        reopened.edit_text(1, "再次校对")
        reopened.change_time(1, .8, 1.4)
        reopened.save()
        self.assertEqual(SubtitleDocument.load(self.video()).entries, reopened.entries)

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
