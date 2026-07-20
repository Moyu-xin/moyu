from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import register as register_api


def _client() -> TestClient:
    with patch.object(register_api.k12_config_store, "ensure_migrated"):
        router = register_api.create_router()
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_grok2api_config_update_uses_dedicated_store() -> None:
    client = _client()
    config = {"enabled": True, "base_url": "https://grok.example", "username": "admin"}
    with (
        patch.object(register_api, "require_admin"),
        patch.object(register_api.grok2api_store, "update_grok2api_config", return_value=config) as update,
        patch.object(register_api.register_service, "get", return_value={"enabled": False}),
    ):
        response = client.post(
            "/api/grok2api-config",
            json={"enabled": True, "base_url": "https://grok.example", "username": "admin", "password": "secret"},
        )

    assert response.status_code == 200
    assert response.json()["grok2api_config"] == config
    update.assert_called_once_with(
        {"enabled": True, "base_url": "https://grok.example", "username": "admin", "password": "secret"}
    )


def test_manual_grok2api_import_forwards_selected_accounts() -> None:
    client = _client()
    result = {"ok": True, "total": 1, "created": 1, "updated": 0, "synced": 1, "sync_failed": 0}
    with (
        patch.object(register_api, "require_admin"),
        patch.object(register_api.grok2api_store, "import_accounts_to_grok2api", return_value=result) as import_accounts,
        patch.object(register_api, "account_pool", return_value={"accounts": []}),
    ):
        response = client.post("/api/accounts/import-grok2api", json={"emails": ["grok:web@example.com"]})

    assert response.status_code == 200
    assert response.json()["grok2api"] == result
    import_accounts.assert_called_once_with(["grok:web@example.com"])


def test_grok2api_connection_errors_are_returned_as_bad_request() -> None:
    client = _client()
    with (
        patch.object(register_api, "require_admin"),
        patch.object(register_api.grok2api_store, "test_grok2api_connection", side_effect=RuntimeError("login failed")),
    ):
        response = client.post("/api/grok2api/connection/test")

    assert response.status_code == 400
    assert response.json()["detail"] == "login failed"
