"""精修效果：爆炸贴、集中线、垫爆炸贴、拼图小图白边、模板（存、套、批量）。两端画得一样。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageStat

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from autoslice.desktop.cover_effects import add_burst, burst_id
from autoslice.desktop.cover_layout import canvas_size, document_layers, text_layout
from autoslice.desktop.cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    ShapeObject,
    StickerObject,
    TextObject,
    TextStyle,
    Transform,
    default_profiles,
    insert_overlay,
    object_for_profile,
    update_text_object,
)
from autoslice.desktop.cover_templates import (
    TEMPLATE_PREFIX,
    CoverTemplateStore,
    apply_template,
    make_template,
)
from autoslice_cover.document_render import compose_document

try:
    from PySide6.QtCore import QBuffer, QIODevice, QPointF
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None


def _frame(path: Path) -> Path:
    image = Image.new("RGB", (1920, 1080), (60, 90, 140))
    ImageDraw.Draw(image).ellipse((800, 200, 1120, 620), fill=(230, 180, 150))
    image.save(path)
    return path


def _document(frame: Path) -> CoverDocument:
    headline = TextObject(
        id="copy-b", text="居然会改变选曲", copy_role="B", z_index=10, emphasis=("改变",),
        transform=Transform(x=0.08, y=0.66), style=TextStyle(font_size=130, fill="#FFFFFF", stroke="#111111", stroke_width=10),
    )
    context = TextObject(
        id="copy-a", text="选秀带手机", copy_role="A", z_index=11, transform=Transform(x=0.2, y=0.55),
        style=TextStyle(font_size=80),
    )
    return CoverDocument(
        objects=(BackgroundObject(id="background-main", asset=AssetRef(path=str(frame))), headline, context),
        profiles=default_profiles(),
    )


def _render(document: CoverDocument, key: str = "4x3") -> Image.Image:
    background, layers = document_layers(document, key)
    return compose_document(canvas_size(key), background, layers)


def _count(image: Image.Image, color: tuple[int, int, int], box=None, tolerance: int = 40) -> int:
    region = image.crop(box) if box else image
    return sum(1 for pixel in region.getdata() if all(abs(a - b) <= tolerance for a, b in zip(pixel, color)))


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.frame = _frame(Path(self.temp.name) / "frame.png")

    def test_new_shapes_round_trip_and_render(self):
        burst = ShapeObject(id="shape-1", shape_type="burst", fill="#FF3B30", stroke="#111111", stroke_width=6,
                            width=0.3, height=0.26, transform=Transform(x=0.05, y=0.05))
        focus = ShapeObject(id="shape-2", shape_type="focus", stroke="#111111", width=1.0, height=1.0,
                            transform=Transform(x=0.0, y=0.0))
        for item in (burst, focus):
            self.assertEqual(ShapeObject.from_payload(item.to_payload()), item)
        document = insert_overlay(insert_overlay(_document(self.frame), burst), focus)
        image = _render(document)
        # 爆炸贴是红的；集中线在画面边缘是黑的，中间空着。
        self.assertGreater(_count(image, (0xFF, 0x3B, 0x30), (72, 54, 72 + 432, 54 + 281)), 20000)
        self.assertGreater(_count(image, (17, 17, 17), (0, 0, 1440, 60)), 5000)
        self.assertEqual(_count(image, (17, 17, 17), (620, 380, 820, 520)), 0)


class BurstTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.document = _document(_frame(Path(self.temp.name) / "frame.png"))

    def test_burst_goes_behind_the_emphasized_word_in_both_ratios(self):
        document = add_burst(self.document, "copy-b", "4x3")
        shape = next(item for item in document.objects if item.id == burst_id("copy-b"))
        self.assertEqual(shape.shape_type, "burst")
        # 在所有字下面。
        self.assertLess(shape.z_index, min(item.z_index for item in document.objects if isinstance(item, TextObject)))
        for key in ("4x3", "16x9"):
            width, height = canvas_size(key)
            placed = object_for_profile(document, shape.id, key)
            layout = text_layout(object_for_profile(document, "copy-b", key), (width, height))
            accent = [run for line in layout.lines for run in line.runs if run.accent]
            self.assertTrue(accent)
            line = next(line for line in layout.lines if any(run.accent for run in line.runs))
            word_left = line.origin_x + next(run for run in line.runs if run.accent).offset
            center_x = (placed.transform.x + placed.width / 2) * width
            # 爆炸贴对准强调词（不是整段字的中间）。
            self.assertLess(abs(center_x - (word_left + 130)), 140, key)
            self.assertGreater(placed.width * width, 260)
        # 强调词没被拆到两行，爆炸贴盖住整个词。
        layout = text_layout(object_for_profile(document, "copy-b", "4x3"), canvas_size("4x3"))
        self.assertTrue(any("改变" in line.text for line in layout.lines))
        # 再垫一次不重复。
        self.assertIs(add_burst(document, "copy-b", "4x3"), document)

    def test_burst_follows_tilted_text(self):
        current = object_for_profile(self.document, "copy-b", "4x3")
        tilted = update_text_object(
            self.document, replace(current, transform=replace(current.transform, rotation=-6.0)), profile_key="4x3",
        )
        shape = object_for_profile(add_burst(tilted, "copy-b", "4x3"), burst_id("copy-b"), "4x3")
        self.assertEqual(shape.transform.rotation, -6.0)


class PictureBorderTests(unittest.TestCase):
    def test_border_round_trips_and_is_drawn_inside_the_picture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        frame = _frame(root / "frame.png")
        evidence = root / "evidence.png"
        Image.new("RGB", (640, 360), (20, 20, 20)).save(evidence)
        picture = ImageObject(id="image-1", asset=AssetRef(path=str(evidence)), transform=Transform(x=0.5, y=0.1, scale=1.6),
                              border="#ffffff", border_width=10)
        self.assertEqual(ImageObject.from_payload(picture.to_payload()).border, "#FFFFFF")
        image = _render(insert_overlay(_document(frame), replace(picture, border="#FFFFFF")))
        # 小图 0.384 倍画布宽：左上角往里 5 像素是白边，往里 30 像素是图片本身。
        left, top = round(0.5 * 1440), round(0.1 * 1080)
        self.assertGreater(min(image.getpixel((left + 5, top + 40))), 230)
        self.assertLess(max(image.getpixel((left + 30, top + 40))), 40)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.frames = root / "cover-frames"
        self.frames.mkdir()
        self.frame = _frame(self.frames / "frame.png")
        logo = root / "logo.png"
        Image.new("RGBA", (120, 120), (255, 0, 0, 255)).save(logo)
        document = _document(self.frame)
        document = insert_overlay(document, StickerObject(id="sticker-1", asset=AssetRef(path=str(logo)),
                                                          transform=Transform(x=0.8, y=0.05)))
        # 从视频截的拼图小图不进模板。
        document = insert_overlay(document, ImageObject(id="image-1", asset=AssetRef(path=str(self.frame))))
        self.source = add_burst(document, "copy-b", "4x3")

    def test_template_keeps_layout_and_overlays_but_not_content(self):
        template = make_template(self.source, "泽音日更", frames_dir=self.frames)
        ids = {item.id for item in template.document.objects}
        self.assertEqual(ids, {"background-main", "copy-b", "copy-a", "sticker-1"})
        self.assertEqual(template.bursts, ("B",))
        texts = [item for item in template.document.objects if isinstance(item, TextObject)]
        self.assertTrue(all(item.text == "" and item.emphasis == () for item in texts))
        background = next(item for item in template.document.objects if isinstance(item, BackgroundObject))
        self.assertIsNone(background.asset)

    def test_apply_keeps_copy_takes_style_and_position_and_does_not_pile_up(self):
        template = make_template(self.source, "泽音日更", frames_dir=self.frames)
        other_frame = _frame(Path(self.temp.name) / "other.png")
        target = CoverDocument(
            objects=(
                BackgroundObject(id="background-main", asset=AssetRef(path=str(other_frame))),
                TextObject(id="copy-b", text="一个晚上被冲了万楼", copy_role="B", emphasis=("万楼",),
                           transform=Transform(x=0.4, y=0.1), style=TextStyle(font_size=90, fill="#00FF00")),
            ),
            profiles=default_profiles(),
        )
        applied = apply_template(target, template)
        headline = object_for_profile(applied, "copy-b", "4x3")
        model = object_for_profile(self.source, "copy-b", "4x3")
        self.assertEqual((headline.text, headline.emphasis), ("一个晚上被冲了万楼", ("万楼",)))
        self.assertEqual((headline.style.fill, headline.style.font_size), (model.style.fill, model.style.font_size))
        self.assertEqual((headline.transform.x, headline.transform.y), (model.transform.x, model.transform.y))
        # 模板有 A、当前没有：补一个隐藏的 A 块，等有文字再显示。
        context = next(item for item in applied.objects if isinstance(item, TextObject) and item.copy_role == "A")
        self.assertFalse(context.visible)
        self.assertIn(TEMPLATE_PREFIX + "sticker-1", {item.id for item in applied.objects})
        # 爆炸贴按新文案重新垫在强调词后面。
        self.assertIn(burst_id("copy-b"), {item.id for item in applied.objects})
        again = apply_template(applied, template)
        self.assertEqual(
            sum(item.id.startswith(TEMPLATE_PREFIX) for item in again.objects),
            sum(item.id.startswith(TEMPLATE_PREFIX) for item in applied.objects),
        )

    def test_store_lists_recently_used_first_and_deletes(self):
        store = CoverTemplateStore(Path(self.temp.name) / "data")
        first = make_template(self.source, "一", frames_dir=self.frames)
        second = make_template(self.source, "二", frames_dir=self.frames)
        store.save(first)
        store.save(second)
        store.mark_used(first.template_id)
        self.assertEqual([item.name for item in store.list()], ["一", "二"])
        loaded = store.get(first.template_id)
        self.assertEqual(apply_template(_document(self.frame), loaded), apply_template(_document(self.frame), first))
        store.delete(first.template_id)
        self.assertEqual([item.name for item in store.list()], ["二"])


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class CanvasMatchesExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_burst_focus_and_border_look_the_same_on_canvas(self):
        from autoslice.desktop.cover_canvas import CoverCanvas

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        frame = _frame(root / "frame.png")
        evidence = root / "evidence.png"
        Image.new("RGB", (640, 360), (20, 120, 20)).save(evidence)
        document = add_burst(_document(frame), "copy-b", "4x3")
        document = insert_overlay(document, ShapeObject(
            id="shape-9", shape_type="focus", stroke="#111111", width=1.0, height=1.0, transform=Transform(x=0.0, y=0.0),
        ))
        document = insert_overlay(document, ImageObject(
            id="image-1", asset=AssetRef(path=str(evidence)), transform=Transform(x=0.55, y=0.06, scale=1.6, rotation=3.0),
            border="#FFFFFF", border_width=10,
        ))
        canvas = CoverCanvas()
        self.addCleanup(canvas.close)
        canvas.resize(1440 + 16, 1080 + 16)
        canvas.set_background_pixmap(QPixmap(str(frame)))
        canvas.set_document(replace(document, selected_object_id=None), "4x3")
        canvas._selected_object = "none"
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        canvas.grab().save(buffer, "PNG")
        shown = Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGB").crop((8, 8, 1448, 1088))
        exported = _render(document)
        difference = ImageChops.difference(shown.resize((180, 135)), exported.resize((180, 135)))
        self.assertLess(sum(ImageStat.Stat(difference).mean) / 3, 4.0)
        # 集中线中间空地点下去不算点中集中线（还能拖底图），点在边上的线圈才算。
        rect = canvas._canvas_rect()
        center = QPointF(rect.center().x(), rect.center().y())
        edge = QPointF(rect.left() + 6, rect.center().y())
        hit_center = canvas._hit_object(center)
        self.assertFalse(isinstance(hit_center, ShapeObject) and hit_center.shape_type == "focus")
        hit_edge = canvas._hit_object(edge)
        self.assertTrue(isinstance(hit_edge, ShapeObject) and hit_edge.shape_type == "focus")


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class EditorEffectsTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover import CoverEditorWidget
        from autoslice.desktop.cover_draft import CoverDraft
        from autoslice.desktop.foundation import DesktopStorage
        from autoslice.desktop.projects import ProjectVideo, SubmissionProject

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app = QApplication.instance() or QApplication([])
        folder = root / "【泽音】选秀带手机居然会改变选曲"
        folder.mkdir()
        video = ProjectVideo("切片.mp4", str(folder / "切片.mp4"), "", "", False, False, "")
        self.project = SubmissionProject("p", folder.name, str(folder), (video,))
        self.widget = CoverEditorWidget(DesktopStorage(root / "data"))
        self.addCleanup(self.widget.deleteLater)
        self.addCleanup(self.widget.close)
        self.frame = _frame(root / "frame.png")
        self.widget.project, self.widget.video = self.project, video
        document = self.widget.service.load_document(self.project, video)[0]
        self.widget.document = replace(document, objects=tuple(
            replace(item, asset=AssetRef(path=str(self.frame))) if isinstance(item, BackgroundObject) else item
            for item in document.objects
        ))
        self.widget.draft = CoverDraft.from_document(self.widget.document)
        self.widget.history.reset(self.widget.document)
        self.widget._apply_draft()
        self.widget._select_copy_role("B")

    def test_burst_button_puts_burst_behind_selected_text_once(self):
        headline = self.widget._selected_text()
        self.widget.burst_button.click()
        self.assertIn(burst_id(headline.id), {item.id for item in self.widget.document.objects})
        before = self.widget.document
        self.widget.burst_button.click()
        self.assertIs(self.widget.document, before)
        self.widget._undo()
        self.assertNotIn(burst_id(headline.id), {item.id for item in self.widget.document.objects})

    def test_frame_picture_goes_in_with_white_border_and_shows_border_controls(self):
        evidence = Path(self.temp.name) / "evidence.png"
        Image.new("RGB", (640, 360), (20, 120, 20)).save(evidence)
        self.widget._frame_picture_ready((evidence, 12.5), None)
        picture = next(item for item in self.widget.document.objects if isinstance(item, ImageObject))
        self.assertEqual((picture.border, picture.border_width), ("#FFFFFF", 10))
        self.assertEqual(self.widget.document.selected_object_id, picture.id)
        self.widget._canvas_object_selected(picture.id)
        self.assertEqual(self.widget.picture_border_button.color(), "#FFFFFF")
        self.widget.picture_border_spin.setValue(20)
        self.assertEqual(object_for_profile(self.widget.document, picture.id, "4x3").border_width, 20)

    def test_save_and_apply_template_from_the_menu(self):
        from unittest import mock

        headline = self.widget._selected_text()
        moved = update_text_object(
            self.widget.document,
            replace(headline, style=replace(headline.style, fill="#00FF00"), transform=replace(headline.transform, x=0.3)),
            profile_key="4x3",
        )
        self.widget.document = moved
        with mock.patch("autoslice.desktop.cover_editor_schemes.QInputDialog.getText", return_value=("日更", True)):
            self.widget._save_template()
        (template,) = self.widget.service.templates.list()
        self.assertEqual(template.name, "日更")
        # 换个样子再套模板：样式和位置回到模板，文字不变。
        current = object_for_profile(self.widget.document, headline.id, "4x3")
        self.widget.document = update_text_object(
            self.widget.document, replace(current, style=replace(current.style, fill="#0000FF")), profile_key="4x3",
        )
        self.widget._fill_template_menu()
        actions = [action for action in self.widget.template_menu.actions() if action.text().startswith("套用：")]
        self.assertEqual([action.text() for action in actions], ["套用：日更"])
        actions[0].trigger()
        applied = object_for_profile(self.widget.document, headline.id, "4x3")
        self.assertEqual((applied.style.fill, applied.transform.x, applied.text), ("#00FF00", 0.3, headline.text))


if __name__ == "__main__":
    unittest.main()
