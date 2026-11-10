"""常用色板的一键选色、自定义色号、跨控件记忆及透明色契约。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.desktop.foundation import DesktopStorage

try:
    from PySide6.QtWidgets import QApplication, QColorDialog

    from autoslice.desktop.cover_widgets import _ColorButton
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class CoverPaletteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = QApplication.instance() or QApplication([])
        self.storage = DesktopStorage(Path(self.temp.name))

    def button(self, **options):
        button = _ColorButton(storage=self.storage, **options)
        self.addCleanup(button.close)
        self.addCleanup(button.deleteLater)
        return button

    def test_cyan_is_one_click_and_does_not_open_the_full_color_dialog(self):
        button = self.button()
        changes = []
        button.color_changed.connect(lambda: changes.append(button.color()))
        with patch.object(QColorDialog, "getColor") as dialog:
            button.menu().swatches["#16D8ED"].click()
        dialog.assert_not_called()
        self.assertEqual(button.color(), "#16D8ED")
        self.assertEqual(changes, ["#16D8ED"])
        self.assertLessEqual(len(button.menu().swatches), 24)

    def test_custom_hex_is_recalled_by_a_new_color_button(self):
        first = self.button()
        first.menu().hex_edit.setText("a1b2c3")
        first.menu().apply_button.click()
        self.assertEqual(first.color(), "#A1B2C3")
        second = self.button()
        self.assertEqual(second.menu().recent_row.itemAt(0).widget().toolTip(), "#A1B2C3")
        second.menu().recent_row.itemAt(0).widget().click()
        self.assertEqual(second.color(), "#A1B2C3")

    def test_bad_hex_keeps_the_current_color(self):
        button = self.button()
        button.set_color("#FFE438")
        button.menu().hex_edit.setText("#GG0000")
        button.menu().apply_button.click()
        self.assertEqual(button.color(), "#FFE438")
        self.assertIn("六位", button.menu().notice.text())

    def test_alpha_and_none_remain_available(self):
        button = self.button(allow_none=True, allow_alpha=True)
        button.menu().hex_edit.setText("#12345680")
        button.menu().apply_button.click()
        self.assertEqual(button.color(), "#12345680")
        next(action for action in button.menu().actions() if action.text() == "无").trigger()
        self.assertEqual(button.color(), "")
        opaque = self.button()
        self.assertFalse(opaque.menu().recent_panel.isVisibleTo(opaque.menu()))

    def test_corrupt_recent_colors_do_not_prevent_selection(self):
        path = self.storage.root / "cover" / "colors.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"recent": {"bad": "value"}}', encoding="utf-8")
        button = self.button()
        button.menu().swatches["#16D8ED"].click()
        self.assertEqual(button.color(), "#16D8ED")


if __name__ == "__main__":
    unittest.main()
