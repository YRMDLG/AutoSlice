"""识别后修整：边界不甩语气词、不拆常用词；起止贴到说话声，小缝接上。"""

from __future__ import annotations

import math
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from autoslice.desktop.asr.refine import fix_boundaries, snap_to_speech
from autoslice.desktop.asr.types import Word


def _words(start: float, end: float, text: str) -> list[Word]:
    step = (end - start) / len(text)
    return [Word(start + i * step, start + (i + 1) * step, char) for i, char in enumerate(text)]


def _wav(path: Path, spans: list[tuple[float, float]], seconds: float, *, noise: float = 0.002) -> None:
    rate = 16000
    frames = bytearray()
    for index in range(int(seconds * rate)):
        t = index / rate
        loud = any(a <= t < b for a, b in spans)
        value = (0.4 * math.sin(2 * math.pi * 220 * t) if loud else 0.0) + noise * math.sin(2 * math.pi * 3000 * t)
        frames += struct.pack("<h", int(max(-1, min(1, value)) * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))


class FixBoundariesTests(unittest.TestCase):
    def test_particle_at_the_head_goes_back_to_the_previous_cue(self):
        # 实测：“他们就觉得你在引导你 / 的粉丝投几号候选人”
        words = _words(0.0, 2.0, "他们就觉得你在引导你") + _words(2.0, 4.0, "的粉丝投几号候选人")
        fixed = fix_boundaries([(0.0, 2.0, "他们就觉得你在引导你"), (2.0, 4.0, "的粉丝投几号候选人")], words, max_chars=16)
        self.assertEqual([text for _s, _e, text in fixed], ["他们就觉得你在引导你的", "粉丝投几号候选人"])
        # 边界挪到“的”说完的时刻
        self.assertAlmostEqual(fixed[0][1], 2.0 + 2.0 / 9, places=3)
        self.assertEqual(fixed[0][1], fixed[1][0])

    def test_common_word_is_not_split(self):
        words = _words(0.0, 1.6, "他这个居") + _words(1.6, 3.0, "然能这样")
        fixed = fix_boundaries([(0.0, 1.6, "他这个居"), (1.6, 3.0, "然能这样")], words, max_chars=16)
        self.assertEqual([text for _s, _e, text in fixed], ["他这个居然", "能这样"])

    def test_real_pause_and_own_words_are_left_alone(self):
        segments = [(0.0, 1.0, "你说"), (1.8, 2.6, "啊这样也行")]
        self.assertEqual(fix_boundaries(segments, [], max_chars=16), segments)
        own = [(0.0, 1.0, "这个"), (1.0, 2.0, "的确不错")]
        self.assertEqual(fix_boundaries(own, [], max_chars=16), own)
        tail = fix_boundaries([(0.0, 1.0, "如果你不去"), (1.0, 2.0, "的话就算了")], [], max_chars=16)
        self.assertEqual([text for _s, _e, text in tail], ["如果你不去的话", "就算了"])

    def test_length_limit_is_kept(self):
        long = "一二三四五六七八九十一二三四五六七八"  # 18 字：再加就超过 16+2
        segments = [(0.0, 3.0, long), (3.0, 4.0, "的事情")]
        self.assertEqual(fix_boundaries(segments, [], max_chars=16), segments)


class SnapToSpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.wav = Path(self.temp.name) / "audio.wav"

    def test_edges_snap_to_voice_and_small_gaps_close(self):
        _wav(self.wav, [(1.0, 2.0), (2.5, 3.5)], 5.0)
        # 起点早了 0.3 秒、终点拖了 0.3 秒；第二句起点晚了、尾音被切
        snapped = snap_to_speech([(0.7, 2.3, "第一句"), (2.6, 3.3, "第二句")], self.wav)
        (s1, e1, _), (s2, e2, _) = snapped
        self.assertAlmostEqual(s1, 1.0 - 0.08, delta=0.03)
        self.assertAlmostEqual(s2, 2.5 - 0.08, delta=0.03)
        self.assertAlmostEqual(e2, 3.5 + 0.2, delta=0.03)
        # 两句之间只剩 0.2 秒：接上，不闪屏
        self.assertEqual(e1, s2)

    def test_flat_background_keeps_the_times(self):
        _wav(self.wav, [(0.0, 5.0)], 5.0)
        segments = [(1.0, 2.0, "第一句"), (3.0, 4.0, "第二句")]
        self.assertEqual(snap_to_speech(segments, self.wav), segments)

    def test_untouched_cues_say_why(self):
        from autoslice.desktop.asr.refine import NO_AUDIO, NO_PAUSE, TOO_LOUD

        # 背景声音铺满：分不清人声和停顿
        _wav(self.wav, [(0.0, 5.0)], 5.0)
        reasons = {}
        snap_to_speech([(1.0, 2.0, "一"), (3.0, 4.0, "二")], self.wav, reasons=reasons)
        self.assertEqual(reasons, {0: TOO_LOUD, 1: TOO_LOUD})
        # 两句之间话是连着说的：有安静的地方，但不在这两句的边界附近
        _wav(self.wav, [(1.0, 4.0)], 6.0)
        reasons = {}
        snapped = snap_to_speech([(1.5, 2.5, "一"), (2.5, 3.5, "二")], self.wav, reasons=reasons)
        self.assertEqual(snapped[0][1], 2.5)
        self.assertEqual(reasons.get(0), NO_PAUSE)
        # 没有音频
        reasons = {}
        snap_to_speech([(1.0, 2.0, "一")], Path(self.temp.name) / "没有.wav", reasons=reasons)
        self.assertEqual(reasons, {0: NO_AUDIO})

    def test_missing_audio_only_fixes_gaps_and_overlaps(self):
        snapped = snap_to_speech([(0.0, 1.0, "一"), (1.05, 2.0, "二"), (1.9, 3.0, "三")], self.wav)
        self.assertEqual(snapped[0][1], snapped[1][0])
        self.assertLessEqual(snapped[1][1], snapped[2][0])



