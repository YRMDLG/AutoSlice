import unittest

from topic_engine import (_build_timeline_report, _dedupe_clip_marks, _expand_clip_marks_with_context, _parse_llm_response)


class TopicEngineParseTests(unittest.TestCase):
    """话题分析解析与去重的快速回归测试。"""

    def test_filter_prompt_example_outside_current_chunk_and_keep_body(self):
        response = """
[0:00:01-0:10:17] 奈雪漏奶茶&抽卡沉船 ✂️
●[0:00:01] 这是提示词里的旧示例，不应该进入当前块

[1:48:50-1:52:03] 疑惑汽车广告奇怪产品 ✂️
●[1:49:00] 主播看到奇怪汽车广告，反复吐槽产品定位
·弹幕跟着刷问号，当前块内确实有内容
"""

        blocks, marks = _parse_llm_response(response, 6530, 6830, [])

        self.assertEqual(len(blocks), 1)
        self.assertIn("疑惑汽车广告奇怪产品", blocks[0])
        self.assertIn("●[1:49:00]", blocks[0])
        self.assertNotIn("奈雪漏奶茶", blocks[0])
        self.assertEqual(
            marks,
            [{"start": 6530, "end": 6723, "title": "疑惑汽车广告奇怪产品"}],
        )

    def test_dedupe_same_range_even_when_title_changes(self):
        response = """
[2:24:30-2:25:05] 感谢英姐礼物&积分吐槽 ✂️ (因为1.1倍>平均)
●[2:24:30] 主播感谢礼物并吐槽积分
[2:24:30-2:25:05] 感谢英姐礼物&积分自嘲 ✂️
●[2:24:40] 同一段内容被模型换标题复述
"""

        blocks, marks = _parse_llm_response(response, 8670, 8970, [])

        self.assertEqual(len(blocks), 1)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["title"], "感谢英姐礼物&积分吐槽")

    def test_no_slice_hint_overrides_scissors_marker(self):
        response = """
[3:05:17-3:05:35] 🎮武士试炼任务重复播放 ✂️ (不切)
●[3:05:17] 只有游戏任务重复播放，明确不切
"""

        blocks, marks = _parse_llm_response(response, 11117, 11417, [])

        self.assertEqual(len(blocks), 1)
        self.assertIn("🎮武士试炼任务重复播放", blocks[0])
        self.assertNotIn("✂️", blocks[0])
        self.assertEqual(marks, [])




    def test_expand_clip_marks_keeps_video_time_basis_and_context(self):
        marks = [{"start": 100, "end": 110, "title": "短高能话题"}]
        srt_segments = [
            (0, 35, "前情说明"),
            (40, 60, "继续铺垫"),
            (95, 111, "高能点"),
            (200, 235, "后续反应"),
        ]

        expanded = _expand_clip_marks_with_context(marks, srt_segments=srt_segments, video_duration=300)

        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded[0]["topic_start"], 100)
        self.assertEqual(expanded[0]["topic_end"], 110)
        self.assertEqual(expanded[0]["start"], 0)
        self.assertEqual(expanded[0]["end"], 235)
        self.assertEqual(expanded[0]["time_basis"], "video_elapsed_seconds")
        self.assertTrue(expanded[0]["context_expanded"])

    def test_dedupe_uses_topic_range_not_expanded_overlap(self):
        marks = [
            {"start": 0, "end": 240, "topic_start": 100, "topic_end": 110, "title": "话题A"},
            {"start": 30, "end": 260, "topic_start": 200, "topic_end": 210, "title": "话题B"},
        ]

        deduped = _dedupe_clip_marks(marks)

        self.assertEqual([m["title"] for m in deduped], ["话题A", "话题B"])
    def test_filter_reasoning_body_and_placeholder_topics(self):
        topics = []
        response = """
[1:10:20－1:10:21]回顾十年前留言视频
·主播找到十年前手机里录给未来自己的视频
·但时间范围只有1:10:20-1:10:21，可能太短
·不要输出Markdown代码块
[2:55:13－3:00:13]无明显话题
[3:27:04－3:27:26]通过关卡六感谢开发团队
·主播恭喜观众通过关卡六，感谢神秘节奏组织
·等等。
·所以输出如下：
[4:03:01－4:08:01]话题标题
·要点
"""

        blocks, marks = _parse_llm_response(response, 4200, 14900, topics)
        report = _build_timeline_report("测试.flv", "弹幕峰值 2 个窗口", topics)

        self.assertEqual(marks, [])
        self.assertIn("回顾十年前留言视频", report)
        self.assertIn("通过关卡六感谢开发团队", report)
        self.assertIn("·主播找到十年前手机里录给未来自己的视频", report)
        self.assertIn("·主播恭喜观众通过关卡六", report)
        for dirty in ("但时间范围", "不要输出", "无明显话题", "话题标题", "·要点", "等等", "所以输出"):
            self.assertNotIn(dirty, report)
        self.assertEqual(len(blocks), 2)
    def test_timeline_report_uses_part_groups_and_body_lines(self):
        topics = []
        response = """
Part 1: 模型不该决定最终分组 (00:00－15:00)
①[0:00:00－0:04:00]开场问候与天气闲聊 ✂️
问好观众，聊这几天天气变热
- 分享下播后点了热卤吃
●感谢棉花糖和告白花束
②[0:16:00－0:20:00]毕业季话题
·有观众回学校参加毕业典礼
·聊毕业了应该开心
"""

        _, marks1 = _parse_llm_response(response, 0, 300, topics)
        _, marks2 = _parse_llm_response(response, 960, 1260, topics)
        report = _build_timeline_report("测试.flv", "弹幕峰值 2 个窗口", topics)

        self.assertIn("## 逐话题时间轴", report)
        self.assertIn("Part 1: 开场问候与天气闲聊", report)
        self.assertIn("①[00:00－04:00]开场问候与天气闲聊 ✂️", report)
        self.assertIn("·问好观众，聊这几天天气变热", report)
        self.assertIn("·分享下播后点了热卤吃", report)
        self.assertIn("●感谢棉花糖和告白花束", report)
        self.assertIn("Part 2: 毕业季话题", report)
        self.assertEqual(marks1, [{"start": 0, "end": 240, "title": "开场问候与天气闲聊"}])
        self.assertEqual(marks2, [])
    def test_dedupe_clip_marks_for_existing_json(self):
        marks = [
            {"start": 1, "end": 617, "title": "奈雪漏奶茶&抽卡沉船"},
            {"start": 1, "end": 617, "title": "奈雪漏奶茶&抽卡沉船"},
            {"start": 621, "end": 1250, "title": "日牌裙子价格惊吓&购物车考古"},
            {"start": 621, "end": 1250, "title": "日牌裙子价格惊吓"},
        ]

        deduped = _dedupe_clip_marks(marks)

        self.assertEqual(
            deduped,
            [
                {"start": 1, "end": 617, "title": "奈雪漏奶茶&抽卡沉船"},
                {"start": 621, "end": 1250, "title": "日牌裙子价格惊吓"},
            ],
        )


if __name__ == "__main__":
    unittest.main()



