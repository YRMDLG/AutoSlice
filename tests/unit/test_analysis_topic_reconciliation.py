import ast
import unittest
from pathlib import Path

from autoslice.analysis.topic import analysis as topic_analysis
from autoslice.analysis.topic import reconciliation as topic_reconciliation
from autoslice.analysis.topic.reconciliation import AdjacentTopicReconciler

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src" / "autoslice"
OWNER_PATH = SRC_ROOT / "analysis" / "topic" / "reconciliation.py"
ANALYSIS_PATH = SRC_ROOT / "analysis" / "topic" / "analysis.py"


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _import_targets(tree):
    targets = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.add(node.module)
            targets.update(
                f"{node.module}.{alias.name}" for alias in node.names
            )
    return targets


class AdjacentTopicReconcilerOwnershipTests(unittest.TestCase):
    def test_reconciliation_has_one_owner_class_and_no_second_implementation(self):
        owner_tree = _parse(OWNER_PATH)
        self.assertEqual(
            [node.name for node in owner_tree.body if isinstance(node, ast.ClassDef)],
            ["AdjacentTopicReconciler"],
        )
        self.assertFalse(
            any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                for node in owner_tree.body
            )
        )

        implementations = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            for node in ast.walk(_parse(path)):
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name == "AdjacentTopicReconciler"
                ):
                    implementations.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(
            implementations,
            ["src/autoslice/analysis/topic/reconciliation.py"],
        )

    def test_analysis_consumes_the_reconciliation_owner_by_identity(self):
        analysis_imports = _import_targets(_parse(ANALYSIS_PATH))
        self.assertIn(
            "autoslice.analysis.topic.reconciliation",
            analysis_imports,
        )
        self.assertIs(
            topic_analysis.topic_reconciliation.AdjacentTopicReconciler,
            AdjacentTopicReconciler,
        )
        self.assertIs(
            topic_reconciliation.AdjacentTopicReconciler,
            AdjacentTopicReconciler,
        )

    def test_reconciliation_owner_has_no_reverse_dependency_on_high_level_modules(self):
        forbidden_prefixes = (
            "autoslice.analysis.topic.analysis",
            "autoslice.analysis.pipeline",
            "autoslice.pipeline",
            "autoslice.topic_engine",
            "autoslice.analysis.topic_engine",
            "autoslice.analysis.review",
        )
        owner_imports = _import_targets(_parse(OWNER_PATH))
        self.assertFalse(
            any(
                target == prefix or target.startswith(f"{prefix}.")
                for target in owner_imports
                for prefix in forbidden_prefixes
            )
        )


