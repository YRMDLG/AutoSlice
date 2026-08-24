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
