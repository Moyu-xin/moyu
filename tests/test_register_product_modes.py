from __future__ import annotations

import base64
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services.register import openai_register


class _FakeRegistrar:
    oauth_error: Exception | None = None
    conversation_error: Exception | None = None
    conversation_calls = 0
    oauth_calls = 0

    def __init__(self, _proxy: str = "") -> None:
        self.closed = False

    def register_free_wu(self, _index: int) -> dict:
        return {
            "email": "new@example.com",
            "password": "secret",
            "access_token": "web-access-token",
            "web_login_ok": True,
            "register_mode": "free",
        }

    def acquire_sub2api_oauth(self, _index: int, account: dict) -> dict:
        type(self).oauth_calls += 1
        if self.oauth_error:
            raise self.oauth_error
        return {
            **account,
            "access_token": "oauth-access-token",
            "refresh_token": "oauth-refresh-token",
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
            "token_source": "sub2api_openai_oauth",
            "oauth_provenance": "sub2api_registration_authorization",
            "product_mode": "finished",
        }

    def send_post_registration_hi(self, _index: int, account: dict) -> dict:
        type(self).conversation_calls += 1
        if self.conversation_error:
            account.update({"chat_test_tried": True, "chat_test_ok": False, "chat_test_method": "conversation_pre_oauth", "chat_test_error": str(self.conversation_error)})
            raise self.conversation_error
        return {
            **account,
            "chat_test_tried": True,
            "chat_test_ok": True,
            "chat_test_method": "conversation_pre_oauth",
            "chat_test_status_code": 200,
        }

    def register(self, _index: int) -> dict:
        raise AssertionError("Free 模式不应调用旧注册引擎")

    def close(self) -> None:
        self.closed = True


class RegisterProductModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_config = dict(openai_register.config)
        self.original_stats = dict(openai_register.stats)
        openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
        _FakeRegistrar.conversation_calls = 0
        _FakeRegistrar.oauth_calls = 0

    def tearDown(self) -> None:
        openai_register.config.clear()
        openai_register.config.update(self.original_config)
        openai_register.stats.clear()
        openai_register.stats.update(self.original_stats)
        _FakeRegistrar.oauth_error = None
        _FakeRegistrar.conversation_error = None
        _FakeRegistrar.conversation_calls = 0
        _FakeRegistrar.oauth_calls = 0

    def _worker_patches(self):
        return (
            patch.object(openai_register, "PlatformRegistrar", _FakeRegistrar),
            patch.object(openai_register, "step"),
            patch.object(openai_register, "log"),
        )

    def test_partial_mode_saves_only_to_partial_inventory(self) -> None:
        openai_register.config.update({"register_mode": "free", "product_mode": "partial", "proxy": ""})
        saved = {"email": "new@example.com", "status": "oauth_missing", "product_mode": "partial"}
        registrar_patch, step_patch, log_patch = self._worker_patches()
        with (
            registrar_patch,
            step_patch,
            log_patch,
            patch.object(openai_register.partial_account_store, "upsert_partial_account", return_value=saved) as upsert,
            patch.object(openai_register, "add_registered_account") as add_registered,
            patch.object(openai_register, "_maybe_import_registered_account_to_warehouses") as import_warehouses,
        ):
            result = openai_register.worker(1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["storage"], "partial")
        self.assertEqual(upsert.call_args.args[0]["status"], "oauth_missing")
        self.assertEqual(_FakeRegistrar.conversation_calls, 0)
        add_registered.assert_not_called()
        import_warehouses.assert_not_called()

    def test_worker_keeps_runtime_config_snapshot_during_hot_reload(self) -> None:
        observed: list[dict] = []

        class SnapshotRegistrar(_FakeRegistrar):
            def register_free_wu(self, index: int) -> dict:
                observed.append(openai_register._mail_config())
                openai_register.config.update({
                    "mail": {"registration_source": "cloudflare_temp_email"},
                    "mailbox_group": "new-group",
                    "product_mode": "finished",
                })
                observed.append(openai_register._mail_config())
                return super().register_free_wu(index)

        runtime_config = {
            **openai_register.config,
            "mail": {"registration_source": "outlook_token", "mailbox_group": "old-group"},
            "mailbox_group": "old-group",
            "register_mode": "free",
            "product_mode": "partial",
        }
        saved = {"email": "new@example.com", "status": "oauth_missing", "product_mode": "partial"}
        with (
            patch.object(openai_register, "PlatformRegistrar", SnapshotRegistrar),
            patch.object(openai_register, "step"),
            patch.object(openai_register, "log"),
            patch.object(openai_register.partial_account_store, "upsert_partial_account", return_value=saved) as upsert,
            patch.object(openai_register, "add_registered_account"),
            patch.object(openai_register, "_maybe_import_registered_account_to_warehouses"),
        ):
            result = openai_register.worker(8, runtime_config=runtime_config)

        self.assertTrue(result["ok"])
        self.assertEqual([item["registration_source"] for item in observed], ["outlook_token", "outlook_token"])
        self.assertEqual([item["mailbox_group"] for item in observed], ["old-group", "old-group"])
        self.assertEqual(upsert.call_args.args[0]["product_mode"], "partial")
        self.assertEqual(openai_register._active_config()["product_mode"], "finished")

    def test_registration_saves_proxy_node_snapshot(self) -> None:
        openai_register.config.update({"register_mode": "free", "product_mode": "partial", "proxy": ""})
        saved = {"email": "new@example.com", "status": "oauth_missing", "product_mode": "partial"}
        assignment = {
            "proxy": "http://127.0.0.1:42002",
            "source": "managed_mihomo",
            "node_id": "node-id",
            "label": "[pool] node-a",
            "slot": 3,
            "managed_slot": 5,
            "region": "JP",
        }
        registrar_patch, step_patch, log_patch = self._worker_patches()
        with (
            registrar_patch,
            step_patch,
            log_patch,
            patch.object(openai_register.partial_account_store, "upsert_partial_account", return_value=saved) as upsert,
            patch.object(openai_register, "add_registered_account"),
            patch.object(openai_register, "_maybe_import_registered_account_to_warehouses"),
        ):
            result = openai_register.worker(1, assignment["proxy"], assignment)

        self.assertTrue(result["ok"])
        payload = upsert.call_args.args[0]
        self.assertEqual(payload["registration_proxy_node_id"], "node-id")
        self.assertEqual(payload["registration_proxy_label"], "[pool] node-a")
        self.assertEqual(payload["registration_proxy"]["proxy"], "http://127.0.0.1:42002")

    def test_finished_mode_oauth_failure_falls_back_to_partial_inventory(self) -> None:
        openai_register.config.update({"register_mode": "free", "product_mode": "finished", "proxy": ""})
        _FakeRegistrar.oauth_error = RuntimeError("oauth exchange failed")
        saved = {"email": "new@example.com", "status": "oauth_failed", "product_mode": "finished"}
        registrar_patch, step_patch, log_patch = self._worker_patches()
        with (
            registrar_patch,
            step_patch,
            log_patch,
            patch.object(openai_register.partial_account_store, "upsert_partial_account", return_value=saved) as upsert,
            patch.object(openai_register, "add_registered_account") as add_registered,
            patch.object(openai_register, "_maybe_import_registered_account_to_warehouses") as import_warehouses,
        ):
            result = openai_register.worker(2)

        self.assertTrue(result["ok"])
        self.assertTrue(result["oauth_failed"])
        self.assertEqual(_FakeRegistrar.conversation_calls, 1)
        self.assertEqual(_FakeRegistrar.oauth_calls, 1)
        self.assertEqual(upsert.call_args.args[0]["status"], "oauth_failed")
        add_registered.assert_not_called()
        import_warehouses.assert_not_called()

    def test_oauth_deactivated_failure_marks_partial_account_dead(self) -> None:
        openai_register.config.update({"register_mode": "free", "product_mode": "finished", "proxy": ""})
        _FakeRegistrar.oauth_error = RuntimeError("sub2api_oauth_account_deactivated")
        saved = {"email": "new@example.com", "status": "oauth_failed", "health_status": "banned"}
        registrar_patch, step_patch, log_patch = self._worker_patches()
        with (
            registrar_patch,
            step_patch,
            log_patch,
            patch.object(openai_register.partial_account_store, "upsert_partial_account", return_value=saved) as upsert,
            patch.object(openai_register, "add_registered_account"),
            patch.object(openai_register, "_maybe_import_registered_account_to_warehouses"),
        ):
            result = openai_register.worker(2)

        self.assertTrue(result["ok"])
        payload = upsert.call_args.args[0]
        self.assertEqual(payload["health_status"], "banned")
        self.assertEqual(payload["health_method"], "oauth")
        self.assertTrue(payload["died_at"])

    def test_finished_mode_oauth_success_saves_and_imports_finished_account(self) -> None:
        openai_register.config.update({"register_mode": "free", "product_mode": "finished", "proxy": ""})
        registrar_patch, step_patch, log_patch = self._worker_patches()
        with (
            registrar_patch,
            step_patch,
            log_patch,
            patch.object(openai_register, "add_registered_account") as add_registered,
            patch.object(openai_register.partial_account_store, "delete_partial_account") as delete_partial,
            patch.object(openai_register, "_maybe_import_registered_account_to_warehouses") as import_warehouses,
        ):
            result = openai_register.worker(3)

        self.assertTrue(result["ok"])
        account = result["result"]
        self.assertEqual(account["product_mode"], "finished")
        self.assertEqual(account["token_source"], "sub2api_openai_oauth")
        self.assertTrue(account["chat_test_ok"])
        self.assertEqual(_FakeRegistrar.conversation_calls, 1)
        self.assertEqual(_FakeRegistrar.oauth_calls, 1)
        add_registered.assert_called_once_with(account)
        delete_partial.assert_called_once_with("new@example.com")
        import_warehouses.assert_called_once_with(3, account)

    def test_finished_mode_conversation_failure_stops_before_oauth(self) -> None:
        openai_register.config.update({"register_mode": "free", "product_mode": "finished", "proxy": ""})
        _FakeRegistrar.conversation_error = RuntimeError("conversation blocked")
        saved = {"email": "new@example.com", "status": "oauth_missing", "product_mode": "finished"}
        registrar_patch, step_patch, log_patch = self._worker_patches()
        with (
            registrar_patch,
            step_patch,
            log_patch,
            patch.object(openai_register.partial_account_store, "upsert_partial_account", return_value=saved) as upsert,
            patch.object(openai_register, "add_registered_account") as add_registered,
            patch.object(openai_register, "_maybe_import_registered_account_to_warehouses") as import_warehouses,
        ):
            result = openai_register.worker(4)

        self.assertTrue(result["ok"])
        self.assertTrue(result["conversation_failed"])
        self.assertEqual(result["storage"], "partial")
        self.assertEqual(_FakeRegistrar.conversation_calls, 1)
        self.assertEqual(_FakeRegistrar.oauth_calls, 0)
        self.assertIn("OAuth 前网页对话失败", upsert.call_args.args[0]["reason"])
        add_registered.assert_not_called()
        import_warehouses.assert_not_called()

    def test_finished_mode_forces_sub2api_and_cpa_import(self) -> None:
        openai_register.config.update({"register_mode": "free", "product_mode": "finished"})
        account = {"email": "new@example.com", "product_mode": "finished"}
        with (
            patch("services.sub2api_store.get_sub2api_config", return_value={"enabled": True, "auto_import": False}),
            patch(
                "services.sub2api_store.import_account_dicts_to_sub2api",
                return_value={"ok": True, "success": 1, "fail": 0, "account_ids": [42]},
            ) as sub2api_import,
            patch("services.sub2api_store.test_sub2api_account", return_value={"ok": True, "status_code": 200}),
            patch("services.cpa_store.get_cpa_config", return_value={"enabled": True, "auto_import": False}),
            patch(
                "services.cpa_store.import_account_dicts_to_cpa",
                return_value={"ok": True, "success": 1, "fail": 0, "results": [{"filename": "new.json"}]},
            ) as cpa_import,
            patch.object(openai_register, "update_registered_account"),
            patch.object(openai_register, "step"),
        ):
            openai_register._maybe_import_registered_account_to_warehouses(4, account)

        sub2api_import.assert_called_once_with(account)
        cpa_import.assert_called_once_with(account)
        self.assertEqual(account["sub2api_health_status"], "ok")
        self.assertEqual(account["cpa_auth_file"], "new.json")


