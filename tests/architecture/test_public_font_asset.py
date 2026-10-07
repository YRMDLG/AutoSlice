"""内置字体的精确发布边界回归。"""

import hashlib
import unittest

from scripts import scan_public_release


class PublicFontAssetTests(unittest.TestCase):
    def test_only_declared_font_path_is_allowed(self):
        relative, expected = next(iter(scan_public_release.PUBLIC_FONT_ASSETS.items()))
        font_path = scan_public_release.ROOT / relative
        self.assertEqual(scan_public_release._path_errors(font_path), [])
        self.assertEqual(hashlib.sha256(font_path.read_bytes()).hexdigest(), expected)
        self.assertTrue(scan_public_release._path_errors(font_path.with_name("personal.ttf")))
        notice = font_path.with_name("NOTICE.md").read_text(encoding="utf-8")
        self.assertIn("Shang hai Rui Xian Creative Design Co., Ltd.", notice)
        self.assertIn(expected, notice)
