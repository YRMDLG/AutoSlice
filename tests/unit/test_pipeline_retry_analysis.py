import tempfile
import unittest
from pathlib import Path

from autoslice.pipeline_retry_analysis import prepare_retry_analysis_state


class PrepareRetryAnalysisStateTests(unittest.TestCase):

    def _run(self, payload, *, ass_path=None, ass_exists=False, peaks=None):
        calls = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "recording.flv"
            source_path = root / "recording.srt"
            corrected_path = root / "corrected.srt"
            video_path.write_bytes(b"")
            source_path.write_text("source", encoding="utf-8")
            if payload.get("corrected_srt_path") == str(corrected_path):
                corrected_path.write_text("corrected", encoding="utf-8")
            explicit_ass_path = root / "recording.ass"
            if ass_exists:
                explicit_ass_path.write_text("ass", encoding="utf-8")

            segments = [(1, 5, "一句")]
            parsed = []

            def parse(path):
                calls.append(("parse", path))
                parsed.append(path)
                return segments

            def analyze(path):
                calls.append(("analyze", path))
                return peaks or []

            def average(values):
                calls.append(("average", values))
                return 12.5

            def high_energy(values, average_density):
                calls.append(("high", values, average_density))
                return [(60, 80.0)] if values else []

            result = prepare_retry_analysis_state(
                video_path,
                ass_path,
                payload,
                parse_srt_segments=parse,
                analyze_danmaku=analyze,
                empty_danmaku_series=lambda: [],
                average_danmaku_density=average,
                high_energy_danmaku_peaks=high_energy,
            )
        return result, calls, parsed, explicit_ass_path

    def test_corrected_subtitle_has_priority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "recording.flv"
            source_path = root / "source.srt"
            corrected_path = root / "corrected.srt"
            video_path.write_bytes(b"")
            source_path.write_text("source", encoding="utf-8")
            corrected_path.write_text("corrected", encoding="utf-8")
            result = prepare_retry_analysis_state(
                video_path,
                None,
                {
                    "source_srt_path": str(source_path),
                    "corrected_srt_path": str(corrected_path),
                },
                parse_srt_segments=lambda path: [path],
                analyze_danmaku=lambda _path: [],
                empty_danmaku_series=list,
                average_danmaku_density=lambda _peaks: 0.0,
                high_energy_danmaku_peaks=lambda _peaks, _average: [],
            )

        self.assertEqual(result["srt_path"], str(corrected_path))
        self.assertEqual(result["srt_segments"], [str(corrected_path)])

    def test_source_subtitle_is_fallback_when_corrected_is_missing(self):
        result, calls, _parsed, _ass = self._run({
            "source_srt_path": None,
            "corrected_srt_path": "missing-corrected.srt",
        })

        self.assertEqual(result["srt_path"], calls[0][1])
        self.assertEqual(result["source_srt_path"], calls[0][1])
        self.assertEqual(result["corrected_srt_path"], "missing-corrected.srt")

    def test_missing_subtitle_keeps_original_error_text(self):
        with self.assertRaisesRegex(
                FileNotFoundError, "^复核字幕不存在: missing.srt$"):
            prepare_retry_analysis_state(
                "recording.flv",
                None,
                {"source_srt_path": "missing.srt"},
                parse_srt_segments=lambda _path: [],
                analyze_danmaku=lambda _path: [],
                empty_danmaku_series=list,
                average_danmaku_density=lambda _peaks: 0.0,
                high_energy_danmaku_peaks=lambda _peaks, _average: [],
            )

    def test_empty_subtitle_keeps_original_error_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "empty.srt"
            path.write_text("empty", encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError, "^复核字幕中没有有效句段$"):
                prepare_retry_analysis_state(
                    path.with_suffix(".flv"),
                    None,
                    {"source_srt_path": str(path)},
                    parse_srt_segments=lambda _path: [],
                    analyze_danmaku=lambda _path: [],
                    empty_danmaku_series=list,
                    average_danmaku_density=lambda _peaks: 0.0,
                    high_energy_danmaku_peaks=lambda _peaks, _average: [],
                )

    def test_no_danmaku_returns_empty_series_and_summary(self):
        result, calls, _parsed, _ass = self._run({"source_srt_path": None})

        self.assertEqual(result["peaks"], [])
        self.assertEqual(result["high_energy_peaks"], [])
        self.assertEqual(result["peak_info"], "无弹幕数据")
        self.assertEqual(
            [call[0] for call in calls], ["parse", "average", "high"]
        )

    def test_danmaku_peak_summary_uses_injected_analysis(self):
        result, calls, _parsed, ass_path = self._run(
            {"source_srt_path": None},
            ass_path=None,
            ass_exists=True,
            peaks=[(0, 10.0), (60, 80.0)],
        )

        self.assertEqual(result["ass_path"], str(ass_path))
        self.assertEqual(result["peaks"], [(0, 10.0), (60, 80.0)])
        self.assertEqual(result["avg_den"], 12.5)
        self.assertEqual(result["high_energy_peaks"], [(60, 80.0)])
        self.assertEqual(
            result["peak_info"],
            "弹幕密度 2 个滑动窗口, 独立高能峰值 1 个, 全场平均密度 12条/分钟",
        )
        self.assertEqual(calls[1][0], "analyze")


if __name__ == "__main__":
    unittest.main()
