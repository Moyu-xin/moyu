from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services import cpa_store


class CpaStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_config = cpa_store.CPA_CONFIG_FILE
        self._original_health = cpa_store.CPA_HEALTH_FILE
        self._temp_dir = tempfile.TemporaryDirectory()
        root = Path(self._temp_dir.name)
        cpa_store.CPA_CONFIG_FILE = root / "cpa_config.json"
        cpa_store.CPA_HEALTH_FILE = root / "cpa_health.json"

    def tearDown(self) -> None:
        cpa_store.CPA_CONFIG_FILE = self._original_config
        cpa_store.CPA_HEALTH_FILE = self._original_health
        self._temp_dir.cleanup()

    def test_management_url_normalization(self) -> None:
        self.assertEqual(cpa_store._auth_files_url("http://127.0.0.1:8317"), "http://127.0.0.1:8317/v0/management/auth-files")
        self.assertEqual(cpa_store._auth_files_url("http://127.0.0.1:8317/v0/management"), "http://127.0.0.1:8317/v0/management/auth-files")
        self.assertEqual(cpa_store._api_call_url("http://127.0.0.1:8317"), "http://127.0.0.1:8317/v0/management/api-call")

    def test_secret_is_preserved_and_redacted(self) -> None:
        first = cpa_store.update_cpa_config({"base_url": "http://localhost:8317", "management_key": "secret-key"})
        self.assertTrue(first["has_management_key"])
        self.assertNotIn("management_key", first)
        cpa_store.update_cpa_config({"management_key": "", "concurrency": 7})
        persisted = json.loads(cpa_store.CPA_CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(persisted["management_key"], "secret-key")
        self.assertEqual(persisted["concurrency"], 7)

    def test_tls_verification_is_enabled_by_default_and_explicitly_configurable(self) -> None:
        initial = cpa_store.get_cpa_config()
        self.assertFalse(initial["skip_ssl_verify"])
        updated = cpa_store.update_cpa_config({"skip_ssl_verify": True})
        self.assertTrue(updated["skip_ssl_verify"])

    def test_build_cpa_auth_file(self) -> None:
        item = cpa_store.build_cpa_auth_file({
            "email": "user@example.com",
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id",
            "chatgpt_account_id": "workspace-1",
        })
        self.assertEqual(item["type"], "codex")
        self.assertEqual(item["email"], "user@example.com")
        self.assertEqual(item["account_id"], "workspace-1")
        self.assertEqual(item["refresh_token"], "refresh")


if __name__ == "__main__":
    unittest.main()
