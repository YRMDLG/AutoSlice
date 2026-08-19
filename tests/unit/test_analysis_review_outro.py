import ast
import hashlib
import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace

from autoslice import pipeline, topic_engine
from autoslice.analysis import boundaries, candidates
from autoslice.analysis import review as review_package
from autoslice.analysis.review import outro, policy
from scripts import architecture_snapshot

ROOT = Path(__file__).resolve().parents[2]
OUTRO_NAMES = (
    "_OUTRO_ACTIVITY_VARIANT_RE",
    "_OUTRO_FAREWELL_EVIDENCE",
    "_OUTRO_TRIGGER_NORMALISE_RE",
    "_detect_stream_outro_clip",
    "_has_outro_farewell_evidence",
    "_normalise_outro_trigger_text",
    "_outro_topic_from_mark",
)


def _streamer_profile(*, with_config=True, title_prefix="【测试主播】"):
    config = None
    if with_config:
        config = SimpleNamespace(
            series_title="晚安测试系列",
            search_tail_sec=120,
            triggers=("今天就直播到这里", "今天就先玩到这里"),
        )
    return SimpleNamespace(title_prefix=title_prefix, outro_clip=config)


class OutroDetectionTests(unittest.TestCase):

    def test_explicit_trigger_can_cross_cues_and_uses_first_tail_match(self):
        mark = outro._detect_stream_outro_clip(
            [
                (930.0, 933.0, "今天就直播到这里"),
                (902.1, 905.0, "到这里了，谢谢大家"),
                (900.2, 902.0, "今天就直播"),
            ],
            1000.2,
            _streamer_profile(),
        )

        self.assertIsNotNone(mark)
        self.assertEqual(mark["start"], 900)
        self.assertEqual(mark["end"], 1001)
        self.assertEqual(mark["outro_trigger"], "今天就直播到这里")
        self.assertEqual(mark["publish_title"], "【测试主播】晚安测试系列")
        self.assertEqual(mark["clip_type"], "stream_outro")
        self.assertTrue(mark["preserve_to_video_end"])
        self.assertEqual(mark["time_basis"], "video_elapsed_seconds")

    def test_generic_goodnight_is_not_an_outro_trigger(self):
        self.assertIsNone(outro._detect_stream_outro_clip(
            [(950.0, 955.0, "大家晚安，明天见")],
            1000,
            _streamer_profile(),
        ))

    def test_trigger_before_configured_tail_is_ignored(self):
        self.assertIsNone(outro._detect_stream_outro_clip(
            [(879.0, 883.0, "今天就直播到这里")],
            1000,
            _streamer_profile(),
        ))

    def test_activity_variant_requires_nearby_farewell_evidence(self):
        for farewell_segment in (
            (890.0, 892.0, "大家晚安"),
            (950.0, 952.0, "拜拜，明天见"),
        ):
            with self.subTest(farewell_segment=farewell_segment):
                mark = outro._detect_stream_outro_clip(
                    [
                        farewell_segment,
                        (900.0, 904.0, "我们今天就先聊到这里了"),
                    ],
                    1000,
                    _streamer_profile(),
                )

                self.assertIsNotNone(mark)
                self.assertEqual(mark["start"], 900)
                self.assertEqual(mark["outro_trigger"], "我们今天就先聊到这里了")

        self.assertIsNone(outro._detect_stream_outro_clip(
            [
                (900.0, 904.0, "我们今天就先唱到这里了"),
                (905.0, 908.0, "接下来换个环节"),
            ],
            1000,
            _streamer_profile(),
        ))

    def test_explicit_config_trigger_wins_same_cue_tie(self):
        mark = outro._detect_stream_outro_clip(
            [(900.0, 904.0, "我们今天就先玩到这里了，晚安")],
            1000,
            _streamer_profile(),
        )

        self.assertEqual(mark["outro_trigger"], "今天就先玩到这里")

    def test_missing_configuration_empty_subtitles_and_invalid_duration_return_none(self):
        segments = [(950.0, 955.0, "今天就直播到这里")]
        self.assertIsNone(outro._detect_stream_outro_clip(
            segments,
            1000,
            _streamer_profile(with_config=False),
        ))
        self.assertIsNone(outro._detect_stream_outro_clip(segments, 1000))
        self.assertIsNone(outro._detect_stream_outro_clip(
            [],
            1000,
            _streamer_profile(),
        ))
        for duration in (None, 0, -1, "无效时长", object()):
            with self.subTest(duration=duration):
                self.assertIsNone(outro._detect_stream_outro_clip(
                    segments,
                    duration,
                    _streamer_profile(),
                ))

    def test_outro_mark_maps_to_report_topic_fields(self):
        topic = outro._outro_topic_from_mark({
            "start": 900,
            "end": 1001,
            "title": "晚安测试系列",
            "publish_title": "【测试主播】晚安测试系列",
            "outro_trigger": "今天就直播到这里",
        })

        self.assertEqual(topic, {
            "start": 900,
            "end": 1001,
            "title": "晚安测试系列",
            "publish_title": "【测试主播】晚安测试系列",
            "can_slice": True,
            "slice_anchor": 900,
            "slice_anchor_source": "收播口令",
            "clip_type": "stream_outro",
            "preserve_to_video_end": True,
            "outro_trigger": "今天就直播到这里",
            "body": [
                "·检测到收播开始语：“今天就直播到这里”",
                "·保留从收播告别到录播实际结束的完整互动",
                "·系列切片：晚安测试系列",
            ],
        })


