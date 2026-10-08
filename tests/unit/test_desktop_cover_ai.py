"""封面 AI：文案只看封面能懂且出自原文；本地排候选、AI 看成品挑；编辑器里点一下就出方案。"""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from autoslice.desktop.ai_settings import AISettings
from autoslice.desktop.cover_ai import (
    AICopy,
    AISchemeIdea,
    CoverAI,
    CoverAIError,
    contact_sheet,
    from_source,
    jpeg_bytes,
)
from autoslice.desktop.cover_model import AssetRef, BackgroundObject, TextObject, object_for_profile
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject
from autoslice_cover.document_layout import background_box

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None

TITLE = "【泽音】选秀带手机居然会改变选曲⁉ 懂姐小音告诉你韩娱特殊操作与内幕👀"
CUES = ((3.0, 5.0, "你怎么知道选秀要交手机"), (12.5, 15.0, "这个位置竞演可以场外干涉"))
CONFIGURED = AISettings(base_url="https://x/v1", has_token=True, text_model="gpt-5.6-terra",
                        vision_model="gpt-5.6-luna", source="file")
COPIES = (AICopy("选秀", "居然会改变选曲", "原话", ""), AICopy("", "竞演可以场外干涉", "结果", ""))


class FakeModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, prompt, *, max_tokens, model_override, images):
        self.calls.append((model_override, len(images), prompt))
        return self.replies.pop(0)


class RoutingModel:
    """并发请求的模拟模型：按提示词内容应答，与调用顺序无关。"""

    def __init__(self, analysis, choice, inspect=None):
        self.replies = {"analysis": analysis, "choice": choice,
                        "inspect": inspect or json.dumps({"busy": [], "note": ""}, ensure_ascii=False)}
        self.calls = []

    def __call__(self, prompt, *, max_tokens, model_override, images):
        kind = "inspect" if "取景框" in prompt else "choice" if "候选封面" in prompt else "analysis"
        self.calls.append((kind, model_override, len(images)))
        return self.replies[kind]


def _analysis_reply(**extra):
    payload = {
        "highlight": {"quote": "这个位置竞演可以场外干涉", "start": 12.5, "end": 15, "reason": "反差"},
        "copies": [
            {"context": "选秀", "headline": "居然会改变选曲", "angle": "原话", "clarity": 5, "reason": "反转"},
            {"context": "", "headline": "竞演可以场外干涉", "angle": "结果", "clarity": 4, "reason": "结果"},
            # 原文没有“震惊”“全网”：编造，必须丢掉。
            {"context": "震惊", "headline": "全网都在看", "angle": "悬念", "clarity": 5, "reason": "营销"},
            # 模型自己都觉得看不懂的碎片，也丢掉。
            {"context": "", "headline": "交手机", "angle": "原话", "clarity": 2, "reason": "碎片"},
        ],
    }
    payload.update(extra)
    return "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def _choice_reply(*picks, rejected="压脸"):
    return json.dumps({
        "picks": [{"direction": direction, "index": index, "reason": f"选 {index}"} for direction, index in picks],
        "rejected": rejected,
    }, ensure_ascii=False)


def _thumb(color):
    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(buffer, format="JPEG")
    return buffer.getvalue()


class CoverAIServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = DesktopStorage(Path(self.temp.name) / "data")

    def test_copies_come_from_source_and_must_be_understandable(self):
        self.assertTrue(from_source("选秀居然会改变", TITLE))
        self.assertFalse(from_source("震惊全网", TITLE))
        # 可以补“到、的”这类虚词让句子通顺，不算加内容。
        self.assertTrue(from_source("高兴到发出鸟叫", "高兴的发出了鸟叫"))
        model = FakeModel(_analysis_reply())
        analysis = CoverAI(self.storage, llm=model, settings=lambda: CONFIGURED).analyze(TITLE, CUES)
        self.assertEqual([item.headline for item in analysis.copies], ["居然会改变选曲", "竞演可以场外干涉"])
        self.assertEqual((analysis.highlight.start, analysis.highlight.quote), (12.5, "这个位置竞演可以场外干涉"))
        model_name, image_count, prompt = model.calls[0]
        self.assertEqual((model_name, image_count), ("gpt-5.6-terra", 0))
        self.assertIn("[12.5] 这个位置竞演可以场外干涉", prompt)
        self.assertIn("只看封面", prompt)

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

    def test_unconfigured_or_all_rejected_gives_readable_errors(self):
        with self.assertRaisesRegex(CoverAIError, "设置页"):
            CoverAI(self.storage, llm=FakeModel(), settings=AISettings).analyze(TITLE, CUES)
        fabricated = json.dumps({"copies": [{"headline": "全网震惊", "clarity": 5}]}, ensure_ascii=False)
        with self.assertRaisesRegex(CoverAIError, "都不合格"):
            CoverAI(self.storage, llm=FakeModel(fabricated), settings=lambda: CONFIGURED).analyze(TITLE, CUES)

    def test_contact_sheet_numbers_every_candidate(self):
        sheet = contact_sheet(tuple(_thumb(color) for color in ("red", "green", "blue", "white", "black")))
        with Image.open(io.BytesIO(sheet)) as image:
            # 每行 4 张：5 张排两行。
            self.assertGreater(image.width, 4 * 320)
            self.assertGreater(image.height, 2 * 240)
            # 第 1 格左上角是红底编号。
            red, green, blue = image.getpixel((14, 14))
            self.assertGreater(red, 150)
            self.assertLess(green, 100)

    def test_choose_maps_one_based_numbers_and_drops_invalid(self):
        model = FakeModel(_choice_reply(("稳妥", 2), ("换个构图", 2), ("大胆一点", 9), ("自由发挥", 1)))
        choice = CoverAI(self.storage, llm=model, settings=lambda: CONFIGURED).choose(
            b"sheet", 4, title=TITLE, recent_thumbnails=(b"a", b"b", b"c"),
        )
        # 重复编号、超出范围的丢掉；不认识的方向按顺序补成“换个构图”。
        self.assertEqual([(pick.direction, pick.index) for pick in choice.picks], [("稳妥", 1), ("换个构图", 0)])
        self.assertEqual(choice.rejected, "压脸")
        model_name, image_count, prompt = model.calls[0]
        self.assertEqual((model_name, image_count), ("gpt-5.6-luna", 3))
        self.assertIn("一票否决", prompt)

    def test_fixes_come_only_from_the_menu_and_rewrites_must_come_from_source(self):
        reply = json.dumps({"fixes": [
            {"issue": "字压脸", "action": "move_bottom"},
            {"issue": "加个贴纸", "action": "add_sticker"},
            {"issue": "编的", "action": "rewrite", "headline": "全网震惊"},
            {"issue": "文案空", "action": "rewrite", "context": "选秀", "headline": "居然会改变选曲"},
            {"issue": "重复", "action": "move_bottom"},
        ]}, ensure_ascii=False)
        model = FakeModel(reply)
        fixes = CoverAI(self.storage, llm=model, settings=lambda: CONFIGURED).suggest_fixes(
            b"cover", texts=("", "交手机"), title=TITLE, source=TITLE, frame=b"frame",
        )
        self.assertEqual([(fix.action, fix.headline) for fix in fixes], [("move_bottom", ""), ("rewrite", "居然会改变选曲")])
        self.assertEqual(fixes[1].label, "换成 A「选秀」 B「居然会改变选曲」")
        self.assertEqual(model.calls[0][1], 2)
        self.assertIn("B「交手机」", model.calls[0][2])

    def test_jpeg_bytes_shrinks_long_side(self):
        path = Path(self.temp.name) / "big.png"
        Image.new("RGB", (3000, 1000), "red").save(path)
        with Image.open(io.BytesIO(jpeg_bytes(path, max_side=600))) as image:
            self.assertEqual(image.size, (600, 200))


class CandidateTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover_migration import document_from_basic_title_values
        from autoslice.desktop.cover_service import CoverService
        from tests.unit.autoslice_cover.test_composition import _person_frame

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.frame = _person_frame(Path(self.temp.name) / "frame.png", 960)
        self.document = document_from_basic_title_values(
            title="〖泽音〗交个备用机", image_path=str(self.frame), selected_timestamp=0.0,
            background_x=0.5, background_y=0.5, background_scale=1.0,
        )
        self.service = CoverService(DesktopStorage(Path(self.temp.name) / "data"))

    def _headline(self, document, key="4x3"):
        item = next(obj for obj in document.objects if isinstance(obj, TextObject) and obj.copy_role == "B")
        return object_for_profile(document, item.id, key)

    def _background(self, document, key="4x3"):
        item = next(obj for obj in document.objects if isinstance(obj, BackgroundObject))
        return object_for_profile(document, item.id, key)

    def test_candidates_vary_copy_layout_and_framing(self):
        candidates = self.service.ai_candidates(self.document, self.frame, COPIES)
        self.assertGreaterEqual(len(candidates), 10)
        self.assertEqual([item.label for item in candidates], [str(index + 1) for index in range(len(candidates))])
        documents = [item.document for item in candidates]
        self.assertEqual(len({id(item) for item in documents}), len(documents))
        self.assertTrue(all(a != b for i, a in enumerate(documents) for b in documents[i + 1:]))
        headlines = {self._headline(item).text for item in documents}
        self.assertEqual(headlines, {"居然会改变选曲", "竞演可以场外干涉"})
        scales = {round(self._background(item).scale, 2) for item in documents}
        # 一部分保持原画面，一部分放大到人物、裁掉两侧杂物。
        self.assertEqual(scales, {1.0, 1.35})

    def test_busy_regions_are_skipped(self):
        everything = self.service.ai_candidates(self.document, self.frame, COPIES)
        without_top = self.service.ai_candidates(self.document, self.frame, COPIES, busy=frozenset({"top"}))
        def tops(candidates):
            return sum(self._headline(item.document).transform.y < 0.3 for item in candidates)
        self.assertGreater(tops(everything), 0)
        self.assertEqual(tops(without_top), 0)
        # 四周都乱、剩不到两种排法时不过滤，交给挑选环节把关。
        crowded = self.service.ai_candidates(
            self.document, self.frame, COPIES, busy=frozenset({"top", "bottom", "left", "right"}),
        )
        self.assertEqual(len(crowded), len(everything))

    def test_inspect_reads_busy_regions(self):
        reply = json.dumps({"busy": ["TOP", "middle", "right"], "note": "顶部有直播标题"}, ensure_ascii=False)
        notes = CoverAI(self.service.storage, llm=FakeModel(reply), settings=lambda: CONFIGURED).inspect(_thumb("red"))
        self.assertEqual(notes.busy, frozenset({"top", "right"}))
        self.assertIsNone(notes.box)
        framed = json.dumps({"box": [0.3, 0.16, 0.75, 0.76], "close": [0.4, 0.2, 0.42, 0.25], "text_zone": "BOTTOM"})
        notes = CoverAI(self.service.storage, llm=FakeModel(framed), settings=lambda: CONFIGURED).inspect(_thumb("blue"))
        # 太小的框不用；位置大小写不敏感。
        self.assertEqual((notes.box, notes.close, notes.text_zone), ((0.3, 0.16, 0.75, 0.76), None, "bottom"))

    def test_view_box_frames_the_person_and_text_goes_to_the_clean_zone(self):
        view = (0.38, 0.15, 0.62, 0.62)
        candidates = self.service.ai_candidates(self.document, self.frame, COPIES, views=(view, None), text_zone="bottom")
        framed = [item for item in candidates if self._background(item.document).scale > 1.05]
        self.assertGreaterEqual(len(framed), 6)
        for item in framed:
            # 字放在 AI 说干净的下方，人物靠上，留出字带。
            self.assertGreater(self._headline(item.document).transform.y, 0.5)
            context = next((obj for obj in item.document.objects if isinstance(obj, TextObject) and obj.copy_role == "A"), None)
            context = object_for_profile(item.document, context.id, "4x3") if context is not None else None
            if context is not None and context.visible and context.transform.y < 0.3:
                continue  # 上下分置：上下都放字，人物居中
            background = self._background(item.document)
            drawn = background_box((1920, 1080), (1440, 1080), scale=background.scale,
                                   focus_x=background.pan_x, focus_y=background.pan_y)
            person_top = (drawn.top + view[1] * drawn.height) / 1080
            person_bottom = (drawn.top + view[3] * drawn.height) / 1080
            self.assertLess(person_top, 0.12)
            self.assertLess(person_bottom, 0.72)
        # 原画面留两张兜底。
        self.assertTrue(any(self._background(item.document).scale == 1.0 for item in candidates))

    def test_layout_engine_follows_the_given_position(self):
        copy = AICopy("", "交个备用机", "反差", "")

        def headline(idea):
            (scheme,) = self.service.schemes_from_ideas(self.document, self.frame, (idea,))
            return self._headline(scheme.document)

        top = headline(AISchemeIdea("大胆一点", copy, "headline", "duo", "", place="top"))
        bottom = headline(AISchemeIdea("大胆一点", copy, "headline", "duo", "", place="bottom"))
        self.assertLess(top.transform.y, 0.3)
        self.assertGreater(bottom.transform.y, 0.5)
        left = headline(AISchemeIdea("换个构图", copy, "slot", "duo", "", place="left"))
        right = headline(AISchemeIdea("换个构图", copy, "slot", "duo", "", place="right"))
        self.assertLess(left.transform.x, right.transform.x)


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class CoverAIEditorTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover import CoverEditorWidget
        from autoslice.desktop.cover_draft import CoverDraft
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
        # 第 1 张候选用标题提炼的文案；第 2 张用 AI 第一组（带 A）；第 3 张是只留 B 的大字。
        self.model = RoutingModel(_analysis_reply(), _choice_reply(("稳妥", 1), ("换个构图", 2), ("大胆一点", 3)))
        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=self.model, settings=lambda: CONFIGURED)
        self.widget.project, self.widget.video = self.project, video
        document = self.widget.service.load_document(self.project, video)[0]
        self.widget.document = replace(document, objects=tuple(
            replace(item, asset=AssetRef(path=str(frame))) if isinstance(item, BackgroundObject) else item
            for item in document.objects
        ))
        self.widget.draft = CoverDraft.from_document(self.widget.document)
        self.widget.history.reset(self.widget.document)
        self.widget._apply_draft()

    def wait_for(self, condition, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.02)
        self.fail("等待超时")

    def _texts(self, key="4x3"):
        return {
            item.copy_role: object_for_profile(self.widget.document, item.id, key)
            for item in self.widget.document.objects if isinstance(item, TextObject)
        }

    def test_ai_picks_from_rendered_candidates_and_feeds_copy_rotation(self):
        self.assertTrue(self.widget.ai_scheme_button.isEnabled())
        self.widget._ai_schemes()
        self.wait_for(lambda: self.widget.scheme_title.text() == "AI 方案")
        self.assertEqual([label.text() for label in self.widget.scheme_labels], ["AI·稳妥", "AI·换个构图", "AI·大胆一点"])
        # 先看原画面、写文案，再看候选总图（附原画面对照）挑选。
        self.assertEqual(sorted(call[0] for call in self.model.calls), ["analysis", "choice", "inspect"])
        self.assertIn(("choice", "gpt-5.6-luna", 2), self.model.calls)
        self.assertIn("这个位置竞演可以场外干涉", self.widget.ai_hint.text())
        self.assertEqual(self.widget._copy_variants[0].headline, "居然会改变选曲")
        self.widget._apply_scheme(2)
        texts = self._texts()
        self.assertFalse("A" in texts and texts["A"].visible)
        self.assertEqual(self.widget._applied_scheme, "ai:大胆一点")
        self.assertTrue(self.widget.undo_button.isEnabled())

    def test_ai_scheme_with_context_adds_a_block_to_b_only_cover(self):
        self.assertNotIn("A", self._texts())
        self.widget._ai_schemes()
        self.wait_for(lambda: self.widget.scheme_title.text() == "AI 方案")
        self.widget._apply_scheme(1)
        texts = self._texts()
        self.assertEqual((texts["A"].text, texts["A"].visible), ("选秀", True))
        self.assertLess(texts["A"].style.font_size, texts["B"].style.font_size)

    def test_all_candidates_rejected_keeps_local_schemes(self):
        self.model.replies["choice"] = _choice_reply(rejected="都压住了画面原有的字")
        self.widget._ai_schemes()
        self.wait_for(lambda: self.widget.ai_scheme_button.isEnabled())
        self.assertIn("都压住了画面原有的字", self.widget.notice_label.text())
        self.assertEqual(self.widget.scheme_title.text(), "快速方案")

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

    def test_ai_fixes_apply_to_the_cover_and_undo(self):
        from autoslice.desktop.cover_ai import AIFix

        def background():
            item = next(obj for obj in self.widget.document.objects if isinstance(obj, BackgroundObject))
            return object_for_profile(self.widget.document, item.id, "4x3")

        start = self._texts()["B"]
        self.assertTrue(self.widget._apply_ai_fix(AIFix("字太小", "bigger")))
        self.assertGreater(self._texts()["B"].style.font_size, start.style.font_size)
        scale = background().scale
        self.assertTrue(self.widget._apply_ai_fix(AIFix("太远", "zoom_in")))
        self.assertAlmostEqual(background().scale, scale * 1.25, places=3)
        zoomed = background()
        # 移字只重排文字，刚拉近的取景不动。
        self.assertTrue(self.widget._apply_ai_fix(AIFix("压脸", "move_top")))
        self.assertLess(self._texts()["B"].transform.y, 0.3)
        self.assertEqual((background().scale, background().pan_x, background().pan_y),
                         (zoomed.scale, zoomed.pan_x, zoomed.pan_y))
        self.assertTrue(self.widget._apply_ai_fix(AIFix("文案空", "rewrite", "选秀", "居然会改变选曲")))
        texts = self._texts()
        self.assertEqual((texts["A"].text, texts["B"].text), ("选秀", "居然会改变选曲"))
        for _ in range(4):
            self.widget._undo()
        self.assertEqual(self._texts()["B"].text, start.text)
        self.assertEqual(self._texts()["B"].style.font_size, start.style.font_size)

    def test_ai_failure_shows_reason(self):
        self.widget.service.ai = CoverAI(self.widget.service.storage, llm=FakeModel(), settings=AISettings)
        self.widget._ai_schemes()
        self.wait_for(lambda: self.widget.ai_scheme_button.isEnabled())
        self.assertIn("设置页", self.widget.notice_label.text())
        self.assertEqual(self.widget.scheme_title.text(), "快速方案")


if __name__ == "__main__":
    unittest.main()
