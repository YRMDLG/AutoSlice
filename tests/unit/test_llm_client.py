import ast
import inspect
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from autoslice import llm_client
from autoslice.llm import transport
from autoslice.llm.contracts import (
    LLMApiConfig,
    LLMProviderUnavailableError,
    LLMResponseFormatError,
    LLMResponseTruncatedError,
)


def make_response(payload, *, status_code=200, text=""):
    response = Mock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def make_http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    error = requests.HTTPError(f"HTTP {status_code}")
    error.response = response
    return error


class LLMCompatibilityFacadeTests(unittest.TestCase):

    def test_root_module_exports_transport_objects_by_identity(self):
        for name in llm_client.__all__:
            with self.subTest(name=name):
                self.assertIs(getattr(llm_client, name), getattr(transport, name))

    def test_root_module_contains_no_function_or_class_implementation(self):
        tree = ast.parse(inspect.getsource(llm_client))
        definitions = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]

        self.assertEqual(definitions, [])


class LLMConfigTransportTests(unittest.TestCase):

    def test_environment_config_keeps_models_and_highest_reasoning(self):
        config = transport.load_api_config(
            project_dir="unused",
            default_model="gpt-5.6-terra",
            environ={
                "AUTOSLICE_API_BASE_URL": " https://gateway.example/v1/ ",
                "AUTOSLICE_API_TOKEN": " test-token ",
                "AUTOSLICE_API_TYPE": "openai-compatible",
                "AUTOSLICE_LLM_MODEL": "gpt-5.6-luna",
            },
        )

        self.assertEqual(config.model, "gpt-5.6-luna")
        self.assertEqual(config.api_type, "openai")
        self.assertEqual(config.analysis_reasoning_effort, "xhigh")
        self.assertEqual(config.review_reasoning_effort, "xhigh")

    def test_invalid_config_does_not_echo_token(self):
        with self.assertRaisesRegex(ValueError, "HTTP") as raised:
            transport.normalise_api_config(
                {
                    "base_url": "file:///tmp",
                    "token": "do-not-leak",
                    "model": "gpt-5.6-terra",
                },
                "test",
                default_model="gpt-5.6-terra",
            )

        self.assertNotIn("do-not-leak", str(raised.exception))


