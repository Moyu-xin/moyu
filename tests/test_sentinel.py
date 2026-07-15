from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from utils.sentinel import SentinelTokenGenerator, build_sentinel_headers, build_sentinel_token


class SentinelTests(unittest.TestCase):
    def test_fingerprint_matches_20260219_shape(self) -> None:
        generator = SentinelTokenGenerator("device-id", "test-user-agent")
        fingerprint = generator._get_config()
        self.assertEqual(len(fingerprint), 25)
        self.assertEqual(fingerprint[4], "test-user-agent")
        self.assertIn("20260219f9f6", generator.SCRIPT_URL)

    def test_build_uses_requirements_and_enforcement_pow(self) -> None:
        response = Mock(status_code=200, text="{}")
        response.json.return_value = {
            "token": "server-challenge",
            "proofofwork": {"required": True, "seed": "seed", "difficulty": "f"},
        }
        session = Mock()
        session.post.return_value = response

        header, cookie = build_sentinel_token(
            session,
            "device-id",
            "oauth_create_account",
            user_agent="test-user-agent",
            sec_ch_ua='"Chromium";v="145"',
        )

        request_payload = json.loads(session.post.call_args.kwargs["data"])
        token = json.loads(header)
        self.assertTrue(request_payload["p"].startswith("gAAAAAC"))
        self.assertTrue(token["p"].startswith("gAAAAAB"))
        self.assertEqual(token["c"], "server-challenge")
        self.assertEqual(token["flow"], "oauth_create_account")
        self.assertEqual(cookie, "0server-challenge")
        self.assertIn("sv=20260219f9f6", session.post.call_args.kwargs["headers"]["Referer"])

    def test_required_turnstile_and_session_observer_are_emitted(self) -> None:
        response = Mock(status_code=200, text="{}")
        response.json.return_value = {
            "token": "server-challenge",
            "proofofwork": {"required": True, "seed": "seed", "difficulty": "f"},
            "turnstile": {"required": True, "dx": "turnstile-dx"},
            "so": {"required": True, "collector_dx": "observer-dx"},
        }
        session = Mock()
        session.post.return_value = response
        with patch("utils.sentinel._run_vm_proofs", return_value={"t": "turnstile-proof", "so": "observer-proof"}):
            headers = build_sentinel_headers(session, "device-id", "oauth_create_account")

        token = json.loads(headers["openai-sentinel-token"])
        observer = json.loads(headers["openai-sentinel-so-token"])
        self.assertEqual(token["t"], "turnstile-proof")
        self.assertEqual(observer["so"], "observer-proof")
        self.assertEqual(observer["c"], "server-challenge")
        self.assertEqual(observer["flow"], "oauth_create_account")

    def test_required_vm_proof_must_not_be_silently_omitted(self) -> None:
        response = Mock(status_code=200, text="{}")
        response.json.return_value = {
            "token": "server-challenge",
            "turnstile": {"required": True, "dx": "turnstile-dx"},
        }
        session = Mock()
        session.post.return_value = response
        with patch("utils.sentinel._run_vm_proofs", return_value={"t": "", "so": ""}):
            with self.assertRaisesRegex(RuntimeError, "sentinel_turnstile_missing"):
                build_sentinel_headers(session, "device-id", "username_password_create")


if __name__ == "__main__":
    unittest.main()
