import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autoslice.streamer_profiles import (
    PACKAGE_PROFILE_PATH,
    active_streamer_profile,
    add_streamer_profile_replacement,
    current_streamer_profile,
    infer_streamer_name_from_filename,
    merge_profile_subtitle_glossary,
    public_streamer_profiles,
    remove_streamer_profile_replacement,
    resolve_streamer_profile,
    streamer_profile_context,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ZEYIN_REQUESTED_GLOSSARY = {
    "朱鹮", "猪獾", "泽音Melody", "泽音melody", "泽音", "音音", "音姐",
    "音妈", "露露", "四禧丸子", "沐霂", "又一", "梨安", "恬豆", "七海",
    "小孩梓", "阿梓", "柚恩", "露早", "EOE", "篮筐", "小沐标", "酥酥又",
    "向心梨", "恬豆包", "柚恩蜜", "gogo队", "小星星", "星瞳", "宣小纸",
    "真纸棒", "脆鲨",
}
GENERIC_SUBTITLE_GLOSSARY = {"SC", "提督", "舰长", "娃衣", "bangumi"}


class StreamerProfileTests(unittest.TestCase):

    def test_source_examples_match_packaged_defaults(self):
        root_profile = json.loads(
            (REPOSITORY_ROOT / "streamer_profiles.json").read_text(encoding="utf-8")
        )
        packaged_profile = json.loads(PACKAGE_PROFILE_PATH.read_text(encoding="utf-8"))
        root_titles = json.loads(
            (REPOSITORY_ROOT / "title_style_profile.example.json").read_text(
                encoding="utf-8"
            )
        )
        packaged_titles = json.loads(
            PACKAGE_PROFILE_PATH.with_name("title_style_profile.example.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(packaged_profile, root_profile)
        self.assertEqual(packaged_titles, root_titles)

    def test_generic_glossary_is_neutral_and_zeyin_keeps_requested_terms(self):
        generic = resolve_streamer_profile("generic")
        zeyin = resolve_streamer_profile("zeyin")

        self.assertEqual(set(generic.subtitle_glossary), GENERIC_SUBTITLE_GLOSSARY)
        self.assertTrue(ZEYIN_REQUESTED_GLOSSARY.isdisjoint(generic.subtitle_glossary))
        self.assertTrue(ZEYIN_REQUESTED_GLOSSARY.issubset(zeyin.subtitle_glossary))
        self.assertEqual(len(zeyin.subtitle_glossary), len(set(zeyin.subtitle_glossary)))

    def test_zeyin_keeps_existing_fixed_replacements(self):
        zeyin = resolve_streamer_profile("zeyin")

        self.assertEqual(zeyin.asr_replacements, (
            ("英英", "音音"),
            ("莹莹", "音音"),
            ("盈盈", "音音"),
            ("应应", "音音"),
            ("音乐生", "音悦生"),
            ("英悦生", "音悦生"),
            ("音悦声", "音悦生"),
            ("音乐声们", "音悦生们"),
            ("晚安音乐声", "晚安音悦生"),
            ("感谢音乐声", "感谢音悦生"),
            ("见音乐声", "见音悦生"),
        ))

    def test_extra_glossary_only_appends_without_replacing_defaults(self):
        zeyin = resolve_streamer_profile("zeyin")
        merged = merge_profile_subtitle_glossary(
            zeyin,
            ["额外专名", "音音", "额外专名"],
        )

        self.assertEqual(merged[:len(zeyin.subtitle_glossary)], zeyin.subtitle_glossary)
        self.assertEqual(merged[-1], "额外专名")
        self.assertEqual(merged.count("音音"), 1)
        self.assertEqual(merged.count("额外专名"), 1)

    def test_auto_matching_and_public_payload_are_generic_by_default(self):
        zeyin = resolve_streamer_profile(
            "auto",
            r"X:\fixtures\泽音Melody\直播.flv",
        )
        generic = resolve_streamer_profile(
            "auto",
            r"X:\fixtures\另一位主播\直播.flv",
        )

        self.assertEqual(zeyin.id, "zeyin")
        self.assertEqual(zeyin.report_name, "音音")
        self.assertIsNotNone(zeyin.outro_clip)
        self.assertEqual(zeyin.outro_clip.series_title, "晚安小音音")
        self.assertIn("今天就直播到这里", zeyin.outro_clip.triggers)
        self.assertEqual(generic.id, "generic")
        self.assertEqual(generic.title_prefix, "")
        public = public_streamer_profiles()
        self.assertEqual(public[0]["id"], "auto")
        self.assertNotIn("path_keywords", public[-1])
        self.assertNotIn("title_style_profile", public[-1])
        self.assertNotIn("asr_replacements", public[-1])

    def test_auto_profile_uses_streamer_name_before_filename_date(self):
        paths = (
            r"X:\fixtures\泽音-2026-07-22 19_58-周三歌杂.flv",
            r"X:\fixtures\泽音_2026年07月22日19点58分.flv",
            r"X:\fixtures\泽音_20260722_1958.flv",
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
            r"X:\fixtures\七海Nana7mi-2026-07-22 20_00-歌杂.flv",
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
                r"X:\fixtures\泽音Melody"
                r"\吃会石然后节奏天国-2026年07月05号-20点03分18秒-001.flv"
            ),
        )

        self.assertEqual(profile.id, "zeyin")
        self.assertEqual(profile.title_prefix, "【泽音】")

    def test_filename_without_date_keeps_generic_profile(self):
        self.assertIsNone(infer_streamer_name_from_filename("周三歌杂.flv"))
        profile = resolve_streamer_profile("auto", r"X:\fixtures\周三歌杂.flv")
        self.assertEqual(profile.id, "generic")
        self.assertEqual(profile.title_prefix, "")

    def test_context_title_can_identify_known_streamer_in_submission_folder(self):
        for context_hint in ("【泽音】测试投稿", "泽音测试投稿", "音音测试投稿"):
            with self.subTest(context_hint=context_hint):
                profile = resolve_streamer_profile(
                    "auto",
                    r"X:\fixtures\普通投稿\剪映导出.mp4",
                    context_hint=context_hint,
                )

                self.assertEqual(profile.id, "zeyin")
                self.assertEqual(profile.report_name, "音音")

    def test_unknown_context_does_not_fall_back_to_zeyin(self):
        profile = resolve_streamer_profile(
            "auto",
            r"X:\fixtures\普通投稿\剪映导出.mp4",
            context_hint="另一位主播的测试投稿",
        )

        self.assertEqual(profile.id, "generic")
        self.assertEqual(profile.asr_replacements, ())

    def test_context_is_nested_and_thread_isolated(self):
        self.assertIsNone(active_streamer_profile())
        self.assertEqual(current_streamer_profile().id, "generic")
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

    def test_context_accepts_frozen_profile_snapshot(self):
        profile = resolve_streamer_profile(
            "auto",
            r"X:\fixtures\七海Nana7mi-2026-07-22 20_00-歌杂.flv",
        )

        with streamer_profile_context(profile):
            active = current_streamer_profile()

        self.assertIs(active, profile)
        self.assertEqual(active.canonical_name, "七海Nana7mi")

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

    def test_user_profile_replacement_override_is_reversible_and_does_not_edit_defaults(self):
        with TemporaryDirectory() as td:
            override_path = Path(td) / "profile-overrides.json"
            with patch.dict(
                    os.environ,
                    {"AUTOSLICE_STREAMER_PROFILE_OVERRIDES": str(override_path)},
                    clear=False):
                before = resolve_streamer_profile("zeyin")
                added = add_streamer_profile_replacement(
                    "zeyin",
                    "测试错词",
                    "测试正词",
                    expected_fingerprint=before.subtitle_review_fingerprint(),
                )
                after = resolve_streamer_profile("zeyin")
                removed = remove_streamer_profile_replacement(
                    "zeyin",
                    "测试错词",
                    "测试正词",
                    expected_fingerprint=after.subtitle_review_fingerprint(),
                )
                final = resolve_streamer_profile("zeyin")

            self.assertTrue(added["added"])
            self.assertIn(("测试错词", "测试正词"), after.asr_replacements)
            self.assertEqual(added["storage_scope"], "本机用户覆盖词库，不修改仓库默认配置")
            self.assertEqual(removed["replacement_count"], len(before.asr_replacements))
            self.assertNotIn(("测试错词", "测试正词"), final.asr_replacements)
            self.assertNotIn("测试错词", (REPOSITORY_ROOT / "streamer_profiles.json").read_text(encoding="utf-8"))

    def test_user_profile_replacement_rejects_stale_profile(self):
        with TemporaryDirectory() as td:
            with patch.dict(
                    os.environ,
                    {"AUTOSLICE_STREAMER_PROFILE_OVERRIDES": str(Path(td) / "overrides.json")},
                    clear=False):
                with self.assertRaisesRegex(ValueError, "配置已变化"):
                    add_streamer_profile_replacement(
                        "zeyin",
                        "测试错词",
                        "测试正词",
                        expected_fingerprint="stale",
                    )


class StreamerResolutionTests(unittest.TestCase):

    def test_unknown_streamer_uses_generic_even_if_config_default_is_zeyin(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "profiles.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "default_profile_id": "zeyin",
                "profiles": [
                    {
                        "id": "generic",
                        "label": "通用主播",
                        "canonical_name": "主播",
                        "report_name": "主播",
                        "subtitle_glossary": ["SC"],
                    },
                    {
                        "id": "zeyin",
                        "label": "泽音 Melody",
                        "canonical_name": "泽音Melody",
                        "report_name": "音音",
                        "title_prefix": "【泽音】",
                        "path_keywords": ["泽音Melody"],
                        "subtitle_glossary": ["朱鹮", "音音"],
                        "asr_replacements": [["英英", "音音"]],
                        "outro_clip": {
                            "series_title": "晚安小音音",
                            "search_tail_sec": 1200,
                            "triggers": ["今天就直播到这里"],
                        },
                    },
                ],
            }, ensure_ascii=False), encoding="utf-8")

            plain = resolve_streamer_profile(
                "auto",
                r"X:\fixtures\普通投稿\剪映导出.mp4",
                config_path=path,
            )
            named = resolve_streamer_profile(
                "auto",
                r"X:\fixtures\未知主播-2026-08-17 20_00-歌杂.flv",
                config_path=path,
            )

        for profile in (plain, named):
            self.assertEqual(profile.id, "generic")
            self.assertEqual(profile.subtitle_glossary, ("SC",))
            self.assertEqual(profile.asr_replacements, ())
            self.assertIsNone(profile.outro_clip)
            self.assertNotIn("泽音", profile.title_prefix)
        self.assertEqual(plain.title_prefix, "")
        self.assertEqual(named.title_prefix, "【未知主播】")

    def test_parallel_workers_use_explicit_immutable_profile_snapshots(self):
        unknown = resolve_streamer_profile(
            "auto",
            r"X:\fixtures\未知主播-2026-08-17 20_00-歌杂.flv",
        )
        zeyin = resolve_streamer_profile("zeyin")

        def read_snapshot(profile):
            with streamer_profile_context(profile):
                active = current_streamer_profile()
                return (
                    active.id,
                    active.label,
                    active.title_prefix,
                    active.subtitle_glossary,
                    active.asr_replacements,
                    active.outro_clip,
                )

        with streamer_profile_context(zeyin):
            with ThreadPoolExecutor(max_workers=2) as executor:
                implicit = executor.submit(current_streamer_profile).result()
                explicit = list(executor.map(read_snapshot, (unknown, zeyin)))

        self.assertEqual(implicit.id, "generic")
        self.assertEqual(explicit[0][0], "generic")
        self.assertEqual(explicit[0][2], "【未知主播】")
        self.assertEqual(explicit[0][4], ())
        self.assertIsNone(explicit[0][5])
        self.assertEqual(explicit[1][0], "zeyin")
        self.assertEqual(explicit[1][2], "【泽音】")
        self.assertIn("朱鹮", explicit[1][3])
        self.assertIn(("英英", "音音"), explicit[1][4])
        self.assertIsNotNone(explicit[1][5])
        with self.assertRaises(FrozenInstanceError):
            unknown.title_prefix = "【被篡改】"


if __name__ == "__main__":
    unittest.main()
