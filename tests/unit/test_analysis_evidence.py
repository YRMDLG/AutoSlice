import unittest

from autoslice.analysis import danmaku, evidence


class AnalysisEvidenceTests(unittest.TestCase):
    def test_srt_summary_buckets_compacts_and_evenly_samples_windows(self):
        segments = [
            (0, 5, "第一 句"),
            (5, 10, "第一 句"),
            (31, 35, "第二句"),
            (61, 65, "第三句"),
            (91, 95, "第四句"),
        ]

        lines = evidence.topic_srt_summary_lines(
            0,
            100,
            segments,
            limit=3,
            bucket_sec=30,
        )

        self.assertEqual(len(lines), 3)
        self.assertIn("第一句", lines[0])
        self.assertIn("第三句", lines[1])
        self.assertIn("第四句", lines[2])
        self.assertNotIn("第一句第一句", lines[0])

    def test_srt_summary_limit_one_keeps_middle_window(self):
        lines = evidence.topic_srt_summary_lines(
            0,
            100,
            [(0, 1, "开头"), (40, 41, "中间"), (80, 81, "结尾")],
            limit=1,
            bucket_sec=30,
        )

        self.assertEqual(len(lines), 1)
        self.assertIn("中间", lines[0])

    def test_danmaku_reference_keeps_strong_spaced_peaks_in_time_order(self):
        window = danmaku.DANMAKU_WINDOW
        lines = evidence.topic_danmaku_reference_lines(
            0,
            window * 4,
            [
                (0, 10),
                (window / 2, 50),
                (window * 2, 30),
                (window * 4, 40),
            ],
            limit=3,
        )

        self.assertEqual(len(lines), 3)
        self.assertIn("50 条/分钟", lines[0])
        self.assertIn("30 条/分钟", lines[1])
        self.assertIn("40 条/分钟", lines[2])

    def test_topic_peak_matching_allows_one_sampling_step_at_edges(self):
        window = danmaku.DANMAKU_WINDOW
        step = danmaku.DANMAKU_WINDOW_STEP
        topic = {"start": 100, "end": 150}
        left_inside = 100 - step - window / 2
        right_inside = 150 + step - window / 2
        outside = right_inside + step + 0.1

        candidates = evidence.topic_peak_candidates(
            topic,
            [(left_inside, 10), (right_inside, 20), (outside, 30)],
        )

        self.assertEqual(candidates, [(left_inside, 10), (right_inside, 20)])


if __name__ == "__main__":
    unittest.main()