class OutroArchitectureTests(unittest.TestCase):

    def test_outro_is_unique_owner_and_compatibility_objects_are_identical(self):
        self.assertEqual(
            outro.FACADE_EXPORTS,
            {name: name for name in OUTRO_NAMES},
        )
        for name in OUTRO_NAMES:
            owner = getattr(outro, name)
            with self.subTest(name=name):
                self.assertNotIn(name, boundaries.FACADE_EXPORTS)
                self.assertIs(getattr(boundaries, name), owner)
                self.assertIs(getattr(candidates, name), owner)
                self.assertIs(getattr(topic_engine, name), owner)

        self.assertIs(pipeline.outro_analysis, outro)
        self.assertIs(
            pipeline._detect_stream_outro_clip,
            outro._detect_stream_outro_clip,
        )
        self.assertIs(
            pipeline._outro_topic_from_mark,
            outro._outro_topic_from_mark,
        )
        self.assertIs(outro.OUTRO_TRIGGER_JOIN_GAP_SEC, policy.OUTRO_TRIGGER_JOIN_GAP_SEC)
        self.assertIs(
            outro.OUTRO_VARIANT_FAREWELL_BEFORE_SEC,
            policy.OUTRO_VARIANT_FAREWELL_BEFORE_SEC,
        )
        self.assertIs(
            outro.OUTRO_VARIANT_FAREWELL_AFTER_SEC,
            policy.OUTRO_VARIANT_FAREWELL_AFTER_SEC,
        )

    def test_migrated_function_sources_are_unchanged(self):
        expected_hashes = {
            "_normalise_outro_trigger_text": "05f6e8b9ca618567c9aa54afee340cf0131073eed64397718f769531ab064a51",
            "_has_outro_farewell_evidence": "b97bc587f5668b72a505c77802c78f1779c22f4f09630837b21274aaa9c357e6",
            "_detect_stream_outro_clip": "5ddedcd1edd4bf6b9cd4d2c1d541f2ba4dd855b57fe7395cb4ccdde59e94bdae",
            "_outro_topic_from_mark": "4fe6518766acae348c37bf0eb7f861154ae48dc61e3353d85fd398918df73f0b",
        }
        actual_hashes = {
            name: hashlib.sha256(
                inspect.getsource(getattr(outro, name)).strip().encode("utf-8")
            ).hexdigest()
            for name in expected_hashes
        }

        self.assertEqual(actual_hashes, expected_hashes)

    def test_importers_dependencies_and_architecture_metrics_are_exact(self):
        current = architecture_snapshot.build_snapshot(ROOT)
        import_edges = {
            (edge["from"], edge["to"])
            for edge in current["import_edges"]
        }
        owner_module = "autoslice.analysis.review.outro"
        self.assertEqual(
            {
                source
                for source, target in import_edges
                if target == owner_module
            },
            {
                "autoslice.analysis.boundaries",
                "autoslice.analysis.candidates",
                "autoslice.pipeline",
                "autoslice.topic_engine",
            },
        )
        self.assertEqual(
            {
                target
                for source, target in import_edges
                if source == owner_module
            },
            {
                "autoslice.analysis.review.policy",
                "autoslice.streamer_profiles",
            },
        )
        self.assertEqual(current["dependency_cycles"], [])
        self.assertEqual(current["duplicate_top_level_definitions"], [])
        self.assertLessEqual(current["test_private_patches"]["total"], 17)

    def test_consumers_have_no_outro_reference_through_boundary_analysis(self):
        stale_references = []
        for relative_path in (
            "src/autoslice/analysis/candidates.py",
            "src/autoslice/pipeline.py",
            "src/autoslice/topic_engine.py",
        ):
            tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
            stale_references.extend(
                (relative_path, node.lineno, node.attr)
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "boundary_analysis"
                and node.attr in OUTRO_NAMES
            )

        self.assertEqual(stale_references, [])

    def test_review_package_remains_lazy_and_lists_outro_in_order(self):
        init_tree = ast.parse(
            (ROOT / "src/autoslice/analysis/review/__init__.py").read_text(
                encoding="utf-8"
            )
        )
        imports = [
            node
            for node in ast.walk(init_tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        self.assertEqual(imports, [])
        self.assertEqual(review_package.__all__, sorted(review_package.__all__))
        self.assertIn("outro", review_package.__all__)


if __name__ == "__main__":
    unittest.main()
