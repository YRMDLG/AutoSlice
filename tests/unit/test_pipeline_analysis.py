import unittest

from autoslice.pipeline_analysis import prepare_pipeline_analysis


class PipelineAnalysisTests(unittest.TestCase):
    def test_high_energy_danmaku_and_srt_chunking_keep_progress_semantics(self):
        calls = []
        progress = []
        peaks = [(0, 10.0), (60, 80.0)]
        segments = [(1, 5, "第一句"), (70, 95, "第二句")]
        chunks = [{"start": 1, "end": 301, "text": "第一句"}]

        def analyze(source_path):
            calls.append(("analyze", source_path))
            return peaks

        def parse(path):
            calls.append(("parse", path))
            return segments

        def chunk(items, density_peaks):
            calls.append(("chunk", items, density_peaks))
            return chunks

        result = prepare_pipeline_analysis(
            "recording.flv",
            "danmaku.ass",
            "corrected.srt",
            progress_callback=lambda *args: progress.append(args),
            analyze_danmaku=analyze,
            empty_danmaku_series=lambda: [],
            average_danmaku_density=lambda values: 20.0,
            high_energy_danmaku_peaks=lambda values, average: [(60, 80.0)],
            parse_srt_text=parse,
            chunk_srt=chunk,
            probe_video_duration=lambda path: 321.0,
        )

        self.assertEqual(result["peaks"], peaks)
        self.assertEqual(result["avg_den"], 20.0)
        self.assertEqual(
            result["peak_info"],
            "弹幕密度 2 个滑动窗口, 独立高能峰值 1 个, 全场平均密度 20条/分钟",
        )
        self.assertIs(result["segs"], segments)
        self.assertIs(result["chunks"], chunks)
        self.assertEqual(result["srt_duration"], 95)
        self.assertEqual(result["probed_video_duration"], 321.0)
        self.assertEqual(result["video_duration"], 321.0)
        self.assertEqual(
            calls,
            [
                ("analyze", "danmaku.ass"),
                ("parse", "corrected.srt"),
                ("chunk", segments, peaks),
            ],
        )
        self.assertEqual(
            progress,
            [
                ("Step 2/5: 弹幕密度分析...", 15, 100),
                ("Step 3/5: SRT 分块中...", 20, 100),
            ],
        )

    def test_no_danmaku_keeps_empty_message_and_does_not_analyze(self):
        calls = []
        progress = []

        def analyze(_source_path):
            calls.append("analyze")
            raise AssertionError("无弹幕输入不应调用弹幕解析")

        result = prepare_pipeline_analysis(
            "recording.flv",
            None,
            "source.srt",
            progress_callback=lambda *args: progress.append(args),
            analyze_danmaku=analyze,
            empty_danmaku_series=lambda: [],
            average_danmaku_density=lambda values: 0.0,
            high_energy_danmaku_peaks=lambda *_args: calls.append("peaks"),
            parse_srt_text=lambda _path: [],
            chunk_srt=lambda _segments, _peaks: [],
            probe_video_duration=lambda _path: None,
        )

        self.assertEqual(result["peaks"], [])
        self.assertEqual(result["avg_den"], 0.0)
        self.assertEqual(result["peak_info"], "无弹幕数据")
        self.assertEqual(result["segs"], [])
        self.assertEqual(result["chunks"], [])
        self.assertIsNone(result["srt_duration"])
        self.assertIsNone(result["probed_video_duration"])
        self.assertIsNone(result["video_duration"])
        self.assertEqual(calls, [])
        self.assertEqual(
            progress,
            [
                ("Step 2/5: 弹幕密度分析...", 15, 100),
                ("Step 3/5: SRT 分块中...", 20, 100),
            ],
        )

    def test_srt_duration_is_fallback_when_probe_has_no_duration(self):
        segments = [(3, 8, "短句"), (20, 44, "末句")]

        result = prepare_pipeline_analysis(
            "recording.flv",
            "danmaku.ass",
            "source.srt",
            analyze_danmaku=lambda _path: [(0, 1)],
            empty_danmaku_series=list,
            average_danmaku_density=lambda _peaks: 1.0,
            high_energy_danmaku_peaks=lambda *_args: [],
            parse_srt_text=lambda _path: segments,
            chunk_srt=lambda _segments, _peaks: ["chunk"],
            probe_video_duration=lambda _path: None,
        )

        self.assertEqual(result["srt_duration"], 44)
        self.assertIsNone(result["probed_video_duration"])
        self.assertEqual(result["video_duration"], 44)

    def test_probed_video_duration_takes_priority_over_srt_duration(self):
        segments = [(3, 8, "短句"), (20, 44, "末句")]

        result = prepare_pipeline_analysis(
            "recording.flv",
            "danmaku.ass",
            "source.srt",
            analyze_danmaku=lambda _path: [(0, 1)],
            empty_danmaku_series=list,
            average_danmaku_density=lambda _peaks: 1.0,
            high_energy_danmaku_peaks=lambda *_args: [],
            parse_srt_text=lambda _path: segments,
            chunk_srt=lambda _segments, _peaks: ["chunk"],
            probe_video_duration=lambda _path: 99,
        )

        self.assertEqual(result["srt_duration"], 44)
        self.assertEqual(result["probed_video_duration"], 99)
        self.assertEqual(result["video_duration"], 99)


if __name__ == "__main__":
    unittest.main()
