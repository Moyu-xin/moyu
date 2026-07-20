from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from services import cpa_store, registered_account_store, sub2api_store
from services.grok import registration
from services.grok.solver_manager import GrokSolverManager
from services.grok.vendor.xconsole_client.solver import YesCaptchaSolver
from services.grok.vendor.xconsole_client.client import XConsoleAuthClient
from services.grok.vendor.xconsole_client import sso as xai_sso
from services.register import mail_provider


def test_xai_alphanumeric_mail_code_is_supported() -> None:
    assert mail_provider._extract_code({"subject": "Your xAI verification code is ABC-123"}) == "ABC-123"
    assert mail_provider._extract_code({"text_content": "xAI verification code XAI0X1"}) == "XAI0X1"


def test_local_solver_task_carries_registration_proxy(monkeypatch) -> None:
    solver = YesCaptchaSolver(
        "local",
        endpoint="http://127.0.0.1:5072",
        proxy="http://127.0.0.1:7897",
    )
    captured: dict[str, object] = {}

    def create_task(task: dict) -> str:
        captured.update(task)
        return "task-1"

    monkeypatch.setattr(solver, "_create_task", create_task)
    monkeypatch.setattr(
        solver,
        "_get_result",
        lambda _task_id: {"status": "ready", "solution": {"token": "solved"}},
    )

    assert solver.solve_turnstile("https://accounts.x.ai/sign-up", "site-key") == "solved"
    assert captured["proxy"] == "http://127.0.0.1:7897"


def test_local_solver_submits_only_one_standard_task(monkeypatch) -> None:
    solver = YesCaptchaSolver("local", endpoint="http://127.0.0.1:5072")
    task_types: list[str] = []

    def create_task(task: dict) -> str:
        task_types.append(str(task.get("type") or ""))
        return f"task-{len(task_types)}"

    monkeypatch.setattr(solver, "_create_task", create_task)
    monkeypatch.setattr(solver, "_get_result", lambda _task_id: {"status": "ready", "solution": {"token": "solved"}})

    assert solver.solve_turnstile("https://accounts.x.ai/sign-up", "site-key") == "solved"
    assert task_types == ["TurnstileTaskProxyless"]


def test_local_solver_cancels_server_task_after_client_timeout(monkeypatch) -> None:
    solver = YesCaptchaSolver("local", endpoint="http://127.0.0.1:5072")
    cancelled: list[str] = []
    monkeypatch.setattr(solver, "_create_task", lambda _task: "task-timeout")
    monkeypatch.setattr(solver, "_get_result", lambda _task_id: (_ for _ in ()).throw(TimeoutError("timed out")))
    monkeypatch.setattr(solver, "_cancel_task", lambda task_id: cancelled.append(task_id))

    with pytest.raises(RuntimeError, match="timed out"):
        solver.solve_turnstile("https://accounts.x.ai/sign-up", "site-key")

    assert cancelled == ["task-timeout"]


def test_local_solver_rejects_tasks_beyond_browser_capacity(monkeypatch) -> None:
    pytest.importorskip("quart")
    from services.grok.turnstile_solver.api_solver import TurnstileAPIServer

    async def scenario() -> None:
        server = TurnstileAPIServer(
            headless=True,
            useragent=None,
            debug=False,
            browser_type="camoufox",
            thread=1,
            proxy_support=False,
        )
        release = asyncio.Event()

        async def wait_forever(**_kwargs):
            await release.wait()

        monkeypatch.setattr(server, "_guarded_solve_turnstile", wait_forever)
        first_id, first_error = await server._enqueue_turnstile("https://accounts.x.ai/sign-up", "site-key")
        second_id, second_error = await server._enqueue_turnstile("https://accounts.x.ai/sign-up", "site-key")

        assert first_id
        assert first_error is None
        assert second_id is None
        assert second_error["errorCode"] == "ERROR_NO_SLOT_AVAILABLE"

        server._solver_tasks[first_id].cancel()
        await asyncio.gather(server._solver_tasks[first_id], return_exceptions=True)

    asyncio.run(scenario())


