import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from autoslice.artifact_store import (
    ARTIFACT_LAYOUT_VERSION,
    artifact_bundle_layout,
    artifact_bundle_stem,
    copy_artifact_file,
    load_artifact_json,
    markdown_relative_artifact_link,
    rewrite_organized_report_links,
    seed_artifact_from_legacy,
    write_artifact_json,
    write_artifact_text,
)


class ArtifactStoreTests(unittest.TestCase):
    def test_layout_is_stable_and_keeps_recording_segments_separate(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = artifact_bundle_layout(
                root / "直播-001.flv",
                output_dir=root / "输出",
                default_output_dir=root,
            )
            second = artifact_bundle_layout(
                root / "直播-002.flv",
                output_dir=root / "输出",
                default_output_dir=root,
            )

        self.assertEqual(first["layout_version"], ARTIFACT_LAYOUT_VERSION)
        self.assertNotEqual(first["artifact_dir"], second["artifact_dir"])
        self.assertTrue(first["artifact_dir"].endswith("直播-001_自动切片"))
        self.assertTrue(first["clip_marks_path"].endswith("数据\\clip_marks.json"))

    def test_artifact_bundle_stem_removes_windows_invalid_characters(self):
        self.assertEqual(
            artifact_bundle_stem("测<试>:?*.flv"),
            "测试",
        )

    def test_atomic_text_json_copy_and_legacy_seed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            text_path = root / "整理包" / "报告.md"
            json_path = root / "整理包" / "数据" / "结果.json"
            legacy_path = root / "旧检查点.json"
            copied_path = root / "整理包" / "数据" / "旧检查点.json"
            seeded_path = root / "整理包" / "数据" / "规范检查点.json"

            write_artifact_text(text_path, "第一版\n")
            first_mtime = text_path.stat().st_mtime_ns
            write_artifact_text(text_path, "第一版\n")
            self.assertEqual(text_path.stat().st_mtime_ns, first_mtime)
            write_artifact_text(text_path, "第二版\n")
            write_artifact_json(json_path, {"标题": "测试"})
            legacy_path.write_text('{"ok": true}\n', encoding="utf-8")
            copied = copy_artifact_file(legacy_path, copied_path)
            seeded = seed_artifact_from_legacy(seeded_path, legacy_path)

            self.assertEqual(text_path.read_text(encoding="utf-8"), "第二版\n")
            self.assertEqual(load_artifact_json(json_path), {"标题": "测试"})
            self.assertTrue(Path(copied).samefile(copied_path))
            self.assertTrue(Path(seeded).samefile(seeded_path))
            self.assertEqual(
                json.loads(copied_path.read_text(encoding="utf-8")),
                {"ok": True},
            )
            self.assertEqual(
                list(root.rglob("*.tmp-*")),
                [],
            )

    def test_report_links_are_rewritten_as_relative_paths(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_dir = root / "录播_自动切片"
            data_dir = artifact_dir / "数据"
            queue_dir = root / "_总清单"
            report_path = artifact_dir / "01_话题分析.md"
            corrected_srt = data_dir / "校对字幕.srt"
            queue_path = queue_dir / "精调任务总清单.md"
            timeline_path = artifact_dir / "03_优化时间轴.md"
            for path in (corrected_srt, queue_path, timeline_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("data", encoding="utf-8")
            report_path.write_text(
                "> 剪映校对字幕: 旧校对字幕.srt\n"
                "> 精调总清单: 旧总清单.md\n"
                "> 字幕优化时间轴: 旧时间轴.md\n"
                "\n正文保持不变\n",
                encoding="utf-8",
            )
            layout = {
                "artifact_dir": str(artifact_dir),
                "report_path": str(report_path),
                "corrected_srt_path": str(corrected_srt),
                "unified_queue_md_path": str(queue_path),
                "optimized_timeline_md_path": str(timeline_path),
            }

            rewrite_organized_report_links(layout)
            rewritten = report_path.read_text(encoding="utf-8")

        self.assertIn("[校对字幕.srt](./数据/校对字幕.srt)", rewritten)
        self.assertIn("[精调任务总清单.md](../_总清单/精调任务总清单.md)", rewritten)
        self.assertIn("[03_优化时间轴.md](./03_优化时间轴.md)", rewritten)
        self.assertIn("正文保持不变", rewritten)
        with TemporaryDirectory() as temporary:
            base_dir = Path(temporary) / "整理包"
            target_path = base_dir / "数据" / "文件.json"
            self.assertEqual(
                markdown_relative_artifact_link(target_path, base_dir),
                "[文件.json](./数据/文件.json)",
            )


if __name__ == "__main__":
    unittest.main()