class Sub2ApiOAuthRedirectTests(unittest.TestCase):
    @staticmethod
    def _registrar() -> openai_register.PlatformRegistrar:
        registrar = object.__new__(openai_register.PlatformRegistrar)
        registrar.session = SimpleNamespace(cookies=[])
        registrar.proxy = ""
        registrar.clearance_user_agent = ""
        registrar.clearance_failure_reason = ""
        registrar.client_auth_session = ""
        registrar.device_id = "device-id"
        registrar.current_mailbox = {}
        registrar.email = "new@example.com"
        return registrar

    def test_callback_redirect_is_extracted_without_requesting_localhost(self) -> None:
        registrar = object.__new__(openai_register.PlatformRegistrar)
        registrar.session = object()
        registrar.proxy = ""
        registrar.clearance_user_agent = ""
        registrar.client_auth_session = ""
        response = Mock(
            status_code=302,
            headers={"Location": "http://localhost:1455/auth/callback?code=oauth-code&state=oauth-state"},
            url="https://auth.openai.com/authorize",
            text="",
        )
        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")) as request,
        ):
            callback = registrar._follow_sub2api_oauth_callback("https://auth.openai.com/authorize", 5)

        self.assertEqual(callback["code"], "oauth-code")
        self.assertEqual(callback["state"], "oauth-state")
        request.assert_called_once()

    def test_consent_page_selects_personal_workspace_before_callback(self) -> None:
        registrar = object.__new__(openai_register.PlatformRegistrar)
        payload = {"workspaces": [{"id": "personal-workspace", "kind": "personal"}]}
        cookie_value = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=") + ".signature"
        registrar.session = SimpleNamespace(cookies=[SimpleNamespace(name="oai-client-auth-session", value=cookie_value)])
        registrar.proxy = ""
        registrar.clearance_user_agent = ""
        registrar.clearance_failure_reason = ""
        registrar.client_auth_session = ""
        registrar.device_id = "device-id"

        consent = Mock(
            status_code=200,
            headers={},
            url="https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            text="<html></html>",
        )
        consent.json.return_value = {}
        selected = Mock(
            status_code=200,
            headers={},
            url="https://auth.openai.com/api/accounts/workspace/select",
            text='{"continue_url":"http://localhost:1455/auth/callback?code=oauth-code&state=oauth-state"}',
        )
        selected.json.return_value = {
            "continue_url": "http://localhost:1455/auth/callback?code=oauth-code&state=oauth-state",
        }
        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", side_effect=[(consent, ""), (selected, "")]) as request,
            patch.object(openai_register, "step"),
        ):
            callback = registrar._follow_sub2api_oauth_callback("https://auth.openai.com/authorize", 6)

        self.assertEqual(callback["code"], "oauth-code")
        self.assertEqual(callback["state"], "oauth-state")
        self.assertEqual(request.call_count, 2)
        workspace_call = request.call_args_list[1]
        self.assertEqual(workspace_call.args[1], "post")
        self.assertEqual(workspace_call.kwargs["json"], {"workspace_id": "personal-workspace"})

    def test_choose_account_posts_matching_session_from_real_form_protocol(self) -> None:
        registrar = self._registrar()
        chooser = Mock(
            status_code=200,
            headers={},
            url="https://auth.openai.com/choose-an-account",
            text="""
                <form method="post" action="/choose-an-account">
                  <input type="hidden" name="intent" value="select">
                  <button name="session_id" value="us_other">Other other@example.com</button>
                  <button name="session_id" value="us_expected">New new@example.com</button>
                </form>
            """,
        )
        chooser.json.return_value = {}
        selected = Mock(
            status_code=302,
            headers={"Location": "http://localhost:1455/auth/callback?code=oauth-code&state=oauth-state"},
            url="https://auth.openai.com/choose-an-account",
            text="",
        )
        selected.json.return_value = {}
        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", side_effect=[(chooser, ""), (selected, "")]) as request,
            patch.object(openai_register, "step"),
        ):
            callback = registrar._follow_sub2api_oauth_callback(
                "https://auth.openai.com/authorize",
                7,
                expected_email="new@example.com",
            )

        self.assertEqual(callback["code"], "oauth-code")
        self.assertEqual(callback["state"], "oauth-state")
        account_call = request.call_args_list[1]
        self.assertEqual(account_call.args[1], "post")
        self.assertEqual(account_call.args[2], "https://auth.openai.com/choose-an-account")
        self.assertEqual(account_call.kwargs["data"], {"intent": "select", "session_id": "us_expected"})

    def test_choose_account_without_match_uses_email_login_form(self) -> None:
        registrar = self._registrar()
        chooser = Mock(
            status_code=200,
            headers={},
            url="https://auth.openai.com/choose-an-account",
            text="""
                <form method="post" action="/choose-an-account">
                  <button name="session_id" value="us_other">Other other@example.com</button>
                </form>
            """,
        )
        login_response = Mock(status_code=200, headers={}, url="https://auth.openai.com/api/accounts/authorize/continue", text="")
        login_response.json.return_value = {"continue_url": "/log-in/password"}
        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(login_response, "")) as request,
            patch.object(openai_register, "build_sentinel_token", return_value=""),
            patch.object(openai_register, "step"),
        ):
            response = registrar._submit_sub2api_oauth_account_step(chooser, 8, "new@example.com")

        self.assertIs(response, login_response)
        call = request.call_args
        self.assertEqual(call.args[2], "https://auth.openai.com/api/accounts/authorize/continue")
        self.assertEqual(call.kwargs["json"], {"username": {"kind": "email", "value": "new@example.com"}})
        self.assertIn("_code_not_before", registrar.current_mailbox)

    def test_account_deactivated_is_reported_explicitly(self) -> None:
        registrar = self._registrar()
        deactivated = Mock(
            status_code=200,
            headers={},
            url="https://auth.openai.com/email-verification",
            text='<html><div data-error-code="account_deactivated">Account deactivated</div></html>',
        )
        deactivated.json.return_value = {}
        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(deactivated, "")),
        ):
            with self.assertRaisesRegex(RuntimeError, "sub2api_oauth_account_deactivated"):
                registrar._follow_sub2api_oauth_callback(
                    "https://auth.openai.com/authorize",
                    9,
                    expected_email="new@example.com",
                )

    def test_login_or_create_account_url_is_login_state(self) -> None:
        self.assertEqual(
            openai_register._classify_auth_state_from_url("https://auth.openai.com/log-in-or-create-account"),
            "login",
        )

    def test_login_password_url_uses_passwordless_email_otp(self) -> None:
        registrar = self._registrar()
        password_page = Mock(
            status_code=200,
            headers={},
            url="https://auth.openai.com/log-in/password",
            text="<html></html>",
        )
        password_page.json.return_value = {}
        otp_page = Mock(
            status_code=200,
            headers={},
            url="https://auth.openai.com/email-verification",
            text="<html></html>",
        )
        otp_page.json.return_value = {}
        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", side_effect=[(password_page, ""), (otp_page, "")]),
            patch.object(registrar, "_send_passwordless_otp") as send_otp,
            patch.object(
                registrar,
                "_wait_for_sub2api_oauth_otp",
                return_value={"continue_url": "http://localhost:1455/auth/callback?code=oauth-code&state=oauth-state"},
            ),
            patch.object(openai_register, "step"),
        ):
            callback = registrar._follow_sub2api_oauth_callback(
                "https://auth.openai.com/authorize",
                10,
                expected_email="new@example.com",
            )

        self.assertEqual(callback["code"], "oauth-code")
        self.assertEqual(callback["state"], "oauth-state")
        send_otp.assert_called_once_with(10)

    def test_oauth_session_rebuild_preserves_headers_and_cookies(self) -> None:
        class CookieSink:
            def __init__(self) -> None:
                self.values = []

            def set(self, name, value, **kwargs) -> None:
                self.values.append((name, value, kwargs))

        registrar = self._registrar()
        old_session = SimpleNamespace(
            headers={"user-agent": "test-agent"},
            cookies=[SimpleNamespace(name="session", value="cookie-value", domain=".openai.com", path="/")],
            close=Mock(),
        )
        new_session = SimpleNamespace(headers={}, cookies=CookieSink())
        registrar.session = old_session
        with patch.object(openai_register, "create_session", return_value=new_session):
            registrar._replace_session_preserving_auth()

        self.assertIs(registrar.session, new_session)
        self.assertEqual(new_session.headers["user-agent"], "test-agent")
        self.assertIn(("session", "cookie-value", {"domain": ".openai.com", "path": "/"}), new_session.cookies.values)
        self.assertTrue(any(item[0] == "oai-did" for item in new_session.cookies.values))
        old_session.close.assert_called_once()

    def test_tls_failure_rebuilds_session_and_retries_new_oauth_authorization(self) -> None:
        registrar = self._registrar()
        registrar._replace_session_preserving_auth = Mock()
        callback = {"code": "oauth-code", "state": "oauth-state"}
        credentials = {"access_token": "oauth-access", "refresh_token": "oauth-refresh"}
        updates = {
            "email": "new@example.com",
            "access_token": "oauth-access",
            "refresh_token": "oauth-refresh",
            "client_id": openai_register.platform_oauth_client_id,
        }
        with (
            patch("services.sub2api_store.generate_openai_oauth_url", side_effect=[
                {"auth_url": "https://auth.openai.com/authorize?attempt=1", "session_id": "session-1"},
                {"auth_url": "https://auth.openai.com/authorize?attempt=2", "session_id": "session-2"},
            ]) as generate,
            patch.object(
                registrar,
                "_follow_sub2api_oauth_callback",
                side_effect=[RuntimeError("curl: (35) invalid library"), callback],
            ),
            patch("services.sub2api_store.exchange_openai_oauth_code", return_value=credentials),
            patch("services.account_oauth_service.oauth_token_updates", return_value=updates),
            patch("services.account_token_profile.token_profile", return_value={"oauth_capable": True}),
            patch.object(openai_register, "step"),
        ):
            result = registrar.acquire_sub2api_oauth(11, {"email": "new@example.com"})

        self.assertEqual(result["refresh_token"], "oauth-refresh")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(registrar._replace_session_preserving_auth.call_count, 2)


