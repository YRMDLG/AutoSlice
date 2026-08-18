import copy
import unittest
from datetime import datetime

from autoslice import timecode
from autoslice.analysis.manual import timebase


class ManualTimebaseTests(unittest.TestCase):
    def test_extracts_video_start_from_filename_and_parent_path(self):
        self.assertEqual(
            timebase.extract_video_start_datetime(r"F:\录播\主播-2026年8月15日-20点01分06秒.flv"),
            datetime(2026, 8, 15, 20, 1, 6),
        )
        self.assertEqual(
            timebase.extract_video_start_datetime(r"F:\录播\2026-08-15 20-01-06\part-01.flv"),
            datetime(2026, 8, 15, 20, 1, 6),
        )

    def test_converts_beijing_wall_clock_to_video_elapsed_seconds(self):
        video_start = datetime(2026, 8, 15, 23, 58, 30)
        lines = [
            "2026-08-15 23:58:00 至 2026-08-16 00:10:00 记录如下",
            "00:01:00 午夜话题⭐",
        ]

        entries = timebase.parse_manual_timeline_lines(lines, video_start)

        self.assertEqual(
            entries,
            [
                {
                    "start": 150,
                    "clock": "2026-08-16 00:01:00",
                    "text": "午夜话题",
                    "stars": 1,
                    "highlight": True,
                    "source": "manual_timeline",
                }
            ],
        )

    def test_parses_hms_and_explicit_video_elapsed_ranges(self):
        self.assertIs(timebase.parse_hms, timecode.parse_hms)
        self.assertEqual(timebase.parse_hms("1:02:03"), 3723)
        self.assertEqual(timebase.parse_hms("02:03"), 123)

        entries = timebase.parse_elapsed_timeline_report_lines(
            [
                "时间基准：视频内时间（播放进度）",
                "① [1:02:03 - 1:03:04] ⭐片段重点",
                "【投稿账号】片段重点的投稿标题",
            ]
        )

        self.assertEqual((entries[0]["start"], entries[0]["end"]), (3723, 3784))
        self.assertEqual(entries[0]["clock"], "视频内时间")
        self.assertTrue(entries[0]["explicit_range"])
        self.assertEqual(
            entries[0]["reference_publish_title"],
            "【投稿账号】片段重点的投稿标题",
        )

    def test_filters_entries_outside_segment_range_with_end_margin(self):
        entries = [
            {"start": -1, "text": "负时间"},
            {"start": 0, "text": "开头"},
            {"start": 120, "text": "结尾"},
            {"start": 135, "text": "容差内"},
            {"start": 136, "text": "容差外"},
        ]

        result = timebase.filter_manual_timeline_entries(entries, 120)

        self.assertEqual(
            [entry["text"] for entry in result],
            ["开头", "结尾", "容差内"],
        )

    def test_srt_alignment_returns_copies_without_mutating_source_entries(self):
        entries = [
            {
                "start": 300,
                "text": "草莓蛋糕烤糊后烤箱冒烟",
                "stars": 2,
                "metadata": {"origin": "manual"},
            }
        ]
        source = copy.deepcopy(entries)
        srt_segments = [
            (200, 220, "主播发现草莓蛋糕烤糊了，随后烤箱开始冒烟"),
        ]

        result = timebase.align_manual_timeline_entries_to_srt(
            entries,
            srt_segments,
        )

        self.assertEqual(entries, source)
        self.assertIsNot(result[0], entries[0])
        self.assertEqual(result[0]["original_start"], 300)
        self.assertEqual(result[0]["alignment_source"], "subtitle_fuzzy_match")
        self.assertNotEqual(result[0]["start"], entries[0]["start"])


if __name__ == "__main__":
    unittest.main()
