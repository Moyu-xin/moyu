from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services import mailbox_pool_store, registered_account_store
from services.register import mail_provider


class InventoryOutboundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.account_file = registered_account_store.REGISTERED_ACCOUNTS_FILE
        self.mailbox_file = mailbox_pool_store.MAILBOX_POOL_FILE
        self.state_file = mail_provider.OUTLOOK_TOKEN_USED_FILE
        registered_account_store.REGISTERED_ACCOUNTS_FILE = root / "registered_accounts.json"
        registered_account_store._pool_cache = None
        registered_account_store._stats_cache = None
        mailbox_pool_store.MAILBOX_POOL_FILE = root / "mailbox_pool.json"
        mail_provider.OUTLOOK_TOKEN_USED_FILE = root / "outlook_token_used.json"

    def tearDown(self) -> None:
        registered_account_store.REGISTERED_ACCOUNTS_FILE = self.account_file
        registered_account_store._pool_cache = None
        registered_account_store._stats_cache = None
        mailbox_pool_store.MAILBOX_POOL_FILE = self.mailbox_file
        mail_provider.OUTLOOK_TOKEN_USED_FILE = self.state_file
        self.temp_dir.cleanup()

    def test_account_quantity_batch_and_outbound_mark(self) -> None:
        for index in range(3):
            registered_account_store.add_registered_account({
                "email": f"user{index}@example.com",
                "access_token": f"token-{index}",
                "registration_batch_id": "register-batch-a" if index < 2 else "register-batch-b",
                "created_at": f"2026-07-19T00:00:0{index}Z",
            })

        selected = registered_account_store.select_accounts_for_outbound(
            quantity=1,
            registration_batch_id="register-batch-a",
        )
        marked = registered_account_store.mark_accounts_outbound(selected, export_format="sub2api")
        batches = {item["id"]: item for item in registered_account_store.registration_batches()}

        self.assertEqual(len(selected), 1)
        self.assertEqual(marked["marked"], 1)
        self.assertEqual(batches["register-batch-a"]["available"], 1)
        self.assertEqual(batches["register-batch-a"]["outbound"], 1)

    def test_account_preview_page_keeps_full_stats_and_platform_counts(self) -> None:
        registered_account_store.add_registered_account({
            "email": "chatgpt@example.com",
            "platform": "openai",
            "access_token": "token-openai",
        })
        registered_account_store.add_registered_account({
            "email": "grok-one@example.com",
            "platform": "grok",
            "sso_token": "sso-one",
        })
        registered_account_store.add_registered_account({
            "email": "grok-two@example.com",
            "platform": "grok",
            "sso_token": "sso-two",
        })

        preview = registered_account_store.account_pool_page(limit=2)

        self.assertEqual(len(preview["accounts"]), 2)
        self.assertEqual(preview["stats"]["total"], 3)
        self.assertEqual(preview["stats"]["by_platform"], {"openai": 1, "grok": 2})
        self.assertTrue(preview["page_info"]["partial"])

    def test_mailbox_split_alias_counts_and_outbound(self) -> None:
        mailbox_pool_store.import_outlook_text(
            "base@outlook.com----password----client-id----refresh-token",
            group="batch-a",
        )
        mail_provider._save_outlook_token_state({
            "base+cgpt001@outlook.com": {"state": "used", "reason": "", "updated_at": "2026-07-19T00:00:00Z"},
            "base+cgpt002@outlook.com": {"state": "existing_account", "reason": "", "updated_at": "2026-07-19T00:00:01Z"},
            "base+cgpt003@outlook.com": {"state": "failed", "reason": "blocked", "updated_at": "2026-07-19T00:00:02Z"},
        })

        item = mailbox_pool_store.list_outlook_mailboxes()["items"][0]
        selected = mailbox_pool_store.select_mailboxes_for_outbound(quantity=1)
        marked = mailbox_pool_store.mark_mailboxes_outbound(selected, export_format="credentials")

        self.assertEqual(item["split_alias_generated"], 3)
        self.assertEqual(item["split_alias_used"], 2)
        self.assertEqual(item["split_alias_failed"], 1)
        self.assertEqual(marked["marked"], 1)
        self.assertEqual(mailbox_pool_store.mailbox_batches(provider_type="outlook_token")[0]["outbound"], 1)


if __name__ == "__main__":
    unittest.main()
