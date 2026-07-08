import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from topic_engine import (CHUNK_SEC, LLM_FULL_TEXT_CHARS, LLM_MAX_TOKENS, _apply_danmaku_slice_decisions, _build_chunk_prompt, _build_timeline_report, _call_llm_with_retry, _clip_marks_from_topics, _dedupe_clip_marks, _expand_clip_marks_with_context, _infer_streamer_name, _is_retryable_llm_error, _make_fallback_topic_from_chunk, _parse_llm_response, _streamer_report_name, chunk_srt, parse_srt_text)

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

    def test_hourly_report_groups_key_points_by_video_hour(self):
        topics = [
            {"start": 120, "end": 300, "title": "开场聊天", "can_slice": False, "body": ["·音音开场聊天"]},
            {"start": 1800, "end": 2100, "title": "生日企划", "can_slice": True, "body": ["·展示生日企划"]},
            {"start": 3900, "end": 4200, "title": "视频评论", "can_slice": False, "body": ["·看视频评论"]},
        ]

        report = _build_timeline_report(
            "测试.flv",
            "弹幕峰值 2 个窗口",
            topics,
            streamer_name="音音",
            group_by_hour=True,
        )

        self.assertIn("Part 1: 第1小时重点", report)
        self.assertIn("Part 2: 第2小时重点", report)
        self.assertIn("②[30:00－35:00]生日企划 ✂️", report)

    def test_danmaku_density_selects_cuttable_key_points(self):
        topics = [
            {"start": 100, "end": 220, "title": "低密度聊天", "can_slice": True, "body": ["·普通聊天"]},
            {"start": 1000, "end": 1120, "title": "高密度生日企划", "can_slice": False, "body": ["·生日企划"]},
            {"start": 2000, "end": 2120, "title": "兜底高密度", "can_slice": False, "fallback": True, "body": ["·兜底"]},
            {"start": 3000, "end": 3120, "title": "游戏开头动画/背景语音", "can_slice": False, "body": ["·音音未发言，仅播放游戏画面/语音"]},
        ]
        peaks = [(120, 60), (1020, 130), (2020, 150), (3020, 180)]

        _apply_danmaku_slice_decisions(topics, peaks, avg_density=80)
        marks = _clip_marks_from_topics(topics)

        self.assertFalse(topics[0]["can_slice"])
        self.assertTrue(topics[1]["can_slice"])
        self.assertFalse(topics[2]["can_slice"])
        self.assertFalse(topics[3]["can_slice"])
        self.assertEqual(marks, [{"start": 1000, "end": 1120, "title": "高密度生日企划"}])

    def test_filter_current_report_draft_noise(self):
        topics = []
        response = """
[1:08:00－1:10:20]考虑分成以下话题
·考虑分成以下话题：
·3. 高中时期经历与抄袭争议（1:04:00-1:06:44）
·更好的方式：按时间顺序整理出核心话题。
[3:21:18－3:22:19]我们仔细分析每个时间段的字幕内容
·我们仔细分析每个时间段的字幕内容，提取可理解的讲话。
·3:14:01-3:16:08：“可以唱哈哈哈哈”这里明显：音音说“可以唱”“练习哼”。
·考虑输出两个话题：
·标题：游戏加油节奏 gogo
[3:46:00－3:51:53]飞机台风提醒
·音音解释猴子钟表模拟器有延迟，主机版也有点延迟但好点。
·念出观众留言：生日会那晚在飞机上，希望下飞机时还没结束。
·音音提到BW期间有超强台风影响江浙沪，担心飞机延误，提醒大家带伞注意安全。
·第二个话题：
[4:22:00－4:24:18]奶茶晚安互动 ✂️
·这里继续讲妈妈打麻将赢钱想到请你喝奶茶，然后晚安，点名音乐生，希望大家早点休息。
[4:24:05－4:25:31]奶茶晚安互动 ✂️
·[开始－结束]话题标题 ✂️
●弹幕/礼物高光
·让我们详细解析字幕，提取关键点。
·注意字幕最后“妈妈今天我妈打麻将赢钱了你喝杯那了”，然后进入下一个时间段。
"""

        _parse_llm_response(response, 4000, 14200, topics)
        _parse_llm_response(response, 15700, 16000, topics)
        report = _build_timeline_report("测试.flv", "弹幕峰值 2 个窗口", topics, streamer_name="音音")

        self.assertIn("飞机台风提醒", report)
        self.assertIn("音音解释猴子钟表模拟器有延迟", report)
        self.assertIn("提醒大家带伞注意安全", report)
        self.assertIn("奶茶晚安互动", report)
        self.assertIn("妈妈打麻将赢钱想到请你喝奶茶", report)
        for dirty in ("考虑分成以下话题", "更好的方式", "我们仔细分析", "这里明显", "考虑输出", "标题：", "第二个话题"):
            self.assertNotIn(dirty, report)
        for dirty in ("让我们详细解析", "提取关键点", "注意字幕最后", "[开始－结束]话题标题"):
            self.assertNotIn(dirty, report)

    def test_dedupe_same_title_overlapping_expanded_clip_marks(self):
        marks = [
            {"start": 15480, "end": 15858, "topic_start": 15720, "topic_end": 15858, "title": "奶茶晚安互动"},
            {"start": 15602, "end": 15858, "topic_start": 15845, "topic_end": 15931, "title": "奶茶晚安互动"},
            {"start": 30, "end": 260, "topic_start": 200, "topic_end": 210, "title": "话题B"},
        ]

        deduped = _dedupe_clip_marks(marks)

        self.assertEqual([m["title"] for m in deduped], ["话题B", "奶茶晚安互动"])

    def test_clean_current_report_structural_draft_and_clip_title(self):
        topics = []
        response = """
[0:40:10－0:41:56]先理解字幕
·先理解字幕：
·“是不是特别修长现在才现最近几年才眼睛没那么大以前眼睛特别大” – 说眼睛修长。
·基于此，我们整理话题：
·音音提到以前人体比例动感大，现在眼睛修长，以前眼睛太大显得不精致。
·话题一：
[1:32:00－1:34:20]所以整体是主播在讲他之前看上一块300万的石头 ✂️
·所以整体是音音在讲他之前看上一块300万的石头，预估价格但没买，然后自己买了五万和二十六万两个小石头。
·柳师傅分解出好的部分做成小件，音音反思通过设计包装，感觉又好了。
[2:58:00－3:00:13]感谢礼物互动
·从字幕看，有多个片段：
·可能的话题：
·2:55:13开始评论文本A。
·音音感叹太难，决定要闭着眼玩这一关。
·感谢“小h六六四幺”的沙画。
"""

        _, marks1 = _parse_llm_response(response, 2400, 2550, topics)
        _, marks2 = _parse_llm_response(response, 5520, 5660, topics)
        _parse_llm_response(response, 17880 - 7200, 18020 - 7200, topics)
        report = _build_timeline_report("测试.flv", "弹幕峰值 2 个窗口", topics, streamer_name="音音")

        self.assertIn("翡翠切石与包装", report)
        self.assertIn("柳师傅分解出好的部分做成小件", report)
        self.assertIn("闭眼关卡挑战", report)
        self.assertEqual(marks1, [])
        self.assertEqual(marks2, [{"start": 5520, "end": 5660, "title": "翡翠切石与包装"}])
        for dirty in ("先理解字幕", "基于此", "话题一", "所以整体", "从字幕看", "可能的话题", "评论文本A"):
            self.assertNotIn(dirty, report)

    def test_drop_current_report_meta_titles_from_report_and_clips(self):
        topics = []
        response = """
[0:52:00－0:53:54]我们仔细看时间线变化 ✂️
·然后0:58:00后继续：“我想怎么出去我已经出来了不要白费力气了这个房间马上就会变为真空空间”
·我们仔细看时间线变化：
·0:58:00-0:59:52（及以后）：继续角色对话，真空空间、皮卡丘、直播等。
[1:30:00－1:32:42]我们分析有哪些连续讲话 ✂️
·我们分析有哪些连续讲话，整理成几个话题。
·## 规划话题结构：
[3:21:18－3:22:19]观察事件
·3:14:01-3:16:08：哼唱练习，再战，讨论拍子不好找。
·3:21:18-3:22:01：落后于上一把，不喊没战斗力，音乐不好，混合关，建议看示范或直接下个游戏。
[3:44:04－3:46:32]输出时不要写Part行
·输出时不要写Part行。
·现在我们来组织。
·字幕内容:
[4:16:00－4:18:00]一个合理的方法 ✂️
·一个合理的方法：以明显的主题变化为界。
·实际上，看字幕文本：
"""

        _, marks = _parse_llm_response(response, 3000, 15500, topics)
        report = _build_timeline_report("测试.flv", "弹幕峰值 2 个窗口", topics, streamer_name="音音")

        self.assertEqual(marks, [])
        self.assertIn("唱歌练习找拍子", report)
        self.assertIn("哼唱练习", report)
        for dirty in (
            "我们仔细看时间线变化", "我们分析有哪些连续讲话", "规划话题结构",
            "观察事件", "输出时不要写Part行", "一个合理的方法", "实际上，看字幕文本",
        ):
            self.assertNotIn(dirty, report)

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

    def test_filter_current_report_meta_noise_and_repair_short_topic_time(self):
        topics = []
        response = """
[0:31:06－0:31:30]收到TXT小说与感谢礼物，讨论直播尺度 ✂️
·音音提到朋友分类发送TXT小说，省去下载网盘时间
·要点用·，如果没有特别弹幕爆点，可以不用●。这里没有明显的观众留言内容在字幕中。
·但是
[3:55:25－3:55:27]连麦分析店铺经营问题 ✂️
·音音与连麦者沟通，了解到店铺在陕西榆林市区，97平米，年租金9万，三个员工各三千六。
·音音指出二人合伙人未给自己发工资，只发员工，店铺每月亏损约1.8万。
·不过，注意原字幕没有说完，但只到“那”，已经完整一个问题。
·但也许有更聪明的做法：因为字幕从3:55:25开始连续多条，可能每条对应真实时间？但都是同样的内容。
·但这样只有2秒，显然不符合常识。但忠实于数据。
"""
        blocks, marks = _parse_llm_response(response, 1800, 14400, topics)
        report = _build_timeline_report("测试.flv", "弹幕峰值 2 个窗口", topics)

        self.assertEqual(len(blocks), 2)
        self.assertIn("[3:55:25－3:56:", report)
        self.assertIn("·音音与连麦者沟通", report)
        self.assertIn("·音音指出二人合伙人未给自己发工资", report)
        self.assertEqual(marks[1]["end"], topics[1]["end"])
        for dirty in (
            "要点用", "没有特别弹幕爆点", "这里没有明显", "·但是", "注意原字幕",
            "更聪明的做法", "每条对应真实时间", "不符合常识", "忠实于数据",
        ):
            self.assertNotIn(dirty, report)

    def test_long_raw_titles_are_shortened_and_more_meta_noise_filtered(self):
        topics = []
        response = """
[1:10:20－1:12:46]感个CH的声音好可以可以能听到哎但说实话稍微有点嘈杂了但没有办法因为那时候条件就是没有那么好太听的话那我就把这个效果关掉好了我真的没有病哦这应该是觉得开场没开好才不能开场那我当时时么了十年了二十年后的我啊你好啊 ✂️
·这段音音在说找到十年前的手机，看到自己曾经给十年后的自己留的视频，感慨UP主努力。
[1:30:00－1:32:42]但是下一次我不确定下一是什么时候看下次可能没有时间啊等一下看一下下天半它都看了
·```我们说先看到这里来看到八点十分了后面你就不看了后面的视了```
·看第二段: 1:27:53-1:27:59 音音说喜欢像素风古早感，第三段: 1:28:00-1:30:14 音音在读祝福请求并回应
·同样，1:28:00-1:30:14的话题，我们取到1:30:14
[4:24:05－4:25:17]感谢我有十八岁的音乐
·第二段：[4:22:00－4:24:18] “想爱你啊音乐生宝宝好可爱妈妈打麻将要赢钱了想到的是请一喝奶茶”
·这显然是连麦或互动，提到妈妈打麻将赢钱请喝奶茶，然后说晚安，感谢观众，收尾。
·我们按时间顺序梳理：
"""
        _parse_llm_response(response, 4200, 16000, topics)
        report = _build_timeline_report("测试.flv", "无弹幕数据", topics, streamer_name="音音")

        self.assertIn("十年前视频感慨", report)
        self.assertNotIn("感个CH的声音好", report)
        self.assertNotIn("```", report)
        self.assertNotIn("看第二段", report)
        self.assertNotIn("同样", report)
        self.assertNotIn("第二段：", report)
        self.assertNotIn("我们按时间顺序梳理", report)
        self.assertTrue(all(len(topic["title"]) <= 24 for topic in topics))

    def test_make_fallback_topic_from_empty_llm_chunk(self):
        chunk = {
            "start": 600,
            "end": 1200,
            "text": "[0:10:00] 音音继续和观众聊天，读弹幕，感谢礼物，聊生日安排",
            "danmaku_info": "无弹幕",
        }

        topic = _make_fallback_topic_from_chunk(chunk, streamer_name="音音")

        self.assertEqual(topic["start"], 600)
        self.assertEqual(topic["end"], 1200)
        self.assertFalse(topic["can_slice"])
        self.assertIn(topic["title"], {"感谢礼物互动", "生日相关聊天", "读弹幕互动"})
        body = "\n".join(topic["body"])
        self.assertIn("连续聊天/互动", body)
        self.assertNotIn("音音继续和观众聊天", body)

    def test_fallback_title_avoids_raw_asr_garbage(self):
        cases = [
            ("imistionhowloneonetothefirstsideofwhaever", "日常聊天互动"),
            ("你动心身感人体比例身体人体比例以前都是这样的人体比例动感特别大", "人体比例讨论"),
            ("我志士一族痔疮的品质决定家族地位今日抽到传说级至疮者", "奇怪广告吐槽"),
            ("赢了啊我这就让你赢了啊这个人都是你找的我这就让你赢了啊", "日常聊天互动"),
        ]
        for text, expected_title in cases:
            topic = _make_fallback_topic_from_chunk(
                {"start": 0, "end": 600, "text": f"[0:00:00] {text}", "danmaku_info": "无弹幕"},
                streamer_name="音音",
            )
            self.assertEqual(topic["title"], expected_title)
            self.assertNotIn(text[:20], "\n".join(topic["body"]))

    def test_filter_outline_reasoning_from_current_report(self):
        topics = []
        response = """
[2:46:04－2:47:56]"感觉今天的手感火热有没有"
·1. 关于舒适区、吃、照镜子的讨论，还有妈妈角色（严厉vs包容）的讨论。
·我们不能输出“无明显话题”，因为有很多讲话。
·话题1: 讨论舒适区与照镜子，妈妈角色争议（2:38:00-2:40:33）
·可能的划分：第一段是聊天，第二段是游戏。
·通常做法是：将字幕时间段按顺序整理为连续的话题。
·考虑时间顺序：
·时间轴整合：
·音音说感觉今天手感很好，准备继续挑战节奏天国。
"""
        _parse_llm_response(response, 9900, 11000, topics)
        report = _build_timeline_report("测试.flv", "无弹幕数据", topics, streamer_name="音音")

        self.assertIn("音音说感觉今天手感很好", report)
        for dirty in ("1. 关于", "我们不能输出", "话题1", "可能的划分", "通常做法", "考虑时间顺序", "时间轴整合"):
            self.assertNotIn(dirty, report)

    def test_parse_srt_text_dedupes_repeated_long_segments_and_repairs_time(self):
        long_text = "这是一段异常长的字幕" * 30
        content = f"""1
03:55:00,000 --> 03:55:00,200
{long_text}

2
03:55:00,200 --> 03:55:00,400
{long_text}

3
03:56:00,000 --> 03:56:03,000
正常字幕
"""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.srt"
            path.write_text(content, encoding="utf-8")

            segs = parse_srt_text(str(path))
            chunks = chunk_srt(segs, peaks=[])

        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0][0], 14100)
        self.assertGreater(segs[0][1] - segs[0][0], 20)
        self.assertIn("3:55:00－3:55:", chunks[0]["text"])

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

    def test_default_chunking_uses_ten_minutes_and_natural_topics(self):
        segs = [
            (0, "开场闲聊"),
            (300, "继续聊天"),
            (599, "第十分钟内内容"),
            (601, "进入下一个处理块"),
        ]

        chunks = chunk_srt(segs, peaks=[])
        prompt, _, _ = _build_chunk_prompt(
            {"start": 0, "end": CHUNK_SEC, "text": "x" * (LLM_FULL_TEXT_CHARS + 100), "danmaku_info": "无弹幕"},
            0,
            1,
            streamer_name="音音",
        )

        self.assertEqual(CHUNK_SEC, 600)
        self.assertEqual(LLM_MAX_TOKENS, 2200)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["start"], 0)
        self.assertEqual(chunks[0]["end"], 600)
        self.assertEqual(chunks[1]["start"], 601)
        self.assertIn("1-2 个核心话题", prompt)
        self.assertEqual(len(prompt.split("## 字幕:\n", 1)[1]), LLM_FULL_TEXT_CHARS)

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


