"""AutoCover 编辑操作契约：删除、层级、插入、样式预设与风格记忆。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from autoslice.desktop.cover_migration import document_from_basic_title_values
from autoslice.desktop.cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    ShapeObject,
    TextObject,
    TextStyle,
    default_profiles,
    insert_overlay,
    object_for_profile,
    restack_object,
    set_object_visible,
    update_text_object,
)
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.cover_style import (
    STYLE_PRESETS,
    CoverStyleMemory,
    CoverStyleMemoryStore,
    streamer_key,
)
from autoslice.desktop.foundation import DesktopStorage


def _document() -> CoverDocument:
    return CoverDocument(
        objects=(
            BackgroundObject(id="background-main"),
            TextObject(id="copy-a", text="上下文", copy_role="A", z_index=10),
            TextObject(id="copy-b", text="主文案", copy_role="B", z_index=11),
            ImageObject(id="image-1", z_index=5),
        ),
        profiles=default_profiles(),
    )


class ObjectEditingTests(unittest.TestCase):
    def test_delete_hides_text_in_both_profiles_even_with_overrides(self):
        document = _document()
        text = next(item for item in document.objects if item.id == "copy-b")
        document = update_text_object(document, replace(text, text="改过"), profile_key="4x3")
        document = set_object_visible(document, "copy-b", False)
        self.assertFalse(object_for_profile(document, "copy-b", "4x3").visible)
        self.assertFalse(object_for_profile(document, "copy-b", "16x9").visible)

    def test_restack_swaps_with_neighbour_in_one_step(self):
        document = restack_object(_document(), "image-1", 1)
        order = sorted(
            (item for item in document.objects if not isinstance(item, BackgroundObject)),
            key=lambda item: item.z_index,
        )
        self.assertEqual([item.id for item in order], ["copy-a", "image-1", "copy-b"])
        self.assertIs(restack_object(document, "copy-b", 1), document)

    def test_new_overlay_goes_above_overlays_but_below_text(self):
        document = _document()
        for index in range(8):
            document = insert_overlay(document, ShapeObject(id=f"shape-{index}"))
        z = {item.id: item.z_index for item in document.objects}
        top_overlay = max(value for key, value in z.items() if key.startswith(("shape", "image")))
        self.assertLess(top_overlay, min(z["copy-a"], z["copy-b"]))
        self.assertEqual(z["shape-7"], top_overlay)
        self.assertEqual(document.selected_object_id, "shape-7")


class TextBoxMigrationTests(unittest.TestCase):
    def test_old_area_fit_becomes_fixed_font_with_same_display(self):
        from autoslice.desktop.cover_layout import text_layout
        from autoslice.desktop.cover_migration import document_from_payload, migrate_text_boxes
        from autoslice.desktop.cover_model import Rect, TextWrap, Transform

        text = TextObject(
            id="copy-b", copy_role="B", text="一个晚上居然被冲了万楼原因居然是这个",
            transform=Transform(x=0.06, y=0.6, scale=1.0), rect=Rect(width=0.64, height=0.25),
            wrap=TextWrap(max_width=0.48, max_lines=2), style=TextStyle(font_size=140),
        )
        old = CoverDocument(
            objects=(BackgroundObject(id="background-main"), text), profiles=default_profiles(), text_revision=1,
        )
        migrated, changed = migrate_text_boxes(old)
        self.assertTrue(changed)
        item = object_for_profile(migrated, "copy-b", "4x3")
        # 旧版行宽被 max_width 截到 0.48 并缩字；升级后显式写回实际字号和行宽。
        self.assertAlmostEqual(item.rect.width, 0.48)
        self.assertEqual(item.wrap.max_width, item.rect.width)
        self.assertLess(item.style.font_size, 140)
        self.assertLessEqual(len(text_layout(item, (1440, 1080)).lines), 2)
        self.assertFalse(migrate_text_boxes(migrated)[1])
        # 存档往返后不会再次迁移。
        reloaded, again = document_from_payload(migrated.to_payload(), "标题")
        self.assertFalse(again)
        self.assertEqual(object_for_profile(reloaded, "copy-b", "4x3").style.font_size, item.style.font_size)

    def test_new_documents_start_with_fitted_fixed_font(self):
        from autoslice.desktop.cover_layout import text_layout

        document = document_from_basic_title_values(
            title="韩国选秀居然可以带手机，选曲全靠现场改，懂姐小音告诉你内幕",
            image_path=None, selected_timestamp=0.0, background_x=0.5, background_y=0.5, background_scale=1.0,
        )
        for item in document.objects:
            if isinstance(item, TextObject):
                layout = text_layout(item, (1440, 1080))
                self.assertLessEqual(len(layout.lines), 2)
                self.assertEqual(layout.font_size, item.style.font_size)


class StylePresetTests(unittest.TestCase):
    def test_preset_changes_colors_only_and_supports_two_tone(self):
        duo = next(preset for preset in STYLE_PRESETS if preset.key == "duo")
        style = TextStyle(font_size=130, font_family="x")
        b, a = duo.apply(style, "B"), duo.apply(style, "A")
        self.assertEqual((b.font_size, b.font_family), (130, "x"))
        self.assertNotEqual(a.fill_color, b.fill_color)
        bar = next(preset for preset in STYLE_PRESETS if preset.backdrop)
        self.assertTrue(bar.apply(style).backdrop)

    def test_style_memory_follows_streamer_and_falls_back_to_recent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CoverStyleMemoryStore(directory)
            store.save(CoverStyleMemory(fill_color="#12D8E6"), streamer=streamer_key("〖泽音〗某期标题"))
            self.assertEqual(store.load(streamer_key("【泽音】另一期")).fill_color, "#12D8E6")
            self.assertEqual(store.load(streamer_key("〖新主播〗第一期")).fill_color, "#12D8E6")
            self.assertEqual(streamer_key("没有前缀"), None)


class SplitLayoutTests(unittest.TestCase):
    def test_centered_subject_puts_a_on_top_and_b_on_bottom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame = root / "center.png"
            image = Image.new("RGB", (1920, 1080), (40, 60, 90))
            draw = ImageDraw.Draw(image)
            draw.ellipse((790, 300, 1130, 700), fill=(224, 172, 140))
            draw.rectangle((700, 700, 1220, 1080), fill=(60, 40, 120))
            image.save(frame)
            document = document_from_basic_title_values(
                title="〖泽音〗选秀带手机居然会改变选曲⁉ 懂姐小音告诉你韩娱特殊操作与内幕", image_path=str(frame),
                selected_timestamp=0.0, background_x=0.5, background_y=0.5, background_scale=1.0, font_size=104,
            )
            document = replace(document, objects=tuple(
                replace(item, asset=AssetRef(path=str(frame))) if isinstance(item, BackgroundObject) else item
                for item in document.objects
            ))
            document = CoverService(DesktopStorage(root / "data")).apply_auto_layout(document, frame)
            texts = {
                item.copy_role: object_for_profile(document, item.id, "4x3")
                for item in document.objects if isinstance(item, TextObject)
            }
            self.assertLess(texts["A"].transform.y, 0.2)
            self.assertGreater(texts["B"].transform.y, 0.6)
            self.assertEqual({texts["A"].align, texts["B"].align}, {"center"})
            # 4:3 是主画布，16:9 跟随上下分置，避免另一比例把字压在脸上。
            wide = {
                item.copy_role: object_for_profile(document, item.id, "16x9")
                for item in document.objects if isinstance(item, TextObject)
            }
            self.assertLess(wide["A"].transform.y, 0.2)
            self.assertGreater(wide["B"].transform.y, 0.6)


if __name__ == "__main__":
    unittest.main()
