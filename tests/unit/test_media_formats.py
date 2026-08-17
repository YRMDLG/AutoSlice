from pathlib import Path
import unittest

from autoslice.media_formats import (
    MEDIA_FORMATS,
    SUPPORTED_VIDEO_EXTENSIONS,
    compatible_output_extensions,
    is_analyzable_video,
    is_scannable_video,
    is_sliceable_video,
    media_format_for,
    normalise_video_extension,
    preferred_output_extension,
    video_filename_stem,
)


class MediaFormatContractTests(unittest.TestCase):
    def test_capability_table_declares_all_supported_video_formats(self):
        expected = (".flv", ".mp4", ".mkv", ".mov", ".avi")

        self.assertEqual(SUPPORTED_VIDEO_EXTENSIONS, expected)
        self.assertEqual(tuple(MEDIA_FORMATS), expected)
        for extension in expected:
            with self.subTest(extension=extension):
                capability = MEDIA_FORMATS[extension]
                self.assertTrue(capability.can_scan)
                self.assertTrue(capability.can_analyze)
                self.assertTrue(capability.can_slice)
                self.assertEqual(capability.copy_output_extension, extension)

    def test_extension_and_capability_lookup_are_case_insensitive(self):
        cases = {
            ".FLV": ".flv",
            "recording.Mp4": ".mp4",
            r"X:\fixtures\直播录像.MKV": ".mkv",
            "/fixtures/直播录像.MoV": ".mov",
            Path("直播录像.AVI"): ".avi",
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalise_video_extension(value), expected)
                self.assertIs(media_format_for(value), MEDIA_FORMATS[expected])
                self.assertTrue(is_scannable_video(value))
                self.assertTrue(is_analyzable_video(value))
                self.assertTrue(is_sliceable_video(value))

    def test_unsupported_files_have_no_video_capabilities(self):
        for value in ("直播录像", "直播录像.", "字幕.srt", "封面.png"):
            with self.subTest(value=value):
                self.assertIsNone(media_format_for(value))
                self.assertFalse(is_scannable_video(value))
                self.assertFalse(is_analyzable_video(value))
                self.assertFalse(is_sliceable_video(value))

        with self.assertRaisesRegex(ValueError, "不支持的视频格式"):
            preferred_output_extension("字幕.srt")

    def test_filename_stem_only_strips_supported_video_suffixes(self):
        self.assertEqual(video_filename_stem(r"X:\录播\周三歌杂.MP4"), "周三歌杂")
        self.assertEqual(video_filename_stem("多点.标题.mKv"), "多点.标题")
        self.assertEqual(video_filename_stem("周三歌杂.srt"), "周三歌杂.srt")
        self.assertEqual(video_filename_stem("无后缀"), "无后缀")

    def test_output_policy_preserves_source_container_and_reads_legacy_flv(self):
        for extension in SUPPORTED_VIDEO_EXTENSIONS:
            source = f"直播录像{extension.upper()}"
            with self.subTest(extension=extension):
                self.assertEqual(preferred_output_extension(source), extension)
                compatible = compatible_output_extensions(source)
                self.assertEqual(compatible[0], extension)
                self.assertIn(".flv", compatible)
                self.assertEqual(len(compatible), len(set(compatible)))


if __name__ == "__main__":
    unittest.main()
