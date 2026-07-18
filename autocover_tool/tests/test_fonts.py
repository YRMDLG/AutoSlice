"""默认字体选择和回退测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import ImageFont

from autocover import fonts


class FontTests(unittest.TestCase):
    """验证本机濑户体、环境变量和系统回退的优先级。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.existing_font = next(
            (path for path in fonts._system_font_candidates() if path.is_file()),
            None,
        )
        if self.existing_font is None:
            self.skipTest("当前测试环境没有可用的系统字体")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_environment_font_has_highest_priority(self) -> None:
        missing_local = self.root / "missing-local.ttf"
        with (
            patch.dict(os.environ, {fonts.FONT_PATH_ENV: str(self.existing_font)}),
            patch("autocover.fonts.LOCAL_FONT_PATH", missing_local),
        ):
            status = fonts.get_default_font_status()

        self.assertTrue(status.available)
        self.assertEqual(status.source, "environment")
        self.assertEqual(status.font_path, self.existing_font.resolve())

    def test_invalid_environment_value_falls_back_to_local_font(self) -> None:
        missing_environment = self.root / "missing-environment.ttf"
        with (
            patch.dict(os.environ, {fonts.FONT_PATH_ENV: str(missing_environment)}),
            patch("autocover.fonts.LOCAL_FONT_PATH", self.existing_font),
        ):
            status = fonts.get_default_font_status()

        self.assertTrue(status.available)
        self.assertEqual(status.source, "local")
        self.assertIn(fonts.FONT_PATH_ENV, status.warning or "")

    def test_missing_preferred_font_uses_system_fallback(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("autocover.fonts.LOCAL_FONT_PATH", self.root / "missing.ttf"),
            patch("autocover.fonts._system_font_candidates", return_value=(self.existing_font,)),
        ):
            status = fonts.get_default_font_status()

        self.assertFalse(status.available)
        self.assertEqual(status.source, "system")
        self.assertEqual(status.render_path, self.existing_font.resolve())
        self.assertNotIn(str(self.existing_font), str(status.to_public_dict()))

    def test_custom_font_path_is_validated(self) -> None:
        resolved = fonts.resolve_font_path(self.existing_font)

        self.assertEqual(Path(resolved), self.existing_font.resolve())
        with self.assertRaisesRegex(FileNotFoundError, "字体文件不存在"):
            fonts.resolve_font_path(self.root / "missing.ttf")

    def test_local_bilibili_font_can_render_chinese_when_configured(self) -> None:
        if not fonts.LOCAL_FONT_PATH.is_file():
            self.skipTest("本机尚未配置 B 站濑户体")

        with patch.dict(os.environ, {}, clear=True):
            status = fonts.get_default_font_status()
        font = ImageFont.truetype(str(status.font_path), size=48)

        self.assertTrue(status.available)
        self.assertEqual(status.source, "local")
        self.assertIn("Seto", status.family)
        self.assertGreater(font.getlength("音音封面"), 0)


if __name__ == "__main__":
    unittest.main()
