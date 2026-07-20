from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.paypal_protocol.phone_pool import (
    PhonePoolStore,
    extract_paypal_otp,
    normalize_pool_phone,
    parse_phone_pool_line,
)


class PaypalPhonePoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "phone-pool.json"
        self.store = PhonePoolStore(self.path)
        self.owner = "admin"

    def test_supported_country_detection_includes_th_and_id(self) -> None:
        self.assertEqual(normalize_pool_phone("+66912345678"), ("+66912345678", "TH"))
        self.assertEqual(normalize_pool_phone("+6281212345678"), ("+6281212345678", "ID"))

    def test_import_persists_and_hides_url_query(self) -> None:
        result = self.store.import_text(
            self.owner,
            "+66912345678----https://sms.example.com/code?token=private",
        )
        self.assertEqual(result["imported"], 1)
        public = self.store.list(self.owner)
        self.assertNotIn("private", json.dumps(public))
        restored = PhonePoolStore(self.path)
        self.assertEqual(restored.load(), 1)

    def test_reserve_failure_reset_and_delete(self) -> None:
        self.store.import_text(
            self.owner,
            "+447911123456----https://sms.example.com/code",
        )
        entry = self.store.reserve(self.owner, "GB", "job-1")
        self.store.mark(
            entry.id,
            "job-1",
            "otp_rejected",
            "验证码被拒绝",
            release=True,
            increment_failure=True,
        )
        self.assertFalse(self.store.list(self.owner)[0]["available"])
        self.assertTrue(self.store.reset(self.owner, entry.id)["available"])
        self.store.delete(self.owner, entry.id)
        self.assertEqual(self.store.list(self.owner), [])

    def test_extracts_paypal_otp(self) -> None:
        self.assertEqual(
            extract_paypal_otp({"message": "Your PayPal verification code is 371841"}),
            "371841",
        )

    def test_rejects_private_code_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "内网"):
            parse_phone_pool_line("+66912345678----http://127.0.0.1/code")


if __name__ == "__main__":
    unittest.main()