def test_xai_action_scraper_reports_empty_chunk_set_without_zero_worker_crash() -> None:
    client = XConsoleAuthClient(transport="urllib")
    try:
        with pytest.raises(RuntimeError, match="exposed 0 JS chunks"):
            client._scrape_action_id("<html><body>sign up</body></html>")
    finally:
        client.close()


def test_xai_action_scraper_supports_live_absolute_chunk_urls(monkeypatch) -> None:
    client = XConsoleAuthClient(transport="urllib")
    requested: list[str] = []
    action_id = "7f" + "a" * 40

    def request(_method: str, url: str, **_kwargs):
        requested.append(url)
        body = f'createUserAndSessionRequest "{action_id}"'.encode()
        return 200, {}, [], body

    monkeypatch.setattr(client, "_request", request)
    try:
        result = client._scrape_action_id(
            '<script src="https://accounts.x.ai/_next/static/chunks/signup.js?v=1"></script>'
        )
    finally:
        client.close()

    assert result == action_id
    assert requested == ["https://accounts.x.ai/_next/static/chunks/signup.js?v=1"]


def test_xai_signup_page_identifies_cloudflare_before_scraping(monkeypatch) -> None:
    client = XConsoleAuthClient(transport="urllib")
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: (200, {}, [], b"<html><title>Just a moment...</title></html>"),
    )
    try:
        with pytest.raises(RuntimeError, match="Cloudflare challenge"):
            client.load_signup_page()
    finally:
        client.close()


def test_mailbox_state_failure_does_not_replace_registration_result(monkeypatch) -> None:
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        registration.mail_provider,
        "mark_mailbox_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("state file locked")),
    )

    registration._mark_mailbox_result(
        {"address": "grok@example.com"},
        success=True,
        emit=lambda message, level="": messages.append((message, level)),
    )

    assert messages == [("邮箱状态保存失败，注册结果继续保留：state file locked", "yellow")]


def test_solver_manager_reclaims_verified_listener_after_app_restart(monkeypatch) -> None:
    manager = GrokSolverManager()
    monkeypatch.setattr(manager, "_alive", lambda _url: True)
    monkeypatch.setattr(manager, "_health", lambda _url: {"ok": True, "configured_threads": 2})
    monkeypatch.setattr(
        manager,
        "_listener_info",
        lambda _url: {"listener_pid": 4321, "listener_verified": True, "actual_threads": 2},
    )

    status = manager.status("http://127.0.0.1:5072")

    assert status["running"] is True
    assert status["managed"] is True
    assert status["state"] == "managed"
    assert status["pid"] == 4321


def test_solver_manager_restarts_nonempty_pool_before_new_registration(monkeypatch) -> None:
    manager = GrokSolverManager()
    stale = {
        "browser_ready": True,
        "actual_threads": 3,
        "listener_verified": True,
        "health": {
            "connected_browsers": 3,
            "inflight_tasks": 2,
            "queued_tasks": 1,
        },
    }
    restarted = {"running": True, "health": {"inflight_tasks": 0, "queued_tasks": 0}}
    monkeypatch.setattr(manager, "_alive", lambda _url: True)
    monkeypatch.setattr(manager, "status", lambda _url: stale)
    monkeypatch.setattr(manager, "restart", lambda _url, threads: restarted)

    assert manager.ensure_started({"local_solver_url": "http://127.0.0.1:5072", "solver_threads": 3}) == restarted


def test_inventory_identity_separates_openai_and_grok() -> None:
    openai = registered_account_store.account_identity_key({"email": "same@example.com", "platform": "openai"})
    grok = registered_account_store.account_identity_key({"email": "same@example.com", "platform": "grok"})
    assert openai == "same@example.com"
    assert grok == "grok:same@example.com"
    assert openai != grok


def test_fetch_sso_respects_save_false(monkeypatch) -> None:
    client = XConsoleAuthClient(transport="urllib")
    client._last_rsc_body = "sso=eyJheader.eyJpayload.signature"
    saved: list[str] = []
    monkeypatch.setattr(xai_sso, "save_sso", lambda token, **_kwargs: saved.append(token))

    token = client.fetch_sso_token(email="grok@example.com", save=False, retries=1)

    assert token == "eyJheader.eyJpayload.signature"
    assert saved == []


