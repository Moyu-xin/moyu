from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services import paypal_payment_store, plus_link_store


BA_URL = "https://www.paypal.com/agreements/approve?ba_token=BA-1EE95436507955234"


class PaypalPaymentStoreTests(unittest.TestCase):
    def service(self) -> paypal_payment_store.PaypalPaymentService:
        return paypal_payment_store.PaypalPaymentService(
            Path(tempfile.mkdtemp()) / "paypal_payments.json"
        )

    def test_public_config_masks_smsbower_key(self) -> None:
        service = self.service()
        state = service.update_config({"smsbower_api_key": "12345678-secret"})

        self.assertEqual(state["config"]["smsbower_api_key"], "")
        self.assertTrue(state["config"]["smsbower_api_key_configured"])

    def test_candidates_are_loaded_from_plus_results(self) -> None:
        service = self.service()
        plus_state = {
            "config": {},
            "results": [
                {
                    "task_id": "plus-1",
                    "status": "success",
                    "email": "buyer@example.com",
                    "long_url": BA_URL,
                    "payment_method_country": "TH",
                    "finished_at": "2026-07-19T00:00:00+00:00",
                }
            ],
        }
        with patch.object(service, "_plus_state", return_value=plus_state):
            candidates = service._candidates()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source_task_id"], "plus-1")
        self.assertEqual(candidates[0]["protocol_mode"], "TH_NEW")
        self.assertTrue(candidates[0]["supported"])

    def test_auto_enqueue_is_disabled_by_default(self) -> None:
        service = self.service()
        with patch.object(service, "start") as start:
            service.enqueue_from_plus_result("plus-1")
        start.assert_not_called()

    def test_kookeey_sticky_rotation_replaces_session_and_keeps_ttl(self) -> None:
        proxy = (
            "gate.kookeey.info:1000:4234302-7bc6243afb:"
            "cc1a1e1a00-TH-60297705-5m"
        )
        rotated = plus_link_store._rotate_residential_proxy(
            proxy,
            country="TH",
            session_id="12345678",
            index=1,
            stage="paypal",
        )

        self.assertIn("cc1a1e1a00-TH-12345678-5m", rotated)
        self.assertNotIn("60297705", rotated)
        self.assertNotIn("-TH-12345678-5m-TH-", rotated)

    def test_payment_queue_consumes_plus_ba_without_mutating_plus_result(self) -> None:
        service = self.service()
        plus_result = {
            "task_id": "plus-1",
            "status": "success",
            "email": "buyer@example.com",
            "long_url": BA_URL,
            "payment_method_country": "TH",
        }
        plus_state = {"config": {}, "results": [plus_result]}
        service.update_config({
            "smsbower_api_key": "12345678-secret",
            "proxy_override": (
                "gate.kookeey.info:1000:4234302-7bc6243afb:"
                "cc1a1e1a00-TH-60297705-5m"
            ),
        })
        protocol_result = {
            "status": "success",
            "country": "TH",
            "mode": "TH_NEW",
            "ba_token_hash": "hash",
        }
        with (
            patch.object(service, "_plus_state", return_value=plus_state),
            patch(
                "services.paypal_protocol.run_paypal_payment",
                return_value=protocol_result,
            ),
        ):
            service.start({"source_task_ids": ["plus-1"]})
            deadline = time.time() + 3
            while service.get()["runner_alive"] and time.time() < deadline:
                time.sleep(0.02)
            state = service.get()

        self.assertFalse(state["runner_alive"])
        self.assertEqual(state["results"][0]["status"], "success")
        self.assertEqual(state["results"][0]["payment_result"], protocol_result)
        self.assertEqual(plus_result["status"], "success")
        self.assertNotIn("payment_result", plus_result)

    def test_manual_ba_can_start_without_plus_result(self) -> None:
        service = self.service()
        service.update_config({
            "smsbower_api_key": "12345678-secret",
            "proxy_override": "http://user:pass@proxy.example.com:1000",
        })
        protocol_result = {"status": "success", "country": "GB", "mode": "GB_NEW"}
        with (
            patch.object(service, "_plus_state", return_value={"config": {}, "results": []}),
            patch("services.paypal_protocol.run_paypal_payment", return_value=protocol_result),
        ):
            service.start({"manual_items": [{"ba_url": BA_URL, "country": "GB"}]})
            deadline = time.time() + 3
            while service.get()["runner_alive"] and time.time() < deadline:
                time.sleep(0.02)
            state = service.get()

        self.assertEqual(state["results"][0]["status"], "success")
        self.assertEqual(state["results"][0]["source_kind"], "manual")
        self.assertEqual(state["results"][0]["protocol_mode"], "GB_NEW")

    def test_phone_pool_provider_does_not_require_smsbower_key(self) -> None:
        service = self.service()
        service.import_phone_pool(
            "+447911123456----https://mail-api.example.com/code?token=secret"
        )
        service.update_config({
            "sms_provider": "phone_pool",
            "proxy_override": "http://user:pass@proxy.example.com:1000",
        })
        with (
            patch.object(service, "_plus_state", return_value={"config": {}, "results": []}),
            patch(
                "services.paypal_protocol.run_paypal_payment",
                return_value={"status": "success", "country": "GB", "mode": "GB_NEW"},
            ) as run,
        ):
            service.start({"manual_items": [{"ba_url": BA_URL, "country": "GB"}]})
            deadline = time.time() + 3
            while service.get()["runner_alive"] and time.time() < deadline:
                time.sleep(0.02)

        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["sms_provider"], "phone_pool")
        self.assertIs(kwargs["phone_pool"], service.phone_pool)
        self.assertEqual(kwargs["smsbower_api_key"], "")


if __name__ == "__main__":
    unittest.main()
