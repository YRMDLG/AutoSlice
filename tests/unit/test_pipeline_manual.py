import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY

from autoslice.pipeline_manual import prepare_pipeline_manual_timeline


class PipelineManualTests(unittest.TestCase):
    def test_explicit_optimized_artifact_copies_json_and_markdown_before_loading(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            selected_json = root / "已选优化时间轴.json"
            selected_md = root / "已选优化时间轴.md"
            layout = {
                "optimized_timeline_json_path": str(root / "bundle/data/optimized.json"),
                "optimized_timeline_md_path": str(root / "bundle/optimized.md"),
            }
            calls = []
            manual_timeline = {
                "path": str(root / "人工记录.docx"),
                "raw_entry_count": 3,
                "entries": [{"start": 10}, {"start": 40}],
                "optimization_warning": "复用产物警告",
            }

            def copy(source_path, destination_path):
                calls.append(("copy", source_path, destination_path))

            def load(path, video_path, manual_timeline_path=None):
                calls.append(("load", path, video_path, manual_timeline_path))
                return manual_timeline

            result = prepare_pipeline_manual_timeline(
                str(root / "录播.flv"),
                str(root / "录播"),
                [(0, 20, "字幕")],
                [(10, 50)],
                300,
                str(root / "人工记录.docx"),
                str(selected_json),
                "测试主播",
                layout,
                copy_artifact_file=copy,
                load_optimized_timeline_artifact=load,
                prepare_optimized_manual_timeline=lambda *_args, **_kwargs: self.fail(
                    "显式产物分支不应现场优化"
                ),
            )

        self.assertEqual(
            calls,
            [
                (
                    "copy",
                    os.path.abspath(selected_json),
                    layout["optimized_timeline_json_path"],
                ),
                (
                    "copy",
                    os.path.abspath(selected_md),
                    layout["optimized_timeline_md_path"],
                ),
                (
                    "load",
                    layout["optimized_timeline_json_path"],
                    str(root / "录播.flv"),
                    str(root / "人工记录.docx"),
                ),
            ],
        )
        self.assertIs(result["manual_timeline"], manual_timeline)
        self.assertEqual(result["raw_manual_entry_count"], 3)
        self.assertIs(result["manual_entries"], manual_timeline["entries"])
        self.assertEqual(result["optimization_warning"], "复用产物警告")

    def test_explicit_artifact_treats_none_and_disabled_manual_path_as_absent(self):
        for manual_timeline_path in (None, "__none__"):
            with self.subTest(manual_timeline_path=manual_timeline_path):
                loaded_paths = []

                def load(_path, _video_path, manual_timeline_path=None):
                    loaded_paths.append(manual_timeline_path)
                    return {"path": None, "entries": [], "raw_entry_count": 0}

                prepare_pipeline_manual_timeline(
                    "recording.flv",
                    "recording",
                    [],
                    [],
                    None,
                    manual_timeline_path,
                    "selected.json",
                    "测试主播",
                    {
                        "optimized_timeline_json_path": "bundle/optimized.json",
                        "optimized_timeline_md_path": "bundle/optimized.md",
                    },
                    copy_artifact_file=lambda *_args: None,
                    load_optimized_timeline_artifact=load,
                    prepare_optimized_manual_timeline=lambda *_args, **_kwargs: self.fail(
                        "显式产物分支不应现场优化"
                    ),
                )

                self.assertEqual(loaded_paths, [None])

    def test_live_optimization_preserves_parameters_count_warning_and_progress(self):
        layout = {
            "optimized_timeline_json_path": "bundle/optimized.json",
            "optimized_timeline_md_path": "bundle/optimized.md",
        }
        segments = [(0, 30, "字幕")]
        peaks = [(15, 80)]
        progress = []
        calls = []
        entries = [{"start": 10}, {"start": 50}]
        manual_timeline = {
            "path": "timelines/人工记录.docx",
            "raw_entry_count": "4",
            "entries": entries,
            "optimization_warning": "现场优化警告",
        }

        def prepare(*args, **kwargs):
            calls.append((args, kwargs))
            return manual_timeline

        result = prepare_pipeline_manual_timeline(
            "recording.flv",
            "recording",
            segments,
            peaks,
            600,
            "timelines/人工记录.docx",
            None,
            "测试主播",
            layout,
            progress_callback=lambda *args: progress.append(args),
            copy_artifact_file=lambda *_args: self.fail("现场优化不应复制显式产物"),
            load_optimized_timeline_artifact=lambda *_args, **_kwargs: self.fail(
                "现场优化不应加载显式产物"
            ),
            prepare_optimized_manual_timeline=prepare,
        )

        self.assertEqual(
            calls,
            [
                (
                    (
                        "recording.flv",
                        "recording",
                        segments,
                        peaks,
                        600,
                        "timelines/人工记录.docx",
                    ),
                    {
                        "streamer_name": "测试主播",
                        "progress_callback": ANY,
                        "retry_incomplete_artifact": False,
                        "artifact_layout": layout,
                    },
                )
            ],
        )
        self.assertEqual(result["raw_manual_entry_count"], 4)
        self.assertIs(result["manual_entries"], entries)
        self.assertEqual(result["optimization_warning"], "现场优化警告")
        self.assertEqual(
            progress,
            [
                (
                    "已加载人工时间轴: 人工记录.docx，原始 4 条 → 字幕优化 2 个候选",
                    24,
                    100,
                )
            ],
        )

    def test_no_entries_does_not_emit_loaded_summary(self):
        progress = []

        result = prepare_pipeline_manual_timeline(
            "recording.flv",
            "recording",
            [],
            [],
            None,
            "__none__",
            None,
            "测试主播",
            {},
            progress_callback=lambda *args: progress.append(args),
            copy_artifact_file=lambda *_args: None,
            load_optimized_timeline_artifact=lambda *_args, **_kwargs: {},
            prepare_optimized_manual_timeline=lambda *_args, **_kwargs: {
                "path": None,
                "raw_entry_count": 0,
                "entries": [],
                "optimization_warning": "无候选警告",
            },
        )

        self.assertEqual(progress, [])
        self.assertEqual(result["manual_entries"], [])
        self.assertEqual(result["optimization_warning"], "无候选警告")


if __name__ == "__main__":
    unittest.main()