def test_build_grok_sub2api_account_uses_grok_oauth_contract() -> None:
    result = sub2api_store.build_grok_sub2api_account(
        {
            "platform": "grok",
            "email": "grok@example.com",
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id",
            "client_id": "client",
            "base_url": "https://cli-chat-proxy.grok.com/v1",
        },
        {"concurrency": 4, "priority": 2, "rate_multiplier": 1.0, "load_factor": 4},
    )
    assert result["platform"] == "grok"
    assert result["type"] == "oauth"
    assert result["credentials"]["refresh_token"] == "refresh"
    assert result["credentials"]["base_url"] == "https://cli-chat-proxy.grok.com/v1"


@dataclass
class _FakeResponse:
    status_code: int
    payload: dict

    def json(self) -> dict:
        return self.payload


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(
            200,
            {"data": {"created": [{"account": {"id": 91, "platform": "grok"}}], "failed": []}},
        )

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(200, {"data": {"items": [], "total": 0}})

    def put(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(200, {"data": {"id": 91, "platform": "grok"}})


def test_grok_sub2api_import_prefers_sso_endpoint(monkeypatch) -> None:
    session = _FakeSession()
    monkeypatch.setattr(sub2api_store, "_verify_tls", lambda _cfg=None: True)
    result = sub2api_store._create_grok_account_in_sub2api(
        {"platform": "grok", "email": "grok@example.com", "sso_token": "sso.jwt.token"},
        session,
        "https://sub.example/api/v1",
        "admin-token",
        sub2api_store._normalize({"group_ids": [7], "concurrency": 3}),
    )
    assert result["ok"] is True
    assert result["method"] == "grok_sso_to_oauth"
    assert result["account_id"] == 91
    assert session.calls[0][0].endswith("/admin/grok/sso-to-oauth")
    assert session.calls[0][1]["json"]["sso_tokens"] == ["sso.jwt.token"]
    assert session.calls[0][1]["json"]["group_ids"] == [7]


def test_grok_sub2api_import_prefers_ready_oauth_over_sso(monkeypatch) -> None:
    session = _FakeSession()
    monkeypatch.setattr(sub2api_store, "_verify_tls", lambda _cfg=None: True)
    result = sub2api_store._create_grok_account_in_sub2api(
        {
            "platform": "grok",
            "email": "grok@example.com",
            "sso_token": "sso.jwt.token",
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id",
        },
        session,
        "https://sub.example/api/v1",
        "admin-token",
        sub2api_store._normalize({"group_ids": [7], "concurrency": 3}),
    )
    assert result["ok"] is True
    assert result["method"] == "grok_oauth_create"
    assert result["account_id"] == 91
    assert session.calls[0][0].endswith("/admin/accounts")
    assert session.calls[1][0].endswith("/admin/accounts")
    account = session.calls[1][1]["json"]
    assert account["platform"] == "grok"
    assert account["credentials"]["refresh_token"] == "refresh"


def test_grok_sub2api_import_updates_known_account(monkeypatch) -> None:
    session = _FakeSession()
    monkeypatch.setattr(sub2api_store, "_verify_tls", lambda _cfg=None: True)
    result = sub2api_store._create_grok_account_in_sub2api(
        {
            "platform": "grok",
            "email": "grok@example.com",
            "access_token": "access",
            "refresh_token": "refresh",
            "sub2api_account_id": 91,
        },
        session,
        "https://sub.example/api/v1",
        "admin-token",
        sub2api_store._normalize({"concurrency": 3}),
    )
    assert result["ok"] is True
    assert result["method"] == "grok_oauth_update"
    assert result["account_id"] == 91
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/admin/accounts/91")
    assert "platform" not in session.calls[0][1]["json"]


def test_grok_sso_only_accounts_require_explicit_reauthorization(monkeypatch) -> None:
    accounts = [
        {"platform": "grok", "email": "expired@example.com", "sso_token": "sso-token"},
        {"platform": "grok", "email": "ready@example.com", "access_token": "access", "refresh_token": "refresh"},
    ]
    monkeypatch.setattr(sub2api_store, "list_registered_accounts", lambda include_secret=True: accounts)
    monkeypatch.setattr(
        sub2api_store,
        "token_profile",
        lambda account: {"oauth_capable": account["email"] == "ready@example.com"},
    )

    regular = sub2api_store._select_accounts()
    reauthorized = sub2api_store._select_accounts(include_grok_sso=True)

    assert [item["email"] for item in regular] == ["ready@example.com"]
    assert [item["email"] for item in reauthorized] == ["expired@example.com", "ready@example.com"]


def test_refresh_grok_oauth_can_reauthorize_from_sso(monkeypatch) -> None:
    account = {
        "platform": "grok",
        "email": "expired@example.com",
        "sso_token": "sso-token",
        "sub2api_account_id": 91,
    }
    persisted: dict = {}
    monkeypatch.setattr(
        sub2api_store.task_proxy_service,
        "assign_task_proxy",
        lambda *_args, **_kwargs: {"proxy": "http://proxy.example:8080", "source": "pool", "label": "node-1"},
    )
    monkeypatch.setattr(
        sub2api_store,
        "exchange_sso_for_oauth",
        lambda sso, **kwargs: {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "id_token": "fresh-id",
            "expires_in": 3600,
        },
    )

    def update(selector, updates):
        persisted.update(updates)
        return {**selector, **updates}

    monkeypatch.setattr(sub2api_store, "update_registered_account", update)

    result = sub2api_store.refresh_grok_account_oauth(account, force_reauthorize=True)

    assert result["method"] == "sso_reauthorize"
    assert result["proxy"] == "http://proxy.example:8080"
    assert result["account"]["sub2api_account_id"] == 91
    assert persisted["access_token"] == "fresh-access"
    assert persisted["refresh_token"] == "fresh-refresh"
    assert persisted["token_refresh_required"] is False


def test_reauthorize_import_reuses_existing_sub2api_account(monkeypatch) -> None:
    account = {
        "platform": "grok",
        "email": "expired@example.com",
        "sso_token": "sso-token",
        "sub2api_account_id": 91,
    }
    prepared = {**account, "access_token": "fresh-access", "refresh_token": "fresh-refresh"}
    imported: list[dict] = []
    monkeypatch.setattr(sub2api_store, "_session", lambda **_kwargs: object())
    monkeypatch.setattr(sub2api_store, "_login_token", lambda _session, _cfg: "admin-token")
    monkeypatch.setattr(sub2api_store, "_load", lambda: {})
    monkeypatch.setattr(sub2api_store, "_save", lambda _data: None)
    monkeypatch.setattr(sub2api_store, "update_registered_account", lambda selector, updates: {**selector, **updates})
    monkeypatch.setattr(
        sub2api_store,
        "refresh_grok_account_oauth",
        lambda *_args, **_kwargs: {"account": prepared, "method": "sso_reauthorize"},
    )

    def create(prepared_account, *_args, **_kwargs):
        imported.append(prepared_account)
        return {"ok": True, "status_code": 200, "method": "grok_oauth_update", "account_id": 91}

    monkeypatch.setattr(sub2api_store, "create_account_in_sub2api", create)

    result = sub2api_store._import_account_list_to_sub2api(
        [account],
        sub2api_store._normalize({"enabled": True, "base_url": "https://sub.example", "concurrency": 1}),
        reauthorize_grok=True,
    )

    assert result["ok"] is True
    assert result["account_ids"] == [91]
    assert result["results"][0]["method"] == "grok_oauth_update"
    assert result["results"][0]["reauthorize_method"] == "sso_reauthorize"
    assert imported == [prepared]


def test_grok_sub2api_import_does_not_treat_business_failure_as_success(monkeypatch) -> None:
    class ImportFailureSession(_FakeSession):
        def post(self, url: str, **kwargs):
            self.calls.append((url, kwargs))
            if url.endswith("/admin/accounts"):
                return _FakeResponse(404, {"message": "not found"})
            return _FakeResponse(
                200,
                {
                    "data": {
                        "account_created": 0,
                        "account_failed": 1,
                        "errors": [{"kind": "account", "message": "invalid Grok credentials"}],
                    }
                },
            )

    session = ImportFailureSession()
    monkeypatch.setattr(sub2api_store, "_verify_tls", lambda _cfg=None: True)
    result = sub2api_store._create_grok_account_in_sub2api(
        {
            "platform": "grok",
            "email": "grok@example.com",
            "access_token": "access",
            "refresh_token": "refresh",
        },
        session,
        "https://sub.example/api/v1",
        "admin-token",
        sub2api_store._normalize({"concurrency": 3}),
    )
    assert result["ok"] is False
    assert result["method"] == "grok_data_import"
    assert result["error"] == "invalid Grok credentials"


def test_build_grok_cpa_auth_file_uses_xai_format() -> None:
    result = cpa_store.build_cpa_auth_file(
        {
            "platform": "grok",
            "email": "grok@example.com",
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id",
        }
    )
    assert result["type"] == "xai"
    assert result["auth_kind"] == "oauth"
    assert result["base_url"] == "https://cli-chat-proxy.grok.com/v1"
    assert result["headers"]["X-XAI-Token-Auth"] == "xai-grok-cli"


class _FakeSolver:
    def solve_turnstile(self, **_kwargs) -> str:
        return "turnstile-token"


class _FakeClient:
    turnstile_sitekey = "site-key"

    def __init__(self, **_kwargs) -> None:
        pass

    def visit_home(self) -> None:
        pass

    def load_signup_page(self) -> None:
        pass

    def validate_password(self, _email: str, _password: str) -> None:
        pass

    def create_email_validation_code(self, _email: str) -> None:
        pass

    def verify_email_validation_code(self, _email: str, _code: str) -> None:
        pass

    def create_account(self, **_kwargs):
        return type("Signup", (), {"http_status": 200, "error": None})()

    def fetch_sso_token(self, **_kwargs) -> str:
        return "header.payload.signature"

    def obtain_session_via_password(self, **_kwargs) -> str:
        return ""

    def close(self) -> None:
        pass


def test_registration_produces_inventory_and_warehouse_fields(monkeypatch) -> None:
    mailbox = {"address": "grok@example.com", "provider": "outlook_token", "provider_ref": "outlook_token#0"}
    configured_mail = {"registration_source": "yyds_mail", "providers": [{"type": "yyds_mail", "enable": True}]}
    captured_mail_configs: list[dict] = []
    states: list[bool] = []
    mail_results = iter(
        [
            {"messages": []},
            {"messages": [{"subject": "xAI code ABC-123", "body_preview": ""}]},
        ]
    )
    monkeypatch.setattr(registration.mail_provider, "create_mailbox", lambda config: captured_mail_configs.append(config) or mailbox)
    monkeypatch.setattr(
        registration.mail_provider,
        "recent_messages",
        lambda *_args, **_kwargs: next(mail_results),
    )
    monkeypatch.setattr(registration.mail_provider, "mark_mailbox_result", lambda _mailbox, *, success, error=None: states.append(success))
    monkeypatch.setattr(registration, "XConsoleAuthClient", _FakeClient)
    monkeypatch.setattr(registration, "_solver", lambda *_args, **_kwargs: _FakeSolver())
    monkeypatch.setattr(
        registration,
        "exchange_sso_for_oauth",
        lambda *_args, **_kwargs: {
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "id",
            "expires_at": 1234567890,
            "expires_in": 21600,
            "client_id": "client",
        },
    )

    result = registration.register_grok_account(
        1,
        proxy="http://127.0.0.1:7897",
        mail_config=configured_mail,
        grok_config={"exchange_oauth": True},
        progress=lambda *_args: None,
    )
    assert result["ok"] is True
    assert result["oauth_ready"] is True
    assert result["result"]["platform"] == "grok"
    assert result["result"]["sso_token"] == "header.payload.signature"
    assert result["result"]["refresh_token"] == "refresh"
    assert result["result"]["base_url"] == "https://cli-chat-proxy.grok.com/v1"
    assert captured_mail_configs == [configured_mail]
    assert states == [True]
