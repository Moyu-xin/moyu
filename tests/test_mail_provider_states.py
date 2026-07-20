from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.register import mail_provider


class MailProviderStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "mailbox_states.json"
        self.path_patch = patch.object(mail_provider, "OUTLOOK_TOKEN_USED_FILE", self.state_file)
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def _gmail_mailbox(self) -> dict[str, str]:
        return {
            "provider": "gmail_oauth",
            "address": "base+cgpt001@gmail.com",
            "inbox_address": "base@gmail.com",
        }

    def test_gmail_success_is_marked_used(self) -> None:
        mailbox = self._gmail_mailbox()
        mail_provider._set_outlook_token_state(mailbox["address"], "in_use")

        mail_provider.mark_mailbox_result(mailbox, success=True)

        state = mail_provider.outlook_token_pool_state()
        self.assertEqual(state[mailbox["address"]]["state"], "used")

    def test_gmail_failure_is_marked_failed(self) -> None:
        mailbox = self._gmail_mailbox()
        mail_provider._set_outlook_token_state(mailbox["address"], "in_use")

        mail_provider.mark_mailbox_result(mailbox, success=False, error=RuntimeError("registration failed"))

        state = mail_provider.outlook_token_pool_state()
        self.assertEqual(state[mailbox["address"]]["state"], "failed")

    def test_gmail_token_failure_marks_alias_and_base_invalid(self) -> None:
        mailbox = self._gmail_mailbox()
        mail_provider._set_outlook_token_state(mailbox["address"], "in_use")

        error = mail_provider.GmailOAuthError("GmailOAuth 刷新失败: invalid_grant")
        mail_provider.mark_mailbox_result(mailbox, success=False, error=error)

        state = mail_provider.outlook_token_pool_state()
        self.assertEqual(state[mailbox["address"]]["state"], "token_invalid")
        self.assertEqual(state[mailbox["inbox_address"]]["state"], "token_invalid")

    def test_gmail_release_removes_in_use_state(self) -> None:
        mailbox = self._gmail_mailbox()
        mail_provider._set_outlook_token_state(mailbox["address"], "in_use")

        mail_provider.release_mailbox(mailbox)

        self.assertNotIn(mailbox["address"], mail_provider.outlook_token_pool_state())

    def test_gmail_credentials_are_normalized_for_provider_runtime(self) -> None:
        credentials = mail_provider.parse_gmail_credentials(
            "base@gmail.com----password----client-id----client-secret----refresh-token"
        )
        self.assertEqual(len(credentials), 1)
        self.assertEqual(credentials[0]["client_secret"], "client-secret")
        self.assertEqual(mail_provider._normalize_gmail_pool(credentials), credentials)

    def test_message_summary_includes_participants_and_full_body(self) -> None:
        summary = mail_provider._message_public_summary({
            "provider": "outlook_token",
            "mailbox": "alias@example.com",
            "subject": "Account verification",
            "sender": {"name": "Example Security", "address": "security@example.com"},
            "to": [
                {"emailAddress": {"name": "Alias", "address": "alias@example.com"}},
                "backup@example.com",
            ],
            "html_content": "<p>Hello Alias</p><p>Your code is ABC-123.</p>",
        })

        self.assertIsNotNone(summary)
        self.assertEqual(summary["sender"], "Example Security <security@example.com>")
        self.assertEqual(summary["recipients"], ["Alias <alias@example.com>", "backup@example.com"])
        self.assertEqual(summary["body_text"], "Hello Alias\nYour code is ABC-123.")
        self.assertEqual(summary["body_preview"], "Hello Alias Your code is ABC-123.")

    def test_graph_participants_keep_names_and_addresses(self) -> None:
        sender = mail_provider.OutlookTokenProvider._graph_sender({
            "from": {"emailAddress": {"name": "OpenAI", "address": "noreply@example.com"}},
        })

        self.assertEqual(sender, "OpenAI <noreply@example.com>")

    def test_openai_registration_modes_only_accept_six_digit_codes(self) -> None:
        for mode in ("free", "free_wu", "k12"):
            with self.subTest(mode=mode):
                self.assertTrue(mail_provider._code_matches_register_mode("030399", mode))
                self.assertFalse(mail_provider._code_matches_register_mode("ABC-123", mode))
                self.assertFalse(mail_provider._code_matches_register_mode("XAI0X1", mode))
                self.assertFalse(mail_provider._code_matches_register_mode("1234567", mode))

    def test_non_openai_registration_modes_keep_alphanumeric_codes(self) -> None:
        self.assertTrue(mail_provider._code_matches_register_mode("ABC-123", "grok"))
        self.assertTrue(mail_provider._code_matches_register_mode("XAI0X1", ""))


if __name__ == "__main__":
    unittest.main()
