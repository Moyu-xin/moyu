from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import gpt_account_health_tool
from services import garbage_health_service
from services.garbage_health_service import GarbageHealthService


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


class GarbageHealthServiceTests(unittest.TestCase):
    def test_scan_requires_explicit_input_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = GarbageHealthService(Path(root) / "garbage-health.json")
            with self.assertRaisesRegex(RuntimeError, "请先选择账号扫描目录"):
                service.scan({"input_dir": ""})

    def test_health_check_reads_quota_from_wham_usage(self) -> None:
        with patch.object(gpt_account_health_tool, "new_session", return_value=_Session()):
            result = gpt_account_health_tool.health_check(
                {"email": "user@example.com", "access_token": "access-token"},
                refresh=False,
                check_models=False,
                check_quota=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["quota_status"], "known")
        self.assertEqual(result["quota_used"], 5.0)
        self.assertEqual(result["quota_total"], 100.0)
        self.assertEqual(result["quota_remaining"], 95.0)
        self.assertEqual(result["quota_source"], "chatgpt-wham")

    def test_quota_requires_both_5h_and_7d_windows(self) -> None:
        quota = gpt_account_health_tool.summarize_wham_usage({
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {"used_percent": 10},
                "secondary_window": {"used_percent": 100},
            },
        })

        self.assertEqual(quota["quota_5h_remaining"], 90.0)
        self.assertEqual(quota["quota_7d_remaining"], 0.0)
        self.assertEqual(quota["quota_remaining"], 0.0)
        self.assertFalse(gpt_account_health_tool.has_available_quota(quota))

    def test_quota_is_available_when_both_5h_and_7d_have_capacity(self) -> None:
        quota = gpt_account_health_tool.summarize_wham_usage({
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {"used_percent": 10},
                "secondary_window": {"used_percent": 25},
            },
        })

        self.assertEqual(quota["quota_5h_remaining"], 90.0)
        self.assertEqual(quota["quota_7d_remaining"], 75.0)
        self.assertTrue(gpt_account_health_tool.has_available_quota(quota))

    def test_proxy_failure_is_quarantined_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = GarbageHealthService(Path(root) / "garbage-health.json")
            account = {"email": "user@example.com", "identity": "identity-1", "access_token": "access-token"}
            results: list[dict] = []
            assignments = [
                {"proxy": "http://127.0.0.1:42000", "source": "managed_mihomo", "label": "bad-node"},
                {"proxy": "http://127.0.0.1:42000", "source": "managed_mihomo", "label": "good-node"},
            ]
            checks = [
                {"ok": False, "status": "network_error", "status_code": 0, "error": "TLS connect error", "identity": "identity-1"},
                {"ok": True, "status": "ok", "status_code": 200, "quota_status": "known", "quota_remaining": 95, "identity": "identity-1"},
            ]
            config = {
                "concurrency": 1,
                "timeout": 10,
                "proxy": "",
                "backend": "auto",
                "refresh_tokens": False,
                "check_models": False,
                "check_quota": True,
                "keep_alive_only": True,
                "keep_with_quota_only": False,
            }
            with (
                patch.object(garbage_health_service.proxy_pool_store, "assign_proxy", side_effect=assignments),
                patch.object(garbage_health_service.proxy_pool_store, "report_proxy_failure", return_value={"ok": True}) as report,
                patch.object(garbage_health_service.tool, "health_check", side_effect=checks) as health,
            ):
                service._execute_checks([account], config, results)

            self.assertEqual(health.call_count, 2)
            report.assert_called_once()
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["ok"])
            self.assertEqual(service._state["stats"]["with_quota"], 1)

    def test_scan_records_actual_input_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            scan_dir = Path(root) / "accounts"
            scan_dir.mkdir()
            service = GarbageHealthService(Path(root) / "garbage-health.json")
            with patch("services.garbage_health_service.tool.collect_accounts", return_value=([], {"json_documents": 0, "raw_account_objects": 0, "deduped_accounts": 0, "skipped_files": [], "skipped_objects": []})):
                state = service.scan({"input_dir": str(scan_dir)})

            self.assertEqual(state["last_scan"]["input_dir"], str(scan_dir.resolve()))


if __name__ == "__main__":
    unittest.main()
