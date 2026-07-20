from __future__ import annotations

from dataclasses import dataclass

from services import mailbox_pool_store
from services.register import mail_provider


def test_yyds_config_is_normalized_redacted_and_projected(monkeypatch) -> None:
    configs = mailbox_pool_store._normalize_provider_configs(
        {
            "registration_source": "yyds_mail",
            "yyds_mail": {
                "enable": True,
                "api_base": "https://maliapi.215.im/v1/",
                "api_key": "secret-key",
                "domain": "first.example, second.example",
                "subdomain": "mail",
                "wildcard": True,
            },
        }
    )
    public = mailbox_pool_store._public_provider_configs(configs)
    assert public["registration_source"] == "yyds_mail"
    assert public["yyds_mail"]["has_api_key"] is True
    assert "api_key" not in public["yyds_mail"]

    monkeypatch.setattr(mailbox_pool_store, "get_provider_configs", lambda **_kwargs: configs)
    projected = mailbox_pool_store.apply_to_register_config({"mail": {"wait_timeout": 90}})
    assert projected["mail"]["registration_source"] == "yyds_mail"
    assert projected["mail"]["providers"] == [
        {
            "enable": True,
            "type": "yyds_mail",
            "mailbox_group": "",
            "api_base": "https://maliapi.215.im/v1",
            "api_key": "secret-key",
            "domain": ["first.example", "second.example"],
            "subdomain": "mail",
            "wildcard": True,
        }
    ]


@dataclass
class _Response:
    status_code: int
    payload: dict

    @property
    def text(self) -> str:
        return str(self.payload)

    def json(self) -> dict:
        return self.payload


class _Session:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/domains"):
            return _Response(200, {"success": True, "data": [{"domain": "ready.example", "receivingReady": True}]})
        if url.endswith("/accounts") and kwargs.get("json", {}).get("domain") == "bad.example":
            return _Response(400, {"success": False, "errorCode": "domain_not_receiving_ready"})
        if url.endswith("/accounts"):
            return _Response(201, {"success": True, "data": {"id": 42, "address": "user@ready.example", "token": "mail-token"}})
        if url.endswith("/inboxes/42/messages"):
            return _Response(200, {"success": True, "data": {"items": [{"id": 7, "subject": "xAI code ABC-123", "createdAt": "2026-07-17T00:00:00Z"}]}})
        if url.endswith("/messages/7"):
            return _Response(200, {"success": True, "data": {"text": "verification code ABC-123", "from": {"address": "accounts@x.ai"}}})
        raise AssertionError(f"unexpected request: {method} {url}")

    def close(self) -> None:
        pass


def test_yyds_provider_retries_ready_domain_and_supports_recent_messages(monkeypatch) -> None:
    session = _Session()
    monkeypatch.setattr(mail_provider, "_create_session", lambda _conf: session)
    monkeypatch.setattr(mail_provider.proxy_settings, "verify_tls", lambda: True)
    provider = mail_provider.YydsMailProvider(
        {
            "api_key": "secret-key",
            "api_base": "https://maliapi.215.im/v1",
            "domain": ["bad.example"],
        },
        {"user_agent": "test", "request_timeout": 5, "wait_timeout": 5, "wait_interval": 1},
    )

    mailbox = provider.create_mailbox("user")
    messages = provider.fetch_recent_messages(mailbox)

    assert mailbox["address"] == "user@ready.example"
    assert mailbox["account_id"] == "42"
    assert messages[0]["message_id"] == "7"
    assert mail_provider._extract_code(messages[0]) == "ABC-123"
    assert any(call[1].endswith("/inboxes/42/messages") for call in session.calls)
