"""素材索引兼容旧导入，选择器能搜索全部条目，不受前 120 张限制。"""

from __future__ import annotations

import importlib.util
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from autoslice.desktop.cover_assets import CoverAsset, CoverAssetLibrary

try:
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog
except ImportError:  # pragma: no cover
    QApplication = None


class AssetSourcesTests(unittest.TestCase):
    def test_legacy_and_both_import_folders_are_searchable_without_copying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = CoverAssetLibrary(root / "data", legacy_root=root / "stickers", legacy_import_root=root / "web-imports")
            sources = (
                root / "stickers" / "主播表情包" / "旧素材.png",
                root / "web-imports" / "分类" / "网页导入.PNG",
                library.import_root / "分类" / "桌面导入.webp",
            )
            for path in sources:
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (16, 16), "red").save(path)
            assets = library.scan()
            self.assertEqual(len(assets), 3)
            self.assertEqual({Path(asset.path) for asset in assets}, {path.resolve() for path in sources})
            self.assertEqual({asset.group for asset in assets}, {"stickers/主播表情包", "旧版导入", "我的导入"})
            remembered = next(asset for asset in assets if asset.name == "网页导入")
            library.mark_used(remembered.asset_id)
            # 再次扫描既保留记忆，也发现外部新放入的素材。
            extra = root / "web-imports" / "新增.png"
            Image.new("RGB", (16, 16), "blue").save(extra)
            self.assertEqual(len(library.scan()), 4)
            self.assertEqual(library.get(remembered.asset_id).usage_count, 1)
            self.assertEqual(len(list(library.import_root.rglob("*.*"))), 1)

    def test_overlapping_sources_do_not_duplicate_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "same"
            source.mkdir()
            Image.new("RGB", (16, 16), "red").save(source / "一张.png")
            library = CoverAssetLibrary(root / "data", legacy_root=source, legacy_import_root=source)
            self.assertEqual(len(library.scan()), 1)

    def test_recent_order_survives_identical_clock_ticks_and_rescan(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "stickers"
            source.mkdir()
            for name in ("a", "b"):
                Image.new("RGB", (16, 16), "red").save(source / f"{name}.png")
            library = CoverAssetLibrary(root / "data", legacy_root=source, legacy_import_root=root / "web-imports")
            first, second = library.scan()
            with patch("autoslice.desktop.cover_assets.datetime", FixedDatetime):
                library.mark_used(first.asset_id)
                library.mark_used(second.asset_id)
            library.scan()
            self.assertEqual([asset.asset_id for asset in library.recent_assets()], [second.asset_id, first.asset_id])

    def test_saved_folder_limits_scope_and_preserves_all_nested_group_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chosen = root / "表情包"
            streamer = chosen / "泽音" / "常用"
            streamer.mkdir(parents=True)
            Image.new("RGB", (16, 16), "red").save(streamer / "可爱.png")
            other = root / "杂图"
            other.mkdir()
            Image.new("RGB", (16, 16), "blue").save(other / "截图.png")
            library = CoverAssetLibrary(root / "data", legacy_root=other, legacy_import_root=other)
            library.set_source_directory(chosen)
            assets = library.scan()
            self.assertEqual([asset.name for asset in assets], ["可爱"])
            self.assertEqual(assets[0].group, "表情包/泽音/常用")
            library.mark_used(assets[0].asset_id)
            restored = CoverAssetLibrary(root / "data", legacy_root=other, legacy_import_root=other)
            self.assertEqual(restored.legacy_root, chosen.resolve())
            self.assertIsNone(restored.legacy_import_root)
            self.assertEqual(restored.scan()[0].usage_count, 1)
            with self.assertRaisesRegex(ValueError, "存在的素材文件夹"):
                restored.set_source_directory(root / "不存在")
            self.assertEqual(restored.legacy_root, chosen.resolve())


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class AssetDialogTests(unittest.TestCase):
    def setUp(self):
        from autoslice.desktop.cover_asset_dialog import CoverAssetDialog

        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        image = Path(self.temp.name) / "thumbnail.png"
        Image.new("RGB", (64, 64), "red").save(image)
        self.assets = tuple(
            CoverAsset(str(index), f"素材{index:03}", str(image), group="OldImports/泽音/常用" if index == 149 else "常用")
            for index in range(150)
        )
        self.dialog = CoverAssetDialog(self.assets)
        self.addCleanup(self.dialog.close)

    def test_search_finds_items_after_the_old_120_item_cutoff(self):
        self.assertEqual(self.dialog.filtered_model.rowCount(), 150)
        self.dialog.search.setText("素材149")
        self.assertEqual(self.dialog.filtered_model.rowCount(), 1)
        self.assertEqual(self.dialog.selected_asset(), self.assets[149])
        self.assertIn("150", self.dialog.count_label.text())
        self.assertIn("当前显示 1 张", self.dialog.count_label.text())

    def test_group_search_is_case_insensitive_and_empty_results_cannot_insert(self):
        self.dialog.search.setText("  oldimports  ")
        self.assertEqual(self.dialog.selected_asset(), self.assets[149])
        self.dialog.search.setText("泽音")
        self.assertEqual(self.dialog.selected_asset(), self.assets[149])
        self.dialog.search.setText("没有的素材")
        self.assertEqual(self.dialog.filtered_model.rowCount(), 0)
        self.assertIsNone(self.dialog.selected_asset())
        button = self.dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.assertFalse(button.isEnabled())
        self.dialog.search.clear()
        self.assertEqual(self.dialog.filtered_model.rowCount(), 150)
        self.assertTrue(button.isEnabled())

    def test_folder_row_shows_the_source_and_change_button_returns_a_distinct_code(self):
        from autoslice.desktop.cover_asset_dialog import CoverAssetDialog

        self.assertTrue(self.dialog.folder_label.isHidden())  # 没给素材文件夹就不显示这一行
        dialog = CoverAssetDialog(self.assets, source_directory="D:/素材/泽音")
        self.addCleanup(dialog.close)
        dialog.show()
        self.assertIn("D:/素材/泽音", dialog.folder_label.text())
        self.assertTrue(dialog.folder_label.isVisible() and dialog.change_folder.isVisible())
        dialog.change_folder.click()
        self.assertEqual(dialog.result(), CoverAssetDialog.CHANGE_FOLDER)
        self.assertNotIn(CoverAssetDialog.CHANGE_FOLDER, (0, 1))  # 不能和取消、插入撞码

    def test_empty_library_says_what_to_do_next(self):
        from autoslice.desktop.cover_asset_dialog import CoverAssetDialog

        dialog = CoverAssetDialog((), source_directory="D:/空")
        self.addCleanup(dialog.close)
        self.assertIn("更换文件夹", dialog.count_label.text())
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled())

    def make_dialog(self, *rows):
        from autoslice.desktop.cover_asset_dialog import CoverAssetDialog

        image = Path(self.temp.name) / "thumbnail.png"
        assets = tuple(CoverAsset(str(index), name, str(image), group=group) for index, (name, group) in enumerate(rows))
        dialog = CoverAssetDialog(assets)
        self.addCleanup(dialog.close)
        return dialog

    def matched(self, dialog, text):
        dialog.search.setText(text)
        model = dialog.filtered_model
        return sorted(model.mapToSource(model.index(row, 0)).row() for row in range(model.rowCount()))

    def test_several_keywords_must_all_match_and_blank_runs_are_ignored(self):
        dialog = self.make_dialog(("可爱", "素材/泽音/表情"), ("震惊", "素材/泽音/表情"), ("可爱", "素材/别人/表情"))
        for text, expected in (
            ("可爱 泽音", [0]), ("可爱　泽音", [0]), ("  泽音   表情  ", [0, 1]), ("可爱 没有", []), ("", [0, 1, 2]),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.matched(dialog, text), expected)

    @unittest.skipUnless(importlib.util.find_spec("pypinyin"), "没装 pypinyin")
    def test_pinyin_and_initials_find_chinese_names_and_combine_with_other_keywords(self):
        dialog = self.make_dialog(("可爱", "素材/泽音/表情"), ("震惊", "素材/泽音/表情"), ("Kira", "素材/英文"))
        for text, expected in (
            ("keai", [0]), ("KEAI", [0]), ("zeyin", [0, 1]), ("zy", [0, 1]), ("keai zeyin", [0]),
            ("keai 震惊", []), ("keai wrong", []), ("kira", [2]),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.matched(dialog, text), expected)

    def test_pinyin_is_only_computed_for_ascii_keywords_and_then_cached(self):
        from autoslice.desktop import cover_asset_dialog

        dialog = self.make_dialog(("可爱", "素材/泽音"), ("震惊", "素材/泽音"))
        with patch.object(cover_asset_dialog, "pinyin_text", wraps=cover_asset_dialog.pinyin_text) as spy:
            self.matched(dialog, "泽音")
            self.assertEqual(spy.call_count, 0)
            self.matched(dialog, "zzz")
            computed = spy.call_count
            self.assertEqual(computed, 2)
            self.matched(dialog, "zzzz")
            self.assertEqual(spy.call_count, computed)

    @unittest.skipUnless(importlib.util.find_spec("pypinyin"), "没装 pypinyin")
    def test_pinyin_cache_warms_in_small_batches_before_the_first_ascii_search(self):
        from autoslice.desktop import cover_asset_dialog

        dialog = self.make_dialog(*((f"可爱{index}", "素材/泽音") for index in range(250)))
        dialog.warm_timer.stop()
        with patch.object(cover_asset_dialog, "pinyin_text", wraps=cover_asset_dialog.pinyin_text) as spy:
            self.assertEqual([dialog.asset_model.warm_pinyin(100) for _ in range(3)], [True, True, False])
            self.assertEqual(spy.call_count, 250)
            self.assertEqual(self.matched(dialog, "zeyin"), list(range(250)))
            self.assertEqual(spy.call_count, 250)  # 搜索直接用预热好的缓存

    def test_idle_timer_finishes_warming_and_stops(self):
        dialog = self.make_dialog(*((f"可爱{index}", "素材/泽音") for index in range(250)))
        deadline = time.monotonic() + 5
        while dialog.warm_timer.isActive() and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertFalse(dialog.warm_timer.isActive())


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class AssetBrowseFlowTests(unittest.TestCase):
    """素材库入口：为空也打开对话框，能在里面换素材文件夹，设置页跟着同步。"""

    def setUp(self):
        from autoslice.desktop.cover import CoverEditorWidget
        from autoslice.desktop.cover_model import CoverDocument
        from autoslice.desktop.foundation import DesktopStorage

        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.empty = self.root / "空素材"
        self.empty.mkdir()
        self.widget = CoverEditorWidget(DesktopStorage(self.root / "data"))
        self.addCleanup(self.widget.deleteLater)
        self.widget.service.asset_library = CoverAssetLibrary(self.root / "library", legacy_root=self.empty)
        self.widget.document = CoverDocument(objects=())
        self.changed = []
        self.widget.material_directory_changed.connect(self.changed.append)

    def browse(self, results, chosen=()):
        opened = []
        scripted = list(results)

        class ScriptedDialog:
            CHANGE_FOLDER = 2

            def __init__(self, assets, parent=None, *, source_directory=""):
                opened.append((assets, source_directory))

            def exec(self):
                return scripted.pop(0)

            def selected_asset(self):
                return None

        with patch("autoslice.desktop.cover_editor_objects.CoverAssetDialog", ScriptedDialog), \
                patch.object(QFileDialog, "getExistingDirectory", side_effect=list(chosen)):
            self.widget._browse_assets()
        return opened

    def test_an_empty_library_still_opens_the_dialog_showing_the_current_folder(self):
        opened = self.browse([0])
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0][0], ())
        self.assertEqual(opened[0][1], str(self.empty.resolve()))

    def test_changing_the_folder_reopens_the_library_with_new_assets_and_syncs_settings(self):
        chosen = self.root / "表情包"
        chosen.mkdir()
        Image.new("RGB", (8, 8), "red").save(chosen / "可爱.png")
        opened = self.browse([2, 0], [str(chosen)])
        self.assertEqual(len(opened), 2)
        self.assertEqual(opened[1][1], str(chosen.resolve()))
        self.assertEqual([item.name for item in opened[1][0]], ["可爱"])
        self.assertEqual(self.changed, [str(chosen.resolve())])
        self.assertEqual(self.widget.service.asset_library.legacy_root, chosen.resolve())

    def test_cancelled_or_invalid_choice_keeps_the_old_folder_and_returns_to_the_library(self):
        for chosen in ("", str(self.root / "不存在")):
            with self.subTest(chosen=chosen):
                opened = self.browse([2, 0], [chosen])
                self.assertEqual([item[1] for item in opened], [str(self.empty.resolve())] * 2)
                self.assertEqual(self.changed, [])
                self.assertEqual(self.widget.service.asset_library.legacy_root, self.empty.resolve())


if __name__ == "__main__":
    unittest.main()
