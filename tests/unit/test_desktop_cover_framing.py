"""走查 M7：按主播记住上次导出时底图的放大和裁切，新视频默认沿用。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from autoslice.desktop.cover_copy import BasicCoverCopy
from autoslice.desktop.cover_editor_text import keep_text_on_canvas
from autoslice.desktop.cover_layout import canvas_size, text_layout
from autoslice.desktop.cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    Rect,
    TextObject,
    Transform,
    default_profiles,
    object_for_profile,
    set_profile_override,
)
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.cover_style import CoverFramingStore
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import SubmissionProject


def _project(title: str) -> SubmissionProject:
    return SubmissionProject(id=title, title=title, directory=".", videos=())


class CoverFramingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frame = self.root / "帧.png"
        Image.new("RGB", (1920, 1080), "#808080").save(self.frame)
        self.service = CoverService(DesktopStorage(self.root / "data"))

    def fresh(self) -> CoverDocument:
        return CoverDocument(
            objects=(BackgroundObject(id="bg", asset=AssetRef(str(self.frame))),),
            profiles=default_profiles(),
        )

    def framed(self, key: str, scale: float, pan_y: float) -> CoverDocument:
        document = self.fresh()
        background = object_for_profile(document, "bg", key)
        return set_profile_override(document, key, replace(background, scale=scale, pan_x=0.5, pan_y=pan_y))

    def test_new_video_of_the_same_streamer_reuses_the_last_exported_crop(self):
        exported = self.framed("4x3", 1.4, 0.4)
        self.service.remember_framing(_project("【泽音】上一期"), exported, ("4x3",))
        laid = self.service.default_layout(_project("【泽音】这一期"), self.fresh(), self.frame)
        shown = object_for_profile(laid, "bg", "4x3")
        self.assertEqual((shown.scale, shown.pan_x, shown.pan_y), (1.4, 0.5, 0.4))
        # 只导出过 4:3：16:9 照常自动取景
        self.assertEqual(object_for_profile(laid, "bg", "16x9").scale, 1.0)

    def test_unzoomed_export_clears_the_memory_instead_of_pinning_the_auto_focus(self):
        project = _project("【泽音】上一期")
        self.service.remember_framing(project, self.framed("4x3", 1.4, 0.4), ("4x3", "16x9"))
        store = CoverFramingStore(self.service.storage.root)
        self.assertEqual(set(store.load("泽音")), {"4x3"})
        # 后来点了“充满”不放大：自动取景的位置不该被记成下次的默认
        self.service.remember_framing(project, self.framed("4x3", 1.0, 0.3), ("4x3",))
        self.assertEqual(store.load("泽音"), {})

    def test_other_streamers_and_untagged_titles_are_not_affected(self):
        self.service.remember_framing(_project("【泽音】上一期"), self.framed("4x3", 1.4, 0.4), ("4x3",))
        for title in ("【别的主播】一期", "没有主播前缀的标题"):
            laid = self.service.default_layout(_project(title), self.fresh(), self.frame)
            self.assertEqual(object_for_profile(laid, "bg", "4x3").scale, 1.0, title)
        self.service.remember_framing(_project("没有主播前缀的标题"), self.framed("4x3", 2.0, 0.5), ("4x3",))
        self.assertEqual(CoverFramingStore(self.service.storage.root).load(None), {})

    def test_bad_records_are_ignored(self):
        store = CoverFramingStore(self.root / "store")
        store.path.parent.mkdir(parents=True)
        store.save("泽音", {"4x3": {"scale": 0.2, "pan_x": 0.5, "pan_y": 0.5}, "16x9": {"scale": "x"}})
        self.assertEqual(store.load("泽音"), {})
        store.path.write_text("不是 JSON", encoding="utf-8")
        self.assertEqual(store.load("泽音"), {})


    def test_schemes_keep_the_crop_the_user_set(self):
        # 封面走查：裁掉两侧后点快速方案 / AI 方案，取景被重置成 1.0，直播界面又全回来
        from autoslice.desktop.cover_migration import document_from_basic_title_values

        cropped = document_from_basic_title_values(
            title="【泽音】选秀带手机居然会改变选曲", image_path=str(self.frame), selected_timestamp=0.0,
            background_x=0.5, background_y=0.55, background_scale=1.4,
        )
        self.assertTrue(self.service.user_framed(cropped))
        self.assertFalse(self.service.user_framed(self.fresh()))
        variants = (BasicCoverCopy(context="当时", headline="选秀带手机居然会改变选曲"),)
        schemes = self.service.layout_schemes(cropped, self.frame, variants)
        self.assertTrue(schemes)
        for scheme in schemes:
            shown = object_for_profile(scheme.document, "background-main", "4x3")
            self.assertEqual((shown.scale, shown.pan_y), (1.4, 0.55), scheme.label)
        # 裁切之前生成的方案，套用时也不能把裁切冲掉
        uncropped = document_from_basic_title_values(
            title="【泽音】选秀带手机居然会改变选曲", image_path=str(self.frame), selected_timestamp=0.0,
            background_x=0.5, background_y=0.5, background_scale=1.0,
        )
        stale = self.service.layout_schemes(uncropped, self.frame, variants)[0]
        self.assertEqual(object_for_profile(stale.document, "background-main", "4x3").scale, 1.0)
        applied = self.service.apply_scheme(cropped, stale)
        self.assertEqual(object_for_profile(applied, "background-main", "4x3").scale, 1.4)


    def test_switching_from_a_scheme_with_context_to_a_headline_only_scheme(self):
        # 封面走查：先套“黄青”（带小字 A）再点“推荐”（只有 B），旧 A 留着和新 B 挤在一起，被拒绝套用
        from autoslice.desktop.cover_autolayout import primary_copy_ids
        from autoslice.desktop.cover_migration import document_from_basic_title_values
        from autoslice.desktop.cover_scheme_quality import safe_scheme

        document = document_from_basic_title_values(
            title="选秀带手机居然会改变选曲", image_path=str(self.frame), selected_timestamp=0.0,
            background_x=0.5, background_y=0.5, background_scale=1.0,
        )
        variants = (BasicCoverCopy(headline="选秀带手机居然会改变选曲"),
                    BasicCoverCopy(context="当时韩国就有选手", headline="他想唱的"))
        schemes = self.service.layout_schemes(document, self.frame, variants)
        with_context = next(item for item in schemes if item.key == "alternate")
        headline_only = next(item for item in schemes if item.key == "recommended")
        first = self.service.apply_scheme(document, with_context)
        a_id = primary_copy_ids(first)["A"]
        self.assertTrue(object_for_profile(first, a_id, "4x3").visible)
        second = self.service.apply_scheme(first, headline_only)
        self.assertTrue(safe_scheme(first, second, set(primary_copy_ids(second).values())))
        self.assertFalse(object_for_profile(second, a_id, "4x3").visible)
        # 再切回带 A 的方案，A 能重新出现
        third = self.service.apply_scheme(second, with_context)
        self.assertTrue(object_for_profile(third, a_id, "4x3").visible)

class KeepTextOnCanvasTests(unittest.TestCase):
    """封面走查：改长文案后字号不变多出一行，压在底部的标题掉出画布。"""

    def document(self, text: str, y: float) -> CoverDocument:
        item = TextObject(id="b", text=text, copy_role="B", transform=Transform(x=0.1, y=y), rect=Rect(width=0.72, height=0.2))
        item = replace(item, style=replace(item.style, font_size=146))
        return CoverDocument(objects=(item,), profiles=default_profiles())

    def ink(self, document: CoverDocument, key: str = "4x3"):
        return text_layout(object_for_profile(document, "b", key), canvas_size(key)).ink

    def test_bottom_text_grows_upward_and_stays_inside(self):
        before = self.document("选秀带手机居然会改变选曲", 0.62)
        typed = self.document("带手机进选秀，居然连歌都换了还不止一次", 0.62)
        kept = keep_text_on_canvas(before, typed, "b")
        height = canvas_size("4x3")[1]
        self.assertGreater(self.ink(typed).bottom, height)
        self.assertAlmostEqual(self.ink(kept).bottom, self.ink(before).bottom, delta=1)
        for key in kept.profiles:
            self.assertLessEqual(self.ink(kept, key).bottom, canvas_size(key)[1])

    def test_top_text_keeps_its_top(self):
        before = self.document("选秀带手机", 0.06)
        typed = self.document("选秀带手机居然会改变选曲", 0.06)
        kept = keep_text_on_canvas(before, typed, "b")
        self.assertAlmostEqual(self.ink(kept).top, self.ink(before).top, delta=1)


if __name__ == "__main__":
    unittest.main()
