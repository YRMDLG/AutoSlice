"""libmpv 字幕预览刷新策略的回归测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, call

try:
    from autoslice.desktop.qt_app.player import MpvAdapter
except ImportError:  # 当前基础解释器未安装 PySide6 时保留可运行的测试发现。
    MpvAdapter = None


@unittest.skipIf(MpvAdapter is None, "PySide6 不在当前解释器中")
class MpvSubtitleRefreshTests(unittest.TestCase):
    def _adapter(self):
        adapter = MpvAdapter.__new__(MpvAdapter)
        adapter._closed = False
        adapter._handle = object()
        adapter._subtitle_loaded = False
        adapter._subtitle_path = None
        adapter._submit = lambda _kind, action: action()
        adapter._command = Mock()
        adapter._set_property = Mock()
        return adapter

    def test_first_update_adds_once_then_reloads_same_track(self):
        adapter = self._adapter()
        path = Path("preview.ass")

        adapter.set_subtitle(path)
        adapter.set_subtitle(path)

        adapter._command.assert_has_calls([
            call("sub-add", path, "select"),
            call("sub-reload"),
        ])
        self.assertEqual(adapter._command.call_count, 2)
        adapter._set_property.assert_any_call("sub-visibility", "yes")
        self.assertEqual(adapter._subtitle_path, path)

    def test_clear_only_hides_track(self):
        adapter = self._adapter()

        adapter.clear_subtitle()

        adapter._set_property.assert_called_once_with("sub-visibility", "no")
        adapter._command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