class ProxyModeTests(unittest.TestCase):

    @staticmethod
    def _config(api_type="openai", **proxy_config):
        return LLMApiConfig(
            "https://gateway.example/v1",
            "test-token",
            "gpt-5.6-terra",
            api_type,
            **proxy_config,
        )

    def tearDown(self):
        transport.reset_reasoning_effort_capability_cache()

    def test_direct_default_uses_dedicated_session_without_environment_proxy(self):
        response = make_response({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "完成"},
            }],
        })
        session = Mock()
        session.post.return_value = response

        with patch.object(transport.requests, "Session", return_value=session):
            result = transport.call_compatible_api(
                "explicit evidence",
                max_tokens=100,
                json_mode=False,
                model_override=None,
                request_timeout=(1, 2),
                load_config=self._config,
            )

        self.assertEqual(result, "完成")
        self.assertIs(session.trust_env, False)
        self.assertNotIn("proxies", session.post.call_args.kwargs)
        session.close.assert_called_once_with()

    def test_system_mode_uses_requests_environment_semantics_for_anthropic(self):
        response = make_response({
            "content": [{"type": "text", "text": "完成"}],
            "stop_reason": "end_turn",
        })
        session = Mock()
        session.post.return_value = response
        config = self._config(api_type="anthropic", proxy_mode="system")

        with patch.object(transport.requests, "Session", return_value=session):
            result = transport.call_compatible_api(
                "explicit evidence",
                max_tokens=100,
                json_mode=False,
                model_override=None,
                request_timeout=(1, 2),
                load_config=lambda: config,
            )

        self.assertEqual(result, "完成")
        self.assertIs(session.trust_env, True)
        self.assertNotIn("proxies", session.post.call_args.kwargs)
        self.assertTrue(session.post.call_args.args[0].endswith("/messages"))
        session.close.assert_called_once_with()

    def test_custom_mode_uses_same_explicit_proxies_for_both_protocols(self):
        proxy_config = {
            "proxy_mode": "custom",
            "http_proxy": "http://proxy.example:8080",
            "https_proxy": "https://proxy.example:8443",
        }
        cases = {
            "openai": {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "完成"},
                }],
            },
            "anthropic": {
                "content": [{"type": "text", "text": "完成"}],
                "stop_reason": "end_turn",
            },
        }

        for api_type, payload in cases.items():
            with self.subTest(api_type=api_type):
                post = Mock(return_value=make_response(payload))
                config = self._config(api_type=api_type, **proxy_config)

                result = transport.call_compatible_api(
                    "explicit evidence",
                    max_tokens=100,
                    json_mode=False,
                    model_override=None,
                    request_timeout=(1, 2),
                    load_config=lambda config=config: config,
                    request_post=post,
                )

                self.assertEqual(result, "完成")
                self.assertEqual(post.call_args.kwargs["proxies"], {
                    "http": "http://proxy.example:8080",
                    "https": "https://proxy.example:8443",
                })

    def test_custom_session_ignores_environment_and_uses_only_explicit_proxy(self):
        response = make_response({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "完成"},
            }],
        })
        session = Mock()
        session.post.return_value = response
        config = self._config(
            proxy_mode="custom",
            https_proxy="http://explicit-proxy.example:8080",
        )

        with patch.object(transport.requests, "Session", return_value=session):
            transport.call_compatible_api(
                "explicit evidence",
                max_tokens=100,
                json_mode=False,
                model_override=None,
                request_timeout=(1, 2),
                load_config=lambda: config,
            )

        self.assertIs(session.trust_env, False)
        self.assertEqual(session.post.call_args.kwargs["proxies"], {
            "https": "http://explicit-proxy.example:8080",
        })
        session.close.assert_called_once_with()

    def test_explicit_proxy_environment_overrides_project_proxy_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "api_config.json"
            config_path.write_text(json.dumps({
                "base_url": "https://gateway.example/v1",
                "token": "test-token",
                "model": "gpt-5.6-terra",
                "api_type": "openai",
                "proxy_mode": "custom",
                "http_proxy": "http://file-proxy.example:8080",
                "https_proxy": "https://file-proxy.example:8443",
            }), encoding="utf-8")

            config = transport.load_api_config(
                project_dir=temp_dir,
                environ={
                    "HTTP_PROXY": "http://ignored-system-proxy.example:3128",
                    "AUTOSLICE_LLM_PROXY_MODE": "custom",
                    "AUTOSLICE_LLM_PROXY_HTTP": (
                        "http://explicit-proxy.example:9080"
                    ),
                },
            )

        self.assertEqual(config.proxy_mode, "custom")
        self.assertEqual(
            config.http_proxy,
            "http://explicit-proxy.example:9080",
        )
        self.assertEqual(
            config.https_proxy,
            "https://file-proxy.example:8443",
        )

    def test_custom_mode_rejects_missing_or_invalid_proxy_urls(self):
        invalid_configs = (
            {"proxy_mode": "custom"},
            {"proxy_mode": "custom", "http_proxy": "socks5://proxy.example:1080"},
            {"proxy_mode": "custom", "http_proxy": "file:///tmp/proxy"},
            {"proxy_mode": "custom", "https_proxy": "https://proxy.example:bad"},
            {"proxy_mode": "custom", "https_proxy": "https://proxy.example/path"},
            {"proxy_mode": "automatic", "http_proxy": "http://proxy.example:8080"},
        )

        for proxy_config in invalid_configs:
            with (
                self.subTest(proxy_config=proxy_config),
                self.assertRaises(ValueError),
            ):
                self._config(**proxy_config)

    def test_proxy_credentials_are_redacted_from_repr_errors_and_mock_output(self):
        credentialed_proxy = "http://proxy-user:proxy-password@proxy.example:8080"
        config = self._config(
            proxy_mode="custom",
            http_proxy=credentialed_proxy,
        )
        response = make_response({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "完成"},
            }],
        })
        post = Mock(return_value=response)

        transport.call_compatible_api(
            "explicit evidence",
            max_tokens=100,
            json_mode=False,
            model_override=None,
            request_timeout=(1, 2),
            load_config=lambda: config,
            request_post=post,
        )

        for public_text in (repr(config), repr(post.call_args)):
            self.assertNotIn("proxy-user", public_text)
            self.assertNotIn("proxy-password", public_text)
            self.assertNotIn("test-token", public_text)

        raw_error = requests.exceptions.ProxyError(
            f"cannot connect through {credentialed_proxy}"
        )
        self.assertNotIn("proxy-user", transport.short_llm_error(raw_error))
        self.assertNotIn("proxy-password", transport.short_llm_error(raw_error))

        with self.assertRaises(transport.LLMTransportError) as raised:
            transport.call_compatible_api(
                "explicit evidence",
                max_tokens=100,
                json_mode=False,
                model_override=None,
                request_timeout=(1, 2),
                load_config=lambda: config,
                request_post=Mock(side_effect=raw_error),
            )

        exception_text = repr(raised.exception)
        self.assertNotIn("proxy-user", exception_text)
        self.assertNotIn("proxy-password", exception_text)
        self.assertNotIn(credentialed_proxy, exception_text)

    def test_proxy_redaction_covers_raw_at_sign_inside_password(self):
        credentialed_proxy = "http://proxy-user:p@ssword@proxy.example:8080"

        redacted = transport.redact_url_credentials(
            f"cannot connect through {credentialed_proxy}"
        )

        self.assertEqual(
            redacted,
            "cannot connect through http://***:***@proxy.example:8080",
        )
        self.assertNotIn("proxy-user", redacted)
        self.assertNotIn("p@ssword", redacted)

    def test_owned_session_closes_after_transport_error(self):
        session = Mock()
        session.post.side_effect = requests.exceptions.ProxyError(
            "cannot connect through http://user:secret@proxy.example:8080"
        )

        with (
            patch.object(transport.requests, "Session", return_value=session),
            self.assertRaises(transport.LLMTransportError),
        ):
            transport.call_compatible_api(
                "explicit evidence",
                max_tokens=100,
                json_mode=False,
                model_override=None,
                request_timeout=(1, 2),
                load_config=self._config,
            )

        session.close.assert_called_once_with()


