from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable
from typing import Any

from services.register import mail_provider
from services.grok.oauth import CLI_BASE_URL, CLI_HEADERS, exchange_sso_for_oauth
from services.grok.vendor.xconsole_client import XConsoleAuthClient, YesCaptchaSolver
from services.grok.vendor.xconsole_client import config as xai_config
from services.grok.vendor.xconsole_client.mailbox import extract_xai_code

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


def _message_signature(message: dict[str, Any]) -> str:
    return "\x1f".join(
        str(message.get(key) or "").strip()
        for key in ("id", "message_id", "received_at", "subject", "body_preview", "code")
    )


def _mailbox_snapshot(mail_config: dict[str, Any], mailbox: dict[str, Any]) -> set[str]:
    try:
        result = mail_provider.recent_messages(mail_config, mailbox, limit=20, mode="auto")
    except Exception:
        return set()
    messages = result.get("messages") if isinstance(result.get("messages"), list) else []
    return {
        signature
        for message in messages
        if isinstance(message, dict) and (signature := _message_signature(message))
    }


def _cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel and should_cancel():
        raise RuntimeError("Grok registration cancelled")


def _wait_for_xai_code(
    mail_config: dict[str, Any],
    mailbox: dict[str, Any],
    *,
    timeout: float,
    progress: Callable[[str], None],
    should_cancel: Callable[[], bool] | None,
    seen_messages: set[str] | None = None,
) -> str:
    deadline = time.time() + max(10.0, float(timeout or 120.0))
    last_count = -1
    baseline = set(seen_messages or ())
    while time.time() < deadline:
        _cancelled(should_cancel)
        result = mail_provider.recent_messages(mail_config, mailbox, limit=20, mode="auto")
        messages = result.get("messages") if isinstance(result.get("messages"), list) else []
        new_messages = [
            message
            for message in messages
            if isinstance(message, dict) and _message_signature(message) not in baseline
        ]
        if len(new_messages) != last_count:
            progress(f"等待 xAI 邮件验证码，当前邮件 {len(messages)} 封")
            last_count = len(new_messages)
        for message in new_messages:
            if not isinstance(message, dict):
                continue
            text = "\n".join(
                str(message.get(key) or "")
                for key in ("subject", "body_preview", "code")
            )
            lowered = text.lower()
            if "xai" not in lowered and "x.ai" not in lowered:
                continue
            code = extract_xai_code(text)
            if code:
                return code
        for _ in range(8):
            _cancelled(should_cancel)
            time.sleep(0.25)
    raise TimeoutError("timeout waiting for xAI email verification code")


def _solver(
    grok_config: dict[str, Any],
    progress: Callable[[str], None],
    *,
    proxy: str = "",
) -> YesCaptchaSolver:
    provider = str(grok_config.get("captcha_provider") or "local").strip().lower()
    if provider == "yescaptcha":
        key = str(grok_config.get("yescaptcha_key") or "").strip()
        if not key:
            raise RuntimeError("YesCaptcha 模式缺少 API Key")
        endpoint = str(grok_config.get("yescaptcha_endpoint") or "").strip() or None
        auto_fallback = True
    else:
        key = "local"
        endpoint = str(grok_config.get("local_solver_url") or "http://127.0.0.1:5072").strip()
        auto_fallback = False
    return YesCaptchaSolver(
        key,
        endpoint=endpoint,
        timeout=max(30.0, float(grok_config.get("captcha_timeout") or 120.0)),
        poll_interval=2.0,
        debug=False,
        on_progress=lambda message: progress(f"Turnstile: {message}"),
        auto_fallback_endpoint=auto_fallback,
        proxy=str(proxy or "").strip() if provider != "yescaptcha" else "",
    )


def _mark_mailbox_result(
    mailbox: dict[str, Any],
    *,
    success: bool,
    error: Exception | str | None = None,
    emit: Callable[[str, str], None],
) -> None:
    try:
        mail_provider.mark_mailbox_result(mailbox, success=success, error=error)
    except Exception as state_error:
        emit(f"邮箱状态保存失败，注册结果继续保留：{state_error}", "yellow")


