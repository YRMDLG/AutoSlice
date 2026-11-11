"""Desktop AI 建议使用 fake 检查器，不产生外部模型请求。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autoslice.desktop.ai_review import AIReviewService, AIReviewSession, Suggestion, document_hash
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import SubmissionProjectService
from autoslice.desktop.subtitles import SubtitleDocument


class DesktopAIReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        folder = self.root / "【投稿】测试😄"
        folder.mkdir()
        (folder / "成片.mp4").touch()
        self.source = folder / "成片.srt"
        self.source.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n错字一\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n错字二\n", encoding="utf-8"
        )
        project = SubmissionProjectService(self.root).refresh().projects[0]
        self.title = project.title
        self.document = SubtitleDocument.load(project.videos[0])
        self.calls = []
        self.config = SimpleNamespace(base_url="https://example.invalid", api_type="openai",
                                      model="existing-model", review_reasoning_effort="high")
        self.profile = SimpleNamespace(id="test", label="测试",
                                       subtitle_review_fingerprint=lambda: "rules-v1")
        def checker(path, **kwargs):
            self.calls.append((str(path), kwargs["use_cache"]))
            return {"suggestions": [
                {"index": 1, "original": "错字一", "corrected": "正字一", "reason": "错别字", "confidence": .9},
                {"index": 2, "original": "错字二", "corrected": "正字二", "reason": "错别字", "confidence": .8},
            ]}
        self.service = AIReviewService(DesktopStorage(self.root / "app-data"),
                                       checker=checker, config_loader=lambda: self.config)
        patcher = patch("autoslice.desktop.ai_review.resolve_streamer_profile", return_value=self.profile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pending_accept_skip_undo_and_private_restart(self):
        session = self.service.check(self.document, self.title)
        self.assertEqual(len(session.pending), 2)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][0].startswith(str(self.service.storage.ai_cache)))
        first, second = session.pending
        self.document.edit_text(first.cue_id, first.suggested_text)
        session.mark(first.suggestion_id, "accepted", self.document.entries)
        session.mark(second.suggestion_id, "skipped", self.document.entries)
        self.service.save(self.document, self.title, session)
        self.assertEqual(self.source.read_text(encoding="utf-8").count("错字一"), 1)
        self.assertEqual(self.document.entries[1].text, "错字二")
        self.assertTrue(self.document.dirty)
        self.assertTrue(self.document.undo())
        session.mark(first.suggestion_id, "pending", self.document.entries)
        self.assertEqual(self.document.entries[0].text, "错字一")
        self.assertTrue(self.document.redo())
        session.mark(first.suggestion_id, "accepted", self.document.entries)
        self.service.save(self.document, self.title, session)
        restored, status = self.service.load(self.document, self.title)
        self.assertEqual(status, "ready")
        self.assertEqual([item.status for item in restored.suggestions], ["accepted", "skipped"])
        self.assertEqual(len(self.calls), 1)

    def test_content_model_and_source_change_invalidate(self):
        session = self.service.check(self.document, self.title)
        self.service.save(self.document, self.title, session)
        self.document.edit_text(1, "人工修改")
        # 走查 M14：字幕改了只作废对不上的那条，另一条原文没变照样恢复
        restored, status = self.service.load(self.document, self.title)
        self.assertEqual(status, "partial")
        self.assertEqual([item.cue_id for item in restored.pending], [2])
        self.assertEqual(restored.dropped, 1)
        self.document.edit_text(2, "也改了")
        self.assertEqual(self.service.load(self.document, self.title)[1], "stale")
        self.document.undo()
        self.document.undo()
        self.config.model = "changed-model"
        self.assertEqual(self.service.load(self.document, self.title)[1], "stale")
        self.config.model = "existing-model"
        self.source.write_text(self.source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        changed = SubtitleDocument.load(self.document.video)
        self.assertEqual(self.service.load(changed, self.title)[1], "stale")

    def test_force_recheck_is_explicit(self):
        self.service.check(self.document, self.title)
        self.service.check(self.document, self.title, force=True)
        self.assertEqual([use_cache for _, use_cache in self.calls], [True, False])

    def test_missing_config_is_recoverable(self):
        service = AIReviewService(self.service.storage, checker=self.service.checker,
                                  config_loader=lambda: (_ for _ in ()).throw(ValueError("未配置 LLM API")))
        with self.assertRaisesRegex(ValueError, "未配置"):
            service.check(self.document, self.title)

    def test_local_session_restores_processed_states_without_ai_configuration(self):
        self.service.config_loader = lambda: (_ for _ in ()).throw(ValueError("未配置 AI"))
        session = AIReviewSession([
            Suggestion("local-1", 1, "错字一", "正字一", "同音", None, status="skipped", source="homophone"),
            Suggestion("local-2", 2, "错字二", "正字二", "同音", None, source="homophone"),
        ], "local-cache", document_hash(self.document.entries), local_only=True)
        self.service.save(self.document, self.title, session)
        restored, status = self.service.load(self.document, self.title)
        self.assertEqual(status, "ready")
        self.assertTrue(restored.local_only)
        self.assertEqual([item.status for item in restored.suggestions], ["skipped", "pending"])
        self.assertEqual(self.calls, [])

    def test_correction_memory_creates_local_suggestions_without_changing_text(self):
        fix = [("错字一", "正字一")]
        self.service.memory.learn("test", "one", fix)
        self.service.memory.learn("test", "two", fix)
        suggestions = self.service.local_suggestions(self.document, self.title)
        self.assertEqual([(item.cue_id, item.suggested_text, item.source) for item in suggestions],
                         [(1, "正字一", "memory"), (2, "正字二", "memory")])
        self.assertEqual(self.document.entries[0].text, "错字一")


if __name__ == "__main__":
    unittest.main()
