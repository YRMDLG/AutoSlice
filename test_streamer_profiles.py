import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from streamer_profiles import (
    active_streamer_profile,
    current_streamer_profile,
    infer_streamer_name_from_filename,
    public_streamer_profiles,
    resolve_streamer_profile,
    streamer_profile_context,
)


class StreamerProfileTests(unittest.TestCase):

    def test_auto_matching_and_public_payload_are_generic_by_default(self):
        zeyin = resolve_streamer_profile(
            "auto",
            r"recordings\1947277414-泽音Melody\直播.flv",
        )
        generic = resolve_streamer_profile(
            "auto",
            r"recordings\另一位主播\直播.flv",
        )

        self.assertEqual(zeyin.id, "zeyin")
        self.assertEqual(zeyin.report_name, "音音")
        self.assertEqual(generic.id, "generic")
        self.assertEqual(generic.title_prefix, "")
        public = public_streamer_profiles()
        self.assertEqual(public[0]["id"], "auto")
        self.assertNotIn("path_keywords", public[-1])
        self.assertNotIn("title_style_profile", public[-1])
        self.assertNotIn("asr_replacements", public[-1])

    def test_auto_profile_uses_streamer_name_before_filename_date(self):
        paths = (
            r"recordings\泽音-2026-07-22 19_58-周三歌杂.flv",
            r"recordings\泽音_2026年07月22日19点58分.flv",
            r"recordings\泽音_20260722_1958.flv",
        )

        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(infer_streamer_name_from_filename(path), "泽音")
                profile = resolve_streamer_profile("auto", path)
                self.assertEqual(profile.id, "zeyin")
                self.assertEqual(profile.report_name, "音音")
                self.assertEqual(profile.title_prefix, "【泽音】")

    def test_unknown_streamer_gets_task_profile_from_filename(self):
        profile = resolve_streamer_profile(
            "auto",
            r"recordings\七海Nana7mi-2026-07-22 20_00-歌杂.flv",
        )

        self.assertEqual(profile.id, "generic")
        self.assertEqual(profile.canonical_name, "七海Nana7mi")
        self.assertEqual(profile.report_name, "七海Nana7mi")
        self.assertEqual(profile.title_prefix, "【七海Nana7mi】")
        self.assertIn("七海Nana7mi", profile.aliases)

    def test_filename_fallback_does_not_override_known_parent_profile(self):
        profile = resolve_streamer_profile(
            "auto",
            (
                r"recordings\1947277414-泽音Melody"
                r"\吃会石然后节奏天国-2026年07月05号-20点03分18秒-001.flv"
            ),
        )

        self.assertEqual(profile.id, "zeyin")
        self.assertEqual(profile.title_prefix, "【泽音】")

    def test_filename_without_date_keeps_generic_profile(self):
        self.assertIsNone(infer_streamer_name_from_filename("周三歌杂.flv"))
        profile = resolve_streamer_profile("auto", r"recordings\周三歌杂.flv")
        self.assertEqual(profile.id, "generic")
        self.assertEqual(profile.title_prefix, "")

    def test_context_is_nested_and_thread_isolated(self):
        self.assertIsNone(active_streamer_profile())
        with streamer_profile_context("generic"):
            self.assertEqual(current_streamer_profile().id, "generic")
            with streamer_profile_context("zeyin"):
                self.assertEqual(current_streamer_profile().id, "zeyin")
            self.assertEqual(current_streamer_profile().id, "generic")
        self.assertIsNone(active_streamer_profile())

        def selected(profile_id):
            with streamer_profile_context(profile_id):
                return current_streamer_profile().id

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(selected, ("generic", "zeyin")))
        self.assertEqual(results, ["generic", "zeyin"])

    def test_invalid_config_has_clear_error(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "profiles.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "default_profile_id": "missing",
                "profiles": [{
                    "id": "generic",
                    "label": "通用",
                    "canonical_name": "主播",
                    "report_name": "主播",
                }],
            }, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "default_profile_id"):
                resolve_streamer_profile("auto", config_path=path)


if __name__ == "__main__":
    unittest.main()
