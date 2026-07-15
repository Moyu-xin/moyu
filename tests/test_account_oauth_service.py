from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services import account_oauth_service
from services.account_token_profile import SUB2API_OAUTH_CLIENT_ID


def _jwt(payload: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(payload)}.sig"


class AccountOAuthServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_file = account_oauth_service.OAUTH_SESSIONS_FILE
        self._temp_dir = tempfile.TemporaryDirectory()
        account_oauth_service.OAUTH_SESSIONS_FILE = Path(self._temp_dir.name) / "oauth_sessions.json"

    def tearDown(self) -> None:
        account_oauth_service.OAUTH_SESSIONS_FILE = self._original_file
        self._temp_dir.cleanup()

    def test_start_and_complete_registered_oauth(self) -> None:
        target = {"email": "oauth@example.com", "inventory_id": "inventory-1", "status": "active"}
        access_token = _jwt({
            "aud": ["https://api.openai.com/v1"],
            "client_id": SUB2API_OAUTH_CLIENT_ID,
            "scp": ["openid", "profile", "email", "offline_access"],
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/profile": {"email": "oauth@example.com"},
        })

        with (
            patch.object(account_oauth_service, "_target_account", return_value=target),
            patch.object(account_oauth_service.sub2api_store, "generate_openai_oauth_url", return_value={
                "auth_url": "https://auth.openai.com/authorize?state=state-123",
                "session_id": "session-123",
            }),
        ):
            started = account_oauth_service.start_account_oauth(source="registered", selector="inventory-1")

        updated = {**target, "access_token": access_token, "refresh_token": "rt.new", "client_id": SUB2API_OAUTH_CLIENT_ID}
        with (
            patch.object(account_oauth_service, "_target_account", return_value=target),
            patch.object(account_oauth_service.sub2api_store, "exchange_openai_oauth_code", return_value={
                "access_token": access_token,
                "refresh_token": "rt.new",
                "id_token": "",
                "client_id": SUB2API_OAUTH_CLIENT_ID,
                "email": "oauth@example.com",
            }) as exchange,
            patch.object(account_oauth_service, "update_registered_account", return_value=updated),
            patch.object(account_oauth_service, "_import_and_verify", return_value={"ok": True, "import": {"ok": True}, "test": {"ok": True}}),
            patch.object(account_oauth_service, "_latest_account", return_value=updated),
            patch.object(account_oauth_service, "account_pool", return_value={"accounts": [updated], "stats": {}}),
            patch.object(account_oauth_service.partial_account_store, "partial_account_pool", return_value={"items": [], "stats": {}}),
        ):
            completed = account_oauth_service.complete_account_oauth(
                job_id=started["job_id"],
                callback="http://localhost:1455/auth/callback?code=code-123&state=state-123",
            )

        self.assertTrue(completed["ok"])
        self.assertTrue(completed["warehouse"]["ok"])
        exchange.assert_called_once_with(session_id="session-123", code="code-123", state="state-123")

    def test_oauth_start_restores_recorded_registration_node(self) -> None:
        target = {
            "email": "oauth@example.com",
            "inventory_id": "inventory-1",
            "status": "active",
            "registration_proxy": {
                "source": "managed_mihomo",
                "node_id": "original-id",
                "node_name": "[pool] node-a",
            },
        }
        assignment = {
            "proxy": "http://127.0.0.1:42007",
            "source": "managed_mihomo",
            "node_id": "restored-id",
            "label": "[pool] node-a",
            "sticky": True,
        }
        with (
            patch.object(account_oauth_service, "_target_account", return_value=target),
            patch.object(account_oauth_service.proxy_pool_store, "assign_proxy", return_value=assignment) as assign,
            patch.object(account_oauth_service.sub2api_store, "generate_openai_oauth_url", return_value={
                "auth_url": "https://auth.openai.com/oauth?state=state-1",
                "session_id": "session-1",
            }),
        ):
            started = account_oauth_service.start_account_oauth(source="registered", selector="inventory-1")

        self.assertTrue(started["proxy_sticky"])
        self.assertEqual(started["proxy_label"], "[pool] node-a")
        self.assertEqual(assign.call_args.kwargs["purpose"], "account_oauth")
        self.assertIs(assign.call_args.kwargs["account"], target)

    def test_automatic_oauth_uses_registration_node_and_promotes_partial(self) -> None:
        target = {
            "id": "partial-1",
            "email": "oauth+cgpt001@example.com",
            "password": "secret",
            "status": "oauth_missing",
            "provider": "outlook_token",
            "registration_proxy": {
                "source": "managed_mihomo",
                "node_name": "[pool] node-a",
            },
        }
        assignment = {
            "proxy": "http://127.0.0.1:42008",
            "source": "managed_mihomo",
            "node_id": "restored-id",
            "label": "[pool] node-a",
            "sticky": True,
        }
        mailbox = {"provider": "outlook_token", "provider_ref": "outlook_token#1", "address": target["email"]}
        finished = {
            **target,
            "access_token": "oauth-access",
            "refresh_token": "oauth-refresh",
            "token_source": "sub2api_openai_oauth",
            "oauth_status": "sub2api_oauth_acquired",
        }
        stored = {**finished, "source_type": "partial_oauth_promoted"}
        registrar = MagicMock()
        registrar.acquire_sub2api_oauth.return_value = finished
        with (
            patch.object(account_oauth_service, "_target_account", return_value=target),
            patch.object(account_oauth_service.proxy_pool_store, "assign_proxy", return_value=assignment) as assign,
            patch.object(account_oauth_service.mailbox_pool_store, "find_mailbox_for_address", return_value=mailbox),
            patch("services.register.openai_register.PlatformRegistrar", return_value=registrar) as registrar_factory,
            patch.object(account_oauth_service, "add_registered_account") as add_registered,
            patch.object(account_oauth_service, "_latest_account", return_value=stored),
            patch.object(account_oauth_service.partial_account_store, "delete_partial_account") as delete_partial,
            patch.object(account_oauth_service, "_import_and_verify", return_value={"ok": True, "import": {"ok": True}, "test": {"ok": True}}),
            patch.object(account_oauth_service, "account_pool", return_value={"accounts": [stored], "stats": {}}),
            patch.object(account_oauth_service.partial_account_store, "partial_account_pool", return_value={"items": [], "stats": {}}),
        ):
            result = account_oauth_service.acquire_account_oauth_automatically(source="partial", selector="partial-1")

        self.assertTrue(result["ok"])
        self.assertTrue(result["proxy_sticky"])
        self.assertEqual(assign.call_args.kwargs["purpose"], "account_oauth_automatic")
        registrar_factory.assert_called_once_with("http://127.0.0.1:42008")
        self.assertEqual(registrar.current_mailbox, mailbox)
        registrar.acquire_sub2api_oauth.assert_called_once_with(1, target)
        registrar.close.assert_called_once()
        add_registered.assert_called_once()
        delete_partial.assert_called_once_with("partial-1")

    def test_automatic_oauth_requires_registration_node_record(self) -> None:
        with patch.object(account_oauth_service, "_target_account", return_value={"email": "legacy@example.com"}):
            with self.assertRaisesRegex(RuntimeError, "没有注册节点记录"):
                account_oauth_service.acquire_account_oauth_automatically(source="registered", selector="legacy@example.com")

    def test_complete_rejects_different_authorized_email(self) -> None:
        target = {"email": "expected@example.com", "inventory_id": "inventory-1"}
        access_token = _jwt({
            "aud": ["https://api.openai.com/v1"],
            "client_id": SUB2API_OAUTH_CLIENT_ID,
            "scp": ["offline_access"],
            "exp": int(time.time()) + 3600,
        })
        with (
            patch.object(account_oauth_service, "_target_account", return_value=target),
            patch.object(account_oauth_service.sub2api_store, "generate_openai_oauth_url", return_value={
                "auth_url": "https://auth.openai.com/authorize?state=state-123",
                "session_id": "session-123",
            }),
        ):
            started = account_oauth_service.start_account_oauth(source="registered", selector="inventory-1")
        with (
            patch.object(account_oauth_service, "_target_account", return_value=target),
            patch.object(account_oauth_service.sub2api_store, "exchange_openai_oauth_code", return_value={
                "access_token": access_token,
                "refresh_token": "rt.new",
                "email": "different@example.com",
            }),
        ):
            with self.assertRaisesRegex(RuntimeError, "授权账号不匹配"):
                account_oauth_service.complete_account_oauth(job_id=started["job_id"], callback="code-123")


if __name__ == "__main__":
    unittest.main()
