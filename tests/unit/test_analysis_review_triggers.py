import ast
import hashlib
import unittest
from pathlib import Path

from autoslice import topic_engine
from autoslice.analysis import boundaries, candidates
from autoslice.analysis.review import triggers
from scripts import architecture_snapshot

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.triggers"
OWNER_PATH = SRC_ROOT / "autoslice/analysis/review/triggers.py"
TRIGGER_NAMES = (
    "_TRIGGER_CONTEXT_TOPIC_RE",
    "_looks_like_sc_or_gift_trigger",
    "_is_explicit_sc_trigger",
    "_gift_trigger_has_question_followup",
    "_find_sc_context_start",
    "_clip_context_requires_trigger",
    "_is_explicit_sc_topic",
)
FUNCTION_NAMES = TRIGGER_NAMES[1:]
EXPECTED_SOURCE_HASHES = {
    "_TRIGGER_CONTEXT_TOPIC_RE": (
        "b815ca87ec62b701a4eb38102a7c7ce74b67d073b777dad79e7bda1b17f5d5a2"
    ),
    "_looks_like_sc_or_gift_trigger": (
        "192c79195d3b69e88d31076a0c9284565b6ece07f0b8d3d45a3abcc85afa9c9e"
    ),
    "_is_explicit_sc_trigger": (
        "772a23f374dd50c3d98774eb8c0bf1c3d6c3aee95815f788f846b6ef67797712"
    ),
    "_gift_trigger_has_question_followup": (
        "ed681da92dbe693be96b53cdbc8fb91a3e913b585a694f850b597de3b85d9b03"
    ),
    "_find_sc_context_start": (
        "d5a4b52a851b935aac623584852505c18af388c2e0ccbbfe3b7b58f8865b4734"
    ),
    "_clip_context_requires_trigger": (
        "d2e1a206b3949886f51823462c07ed753a62ba3370eb1599b9be6bca9e0184d6"
    ),
    "_is_explicit_sc_topic": (
        "9798affe36ebf7bfb6a880713749105329d389fab68a3861b261274e855671ca"
    ),
}
OWNER_DEPENDENCIES = {
    "autoslice.analysis.review.policy",
    "autoslice.transcription.segments",
}
PRODUCTION_IMPORTERS = {
    "autoslice.analysis.boundaries",
    "autoslice.analysis.candidates",
    "autoslice.analysis.review.context_edges",
    "autoslice.analysis.review.decisions",
    "autoslice.topic_engine",
}


def _module_name(path):
    parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _direct_imports(tree):
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.update(
                f"{node.module}.{alias.name}"
                for alias in node.names
                if alias.name != "*"
            )
    return imported


class ReviewTriggerOwnershipTests(unittest.TestCase):
    def test_migrated_constant_and_function_source_hashes_are_exact(self):
        source = OWNER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        definitions = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_NAMES:
                definitions[node.name] = ast.get_source_segment(source, node)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name)
                and target.id == "_TRIGGER_CONTEXT_TOPIC_RE"
                for target in node.targets
            ):
                definitions["_TRIGGER_CONTEXT_TOPIC_RE"] = ast.get_source_segment(
                    source,
                    node,
                )

        actual = {
            name: hashlib.sha256(definitions[name].encode("utf-8")).hexdigest()
            for name in TRIGGER_NAMES
        }
        self.assertEqual(actual, EXPECTED_SOURCE_HASHES)

    def test_triggers_is_unique_owner_and_facades_keep_object_identity(self):
        implementations = []
        compiled_constants = []
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in FUNCTION_NAMES:
                    implementations.append((_module_name(path), node.name))
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Call)
                    and any(
                        isinstance(target, ast.Name)
                        and target.id == "_TRIGGER_CONTEXT_TOPIC_RE"
                        for target in node.targets
                    )
                ):
                    compiled_constants.append(_module_name(path))

        self.assertEqual(
            set(implementations),
            {(OWNER_MODULE, name) for name in FUNCTION_NAMES},
        )
        self.assertEqual(compiled_constants, [OWNER_MODULE])
        for name in TRIGGER_NAMES:
            owner = getattr(triggers, name)
            with self.subTest(name=name):
                self.assertNotIn(name, boundaries.FACADE_EXPORTS)
                self.assertIs(getattr(boundaries, name), owner)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)

    def test_facade_exports_are_static_and_exact(self):
        source = OWNER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignments = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "FACADE_EXPORTS"
                for target in node.targets
            )
        ]

        expected = {name: name for name in TRIGGER_NAMES}
        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.Dict)
        self.assertEqual(ast.literal_eval(assignments[0].value), expected)
        self.assertEqual(triggers.FACADE_EXPORTS, expected)

    def test_owner_dependencies_and_production_importers_are_exact(self):
        owner_tree = ast.parse(OWNER_PATH.read_text(encoding="utf-8"))
        direct_imports = _direct_imports(owner_tree)
        self.assertEqual(
            {name for name in direct_imports if name.startswith("autoslice.")},
            OWNER_DEPENDENCIES,
        )
        self.assertEqual(
            direct_imports - OWNER_DEPENDENCIES,
            {"__future__.annotations", "re"},
        )

        importers = set()
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if OWNER_MODULE in _direct_imports(tree):
                importers.add(_module_name(path))
        self.assertEqual(importers, PRODUCTION_IMPORTERS)

    def test_consumers_call_owner_not_local_aliases(self):
        local_calls = []
        owner_calls = set()
        for relative_path in (
            "autoslice/analysis/boundaries.py",
            "autoslice/analysis/review/context_edges.py",
        ):
            tree = ast.parse((SRC_ROOT / relative_path).read_text(encoding="utf-8"))
            for function in (
                node for node in tree.body if isinstance(node, ast.FunctionDef)
            ):
                for node in ast.walk(function):
                    if not isinstance(node, ast.Call):
                        continue
                    if isinstance(node.func, ast.Name) and node.func.id in FUNCTION_NAMES:
                        local_calls.append(
                            (relative_path, function.name, node.func.id, node.lineno)
                        )
                    if (
                        isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "trigger_analysis"
                        and node.func.attr in FUNCTION_NAMES
                    ):
                        owner_calls.add(node.func.attr)

        self.assertEqual(local_calls, [])
        self.assertEqual(owner_calls, set(FUNCTION_NAMES))

    def test_architecture_has_no_cycles_duplicates_or_patch_growth(self):
        current = architecture_snapshot.build_snapshot(ROOT)

        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertEqual(
            current["summary"]["duplicate_top_level_definition_count"],
            0,
        )
        self.assertLessEqual(current["test_private_patches"]["total"], 17)


