from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import shipping_account_store
from services.register_service import RegisterService


def grok_account(email: str, *, health_status: str = "unknown") -> dict:
    return {
        "platform": "grok",
        "email": email,
        "password": "secret",
        "sso_token": "sso-token",
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "id_token": "id-token",
        "health_status": health_status,
        "registration_batch_id": "batch-1",
        "registration_batch_label": "Batch 1",
    }


class ShippingAccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_accounts_file = shipping_account_store.SHIPPING_ACCOUNTS_FILE
        self.old_config_file = shipping_account_store.SHIPPING_CONFIG_FILE
        shipping_account_store.SHIPPING_ACCOUNTS_FILE = self.root / "shipping_accounts.json"
        shipping_account_store.SHIPPING_CONFIG_FILE = self.root / "shipping_store_config.json"

    def tearDown(self) -> None:
        shipping_account_store.SHIPPING_ACCOUNTS_FILE = self.old_accounts_file
        shipping_account_store.SHIPPING_CONFIG_FILE = self.old_config_file
        self.temp.cleanup()

    def test_shipping_inventory_is_independent_and_deduplicated(self) -> None:
        first = shipping_account_store.add_account(grok_account("one@example.com"))
        shipping_account_store.add_account({**grok_account("one@example.com"), "note": "updated"})
        pool = shipping_account_store.pool(include_secret=False)
        self.assertEqual(pool["stats"]["total"], 1)
        self.assertEqual(pool["accounts"][0]["shipping_id"], first["shipping_id"])
        self.assertEqual(pool["accounts"][0]["note"], "updated")
        self.assertNotIn("sso_token", pool["accounts"][0])

    def test_outbound_only_exports_confirmed_alive_accounts(self) -> None:
        alive = shipping_account_store.add_account(grok_account("alive@example.com", health_status="ok"))
        shipping_account_store.add_account(grok_account("unknown@example.com", health_status="unknown"))
        result = shipping_account_store.export_accounts(format="grok2api", quantity=10)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["content"]["provider"], "grok_web")
        self.assertEqual(len(result["content"]["accounts"]), 1)
        updated = shipping_account_store.select_accounts([alive["shipping_id"]])[0]
        self.assertTrue(updated["outbound_at"])
        self.assertEqual(updated["outbound_format"], "grok2api")

    def test_store_publish_marks_only_acknowledged_source_ids(self) -> None:
        account = shipping_account_store.add_account(grok_account("store@example.com", health_status="alive"))
        shipping_account_store.update_config({"enabled": True, "base_url": "https://store.example", "api_key": "integration-secret"})
        response = {
            "success": True,
            "publishBatchId": "store-batch",
            "stockItems": [{"sourceId": account["shipping_id"], "id": "stock-1", "status": "available"}],
            "stockStats": {"total": 1, "available": 1, "checking": 0, "outbound": 0, "invalid": 0},
        }
        with patch.object(shipping_account_store, "_store_request", return_value=response) as request:
            result = shipping_account_store.publish_to_store(quantity=1)
        self.assertEqual(result["published"], 1)
        request.assert_called_once()
        updated = shipping_account_store.select_accounts([account["shipping_id"]])[0]
        self.assertEqual(updated["store_listing_id"], "stock-1")
        self.assertFalse(updated["outbound_at"])
        self.assertTrue(updated["store_synced_at"])

    def test_grok_shipping_route_does_not_write_finished_inventory(self) -> None:
        service = RegisterService.__new__(RegisterService)
        service._append_log = lambda *_args, **_kwargs: None
        service._is_enabled = lambda: True
        account = grok_account("route@example.com")
        task_config = {"register_mode": "grok", "mail": {}, "grok": {"delivery_target": "shipping"}, "registration_batch_id": "batch-route"}
        with (
            patch("services.register_service.register_grok_account", return_value={"ok": True, "index": 1, "result": account}),
            patch("services.register_service.shipping_account_store.add_account") as add_shipping,
            patch("services.register_service.shipping_account_store.get_config", return_value={"enabled": False}),
            patch("services.register_service.add_registered_account") as add_finished,
        ):
            result = service._run_registration_task(1, "http://127.0.0.1:7890", {}, "total", task_config)
        self.assertTrue(result["ok"])
        self.assertEqual(result["storage"], "shipping")
        add_shipping.assert_called_once()
        add_finished.assert_not_called()

    def test_grok_shipping_route_auto_syncs_when_store_is_enabled(self) -> None:
        service = RegisterService.__new__(RegisterService)
        service._append_log = lambda *_args, **_kwargs: None
        service._is_enabled = lambda: True
        account = grok_account("auto-store@example.com")
        task_config = {"register_mode": "grok", "mail": {}, "grok": {"delivery_target": "shipping"}}
        with (
            patch("services.register_service.register_grok_account", return_value={"ok": True, "index": 1, "result": account}),
            patch("services.register_service.shipping_account_store.add_account", return_value={"shipping_id": "ship-auto"}),
            patch("services.register_service.shipping_account_store.get_config", return_value={"enabled": True}),
            patch("services.register_service.shipping_account_store.publish_to_store", return_value={"ok": True, "published": 1}) as publish,
            patch("services.register_service.add_registered_account") as add_finished,
        ):
            result = service._run_registration_task(1, "http://127.0.0.1:7890", {}, "total", task_config)
        publish.assert_called_once_with(selectors=["ship-auto"], allow_unverified=True)
        self.assertTrue(result["warehouse"]["store"]["ok"])
        add_finished.assert_not_called()


if __name__ == "__main__":
    unittest.main()
