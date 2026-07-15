from __future__ import annotations

import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from services import partial_account_health_service


class PartialAccountHealthServiceTests(unittest.TestCase):
    def test_health_check_uses_proxy_pool_and_persists_result(self) -> None:
        account = {
            "id": "partial-1",
            "email": "alive@example.com",
            "access_token": "web-access-token",
            "status": "oauth_failed",
        }
        health = {
            "ok": True,
            "status": "ok",
            "status_code": 200,
            "updates": {
                "status": "active",
                "health_status": "ok",
                "health_code": 200,
                "health_checked_at": "2026-07-14T12:00:00+00:00",
            },
        }
        pool = {"items": [], "stats": {"total": 1, "health_alive": 1}}
        with (
            patch.object(partial_account_health_service.partial_account_store, "list_partial_accounts", return_value=[account]),
            patch.object(partial_account_health_service.proxy_pool_store, "get_proxy_pool_config", return_value={"enabled": False}),
            patch.object(
                partial_account_health_service.proxy_pool_store,
                "assign_proxy",
                return_value={"proxy": "http://127.0.0.1:42000", "source": "managed_mihomo", "label": "node-a"},
            ) as assign_proxy,
            patch.object(partial_account_health_service, "_local_health_and_quota", return_value=health) as check,
            patch.object(partial_account_health_service.partial_account_store, "update_partial_account") as update,
            patch.object(partial_account_health_service.partial_account_store, "partial_account_pool", return_value=pool),
        ):
            result = partial_account_health_service.check_partial_accounts(["partial-1"], concurrency=1)

        self.assertEqual(result["stats"], {"total": 1, "alive": 1, "dead": 0, "failed": 0, "quota_ok": 0, "quota_failed": 1})
        assign_proxy.assert_called_once()
        self.assertEqual(check.call_args.kwargs["proxy"], "http://127.0.0.1:42000")
        updates = update.call_args.args[1]
        self.assertNotIn("status", updates)
        self.assertEqual(updates["health_status"], "ok")
        self.assertEqual(updates["died_at"], "")
        self.assertEqual(updates["health_proxy_label"], "node-a")

    def test_terminal_result_sets_first_death_time(self) -> None:
        account = {
            "id": "partial-dead",
            "email": "dead@example.com",
            "status": "oauth_failed",
            "registered_at": "2026-07-13T12:00:00+00:00",
        }
        health = {
            "ok": False,
            "status": "banned",
            "status_code": 401,
            "updates": {
                "status": "banned",
                "health_status": "banned",
                "health_code": 401,
                "health_checked_at": "2026-07-14T12:00:00+00:00",
            },
        }
        with (
            patch.object(partial_account_health_service.partial_account_store, "list_partial_accounts", return_value=[account]),
            patch.object(partial_account_health_service.proxy_pool_store, "get_proxy_pool_config", return_value={"enabled": False}),
            patch.object(partial_account_health_service.proxy_pool_store, "assign_proxy", return_value={}),
            patch.object(partial_account_health_service, "_local_health_and_quota", return_value=health),
            patch.object(partial_account_health_service.partial_account_store, "update_partial_account") as update,
            patch.object(partial_account_health_service.partial_account_store, "partial_account_pool", return_value={"items": [], "stats": {}}),
        ):
            result = partial_account_health_service.check_partial_accounts(["partial-dead"], concurrency=1)

        self.assertEqual(result["stats"]["dead"], 1)
        updates = update.call_args.args[1]
        self.assertEqual(updates["died_at"], "2026-07-14T12:00:00+00:00")
        self.assertNotIn("status", updates)

    def test_sticky_proxy_failure_does_not_run_health_check_directly(self) -> None:
        account = {
            "id": "partial-sticky",
            "email": "sticky@example.com",
            "access_token": "web-access-token",
            "status": "oauth_failed",
            "registration_proxy": {
                "source": "managed_mihomo",
                "node_name": "registered-node",
            },
        }
        assignment = {
            "proxy": "",
            "source": "registration_sticky",
            "label": "registered-node",
            "sticky": True,
            "sticky_failure": True,
            "error": "registered node unavailable",
        }
        with (
            patch.object(partial_account_health_service.partial_account_store, "list_partial_accounts", return_value=[account]),
            patch.object(partial_account_health_service.proxy_pool_store, "get_proxy_pool_config", return_value={"enabled": False}),
            patch.object(partial_account_health_service.proxy_pool_store, "assign_proxy", return_value=assignment) as assign_proxy,
            patch.object(partial_account_health_service, "_local_health_and_quota") as check,
            patch.object(partial_account_health_service.partial_account_store, "update_partial_account") as update,
            patch.object(partial_account_health_service.partial_account_store, "partial_account_pool", return_value={"items": [], "stats": {}}),
        ):
            result = partial_account_health_service.check_partial_accounts(["partial-sticky"], concurrency=1)

        check.assert_not_called()
        self.assertEqual(result["results"][0]["status"], "network_error")
        self.assertEqual(assign_proxy.call_args.kwargs["account"]["registration_proxy"], account["registration_proxy"])
        updates = update.call_args.args[1]
        self.assertTrue(updates["health_proxy_sticky"])
        self.assertIn("registered node unavailable", updates["health_error"])

    def test_transient_failure_preserves_last_confirmed_alive_status(self) -> None:
        account = {
            "id": "partial-alive",
            "email": "alive@example.com",
            "access_token": "web-access-token",
            "status": "oauth_failed",
            "health_status": "ok",
            "health_code": 200,
            "health_error": "",
            "health_checked_at": "2026-07-14T12:00:00+00:00",
        }
        transient = {
            "ok": False,
            "status": "blocked",
            "status_code": 403,
            "error": "proxy blocked",
            "updates": {
                "health_status": "blocked",
                "health_code": 403,
                "health_error": "proxy blocked",
                "health_checked_at": "2026-07-14T13:00:00+00:00",
            },
        }
        with (
            patch.object(partial_account_health_service.partial_account_store, "list_partial_accounts", return_value=[account]),
            patch.object(partial_account_health_service.proxy_pool_store, "get_proxy_pool_config", return_value={"enabled": False}),
            patch.object(partial_account_health_service.proxy_pool_store, "assign_proxy", return_value={"proxy": "http://127.0.0.1:42000", "source": "account"}),
            patch.object(partial_account_health_service, "_local_health_and_quota", return_value=transient),
            patch.object(partial_account_health_service.partial_account_store, "update_partial_account") as update,
            patch.object(partial_account_health_service.partial_account_store, "partial_account_pool", return_value={"items": [], "stats": {}}),
        ):
            partial_account_health_service.check_partial_accounts(["partial-alive"], concurrency=1)

        updates = update.call_args.args[1]
        self.assertEqual(updates["health_status"], "ok")
        self.assertEqual(updates["health_code"], 200)
        self.assertEqual(updates["health_checked_at"], "2026-07-14T12:00:00+00:00")
        self.assertEqual(updates["last_health_attempt_status"], "blocked")
        self.assertEqual(updates["last_health_attempt_code"], 403)
        self.assertEqual(updates["last_health_attempt_error"], "proxy blocked")

    def test_schedule_settings_are_persisted_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_file = Path(temp_dir) / "partial_health_settings.json"
            scheduler = partial_account_health_service.PartialAccountHealthScheduler(settings_file)
            state = scheduler.update_settings({
                "interval_minutes": 15,
                "concurrency": 99,
                "refresh_quota": True,
                "method": "sub2api",
            })
            reloaded = partial_account_health_service.PartialAccountHealthScheduler(settings_file).get_state()

        self.assertEqual(state["settings"]["interval_minutes"], 15)
        self.assertEqual(state["settings"]["concurrency"], 32)
        self.assertTrue(reloaded["settings"]["refresh_quota"])
        self.assertEqual(reloaded["settings"]["method"], "sub2api")

    def test_sub2api_method_skips_local_proxy_assignment(self) -> None:
        account = {
            "id": "partial-sub2api",
            "email": "sub2api@example.com",
            "sub2api_account_id": 12,
        }
        health = {
            "ok": True,
            "status": "ok",
            "status_code": 200,
            "updates": {"health_status": "ok", "health_checked_at": "2026-07-14T12:00:00+00:00"},
        }
        with (
            patch.object(partial_account_health_service.partial_account_store, "list_partial_accounts", return_value=[account]),
            patch.object(partial_account_health_service.proxy_pool_store, "get_proxy_pool_config", return_value={"enabled": False}),
            patch.object(partial_account_health_service.proxy_pool_store, "assign_proxy") as assign_proxy,
            patch.object(partial_account_health_service, "_sub2api_health", return_value=health) as check,
            patch.object(partial_account_health_service.partial_account_store, "update_partial_account"),
            patch.object(partial_account_health_service.partial_account_store, "partial_account_pool", return_value={"items": [], "stats": {}}),
        ):
            result = partial_account_health_service.check_partial_accounts(["partial-sub2api"], method="sub2api", concurrency=1)

        self.assertEqual(result["stats"]["alive"], 1)
        check.assert_called_once_with(account, mode="")
        assign_proxy.assert_not_called()

    def test_background_task_can_be_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scheduler = partial_account_health_service.PartialAccountHealthScheduler(Path(temp_dir) / "partial_health_settings.json")

            def wait_for_stop(*_args, **kwargs):
                stop_event = kwargs["stop_event"]
                stop_event.wait(1)
                return {"stats": {"total": 1, "alive": 0, "dead": 0, "failed": 0}, "stopped": True}

            with patch.object(partial_account_health_service, "check_partial_accounts", side_effect=wait_for_stop):
                started = scheduler.start_task({"selectors": ["partial-1"], "method": "local", "concurrency": 1})
                self.assertTrue(started["task"]["enabled"])
                stopped = scheduler.stop_task()
                self.assertTrue(stopped["task"]["stop_requested"])
                scheduler._task.join(timeout=2)
                final = scheduler.get_state()

        self.assertFalse(final["task"]["enabled"])
        self.assertFalse(final["task"]["running"])
        self.assertEqual(final["task"]["stats"]["total"], 1)

    def test_scheduler_waits_before_first_automatic_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scheduler = partial_account_health_service.PartialAccountHealthScheduler(Path(temp_dir) / "partial_health_settings.json")
            with patch.object(partial_account_health_service, "check_partial_accounts") as check:
                scheduler.start_schedule({"interval_minutes": 1, "concurrency": 1})
                time.sleep(0.05)
                scheduler.stop_schedule()
                time.sleep(0.05)

        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
