import json
import tempfile
import unittest
from pathlib import Path

from subtitle_workflow import (
    high_confidence_corrections,
    parse_srt_document,
    save_corrected_srt,
    scan_submission_pairs,
    serialise_srt,
    suggest_subtitle_corrections,
)


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:02,500 position:50%
音音晚上好

2
00:00:02,500 --> 00:00:05,000
我看到一个瓦衣
是兔女郎的瓦衣

3
00:00:05,000 --> 00:00:07,000
这个娃衣很特别
"""


class SubtitleParsingAndReviewTests(unittest.TestCase):
    def test_parse_and_serialise_preserve_multiline_timeline_and_settings(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "字幕.srt"
            path.write_text(SAMPLE_SRT, encoding="utf-8")
            cues = parse_srt_document(path)

        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[1].text, "我看到一个瓦衣\n是兔女郎的瓦衣")
        self.assertEqual(cues[0].settings, " position:50%")
        rebuilt = serialise_srt(cues)
        self.assertIn("00:00:01,000 --> 00:00:02,500 position:50%", rebuilt)
        self.assertIn("我看到一个瓦衣\n是兔女郎的瓦衣", rebuilt)

    def test_gb18030_srt_is_supported(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "字幕.srt"
            path.write_bytes(SAMPLE_SRT.encode("gb18030"))
            cues = parse_srt_document(path)
        self.assertEqual(cues[0].text, "音音晚上好")

    def test_invalid_or_reverse_timeline_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "字幕.srt"
            path.write_text(
                "1\n00:00:03,000 --> 00:00:02,000\n测试\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "结束时间"):
                parse_srt_document(path)

    def test_save_corrected_srt_keeps_source_and_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            source_before = source.read_bytes()
            output = save_corrected_srt(
                source,
                [{
                    "index": 2,
                    "original": "我看到一个瓦衣\n是兔女郎的瓦衣",
                    "corrected": "我看到一个娃衣\n是兔女郎的娃衣",
                }],
            )
            corrected = Path(output).read_text(encoding="utf-8")

            self.assertEqual(source.read_bytes(), source_before)
            self.assertIn("我看到一个娃衣\n是兔女郎的娃衣", corrected)
            self.assertIn("00:00:02,500 --> 00:00:05,000", corrected)
            self.assertTrue(output.endswith("_校对.srt"))

    def test_save_rejects_stale_original_and_unknown_index(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "原文已变化"):
                save_corrected_srt(
                    source,
                    [{"index": 1, "original": "旧原文", "corrected": "新文字"}],
                )
            with self.assertRaisesRegex(ValueError, "序号不存在"):
                save_corrected_srt(source, [{"index": 99, "corrected": "新文字"}])

    def test_scan_pairs_different_jianying_names_and_ignores_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            clip = root / "投稿标题"
            clip.mkdir()
            (clip / "7月16日 (1).mp4").write_bytes(b"video")
            (clip / "7月16日 (2).srt").write_text(SAMPLE_SRT, encoding="utf-8")
            (clip / "7月16日 (1)_字幕版.mp4").write_bytes(b"output")
            (clip / "7月16日 (2)_校对.srt").write_text(SAMPLE_SRT, encoding="utf-8")

            pairs = scan_submission_pairs(root)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["title"], "投稿标题")
        self.assertEqual(pairs[0]["cue_count"], 3)
        self.assertTrue(pairs[0]["video_path"].endswith("7月16日 (1).mp4"))

    def test_review_retries_incomplete_batch_filters_rewrite_and_caches(self):
        calls = []

        def fake_runner(prompt, compact_prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {"reviewed_indices": [1, 2], "corrections": []}
            return {
                "reviewed_indices": [1, 2, 3],
                "corrections": [
                    {
                        "index": 1,
                        "original": "音音晚上好",
                        "corrected": "音音，晚上好！",
                        "reason": "只改标点",
                        "confidence": 0.99,
                    },
                    {
                        "index": 2,
                        "original": "我看到一个瓦衣\n是兔女郎的瓦衣",
                        "corrected": "我看到一个娃衣\n是兔女郎的娃衣",
                        "reason": "结合后一句‘娃衣’确认同音误识别",
                        "confidence": 0.97,
                    },
                    {
                        "index": 3,
                        "original": "这个娃衣很特别",
                        "corrected": "她觉得这一套兔女郎服装很独特",
                        "reason": "润色",
                        "confidence": 0.92,
                    },
                ],
            }

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            result = suggest_subtitle_corrections(
                source,
                context_title="兔女郎娃衣",
                llm_runner=fake_runner,
            )
            cached = suggest_subtitle_corrections(
                source,
                context_title="兔女郎娃衣",
                llm_runner=lambda *_: self.fail("命中缓存后不应调用 AI"),
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual([item["index"] for item in result["suggestions"]], [2])
        self.assertEqual(result["suggestions"][0]["corrected"], "我看到一个娃衣\n是兔女郎的娃衣")
        self.assertFalse(result["cache_hit"])
        self.assertTrue(cached["cache_hit"])

    def test_review_cache_invalidates_when_source_changes(self):
        calls = []

        def runner(prompt, compact_prompt):
            calls.append(1)
            indices = json.loads(prompt.split("待检查序号：", 1)[1].split("\n", 1)[0])
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            suggest_subtitle_corrections(source, llm_runner=runner)
            source.write_text(SAMPLE_SRT.replace("很特别", "非常特别"), encoding="utf-8")
            suggest_subtitle_corrections(source, llm_runner=runner)

        self.assertEqual(len(calls), 2)

    def test_high_confidence_only_selects_default_safe_items(self):
        selected = high_confidence_corrections({
            "suggestions": [
                {"index": 1, "confidence": 0.91},
                {"index": 2, "confidence": 0.72},
            ]
        })
        self.assertEqual([item["index"] for item in selected], [1])


if __name__ == "__main__":
    unittest.main()
