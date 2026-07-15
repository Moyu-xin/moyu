from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import k12_config_store


class K12ConfigStoreTests(unittest.TestCase):
    def test_legacy_sub2api_fields_are_migrated_once_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root)
            k12_path = data_dir / "k12_config.json"
            sub2api_path = data_dir / "sub2api_config.json"
            sub2api_path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "base_url": "http://sub2api.invalid",
                        "k12_mode": True,
                        "k12_team_ids": {"outlook": ["outlook-team"], "gmail": ["gmail-team"]},
                        "k12_join_route": "accept",
                        "k12_accept_invite": False,
                        "k12_invite_timeout": 45,
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(k12_config_store, "K12_CONFIG_FILE", k12_path),
                patch.object(k12_config_store, "LEGACY_SUB2API_CONFIG_FILE", sub2api_path),
            ):
                config = k12_config_store.get_k12_config()

                self.assertTrue(config["enabled"])
                self.assertEqual(config["team_ids"]["outlook"], ["outlook-team"])
                self.assertEqual(config["team_ids"]["gmail"], ["gmail-team"])
                self.assertEqual(config["join_route"], "accept")
                self.assertFalse(config["accept_invite"])
                self.assertEqual(config["invite_timeout"], 45)
                self.assertTrue(config["migrated_from_sub2api_at"])

                cleaned = json.loads(sub2api_path.read_text(encoding="utf-8"))
                self.assertEqual(cleaned["base_url"], "http://sub2api.invalid")
                self.assertFalse(any(key in cleaned for key in k12_config_store.LEGACY_KEYS))

                reintroduced = {**cleaned, "k12_mode": False, "k12_invite_timeout": 999}
                sub2api_path.write_text(json.dumps(reintroduced), encoding="utf-8")
                second = k12_config_store.get_k12_config()
                self.assertTrue(second["enabled"])
                self.assertEqual(second["invite_timeout"], 45)
                cleaned_again = json.loads(sub2api_path.read_text(encoding="utf-8"))
                self.assertFalse(any(key in cleaned_again for key in k12_config_store.LEGACY_KEYS))

    def test_update_normalizes_team_ids_and_domain_selection(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root)
            with (
                patch.object(k12_config_store, "K12_CONFIG_FILE", data_dir / "k12_config.json"),
                patch.object(k12_config_store, "LEGACY_SUB2API_CONFIG_FILE", data_dir / "sub2api_config.json"),
            ):
                config = k12_config_store.update_k12_config(
                    {
                        "team_ids": {
                            "outlook": ["outlook-team", "outlook-team", ""],
                            "gmail": ["gmail-team"],
                        },
                        "invite_timeout": 1,
                    }
                )

                self.assertEqual(config["team_ids"]["outlook"], ["outlook-team"])
                self.assertEqual(config["invite_timeout"], 10)
                self.assertEqual(k12_config_store.choose_k12_team_id("a@outlook.com", config), "outlook-team")
                self.assertEqual(k12_config_store.choose_k12_team_id("a@gmail.com", config), "gmail-team")
                self.assertEqual(k12_config_store.choose_k12_team_id("a@example.com", config), "")


if __name__ == "__main__":
    unittest.main()
