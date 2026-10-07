"""构图分析：人脸避让、比例裁切保主体、字幕带识别。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from autoslice.desktop.cover_migration import document_from_basic_title_values
from autoslice.desktop.cover_model import AssetRef, BackgroundObject, TextObject, object_for_profile
from autoslice.desktop.cover_service import CoverFrame, CoverService, recommended_frame
from autoslice.desktop.foundation import DesktopStorage
from autoslice_cover.composition import best_crop_focus, region_cost, saliency_map
from autoslice_cover.video import FrameMetrics


def _person_frame(path: Path, center_x: int, *, subtitle: bool = False, texture: bool = True) -> Path:
    image = Image.new("RGB", (1920, 1080), (40, 60, 90))
    draw = ImageDraw.Draw(image)
    draw.ellipse((center_x - 170, 220, center_x + 170, 640), fill=(224, 172, 140))
    draw.rectangle((center_x - 260, 640, center_x + 260, 1080), fill=(60, 40, 120))
    # 衣服纹理：真实画面里身体区域并不是平涂色块。
    for y in (range(650, 1080, 18) if texture else ()):
        draw.line((center_x - 260, y, center_x + 260, y), fill=(200, 190, 230), width=4)
    if subtitle:
        for x in range(420, 1500, 26):
            draw.rectangle((x, 880, x + 14, 930), fill=(255, 255, 255))
    image.save(path)
    return path


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_face_region_costs_more_than_empty_side(self):
        saliency = saliency_map(_person_frame(self.root / "right.png", 1500))
        face_side = region_cost(saliency, (0.6, 0.1, 0.95, 0.5))
        empty_side = region_cost(saliency, (0.05, 0.1, 0.4, 0.5))
        self.assertGreater(face_side, empty_side * 3)

    def test_crop_focus_keeps_edge_subject_and_stays_centered_otherwise(self):
        edge = saliency_map(_person_frame(self.root / "edge.png", 1760))
        center = saliency_map(_person_frame(self.root / "center.png", 960))
        self.assertGreater(best_crop_focus(edge, 4 / 3)[0], 0.8)
        self.assertEqual(best_crop_focus(center, 4 / 3), (0.5, 0.5))
        self.assertEqual(best_crop_focus(center, 16 / 9), (0.5, 0.5))

    def test_burned_subtitle_band_is_detected(self):
        saliency = saliency_map(_person_frame(self.root / "sub.png", 960, subtitle=True, texture=False))
        self.assertIsNotNone(saliency.subtitle_band)
        top, bottom = saliency.subtitle_band
        self.assertLess(abs((top + bottom) / 2 - 0.84), 0.08)
        self.assertIsNone(saliency_map(_person_frame(self.root / "clean.png", 960, texture=False)).subtitle_band)


class AutoLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = CoverService(DesktopStorage(self.root / "data"))

    def _layout(self, center_x: int):
        path = _person_frame(self.root / f"frame-{center_x}.png", center_x)
        document = document_from_basic_title_values(
            title="〖泽音〗泽音一个晚上居然被冲了万楼？！", image_path=str(path), selected_timestamp=0.0,
            background_x=0.5, background_y=0.5, background_scale=1.0, font_size=104,
        )
        document = replace(document, objects=tuple(
            replace(item, asset=AssetRef(path=str(path))) if isinstance(item, BackgroundObject) else item
            for item in document.objects
        ))
        return self.service.apply_auto_layout(document, path)

    def _text_x(self, document, key):
        return [
            object_for_profile(document, item.id, key).transform.x
            for item in document.objects if isinstance(item, TextObject)
        ]

    def test_text_goes_to_the_side_without_the_person(self):
        self.assertTrue(all(x < 0.2 for x in self._text_x(self._layout(1500), "4x3")))
        self.assertTrue(all(x > 0.4 for x in self._text_x(self._layout(420), "4x3")))

    def test_each_profile_frames_the_subject_independently(self):
        document = self._layout(1760)
        four = object_for_profile(document, "background-main", "4x3")
        wide = object_for_profile(document, "background-main", "16x9")
        self.assertGreater(four.pan_x, 0.8)
        self.assertEqual(wide.pan_x, 0.5)


class FrameRecommendationTests(unittest.TestCase):
    def _frame(self, timestamp, score, subtitle_risk=0.0):
        metrics = FrameMetrics(0.5, 0.9, 0.5, 0.5, 0.3, subtitle_risk)
        return CoverFrame(Path(f"{timestamp}.jpg"), timestamp, score, metrics)

    def test_only_clearly_better_frame_is_recommended(self):
        frames = tuple(self._frame(t, s) for t, s in ((0.0, 50), (0.4, 51), (0.8, 60), (1.2, 50)))
        self.assertEqual(recommended_frame(frames).timestamp, 0.8)
        flat = tuple(self._frame(t, 50 + t) for t in (0.0, 0.4, 0.8, 1.2))
        self.assertIsNone(recommended_frame(flat))

    def test_subtitle_risk_blocks_recommendation(self):
        frames = (self._frame(0.0, 50), self._frame(0.4, 50), self._frame(0.8, 70, subtitle_risk=0.8))
        self.assertIsNone(recommended_frame(frames))


if __name__ == "__main__":
    unittest.main()
