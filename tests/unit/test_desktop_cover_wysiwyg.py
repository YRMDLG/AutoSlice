"""AutoCover 所见即所得：画布截图与导出逐像素对照，以及编辑写回契约。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QBuffer, QIODevice, QPoint, Qt
    from PySide6.QtGui import QPixmap
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None

from autoslice.desktop.cover_migration import document_from_payload
from autoslice.desktop.cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    Rect,
    ShapeObject,
    StickerObject,
    TextObject,
    TextStyle,
    Transform,
    default_profiles,
    object_for_profile,
    update_text_object,
)
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject


def _fixture_images(root: Path) -> tuple[Path, Path]:
    frame = Image.new("RGB", (1280, 720), "#3060A0")
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, 0, 160, 720), fill="#FF2020")
    draw.rectangle((1120, 0, 1280, 720), fill="#20FF20")
    for x in range(0, 1280, 80):
        draw.line((x, 0, x, 720), fill="#FFFFFF", width=3)
    frame_path = root / "frame.png"
    frame.save(frame_path)
    sticker = Image.new("RGBA", (200, 120), (255, 200, 0, 255))
    ImageDraw.Draw(sticker).ellipse((20, 10, 180, 110), fill=(200, 0, 120, 255))
    sticker_path = root / "sticker.png"
    sticker.save(sticker_path)
    return frame_path, sticker_path


def _document(frame: Path, sticker: Path) -> CoverDocument:
    background = BackgroundObject(
        id="background-main", asset=AssetRef(path=str(frame)), pan_x=0.0, pan_y=0.3, scale=1.6,
    )
    title = TextObject(
        id="copy-b", text="被冲了万楼？！", copy_role="B", z_index=10,
        transform=Transform(x=0.18, y=0.12, rotation=-8.0), rect=Rect(width=0.66, height=0.26),
        align="right", style=TextStyle(font_size=120, outer_stroke="#FFFFFF", outer_stroke_width=6, accent="#00FFFF"),
        emphasis=("万楼",),
    )
    context = TextObject(
        id="copy-a", text="一个晚上", copy_role="A", z_index=11,
        transform=Transform(x=0.06, y=0.62), rect=Rect(width=0.5, height=0.14),
        style=TextStyle(font_size=72, backdrop="#000000A0", shadow=False),
    )
    image = StickerObject(
        id="sticker-1", z_index=12, asset=AssetRef(path=str(sticker), asset_id="s1"),
        transform=Transform(x=0.55, y=0.55, scale=1.2, rotation=20.0), opacity=0.6,
    )
    shape = ShapeObject(
        id="shape-1", z_index=9, shape_type="arrow", stroke_width=10,
        transform=Transform(x=0.62, y=0.20, rotation=30.0),
    )
    return CoverDocument(objects=(background, title, context, image, shape), profiles=default_profiles())


def _grab(canvas) -> Image.Image:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    canvas.grab().save(buffer, "PNG")
    return Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGB")


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class CoverWysiwygTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frame, self.sticker = _fixture_images(self.root)
        self.document = _document(self.frame, self.sticker)
        self.canvas = CoverCanvas()
        self.addCleanup(self.canvas.close)
        self.canvas.set_background_pixmap(QPixmap(str(self.frame)))

    def _renders(self, profile_key: str) -> tuple[Image.Image, Image.Image]:
        """同一份文档的画布截图与导出图，都裁成导出尺寸。"""

        width, height = (1440, 1080) if profile_key == "4x3" else (1920, 1080)
        self.canvas.resize(width + 16, height + 16)
        self.canvas.set_document(replace(self.document, selected_object_id=None), profile_key)
        self.canvas._selected_object = "none"
        shown = _grab(self.canvas).crop((8, 8, 8 + width, 8 + height))
        service = CoverService(DesktopStorage(self.root / "data"))
        video = ProjectVideo("v.mp4", str(self.root / "v.mp4"), "", "", False, False, "")
        output = self.root / f"export-{profile_key}.jpg"
        service._render_document(self.document, video, output, canvas_key=profile_key)
        with Image.open(output) as exported:
            return shown, exported.convert("RGB")

    def _compare(self, profile_key: str) -> float:
        shown, exported = self._renders(profile_key)
        small = (shown.width // 8, shown.height // 8)
        difference = ImageChops.difference(shown.resize(small), exported.resize(small))
        return sum(ImageStat.Stat(difference).mean) / 3

    def test_canvas_matches_export_for_both_profiles(self):
        # 抗锯齿和 JPEG 压缩会带来轻微差异；方向、位置或层级错误会远超阈值。
        self.assertLess(self._compare("4x3"), 4.0)
        self.assertLess(self._compare("16x9"), 4.0)

    def test_emphasis_is_painted_the_same_on_canvas_and_export(self):
        def accent_pixels(image: Image.Image) -> int:
            return sum(1 for r, g, b in image.getdata() if r < 60 and g > 200 and b > 200)

        shown, exported = self._renders("4x3")
        self.assertGreater(accent_pixels(exported), 3000)
        self.assertAlmostEqual(accent_pixels(shown) / accent_pixels(exported), 1.0, delta=0.15)

    def test_background_drag_follows_pointer(self):
        self.canvas.resize(900, 700)
        document = replace(self.document, objects=tuple(
            replace(item, pan_x=0.5) if isinstance(item, BackgroundObject) else item
            for item in self.document.objects
        ))
        self.canvas.set_document(document, "4x3")
        self.canvas.show()
        before = self.canvas._background_box(self.canvas._find_background())
        QTest.mousePress(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(450, 650))
        QTest.mouseMove(self.canvas, QPoint(500, 650))
        after = self.canvas._background_box(self.canvas._find_background())
        QTest.mouseRelease(self.canvas, Qt.MouseButton.LeftButton, pos=QPoint(500, 650))
        factor = 1440 / self.canvas._canvas_rect().width()
        self.assertAlmostEqual(after.left - before.left, 50 * factor, delta=2)


class CoverEditWritebackTests(unittest.TestCase):
    def test_text_and_style_are_shared_but_size_stays_per_profile(self):
        text = TextObject(id="copy-b", text="旧文案", style=TextStyle(font_size=100))
        document = CoverDocument(objects=(text,), profiles=default_profiles())
        other = replace(text, style=replace(text.style, font_size=80))
        document = update_text_object(document, other, profile_key="16x9")
        edited = replace(text, text="新文案", style=replace(text.style, font_size=130, fill="#FF0000"))
        document = update_text_object(document, edited, profile_key="4x3")
        four, wide = (object_for_profile(document, "copy-b", key) for key in ("4x3", "16x9"))
        self.assertEqual((four.text, wide.text), ("新文案", "新文案"))
        self.assertEqual((four.style.fill_color, wide.style.fill_color), ("#FF0000", "#FF0000"))
        self.assertEqual((four.style.font_size, wide.style.font_size), (130, 80))

    def test_legacy_documents_keep_text_above_overlays(self):
        legacy = CoverDocument(
            objects=(
                BackgroundObject(id="background-main"),
                TextObject(id="copy-b", text="字", z_index=10),
                ImageObject(id="image-1", z_index=20),
                ShapeObject(id="shape-1", z_index=25),
            ),
            profiles=default_profiles(),
        ).to_payload()
        legacy.pop("layer_revision")
        document, migrated = document_from_payload(legacy, "标题")
        self.assertTrue(migrated)
        z = {item.id: item.z_index for item in document.objects}
        self.assertGreater(z["copy-b"], max(z["image-1"], z["shape-1"]))
        self.assertEqual(document.layer_revision, 1)
        _again, migrated_again = document_from_payload(document.to_payload(), "标题")
        self.assertFalse(migrated_again)


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class CoverEditorTextEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_typing_in_copy_box_updates_document_and_both_profiles(self):
        from autoslice.desktop.cover import CoverEditorWidget

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        project_dir = root / "项目"
        project_dir.mkdir()
        video = project_dir / "成片.mp4"
        video.write_bytes(b"video")
        project = SubmissionProject(
            "p1", "〖泽音〗泽音一个晚上居然被冲了万楼？！", str(project_dir),
            (ProjectVideo("成片.mp4", str(video), "", "", False, False, ""),),
        )
        widget = CoverEditorWidget(DesktopStorage(root / "data"))
        self.addCleanup(widget.close)
        widget.set_context(project, project.videos[0])
        widget._select_copy_role("B")
        widget.title_edit.setPlainText("改过的主文案")
        text_id = widget._selected_text_id
        for key in ("4x3", "16x9"):
            self.assertEqual(object_for_profile(widget.document, text_id, key).text, "改过的主文案")
        widget.title_edit.setPlainText("")
        self.assertFalse(object_for_profile(widget.document, text_id, "4x3").visible)


if __name__ == "__main__":
    unittest.main()
