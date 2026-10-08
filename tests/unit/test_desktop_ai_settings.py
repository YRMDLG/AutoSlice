"""AI 接口配置：设置页读写 api_config.json，测试连接分别验证文字与看图模型。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoslice.desktop import ai_settings
from autoslice.desktop.ai_settings import (
    check_connection,
    read_ai_settings,
    save_ai_settings,
    split_endpoint,
)

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - CI 无 Qt 时跳过
    QApplication = None


class AISettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "api_config.json"
        path_patch = patch.object(ai_settings, "ai_config_path", return_value=self.path)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        env_patch = patch.dict("os.environ", {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in ("AUTOSLICE_API_BASE_URL", "AUTOSLICE_API_TOKEN"):
            ai_settings.os.environ.pop(key, None)

    def test_full_endpoint_is_split_into_base_and_type(self):
        self.assertEqual(split_endpoint("http://relay.example:8318/v1/responses"),
                         ("http://relay.example:8318/v1", "openai-responses"))
        self.assertEqual(split_endpoint("https://x/v1/chat/completions/"), ("https://x/v1", "openai"))
        self.assertEqual(split_endpoint("https://x/v1", "anthropic"), ("https://x/v1", "anthropic"))

    def test_plain_http_needs_explicit_permission(self):
        values = dict(base_url="http://relay.example:8318/v1/responses", api_type="", token="secret-1",
                      text_model="gpt-5.6-terra", vision_model="gpt-5.6-luna")
        with self.assertRaisesRegex(ValueError, "allow_insecure_http"):
            save_ai_settings(**values)
        self.assertFalse(self.path.exists())
        save_ai_settings(**values, allow_insecure_http=True)
        settings = read_ai_settings()
        self.assertEqual((settings.base_url, settings.api_type), ("http://relay.example:8318/v1", "openai-responses"))
        self.assertEqual((settings.text_model, settings.vision_model), ("gpt-5.6-terra", "gpt-5.6-luna"))
        self.assertTrue(settings.has_token and settings.allow_insecure_http and settings.configured)

    def test_blank_token_keeps_saved_one_and_other_fields_survive(self):
        self.path.write_text(json.dumps({
            "base_url": "https://old/v1", "token": "secret-1", "model": "m", "review_reasoning_effort": "high",
        }), encoding="utf-8")
        save_ai_settings(base_url="https://new/v1", api_type="openai", token="", text_model="t", vision_model="")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["token"], "secret-1")
        self.assertEqual(payload["review_reasoning_effort"], "high")
        self.assertEqual(payload["vision_model"], "t")

    def test_check_connection_asks_text_and_vision_models(self):
        save_ai_settings(base_url="https://x/v1/responses", api_type="", token="secret-1",
                         text_model="gpt-5.6-terra", vision_model="gpt-5.6-luna")
        calls = []

        def fake_llm(prompt, *, max_tokens, model_override, images):
            calls.append((model_override, len(images)))
            if images:
                raise RuntimeError("model not found")
            return "可用"

        lines = check_connection(fake_llm)
        self.assertEqual(calls, [("gpt-5.6-terra", 0), ("gpt-5.6-luna", 1)])
        self.assertTrue(lines[0].startswith("✓ 文字模型"))
        self.assertTrue(lines[1].startswith("✗ 看图模型"))
        self.assertIn("model not found", lines[1])

    def test_environment_configuration_is_read_only(self):
        with patch.dict("os.environ", {"AUTOSLICE_API_BASE_URL": "https://env/v1", "AUTOSLICE_API_TOKEN": "t",
                                       "AUTOSLICE_LLM_MODEL": "env-model", "AUTOSLICE_API_TYPE": "openai"}):
            settings = read_ai_settings()
        self.assertEqual((settings.source, settings.text_model), ("env", "env-model"))


@unittest.skipIf(QApplication is None, "PySide6 不可用")
class AISettingsPanelTests(AISettingsTests):
    def test_panel_splits_pasted_url_and_saves(self):
        from autoslice.desktop.qt_app.ai_settings_panel import AISettingsPanel

        app = QApplication.instance() or QApplication([])
        panel = AISettingsPanel(run=lambda action, callback: callback(action(), None))
        self.addCleanup(panel.deleteLater)
        panel.url_edit.setText("http://relay.example:8318/v1/responses")
        panel._url_changed()
        self.assertEqual(panel.url_edit.text(), "http://relay.example:8318/v1")
        self.assertEqual(panel.type_box.currentData(), "openai-responses")
        self.assertFalse(panel.http_check.isHidden())
        panel.token_edit.setText("secret-1")
        self.assertFalse(panel._save())
        self.assertIn("没保存", panel.status.text())
        panel.http_check.setChecked(True)
        panel.token_edit.setText("secret-1")
        self.assertTrue(panel._save())
        self.assertEqual(panel.token_edit.text(), "")
        self.assertIn("已保存", panel.token_edit.placeholderText())
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
