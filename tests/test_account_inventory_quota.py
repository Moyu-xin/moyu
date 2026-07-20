from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import account_inventory_service
from services.account_inventory_service import AccountInventoryService


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _Session:
    def get(self, url: str, **_kwargs):
        if url.endswith("/backend-api/me"):
            return _Response(200, {"plan_type": "free"})
        if url.endswith("/backend-api/models"):
            return _Response(200, {"models": [{"slug": "gpt-5-mini", "title": "GPT-5 Mini"}]})
        if url.endswith("/backend-api/wham/usage"):
            return _Response(200, {
                "plan_type": "free",
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": {"used_percent": 5, "reset_at": 1786591163},
                },
            })
        raise AssertionError(url)


class AccountInventoryQuotaTests(unittest.TestCase):
    def test_wham_usage_is_normalized_as_percentage_quota(self) -> None:
        with patch.object(account_inventory_service, "_session", return_value=_Session()):
            result = account_inventory_service._local_health_and_quota(
                {"email": "user@example.com", "access_token": "access-token", "plan_type": "free"},
                refresh_quota=True,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["quota_ok"])
        self.assertEqual(result["quota"]["quota_status"], "known")
        self.assertEqual(result["quota"]["quota_used"], 5.0)
        self.assertEqual(result["quota"]["quota_total"], 100.0)
        self.assertEqual(result["quota"]["quota_remaining"], 95.0)

    def test_combined_health_task_does_not_fail_when_only_quota_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = AccountInventoryService(Path(root) / "state.json", Path(root) / "settings.json")
            account = {"email": "user@example.com", "access_token": "access-token"}
            with (
                patch.object(service, "_selected_accounts", return_value=[account]),
                patch.object(service, "check_account", return_value={
                    "ok": True,
                    "quota_ok": False,
                    "method": "local",
                    "status": "ok",
                    "status_code": 200,
                    "quota": {"quota_status": "unknown", "quota_error_status": "quota_unknown"},
                }),
                patch.object(account_inventory_service.proxy_pool_store, "assign_proxy", return_value={"proxy": "", "source": "fallback"}),
                patch.object(account_inventory_service, "account_pool", return_value={"accounts": [], "stats": {}}),
            ):
                result = service.check_accounts(refresh_quota=True, task_label="测活+额度", concurrency=1)

            self.assertEqual(result["success"], 1)
            self.assertEqual(result["fail"], 0)

    def test_managed_proxy_failure_is_quarantined_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = AccountInventoryService(Path(root) / "state.json", Path(root) / "settings.json")
            account = {"email": "user@example.com", "access_token": "access-token"}
            assignments = [
                {"proxy": "http://127.0.0.1:42000", "source": "managed_mihomo", "label": "bad-node"},
                {"proxy": "http://127.0.0.1:42000", "source": "managed_mihomo", "label": "good-node"},
            ]
            checks = [
                {"ok": False, "quota_ok": False, "method": "local", "status": "network_error", "status_code": 0, "error": "TLS connect error"},
                {"ok": True, "quota_ok": True, "method": "local", "status": "ok", "status_code": 200, "quota": {"quota_status": "known", "quota_used": 5, "quota_total": 100}},
            ]
            with (
                patch.object(service, "_selected_accounts", return_value=[account]),
                patch.object(service, "check_account", side_effect=checks) as check,
                patch.object(account_inventory_service.proxy_pool_store, "assign_proxy", side_effect=assignments),
                patch.object(account_inventory_service.proxy_pool_store, "report_proxy_failure", return_value={"ok": True}) as report,
                patch.object(account_inventory_service, "account_pool", return_value={"accounts": [], "stats": {}}),
            ):
                result = service.check_accounts(refresh_quota=True, task_label="同步额度", concurrency=1)

            self.assertEqual(check.call_count, 2)
            report.assert_called_once()
            self.assertEqual(result["success"], 1)
            self.assertEqual(result["fail"], 0)


if __name__ == "__main__":
    unittest.main()
