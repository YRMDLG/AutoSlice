import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_config


class RuntimeConfigTests(unittest.TestCase):

    def test_local_config_path_can_be_isolated_by_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            configured = Path(temp_dir) / "isolated.json"
            with patch.dict(os.environ, {
                runtime_config.LOCAL_CONFIG_ENV_NAME: str(configured),
            }):
                resolved = runtime_config.resolve_local_config_path()

        self.assertEqual(resolved, configured.resolve())

    def test_local_config_accepts_funasr_adjustment_keys(self):
        for key in (
            "AUTOSLICE_FUNASR_DEVICE",
            "AUTOSLICE_FUNASR_HOTWORDS",
            "AUTOSLICE_FUNASR_MODEL_DIR",
            "AUTOSLICE_FUNASR_VAD_DIR",
            "AUTOSLICE_FUNASR_PUNC_DIR",
        ):
            self.assertIn(key, runtime_config._LOCAL_ENVIRONMENT_KEYS)

    def test_local_config_accepts_only_explicit_llm_proxy_keys_not_tokens(self):
        proxy_keys = {
            "AUTOSLICE_LLM_PROXY_MODE",
            "AUTOSLICE_LLM_PROXY_HTTP",
            "AUTOSLICE_LLM_PROXY_HTTPS",
        }

        self.assertTrue(proxy_keys <= runtime_config._LOCAL_ENVIRONMENT_KEYS)
        self.assertNotIn(
            "AUTOSLICE_API_TOKEN",
            runtime_config._LOCAL_ENVIRONMENT_KEYS,
        )
        self.assertNotIn("HTTP_PROXY", runtime_config._LOCAL_ENVIRONMENT_KEYS)

    def test_local_environment_filters_tokens_but_keeps_proxy_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "autoslice.local.json"
            config_path.write_text(
                '{\n'
                '  "AUTOSLICE_LLM_PROXY_MODE": "custom",\n'
                '  "AUTOSLICE_LLM_PROXY_HTTP": "http://proxy.example:8080",\n'
                '  "AUTOSLICE_API_TOKEN": "must-not-be-loaded"\n'
                '}\n',
                encoding="utf-8",
            )
            with patch.object(runtime_config, "LOCAL_CONFIG_PATH", config_path):
                environment = runtime_config._read_local_environment()

        self.assertEqual(environment, {
            "AUTOSLICE_LLM_PROXY_MODE": "custom",
            "AUTOSLICE_LLM_PROXY_HTTP": "http://proxy.example:8080",
        })

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