try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - 没装 Qt 的环境跳过
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class SnapFromSubtitlePageTests(unittest.TestCase):
    """字幕页“全部字幕贴合声音”：已有字幕（旧识别结果）也能一键修，一步撤销。"""

    def setUp(self):
        import os
        import time
        from unittest.mock import patch

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from autoslice.desktop.foundation import DesktopStorage
        from autoslice.desktop.projects import SubmissionProjectService
        from autoslice.desktop.qt_app.window import DesktopWindow

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        folder = root / "项目"
        folder.mkdir()
        (folder / "视频.mp4").write_bytes(b"video")
        # 第一句拖进了静音、第二句尾音被切
        (folder / "视频.srt").write_text(
            "1\n00:00:00,700 --> 00:00:02,300\n第一句\n\n2\n00:00:02,600 --> 00:00:03,300\n第二句\n", encoding="utf-8")
        self.wav = root / "audio.wav"
        _wav(self.wav, [(1.0, 2.0), (2.5, 3.5)], 5.0)
        self.app = QApplication.instance() or QApplication([])
        with patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("测试无播放器")):
            self.window = DesktopWindow(SubmissionProjectService(root), DesktopStorage(root / "data"))
        self.window.show()
        self.addCleanup(lambda: (setattr(self.window, "_resolve_unsaved", lambda: True), self.window.close()))
        self.time = time
        self.wait_for(lambda: bool(self.window.project_buttons))
        self.window._select_real_project(next(p for p in self.window.service.snapshot.projects if p.title == "项目"))
        self.wait_for(lambda: self.window.document is not None)

    def wait_for(self, condition):
        deadline = self.time.monotonic() + 10
        while self.time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            self.time.sleep(0.01)
        self.fail(f"等待超时：{self.window.subtitle_status.text()} · {self.window.app_status.text()} · loading={self.window._loading}")

    def test_snap_all_cues_and_undo_in_one_step(self):
        from unittest.mock import patch

        from autoslice.transcription.contracts import srt_timestamp_seconds

        before = list(self.window.document.entries)
        with patch("autoslice.desktop.asr.refine.speech_audio", return_value=self.wav):
            self.window._snap_cues_to_speech(selected_only=False)
            self.wait_for(lambda: not self.window._speech_snapping)
        times = [(srt_timestamp_seconds(e.start), srt_timestamp_seconds(e.end)) for e in self.window.document.entries]
        self.assertAlmostEqual(times[0][0], 0.92, delta=0.03)
        self.assertAlmostEqual(times[1][1], 3.7, delta=0.03)
        self.assertEqual(times[0][1], times[1][0])
        self.assertIn("已按声音调整 2 条", self.window.app_status.text())
        self.window._undo()
        self.assertEqual(list(self.window.document.entries), before)

    def test_loud_background_is_explained_and_can_retry_with_music_turned_down(self):
        from unittest.mock import patch

        from PySide6.QtWidgets import QMessageBox

        loud = Path(self.temp.name) / "loud.wav"
        _wav(loud, [(0.0, 5.0)], 5.0)
        used = []

        def audio(_cache, _video, *, foreground=False):
            used.append(foreground)
            return self.wav if foreground else loud

        with patch("autoslice.desktop.asr.refine.speech_audio", side_effect=audio),                 patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as ask:
            self.window._snap_cues_to_speech(selected_only=False)
            self.wait_for(lambda: used == [False, True] and not self.window._speech_snapping)
        self.assertIn("2 条字幕附近背景声音太满", ask.call_args.args[2])
        # 压低背景音乐后的那份音频分得清：两句都调整了
        self.assertIn("已按声音调整 2 条", self.window.app_status.text())

if __name__ == "__main__":
    unittest.main()
