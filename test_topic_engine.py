import unittest
from unittest.mock import patch

import requests

from topic_engine import (_build_chunk_prompt, _build_timeline_report, _call_llm_with_retry, _dedupe_clip_marks, _expand_clip_marks_with_context, _infer_streamer_name, _is_retryable_llm_error, _parse_llm_response, _streamer_report_name)

def make_http_error(status):
    response = requests.Response()
    response.status_code = status
    response._content = b"server busy"
    return requests.HTTPError(response=response)


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


    def test_call_llm_with_retry_retries_500_and_uses_compact_prompt(self):
        calls = []
        sleeps = []

        def fake_call(prompt, max_tokens):
            calls.append((prompt, max_tokens))
            if len(calls) < 3:
                raise make_http_error(500)
            return "OK"

        with patch("topic_engine.call_llm", side_effect=fake_call):
            result = _call_llm_with_retry(
                "完整提示",
                compact_prompt="紧凑提示",
                max_tokens=1500,
                compact_max_tokens=900,
                attempts=4,
                sleep_func=sleeps.append,
            )

        self.assertEqual(result, "OK")
        self.assertEqual(calls, [("完整提示", 1500), ("完整提示", 1500), ("紧凑提示", 900)])
        self.assertEqual(sleeps, [3, 8])


    def test_report_includes_api_warning_and_failed_chunks_without_topics(self):
        report = _build_timeline_report(
            "测试.flv",
            "弹幕峰值 0 个窗口",
            [],
            failed_chunks=[{"index": 1, "time": "0:00:48", "error": "HTTP 500"}],
            api_warning="HTTP 500",
        )

        self.assertIn("本次没有解析到有效话题。", report)
        self.assertIn("## API 预检警告", report)
        self.assertIn("HTTP 500", report)
        self.assertIn("## LLM 分块失败记录", report)
        self.assertIn("块 1 [0:00:48]", report)
    def test_call_llm_with_retry_does_not_retry_400(self):
        calls = []
        sleeps = []

        def fake_call(prompt, max_tokens):
            calls.append((prompt, max_tokens))
            raise make_http_error(400)

        with patch("topic_engine.call_llm", side_effect=fake_call):
            with self.assertRaises(requests.HTTPError):
                _call_llm_with_retry(
                    "完整提示",
                    compact_prompt="紧凑提示",
                    attempts=4,
                    sleep_func=sleeps.append,
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        self.assertFalse(_is_retryable_llm_error(make_http_error(400)))
        self.assertTrue(_is_retryable_llm_error(make_http_error(500)))
    def test_clean_title_and_body_residual_model_notes(self):
        topics = []
        response = """
[0:49:52－0:49:59]宣布痔疮家族传统 ？但时间太短。最好合并。 ✂️
·例如：
·主播（或游戏）提到“志士一族”通过痔疮品质决定家族地位。
·由于弹幕密度远低于平均，不加✂️。
·所以输出话题。
·要点要写具体。
·再看弹幕信息：峰值132条/分钟。
[3:42:53－3:43:07]主播抱怨游戏重复关卡
·主播反复说不想玩了，因为游戏一直重复，手按痛了。
·由于弹幕密度远低于平均，不加✂️。
·所以整理信息：
·主播先提到觉得猫更可爱，然后说游戏重复、按手痛、不想玩。
"""

        blocks, marks = _parse_llm_response(response, 2900, 13600, topics)
        report = _build_timeline_report("测试.flv", "弹幕峰值 2 个窗口", topics)

        self.assertEqual(marks[0]["title"], "宣布痔疮家族传统")
        self.assertIn("①[49:52－49:59]宣布痔疮家族传统 ✂️", report)
        self.assertIn("主播反复说不想玩了", report)
        self.assertIn("主播先提到觉得猫更可爱", report)
        for dirty in ("但时间太短", "最好合并", "例如", "由于弹幕密度", "所以输出", "要点要写", "再看弹幕信息", "所以整理信息"):
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

    def test_filter_latest_report_residual_notes_and_fragments(self):
        topics = []
        response = """
[0:54:56－0:55:11]吐槽USB接口比喻 ✂️
·主播评论一个视频广告，吐槽其作者精神状态
·我们还需要考虑其他可能性：也许从0:55:11到0:59:56
·但弹幕信息给了峰值59条/分钟，低于平均，可以提一句弹幕反应不活跃。
·由于字幕重复且卡顿，可能这是一个主播在模仿什么或口误。
·我们尽量简洁。
·所以只有一个话题。
[1:38:02－1:43:02]强调翡翠需要故事包装
·主播认为到了这个阶段必须开始讲故事、包装，不能太直白
·输出
[3:53:01－3:58:01]讨论咖啡加盟被割韭菜
·主播与连麦者对话，提到加盟咖啡品牌，投资八十多万
·（没有弹幕爆点信息）
·最后，如果无明显话题，输出“无明显话题”。但这里有明显话题。
[4:08:01－4:13:01]批评代理选择不当
·主播指责对方没有拿到区县代理，仅获得市级代理
●弹幕反应平静，无爆点
·根据格式，如果有礼物、弹幕爆点、观众金句才用●，如果没有就不写。
·所以不用写●。
·但
[3:05:17－3:10:17]游戏过关武士变身
·泽音Melody表示关卡终于过关，庆幸及时关闭否则会被折磨
·而且我们只能写基于字幕的，不要编造。
·但标题可以更简洁。
[3:10:18－3:11:16]泽音Melody大笑复读“没喊出来”
·泽音Melody持续大笑，反复说“没喊出来”
·但优先简洁。
·现在写。
·我决定输出一个话题。
[3:16:01－3:16:55]游戏角色武士闯关失败
·吐槽“没点刀法法基本功”
·另外，注意起始时间
●弹幕高密度，观众对泽音Melody遭遇反应活跃
"""
        blocks, marks = _parse_llm_response(response, 3200, 15200, topics)
        report = _build_timeline_report("测试.flv", "弹幕峰值 2 个窗口", topics)

        self.assertEqual(marks, [{"start": 3296, "end": 3311, "title": "吐槽USB接口比喻"}])
        self.assertEqual(len(blocks), 7)
        self.assertIn("·主播评论一个视频广告，吐槽其作者精神状态", report)
        self.assertIn("·主播认为到了这个阶段必须开始讲故事、包装，不能太直白", report)
        self.assertIn("·主播与连麦者对话，提到加盟咖啡品牌，投资八十多万", report)
        self.assertIn("·主播指责对方没有拿到区县代理，仅获得市级代理", report)
        self.assertIn("·泽音Melody表示关卡终于过关", report)
        self.assertIn("·泽音Melody持续大笑", report)
        for dirty in (
            "内容要点", "我们还需要考虑", "其他可能性", "弹幕信息", "峰值59", "可以提一句",
            "由于字幕", "我们尽量简洁", "所以只有一个话题", "·输出", "没有弹幕爆点",
            "如果无明显话题", "根据格式", "如果有礼物", "才用●", "不用写", "无爆点", "·但",
            "只能写基于字幕", "标题可以", "优先简洁", "现在写", "我决定", "注意起始时间",
            "弹幕高密度", "反应活跃",
        ):
            self.assertNotIn(dirty, report)

    def test_keep_concrete_danmaku_and_gift_lines(self):
        topics = []
        response = """
[0:31:19－0:31:33]分享分类TXT与弹幕互动 ✂️
·主播收到分类好的TXT文件，称赞对方贴心且不用网盘
●收到独角兽文班样购买的出道礼物
●弹幕要求直播读文
●弹幕高能，密度达119条/分钟，观众积极互动
"""
        _parse_llm_response(response, 1800, 2000, topics)
        report = _build_timeline_report("测试.flv", "弹幕峰值 2 个窗口", topics)

        self.assertIn("●收到独角兽文班样购买的出道礼物", report)
        self.assertIn("●弹幕要求直播读文", report)
        self.assertNotIn("密度达119", report)

    def test_report_replaces_generic_streamer_role_with_fan_nickname(self):
        topics = []
        response = """
[0:10:00－0:11:00]主播聊出差
·主播提到从上海回来后作息变正常
·观众问主播明天是否直播
"""
        _parse_llm_response(response, 590, 700, topics)
        report = _build_timeline_report(
            "测试.flv",
            "无弹幕数据",
            topics,
            streamer_name="泽音Melody",
        )

        self.assertIn("音音聊出差", report)
        self.assertIn("·音音提到从上海回来后作息变正常", report)
        self.assertIn("观众问音音明天是否直播", report)
        self.assertNotIn("泽音Melody", report)
        self.assertNotIn("主播", report)

    def test_infer_streamer_name_from_recording_path(self):
        path = r"F:\001\1947277414-泽音Melody\2026年\07月\05号\测试.flv"

        self.assertEqual(_infer_streamer_name(path), "泽音Melody")
        self.assertEqual(_streamer_report_name("泽音Melody"), "音音")
        self.assertEqual(_infer_streamer_name(r"F:\Videos\测试.flv"), "主播")

    def test_chunk_prompt_requests_full_timeline_and_fan_aliases(self):
        prompt, _, _ = _build_chunk_prompt(
            {"start": 0, "end": 300, "text": "[0:00:01] 测试", "danmaku_info": "无弹幕"},
            0,
            1,
            streamer_name="音音",
        )

        self.assertIn("全程时间轴，不是只挑爆点", prompt)
        self.assertIn("普通聊天、过渡、游戏过程、读弹幕、感谢礼物也要写进时间轴", prompt)
        self.assertIn("主播展示称呼: 音音", prompt)
        self.assertIn("音姐、麻麻、音音", prompt)

    def test_expand_context_includes_sc_or_gift_trigger_before_topic(self):
        marks = [{"start": 200, "end": 220, "title": "回答观众提问"}]
        srt_segments = [
            (20, 30, "普通铺垫闲聊"),
            (65, 72, "谢谢小明的醒目留言"),
            (73, 84, "他说最近工作压力很大怎么办"),
            (190, 225, "针对这个问题展开认真讨论"),
            (300, 320, "后续总结"),
        ]

        expanded = _expand_clip_marks_with_context(marks, srt_segments=srt_segments, video_duration=400)

        self.assertEqual(expanded[0]["topic_start"], 200)
        self.assertEqual(expanded[0]["topic_end"], 220)
        self.assertEqual(expanded[0]["start"], 65)
        self.assertGreaterEqual(expanded[0]["end"], 320)

    def test_expand_context_handles_sc_word_misrecognized_as_thanks_gift(self):
        marks = [{"start": 260, "end": 280, "title": "讨论观众问题"}]
        srt_segments = [
            (100, 110, "感谢阿月老板送的礼物"),
            (111, 124, "他问如果毕业后很迷茫该怎么办"),
            (250, 285, "泽音开始回答这个问题"),
        ]

        expanded = _expand_clip_marks_with_context(marks, srt_segments=srt_segments, video_duration=360)

        self.assertEqual(expanded[0]["start"], 100)


if __name__ == "__main__":
    unittest.main()






