"""Desktop-08.7 选择、磁吸和波形预览契约。"""

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from autoslice.desktop.commands import CommandDispatcher
from autoslice.desktop.selection import CueSelection
from autoslice.desktop.snap import SnapEngine
from autoslice.desktop.subtitle_preview import SubtitlePreviewService
from autoslice.desktop.foundation import DesktopStorage


class DesktopInteractionTests(unittest.TestCase):
    def test_ctrl_shift_selection_and_active(self):
        model = CueSelection()
        order = [1, 2, 3, 4, 5]
        model.choose(2, order)
        model.choose(4, order, shift=True)
        self.assertEqual(model.selected, {2, 3, 4})
        model.choose(3, order, ctrl=True)
        self.assertEqual(model.selected, {2, 4})
        self.assertEqual(model.active, 3)

    def test_snap_uses_pixel_threshold_and_hysteresis(self):
        engine = SnapEngine(threshold_px=8, release_px=13)
        self.assertEqual(engine.snap(1.04, [1.0], 100), 1.0)
        self.assertEqual(engine.snap(1.11, [1.0], 100), 1.0)
        self.assertEqual(engine.snap(1.15, [1.0], 100), 1.15)
        self.assertEqual(engine.snap(1.04, [1.0], 100, bypass=True), 1.04)

    def test_quality_flags_are_lightweight(self):
        entries = [SimpleNamespace(index=1, start="00:00:00,000", end="00:00:00,300",
                                   text="x" * 40)]
        flags = SubtitlePreviewService.quality_flags(entries)
        self.assertIn("可能超出安全宽度", flags[1])
        self.assertIn("显示时间过短", flags[1])

    def test_preview_atomically_reuses_one_ass_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "视频.mp4"
            source.touch()
            service = SubtitlePreviewService(DesktopStorage(root / "app-data"))
            entries = [SimpleNamespace(index=1, start="00:00:00,000",
                                       end="00:00:01,000", text="原文")]

            first = service.render(source, entries)
            same = service.render(source, entries)
            entries[0].text = "修改后"
            changed = service.render(source, entries)

            self.assertEqual(first, same)
            self.assertEqual(first, changed)
            self.assertTrue(first.is_file())
            self.assertIn("preview.ass", first.name)
            self.assertIn("修改后", changed.read_text(encoding="utf-8"))
            self.assertFalse(list(first.parent.glob("*.tmp")))

    def test_preview_uses_detected_video_canvas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "video.mp4"
            source.touch()
            service = SubtitlePreviewService(
                DesktopStorage(root / "app-data"),
                probe_video=lambda _path: {"width": 1280, "height": 720},
            )
            entries = [SimpleNamespace(index=1, start="00:00:00,000",
                                       end="00:00:01,000", text="测试")]
            preview = service.render(source, entries)
            document = preview.read_text(encoding="utf-8")
            self.assertIn("PlayResX: 1280", document)
            self.assertIn("PlayResY: 720", document)

    def test_command_registry_keeps_stable_names(self):
        called = []
        commands = CommandDispatcher()
        commands.register("play_pause", lambda: called.append("play"))
        commands.register("trim_start_to_playhead", lambda: called.append("trim"))
        self.assertEqual(commands.names(), ("play_pause", "trim_start_to_playhead"))
        self.assertTrue(commands.dispatch("play_pause"))
        self.assertEqual(called, ["play"])


if __name__ == "__main__":
    unittest.main()