class AdjacentTopicReconcilerTests(unittest.TestCase):
    def _topic(self, chunk_index, start, end, title, body, **extra):
        topic = {
            "_chunk_index": chunk_index,
            "start": start,
            "end": end,
            "start_str": f"0:{start // 60:02d}:{start % 60:02d}",
            "end_str": f"0:{end // 60:02d}:{end % 60:02d}",
            "title": title,
            "publish_title": title,
            "can_slice": False,
            "body": list(body),
        }
        topic.update(extra)
        return topic

    def test_strong_overlap_in_adjacent_chunks_merges_fields_and_cleans_metadata(self):
        topics = [
            self._topic(
                0,
                580,
                700,
                "闹钟故事",
                ["·主播讲述闹钟设错", "·观众追问原因"],
                source_note="保留未知公开字段",
            ),
            self._topic(
                1,
                600,
                690,
                "闹钟设错后的爆笑反转",
                ["主播讲述闹钟设错", "·主播补充最后结果"],
                publish_title="【测试】闹钟设错后睡过头😂",
                can_slice=True,
                title_hook={
                    "type": "反差",
                    "fact": "闹钟设错",
                    "contrast": "因此睡过头",
                },
            ),
        ]

        reconciled = AdjacentTopicReconciler.reconcile(topics)

        self.assertEqual(len(reconciled), 1)
        merged = reconciled[0]
        self.assertEqual((merged["start"], merged["end"]), (580, 700))
        self.assertEqual(merged["title"], "闹钟设错后的爆笑反转")
        self.assertEqual(merged["publish_title"], "【测试】闹钟设错后睡过头😂")
        self.assertEqual(
            merged["title_hook"],
            {"type": "反差", "fact": "闹钟设错", "contrast": "因此睡过头"},
        )
        self.assertTrue(merged["can_slice"])
        self.assertEqual(
            merged["body"],
            ["·主播讲述闹钟设错", "·观众追问原因", "·主播补充最后结果"],
        )
        self.assertEqual(merged["source_note"], "保留未知公开字段")
        self.assertEqual(merged["start_str"], "0:09:40")
        self.assertEqual(merged["end_str"], "0:11:40")
        self.assertFalse(any(key.startswith("_chunk") for key in merged))

    def test_strong_overlap_without_textual_relation_does_not_merge(self):
        topics = [
            self._topic(
                0,
                580,
                700,
                "午饭外卖选择",
                ["·主播讨论午饭要点什么外卖"],
            ),
            self._topic(
                1,
                600,
                690,
                "节奏游戏挑战",
                ["·主播开始挑战新的节奏游戏关卡"],
            ),
        ]

        reconciled = AdjacentTopicReconciler.reconcile(topics)

        self.assertEqual(len(reconciled), 2)
        self.assertEqual(
            [topic["title"] for topic in reconciled],
            ["午饭外卖选择", "节奏游戏挑战"],
        )

    def test_strong_overlap_with_shared_generic_words_does_not_merge(self):
        topics = [
            self._topic(
                0,
                580,
                690,
                "主播回应观众的午饭外卖选择并决定点麻辣香锅",
                ["·午饭外卖配送延迟退款，主播回应观众"],
            ),
            self._topic(
                1,
                600,
                650,
                "主播回应观众的节奏游戏挑战并开始最高难度关卡",
                ["·节奏游戏最高难度连击通关，主播回应观众"],
            ),
        ]

        reconciled = AdjacentTopicReconciler.reconcile(topics)

        self.assertEqual(len(reconciled), 2)
        self.assertEqual(
            [topic["title"] for topic in reconciled],
            [
                "主播回应观众的午饭外卖选择并决定点麻辣香锅",
                "主播回应观众的节奏游戏挑战并开始最高难度关卡",
            ],
        )

    def test_short_gap_with_stable_textual_relation_merges(self):
        topics = [
            self._topic(
                0,
                520,
                598,
                "骗朋友激活尴尬游戏",
                ["·主播计划骗朋友激活游戏并让对方叫爸爸"],
            ),
            self._topic(
                1,
                603,
                680,
                "骗朋友激活游戏的后果",
                ["·主播继续说明骗朋友激活游戏后会出现在最近游玩列表"],
            ),
        ]

        reconciled = AdjacentTopicReconciler.reconcile(topics)

        self.assertEqual(len(reconciled), 1)
        self.assertEqual((reconciled[0]["start"], reconciled[0]["end"]), (520, 680))

    def test_adjacent_but_independent_topics_do_not_merge(self):
        topics = [
            self._topic(0, 540, 598, "午饭外卖选择", ["·主播讨论午饭要点什么外卖"]),
            self._topic(1, 603, 680, "节奏游戏挑战", ["·主播开始挑战新的节奏游戏关卡"]),
        ]

        reconciled = AdjacentTopicReconciler.reconcile(topics)

        self.assertEqual([topic["title"] for topic in reconciled], ["午饭外卖选择", "节奏游戏挑战"])

    def test_non_adjacent_chunk_candidates_never_merge(self):
        topics = [
            self._topic(0, 580, 650, "同一件事", ["·同一段完整事件"]),
            self._topic(2, 600, 640, "同一件事", ["·同一段完整事件"]),
        ]

        reconciled = AdjacentTopicReconciler.reconcile(topics)

        self.assertEqual(len(reconciled), 2)

    def test_fallback_never_merges_into_real_candidate_and_results_stay_sorted(self):
        topics = [
            self._topic(
                1,
                600,
                1200,
                "边界事件",
                ["·失败块兜底正文"],
                fallback=True,
            ),
            self._topic(
                0,
                580,
                650,
                "边界事件",
                ["·真实候选正文"],
                can_slice=True,
            ),
            self._topic(2, 1250, 1300, "后续独立话题", ["·后续正文"]),
        ]

        reconciled = AdjacentTopicReconciler.reconcile(topics)

        self.assertEqual(len(reconciled), 3)
        self.assertEqual([topic["start"] for topic in reconciled], [580, 600, 1250])
        self.assertEqual(reconciled[0]["body"], ["·真实候选正文"])
        self.assertTrue(reconciled[0]["can_slice"])
        self.assertTrue(reconciled[1]["fallback"])
        self.assertEqual(reconciled[1]["body"], ["·失败块兜底正文"])
        self.assertTrue(all(not any(key.startswith("_chunk") for key in topic) for topic in reconciled))


if __name__ == "__main__":
    unittest.main()
