"""AutoCover 两阶段 AI 封面文案 owner 测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autoslice_cover.copy_recommendations import (
    generate_copy_recommendations,
    resolve_task_srt_path,
    validate_candidate_payload,
)


def _candidate_payload() -> dict[str, object]:
    return {
        "candidates": [
            {
                "label": "后果优先",
                "reason": "先写送礼，再落到免费游戏反转",
                "template_key": "headline",
                "palette_key": "latest_conflict",
                "evidence_quotes": ["朋友说要送我一个大作"],
                "lines": [
                    {"text": "朋友说送我大作", "role": "context"},
                    {"text": "点开竟是免费游戏", "role": "emphasis"},
                ],
            },
            {
                "label": "原话反差",
                "reason": "保留双方身份与最后一句原话",
                "template_key": "dialog",
                "palette_key": "latest_cyan",
                "evidence_quotes": ["点开以后她说这游戏本来就免费"],
                "lines": [
                    {"text": "朋友神秘送礼", "role": "context"},
                    {"text": "她说这可是大作", "role": "quote"},
                    {"text": "结果根本不要钱", "role": "emphasis"},
                ],
            },
            {
                "label": "双角色对话",
                "reason": "用两种角色色区分朋友与主播",
                "template_key": "evidence",
                "palette_key": "latest_soft",
                "evidence_quotes": ["朋友说要送我一个大作", "点开以后她说这游戏本来就免费"],
                "lines": [
                    {"text": "朋友说送你一个游戏", "role": "context"},
                    {"text": "朋友：绝对是大作", "role": "quote"},
                    {"text": "主播：它不是免费的吗", "role": "neutral"},
                    {"text": "送礼送了个寂寞", "role": "emphasis"},
                ],
            },
        ]
    }


def _review_payload(selected_index: int = 1) -> dict[str, object]:
    return {
        "selected_index": selected_index,
        "reviews": [
            {
                "candidate_index": index,
                "original_accuracy": "pass",
                "hook_consequence": "pass",
                "clickability_score": 5 - index,
                "reason": f"候选 {index + 1} 与字幕一致",
            }
            for index in range(3)
        ],
    }


class CopyRecommendationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.video = (self.root / "测试主播-2026-08-24-切片.mp4").resolve()
        self.video.write_bytes(b"video")
        self.srt = self.video.with_suffix(".srt")
        self.srt.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\n朋友说要送我一个大作\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\n点开以后她说这游戏本来就免费\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _generate_with_review(self, review: object) -> object:
        calls = 0

        def runner(_prompt: str, **_kwargs: object) -> str:
            nonlocal calls
            calls += 1
            payload = _candidate_payload() if calls == 1 else review
            return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)

        return generate_copy_recommendations(
            video_path=self.video,
            current_title="【测试主播】朋友说送大作，结果点开是免费游戏",
            editorial_interest_reason="具体诱因和免费游戏后果形成完整反差",
            corrected_srt_path=self.srt,
            runner=runner,
        )

    def test_two_stage_generation_uses_injected_runner_and_stage_models(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def runner(prompt: str, **kwargs: object) -> str:
            calls.append((prompt, kwargs))
            payload = _candidate_payload() if len(calls) == 1 else _review_payload(1)
            return json.dumps(payload, ensure_ascii=False)

        result = generate_copy_recommendations(
            video_path=self.video,
            current_title="【测试主播】朋友说送大作，结果点开是免费游戏",
            publish_title="【测试主播】朋友说要送我一款大作，点开却是免费游戏",
            editorial_interest_reason="具体诱因和免费游戏后果形成完整反差",
            corrected_srt_path=self.srt,
            runner=runner,
            environ={
                "AUTOSLICE_ANALYSIS_MODEL": "analysis-test-model",
                "AUTOSLICE_LLM_MODEL": "review-primary-model",
                "AUTOSLICE_LLM_REVIEW_MODEL": "review-secondary-model",
            },
        )

        self.assertEqual(result.source, "ai")
        self.assertEqual(result.selected_index, 1)
        self.assertEqual(len(result.candidates), 3)
        self.assertEqual([call[1]["reasoning_stage"] for call in calls], ["analysis", "review"])
        self.assertEqual(calls[0][1]["model_override"], "analysis-test-model")
        self.assertEqual(calls[1][1]["model_override"], "review-primary-model")
        self.assertTrue(all(call[1]["require_json"] for call in calls))
        self.assertIn("最终校对字幕", calls[0][0])
        self.assertIn("免费游戏后果形成完整反差", calls[0][0])
        self.assertIn("测试主播", calls[0][0])
        self.assertNotIn(str(self.srt), calls[0][0])
        self.assertNotIn(str(self.video), calls[1][0])

    def test_review_prompt_lists_all_candidates_and_uses_concrete_status_values(self) -> None:
        prompts: list[str] = []

        def runner(prompt: str, **_kwargs: object) -> str:
            prompts.append(prompt)
            payload = _candidate_payload() if len(prompts) == 1 else _review_payload(0)
            return json.dumps(payload, ensure_ascii=False)

        result = generate_copy_recommendations(
            video_path=self.video,
            current_title="当前标题",
            corrected_srt_path=self.srt,
            runner=runner,
        )

        self.assertEqual(result.source, "ai")
        review_prompt = prompts[1]
        self.assertEqual(review_prompt.count('"candidate_index":'), 3)
        for index in range(3):
            self.assertIn(f'"candidate_index": {index}', review_prompt)
        self.assertIn('"original_accuracy": "pass"', review_prompt)
        self.assertIn('"hook_consequence": "pass"', review_prompt)
        self.assertIn("只允许使用具体字符串 pass 或 fail", review_prompt)
        self.assertIn("reason 必须是非空字符串且不超过 120 字", review_prompt)
        self.assertIn("evidence_quotes", review_prompt)
        self.assertIn("语义通顺", review_prompt)
        self.assertIn("乱码", review_prompt)
        self.assertIn("每条 evidence_quotes 是否确实支持候选文字", review_prompt)
        self.assertNotIn("pass|fail", review_prompt)

    def test_evidence_quotes_must_be_short_strings_from_the_final_srt(self) -> None:
        valid = _candidate_payload()
        source = self.srt.read_text(encoding="utf-8")
        self.assertEqual(len(validate_candidate_payload(valid, source_text=source)), 3)

        missing = _candidate_payload()
        del missing["candidates"][0]["evidence_quotes"]  # type: ignore[index]
        with self.assertRaises(ValueError):
            validate_candidate_payload(missing, source_text=source)

        outside = _candidate_payload()
        outside["candidates"][0]["evidence_quotes"] = ["字幕里没有这句话"]  # type: ignore[index]
        with self.assertRaises(ValueError):
            validate_candidate_payload(outside, source_text=source)

    def test_srt_omission_marker_cannot_be_used_as_evidence(self) -> None:
        valid = _candidate_payload()
        source = (
            self.srt.read_text(encoding="utf-8")
            + "\n\n[字幕中段因长度限制省略]\n\n最后一句实际保留"
        )
        valid["candidates"][0]["evidence_quotes"] = ["字幕中段因长度限制省略"]  # type: ignore[index]
        with self.assertRaises(ValueError):
            validate_candidate_payload(valid, source_text=source)

    def test_abnormal_candidate_content_falls_back_with_safe_category(self) -> None:
        abnormal = _candidate_payload()
        abnormal["candidates"][0]["lines"][0]["text"] = "lll组合"  # type: ignore[index]
        calls = 0

        def runner(_prompt: str, **_kwargs: object) -> str:
            nonlocal calls
            calls += 1
            return json.dumps(abnormal if calls == 1 else _review_payload(), ensure_ascii=False)

        result = generate_copy_recommendations(
            video_path=self.video,
            current_title="坏标题 lll组合",
            corrected_srt_path=self.srt,
            runner=runner,
        )

        self.assertEqual(result.source, "fallback")
        self.assertEqual(calls, 1)
        self.assertIn("Luna 文案生成：文案疑似乱码", result.warning or "")
        self.assertFalse(
            any("lll" in line.text for candidate in result.candidates for line in candidate.lines)
        )

    def test_replacement_character_is_rejected_but_normal_chinese_repetition_is_allowed(self) -> None:
        valid = _candidate_payload()
        valid["candidates"][0]["lines"][0]["text"] = "哈哈哈"  # type: ignore[index]
        self.assertEqual(len(validate_candidate_payload(valid)), 3)

        garbled = _candidate_payload()
        garbled["candidates"][0]["lines"][0]["text"] = "字幕�错误"  # type: ignore[index]
        with self.assertRaises(ValueError):
            validate_candidate_payload(garbled)

    def test_srt_drives_fallback_instead_of_bad_current_title(self) -> None:
        def runner(_prompt: str, **_kwargs: object) -> str:
            raise RuntimeError("模拟离线失败")

        result = generate_copy_recommendations(
            video_path=self.video,
            current_title="lll组合 没有消息配奇妙",
            editorial_interest_reason="喝法被听出来了",
            corrected_srt_path=self.srt,
            runner=runner,
        )

        self.assertEqual(result.source, "fallback")
        fallback_text = " ".join(
            line.text for candidate in result.candidates for line in candidate.lines
        )
        self.assertIn("朋友说要送我一个大作", fallback_text)
        self.assertNotIn("lll组合", fallback_text)
        self.assertNotIn("没有消息配奇妙", fallback_text)

    def test_reason_is_truncated_instead_of_rejecting_candidate_or_review(self) -> None:
        candidate_payload = _candidate_payload()
        candidate_payload["candidates"][0]["reason"] = "候选解释" * 80  # type: ignore[index]
        candidates = validate_candidate_payload(candidate_payload)
        self.assertEqual(len(candidates[0].reason), 120)

        review = _review_payload(0)
        review["reviews"][0]["reason"] = "复核解释" * 80  # type: ignore[index]
        result = self._generate_with_review(review)

        self.assertEqual(result.source, "ai")
        self.assertIsNone(result.warning)

    def test_invalid_review_gets_one_fixed_correction_request_without_echoing_output(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []
        raw_review_marker = "RAW_TERRA_OUTPUT token=sk-secret C:\\private\\answer.json"

        def runner(prompt: str, **kwargs: object) -> str:
            calls.append((prompt, kwargs))
            if len(calls) == 1:
                return json.dumps(_candidate_payload(), ensure_ascii=False)
            if len(calls) == 2:
                return raw_review_marker
            return json.dumps(_review_payload(2), ensure_ascii=False)

        result = generate_copy_recommendations(
            video_path=self.video,
            current_title="当前标题",
            corrected_srt_path=self.srt,
            runner=runner,
            environ={"AUTOSLICE_LLM_MODEL": "terra-test-model"},
        )

        self.assertEqual(result.source, "ai")
        self.assertEqual(result.selected_index, 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual([call[1]["reasoning_stage"] for call in calls], ["analysis", "review", "review"])
        self.assertEqual(calls[1][1]["model_override"], "terra-test-model")
        self.assertEqual(calls[2][1]["model_override"], "terra-test-model")
        self.assertNotIn(raw_review_marker, calls[2][0])
        self.assertNotIn(str(self.video), calls[2][0])
        self.assertNotIn("sk-secret", calls[2][0])
        self.assertIn("evidence_quotes", calls[2][0])
        self.assertIn("无意义拼接", calls[2][0])

    def test_invalid_review_after_one_correction_falls_back_with_final_category(self) -> None:
        calls = 0
        invalid_review = _review_payload(0)
        invalid_review["reviews"][1]["candidate_index"] = 0  # type: ignore[index]

        def runner(_prompt: str, **_kwargs: object) -> str:
            nonlocal calls
            calls += 1
            payload = _candidate_payload() if calls == 1 else invalid_review
            return json.dumps(payload, ensure_ascii=False)

        result = generate_copy_recommendations(
            video_path=self.video,
            current_title="当前标题",
            corrected_srt_path=self.srt,
            runner=runner,
        )

        self.assertEqual(result.source, "fallback")
        self.assertEqual(calls, 3)
        self.assertIn("Terra 独立复核：reviews/索引不合法", result.warning or "")

    def test_review_normalizes_only_explicit_safe_scalar_variants(self) -> None:
        review = {
            "selected_index": "1",
            "reviews": [
                {
                    "candidate_index": "0",
                    "original_accuracy": "通过",
                    "hook_consequence": True,
                    "clickability_score": "4.5",
                    "reason": "中文与布尔通过值都明确",
                },
                {
                    "candidate_index": 1,
                    "original_accuracy": "TRUE",
                    "hook_consequence": "pass",
                    "clickability_score": "5",
                    "reason": "英文通过值大小写可规范化",
                },
                {
                    "candidate_index": "2",
                    "original_accuracy": False,
                    "hook_consequence": "不通过",
                    "clickability_score": "1.0",
                    "reason": "明确失败值仍保持失败",
                },
            ],
        }

        result = self._generate_with_review(review)

        self.assertEqual(result.source, "ai")
        self.assertEqual(result.selected_index, 1)
        self.assertIsNone(result.warning)

    def test_failed_selected_candidate_uses_best_double_pass_candidate(self) -> None:
        cases = (
            ([4, 5, 5], 2),
            ([5, 5, 5], 0),
        )
        for scores, expected_index in cases:
            with self.subTest(scores=scores):
                review = _review_payload(1)
                reviews = review["reviews"]
                reviews[1]["original_accuracy"] = "fail"  # type: ignore[index]
                for index, score in enumerate(scores):
                    reviews[index]["clickability_score"] = score  # type: ignore[index]

                result = self._generate_with_review(review)

                self.assertEqual(result.source, "ai")
                self.assertEqual(result.selected_index, expected_index)
                self.assertIsNone(result.warning)

    def test_review_without_any_double_pass_candidate_falls_back_with_category(self) -> None:
        review = _review_payload(1)
        for item in review["reviews"]:  # type: ignore[assignment]
            item["original_accuracy"] = "fail"

        result = self._generate_with_review(review)

        self.assertEqual(result.source, "fallback")
        self.assertIn("Terra 独立复核：没有通过项", result.warning or "")

    def test_incomplete_duplicate_or_unsafe_review_fields_still_fall_back(self) -> None:
        incomplete = _review_payload(0)
        incomplete["reviews"] = incomplete["reviews"][:1]  # type: ignore[index]

        duplicate = _review_payload(0)
        duplicate["reviews"][1]["candidate_index"] = 0  # type: ignore[index]

        unsafe_index = _review_payload(0)
        unsafe_index["selected_index"] = "first"

        unsafe_status = _review_payload(0)
        unsafe_status["reviews"][0]["original_accuracy"] = "pass|fail"  # type: ignore[index]

        unsafe_score = _review_payload(0)
        unsafe_score["reviews"][0]["clickability_score"] = 6  # type: ignore[index]

        unsafe_reason = _review_payload(0)
        unsafe_reason["reviews"][0]["reason"] = ""  # type: ignore[index]

        for label, review, category in (
            ("incomplete", incomplete, "reviews/索引不合法"),
            ("duplicate", duplicate, "reviews/索引不合法"),
            ("unsafe_index", unsafe_index, "reviews/索引不合法"),
            ("unsafe_status", unsafe_status, "状态字段不合法"),
            ("unsafe_score", unsafe_score, "分数不合法"),
            ("unsafe_reason", unsafe_reason, "reason 不合法/为空"),
        ):
            with self.subTest(case=label):
                result = self._generate_with_review(review)

                self.assertEqual(result.source, "fallback")
                self.assertIn(f"Terra 独立复核：{category}", result.warning or "")

    def test_invalid_review_json_and_http_failure_have_safe_categories(self) -> None:
        invalid_json = self._generate_with_review("not json")
        self.assertEqual(invalid_json.source, "fallback")
        self.assertIn("Terra 独立复核：输出 JSON 不合法", invalid_json.warning or "")

        class HttpFailure(RuntimeError):
            status_code = 503

        def runner(_prompt: str, **_kwargs: object) -> str:
            raise HttpFailure("token=sk-secret C:\\private\\final.srt 原始输出")

        http_failure = generate_copy_recommendations(
            video_path=self.video,
            current_title="已有标题",
            corrected_srt_path=self.srt,
            runner=runner,
        )
        warning = http_failure.warning or ""
        self.assertEqual(http_failure.source, "fallback")
        self.assertIn("Luna 文案生成：HTTP 503", warning)
        self.assertNotIn("sk-secret", warning)
        self.assertNotIn("final.srt", warning)
        self.assertNotIn("原始输出", warning)

    def test_missing_reliable_srt_never_calls_runner_or_media_tools(self) -> None:
        self.srt.unlink()

        result = generate_copy_recommendations(
            video_path=self.video,
            current_title="已有标题",
            editorial_interest_reason="已有理由",
            runner=lambda *_args, **_kwargs: self.fail("没有 SRT 时不得调用 LLM"),
        )

        self.assertEqual(result.source, "fallback")
        self.assertEqual(len(result.candidates), 3)
        self.assertIn("未调用 AI", result.warning or "")

    def test_fallback_uses_actual_video_path_for_profile_rules(self) -> None:
        video_dir = self.root / "泽音Melody" / "切片"
        video_dir.mkdir(parents=True)
        video = video_dir / "01_采访.mp4"
        video.write_bytes(b"video")

        result = generate_copy_recommendations(
            video_path=video,
            current_title="采访时守星沙",
            runner=lambda *_args, **_kwargs: self.fail("没有 SRT 时不得调用 LLM"),
        )

        self.assertEqual(result.source, "fallback")
        self.assertTrue(
            any(
                "SSXS" in line.text
                for candidate in result.candidates
                for line in candidate.lines
            )
        )

    def test_bad_output_and_sensitive_error_are_redacted_in_warning(self) -> None:
        def runner(_prompt: str, **_kwargs: object) -> str:
            raise RuntimeError(
                f"https://user:secret@example.test/v1 token=sk-secret {self.srt} prompt全文"
            )

        result = generate_copy_recommendations(
            video_path=self.video,
            current_title="已有标题",
            editorial_interest_reason="已有理由",
            runner=runner,
        )

        warning = result.warning or ""
        self.assertEqual(result.source, "fallback")
        self.assertIn("Luna 文案生成：调用失败", warning)
        self.assertNotIn("secret", warning)
        self.assertNotIn("example.test", warning)
        self.assertNotIn(str(self.srt), warning)
        self.assertNotIn("prompt全文", warning)

    def test_srt_resolution_is_exact_and_never_fuzzy_or_cross_directory(self) -> None:
        other = self.root / "其他目录"
        other.mkdir()
        fuzzy = other / f"{self.video.stem}_校对.srt"
        fuzzy.write_text(self.srt.read_text(encoding="utf-8"), encoding="utf-8")
        self.srt.unlink()

        self.assertIsNone(resolve_task_srt_path(self.video))
        self.assertEqual(resolve_task_srt_path(self.video, fuzzy), fuzzy.resolve())

    def test_candidate_validation_rejects_duplicates_unknown_roles_and_overlong_lines(self) -> None:
        valid = _candidate_payload()
        self.assertEqual(len(validate_candidate_payload(valid)), 3)

        duplicate = _candidate_payload()
        duplicate["candidates"][1] = duplicate["candidates"][0]  # type: ignore[index]
        with self.assertRaises(ValueError):
            validate_candidate_payload(duplicate)

        bad_role = _candidate_payload()
        bad_role["candidates"][0]["lines"][0]["role"] = "speaker"  # type: ignore[index]
        with self.assertRaises(ValueError):
            validate_candidate_payload(bad_role)

        overlong = _candidate_payload()
        overlong["candidates"][0]["lines"][0]["text"] = "超长" * 40  # type: ignore[index]
        with self.assertRaises(ValueError):
            validate_candidate_payload(overlong)

        overlong_label = _candidate_payload()
        overlong_label["candidates"][0]["label"] = "标签" * 20  # type: ignore[index]
        with self.assertRaises(ValueError):
            validate_candidate_payload(overlong_label)


if __name__ == "__main__":
    unittest.main()
