from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system as system_api
from services.config import ConfigStore
from services.system_maintenance_service import SystemMaintenanceService


class ConfigAuthKeyTests(unittest.TestCase):
    def test_config_key_can_be_rotated_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"auth-key": "a" * 32}), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CHATGPT2API_AUTH_KEY", None)
                store = ConfigStore(path)
                store.update_auth_key("a" * 32, "b" * 32)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["auth-key"], "b" * 32)

    def test_environment_key_cannot_be_rotated_from_the_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"auth-key": "a" * 32}), encoding="utf-8")
            with patch.dict(os.environ, {"CHATGPT2API_AUTH_KEY": "environment-key-value-123456"}):
                store = ConfigStore(path)
                with self.assertRaisesRegex(RuntimeError, "environment variable"):
                    store.update_auth_key("environment-key-value-123456", "b" * 32)


class SystemMaintenanceServiceTests(unittest.TestCase):
    def _service(self, root: Path) -> SystemMaintenanceService:
        return SystemMaintenanceService(root=root, settings_file=root / "data" / "system_settings.json")

    def test_memory_snapshot_includes_prediction_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = self._service(Path(temp))
            snapshot = service.memory_snapshot(registration_threads=4)
            self.assertEqual(snapshot["prediction"]["registration_threads"], 4)
            self.assertGreater(snapshot["prediction"]["predicted_peak_mb"], 0)
            self.assertEqual(len(snapshot["history"]), 1)

    def test_cleanup_only_removes_allowlisted_cache_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_file = root / "logs" / "test.log"
            python_cache = root / "api" / "__pycache__" / "test.pyc"
            frontend_cache = root / "web" / ".next" / "cache.bin"
            account_data = root / "data" / "registered_accounts.json"
            for path in (log_file, python_cache, frontend_cache, account_data):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"sensitive-state")

            service = self._service(root)
            preview = service.cleanup(["logs", "python_cache", "frontend_cache"], dry_run=True)
            self.assertEqual(preview["removed_files"], 3)
            self.assertTrue(account_data.exists())

            result = service.cleanup(["logs", "python_cache", "frontend_cache"])
            self.assertTrue(result["ok"])
            self.assertEqual(log_file.read_bytes(), b"")
            self.assertFalse(python_cache.exists())
            self.assertFalse(frontend_cache.exists())
            self.assertEqual(account_data.read_bytes(), b"sensitive-state")

    def test_cleanup_rejects_unknown_category(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = self._service(Path(temp))
            with self.assertRaisesRegex(ValueError, "Unsupported cleanup"):
                service.cleanup(["data"])

    def test_git_status_degrades_cleanly_when_git_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = self._service(Path(temp))
            with patch.object(service, "_run_git", side_effect=RuntimeError("Git is not installed")):
                status = service.git_status()
            self.assertFalse(status["is_repo"])
            self.assertEqual(status["reason"], "Git is not installed")

    def test_docker_restart_is_reported_as_supervised(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CHATGPT2API_DEPLOYMENT_MODE": "docker"}):
            status = self._service(Path(temp)).restart_status()
            self.assertTrue(status["supported"])
            self.assertEqual(status["mode"], "docker-supervisor")

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_update_fast_forwards_and_rejects_dirty_tree(self) -> None:
        def git(cwd: Path, *args: str) -> None:
            result = subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode:
                self.fail(result.stderr or result.stdout)

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            remote = base / "remote.git"
            source = base / "source"
            deploy = base / "deploy"
            writer = base / "writer"
            git(base, "init", "--bare", "--initial-branch=main", str(remote))
            git(base, "init", "--initial-branch=main", str(source))
            git(source, "config", "user.email", "tests@example.invalid")
            git(source, "config", "user.name", "Tests")
            (source / "version.txt").write_text("one", encoding="utf-8")
            git(source, "add", "version.txt")
            git(source, "commit", "-m", "initial")
            git(source, "remote", "add", "origin", str(remote))
            git(source, "push", "-u", "origin", "main")
            git(base, "clone", str(remote), str(deploy))
            git(base, "clone", str(remote), str(writer))
            git(writer, "config", "user.email", "tests@example.invalid")
            git(writer, "config", "user.name", "Tests")
            (writer / "version.txt").write_text("two", encoding="utf-8")
            git(writer, "add", "version.txt")
            git(writer, "commit", "-m", "update")
            git(writer, "push", "origin", "main")

            service = self._service(deploy)
            checked = service.git_check()
            self.assertEqual(checked["behind"], 1)
            updated = service.git_update()
            self.assertTrue(updated["updated"])
            self.assertEqual((deploy / "version.txt").read_text(encoding="utf-8"), "two")

            (deploy / "version.txt").write_text("local edit", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "uncommitted"):
                service.git_update()


class SystemApiTests(unittest.TestCase):
    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(system_api.create_router("1.0.0"))
        return TestClient(app)

    def test_rotate_key_validates_confirmation(self) -> None:
        with patch.object(system_api, "require_admin"):
            response = self._client().post(
                "/api/system/auth-key",
                json={"current_key": "a" * 32, "new_key": "b" * 32, "confirm_key": "c" * 32},
            )
        self.assertEqual(response.status_code, 400)

    def test_rotate_key_delegates_to_config_store(self) -> None:
        with (
            patch.object(system_api, "require_admin"),
            patch.object(system_api.config, "update_auth_key") as update,
        ):
            response = self._client().post(
                "/api/system/auth-key",
                json={"current_key": "a" * 32, "new_key": "b" * 32, "confirm_key": "b" * 32},
            )
        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with("a" * 32, "b" * 32)


if __name__ == "__main__":
    unittest.main()
