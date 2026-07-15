from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services import partial_account_store


class PartialAccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_file = partial_account_store.PARTIAL_ACCOUNTS_FILE
        self._temp_dir = tempfile.TemporaryDirectory()
        partial_account_store.PARTIAL_ACCOUNTS_FILE = Path(self._temp_dir.name) / "partial_accounts.json"

    def tearDown(self) -> None:
        partial_account_store.PARTIAL_ACCOUNTS_FILE = self._original_file
        self._temp_dir.cleanup()

    def test_registration_failure_is_not_stored(self) -> None:
        result = partial_account_store.upsert_partial_account({
            "email": "failed@example.com",
            "password": "secret",
            "status": "failed_after_partial",
            "stage": "email_verified",
            "reason": "otp timeout",
        })
        self.assertIsNone(result)
        self.assertEqual(partial_account_store.list_partial_accounts(), [])

    def test_web_success_with_oauth_failure_is_stored(self) -> None:
        result = partial_account_store.upsert_partial_account({
            "email": "oauth@example.com",
            "password": "secret",
            "status": "oauth_missing",
            "stage": "oauth_failed",
            "web_login_ok": True,
            "access_token": "web-access-token",
            "chat_test_method": "conversation",
        })
        self.assertIsNotNone(result)
        self.assertTrue(result["web_login_ok"])
        self.assertEqual(result["status"], "oauth_missing")

    def test_listing_prunes_legacy_registration_failures(self) -> None:
        path = partial_account_store.PARTIAL_ACCOUNTS_FILE
        path.write_text(json.dumps([
            {"email": "failed@example.com", "status": "free_wu_failed", "stage": "free_wu_failed"},
            {"email": "oauth@example.com", "status": "oauth_failed", "stage": "oauth_failed", "web_login_ok": True},
        ]), encoding="utf-8")
        items = partial_account_store.list_partial_accounts()
        self.assertEqual([item["email"] for item in items], ["oauth@example.com"])
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 1)
        self.assertEqual([item["email"] for item in persisted["items"]], ["oauth@example.com"])

    def test_health_updates_preserve_partial_status_and_update_stats(self) -> None:
        saved = partial_account_store.upsert_partial_account({
            "email": "alive@example.com",
            "status": "oauth_failed",
            "stage": "sub2api_oauth_failed",
            "web_login_ok": True,
            "access_token": "web-access-token",
        })
        self.assertIsNotNone(saved)

        updated = partial_account_store.update_partial_account(
            "alive@example.com",
            {
                "health_status": "ok",
                "health_code": 200,
                "health_checked_at": "2026-07-14T12:00:00+00:00",
                "health_method": "local",
            },
        )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], "oauth_failed")
        self.assertEqual(updated["health_status"], "ok")
        self.assertEqual(updated["health_code"], 200)
        pool = partial_account_store.partial_account_pool()
        self.assertEqual(pool["stats"]["health_alive"], 1)
        self.assertEqual(pool["stats"]["health_dead"], 0)

    def test_terminal_health_backfills_death_time_and_fixed_lifespan(self) -> None:
        saved = partial_account_store.upsert_partial_account({
            "email": "dead@example.com",
            "status": "oauth_failed",
            "stage": "sub2api_oauth_failed",
            "web_login_ok": True,
            "registered_at": "2026-07-13T12:00:00+00:00",
            "health_status": "banned",
            "health_checked_at": "2026-07-14T12:00:00+00:00",
        })

        self.assertIsNotNone(saved)
        self.assertEqual(saved["registered_at"], "2026-07-13T12:00:00+00:00")
        self.assertEqual(saved["died_at"], "2026-07-14T12:00:00+00:00")
        self.assertEqual(saved["lifespan_seconds"], 86400)
        self.assertEqual(partial_account_store.partial_account_pool()["stats"]["health_dead"], 1)

    def test_import_partial_accounts_supports_text_json_and_updates(self) -> None:
        first = partial_account_store.import_partial_accounts(
            "first@example.com----secret----refresh-1----access-1"
        )
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["updated"], 0)
        saved = partial_account_store.list_partial_accounts()[0]
        self.assertEqual(saved["email"], "first@example.com")
        self.assertEqual(saved["source"], "manual_partial")
        self.assertTrue(saved["web_login_ok"])

        second = partial_account_store.import_partial_accounts(json.dumps({
            "items": [
                {"email": "first@example.com", "password": "changed"},
                {"email": "second@example.com", "status": "oauth_missing"},
            ]
        }))
        self.assertEqual(second["imported"], 1)
        self.assertEqual(second["updated"], 1)
        by_email = {item["email"]: item for item in partial_account_store.list_partial_accounts()}
        self.assertEqual(by_email["first@example.com"]["password"], "changed")
        self.assertEqual(by_email["second@example.com"]["status"], "oauth_missing")

        sub2api = partial_account_store.import_partial_accounts(json.dumps({
            "type": "sub2api-data",
            "accounts": [{
                "name": "third@example.com",
                "platform": "openai",
                "type": "oauth",
                "credentials": {"email": "third@example.com", "access_token": "access-3"},
            }],
        }))
        self.assertEqual(sub2api["imported"], 1)
        third = next(item for item in partial_account_store.list_partial_accounts() if item["email"] == "third@example.com")
        self.assertEqual(third["access_token"], "access-3")

    def test_bulk_delete_and_prune_terminal_accounts(self) -> None:
        for email, health in (
            ("alive@example.com", "ok"),
            ("dead@example.com", "banned"),
            ("other@example.com", "unknown"),
        ):
            partial_account_store.upsert_partial_account({
                "email": email,
                "status": "oauth_failed",
                "stage": "oauth_failed",
                "web_login_ok": True,
                "health_status": health,
            })

        pruned = partial_account_store.prune_terminal_partial_accounts()
        self.assertEqual(pruned["removed"], 1)
        self.assertEqual({item["email"] for item in pruned["items"]}, {"alive@example.com", "other@example.com"})

        removed = partial_account_store.delete_partial_accounts(["alive@example.com", "missing@example.com"])
        self.assertEqual(removed["removed"], 1)
        self.assertEqual([item["email"] for item in removed["items"]], ["other@example.com"])


if __name__ == "__main__":
    unittest.main()
