from __future__ import annotations

import base64
import json
import time
import unittest

from services.account_token_profile import (
    CHATGPT_WEB_CLIENT_ID,
    INVENTORY_OAUTH_CLIENT_ID,
    SUB2API_OAUTH_CLIENT_ID,
    token_profile,
)


def _jwt(payload: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.sig"


class AccountTokenProfileTests(unittest.TestCase):
    def _platform_account(self, client_id: str) -> dict:
        return {
            "email": "oauth@example.com",
            "access_token": _jwt({
                "aud": ["https://api.openai.com/v1"],
                "client_id": client_id,
                "scp": ["openid", "profile", "email", "offline_access"],
                "exp": int(time.time()) + 3600,
            }),
            "refresh_token": "rt.test-value",
            "client_id": client_id,
            "status": "active",
            "oauth_refresh_status": "ok",
            "token_refresh_required": False,
        }

    def test_inventory_oauth_refresh_token_is_classified(self) -> None:
        profile = token_profile(self._platform_account(INVENTORY_OAUTH_CLIENT_ID))
        self.assertFalse(profile["oauth_capable"])
        self.assertFalse(profile["finished_account"])
        self.assertTrue(profile["needs_oauth"])
        self.assertEqual(profile["refresh_token_kind"], "inventory_oauth_rt")
        self.assertEqual(profile["oauth_client_kind"], "inventory")
        self.assertEqual(profile["oauth_token_status"], "incompatible_refresh")

    def test_sub2api_oauth_refresh_token_is_classified(self) -> None:
        account = self._platform_account(SUB2API_OAUTH_CLIENT_ID)
        account.update({"token_source": "sub2api_openai_oauth", "oauth_provenance": "sub2api_manual_authorization"})
        profile = token_profile(account)
        self.assertTrue(profile["oauth_capable"])
        self.assertEqual(profile["refresh_token_kind"], "sub2api_oauth_rt")
        self.assertEqual(profile["oauth_client_kind"], "sub2api")

    def test_app_emo_without_sub2api_provenance_is_not_finished(self) -> None:
        profile = token_profile(self._platform_account(SUB2API_OAUTH_CLIENT_ID))
        self.assertFalse(profile["oauth_capable"])
        self.assertEqual(profile["oauth_token_status"], "unverified_provenance")

    def test_failed_refresh_is_not_finished(self) -> None:
        account = self._platform_account(INVENTORY_OAUTH_CLIENT_ID)
        account.update({"oauth_refresh_status": "refresh_token_invalid", "token_refresh_required": True})
        profile = token_profile(account)
        self.assertFalse(profile["oauth_capable"])
        self.assertFalse(profile["finished_account"])
        self.assertTrue(profile["needs_oauth"])
        self.assertEqual(profile["oauth_token_status"], "invalid_refresh")

    def test_web_session_access_token_is_not_platform_oauth(self) -> None:
        account = self._platform_account(CHATGPT_WEB_CLIENT_ID)
        account["refresh_token"] = ""
        profile = token_profile(account)
        self.assertFalse(profile["oauth_capable"])
        self.assertEqual(profile["oauth_token_status"], "web_session_only")
        self.assertEqual(profile["refresh_token_kind"], "web_session_only")

    def test_sub2api_health_can_make_account_finished(self) -> None:
        profile = token_profile({
            "email": "warehouse@example.com",
            "sub2api_account_id": 42,
            "sub2api_health_status": "ok",
            "status": "active",
        })
        self.assertTrue(profile["finished_account"])
        self.assertEqual(profile["finished_reason"], "sub2api_healthy")


if __name__ == "__main__":
    unittest.main()
