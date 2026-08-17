import re
import unittest

from autoslice.transcription import segments


class TranscriptionSegmentTests(unittest.TestCase):
    def test_final_text_keeps_comma_spacing_and_removes_other_punctuation(self):
        text = segments.normalise_asr_text(
            "只有三条SC，昨天没看到啊。真的没事吗？“不要怕！”"
        )

        self.assertEqual(text, "只有三条SC 昨天没看到啊真的没事吗不要怕")

    def test_punctuation_alignment_preserves_timestamp_count(self):
        timestamps = [[index * 100, (index + 1) * 100] for index in range(4)]

        tokens, aligned = segments.align_funasr_tokens(
            "只有SC吗？",
            timestamps,
            raw_text="只 有 SC 吗",
        )

        self.assertTrue(aligned)
        self.assertEqual(tokens, ["只", "有", "SC", "吗？"])
        self.assertEqual(len(tokens), len(timestamps))

    def test_unaligned_long_result_is_split_without_losing_text_or_time(self):
        source_text = "这是时间戳没有正确对齐时也必须拆开的超长字幕内容" * 3

        result = segments.segments_from_funasr_result(
            source_text,
            [[0, 6000], [6000, 12000]],
        )

        self.assertGreater(len(result), 1)
        self.assertEqual(result[0][0], 0.0)
        self.assertEqual(result[-1][1], 12.0)
        self.assertEqual("".join(item[2] for item in result), source_text)
        self.assertTrue(
            all(
                len(re.sub(r"\s+", "", item[2])) <= segments.SUBTITLE_MAX_CHARS
                for item in result
            )
        )
        self.assertTrue(
            all(later[0] >= earlier[1] for earlier, later in zip(result, result[1:]))
        )

    def test_sentence_punctuation_builds_short_readable_segments(self):
        timestamps = [[index * 500, (index + 1) * 500] for index in range(10)]

        result = segments.segments_from_funasr_result(
            "今天天气很好。我们出门！",
            timestamps,
            raw_text="今 天 天 气 很 好 我 们 出 门",
        )

        self.assertEqual([item[2] for item in result], ["今天天气很好", "我们出门"])

    def test_pre_context_tokens_are_trimmed_before_sentence_building(self):
        text, timestamps, aligned = segments.trim_funasr_tokens_to_core(
            "前 段 后 段",
            [
                [18000, 18500],
                [18500, 19000],
                [25000, 25500],
                [25500, 26000],
            ],
            input_start=100.0,
            core_start=120.0,
            core_end=240.0,
        )

        self.assertTrue(aligned)
        self.assertEqual(text, "后 段")
        self.assertEqual(timestamps, [[25000, 25500], [25500, 26000]])

    def test_boundary_dedupe_prefers_complete_overlapping_sentence(self):
        source = [
            (119.25, 119.98, "这个好像真"),
            (119.31, 122.09, "这个好像真是手套"),
            (130.0, 131.0, "下一句"),
            (131.0, 132.0, "一句"),
        ]

        result = segments.dedupe_overlapping_funasr_segments(source)

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], (119.25, 122.09, "这个好像真是手套"))
        self.assertEqual(result[1:], source[2:])

    def test_srt_timestamp_round_trip_keeps_milliseconds(self):
        rendered = segments.srt_time(3723.456)

        self.assertEqual(rendered, "01:02:03,456")
        self.assertAlmostEqual(segments.parse_srt_timestamp(rendered), 3723.456)


if __name__ == "__main__":
    unittest.main()
