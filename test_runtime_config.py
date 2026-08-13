import os
import unittest
from unittest.mock import patch

import runtime_config


class RuntimeConfigTests(unittest.TestCase):

    def test_local_config_accepts_funasr_adjustment_keys(self):
        for key in (
            "AUTOSLICE_FUNASR_DEVICE",
            "AUTOSLICE_FUNASR_HOTWORDS",
            "AUTOSLICE_FUNASR_MODEL_DIR",
            "AUTOSLICE_FUNASR_VAD_DIR",
            "AUTOSLICE_FUNASR_PUNC_DIR",
        ):
            self.assertIn(key, runtime_config._LOCAL_ENVIRONMENT_KEYS)

    def test_configured_value_prioritizes_environment_then_local_then_default(self):
        key = "AUTOSLICE_TEST_DIRECTORY"
        with patch.object(runtime_config, "LOCAL_ENVIRONMENT", {key: r"D:\\local"}):
            with patch.dict(os.environ, {key: r"E:\\explicit"}, clear=False):
                self.assertEqual(
                    runtime_config.configured_value(key, "fallback"),
                    r"E:\\explicit",
                )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    runtime_config.configured_value(key, "fallback"),
                    r"D:\\local",
                )
        with patch.object(runtime_config, "LOCAL_ENVIRONMENT", {}):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    runtime_config.configured_value(key, "fallback"),
                    str(runtime_config.PROJECT_DIR / "fallback"),
                )

    def test_apply_local_environment_does_not_replace_explicit_values(self):
        with patch.object(runtime_config, "LOCAL_ENVIRONMENT", {
            "AUTOSLICE_OUTPUT_DIR": r"D:\\local-output",
            "AUTOCOVER_OUTPUT_DIR": r"D:\\local-covers",
        }):
            target = {"AUTOSLICE_OUTPUT_DIR": r"E:\\explicit-output"}
            result = runtime_config.apply_local_environment(target)

        self.assertIs(result, target)
        self.assertEqual(target["AUTOSLICE_OUTPUT_DIR"], r"E:\\explicit-output")
        self.assertEqual(target["AUTOCOVER_OUTPUT_DIR"], r"D:\\local-covers")


if __name__ == "__main__":
    unittest.main()
