"""AutoCover Phase 1：文档模型、版本化载荷和旧草稿迁移。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoslice.desktop.cover_migration import document_from_basic_title_values, document_from_payload
from autoslice.desktop.cover_model import (
    AssetRef,
    BackgroundObject,
    CoverDocument,
    ImageObject,
    ShapeObject,
    StickerObject,
    TextObject,
    TextStyle,
    Transform,
)
from autoslice.desktop.cover_service import CoverService
from autoslice.desktop.foundation import DesktopStorage
from autoslice.desktop.projects import ProjectVideo, SubmissionProject


class CoverDocumentModelTests(unittest.TestCase):
    def test_basic_copy_is_persisted_as_independent_a_and_b_objects(self):
        document = document_from_basic_title_values(
            title="音姐今天要给沐霂点男模 / 哎呀我还没见过男模啥样呢",
            image_path="frame.jpg",
            selected_timestamp=0.0,
            background_x=0.5,
            background_y=0.5,
            background_scale=1.0,
        )
        texts = [item for item in document.objects if isinstance(item, TextObject)]
        self.assertEqual([item.copy_role for item in texts], ["B", "A"])
        self.assertEqual({item.id for item in texts}, {"copy-a", "copy-b"})
        self.assertNotIn("/", "".join(item.text for item in texts))
        restored = CoverDocument.from_payload(document.to_payload())
        self.assertEqual(
            [(item.id, item.copy_role, item.text) for item in restored.objects if isinstance(item, TextObject)],
            [(item.id, item.copy_role, item.text) for item in texts],
        )

    def test_all_renderable_object_kinds_roundtrip_without_selection_state(self):
        document = CoverDocument(
            objects=(
                BackgroundObject(
                    id="background-main",
                    asset=AssetRef(path="frame.jpg"),
                    transform=Transform(rotation=4.0),
                    pan_x=0.21,
                    pan_y=0.77,
                    scale=1.6,
                ),
                TextObject(
                    id="title-main",
                    text="倾斜标题",
                    transform=Transform(x=0.2, y=0.3, rotation=-7.5),
                ),
                ImageObject(
                    id="host-cutout",
                    asset=AssetRef(asset_id="host-1"),
                    transform=Transform(x=0.72, y=0.52, scale=0.62, rotation=-6.0),
                ),
                StickerObject(
                    id="reaction-wow",
                    asset=AssetRef(asset_id="sticker-1"),
                    transform=Transform(rotation=12.0),
                    category="reaction",
                ),
                ShapeObject(
                    id="focus-ring",
                    transform=Transform(x=0.56, y=0.46, scale=0.3),
                    shape_type="circle",
                ),
            ),
            selected_object_id="title-main",
        )

        restored = CoverDocument.from_payload(document.to_payload())

        self.assertEqual(restored, document)
        self.assertEqual([item.kind for item in restored.objects], [
            "background", "text", "image", "sticker", "shape",
        ])
        object_payload = restored.to_payload()["objects"]
        self.assertTrue(all("selected" not in item for item in object_payload))
        self.assertEqual(object_payload[1]["transform"]["rotation"], -7.5)

    def test_profile_overrides_roundtrip_without_changing_base_objects(self):
        document = CoverDocument(
            objects=(TextObject(id="title-main", text="标题", transform=Transform(x=0.2, y=0.2)),),
            profiles={
                "4x3": CoverDocument().profiles["4x3"],
                "16x9": CoverDocument().profiles["16x9"].__class__(
                    "16x9", 1920, 1080,
                    overrides={"title-main": {"transform": {"x": 0.7, "y": 0.4}}},
                    export_suffix="-16x9",
                ),
            },
        )
        restored = CoverDocument.from_payload(document.to_payload())
        self.assertEqual(restored.profiles["16x9"].overrides["title-main"]["transform"]["x"], 0.7)
        self.assertEqual(restored.objects[0].transform.x, 0.2)


class CoverMigrationTests(unittest.TestCase):
    def test_text_style_has_formal_defaults_and_serializes_explicit_fields(self):
        style = TextObject.from_payload({"id": "title-main", "text": "默认"}).style
        self.assertEqual(style.fill_color, "#FFE438")
        self.assertEqual(style.stroke_color, "#111111")
        self.assertEqual(style.font_weight, 900)
        payload = style.to_payload()
        for key in ("font_family", "font_weight", "fill_color", "stroke_color", "stroke_width", "shadow", "line_spacing"):
            self.assertIn(key, payload)

    def test_legacy_migration_does_not_fall_back_to_qt_black_text(self):
        document, _migrated = document_from_payload(
            {"version": 3, "title": "旧标题", "font_size": 96},
            "回退标题",
        )
        text = next(item for item in document.objects if isinstance(item, TextObject))
        self.assertEqual(text.style.fill_color, "#FFE438")
        self.assertEqual(text.style.stroke_color, "#111111")
        self.assertTrue(text.style.shadow)

    def test_legacy_versions_migrate_to_background_and_text(self):
        for version in (1, 2, 3):
            document, migrated = document_from_payload(
                {
                    "version": version,
                    "title": "旧标题",
                    "image_path": "frame.jpg",
                    "selected_timestamp": 2.5,
                    "text_x": 0.21,
                    "text_y": 0.37,
                    "font_size": 88,
                    "background_x": 0.18,
                    "background_y": 0.82,
                    "background_scale": 1.35,
                },
                "回退标题",
            )
            self.assertTrue(migrated)
            self.assertEqual({item.kind for item in document.objects}, {"background", "text"})
            background = next(item for item in document.objects if item.kind == "background")
            text = next(item for item in document.objects if item.kind == "text")
            self.assertEqual(background.asset.path, "frame.jpg")
            self.assertAlmostEqual(background.pan_x, 0.18)
            self.assertAlmostEqual(background.pan_y, 0.82)
            self.assertAlmostEqual(background.scale, 1.35)
            self.assertAlmostEqual(text.transform.x, 0.21)
            self.assertAlmostEqual(text.transform.y, 0.37)
            self.assertEqual(text.style.font_size, 88)

    def test_old_v4_style_defaults_are_canonicalized_once_and_keep_size_rect_position(self):
        payload = {
            "version": 4,
            "source": {"selected_timestamp": 3.5},
            "profiles": {
                "4x3": {
                    "key": "4x3", "width": 1440, "height": 1080,
                    "overrides": {
                        "title-main": {
                            "transform": {"x": 0.71, "y": 0.23},
                            "rect": {"x": 0.0, "y": 0.0, "width": 0.84, "height": 0.12},
                            "style": {"font_family": "", "font_size": 107, "fill": "#FFFFFF", "stroke_width": 22, "shadow": True},
                        },
                    },
                },
                "16x9": {"key": "16x9", "width": 1920, "height": 1080, "overrides": {}},
            },
            "objects": [{
                "id": "title-main", "kind": "text", "transform": {"x": 0.12, "y": 0.34},
                "text": "保留字号和位置", "rect": {"x": 0, "y": 0, "width": 0.84, "height": 0.12},
                "wrap": {"max_width": 0.86, "mode": "wrap", "max_lines": 8},
                "style": {"font_family": "", "font_size": 104, "fill": "#FFFFFF", "stroke": "#111111", "stroke_width": 22, "shadow": False},
            }],
            "active_profile": "4x3",
        }
        document, migrated = document_from_payload(payload, "回退")
        self.assertTrue(migrated)
        self.assertEqual(document.style_revision, 1)
        text = next(item for item in document.objects if isinstance(item, TextObject))
        self.assertEqual(text.style.font_size, 104)
        self.assertAlmostEqual(text.transform.x, 0.12)
        self.assertAlmostEqual(text.transform.y, 0.34)
        self.assertEqual(text.style.fill_color, "#FFE438")
        self.assertEqual(text.style.stroke_color, "#111111")
        self.assertEqual(text.style.stroke_width, 6)
        self.assertTrue(text.style.shadow)
        override = document.profiles["4x3"].overrides["title-main"]
        # 固定字号升级写回旧版实际显示的字号：0.12 高的框里 107 号会被缩小一点。
        self.assertLessEqual(override["style"]["font_size"], 107)
        self.assertGreaterEqual(override["style"]["font_size"], 100)
        self.assertEqual(override["style"]["fill_color"], "#FFE438")
        self.assertEqual(override["style"]["stroke_width"], 6)
        self.assertAlmostEqual(override["transform"]["x"], 0.71)

        current, migrated_again = document_from_payload(document.to_payload(), "回退")
        self.assertFalse(migrated_again)
        self.assertEqual(current, document)

    def test_service_saves_legacy_draft_as_v4_and_restores_same_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "项目"
            project_dir.mkdir()
            video_path = project_dir / "视频.mp4"
            video_path.write_bytes(b"video")
            project = SubmissionProject(
                "project-1", "项目", str(project_dir),
                (ProjectVideo("视频.mp4", str(video_path), "", "", False, False, ""),),
            )
            video = project.videos[0]
            storage = DesktopStorage(root / "data")
            service = CoverService(storage)
            legacy = {
                "version": 2,
                "title": "旧草稿",
                "image_path": None,
                "selected_timestamp": 1.25,
                "text_x": 0.24,
                "text_y": 0.31,
                "font_size": 96,
                "background_x": 0.4,
                "background_y": 0.6,
                "background_scale": 1.2,
            }
            storage.save_draft("cover", project.directory, video.path, legacy)

            document, read = service.load_document(project, video)
            self.assertEqual(read.status, "ready")
            self.assertEqual(document.version, 4)
            envelope = json.loads(storage.draft_path("cover", project.directory, video.path).read_text(encoding="utf-8"))
            self.assertEqual(envelope["payload"]["version"], 4)

            draft, _ = service.load(project, video)
            self.assertEqual(draft.title, "旧草稿")
            self.assertAlmostEqual(draft.selected_timestamp, 1.25)
            self.assertAlmostEqual(draft.text_x, 0.24)
            self.assertAlmostEqual(draft.text_y, 0.31)
            self.assertEqual(draft.font_size, 96)
            self.assertAlmostEqual(draft.background_x, 0.4)
            self.assertAlmostEqual(draft.background_y, 0.6)
            self.assertAlmostEqual(draft.background_scale, 1.2)

    def test_service_loads_old_v4_style_and_persists_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "项目"
            project_dir.mkdir()
            video_path = project_dir / "视频.mp4"
            video_path.write_bytes(b"video")
            project = SubmissionProject("project-1", "项目", str(project_dir),
                (ProjectVideo("视频.mp4", str(video_path), "", "", False, False, ""),))
            video = project.videos[0]
            storage = DesktopStorage(root / "data")
            service = CoverService(storage)
            document = CoverDocument(objects=(TextObject(id="title-main", text="旧 v4", style=TextStyle(
                font_family="", font_size=107, fill="#FFFFFF", stroke="#111111", stroke_width=22,
                shadow=True, line_spacing=1.12),),))
            payload = document.to_payload()
            payload.pop("style_revision", None)
            storage.save_draft("cover", project.directory, video.path, payload)
            loaded, _ = service.load_document(project, video)
            self.assertEqual(loaded.style_revision, 1)
            loaded_text = next(item for item in loaded.objects if isinstance(item, TextObject))
            self.assertEqual(loaded_text.style.fill_color, "#FFE438")
            saved = json.loads(storage.draft_path("cover", project.directory, video.path).read_text(encoding="utf-8"))
            self.assertEqual(saved["payload"]["style_revision"], 1)
            reopened, _ = service.load_document(project, video)
            self.assertEqual(reopened, loaded)


if __name__ == "__main__":
    unittest.main()
