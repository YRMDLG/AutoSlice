"""“AI 改一改”的修改：组合操作按顺序落地、改完不出画、已经做过的算没变化、看不出变化的不显示；侧边取景。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from autoslice.desktop.ai_settings import AISettings
from autoslice.desktop.cover_ai import AIFix, AIFrameNotes, AIOutline, CoverAI, CoverAIError
from autoslice.desktop.cover_fixes import FIX_TILT, apply_fix, describe_state, describe_text_layers
from autoslice.desktop.cover_framing import subject_crop, text_bands
from autoslice.desktop.cover_layout import canvas_size, text_layout
from autoslice.desktop.cover_migration import document_from_basic_title_values
from autoslice.desktop.cover_model import AssetRef, BackgroundObject, TextObject, object_for_profile
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.foundation import DesktopStorage

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = QPushButton = None

CONFIGURED = AISettings(base_url="https://x/v1", has_token=True, text_model="t", vision_model="v", source="file")


class FixTests(unittest.TestCase):
    def setUp(self):
        from tests.unit.autoslice_cover.test_composition import _person_frame

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.service = CoverService(DesktopStorage(root / "data"))
        self.frame = _person_frame(root / "center.png", 960)
        document = document_from_basic_title_values(
            title="〖泽音〗选秀带手机居然会改变选曲", image_path=str(self.frame), selected_timestamp=0.0,
            background_x=0.5, background_y=0.5, background_scale=1.0,
        )
        document = replace(document, objects=tuple(
            replace(item, asset=AssetRef(path=str(self.frame))) if isinstance(item, BackgroundObject) else item
            for item in document.objects
        ))
        # 字放在下缘：放大后会往下长，正好考“改完不出画”。
        self.document = self.service.apply_auto_layout(document, self.frame, mode="lead", position="bottom")

    def _headline(self, document):
        item = next(obj for obj in document.objects if isinstance(obj, TextObject) and obj.copy_role == "B")
        return object_for_profile(document, item.id, "4x3")

    def test_bundle_applies_in_order_and_keeps_text_inside(self):
        before = self._headline(self.document)
        fixed = apply_fix(self.service, self.document, self.frame, AIFix("更抓眼", ("bigger", "tilt", "outline")), "4x3")
        after = self._headline(fixed)
        self.assertGreater(after.style.font_size, before.style.font_size * 1.25)
        self.assertEqual(after.transform.rotation, FIX_TILT)
        self.assertEqual(after.style.outer_stroke, "#FFFFFF")
        ink = text_layout(after, canvas_size("4x3")).ink
        self.assertLessEqual(ink.bottom, 1080 * 0.98 + 1)
        self.assertGreaterEqual(ink.left, 1440 * 0.02 - 1)
        self.assertLessEqual(ink.right, 1440 * 0.98 + 1)
        # 居中的字以中线放大，不往一边跑。
        self.assertAlmostEqual((ink.left + ink.right) / 2, 720, delta=60)

    def test_already_done_is_no_change_and_state_says_so(self):
        tilted = apply_fix(self.service, self.document, self.frame, AIFix("动感", ("tilt", "outline")), "4x3")
        self.assertIs(apply_fix(self.service, tilted, self.frame, AIFix("动感", ("tilt", "outline")), "4x3"), tilted)
        state = describe_state(tilted, "4x3")
        self.assertIn("字倾斜：有", state)
        self.assertIn("外描边：有", state)
        self.assertIn("强调词：无", state)
        # 白描边的字，外圈用黑色。
        red = apply_fix(self.service, self.document, self.frame, AIFix("换色", ("restyle", "outline"), preset="yellow-red"), "4x3")
        self.assertEqual(self._headline(red).style.outer_stroke, "#111111")

    def test_moving_text_where_it_already_is_is_no_change(self):
        # 字本来就在下缘：“移到下方”只会重排出几个像素的差别，算没改。
        self.assertIs(apply_fix(self.service, self.document, self.frame, AIFix("压脸", ("move_bottom",)), "4x3"), self.document)
        moved = apply_fix(self.service, self.document, self.frame, AIFix("压脸", ("move_top",)), "4x3")
        self.assertLess(self._headline(moved).transform.y, 0.4)

    def test_outline_colors_cover_custom_text_without_moving_or_restyling_it(self):
        extra = TextObject(id="extra-caption", text="用户添加的第三行")
        extra = replace(extra, transform=replace(extra.transform, x=-0.1), emphasis=("第三行",))
        document = replace(self.document, objects=(*self.document.objects, extra))
        self.assertIn(extra.id, {item["id"] for item in describe_text_layers(document, "4x3")})
        fix = AIFix("描边更协调", ("outline_colors",), outlines=(AIOutline(extra.id, "#4B2848", "#FFF4DD"),))
        changed = apply_fix(self.service, document, self.frame, fix, "4x3")
        after = object_for_profile(changed, extra.id, "4x3")
        self.assertEqual(after.style.stroke, "#4B2848")
        self.assertEqual(after.style.outer_stroke, "#FFF4DD")
        self.assertGreater(after.style.outer_stroke_width, 0)
        self.assertEqual(replace(after, style=extra.style), extra)
        self.assertEqual(replace(after.style, stroke=extra.style.stroke, outer_stroke=extra.style.outer_stroke,
                                 outer_stroke_width=extra.style.outer_stroke_width), extra.style)
        self.assertNotEqual(self.service.scheme_thumbnail(document), self.service.scheme_thumbnail(changed))
        removed = apply_fix(self.service, changed, self.frame, AIFix(
            "单层描边", ("outline_colors",), outlines=(AIOutline(extra.id, "#183A53", ""),),
        ), "4x3")
        self.assertEqual(object_for_profile(removed, extra.id, "4x3").style.outer_stroke_width, 0)
        kept = apply_fix(self.service, changed, self.frame, AIFix(
            "只改内边", ("outline_colors",), outlines=(AIOutline(extra.id, "#633D26"),),
        ), "4x3")
        kept_style = object_for_profile(kept, extra.id, "4x3").style
        self.assertEqual(kept_style.outer_stroke, after.style.outer_stroke)
        self.assertEqual(kept_style.outer_stroke_width, after.style.outer_stroke_width)

    def test_outline_colors_do_not_change_locked_text(self):
        headline = self._headline(self.document)
        locked = replace(headline, locked=True)
        document = replace(self.document, objects=tuple(locked if item.id == headline.id else item for item in self.document.objects))
        self.assertNotIn(locked.id, {item["id"] for item in describe_text_layers(document, "4x3")})
        fix = AIFix("不能改锁定字", ("outline_colors",), outlines=(AIOutline(locked.id, "#4B2848"),))
        self.assertEqual(apply_fix(self.service, document, self.frame, fix, "4x3"), document)


class SideFramingTests(unittest.TestCase):
    def test_side_zone_moves_face_away_from_the_text_side(self):
        # 左边一整条聊天栏：字放右边，人让到左边。
        notes = AIFrameNotes(busy=frozenset({"left"}), note="", face=(0.42, 0.25, 0.58, 0.55), ui=((0.0, 0.0, 0.3, 1.0),))
        crop, _cost = subject_crop((1920, 1080), (1440, 1080), notes, "right", "loose")
        face_center = ((0.42 + 0.58) / 2 - crop[0]) / (crop[2] - crop[0])
        self.assertLess(face_center, 0.5)
        (band,) = text_bands(crop, "right", 0.8)
        self.assertGreater(band[0], 0.42)
        # 脸最多占画面高度一半。
        tight, _ = subject_crop((1920, 1080), (1440, 1080), notes, "bottom", "tight")
        self.assertLessEqual(0.3 / (tight[3] - tight[1]), 0.5 + 1e-6)


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class CritiqueDialogTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover import CoverEditorWidget
        from autoslice.desktop.cover_draft import CoverDraft
        from autoslice.desktop.projects import ProjectVideo, SubmissionProject
        from tests.unit.autoslice_cover.test_composition import _person_frame

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app = QApplication.instance() or QApplication([])
        folder = root / "【泽音】选秀带手机居然会改变选曲"
        folder.mkdir()
        video = ProjectVideo("切片.mp4", str(folder / "切片.mp4"), "", "", False, False, "")
        project = SubmissionProject("p", folder.name, str(folder), (video,))
        self.widget = CoverEditorWidget(DesktopStorage(root / "data"))
        self.addCleanup(self.widget.deleteLater)
        # 没有事件循环时 deleteLater 不执行：弹出的“AI 改一改”窗口要关掉，否则会截走后续 Qt 测试的悬停事件。
        self.addCleanup(self.widget.close)
        self.addCleanup(lambda: getattr(self.widget, "_critique_box", None) and self.widget._critique_box.close())
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

    def _wait(self, condition, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.02)
        self.fail("等待超时")

    def test_dialog_shows_only_fixes_that_change_the_cover(self):
        headline = next(item for item in self.widget.document.objects if isinstance(item, TextObject) and item.copy_role == "B")
        reply = json.dumps({"fixes": [
            # 封面本来就在下面：移下去等于没改，不显示。
            {"issue": "压脸", "actions": ["move_bottom"]},
            {"issue": "没动感", "actions": ["bigger", "tilt"]},
            {"issue": "没重点", "actions": ["emphasize"], "words": [headline.text[:2]]},
        ]}, ensure_ascii=False)
        calls = []

        def model(prompt, **_kwargs):
            calls.append(prompt)
            if prompt.startswith("封面修改成品复核"):
                return json.dumps({"picks": [{"index": 2, "reason": "重点更突出"}, {"index": 3, "reason": "强调更清楚"}]})
            return reply

        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=model, settings=lambda: CONFIGURED)
        self.widget.document = self.widget.service.apply_auto_layout(
            self.widget.document, self.widget.draft.image_path, mode="lead", position="bottom", reframe=False,
        )
        self.widget._ai_critique()
        self._wait(lambda: getattr(self.widget, "_critique_box", None) is not None)
        buttons = self.widget._critique_box.apply_buttons
        self.assertEqual(len(buttons), 2)
        self.assertIn("封面现在：强调词：无", calls[0])
        buttons[0].click()
        self.assertEqual(buttons[0].text(), "已应用")
        self.assertEqual(object_for_profile(self.widget.document, headline.id, "4x3").transform.rotation, FIX_TILT)

    def _outline_dialog(self):
        headline = next(item for item in self.widget.document.objects if isinstance(item, TextObject) and item.copy_role == "B")
        reply = json.dumps({"fixes": [
            {"issue": "深紫与画面更协调", "actions": ["outline_colors"], "outlines": [
                {"id": headline.id, "stroke": "#4B2848", "outer_stroke": "#FFF4DD"},
            ]},
            {"issue": "深蓝保持文字清楚", "actions": ["outline_colors"], "outlines": [
                {"id": headline.id, "stroke": "#183A53", "outer_stroke": ""},
            ]},
        ]}, ensure_ascii=False)
        def model(prompt, **_kwargs):
            if prompt.startswith("封面修改成品复核"):
                return json.dumps({"picks": [{"index": 2, "reason": "深紫更协调"}, {"index": 3, "reason": "深蓝更清楚"}]})
            return reply

        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=model, settings=lambda: CONFIGURED)
        self.widget._ai_critique()
        self._wait(lambda: getattr(self.widget, "_critique_box", None) is not None)
        return self.widget._critique_box

    def test_applied_colors_match_preview_and_switch_without_stacking_changes(self):
        original = self.widget.document
        dialog = self._outline_dialog()
        self.assertEqual(dialog.tabs.count(), 2)
        for index in (0, 1):
            dialog.tabs.setCurrentIndex(index)
            self.app.processEvents()
            dialog.apply_buttons[index].click()
            self.assertEqual(self.widget.document, dialog.previews[index].document)
            self.assertEqual(
                self.widget.service.scheme_thumbnail(self.widget.document, canvas_key="4x3", width=560),
                dialog.previews[index].thumbnail,
            )
        undo = next(button for button in dialog.findChildren(QPushButton) if button.text() == "撤销")
        undo.click()
        self.assertEqual(self.widget.document, dialog.previews[0].document)
        dialog.activateWindow()
        self.app.processEvents()
        QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertEqual(self.widget.document, original)
        redo = next(button for button in dialog.findChildren(QPushButton) if button.text() == "重做")
        redo.click()
        self.assertEqual(self.widget.document, dialog.previews[0].document)

    def test_stale_preview_does_not_overwrite_a_new_edit(self):
        dialog = self._outline_dialog()
        original = self.widget.document
        base = next(item for item in self.widget.document.objects if isinstance(item, TextObject))
        headline = object_for_profile(self.widget.document, base.id, "4x3")
        # 模拟用户在非模态预览打开时继续改字，应用旧方案不得覆盖这份新文档。
        from autoslice.desktop.cover_model import update_text_object

        self.widget.document = update_text_object(self.widget.document, replace(headline, text="用户的新修改"), profile_key="4x3")
        changed = self.widget.document
        dialog.apply_buttons[0].click()
        self.assertEqual(self.widget.document, changed)
        self.assertIn("封面或画布已变化", dialog.notice.text())
        # 即使另一个项目恰好加载同一份封面，也不能套用前一项目的候选。
        self.widget.document = original
        self.widget.project = replace(self.widget.project, id="another-project")
        dialog.apply_buttons[0].click()
        self.assertEqual(self.widget.document, original)
        self.assertTrue(dialog.notice.isVisible())

    def test_comparison_images_and_apply_button_fit_in_a_small_window(self):
        from autoslice.desktop.cover_critique_dialog import CoverComparisonImage

        dialog = self._outline_dialog()
        dialog.resize(640, 480)
        self.app.processEvents()
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            self.app.processEvents()
            page = dialog.tabs.currentWidget()
            pictures = page.findChildren(CoverComparisonImage)
            self.assertEqual(len(pictures), 2)
            for picture in pictures:
                pixmap = picture.pixmap()
                self.assertFalse(pixmap.isNull())
                self.assertLessEqual(pixmap.width(), picture.width())
                self.assertLessEqual(pixmap.height(), picture.height())
                self.assertAlmostEqual(pixmap.width() / pixmap.height(), 4 / 3, delta=0.02)
            self.assertTrue(page.rect().contains(dialog.apply_buttons[index].geometry()))

    def test_custom_text_preview_applies_and_undoes_after_review(self):
        extra = TextObject(id="new-caption", text="自己添加的文字")
        extra = replace(extra, transform=replace(extra.transform, x=0.1, y=0.08), style=replace(extra.style, font_size=55))
        self.widget.document = replace(self.widget.document, objects=(*self.widget.document.objects, extra))
        original = self.widget.document
        self.widget.history.reset(original)

        def model(prompt, **_kwargs):
            if prompt.startswith("封面修改成品复核"):
                return json.dumps({"picks": [{"index": 2, "reason": "自定义文字更清楚"}]})
            return json.dumps({"fixes": [{"issue": "自定义文字调整", "actions": ["edit_objects"], "edits": [
                {"id": extra.id, "fill": "#FFFFFF", "dx": 0.02},
            ]}]})

        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=model, settings=lambda: CONFIGURED)
        self.widget._ai_critique()
        self._wait(lambda: getattr(self.widget, "_critique_box", None) is not None)
        dialog = self.widget._critique_box
        self.assertEqual(dialog.previews[0].fix.issue, "自定义文字更清楚")
        self.assertIn(extra.text, dialog.previews[0].fix.steps[0])
        self.assertEqual(self.widget.document, original)
        dialog.apply_buttons[0].click()
        self.assertEqual(self.widget.document, dialog.previews[0].document)
        self.assertEqual(object_for_profile(self.widget.document, extra.id, "4x3").style.fill, "#FFFFFF")
        next(button for button in dialog.findChildren(QPushButton) if button.text() == "撤销").click()
        self.assertEqual(self.widget.document, original)

    def test_review_can_keep_original_without_opening_candidates(self):
        original = self.widget.document

        def model(prompt, **_kwargs):
            if prompt.startswith("封面修改成品复核"):
                return json.dumps({"picks": [], "rejected": "原图的配色更协调"})
            return json.dumps({"fixes": [{"issue": "配色", "actions": ["restyle"], "preset": "yellow-purple"}]})

        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=model, settings=lambda: CONFIGURED)
        with patch.object(QMessageBox, "information") as message:
            self.widget._ai_critique()
            self._wait(lambda: self.widget.ai_critique_button.isEnabled())
        self.assertEqual(self.widget.document, original)
        self.assertIsNone(getattr(self.widget, "_critique_box", None))
        self.assertEqual(message.call_args.args[2], "原图的配色更协调")

    def test_review_failure_keeps_original_and_shows_error(self):
        original = self.widget.document

        def model(prompt, **_kwargs):
            if prompt.startswith("封面修改成品复核"):
                raise CoverAIError("复核服务暂不可用")
            return json.dumps({"fixes": [{"issue": "配色", "actions": ["restyle"], "preset": "yellow-purple"}]})

        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=model, settings=lambda: CONFIGURED)
        self.widget._ai_critique()
        self._wait(lambda: self.widget.ai_critique_button.isEnabled())
        self.assertEqual(self.widget.document, original)
        self.assertIsNone(getattr(self.widget, "_critique_box", None))
        self.assertIn("复核服务暂不可用", self.widget.notice_label.text())

    def test_edits_during_critique_discard_old_result(self):
        def model(prompt, **_kwargs):
            if prompt.startswith("封面修改成品复核"):
                return json.dumps({"picks": [{"index": 2, "reason": "文字更清楚"}]})
            return json.dumps({"fixes": [{"issue": "配色", "actions": ["restyle"], "preset": "yellow-purple"}]})

        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=model, settings=lambda: CONFIGURED)
        self.widget._ai_critique()
        self.widget.document = replace(self.widget.document, objects=tuple(
            replace(item, text="生成期间的新文字") if isinstance(item, TextObject) else item
            for item in self.widget.document.objects
        ))
        changed = self.widget.document
        self._wait(lambda: self.widget.ai_critique_button.isEnabled())
        self.assertEqual(self.widget.document, changed)
        self.assertIsNone(getattr(self.widget, "_critique_box", None))
        self.assertIn("封面已有修改", self.widget.notice_label.text())

    def test_near_duplicate_renderings_are_filtered_before_review(self):
        target = self.widget._primary_copy_ids()["B"]
        reviews = []

        def model(prompt, **_kwargs):
            if prompt.startswith("封面修改成品复核"):
                reviews.append(prompt)
                return json.dumps({"picks": [{"index": 2, "reason": "文字更清楚"}]})
            return json.dumps({"fixes": [
                {"issue": "微小移动", "actions": ["edit_objects"], "edits": [{"id": target, "dx": 0.000001}]},
                {"issue": "清楚", "actions": ["edit_objects"], "edits": [{"id": target, "fill": "#FFFFFF"}]},
                {"issue": "几乎重复", "actions": ["edit_objects"], "edits": [{"id": target, "fill": "#FFFFFF", "dy": 0.00001}]},
            ]})

        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=model, settings=lambda: CONFIGURED)
        self.widget._ai_critique()
        self._wait(lambda: getattr(self.widget, "_critique_box", None) is not None)
        self.assertEqual(len(self.widget._critique_box.previews), 1)
        self.assertEqual(len(reviews), 1)
        self.assertIn("编号 2~2", reviews[0])


if __name__ == "__main__":
    unittest.main()
