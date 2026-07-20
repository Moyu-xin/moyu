from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services import account_inventory_service
from services.account_inventory_service import AccountInventoryService
from services.account_token_profile import token_profile


class _Cookies:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, name: str, value: str, **_kwargs) -> None:
        self.values[name] = value


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None, *, text: str = "", url: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.url = url

    def json(self) -> dict:
        return self._payload


class _GrokSession:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.cookies = _Cookies()
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs) -> _Response:
        self.urls.append(url)
        return self.response


class _SequenceGrokSession(_GrokSession):
    def __init__(self, responses: list[_Response]) -> None:
        super().__init__(responses[0])
        self.responses = responses

    def get(self, url: str, **_kwargs) -> _Response:
        self.urls.append(url)
        return self.responses.pop(0)


class AccountInventoryGrokTests(unittest.TestCase):
    def test_grok_oauth_health_uses_cli_models_endpoint(self) -> None:
        session = _GrokSession(_Response(200, {"data": [{"id": "grok-4"}]}))
        account = {
            "platform": "grok",
            "email": "grok@example.com",
            "access_token": "grok-access-token",
            "token_expires_at": int(time.time()) + 3600,
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        }
        with patch.object(account_inventory_service, "_session", return_value=session):
            result = account_inventory_service._grok_health_and_quota(account, refresh_quota=True, proxy="http://proxy")

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "grok_oauth")
        self.assertEqual(result["quota"]["quota_status"], "unavailable")
        self.assertEqual(session.urls, ["https://cli-chat-proxy.grok.com/v1/models"])

    def test_grok_sso_only_health_checks_xai_session(self) -> None:
        session = _GrokSession(_Response(200, text="account", url="https://accounts.x.ai/"))
        account = {"platform": "grok", "email": "grok@example.com", "sso_token": "sso-token"}
        with patch.object(account_inventory_service, "_session", return_value=session):
            result = account_inventory_service._grok_health_and_quota(account, refresh_quota=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "grok_sso")
        self.assertEqual(session.cookies.values["sso"], "sso-token")
        self.assertEqual(session.urls, ["https://accounts.x.ai/"])

    def test_invalid_grok_oauth_falls_back_to_sso_and_requires_refresh(self) -> None:
        session = _SequenceGrokSession([
            _Response(401, {"error": {"message": "invalid token"}}),
            _Response(200, text="account", url="https://accounts.x.ai/"),
        ])
        account = {
            "platform": "grok",
            "email": "grok@example.com",
            "access_token": "stale-token",
            "sso_token": "sso-token",
            "token_expires_at": int(time.time()) + 3600,
        }
        with patch.object(account_inventory_service, "_session", return_value=session):
            result = account_inventory_service._grok_health_and_quota(account, refresh_quota=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "grok_sso")
        self.assertTrue(result["updates"]["token_refresh_required"])
        self.assertEqual(result["updates"]["oauth_refresh_status"], "token_invalid")

    def test_grok_batch_uses_current_pool_instead_of_legacy_account_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = AccountInventoryService(Path(root) / "state.json", Path(root) / "settings.json")
            account = {
                "platform": "grok",
                "email": "grok@example.com",
                "sso_token": "sso-token",
                "proxy": "http://127.0.0.1:42000",
            }

            def assign_proxy(*_args, **kwargs):
                self.assertIsNone(kwargs.get("account"))
                return {"proxy": "http://127.0.0.1:43000", "source": "managed_mihomo", "label": "fresh-node"}

            with (
                patch.object(service, "_selected_accounts", return_value=[account]),
                patch.object(service, "check_account", return_value={"ok": True, "quota_ok": False, "method": "grok_sso", "status": "ok", "status_code": 200}) as check,
                patch.object(account_inventory_service.proxy_pool_store, "assign_proxy", side_effect=assign_proxy),
                patch.object(account_inventory_service, "account_pool", return_value={"accounts": [], "stats": {}}),
            ):
                result = service.check_accounts(refresh_quota=True, concurrency=1)

            self.assertEqual(result["success"], 1)
            self.assertEqual(check.call_args.kwargs["proxy"], "http://127.0.0.1:43000")

    def test_stop_prevents_remaining_accounts_from_starting(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = AccountInventoryService(Path(root) / "state.json", Path(root) / "settings.json")
            accounts = [
                {"platform": "grok", "email": f"grok-{index}@example.com", "sso_token": "sso"}
                for index in range(3)
            ]
            started = threading.Event()
            release = threading.Event()
            calls: list[str] = []

            def slow_check(account: dict, **_kwargs):
                calls.append(str(account.get("email") or ""))
                started.set()
                self.assertTrue(release.wait(5))
                return {"ok": True, "quota_ok": False, "method": "grok_sso", "status": "ok", "status_code": 200}

            with (
                patch.object(service, "_selected_accounts", return_value=accounts),
                patch.object(service, "check_account", side_effect=slow_check),
                patch.object(account_inventory_service.proxy_pool_store, "assign_proxy", return_value={"proxy": "", "source": "fallback"}),
                patch.object(account_inventory_service, "account_pool", return_value={"accounts": [], "stats": {}}),
            ):
                service.start({"refresh_quota": False, "concurrency": 1}, include_pool=False)
                self.assertTrue(started.wait(5))
                stopped = service.stop(include_pool=False)
                self.assertFalse(stopped["enabled"])
                release.set()
                self.assertIsNotNone(service._runner)
                service._runner.join(5)

            self.assertEqual(calls, ["grok-0@example.com"])
            state = service.get(include_pool=False)
            self.assertEqual(state["stats"]["total"], 3)
            self.assertEqual(state["stats"]["done"], 1)
            self.assertIn("任务已停止", state["logs"][-1]["text"])

    def test_grok_tokens_are_counted_as_grok_oauth(self) -> None:
        profile = token_profile({
            "platform": "grok",
            "access_token": "opaque-access-token",
            "refresh_token": "opaque-refresh-token",
            "token_expires_at": int(time.time()) + 3600,
            "status": "active",
        })

        self.assertTrue(profile["oauth_capable"])
        self.assertEqual(profile["oauth_client_kind"], "grok")
        self.assertEqual(profile["refresh_token_kind"], "grok_oauth_rt")
        self.assertFalse(profile["needs_oauth"])


if __name__ == "__main__":
    unittest.main()
