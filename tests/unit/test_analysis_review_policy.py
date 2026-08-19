import ast
import re
import unittest
from pathlib import Path

from autoslice.analysis import clip_policy as legacy_clip_policy
from autoslice.analysis.review import policy as clip_policy

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src"
OWNER_MODULE = "autoslice.analysis.review.policy"
LEGACY_MODULE = "autoslice.analysis.clip_policy"
POLICY_CONSUMERS = {
    "autoslice.analysis.boundaries",
    "autoslice.analysis.review.context_edges",
    "autoslice.analysis.review.context_evidence",
    "autoslice.analysis.review.reconciliation",
    "autoslice.analysis.candidates",
    "autoslice.analysis.manual.candidates",
    "autoslice.analysis.manual.enrichment",
    "autoslice.analysis.review.candidates",
    "autoslice.analysis.review.finalization",
    "autoslice.analysis.review.prompt",
    "autoslice.analysis.review.transitions",
    "autoslice.analysis.review.triggers",
    "autoslice.analysis.review.workflow",
    "autoslice.analysis.review.decisions",
    "autoslice.analysis.topic.analysis",
    "autoslice.pipeline",
    "autoslice.topic_engine",
}


class AnalysisReviewPolicyTests(unittest.TestCase):
    @staticmethod
    def _module_name(path):
        parts = list(path.relative_to(SRC_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    @staticmethod
    def _imported_names(path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(
                    f"{node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )
        return imported

    def test_policy_owner_and_legacy_facade_are_definition_free(self):
        for relative_path in (
            "autoslice/analysis/review/policy.py",
            "autoslice/analysis/clip_policy.py",
        ):
            with self.subTest(path=relative_path):
                tree = ast.parse(
                    (SRC_ROOT / relative_path).read_text(encoding="utf-8")
                )
                self.assertFalse(
                    any(
                        isinstance(
                            node,
                            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                        )
                        for node in tree.body
                    )
                )

    def test_legacy_facade_forwards_every_owner_name_by_identity(self):
        self.assertIs(legacy_clip_policy.FACADE_EXPORTS, clip_policy.FACADE_EXPORTS)
        for name, value in vars(clip_policy).items():
            if name.startswith("__"):
                continue
            with self.subTest(name=name):
                self.assertIs(getattr(legacy_clip_policy, name), value)

    def test_policy_thresholds_and_regex_identity_are_preserved(self):
        self.assertEqual(clip_policy.CLIP_MIN_INTEREST_SCORE, 75)
        self.assertEqual(clip_policy.CLIP_MANUAL_REVIEW_MIN_STARS, 4)
        self.assertEqual(clip_policy.SC_CONTEXT_LOOKBACK_SEC, 180)
        self.assertEqual(clip_policy.SC_FALLBACK_GIFT_LOOKBACK_SEC, 15)
        self.assertEqual(clip_policy.OUTRO_TRIGGER_JOIN_GAP_SEC, 8)
        self.assertIsInstance(clip_policy.THANKS_TRIGGER_RE, re.Pattern)
        self.assertIs(
            legacy_clip_policy.THANKS_TRIGGER_RE,
            clip_policy.THANKS_TRIGGER_RE,
        )

    def test_review_package_is_lazy_and_declares_review_modules(self):
        package_path = SRC_ROOT / "autoslice/analysis/review/__init__.py"
        tree = ast.parse(package_path.read_text(encoding="utf-8"))
        imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        declared = None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            ):
                declared = ast.literal_eval(node.value)

        self.assertEqual(imports, [])
        self.assertEqual(
            declared,
            [
                "candidates",
                "context_edges",
                "context_evidence",
                "decisions",
                "deduplication",
                "finalization",
                "outro",
                "policy",
                "prompt",
                "reconciliation",
                "scoring",
                "transitions",
                "triggers",
                "workflow",
            ],
        )

    def test_policy_consumers_import_owner_and_no_production_uses_facade(self):
        owner_importers = set()
        legacy_importers = set()
        for path in SRC_ROOT.rglob("*.py"):
            module_name = self._module_name(path)
            imported = self._imported_names(path)
            if OWNER_MODULE in imported:
                owner_importers.add(module_name)
            if LEGACY_MODULE in imported:
                legacy_importers.add(module_name)

        self.assertEqual(owner_importers, POLICY_CONSUMERS | {LEGACY_MODULE})
        self.assertEqual(legacy_importers, set())

    def test_policy_owner_has_no_reverse_dependency_on_consumers_or_facade(self):
        imported = self._imported_names(
            SRC_ROOT / "autoslice/analysis/review/policy.py"
        )

        self.assertTrue(imported.isdisjoint(POLICY_CONSUMERS | {LEGACY_MODULE}))


if __name__ == "__main__":
    unittest.main()
