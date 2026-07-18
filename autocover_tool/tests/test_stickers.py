"""本地表情包素材库测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from autocover.stickers import StickerLibrary


class StickerLibraryTests(unittest.TestCase):
    """验证中文目录扫描、稳定 ID 和路径安全。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "视频素材"
        self.expressions = self.root / "沐霂表情包"
        self.nested = self.root / "表情包" / "常用"
        self.cover = self.root / "封面"
        for directory in (self.expressions, self.nested, self.cover):
            directory.mkdir(parents=True)
        Image.new("RGBA", (160, 120), (255, 0, 80, 128)).save(self.expressions / "害羞.png")
        Image.new("RGB", (240, 180), "#40c8dd").save(self.nested / "震惊.jpg")
        Image.new("RGB", (320, 180), "#ffffff").save(self.cover / "普通封面.png")
        (self.expressions / "说明.txt").write_text("不是图片", encoding="utf-8")
        (self.expressions / "损坏.png").write_bytes(b"not-an-image")
        self.library = StickerLibrary(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scans_only_valid_expression_pack_images(self) -> None:
        assets = self.library.scan()

        self.assertEqual([asset.name for asset in assets], ["害羞", "震惊"])
        self.assertEqual({asset.group for asset in assets}, {"沐霂表情包", "表情包"})
        self.assertEqual({(asset.width, asset.height) for asset in assets}, {(160, 120), (240, 180)})
        payload = assets[0].to_dict()
        self.assertNotIn(str(self.root), str(payload))
        self.assertNotIn("path", payload)

    def test_asset_ids_are_stable_and_resolve_registered_files(self) -> None:
        first = self.library.scan()
        second = self.library.scan()

        self.assertEqual([asset.id for asset in first], [asset.id for asset in second])
        path = self.library.resolve(first[0].id)
        self.assertTrue(path.is_file())
        self.assertTrue(path.is_relative_to(self.root.resolve()))

    def test_rejects_unknown_ids_and_missing_registered_files(self) -> None:
        asset = self.library.scan()[0]
        with self.assertRaisesRegex(KeyError, "不存在"):
            self.library.resolve("../Windows")

        self.library.resolve(asset.id).unlink()
        with self.assertRaisesRegex(FileNotFoundError, "已不存在"):
            self.library.resolve(asset.id)

    def test_missing_root_returns_an_empty_library(self) -> None:
        library = StickerLibrary(self.root / "不存在")

        self.assertEqual(library.scan(), [])
        self.assertEqual(library.list_assets(), [])


if __name__ == "__main__":
    unittest.main()
