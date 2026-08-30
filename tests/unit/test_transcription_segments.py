import re
import unittest

from autoslice.transcription import segments


def _timed_characters(text, *, step=0.3, gaps_after=None):
    """构造不依赖媒体和 ASR 运行时的代表性字级时间戳。"""
    gaps_after = gaps_after or {}
    cursor = 0.0
    timed_tokens = []
    for index, char in enumerate(text):
        end = cursor + step
        timed_tokens.append((cursor, end, char))
        cursor = end + gaps_after.get(index, 0.0)
    return timed_tokens


REPRESENTATIVE_SHORT_CLIP_FIXTURES = (
    {
        "name": "target_gap",
        "tokens": _timed_characters(
            "今天先把这个问题讲清楚再继续说明一下",
            step=0.12,
            gaps_after={9: 0.2},
        ),
        "reason": segments.SubtitleBoundaryReason.TARGET_GAP,
    },
    {
        "name": "pause",
        "tokens": _timed_characters(
            "先说明白然后继续",
            step=0.12,
            gaps_after={4: 0.8},
        ),
        "reason": segments.SubtitleBoundaryReason.PAUSE,
    },
    {
        "name": "sentence_punctuation",
        "tokens": _timed_characters("今天天气很好。我们出门", step=0.2),
        "reason": segments.SubtitleBoundaryReason.SENTENCE_PUNCTUATION,
    },
    {
        "name": "target_duration",
        "tokens": _timed_characters("持续讲话没有任何停顿后面继续", step=0.5),
        "reason": segments.SubtitleBoundaryReason.TARGET_DURATION,
    },
    {
        "name": "max_duration",
        "tokens": _timed_characters("慢慢说", step=4.0),
        "reason": segments.SubtitleBoundaryReason.MAX_DURATION,
    },
    {
        "name": "max_chars",
        "tokens": _timed_characters("完全没有停顿标点仍要按字数强制拆开", step=0.05),
        "reason": segments.SubtitleBoundaryReason.MAX_CHARS,
    },
)


class TranscriptionSegmentTests(unittest.TestCase):
    def test_default_visible_subtitle_limit_accepts_sixteen_but_splits_seventeen(self):
        limit = segments.SUBTITLE_MAX_CHARS

        self.assertEqual(limit, 16)
        within_limit = "字" * limit
        self.assertEqual(segments.subtitle_text_size(within_limit), limit)
        self.assertEqual(
            segments.split_subtitle_text_for_display(within_limit),
            [within_limit],
        )

        spaced_within_limit = "字" * 8 + " " + "字" * 8
        self.assertEqual(segments.subtitle_text_size(spaced_within_limit), limit)
        self.assertEqual(
            segments.split_subtitle_text_for_display(spaced_within_limit),
            [spaced_within_limit],
        )

        over_limit = "字" * (limit + 1)
        parts = segments.split_subtitle_text_for_display(over_limit)
        self.assertEqual("".join(parts), over_limit)
        self.assertGreater(len(parts), 1)
        self.assertTrue(
            all(segments.subtitle_text_size(part) <= limit for part in parts)
        )

    def test_representative_short_clip_boundaries_expose_structured_reasons(self):
        for fixture in REPRESENTATIVE_SHORT_CLIP_FIXTURES:
            with self.subTest(fixture=fixture["name"]):
                trace = segments.segment_timed_tokens_with_trace(fixture["tokens"])

                self.assertGreater(len(trace.segments), 1)
                self.assertTrue(trace.boundaries)
                self.assertIsInstance(
                    trace.boundaries[0].reason,
                    segments.SubtitleBoundaryReason,
                )
                self.assertIs(trace.boundaries[0].reason, fixture["reason"])
                self.assertEqual(
                    segments.segment_timed_tokens(fixture["tokens"]),
                    list(trace.segments),
                )

    def test_tail_rebalance_keeps_phrase_whole_with_explicit_lower_limit(self):
        source_text = "我我这几天头已经越来越好看了，但是后面还有重点"

        trace = segments.segment_timed_tokens_with_trace(
            _timed_characters(source_text, step=0.3),
            max_chars=13,
        )
        texts = [item[2] for item in trace.segments]

        self.assertEqual(
            texts,
            ["我我这几天头已经", "越来越好看了", "但是后面还有重点"],
        )
        self.assertEqual(
            [boundary.reason for boundary in trace.boundaries],
            [
                segments.SubtitleBoundaryReason.MAX_CHARS,
                segments.SubtitleBoundaryReason.CLAUSE_PUNCTUATION,
            ],
        )
        self.assertEqual(
            "".join(re.sub(r"\s+", "", text) for text in texts),
            source_text.replace("，", ""),
        )
        self.assertFalse(any(re.match(r"^.{1,2}\s+", text) for text in texts))
        self.assertTrue(all(
            segments.subtitle_text_size(text) <= segments.SUBTITLE_MAX_CHARS
            for text in texts
        ))

    def test_natural_sentence_connectors_are_never_shifted_to_previous_cue(self):
        connectors = ("所以", "但是", "然后", "不过", "其实", "因为", "如果")
        for connector in connectors:
            with self.subTest(connector=connector):
                source_text = f"前面的内容已经说完了，{connector}后面继续说明"
                result = segments.segment_timed_tokens(
                    _timed_characters(source_text, step=0.25)
                )
                texts = [item[2] for item in result]

                connector_index = next(
                    index for index, text in enumerate(texts)
                    if text.startswith(connector)
                )
                self.assertGreater(connector_index, 0)
                self.assertFalse(texts[connector_index - 1].endswith(connector))
                self.assertEqual(
                    "".join(re.sub(r"\s+", "", text) for text in texts),
                    source_text.replace("，", ""),
                )

    def test_unaligned_long_cue_trace_records_proportional_text_limit_boundaries(self):
        source_text = "这是时间戳无法对齐时仍需连续拆分的超长字幕内容" * 3

        trace = segments.segments_from_funasr_result_with_trace(
            source_text,
            [[0, 6000], [6000, 12000]],
        )

        self.assertGreater(len(trace.segments), 1)
        self.assertEqual(trace.segments[0][0], 0.0)
        self.assertEqual(trace.segments[-1][1], 12.0)
        self.assertEqual("".join(item[2] for item in trace.segments), source_text)
        self.assertEqual(len(trace.boundaries), len(trace.segments) - 1)
        self.assertTrue(all(
            boundary.reason is segments.SubtitleBoundaryReason.UNALIGNED_TEXT_LIMIT
            for boundary in trace.boundaries
        ))
        self.assertTrue(all(
            later[0] == earlier[1]
            for earlier, later in zip(trace.segments, trace.segments[1:])
        ))
        self.assertTrue(all(
            segments.subtitle_text_size(text) <= segments.SUBTITLE_MAX_CHARS
            for _start, _end, text in trace.segments
        ))

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

    def test_srt_video_duration_uses_last_segment_and_handles_empty_input(self):
        self.assertIsNone(segments.srt_video_duration([]))
        self.assertEqual(
            segments.srt_video_duration(
                [(1.0, 3.5, "前"), (5.0, 9.25, "后")]
            ),
            9.25,
        )


if __name__ == "__main__":
    unittest.main()
