"""不依赖 Flask 的共用 Web 安全策略测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autoslice.security_policy import SecurityConfigurationError, SecurityPolicy


class _CookieResponse:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.cookie_name = ""
        self.cookie_value = ""
        self.cookie_options: dict[str, object] = {}

    def set_cookie(self, name, value, **options) -> None:
        self.cookie_name = name
        self.cookie_value = value
        self.cookie_options = options


class SecurityPolicyTests(unittest.TestCase):
    STRONG_TOKEN = "A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6"

    @staticmethod
    def _policy(environment):
        return SecurityPolicy(
            env_prefix="TESTSERVICE",
            cookie_name="test_local_session",
            access_header="X-TestService-Token",
            environ=environment,
        )

    @staticmethod
    def _authorize(policy, **overrides):
        request = {
            "method": "GET",
            "scheme": "http",
            "host_header": "localhost:5002",
            "headers": {},
            "cookies": {},
            "origin": "",
            "referer": "",
        }
        request.update(overrides)
        return policy.authorize(**request)

    def test_local_session_is_memory_only_and_bound_to_host(self):
        policy = self._policy({})
        response = _CookieResponse()

        attached = policy.attach_session_cookie(
            response,
            scheme="http",
            host_header="localhost:5002",
        )
        accepted = self._authorize(
            policy,
            method="POST",
            cookies={response.cookie_name: response.cookie_value},
        )
        other_host = self._authorize(
            policy,
            method="POST",
            host_header="127.0.0.1:5002",
            cookies={response.cookie_name: response.cookie_value},
        )

        self.assertTrue(attached)
        self.assertTrue(accepted.allowed)
        self.assertFalse(other_host.allowed)
        self.assertEqual(other_host.code, "write_proof_required")
        other_port = self._authorize(
            policy,
            method="POST",
            host_header="localhost:5010",
            cookies={response.cookie_name: response.cookie_value},
        )
        self.assertFalse(other_port.allowed)
        self.assertEqual(other_port.code, "write_proof_required")
        self.assertTrue(response.cookie_options["httponly"])
        self.assertEqual(response.cookie_options["samesite"], "Strict")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_origin_must_match_scheme_host_and_port_exactly(self):
        policy = self._policy({})

        accepted = self._authorize(
            policy,
            method="POST",
            host_header="[::1]:5002",
            origin="http://[::1]:5002",
        )
        wrong_port = self._authorize(
            policy,
            method="POST",
            origin="http://localhost:5010",
        )
        forged_host = self._authorize(
            policy,
            host_header="localhost.attacker.example",
        )

        self.assertTrue(accepted.allowed)
        self.assertEqual(wrong_port.code, "cross_site_write")
        self.assertEqual(forged_host.code, "untrusted_host")

    def test_invalid_lan_configuration_fails_closed_before_binding(self):
        policy = self._policy({
            "TESTSERVICE_LAN_MODE": "1",
            "TESTSERVICE_LAN_TOKEN": "x" * 64,
            "TESTSERVICE_LAN_HOSTS": "192.168.1.50",
            "TESTSERVICE_LAN_ORIGINS": "http://192.168.1.50:5002",
            "TESTSERVICE_ALLOWED_ROOTS": "relative/path",
        })

        decision = self._authorize(
            policy,
            host_header="192.168.1.50:5002",
            headers={"X-TestService-Token": "x" * 64},
        )

        self.assertEqual(decision.code, "invalid_lan_configuration")
        with self.assertRaises(SecurityConfigurationError):
            policy.bind_host()

    def test_lan_path_validation_recurses_and_checks_all_input_carriers(self):
        with tempfile.TemporaryDirectory() as allowed_dir, tempfile.TemporaryDirectory() as blocked_dir:
            environment = {
                "TESTSERVICE_LAN_MODE": "1",
                "TESTSERVICE_LAN_TOKEN": self.STRONG_TOKEN,
                "TESTSERVICE_LAN_HOSTS": "192.168.1.50",
                "TESTSERVICE_LAN_ORIGINS": "http://192.168.1.50:5002",
                "TESTSERVICE_ALLOWED_ROOTS": allowed_dir,
            }
            policy = self._policy(environment)
            allowed = Path(allowed_dir)
            blocked = Path(blocked_dir)

            accepted = policy.validate_paths(
                json_payload={"nested": {"source_paths": [str(allowed / "a.mp4")]}},
                form_payload={"output_dir": str(allowed / "output")},
                query_payload={"report_path": str(allowed / "report.json")},
                upload_filenames={"file": "timeline.docx"},
            )
            escaped = policy.validate_paths(
                json_payload={"nested": {"video_path": str(blocked / "a.mp4")}},
            )
            timeline_json = policy.validate_paths(
                json_payload={"timeline_json": str(blocked / "marks.json")},
            )
            title_file = policy.validate_paths(
                json_payload={"title_file": str(blocked / "titles.md")},
            )
            relative = policy.validate_paths(
                form_payload={"output_dir": "relative/output"},
            )
            unsafe_upload = policy.validate_paths(
                upload_filenames={"file": "../timeline.docx"},
            )

        self.assertTrue(accepted.allowed)
        self.assertEqual(escaped.code, "path_outside_allowed_roots")
        self.assertEqual(timeline_json.code, "path_outside_allowed_roots")
        self.assertEqual(title_file.code, "path_outside_allowed_roots")
        self.assertEqual(relative.code, "path_outside_allowed_roots")
        self.assertEqual(unsafe_upload.code, "unsafe_upload_filename")

    def test_effective_paths_and_lan_response_redaction_use_current_settings(self):
        with tempfile.TemporaryDirectory() as allowed_dir, tempfile.TemporaryDirectory() as blocked_dir:
            environment = {
                "TESTSERVICE_LAN_MODE": "1",
                "TESTSERVICE_LAN_TOKEN": self.STRONG_TOKEN,
                "TESTSERVICE_LAN_HOSTS": "192.168.1.50",
                "TESTSERVICE_LAN_ORIGINS": "http://192.168.1.50:5002",
                "TESTSERVICE_ALLOWED_ROOTS": allowed_dir,
            }
            policy = self._policy(environment)
            allowed = Path(allowed_dir) / "output" / "clip.mp4"
            blocked = Path(blocked_dir) / "secret.mp4"

            self.assertTrue(policy.validate_effective_paths(allowed).allowed)
            rejection = policy.validate_effective_paths(blocked)
            self.assertFalse(rejection.allowed)
            self.assertEqual(rejection.code, "path_outside_allowed_roots")
            self.assertTrue(policy.path_is_allowed(allowed))
            self.assertFalse(policy.path_is_allowed(blocked))

            redacted = policy.redact_lan_payload({
                "path": str(blocked),
                "nested": [f"读取失败：{blocked}"],
                "url": "/api/tasks/demo/media?token=opaque",
            })
            self.assertNotIn(str(blocked), str(redacted))
            self.assertEqual(redacted["url"], "/api/tasks/demo/media?token=opaque")

            environment["TESTSERVICE_LAN_MODE"] = "0"
            self.assertTrue(policy.validate_effective_paths(blocked).allowed)
            self.assertEqual(policy.redact_lan_payload({"path": str(blocked)}), {
                "path": str(blocked),
            })


if __name__ == "__main__":
    unittest.main()
