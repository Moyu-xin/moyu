from __future__ import annotations

import unittest

from services.security_redaction import redact_text, redact_value


class SecurityRedactionTests(unittest.TestCase):
    def test_nested_secret_fields_are_redacted(self) -> None:
        redacted = redact_value({
            "access_token": "secret-access",
            "nested": {"password": "secret-password", "message": "safe"},
        })
        self.assertEqual(redacted["access_token"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["password"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["message"], "safe")

    def test_bearer_jwt_and_query_code_are_redacted(self) -> None:
        text = "Bearer token.value code=oauth-code access_token=plain eyJabcdefgh.abcdefgh.abcdefgh"
        redacted = redact_text(text)
        self.assertNotIn("token.value", redacted)
        self.assertNotIn("oauth-code", redacted)
        self.assertNotIn("plain", redacted)
        self.assertNotIn("eyJabcdefgh", redacted)


if __name__ == "__main__":
    unittest.main()
