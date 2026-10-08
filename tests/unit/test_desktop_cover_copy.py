"""AutoCover vNext 本地基础文案提取回归。"""

from __future__ import annotations

import unittest

from autoslice.desktop.cover_copy import CopyLexicon, generate_basic_copy_variants
from autoslice.streamer_profiles import CoverEmphasisTerm


class BasicCopyContractTests(unittest.TestCase):
    def test_packaging_suspense_does_not_beat_real_event(self):
        title = "〖泽音〗音姐直播间突入神秘人物👀气的音姐大骂音悦生要他退钱！😰究竟发生了什么🤔"
        variants = generate_basic_copy_variants(title)

        self.assertTrue(variants)
        self.assertIn("退钱", variants[0].headline)
        self.assertNotIn("究竟发生了什么", variants[0].text)

    def test_warning_can_be_a_single_strong_headline(self):
        title = "〖泽音〗警告：本视频请勿外放！音姐打《啊。啊。啊。》太隐晦了🥵"
        best = generate_basic_copy_variants(title)[0]

        self.assertIn("请勿外放", best.headline)
        self.assertFalse(best.context)

    def test_final_reaction_is_kept_for_dialogue_title(self):
        title = (
            "〖泽音〗音悦生直播点鸭子😰"
            "“点鸭子你去菜场啊来我们这🤔”"
            "“原来是菜场啊 我以为是超市呢🤭”"
            "“我恨我秒懂🤬”"
        )
        self.assertIn("秒懂", generate_basic_copy_variants(title)[0].headline)

    def test_basic_candidates_never_exceed_two_semantic_blocks(self):
        title = (
            "〖泽音〗看大家给音姐弄大屏😮"
            "“感觉去线下得要更低调了😰”"
            "“穿洞洞鞋拿purax的是音音！🤩”"
            "“嘘嘘嘘！不要再说了”"
        )
        for candidate in generate_basic_copy_variants(title):
            self.assertTrue(candidate.headline)
            self.assertLessEqual(candidate.text.count("\n"), 1)

    def test_basic_mode_only_extracts_substrings_from_original_title(self):
        title = "〖糖&牛〗BW糖牛要打一架了👀音姐要狠狠爆牛哥口嗨之仇🤬结果可恶牛哥居然要下架切片员视频销毁证据😡坏事做尽！"
        for candidate in generate_basic_copy_variants(title):
            if candidate.context:
                self.assertIn(candidate.context, title)
            self.assertIn(candidate.headline, title)

    def test_prefix_is_not_left_in_generated_copy(self):
        variants = generate_basic_copy_variants("〖泽音〗音悦生屁股这么翘，一定能顶瓶汽水吧")
        self.assertTrue(variants)
        self.assertTrue(all("〖泽音〗" not in candidate.text for candidate in variants))

    def test_default_candidate_prefers_short_original_burst_over_suspense_suffix(self):
        best = generate_basic_copy_variants("〖泽音〗直播间突入神秘人物气的音姐大骂音悦生要他退钱！究竟发生了什么")[0]

        self.assertNotIn("究竟发生了什么", best.text)
        self.assertLessEqual(len(best.headline), 18)
        self.assertIn("退钱", best.text)

    def test_long_context_is_dropped_when_it_would_recreate_a_giant_text_box(self):
        best = generate_basic_copy_variants("〖泽音〗苦主小音幻想音悦生以后对别人说天衣无缝情话😰音音心里就一阵心酸😭")[0]

        self.assertTrue(best.headline)
        self.assertLessEqual(len(best.context), 20)
        self.assertLessEqual(best.text.count("\n"), 1)

    def test_slash_separated_failure_sample_stays_two_original_blocks(self):
        title = "音姐今天要给沐霂点男模 / 哎呀我还没见过男模啥样呢"
        best = generate_basic_copy_variants(title)[0]

        self.assertEqual(best.context, "音姐今天要给沐霂点男模")
        self.assertEqual(best.headline, "哎呀我还没见过男模啥样呢")

    def test_teaser_promise_does_not_beat_the_surprise_clause(self):
        # “××告诉你……内幕”是预告，“居然”所在的分句才是爆点。
        best = generate_basic_copy_variants("【泽音】选秀带手机居然会改变选曲⁉ 懂姐小音告诉你韩娱特殊操作与内幕👀")[0]
        self.assertEqual(best.headline, "选秀带手机居然会改变选曲⁉")
        self.assertNotIn("告诉你", best.text)


class GoldenSetTests(unittest.TestCase):
    """AUTOCOVER_COPY_GOLDEN_SET.md 中截取边界与上下文选择的代表案例。"""

    def best(self, title):
        return generate_basic_copy_variants(title)[0]

    def test_cut_starts_at_phrase_boundary_and_lead_becomes_context(self):
        best = self.best("〖泽音〗泽音一个晚上居然被冲了万楼？！😱原因居然是这个？！👀")
        # 不再把“居然”切成“然”；舍去的前半段去掉主语后作为 A。
        self.assertEqual(best.headline, "被冲了万楼？！")
        self.assertEqual(best.context, "一个晚上")

    def test_short_burst_keeps_original_exclamation(self):
        best = self.best("〖泽音〗音姐直播间突入神秘人物👀气的音姐大骂音悦生要他退钱！😰究竟发生了什么🤔")
        self.assertEqual(best.headline, "退钱！")

    def test_cut_never_starts_with_noun_fragment(self):
        best = self.best(
            "〖泽音〗音姐安慰没抢到BW票的音悦生们😭“大家不要激动啊😰黄牛就是利用这种心理赚的钱 总会有机会的😘”"
        )
        self.assertEqual(best.headline, "总会有机会的")

    def test_quoted_reaction_takes_narrative_background_as_context(self):
        best = self.best(
            "〖泽音〗看大家给音姐弄大屏😮“感觉去线下得要更低调了😰”"
            "“穿洞洞鞋拿purax的是音音！🤩”“嘘嘘嘘！不要再说了”"
        )
        self.assertEqual(best.headline, "不要再说了")
        self.assertIn("大家给音姐弄大屏", best.context)

    def test_same_quote_first_half_is_preferred_context(self):
        best = self.best("〖泽音〗流氓音姐张口就是“想拍一下了🥵音悦生屁股这么翘，一定能顶瓶汽水吧🤭”")
        self.assertEqual((best.context, best.headline), ("音悦生屁股这么翘", "一定能顶瓶汽水吧"))


class StreamerLexiconTests(unittest.TestCase):
    """主播专属的梗和事件词来自主播档案，代码只保留通用规则。"""

    def test_streamer_terms_are_loaded_from_profile_by_title(self):
        title = "〖泽音〗泽音一个晚上居然被冲了万楼？！😱原因居然是这个？！👀"
        self.assertEqual(generate_basic_copy_variants(title)[0].headline, "被冲了万楼？！")
        # 不带泽音档案的词表时，“万楼”不再被当成短爆点截取。
        self.assertNotEqual(generate_basic_copy_variants(title, lexicon=CopyLexicon())[0].headline, "被冲了万楼？！")

    def test_new_streamer_term_only_needs_profile_data(self):
        title = "〖小明〗小明今天直播的时候突然把整个键盘吃掉了？！大家都看傻了"
        lexicon = CopyLexicon((CoverEmphasisTerm("键盘吃掉", compact=3),))
        best = generate_basic_copy_variants(title, lexicon=lexicon)[0]
        self.assertIn("键盘吃掉", best.headline)
        self.assertIn(best.headline, title)


if __name__ == "__main__":
    unittest.main()
