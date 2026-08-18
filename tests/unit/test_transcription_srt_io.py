import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autoslice.transcription import srt_io


class TranscriptionSrtIoTests(unittest.TestCase):
    def test_read_entries_preserves_multiline_text_and_timestamps(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "recording.srt"
            path.write_text(
                "1\n00:00:01,250 --> 00:00:03,500\n第一行\n第二行\n\n",
                encoding="utf-8",
            )

            entries = srt_io.read_srt_entries(path)

        self.assertEqual(entries, [(1.25, 3.5, "第一行 第二行")])

    def test_repeated_legacy_block_is_rebuilt_from_timed_tokens(self):
        raw_text = (
            "今天晚上 天气真的 特别不错 我们准备 "
            "一起出去 公园散步 然后回来 早点休息"
        )
        blocks = []
        for index in range(8):
            blocks.append(
                f"{index + 1}\n"
                f"00:00:{index:02d},000 --> 00:00:{index + 1:02d},000\n"
                f"{raw_text}\n\n"
            )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "recording.srt"
            path.write_text("".join(blocks), encoding="utf-8")

            repaired = srt_io.load_repaired_srt_segments(path)

        self.assertGreater(len(repaired), 0)
        self.assertLess(len(repaired), 8)
        self.assertEqual(
            "".join(item[2].replace(" ", "") for item in repaired),
            raw_text.replace(" ", ""),
        )
        self.assertEqual(repaired[0][0], 0.0)
        self.assertEqual(repaired[-1][1], 8.0)

    def test_writer_filters_short_text_and_keeps_contiguous_indices(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "recording.srt"

            written = srt_io.write_srt_segments(
                path,
                [
                    (0.0, 0.5, "啊"),
                    (0.5, 1.5, "第一句"),
                    (1.5, 2.5, "第二句"),
                ],
                minimum_text_chars=2,
            )
            content = path.read_text(encoding="utf-8")

        self.assertEqual(written, 2)
        self.assertNotIn("\n啊\n", content)
        self.assertIn("1\n00:00:00,500 --> 00:00:01,500\n第一句", content)
        self.assertIn("2\n00:00:01,500 --> 00:00:02,500\n第二句", content)

    def test_corrected_export_is_atomic_and_cleans_failed_temporary_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            output = root / "corrected.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n测试字幕\n\n",
                encoding="utf-8",
            )
            output.write_text("旧文件", encoding="utf-8")

            with (
                patch.object(
                    srt_io.checkpoint_store,
                    "commit_file_atomically",
                    side_effect=OSError("replace failed"),
                ),
                self.assertRaises(OSError),
            ):
                srt_io.export_corrected_srt(source, output)

            content = output.read_text(encoding="utf-8")
            temporary_exists = os.path.exists(str(output) + ".tmp")

        self.assertEqual(content, "旧文件")
        self.assertFalse(temporary_exists)

    def test_corrected_export_does_not_overwrite_source_srt(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            original = "1\n00:00:00,000 --> 00:00:01,000\n测试，字幕。\n\n"
            source.write_text(original, encoding="utf-8")

            output = srt_io.export_corrected_srt(source)

            source_content = source.read_text(encoding="utf-8")
            output_content = Path(output).read_text(encoding="utf-8")

        self.assertEqual(source_content, original)
        self.assertIn("测试 字幕", output_content)
        self.assertNotIn("。", output_content)


if __name__ == "__main__":
    unittest.main()
