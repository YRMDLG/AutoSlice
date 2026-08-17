import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import subtitle_workflow
from autoslice.llm import transport as llm_gateway
from autoslice.transcription import contracts as transcription_contracts
from subtitle_workflow import (
    DEFAULT_SUBTITLE_GLOSSARY,
    DEFAULT_SUBTITLE_STYLE,
    DEFAULT_VIDEO_EXPORT,
    EXACT_SUBTITLE_FONT,
    EXACT_SUBTITLE_FONT_RESOLVED,
    SUBTITLE_REVIEW_BATCH_SIZE,
    SUBTITLE_REVIEW_CONCURRENCY,
    _default_llm_runner,
    _nvenc_available,
    _split_cue_for_ass,
    build_ass_document,
    burn_subtitles,
    generate_subtitle_reference_titles,
    high_confidence_corrections,
    load_subtitle_edit_state,
    normalise_subtitle_style,
    normalise_video_export,
    parse_srt_document,
    reflow_subtitle_srt_for_display,
    render_subtitle_preview,
    save_corrected_srt,
    scan_submission_pairs,
    serialise_srt,
    normalise_subtitle_review_dictionary,
    suggest_subtitle_corrections,
    transcribe_submission_video,
    verify_exact_subtitle_font,
    write_ass_from_srt,
)


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:02,500 position:50%
音音晚上好

2
00:00:02,500 --> 00:00:05,000
我看到一个瓦衣
是兔女郎的瓦衣

