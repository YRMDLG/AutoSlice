"""AI 方案回归：自建文字避让、锁定保护、爆炸贴随文字排版。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autoslice.desktop.cover_ai import AICopy
from autoslice.desktop.cover_autolayout import CoverScheme, primary_copy_ids
from autoslice.desktop.cover_effects import add_burst, burst_id
from autoslice.desktop.cover_fix_quality import object_bounds
from autoslice.desktop.cover_model import (
    Rect,
    TextObject,
    TextStyle,
    Transform,
    object_for_profile,
    set_object_locked,
    set_object_visible,
    set_profile_override,
)
from autoslice.desktop.cover_scheme_quality import safe_scheme
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.foundation import DesktopStorage
from tests.unit.test_desktop_cover_effects import _document, _frame


class SchemeQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.frame = _frame(root / "frame.png")
        self.document = _document(self.frame)
        self.service = CoverService(DesktopStorage(root / "data"))

    def test_reordered_custom_text_is_never_taken_as_primary(self):
        custom = TextObject(id="text-90", copy_role="B", text="用户旁白")
        document = replace(self.document, objects=(custom, *self.document.objects))
        self.assertEqual(primary_copy_ids(document), {"A": "copy-a", "B": "copy-b"})
        only_custom = replace(document, objects=(custom,))
        self.assertEqual(primary_copy_ids(only_custom), {})
        legacy = replace(document, objects=(replace(custom, id="title-main"),))
        self.assertEqual(primary_copy_ids(legacy), {"B": "title-main"})

    def test_scheme_moves_burst_in_both_ratios_and_preserves_custom_style(self):
        before = add_burst(self.document, "copy-b", "4x3")
        for key in before.profiles:
            burst = object_for_profile(before, burst_id("copy-b"), key)
            before = set_profile_override(before, key, replace(burst, fill="#45ABEF", stroke="#123456"))
        target = before
        for key in before.profiles:
            text = object_for_profile(target, "copy-b", key)
            target = set_profile_override(target, key, replace(text, transform=replace(text.transform, x=0.2, y=0.25)))
        after = self.service.apply_scheme(before, CoverScheme("ai:x", "x", "x", target))
        for key in before.profiles:
            old = object_for_profile(before, burst_id("copy-b"), key)
            new = object_for_profile(after, burst_id("copy-b"), key)
            self.assertTrue(new.visible)
            self.assertEqual((new.fill, new.stroke, new.z_index), (old.fill, old.stroke, old.z_index))
            self.assertAlmostEqual(new.transform.x - old.transform.x, 0.12)
            self.assertAlmostEqual(new.transform.y - old.transform.y, -0.41)

    def test_candidates_preserve_all_text_and_follow_bursts(self):
        before = add_burst(self.document, "copy-b", "4x3")
        extra = TextObject(id="text-3", text="固定旁白", copy_role="B", z_index=30,
                           transform=Transform(x=0.03, y=0.35), rect=Rect(width=0.2),
                           style=TextStyle(font_size=40))
        before = replace(before, objects=(extra, *before.objects))
        copy = AICopy("选秀带手机", "居然会改变选曲", "当前", "", ("改变",))
        candidates = self.service.ai_candidates(before, self.frame, (copy,), preserve_copy=True)
        self.assertTrue(candidates)
        for candidate in candidates:
            for key in before.profiles:
                self.assertEqual(object_for_profile(candidate.document, extra.id, key), extra)
                a = object_for_profile(candidate.document, "copy-a", key)
                b = object_for_profile(candidate.document, "copy-b", key)
                self.assertEqual((a.text, b.text), (copy.context, copy.headline))
                self.assertTrue(a.visible)
                self.assertTrue(object_for_profile(candidate.document, burst_id("copy-b"), key).visible)
            self.assertTrue(safe_scheme(before, candidate.document, {"copy-a", "copy-b"}))

    def test_collision_in_secondary_ratio_is_rejected(self):
        extra = TextObject(id="text-3", text="固定文字", transform=Transform(x=0.05, y=0.12),
                           style=TextStyle(font_size=80))
        before = replace(self.document, objects=(*self.document.objects, extra))
        text = object_for_profile(before, "copy-b", "16x9")
        after = set_profile_override(before, "16x9", replace(text, transform=extra.transform))
        self.assertFalse(safe_scheme(before, after, {"copy-a", "copy-b"}))
        self.assertTrue(safe_scheme(before, before, {"copy-a", "copy-b"}))

    def test_new_clipping_is_rejected(self):
        text = object_for_profile(self.document, "copy-b", "4x3")
        after = set_profile_override(self.document, "4x3", replace(text, transform=Transform(x=0.98, y=0.2)))
        self.assertFalse(safe_scheme(self.document, after, {"copy-a", "copy-b"}))

    def test_locked_and_hidden_primary_are_preserved_in_candidate_previews(self):
        before = set_object_locked(self.document, "copy-b", True)
        before = set_object_visible(before, "copy-a", False)
        target = replace(before, objects=tuple(
            replace(item, text="方案改写") if isinstance(item, TextObject) else item for item in before.objects
        ))
        after = self.service.apply_scheme(before, CoverScheme("ai:x", "x", "x", target))
        for key in before.profiles:
            self.assertEqual(object_for_profile(before, "copy-b", key), object_for_profile(after, "copy-b", key))
            self.assertEqual(object_for_profile(before, "copy-a", key), object_for_profile(after, "copy-a", key))

    def test_local_thumbnails_match_applied_bursts(self):
        from autoslice.desktop.cover_copy import BasicCoverCopy

        before = add_burst(self.document, "copy-b", "4x3")
        schemes = self.service.layout_schemes(
            before, self.frame, (BasicCoverCopy("选秀带手机", "居然会改变选曲", emphasis=("改变",)),),
        )
        self.assertTrue(schemes)
        for scheme in schemes:
            applied = self.service.apply_scheme(before, scheme)
            for key in before.profiles:
                self.assertEqual(
                    self.service.scheme_thumbnail(applied, canvas_key=key, width=200),
                    self.service.scheme_thumbnail(scheme.document, canvas_key=key, width=200),
                )

    def test_burst_stays_behind_its_text_after_relayout(self):
        before = add_burst(self.document, "copy-b", "4x3")
        candidates = self.service.ai_candidates(
            before, self.frame, (AICopy("选秀带手机", "居然会改变选曲", "当前", "", ("改变",)),),
            preserve_copy=True,
        )
        self.assertTrue(candidates)
        for candidate in candidates:
            for key in before.profiles:
                text = object_for_profile(candidate.document, "copy-b", key)
                burst = object_for_profile(candidate.document, burst_id(text.id), key)
                box, shape = object_bounds(text, key), object_bounds(burst, key)
                self.assertLess(shape.left, box.right)
                self.assertGreater(shape.right, box.left)
                self.assertLess(shape.top, box.bottom)
                self.assertGreater(shape.bottom, box.top)
                self.assertLess(burst.z_index, text.z_index)


if __name__ == "__main__":
    unittest.main()
