from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services import sub2api_store


class Sub2ApiSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_config = sub2api_store.SUB2API_CONFIG_FILE
        self._original_health = sub2api_store.SUB2API_HEALTH_FILE
        self._temp_dir = tempfile.TemporaryDirectory()
        root = Path(self._temp_dir.name)
        sub2api_store.SUB2API_CONFIG_FILE = root / "sub2api_config.json"
        sub2api_store.SUB2API_HEALTH_FILE = root / "sub2api_health.json"

    def tearDown(self) -> None:
        sub2api_store.SUB2API_CONFIG_FILE = self._original_config
        sub2api_store.SUB2API_HEALTH_FILE = self._original_health
        self._temp_dir.cleanup()

    def test_tls_verification_is_enabled_by_default_and_explicitly_configurable(self) -> None:
        initial = sub2api_store.get_sub2api_config()
        self.assertFalse(initial["skip_ssl_verify"])
        updated = sub2api_store.update_sub2api_config({"skip_ssl_verify": True})
        self.assertTrue(updated["skip_ssl_verify"])


if __name__ == "__main__":
    unittest.main()