3
00:00:05,000 --> 00:00:07,000
这个娃衣很特别
"""


def assert_same_path(testcase, actual, expected):
    """Windows CI 可能用 8.3 短路径表示同一个临时文件。"""

    actual_path = Path(actual)
    expected_path = Path(expected)
    if actual_path.exists() and expected_path.exists():
        testcase.assertTrue(
            actual_path.samefile(expected_path),
            f"路径不一致: {actual_path} != {expected_path}",
        )
        return
    if os.name == "nt":
        actual_suffix = _temporary_path_suffix(actual_path)
        expected_suffix = _temporary_path_suffix(expected_path)
        if actual_suffix is not None and expected_suffix is not None:
            testcase.assertEqual(actual_suffix, expected_suffix)
            return
    testcase.assertEqual(
        os.path.normcase(os.path.normpath(str(actual_path))),
        os.path.normcase(os.path.normpath(str(expected_path))),
    )


def _temporary_path_suffix(path):
    """提取临时目录后的部分，兼容 Windows 用户目录短路径。"""

    path_parts = tuple(part.casefold() for part in Path(path).parts)
    temp_parts = tuple(part.casefold() for part in Path(tempfile.gettempdir()).parts)
    for marker_size in range(min(3, len(temp_parts)), 0, -1):
        marker = temp_parts[-marker_size:]
        for index in range(len(path_parts) - marker_size + 1):
            if path_parts[index:index + marker_size] == marker:
                return path_parts[index + marker_size:]
    return None


class SubtitleParsingAndReviewTests(unittest.TestCase):
    def test_shared_srt_contracts_keep_object_identity(self):
        self.assertIs(subtitle_workflow.llm_gateway, llm_gateway)
        self.assertIs(
            subtitle_workflow.SubtitleCue,
            transcription_contracts.SubtitleCue,
        )
        self.assertIs(
            subtitle_workflow.DEFAULT_SUBTITLE_GLOSSARY,
            transcription_contracts.DEFAULT_SUBTITLE_GLOSSARY,
        )

    def test_parse_and_serialise_preserve_multiline_timeline_and_settings(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "字幕.srt"
            path.write_text(SAMPLE_SRT, encoding="utf-8")
            cues = parse_srt_document(path)

        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[1].text, "我看到一个瓦衣\n是兔女郎的瓦衣")
        self.assertEqual(cues[0].settings, " position:50%")
        rebuilt = serialise_srt(cues)
        self.assertIn("00:00:01,000 --> 00:00:02,500 position:50%", rebuilt)
        self.assertIn("我看到一个瓦衣\n是兔女郎的瓦衣", rebuilt)

    def test_gb18030_srt_is_supported(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "字幕.srt"
            path.write_bytes(SAMPLE_SRT.encode("gb18030"))
            cues = parse_srt_document(path)
        self.assertEqual(cues[0].text, "音音晚上好")

    def test_default_glossary_contains_requested_names_and_reaches_prompt(self):
        requested_terms = {
            "朱鹮", "猪獾", "泽音Melody", "泽音melody", "泽音", "音音",
            "音姐", "音妈", "露露", "四禧丸子", "沐霂", "又一", "梨安",
            "恬豆", "七海", "小孩梓", "阿梓", "柚恩", "露早", "EOE", "篮筐",
            "小沐标", "酥酥又", "向心梨", "恬豆包", "柚恩蜜", "gogo队",
            "小星星", "星瞳", "宣小纸", "真纸棒", "脆鲨",
        }
        prompts = []

        def runner(prompt, _compact_prompt):
            prompts.append(prompt)
            indices = json.loads(
                prompt.split("待检查序号：", 1)[1].split("\n", 1)[0]
            )
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "专名字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            result = suggest_subtitle_corrections(
                source,
                llm_runner=runner,
                use_cache=False,
            )

        self.assertTrue(requested_terms.issubset(DEFAULT_SUBTITLE_GLOSSARY))
        self.assertEqual(
            len(DEFAULT_SUBTITLE_GLOSSARY),
            len(set(DEFAULT_SUBTITLE_GLOSSARY)),
        )
        self.assertTrue(requested_terms.issubset(result["glossary"]))
        self.assertTrue(prompts[0].startswith("你是直播切片的字幕校对员。"))
        for term in requested_terms:
            self.assertIn(term, prompts[0])

    def test_extra_glossary_and_streamer_replacements_are_merged_and_applied(self):
        prompts = []

        def runner(prompt, _compact_prompt):
            prompts.append(prompt)
            indices = json.loads(
                prompt.split("待检查序号：", 1)[1].split("\n", 1)[0]
            )
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "主播字幕.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n英英晚上好\n",
                encoding="utf-8",
            )
            result = suggest_subtitle_corrections(
                source,
                glossary=["额外专名"],
                replacements=[("英英", "音音")],
                streamer_profile_id="zeyin",
                streamer_profile_label="泽音 Melody",
                llm_runner=runner,
                use_cache=False,
            )
            cached = suggest_subtitle_corrections(
                source,
                glossary=["额外专名"],
                replacements=[("英英", "音音")],
                streamer_profile_id="zeyin",
                streamer_profile_label="泽音 Melody",
                llm_runner=lambda *_: self.fail("固定映射缓存不应重新调用 AI"),
            )

        self.assertIn("朱鹮", result["glossary"])
        self.assertIn("额外专名", result["glossary"])
        self.assertEqual(result["replacements"], [["英英", "音音"]])
        self.assertEqual(result["streamer_profile_id"], "zeyin")
        self.assertEqual(result["replacement_count"], 1)
        self.assertIn('"错误词": "英英"', prompts[0])
        self.assertIn('"正确词": "音音"', prompts[0])
        self.assertIn("主动检查与优先词表发音相近", prompts[0])
        self.assertEqual(result["suggestions"][0]["corrected"], "音音晚上好")
        self.assertEqual(result["suggestions"][0]["confidence"], 1.0)
        self.assertEqual(result["suggestions"][0]["source"], "fixed_replacement")
        self.assertEqual(
            high_confidence_corrections(result)[0]["corrected"],
            "音音晚上好",
        )
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(
            high_confidence_corrections(cached)[0]["corrected"],
            "音音晚上好",
        )

    def test_review_cache_invalidates_when_replacements_or_profile_change(self):
        calls = []

        def runner(prompt, _compact_prompt):
            calls.append(prompt)
            indices = json.loads(
                prompt.split("待检查序号：", 1)[1].split("\n", 1)[0]
            )
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "主播字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            suggest_subtitle_corrections(
                source,
                replacements=[("英英", "音音")],
                streamer_profile_id="zeyin",
                llm_runner=runner,
            )
            suggest_subtitle_corrections(
                source,
                replacements=[("莹莹", "音音")],
                streamer_profile_id="zeyin",
                llm_runner=runner,
            )
            suggest_subtitle_corrections(
                source,
                replacements=[("莹莹", "音音")],
                streamer_profile_id="generic",
                llm_runner=runner,
            )

        self.assertEqual(len(calls), 3)

    def test_review_dictionary_normalisation_is_stable_and_deduplicated(self):
        glossary, replacements = normalise_subtitle_review_dictionary(
            ["音音", "额外专名", "额外专名"],
            [("英英", "音音"), ("英英", "音音")],
        )

        self.assertEqual(glossary.count("音音"), 1)
        self.assertEqual(glossary.count("额外专名"), 1)
        self.assertEqual(replacements, (("英英", "音音"),))

    def test_fixed_replacements_apply_longest_source_first(self):
        corrected, applied = subtitle_workflow._apply_fixed_replacements(
            "感谢音乐声们",
            (("音乐声", "音悦生"), ("音乐声们", "音悦生们")),
        )

        self.assertEqual(corrected, "感谢音悦生们")
        self.assertIn("音乐声们 → 音悦生们", applied)

    def test_reference_titles_use_two_stage_generation_and_final_judgement(self):
        calls = []

        def runner(prompt, compact_prompt, progress_label):
            calls.append((prompt, compact_prompt, progress_label))
            if len(calls) == 1:
                return {
                    "content_summary": "音音先说免费游戏，最后承认是在整朋友",
                    "hook": "免费诱饵和朋友上当的反差",
                    "candidates": [
                        {"title": "免费游戏送朋友，结果朋友真上当了😂"},
                        {"title": "朋友以为捡到免费游戏，最后才发现被骗"},
                        {"title": "拿免费游戏整朋友，音音自己先笑场了"},
                    ],
                }
            return {
                "recommended_title": "朋友以为白捡免费游戏，最后发现从头被骗😂",
                "reason": "同时保留免费诱饵和被骗结果",
                "alternatives": [
                    "免费游戏送上门，朋友玩到最后才发现是整蛊",
                    "音音拿免费游戏钓朋友，结果对方真信了",
                ],
            }

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "成片.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n"
                "这个游戏免费送给你\n\n"
                "2\n00:00:02,000 --> 00:00:05,000\n"
                "他居然真的信了哈哈哈\n",
                encoding="utf-8",
            )
            result = generate_subtitle_reference_titles(
                source,
                context_title="一起看",
                llm_runner=runner,
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("这个游戏免费送给你", calls[0][0])
        self.assertIn("候选标题", calls[1][0])
        self.assertEqual(
            result["recommended_title"],
            "朋友以为白捡免费游戏，最后发现从头被骗😂",
        )
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["cue_count"], 2)
        self.assertFalse(result["sampled"])

    def test_reference_title_long_subtitles_sample_the_whole_timeline(self):
        observed = []

        def runner(prompt, _compact_prompt, _progress_label):
            observed.append(prompt)
            if len(observed) == 1:
                return {
                    "content_summary": "覆盖整段事件",
                    "hook": "开头铺垫和结尾反转",
                    "candidates": [
                        {"title": "开头铺垫很认真，结尾突然翻车"},
                        {"title": "一路认真解释，最后一句把自己拆穿"},
                        {"title": "前面说得头头是道，结尾直接露馅"},
                    ],
                }
            return {
                "recommended_title": "前面说得头头是道，结尾一句直接露馅",
                "reason": "覆盖开头和结尾",
                "alternatives": [],
            }

        cue_blocks = []
        for index in range(1, 181):
            start = index - 1
            end = index
            cue_blocks.append(
                f"{index}\n00:{start // 60:02d}:{start % 60:02d},000 --> "
                f"00:{end // 60:02d}:{end % 60:02d},000\n"
                f"第{index}条" + "很长字幕" * 40
            )
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "长成片.srt"
            source.write_text("\n\n".join(cue_blocks), encoding="utf-8")
            result = generate_subtitle_reference_titles(
                source,
                llm_runner=runner,
            )

        self.assertTrue(result["sampled"])
        self.assertIn("第1条", observed[0])
        self.assertIn("第180条", observed[0])

    def test_invalid_or_reverse_timeline_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "字幕.srt"
            path.write_text(
                "1\n00:00:03,000 --> 00:00:02,000\n测试\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "结束时间"):
                parse_srt_document(path)

    def test_save_corrected_srt_keeps_source_and_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            source_before = source.read_bytes()
            output = save_corrected_srt(
                source,
                [{
                    "index": 2,
                    "original": "我看到一个瓦衣\n是兔女郎的瓦衣",
                    "corrected": "我看到一个娃衣\n是兔女郎的娃衣",
                }],
            )
            corrected = Path(output).read_text(encoding="utf-8")

            self.assertEqual(source.read_bytes(), source_before)
            self.assertIn("我看到一个娃衣\n是兔女郎的娃衣", corrected)
            self.assertIn("00:00:02,500 --> 00:00:05,000", corrected)
            self.assertTrue(output.endswith("_校对.srt"))

    def test_save_rejects_stale_original_and_unknown_index(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "原文已变化"):
                save_corrected_srt(
                    source,
                    [{"index": 1, "original": "旧原文", "corrected": "新文字"}],
                )
            with self.assertRaisesRegex(ValueError, "序号不存在"):
                save_corrected_srt(source, [{"index": 99, "corrected": "新文字"}])

    def test_save_corrected_srt_can_delete_cues_without_changing_source(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            source_before = source.read_bytes()
            output = save_corrected_srt(
                source,
                [{"index": 2, "corrected": "这是校对后的字幕"}],
                deleted_indices=[1, 3],
            )
            corrected_cues = parse_srt_document(output)
            source_after = source.read_bytes()

        self.assertEqual(source_after, source_before)
        self.assertEqual([cue.index for cue in corrected_cues], [2])
        self.assertEqual(corrected_cues[0].text, "这是校对后的字幕")
        self.assertEqual(corrected_cues[0].start, "00:00:02,500")

    def test_save_rejects_invalid_or_conflicting_subtitle_deletions(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            cases = (
                ([1, 1], "序号重复"),
                ([99], "序号不存在"),
                (["1"], "必须是整数"),
                ([1, 2, 3], "不能删除全部字幕"),
            )
            for deleted_indices, message in cases:
                with self.subTest(deleted_indices=deleted_indices):
                    with self.assertRaisesRegex(ValueError, message):
                        save_corrected_srt(
                            source,
                            [],
                            deleted_indices=deleted_indices,
                        )
            with self.assertRaisesRegex(ValueError, "不能同时修改和删除"):
                save_corrected_srt(
                    source,
                    [{"index": 1, "corrected": "新文字"}],
                    deleted_indices=[1],
                )

    def test_save_corrected_srt_merges_adjacent_cues_without_changing_source(self):
        source_text = (
            "1\n00:00:00,000 --> 00:00:01,000\n越来越\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n大了 还要我怎么样\n\n"
            "3\n00:00:02,000 --> 00:00:03,000\n下一句话\n"
        )
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(source_text, encoding="utf-8")
            source_before = source.read_bytes()
            output = save_corrected_srt(
                source,
                [{"index": 2, "corrected": "大了 还要我怎么办"}],
                merge_pairs=[{"first": 1, "second": 2}],
            )
            merged = parse_srt_document(output)
            source_after = source.read_bytes()

        self.assertEqual(source_after, source_before)
        self.assertEqual([cue.index for cue in merged], [1, 3])
        self.assertEqual(merged[0].start, "00:00:00,000")
        self.assertEqual(merged[0].end, "00:00:02,000")
        self.assertEqual(merged[0].text, "越来越大了 还要我怎么办")
        self.assertEqual(merged[1].text, "下一句话")

    def test_save_corrected_srt_merges_a_chain_and_applies_group_override(self):
        source_text = (
            "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n第二句\n\n"
            "3\n00:00:02,000 --> 00:00:03,000\n第三句\n"
        )
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(source_text, encoding="utf-8")
            output = save_corrected_srt(
                source,
                [],
                merge_pairs=[
                    {"first": 1, "second": 2},
                    {"first": 2, "second": 3},
                ],
                merge_overrides={"1": "整理后的完整句子"},
            )
            merged = parse_srt_document(output)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].index, 1)
        self.assertEqual(merged[0].start, "00:00:00,000")
        self.assertEqual(merged[0].end, "00:00:03,000")
        self.assertEqual(merged[0].text, "整理后的完整句子")

    def test_save_corrected_srt_applies_and_restores_manual_timing(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            output = save_corrected_srt(
                source,
                [{"index": 1, "corrected": "音音你好"}],
                time_overrides={"1": {"start": 1.2, "end": 2.3}},
            )
            corrected = parse_srt_document(output)
            edit_state = load_subtitle_edit_state(source)

            self.assertEqual(corrected[0].start, "00:00:01,200")
            self.assertEqual(corrected[0].end, "00:00:02,300")
            self.assertEqual(
                edit_state["time_overrides"],
                {"1": {"start": 1.2, "end": 2.3}},
            )
            self.assertEqual(edit_state["corrections"][0]["corrected"], "音音你好")
            self.assertTrue(Path(edit_state["state_path"]).is_file())

            source.write_text(SAMPLE_SRT.replace("音音晚上好", "音音早上好"), encoding="utf-8")
            self.assertIsNone(load_subtitle_edit_state(source))

    def test_manual_timing_validates_group_root_and_timeline_order(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            cases = (
                ({"1": {"start": 2.0, "end": 1.0}}, None, "结束时间"),
                ({"2": {"start": 0.5, "end": 1.5}}, None, "时间轴会倒序"),
                (
                    {"2": {"start": 2.5, "end": 4.5}},
                    [{"first": 1, "second": 2}],
                    "组首条",
                ),
            )
            for timings, pairs, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        save_corrected_srt(
                            source,
                            [],
                            merge_pairs=pairs,
                            time_overrides=timings,
                        )
            with self.assertRaisesRegex(ValueError, "已删除字幕"):
                save_corrected_srt(
                    source,
                    [],
                    deleted_indices=[1],
                    time_overrides={"1": {"start": 1.0, "end": 2.0}},
                )

    def test_save_corrected_srt_rejects_invalid_merge_relationships(self):
        source_text = (
            "1\n00:00:00,000 --> 00:00:01,000\n第一条\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n第二条\n\n"
            "3\n00:00:02,000 --> 00:00:03,000\n第三条\n"
        )
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(source_text, encoding="utf-8")
            cases = (
                ([{"first": 1, "second": 3}], None, "相邻"),
                ([{"first": 1, "second": 2}], {"2": "错误"}, "组首条"),
                ([{"first": 1, "second": 2}], {"3": "错误"}, "没有对应"),
            )
            for merge_pairs, merge_overrides, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        save_corrected_srt(
                            source,
                            [],
                            merge_pairs=merge_pairs,
                            merge_overrides=merge_overrides,
                        )
            with self.assertRaisesRegex(ValueError, "已删除字幕"):
                save_corrected_srt(
                    source,
                    [],
                    deleted_indices=[2],
                    merge_pairs=[{"first": 1, "second": 2}],
                )

    def test_scan_pairs_different_jianying_names_and_ignores_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            clip = root / "投稿标题"
            clip.mkdir()
            (clip / "7月16日 (1).mp4").write_bytes(b"video")
            (clip / "7月16日 (2).srt").write_text(SAMPLE_SRT, encoding="utf-8")
            (clip / "7月16日 (1)_字幕版.mp4").write_bytes(b"output")
            (clip / "7月16日 (2)_校对.srt").write_text(SAMPLE_SRT, encoding="utf-8")

            pairs = scan_submission_pairs(root)

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["title"], "投稿标题")
        self.assertEqual(pairs[0]["cue_count"], 3)
        self.assertTrue(pairs[0]["video_path"].endswith("7月16日 (1).mp4"))
        for field in (
                "folder_created_at", "folder_modified_at",
                "source_created_at", "source_modified_at"):
            self.assertIsInstance(pairs[0][field], float)
            self.assertGreater(pairs[0][field], 0)

    def test_scan_includes_video_without_srt_until_transcription_finishes(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "待识别投稿"
            folder.mkdir()
            video = folder / "精剪成片.mp4"
            video.write_bytes(b"video")

            before = scan_submission_pairs(td)
            expected_id = before[0]["id"]
            expected_srt = video.with_suffix(".srt")
            expected_srt.write_text(SAMPLE_SRT, encoding="utf-8")
            after = scan_submission_pairs(td)

        self.assertEqual(len(before), 1)
        self.assertFalse(before[0]["has_source_srt"])
        self.assertTrue(before[0]["needs_transcription"])
        self.assertEqual(before[0]["srt_path"], str(expected_srt))
        self.assertEqual(before[0]["cue_count"], 0)
        self.assertEqual(after[0]["id"], expected_id)
        self.assertTrue(after[0]["has_source_srt"])
        self.assertFalse(after[0]["needs_transcription"])
        self.assertEqual(after[0]["cue_count"], 3)

    def test_reflow_long_subtitles_keeps_source_and_scanner_prefers_working_copy(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / "待校对投稿"
            folder.mkdir()
            video = folder / "精剪成片.mp4"
            source = video.with_suffix(".srt")
            video.write_bytes(b"video")
            source_text = "这是需要整理为适合二十号字幕显示的超长字幕内容" * 3
            source.write_text(
                f"1\n00:00:00,000 --> 00:00:06,000\n{source_text}\n",
                encoding="utf-8",
            )
            source_before = source.read_bytes()

            result = reflow_subtitle_srt_for_display(source)
            reflowed = Path(result["srt_path"])
            reflowed_cues = parse_srt_document(reflowed)
            pairs = scan_submission_pairs(td)
            corrected = Path(result["corrected_srt_path"])
            corrected.write_text(reflowed.read_text(encoding="utf-8"), encoding="utf-8")
            protected_pairs = scan_submission_pairs(td)
            source_after = source.read_bytes()

        self.assertEqual(source_after, source_before)
        self.assertTrue(reflowed.name.endswith("_排版.srt"))
        self.assertEqual("".join(cue.text for cue in reflowed_cues), source_text)
        self.assertTrue(all(
            len("".join(cue.text.split())) <= result["max_chars"]
            for cue in reflowed_cues
        ))
        self.assertTrue(all(
            later.start_seconds >= earlier.end_seconds
            for earlier, later in zip(reflowed_cues, reflowed_cues[1:])
        ))
        assert_same_path(self, pairs[0]["srt_path"], reflowed)
        assert_same_path(self, pairs[0]["raw_srt_path"], source)
        self.assertTrue(pairs[0]["is_reflowed_srt"])
        self.assertTrue(pairs[0]["can_reflow_srt"])
        self.assertFalse(protected_pairs[0]["can_reflow_srt"])

    def test_reflow_rebalances_short_clause_tail_between_adjacent_cues(self):
        source_text = (
            "1\n00:00:00,000 --> 00:00:02,000\n我我这几天头已经越来越\n\n"
            "2\n00:00:02,800 --> 00:00:04,000\n大了 还要我怎么样\n\n"
            "3\n00:00:04,100 --> 00:00:05,000\n唉 什么标题啊\n"
        )
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(source_text, encoding="utf-8")
            result = reflow_subtitle_srt_for_display(source)
            cues = parse_srt_document(result["srt_path"])

        self.assertEqual(cues[0].text, "我我这几天头已经")
        self.assertEqual(cues[1].text, "越来越大了 还要我怎么样")
        self.assertEqual(cues[2].text, "唉 什么标题啊")
        self.assertTrue(all(
            len("".join(cue.text.split())) <= result["max_chars"]
            for cue in cues
        ))

    def test_reflow_recovers_unknown_short_tail_but_keeps_sentence_connectors(self):
        source_text = (
            "1\n00:00:00,000 --> 00:00:02,000\n今天直播状态特别引人不\n\n"
            "2\n00:00:02,100 --> 00:00:03,500\n适 后面还是正常内容\n\n"
            "3\n00:00:03,600 --> 00:00:05,000\n今天真的非常开心\n\n"
            "4\n00:00:05,100 --> 00:00:06,500\n所以 接下来继续聊天\n"
        )
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(source_text, encoding="utf-8")
            result = reflow_subtitle_srt_for_display(source)
            cues = parse_srt_document(result["srt_path"])

        self.assertEqual(cues[0].text, "今天直播状态特别")
        self.assertEqual(cues[1].text, "引人不适 后面还是正常内容")
        self.assertEqual(cues[2].text, "今天真的非常开心")
        self.assertEqual(cues[3].text, "所以 接下来继续聊天")

    def test_transcription_cleans_checkpoint_only_after_valid_srt(self):
        with tempfile.TemporaryDirectory() as td:
            video = Path(td) / "精剪成片.mp4"
            video.write_bytes(b"video")
            checkpoint = video.with_name(f"{video.stem}_asr_checkpoint.json")
            observed_call = {}

            def fake_ensure(
                    video_path, progress_callback=None, checkpoint_path=None,
                    foreground_only=False):
                observed_call.update({
                    "checkpoint_path": checkpoint_path,
                    "foreground_only": foreground_only,
                })
                Path(checkpoint_path).write_text(json.dumps({
                    "status": "completed",
                    "foreground_filter_mode": "speaker_diarization",
                    "speaker_filtered_segment_count": 2,
                    "speaker_filtered_chunk_count": 1,
                }), encoding="utf-8")
                srt = Path(video_path).with_suffix(".srt")
                srt.write_text(SAMPLE_SRT, encoding="utf-8")
                return str(srt)

            result = transcribe_submission_video(
                video,
                transcription_service=fake_ensure,
            )

            srt_exists = Path(result["srt_path"]).is_file()
            checkpoint_exists = checkpoint.exists()

        self.assertEqual(result["cue_count"], 3)
        self.assertTrue(srt_exists)
        self.assertFalse(checkpoint_exists)
        assert_same_path(
            self,
            observed_call["checkpoint_path"],
            checkpoint,
        )
        self.assertTrue(observed_call["foreground_only"])
        self.assertEqual(result["background_filter"], {
            "enabled": True,
            "mode": "speaker_diarization",
            "speaker_filtered_segment_count": 2,
            "speaker_filtered_chunk_count": 1,
        })

    def test_transcription_failure_keeps_checkpoint_for_resume(self):
        with tempfile.TemporaryDirectory() as td:
            video = Path(td) / "精剪成片.mp4"
            video.write_bytes(b"video")
            checkpoint = video.with_name(f"{video.stem}_asr_checkpoint.json")

            def fail_with_checkpoint(
                    _video_path, progress_callback=None, checkpoint_path=None,
                    foreground_only=False):
                Path(checkpoint_path).write_text('{"status":"failed"}', encoding="utf-8")
                raise RuntimeError("转录中断")

            with self.assertRaisesRegex(RuntimeError, "转录中断"):
                transcribe_submission_video(
                    video,
                    transcription_service=fail_with_checkpoint,
                )
            checkpoint_exists = checkpoint.exists()

        self.assertTrue(checkpoint_exists)

    def test_transcription_requires_explicit_service_injection(self):
        with tempfile.TemporaryDirectory() as td:
            video = Path(td) / "精剪成片.mp4"
            video.write_bytes(b"video")

            with self.assertRaisesRegex(ValueError, "显式注入"):
                transcribe_submission_video(video)

    def test_review_retries_incomplete_batch_filters_rewrite_and_caches(self):
        calls = []

        def fake_runner(prompt, compact_prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {"reviewed_indices": [1, 2], "corrections": []}
            return {
                "reviewed_indices": [1, 2, 3],
                "corrections": [
                    {
                        "index": 1,
                        "original": "音音晚上好",
                        "corrected": "音音，晚上好！",
                        "reason": "只改标点",
                        "confidence": 0.99,
                    },
                    {
                        "index": 2,
                        "original": "我看到一个瓦衣\n是兔女郎的瓦衣",
                        "corrected": "我看到一个娃衣\n是兔女郎的娃衣",
                        "reason": "结合后一句‘娃衣’确认同音误识别",
                        "confidence": 0.97,
                    },
                    {
                        "index": 3,
                        "original": "这个娃衣很特别",
                        "corrected": "她觉得这一套兔女郎服装很独特",
                        "reason": "润色",
                        "confidence": 0.92,
                    },
                ],
            }

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            result = suggest_subtitle_corrections(
                source,
                context_title="兔女郎娃衣",
                llm_runner=fake_runner,
            )
            cached = suggest_subtitle_corrections(
                source,
                context_title="兔女郎娃衣",
                llm_runner=lambda *_: self.fail("命中缓存后不应调用 AI"),
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual([item["index"] for item in result["suggestions"]], [2])
        self.assertEqual(result["suggestions"][0]["corrected"], "我看到一个娃衣\n是兔女郎的娃衣")
        self.assertFalse(result["cache_hit"])
        self.assertTrue(cached["cache_hit"])

    def test_review_uses_small_batches_for_reasoning_model(self):
        calls = []
        active_calls = 0
        peak_calls = 0
        lock = threading.Lock()
        first_wave = threading.Barrier(2)

        def runner(prompt, _compact_prompt):
            nonlocal active_calls, peak_calls
            self.assertIn("不能只因视频标题或优先词表", prompt)
            encoded_indices = prompt.split("待检查序号：", 1)[1].split("\n", 1)[0]
            indices = json.loads(encoded_indices)
            with lock:
                calls.append(indices)
                active_calls += 1
                peak_calls = max(peak_calls, active_calls)
            try:
                if len(indices) == SUBTITLE_REVIEW_BATCH_SIZE:
                    first_wave.wait(timeout=2)
                return {"reviewed_indices": indices, "corrections": []}
            finally:
                with lock:
                    active_calls -= 1

        cues = []
        for index in range(1, 66):
            start = index - 1
            cues.append(
                f"{index}\n"
                f"00:{start // 60:02d}:{start % 60:02d},000 --> "
                f"00:{start // 60:02d}:{start % 60:02d},900\n"
                f"第{index}条字幕"
            )
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "长字幕.srt"
            source.write_text("\n\n".join(cues), encoding="utf-8")
            suggest_subtitle_corrections(
                source,
                llm_runner=runner,
                use_cache=False,
            )

        self.assertEqual(SUBTITLE_REVIEW_BATCH_SIZE, 30)
        self.assertEqual(SUBTITLE_REVIEW_CONCURRENCY, 2)
        self.assertEqual(sorted(len(indices) for indices in calls), [5, 30, 30])
        self.assertEqual(sorted(index for batch in calls for index in batch), list(range(1, 66)))
        self.assertEqual(peak_calls, 2)

    def test_default_review_runner_reserves_reasoning_output_budget(self):
        response = '{"reviewed_indices":[],"corrections":[]}'
        with patch(
            "autoslice.llm.transport.call_llm_with_retry",
            return_value=response,
        ) as call:
            payload = _default_llm_runner("完整提示", "紧凑提示")

        self.assertEqual(payload["reviewed_indices"], [])
        kwargs = call.call_args.kwargs
        self.assertGreaterEqual(kwargs["max_tokens"], 12000)
        self.assertGreaterEqual(kwargs["compact_max_tokens"], 12000)

    def test_default_parallel_batches_share_one_provider_retry_coordinator(self):
        cue_blocks = []
        for index in range(1, 36):
            second = index - 1
            cue_blocks.append(
                f"{index}\n"
                f"00:00:{second:02d},000 --> 00:00:{second:02d},900\n"
                f"第{index}条字幕"
            )
        coordinator = object()
        observed_coordinators = []

        def fake_call(prompt, **kwargs):
            observed_coordinators.append(kwargs.get("retry_coordinator"))
            indices = json.loads(
                prompt.split("待检查序号：", 1)[1].split("\n", 1)[0]
            )
            return json.dumps({
                "reviewed_indices": indices,
                "corrections": [],
            }, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "并发字幕.srt"
            source.write_text("\n\n".join(cue_blocks), encoding="utf-8")
            with (
                patch(
                    "autoslice.llm.transport.LLMProviderRetryCoordinator",
                    return_value=coordinator,
                ) as coordinator_factory,
                patch(
                    "autoslice.llm.transport.call_llm_with_retry",
                    side_effect=fake_call,
                ) as call,
            ):
                result = suggest_subtitle_corrections(
                    source,
                    use_cache=False,
                )

        self.assertEqual(result["suggestions"], [])
        self.assertEqual(coordinator_factory.call_count, 1)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(observed_coordinators, [coordinator, coordinator])

    def test_custom_review_runner_does_not_create_provider_coordinator(self):
        def custom_runner(prompt, _compact_prompt):
            indices = json.loads(
                prompt.split("待检查序号：", 1)[1].split("\n", 1)[0]
            )
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            with patch(
                "autoslice.llm.transport.LLMProviderRetryCoordinator",
                side_effect=AssertionError("自定义 runner 不应创建内置协调器"),
            ):
                result = suggest_subtitle_corrections(
                    source,
                    llm_runner=custom_runner,
                    use_cache=False,
                )

        self.assertEqual(result["suggestions"], [])

    def test_review_cache_invalidates_when_source_changes(self):
        calls = []

        def runner(prompt, compact_prompt):
            calls.append(1)
            indices = json.loads(prompt.split("待检查序号：", 1)[1].split("\n", 1)[0])
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            suggest_subtitle_corrections(source, llm_runner=runner)
            source.write_text(SAMPLE_SRT.replace("很特别", "非常特别"), encoding="utf-8")
            suggest_subtitle_corrections(source, llm_runner=runner)

        self.assertEqual(len(calls), 2)

    def test_review_retries_when_corrections_shape_is_invalid(self):
        calls = []

        def runner(prompt, _compact_prompt):
            indices = json.loads(prompt.split("待检查序号：", 1)[1].split("\n", 1)[0])
            calls.append(indices)
            if len(calls) == 1:
                return {"reviewed_indices": indices, "corrections": {}}
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            result = suggest_subtitle_corrections(
                source,
                llm_runner=runner,
                use_cache=False,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(result["suggestions"], [])

    def test_malformed_matching_cache_is_ignored_and_rebuilt(self):
        calls = []

        def runner(prompt, _compact_prompt):
            calls.append(1)
            indices = json.loads(prompt.split("待检查序号：", 1)[1].split("\n", 1)[0])
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            first = suggest_subtitle_corrections(source, llm_runner=runner)
            cache_path = Path(first["cache_path"])
            malformed = json.loads(cache_path.read_text(encoding="utf-8"))
            malformed["suggestions"] = "不是建议数组"
            cache_path.write_text(
                json.dumps(malformed, ensure_ascii=False),
                encoding="utf-8",
            )

            rebuilt = suggest_subtitle_corrections(source, llm_runner=runner)

        self.assertEqual(len(calls), 2)
        self.assertFalse(rebuilt["cache_hit"])

    def test_force_review_failure_preserves_last_valid_cache(self):
        def successful_runner(prompt, _compact_prompt):
            indices = json.loads(prompt.split("待检查序号：", 1)[1].split("\n", 1)[0])
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            suggest_subtitle_corrections(source, llm_runner=successful_runner)

            with self.assertRaisesRegex(RuntimeError, "重新检查失败"):
                suggest_subtitle_corrections(
                    source,
                    llm_runner=lambda *_: (_ for _ in ()).throw(RuntimeError("重新检查失败")),
                    use_cache=False,
                )

            cached = suggest_subtitle_corrections(
                source,
                llm_runner=lambda *_: self.fail("失败重检不应破坏旧缓存"),
            )

        self.assertTrue(cached["cache_hit"])

    def test_review_aborts_if_source_changes_during_ai_request(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")

            def runner(prompt, _compact_prompt):
                indices = json.loads(prompt.split("待检查序号：", 1)[1].split("\n", 1)[0])
                source.write_text(
                    SAMPLE_SRT.replace("这个娃衣很特别", "这个娃衣非常特别"),
                    encoding="utf-8",
                )
                return {"reviewed_indices": indices, "corrections": []}

            with self.assertRaisesRegex(RuntimeError, "检查期间已变化"):
                suggest_subtitle_corrections(
                    source,
                    llm_runner=runner,
                    use_cache=False,
                )

            self.assertFalse((source.parent / "字幕_字幕校对建议.json").exists())

    def test_concurrent_review_cache_writes_are_atomic(self):
        barrier = threading.Barrier(2)

        def runner(prompt, _compact_prompt):
            indices = json.loads(prompt.split("待检查序号：", 1)[1].split("\n", 1)[0])
            barrier.wait(timeout=2)
            return {"reviewed_indices": indices, "corrections": []}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "字幕.srt"
            source.write_text(SAMPLE_SRT, encoding="utf-8")
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        suggest_subtitle_corrections,
                        source,
                        llm_runner=runner,
                        use_cache=False,
                    )
                    for _ in range(2)
                ]
                results = [future.result(timeout=5) for future in futures]

            cache_payload = json.loads(
                Path(results[0]["cache_path"]).read_text(encoding="utf-8")
            )
            leftovers = list(source.parent.glob("*.tmp"))

        self.assertEqual(cache_payload["suggestions"], [])
        self.assertEqual(leftovers, [])

    def test_high_confidence_only_selects_default_safe_items(self):
        selected = high_confidence_corrections({
            "suggestions": [
                {
                    "index": 1,
                    "confidence": 0.96,
                    "original": "看到瓦衣",
                    "corrected": "看到娃衣",
                },
                {
                    "index": 2,
                    "confidence": 0.99,
                    "original": "兔女郎瓦瓦衣",
                    "corrected": "兔女郎娃衣",
                },
                {
                    "index": 3,
                    "confidence": 0.72,
                    "original": "叉上",
                    "corrected": "X上",
                },
                {
                    "index": 4,
                    "confidence": 0.99,
                    "original": "今天真的非常开心",
                    "corrected": "昨天其实特别难过",
                },
            ]
        })
        self.assertEqual([item["index"] for item in selected], [1])


class SubtitleRenderingTests(unittest.TestCase):
    @staticmethod
    def _make_video(path, duration=2):
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c=0x243044:s=640x360:d={duration}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-shortest", str(path),
            ],
            check=True,
        )

    def test_default_style_is_exact_user_requested_jianying_style(self):
        self.assertEqual(DEFAULT_SUBTITLE_STYLE, {
            "font_name": "Noto Sans S Chinese Black",
            "font_size": 20.0,
            "font_color": "ffffff",
            "outline_color": "d06e95",
            "outline_width": 100.0,
            "x": 0.0,
            "y": -788.0,
            "shadow": 0.0,
        })
        with self.assertRaisesRegex(ValueError, "字体必须"):
            normalise_subtitle_style({"font_name": "Noto Sans SC"})

    def test_default_video_export_matches_jianying_settings(self):
        self.assertEqual(normalise_video_export(), {
            "width": 1920,
            "height": 1080,
            "bitrate_kbps": 8000,
            "rate_control": "vbr",
            "codec": "h264",
            "container": "mp4",
            "fps": 60.0,
            "color_space": "bt709",
            "color_range": "tv",
            "audio": "copy",
        })
        self.assertEqual(DEFAULT_VIDEO_EXPORT["bitrate_kbps"], 8000)

    def test_nvenc_probe_uses_supported_frame_dimensions(self):
        _nvenc_available.cache_clear()
        try:
            with patch("subtitle_workflow.subprocess.run") as run:
                run.return_value.returncode = 0
                self.assertTrue(_nvenc_available())

            command = run.call_args.args[0]
            source = command[command.index("-i") + 1]
            self.assertIn("s=320x180", source)
        finally:
            _nvenc_available.cache_clear()

    def test_ass_maps_style_color_position_and_escapes_text(self):
        cues = [
            parse_srt_document_from_text(
                "1\n00:00:00,000 --> 00:00:01,000\n测试{样式}\\路径\n第二行\n"
            )[0]
        ]
        document = build_ass_document(cues, 1920, 1080)
        self.assertIn(f"Style: Default,{EXACT_SUBTITLE_FONT},135.0", document)
        self.assertIn("&H00FFFFFF", document)
        self.assertIn("&H00956ED0", document)
        self.assertIn(",5.33,0.0,5,", document)
        self.assertIn(r"{\an5\pos(960,966)}", document)
        self.assertIn(r"测试\{样式\}\\路径 第二行", document)

    def test_ass_splits_oversized_cue_into_safe_sequential_events(self):
        source_text = "超长字幕必须拆分避免越过画面边缘" * 4
        cue = parse_srt_document_from_text(
            f"1\n00:00:00,000 --> 00:00:12,000\n{source_text}\n"
        )[0]

        document = build_ass_document([cue], 1920, 1080)
        events = [
            line for line in document.splitlines()
            if line.startswith("Dialogue:")
        ]
        event_texts = [line.rsplit("}", 1)[-1] for line in events]

        self.assertGreater(len(events), 1)
        self.assertEqual("".join(event_texts), source_text)
        self.assertTrue(all(len(text) <= 13 for text in event_texts))
        self.assertTrue(all(r"{\an5\pos(960,966)}" in event for event in events))

        document_4x3 = build_ass_document([cue], 1440, 1080)
        events_4x3 = [
            line for line in document_4x3.splitlines()
            if line.startswith("Dialogue:")
        ]
        event_texts_4x3 = [line.rsplit("}", 1)[-1] for line in events_4x3]

        self.assertGreater(len(events_4x3), len(events))
        self.assertEqual("".join(event_texts_4x3), source_text)
        self.assertTrue(all(len(text) <= 9 for text in event_texts_4x3))

    def test_ass_split_keeps_very_short_valid_cue_inside_original_interval(self):
        source_text = "极短时间内也要依次显示完整字幕" * 5
        cue = parse_srt_document_from_text(
            f"1\n00:00:10,000 --> 00:00:10,050\n{source_text}\n"
        )[0]

        events = _split_cue_for_ass(cue, 9)

        self.assertGreater(len(events), 1)
        self.assertEqual(events[0][0], 10.0)
        self.assertEqual(events[-1][1], 10.05)
        self.assertEqual("".join(event[2] for event in events), source_text)
        self.assertTrue(all(
            10.0 <= start < end <= 10.05
            for start, end, _text in events
        ))
        self.assertTrue(all(
            later[0] >= earlier[1]
            for earlier, later in zip(events, events[1:])
        ))

    def test_exact_font_resolves_to_noto_sans_hans_black(self):
        verify_exact_subtitle_font.cache_clear()
        result = verify_exact_subtitle_font()
        self.assertEqual(result["requested"], EXACT_SUBTITLE_FONT)
        self.assertEqual(result["expected_resolved"], EXACT_SUBTITLE_FONT_RESOLVED)
        self.assertIsInstance(result["available"], bool)
        if result["available"]:
            self.assertEqual(result["resolved"], EXACT_SUBTITLE_FONT_RESOLVED)

    def test_write_ass_saves_style_without_touching_srt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "clip.mp4"
            srt = root / "clip_校对.srt"
            self._make_video(video)
            srt.write_text(SAMPLE_SRT, encoding="utf-8")
            original = srt.read_bytes()

            result = write_ass_from_srt(srt, video)

            self.assertEqual(srt.read_bytes(), original)
            self.assertTrue(Path(result["ass_path"]).is_file())
            self.assertTrue(Path(result["style_path"]).is_file())
            self.assertIn(EXACT_SUBTITLE_FONT, Path(result["ass_path"]).read_text(encoding="utf-8"))
            style = json.loads(Path(result["style_path"]).read_text(encoding="utf-8"))
            self.assertEqual(style, DEFAULT_SUBTITLE_STYLE)

    def test_preview_and_software_burn_produce_valid_media(self):
        verify_exact_subtitle_font.cache_clear()
        if not verify_exact_subtitle_font()["available"]:
            self.skipTest("需要本机安装指定的 Noto Sans S Chinese Black 字体")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "clip.mp4"
            srt = root / "clip_校对.srt"
            output = root / "clip_字幕版.mp4"
            self._make_video(video)
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:01,900\n音音字幕预览\n",
                encoding="utf-8",
            )

            fast_export = {
                "width": 640,
                "height": 360,
                "fps": 30,
                "bitrate_kbps": 1200,
            }
            jpeg, selected_time = render_subtitle_preview(
                video,
                srt,
                export_settings=fast_export,
            )
            result = burn_subtitles(
                video,
                srt,
                output_path=output,
                encoder="libx264",
                export_settings=fast_export,
            )
            decode = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
                    "-i", str(output), "-f", "null", os.devnull,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertTrue(jpeg.startswith(b"\xff\xd8"))
            self.assertGreater(len(jpeg), 1000)
            self.assertAlmostEqual(selected_time, 0.95, places=2)
            self.assertTrue(output.is_file())
            self.assertEqual(result["encoder"], "libx264")
            self.assertTrue(result["output_video_info"]["has_audio"])
            self.assertEqual(result["output_video_info"]["width"], 640)
            self.assertEqual(result["output_video_info"]["height"], 360)
            self.assertAlmostEqual(result["output_video_info"]["fps"], 30, places=2)
            self.assertEqual(result["output_video_info"]["color_space"], "bt709")
            self.assertEqual(result["output_video_info"]["color_transfer"], "bt709")
            self.assertEqual(result["output_video_info"]["color_primaries"], "bt709")
            self.assertLess(
                abs(result["output_video_info"]["duration"] - 2.0),
                0.2,
            )
            self.assertEqual(decode.returncode, 0, decode.stderr.decode("utf-8", errors="replace"))
            self.assertFalse((root / "clip_字幕版.part.mp4").exists())


def parse_srt_document_from_text(text):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "inline.srt"
        path.write_text(text, encoding="utf-8")
        return parse_srt_document(path)


if __name__ == "__main__":
    unittest.main()
