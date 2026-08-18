import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.analysis.topic import analysis as topic_analysis

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src" / "autoslice"
OWNER_PATH = SRC_ROOT / "analysis" / "topic" / "analysis.py"
FACADE_PATH = SRC_ROOT / "analysis" / "topic_analysis.py"
TOPIC_INIT_PATH = SRC_ROOT / "analysis" / "topic" / "__init__.py"

TOPIC_ANALYSIS_CONSUMERS = (
    "src/autoslice/analysis/topic/chunking.py",
    "src/autoslice/analysis/candidates.py",
    "src/autoslice/analysis/manual/workflow.py",
    "src/autoslice/pipeline.py",
    "src/autoslice/reporting.py",
    "src/autoslice/topic_engine.py",
)


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _legacy_topic_analysis_imports(tree):
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(
                alias.name
                for alias in node.names
                if alias.name == "autoslice.analysis.topic_analysis"
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module == "autoslice.analysis.topic_analysis":
                imports.append(node.module)
            elif node.module == "autoslice.analysis":
                imports.extend(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name == "topic_analysis"
                )
    return imports


def _import_targets(tree):
    targets = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.update(
                f"{node.module}.{alias.name}" for alias in node.names
            )
    return targets


class TopicAnalysisOwnershipTests(unittest.TestCase):
    def test_topic_owner_has_all_nine_functions_and_facade_has_no_definitions(self):
        owner_tree = _parse(OWNER_PATH)
        facade_tree = _parse(FACADE_PATH)
        definition_types = (ast.FunctionDef, ast.AsyncFunctionDef)
        owner_functions = [
            node.name for node in owner_tree.body if isinstance(node, definition_types)
        ]

        self.assertEqual(len(owner_functions), 9)
        self.assertEqual(len(owner_functions), len(set(owner_functions)))
        self.assertIn("build_chunk_prompt", owner_functions)
        self.assertIn("parse_llm_response", owner_functions)
        self.assertIn("analyze_topic_chunks", owner_functions)
        self.assertFalse(
            any(isinstance(node, definition_types) for node in facade_tree.body)
        )
        self.assertFalse(any(isinstance(node, ast.ClassDef) for node in facade_tree.body))

    def test_legacy_facade_reexports_every_owner_object_by_identity(self):
        from autoslice.analysis import topic_analysis as compatibility
        from autoslice.analysis.topic import analysis as owner

        self.assertIs(compatibility.FACADE_EXPORTS, owner.FACADE_EXPORTS)
        for name, value in vars(owner).items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertIs(getattr(compatibility, name), value)

    def test_production_consumers_import_topic_owner_directly(self):
        for relative_path in TOPIC_ANALYSIS_CONSUMERS:
            path = ROOT / relative_path
            tree = _parse(path)
            direct_import = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "autoslice.analysis.topic"
                and any(alias.name == "analysis" for alias in node.names)
                for node in ast.walk(tree)
            )
            with self.subTest(path=relative_path):
                self.assertTrue(direct_import)
                self.assertEqual(_legacy_topic_analysis_imports(tree), [])

        violations = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            imports = _legacy_topic_analysis_imports(_parse(path))
            if imports:
                violations.append((path.relative_to(ROOT).as_posix(), imports))
        self.assertEqual(violations, [])

    def test_topic_package_is_lazy_and_owner_has_no_reverse_dependency(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import sys; import autoslice.analysis.topic; "
                    "assert 'autoslice.analysis.topic.analysis' not in sys.modules"
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

        topic_init_tree = _parse(TOPIC_INIT_PATH)
        all_assignment = next(
            node
            for node in topic_init_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        )
        self.assertIn("analysis", ast.literal_eval(all_assignment.value))

        forbidden_targets = {
            "autoslice.analysis.boundaries",
            "autoslice.analysis.topic_analysis",
            *(path.removeprefix("src/").removesuffix(".py").replace("/", ".")
              for path in TOPIC_ANALYSIS_CONSUMERS),
        }
        owner_imports = _import_targets(_parse(OWNER_PATH))
        self.assertIn(
            "autoslice.analysis.review.deduplication",
            owner_imports,
        )
        self.assertTrue(owner_imports.isdisjoint(forbidden_targets))


class TopicAnalysisFormattingTests(unittest.TestCase):
    def test_build_chunk_prompt_uses_subtitles_and_danmaku_but_not_manual_timeline(self):
        chunk = {
            "start": 60,
            "end": 660,
            "text": "[0:01:00] 主播讲述闹钟设错时间的经过",
            "danmaku_info": "[弹幕: 本段峰值120条/分钟]",
            "danmaku_evidence": ["[0:02:00] 弹幕开始讨论闹钟"],
            "manual_timeline_info": "⭐ 人工记录不应进入首轮",
        }

        prompt, start, end = topic_analysis.build_chunk_prompt(
            chunk,
            0,
            2,
            streamer_name="测试主播",
        )

        self.assertEqual((start, end), (60, 660))
        self.assertIn("第1/2块", prompt)
        self.assertIn("主播讲述闹钟", prompt)
        self.assertIn("弹幕开始讨论闹钟", prompt)
        self.assertNotIn("人工记录不应进入首轮", prompt)

    def test_build_chunk_prompt_compact_mode_limits_subtitle_payload(self):
        marker = "结尾标记"
        chunk = {
            "start": 0,
            "end": 600,
            "text": "前" * topic_analysis.LLM_COMPACT_TEXT_CHARS + marker,
            "danmaku_info": "无弹幕",
        }

        compact_prompt, _, _ = topic_analysis.build_chunk_prompt(
            chunk,
            0,
            1,
            compact=True,
        )
        full_prompt, _, _ = topic_analysis.build_chunk_prompt(
            chunk,
            0,
            1,
            compact=False,
        )

        self.assertNotIn(marker, compact_prompt)
        self.assertIn(marker, full_prompt)

    def test_repair_short_topic_end_uses_body_length_and_chunk_boundary(self):
        unchanged = topic_analysis.repair_short_topic_end(
            100,
            112,
            ["很长的正文" * 20],
            600,
        )
        repaired = topic_analysis.repair_short_topic_end(
            100,
            102,
            ["很长的正文" * 20],
            130,
        )

        self.assertEqual(unchanged, 112)
        self.assertGreater(repaired, 102)
        self.assertLessEqual(repaired, 130)

    def test_code_fence_and_chunk_range_helpers_keep_legacy_contract(self):
        self.assertEqual(
            topic_analysis.strip_code_fence("```json\n{\"topics\": []}\n```"),
            '{"topics": []}',
        )
        self.assertTrue(topic_analysis.is_topic_in_chunk(10, 20, 0, 100))
        self.assertTrue(topic_analysis.is_topic_in_chunk(-80, 20, 0, 100))
        self.assertFalse(topic_analysis.is_topic_in_chunk(-91, 20, 0, 100))
        self.assertFalse(topic_analysis.is_topic_in_chunk(10, 191, 0, 100))
        self.assertFalse(topic_analysis.is_topic_in_chunk(20, 20, 0, 100))


class TopicAnalysisResponseTests(unittest.TestCase):
    def test_parse_json_response_adds_topic_and_clip_mark(self):
        accepted = []
        response = json.dumps(
            {
                "topics": [
                    {
                        "start": "0:00:30",
                        "end": "0:02:00",
                        "title": "闹钟误设成半夜",
                        "publish_title": "主播把中午闹钟设到半夜😂",
                        "title_hook": {
                            "type": "反差",
                            "fact": "闹钟离谱反转",
                            "contrast": "中午被设成半夜",
                        },
                        "can_slice": True,
                        "points": ["主播说明自己因此睡过头"],
                    }
                ]
            },
            ensure_ascii=False,
        )

        blocks, marks = topic_analysis.parse_json_topics_response(
            response,
            0,
            600,
            accepted,
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["title"], "闹钟误设成半夜")
        self.assertEqual(
            accepted[0]["title_hook"],
            {
                "type": "反差",
                "fact": "闹钟离谱反转",
                "contrast": "中午被设成半夜",
            },
        )
        self.assertEqual(marks, [{"start": 30, "end": 120, "title": "闹钟误设成半夜"}])
        self.assertEqual(len(blocks), 1)
        self.assertIn("闹钟误设成半夜", blocks[0])

    def test_parse_json_response_rejects_invalid_and_duplicate_topics(self):
        accepted = [
            {
                "start": 30,
                "end": 120,
                "title": "已有话题",
                "body": ["已有正文"],
            }
        ]
        response = json.dumps(
            {
                "topics": [
                    {
                        "start": "坏时间",
                        "end": "0:02:00",
                        "title": "坏时间",
                        "points": ["无效"],
                    },
                    {
                        "start": "0:00:35",
                        "end": "0:02:05",
                        "title": "已有话题",
                        "points": ["重复正文"],
                    },
                ]
            },
            ensure_ascii=False,
        )

        blocks, marks = topic_analysis.parse_json_topics_response(
            response,
            0,
            600,
            accepted,
        )

        self.assertEqual(blocks, [])
        self.assertEqual(marks, [])
        self.assertEqual(len(accepted), 1)

    def test_markdown_fallback_parses_heading_and_ignores_part_label(self):
        response = """
Part 2: 重复分块标题
[0:00:30 - 0:02:10] ✂️ 主播误设闹钟
·主播说明自己把中午十二点设成了半夜十二点
"""
        accepted = []

        blocks, marks = topic_analysis.parse_llm_response(
            response,
            0,
            600,
            accepted,
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["title"], "主播误设闹钟")
        self.assertEqual(marks[0]["start"], 30)
        self.assertIn("主播误设闹钟", blocks[0])
        self.assertNotIn("Part 2", blocks[0])

    def test_fallback_topic_strips_time_labels_and_never_marks_slice(self):
        chunk = {
            "start": 600,
            "end": 1200,
            "text": (
                "[0:10:00] 主播在这一段连续聊天并说明一件完整事情\n"
                "[0:10:20] 随后继续补充事情的后续情况"
            ),
        }

        topic = topic_analysis.make_fallback_topic_from_chunk(
            chunk,
            streamer_name="测试主播",
        )

        self.assertIsNotNone(topic)
        self.assertEqual((topic["start"], topic["end"]), (600, 1200))
        self.assertFalse(topic["can_slice"])
        self.assertTrue(topic["fallback"])
        self.assertIn("测试主播", topic["body"][0])


class TopicAnalysisOrchestrationTests(unittest.TestCase):
    def _response(self, start, end, title):
        return json.dumps(
            {
                "topics": [
                    {
                        "start": start,
                        "end": end,
                        "title": title,
                        "can_slice": False,
                        "points": [f"主播完整说明{title}的前因后果"],
                    }
                ]
            },
            ensure_ascii=False,
        )

    def test_empty_chunks_do_not_load_api(self):
        with patch(
            "autoslice.analysis.topic.analysis.llm_gateway.load_api_config",
            side_effect=AssertionError("空输入不应读取 API"),
        ):
            self.assertEqual(
                topic_analysis.analyze_topic_chunks([], "测试主播"),
                ([], [], None),
            )

    def test_success_and_failed_chunk_merge_in_video_order_with_fallback(self):
        chunks = [
            {
                "start": 0,
                "end": 600,
                "text": "[0:00:10] 主播完整说明第一件事情的前因后果",
                "danmaku_info": "无弹幕",
            },
            {
                "start": 600,
                "end": 1200,
                "text": "[0:10:10] 主播继续聊天并完整说明第二件事情的前因后果",
                "danmaku_info": "无弹幕",
            },
        ]
        responses = [
            self._response("0:00:10", "0:01:30", "第一件事情"),
            RuntimeError("第二块临时失败"),
        ]

        with (
            patch(
                "autoslice.analysis.topic.analysis.llm_gateway.load_api_config",
                return_value=("https://example.test", "token", "model"),
            ),
            patch(
                "autoslice.analysis.topic.analysis.llm_execution.configured_llm_concurrency",
                return_value=1,
            ),
            patch(
                "autoslice.analysis.topic.analysis.llm_gateway.call_llm_with_retry",
                side_effect=responses,
            ),
        ):
            topics, failed_chunks, warning = (
                topic_analysis.analyze_topic_chunks(chunks, "测试主播")
            )

        self.assertEqual(topics[0]["title"], "第一件事情")
        self.assertTrue(topics[1]["fallback"])
        self.assertEqual(failed_chunks[0]["index"], 2)
        self.assertIn("第二块临时失败", failed_chunks[0]["error"])
        self.assertIsNone(warning)


if __name__ == "__main__":
    unittest.main()
