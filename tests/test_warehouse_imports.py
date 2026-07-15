from __future__ import annotations

import unittest
from unittest.mock import patch

from services import cpa_store, sub2api_store


class WarehouseImportTests(unittest.TestCase):
    def test_sub2api_concurrency_is_capped_at_execution_limit(self) -> None:
        self.assertEqual(sub2api_store._normalize({"concurrency": 100})["concurrency"], 50)

    def test_sub2api_partial_import_is_not_reported_as_success(self) -> None:
        accounts = [
            {"email": "ok@example.com", "access_token": "at-ok"},
            {"email": "failed@example.com", "access_token": "at-failed"},
        ]
        config = {"enabled": True, "base_url": "http://sub2api.invalid", "concurrency": 2}

        def create(account, *_args, **_kwargs):
            if account["email"] == "ok@example.com":
                return {"ok": True, "status_code": 201, "account_id": 42}
            return {"ok": False, "status_code": 409, "response": {"detail": "duplicate"}}

        with (
            patch.object(sub2api_store, "_session", return_value=object()),
            patch.object(sub2api_store, "_login_token", return_value="admin-token"),
            patch.object(sub2api_store, "create_account_in_sub2api", side_effect=create),
            patch.object(sub2api_store, "_load", return_value=config),
            patch.object(sub2api_store, "_save"),
            patch.object(sub2api_store, "update_registered_account"),
        ):
            result = sub2api_store._import_account_list_to_sub2api(accounts, config)

        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["fail"], 1)
        self.assertEqual(result["status_code"], 409)
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(all("account" not in item for item in result["results"]))

    def test_existing_sub2api_account_updates_credentials_without_duplicate_create(self) -> None:
        class Response:
            status_code = 200
            text = "{}"

            @staticmethod
            def json():
                return {"ok": True}

        class Session:
            def __init__(self):
                self.put_calls = []
                self.post_calls = []

            def put(self, url, **kwargs):
                self.put_calls.append((url, kwargs))
                return Response()

            def post(self, url, **kwargs):
                self.post_calls.append((url, kwargs))
                return Response()

        session = Session()
        account = {
            "email": "oauth@example.com",
            "access_token": "at-new",
            "refresh_token": "rt.new",
            "client_id": "app-client",
            "sub2api_account_id": 77,
        }
        with patch.object(sub2api_store, "token_profile", return_value={"oauth_capable": True}):
            result = sub2api_store.create_account_in_sub2api(
                account,
                session,
                "http://sub2api.invalid/api/v1",
                "admin-token",
                {"concurrency": 10, "auto_pause_on_expired": True},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "oauth_update")
        self.assertEqual(result["account_id"], 77)
        self.assertEqual(len(session.put_calls), 1)
        self.assertTrue(all("/admin/accounts/77/refresh" in call[0] for call in session.post_calls))

    def test_non_sub2api_oauth_account_is_rejected_before_import(self) -> None:
        with patch.object(sub2api_store, "token_profile", return_value={
            "oauth_capable": False,
            "refresh_token_label": "本地库存刷新 RT（不可用于 sub2api）",
        }):
            result = sub2api_store.create_account_in_sub2api(
                {"email": "legacy@example.com", "refresh_token": "rt.legacy"},
                object(),
                "http://sub2api.invalid/api/v1",
                "admin-token",
                {},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["method"], "oauth_rejected")
        self.assertIn("不可用于 sub2api", result["error"])

    def test_garbage_import_can_fallback_to_access_token_data_import(self) -> None:
        class Response:
            status_code = 200
            text = "{}"

            @staticmethod
            def json():
                return {"ok": True}

        class Session:
            def __init__(self):
                self.post_calls = []

            def post(self, url, **kwargs):
                self.post_calls.append((url, kwargs))
                return Response()

        session = Session()
        account = {
            "email": "web-at@example.com",
            "access_token": "web-access-token",
            "refresh_token": "legacy-refresh-token",
        }
        with patch.object(sub2api_store, "token_profile", return_value={"oauth_capable": False}):
            result = sub2api_store.create_account_in_sub2api(
                account,
                session,
                "http://sub2api.invalid/api/v1",
                "admin-token",
                {"concurrency": 10, "group_ids": [13]},
                allow_access_token_fallback=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "data_import")
        self.assertEqual(len(session.post_calls), 1)
        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "http://sub2api.invalid/api/v1/admin/accounts/data")
        imported = kwargs["json"]["data"]["accounts"][0]
        self.assertEqual(imported["group_ids"], [13])
        self.assertEqual(imported["credentials"]["access_token"], "web-access-token")
        self.assertNotIn("refresh_token", imported["credentials"])

    def test_cpa_partial_import_is_not_reported_as_success(self) -> None:
        accounts = [
            {"email": "ok@example.com", "access_token": "at-ok"},
            {"email": "failed@example.com", "access_token": "at-failed"},
        ]
        config = {"enabled": True, "base_url": "http://cpa.invalid", "concurrency": 2}

        def upload(account, _config, **_kwargs):
            if account["email"] == "ok@example.com":
                return {"ok": True, "status_code": 201, "filename": "ok.json"}
            return {"ok": False, "status_code": 500, "filename": "failed.json", "error": "upload failed"}

        with (
            patch.object(cpa_store, "_upload_cpa_account", side_effect=upload),
            patch.object(cpa_store, "_load_config", return_value=config),
            patch.object(cpa_store, "_save_config"),
            patch.object(cpa_store, "update_registered_account"),
        ):
            result = cpa_store._import_account_list_to_cpa(accounts, config)

        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["fail"], 1)
        self.assertEqual(len(result["results"]), 2)


if __name__ == "__main__":
    unittest.main()
