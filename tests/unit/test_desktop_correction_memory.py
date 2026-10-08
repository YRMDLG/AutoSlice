"""字幕纠错记忆：从保存的改动里学错词，两个视频都改过才写进本机词库。"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
