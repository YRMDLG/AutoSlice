import json
import unittest

from autoslice.task_results import (
    build_pipeline_result_summary,
    normalize_task_result,
)


class TaskResultSummaryTests(unittest.TestCase):
    def test_normalize_task_result_decodes_json_but_preserves_plain_text(self):
        self.assertEqual(normalize_task_result('{"count":2}'), {"count": 2})
        self.assertEqual(normalize_task_result("普通完成文本"), "普通完成文本")
        marker = {"already": "decoded"}
        self.assertIs(normalize_task_result(marker), marker)

    def test_pipeline_summary_excludes_large_payloads_and_stays_small(self):
        result = {
            "report": "完整报告" * 50_000,
            "topic_count": 58,
            "clip_marks": [
                {"start": index, "end": index + 30, "subtitle": "字幕" * 500}
                for index in range(12)
            ],
            "analysis_topics": [{"body": "话题" * 10_000}],
            "failed_chunks": [{"index": 3}, {"index": 9}],
            "artifact_dir": r"F:\output\直播_自动切片",
            "overview_path": r"F:\output\直播_自动切片\00_概览.md",
            "slice_dir": r"F:\output\直播_话题切片",
            "json_path": r"F:\output\直播_自动切片\数据\clip_marks.json",
            "md_path": r"F:\output\直播_自动切片\01_话题分析.md",
            "srt_path": r"F:\output\直播.srt",
            "api_precheck_warning": "临时 warning" * 1000,
        }

        summary = build_pipeline_result_summary(result)
        encoded = json.dumps(summary, ensure_ascii=False).encode("utf-8")

        self.assertEqual(summary["topic_count"], 58)
        self.assertEqual(summary["slice_count"], 12)
        self.assertEqual(summary["failed_chunk_count"], 2)
        self.assertTrue(summary["report_available"])
        self.assertNotIn("report", summary)
        self.assertNotIn("clip_marks", summary)
        self.assertNotIn("analysis_topics", summary)
        self.assertNotIn("subtitle", json.dumps(summary, ensure_ascii=False))
        self.assertLess(len(encoded), 64 * 1024)
        self.assertLessEqual(len(summary["api_precheck_warning"]), 2000)

    def test_explicit_slice_count_and_paths_are_preserved(self):
        summary = build_pipeline_result_summary({
            "topic_count": "3",
            "slice_count": "2",
            "clip_marks": [{"start": 1}],
            "artifact_dir": "artifact",
            "overview_path": "overview.md",
            "slice_dir": "clips",
            "json_path": "marks.json",
            "md_path": "report.md",
            "srt_path": "source.srt",
        })

        self.assertEqual(summary["topic_count"], 3)
        self.assertEqual(summary["slice_count"], 2)
        self.assertEqual(summary["artifact_dir"], "artifact")
        self.assertEqual(summary["overview_path"], "overview.md")
        self.assertEqual(summary["slice_dir"], "clips")


if __name__ == "__main__":
    unittest.main()
