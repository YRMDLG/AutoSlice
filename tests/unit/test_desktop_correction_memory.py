"""字幕纠错记忆：从保存的改动里学错词，两个视频都改过才写进本机词库。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoslice.desktop.correction_memory import CorrectionMemory, correction_pairs
from autoslice.desktop.foundation import DesktopStorage


class CorrectionPairTests(unittest.TestCase):
    def test_single_character_fix_keeps_one_character_of_context(self):
        self.assertEqual(correction_pairs("感谢音乐生们的礼物", "感谢音悦生们的礼物"), (("音乐生", "音悦生"),))

    def test_whole_word_fix_is_kept_as_is(self):
        self.assertEqual(correction_pairs("英英今天好开心", "音音今天好开心"), (("英英", "音音"),))

    def test_added_or_removed_words_and_punctuation_are_not_learned(self):
        self.assertEqual(correction_pairs("今天好开心", "今天真的好开心"), ())
        self.assertEqual(correction_pairs("嗯今天好开心", "今天好开心"), ())
        self.assertEqual(correction_pairs("今天好开心。", "今天好开心！"), ())

    def test_long_rewrites_are_not_learned(self):
        self.assertEqual(correction_pairs("这个东西我觉得不太行", "主播说这款耳机音质一般"), ())


class CorrectionMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.promoted = []
        self.demoted = []
        self.memory = CorrectionMemory(
            DesktopStorage(Path(self.temp.name)),
            promote=lambda *args: self.promoted.append(args),
            demote=lambda *args: self.demoted.append(args),
        )
        self.fix = [("感谢音乐生们的礼物", "感谢音悦生们的礼物")]

    def test_needs_two_different_videos_before_it_is_remembered(self):
        self.assertEqual(self.memory.learn("zeyin", "video-1", self.fix), ())
        # 同一个视频再存一次不算第二次。
        self.assertEqual(self.memory.learn("zeyin", "video-1", self.fix), ())
        self.assertEqual(self.memory.learn("zeyin", "video-2", self.fix), (("音乐生", "音悦生"),))
        self.assertEqual(self.promoted, [("zeyin", "音乐生", "音悦生")])
        # 已经记住的不重复写。
        self.memory.learn("zeyin", "video-3", self.fix)
        self.assertEqual(len(self.promoted), 1)
        (_profile, entry), = self.memory.entries("zeyin")
        self.assertTrue(entry.promoted)
        self.assertEqual(entry.videos, 3)

    def test_conflicting_mapping_is_not_written_and_reason_is_kept(self):
        def refuse(*_args):
            raise ValueError("错词“音乐生”已有固定纠错")

        memory = CorrectionMemory(DesktopStorage(Path(self.temp.name) / "other"), promote=refuse)
        memory.learn("zeyin", "video-1", self.fix)
        self.assertEqual(memory.learn("zeyin", "video-2", self.fix), ())
        (_profile, entry), = memory.entries()
        self.assertFalse(entry.promoted)
        self.assertIn("已有固定纠错", entry.note)

    def test_forget_removes_from_dictionary_and_never_relearns(self):
        self.memory.learn("zeyin", "video-1", self.fix)
        self.memory.learn("zeyin", "video-2", self.fix)
        self.memory.forget("zeyin", "音乐生", "音悦生")
        self.assertEqual(self.demoted, [("zeyin", "音乐生", "音悦生")])
        self.assertEqual(self.memory.entries(), ())
        self.memory.learn("zeyin", "video-4", self.fix)
        self.assertEqual(len(self.promoted), 1)

    def test_profiles_are_kept_apart(self):
        self.memory.learn("zeyin", "video-1", self.fix)
        self.memory.learn("other", "video-2", self.fix)
        self.assertEqual(self.promoted, [])
        self.assertEqual({profile for profile, _entry in self.memory.entries()}, {"zeyin", "other"})

    def test_kept_correct_words_in_other_videos_reduce_precision(self):
        fix = [("她的意思账号", "她的ins账号")]
        for number in range(10):
            self.memory.learn("zeyin", f"kept-{number}", [], final_text="这个词是什么意思")
        self.memory.learn("zeyin", "changed-1", fix, final_text="她的ins账号")
        self.memory.learn("zeyin", "changed-2", fix, final_text="她的ins账号")
        self.assertEqual(self.promoted, [])
        self.assertEqual(self.memory.replacements("zeyin"), ())
        (_, entry), = self.memory.entries()
        self.assertEqual(entry.kept, 10)
        self.assertAlmostEqual(entry.precision, 2 / 12)

    def test_default_learning_stays_advisory_and_resaving_replaces_old_evidence(self):
        memory = CorrectionMemory(DesktopStorage(Path(self.temp.name) / "advisory"))
        memory.learn("zeyin", "one", self.fix)
        memory.learn("zeyin", "two", self.fix)
        self.assertEqual(memory.replacements("zeyin"), (("音乐生", "音悦生"),))
        memory.learn("zeyin", "two", [], final_text="音乐生")
        self.assertEqual(memory.replacements("zeyin"), ())
        (_, entry), = memory.entries()
        self.assertEqual(entry.videos, 1)
        self.assertFalse(entry.promoted)

    def test_old_automatic_replacements_are_migrated_once(self):
        root = Path(self.temp.name) / "legacy"
        path = root / "learning" / "subtitle-corrections.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema_version": 1, "profiles": {"zeyin": {
            "意思\tins": {"wrong": "意思", "right": "ins", "videos": ["one", "two"], "promoted": True},
        }}}), encoding="utf-8")
        removed = []
        memory = CorrectionMemory(DesktopStorage(root), demote=lambda *args: removed.append(args))
        self.assertEqual(removed, [("zeyin", "意思", "ins")])
        self.assertEqual(memory.replacements("zeyin"), ())
        CorrectionMemory(DesktopStorage(root), demote=lambda *args: removed.append(args))
        self.assertEqual(len(removed), 1)


if __name__ == "__main__":
    unittest.main()
