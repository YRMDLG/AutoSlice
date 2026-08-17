import ast
import inspect
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import Mock

import requests

import llm_client
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
