from __future__ import annotations

import json
from pathlib import Path

import pytest

from services import grok2api_store
from services import register_service as register_service_module


def test_grok2api_root_url_accepts_public_and_admin_urls() -> None:
    assert grok2api_store._root_url("https://grok.example/v1/") == "https://grok.example"
    assert grok2api_store._root_url("https://grok.example/api/admin/v1") == "https://grok.example"
    assert grok2api_store._root_url("https://grok.example") == "https://grok.example"


def test_grok2api_config_masks_secrets_and_preserves_blank_updates(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "grok2api_config.json"
    monkeypatch.setattr(grok2api_store, "GROK2API_CONFIG_FILE", config_file)

    saved = grok2api_store.update_grok2api_config(
        {
            "enabled": True,
            "base_url": "https://grok.example/v1/",
            "username": "admin",
            "password": "secret-password",
            "admin_token": "admin-access-token",
        }
    )
    assert saved["base_url"] == "https://grok.example/v1"
    assert saved["has_password"] is True
    assert saved["has_admin_token"] is True
    assert "password" not in saved
    assert "admin_token" not in saved

    grok2api_store.update_grok2api_config({"password": "", "admin_token": "", "notes": "updated"})
    stored = json.loads(config_file.read_text(encoding="utf-8"))
    assert stored["password"] == "secret-password"
    assert stored["admin_token"] == "admin-access-token"
    assert stored["notes"] == "updated"


def test_grok2api_import_document_contains_only_web_sso_fields() -> None:
    document = grok2api_store._build_import_document(
        [
            {
                "platform": "grok",
                "email": "grok@example.com",
                "sso_token": "sso-secret",
                "access_token": "must-not-export",
                "refresh_token": "must-not-export",
            }
        ]
    )

    assert document == {
        "provider": "grok_web",
        "accounts": [
            {
                "name": "grok@example.com",
                "sso_token": "sso-secret",
                "tier": "auto",
            }
        ],
    }


def test_grok2api_sse_parser_returns_complete_event() -> None:
    events = grok2api_store._parse_sse(
        ': connected\n\n'
        'event: progress\n'
        'data: {"completed":1,"total":1}\n\n'
        'event: complete\n'
        'data: {"created":1,"updated":0,"synced":1,"syncFailed":0}\n\n'
    )

    assert [event["event"] for event in events] == ["progress", "complete"]
    assert grok2api_store._complete_event(events)["created"] == 1


def test_select_grok2api_accounts_requires_grok_sso(monkeypatch) -> None:
    accounts = [
        {"platform": "grok", "email": "web@example.com", "sso_token": "sso"},
        {"platform": "grok", "email": "oauth@example.com", "access_token": "access"},
        {"platform": "openai", "email": "openai@example.com", "sso_token": "other"},
    ]
    monkeypatch.setattr(grok2api_store, "list_registered_accounts", lambda include_secret=True: accounts)

    assert grok2api_store._select_accounts() == [accounts[0]]


def test_grok_registration_auto_imports_raw_sso_to_grok2api(tmp_path: Path, monkeypatch) -> None:
    service = register_service_module.RegisterService(tmp_path / "register.json")
    account = {
        "platform": "grok",
        "email": "web@example.com",
        "sso_token": "raw-web-sso",
        "access_token": "build-access",
    }
    imported: list[dict[str, str]] = []
    monkeypatch.setattr(
        register_service_module,
        "register_grok_account",
        lambda *_args, **_kwargs: {"ok": True, "result": account.copy()},
    )
    monkeypatch.setattr(register_service_module, "add_registered_account", lambda value: value)
    monkeypatch.setattr(
        register_service_module.grok2api_store,
        "import_account_dicts_to_grok2api",
        lambda value: imported.append(value.copy()) or {"ok": True, "created": 1},
    )

    result = service._run_registration_task(
        1,
        "",
        {},
        "total",
        {
            "register_mode": "grok",
            "mail": {},
            "grok": {
                "auto_import_sub2api": False,
                "auto_import_cpa": False,
                "auto_import_grok2api": True,
            },
        },
    )

    assert result["warehouse"]["grok2api"]["ok"] is True
    assert imported == [account]
    assert imported[0]["sso_token"] == "raw-web-sso"


def test_grok2api_connection_wraps_transport_errors(monkeypatch) -> None:
    class BrokenSession:
        def get(self, *_args, **_kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr(
        grok2api_store,
        "_load_config",
        lambda: grok2api_store._normalize(
            {"enabled": True, "base_url": "http://127.0.0.1:9999", "admin_token": "token"}
        ),
    )
    monkeypatch.setattr(grok2api_store, "_session", lambda: BrokenSession())

    with pytest.raises(RuntimeError, match="仓库连接失败"):
        grok2api_store.test_grok2api_connection()


def test_grok2api_batch_import_updates_inventory_once(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = (
            'event: complete\n'
            'data: {"created":2,"updated":0,"synced":2,"syncFailed":0}\n\n'
        )

        def json(self):
            return {}

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    accounts = [
        {"platform": "grok", "inventory_id": "grok-1", "email": "one@example.com", "sso_token": "sso-1"},
        {"platform": "grok", "inventory_id": "grok-2", "email": "two@example.com", "sso_token": "sso-2"},
    ]
    updates: list[dict[str, dict[str, str]]] = []
    monkeypatch.setattr(grok2api_store, "_session", lambda: Session())
    monkeypatch.setattr(grok2api_store, "_login_token", lambda *_args: "admin-token")
    monkeypatch.setattr(grok2api_store, "_load_config", lambda: {"enabled": True})
    monkeypatch.setattr(grok2api_store, "_save_config", lambda _cfg: None)
    monkeypatch.setattr(
        grok2api_store,
        "update_registered_accounts_bulk",
        lambda value: updates.append(value) or {"updated": len(value)},
    )

    result = grok2api_store._import_account_list_to_grok2api(
        accounts,
        {
            "enabled": True,
            "base_url": "https://grok.example",
            "admin_token": "admin-token",
            "skip_ssl_verify": False,
        },
    )

    assert result["ok"] is True
    assert result["created"] == 2
    assert len(updates) == 1
    assert set(updates[0]) == {"grok-1", "grok-2"}
    assert all(item["grok2api_import_status"] == "ok" for item in updates[0].values())
