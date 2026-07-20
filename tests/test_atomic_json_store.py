from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.atomic_json_store import JsonStoreReadError, read_json, write_json


class AtomicJsonStoreTests(unittest.TestCase):
    def test_utf8_bom_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text('{"version": 1}', encoding="utf-8-sig")

            self.assertEqual(read_json(path, {}), {"version": 1})

    def test_successful_write_keeps_previous_version_as_backup(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text('{"version": 1}\n', encoding="utf-8")

            write_json(path, {"version": 2})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 2})
            self.assertEqual(json.loads(path.with_suffix(".json.bak").read_text(encoding="utf-8")), {"version": 1})

    def test_failed_replace_does_not_truncate_original_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            original = '{"version": 1}\n'
            path.write_text(original, encoding="utf-8")
            real_replace = os.replace

            def replace(source, destination):
                if Path(destination) == path:
                    raise OSError("simulated replace failure")
                return real_replace(source, destination)

            with patch("services.atomic_json_store.os.replace", side_effect=replace):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    write_json(path, {"version": 2})

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(root).glob("*.tmp")), [])

    def test_corrupt_primary_is_restored_from_backup(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text('{"version": 1}\n', encoding="utf-8")
            write_json(path, {"version": 2})
            path.write_text('{broken', encoding="utf-8")

            recovered = read_json(path, {})

            self.assertEqual(recovered, {"version": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 1})

    def test_corrupt_primary_and_backup_raise(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text('{broken', encoding="utf-8")
            path.with_suffix(".json.bak").write_text('{also-broken', encoding="utf-8")

            with self.assertRaises(JsonStoreReadError):
                read_json(path, {})

    def test_schema_version_is_written_without_mutating_input(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            payload = {"value": 1}
            write_json(path, payload, schema_version=1)

            self.assertEqual(payload, {"value": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 1, "schema_version": 1})

    def test_read_migrates_legacy_data_without_changing_return_shape(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text('[{"value": 1}]', encoding="utf-8")

            result = read_json(path, [], expected_schema_version=1)

            self.assertEqual(result, [{"value": 1}])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"schema_version": 1, "items": [{"value": 1}]},
            )
            self.assertEqual(json.loads(path.with_suffix(".json.bak").read_text(encoding="utf-8")), [{"value": 1}])

    def test_future_schema_does_not_restore_older_backup(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text('{"schema_version": 2, "value": "new"}\n', encoding="utf-8")
            path.with_suffix(".json.bak").write_text('{"schema_version": 1, "value": "old"}\n', encoding="utf-8")

            with self.assertRaisesRegex(JsonStoreReadError, "newer than supported"):
                read_json(path, {}, expected_schema_version=1)

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"schema_version": 2, "value": "new"})

    def test_unchanged_file_reuses_parsed_json_and_external_change_invalidates(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text('{"value": 1}\n', encoding="utf-8")

            with patch("services.atomic_json_store.json.loads", wraps=json.loads) as loads:
                self.assertEqual(read_json(path, {}), {"value": 1})
                self.assertEqual(read_json(path, {}), {"value": 1})
                self.assertEqual(loads.call_count, 1)

                path.write_text('{"value": 2, "changed": true}\n', encoding="utf-8")
                self.assertEqual(read_json(path, {}), {"value": 2, "changed": True})
                self.assertEqual(loads.call_count, 2)


if __name__ == "__main__":
    unittest.main()
