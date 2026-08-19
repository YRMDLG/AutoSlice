import ast
import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from autoslice import pipeline, timecode, topic_engine
from autoslice.analysis.manual import artifacts, timebase
from autoslice.streamer_profiles import streamer_profile_context


class ManualArtifactTests(unittest.TestCase):
    def test_migrated_function_sources_match_the_verified_pipeline_baseline(self):
        expected_hashes = {
            "optimized_timeline_paths": (
                "adb300f27bfc50f9ed97c4ef87907fc7a0aa712c7c2c8c21c3e32dfb1a1459cf"
            ),
            "write_optimized_timeline_files": (
                "527a8962f143eda0f714171500daedff995cb936b66611a04ac0e7e2401e2827"
            ),
            "load_optimized_timeline_artifact": (
                "4d07d5b4766b2dd573551e431a578ec024ccc059a07211e1095195789a52df43"
            ),
        }
        source = Path(artifacts.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in expected_hashes
        }
        self.assertEqual(set(functions), set(expected_hashes))
        self.assertEqual(
            {
                name: hashlib.sha256(
                    ast.get_source_segment(source, functions[name]).encode("utf-8")
                ).hexdigest()
                for name in expected_hashes
            },
            expected_hashes,
        )

    def test_artifacts_are_the_only_owner_and_compatibility_aliases_share_identity(self):
        aliases = {
            "optimized_timeline_paths": "_optimized_timeline_paths",
            "write_optimized_timeline_files": "_write_optimized_timeline_files",
            "load_optimized_timeline_artifact": "_load_optimized_timeline_artifact",
        }
        self.assertEqual(
            artifacts.FACADE_EXPORTS,
            {
                topic_engine_name: owner_name
                for owner_name, topic_engine_name in aliases.items()
            },
        )
        for owner_name, topic_engine_name in aliases.items():
            with self.subTest(owner=owner_name):
                owner = getattr(artifacts, owner_name)
                self.assertEqual(owner.__module__, artifacts.__name__)
                self.assertIs(getattr(pipeline, owner_name), owner)
                self.assertIs(getattr(topic_engine, topic_engine_name), owner)

        pipeline_tree = ast.parse(Path(pipeline.__file__).read_text(encoding="utf-8"))
        pipeline_functions = {
            node.name
            for node in pipeline_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(set(aliases).isdisjoint(pipeline_functions))
        self.assertIn("prepare_optimized_manual_timeline", pipeline_functions)

        prepare = next(
            node
            for node in pipeline_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "prepare_optimized_manual_timeline"
        )
        global_calls = {
            node.func.id
            for node in ast.walk(prepare)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertTrue(set(aliases).issubset(global_calls))

    def test_owner_imports_only_approved_dependencies_without_reverse_edges(self):
        tree = ast.parse(Path(artifacts.__file__).read_text(encoding="utf-8"))
        dependencies = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
                if node.module in {"autoslice", "autoslice.analysis.manual"}:
                    dependencies.update(
                        f"{node.module}.{alias.name}" for alias in node.names
                    )
                else:
                    dependencies.add(node.module)

        self.assertEqual(
            dependencies,
            {
                "json",
                "os",
                "pathlib",
                "autoslice.timecode",
                "autoslice.analysis.manual.candidates",
                "autoslice.analysis.manual.timebase",
                "autoslice.streamer_profiles",
            },
        )
        self.assertNotIn("autoslice.pipeline", dependencies)
        self.assertNotIn("autoslice.topic_engine", dependencies)

    def test_paths_use_legacy_names_or_artifact_layout(self):
        base = str(Path("录播") / "测试场")
        self.assertEqual(
            artifacts.optimized_timeline_paths(base),
            (base + "_优化时间轴.json", base + "_优化时间轴.md"),
        )
        layout = {
            "optimized_timeline_json_path": "bundle/data/optimized.json",
            "optimized_timeline_md_path": "bundle/optimized.md",
        }
        self.assertEqual(
            artifacts.optimized_timeline_paths(base, artifact_layout=layout),
            (layout["optimized_timeline_json_path"], layout["optimized_timeline_md_path"]),
        )

    def test_write_preserves_json_markdown_profile_version_and_warning_contract(self):
        raw_entries = [
            {"start": 70, "text": "第一条"},
            {"start": 180, "text": "第二条"},
        ]
        optimized_entries = [
            {
                "start": 65,
                "end": 125,
                "text": "字幕校准话题",
                "stars": 6,
                "ai_enriched": True,
                "summary": ["已核对字幕和弹幕"],
                "original_entries": [
                    {
                        "original_start": 70,
                        "start": 65,
                        "alignment_shift_sec": -5,
                    }
                ],
            },
            {
                "start": 180,
                "end": 220,
                "text": "待复核话题",
                "stars": 0,
                "ai_enriched": False,
            },
        ]

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "输入" / "主播-2026年8月15日-20点01分06秒.flv"
            layout = {
                "optimized_timeline_json_path": str(root / "bundle" / "data" / "optimized.json"),
                "optimized_timeline_md_path": str(root / "bundle" / "optimized.md"),
            }
            with streamer_profile_context("generic") as profile:
                json_path, md_path = artifacts.write_optimized_timeline_files(
                    str(root / "legacy" / "video"),
                    "人工时间轴.docx",
                    raw_entries,
                    optimized_entries,
                    warning="存在低权重候选",
                    artifact_layout=layout,
                    video_path=str(video_path),
                )
            payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
            markdown = Path(md_path).read_text(encoding="utf-8")

        self.assertEqual((json_path, md_path), tuple(layout.values()))
        self.assertEqual(payload["video_path"], str(video_path.resolve()))
        self.assertEqual(payload["source_path"], "人工时间轴.docx")
        self.assertEqual(payload["streamer_profile_id"], profile.id)
        self.assertEqual(
            payload["optimization_version"],
            timebase.MANUAL_TIMELINE_OPTIMIZATION_VERSION,
        )
        self.assertEqual(payload["raw_entry_count"], 2)
        self.assertEqual(payload["optimized_entry_count"], 2)
        self.assertEqual(payload["warning"], "存在低权重候选")
        self.assertEqual(payload["entries"], optimized_entries)
        self.assertIn("# 字幕校准后的人工时间轴", markdown)
        self.assertIn("> 原始文件: 人工时间轴.docx", markdown)
        self.assertIn("> 原始 2 条 → 优化 2 个话题候选", markdown)
        self.assertIn("> 警告: 存在低权重候选", markdown)
        self.assertIn("字幕校准话题" + " ⭐" * 5, markdown)
        self.assertIn("- 状态: 字幕/AI初审（完整分析时再次独立复核）", markdown)
        self.assertIn("- 状态: 低权重参考", markdown)
        self.assertIn(
            f"- 字幕校时: {timecode.format_elapsed(70)}→"
            f"{timecode.format_elapsed(65)} (-5秒)",
            markdown,
        )
        self.assertIn("- 已核对字幕和弹幕", markdown)
        self.assertTrue(markdown.endswith("\n"))

    def test_load_validates_video_and_manual_timeline_binding(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "主播-2026年8月15日-20点01分06秒.flv"
            source_path = root / "20260815.docx"
            artifact_path = root / "优化时间轴.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "video_path": str(video_path.resolve()),
                        "source_path": str(source_path),
                        "streamer_profile_id": "generic",
                        "optimization_version": 3,
                        "raw_entry_count": 2,
                        "warning": None,
                        "entries": [
                            {"start": 10, "end": 80, "text": "闹钟故事"}
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            loaded = artifacts.load_optimized_timeline_artifact(
                str(artifact_path),
                str(video_path.resolve()),
                str(source_path),
            )
            with self.assertRaisesRegex(ValueError, "不属于当前选择的录播"):
                artifacts.load_optimized_timeline_artifact(
                    str(artifact_path),
                    str((root / "另一场.flv").resolve()),
                    str(source_path),
                )
            with self.assertRaisesRegex(ValueError, "人工 DOCX 不一致"):
                artifacts.load_optimized_timeline_artifact(
                    str(artifact_path),
                    str(video_path.resolve()),
                    str((root / "另一天.docx").resolve()),
                )

        self.assertEqual(loaded["path"], str(source_path))
        self.assertEqual(loaded["source_entry_count"], 2)
        self.assertEqual(loaded["raw_entry_count"], 2)
        self.assertEqual(loaded["optimized_entry_count"], 1)
        self.assertEqual(loaded["optimized_json_path"], str(artifact_path))
        self.assertEqual(
            loaded["optimized_md_path"],
            str(artifact_path.with_suffix(".md")),
        )
        self.assertIsNone(loaded["optimization_warning"])
        self.assertEqual(loaded["optimization_version"], 3)
        self.assertEqual(loaded["streamer_profile_id"], "generic")
        self.assertEqual(loaded["mode"], "optimized_artifact")
        self.assertEqual(loaded["video_start"], datetime(2026, 8, 15, 20, 1, 6))

    def test_load_filters_ungrounded_entries_and_combines_warning(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video_path = root / "完整版.flv"
            artifact_path = root / "完整版_优化时间轴.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "video_path": str(video_path),
                        "source_path": None,
                        "streamer_profile_id": "profile-x",
                        "optimization_version": 9,
                        "raw_entry_count": 5,
                        "warning": "已有警告",
                        "entries": [
                            {
                                "start": 120,
                                "end": 180,
                                "text": "草莓蛋糕烤糊后烤箱冒烟",
                                "summary": ["草莓蛋糕烤糊了"],
                                "stars": 3,
                                "evidence": [
                                    "·弹幕依据：附近出现峰值",
                                    "●人工时间轴⭐⭐⭐：0:02:20 男生仿妆小鞠",
                                ],
                                "original_entries": [
                                    {"start": 110, "text": "草莓蛋糕烤糊", "stars": 0},
                                    {"start": 140, "text": "男生仿妆小鞠", "stars": 3},
                                ],
                            },
                            {
                                "start": 300,
                                "end": 360,
                                "text": "价格变成9999",
                                "summary": ["商家修改价格"],
                                "original_entries": [
                                    {"start": 300, "text": "手机没电了", "stars": 0}
                                ],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            loaded = artifacts.load_optimized_timeline_artifact(
                str(artifact_path), str(video_path)
            )

        self.assertEqual(loaded["optimized_entry_count"], 1)
        self.assertEqual(loaded["optimization_version"], 9)
        self.assertEqual(loaded["streamer_profile_id"], "profile-x")
        self.assertEqual(
            loaded["optimization_warning"],
            "已有警告；已忽略 1 个与原人工记录语义不符的优化候选",
        )
        entry = loaded["entries"][0]
        self.assertEqual(
            [item["text"] for item in entry["original_entries"]],
            ["草莓蛋糕烤糊"],
        )
        self.assertEqual(entry["stars"], 0)
        self.assertFalse(entry["highlight"])
        evidence = "\n".join(entry["evidence"])
        self.assertIn("·弹幕依据：附近出现峰值", evidence)
        self.assertIn("·时间轴：", evidence)
        self.assertNotIn("男生仿妆", evidence)

    def test_load_reports_missing_invalid_or_incomplete_json(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing_path = root / "missing.json"
            with self.assertRaisesRegex(FileNotFoundError, "优化时间轴文件不存在"):
                artifacts.load_optimized_timeline_artifact(
                    str(missing_path), str(root / "video.flv")
                )

            invalid_path = root / "invalid.json"
            invalid_path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "优化时间轴 JSON 无法读取"):
                artifacts.load_optimized_timeline_artifact(
                    str(invalid_path), str(root / "video.flv")
                )

            incomplete_path = root / "incomplete.json"
            incomplete_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "缺少 entries 数组"):
                artifacts.load_optimized_timeline_artifact(
                    str(incomplete_path), str(root / "video.flv")
                )


if __name__ == "__main__":
    unittest.main()
