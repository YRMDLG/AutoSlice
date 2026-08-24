import http.client
import json
import socket
import unittest
from unittest.mock import Mock, call

import autoslice_cover
from autoslice.autocover_service import (
    AUTOCOVER_API_VERSION,
    AUTOCOVER_PROBE_INCOMPATIBLE,
    AUTOCOVER_PROBE_READY,
    AUTOCOVER_PROBE_UNAVAILABLE,
    AUTOCOVER_SERVICE_ID,
    DEFAULT_AUTOCOVER_URL,
    autocover_endpoint_from_url,
    configured_autocover_endpoint,
    probe_autocover_endpoint,
    probe_autocover_service,
)


class AutoCoverEndpointTests(unittest.TestCase):
    def test_probe_contract_matches_autocover_public_contract(self):
        self.assertEqual(AUTOCOVER_SERVICE_ID, autoslice_cover.SERVICE_ID)
        self.assertEqual(AUTOCOVER_API_VERSION, autoslice_cover.API_VERSION)

    def test_configured_localhost_keeps_browser_url_but_probes_ipv4_loopback(self):
        endpoint = configured_autocover_endpoint({
            "AUTOCOVER_URL": "http://localhost:5017",
        })

        self.assertEqual(endpoint.browser_url, "http://localhost:5017")
        self.assertEqual(endpoint.port, 5017)
        self.assertEqual(
            endpoint.probe_url,
            "http://127.0.0.1:5017/api/options",
        )

    def test_root_trailing_slash_is_accepted_and_normalized(self):
        configured = configured_autocover_endpoint({
            "AUTOCOVER_URL": "http://127.0.0.1:5012/",
        })
        parsed = autocover_endpoint_from_url("http://localhost:5013/")

        self.assertEqual(configured.browser_url, "http://127.0.0.1:5012")
        self.assertEqual(configured.port, 5012)
        self.assertEqual(parsed.browser_url, "http://localhost:5013")
        self.assertEqual(parsed.port, 5013)

    def test_real_paths_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "不带实际路径"):
            autocover_endpoint_from_url("http://127.0.0.1:5010/api/options")

        configured = configured_autocover_endpoint({
            "AUTOCOVER_URL": "http://127.0.0.1:5010/api/options",
        })
        self.assertEqual(configured.browser_url, DEFAULT_AUTOCOVER_URL)

    def test_unsafe_urls_fall_back_to_default_loopback_endpoint(self):
        unsafe_urls = (
            "https://127.0.0.1:5010",
            "http://example.com:5010",
            "http://user:password@127.0.0.1:5010",
            "http://127.0.0.1",
            "http://127.0.0.1:bad",
            "http://127.0.0.1:70000",
            "http://127.0.0.1:5010/api/options",
            "http://127.0.0.1:5010?token=secret",
            "http://127.0.0.1:5010#fragment",
            "http://[::1]:5010",
        )

        for unsafe_url in unsafe_urls:
            with self.subTest(unsafe_url=unsafe_url):
                endpoint = configured_autocover_endpoint({
                    "AUTOCOVER_URL": unsafe_url,
                })
                self.assertEqual(endpoint.browser_url, DEFAULT_AUTOCOVER_URL)
                self.assertEqual(
                    endpoint.probe_url,
                    "http://127.0.0.1:5010/api/options",
                )


