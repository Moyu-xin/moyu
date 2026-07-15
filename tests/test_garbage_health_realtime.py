from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import garbage_health_service, sub2api_store


class GarbageHealthRealtimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.service = garbage_health_service.GarbageHealthService(self.root / "state.json")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_realtime_import_forces_quota_check(self) -> None:
        config = garbage_health_service._normalize_config({
            "check_quota": False,
            "realtime_import_sub2api": True,
            "sub2api_group_id": "7",
        })
        self.assertTrue(config["check_quota"])
        self.assertEqual(config["sub2api_group_id"], "7")

    def test_available_quota_is_imported_immediately_to_selected_group(self) -> None:
        account = {
            "email": "quota@example.com",
            "identity": "quota@example.com::scope",
            "access_token": "access-token",
            "sources": [],
        }
        config = garbage_health_service._normalize_config({
            "input_dir": str(self.root),
            "concurrency": 1,
            "realtime_import_sub2api": True,
            "sub2api_group_id": "7",
        })
        health = {
            "ok": True,
            "status": "ok",
            "status_code": 200,
            "email": account["email"],
            "identity": account["identity"],
            "quota_status": "known",
            "quota_total": 100,
            "quota_used": 20,
            "quota_remaining": 80,
        }
        with (
            patch.object(garbage_health_service.proxy_pool_store, "assign_proxy", return_value={"proxy": "", "source": "fallback"}),
            patch.object(garbage_health_service.tool, "health_check", return_value=health),
            patch.object(garbage_health_service, "token_profile", return_value={"oauth_capable": True}),
            patch.object(
                sub2api_store,
                "import_account_dicts_to_sub2api",
                return_value={"ok": True, "account_ids": [42], "results": [{"ok": True, "account_id": 42}]},
            ) as importer,
        ):
            results: list[dict] = []
            self.service._execute_checks([account], config, results)

        importer.assert_called_once_with(account, group_ids=[7], allow_access_token_fallback=True)
        self.assertEqual(results[0]["sub2api_import_status"], "ok")
        self.assertEqual(results[0]["sub2api_account_id"], 42)
        self.assertEqual(self.service.get()["stats"]["realtime_import_success"], 1)

    def test_proxy_pool_is_used_even_when_legacy_manual_proxy_is_present(self) -> None:
        account = {
            "email": "manual@example.com",
            "identity": "manual@example.com::scope",
            "access_token": "token",
            "proxy": "http://account-proxy.invalid:8080",
            "sources": [],
        }
        config = garbage_health_service._normalize_config({"input_dir": str(self.root), "concurrency": 1, "proxy": "http://127.0.0.1:7897"})
        health = {"ok": False, "status": "banned", "status_code": 401, "email": account["email"], "identity": account["identity"]}
        with (
            patch.object(
                garbage_health_service.proxy_pool_store,
                "assign_proxy",
                return_value={"proxy": "http://127.0.0.1:42000", "source": "managed_mihomo", "label": "node-a"},
            ) as assign_proxy,
            patch.object(garbage_health_service.tool, "health_check", return_value=health) as health_check,
        ):
            self.service._execute_checks([account], config, [])
        assign_proxy.assert_called_once()
        self.assertEqual(assign_proxy.call_args.kwargs["fallback"], "")
        self.assertEqual(assign_proxy.call_args.kwargs["purpose"], "garbage_health")
        self.assertIsNone(assign_proxy.call_args.kwargs["account"])
        self.assertFalse(assign_proxy.call_args.kwargs["validate_node"])
        self.assertEqual(health_check.call_args.kwargs["proxy"], "http://127.0.0.1:42000")

    def test_reserved_invalid_domain_skips_network_check(self) -> None:
        account = {"email": "fixture@example.invalid", "identity": "fixture@example.invalid::scope", "access_token": "token", "sources": []}
        config = garbage_health_service._normalize_config({"input_dir": str(self.root), "concurrency": 1})
        with (
            patch.object(garbage_health_service.proxy_pool_store, "assign_proxy") as assign_proxy,
            patch.object(garbage_health_service.tool, "health_check") as health_check,
        ):
            results: list[dict] = []
            self.service._execute_checks([account], config, results)
        assign_proxy.assert_not_called()
        health_check.assert_not_called()
        self.assertEqual(results[0]["status"], "invalid")

    def test_terminal_dead_account_is_removed_from_json_list(self) -> None:
        source = self.root / "accounts.json"
        source.write_text(json.dumps({
            "accounts": [
                {"email": "dead@example.com", "access_token": "dead-token"},
                {"email": "alive@example.com", "access_token": "alive-token"},
            ]
        }), encoding="utf-8")
        account = {
            "email": "dead@example.com",
            "identity": "dead@example.com::scope",
            "access_token": "dead-token",
            "sources": [{"source": "accounts.json", "path": "$.accounts[0]"}],
        }
        config = garbage_health_service._normalize_config({
            "input_dir": str(self.root),
            "concurrency": 1,
            "auto_delete_dead": True,
        })
        health = {
            "ok": False,
            "status": "unauthorized",
            "status_code": 401,
            "email": account["email"],
            "identity": account["identity"],
        }
        with (
            patch.object(garbage_health_service.proxy_pool_store, "assign_proxy", return_value={"proxy": "", "source": "fallback"}),
            patch.object(garbage_health_service.tool, "health_check", return_value=health),
        ):
            results: list[dict] = []
            self.service._execute_checks([account], config, results)

        remaining = json.loads(source.read_text(encoding="utf-8"))["accounts"]
        self.assertEqual([item["email"] for item in remaining], ["alive@example.com"])
        self.assertTrue(source.with_suffix(".json.bak").exists())
        self.assertEqual(results[0]["source_delete_status"], "deleted")
        self.assertEqual(self.service.get()["stats"]["deleted_dead_accounts"], 1)

    def test_transient_failure_is_not_deleted(self) -> None:
        source = self.root / "account.json"
        source.write_text(json.dumps({"email": "timeout@example.com", "access_token": "token"}), encoding="utf-8")
        account = {
            "email": "timeout@example.com",
            "identity": "timeout@example.com::scope",
            "access_token": "token",
            "sources": [{"source": "account.json", "path": "$"}],
        }
        config = garbage_health_service._normalize_config({"input_dir": str(self.root), "concurrency": 1, "auto_delete_dead": True})
        health = {"ok": False, "status": "timeout", "status_code": 0, "email": account["email"], "identity": account["identity"]}
        with (
            patch.object(garbage_health_service.proxy_pool_store, "assign_proxy", return_value={"proxy": "", "source": "fallback"}),
            patch.object(garbage_health_service.tool, "health_check", return_value=health),
        ):
            self.service._execute_checks([account], config, [])
        self.assertTrue(source.exists())


class Sub2ApiGroupOverrideTests(unittest.TestCase):
    def test_import_group_override_does_not_mutate_stored_config(self) -> None:
        stored = {"enabled": True, "base_url": "http://sub2api.invalid", "group_ids": [1]}
        captured: dict = {}

        def import_list(accounts, config, **_kwargs):
            captured.update(config)
            return {"ok": True, "total": len(accounts)}

        with (
            patch.object(sub2api_store, "_load", return_value=stored),
            patch.object(sub2api_store, "_import_account_list_to_sub2api", side_effect=import_list),
        ):
            sub2api_store.import_account_dicts_to_sub2api({"access_token": "token"}, group_ids=[9])

        self.assertEqual(captured["group_ids"], [9])
        self.assertEqual(stored["group_ids"], [1])


if __name__ == "__main__":
    unittest.main()
