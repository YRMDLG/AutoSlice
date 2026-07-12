import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from topic_engine import STREAMER_NICKNAME_MAP, load_api_config


class PublicConfigTests(unittest.TestCase):
    def test_load_api_config_from_environment(self):
        env = {
            "AUTOSLICE_API_BASE_URL": "https://example.test/v1/",
            "AUTOSLICE_API_TOKEN": "test-token",
            "AUTOSLICE_LLM_MODEL": "test-model",
        }
        with (
            patch("topic_engine.os.path.exists", return_value=False),
            patch.dict(os.environ, env, clear=True),
        ):
            self.assertEqual(
                load_api_config(),
                ("https://example.test/v1", "test-token", "test-model"),
            )

    def test_missing_api_config_has_clear_error(self):
        with (
            patch("topic_engine.os.path.exists", return_value=False),
            patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "api_config.example.json"):
                load_api_config()

    def test_example_config_contains_placeholder_only(self):
        config = json.loads(
            (Path(__file__).parent / "api_config.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(config["token"], "YOUR_API_TOKEN")
        self.assertNotIn("sk-", config["token"])

    def test_default_build_has_no_private_streamer_mapping(self):
        self.assertEqual(STREAMER_NICKNAME_MAP, {})

    def test_web_pages_use_portable_default_paths(self):
        from app import DEFAULT_OUTPUT_DIR, DEFAULT_VIDEO_DIR, app

        client = app.test_client()
        for route in ("/", "/topic-v2"):
            response = client.get(route)
            self.assertEqual(response.status_code, 200)
            body = response.get_data(as_text=True)
            self.assertNotRegex(body, r"\d{10}-[\w\u4e00-\u9fff]+")
            self.assertNotRegex(body, r"[A-Z]:\\")

        self.assertTrue(DEFAULT_VIDEO_DIR.endswith("recordings"))
        self.assertTrue(DEFAULT_OUTPUT_DIR.endswith("output"))

    def test_removed_legacy_topic_page_returns_not_found(self):
        from app import app

        self.assertEqual(app.test_client().get("/topic").status_code, 404)


if __name__ == "__main__":
    unittest.main()
