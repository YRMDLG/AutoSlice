import unittest

from autoslice.transcription import background_filter


class BackgroundFilterPolicyTests(unittest.TestCase):
    def test_modes_have_one_central_policy(self):
        off = background_filter.background_filter_policy("off")
        soft = background_filter.background_filter_policy("soft")
        strict = background_filter.background_filter_policy("strict")

        self.assertFalse(off.apply_audio_gate)
        self.assertFalse(off.request_speaker_model)
        self.assertFalse(off.discard_non_primary_speakers)
        self.assertTrue(soft.apply_audio_gate)
        self.assertTrue(soft.request_speaker_model)
        self.assertFalse(soft.discard_non_primary_speakers)
        self.assertTrue(strict.discard_non_primary_speakers)
        self.assertIn("仅单人直播", strict.risk_hint)

    def test_legacy_boolean_maps_to_off_and_soft(self):
        self.assertEqual(
            background_filter.normalise_background_filter_mode(
                foreground_only=False
            ),
            "off",
        )
        self.assertEqual(
            background_filter.normalise_background_filter_mode(
                foreground_only=True
            ),
            "soft",
        )

    def test_new_and_legacy_fields_must_agree(self):
        self.assertEqual(
            background_filter.normalise_background_filter_mode(
                "soft",
                foreground_only=True,
            ),
            "soft",
        )
        with self.assertRaisesRegex(ValueError, "含义不一致"):
            background_filter.normalise_background_filter_mode(
                "strict",
                foreground_only=True,
            )

    def test_invalid_mode_and_non_boolean_legacy_value_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "以下值之一"):
            background_filter.normalise_background_filter_mode("aggressive")
        with self.assertRaisesRegex(ValueError, "必须是布尔值"):
            background_filter.normalise_background_filter_mode(
                foreground_only=1
            )

    def test_strict_without_speaker_model_falls_back_to_soft(self):
        result = background_filter.build_background_filter_result(
            "strict",
            speaker_model_ready=False,
            speaker_model_used=False,
            device="cpu",
        )

        self.assertEqual(result["actual_mode"], "soft")
        self.assertFalse(result["speaker_model_ready"])
        self.assertFalse(result["used"])
        self.assertEqual(result["removed_segment_count"], 0)
        self.assertEqual(result["model"], "基础降噪/门限")
        self.assertEqual(result["device"], "cpu")
        self.assertIn(
            "未区分或删除非主要说话人",
            result["fallback_reason"],
        )

    def test_campp_load_failure_has_distinct_safe_fallback_reason(self):
        result = background_filter.build_background_filter_result(
            "strict",
            speaker_model_ready=True,
            speaker_model_used=False,
            speaker_model_load_failed=True,
            device="cpu",
        )

        self.assertEqual(result["actual_mode"], "soft")
        self.assertTrue(result["speaker_model_ready"])
        self.assertFalse(result["speaker_model_used"])
        self.assertTrue(result["speaker_model_load_failed"])
        self.assertEqual(result["mode"], "adaptive_gate")
        self.assertIn("文件已检测到但加载失败", result["fallback_reason"])
        self.assertNotIn("speaker-cache", result["fallback_reason"])

    def test_result_contract_reports_statistics_device_and_safe_labels(self):
        result = background_filter.build_background_filter_result(
            "soft",
            speaker_model_ready=True,
            speaker_model_used=True,
            detected_speaker_count=3,
            candidate_segment_count=4,
            candidate_seconds=2.3456,
            device="cuda:0",
        )

        self.assertEqual(result["requested_mode"], "soft")
        self.assertEqual(result["actual_mode"], "soft")
        self.assertEqual(result["detected_speaker_count"], 3)
        self.assertEqual(result["detected_speaker_count_scope"], "max_per_chunk")
        self.assertEqual(result["candidate_segment_count"], 4)
        self.assertEqual(result["candidate_seconds"], 2.346)
        self.assertEqual(result["removed_segment_count"], 0)
        self.assertEqual(result["model"], "CAM++")
        self.assertEqual(result["device"], "cuda:0")
        self.assertEqual(result["fallback_reason"], "")

    def test_public_modes_explain_soft_retention_and_strict_risk(self):
        modes = {
            item["mode"]: item
            for item in background_filter.public_background_filter_modes()
        }

        self.assertFalse(modes["soft"]["removes_non_primary_speakers"])
        self.assertTrue(modes["strict"]["removes_non_primary_speakers"])
        self.assertIn("单人直播", modes["strict"]["risk_hint"])


if __name__ == "__main__":
    unittest.main()
