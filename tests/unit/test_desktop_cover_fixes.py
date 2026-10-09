"""“AI 改一改”的修改：组合操作按顺序落地、改完不出画、已经做过的算没变化、看不出变化的不显示；侧边取景。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from autoslice.desktop.ai_settings import AISettings
from autoslice.desktop.cover_ai import AIFix, AIFrameNotes, CoverAI
from autoslice.desktop.cover_fixes import FIX_TILT, apply_fix, describe_state
from autoslice.desktop.cover_framing import subject_crop, text_bands
from autoslice.desktop.cover_layout import canvas_size, text_layout
from autoslice.desktop.cover_migration import document_from_basic_title_values
from autoslice.desktop.cover_model import AssetRef, BackgroundObject, TextObject, object_for_profile
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.foundation import DesktopStorage

try:
    from PySide6.QtWidgets import QApplication, QPushButton
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
            return reply

        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=model, settings=lambda: CONFIGURED)
        self.widget.document = self.widget.service.apply_auto_layout(
            self.widget.document, self.widget.draft.image_path, mode="lead", position="bottom", reframe=False,
        )
        self.widget._ai_critique()
        self._wait(lambda: getattr(self.widget, "_critique_box", None) is not None)
        buttons = [button for button in self.widget._critique_box.findChildren(QPushButton) if button.text() == "应用"]
        self.assertEqual(len(buttons), 2)
        self.assertIn("封面现在：强调词：无", calls[0])
        buttons[0].click()
        self.assertEqual(buttons[0].text(), "已应用")
        self.assertEqual(object_for_profile(self.widget.document, headline.id, "4x3").transform.rotation, FIX_TILT)


if __name__ == "__main__":
    unittest.main()
