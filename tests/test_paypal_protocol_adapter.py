from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from services.paypal_protocol.adapter import (
    PaypalProtocolConfigError,
    _sanitize_result,
    parse_ba_token,
    run_paypal_payment,
)
from services.paypal_protocol.br_new_bridge import verify_vendor_integrity
from services.paypal_protocol.phone_pool import PhonePoolStore


class PaypalProtocolAdapterTests(unittest.TestCase):
    def test_embedded_vendor_manifest_is_intact(self) -> None:
        self.assertEqual(verify_vendor_integrity(), 29)

    def test_parse_ba_token_accepts_token_and_paypal_url(self) -> None:
        token = "BA-1EE95436507955234"
        self.assertEqual(parse_ba_token(token), token)
        self.assertEqual(
            parse_ba_token(f"https://www.paypal.com/agreements/approve?ba_token={token}"),
            token,
        )

    def test_parse_ba_token_rejects_non_paypal_hosts_and_paths(self) -> None:
        with self.assertRaises(PaypalProtocolConfigError):
            parse_ba_token("https://example.com/agreements/approve?ba_token=BA-1EE95436507955234")
        with self.assertRaises(PaypalProtocolConfigError):
            parse_ba_token("https://www.paypal.com/signin?ba_token=BA-1EE95436507955234")

    def test_result_sanitizer_does_not_persist_redirect_secrets(self) -> None:
        raw = {
            "status": "success",
            "ba_token": "BA-1EE95436507955234",
            "ec_token": "EC-12345678901234567",
            "return_url": "https://merchant.invalid/secret",
            "final_redirect_url": "https://pay.invalid/client_secret",
            "identity_elevation": {"buyer_ready": True, "user_id": "USER123"},
        }
        result = _sanitize_result(raw, country="TH", ba_token=raw["ba_token"])

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["mode"], "TH_NEW")
        self.assertNotIn("return_url", result)
        self.assertNotIn("final_redirect_url", result)
        self.assertNotIn(raw["ba_token"], str(result))

    def test_phone_pool_callbacks_feed_worker_and_release_number(self) -> None:
        store = PhonePoolStore(Path(tempfile.mkdtemp()) / "phone-pool.json")
        store.import_text(
            "admin",
            "+66912345678----https://sms.example.com/code",
        )

        class FakeBridge:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def run(self):
                reserved = self.kwargs["reserve_number"]()
                self.kwargs["on_sms_sent"]()
                code = self.kwargs["wait_for_code"]("OTP")
                self.kwargs["on_confirmation"](code == "371841")
                return {"status": "success", "ba_token": "BA-1EE95436507955234"}

        with (
            patch("services.paypal_protocol.adapter.BrNewProtocolBridge", FakeBridge),
            patch.object(store, "wait_for_code", return_value="371841"),
        ):
            result = run_paypal_payment(
                ba_url="https://www.paypal.com/agreements/approve?ba_token=BA-1EE95436507955234",
                country="TH",
                proxy_url="http://user:pass@proxy.example.com:1000",
                sms_provider="phone_pool",
                phone_pool=store,
                phone_pool_owner_id="admin",
                phone_pool_job_id="job-1",
            )

        self.assertEqual(result["status"], "success")
        self.assertTrue(store.list("admin")[0]["available"])


if __name__ == "__main__":
    unittest.main()
