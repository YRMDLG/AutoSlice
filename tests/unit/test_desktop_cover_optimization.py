"""AI 改一改的多场景回归：自定义对象、约束保护、成品筛选与原图胜出。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from autoslice.desktop.ai_settings import AISettings
from autoslice.desktop.cover_ai import AIFix, CoverAI, CoverAIError, contact_sheet
from autoslice.desktop.cover_effects import add_burst, burst_id
from autoslice.desktop.cover_fix_quality import describe_scene, safe_candidate, visually_distinct
from autoslice.desktop.cover_fix_schema import AIObjectEdit, object_changes
from autoslice.desktop.cover_fixes import apply_fix, describe_text_layers
from autoslice.desktop.cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    ShapeObject,
    StickerObject,
    TextObject,
    Transform,
    object_for_profile,
    set_profile_override,
)
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.foundation import DesktopStorage

CONFIGURED = AISettings(base_url="https://example.invalid/v1", has_token=True, text_model="t", vision_model="v", source="file")


class CoverOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frame = self.root / "背景 with 空格.png"
        Image.new("RGB", (960, 720), "#808080").save(self.frame)
        self.service = CoverService(DesktopStorage(self.root / "data"))

    def document(self, count=3):
        texts = []
        for index in range(count):
            item = TextObject(id=f"custom-{index}", text=f"第{index + 1}段自定义文字", transform=Transform(x=0.08, y=0.08 + index * 0.16))
            texts.append(replace(item, style=replace(item.style, font_size=65)))
        return CoverDocument(objects=(BackgroundObject(id="bg", asset=AssetRef(str(self.frame))), *texts))

    def ai(self, reply):
        self.reply_number = getattr(self, "reply_number", 0) + 1
        storage = DesktopStorage(self.root / f"reply-{self.reply_number}")
        return CoverAI(storage, llm=lambda *_args, **_kwargs: json.dumps(reply, ensure_ascii=False), settings=lambda: CONFIGURED)

    def test_known_unlocked_targets_and_finite_parameters_only(self):
        scene = [
            {"id": "text", "kind": "text", "text": "今天真的很好看"},
            {"id": "locked", "kind": "text", "locked": True},
            {"id": "hidden", "kind": "text", "visible": False},
            {"id": "bg", "kind": "background"},
        ]
        changes = object_changes([
            {"id": "text", "scale": float("nan"), "dx": True, "dy": 999, "fill": "url(file:///bad)", "text": "不能替换"},
            {"id": "locked", "scale": 1.2}, {"id": "hidden", "scale": 1.2},
            {"id": "unknown", "scale": 1.2}, {"id": "bg", "scale": 1.2},
            {"id": "text", "scale": 1.15, "fill": "#aabbcc", "words": ["很好", "不存在"]},
            {"id": "text", "dx": 0.2},
        ], scene)
        self.assertEqual(changes, (AIObjectEdit("text", scale=1.15, fill="#AABBCC", words=("很好",), name="今天真的很好看"),))

    def test_prompt_and_parser_support_all_scene_objects_without_replacing_copy(self):
        document = self.document()
        document = replace(document, objects=(*document.objects, ShapeObject(id="badge")))
        calls = []

        def model(prompt, **_kwargs):
            calls.append(prompt)
            return json.dumps({"fixes": [{"issue": "拉开间距", "actions": ["edit_objects"], "edits": [
                {"id": "custom-2", "dy": 0.03, "text": "偷偷换字"}, {"id": "badge", "dx": 0.1},
            ]}]})

        ai = CoverAI(self.service.storage, llm=model, settings=lambda: CONFIGURED)
        fixes = ai.suggest_fixes(b"cover", scene=describe_scene(document, "4x3"), text_layers=describe_text_layers(document, "4x3"))
        self.assertEqual([edit.object_id for edit in fixes[0].edits], ["custom-2", "badge"])
        self.assertIn("多人", calls[0])
        self.assertIn("不要假定", calls[0])
        self.assertIn("bounds", calls[0])
        self.assertNotIn(str(self.frame), calls[0])
        self.assertIn("第3段自定义文字", fixes[0].steps[0])
        after = apply_fix(self.service, document, self.frame, fixes[0], "4x3")
        self.assertEqual(object_for_profile(after, "custom-2", "4x3").text, "第3段自定义文字")

    def test_arbitrary_text_count_and_both_profiles_keep_other_geometry(self):
        for count in (1, 2, 5):
            for key, other in (("4x3", "16x9"), ("16x9", "4x3")):
                with self.subTest(count=count, profile=key):
                    before = self.document(count)
                    target = f"custom-{count - 1}"
                    fix = AIFix("局部调整", ("edit_objects",), edits=(AIObjectEdit(target, scale=1.1, dx=0.03, fill="#FFFFFF"),))
                    after = apply_fix(self.service, before, self.frame, fix, key)
                    original = object_for_profile(before, target, key)
                    updated = object_for_profile(after, target, key)
                    self.assertGreater(updated.style.font_size, original.style.font_size)
                    self.assertEqual(updated.text, original.text)
                    self.assertEqual(object_for_profile(after, target, other).transform, object_for_profile(before, target, other).transform)
                    self.assertEqual(object_for_profile(after, target, other).style.font_size, original.style.font_size)
                    for index in range(count - 1):
                        self.assertEqual(object_for_profile(after, f"custom-{index}", key), object_for_profile(before, f"custom-{index}", key))

    def test_legacy_style_changes_include_custom_text_and_honor_locks(self):
        before = self.document(3)
        before = replace(before, objects=tuple(replace(item, locked=True) if item.id == "custom-1" else item for item in before.objects))
        after = apply_fix(self.service, before, self.frame, AIFix("动感", ("tilt", "outline")), "4x3")
        self.assertEqual(object_for_profile(before, "custom-1", "4x3"), object_for_profile(after, "custom-1", "4x3"))
        self.assertEqual(object_for_profile(after, "custom-2", "4x3").transform.rotation, -6)

    def test_locked_and_hidden_objects_are_described_but_never_changed(self):
        document = self.document()
        document = replace(document, objects=tuple(
            replace(item, locked=True) if item.id == "custom-0" else replace(item, visible=False) if item.id == "custom-1" else item
            for item in document.objects
        ))
        self.assertEqual(len(describe_scene(document, "4x3")), 4)
        self.assertEqual([row["id"] for row in describe_text_layers(document, "4x3")], ["custom-2"])
        fix = AIFix("不能动", ("edit_objects",), edits=(AIObjectEdit("custom-0", dx=0.1), AIObjectEdit("custom-1", dx=0.1)))
        self.assertEqual(apply_fix(self.service, document, self.frame, fix, "4x3"), document)

    def test_images_stickers_shapes_and_linked_burst_follow(self):
        before = add_burst(self.document(1), "custom-0", "4x3")
        before = replace(before, objects=(*before.objects,
            ImageObject(id="portrait", asset=AssetRef(str(self.frame))),
            StickerObject(id="sticker", asset=AssetRef(str(self.frame))), ShapeObject(id="shape"),
        ))
        edits = tuple(AIObjectEdit(object_id, dx=0.03, scale=1.1) for object_id in ("custom-0", "portrait", "sticker", "shape"))
        after = apply_fix(self.service, before, self.frame, AIFix("对象调整", ("edit_objects",), edits=edits), "4x3")
        for object_id in ("portrait", "sticker", "shape", burst_id("custom-0")):
            self.assertNotEqual(object_for_profile(before, object_id, "4x3"), object_for_profile(after, object_id, "4x3"))
            self.assertEqual(object_for_profile(before, object_id, "16x9"), object_for_profile(after, object_id, "16x9"))

    def test_new_clipping_overlap_and_tiny_text_are_rejected(self):
        before = self.document(2)
        for edit in (AIObjectEdit("custom-0", dy=-0.3), AIObjectEdit("custom-1", dy=-0.16)):
            after = apply_fix(self.service, before, self.frame, AIFix("坏方案", ("edit_objects",), edits=(edit,)), "4x3")
            self.assertFalse(safe_candidate(before, after, "4x3"))
        old = object_for_profile(before, "custom-0", "4x3")
        tiny = set_profile_override(before, "4x3", replace(old, style=replace(old.style, font_size=12)))
        self.assertFalse(safe_candidate(before, tiny, "4x3"))

    def test_existing_overlap_cropping_and_locked_obstacles_can_be_preserved(self):
        before = self.document(2)
        before = replace(before, objects=tuple(
            replace(item, transform=Transform(x=-0.02, y=0.1, rotation=-6)) if isinstance(item, TextObject) else item
            for item in before.objects
        ))
        after = apply_fix(self.service, before, self.frame, AIFix("只调色", ("edit_objects",), edits=(AIObjectEdit("custom-1", fill="#FFFFFF"),)), "4x3")
        self.assertTrue(safe_candidate(before, after, "4x3"))

    def test_manual_lines_mixed_characters_and_original_words_survive_edits(self):
        before = self.document(1)
        text = replace(before.objects[1], text="第一句  保留空格\nABC 2026 第二句", emphasis=("ABC",))
        before = replace(before, objects=(before.objects[0], text))
        edit = AIObjectEdit(text.id, width_scale=1.2, line_spacing=1.3, accent="#00CCFF")
        after = apply_fix(self.service, before, self.frame, AIFix("排版", ("edit_objects",), edits=(edit,)), "4x3")
        self.assertEqual(object_for_profile(after, text.id, "4x3").text, text.text)
        self.assertEqual(object_for_profile(after, text.id, "16x9").text, text.text)
        self.assertEqual(object_for_profile(after, text.id, "4x3").emphasis, text.emphasis)

    def test_background_lock_and_covers_without_text(self):
        background = replace(self.document().objects[0], locked=True)
        before = CoverDocument(objects=(background,))
        for action in ("zoom_in", "zoom_out", "move_top", "bigger", "rewrite"):
            with self.subTest(action=action):
                self.assertEqual(apply_fix(self.service, before, self.frame, AIFix("保持", (action,)), "4x3"), before)
        portrait = ImageObject(id="subject", asset=AssetRef(str(self.frame)), transform=Transform(x=0.1, y=0.1))
        before = replace(before, objects=(*before.objects, portrait))
        after = apply_fix(self.service, before, self.frame, AIFix("取景", ("edit_objects",), edits=(AIObjectEdit("subject", dx=0.03),)), "4x3")
        self.assertTrue(safe_candidate(before, after, "4x3"))
        self.assertEqual(object_for_profile(after, background.id, "4x3"), background)

    def test_shared_spacing_cannot_break_other_aspect_ratio(self):
        before = self.document(1)
        text = replace(before.objects[1], text="上面一行\n下面一行")
        text = replace(text, style=replace(text.style, font_size=80, line_spacing=1.0))
        before = replace(before, objects=(before.objects[0], text))
        before = set_profile_override(before, "16x9", replace(text, transform=replace(text.transform, y=0.84)))
        after = apply_fix(self.service, before, self.frame, AIFix("行距", ("edit_objects",), edits=(AIObjectEdit(text.id, line_spacing=1.8),)), "4x3")
        self.assertFalse(safe_candidate(before, after, "4x3"))

    def test_current_visible_text_can_share_color_with_hidden_other_profile(self):
        before = self.document(1)
        text = before.objects[1]
        before = set_profile_override(before, "16x9", replace(text, visible=False))
        after = apply_fix(self.service, before, self.frame, AIFix("配色", ("edit_objects",), edits=(AIObjectEdit(text.id, fill="#FFFFFF"),)), "4x3")
        self.assertTrue(safe_candidate(before, after, "4x3"))
        hidden = object_for_profile(after, text.id, "16x9")
        self.assertFalse(hidden.visible)
        self.assertEqual(hidden.transform, text.transform)

    def test_legacy_recolor_keeps_intentional_cropping(self):
        before = self.document(1)
        text = replace(before.objects[1], transform=Transform(x=-0.04, y=0.1))
        before = replace(before, objects=(before.objects[0], text))
        after = apply_fix(self.service, before, self.frame, AIFix("配色", ("restyle",), preset="white"), "4x3")
        self.assertEqual(object_for_profile(after, text.id, "4x3").transform, text.transform)

    def test_linked_burst_respects_its_own_lock(self):
        before = add_burst(self.document(1), "custom-0", "4x3")
        before = replace(before, objects=tuple(replace(item, locked=True) if item.id == burst_id("custom-0") else item for item in before.objects))
        after = apply_fix(self.service, before, self.frame, AIFix("移动", ("edit_objects",), edits=(AIObjectEdit("custom-0", dx=0.03),)), "4x3")
        self.assertEqual(object_for_profile(before, burst_id("custom-0"), "4x3"), object_for_profile(after, burst_id("custom-0"), "4x3"))

    def test_visual_comparison_ignores_invisible_changes_and_duplicates(self):
        before = self.service.scheme_thumbnail(self.document(1), width=560)
        self.assertFalse(visually_distinct(before, before))
        document = self.document(1)
        after = apply_fix(self.service, document, self.frame, AIFix("换色", ("edit_objects",), edits=(AIObjectEdit("custom-0", fill="#FFFFFF"),)), "4x3")
        self.assertTrue(visually_distinct(before, self.service.scheme_thumbnail(after, width=560)))
        with Image.open(io.BytesIO(before)) as image:
            image.putpixel((0, 0), (255, 0, 0))
            output = io.BytesIO()
            image.save(output, format="PNG")
        self.assertFalse(visually_distinct(before, output.getvalue()))

    def test_review_rejects_original_unknown_duplicate_and_unexplained_picks(self):
        result = self.ai({"picks": [
            {"index": 1, "reason": "原图"}, {"index": 3, "reason": "间距更清楚"},
            {"index": 3, "reason": "重复"}, {"index": 2}, {"index": True, "reason": "错误"},
            {"index": 99, "reason": "越界"},
        ]}).review_fixes(b"sheet", 2)
        self.assertEqual([(pick.index, pick.reason) for pick in result.picks], [(1, "间距更清楚")])
        self.assertEqual(self.ai({"picks": [], "rejected": "原图更协调"}).review_fixes(b"sheet", 2).picks, ())
        with self.assertRaises(CoverAIError):
            self.ai({"fixes": []}).review_fixes(b"sheet", 2)

    def test_review_sheet_includes_original_and_preserves_aspect_ratio(self):
        images = tuple(self.service.scheme_thumbnail(self.document(count), width=240) for count in (1, 2, 3))
        with Image.open(io.BytesIO(contact_sheet(images, labels_outside=True))) as sheet:
            self.assertGreater(sheet.width, 3 * 240)

    def test_review_numbers_do_not_cover_top_left_content(self):
        output = io.BytesIO()
        Image.new("RGB", (240, 180), "white").save(output, format="PNG")
        with Image.open(io.BytesIO(contact_sheet((output.getvalue(),), labels_outside=True))) as sheet:
            self.assertTrue(all(channel > 245 for channel in sheet.getpixel((15, 60))))


if __name__ == "__main__":
    unittest.main()
