from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services import sqlite_store
from services.atomic_json_store import read_json, write_json


class SqliteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.original_data_dir = sqlite_store.DATA_DIR
        self.original_database_file = sqlite_store.DATABASE_FILE
        self.original_backend = sqlite_store.STORAGE_BACKEND
        sqlite_store.DATA_DIR = self.root
        sqlite_store.DATABASE_FILE = self.root / "moyu.sqlite3"
        sqlite_store.STORAGE_BACKEND = "sqlite"
        sqlite_store._schema_ready_for.clear()

    def tearDown(self) -> None:
        sqlite_store.DATA_DIR = self.original_data_dir
        sqlite_store.DATABASE_FILE = self.original_database_file
        sqlite_store.STORAGE_BACKEND = self.original_backend
        sqlite_store._schema_ready_for.clear()
        self.temp_dir.cleanup()

    def test_json_collection_is_migrated_and_paged(self) -> None:
        path = self.root / "registered_accounts.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "items": [
                {"email": "a@example.com", "created_at": "2026-01-01T00:00:00Z"},
                {"email": "b@example.com", "created_at": "2026-01-02T00:00:00Z"},
            ],
        }), encoding="utf-8")

        migrated = read_json(path, [], expected_schema_version=1)
        page, total = sqlite_store.collection_page(path, limit=1, offset=1) or ([], 0)

        self.assertEqual(len(migrated["items"]), 2)
        self.assertEqual(total, 2)
        self.assertEqual(page[0]["email"], "b@example.com")
        self.assertTrue(path.with_suffix(".json.pre-sqlite.bak").exists())
        self.assertEqual(sqlite_store.storage_status()["collection_items"], 2)

    def test_sqlite_becomes_authoritative_after_write(self) -> None:
        path = self.root / "registered_accounts.json"
        path.write_text(json.dumps({"schema_version": 1, "items": [{"email": "old@example.com"}]}), encoding="utf-8")
        read_json(path, [], expected_schema_version=1)

        write_json(path, [{"email": "new@example.com"}], schema_version=1)
        path.write_text(json.dumps({"schema_version": 1, "items": [{"email": "stale@example.com"}]}), encoding="utf-8")

        current = read_json(path, [], expected_schema_version=1)
        self.assertEqual(current["items"][0]["email"], "new@example.com")


if __name__ == "__main__":
    unittest.main()
