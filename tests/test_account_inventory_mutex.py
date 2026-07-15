from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_inventory_service import AccountInventoryService


class AccountInventoryMutexTests(unittest.TestCase):
    def test_forget_account_removes_deleted_account_task_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = AccountInventoryService(Path(root) / "state.json", Path(root) / "settings.json")
            service._state.update({
                "enabled": False,
                "current_job": {"options": {"emails": ["codex-smoke-account@example.com"]}},
                "logs": [
                    {"text": "checked codex-smoke-account@example.com"},
                    {"text": "checked retained@example.com"},
                ],
                "results": [
                    {"email": "codex-smoke-account@example.com", "ok": True},
                    {"email": "retained@example.com", "ok": True},
                ],
                "stats": {**service._state["stats"], "done": 2, "success": 2, "total": 2},
            })

            service.forget_account("codex-smoke-account@example.com")

            self.assertIsNone(service._state["current_job"])
            self.assertEqual(service._state["results"], [{"email": "retained@example.com", "ok": True}])
            self.assertEqual(service._state["logs"], [{"text": "checked retained@example.com"}])
            self.assertEqual(service._state["stats"]["done"], 0)

    def test_parallel_bulk_request_returns_current_job(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            service = AccountInventoryService(Path(root) / "state.json", Path(root) / "settings.json")
            started = threading.Event()
            release = threading.Event()
            first_result: dict = {}

            def slow_check(*_args, **_kwargs):
                started.set()
                self.assertTrue(release.wait(5))
                return {"ok": True, "quota_ok": True, "status": "ok", "email": "user@example.com"}

            def run_first() -> None:
                first_result.update(service.check_accounts(refresh_quota=False, concurrency=1))

            with (
                patch.object(service, "_selected_accounts", return_value=[{"email": "user@example.com", "access_token": "at"}]),
                patch.object(service, "check_account", side_effect=slow_check),
                patch("services.account_inventory_service.proxy_pool_store.assign_proxy", return_value={"proxy": "", "source": "fallback"}),
                patch("services.account_inventory_service.account_pool", return_value={"accounts": [], "stats": {}}),
            ):
                thread = threading.Thread(target=run_first)
                thread.start()
                self.assertTrue(started.wait(5))

                duplicate = service.check_accounts(refresh_quota=True, concurrency=1)
                self.assertTrue(duplicate["busy"])
                self.assertFalse(duplicate["ok"])
                self.assertIsInstance(duplicate.get("current_job"), dict)

                release.set()
                thread.join(5)

            self.assertFalse(thread.is_alive())
            self.assertTrue(first_result["ok"])
            self.assertFalse(service._job_lock.locked())


if __name__ == "__main__":
    unittest.main()
