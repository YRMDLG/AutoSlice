"""关键词强调与按脸取景：强调词只换颜色、两端画得一样；裁切保住整张脸、字带避开界面；引子在上的叠放；方案换帧。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from autoslice.desktop.cover_ai import AIFrameNotes
from autoslice.desktop.cover_autolayout import CoverScheme
from autoslice.desktop.cover_framing import area, overlap, subject_crop, text_bands, text_hit
from autoslice.desktop.cover_layout import canvas_size, document_layers, text_layout, text_paint
from autoslice.desktop.cover_migration import document_from_basic_title_values
from autoslice.desktop.cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    Rect,
    TextObject,
    TextStyle,
    TextWrap,
    Transform,
    default_profiles,
    emphasis_words,
    object_for_profile,
    update_text_object,
)
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.cover_style import STYLE_PRESETS, accent_for
from autoslice.desktop.foundation import DesktopStorage
from autoslice_cover.document_layout import emphasize
from autoslice_cover.document_render import compose_document

try:
    from PySide6.QtGui import QTextCursor
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = QTextCursor = None

YELLOW = (0xFF, 0xE4, 0x38)
# _person_frame(…, 960) 画的人：脸在中间，身体在下面。
FACE = (0.41, 0.2, 0.59, 0.6)
PERSON = (0.36, 0.18, 0.64, 1.0)


def _text(**changes) -> TextObject:
    values = dict(
        id="copy-b", text="我靠，真给我开盒了", copy_role="B", transform=Transform(x=0.05, y=0.55),
        rect=Rect(width=0.9, height=0.3), wrap=TextWrap(max_width=0.9),
        style=TextStyle(font_size=150, fill="#FFFFFF", stroke="#111111", stroke_width=12),
    )
    values.update(changes)
    return TextObject(**values)


def _yellow_pixels(image: Image.Image) -> int:
    return sum(1 for pixel in image.getdata() if all(abs(a - b) < 40 for a, b in zip(pixel, YELLOW)))


class EmphasisModelTests(unittest.TestCase):
    def test_words_are_cleaned_and_round_trip(self):
        self.assertEqual(emphasis_words("开盒 我靠，开盒"), ("开盒", "我靠"))
        self.assertEqual(emphasis_words(["", " 一 ", 3, "一"]), ("一",))
        self.assertEqual(emphasis_words(None), ())
        item = _text(emphasis=("开盒",), style=TextStyle(accent="#16d8ed"))
        restored = TextObject.from_payload(item.to_payload())
        self.assertEqual((restored.emphasis, restored.style.accent), (("开盒",), "#16D8ED"))
        # 旧草稿没有这两个字段：不强调、强调色自动。
        payload = item.to_payload()
        del payload["emphasis"]
        del payload["style"]["accent"]
        old = TextObject.from_payload(payload)
        self.assertEqual((old.emphasis, old.style.accent), ((), ""))

    def test_emphasis_and_accent_are_shared_by_both_ratios(self):
        document = CoverDocument(objects=(BackgroundObject(id="bg"), _text()), profiles=default_profiles())
        current = object_for_profile(document, "copy-b", "4x3")
        document = update_text_object(
            document, replace(current, emphasis=("开盒",), style=replace(current.style, accent="#FF0000")), profile_key="4x3",
        )
        for key in ("4x3", "16x9"):
            item = object_for_profile(document, "copy-b", key)
            self.assertEqual((item.emphasis, item.style.accent), (("开盒",), "#FF0000"), key)

    def test_accent_contrasts_with_fill_and_stroke(self):
        self.assertEqual(accent_for("#FFFFFF", "#111111"), "#FFE438")
        self.assertEqual(accent_for("#FFE438", "#111111"), "#16D8ED")
        # 白描边的彩字：强调词用黑字，不用会和白边糊在一起的浅色。
        self.assertEqual(accent_for("#F44336", "#FFFFFF"), "#111111")
        # 换配色时强调色回到自动。
        style = STYLE_PRESETS[0].apply(TextStyle(accent="#123456"))
        self.assertEqual(style.accent, "")


class EmphasisLayoutTests(unittest.TestCase):
    def test_runs_split_by_words_even_across_lines_without_moving_glyphs(self):
        plain = text_layout(_text(), canvas_size("4x3"))
        marked = text_layout(_text(emphasis=("我靠", "开盒")), canvas_size("4x3"))
        self.assertEqual([line.text for line in marked.lines], [line.text for line in plain.lines])
        self.assertEqual((marked.ink, marked.area, marked.font_size), (plain.ink, plain.area, plain.font_size))
        accented = "".join(run.text for line in marked.lines for run in line.runs if run.accent)
        self.assertEqual(accented, "我靠开盒")
        # 词被断行拆开也照样标上。
        layout = text_layout(_text(text="ABCD开盒", emphasis=("D开",), rect=Rect(width=0.9, height=0.3)), canvas_size("4x3"))
        split = emphasize(replace(layout, lines=(
            replace(layout.lines[0], text="ABCD", runs=(replace(layout.lines[0].runs[0], text="ABCD"),)),
            replace(layout.lines[0], text="开盒", runs=(replace(layout.lines[0].runs[0], text="开盒"),)),
        )), ("D开",))
        self.assertEqual(
            [[(run.text, run.accent) for run in line.runs] for line in split.lines],
            [[("ABC", False), ("D", True)], [("开", True), ("盒", False)]],
        )
        # 文案里没有的词不影响。
        self.assertIs(emphasize(plain, ("没有",)), plain)

    def test_export_paints_words_in_accent_color(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        background = Path(temp.name) / "bg.png"
        Image.new("RGB", (1440, 1080), (40, 60, 90)).save(background)

        def render(text: TextObject) -> Image.Image:
            document = CoverDocument(
                objects=(BackgroundObject(id="bg", asset=AssetRef(path=str(background))), text), profiles=default_profiles(),
            )
            base, layers = document_layers(document, "4x3")
            return compose_document(canvas_size("4x3"), base, layers)

        self.assertEqual(_yellow_pixels(render(_text())), 0)
        marked = _text(emphasis=("开盒",))
        self.assertEqual(text_paint(marked).accent, "#FFE438")
        self.assertGreater(_yellow_pixels(render(marked)), 2000)
        # 指定的强调色优先于自动配色。
        custom = _text(emphasis=("开盒",), style=replace(marked.style, accent="#00FF00"))
        self.assertEqual(text_paint(custom).accent, "#00FF00")
        self.assertEqual(_yellow_pixels(render(custom)), 0)


class FramingTests(unittest.TestCase):
    def setUp(self):
        # 1920×1080 的直播画面：脸在中间，右侧整条聊天栏，左上一行直播标题。
        self.notes = AIFrameNotes(
            busy=frozenset({"top", "right"}), note="", face=FACE, person=PERSON,
            ui=((0.0, 0.0, 0.55, 0.1), (0.72, 0.0, 1.0, 1.0)),
        )

    def test_crop_keeps_whole_face_and_keeps_ui_out_of_the_text_band(self):
        for zone in ("bottom", "top", ""):
            crop, _cost = subject_crop((1920, 1080), (1440, 1080), self.notes, zone, "loose")
            self.assertAlmostEqual((crop[2] - crop[0]) * 1920 / ((crop[3] - crop[1]) * 1080), 4 / 3, places=2)
            self.assertLessEqual(crop[0], FACE[0])
            self.assertGreaterEqual(crop[2], FACE[2])
            self.assertLessEqual(crop[1], FACE[1])
            self.assertGreaterEqual(crop[3], FACE[3])
            # 右侧聊天栏不进字带。
            share = 0.62 if zone else 0.55
            for band in text_bands(crop, zone, share):
                self.assertLess(overlap(band, self.notes.ui[1]) / area(band), 0.05, zone)

    def test_tight_is_closer_than_loose_and_no_face_means_no_crop(self):
        small = replace(self.notes, face=(0.45, 0.3, 0.55, 0.5), person=None)
        loose, _ = subject_crop((1920, 1080), (1440, 1080), small, "bottom", "loose")
        tight, _ = subject_crop((1920, 1080), (1440, 1080), small, "bottom", "tight")
        self.assertLess(tight[3] - tight[1], (loose[3] - loose[1]) * 0.83)
        # 脸已经很大（占四成高）时，特写再近脸就超过画面一半：退回宽松取景。
        big_loose, _ = subject_crop((1920, 1080), (1440, 1080), self.notes, "bottom", "loose")
        big_tight, _ = subject_crop((1920, 1080), (1440, 1080), self.notes, "bottom", "tight")
        self.assertEqual(big_tight, big_loose)
        self.assertIsNone(subject_crop((1920, 1080), (1440, 1080), replace(self.notes, face=None), "bottom", "loose"))

    def test_text_hit_measures_text_over_ui_or_face(self):
        document = CoverDocument(
            objects=(BackgroundObject(id="bg", asset=AssetRef(path="x.png")),
                     _text(transform=Transform(x=0.05, y=0.02), rect=Rect(width=0.5, height=0.1),
                           wrap=TextWrap(max_width=0.5), style=TextStyle(font_size=60))),
            profiles=default_profiles(),
        )
        # 4:3 画布按高铺满，画面左右各裁掉 240 像素：字在原画面顶部的直播标题上。
        self.assertGreater(text_hit(document, (1920, 1080), self.notes), 0.5)
        clear = replace(self.notes, ui=((0.0, 0.9, 0.2, 1.0),), face=None)
        self.assertEqual(text_hit(document, (1920, 1080), clear), 0.0)


class LayoutAndSchemeTests(unittest.TestCase):
    def setUp(self):
        from tests.unit.autoslice_cover.test_composition import _person_frame

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.service = CoverService(DesktopStorage(root / "data"))
        self.frame = _person_frame(root / "center.png", 960)
        self.other = _person_frame(root / "other.png", 900)
        document = document_from_basic_title_values(
            title="〖泽音〗选手想唱的和粉丝投的不一样", image_path=str(self.frame), selected_timestamp=3.0,
            background_x=0.5, background_y=0.5, background_scale=1.0,
        )
        self.document = replace(document, objects=tuple(
            replace(item, asset=AssetRef(path=str(self.frame))) if isinstance(item, BackgroundObject) else item
            for item in document.objects
        ))

    def _seeded(self):
        from autoslice.desktop.cover_copy import BasicCoverCopy

        return self.service._seed_copy(
            self.document, BasicCoverCopy("选手想唱的", "和粉丝投的不一样", emphasis=("不一样", "想唱")), None, big=False,
        )

    def test_seeded_copy_puts_each_word_on_the_block_that_has_it(self):
        document = self._seeded()
        texts = {item.copy_role: item for item in document.objects if isinstance(item, TextObject)}
        self.assertEqual((texts["A"].emphasis, texts["B"].emphasis), (("想唱",), ("不一样",)))

    def test_lead_puts_small_context_above_big_headline(self):
        notes = AIFrameNotes(busy=frozenset(), note="", face=FACE, person=PERSON)
        laid = self.service.apply_auto_layout(self._seeded(), self.frame, mode="lead", position="bottom",
                                              notes=notes, framing="loose")
        ids = {item.copy_role: item.id for item in laid.objects if isinstance(item, TextObject)}
        a, b = object_for_profile(laid, ids["A"], "4x3"), object_for_profile(laid, ids["B"], "4x3")
        self.assertLess(a.transform.y, b.transform.y)
        self.assertLess(a.style.font_size, b.style.font_size)
        # 整组放在下方字带。
        self.assertGreater(a.transform.y, 0.45)

    def test_scheme_on_another_frame_swaps_frame_unless_locked(self):
        moved = self.service.with_frame(self.document, self.other, 7.5)
        scheme = CoverScheme("ai:稳妥", "AI·稳妥", "", moved)
        applied = self.service.apply_scheme(self.document, scheme)
        background = next(item for item in applied.objects if isinstance(item, BackgroundObject))
        self.assertEqual((background.asset.path, applied.source.selected_timestamp), (str(self.other), 7.5))
        locked = replace(self.document, source=replace(self.document.source, frame_locked=True))
        kept = self.service.apply_scheme(locked, scheme)
        background = next(item for item in kept.objects if isinstance(item, BackgroundObject))
        self.assertEqual((background.asset.path, kept.source.selected_timestamp), (str(self.frame), 3.0))


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class EmphasisEditorTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover import CoverEditorWidget
        from autoslice.desktop.cover_draft import CoverDraft
        from autoslice.desktop.projects import ProjectVideo, SubmissionProject
        from tests.unit.autoslice_cover.test_composition import _person_frame

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app = QApplication.instance() or QApplication([])
        folder = root / "【泽音】真给我开盒了"
        folder.mkdir()
        video = ProjectVideo("切片.mp4", str(folder / "切片.mp4"), "", "", False, False, "")
        project = SubmissionProject("p", folder.name, str(folder), (video,))
        self.widget = CoverEditorWidget(DesktopStorage(root / "data"))
        self.addCleanup(self.widget.deleteLater)
        frame = _person_frame(root / "frame.png", 960)
        self.widget.project, self.widget.video = project, video
        document = self.widget.service.load_document(project, video)[0]
        self.widget.document = replace(document, objects=tuple(
            replace(item, asset=AssetRef(path=str(frame))) if isinstance(item, BackgroundObject) else item
            for item in document.objects
        ))
        self.widget.draft = CoverDraft.from_document(self.widget.document)
        self.widget.history.reset(self.widget.document)
        self.widget._apply_draft()
        self.widget._select_copy_role("B")

    def _headline(self) -> TextObject:
        item = next(obj for obj in self.widget.document.objects if isinstance(obj, TextObject) and obj.copy_role == "B")
        return object_for_profile(self.widget.document, item.id, "4x3")

    def test_select_text_then_emphasize_toggles_words_and_undo(self):
        text = self._headline().text
        cursor = self.widget.title_edit.textCursor()
        cursor.setPosition(len(text) - 3)
        cursor.setPosition(len(text) - 1, QTextCursor.MoveMode.KeepAnchor)
        self.widget.title_edit.setTextCursor(cursor)
        word = text[-3:-1]
        self.widget._emphasize_selection()
        self.assertEqual(self._headline().emphasis, (word,))
        self.assertEqual(self.widget.emphasis_edit.text(), word)
        # 画布按强调词分开填色。
        layout = text_layout(self._headline(), canvas_size("4x3"))
        _path, _emoji, _key, accent = self.widget.canvas._glyph_path(layout)
        self.assertFalse(accent.isEmpty())
        # 再点一次取消；撤销回到强调状态。
        self.widget._emphasize_selection()
        self.assertEqual(self._headline().emphasis, ())
        self.widget._undo()
        self.assertEqual(self._headline().emphasis, (word,))

    def test_accent_color_button_writes_style(self):
        self.widget.emphasis_edit.setText(self._headline().text[:2])
        self.widget.accent_button._choose("#00FF00")
        self.assertEqual(self._headline().style.accent, "#00FF00")
        self.assertEqual(self.widget.accent_button.text(), "#00FF00")
        self.widget.accent_button._choose("")
        self.assertEqual(self.widget.accent_button.text(), "自动")


if __name__ == "__main__":
    unittest.main()