class ReviewTriggerBehaviorTests(unittest.TestCase):
    def test_explicit_sc_spellings_and_gift_thanks_are_recognised(self):
        for text in ("收到一个 s c", "Thanks for the SUPER CHAT", "醒目留言来了"):
            with self.subTest(text=text):
                self.assertTrue(triggers._is_explicit_sc_trigger(text))
                self.assertTrue(triggers._looks_like_sc_or_gift_trigger(text))

        self.assertTrue(triggers._looks_like_sc_or_gift_trigger("谢谢老板送的舰长"))
        self.assertFalse(triggers._looks_like_sc_or_gift_trigger("谢谢大家今天来看直播"))

    def test_gift_thanks_require_question_evidence_or_short_lookback(self):
        question_segments = [
            (20, 23, "谢谢老板送的礼物"),
            (24, 28, "他说为什么最近不唱歌呢"),
            (100, 104, "开始回答这个问题"),
        ]
        unrelated_segments = [
            (20, 23, "谢谢老板送的礼物"),
            (24, 28, "接下来继续玩游戏"),
            (100, 104, "聊一个普通话题"),
        ]
        short_lookback_segments = [
            (89, 91, "谢谢老板送的礼物"),
            (100, 104, "开始回答"),
        ]

        self.assertTrue(
            triggers._gift_trigger_has_question_followup(0, 100, question_segments)
        )
        self.assertEqual(triggers._find_sc_context_start(100, question_segments), 20)
        self.assertFalse(
            triggers._gift_trigger_has_question_followup(0, 100, unrelated_segments)
        )
        self.assertIsNone(
            triggers._find_sc_context_start(100, unrelated_segments)
        )
        self.assertEqual(
            triggers._find_sc_context_start(100, short_lookback_segments),
            89,
        )

    def test_nearest_eligible_trigger_wins_and_contiguous_cues_attach(self):
        segments = [
            (20, 24, "收到一条醒目留言"),
            (80, 88, "先把这位观众的原话念完"),
            (90, 93, "谢谢老板送的礼物"),
            (100, 104, "开始回答"),
        ]

        self.assertEqual(triggers._find_sc_context_start(100, segments), 80)

    def test_context_requires_trigger_prefers_explicit_field_then_text(self):
        self.assertFalse(triggers._clip_context_requires_trigger({
            "context_requires_trigger": False,
            "title": "回应醒目留言",
        }))
        self.assertTrue(triggers._clip_context_requires_trigger({
            "context_requires_trigger": True,
            "title": "普通聊天",
        }))
        for mark in (
            {"title": "回应观众留言"},
            {"publish_title": "读完付费留言后的反应"},
            {"body": ["·感谢舰长礼物并回应问题"]},
        ):
            with self.subTest(mark=mark):
                self.assertTrue(triggers._clip_context_requires_trigger(mark))
        self.assertFalse(triggers._clip_context_requires_trigger({
            "title": "展示手机闹钟",
            "body": ["·普通系统设置讨论"],
        }))

    def test_explicit_sc_topic_uses_title_fields_only(self):
        for mark in (
            {"title": "回答 SC 提问"},
            {"publish_title": "一条 Super Chat 引发的讨论"},
            {"title": "回应醒目留言"},
        ):
            with self.subTest(mark=mark):
                self.assertTrue(triggers._is_explicit_sc_topic(mark))

        self.assertFalse(triggers._is_explicit_sc_topic({
            "title": "普通话题",
            "body": ["·提到 SC"],
        }))


if __name__ == "__main__":
    unittest.main()