class AutoCoverProbeTests(unittest.TestCase):
    @staticmethod
    def _response(payload=None, *, status=200, reason="OK", raw=None):
        response = Mock()
        response.status = status
        response.reason = reason
        response.read.return_value = (
            raw if raw is not None else json.dumps(payload).encode("utf-8")
        )
        return response

    @classmethod
    def _connection(cls, payload=None, **response_kwargs):
        connection = Mock()
        connection.getresponse.return_value = cls._response(
            payload,
            **response_kwargs,
        )
        return connection

    def test_probe_uses_one_direct_ipv4_loopback_connection_with_dynamic_port(self):
        endpoint = configured_autocover_endpoint({
            "AUTOCOVER_URL": "http://localhost:5019",
        })
        connection = self._connection({
            "service": "autocover",
            "api_version": AUTOCOVER_API_VERSION,
        })
        connection_factory = Mock(return_value=connection)

        result = probe_autocover_endpoint(
            endpoint,
            timeout=0.25,
            connection_factory=connection_factory,
        )

        self.assertEqual(result.status, AUTOCOVER_PROBE_READY)
        connection_factory.assert_called_once_with(
            "127.0.0.1",
            5019,
            timeout=0.25,
        )
        self.assertEqual(
            connection.method_calls,
            [
                call.connect(),
                call.request(
                    "GET",
                    "/api/options",
                    headers={"Accept": "application/json"},
                ),
                call.getresponse(),
                call.close(),
            ],
        )

    def test_connection_refused_is_reported_as_unavailable(self):
        endpoint = configured_autocover_endpoint()
        connection = Mock()
        connection.connect.side_effect = ConnectionRefusedError(10061, "连接被拒绝")

        result = probe_autocover_endpoint(
            endpoint,
            connection_factory=Mock(return_value=connection),
        )

        self.assertEqual(result.status, AUTOCOVER_PROBE_UNAVAILABLE)
        self.assertIn("连接被拒绝", result.reason)
        connection.request.assert_not_called()

    def test_connect_timeout_is_unavailable_but_response_timeouts_are_incompatible(self):
        endpoint = configured_autocover_endpoint()
        connect_timeout = Mock()
        connect_timeout.connect.side_effect = socket.timeout("connect timed out")

        unavailable = probe_autocover_endpoint(
            endpoint,
            connection_factory=Mock(return_value=connect_timeout),
        )

        self.assertEqual(unavailable.status, AUTOCOVER_PROBE_UNAVAILABLE)

        for stage in ("getresponse", "read"):
            with self.subTest(stage=stage):
                connection = self._connection({
                    "service": "autocover",
                    "api_version": AUTOCOVER_API_VERSION,
                })
                if stage == "getresponse":
                    connection.getresponse.side_effect = socket.timeout("response timed out")
                else:
                    connection.getresponse.return_value.read.side_effect = socket.timeout(
                        "read timed out"
                    )

                incompatible = probe_autocover_endpoint(
                    endpoint,
                    connection_factory=Mock(return_value=connection),
                )

                self.assertEqual(incompatible.status, AUTOCOVER_PROBE_INCOMPATIBLE)
                self.assertIn("连接后返回异常", incompatible.reason)

    def test_wrong_contract_and_bad_status_line_are_safe_and_incompatible(self):
        endpoint = configured_autocover_endpoint()
        wrong_contract_connection = self._connection({
            "service": {
                "name": "not-autocover",
                "token": "top-secret",
                "log": r"C:\Users\private\service.log",
            },
            "api_version": {"cookie": "private-session"},
        })

        wrong_contract = probe_autocover_endpoint(
            endpoint,
            connection_factory=Mock(return_value=wrong_contract_connection),
        )

        self.assertEqual(wrong_contract.status, AUTOCOVER_PROBE_INCOMPATIBLE)
        self.assertIn("not-autocover", wrong_contract.reason)
        self.assertNotIn("top-secret", wrong_contract.reason)
        self.assertNotIn("private-session", wrong_contract.reason)
        self.assertNotIn("C:\\Users", wrong_contract.reason)

        bad_status_connection = Mock()
        bad_status_connection.getresponse.side_effect = http.client.BadStatusLine(
            "Token=top-secret C:\\Users\\private\\service.log"
        )
        bad_status = probe_autocover_endpoint(
            endpoint,
            connection_factory=Mock(return_value=bad_status_connection),
        )

        self.assertEqual(bad_status.status, AUTOCOVER_PROBE_INCOMPATIBLE)
        self.assertIn("HTTP 响应无效", bad_status.reason)
        self.assertNotIn("top-secret", bad_status.reason)
        self.assertNotIn("C:\\Users", bad_status.reason)
        self.assertNotIn("traceback", bad_status.reason.casefold())

    def test_http_404_redirect_and_non_json_response_are_incompatible(self):
        endpoint = configured_autocover_endpoint()
        for status, reason in ((404, "Not Found"), (302, "Found")):
            with self.subTest(status=status):
                connection = self._connection(status=status, reason=reason, raw=b"")
                result = probe_autocover_endpoint(
                    endpoint,
                    connection_factory=Mock(return_value=connection),
                )

                self.assertEqual(result.status, AUTOCOVER_PROBE_INCOMPATIBLE)
                self.assertEqual(result.reason, f"HTTP {status} {reason}")
                connection.getresponse.return_value.read.assert_not_called()

        non_json_connection = self._connection(raw=b"not-json")
        non_json = probe_autocover_endpoint(
            endpoint,
            connection_factory=Mock(return_value=non_json_connection),
        )

        self.assertEqual(non_json.status, AUTOCOVER_PROBE_INCOMPATIBLE)
        self.assertIn("响应不是有效 JSON", non_json.reason)

    def test_remote_disconnect_reset_and_oversized_response_are_incompatible(self):
        endpoint = configured_autocover_endpoint()
        for error in (
                http.client.RemoteDisconnected("closed"),
                ConnectionResetError(10054, "connection reset")):
            with self.subTest(error=type(error).__name__):
                connection = Mock()
                connection.getresponse.side_effect = error

                result = probe_autocover_endpoint(
                    endpoint,
                    connection_factory=Mock(return_value=connection),
                )

                self.assertEqual(result.status, AUTOCOVER_PROBE_INCOMPATIBLE)

        oversized = self._connection(raw=b"x" * (64 * 1024 + 1))
        result = probe_autocover_endpoint(
            endpoint,
            connection_factory=Mock(return_value=oversized),
        )
        self.assertEqual(result.status, AUTOCOVER_PROBE_INCOMPATIBLE)
        self.assertIn("安全上限", result.reason)

    def test_legacy_payload_probe_preserves_launcher_contract(self):
        payload = {
            "service": "autocover",
            "api_version": AUTOCOVER_API_VERSION,
        }
        connection = self._connection(payload)
        connection_factory = Mock(return_value=connection)

        result = probe_autocover_service(
            5021,
            connection_factory=connection_factory,
        )

        self.assertEqual(result, payload)
        connection_factory.assert_called_once_with(
            "127.0.0.1",
            5021,
            timeout=0.8,
        )

        wrong_payload = {"service": "other", "api_version": 1}
        wrong_connection = self._connection(wrong_payload)
        self.assertEqual(
            probe_autocover_service(
                5022,
                connection_factory=Mock(return_value=wrong_connection),
            ),
            wrong_payload,
        )

        invalid_connection = Mock()
        invalid_connection.getresponse.side_effect = http.client.BadStatusLine("invalid")
        self.assertIsNone(probe_autocover_service(
            5023,
            connection_factory=Mock(return_value=invalid_connection),
        ))


if __name__ == "__main__":
    unittest.main()
