from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import register as register_api


class RegisterApiContractTests(unittest.TestCase):
    def _client(self) -> TestClient:
        with patch.object(register_api.k12_config_store, "ensure_migrated"):
            router = register_api.create_router()
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_register_rejects_legacy_mail_and_proxy_fields(self) -> None:
        client = self._client()

        with patch.object(register_api, "require_admin"):
            response = client.post(
                "/api/register",
                json={"mail": {"providers": []}, "proxy": "http://127.0.0.1:7890"},
            )

        self.assertEqual(response.status_code, 422)
        rejected_fields = {tuple(item.get("loc") or ()) for item in response.json().get("detail", [])}
        self.assertIn(("body", "mail"), rejected_fields)
        self.assertIn(("body", "proxy"), rejected_fields)

    def test_register_start_returns_bad_request_for_invalid_mail_source(self) -> None:
        client = self._client()
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.register_service, "start", side_effect=RuntimeError("empty mailbox group")),
        ):
            response = client.post("/api/register/start")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "empty mailbox group")

    def test_managed_mihomo_start_honors_requested_threads(self) -> None:
        client = self._client()
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.register_service, "is_running", return_value=False),
            patch.object(register_api.proxy_pool_store, "get_proxy_pool_config", return_value={}),
            patch.object(register_api.managed_mihomo_service.managed_mihomo_manager, "start", return_value={"running": 2}) as start,
        ):
            response = client.post("/api/proxy-pool/managed-mihomo/start", json={"threads": 2})

        self.assertEqual(response.status_code, 200)
        start.assert_called_once_with(2, {})

    def test_managed_mihomo_stop_is_blocked_while_registration_runs(self) -> None:
        client = self._client()
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.register_service, "is_running", return_value=True),
            patch.object(register_api.managed_mihomo_service.managed_mihomo_manager, "stop") as stop,
        ):
            response = client.post("/api/proxy-pool/managed-mihomo/stop")

        self.assertEqual(response.status_code, 409)
        self.assertIn("注册任务运行中", response.json()["detail"])
        stop.assert_not_called()

    def test_proxy_config_update_reports_hot_reload(self) -> None:
        client = self._client()
        hot_reload = {"revision": 7, "running": True, "task_snapshot_mode": True}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.proxy_pool_store, "update_proxy_pool_config", return_value={"enabled": True}),
            patch.object(register_api.register_service, "hot_reload_proxy_config", return_value=hot_reload),
        ):
            response = client.post("/api/proxy-pool", json={"enabled": True})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hot_reload"], hot_reload)

    def test_large_inventory_preview_endpoints_forward_paging(self) -> None:
        client = self._client()
        mailbox_pool = {"items": [{"email": "mail@example.com"}], "page_info": {"partial": True}}
        account_result = {"accounts": [{"email": "done@example.com"}], "page_info": {"partial": True}}
        partial_result = {"items": [{"email": "partial@example.com"}], "page_info": {"partial": True}}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.mailbox_pool_store, "list_outlook_mailboxes_page", return_value=mailbox_pool) as mail_page,
            patch.object(register_api, "account_pool_page", return_value=account_result) as account_page,
            patch.object(register_api.partial_account_store, "partial_account_pool_page", return_value=partial_result) as partial_page,
        ):
            mail_response = client.get("/api/outlook-mailboxes?limit=100&offset=20")
            account_response = client.get("/api/accounts?limit=100&offset=20")
            partial_response = client.get("/api/partial-accounts?limit=100&offset=20")

        self.assertEqual(mail_response.json()["mailboxes"], mailbox_pool)
        self.assertEqual(account_response.json()["account_pool"], account_result)
        self.assertEqual(partial_response.json()["partial_account_pool"], partial_result)
        mail_page.assert_called_once_with(100, 20)
        account_page.assert_called_once_with(100, 20)
        partial_page.assert_called_once_with(100, 20, include_secret=True)

    def test_large_inventory_preview_rejects_unbounded_limit(self) -> None:
        client = self._client()
        with patch.object(register_api, "require_admin"):
            response = client.get("/api/accounts?limit=501")

        self.assertEqual(response.status_code, 422)

    def test_sub2api_partial_result_remains_structured_http_success(self) -> None:
        client = self._client()
        result = {
            "ok": False,
            "partial": True,
            "total": 2,
            "success": 1,
            "fail": 1,
            "results": [
                {"email": "ok@example.com", "ok": True, "status_code": 201},
                {"email": "failed@example.com", "ok": False, "status_code": 409, "error": "duplicate"},
            ],
        }

        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.sub2api_store, "import_accounts_to_sub2api", return_value=result),
            patch.object(register_api, "account_pool", return_value={"items": []}),
        ):
            response = client.post("/api/accounts/import-sub2api", json={"emails": []})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sub2api"], result)

    def test_cpa_partial_result_remains_structured_http_success(self) -> None:
        client = self._client()
        result = {
            "ok": False,
            "partial": True,
            "total": 2,
            "success": 1,
            "fail": 1,
            "results": [
                {"name": "ok.json", "ok": True, "status_code": 201},
                {"name": "failed.json", "ok": False, "status_code": 500, "error": "upload failed"},
            ],
        }

        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.cpa_store, "import_accounts_to_cpa", return_value=result),
            patch.object(register_api, "account_pool", return_value={"items": []}),
        ):
            response = client.post("/api/accounts/import-cpa", json={"emails": []})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cpa"], result)

    def test_account_oauth_start_contract(self) -> None:
        client = self._client()
        result = {"job_id": "job-1", "source": "registered", "email": "oauth@example.com", "auth_url": "https://auth.example", "expires_at": "2026-07-14T00:00:00+00:00"}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.account_oauth_service, "start_account_oauth", return_value=result) as start,
        ):
            response = client.post("/api/account-oauth/start", json={"source": "registered", "selector": "inventory-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["oauth"], result)
        start.assert_called_once_with(source="registered", selector="inventory-1", redirect_uri="")

    def test_account_oauth_automatic_contract(self) -> None:
        client = self._client()
        result = {"ok": True, "proxy_sticky": True, "proxy_label": "node-a"}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.account_oauth_service, "acquire_account_oauth_automatically", return_value=result) as acquire,
        ):
            response = client.post("/api/account-oauth/automatic", json={"source": "partial", "selector": "partial-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["oauth"], result)
        acquire.assert_called_once_with(source="partial", selector="partial-1")

    def test_partial_account_import_sub2api_contract(self) -> None:
        client = self._client()
        result = {"ok": True, "warehouse": {"ok": True}}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.account_oauth_service, "import_partial_account_to_sub2api", return_value=result) as promote,
        ):
            response = client.post("/api/partial-accounts/partial-1/import-sub2api")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["promotion"], result)
        promote.assert_called_once_with("partial-1")

    def test_partial_account_bulk_import_sub2api_contract(self) -> None:
        client = self._client()
        result = {"ok": False, "partial": True, "total": 2, "success": 1, "fail": 1}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.account_oauth_service, "import_partial_accounts_to_sub2api", return_value=result) as promote,
        ):
            response = client.post("/api/partial-accounts/import-sub2api", json={"selectors": ["partial-1", "partial-2"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["promotion"], result)
        promote.assert_called_once_with(["partial-1", "partial-2"])

    def test_partial_account_health_forwards_refresh_type_mode(self) -> None:
        client = self._client()
        result = {"ok": True, "stats": {"total": 1, "alive": 1, "dead": 0, "failed": 0}}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.partial_account_health_service, "check_partial_accounts", return_value=result) as check,
        ):
            response = client.post(
                "/api/partial-accounts/health/check",
                json={"selectors": ["partial-1"], "refresh_quota": False, "concurrency": 3, "mode": "refresh_type"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["partial_health"], result)
        check.assert_called_once_with(["partial-1"], refresh_quota=False, concurrency=3, mode="refresh_type", method="auto")

    def test_partial_account_quota_contract_uses_local_method(self) -> None:
        client = self._client()
        result = {"ok": True, "stats": {"total": 1, "alive": 1, "dead": 0, "failed": 0}}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.partial_account_health_service, "check_partial_accounts", return_value=result) as check,
        ):
            response = client.post(
                "/api/partial-accounts/quota/check",
                json={"selectors": ["partial-1"], "concurrency": 2},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["partial_health"], result)
        check.assert_called_once_with(["partial-1"], refresh_quota=True, concurrency=2, mode="", method="local")

    def test_partial_account_background_task_contracts(self) -> None:
        client = self._client()
        started = {"task": {"enabled": True, "running": True}}
        stopped = {"task": {"enabled": True, "running": True, "stop_requested": True}}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.partial_account_health_service.partial_account_health_scheduler, "start_task", return_value=started) as start,
            patch.object(register_api.partial_account_health_service.partial_account_health_scheduler, "stop_task", return_value=stopped) as stop,
        ):
            start_response = client.post(
                "/api/partial-accounts/health/start",
                json={"selectors": ["partial-1"], "method": "local", "refresh_quota": True, "concurrency": 2},
            )
            stop_response = client.post("/api/partial-accounts/health/stop")

        self.assertEqual(start_response.status_code, 200)
        self.assertEqual(start_response.json()["partial_health"], started)
        start.assert_called_once_with({"selectors": ["partial-1"], "method": "local", "refresh_quota": True, "concurrency": 2})
        self.assertEqual(stop_response.status_code, 200)
        self.assertEqual(stop_response.json()["partial_health"], stopped)
        stop.assert_called_once_with()

    def test_partial_account_bulk_delete_contract(self) -> None:
        client = self._client()
        pool = {"items": [], "stats": {"total": 0}, "removed": 2}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.partial_account_store, "delete_partial_accounts", return_value=pool) as delete,
        ):
            response = client.post("/api/partial-accounts/delete", json={"selectors": ["partial-1", "partial-2"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["partial_account_pool"], pool)
        delete.assert_called_once_with(["partial-1", "partial-2"])

    def test_partial_account_export_sub2api_uses_selected_account_dicts(self) -> None:
        client = self._client()
        accounts = [{"id": "partial-1", "email": "one@example.com", "access_token": "token"}]
        exported = {"count": 1, "data": {"type": "sub2api-data", "accounts": []}}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.partial_account_store, "select_partial_accounts", return_value=accounts) as select,
            patch.object(register_api.sub2api_store, "export_account_dicts_to_sub2api", return_value=exported) as export,
        ):
            response = client.post("/api/partial-accounts/export-sub2api", json={"selectors": ["partial-1"]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sub2api"], exported)
        select.assert_called_once_with(["partial-1"], include_secret=True)
        export.assert_called_once_with(accounts)

    def test_email_webhook_rejects_requests_when_secret_is_not_configured(self) -> None:
        client = self._client()
        with patch.object(register_api.mailbox_pool_store, "get_provider_configs", return_value={"openai_cpa": {}}):
            response = client.post("/api/webhook/email", json={"to_addr": "mailbox@example.com", "text": "code"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "webhook is not configured")

    def test_email_webhook_does_not_log_secret_values(self) -> None:
        client = self._client()
        expected = "expected-webhook-secret"
        provided = "provided-webhook-secret"
        with (
            patch.object(register_api.mailbox_pool_store, "get_provider_configs", return_value={"openai_cpa": {"webhook_secret": expected}}),
            self.assertLogs(register_api.logger, level="WARNING") as captured,
        ):
            response = client.post(
                "/api/webhook/email",
                headers={"x-email-webhook-secret": provided, "authorization": "Bearer sensitive-token", "cookie": "session=sensitive"},
                json={"to_addr": "mailbox@example.com", "text": "code", "secret": provided},
            )

        self.assertEqual(response.status_code, 403)
        logs = "\n".join(captured.output)
        self.assertNotIn(expected, logs)
        self.assertNotIn(provided, logs)
        self.assertNotIn("sensitive-token", logs)
        self.assertNotIn("session=sensitive", logs)

    def test_email_webhook_accepts_configured_secret(self) -> None:
        client = self._client()
        with (
            patch.object(register_api.mailbox_pool_store, "get_provider_configs", return_value={"openai_cpa": {"webhook_secret": "valid-secret"}}),
            patch.object(register_api.mail_provider, "push_openai_cpa_message", return_value=True) as push,
        ):
            response = client.post(
                "/api/webhook/email",
                headers={"x-email-webhook-secret": "valid-secret"},
                json={"to_addr": "mailbox@example.com", "text": "code"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["stored"])
        push.assert_called_once()

    def test_email_webhook_rejects_oversized_payload_before_parsing(self) -> None:
        client = self._client()
        response = client.post(
            "/api/webhook/email",
            headers={"content-length": str(register_api.MAX_WEBHOOK_BODY_BYTES + 1)},
            content=b"{}",
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "webhook payload is too large")


if __name__ == "__main__":
    unittest.main()
