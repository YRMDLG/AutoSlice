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

    def test_model_list_marks_which_models_can_see_images(self):
        # 走查 M3：DeepSeek 的 /models 用 input_modalities 标了能不能看图
        save_ai_settings(base_url="https://api.deepseek.com", api_type="openai", token="saved-key",
                         text_model="deepseek-flash", vision_model="deepseek-flash")
        calls = []

        def get(url, *, headers, timeout):
            calls.append((url, headers))
            return type("Response", (), {"status_code": 200, "json": lambda _self: {"data": [
                {"id": "deepseek-v4-pro", "input_modalities": ["text"]},
                {"id": "deepseek-flash", "input_modalities": ["text", "image"]},
                {"id": ""},
            ]}})()

        models = ai_settings.list_models("https://api.deepseek.com", "openai", get=get)
        self.assertEqual([(model.id, model.vision) for model in models],
                         [("deepseek-flash", True), ("deepseek-v4-pro", False)])
        # 密钥留空时用已保存的
        self.assertEqual(calls, [("https://api.deepseek.com/models", {"Authorization": "Bearer saved-key"})])

    def test_model_list_errors_are_plain_and_hide_the_key(self):
        def status(code):
            return lambda *_args, **_kwargs: type("Response", (), {"status_code": code, "json": lambda _self: {}})()

        for code, words in ((401, "密钥不对"), (404, "不提供模型列表"), (500, "HTTP 500")):
            with self.assertRaises(ValueError) as caught:
                ai_settings.list_models("https://relay.example/v1", "openai", "sk-secret", get=status(code))
            self.assertIn(words, str(caught.exception))
            self.assertNotIn("sk-secret", str(caught.exception))
        with self.assertRaises(ValueError):
            ai_settings.list_models("", "openai", "sk-secret", get=status(200))

    def test_presets_are_recognised_by_host(self):
        self.assertEqual(ai_settings.preset_for("https://api.deepseek.com"), "deepseek")
        self.assertEqual(ai_settings.preset_for("https://API.DeepSeek.com/v1"), "deepseek")
        self.assertEqual(ai_settings.preset_for("http://relay.example:8318/v1"), "custom")
        self.assertEqual(ai_settings.preset_for(""), "custom")

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

    def test_new_user_starts_from_a_provider_preset_and_can_fetch_models(self):
        # 走查 M3：不再默认你有中转；选服务商就填好地址和模型，只差密钥
        from autoslice.desktop.ai_settings import ModelInfo
        from autoslice.desktop.qt_app.ai_settings_panel import AISettingsPanel

        QApplication.instance() or QApplication([])
        panel = AISettingsPanel(run=lambda action, callback: callback(action(), None))
        self.addCleanup(panel.deleteLater)
        self.assertEqual(panel.provider_box.currentData(), "deepseek")
        self.assertEqual(panel.url_edit.text(), "https://api.deepseek.com")
        self.assertEqual(panel.text_model_box.currentText(), "deepseek-flash")
        panel.provider_box.setCurrentIndex(panel.provider_box.findData("custom"))
        panel._provider_chosen()
        self.assertEqual(panel.url_edit.text(), "")
        panel.url_edit.setText("https://api.deepseek.com/chat/completions")
        panel._url_changed()
        self.assertEqual(panel.provider_box.currentData(), "deepseek")
        models = (ModelInfo("deepseek-flash", True), ModelInfo("deepseek-v4-pro", False))
        with patch("autoslice.desktop.qt_app.ai_settings_panel.list_models", return_value=models):
            panel._fetch_models()
        self.assertEqual([panel.vision_model_box.itemText(i) for i in range(panel.vision_model_box.count())],
                         ["deepseek-flash"])
        self.assertEqual(panel.text_model_box.count(), 2)
        self.assertIn("能看图的：deepseek-flash", panel.status.text())

    def test_empty_address_gets_a_plain_hint_instead_of_file_path(self):
        # 走查 N12：没填地址点“测试连接”曾提示“没保存：API 配置缺少 base_url：<完整路径>”
        from autoslice.desktop.qt_app.ai_settings_panel import AISettingsPanel

        QApplication.instance() or QApplication([])
        panel = AISettingsPanel(run=lambda action, callback: callback(action(), None))
        self.addCleanup(panel.deleteLater)
        panel.url_edit.setText("")
        panel.token_edit.setText("secret")
        self.assertFalse(panel._save())
        self.assertIn("接口地址", panel.status.text())
        self.assertNotIn("base_url", panel.status.text())


if __name__ == "__main__":
    unittest.main()
