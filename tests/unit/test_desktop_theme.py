"""夜航 / 晨雾两套主题：令牌、预混色、持久化和即时切换。"""

from __future__ import annotations

import re
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QPushButton

from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.qt_preview import appearance, icons, theme
from autoslice.desktop.qt_preview.appearance import Appearance

HEX = re.compile(r"^#[0-9A-F]{6}$")


class ThemeTokenTests(unittest.TestCase):
    def tearDown(self):
        theme.apply_theme("night")

    def test_color_mix_matches_css_srgb(self):
        # color-mix(in srgb, #D8E6F7 45%, #141413)：夜航选中描边
        self.assertEqual(theme.color_mix("#D8E6F7", 0.45, "#141413"), "#6C727A")
        self.assertEqual(theme.color_mix("#FFFFFF", 1.0, "#000000"), "#FFFFFF")
        self.assertEqual(theme.color_mix("#FFFFFF", 0.0, "#000000"), "#000000")

    def test_every_token_is_a_hex_color_with_its_role_in_both_themes(self):
        for name, spec in theme.THEMES.items():
            for accent in spec.accents:
                theme.apply_theme(name, accent)
                for item in fields(theme.Palette):
                    value = getattr(theme.COLORS, item.name)
                    self.assertRegex(value.upper(), HEX, f"{name}.{item.name}")
                    self.assertEqual(value.role, item.name)

    def test_text_stays_readable_on_cards_and_ground(self):
        for name in theme.THEMES:
            theme.apply_theme(name)
            c = theme.COLORS
            for surface in (c.card, c.ground):
                self.assertGreaterEqual(theme.contrast(c.text, surface), 7.0, name)
                self.assertGreaterEqual(theme.contrast(c.muted, surface), 4.5, name)
                self.assertGreaterEqual(theme.contrast(c.subtle, surface), 4.5, name)

    def test_selected_playing_and_hover_stay_distinguishable(self):
        for name in theme.THEMES:
            theme.apply_theme(name)
            c = theme.COLORS
            # 字幕行：选中靠强调描边，播放与悬停底色不同
            self.assertNotEqual(c.accent_line, c.row_selected)
            self.assertNotEqual(c.playback, c.hover)
            # 时间轴块：三态底色和描边两两不同
            self.assertEqual(len({c.block, c.block_playing, c.block_selected}), 3, name)
            self.assertEqual(len({c.block_border, c.block_playing_border, c.block_selected_border}), 3, name)

    def test_night_buttons_are_outlined_and_morning_primary_is_filled(self):
        theme.apply_theme("night")
        self.assertFalse(theme.STYLE.filled_buttons)
        self.assertFalse(theme.STYLE.filled_primary)
        self.assertEqual(theme.COLORS.primary_text, theme.COLORS.accent)
        theme.apply_theme("morning")
        self.assertTrue(theme.STYLE.filled_primary)
        self.assertEqual(theme.STYLE.card_radius, 12)

    def test_morning_accent_has_no_cyan(self):
        for accent in theme.THEMES["morning"].accents:
            r, g, b = (int(accent[i:i + 2], 16) for i in (1, 3, 5))
            # 淡蓝灰：蓝最多，红绿接近；青色会是绿蓝都高、红明显低
            self.assertGreaterEqual(b, g)
            self.assertLess(b - r, 32)

    def test_bad_values_fall_back_to_defaults(self):
        self.assertEqual(theme.normalize("sunset", "#123456", "#FFFFFF"), ("night", "#D8E6F7", "#0A0A0A"))
        self.assertEqual(theme.normalize("morning", "#d5dee8", "#000000"), ("morning", "#D5DEE8", ""))
        self.assertEqual(theme.normalize("night", "#e4eefa", "#000000"), ("night", "#E4EEFA", "#000000"))

    def test_stylesheet_follows_current_theme(self):
        theme.apply_theme("night")
        night = theme.stylesheet()
        theme.apply_theme("morning")
        morning = theme.stylesheet()
        self.assertIn("#0A0A0A", night)
        self.assertIn("#F4F3F0", morning)
        self.assertNotEqual(night, morning)


def _switch(value):
    """轻量切换：只换令牌、通知换主题钩子，不重套整个应用的样式表。

    测试会话里前面的用例留下了很多窗口，真的重套全局样式表会把它们全部重新套一遍，慢到像卡死。
    """

    theme.apply_theme(value.theme, value.accent, value.ground)
    appearance.hub().changed.emit()
    return appearance.current()


class AppearanceTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.storage = DesktopStorage(Path(self.temp.name))

    def tearDown(self):
        _switch(Appearance.of("night"))
        self.temp.cleanup()

    def test_preferences_round_trip_and_survive_session_saves(self):
        appearance.save(self.storage, Appearance.of("morning", "#CBD6E2"))
        self.storage.save_session({"page": 1})
        self.assertEqual(appearance.load(self.storage), Appearance("morning", "#CBD6E2", ""))

    def test_missing_or_broken_preferences_load_night(self):
        self.assertEqual(appearance.load(self.storage), Appearance.of("night"))
        self.storage.preferences_path.write_text("{oops", encoding="utf-8")
        self.assertEqual(appearance.load(self.storage), Appearance.of("night"))

    def test_apply_restyles_app_and_recolors_themed_icons(self):
        button = QPushButton("保存")
        button.setIcon(icons.icon("save", theme.COLORS.primary_text))
        before = button.icon().cacheKey()
        fake = Mock()
        fake.allWidgets.return_value = [button]
        appearance.apply(fake, Appearance.of("morning"))
        fake.setStyleSheet.assert_called_once()
        self.assertIn("#F4F3F0", fake.setStyleSheet.call_args.args[0])
        fake.setPalette.assert_called_once()
        self.assertNotEqual(button.icon().cacheKey(), before)
        self.assertEqual(icons.spec_of(button.icon())[1], "primary_text")

    def test_themed_hook_reruns_on_switch_and_dies_with_owner(self):
        owner = QPushButton()
        calls = []
        appearance.themed(owner, lambda: calls.append(theme.current_theme()[0]))
        _switch(Appearance.of("morning"))
        self.assertEqual(calls, ["night", "morning"])
        owner.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _switch(Appearance.of("night"))
        self.assertEqual(len(calls), 2)

    def test_settings_panel_switches_theme_instantly_and_saves(self):
        from autoslice.desktop.qt_app.appearance_panel import AppearancePanel

        panel = AppearancePanel(self.storage)
        self.addCleanup(panel.deleteLater)
        with patch.object(appearance, "apply", side_effect=lambda _app, value: _switch(value)) as applied:
            panel.theme_buttons["morning"].click()
            self.assertEqual(applied.call_count, 1)
            self.assertEqual(theme.current_theme()[0], "morning")
            self.assertEqual(theme.COLORS.ground, "#F4F3F0")
            self.assertEqual(appearance.load(self.storage).theme, "morning")
            self.assertFalse(panel.ground_row.isVisibleTo(panel))
            panel.accent_buttons[2].click()
        self.assertEqual(theme.COLORS.accent, theme.THEMES["morning"].accents[2])
        self.assertTrue(panel.accent_buttons[2].isChecked())
        self.assertFalse(panel.accent_buttons[0].isChecked())


if __name__ == "__main__":
    unittest.main()
