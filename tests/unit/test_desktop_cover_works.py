"""封面作品库：导出动作各记一次，快速方案避开最近用过的构图和配色。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autoslice.desktop.cover_copy import BasicCoverCopy
from autoslice.desktop.cover_migration import document_from_basic_title_values
from autoslice.desktop.cover_model import AssetRef, BackgroundObject
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.cover_works import CoverWork, composition_signature, palette_signature
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject


def _work(composition: str, palette: str = "") -> CoverWork:
    return CoverWork(
        work_id="w", time="", streamer="泽音", project="", video="", canvas_keys=("4x3",),
        composition=composition, palette=palette, context="", headline="",
    )


class CoverWorksTests(unittest.TestCase):
    def setUp(self):
        from tests.unit.autoslice_cover.test_composition import _person_frame

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = CoverService(DesktopStorage(self.root / "data"))
        self.frame = _person_frame(self.root / "center.png", 960)
        document = document_from_basic_title_values(
            title="〖泽音〗泽音一个晚上居然被冲了万楼？！", image_path=str(self.frame), selected_timestamp=0.0,
            background_x=0.5, background_y=0.5, background_scale=1.0,
        )
        self.document = replace(document, objects=tuple(
            replace(item, asset=AssetRef(path=str(self.frame))) if isinstance(item, BackgroundObject) else item
            for item in document.objects
        ))
        folder = self.root / "〖泽音〗万楼"
        folder.mkdir()
        video = folder / "切片.mp4"
        video.touch()
        self.video = ProjectVideo("切片.mp4", str(video), "", "", False, False, "")
        self.project = SubmissionProject("p", "〖泽音〗万楼", str(folder), (self.video,))

    def test_double_export_is_one_work_and_one_style_memory(self):
        remembered = []
        original = self.service.remember_style
        self.service.remember_style = lambda *args: (remembered.append(args), original(*args))
        document = self.service.apply_auto_layout(self.document, self.frame, mode="split")
        outputs = self.service.export_both(
            self.project, self.video, document,
            work={"scheme": "split", "edits_after_scheme": 2, "basic_copy": ("A", "B")},
        )
        self.assertEqual(len(remembered), 1)
        (work,) = self.service.works.recent()
        self.assertEqual(work.canvas_keys, ("4x3", "16x9"))
        self.assertEqual(work.outputs, tuple(str(item) for item in outputs))
        self.assertEqual((work.scheme, work.edits_after_scheme, work.basic_copy), ("split", 2, ("A", "B")))
        self.assertEqual(work.streamer, "泽音")
        self.assertEqual(work.composition, composition_signature(document))
        self.assertTrue((self.service.works.root / work.thumbnail).is_file())

    def test_composition_signature_reads_where_the_text_actually_is(self):
        split = self.service.apply_auto_layout(self.document, self.frame, mode="split")
        self.assertEqual(composition_signature(split), "A上中|B下中")
        self.assertNotEqual(palette_signature(split), "")

    def test_recent_is_newest_first_and_filters_by_streamer(self):
        document = self.service.apply_auto_layout(self.document, self.frame)
        self.service.export_document(self.project, self.video, document, work={"scheme": "recommended"})
        other = SubmissionProject("q", "〖糖&牛〗别的", self.project.directory, (self.video,))
        self.service.export_document(other, self.video, document)
        self.assertEqual([item.streamer for item in self.service.works.recent()], ["糖&牛", "泽音"])
        self.assertEqual([item.streamer for item in self.service.works.recent(streamer="泽音")], ["泽音"])
        self.assertEqual(self.service.works.summary()["count"], 2)

    def test_second_scheme_avoids_the_composition_used_recently(self):
        variants = (BasicCoverCopy("泽音一个晚上", "居然被冲了万楼"), BasicCoverCopy("", "被冲了万楼？！"))
        fresh = self.service.layout_schemes(self.document, self.frame, variants)
        used = composition_signature(fresh[1].document)
        recent = tuple(_work(used) for _ in range(3))
        again = self.service.layout_schemes(self.document, self.frame, variants, recent=recent)
        self.assertNotEqual(composition_signature(again[1].document), used)

    def test_third_scheme_avoids_the_palette_used_recently(self):
        variants = (BasicCoverCopy("泽音一个晚上", "居然被冲了万楼"), BasicCoverCopy("", "被冲了万楼？！"))
        fresh = self.service.layout_schemes(self.document, self.frame, variants)
        used = palette_signature(fresh[2].document)
        again = self.service.layout_schemes(self.document, self.frame, variants, recent=(_work("", used),))
        self.assertNotEqual(palette_signature(again[2].document).split("|")[0], used.split("|")[0])


if __name__ == "__main__":
    unittest.main()
