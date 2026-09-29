"""Desktop 草稿与会话基础协议的定向验证。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoslice.desktop.foundation import DesktopStorage


class DesktopFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "【投稿】标题😄"
        self.project.mkdir()
        self.source = self.project / "成片.srt"
        self.source.write_text("1\n00:00:01,000 --> 00:00:02,000\n原文\n", encoding="utf-8")
        self.corrected = self.project / "成片_校对.srt"
        self.storage = DesktopStorage(self.root / "app-data")

    def test_draft_roundtrip_without_touching_source(self):
        before = self.source.read_bytes()
        draft = self.storage.save_draft(
            "subtitle", self.project, self.source, {"cue": 1, "text": "未保存✍️"},
            dependencies=[self.corrected],
        )
        loaded = self.storage.read_draft(
            "subtitle", self.project, self.source, dependencies=[self.corrected],
        )
        self.assertEqual(loaded.status, "ready")
        self.assertEqual(loaded.payload["text"], "未保存✍️")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(draft.is_relative_to(self.storage.drafts))
        self.assertFalse(list(draft.parent.glob("*.tmp")))

    def test_changed_source_or_corrected_file_requires_review(self):
        self.storage.save_draft(
            "subtitle", self.project, self.source, {"text": "草稿"},
            dependencies=[self.corrected],
        )
        self.corrected.write_text("外部新建", encoding="utf-8")
        self.assertEqual(
            self.storage.read_draft(
                "subtitle", self.project, self.source, dependencies=[self.corrected],
            ).status,
            "source_changed",
        )
        self.storage.save_draft(
            "subtitle", self.project, self.source, {"text": "继续编辑"},
            dependencies=[self.corrected],
        )
        self.assertEqual(
            self.storage.read_draft(
                "subtitle", self.project, self.source, dependencies=[self.corrected],
            ).status,
            "source_changed",
        )
        self.corrected.unlink()
        self.source.write_text("外部改动", encoding="utf-8")
        self.assertEqual(
            self.storage.read_draft(
                "subtitle", self.project, self.source, dependencies=[self.corrected],
            ).status,
            "source_changed",
        )

    def test_unknown_schema_is_kept_but_not_loaded(self):
        path = self.storage.save_draft("subtitle", self.project, self.source, {"text": "草稿"})
        value = json.loads(path.read_text(encoding="utf-8"))
        value["schema_version"] = 99
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.storage.read_draft("subtitle", self.project, self.source).status,
                         "incompatible")
        self.assertTrue(path.exists())

    def test_baseline_from_editor_open_detects_change_before_first_autosave(self):
        baseline = self.storage.capture_baseline(
            self.source, dependencies=[self.corrected],
        )
        self.source.write_text("外部修改发生在首次自动保存前", encoding="utf-8")
        self.storage.save_draft(
            "subtitle", self.project, self.source, {"text": "编辑器内容"},
            dependencies=[self.corrected], baseline=baseline,
        )
        self.assertEqual(
            self.storage.read_draft(
                "subtitle", self.project, self.source, dependencies=[self.corrected],
            ).status,
            "source_changed",
        )

    def test_session_and_cache_are_separate(self):
        self.storage.save_session({"page": "字幕校对", "cue": 3})
        self.storage.thumbnails.mkdir(parents=True)
        (self.storage.thumbnails / "cached.png").touch()
        (self.storage.thumbnails / "cached.png").unlink()
        self.assertEqual(self.storage.read_session(), {"page": "字幕校对", "cue": 3})
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
