import copy
import unittest
from unittest.mock import patch

from autoslice.analysis.manual import workflow as manual_workflow


class ManualWorkflowTests(unittest.TestCase):
    def test_prompt_format_and_chunk_attachment_preserve_entries(self):
        entries = [
            {
                "start": 210,
                "end": 260,
                "clock": "2026-07-14 20:03:30",
                "text": "第二项",
                "summary": ["·第二项摘要"],
                "stars": 0,
            },
            {
                "start": 120,
                "end": 180,
                "clock": "2026-07-14 20:02:00",
                "text": "重点项",
                "summary": ["·第一点", "·第二点", "·忽略的第三点"],
                "stars": 2,
            },
        ]
        original_entries = copy.deepcopy(entries)
        chunks = [{"start": 100, "end": 220}, {"start": 1000, "end": 1100}]

        attached = manual_workflow.attach_manual_timeline_to_chunks(chunks, entries)

        self.assertIs(attached, chunks)
        self.assertEqual(entries, original_entries)
        self.assertEqual(
            chunks[0]["manual_timeline_info"].splitlines(),
            [
                "- [0:02:00-0:03:00 / 2026-07-14 20:02:00] ⭐⭐ 重点项 | 第一点；第二点",
                "- [0:03:30-0:04:20 / 2026-07-14 20:03:30] 第二项 | 第二项摘要",
            ],
        )
        self.assertEqual(chunks[1]["manual_timeline_info"], "无")

    def test_optimized_entries_preserve_topics_and_normalize_output(self):
        topics = [
            {
                "start": 100,
                "end": 180,
                "title": "袜子破洞引发吐槽",
                "publish_title": "袜子破了还怪洗衣机",
                "body": [
                    "·完整说明事情经过",
                    "·字幕核查：音音发现袜子破了",
                    "●人工时间轴⭐⭐：袜子破了",
                ],
                "manual_stars": 1,
                "manual_timeline": [
                    {
                        "start": 120,
                        "original_start": 125,
                        "clock": "2026-07-14 20:02:00",
                        "text": "袜子破了",
                        "stars": 2,
                        "alignment_score": 0.8,
                        "alignment_shift_sec": -5,
                    }
                ],
                "ai_enriched": True,
            }
        ]
        original_topics = copy.deepcopy(topics)

        entries = manual_workflow.optimized_manual_entries_from_topics(topics)

        self.assertEqual(topics, original_topics)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "袜子破洞引发吐槽")
        self.assertEqual(entries[0]["summary"], ["完整说明事情经过"])
        self.assertEqual(entries[0]["stars"], 2)
        self.assertEqual(entries[0]["source"], "optimized_manual_timeline")
        self.assertTrue(entries[0]["ai_enriched"])

    def test_failed_enrichment_keeps_topics_and_returns_warning(self):
        topics = [{"start": 10, "end": 80, "title": "原候选"}]
        original_topics = copy.deepcopy(topics)

        with patch(
            "autoslice.analysis.manual.review.enrich_manual_topics_with_llm",
            side_effect=RuntimeError("上游暂不可用"),
        ):
            warning = manual_workflow.try_enrich_manual_topics(topics)

        self.assertEqual(topics, original_topics)
        self.assertEqual(
            warning,
            "人工时间轴 AI 复核失败，已保留字幕/弹幕规则结果：上游暂不可用",
        )

    def test_retry_detection_warning_and_accepted_sorting_are_pure(self):
        accepted_late = {
            "start": 200,
            "end": 260,
            "text": "已通过候选二",
            "summary": ["完整事件二"],
            "ai_enriched": True,
            "reference_only": False,
        }
        accepted_early = {
            "start": 20,
            "end": 80,
            "text": "已通过候选一",
            "summary": ["完整事件一"],
            "ai_enriched": True,
            "reference_only": False,
        }
        entries = [accepted_late, accepted_early]
        original_entries = copy.deepcopy(entries)

        optimized, warning = manual_workflow.retry_optimized_timeline_entries(
            entries,
            srt_segments=[],
            peaks=[],
        )

        self.assertEqual(entries, original_entries)
        self.assertEqual([item["start"] for item in optimized], [20, 200])
        self.assertIsNone(warning)
        self.assertIsNot(optimized[0], accepted_early)
        self.assertTrue(
            manual_workflow.optimized_entry_needs_retry(
                {
                    "text": "5-15字具体短标题",
                    "summary": ["完整事件"],
                    "ai_enriched": True,
                }
            )
        )
        self.assertEqual(
            manual_workflow.batch_warning_text(
                ["第 1/2 批优化失败：超时"],
                pending_count=3,
            ),
            "人工时间轴部分未完成字幕校准，相关条目仅作低权重参考："
            "第 1/2 批优化失败：超时；尚有 3 项等待后续批次",
        )


if __name__ == "__main__":
    unittest.main()