class LLMHttpTransportTests(unittest.TestCase):

    def tearDown(self):
        transport.reset_reasoning_effort_capability_cache()

    def test_openai_payload_uses_json_mode_timeout_and_xhigh_reasoning(self):
        response = make_response({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"topics": []}'},
            }],
        })
        post = Mock(return_value=response)
        config = LLMApiConfig(
            "https://gateway.example/v1",
            "test-token",
            "gpt-5.6-terra",
            "openai",
        )

        result = transport.call_compatible_api(
            "explicit evidence",
            max_tokens=16000,
            json_mode=True,
            model_override="gpt-5.6-luna",
            request_timeout=(30, 300),
            load_config=lambda: config,
            request_post=post,
            reasoning_stage="analysis",
        )

        self.assertEqual(result, '{"topics": []}')
        self.assertEqual(
            post.call_args.args[0],
            "https://gateway.example/v1/chat/completions",
        )
        self.assertEqual(post.call_args.kwargs["timeout"], (30.0, 300.0))
        self.assertEqual(post.call_args.kwargs["json"]["model"], "gpt-5.6-luna")
        self.assertEqual(post.call_args.kwargs["json"]["reasoning_effort"], "xhigh")
        self.assertEqual(
            post.call_args.kwargs["json"]["response_format"],
            {"type": "json_object"},
        )

    def test_anthropic_compatible_request_uses_messages_protocol(self):
        response = make_response({
            "content": [{"type": "text", "text": "完成"}],
            "stop_reason": "end_turn",
        })
        post = Mock(return_value=response)

        result = transport.call_compatible_api(
            "explicit evidence",
            max_tokens=12000,
            json_mode=False,
            model_override=None,
            request_timeout=(30, 300),
            load_config=lambda: (
                "https://gateway.example/v1",
                "sk-ant-test",
                "gpt-5.6-terra",
            ),
            request_post=post,
        )

        self.assertEqual(result, "完成")
        self.assertEqual(post.call_args.args[0], "https://gateway.example/v1/messages")
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "sk-ant-test")
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    def test_reasoning_fallback_is_cached_only_for_explicit_rejection(self):
        rejected = make_response(
            {"error": "unsupported"},
            status_code=400,
            text="Unknown parameter: reasoning_effort",
        )
        valid = make_response({
            "choices": [{"finish_reason": "stop", "message": {"content": "完成"}}],
        })
        post = Mock(side_effect=[rejected, valid, valid])
        config = LLMApiConfig(
            "https://gateway.example/v1",
            "test-token",
            "gpt-5.6-terra",
            "openai",
            review_reasoning_effort="high",
        )

        for prompt in ("first evidence", "second evidence"):
            self.assertEqual(
                transport.call_compatible_api(
                    prompt,
                    max_tokens=100,
                    json_mode=False,
                    model_override=None,
                    request_timeout=(1, 2),
                    load_config=lambda: config,
                    request_post=post,
                    reasoning_stage="review",
                ),
                "完成",
            )

        self.assertEqual(post.call_count, 3)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["reasoning_effort"], "high")
        self.assertNotIn("reasoning_effort", post.call_args_list[1].kwargs["json"])
        self.assertNotIn("reasoning_effort", post.call_args_list[2].kwargs["json"])

    def test_malformed_success_response_raises_safe_structured_error(self):
        response = make_response([])

        with self.assertRaises(LLMResponseFormatError) as raised:
            transport.call_compatible_api(
                "private prompt body",
                max_tokens=100,
                json_mode=False,
                model_override=None,
                request_timeout=(1, 2),
                load_config=lambda: LLMApiConfig(
                    "https://gateway.example/v1",
                    "test-token",
                    "gpt-5.6-terra",
                    "openai",
                ),
                request_post=Mock(return_value=response),
            )

        self.assertNotIn("private prompt body", str(raised.exception))
        self.assertTrue(raised.exception.retryable)

    def test_truncated_response_has_specific_error(self):
        with self.assertRaises(LLMResponseTruncatedError):
            transport.parse_anthropic_response(
                {"content": [], "stop_reason": "max_tokens"},
                "gpt-5.6-terra",
                12000,
            )