def register_grok_account(
    index: int,
    *,
    proxy: str,
    mail_config: dict[str, Any],
    grok_config: dict[str, Any],
    progress: Callable[[str, str], None],
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run xAI signup, SSO extraction, and optional Grok CLI OAuth exchange."""

    def emit(message: str, level: str = "") -> None:
        progress(f"[Grok任务{index}] {message}", level)

    mailbox: dict[str, Any] | None = None
    client: XConsoleAuthClient | None = None
    email = ""
    password = ""
    sso = ""
    try:
        _cancelled(should_cancel)
        mailbox = mail_provider.create_mailbox(mail_config)
        email = str(mailbox.get("address") or "").strip().lower()
        if not email:
            raise RuntimeError("邮箱提供商没有返回邮箱地址")
        password = f"Gk!{secrets.token_urlsafe(12)}aA7"
        emit(f"已领取邮箱 {email}")

        client = XConsoleAuthClient(
            debug=False,
            proxy=str(proxy or "").strip() or None,
            signup_url=SIGNUP_URL,
            timeout=max(15.0, float(grok_config.get("request_timeout") or 30.0)),
        )
        emit("加载 xAI 注册页")
        client.visit_home()
        client.load_signup_page()
        _cancelled(should_cancel)

        sitekey = str(getattr(client, "turnstile_sitekey", "") or xai_config.TURNSTILE_SITEKEY).strip()
        if not sitekey:
            raise RuntimeError("未能从 xAI 注册页提取 Turnstile sitekey")
        solver = _solver(grok_config, lambda message: emit(message), proxy=proxy)
        emit("开始本地 Turnstile 验证")
        turnstile = solver.solve_turnstile(
            website_url=SIGNUP_URL,
            website_key=sitekey,
            premium=str(grok_config.get("captcha_provider") or "local").lower() == "yescaptcha",
            fallback_non_premium=True,
        )
        if not turnstile:
            raise RuntimeError("Turnstile Solver 返回空 token")

        _cancelled(should_cancel)
        client.validate_password(email, password)
        seen_messages = _mailbox_snapshot(mail_config, mailbox)
        emit("发送 xAI 邮箱验证码")
        client.create_email_validation_code(email)
        code = _wait_for_xai_code(
            mail_config,
            mailbox,
            timeout=float(grok_config.get("email_timeout") or 120.0),
            progress=lambda message: emit(message),
            should_cancel=should_cancel,
            seen_messages=seen_messages,
        )
        emit(f"已收到 xAI 验证码 {code[:3]}***")
        verification = client.verify_email_validation_code(email, code)
        verification_http = int(getattr(verification, "http_status", 200) or 200)
        verification_grpc = getattr(verification, "grpc_status", None)
        if verification_http >= 400 or verification_grpc not in (None, 0):
            raise RuntimeError(
                "xAI email verification rejected: "
                f"HTTP {verification_http}, gRPC {verification_grpc}"
            )
        client.validate_password(email, password)

        _cancelled(should_cancel)
        emit("创建 xAI 账号")
        signup = client.create_account(
            email=email,
            given_name="Moyu",
            family_name="User",
            password=password,
            email_validation_code=code,
            turnstile_token=turnstile,
            castle_request_token="",
            conversion_id=str(uuid.uuid4()),
        )
        http_status = int(getattr(signup, "http_status", 0) or 0)
        if http_status != 200:
            raise RuntimeError(
                f"xAI create_account failed: HTTP {http_status}, "
                f"{str(getattr(signup, 'error', '') or '')[:300]}"
            )
        extract_signup_error = getattr(client, "extract_signup_error", None)
        signup_error = (
            str(extract_signup_error(str(getattr(signup, "rsc_body", "") or "")) or "")
            if callable(extract_signup_error)
            else ""
        )
        if signup_error:
            raise RuntimeError(f"xAI create_account rejected: {signup_error}")

        emit("提取 xAI SSO")
        sso = str(
            client.fetch_sso_token(
                email=email,
                password=password,
                save=False,
                retries=4,
            )
            or ""
        ).strip()
        if not sso:
            emit("SSO 链为空，尝试新账号密码会话", "yellow")
            time.sleep(2.0)
            signin_url = "https://accounts.x.ai/sign-in?redirect=grok-com"
            signin_turnstile = solver.solve_turnstile(
                website_url=signin_url,
                website_key=sitekey,
                premium=False,
                fallback_non_premium=True,
            )
            sso = str(
                client.obtain_session_via_password(
                    email=email,
                    password=password,
                    turnstile_token=signin_turnstile,
                    referer=signin_url,
                    retries=3,
                )
                or ""
            ).strip()
            if not sso:
                signin_turnstile = solver.solve_turnstile(
                    website_url=signin_url,
                    website_key=sitekey,
                    premium=False,
                    fallback_non_premium=True,
                )
                time.sleep(1.5)
                sso = str(
                    client.obtain_session_via_password(
                        email=email,
                        password=password,
                        turnstile_token=signin_turnstile,
                        referer=signin_url,
                        retries=2,
                    )
                    or ""
                ).strip()
        if not sso:
            raise RuntimeError("账号可能已创建，但没有取得 xAI SSO")

        token: dict[str, Any] = {}
        oauth_error = ""
        if bool(grok_config.get("exchange_oauth", True)):
            try:
                token = exchange_sso_for_oauth(
                    sso,
                    proxy=proxy,
                    timeout=float(grok_config.get("oauth_timeout") or 45.0),
                    progress=lambda message: emit(message),
                    should_cancel=should_cancel,
                )
            except Exception as error:
                oauth_error = str(error)
                emit(f"OAuth 换取失败，保留 SSO 供 sub2api 导入：{oauth_error}", "yellow")

        account = {
            "platform": "grok",
            "email": email,
            "password": password,
            "sso_token": sso,
            "access_token": str(token.get("access_token") or ""),
            "refresh_token": str(token.get("refresh_token") or ""),
            "id_token": str(token.get("id_token") or ""),
            "token_type": str(token.get("token_type") or "Bearer"),
            "client_id": str(token.get("client_id") or ""),
            "scope": str(token.get("scope") or ""),
            "subject": str(token.get("sub") or ""),
            "token_expires_at": token.get("expires_at"),
            "token_expires_in": token.get("expires_in"),
            "token_endpoint": str(token.get("token_endpoint") or ""),
            "base_url": str(token.get("base_url") or CLI_BASE_URL),
            "headers": token.get("headers") if isinstance(token.get("headers"), dict) else dict(CLI_HEADERS),
            "source_type": "grok_register",
            "register_mode": "grok",
            "plan_type": "grok",
            "proxy": str(proxy or ""),
            "oauth_status": "ready" if token.get("access_token") else "sso_only",
            "oauth_error": oauth_error,
        }
        _mark_mailbox_result(mailbox, success=True, emit=emit)
        emit("Grok 账号注册完成", "green")
        return {"ok": True, "index": index, "result": account, "oauth_ready": bool(token.get("access_token"))}
    except Exception as error:
        if mailbox:
            _mark_mailbox_result(mailbox, success=False, error=error, emit=emit)
        emit(f"失败：{error}", "red")
        return {"ok": False, "index": index, "email": email, "error": str(error), "sso_token": sso}
    finally:
        if client is not None:
            client.close()
