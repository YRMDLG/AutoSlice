"""AutoCover 共享几何与文字排版的纯逻辑契约。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from autoslice_cover.document_layout import (
    BACKGROUND_SCALE_MAX,
    Box,
    background_box,
    break_penalty,
    focus_after_drag,
    layout_text,
    overlay_box,
)
from autoslice_cover.document_render import (
    BackgroundLayer,
    ImageLayer,
    ShapeLayer,
    TextLayer,
    TextPaint,
    compose_document,
    rgba,
)
from autoslice_cover.fonts import resolve_font_stack


class BackgroundGeometryTests(unittest.TestCase):
    def test_focus_matches_imageops_fit_centering(self):
        # 0 显示左边，1 显示右边，与 ImageOps.fit 和旧草稿同义。
        left = background_box((1920, 1080), (1440, 1080), focus_x=0.0)
        right = background_box((1920, 1080), (1440, 1080), focus_x=1.0)
        self.assertEqual(left.left, 0.0)
        self.assertAlmostEqual(right.right, 1440.0)

    def test_contain_centers_and_scale_is_clamped(self):
        box = background_box((1920, 1080), (1440, 1080), fit_mode="contain")
        self.assertAlmostEqual(box.width, 1440.0)
        self.assertAlmostEqual(box.center_y, 540.0)
        huge = background_box((1440, 1080), (1440, 1080), scale=99)
        self.assertAlmostEqual(huge.width, 1440 * BACKGROUND_SCALE_MAX)

    def test_drag_moves_content_with_pointer(self):
        box = background_box((1920, 1080), (1440, 1080), focus_x=0.5)
        moved = focus_after_drag(0.5, 100.0, 1440, box.width)
        after = background_box((1920, 1080), (1440, 1080), focus_x=moved)
        self.assertAlmostEqual(after.left - box.left, 100.0, places=3)
        self.assertEqual(focus_after_drag(0.3, 50.0, 1440, 1440), 0.3)

    def test_overlay_keeps_source_aspect_ratio(self):
        box = overlay_box((200, 100), (1440, 1080), x=0.1, y=0.2, scale=1.0)
        self.assertAlmostEqual(box.width, 1440 * 0.24)
        self.assertAlmostEqual(box.height, box.width / 2)
        self.assertEqual((box.left, box.top), (144.0, 216.0))


class BreakRuleTests(unittest.TestCase):
    def test_forbidden_breaks(self):
        text = "被冲了1万楼？！ABC字"
        self.assertIsNone(break_penalty(text, text.index("万")))   # 数字与量词
        self.assertIsNone(break_penalty(text, text.index("？")))   # 标点不进行首
        self.assertIsNone(break_penalty(text, text.index("B")))    # 拉丁单词

    def test_semantic_breaks_are_cheaper_than_plain_ones(self):
        text = "一个晚上居然被冲了万楼"
        plain = break_penalty(text, text.index("然"))
        connective = break_penalty(text, text.index("居"))
        particle = break_penalty(text, text.index("万"))
        self.assertLess(connective, plain)
        self.assertLess(particle, plain)
        self.assertEqual(break_penalty("你好，世界", 3), 0.0)

    def test_break_after_pronoun_object_but_not_inside_plural(self):
        text = "懂姐小音告诉你韩娱特殊操作"
        self.assertLess(break_penalty(text, text.index("韩")), break_penalty(text, text.index("娱")))
        self.assertEqual(break_penalty("你们好棒", 1), break_penalty("大家好棒", 1))


class TextLayoutTests(unittest.TestCase):
    def setUp(self):
        self.fonts = resolve_font_stack()

    def _layout(self, text, width=720, height=260, size=104, align="left"):
        return layout_text(
            text, area=Box(100, 80, width, height), requested_size=size, minimum_size=42,
            stroke_width=6, line_spacing=1.12, align=align, font_paths=self.fonts,
        )

    def test_layout_stays_inside_area_and_never_leaves_short_tail(self):
        layout = self._layout("一个晚上居然被冲了万楼原因居然是这个")
        self.assertLessEqual(len(layout.lines), 2)
        self.assertGreater(len(layout.lines[-1].text), 2)
        self.assertLessEqual(layout.ink.right, layout.area.right + 1)
        self.assertGreaterEqual(layout.ink.left, layout.area.left - 1)
        self.assertLessEqual(layout.ink.bottom, layout.area.bottom + 1)

    def test_alignment_moves_ink_inside_area(self):
        left = self._layout("短标题", align="left")
        right = self._layout("短标题", align="right")
        self.assertAlmostEqual(left.ink.left, left.area.left, delta=1)
        self.assertAlmostEqual(right.ink.right, right.area.right, delta=1)

    def test_translation_reuses_same_relative_layout(self):
        first = self._layout("拖动不改变排版")
        moved = layout_text(
            "拖动不改变排版", area=Box(400, 300, 720, 260), requested_size=104, minimum_size=42,
            stroke_width=6, line_spacing=1.12, align="left", font_paths=self.fonts,
        )
        self.assertEqual([line.text for line in first.lines], [line.text for line in moved.lines])
        self.assertAlmostEqual(moved.lines[0].baseline - first.lines[0].baseline, 220.0)


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.frame = root / "frame.png"
        image = Image.new("RGB", (1920, 1080), "#3060A0")
        ImageDraw.Draw(image).rectangle((0, 0, 200, 1080), fill="#FF2020")
        image.save(self.frame)
        self.sticker = root / "sticker.png"
        Image.new("RGBA", (100, 100), (0, 255, 0, 255)).save(self.sticker)

    def test_layers_follow_given_order_and_opacity(self):
        background = BackgroundLayer(str(self.frame), background_box((1920, 1080), (1440, 1080), focus_x=0.0))
        box = Box(600, 400, 200, 200)
        opaque = compose_document((1440, 1080), background, (
            ImageLayer(str(self.sticker), box),
            ShapeLayer("rect", box, "#FFFFFF", 4, fill="#0000FF"),
        ))
        self.assertEqual(opaque.getpixel((700, 500)), (0, 0, 255))
        half = compose_document((1440, 1080), background, (ImageLayer(str(self.sticker), box, opacity=0.5),))
        red, green, blue = half.getpixel((700, 500))
        self.assertTrue(100 < green < 200 and blue > 60)
        self.assertEqual(opaque.getpixel((20, 20))[:1], (255,))

    def test_text_layer_draws_fill_color(self):
        fonts = resolve_font_stack()
        layout = layout_text(
            "测试", area=Box(100, 100, 600, 300), requested_size=160, minimum_size=42,
            stroke_width=6, line_spacing=1.12, align="left", font_paths=fonts,
        )
        image = compose_document((1440, 1080), None, (
            TextLayer(layout, TextPaint("#00E5FF", "#111111", 6, backdrop="#FF000080")),
        ))
        pixels = list(image.crop((100, 100, 700, 400)).getdata())
        self.assertTrue(any(blue > 200 and green > 180 and red < 60 for red, green, blue in pixels))
        self.assertTrue(any(red > 100 and green < 40 and blue < 40 for red, green, blue in pixels))

    def test_rgba_parses_css_alpha(self):
        self.assertEqual(rgba("#11223380"), (0x11, 0x22, 0x33, 0x80))
        self.assertEqual(rgba("bad", "#FFFFFF"), (255, 255, 255, 255))


if __name__ == "__main__":
    unittest.main()