class LLMRetryTransportTests(unittest.TestCase):

    def test_rate_limit_retries_with_bounded_delay(self):
        outcomes = iter([make_http_error(429), "完成"])
        sleeps = []

        def fake_call(*_args, **_kwargs):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = transport.call_llm_with_retry(
            "explicit evidence",
            attempts=2,
            sleep_func=sleeps.append,
            call_func=fake_call,
            retry_delays=(3,),
        )

        self.assertEqual(result, "完成")
        self.assertEqual(sleeps, [3])

    def test_single_503_uses_only_provider_recovery_budget(self):
        sleeps = []
        call = Mock(side_effect=make_http_error(503))

        with self.assertRaisesRegex(LLMProviderUnavailableError, "检查点不会丢失"):
            transport.call_llm_with_retry(
                "explicit evidence",
                sleep_func=sleeps.append,
                call_func=call,
                provider_retry_delays=(3, 8),
            )

        self.assertEqual(call.call_count, 3)
        self.assertEqual(sleeps, [3, 8])

    def test_parallel_503_shares_two_recovery_probes(self):
        barrier = threading.Barrier(3)
        state = {"calls": 0}
        lock = threading.Lock()
        sleeps = []
        coordinator = transport.LLMProviderRetryCoordinator(delays=(3, 8))

        def fake_call(*_args, **_kwargs):
            with lock:
                state["calls"] += 1
                call_number = state["calls"]
            if call_number <= 3:
                barrier.wait(timeout=1)
            raise make_http_error(503)

        def request():
            return transport.call_llm_with_retry(
                "explicit evidence",
                retry_coordinator=coordinator,
                sleep_func=sleeps.append,
                call_func=fake_call,
            )

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(request) for _ in range(3)]
            outcomes = []
            for future in as_completed(futures):
                with self.assertRaises(LLMProviderUnavailableError) as raised:
                    future.result()
                outcomes.append(raised.exception)

        self.assertEqual(state["calls"], 5)
        self.assertEqual(sleeps, [3, 8])
        self.assertEqual(len(outcomes), 3)


class LLMTransportIsolationTests(unittest.TestCase):

    def test_transport_contains_no_business_prompt(self):
        source = inspect.getsource(transport)

        for forbidden in ("你是直播", "投稿标题", "弹幕峰值", "字幕证据"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
