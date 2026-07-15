from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.register_service import RegisterService, _normalize


class RegisterTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.service = RegisterService(Path(self._temp_dir.name) / "register.json")

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    @staticmethod
    def _worker(index: int, *_args, **_kwargs) -> dict:
        return {"ok": True, "index": index, "result": {"email": f"user-{index}@example.com", "access_token": f"at-{index}"}}

    def _run(self, **config) -> None:
        self.service._config.update({"enabled": True, "threads": 1, "total": 10, **config})
        with (
            patch("services.register_service.proxy_pool_store.assign_proxy", return_value={"proxy": "", "source": "fallback"}),
            patch("services.register_service.openai_register.worker", side_effect=self._worker) as worker,
            patch("services.register_service.list_registered_accounts", return_value=[]),
            patch("services.register_service.partial_account_store.list_partial_accounts", return_value=[]),
        ):
            self.worker = worker
            self.service._run()

    def test_total_mode_stops_at_task_count(self) -> None:
        self._run(mode="total", total=2)
        self.assertEqual(self.worker.call_count, 2)
        self.assertEqual(self.service._config["stats"]["done"], 2)

    def test_available_mode_counts_only_health_passed_accounts(self) -> None:
        checks = [
            {"ok": True, "quota_ok": True},
            {"ok": False, "quota_ok": False, "status": "blocked"},
            {"ok": True, "quota_ok": True},
        ]
        with patch("services.account_inventory_service.account_inventory_service.check_account", side_effect=checks):
            self._run(mode="available", target_available=2, total=8)
        self.assertEqual(self.worker.call_count, 3)
        self.assertEqual(self.service._config["stats"]["current_available"], 2)

    def test_quota_mode_accumulates_remaining_quota(self) -> None:
        checks = [
            {"ok": True, "quota_ok": True, "quota": {"quota_remaining": 3}},
            {"ok": True, "quota_ok": True, "quota": {"quota_remaining": 4}},
        ]
        with patch("services.account_inventory_service.account_inventory_service.check_account", side_effect=checks):
            self._run(mode="quota", target_quota=7, total=8)
        self.assertEqual(self.worker.call_count, 2)
        self.assertEqual(self.service._config["stats"]["current_quota"], 7)

    def test_non_total_mode_respects_max_attempts(self) -> None:
        with patch("services.account_inventory_service.account_inventory_service.check_account", return_value={"ok": False, "quota_ok": False}):
            self._run(mode="available", target_available=5, total=2)
        self.assertEqual(self.worker.call_count, 2)
        self.assertEqual(self.service._config["stats"]["current_available"], 0)

    def test_normalize_caps_threads_and_drops_plus_placeholder(self) -> None:
        config = _normalize({"threads": 1000, "register_mode": "plus"})
        self.assertEqual(config["threads"], 64)
        self.assertEqual(config["register_mode"], "free")
        self.assertEqual(config["product_mode"], "partial")

    def test_normalize_migrates_free_wu_and_validates_product_mode(self) -> None:
        migrated = _normalize({"register_mode": "free_wu", "product_mode": "finished"})
        self.assertEqual(migrated["register_mode"], "free")
        self.assertEqual(migrated["product_mode"], "finished")

        invalid = _normalize({"register_mode": "free", "product_mode": "unknown"})
        self.assertEqual(invalid["product_mode"], "partial")

    def test_normalize_migrates_free_crash_to_finished_free(self) -> None:
        migrated = _normalize({"register_mode": "free_crash", "product_mode": "partial"})
        self.assertEqual(migrated["register_mode"], "free")
        self.assertEqual(migrated["product_mode"], "finished")

    def test_get_refreshes_registered_partial_and_stored_totals(self) -> None:
        with (
            patch("services.register_service.list_registered_accounts", return_value=[{"email": "finished@example.com"}]),
            patch("services.register_service.partial_account_store.list_partial_accounts", return_value=[{"email": "partial@example.com"}]),
        ):
            snapshot = self.service.get()
        self.assertEqual(snapshot["stats"]["registered_total"], 1)
        self.assertEqual(snapshot["stats"]["partial_total"], 1)
        self.assertEqual(snapshot["stats"]["stored_total"], 2)

    def test_unchanged_inventory_metrics_are_reused(self) -> None:
        with (
            patch("services.register_service.list_registered_accounts", return_value=[]) as registered,
            patch("services.register_service.partial_account_store.list_partial_accounts", return_value=[]) as partial,
        ):
            self.service.get()
            self.service.get()
        registered.assert_called_once_with(include_secret=False)
        partial.assert_called_once_with(include_secret=False)

    def test_proxy_hot_reload_preserves_running_tasks(self) -> None:
        runner = Mock()
        runner.is_alive.return_value = True
        self.service._runner = runner
        self.service._config["enabled"] = True
        self.service._config["stats"]["running"] = 3

        status = self.service.hot_reload_proxy_config()
        snapshot = self.service.get()

        self.assertTrue(status["running"])
        self.assertEqual(status["preserved_running_tasks"], 3)
        self.assertEqual(status["applies_to"], "new_tasks")
        self.assertTrue(status["task_snapshot_mode"])
        self.assertEqual(snapshot["runtime_config"]["last_hot_reload"]["revision"], status["revision"])

    def test_start_rejects_empty_selected_mailbox_group_before_launch(self) -> None:
        projected = {
            **self.service._config,
            "mailbox_group": "empty-group",
            "mail": {"registration_source": "outlook_token", "mailbox_group": "empty-group", "providers": []},
        }
        with patch("services.register_service.mailbox_pool_store.apply_to_register_config", return_value=projected):
            with self.assertRaisesRegex(RuntimeError, "empty-group"):
                self.service.start()
        self.assertFalse(self.service._config["enabled"])
        self.assertIsNone(self.service._runner)

    def test_start_rejects_mailbox_pool_when_all_credentials_are_unavailable(self) -> None:
        projected = {
            **self.service._config,
            "mailbox_group": "used-group",
            "mail": {
                "registration_source": "outlook_token",
                "mailbox_group": "used-group",
                "providers": [{
                    "enable": True,
                    "type": "outlook_token",
                    "mailboxes": "used@example.com----password----client-id----refresh-token",
                }],
            },
        }
        with (
            patch("services.register_service.mailbox_pool_store.apply_to_register_config", return_value=projected),
            patch("services.register_service.mail_provider.available_outlook_token_count", return_value=0),
        ):
            with self.assertRaisesRegex(RuntimeError, "used-group"):
                self.service.start()
        self.assertFalse(self.service._config["enabled"])
        self.assertIsNone(self.service._runner)


if __name__ == "__main__":
    unittest.main()
