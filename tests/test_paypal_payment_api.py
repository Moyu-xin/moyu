from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import register as register_api


class PaypalPaymentApiTests(unittest.TestCase):
    def client(self) -> TestClient:
        with patch.object(register_api.k12_config_store, "ensure_migrated"):
            router = register_api.create_router()
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_payment_config_contract(self) -> None:
        expected = {"config": {"protocol_country": "TH", "smsbower_api_key": ""}}
        with (
            patch.object(register_api, "require_admin"),
            patch.object(
                register_api.paypal_payment_store.paypal_payment_service,
                "update_config",
                return_value=expected,
            ) as update,
        ):
            response = self.client().post(
                "/api/paypal-payments/config",
                json={
                    "protocol_country": "TH",
                    "smsbower_api_key": "12345678-secret",
                    "smsbower_max_price": 0.014,
                },
            )

        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with({
            "protocol_country": "TH",
            "smsbower_api_key": "12345678-secret",
            "smsbower_max_price": 0.014,
        })

    def test_payment_start_returns_bad_request_for_empty_selection(self) -> None:
        with (
            patch.object(register_api, "require_admin"),
            patch.object(
                register_api.paypal_payment_store.paypal_payment_service,
                "start",
                side_effect=RuntimeError("没有选择可支付的 Plus BA 结果"),
            ),
        ):
            response = self.client().post(
                "/api/paypal-payments/start",
                json={"source_task_ids": []},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "没有选择可支付的 Plus BA 结果")

    def test_manual_ba_start_contract(self) -> None:
        expected = {"results": [{"status": "queued"}]}
        payload = {
            "manual_items": [{
                "ba_url": "https://www.paypal.com/agreements/approve?ba_token=BA-1EE95436507955234",
                "country": "TH",
            }],
            "persist_config": False,
        }
        with (
            patch.object(register_api, "require_admin"),
            patch.object(
                register_api.paypal_payment_store.paypal_payment_service,
                "start",
                return_value=expected,
            ) as start,
        ):
            response = self.client().post("/api/paypal-payments/start", json=payload)

        self.assertEqual(response.status_code, 200)
        start.assert_called_once_with(payload)

    def test_phone_pool_import_contract(self) -> None:
        expected = {
            "import_result": {"imported": 1, "updated": 0, "invalid": [], "total": 1},
            "paypal_payments": {"phone_pool": {"total": 1}},
        }
        with (
            patch.object(register_api, "require_admin"),
            patch.object(
                register_api.paypal_payment_store.paypal_payment_service,
                "import_phone_pool",
                return_value=expected,
            ) as import_pool,
        ):
            response = self.client().post(
                "/api/paypal-payments/phone-pool/import",
                json={"text": "+66912345678----https://sms.example.com/code"},
            )

        self.assertEqual(response.status_code, 200)
        import_pool.assert_called_once_with(
            "+66912345678----https://sms.example.com/code"
        )


if __name__ == "__main__":
    unittest.main()
