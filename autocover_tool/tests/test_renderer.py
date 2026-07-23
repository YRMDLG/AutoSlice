"""双比例历史风格封面渲染测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from autocover.emoji import (
    get_chromium_path,
    get_emoji_font_path,
    render_emoji_image,
)
from autocover.renderer import (
    StickerOverlay,
    TextTransform,
    compose_background,
    draw_cover_text,
    render_cover,
    resolve_font_path,
)
from autocover.fonts import LOCAL_FONT_PATH
from autocover.style import HOME_4_3, TEMPLATES, get_canvas_spec, get_palette, get_template
from autocover.titles import CoverLine


class RendererTests(unittest.TestCase):
    """验证画布、布局、字体和输出体积。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.frame_path = self.root / "frame.jpg"
        frame = Image.new("RGB", (1920, 1080), "#5a356f")
        draw = ImageDraw.Draw(frame)
        draw.rectangle((0, 0, 620, 1080), fill="#19b9c7")
        draw.rectangle((1280, 0, 1920, 1080), fill="#f27ca7")
        draw.ellipse((1340, 120, 1840, 920), fill="#ffe9f1")
        frame.save(self.frame_path, quality=95)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_renders_both_bilibili_canvas_sizes(self) -> None:
        title = "【泽音】音悦生发来离谱SC，音音一眼识破是群发🤣"
        wide = render_cover(self.frame_path, title, self.root / "wide.jpg", canvas_key="16x9")
        home = render_cover(self.frame_path, title, self.root / "home.jpg", canvas_key="4x3")

        with Image.open(wide.output_path) as wide_image:
            self.assertEqual(wide_image.size, (1920, 1080))
        with Image.open(home.output_path) as home_image:
            self.assertEqual(home_image.size, (1440, 1080))
        self.assertEqual(wide.template_key, "evidence")
        self.assertEqual(home.template_key, "evidence")
        self.assertNotEqual([item.box for item in wide.placements], [item.box for item in home.placements])

    def test_all_templates_keep_text_inside_canvas(self) -> None:
        title = "【泽音】看视频发现离谱场面😰音音当场吐槽🤣“这也太夸张了吧！”"
        for template_key in TEMPLATES:
            with self.subTest(template_key=template_key):
                result = render_cover(
                    self.frame_path,
                    title,
                    self.root / f"{template_key}.jpg",
                    canvas_key="4x3",
                    template_key=template_key,
                )
                self.assertGreater(len(result.placements), 0)
                for placement in result.placements:
                    left, top, right, bottom = placement.box
                    self.assertGreaterEqual(left, 0)
                    self.assertGreaterEqual(top, 0)
                    self.assertLessEqual(right, result.width)
                    self.assertLessEqual(bottom, result.height)

    def test_preserve_templates_keep_both_sides_in_four_by_three(self) -> None:
        with Image.open(self.frame_path) as frame:
            image = compose_background(frame, HOME_4_3, get_template("reaction"))

        left_pixel = image.getpixel((40, HOME_4_3.height // 2))
        right_pixel = image.getpixel((HOME_4_3.width - 40, HOME_4_3.height // 2))
        self.assertGreater(left_pixel[1], left_pixel[0])
        self.assertGreater(right_pixel[0], right_pixel[1])

    def test_text_moves_opposite_a_detailed_left_side(self) -> None:
        frame_path = self.root / "left-subject.jpg"
        frame = Image.new("RGB", (1920, 1080), "#b991aa")
        draw = ImageDraw.Draw(frame)
        for y in range(0, 1080, 32):
            for x in range(0, 760, 32):
                color = "#ffffff" if (x // 32 + y // 32) % 2 else "#1c1c1c"
                draw.rectangle((x, y, x + 31, y + 31), fill=color)
        frame.save(frame_path)

        result = render_cover(
            frame_path,
            "【泽音】线下发现离谱秘密，音音当场震惊",
            self.root / "right-text.jpg",
            template_key="dialog",
        )

        self.assertTrue(all(placement.box[0] > result.width * 0.20 for placement in result.placements))

    def test_night_copy_moves_away_from_a_detailed_center_subject(self) -> None:
        frame_path = self.root / "center-subject.jpg"
        frame = Image.new("RGB", (1920, 1080), "#aa8cae")
        draw = ImageDraw.Draw(frame)
        for y in range(0, 1080, 28):
            for x in range(700, 1220, 28):
                color = "#ffffff" if (x // 28 + y // 28) % 2 else "#271f31"
                draw.rectangle((x, y, x + 27, y + 27), fill=color)
        frame.save(frame_path)

        result = render_cover(
            frame_path,
            "【泽音】晚安小音音，生日快乐音音",
            self.root / "night-side.jpg",
            template_key="night",
        )

        self.assertTrue(
            all(
                placement.box[2] <= result.width * 0.50
                or placement.box[0] >= result.width * 0.50
                for placement in result.placements
            )
        )

    def test_night_copy_uses_side_layout_for_a_portrait_frame(self) -> None:
        frame_path = self.root / "portrait-subject.jpg"
        frame = Image.new("RGB", (1080, 1920), "#2d2433")
        draw = ImageDraw.Draw(frame)
        draw.rectangle((120, 0, 1080, 1920), fill="#d981aa")
        for y in range(100, 1800, 36):
            draw.line((260, y, 1000, y), fill="#fff4fa", width=10)
        frame.save(frame_path)

        result = render_cover(
            frame_path,
            "【泽音】晚安小音音，BW结束突击小音",
            self.root / "night-portrait.jpg",
            template_key="night",
        )

        self.assertTrue(all(placement.box[2] <= result.width * 0.67 for placement in result.placements))
        self.assertGreater(result.placements[0].font_size, result.placements[1].font_size)

    def test_dialog_copy_preserves_a_portrait_frame_beside_text(self) -> None:
        frame_path = self.root / "portrait-dialog.jpg"
        frame = Image.new("RGB", (1080, 1920), "#d981aa")
        draw = ImageDraw.Draw(frame)
        draw.rectangle((0, 0, 1080, 220), fill="#f2cf4b")
        draw.rectangle((0, 1700, 1080, 1920), fill="#42c7cb")
        frame.save(frame_path)

        wide = render_cover(
            frame_path,
            "【泽音】打车聊3D被司机回头盯上，三人当场演起建模设计师",
            self.root / "portrait-dialog-wide.jpg",
            template_key="dialog",
        )
        home = render_cover(
            frame_path,
            "【泽音】打车聊3D被司机回头盯上，三人当场演起建模设计师",
            self.root / "portrait-dialog-home.jpg",
            canvas_key="4x3",
            template_key="dialog",
        )

        self.assertTrue(all(placement.box[0] == 250 for placement in wide.placements))
        self.assertTrue(all(placement.box[0] == 14 for placement in home.placements))
        self.assertEqual(
            [placement.font_size for placement in wide.placements],
            [placement.font_size for placement in home.placements],
        )
        self.assertEqual(
            [placement.box[1] for placement in wide.placements],
            [placement.box[1] for placement in home.placements],
        )
        for result in (wide, home):
            for placement in result.placements:
                self.assertGreaterEqual(placement.box[0], 10)
                self.assertLessEqual(placement.box[2], result.width - 10)
                self.assertLessEqual(placement.box[3], result.height)
        with Image.open(home.output_path) as image:
            top_left = image.getpixel((40, 40))
            bottom_left = image.getpixel((40, image.height - 40))
        self.assertGreater(top_left[0], top_left[2])
        self.assertGreater(bottom_left[1], bottom_left[0])

    def test_portrait_copy_keeps_a_short_phrase_on_one_line(self) -> None:
        frame_path = self.root / "portrait-phrase.jpg"
        Image.new("RGB", (1080, 1920), "#c881a6").save(frame_path)

        result = render_cover(
            frame_path,
            "【泽音】线下发现花礼的真实秘密，还被问是不是去矮人国当白雪公主",
            self.root / "portrait-phrase-cover.jpg",
            template_key="dialog",
        )

        texts = [placement.text for placement in result.placements]
        self.assertTrue(any("矮人国" in text for text in texts))
        self.assertGreaterEqual(min(placement.font_size for placement in result.placements), 50)

    def test_portrait_copy_splits_a_long_detail_for_readability(self) -> None:
        frame_path = self.root / "portrait-long-detail.jpg"
        Image.new("RGB", (1080, 1920), "#c881a6").save(frame_path)

        result = render_cover(
            frame_path,
            "【泽音】BW回来睡到凌晨三点，吃完早饭又继续睡，醒来还偷偷改了新3D刘海和嘴巴",
            self.root / "portrait-long-detail-cover.jpg",
            template_key="dialog",
        )

        texts = [placement.text for placement in result.placements]
        self.assertIn("醒来还偷偷改了", texts)
        self.assertIn("新3D刘海和嘴巴", texts)
        self.assertGreaterEqual(min(placement.font_size for placement in result.placements), 50)

    def test_landscape_impact_copy_scales_each_line_independently(self) -> None:
        result = render_cover(
            self.frame_path,
            "测试",
            self.root / "independent-lines.jpg",
            template_key="dialog",
            copy_lines=["短背景", "这是一条明显更长的上下文说明", "爆了！"],
        )

        sizes = [placement.font_size for placement in result.placements]
        self.assertGreater(sizes[0], sizes[1])
        self.assertGreater(sizes[2], sizes[1])
        self.assertLess(result.placements[0].box[1], result.height * 0.20)
        self.assertGreater(result.placements[-1].box[1], result.height * 0.50)

    def test_default_background_keeps_reference_brightness(self) -> None:
        frame = Image.new("RGB", (1920, 1080), "#8090a0")
        image = compose_background(frame, HOME_4_3, get_template("dialog"))

        self.assertEqual(image.getpixel((720, 540)), (128, 144, 160))

    def test_jpeg_respects_size_limit(self) -> None:
        result = render_cover(
            self.frame_path,
            "【泽音】音姐亲授线下上台秘籍",
            self.root / "limited.jpg",
            max_bytes=350_000,
        )

        self.assertLessEqual(result.file_size, 350_000)
        self.assertEqual(result.file_size, Path(result.output_path).stat().st_size)

    def test_custom_line_colors_and_count_validation(self) -> None:
        result = render_cover(
            self.frame_path,
            "测试",
            self.root / "custom.jpg",
            template_key="headline",
            copy_lines=["第一行", "第二行"],
            line_colors=["#ff0000", "#00ff00"],
            line_stroke_colors=["#111111", "#ffffff"],
        )

        self.assertEqual([item.color for item in result.placements], ["#ff0000", "#00ff00"])
        self.assertEqual(
            [item.stroke_color for item in result.placements],
            ["#111111", "#ffffff"],
        )
        with self.assertRaisesRegex(ValueError, "颜色数量"):
            render_cover(
                self.frame_path,
                "测试",
                self.root / "invalid.jpg",
                copy_lines=["第一行", "第二行"],
                line_colors=["#ff0000"],
            )
        with self.assertRaisesRegex(ValueError, "描边颜色数量"):
            render_cover(
                self.frame_path,
                "测试",
                self.root / "invalid-stroke.jpg",
                copy_lines=["第一行", "第二行"],
                line_stroke_colors=["#111111"],
            )

    def test_manual_copy_supports_eight_lines_beyond_template_suggestion(self) -> None:
        lines = [f"手动第{index}行" for index in range(1, 9)]
        result = render_cover(
            self.frame_path,
            "测试",
            self.root / "eight-lines.jpg",
            template_key="headline",
            copy_lines=lines,
        )

        self.assertEqual([item.text for item in result.placements], lines)
        for placement in result.placements:
            left, top, right, bottom = placement.box
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, HOME_4_3.width)
            self.assertLessEqual(bottom, HOME_4_3.height)
        with self.assertRaisesRegex(ValueError, "最多支持 8 行"):
            render_cover(
                self.frame_path,
                "测试",
                self.root / "nine-lines.jpg",
                template_key="headline",
                copy_lines=lines + ["第九行"],
            )

    def test_manual_text_transform_moves_and_resizes_a_line(self) -> None:
        automatic = render_cover(
            self.frame_path,
            "测试",
            self.root / "automatic-text.jpg",
            canvas_key="4x3",
            template_key="dialog",
            copy_lines=["标题"],
        )
        manual = render_cover(
            self.frame_path,
            "测试",
            self.root / "manual-text.jpg",
            canvas_key="4x3",
            template_key="dialog",
            copy_lines=["标题"],
            text_transforms=[TextTransform(x=0.45, y=0.35, scale=1.25)],
        )

        placement = manual.placements[0]
        self.assertAlmostEqual(placement.box[0], 1440 * 0.45, delta=2)
        self.assertAlmostEqual(placement.box[1], 1080 * 0.35, delta=2)
        self.assertGreater(placement.font_size, automatic.placements[0].font_size)
        self.assertGreater(placement.stroke_width, 0)
        self.assertLessEqual(placement.box[2], manual.width)
        self.assertLessEqual(placement.box[3], manual.height)

    def test_manual_text_transform_keeps_absolute_size_after_copy_changes(self) -> None:
        transform = TextTransform(x=0.20, y=0.30, scale=1.0, font_size=116)
        before = render_cover(
            self.frame_path,
            "测试",
            self.root / "absolute-size-before.jpg",
            canvas_key="4x3",
            template_key="headline",
            copy_lines=["朱鹮"],
            text_transforms=[transform],
        )
        after = render_cover(
            self.frame_path,
            "测试",
            self.root / "absolute-size-after.jpg",
            canvas_key="4x3",
            template_key="headline",
            copy_lines=["朱鹮新增"],
            text_transforms=[transform],
        )

        self.assertEqual(before.placements[0].font_size, 116)
        self.assertEqual(after.placements[0].font_size, 116)
        self.assertEqual(before.placements[0].box[:2], after.placements[0].box[:2])
        with self.assertRaisesRegex(ValueError, "实际字号"):
            render_cover(
                self.frame_path,
                "测试",
                self.root / "invalid-absolute-size.jpg",
                copy_lines=["标题"],
                text_transforms=[TextTransform(0.2, 0.3, font_size=321)],
            )

    def test_sticker_overlay_and_background_only_preview_are_created(self) -> None:
        sticker_path = self.root / "sticker.png"
        sticker = Image.new("RGBA", (240, 120), (0, 0, 0, 0))
        ImageDraw.Draw(sticker).rectangle((0, 0, 239, 119), fill=(30, 230, 80, 255))
        sticker.save(sticker_path)
        background_path = self.root / "background.jpg"

        result = render_cover(
            self.frame_path,
            "测试",
            self.root / "sticker-cover.jpg",
            canvas_key="4x3",
            template_key="dialog",
            copy_lines=["标题"],
            stickers=[
                StickerOverlay(
                    asset_id="smile",
                    image_path=str(sticker_path),
                    x=0.70,
                    y=0.25,
                    width=0.12,
                )
            ],
            background_output_path=background_path,
        )

        self.assertTrue(background_path.is_file())
        self.assertEqual(result.background_path, str(background_path.resolve()))
        placement = result.sticker_placements[0]
        self.assertEqual(placement.asset_id, "smile")
        self.assertAlmostEqual(placement.box[0], 1440 * 0.70, delta=2)
        self.assertAlmostEqual(placement.box[1], 1080 * 0.25, delta=2)
        with Image.open(result.output_path) as image:
            center = image.getpixel(
                (
                    (placement.box[0] + placement.box[2]) // 2,
                    (placement.box[1] + placement.box[3]) // 2,
                )
            )
        self.assertGreater(center[1], center[0] * 1.5)
        self.assertGreater(center[1], center[2] * 1.5)

    def test_jpeg_encoding_failure_preserves_existing_output_and_cleans_temp(self) -> None:
        output = self.root / "existing.jpg"
        original = b"existing-cover"
        output.write_bytes(original)

        def partial_then_fail(_image, file, *args, **kwargs):
            Path(file).write_bytes(b"partial-jpeg")
            raise OSError("模拟磁盘写入失败")

        with (
            patch.object(Image.Image, "save", new=partial_then_fail),
            self.assertRaisesRegex(OSError, "磁盘写入失败"),
        ):
            render_cover(self.frame_path, "测试", output)

        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_drawing_failure_preserves_existing_background_and_cover(self) -> None:
        output = self.root / "existing-cover.jpg"
        background = self.root / "existing-background.jpg"
        output.write_bytes(b"old-cover")
        background.write_bytes(b"old-background")

        with (
            patch(
                "autocover.renderer.draw_cover_text",
                side_effect=RuntimeError("模拟文字绘制失败"),
            ),
            self.assertRaisesRegex(RuntimeError, "文字绘制失败"),
        ):
            render_cover(
                self.frame_path,
                "测试",
                output,
                background_output_path=background,
            )

        self.assertEqual(output.read_bytes(), b"old-cover")
        self.assertEqual(background.read_bytes(), b"old-background")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])
        self.assertEqual(list(self.root.glob(".*.backup")), [])

    def test_second_jpeg_failure_preserves_both_existing_outputs(self) -> None:
        output = self.root / "existing-cover.jpg"
        background = self.root / "existing-background.jpg"
        output.write_bytes(b"old-cover")
        background.write_bytes(b"old-background")
        save_calls = 0

        def fail_second_save(_image, file, *args, **kwargs):
            nonlocal save_calls
            save_calls += 1
            Path(file).write_bytes(b"encoded" if save_calls == 1 else b"partial")
            if save_calls == 2:
                raise OSError("模拟成品编码失败")

        with (
            patch.object(Image.Image, "save", new=fail_second_save),
            self.assertRaisesRegex(OSError, "成品编码失败"),
        ):
            render_cover(
                self.frame_path,
                "测试",
                output,
                background_output_path=background,
            )

        self.assertEqual(output.read_bytes(), b"old-cover")
        self.assertEqual(background.read_bytes(), b"old-background")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_second_replace_failure_rolls_back_both_existing_outputs(self) -> None:
        output = self.root / "existing-cover.jpg"
        background = self.root / "existing-background.jpg"
        output.write_bytes(b"old-cover")
        background.write_bytes(b"old-background")
        path_type = type(output)
        real_replace = path_type.replace

        def fail_cover_replace(path, target):
            target_path = Path(target)
            if path.name.endswith(".tmp") and target_path == output:
                raise OSError("模拟成品替换失败")
            return real_replace(path, target)

        with (
            patch.object(path_type, "replace", new=fail_cover_replace),
            self.assertRaisesRegex(OSError, "成品替换失败"),
        ):
            render_cover(
                self.frame_path,
                "测试",
                output,
                background_output_path=background,
            )

        self.assertEqual(output.read_bytes(), b"old-cover")
        self.assertEqual(background.read_bytes(), b"old-background")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])
        self.assertEqual(list(self.root.glob(".*.backup")), [])

    def test_manual_layout_and_sticker_parameters_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "文字布局数量"):
            render_cover(
                self.frame_path,
                "测试",
                self.root / "invalid-layout.jpg",
                copy_lines=["第一行", "第二行"],
                text_transforms=[TextTransform(0.2, 0.2)],
            )
        with self.assertRaisesRegex(ValueError, "贴图宽度"):
            render_cover(
                self.frame_path,
                "测试",
                self.root / "invalid-sticker.jpg",
                stickers=[StickerOverlay("tiny", str(self.frame_path), 0.2, 0.2, 0.01)],
            )

    def test_chinese_font_fallback_can_be_loaded(self) -> None:
        font_path = resolve_font_path()

        if font_path is not None:
            font = ImageFont.truetype(font_path, size=48)
            self.assertGreater(font.getlength("音音封面"), 0)

    def test_missing_seto_glyph_is_drawn_with_a_fallback_font(self) -> None:
        if not LOCAL_FONT_PATH.is_file():
            self.skipTest("本机尚未配置 B 站濑户体")

        canvas = get_canvas_spec("16x9")
        image = Image.new("RGB", (canvas.width, canvas.height), "#ffffff")
        placements = draw_cover_text(
            image,
            [CoverLine("朱鹮", "emphasis")],
            canvas,
            get_template("headline"),
            get_palette("latest_yellow"),
            font_path=str(LOCAL_FONT_PATH),
            line_colors=["#ff204f"],
            line_stroke_colors=["#ffffff"],
        )

        placement = placements[0]
        primary = ImageFont.truetype(str(LOCAL_FONT_PATH), size=placement.font_size)
        glyph_start = placement.box[0] + round(primary.getlength("朱"))
        right_half = image.crop(
            (
                min(placement.box[2] - 1, glyph_start + placement.stroke_width * 2),
                placement.box[1],
                placement.box[2],
                placement.box[3],
            )
        )
        pixels = right_half.load()
        red_pixels = sum(
            1
            for y in range(right_half.height)
            for x in range(right_half.width)
            if pixels[x, y][0] > 200 and pixels[x, y][1] < 90 and pixels[x, y][2] < 130
        )

        self.assertGreater(red_pixels, 100, "“鹮”字没有使用系统字体补全")

    def test_windows_emoji_layer_is_colored_and_transparent(self) -> None:
        if get_emoji_font_path() is None or get_chromium_path() is None:
            self.skipTest("当前系统没有 Windows Emoji 字体或 Chromium")

        render_emoji_image.cache_clear()
        emoji = render_emoji_image("\U0001f494\U0001f496", 160)

        self.assertIsNotNone(emoji)
        assert emoji is not None
        pixels = list(
            emoji.get_flattened_data()
            if hasattr(emoji, "get_flattened_data")
            else emoji.getdata()
        )
        red_pixels = sum(
            1
            for red, green, blue, alpha in pixels
            if alpha > 128 and red > 170 and green < 100 and blue < 100
        )
        yellow_pixels = sum(
            1
            for red, green, blue, alpha in pixels
            if alpha > 128 and red > 180 and green > 140 and blue < 100
        )
        transparent_pixels = sum(1 for *_, alpha in pixels if alpha == 0)

        self.assertGreater(red_pixels, 1_000, "Emoji 没有保留 Windows 彩色红色字形")
        self.assertGreater(yellow_pixels, 100, "Emoji 没有保留 Windows 彩色高光")
        self.assertGreater(transparent_pixels, 100, "Emoji 图层背景不是透明的")

    def test_chinese_and_windows_emoji_can_be_rendered_on_the_same_line(self) -> None:
        if get_emoji_font_path() is None or get_chromium_path() is None:
            self.skipTest("当前系统没有 Windows Emoji 字体或 Chromium")

        result = render_cover(
            self.frame_path,
            "测试",
            self.root / "mixed-chinese-emoji.jpg",
            canvas_key="16x9",
            template_key="headline",
            copy_lines=["朱鹮\U0001f494\U0001f496"],
            line_colors=["#d06e95"],
            line_stroke_colors=["#ffffff"],
            text_transforms=[TextTransform(0.15, 0.30, 1.0)],
        )

        with Image.open(result.output_path) as image:
            crop = image.crop(result.placements[0].box).convert("RGB")
            pixels = list(
                crop.get_flattened_data()
                if hasattr(crop, "get_flattened_data")
                else crop.getdata()
            )
        red_pixels = sum(
            1
            for red, green, blue in pixels
            if red > 170 and green < 100 and blue < 100
        )
        yellow_pixels = sum(
            1
            for red, green, blue in pixels
            if red > 180 and green > 140 and blue < 100
        )

        self.assertGreater(red_pixels, 1_000, "导出的封面没有彩色 Emoji")
        self.assertGreater(yellow_pixels, 100, "导出的 Emoji 颜色不完整")


if __name__ == "__main__":
    unittest.main()
