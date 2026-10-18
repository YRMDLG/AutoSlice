"""同音检测：本地找“发音和术语相同、写法不同”的字幕片段；并进 AI 检查队列、本地单独检查按钮。

需要 pypinyin；没装时只测“整体关闭”那条。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autoslice.desktop import homophone
from autoslice.desktop.ai_review import AIReviewService
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.homophone import HomophoneIndex, apply_hits, describe
from autoslice.desktop.projects import SubmissionProjectService
from autoslice.desktop.subtitles import SubtitleDocument

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

needs_pinyin = unittest.skipUnless(homophone.available(), "没装 pypinyin")
WRONG, RIGHT = "紫雨亭", "子语庭"


@needs_pinyin
class HomophoneIndexTests(unittest.TestCase):
    def test_finds_same_pronunciation_with_different_characters_and_replaces_it(self):
        index = HomophoneIndex([RIGHT])
        text = f"今天来聊聊{WRONG}这个活动"
        hits = index.find(text)
        self.assertEqual([(hit.found, hit.term) for hit in hits], [(WRONG, RIGHT)])
        self.assertEqual(text[hits[0].start:hits[0].end], WRONG)
        self.assertEqual(apply_hits(text, hits), f"今天来聊聊{RIGHT}这个活动")
        self.assertIn(WRONG, describe(hits))
        self.assertIn(RIGHT, describe(hits))

    def test_the_correct_term_and_unrelated_text_are_left_alone(self):
        index = HomophoneIndex([RIGHT])
        self.assertEqual(index.find(f"今天来聊聊{RIGHT}这个活动"), [])
        self.assertEqual(index.find("大家好欢迎来到直播间"), [])
        self.assertEqual(index.find(""), [])

    def test_three_character_terms_ignore_tones_but_two_character_terms_do_not(self):
        self.assertEqual([hit.found for hit in HomophoneIndex([RIGHT]).find("今天紫鱼停那边")], ["紫鱼停"])
        self.assertEqual([hit.found for hit in HomophoneIndex(["提督"]).find("这位题督很强")], ["题督"])
        self.assertEqual(HomophoneIndex(["提督"]).find("这位体督很强"), [], "声调不同的两字词不算，同音字太多")

    def test_latin_text_and_punctuation_break_the_window(self):
        index = HomophoneIndex([RIGHT])
        self.assertEqual(index.find("紫雨 亭"), [])
        self.assertEqual(index.find("紫雨ting"), [])

    def test_overlapping_matches_keep_the_longer_one_and_several_hits_apply_together(self):
        index = HomophoneIndex(["语庭", RIGHT])
        hits = index.find(f"{WRONG}和紫雨亭")
        self.assertEqual([hit.term for hit in hits], [RIGHT, RIGHT])
        self.assertEqual(apply_hits(f"{WRONG}和紫雨亭", hits), f"{RIGHT}和{RIGHT}")

    def test_earlier_terms_win_when_two_terms_sound_alike(self):
        index = HomophoneIndex(["子语庭", "紫雨廷"])
        self.assertEqual(index.find("紫雨亭")[0].term, "子语庭")

    def test_unusable_terms_are_ignored(self):
        index = HomophoneIndex(["a", "ABC", "一", "x" * 30, "", "提督提督提督提督提督", "ins上面"])
        self.assertEqual(index.find("提督提督提督提督提督"), [])


class HomophoneUnavailableTests(unittest.TestCase):
    def test_without_pypinyin_everything_is_off_and_nothing_raises(self):
        index = HomophoneIndex([RIGHT], enabled=False)
        self.assertFalse(index.ready)
        self.assertEqual(index.find(WRONG), [])


@needs_pinyin
class HomophoneReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        folder = self.root / "【投稿】测试"
        folder.mkdir()
        (folder / "成片.mp4").touch()
        (folder / "成片.srt").write_text(
            f"1\n00:00:01,000 --> 00:00:02,000\n今天来聊聊{WRONG}\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n这一条没问题\n\n"
            f"3\n00:00:05,000 --> 00:00:06,000\n{WRONG}真好看\n", encoding="utf-8")
        project = SubmissionProjectService(self.root).refresh().projects[0]
        self.title = project.title
        self.document = SubtitleDocument.load(project.videos[0])
        self.config = SimpleNamespace(base_url="https://example.invalid", api_type="openai",
                                      model="m", review_reasoning_effort="high")
        self.profile = SimpleNamespace(id="test", label="测试", subtitle_review_fingerprint=lambda: "v1")
        patcher = patch("autoslice.desktop.ai_review.resolve_streamer_profile", return_value=self.profile)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ai_covered = []

    def service(self, terms=(RIGHT,)):
        def checker(path, **_kwargs):
            return {"suggestions": [{"index": index, "original": text, "corrected": "AI 的改法", "reason": "AI", "confidence": .5}
                                    for index, text in self.ai_covered]}

        return AIReviewService(DesktopStorage(self.root / "app-data"), checker=checker,
                               config_loader=lambda: self.config, terms_provider=lambda d, t: list(terms))

    def test_local_suggestions_are_free_marked_and_need_no_ai_config(self):
        service = self.service()
        service.config_loader = lambda: (_ for _ in ()).throw(ValueError("未配置 AI"))
        found = service.local_suggestions(self.document, self.title)
        self.assertEqual([(item.cue_id, item.source, item.suggested_text) for item in found],
                         [(1, "homophone", f"今天来聊聊{RIGHT}"), (3, "homophone", f"{RIGHT}真好看")])
        self.assertTrue(all(item.status == "pending" and item.confidence is None for item in found))
        self.assertEqual(len({item.suggestion_id for item in found}), 2)

    def test_ai_check_merges_local_hits_but_lets_ai_own_the_cues_it_covered(self):
        self.ai_covered = [(1, f"今天来聊聊{WRONG}")]
        session = self.service().check(self.document, self.title)
        by_cue = {item.cue_id: item for item in session.suggestions}
        self.assertEqual(by_cue[1].source, "ai")
        self.assertEqual(by_cue[3].source, "homophone")
        self.assertEqual(len(session.suggestions), 2)
        self.assertFalse(session.local_only)

    def test_session_with_homophone_suggestions_survives_a_restart(self):
        service = self.service()
        session = service.check(self.document, self.title)
        service.save(self.document, self.title, session)
        restored, status = service.load(self.document, self.title)
        self.assertEqual(status, "ready")
        self.assertEqual(sorted(item.source for item in restored.suggestions), ["homophone", "homophone"])

    def test_terms_come_from_the_streamer_glossary_and_learned_terms_by_default(self):
        from autoslice.desktop.archive import Knowledge

        service = AIReviewService(DesktopStorage(self.root / "app-data"), config_loader=lambda: self.config)
        Knowledge(service.storage).add_glossary("zeyin", [RIGHT])
        folder = self.root / "【泽音】另一个项目"
        folder.mkdir()
        (folder / "成片.mp4").touch()
        (folder / "成片.srt").write_text(f"1\n00:00:01,000 --> 00:00:02,000\n聊聊{WRONG}\n", encoding="utf-8")
        project = next(p for p in SubmissionProjectService(self.root).refresh().projects if "泽音" in p.title)
        document = SubtitleDocument.load(project.videos[0])
        found = service.local_suggestions(document, project.title)
        self.assertEqual([item.suggested_text for item in found], [f"聊聊{RIGHT}"])


@needs_pinyin
@unittest.skipIf(QApplication is None, "PySide6 不可用")
class HomophoneQtTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.qt_app.window import DesktopWindow

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        folder = self.root / "中文项目"
        folder.mkdir()
        (folder / "视频.mp4").touch()
        (folder / "视频.srt").write_text(
            f"1\n00:00:01,000 --> 00:00:02,000\n今天来聊聊{WRONG}\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n这一条没问题\n", encoding="utf-8")
        self.app = QApplication.instance() or QApplication([])
        storage = DesktopStorage(self.root / "app-data")
        # 没配置 AI：同音检查照样能用
        service = AIReviewService(storage, config_loader=lambda: (_ for _ in ()).throw(ValueError("未配置 AI")),
                                  terms_provider=lambda d, t: [RIGHT])
        with patch("autoslice.desktop.qt_app.window.AIReviewService", return_value=service), \
                patch("autoslice.desktop.qt_app.window.MpvAdapter", side_effect=OSError("测试无播放器")):
            self.window = DesktopWindow(SubmissionProjectService(self.root), storage)
        self.window.show()

        def close_window():
            self.window._resolve_unsaved = lambda: True
            self.window.close()

        self.addCleanup(close_window)
        self.wait_for(lambda: bool(self.window.project_buttons))
        project = next(p for p in self.window.service.snapshot.projects if p.title == "中文项目")
        self.window._select_real_project(project)
        self.wait_for(lambda: self.window.document is not None)
        self.wait_for(lambda: not self.window._jobs and not self.window.cover_editor._jobs)

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail("Qt 任务未完成")

    def test_local_check_button_queues_accepts_and_stays_free_of_ai_prompts(self):
        window = self.window
        self.assertTrue(window.ai_local.isEnabled())
        window.ai_local.click()
        self.assertEqual(len(window.ai_session.pending), 1)
        self.assertTrue(window.ai_session.local_only)
        self.assertEqual(window.ai_run.text(), "AI 检查", "只有本地结果时不显示“重新检查”")
        self.assertIn("找到 1 处", window.ai_note.text())
        self.assertIn(f"今天来聊聊{RIGHT}", window.ai_diff.toolTip())
        window._accept_ai()
        self.assertEqual(window.document.entries[0].text, f"今天来聊聊{RIGHT}")
        self.assertTrue(window.document.dirty)
        # 再检查一遍：已经改对了，没有新的疑似
        window.ai_local.click()
        self.assertEqual(len(window.ai_session.pending), 0)
        self.assertIn("没有发现", window.ai_note.text())
        # 撤销后能再次发现
        self.assertTrue(window.document.undo())
        window.model.set_document(window.document)
        window.ai_session = None
        window.ai_local.click()
        self.assertEqual(len(window.ai_session.pending), 1)

    def test_missing_pypinyin_is_explained_instead_of_failing(self):
        with patch("autoslice.desktop.qt_app.window_ai.homophone_available", return_value=False):
            self.window.ai_local.click()
        self.assertIn("pypinyin", self.window.ai_note.text())
        self.assertIsNone(self.window.ai_session)


if __name__ == "__main__":
    unittest.main()
