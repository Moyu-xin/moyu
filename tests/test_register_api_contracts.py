from __future__ import annotations

import inspect
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

    def test_blocking_warehouse_and_inventory_routes_use_threadpool_handlers(self) -> None:
        client = self._client()
        blocking_paths = {
            "/api/outlook-mailboxes",
            "/api/outlook-mailboxes/messages",
            "/api/accounts",
            "/api/partial-accounts",
            "/api/sub2api/groups",
            "/api/sub2api/accounts",
            "/api/cpa/accounts",
            "/api/grok2api/accounts",
            "/api/proxy-pool/test",
        }
        endpoints = {
            route.path: route.endpoint
            for route in client.app.routes
            if getattr(route, "path", "") in blocking_paths
        }

        self.assertEqual(set(endpoints), blocking_paths)
        for path, endpoint in endpoints.items():
            with self.subTest(path=path):
                self.assertFalse(inspect.iscoroutinefunction(endpoint))

    def test_register_start_returns_bad_request_for_invalid_mail_source(self) -> None:
        client = self._client()
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.register_service, "start", side_effect=RuntimeError("empty mailbox group")),
        ):
            response = client.post("/api/register/start")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "empty mailbox group")

    def test_register_logs_clear_uses_dedicated_service_operation(self) -> None:
        client = self._client()
        expected = {"enabled": True, "logs": [], "stats": {"success": 8, "fail": 3}}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.register_service, "clear_logs", return_value=expected) as clear_logs,
        ):
            response = client.post("/api/register/logs/clear")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["register"], expected)
        clear_logs.assert_called_once_with()

    def test_account_outbound_selects_quantity_batch_and_marks_export(self) -> None:
        client = self._client()
        accounts = [{"email": "a@example.com", "access_token": "token"}]
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api, "select_accounts_for_outbound", return_value=accounts) as select,
            patch.object(register_api.sub2api_store, "export_account_dicts_to_sub2api", return_value={"count": 1, "data": {"accounts": [1]}}),
            patch.object(register_api, "mark_accounts_outbound", return_value={"marked": 1, "outbound_batch_id": "out-1", "outbound_at": "now"}),
            patch.object(register_api, "registration_batches", return_value=[]),
        ):
            response = client.post("/api/accounts/outbound", json={
                "quantity": 1,
                "registration_batch_id": "register-a",
                "format": "sub2api",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_outbound"]["count"], 1)
        select.assert_called_once_with(quantity=1, registration_batch_id="register-a", include_outbound=False)

    def test_mailbox_outbound_returns_credentials_payload(self) -> None:
        client = self._client()
        mailboxes = [{
            "provider_type": "outlook_token",
            "email": "mail@example.com",
            "password": "password",
            "client_id": "client",
            "refresh_token": "refresh",
        }]
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.mailbox_pool_store, "select_mailboxes_for_outbound", return_value=mailboxes),
            patch.object(register_api.mailbox_pool_store, "mark_mailboxes_outbound", return_value={"marked": 1, "outbound_batch_id": "mail-out-1", "outbound_at": "now"}),
            patch.object(register_api.mailbox_pool_store, "mailbox_batches", return_value=[]),
        ):
            response = client.post("/api/mailboxes/outbound", json={"quantity": 1, "format": "credentials"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("mail@example.com----password----client----refresh", response.json()["mailbox_outbound"]["data"])

    def test_grok_solver_restart_uses_requested_url_and_threads(self) -> None:
        client = self._client()
        expected = {"running": True, "reachable": True, "actual_threads": 4}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.register_service, "is_running", return_value=False),
            patch.object(register_api.register_service, "get", return_value={"grok": {"solver_threads": 1}}),
            patch.object(register_api.grok_solver_manager, "restart", return_value=expected) as restart,
        ):
            response = client.post(
                "/api/grok/solver",
                json={"action": "restart", "url": "http://127.0.0.1:5072", "threads": 4},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["grok_solver"], expected)
        restart.assert_called_once_with("http://127.0.0.1:5072", threads=4)

    def test_grok_solver_stop_targets_configured_endpoint(self) -> None:
        client = self._client()
        expected = {"running": False, "reachable": False}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.register_service, "is_running", return_value=False),
            patch.object(register_api.register_service, "get", return_value={"grok": {"local_solver_url": "http://127.0.0.1:5073"}}),
            patch.object(register_api.grok_solver_manager, "stop", return_value=expected) as stop,
        ):
            response = client.post("/api/grok/solver", json={"action": "stop"})

        self.assertEqual(response.status_code, 200)
        stop.assert_called_once_with("http://127.0.0.1:5073")

    def test_grok_solver_cannot_stop_while_registration_runs(self) -> None:
        client = self._client()
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.register_service, "is_running", return_value=True),
            patch.object(register_api.grok_solver_manager, "stop") as stop,
        ):
            response = client.post("/api/grok/solver", json={"action": "stop"})

        self.assertEqual(response.status_code, 409)
        self.assertIn("请先停止注册任务", response.json()["detail"])
        stop.assert_not_called()

    def test_plus_link_retry_returns_bad_request_for_non_retryable_task(self) -> None:
        client = self._client()
        with (
            patch.object(register_api, "require_admin"),
            patch.object(
                register_api.plus_link_store.plus_link_service,
                "retry_task",
                side_effect=RuntimeError("临时 token 任务不保存凭据，请重新粘贴 token 后启动"),
            ),
        ):
            response = client.post("/api/plus-link/tasks/manual-task/retry")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "临时 token 任务不保存凭据，请重新粘贴 token 后启动")

    def test_plus_link_retry_failed_endpoint_returns_bulk_summary(self) -> None:
        client = self._client()
        expected = {
            "results": [],
            "bulk_retry": {"requested": 3, "restarted": 2, "skipped": 1},
        }
        with (
            patch.object(register_api, "require_admin"),
            patch.object(
                register_api.plus_link_store.plus_link_service,
                "retry_failed_tasks",
                return_value=expected,
            ) as retry_failed,
        ):
            response = client.post("/api/plus-link/retry-failed")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["plus_link"], expected)
        retry_failed.assert_called_once_with()

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

    def test_plus_link_config_preserves_checkout_protocol_fields(self) -> None:
        client = self._client()
        expected = {
            "residential_parent_proxy_override": "http://127.0.0.1:7897",
            "checkout_ui_mode": "custom",
            "provider_country": "BR",
            "approve_country": "JP",
            "stripe_provider_egress": "left_residential",
            "stripe_poll_egress": "promotion",
            "paypal_confirm_mode": "payment_method",
            "paypal_confirm_protocol": "standalone",
            "proxy_quality_gate_enabled": True,
            "proxy_quality_candidate_limit": 4,
            "proxy_quality_probe_rounds": 2,
            "proxy_quality_max_latency_ms": 15000,
            "proxy_quality_quarantine_seconds": 600,
        }
        with (
            patch.object(register_api, "require_admin"),
            patch.object(
                register_api.plus_link_store.plus_link_service,
                "update_config",
                return_value={"config": expected},
            ) as update_config,
        ):
            response = client.post("/api/plus-link/config", json=expected)

        self.assertEqual(response.status_code, 200)
        update_config.assert_called_once_with(expected)

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

    def test_sub2api_import_forwards_grok_reauthorization(self) -> None:
        client = self._client()
        result = {"ok": True, "total": 1, "success": 1, "fail": 0}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api.sub2api_store, "import_accounts_to_sub2api", return_value=result) as import_accounts,
            patch.object(register_api, "account_pool", return_value={"accounts": []}),
        ):
            response = client.post(
                "/api/accounts/import-sub2api",
                json={"emails": ["grok:expired@example.com"], "reauthorize_grok": True},
            )

        self.assertEqual(response.status_code, 200)
        import_accounts.assert_called_once_with(["grok:expired@example.com"], reauthorize_grok=True)

    def test_grok_refresh_token_uses_sso_reauthorization(self) -> None:
        client = self._client()
        account = {
            "platform": "grok",
            "email": "expired@example.com",
            "sso_token": "sso-token",
            "sub2api_account_id": 91,
        }
        refreshed_account = {**account, "access_token": "fresh-access", "refresh_token": "fresh-refresh"}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(register_api, "list_registered_accounts", return_value=[account]),
            patch.object(
                register_api.sub2api_store,
                "refresh_grok_account_oauth",
                return_value={"account": refreshed_account, "method": "sso_reauthorize", "proxy": ""},
            ) as refresh_grok,
            patch("services.account_inventory_service._grok_health_and_quota", return_value={"updates": {}}),
            patch.object(register_api, "account_pool", return_value={"accounts": [refreshed_account]}),
        ):
            response = client.post("/api/accounts/grok%3Aexpired%40example.com/refresh-token")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["refresh_method"], "sso_reauthorize")
        refresh_grok.assert_called_once_with(account)

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
