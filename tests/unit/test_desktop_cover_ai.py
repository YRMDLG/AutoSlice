"""封面 AI：文案逐字出自原文、方案只在可选范围内、结果缓存，编辑器里点一下就出三套方案。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from autoslice.desktop.ai_settings import AISettings
from autoslice.desktop.cover_ai import (
    AIAnalysis,
    AICopy,
    CoverAI,
    CoverAIError,
    from_source,
    jpeg_bytes,
)
from autoslice.desktop.cover_model import AssetRef, BackgroundObject, TextObject, object_for_profile
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None

TITLE = "【泽音】选秀带手机居然会改变选曲⁉ 懂姐小音告诉你韩娱特殊操作与内幕👀"
CUES = ((3.0, 5.0, "你怎么知道选秀要交手机"), (12.5, 15.0, "这个位置竞演可以场外干涉"))
CONFIGURED = AISettings(base_url="https://x/v1", has_token=True, text_model="gpt-5.6-terra",
                        vision_model="gpt-5.6-luna", source="file")


class FakeModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, prompt, *, max_tokens, model_override, images):
        self.calls.append((model_override, len(images), prompt))
        return self.replies.pop(0)


def _analysis_reply(**extra):
    payload = {
        "highlight": {"quote": "这个位置竞演可以场外干涉", "start": 12.5, "end": 15, "reason": "反差"},
        "copies": [
            {"context": "选秀", "headline": "居然会改变选曲", "angle": "原话", "reason": "反转"},
            {"context": "", "headline": "竞演可以场外干涉", "angle": "结果", "reason": "结果"},
            # 原文没有“震惊”“全网”：编造，必须丢掉。
            {"context": "震惊", "headline": "全网都在看", "angle": "悬念", "reason": "营销"},
        ],
    }
    payload.update(extra)
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


class CoverAIServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = DesktopStorage(Path(self.temp.name) / "data")

    def test_copies_must_come_from_title_or_subtitles(self):
        self.assertTrue(from_source("选秀居然会改变", TITLE))
        self.assertFalse(from_source("震惊全网", TITLE))
        model = FakeModel(_analysis_reply())
        analysis = CoverAI(self.storage, llm=model, settings=lambda: CONFIGURED).analyze(TITLE, CUES)
        self.assertEqual([item.headline for item in analysis.copies], ["居然会改变选曲", "竞演可以场外干涉"])
        self.assertEqual((analysis.highlight.start, analysis.highlight.quote), (12.5, "这个位置竞演可以场外干涉"))
        model_name, image_count, prompt = model.calls[0]
        self.assertEqual((model_name, image_count), ("gpt-5.6-terra", 0))
        self.assertIn("[12.5] 这个位置竞演可以场外干涉", prompt)

    def test_same_request_is_answered_from_cache(self):
        model = FakeModel(_analysis_reply())
        ai = CoverAI(self.storage, llm=model, settings=lambda: CONFIGURED)
        first = ai.analyze(TITLE, CUES)
        self.assertEqual(ai.analyze(TITLE, CUES), first)
        self.assertEqual(len(model.calls), 1)
        # 再点一次（下一轮）会重新问。
        model.replies.append(_analysis_reply())
        ai.analyze(TITLE, CUES, round_index=1)
        self.assertEqual(len(model.calls), 2)

    def test_unconfigured_or_all_fabricated_gives_readable_errors(self):
        with self.assertRaisesRegex(CoverAIError, "设置页"):
            CoverAI(self.storage, llm=FakeModel(), settings=AISettings).analyze(TITLE, CUES)
        fabricated = json.dumps({"copies": [{"headline": "全网震惊"}]}, ensure_ascii=False)
        with self.assertRaisesRegex(CoverAIError, "不是出自原文"):
            CoverAI(self.storage, llm=FakeModel(fabricated), settings=lambda: CONFIGURED).analyze(TITLE, CUES)

    def test_design_keeps_only_known_layouts_and_presets_and_sends_images(self):
        analysis = AIAnalysis(None, (AICopy("选秀", "居然会改变选曲", "原话", ""), AICopy("", "竞演可以场外干涉", "结果", "")))
        reply = json.dumps({"schemes": [
            {"direction": "稳妥", "copy": 0, "layout": "split", "preset": "duo", "reason": "人物居中"},
            {"direction": "换个构图", "copy": 9, "layout": "slot", "preset": "white", "reason": "侧边"},
            {"direction": "大胆一点", "copy": 1, "layout": "spiral", "preset": "duo", "reason": "不存在的排法"},
        ]}, ensure_ascii=False)
        model = FakeModel(reply)
        ideas = CoverAI(self.storage, llm=model, settings=lambda: CONFIGURED).design(
            b"frame", analysis, recent_thumbnails=(b"a", b"b"),
        )
        self.assertEqual([(item.direction, item.layout, item.copy.headline) for item in ideas],
                         [("稳妥", "split", "居然会改变选曲"), ("换个构图", "slot", "居然会改变选曲")])
        self.assertEqual(model.calls[0][:2], ("gpt-5.6-luna", 3))

    def test_place_is_kept_only_when_it_fits_the_layout(self):
        analysis = AIAnalysis(None, (AICopy("", "交个备用机", "反差", ""),))
        reply = json.dumps({"schemes": [
            {"direction": "大胆一点", "copy": 0, "layout": "headline", "place": "top", "preset": "duo"},
            {"direction": "换个构图", "copy": 0, "layout": "slot", "place": "LEFT", "preset": "duo"},
            {"direction": "稳妥", "copy": 0, "layout": "split", "place": "left", "preset": "duo"},
        ]}, ensure_ascii=False)
        ideas = CoverAI(self.storage, llm=FakeModel(reply), settings=lambda: CONFIGURED).design(b"frame", analysis)
        self.assertEqual([item.place for item in ideas], ["top", "left", ""])

    def test_layout_engine_follows_the_place_ai_chose(self):
        from autoslice.desktop.cover_ai import AISchemeIdea
        from autoslice.desktop.cover_migration import document_from_basic_title_values
        from autoslice.desktop.cover_service import CoverService
        from tests.unit.autoslice_cover.test_composition import _person_frame

        frame = _person_frame(Path(self.temp.name) / "frame.png", 960)
        document = document_from_basic_title_values(
            title="〖泽音〗交个备用机", image_path=str(frame), selected_timestamp=0.0,
            background_x=0.5, background_y=0.5, background_scale=1.0,
        )
        service = CoverService(self.storage)

        def headline(idea):
            scheme, = service.schemes_from_ideas(document, frame, (idea,))
            item = next(obj for obj in scheme.document.objects if isinstance(obj, TextObject) and obj.copy_role == "B")
            return object_for_profile(scheme.document, item.id, "4x3")

        copy = AICopy("", "交个备用机", "反差", "")
        top = headline(AISchemeIdea("大胆一点", copy, "headline", "duo", "", place="top"))
        bottom = headline(AISchemeIdea("大胆一点", copy, "headline", "duo", "", place="bottom"))
        self.assertLess(top.transform.y, 0.3)
        self.assertGreater(bottom.transform.y, 0.5)
        left = headline(AISchemeIdea("换个构图", copy, "slot", "duo", "", place="left"))
        right = headline(AISchemeIdea("换个构图", copy, "slot", "duo", "", place="right"))
        self.assertLess(left.transform.x, right.transform.x)

    def test_critique_returns_short_notes(self):
        reply = json.dumps({"notes": [{"issue": "小图上 A 太小", "suggestion": "A 再大一点"}]}, ensure_ascii=False)
        notes = CoverAI(self.storage, llm=FakeModel(reply), settings=lambda: CONFIGURED).critique(b"cover")
        self.assertEqual([(note.issue, note.suggestion) for note in notes], [("小图上 A 太小", "A 再大一点")])

    def test_jpeg_bytes_shrinks_long_side(self):
        path = Path(self.temp.name) / "big.png"
        Image.new("RGB", (3000, 1000), "red").save(path)
        with Image.open(__import__("io").BytesIO(jpeg_bytes(path, max_side=600))) as image:
            self.assertEqual(image.size, (600, 200))


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class CoverAIEditorTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover import CoverEditorWidget
        from tests.unit.autoslice_cover.test_composition import _person_frame

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app = QApplication.instance() or QApplication([])
        folder = root / TITLE
        folder.mkdir()
        srt = folder / "切片.srt"
        srt.write_text(
            "1\n00:00:03,000 --> 00:00:05,000\n你怎么知道选秀要交手机\n\n"
            "2\n00:00:12,500 --> 00:00:15,000\n这个位置竞演可以场外干涉\n", encoding="utf-8",
        )
        video = ProjectVideo("切片.mp4", str(folder / "切片.mp4"), str(srt), str(folder / "切片_校对.srt"), True, False, "")
        self.project = SubmissionProject("p", TITLE, str(folder), (video,))
        self.widget = CoverEditorWidget(DesktopStorage(root / "data"))
        self.addCleanup(self.widget.deleteLater)
        frame = _person_frame(root / "frame.png", 960)
        scheme_reply = json.dumps({"schemes": [
            {"direction": "稳妥", "copy": 0, "layout": "split", "preset": "duo", "reason": "人物居中，上下分置最稳"},
            {"direction": "换个构图", "copy": 1, "layout": "slot", "preset": "white", "reason": "文字放侧边"},
            {"direction": "大胆一点", "copy": 1, "layout": "headline", "preset": "yellow-red", "reason": "大字更抢眼"},
        ]}, ensure_ascii=False)
        self.model = FakeModel(_analysis_reply(), scheme_reply)
        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=self.model, settings=lambda: CONFIGURED)
        self.widget.project, self.widget.video = self.project, video
        document = self.widget.service.load_document(self.project, video)[0]
        self.widget.document = replace(document, objects=tuple(
            replace(item, asset=AssetRef(path=str(frame))) if isinstance(item, BackgroundObject) else item
            for item in document.objects
        ))
        from autoslice.desktop.cover_draft import CoverDraft

        self.widget.draft = CoverDraft.from_document(self.widget.document)
        self.widget.history.reset(self.widget.document)
        self.widget._apply_draft()

    def wait_for(self, condition, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.02)
        self.fail("等待超时")

    def test_ai_button_fills_three_editable_schemes_and_copy_rotation(self):
        self.assertTrue(self.widget.ai_scheme_button.isEnabled())
        self.widget._ai_schemes()
        self.wait_for(lambda: self.widget.scheme_title.text() == "AI 方案")
        self.assertEqual([label.text() for label in self.widget.scheme_labels], ["AI·稳妥", "AI·换个构图", "AI·大胆一点"])
        self.assertIn("这个位置竞演可以场外干涉", self.widget.ai_hint.text())
        self.assertFalse(self.widget.ai_hint.isHidden())
        self.assertEqual(self.widget._copy_variants[0].headline, "居然会改变选曲")
        # 套用“大胆一点”：大字方案只留 B，配色是黄红，且能撤销。
        self.widget._apply_scheme(2)
        texts = {
            item.copy_role: object_for_profile(self.widget.document, item.id, "4x3")
            for item in self.widget.document.objects if isinstance(item, TextObject)
        }
        self.assertEqual(texts["B"].text, "竞演可以场外干涉")
        self.assertFalse("A" in texts and texts["A"].visible)
        self.assertEqual(self.widget._applied_scheme, "ai:大胆一点")
        self.assertTrue(self.widget.undo_button.isEnabled())

    def _texts(self, key="4x3"):
        return {
            item.copy_role: object_for_profile(self.widget.document, item.id, key)
            for item in self.widget.document.objects if isinstance(item, TextObject)
        }

    def test_ai_scheme_with_context_adds_a_block_to_b_only_cover(self):
        self.assertNotIn("A", self._texts())
        self.widget._ai_schemes()
        self.wait_for(lambda: self.widget.scheme_title.text() == "AI 方案")
        self.widget._apply_scheme(0)
        texts = self._texts()
        self.assertEqual((texts["A"].text, texts["A"].visible), ("选秀", True))
        self.assertLess(texts["A"].style.font_size, texts["B"].style.font_size)

    def test_cycle_copy_puts_context_above_headline_on_b_only_cover(self):
        from autoslice.desktop.cover_copy import BasicCoverCopy

        self.widget._copy_variants = (BasicCoverCopy("选秀", "居然会改变选曲"),)
        self.widget._copy_variant_index = -1
        self.widget._cycle_copy()
        for key in ("4x3", "16x9"):
            texts = self._texts(key)
            self.assertEqual((texts["A"].text, texts["A"].visible), ("选秀", True), key)
            self.assertLess(texts["A"].transform.y, texts["B"].transform.y, key)
            self.assertLess(texts["A"].style.font_size, texts["B"].style.font_size, key)

    def test_ai_failure_shows_reason_and_keeps_local_schemes_usable(self):
        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=FakeModel(), settings=AISettings)
        self.widget._ai_schemes()
        self.wait_for(lambda: self.widget.ai_scheme_button.isEnabled())
        self.assertIn("设置页", self.widget.notice_label.text())
        self.assertEqual(self.widget.scheme_title.text(), "快速方案")


if __name__ == "__main__":
    unittest.main()
