"""本地表情包素材库测试。"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from autoslice_cover.stickers import StickerLibrary


class StickerLibraryTests(unittest.TestCase):
    """验证中文目录扫描、稳定 ID 和路径安全。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "视频素材"
        self.expressions = self.root / "沐霂表情包"
        self.nested = self.root / "表情包" / "常用"
        self.cover = self.root / "封面"
        self.imported = Path(self.temporary.name) / "导入贴图"
        for directory in (self.expressions, self.nested, self.cover):
            directory.mkdir(parents=True)
        Image.new("RGBA", (160, 120), (255, 0, 80, 128)).save(self.expressions / "害羞.png")
        Image.new("RGB", (240, 180), "#40c8dd").save(self.nested / "震惊.jpg")
        Image.new("RGB", (320, 180), "#ffffff").save(self.cover / "普通封面.png")
        (self.expressions / "说明.txt").write_text("不是图片", encoding="utf-8")
        (self.expressions / "损坏.png").write_bytes(b"not-an-image")
        self.library = StickerLibrary(self.root, import_root=self.imported)

    @staticmethod
    def _png_with_dimensions(width: int, height: int) -> bytes:
        """构造只含 PNG 头的测试输入，用于验证尺寸异常不会进入解码。"""

        import struct
        import zlib

        header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        chunk = b"IHDR" + header
        return (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", len(header))
            + chunk
            + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
            + b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scans_only_valid_expression_pack_images(self) -> None:
        assets = self.library.scan()

        self.assertEqual({asset.name for asset in assets}, {"害羞", "震惊"})
        self.assertEqual({asset.group for asset in assets}, {"沐霂表情包", "常用"})
        self.assertEqual({(asset.width, asset.height) for asset in assets}, {(160, 120), (240, 180)})
        payload = assets[0].to_dict()
        self.assertNotIn(str(self.root), str(payload))
        self.assertNotIn("path", payload)

        summary = self.library.summary()
        self.assertTrue(summary["available"])
        self.assertEqual(summary["asset_count"], 2)
        self.assertEqual(summary["group_count"], 2)
        self.assertEqual(summary["invalid_count"], 1)
        self.assertNotIn(str(self.root), str(summary))
        self.assertTrue((self.expressions / "损坏.png").is_file())

    def test_expression_root_groups_assets_by_streamer_directory(self) -> None:
        streamer_dir = self.root / "表情包" / "泽音melody" / "开心"
        streamer_dir.mkdir(parents=True)
        Image.new("RGBA", (180, 180), (255, 255, 255, 128)).save(streamer_dir / "音音.png")

        library = StickerLibrary(self.root / "表情包", import_root=self.imported)
        assets = library.scan()

        self.assertEqual({asset.name for asset in assets}, {"震惊", "音音"})
        self.assertEqual({asset.group for asset in assets}, {"常用", "泽音melody"})
        self.assertEqual(library.summary()["group_count"], 2)

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
        library = StickerLibrary(self.root / "不存在", import_root=self.imported)

        self.assertEqual(library.scan(), [])
        self.assertEqual(library.list_assets(), [])
        self.assertFalse(library.summary()["available"])

    def test_import_rejects_each_decode_budget_without_writing(self) -> None:
        image_bytes = io.BytesIO()
        Image.new("RGB", (2, 2), "#40c8dd").save(image_bytes, format="PNG")
        raw = image_bytes.getvalue()

        for name, setting, limit in (
            ("pixels", "MAX_STICKER_PIXELS", 3),
            ("width", "MAX_STICKER_WIDTH", 1),
            ("height", "MAX_STICKER_HEIGHT", 1),
            ("bytes", "MAX_STICKER_BYTES", len(raw) - 1),
        ):
            with self.subTest(name=name), patch(
                f"autoslice_cover.stickers.{setting}", limit
            ):
                with self.assertRaisesRegex(ValueError, "不是有效图片"):
                    self.library.import_image(f"超限-{name}.png", raw)
            self.assertFalse(any(self.imported.glob(f"超限-{name}-*")))

    def test_import_rejects_decompression_bomb_without_writing(self) -> None:
        raw = self._png_with_dimensions(100_000, 100_000)

        with self.assertRaisesRegex(ValueError, "不是有效图片"):
            self.library.import_image("超大.png", raw)
        self.assertFalse(any(self.imported.glob("超大-*")))

    def test_animated_webp_is_bounded_by_frame_budget(self) -> None:
        first = Image.new("RGBA", (8, 8), (255, 0, 0, 255))
        second = Image.new("RGBA", (8, 8), (0, 255, 0, 255))
        image_bytes = io.BytesIO()
        first.save(
            image_bytes,
            format="WEBP",
            save_all=True,
            append_images=[second],
            duration=20,
            loop=0,
        )

        with patch("autoslice_cover.stickers.MAX_STICKER_FRAMES", 1):
            with self.assertRaisesRegex(ValueError, "不是有效图片"):
                self.library.import_image("动画.webp", image_bytes.getvalue())

        asset = self.library.import_image("动画.webp", image_bytes.getvalue())
        self.assertEqual((asset.width, asset.height), (8, 8))
        self.assertTrue(any(self.imported.glob("动画-*.webp")))

    def test_resolve_revalidates_a_replaced_registered_file(self) -> None:
        asset = self.library.scan()[0]
        path = self.library.resolve(asset.id)
        path.write_bytes(b"not-an-image")

        with self.assertRaisesRegex(ValueError, "损坏或超过"):
            self.library.resolve(asset.id)


if __name__ == "__main__":
    unittest.main()
