"""标题解析与历史风格配置测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autocover.style import (
    HOME_4_3,
    MELODY_STYLE,
    PALETTES,
    PERSONAL_16_9,
    TEMPLATES,
    get_canvas_spec,
    get_palette,
    get_template,
)
from autocover.titles import (
    create_cover_copy,
    create_cover_lines,
    load_title_map,
    match_title,
    parse_title_markdown,
    recommend_layout_variants,
    recommend_visual_style,
    strip_title_prefix,
    title_from_filename,
    visual_units,
)


class TitleParserTests(unittest.TestCase):
    """验证投稿标题导入和视频匹配。"""

    SAMPLE = """# 投稿标题

## 01（03:57）

原文件：`01_124s_赶飞机趣事.flv`

**【泽音】下飞机遇到狂风，裙子当场被吹飞😱“没有梦幻动作好吗！”**

## 02（02:32）

原文件：`02_360s_控场心得.flv`

**【泽音】音姐亲授上台秘籍👀“动作做错就很抢镜了！”**
"""

    def test_parse_title_markdown_maps_original_files(self) -> None:
        result = parse_title_markdown(self.SAMPLE)

        self.assertEqual(len(result), 2)
        self.assertIn("01_124s_赶飞机趣事.flv", result)
        self.assertTrue(result["02_360s_控场心得.flv"].startswith("【泽音】"))

    def test_load_title_map_reads_utf8_sig(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "投稿标题.md"
            path.write_text(self.SAMPLE, encoding="utf-8-sig")

            result = load_title_map(path)

        self.assertEqual(len(result), 2)

    def test_match_title_falls_back_to_filename(self) -> None:
        title_map = parse_title_markdown(self.SAMPLE)

        self.assertIn("下飞机", match_title("01_124s_赶飞机趣事.flv", title_map))
        self.assertEqual(title_from_filename("08_2520s_没票丝滑进BW.flv"), "没票丝滑进BW")
        self.assertEqual(match_title("99_1s_未知片段.mp4", title_map), "未知片段")


class CoverCopyTests(unittest.TestCase):
    """验证封面短文案拆分。"""

    def test_strip_title_prefix_supports_account_variants(self) -> None:
        self.assertEqual(strip_title_prefix("【泽音】测试标题"), "测试标题")
        self.assertEqual(strip_title_prefix("[泽音Melody] 测试标题"), "测试标题")

    def test_create_cover_lines_keeps_hook_and_quote(self) -> None:
        title = "【泽音】想看虐文却收到三篇“全员去世”😰“虐不是让你把人写死啊！”最后只能翻自己网盘🤣"

        lines = create_cover_lines(title)

        self.assertLessEqual(len(lines), 4)
        self.assertTrue(any("想看虐文" in line for line in lines))
        self.assertTrue(any("写死" in line or "网盘" in line for line in lines))
        self.assertFalse(any("【泽音】" in line for line in lines))
        self.assertTrue(all(visual_units(line) <= MELODY_STYLE.max_line_units for line in lines))

    def test_create_cover_lines_splits_emoji_boundary(self) -> None:
        lines = create_cover_lines("【泽音】睡到凌晨三点😴醒来还偷偷改了新3D刘海和嘴巴👀")

        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(any(line.startswith("醒来") for line in lines))
        self.assertFalse(any("😴" in line or "👀" in line for line in lines))

    def test_create_cover_lines_removes_short_repeated_punchline(self) -> None:
        lines = create_cover_lines("【泽音】线下发现花礼的真实秘密👀亲手量了长度😳秘密！")

        self.assertEqual(sum("秘密" in line for line in lines), 1)

    def test_quotes_do_not_break_a_complete_phrase(self) -> None:
        lines = create_cover_lines(
            "【泽音】亲手量了“老鼠”的长度😳“为什么是长度？多少？秘密！”不是姐们你在说啥🤣"
        )

        self.assertIn("亲手量了老鼠的长度", lines)

    def test_short_question_and_answer_keep_the_real_punchline(self) -> None:
        lines = create_cover_lines(
            "【泽音】线下发现花礼的真实秘密，亲手量了花礼的长度，"
            "为什么是长度？多少？秘密！不是姐们何意味"
        )

        self.assertEqual(
            lines,
            [
                "线下发现花礼的真实秘密",
                "亲手量了花礼的长度",
                "为什么是长度？",
                "多少？秘密！",
            ],
        )

    def test_wrap_rebalances_a_single_character_orphan(self) -> None:
        lines = create_cover_lines(
            "【泽音】线下发现花礼的真实秘密，还被问是不是去矮人国当白雪公主"
        )

        self.assertTrue(all(len(line) > 1 for line in lines))
        self.assertIn("还被问是不是去", lines)
        self.assertIn("矮人国当白雪公主", lines)

    def test_latest_copy_removes_a_redundant_sentence_particle(self) -> None:
        lines = create_cover_lines(
            "【泽音】最近来太勤连保安都认识音音了",
            template_key="dialog",
            max_line_units=26,
        )

        self.assertEqual(lines, ["最近来太勤连保安都认识音音"])

    def test_low_information_reaction_does_not_displace_the_punchline(self) -> None:
        lines = create_cover_lines(
            "【泽音】亲手量了老鼠的长度，为什么是长度？多少？秘密！不是姐们你在说啥"
        )

        self.assertEqual(lines, ["亲手量了老鼠的长度", "为什么是长度？", "多少？秘密！"])

    def test_create_cover_lines_uses_night_template_limit(self) -> None:
        lines = create_cover_lines("【泽音】晚安小音音💤视频与节奏天国音💖周一休息")

        self.assertLessEqual(len(lines), 2)

    def test_night_copy_keeps_main_title_above_smaller_context(self) -> None:
        copy = create_cover_copy("【泽音】晚安小音音💤视频与节奏天国音")

        self.assertEqual([line.role for line in copy], ["emphasis", "context"])

    def test_cover_copy_assigns_semantic_roles(self) -> None:
        copy = create_cover_copy(
            "【泽音】音悦生发来离谱SC😰音音当场识破群发🤣“看清楚这里是谁的直播间！”"
        )

        self.assertEqual(copy[0].role, "context")
        self.assertEqual(copy[-1].role, "emphasis")
        self.assertTrue(all(line.role in {"context", "quote", "emphasis"} for line in copy))

    def test_invalid_copy_limits_raise_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须为正数"):
            create_cover_lines("测试", max_lines=0)


class StyleTests(unittest.TestCase):
    """验证双比例规格和历史配色。"""

    def test_canvas_specs_match_bilibili_ratios(self) -> None:
        self.assertEqual((PERSONAL_16_9.width, PERSONAL_16_9.height), (1920, 1080))
        self.assertEqual((HOME_4_3.width, HOME_4_3.height), (1440, 1080))
        self.assertAlmostEqual(PERSONAL_16_9.aspect_ratio, 16 / 9)
        self.assertAlmostEqual(HOME_4_3.aspect_ratio, 4 / 3)

    def test_style_uses_semantic_colors_instead_of_blind_rotation(self) -> None:
        self.assertEqual(MELODY_STYLE.color_for_role("context"), "#FFE438")
        self.assertEqual(MELODY_STYLE.color_for_role("quote"), "#16D8ED")
        self.assertEqual(MELODY_STYLE.color_for_role("emphasis"), "#16D8ED")
        self.assertEqual(get_palette("latest_conflict").stroke_for_role("quote"), "#FFFFFF")
        self.assertIsNone(get_palette("latest_cyan").to_dict()["quote_stroke_color"])

        with self.assertRaisesRegex(ValueError, "不支持的文字角色"):
            MELODY_STYLE.color_for_role("unknown")

    def test_multiple_historical_palettes_and_templates_are_available(self) -> None:
        self.assertEqual(
            set(TEMPLATES),
            {
                "dialog",
                "headline",
                "evidence",
                "reaction",
                "gameplay",
                "night",
                "performance",
                "poster",
                "warning",
            },
        )
        self.assertGreaterEqual(len(PALETTES), 9)
        self.assertEqual(get_palette("conflict").color_for_role("emphasis"), "#FF4E43")
        self.assertEqual(get_template("night").background_mode, "series_asset")
        self.assertEqual(get_template("evidence").layout, "evidence_split")

    def test_style_recommendation_handles_historical_series(self) -> None:
        night = recommend_visual_style("【泽音】晚安小音音💤唱歌小音的一晚")
        warning = recommend_visual_style("【泽音】警告：本视频请勿外放！")
        hot = recommend_visual_style("【泽音】赌石小音一刀地狱，破产了")
        short = recommend_visual_style("【泽音】音姐亲授上台秘籍")

        self.assertEqual((night.template_key, night.palette_key), ("night", "night_purple"))
        self.assertEqual((warning.template_key, warning.palette_key), ("warning", "warning"))
        self.assertEqual(hot.palette_key, "latest_conflict")
        self.assertEqual(short.template_key, "headline")
        self.assertEqual(short.palette_key, "latest_yellow")

    def test_style_recommendation_covers_full_space_patterns(self) -> None:
        cases = {
            "【泽音】7月第一张红SC，音音看完气坏了": "evidence",
            "【泽音】看AI生贺二创，发现大家都有原配": "reaction",
            "【泽音】节奏天国按键没喊出来": "gameplay",
            "【泽音】3D首场上车舞": "performance",
            "【泽音】一周年纪念回，来自朋友们的祝福": "poster",
        }

        for title, template_key in cases.items():
            with self.subTest(title=title):
                self.assertEqual(recommend_visual_style(title).template_key, template_key)

    def test_talking_about_3d_does_not_force_stage_template(self) -> None:
        recommendation = recommend_visual_style(
            "【泽音】打车聊3D被司机回头盯上，三人当场演起建模设计师"
        )

        self.assertEqual(recommendation.template_key, "dialog")

    def test_layout_variants_follow_the_current_complete_title(self) -> None:
        long_title = (
            "【泽音】下飞机遇到狂风，裙子当场被吹飞😱"
            "“玛丽莲？别搞笑了，没有梦幻动作好吗！”"
        )
        short_title = "【泽音】音姐亲授上台秘籍"

        long_variants = recommend_layout_variants(long_title)
        short_variants = recommend_layout_variants(short_title)

        self.assertEqual(long_variants[0].template_key, "dialog")
        self.assertEqual(short_variants[0].template_key, "headline")
        self.assertEqual(len(long_variants), 3)
        self.assertEqual(len({item.template_key for item in long_variants}), 3)
        self.assertEqual(
            [line.text for line in long_variants[0].lines],
            ["下飞机遇到狂风", "裙子当场被吹飞", "玛丽莲？", "没有梦幻动作好吗！"],
        )
        self.assertEqual(len(long_variants[1].to_dict()["lines"]), 2)

    def test_unknown_canvas_has_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "不支持的封面比例"):
            get_canvas_spec("1x1")

        with self.assertRaisesRegex(ValueError, "不支持的调色板"):
            get_palette("blue-only")

        with self.assertRaisesRegex(ValueError, "不支持的封面模板"):
            get_template("magazine")


if __name__ == "__main__":
    unittest.main()
