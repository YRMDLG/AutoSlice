"""AutoCover 编辑操作契约：删除、层级、插入、样式预设与风格记忆。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from autoslice.desktop.cover_layout import text_layout
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


class SchemeTests(unittest.TestCase):
    def setUp(self):
        from tests.unit.autoslice_cover.test_composition import _person_frame

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.service = CoverService(DesktopStorage(root / "data"))
        self.frame = _person_frame(root / "center.png", 960)
        document = document_from_basic_title_values(
            title="〖泽音〗泽音一个晚上居然被冲了万楼？！", image_path=str(self.frame), selected_timestamp=0.0,
            background_x=0.5, background_y=0.5, background_scale=1.0,
        )
        self.document = replace(document, objects=tuple(
            replace(item, asset=AssetRef(path=str(self.frame))) if isinstance(item, BackgroundObject) else item
            for item in document.objects
        ))

    def _variants(self):
        from autoslice.desktop.cover_copy import BasicCoverCopy

        return (BasicCoverCopy("泽音一个晚上", "居然被冲了万楼"), BasicCoverCopy("", "被冲了万楼？！"))

    def test_three_distinct_schemes_with_thumbnails(self):
        schemes = self.service.layout_schemes(self.document, self.frame, self._variants())
        self.assertEqual(len(schemes), 3)
        self.assertEqual(len({repr(item.document.profiles) + repr(item.document.objects) for item in schemes}), 3)
        thumbnail = self.service.scheme_thumbnail(schemes[0].document, width=120)
        self.assertTrue(thumbnail.startswith(b"\xff\xd8"))
        # 换一批：第三套换配色。
        again = self.service.layout_schemes(self.document, self.frame, self._variants(), batch=1)
        self.assertNotEqual(schemes[2].label, again[2].label)

    def test_apply_scheme_keeps_user_objects(self):
        extra = TextObject(id="text-9", text="自建", copy_role="B", z_index=30)
        image = ImageObject(id="image-1", z_index=5)
        document = replace(self.document, objects=(*self.document.objects, extra, image))
        schemes = self.service.layout_schemes(document, self.frame, self._variants())
        applied = self.service.apply_scheme(document, schemes[1])
        self.assertIn(extra, applied.objects)
        self.assertIn(image, applied.objects)
        self.assertNotIn("text-9", applied.profiles["4x3"].overrides)
        before = object_for_profile(document, "copy-b", "4x3")
        after = object_for_profile(applied, "copy-b", "4x3")
        self.assertNotEqual((before.transform, before.style.font_size), (after.transform, after.style.font_size))

    def test_auto_layout_only_moves_primary_copy(self):
        extra = TextObject(id="text-9", text="自建文本框", copy_role="B", z_index=30)
        document = replace(self.document, objects=(*self.document.objects, extra))
        laid = self.service.apply_auto_layout(document, self.frame)
        self.assertEqual(object_for_profile(laid, "text-9", "4x3"), extra)

    def test_lone_headline_on_centered_subject_takes_a_bottom_band(self):
        document = set_object_visible(self.document, "copy-a", False)
        laid = self.service.apply_auto_layout(document, self.frame)
        b = object_for_profile(laid, "copy-b", "4x3")
        self.assertAlmostEqual(b.rect.width, 0.88)
        layout_bottom = b.transform.y + text_layout(b, (1440, 1080)).area.height / 1080
        self.assertAlmostEqual(layout_bottom, 0.95, delta=0.01)

    def test_stack_puts_small_context_under_big_headline(self):
        laid = self.service.apply_auto_layout(self.document, self.frame, mode="stack")
        a, b = object_for_profile(laid, "copy-a", "4x3"), object_for_profile(laid, "copy-b", "4x3")
        self.assertLess(b.transform.y, a.transform.y)
        self.assertLess(a.style.font_size, b.style.font_size)
        self.assertEqual((a.align, b.align), ("center", "center"))
        # 字号拟合后描边按比例跟随。
        self.assertAlmostEqual(b.style.stroke_width / b.style.font_size, a.style.stroke_width / a.style.font_size, delta=0.02)

    def test_overview_pick_prefers_quality_without_subtitles(self):
        from autoslice.desktop.cover_service import CoverFrame, best_overview_frame
        from autoslice_cover.video import FrameMetrics

        def frame(timestamp, score, risk=0.0):
            return CoverFrame(Path(f"{timestamp}.jpg"), timestamp, score, FrameMetrics(0.5, 0.9, 0.5, 0.5, 0.3, risk))

        self.assertEqual(best_overview_frame((frame(5, 60), frame(12, 70, 0.9), frame(20, 66))).timestamp, 20)
        self.assertIsNone(best_overview_frame(()))


class RatioSyncTests(unittest.TestCase):
    def test_sync_keeps_font_pixel_width_and_center(self):
        from autoslice.desktop.cover_model import Rect, TextWrap, Transform

        text = TextObject(
            id="copy-b", copy_role="B", text="同步到另一比例", z_index=11,
            transform=Transform(x=0.2, y=0.6), rect=Rect(width=0.6, height=0.2),
            wrap=TextWrap(max_width=0.6, max_lines=8), style=TextStyle(font_size=120),
        )
        shape = ShapeObject(id="shape-1", z_index=5, width=0.2, height=0.1, transform=Transform(x=0.1, y=0.1))
        document = CoverDocument(objects=(BackgroundObject(id="background-main"), text, shape), profiles=default_profiles())
        synced = CoverService.sync_profile(document, "4x3", "16x9")
        wide = object_for_profile(synced, "copy-b", "16x9")
        self.assertEqual(wide.style.font_size, 120)
        self.assertAlmostEqual(wide.rect.width * 1920, 0.6 * 1440, places=3)
        self.assertAlmostEqual(wide.transform.x + wide.rect.width / 2, 0.5, places=6)
        self.assertAlmostEqual(wide.transform.y, 0.6)
        moved_shape = object_for_profile(synced, "shape-1", "16x9")
        self.assertAlmostEqual(moved_shape.width * 1920, 0.2 * 1440, places=3)
        # 底图取景不同步；源比例不变。
        self.assertNotIn("background-main", synced.profiles["16x9"].overrides)
        self.assertEqual(synced.profiles["4x3"], document.profiles["4x3"])


class AssetLibraryTests(unittest.TestCase):
    def test_recent_assets_newest_first(self):
        from autoslice.desktop.cover_assets import CoverAssetLibrary

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stickers = root / "stickers" / "泽音"
            stickers.mkdir(parents=True)
            for name in ("a", "b", "c"):
                Image.new("RGBA", (16, 16), "red").save(stickers / f"{name}.png")
            library = CoverAssetLibrary(root / "data", legacy_root=root / "stickers")
            assets = {item.name: item for item in library.list_assets()}
            self.assertEqual(library.recent_assets(), ())
            first = next(item for name, item in assets.items() if name.startswith("a"))
            second = next(item for name, item in assets.items() if name.startswith("b"))
            library.mark_used(first.asset_id)
            library.mark_used(second.asset_id)
            self.assertEqual([item.asset_id for item in library.recent_assets()], [second.asset_id, first.asset_id])
            self.assertEqual(len(library.recent_assets(1)), 1)


class LockAndSharedFieldTests(unittest.TestCase):
    def test_lock_is_object_level_and_shared_fields_reach_overrides(self):
        from autoslice.desktop.cover_model import set_object_locked, update_shared_fields

        document = insert_overlay(_document(), ShapeObject(id="shape-1", stroke="#FFDB4D", stroke_width=6))
        document = set_object_locked(document, "copy-b", True)
        self.assertTrue(object_for_profile(document, "copy-b", "16x9").locked)
        moved = replace(object_for_profile(document, "shape-1", "4x3"), transform=replace(object_for_profile(document, "shape-1", "4x3").transform, x=0.3))
        profile = document.profiles["4x3"]
        document = replace(document, profiles={**document.profiles, "4x3": replace(profile, overrides={
            **profile.overrides, "shape-1": {"transform": moved.transform.to_payload(), "stroke": moved.stroke, "stroke_width": 6},
        })})
        styled = update_shared_fields(document, "shape-1", stroke="#FF0000", stroke_width=10)
        for key in ("4x3", "16x9"):
            shape = object_for_profile(styled, "shape-1", key)
            self.assertEqual((shape.stroke, shape.stroke_width), ("#FF0000", 10))
        self.assertAlmostEqual(object_for_profile(styled, "shape-1", "4x3").transform.x, 0.3)

    def test_schemes_and_ratio_sync_leave_locked_objects_alone(self):
        from autoslice.desktop.cover_model import set_object_locked
        from autoslice.desktop.cover_service import CoverScheme

        document = set_object_locked(_document(), "copy-b", True)
        source = replace(document, objects=tuple(
            replace(item, text="方案里的新字") if isinstance(item, TextObject) else item for item in document.objects
        ))
        applied = CoverService.apply_scheme(document, CoverScheme("x", "x", "x", source))
        self.assertEqual(next(item for item in applied.objects if item.id == "copy-b").text, "主文案")
        self.assertEqual(next(item for item in applied.objects if item.id == "copy-a").text, "方案里的新字")
        synced = CoverService.sync_profile(document, "4x3", "16x9")
        self.assertNotIn("copy-b", synced.profiles["16x9"].overrides)
        self.assertIn("copy-a", synced.profiles["16x9"].overrides)


class SourceChangedDraftTests(unittest.TestCase):
    def test_changed_video_keeps_layout_but_drops_old_frame(self):
        from autoslice.desktop.projects import ProjectVideo, SubmissionProject

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video_path = root / "clip.mp4"
            video_path.write_bytes(b"first")
            frame = root / "frame.jpg"
            Image.new("RGB", (64, 36), "blue").save(frame)
            project = SubmissionProject("p", "〖泽音〗某期", directory, ())
            video = ProjectVideo("clip.mp4", str(video_path), "", "", False, False, "")
            service = CoverService(DesktopStorage(root / "data"))
            document = replace(_document(), objects=tuple(
                replace(item, asset=AssetRef(path=str(frame))) if isinstance(item, BackgroundObject)
                else replace(item, text="原来的主文案") if item.id == "copy-b" else item
                for item in _document().objects
            ))
            document = replace(document, source=replace(document.source, selected_timestamp=12.5, frame_locked=True))
            service.save_document(project, video, document)
            video_path.write_bytes(b"re-exported and longer")
            loaded, read = service.load_document(project, video)
            self.assertEqual(read.status, "source_changed")
            self.assertEqual(next(item for item in loaded.objects if item.id == "copy-b").text, "原来的主文案")
            background = next(item for item in loaded.objects if isinstance(item, BackgroundObject))
            self.assertIsNone(background.asset)
            self.assertEqual(loaded.source.selected_timestamp, 12.5)
            self.assertFalse(loaded.source.frame_locked)
            self.assertFalse(service.has_draft(project, video))


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


class AccountStyleTests(unittest.TestCase):
    """配色与粗细参考 B 站“绝对忠诚的Y”的切片封面。"""

    def _preset(self, key):
        return next(preset for preset in STYLE_PRESETS if preset.key == key)

    def test_stroke_follows_font_size(self):
        classic = self._preset("classic")
        self.assertEqual(classic.apply(TextStyle(font_size=150)).stroke_width, 12)
        self.assertEqual(classic.apply(TextStyle(font_size=75)).stroke_width, 6)
        from autoslice.desktop.cover_model import resize_text_style

        resized = resize_text_style(TextStyle(font_size=100, stroke_width=8, outer_stroke_width=4), 150)
        self.assertEqual((resized.font_size, resized.stroke_width, resized.outer_stroke_width), (150, 12, 6))

    def test_red_and_purple_lines_get_white_outline_while_context_stays_yellow(self):
        for key, fill in (("yellow-red", "#F44336"), ("yellow-purple", "#6739C6")):
            preset = self._preset(key)
            b, a = preset.apply(TextStyle(font_size=120), "B"), preset.apply(TextStyle(font_size=80), "A")
            self.assertEqual((b.fill_color, b.stroke_color), (fill, "#FFFFFF"))
            self.assertEqual((a.fill_color, a.stroke_color), ("#FFE438", "#111111"))

    def test_memory_keeps_two_tone_for_next_cover(self):
        memory = CoverStyleMemory(fill_color="#16D8ED", context_fill="#FFE438", headline_size=120, stroke_width=10)
        self.assertEqual(memory.text_style(role="B").fill_color, "#16D8ED")
        a = memory.text_style(role="A")
        self.assertEqual(a.fill_color, "#FFE438")
        self.assertEqual(a.stroke_width, round(10 * a.font_size / 120))

    def test_remember_style_records_context_colors(self):
        with tempfile.TemporaryDirectory() as directory:
            service = CoverService(DesktopStorage(Path(directory) / "data"))
            duo = self._preset("duo")
            document = _document()
            document = replace(document, objects=tuple(
                replace(item, style=duo.apply(item.style, item.copy_role)) if isinstance(item, TextObject) else item
                for item in document.objects
            ))
            from autoslice.desktop.projects import SubmissionProject

            project = SubmissionProject("p", "〖泽音〗某期", directory, ())
            service.remember_style(project, document)
            memory = service.style_memory.load(streamer_key(project.title))
            self.assertEqual((memory.fill_color, memory.context_fill), ("#16D8ED", "#FFE438"))


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
