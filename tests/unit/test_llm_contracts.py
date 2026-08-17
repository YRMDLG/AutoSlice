import inspect
import unittest

from autoslice.llm import contracts
from autoslice.llm.contracts import (
    DEFAULT_REASONING_EFFORT,
    LLMApiConfig,
    LLMGatewayError,
    LLMProtocol,
    LLMProviderUnavailableError,
    LLMRequest,
    LLMResponse,
    LLMResponseFormatError,
    LLMTimeout,
    LLMTransportError,
    ReasoningEffort,
    RetryCategory,
    classify_retry,
    http_retry_category,
    is_retryable_error,
    normalise_protocol,
    normalise_reasoning_effort,
    normalise_timeout,
)


class LLMProtocolContractTests(unittest.TestCase):

    def test_openai_and_anthropic_aliases_are_stable(self):
        aliases = {
            "OPENAI": "openai",
            "openai-compatible": "openai",
            "chat-completions": "openai",
            "Anthropic": "anthropic",
            "anthropic-compatible": "anthropic",
            "messages": "anthropic",
            LLMProtocol.OPENAI: "openai",
            LLMProtocol.ANTHROPIC: "anthropic",
        }

        for value, expected in aliases.items():
            with self.subTest(value=value):
                self.assertEqual(normalise_protocol(value), expected)

    def test_unknown_protocol_is_rejected(self):
        for value in (None, "", "responses", "custom"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "openai 或 anthropic"):
                    normalise_protocol(value)


class LLMReasoningContractTests(unittest.TestCase):

    def test_highest_reasoning_effort_semantics_are_preserved(self):
        self.assertEqual(DEFAULT_REASONING_EFFORT, "xhigh")
        self.assertEqual(normalise_reasoning_effort("max"), "xhigh")
        self.assertEqual(
            normalise_reasoning_effort(ReasoningEffort.XHIGH),
            "xhigh",
        )
        self.assertEqual(
            normalise_reasoning_effort(None, default="max"),
            "xhigh",
        )

    def test_reasoning_effort_can_be_disabled_explicitly(self):
        for value in (None, "", "none", "off", "default", "false"):
            with self.subTest(value=value):
                self.assertIsNone(normalise_reasoning_effort(value))

    def test_api_config_keeps_legacy_unpacking_and_stage_effort(self):
        config = LLMApiConfig(
            "https://gateway.example/v1/",
            "test-token",
            "gpt-5.6-terra",
            "openai-compatible",
            analysis_reasoning_effort="max",
            review_reasoning_effort="high",
        )

        self.assertEqual(
            tuple(config),
            ("https://gateway.example/v1", "test-token", "gpt-5.6-terra"),
        )
        self.assertEqual(config.api_type, "openai")
        self.assertEqual(config.reasoning_effort_for("analysis"), "xhigh")
        self.assertEqual(config.reasoning_effort_for("review"), "high")
        self.assertIsNone(config.reasoning_effort_for(None))


class LLMRequestResponseContractTests(unittest.TestCase):

    def test_timeout_normalises_scalar_pair_and_existing_contract(self):
        self.assertEqual(
            normalise_timeout(30).as_requests_timeout(),
            (30.0, 30.0),
        )
        timeout = normalise_timeout((30, 300))
        self.assertEqual(timeout, LLMTimeout(30, 300))
        self.assertIs(normalise_timeout(timeout), timeout)

    def test_request_normalises_transport_parameters_without_changing_prompt(self):
        prompt = "仅包含调用方显式传入的证据"
        request = LLMRequest(
            prompt=prompt,
            model=" gpt-5.6-luna ",
            protocol="chat-completions",
            max_tokens=16000,
            timeout=(30, 300),
            json_mode=True,
            reasoning_effort="max",
        )

        self.assertEqual(request.prompt, prompt)
        self.assertEqual(request.model, "gpt-5.6-luna")
        self.assertEqual(request.protocol, "openai")
        self.assertEqual(request.timeout.as_requests_timeout(), (30.0, 300.0))
        self.assertEqual(request.reasoning_effort, "xhigh")
        self.assertTrue(request.json_mode)

    def test_response_normalises_provider_identity(self):
        response = LLMResponse(
            text='{"topics": []}',
            model="gpt-5.6-luna",
            protocol="messages",
            finish_reason="end_turn",
        )

        self.assertEqual(response.protocol, "anthropic")
        self.assertEqual(response.text, '{"topics": []}')

    def test_invalid_timeout_or_request_budget_is_rejected(self):
        for timeout in (0, -1, (30,), (30, 0), True, "30"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    normalise_timeout(timeout)
        with self.assertRaisesRegex(ValueError, "max_tokens"):
            LLMRequest(
                prompt="evidence",
                model="gpt-5.6-luna",
                protocol="openai",
                max_tokens=0,
                timeout=(30, 300),
            )


class LLMErrorContractTests(unittest.TestCase):

    def test_http_and_network_errors_have_stable_retry_categories(self):
        self.assertIs(http_retry_category(429), RetryCategory.RATE_LIMITED)
        self.assertIs(
            http_retry_category(503),
            RetryCategory.PROVIDER_UNAVAILABLE,
        )
        self.assertIs(http_retry_category(500), RetryCategory.SERVER_ERROR)
        self.assertIs(http_retry_category(400), RetryCategory.NOT_RETRYABLE)
        self.assertIs(
            classify_retry(TimeoutError("timed out")),
            RetryCategory.TRANSIENT_NETWORK,
        )

    def test_structured_errors_expose_safe_serialisable_details(self):
        error = LLMProviderUnavailableError(
            "上游暂时不可用",
            protocol="openai-compatible",
            model="gpt-5.6-terra",
            status_code=503,
        )

        self.assertEqual(error.to_dict(), {
            "code": "llm_provider_unavailable",
            "message": "上游暂时不可用",
            "retry_category": "provider_unavailable",
            "retryable": True,
            "protocol": "openai",
            "model": "gpt-5.6-terra",
            "status_code": 503,
        })
        self.assertNotIn("token", error.to_dict())

    def test_retryability_comes_from_error_contract_not_message_text(self):
        retryable = LLMResponseFormatError("任意描述")
        terminal = LLMGatewayError("503 只是普通文本")
        transport = LLMTransportError(
            "rate limited",
            retry_category=RetryCategory.RATE_LIMITED,
            status_code=429,
        )

        self.assertTrue(is_retryable_error(retryable))
        self.assertFalse(is_retryable_error(terminal))
        self.assertTrue(is_retryable_error(transport))


class LLMContractIsolationTests(unittest.TestCase):

    def test_contract_module_has_no_http_or_business_prompt_implementation(self):
        source = inspect.getsource(contracts)

        self.assertNotIn("import requests", source)
        self.assertNotIn("requests.post", source)
        self.assertNotIn("api_config.json", source)
        self.assertNotIn("你是直播", source)
        self.assertNotIn("投稿标题", source)


if __name__ == "__main__":
    unittest.main()
