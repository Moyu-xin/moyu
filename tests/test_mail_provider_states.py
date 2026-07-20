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


if __name__ == "__main__":
    unittest.main()