class PreOAuthConversationTests(unittest.TestCase):
    def test_hi_conversation_uses_registered_access_token_and_same_session(self) -> None:
        registrar = object.__new__(openai_register.PlatformRegistrar)
        registrar.session = object()
        registrar.proxy = "http://127.0.0.1:42000"
        registrar.clearance_user_agent = ""
        registrar.device_id = "device-id"
        response = Mock(status_code=200, headers={"content-type": "text/event-stream"}, url="https://chatgpt.com/backend-api/conversation", text="data: ok")
        response.json.side_effect = ValueError("not json")
        account = {"email": "new@example.com", "access_token": "web-access-token"}

        with (
            patch.object(openai_register, "_headers_with_clearance", side_effect=lambda headers, *_args: headers),
            patch.object(openai_register, "request_with_local_retry", return_value=(response, "")) as request,
            patch.object(openai_register, "step"),
        ):
            result = registrar.send_post_registration_hi(10, account)

        self.assertIs(result, account)
        self.assertTrue(account["chat_test_ok"])
        self.assertEqual(account["chat_test_method"], "conversation_pre_oauth")
        call = request.call_args
        self.assertIs(call.args[0], registrar.session)
        self.assertEqual(call.args[1], "post")
        self.assertEqual(call.kwargs["headers"]["authorization"], "Bearer web-access-token")
        self.assertEqual(call.kwargs["json"]["messages"][0]["content"]["parts"], ["Hi"])
        self.assertEqual(call.kwargs["json"]["model"], "auto")
        self.assertEqual(call.kwargs["retry_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
