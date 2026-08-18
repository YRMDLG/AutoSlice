import unittest
from unittest.mock import patch

from autoslice.analysis import chunking


class AnalysisChunkingTests(unittest.TestCase):
    def test_parse_srt_text_filters_only_visibly_too_short_cues(self):
        repaired = [
            (1.0, 2.0, "啊"),
            (2.0, 3.0, "好的"),
            (3.0, 4.0, "A B"),
        ]

        with patch(
            "autoslice.analysis.chunking.transcription_srt_io.load_repaired_srt_segments",
            return_value=repaired,
        ) as load:
            result = chunking.parse_srt_text("测试.srt")

        load.assert_called_once_with("测试.srt")
        self.assertEqual(result, repaired[1:])

    def test_chunk_srt_empty_input_does_not_analyze_danmaku(self):
        self.assertEqual(chunking.chunk_srt([], peaks=[]), [])

    def test_chunk_srt_supports_three_and_two_field_segments_and_splits_by_time(self):
        segments = [
            (0, 2, "第一句"),
            (5, "第二句"),
            (11, 14, "第三句"),
        ]

        chunks = chunking.chunk_srt(segments, peaks=[], chunk_sec=10)

        self.assertEqual(len(chunks), 2)
        self.assertEqual((chunks[0]["start"], chunks[0]["end"]), (0, 600))
        self.assertIn("[0:00:00－0:00:02] 第一句", chunks[0]["text"])
        self.assertIn("[0:00:05] 第二句", chunks[0]["text"])
        self.assertEqual((chunks[1]["start"], chunks[1]["end"]), (11, 611))
        self.assertEqual(chunks[1]["text"], "[0:00:11－0:00:14] 第三句")

    def test_make_chunk_summarizes_peak_ratio_and_has_peak_flag(self):
        result = chunking.make_chunk(
            100,
            ["[0:01:40] 测试字幕"],
            peaks=[(120, 80), (200, 40)],
            avg_density=20,
            independent_peaks=[],
        )

        self.assertEqual(result["start"], 100)
        self.assertEqual(result["end"], 700)
        self.assertTrue(result["has_peaks"])
        self.assertIn("峰值80条/分钟 = 4.0倍平均", result["danmaku_info"])

    def test_make_chunk_without_nearby_peak_uses_low_density_summary(self):
        result = chunking.make_chunk(
            100,
            ["[0:01:40] 测试字幕"],
            peaks=[(900, 40)],
            avg_density=20,
            independent_peaks=[],
        )

        self.assertFalse(result["has_peaks"])
        self.assertIn("本段无峰值", result["danmaku_info"])
        self.assertEqual(result["danmaku_evidence"], [])

    def test_make_chunk_keeps_top_four_evidence_rows_in_stable_score_order(self):
        independent_peaks = [
            (200, 50),
            (100, 40),
            (300, 30),
            (400, 20),
            (500, 10),
        ]
        result = chunking.make_chunk(
            0,
            ["[0:00:00] 测试字幕"],
            peaks=independent_peaks,
            avg_density=10,
            independent_peaks=independent_peaks,
        )
        expected_rows = []
        for peak_start, density in independent_peaks:
            features = chunking.danmaku_analysis._danmaku_peak_features(
                independent_peaks,
                peak_start,
                density,
                avg_density=10,
            )
            expected_rows.append(
                (
                    float(features["selection_score"]),
                    peak_start,
                    chunking.danmaku_analysis._danmaku_prompt_evidence(
                        features
                    ),
                )
            )
        expected_rows.sort(key=lambda row: (-row[0], row[1]))

        self.assertEqual(
            result["danmaku_evidence"],
            [row[2] for row in expected_rows[:4]],
        )


if __name__ == "__main__":
    unittest.main()
