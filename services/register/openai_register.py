from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from curl_cffi import requests

from services.proxy_service import ClearanceBundle, proxy_settings
from services.security_redaction import redact_text, redact_value
from services.atomic_json_store import read_json
from services.register import mail_provider
from services import k12_service, proxy_pool_store
from services.chatgpt_web_service import ChatGPTWebClient
from services.registered_account_store import add_registered_account, update_registered_account
from services import partial_account_store

base_dir = Path(__file__).resolve().parent
config = {
    "mail": {
        "request_timeout": 30,
        "wait_timeout": 30,
        "wait_interval": 2,
        "api_use_register_proxy": True,
        "providers": [],
    },
    "proxy": "",
    "total": 10,
    "threads": 3,
    "k12_mode": False,
    "register_mode": "free",
    "product_mode": "partial",
    "mailbox_group": "",
}
register_config_file = base_dir.parents[1] / "data" / "register.json"
saved_config = read_json(register_config_file, {}, expected_schema_version=1)
config.update({key: saved_config[key] for key in ("mail", "proxy", "total", "threads", "k12_mode", "register_mode", "product_mode", "mailbox_group") if key in saved_config})
if str(config.get("register_mode") or "").strip().lower() == "free_wu":
    config["register_mode"] = "free"
elif str(config.get("register_mode") or "").strip().lower() == "free_crash":
    config["register_mode"] = "free"
    config["product_mode"] = "finished"

auth_base = "https://auth.openai.com"
chatgpt_base = "https://chatgpt.com"
platform_base = "https://platform.openai.com"
platform_oauth_client_id = "app_EMoamEEZ73f0CkXaXp7hrann"
platform_oauth_redirect_uri = "http://localhost:1455/auth/callback"
platform_oauth_audience = "https://api.openai.com/v1"
platform_auth0_client = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
sec_ch_ua = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
sec_ch_ua_full_version_list = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
default_timeout = 30
print_lock = threading.Lock()
stats_lock = threading.Lock()
stats = {"done": 0, "success": 0, "fail": 0, "start_time": 0.0}
register_log_sink = None

common_headers = {
    "accept": "application/json",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "connection": "keep-alive",
    "content-type": "application/json",
    "dnt": "1",
    "origin": auth_base,
    "priority": "u=1, i",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": user_agent,
}

navigate_headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-encoding": "gzip, deflate, br",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "connection": "keep-alive",
    "dnt": "1",
    "sec-gpc": "1",
    "sec-ch-ua": sec_ch_ua,
    "sec-ch-ua-arch": '"x86_64"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version-list": sec_ch_ua_full_version_list,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"10.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": user_agent,
}


def log(text: str, color: str = "") -> None:
    colors = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}
    if register_log_sink:
        try:
            register_log_sink(text, color)
        except Exception:
            pass
    with print_lock:
        prefix = colors.get(color, "")
        suffix = "\033[0m" if prefix else ""
        print(f"{prefix}{datetime.now().strftime('%H:%M:%S')} {text}{suffix}")


def step(index: int, text: str, color: str = "") -> None:
    log(f"[任务{index}] {text}", color)


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


from utils.pkce import generate_pkce as _generate_pkce  # noqa: F401


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> tuple[str, str]:
    return random.choice(["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]), random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )


def _random_birthdate() -> str:
    return f"{random.randint(1996, 2006):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _response_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _response_debug_detail(resp, limit: int = 800) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    parts = [
        f"url={redact_text(getattr(resp, 'url', ''))[:300]}",
        f"content_type={str(getattr(resp, 'headers', {}).get('content-type') or '')}",
    ]
    for key in ("cf-ray", "x-request-id", "openai-processing-ms"):
        value = str(getattr(resp, "headers", {}).get(key) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if data:
        parts.append(f"json={json.dumps(redact_value(data), ensure_ascii=False)[:limit]}")
    else:
        parts.append(f"body={redact_text(getattr(resp, 'text', ''))[:limit]}")
    return ", ".join(parts)


class _OAuthAccountChooserParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_chooser_form = False
        self._current_button: dict[str, Any] | None = None
        self.choices: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "form":
            action = values.get("action", "").strip().lower()
            method = values.get("method", "post").strip().lower()
            self._in_chooser_form = action.endswith("/choose-an-account") and method == "post"
            return
        if not self._in_chooser_form or tag.lower() != "button":
            return
        if values.get("name") != "session_id" or not values.get("value"):
            return
        self._current_button = {"session_id": values["value"], "text_parts": []}

    def handle_data(self, data: str) -> None:
        if self._current_button is not None and data.strip():
            self._current_button["text_parts"].append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "button" and self._current_button is not None:
            label = " ".join(self._current_button.pop("text_parts", []))
            self.choices.append({"session_id": str(self._current_button["session_id"]), "label": label})
            self._current_button = None
        elif lowered == "form":
            self._in_chooser_form = False


def _oauth_account_choices(html: object) -> list[dict[str, str]]:
    parser = _OAuthAccountChooserParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:
        return []
    return parser.choices


def _oauth_response_error_code(resp) -> str:
    if resp is None:
        return ""
    data = _response_json(resp)
    combined = f"{json.dumps(data, ensure_ascii=False)}\n{str(getattr(resp, 'text', '') or '')}".lower()
    for code in (
        "account_deactivated",
        "account_disabled",
        "account_suspended",
        "account_closed",
    ):
        if code in combined or code.replace("_", " ") in combined:
            return code
    return ""


def _is_cloudflare_challenge(resp) -> bool:
    if resp is None:
        return False
    try:
        status_code = int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    return (
        "<title>just a moment" in text
        or "<title>attention required! | cloudflare" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
        or "cf-browser-verification" in text
    )


def _truthy(value: object, fallback: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _mail_config(register_proxy: str = "", register_mode: str = "") -> dict:
    mail = config["mail"] if isinstance(config.get("mail"), dict) else {}
    use_register_proxy = _truthy(mail.get("api_use_register_proxy"), True)
    proxy = str(register_proxy or "").strip() if use_register_proxy else ""
    result = {**mail, "api_use_register_proxy": use_register_proxy, "proxy": proxy}
    result["mailbox_group"] = str(config.get("mailbox_group") or mail.get("mailbox_group") or "").strip()
    if register_mode:
        result["register_mode"] = register_mode
    return result


def _mailbox_group(mailbox: dict | None = None) -> str:
    return str((mailbox or {}).get("mailbox_group") or config.get("mailbox_group") or (config.get("mail") if isinstance(config.get("mail"), dict) else {}).get("mailbox_group") or "").strip()


def _classify_auth_state_from_url(url: str) -> str:
    """Classify the current Auth0/OpenAI page from URL only.

    This intentionally treats /about-you before any fallback branch so a
    verified-email flow never calls /api/accounts/user/register on the wrong
    auth step.
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    if extract_oauth_callback_params_from_url(raw):
        return "oauth_callback"
    lowered = raw.lower()
    try:
        parsed = urlparse(lowered)
        path = parsed.path or ""
    except Exception:
        path = lowered
    if "choose-an-account" in path:
        return "choose_account"
    if "add-phone" in path or "phone" in path and "add" in path:
        return "add_phone"
    if "sign-in-with-chatgpt/codex/consent" in path or path.rstrip("/").endswith("/workspace"):
        return "workspace"
    if "about-you" in path or "about_you" in path or path.rstrip("/") in {"/about"}:
        return "about_you"
    if "email-verification" in path or "email_otp" in lowered or "email-otp" in lowered:
        return "email_verification"
    if path.endswith("/log-in/password") or path.endswith("/login/password"):
        return "login_password"
    if "log-in-or-create-account" in path:
        return "login"
    if "create-account" in path or "signup" in path or "sign-up" in path:
        return "signup"
    if "/log-in" in path or "/login" in path:
        return "login"
    return ""


def _authorize_landed_page(resp) -> str:
    """诊断用：粗判 authorize 之后落在哪个页面。返回 signup / login / "" 仅供日志。

    注意：email-verification / email_otp_verification 在注册和登录流程里都会出现，
    无法据此可靠区分，所以这里只用于打日志，绝不据此中断注册流程。
    """
    if resp is None:
        return ""
    if _oauth_response_error_code(resp):
        return "account_deactivated"
    final_url_raw = str(getattr(resp, "url", "") or "")
    url_state = _classify_auth_state_from_url(final_url_raw)
    if url_state:
        return url_state
    final_url = final_url_raw.lower()
    data = _response_json(resp)
    page_type = ""
    page = data.get("page") if isinstance(data, dict) else None
    if isinstance(page, dict):
        page_type = str(page.get("type") or "").lower()
    if "email-verification" in final_url or "email_otp" in page_type or "email" in page_type and "verification" in page_type:
        return "email_verification"
    if "about-you" in final_url or "about_you" in page_type or "about" in page_type:
        return "about_you"
    if "sign-in-with-chatgpt/codex/consent" in final_url or "workspace" in final_url or page_type == "sign_in_with_chatgpt_codex_consent":
        return "workspace"
    if "create-account" in final_url or "signup" in final_url or "create_account" in page_type:
        return "signup"
    if "/log-in" in final_url or "/login" in final_url or page_type in {"login", "password_verification"}:
        return "login"
    return ""


def create_mailbox(username: str | None = None, register_proxy: str = "", register_mode: str = "") -> dict:
    return mail_provider.create_mailbox(_mail_config(register_proxy, register_mode=register_mode), username)


def wait_for_code(mailbox: dict, register_proxy: str = "", register_mode: str = "") -> str | None:
    return mail_provider.wait_for_code(_mail_config(register_proxy, register_mode=register_mode), mailbox)


from utils.sentinel import (  # noqa: F401
    SentinelTokenGenerator,
    build_sentinel_headers as _build_sentinel_headers_bundle,
)


def build_sentinel_request_headers(session: requests.Session, device_id: str, flow: str) -> dict[str, str]:
    bundle = _build_sentinel_headers_bundle(session, device_id, flow, user_agent=user_agent, sec_ch_ua=sec_ch_ua)
    oai_sc = str(bundle.get("oai-sc") or "")
    if oai_sc:
        try:
            session.cookies.set("oai-sc", oai_sc, domain=".auth.openai.com")
            session.cookies.set("oai-sc", oai_sc, domain="auth.openai.com")
        except Exception:
            pass
    return {key: value for key, value in bundle.items() if key.startswith("openai-sentinel-") and value}


def build_sentinel_token(session: requests.Session, device_id: str, flow: str) -> str:
    """请求 sentinel token，返回 sentinel header 字符串（兼容旧接口）。"""
    return build_sentinel_request_headers(session, device_id, flow).get("openai-sentinel-token", "")


def create_session(proxy: str = "") -> Any:
    kwargs = proxy_settings.build_session_kwargs(
        proxy=proxy,
        upstream=True,
        impersonate="chrome145",
        verify=proxy_settings.verify_tls(),
    )
    return requests.Session(**kwargs)


def _is_tls_session_error(error: object) -> bool:
    text = str(error or "").strip().lower()
    return any(
        marker in text
        for marker in (
            "invalid library",
            "curl: (35)",
            "ssl connect error",
            "tls connect error",
            "tls handshake",
            "wrong version number",
        )
    )


def _apply_clearance_to_session(session: requests.Session, bundle: ClearanceBundle | None) -> None:
    if bundle is None:
        return
    if bundle.user_agent:
        session.headers["User-Agent"] = bundle.user_agent
        session.headers["user-agent"] = bundle.user_agent
    for name, value in bundle.cookies.items():
        try:
            session.cookies.set(name, value, domain=f".{bundle.target_host or 'openai.com'}")
            session.cookies.set(name, value, domain=bundle.target_host or "auth.openai.com")
        except Exception:
            continue


def _headers_with_clearance(
    headers: dict[str, str],
    target_url: str,
    proxy: str = "",
    user_agent_override: str = "",
) -> dict[str, str]:
    merged = proxy_settings.build_headers(
        headers=headers,
        target_url=target_url,
        proxy=proxy,
        upstream=True,
    )
    normalized = {str(key): str(value) for key, value in merged.items()}
    if user_agent_override:
        ua_key = next((key for key in normalized if key.lower() == "user-agent"), "user-agent")
        normalized[ua_key] = user_agent_override
    return normalized


def _cloudflare_block_message(resp, prefix: str = "被 Cloudflare 拦截", reason: str = "") -> str:
    status = getattr(resp, "status_code", "unknown")
    debug = _response_debug_detail(resp)
    reason = reason or "clearance 刷新失败或重试后仍失败，请更换 IP/代理重试"
    return f"{prefix}，{reason}: status={status}, {debug}"


def request_with_local_retry(session: requests.Session, method: str, url: str, retry_attempts: int | None = None, **kwargs):
    method_name = str(method or "GET").upper()
    attempts = retry_attempts if retry_attempts is not None else (3 if method_name in {"GET", "HEAD", "OPTIONS"} else 1)
    last_error = ""
    for _ in range(max(1, attempts)):
        try:
            return session.request(method_name, url, timeout=default_timeout, **kwargs), ""
        except Exception as error:
            last_error = str(error)
            if attempts > 1:
                time.sleep(1)
    return None, last_error


def validate_otp(session: requests.Session, device_id: str, code: str):
    headers = dict(common_headers)
    headers["referer"] = f"{auth_base}/email-verification"
    headers["oai-device-id"] = device_id
    headers.update(_make_trace_headers())
    # 对齐 HAR: 浏览器 OTP validate 不发 openai-sentinel-token
    resp, error = request_with_local_retry(session, "post", f"{auth_base}/api/accounts/email-otp/validate", json={"code": code}, headers=headers, verify=proxy_settings.verify_tls())
    return resp, error


def extract_oauth_callback_params_from_url(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {"code": code, "state": str((params.get("state") or [""])[0]).strip(), "scope": str((params.get("scope") or [""])[0]).strip()}


def request_platform_oauth_token(session: requests.Session, code: str, code_verifier: str) -> dict | None:
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "auth0-client": platform_auth0_client,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": platform_base,
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": f"{platform_base}/",
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": user_agent,
    }
    resp = session.post(
        f"{auth_base}/api/accounts/oauth/token",
        headers=headers,
        json={
            "client_id": platform_oauth_client_id,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": platform_oauth_redirect_uri,
        },
        verify=proxy_settings.verify_tls(),
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"OAuth token exchange failed: HTTP {resp.status_code}")
        return None
    return _response_json(resp)


def _session_cookie_value(session: requests.Session, name: str) -> str:
    try:
        for cookie in session.cookies:
            if getattr(cookie, "name", "") == name:
                return str(getattr(cookie, "value", "") or "")
    except Exception:
        pass
    try:
        return str(session.cookies.get(name) or "")
    except Exception:
        return ""


def _nextauth_signin_url(resp) -> str:
    data = _response_json(resp)
    url = str(data.get("url") or data.get("redirect") or "").strip()
    if url:
        return url
    return str(getattr(resp, "headers", {}).get("location") or "").strip()


def _parse_session_expires(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except Exception:
        return None


class PlatformRegistrar:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = str(proxy or "").strip()
        self.session = create_session(self.proxy)
        self.clearance_user_agent = ""
        self.clearance_failure_reason = ""
        self.device_id = str(uuid.uuid4())
        self.code_verifier = ""
        self.platform_auth_code = ""
        self.client_auth_session = ""
        self.partial_stage = ""
        self.current_mailbox: dict | None = None
        self.web_login_ok = False
        self.web_login_result: dict[str, Any] = {}
        self.oauth_completed = False

    def close(self) -> None:
        self.session.close()

    def _replace_session_preserving_auth(self) -> None:
        old_session = self.session
        new_session = create_session(self.proxy)
        try:
            old_headers = getattr(old_session, "headers", None)
            new_headers = getattr(new_session, "headers", None)
            if old_headers is not None and new_headers is not None:
                new_headers.update(dict(old_headers))
        except Exception:
            pass

        copied_names: set[str] = set()
        try:
            for cookie in getattr(old_session, "cookies", []) or []:
                name = str(getattr(cookie, "name", "") or "").strip()
                if not name:
                    continue
                value = str(getattr(cookie, "value", "") or "")
                kwargs: dict[str, str] = {}
                domain = str(getattr(cookie, "domain", "") or "").strip()
                path = str(getattr(cookie, "path", "") or "").strip()
                if domain:
                    kwargs["domain"] = domain
                if path:
                    kwargs["path"] = path
                new_session.cookies.set(name, value, **kwargs)
                copied_names.add(name)
        except Exception:
            pass
        try:
            cookie_values = old_session.cookies.get_dict()
            for name, value in cookie_values.items():
                if str(name) not in copied_names:
                    new_session.cookies.set(str(name), str(value))
        except Exception:
            pass
        try:
            new_session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        except Exception:
            pass

        self.session = new_session
        try:
            old_session.close()
        except Exception:
            pass

    def _navigate_headers(self, referer: str = "") -> dict[str, str]:
        headers = dict(navigate_headers)
        if referer:
            headers["referer"] = referer
        if self.client_auth_session:
            headers["oai-client-auth-session"] = self.client_auth_session
        return headers

    def _json_headers(self, referer: str) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        if self.client_auth_session:
            headers["oai-client-auth-session"] = self.client_auth_session
        headers.update(_make_trace_headers())
        return headers

    def _record_partial(
        self,
        stage: str,
        status: str | None = None,
        *,
        error: object = "",
        mailbox: dict | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        email = str(getattr(self, "email", "") or "").strip()
        password = str(getattr(self, "password", "") or "").strip()
        if "@" not in email:
            return
        resolved_status = str(status or stage or "").strip().lower()
        extra_payload = dict(extra or {})
        web_login_ok = bool(extra_payload.get("web_login_ok") or extra_payload.get("chat_test_ok"))
        if not web_login_ok or resolved_status not in partial_account_store.OAUTH_PARTIAL_STATUSES:
            return
        self.partial_stage = stage or self.partial_stage
        payload: dict[str, Any] = {
            "email": email,
            "password": password,
            "stage": stage or self.partial_stage or "partial",
            "status": resolved_status,
            "reason": str(error or "")[:800],
            "source": "register",
            "register_mode": str(config.get("register_mode") or ("k12" if config.get("k12_mode") else "free")),
            "mailbox_group": _mailbox_group(mailbox),
            "proxy": self.proxy,
            "client_id": platform_oauth_client_id,
            "web_login_ok": True,
        }
        if mailbox is None:
            mailbox = self.current_mailbox
        if mailbox:
            payload.update(
                {
                    "provider": str(mailbox.get("provider") or ""),
                    "provider_label": str(mailbox.get("label") or mailbox.get("provider_ref") or ""),
                    "inbox_address": str(mailbox.get("inbox_address") or ""),
                    "mailbox_group": _mailbox_group(mailbox),
                }
            )
        if extra_payload:
            payload.update({key: value for key, value in extra_payload.items() if value not in (None, "")})
        try:
            partial_account_store.upsert_partial_account(payload)
        except Exception:
            pass

    def _refresh_cloudflare_clearance(self, target_url: str, index: int) -> ClearanceBundle | None:
        self.clearance_failure_reason = ""
        profile = proxy_settings.get_profile(proxy=self.proxy, upstream=True)
        if not profile.clearance_enabled:
            self.clearance_failure_reason = (
                "可尝试使用 FlareSolverr 清障方式，注意需要 Docker 部署 flaresolverr、privoxy、warp-proxy 等相关容器"
            )
            step(index, f"检测到 Cloudflare 拦截，{self.clearance_failure_reason}", "yellow")
            return None
        step(index, "检测到 Cloudflare 拦截，尝试刷新 clearance", "yellow")
        bundle = proxy_settings.refresh_clearance(
            target_url=target_url,
            proxy=self.proxy,
            force=True,
            upstream=True,
        )
        if bundle is not None:
            _apply_clearance_to_session(self.session, bundle)
            self.clearance_user_agent = bundle.user_agent or self.clearance_user_agent
            step(index, "Cloudflare clearance 刷新完成，重试当前请求", "yellow")
        else:
            self.clearance_failure_reason = "clearance 刷新未返回可用 Cookie，请检查 FlareSolverr URL、代理和出口 IP"
            step(index, f"Cloudflare clearance 刷新失败：{self.clearance_failure_reason}", "yellow")
        return bundle

    def _follow_to_landing(self, url: str, headers: dict, index: int, depth: int = 0) -> tuple[Any, str]:
        if depth > 10:
            return None, ""
        resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=False, verify=proxy_settings.verify_tls())
        if resp is None:
            return None, error
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                return None, _cloudflare_block_message(resp, reason=self.clearance_failure_reason)
            retry_headers = _headers_with_clearance(dict(headers), url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", url, headers=retry_headers, allow_redirects=False, verify=proxy_settings.verify_tls())
            if resp is None or _is_cloudflare_challenge(resp):
                return None, error or _cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截")
        location = str(resp.headers.get("Location", "") or "") if resp.status_code in (301, 302, 303, 307, 308) else ""
        if "localhost:1455" in location.lower():
            return resp, "callback_redirect"
        if location:
            next_url = location if location.startswith("http") else f"{auth_base}{location}"
            return self._follow_to_landing(next_url, headers, index, depth + 1)
        return resp, ""

    def _platform_authorize(self, email: str, index: int) -> str:
        step(index, "开始注册流程（Auth0 入口）")
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        self.code_verifier, code_challenge = _generate_pkce()
        start_url = f"{auth_base}/create-account"
        screen_hint_val = "login_or_signup"
        params = {
            "issuer": auth_base,
            "client_id": platform_oauth_client_id,
            "audience": platform_oauth_audience,
            "redirect_uri": platform_oauth_redirect_uri,
            "device_id": self.device_id,
            "screen_hint": screen_hint_val,
            "prompt": "login",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": platform_auth0_client,
        }
        target_url = f"{auth_base}/api/accounts/authorize?{urlencode(params)}"
        headers = self._navigate_headers(f"{platform_base}/")
        headers = _headers_with_clearance(headers, target_url, self.proxy, self.clearance_user_agent)
        resp, flag = self._follow_to_landing(target_url, headers, index)
        if resp is None:
            raise RuntimeError(flag or "authorize_request_failed")
        if flag == "callback_redirect":
            location = str(resp.headers.get("Location", "") or "")
            callback_params = extract_oauth_callback_params_from_url(location)
            if callback_params:
                self.platform_auth_code = str(callback_params.get("code") or "").strip()
                step(index, "Auth0 入口返回[oauth_callback]")
                return "oauth_callback"
        if resp.status_code == 200:
            resp_url = str(getattr(resp, "url", "") or "")
            callback_params = extract_oauth_callback_params_from_url(resp_url)
            if callback_params:
                self.platform_auth_code = str(callback_params.get("code") or "").strip()
                step(index, f"Auth0 入口返回[oauth_callback] url={resp_url[:160]}")
                return "oauth_callback"
            landed = _authorize_landed_page(resp)
            step(index, f"Auth0 入口返回[{landed or '?'}] url={resp_url[:160]}")
            return landed
        err_body = _response_json(resp).get("error", {}) if resp else {}
        detail = f": {err_body.get('code', '')} - {err_body.get('message', '')}".strip(" -") if err_body else ""
        debug = _response_debug_detail(resp)
        raise RuntimeError(f"authorize_http_{resp.status_code}{detail}, {debug}")

    def _authorize_continue(self, email: str, index: int, screen_hint: str = "login_or_signup") -> dict:
        step(index, "提交账号信息到 authorize/continue")
        url = f"{auth_base}/api/accounts/authorize/continue"
        headers = self._json_headers(f"{auth_base}/create-account")
        step(index, "正在计算注册风控算力挑战（Sentinel PoW）...")
        sentinel = build_sentinel_token(self.session, self.device_id, "authorize_continue")
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
            step(index, "注册风控算力挑战通过")
        else:
            step(index, "注册风控算力挑战未返回令牌，继续尝试当前流程", "yellow")
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(
            self.session, "post", url,
            json={"username": {"value": email, "kind": "email"}, "screen_hint": screen_hint},
            headers=headers, verify=proxy_settings.verify_tls(),
        )
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/create-account")
            sentinel = build_sentinel_token(self.session, self.device_id, "authorize_continue")
            if sentinel:
                headers["openai-sentinel-token"] = sentinel
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(
                self.session, "post", url,
                json={"username": {"value": email, "kind": "email"}, "screen_hint": screen_hint},
                headers=headers, verify=proxy_settings.verify_tls(),
            )
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None:
            raise RuntimeError(error or "authorize_continue_request_failed")
        if resp.status_code == 403:
            return {"status": 403, "error": "403 拦截"}
        if resp.status_code != 200:
            data = _response_json(resp)
            raise RuntimeError(error or f"authorize_continue_http_{resp.status_code}, detail={json.dumps(data, ensure_ascii=False)[:300]}")
        return _response_json(resp)

    def _follow_flow_url(self, raw_url: str, referer: str, index: int, label: str) -> str:
        url = str(raw_url or "").strip()
        if not url:
            return ""
        url = url if url.startswith("http") else f"{auth_base}{url if url.startswith('/') else '/' + url}"
        callback_params = extract_oauth_callback_params_from_url(url)
        if callback_params:
            self.platform_auth_code = str(callback_params.get("code") or "").strip()
            step(index, f"{label} 获取 OAuth callback code")
            return "oauth_callback"
        headers = _headers_with_clearance(self._navigate_headers(referer), url, self.proxy, self.clearance_user_agent)
        resp, flag = self._follow_to_landing(url, headers, index)
        if resp is None:
            raise RuntimeError(flag or f"follow_flow failed: {url[:100]}")
        if flag == "callback_redirect":
            location = str(resp.headers.get("Location", "") or "")
            cp = extract_oauth_callback_params_from_url(location)
            if cp:
                self.platform_auth_code = str(cp.get("code") or "").strip()
                step(index, f"{label} 获取 OAuth callback")
                return "oauth_callback"
        if resp.status_code == 200:
            final_url = str(getattr(resp, "url", "") or "")
            cp = extract_oauth_callback_params_from_url(final_url)
            if cp:
                self.platform_auth_code = str(cp.get("code") or "").strip()
                step(index, f"{label} 获取 OAuth callback")
                return "oauth_callback"
            landed = _authorize_landed_page(resp)
            step(index, f"{label}[{landed or '?'}] url={final_url[:160]}")
            return landed
        raise RuntimeError(f"follow_flow_http_{resp.status_code}, {_response_debug_detail(resp)}")

    def _free_wu_follow_redirects(self, raw_url: str, index: int, referer: str = "", max_hops: int = 20):
        url = urljoin(f"{chatgpt_base}/", str(raw_url or "").strip())
        if not url:
            raise RuntimeError("free_wu_empty_redirect_url")
        current_referer = referer or chatgpt_base + "/"
        last_resp = None
        for _ in range(max(1, max_hops)):
            headers = _headers_with_clearance(self._navigate_headers(current_referer), url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=False, verify=proxy_settings.verify_tls())
            if _is_cloudflare_challenge(resp):
                bundle = self._refresh_cloudflare_clearance(url, index)
                if bundle is None:
                    raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
                headers = _headers_with_clearance(self._navigate_headers(current_referer), url, self.proxy, self.clearance_user_agent)
                resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=False, verify=proxy_settings.verify_tls())
                if _is_cloudflare_challenge(resp):
                    raise RuntimeError(_cloudflare_block_message(resp, "Free Cloudflare clearance 重试仍被拦截"))
            if resp is None:
                raise RuntimeError(error or "free_wu_redirect_request_failed")
            last_resp = resp
            location = str(resp.headers.get("Location", "") or "") if resp.status_code in (301, 302, 303, 307, 308) else ""
            if not location:
                return resp
            current_referer = str(getattr(resp, "url", "") or url)
            url = urljoin(current_referer, location)
        return last_resp

    @staticmethod
    def _oauth_workspaces_from_payload(payload: object) -> list[dict[str, Any]]:
        source = payload if isinstance(payload, dict) else {}
        auth_session = source.get("oai-client-auth-session")
        auth_session = auth_session if isinstance(auth_session, dict) else source
        workspaces = auth_session.get("workspaces") if isinstance(auth_session, dict) else None
        return [item for item in workspaces if isinstance(item, dict)] if isinstance(workspaces, list) else []

    def _sub2api_oauth_workspaces(self, response_data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        workspaces = self._oauth_workspaces_from_payload(response_data or {})
        if workspaces:
            return workspaces
        cookie = _session_cookie_value(self.session, "oai-client-auth-session")
        encoded = str(cookie or "").split(".", 1)[0].strip()
        if not encoded:
            return []
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return []
        return self._oauth_workspaces_from_payload(payload)

    def _select_sub2api_oauth_workspace(
        self,
        index: int,
        referer: str,
        response_data: dict[str, Any] | None = None,
        workspace_hint: str = "",
    ) -> str:
        workspaces = self._sub2api_oauth_workspaces(response_data)
        hint = str(workspace_hint or "").strip()
        selected = next((item for item in workspaces if hint and str(item.get("id") or "").strip() == hint), None)
        if selected is None:
            selected = next((item for item in workspaces if str(item.get("kind") or "").lower() == "personal" and str(item.get("id") or "").strip()), None)
        if selected is None:
            selected = next((item for item in workspaces if str(item.get("id") or "").strip()), None)
        workspace_id = str((selected or {}).get("id") or hint).strip()
        if not workspace_id:
            raise RuntimeError("sub2api_oauth_workspace_missing")

        step(index, f"成品模式：选择 OAuth workspace {workspace_id[:12]}...")
        url = f"{auth_base}/api/accounts/workspace/select"
        headers = self._free_wu_json_headers(referer or f"{auth_base}/sign-in-with-chatgpt/codex/consent")
        headers["accept"] = "application/json"
        headers["content-type"] = "application/json"
        resp, error = request_with_local_retry(
            self.session,
            "post",
            url,
            json={"workspace_id": workspace_id},
            headers=_headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent),
            allow_redirects=False,
            verify=proxy_settings.verify_tls(),
        )
        if resp is None or resp.status_code not in (200, 201, 302, 303):
            raise RuntimeError(error or f"sub2api_oauth_workspace_select_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp)}")
        data = _response_json(resp)
        location = str(getattr(resp, "headers", {}).get("Location") or "").strip()
        continue_url = str(data.get("continue_url") or data.get("redirect_url") or data.get("url") or location).strip()
        if not continue_url:
            raise RuntimeError(f"sub2api_oauth_workspace_select_missing_continue_url: {_response_debug_detail(resp)}")
        return urljoin(str(getattr(resp, "url", "") or url), continue_url)

    def _post_sub2api_oauth_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        referer: str,
    ):
        headers = self._navigate_headers(referer)
        headers["accept"] = navigate_headers["accept"]
        headers["content-type"] = "application/x-www-form-urlencoded"
        headers["origin"] = auth_base
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(
            self.session,
            "post",
            url,
            data=data,
            headers=headers,
            allow_redirects=False,
            verify=proxy_settings.verify_tls(),
        )
        if resp is None:
            raise RuntimeError(error or "sub2api_oauth_form_request_failed")
        error_code = _oauth_response_error_code(resp)
        if error_code:
            raise RuntimeError(f"sub2api_oauth_{error_code}")
        return resp

    def _submit_sub2api_oauth_login_email(self, index: int, expected_email: str, referer: str):
        email = str(expected_email or "").strip()
        if not email:
            raise RuntimeError("sub2api_oauth_expected_email_missing")
        if isinstance(self.current_mailbox, dict):
            self.current_mailbox["_code_not_before"] = datetime.now(timezone.utc)
        step(index, "成品模式：提交 OAuth 登录邮箱")
        url = f"{auth_base}/api/accounts/authorize/continue"
        headers = self._json_headers(referer or f"{auth_base}/log-in-or-create-account")
        sentinel = build_sentinel_token(self.session, self.device_id, "authorize_continue")
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(
            self.session,
            "post",
            url,
            json={"username": {"kind": "email", "value": email}},
            headers=headers,
            allow_redirects=False,
            verify=proxy_settings.verify_tls(),
        )
        if resp is None or int(getattr(resp, "status_code", 0) or 0) != 200:
            raise RuntimeError(error or f"sub2api_oauth_authorize_continue_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp)}")
        error_code = _oauth_response_error_code(resp)
        if error_code:
            raise RuntimeError(f"sub2api_oauth_{error_code}")
        return resp

    def _submit_sub2api_oauth_account_step(
        self,
        response,
        index: int,
        expected_email: str,
        used_session_ids: set[str] | None = None,
    ):
        final_url = str(getattr(response, "url", "") or f"{auth_base}/choose-an-account")
        expected = str(expected_email or "").strip().lower()
        used = used_session_ids if used_session_ids is not None else set()
        choices = _oauth_account_choices(getattr(response, "text", ""))
        selected = next(
            (
                item
                for item in choices
                if expected
                and expected in str(item.get("label") or "").strip().lower().split()
                and str(item.get("session_id") or "") not in used
            ),
            None,
        )
        if selected is not None:
            used.add(str(selected.get("session_id") or ""))
            step(index, "成品模式：选择当前注册账号的 OAuth 会话")
            return self._post_sub2api_oauth_form(
                urljoin(final_url, "/choose-an-account"),
                {"intent": "select", "session_id": str(selected.get("session_id") or "")},
                referer=final_url,
            )

        if not expected:
            raise RuntimeError("sub2api_oauth_expected_email_missing")
        step(index, "成品模式：账号选择页没有匹配会话，改用邮箱登录并发送验证码")
        return self._submit_sub2api_oauth_login_email(index, expected_email, final_url)

    def _wait_for_sub2api_oauth_otp(self, index: int) -> dict[str, Any]:
        mailbox = self.current_mailbox
        if not isinstance(mailbox, dict):
            raise RuntimeError("sub2api_oauth_mailbox_missing")
        last_error = ""
        for attempt in range(1, 3):
            step(index, "成品模式：等待 OAuth 登录验证码" if attempt == 1 else "成品模式：重新等待 OAuth 登录验证码")
            code = wait_for_code(mailbox, register_proxy=self.proxy, register_mode="free_wu")
            if code:
                try:
                    return self._validate_otp(code, index)
                except RuntimeError as error:
                    last_error = str(error)
                    lowered = last_error.lower()
                    if "account_deactivated" in lowered or "account deactivated" in lowered:
                        raise RuntimeError("sub2api_oauth_account_deactivated") from error
                    if "wrong_email_otp_code" not in lowered and "wrong code" not in lowered:
                        raise
            if attempt < 2:
                mailbox["_code_not_before"] = datetime.now(timezone.utc)
                self._send_passwordless_otp(index)
        raise RuntimeError(last_error or "sub2api_oauth_email_otp_timeout")

    @staticmethod
    def _sub2api_oauth_next_url(response) -> str:
        final_url = str(getattr(response, "url", "") or auth_base)
        data = _response_json(response)
        for key in ("continue_url", "redirect_url", "redirect_to", "url", "next_url"):
            value = str(data.get(key) or "").strip()
            if value:
                return urljoin(final_url, value)
        if int(getattr(response, "status_code", 0) or 0) in (301, 302, 303, 307, 308):
            location = str(getattr(response, "headers", {}).get("Location") or "").strip()
            if location:
                return urljoin(final_url, location)
        return ""

    def _follow_sub2api_oauth_callback(
        self,
        auth_url: str,
        index: int,
        max_hops: int = 30,
        workspace_hint: str = "",
        expected_email: str = "",
    ) -> dict[str, str]:
        url = str(auth_url or "").strip()
        if not url:
            raise RuntimeError("sub2api_oauth_missing_auth_url")
        current_referer = chatgpt_base + "/"
        pending_response = None
        state_visits: dict[str, int] = {}
        used_session_ids: set[str] = set()
        email_login_submissions = 0
        for _ in range(max(1, max_hops)):
            callback = extract_oauth_callback_params_from_url(url)
            if callback:
                return callback
            if pending_response is None:
                headers = _headers_with_clearance(self._navigate_headers(current_referer), url, self.proxy, self.clearance_user_agent)
                resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=False, verify=proxy_settings.verify_tls())
                if _is_cloudflare_challenge(resp):
                    bundle = self._refresh_cloudflare_clearance(url, index)
                    if bundle is None:
                        raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
                    headers = _headers_with_clearance(self._navigate_headers(current_referer), url, self.proxy, self.clearance_user_agent)
                    resp, error = request_with_local_retry(self.session, "get", url, headers=headers, allow_redirects=False, verify=proxy_settings.verify_tls())
            else:
                resp, error = pending_response, ""
                pending_response = None
            if resp is None:
                raise RuntimeError(error or "sub2api_oauth_authorize_request_failed")
            error_code = _oauth_response_error_code(resp)
            if error_code:
                raise RuntimeError(f"sub2api_oauth_{error_code}")
            final_url = str(getattr(resp, "url", "") or url)
            callback = extract_oauth_callback_params_from_url(final_url)
            if callback:
                return callback
            response_next = self._sub2api_oauth_next_url(resp)
            if response_next:
                current_referer = final_url
                url = response_next
                continue
            landed = _authorize_landed_page(resp)
            data = _response_json(resp)
            state_key = f"{landed or 'unknown'}:{urlparse(final_url).path.lower()}"
            state_visits[state_key] = state_visits.get(state_key, 0) + 1
            if state_visits[state_key] > 4:
                raise RuntimeError(f"sub2api_oauth_redirect_loop: state={landed or 'unknown'}, url={final_url[:180]}")
            if landed == "choose_account":
                current_referer = final_url
                pending_response = self._submit_sub2api_oauth_account_step(
                    resp,
                    index,
                    expected_email or getattr(self, "email", ""),
                    used_session_ids,
                )
                url = str(getattr(pending_response, "url", "") or final_url)
                continue
            if landed == "login_password":
                step(index, "成品模式：OAuth 要求重新登录，改用邮箱验证码")
                if isinstance(self.current_mailbox, dict):
                    self.current_mailbox["_code_not_before"] = datetime.now(timezone.utc)
                self._send_passwordless_otp(index)
                current_referer = final_url
                url = f"{auth_base}/email-verification"
                continue
            if landed == "login":
                email_login_submissions += 1
                if email_login_submissions > 2:
                    raise RuntimeError(f"sub2api_oauth_login_redirect_loop: url={final_url[:180]}")
                current_referer = final_url
                pending_response = self._submit_sub2api_oauth_login_email(
                    index,
                    expected_email or str(getattr(self, "email", "") or ""),
                    final_url,
                )
                url = str(getattr(pending_response, "url", "") or final_url)
                continue
            if landed == "email_verification":
                validate_data = self._wait_for_sub2api_oauth_otp(index)
                response_next = next(
                    (
                        str(validate_data.get(key) or "").strip()
                        for key in ("continue_url", "redirect_url", "redirect_to", "url", "next_url")
                        if str(validate_data.get(key) or "").strip()
                    ),
                    "",
                )
                current_referer = final_url
                url = urljoin(final_url, response_next) if response_next else final_url
                continue
            if landed == "workspace":
                current_referer = final_url
                url = self._select_sub2api_oauth_workspace(index, final_url, data, workspace_hint)
                continue
            if landed == "account_deactivated":
                raise RuntimeError("sub2api_oauth_account_deactivated")
            if landed == "add_phone":
                raise RuntimeError("sub2api_oauth_phone_required")
            raise RuntimeError(f"sub2api_oauth_requires_interaction: HTTP {resp.status_code}, landing={landed or 'unknown'}, url={final_url[:180]}")
        raise RuntimeError("sub2api_oauth_redirect_limit_exceeded")

    def acquire_sub2api_oauth(self, index: int, account: dict[str, Any]) -> dict[str, Any]:
        from services import sub2api_store
        from services.account_oauth_service import oauth_token_updates
        from services.account_token_profile import token_profile

        step(index, "成品模式：重建 OAuth HTTP 会话并保留登录 Cookie")
        self._replace_session_preserving_auth()
        oauth: dict[str, Any] = {}
        callback: dict[str, str] = {}
        for attempt in range(2):
            step(index, "成品模式：通过 sub2api 生成 OpenAI OAuth 授权链接")
            oauth = sub2api_store.generate_openai_oauth_url()
            try:
                callback = self._follow_sub2api_oauth_callback(
                    str(oauth.get("auth_url") or ""),
                    index,
                    workspace_hint=str(account.get("chatgpt_account_id") or account.get("workspace_id") or ""),
                    expected_email=str(account.get("email") or ""),
                )
                break
            except Exception as error:
                if attempt == 0 and _is_tls_session_error(error):
                    step(index, f"成品模式：OAuth TLS 会话异常，重建后重试：{str(error)[:180]}", "yellow")
                    self._replace_session_preserving_auth()
                    continue
                raise
        code = str(callback.get("code") or "").strip()
        state = str(callback.get("state") or "").strip()
        if not code or not state:
            raise RuntimeError("sub2api_oauth_callback_missing_code_or_state")
        step(index, "成品模式：已获取 OAuth 回调，交由 sub2api 换取 Token")
        credentials = sub2api_store.exchange_openai_oauth_code(
            session_id=str(oauth.get("session_id") or ""),
            code=code,
            state=state,
        )
        updates = oauth_token_updates(credentials, provenance="sub2api_registration_authorization")
        expected_email = str(account.get("email") or "").strip().lower()
        actual_email = str(updates.get("email") or expected_email).strip().lower()
        if expected_email and actual_email and expected_email != actual_email:
            raise RuntimeError(f"sub2api_oauth_email_mismatch: expected={expected_email}, actual={actual_email}")
        finished = {
            **account,
            **updates,
            "register_mode": "free",
            "product_mode": "finished",
            "oauth_status": "sub2api_oauth_acquired",
        }
        if not token_profile(finished).get("oauth_capable"):
            raise RuntimeError("sub2api_oauth_token_profile_invalid")
        step(index, "成品模式：sub2api OAuth Token 获取完成", "green")
        return finished

    def _free_wu_json_headers(self, referer: str, *, origin: str = auth_base) -> dict[str, str]:
        headers = dict(common_headers)
        headers["referer"] = referer
        headers["origin"] = origin
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        if self.client_auth_session:
            headers["oai-client-auth-session"] = self.client_auth_session
        return headers

    def _free_wu_init_page_email(self, email: str, index: int) -> None:
        step(index, "Free：初始化 chatgpt.com NextAuth 邮箱注册入口")
        landing_headers = _headers_with_clearance(dict(navigate_headers), chatgpt_base + "/", self.proxy, self.clearance_user_agent)
        landing_resp, landing_error = request_with_local_retry(self.session, "get", chatgpt_base + "/", headers=landing_headers, verify=proxy_settings.verify_tls())
        if _is_cloudflare_challenge(landing_resp):
            bundle = self._refresh_cloudflare_clearance(chatgpt_base + "/", index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(landing_resp, reason=self.clearance_failure_reason))
            landing_headers = _headers_with_clearance(dict(navigate_headers), chatgpt_base + "/", self.proxy, self.clearance_user_agent)
            landing_resp, landing_error = request_with_local_retry(self.session, "get", chatgpt_base + "/", headers=landing_headers, verify=proxy_settings.verify_tls())
        if landing_resp is None:
            raise RuntimeError(landing_error or "free_wu_chatgpt_landing_failed")

        csrf_url = f"{chatgpt_base}/api/auth/csrf"
        csrf_headers = _headers_with_clearance(
            {
                "accept": "application/json",
                "referer": chatgpt_base + "/",
                "user-agent": user_agent,
                "cache-control": "no-cache",
            },
            csrf_url,
            self.proxy,
            self.clearance_user_agent,
        )
        csrf_resp, csrf_error = request_with_local_retry(self.session, "get", csrf_url, headers=csrf_headers, verify=proxy_settings.verify_tls())
        if csrf_resp is None or csrf_resp.status_code != 200:
            raise RuntimeError(csrf_error or f"free_wu_csrf_http_{getattr(csrf_resp, 'status_code', 'unknown')}, {_response_debug_detail(csrf_resp)}")
        csrf_token = str(_response_json(csrf_resp).get("csrfToken") or "").strip()
        if not csrf_token:
            raise RuntimeError(f"free_wu_csrf_missing, {_response_debug_detail(csrf_resp)}")

        params = urlencode({"prompt": "login", "screen_hint": "login_or_signup", "login_hint": email})
        signin_url = f"{chatgpt_base}/api/auth/signin/openai?{params}"
        signin_headers = _headers_with_clearance(
            {
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": chatgpt_base,
                "referer": chatgpt_base + "/",
                "user-agent": user_agent,
                "cache-control": "no-cache",
            },
            signin_url,
            self.proxy,
            self.clearance_user_agent,
        )
        signin_resp, signin_error = request_with_local_retry(
            self.session,
            "post",
            signin_url,
            data={"callbackUrl": chatgpt_base + "/", "csrfToken": csrf_token, "json": "true"},
            headers=signin_headers,
            allow_redirects=False,
            verify=proxy_settings.verify_tls(),
        )
        if signin_resp is None:
            raise RuntimeError(signin_error or "free_wu_signin_request_failed")
        redirect_url = _nextauth_signin_url(signin_resp)
        if not redirect_url:
            raise RuntimeError(f"free_wu_signin_missing_redirect_http_{signin_resp.status_code}, {_response_debug_detail(signin_resp)}")
        final_resp = self._free_wu_follow_redirects(redirect_url, index, referer=chatgpt_base + "/")
        device_id = _session_cookie_value(self.session, "oai-did")
        if device_id:
            self.device_id = device_id
            try:
                self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
                self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
            except Exception:
                pass
        step(index, f"Free：初始化完成，落点 HTTP {getattr(final_resp, 'status_code', 0)}，device={self.device_id[:12]}...")

    def _free_wu_create_account(self, name: str, birthdate: str, index: int) -> dict:
        step(index, "Free：提交 about-you 资料")
        url = f"{auth_base}/api/accounts/create_account"
        headers = self._free_wu_json_headers(f"{auth_base}/about-you")
        headers["accept"] = "application/json"
        headers["content-type"] = "application/json"
        sentinel_headers = build_sentinel_request_headers(self.session, self.device_id, "oauth_create_account")
        headers.update(sentinel_headers)
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=proxy_settings.verify_tls())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(url, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._free_wu_json_headers(f"{auth_base}/about-you")
            headers["accept"] = "application/json"
            headers["content-type"] = "application/json"
            headers.update(build_sentinel_request_headers(self.session, self.device_id, "oauth_create_account"))
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=proxy_settings.verify_tls())
        if resp is None or resp.status_code not in (200, 201, 302):
            raise RuntimeError(error or f"free_wu_create_account_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp)}")
        data = _response_json(resp)
        if isinstance(data.get("error"), dict):
            raise RuntimeError(f"free_wu_create_account_error_{data['error'].get('code') or 'unknown'}: {data['error'].get('message') or ''}")
        return data

    def _free_wu_complete_chatgpt_session(self, continue_url: str, index: int) -> dict:
        if continue_url:
            step(index, "Free：访问 ChatGPT OAuth callback")
            self._free_wu_follow_redirects(continue_url, index, referer=f"{auth_base}/about-you")
        session_url = f"{chatgpt_base}/api/auth/session"
        headers = _headers_with_clearance(
            {
                "accept": "application/json",
                "referer": chatgpt_base + "/",
                "user-agent": user_agent,
                "cache-control": "no-cache",
            },
            session_url,
            self.proxy,
            self.clearance_user_agent,
        )
        resp, error = request_with_local_retry(self.session, "get", session_url, headers=headers, verify=proxy_settings.verify_tls())
        if resp is None or resp.status_code != 200:
            raise RuntimeError(error or f"free_wu_session_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp)}")
        data = _response_json(resp)
        access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError(f"free_wu_session_missing_access_token, {_response_debug_detail(resp)}")
        return data

    def register_free_wu(self, index: int) -> dict:
        step(index, "Free 模式启动：使用 chatgpt.com NextAuth 注册协议")
        mailbox = create_mailbox(register_proxy=self.proxy, register_mode="free_wu")
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise RuntimeError("邮箱缺少 address")
        label = str(mailbox.get("label") or "")
        inbox_address = str(mailbox.get("inbox_address") or "").strip()
        alias_note = f" -> 收件箱 {inbox_address}" if inbox_address and inbox_address.lower() != email.lower() else ""
        step(index, f"邮箱创建完成[{label}]: {email}{alias_note}")
        self.current_mailbox = mailbox
        password = _random_password()
        first_name, last_name = _random_name()
        name = f"{first_name} {last_name}"
        birthdate = _random_birthdate()
        self.email = email
        self.password = password
        try:
            self._free_wu_init_page_email(email, index)
            step(index, "Free：等待邮箱验证码")
            code = wait_for_code(mailbox, register_proxy=self.proxy, register_mode="free_wu")
            if not code:
                raise RuntimeError("free_wu_email_otp_timeout")
            validate_data = self._validate_otp(code, index)
            continue_url = str(validate_data.get("continue_url") or "").strip()
            if continue_url:
                self._free_wu_follow_redirects(continue_url, index, referer=f"{auth_base}/email-verification")
            create_result = self._free_wu_create_account(name, birthdate, index)
            session_data = self._free_wu_complete_chatgpt_session(str(create_result.get("continue_url") or ""), index)
            account = session_data.get("account") if isinstance(session_data.get("account"), dict) else {}
            token_expires_at = _parse_session_expires(session_data.get("expires"))
            registered_at = datetime.now(timezone.utc).isoformat()
            result = {
                "email": email,
                "password": password,
                "access_token": str(session_data.get("accessToken") or session_data.get("access_token") or "").strip(),
                "refresh_token": "",
                "id_token": "",
                "session_token": str(session_data.get("sessionToken") or session_data.get("session_token") or _session_cookie_value(self.session, "__Secure-next-auth.session-token") or _session_cookie_value(self.session, "next-auth.session-token") or "").strip(),
                "source_type": "web",
                "register_mode": "free",
                "product_mode": str(config.get("product_mode") or "partial"),
                "mailbox_group": _mailbox_group(mailbox),
                "provider": str(mailbox.get("provider") or ""),
                "provider_label": str(mailbox.get("label") or mailbox.get("provider_ref") or ""),
                "inbox_address": inbox_address,
                "oauth_status": "chatgpt_nextauth_session",
                "token_source": "chatgpt_nextauth",
                "client_id": "chatgpt-nextauth",
                "chatgpt_account_id": str(account.get("id") or account.get("account_id") or account.get("accountId") or "").strip(),
                "chatgpt_user_id": str((session_data.get("user") if isinstance(session_data.get("user"), dict) else {}).get("id") or "").strip(),
                "plan_type": str(account.get("planType") or account.get("plan_type") or "free").strip().lower() or "free",
                "account_type": "free",
                "token_expires_at": token_expires_at,
                "token_expires_in": max(0, token_expires_at - int(time.time())) if token_expires_at else None,
                "health_status": "unknown",
                "web_login_ok": True,
                "created_at": registered_at,
                "registered_at": registered_at,
                "free_wu_session": {
                    "expires": str(session_data.get("expires") or ""),
                    "account_id": str(account.get("id") or account.get("account_id") or account.get("accountId") or ""),
                },
            }
            mail_provider.mark_mailbox_result(mailbox, success=True)
            step(index, "Free：ChatGPT Web session 获取完成")
            return result
        except Exception as error:
            self._record_partial(self.partial_stage or "free_wu_failed", "free_wu_failed", error=error, mailbox=mailbox)
            mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            raise

    def send_post_registration_hi(self, index: int, account: dict[str, Any]) -> dict[str, Any]:
        access_token = str(account.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("pre_oauth_conversation_missing_access_token")

        account.update(
            {
                "chat_test_tried": True,
                "chat_test_ok": False,
                "chat_test_method": "conversation_pre_oauth",
                "chat_test_error": "",
            }
        )
        step(index, "成品模式：OAuth 前发送一次 ChatGPT 网页对话 Hi")
        url = f"{chatgpt_base}/backend-api/conversation"
        body = {
            "action": "next",
            "messages": [
                {
                    "id": str(uuid.uuid4()),
                    "role": "user",
                    "content": {"content_type": "text", "parts": ["Hi"]},
                }
            ],
            "conversation_id": None,
            "parent_message_id": str(uuid.uuid4()),
            "model": "auto",
            "timezone_offset_min": -480,
            "history_and_training_disabled": True,
            "conversation_mode": {"kind": "primary_assistant"},
        }
        headers = {
            "accept": "text/event-stream",
            "accept-language": "en-US,en;q=0.9",
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "origin": chatgpt_base,
            "referer": f"{chatgpt_base}/",
            "oai-device-id": self.device_id,
            "sec-ch-ua": sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": user_agent,
        }
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(
            self.session,
            "post",
            url,
            json=body,
            headers=headers,
            retry_attempts=1,
            verify=proxy_settings.verify_tls(),
        )
        if resp is None:
            account["chat_test_error"] = str(error or "conversation request failed")[:500]
            raise RuntimeError(f"pre_oauth_conversation_request_failed: {account['chat_test_error']}")

        status_code = int(getattr(resp, "status_code", 0) or 0)
        data = _response_json(resp)
        response_error = data.get("error") if isinstance(data.get("error"), dict) else {}
        if not 200 <= status_code < 300 or response_error:
            detail = str(response_error.get("code") or response_error.get("message") or _response_debug_detail(resp, 500))
            account.update({"chat_test_status_code": status_code, "chat_test_error": detail[:500]})
            raise RuntimeError(f"pre_oauth_conversation_http_{status_code}: {detail[:500]}")

        verified_at = datetime.now(timezone.utc).isoformat()
        account.update(
            {
                "chat_test_ok": True,
                "chat_test_status_code": status_code,
                "chat_test_error": "",
                "conversation_verified_at": verified_at,
            }
        )
        step(index, f"成品模式：网页对话通过，HTTP {status_code}，继续获取 sub2api OAuth", "green")
        return account

    def _register_user(self, email: str, password: str, index: int, referer_path: str = "/create-account/password") -> dict:
        step(index, "提交注册信息 (密码: " + password[:4] + "****)")
        url = f"{auth_base}/api/accounts/user/register"
        referer = f"{auth_base}{referer_path if referer_path.startswith('/') else '/' + referer_path}"
        headers = self._json_headers(referer)
        step(index, "正在计算密码阶段风控算力挑战（Sentinel PoW）...")
        sentinel = build_sentinel_token(self.session, self.device_id, "username_password_create")
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
            step(index, "密码阶段风控算力挑战通过")
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=proxy_settings.verify_tls())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(referer)
            sentinel = build_sentinel_token(self.session, self.device_id, "username_password_create")
            if sentinel:
                headers["openai-sentinel-token"] = sentinel
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"username": email, "password": password}, headers=headers, verify=proxy_settings.verify_tls())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            data = _response_json(resp) if resp is not None else {}
            err_code = data.get("error", {}).get("code", "") if isinstance(data.get("error"), dict) else ""
            err_msg = data.get("error", {}).get("message", "") if isinstance(data.get("error"), dict) else ""
            if err_code == "account_creation_failed" or "Failed to create account" in err_msg:
                step(index, "注册失败: 账号创建被拦截 (IP/域名可能已黑)", "yellow")
                return {"status": "blocked", "error": "account_creation_failed"}
            detail = f", detail={json.dumps(data, ensure_ascii=False)[:200]}" if data else ""
            raise RuntimeError(error or f"user_register_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "密码设置成功")
        self._record_partial("password_set", "password_set")
        return {"status": "ok", "data": _response_json(resp)}

    def _send_otp(self, index: int, referer_path: str = "/create-account/password") -> None:
        step(index, "正在向邮箱主动请求发送验证码...")
        url = f"{auth_base}/api/accounts/email-otp/send"
        referer = f"{auth_base}{referer_path if referer_path.startswith('/') else '/' + referer_path}"
        headers = self._json_headers(referer)
        sentinel = build_sentinel_token(self.session, self.device_id, "authorize_continue")
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={}, headers=headers, verify=proxy_settings.verify_tls())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(referer)
            sentinel = build_sentinel_token(self.session, self.device_id, "authorize_continue")
            if sentinel:
                headers["openai-sentinel-token"] = sentinel
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={}, headers=headers, verify=proxy_settings.verify_tls())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 201, 204):
            detail = f", {_response_debug_detail(resp)}" if resp is not None else ""
            raise RuntimeError(error or f"send_otp_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "验证码发送请求已受理")

    def _validate_otp(self, code: str, index: int) -> dict:
        step(index, "已获取邮箱验证码，正在提交核验...")
        resp, error = validate_otp(self.session, self.device_id, code)
        if resp is None or resp.status_code != 200:
            body = ""
            try:
                body = (resp.text or "")[:500] if resp is not None else ""
            except Exception:
                pass
            raise RuntimeError(error or f"validate_otp_http_{getattr(resp, 'status_code', 'unknown')}_body={body}")
        data = _response_json(resp)
        auth_session = str(data.get("oai-client-auth-session") or getattr(resp, "headers", {}).get("oai-client-auth-session") or "").strip()
        if auth_session:
            self.client_auth_session = auth_session
        fields = ",".join(data.keys()) if isinstance(data, dict) else ""
        step(index, f"OTP 校验通过{('，返回字段: ' + fields) if fields else ''}")
        self._record_partial("email_verified", "email_verified", extra={"continue_url": str(data.get("continue_url") or "")})
        return data

    def _send_passwordless_otp(self, index: int) -> None:
        step(index, "无密码通道注册发信...")
        url = f"{auth_base}/api/accounts/passwordless/send-otp"
        headers = self._json_headers(f"{auth_base}/log-in/password")
        sentinel = build_sentinel_token(self.session, self.device_id, "authorize_continue")
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={}, headers=headers, verify=proxy_settings.verify_tls())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/log-in/password")
            sentinel = build_sentinel_token(self.session, self.device_id, "authorize_continue")
            if sentinel:
                headers["openai-sentinel-token"] = sentinel
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={}, headers=headers, verify=proxy_settings.verify_tls())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code != 200:
            detail = f", {_response_debug_detail(resp)}" if resp is not None else ""
            raise RuntimeError(error or f"passwordless_send_otp_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        step(index, "无密码通道邮件发送成功")

    def _navigate_after_otp(self, validate_data: dict, index: int) -> str:
        candidates: list[str] = []
        if isinstance(validate_data, dict):
            for key in ("continue_url", "redirect_url", "redirect_to", "url", "next_url"):
                value = str(validate_data.get(key) or "").strip()
                if value:
                    candidates.append(value)
        candidates.extend([
            f"{auth_base}/create-account/password",
            f"{auth_base}/create-account",
        ])
        seen: set[str] = set()
        last_error = ""
        for raw_url in candidates:
            url = raw_url if raw_url.startswith("http") else f"{auth_base}{raw_url if raw_url.startswith('/') else '/' + raw_url}"
            if url in seen:
                continue
            seen.add(url)
            callback_params = extract_oauth_callback_params_from_url(url)
            if callback_params:
                self.platform_auth_code = str(callback_params.get("code") or "").strip()
                step(index, "验证码后跳转获取 OAuth callback code")
                return "oauth_callback"
            headers = _headers_with_clearance(self._navigate_headers(f"{auth_base}/email-verification"), url, self.proxy, self.clearance_user_agent)
            resp, flag = self._follow_to_landing(url, headers, index)
            if resp is None:
                last_error = flag
                continue
            if flag == "callback_redirect":
                loc = str(resp.headers.get("Location", "") or "")
                cp = extract_oauth_callback_params_from_url(loc)
                if cp:
                    self.platform_auth_code = str(cp.get("code") or "").strip()
                    step(index, "验证码后跳转获取 OAuth callback")
                    return "oauth_callback"
            if resp.status_code in (200, 302):
                final_url = str(getattr(resp, "url", "") or "")
                callback_params = extract_oauth_callback_params_from_url(final_url)
                if callback_params:
                    self.platform_auth_code = str(callback_params.get("code") or "").strip()
                    step(index, "验证码后跳转获取 OAuth callback")
                    return "oauth_callback"
                landed = _authorize_landed_page(resp)
                step(index, f"验证码后跳转完成[{landed or '?'}] url={final_url[:160]}")
                return landed
            last_error = f"navigate_after_otp_http_{getattr(resp, 'status_code', 'unknown')}, {_response_debug_detail(resp)}"
        raise RuntimeError(last_error or "验证码后跳转失败")

    def _create_account(self, name: str, birthdate: str, index: int) -> str:
        step(index, "正在提交账号资料...")
        url = f"{auth_base}/api/accounts/create_account"
        # 对齐 HAR: create_account 只需要极简 headers
        headers = self._json_headers(f"{auth_base}/about-you")
        headers["accept"] = "application/json"
        headers["content-type"] = "application/json"
        headers.update(build_sentinel_request_headers(self.session, self.device_id, "oauth_create_account"))
        headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
        resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=proxy_settings.verify_tls())
        if _is_cloudflare_challenge(resp):
            bundle = self._refresh_cloudflare_clearance(auth_base, index)
            if bundle is None:
                raise RuntimeError(_cloudflare_block_message(resp, reason=self.clearance_failure_reason))
            headers = self._json_headers(f"{auth_base}/about-you")
            headers["accept"] = "application/json"
            headers["content-type"] = "application/json"
            headers.update(build_sentinel_request_headers(self.session, self.device_id, "oauth_create_account"))
            headers = _headers_with_clearance(headers, url, self.proxy, self.clearance_user_agent)
            resp, error = request_with_local_retry(self.session, "post", url, json={"name": name, "birthdate": birthdate}, headers=headers, verify=proxy_settings.verify_tls())
            if _is_cloudflare_challenge(resp):
                raise RuntimeError(_cloudflare_block_message(resp, "Cloudflare clearance 重试仍被拦截"))
        if resp is None or resp.status_code not in (200, 302):
            data = _response_json(resp) if resp is not None else {}
            if data.get("message") == "Failed to create account. Please try again.":
                step(index, "账号资料提交失败：邮箱域名或账号可能被限制", "yellow")
            detail = f", detail={json.dumps(data, ensure_ascii=False)[:200]}" if data else ""
            raise RuntimeError(error or f"create_account_http_{getattr(resp, 'status_code', 'unknown')}{detail}")
        data = _response_json(resp)
        continue_url = str(data.get("continue_url") or "").strip()
        callback_params = extract_oauth_callback_params_from_url(continue_url)
        self.platform_auth_code = str((callback_params or {}).get("code") or "").strip()
        if continue_url:
            step(index, f"账号资料提交完成")
            self._record_partial("profile_created", "profile_created", extra={"name": name, "birthdate": birthdate, "continue_url": continue_url})
            if self.platform_auth_code:
                return "oauth_callback"
            return self._follow_flow_url(continue_url, f"{auth_base}/about-you", index, "账号资料后跳转")
        else:
            step(index, "账号资料提交完成，但未返回 continue_url")
            self._record_partial("profile_created", "profile_created", extra={"name": name, "birthdate": birthdate})
        return "oauth_callback" if self.platform_auth_code else ""

    def _exchange_registered_tokens(self, index: int) -> dict:
        step(index, "开始换 token")
        tokens = request_platform_oauth_token(self.session, self.platform_auth_code, self.code_verifier)
        if not tokens:
            raise RuntimeError("token换取失败")
        step(index, "token 换取完成")
        return tokens

    def _wait_for_valid_otp(
        self,
        mailbox: dict,
        index: int,
        attempts: int = 3,
        resend_referer_path: str = "/email-verification",
    ) -> dict:
        last_wrong: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            step(index, "正在等待邮箱验证码..." if attempt == 1 else f"未收到验证码，准备重试 {attempt}/{attempts}...")
            code = wait_for_code(mailbox, register_proxy=self.proxy)
            if not code:
                break
            step(index, f"获取到验证码 {code}")
            try:
                return self._validate_otp(code, index)
            except RuntimeError as error:
                if "wrong_email_otp_code" in str(error) or "Wrong code" in str(error):
                    last_wrong = error
                    step(index, f"验证码 {code} 被 OpenAI 判定错误，准备请求新的验证码并重试", "yellow")
                    if attempt < max(1, attempts):
                        mailbox["_code_not_before"] = datetime.now(timezone.utc)
                        try:
                            self._send_otp(index, referer_path=resend_referer_path)
                        except Exception as resend_error:
                            step(index, f"验证码错误后重新发送失败：{resend_error}", "yellow")
                    continue
                raise
        if last_wrong is not None:
            raise last_wrong
        raise RuntimeError("验证码重试次数达上限，丢弃当前邮箱")

    def _test_chatgpt_web(self, index: int, mailbox: dict | None = None, mail_config: dict | None = None) -> dict:
        result: dict = {
            "chat_test_tried": True,
            "chat_test_ok": False,
            "chat_test_method": "",
            "chat_test_error": "",
        }
        client = ChatGPTWebClient(email=self.email, password=self.password, proxy=self.proxy, mailbox=mailbox, mail_config=mail_config, log=lambda msg, level="info": step(index, msg, level))
        try:
            step(index, "登录 chatgpt.com 进行对话测试")
            login_data = client.login()
            token = str(login_data.get("access_token") or "").strip()
            if not token:
                result["chat_test_error"] = "登录后未获取到 access_token"
                return result
            result["chat_test_method"] = "conversation"
            conv = client.send_test_conversation("Hi")
            if conv.get("ok"):
                result["chat_test_ok"] = True
                result["access_token"] = token
                result["token_source"] = "chatgpt_web"
                step(index, "ChatGPT Web 对话测试通过")
            else:
                step(index, f"对话接口返回 {conv.get('status_code')}，降级到 backend-api/me 健康检查", "yellow")
                result["chat_test_method"] = "me_check"
                me = client.check_backend_me(token)
                if me.get("ok"):
                    result["chat_test_ok"] = True
                    result["access_token"] = token
                    result["token_source"] = "chatgpt_web"
                    step(index, "ChatGPT backend-api/me 健康检查通过")
                else:
                    result["chat_test_error"] = f"对话({conv.get('status_code')})/me接口({me.get('status_code')})均失败"
                    step(index, result["chat_test_error"], "yellow")
        except Exception as e:
            result["chat_test_error"] = str(e)[:300]
            step(index, f"ChatGPT Web 测试异常: {e}", "yellow")
        finally:
            client.close()
        return result

    def register(self, index: int) -> dict:
        step(index, "开始创建邮箱")
        mailbox = create_mailbox(register_proxy=self.proxy)
        email = str(mailbox.get("address") or "").strip()
        if not email:
            mail_provider.release_mailbox(mailbox)
            raise RuntimeError("邮箱缺少 address")
        label = str(mailbox.get("label") or "")
        inbox_address = str(mailbox.get("inbox_address") or "").strip()
        alias_note = f" -> 收件箱 {inbox_address}" if inbox_address and inbox_address.lower() != email.lower() else ""
        step(index, f"邮箱创建完成[{label}]: {email}{alias_note}")
        self.current_mailbox = mailbox
        try:
            password = _random_password()
            self.email = email
            self.password = password
            first_name, last_name = _random_name()
            profile_created = False
            is_takeover = False
            landed = self._platform_authorize(email, index)
            if landed == "oauth_callback":
                pass
            elif landed == "login":
                is_takeover = True
                step(index, "该邮箱无需密码注册，准备走无密码通道进行接管")
                self._send_passwordless_otp(index)
                mailbox["_code_not_before"] = datetime.now(timezone.utc)
                code = wait_for_code(mailbox, register_proxy=self.proxy)
                if not code:
                    raise RuntimeError("无密码通道验证码超时")
                validate_data = self._validate_otp(code, index)
                password = "Takeover_NoPassword"
                self.password = password
                landed_after = self._navigate_after_otp(validate_data, index)
                if landed_after == "about_you":
                    self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
                    profile_created = True
                elif landed_after == "oauth_callback":
                    profile_created = True
                elif landed_after == "add_phone":
                    self._record_partial("add_phone", "add_phone", error="OpenAI/Auth0 要求添加手机号", extra={"landing": "add_phone"})
                    raise RuntimeError("phone_required_add_phone: OpenAI/Auth0 要求添加手机号")
            elif landed == "about_you":
                self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
                profile_created = True
            elif landed == "email_verification":
                mailbox["_code_not_before"] = datetime.now(timezone.utc)
                self._send_otp(index, referer_path="/email-verification")
                validate_data = self._wait_for_valid_otp(mailbox, index, resend_referer_path="/email-verification")
                landed_after = self._navigate_after_otp(validate_data, index)
                if landed_after == "oauth_callback":
                    profile_created = True
                elif landed_after == "add_phone":
                    self._record_partial("add_phone", "add_phone", error="OpenAI/Auth0 要求添加手机号", extra={"landing": "add_phone"})
                    raise RuntimeError("phone_required_add_phone: OpenAI/Auth0 要求添加手机号")
                else:
                    if landed_after != "about_you":
                        step(index, f"OTP 后落点不是 about-you，而是 {landed_after or '?'}，仍尝试提交资料", "yellow")
                    self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
                    profile_created = True
            else:
                # 密码流程兼容两种 Auth0 状态：
                # 1) 部分会话可直接发 OTP；
                # 2) 当前多数会话需要先提交密码，再允许 email-otp/send。
                mailbox["_code_not_before"] = datetime.now(timezone.utc)
                try:
                    self._send_otp(index, referer_path="/create-account/password")
                except RuntimeError as error:
                    error_text = str(error)
                    should_fallback = (
                        "send_otp_http_400" in error_text
                        or "invalid_auth_step" in error_text
                        or "Invalid authorization step" in error_text
                        or "authorization step" in error_text
                    )
                    if not should_fallback:
                        raise
                    step(index, "验证码发送被拒，切换为：先提交密码，再重新发送验证码", "yellow")
                    register_result = self._register_user(email, password, index, referer_path="/create-account/password")
                    if register_result.get("status") == "blocked":
                        raise RuntimeError(str(register_result.get("error") or "account_creation_failed"))
                    mailbox["_code_not_before"] = datetime.now(timezone.utc)
                    self._send_otp(index, referer_path="/create-account/password")
                validate_data = self._wait_for_valid_otp(mailbox, index, resend_referer_path="/create-account/password")
                landed_after = self._navigate_after_otp(validate_data, index)
                if landed_after == "oauth_callback":
                    profile_created = True
                elif landed_after == "add_phone":
                    self._record_partial("add_phone", "add_phone", error="OpenAI/Auth0 要求添加手机号", extra={"landing": "add_phone"})
                    raise RuntimeError("phone_required_add_phone: OpenAI/Auth0 要求添加手机号")
                else:
                    if landed_after != "about_you":
                        step(index, f"OTP 后落点不是 about-you，而是 {landed_after or '?'}，仍尝试提交资料", "yellow")
                    self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
                    profile_created = True
            if not self.platform_auth_code and not profile_created:
                self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
            chat_test_result = self._test_chatgpt_web(index, mailbox=mailbox, mail_config=_mail_config(self.proxy))
            self.web_login_result = dict(chat_test_result or {})
            self.web_login_ok = bool(chat_test_result.get("chat_test_ok"))
            if not self.platform_auth_code:
                step(index, "注册完成，重新 Auth0 入口取 OAuth code")
                retry_landed = self._platform_authorize(email, index)
                if retry_landed == "about_you":
                    self._create_account(f"{first_name} {last_name}", _random_birthdate(), index)
                elif retry_landed == "login":
                    raise RuntimeError("auth_flow_login_existing_account: authorize retry landed on login")
            if not self.platform_auth_code:
                raise RuntimeError("oauth_callback_missing_after_register_flow")
            try:
                tokens = self._exchange_registered_tokens(index)
            except Exception as token_error:
                raise
            self.oauth_completed = True
            k12_result: dict[str, Any] | None = None
            reg_mode = str(config.get("register_mode") or "free")
            if bool(config.get("k12_mode")) or reg_mode == "k12":
                try:
                    k12_result = k12_service.request_k12_join(
                        session=self.session,
                        access_token=str(tokens.get("access_token") or "").strip(),
                        email=email,
                        mailbox=mailbox,
                        mail_config=_mail_config(self.proxy),
                        index=index,
                        user_agent=user_agent,
                        log_step=step,
                        force_enabled=True,
                    )
                    step(index, f"K12 结果：{k12_service.summarize_k12_result(k12_result)}")
                except Exception as error:
                    k12_result = {"enabled": True, "status": "error", "error": str(error)}
                    step(index, f"K12 加入失败：{error}", "yellow")
        except Exception as error:
            keep_as_oauth_partial = self.web_login_ok and not self.oauth_completed
            if keep_as_oauth_partial:
                error_text = str(error)
                status = "token_exchange_failed" if "token" in error_text.lower() else "oauth_missing" if "callback" in error_text.lower() or "oauth" in error_text.lower() else "oauth_failed"
                self._record_partial(
                    "oauth_failed",
                    status,
                    error=error,
                    extra={
                        **self.web_login_result,
                        "web_login_ok": True,
                        "oauth_status": status,
                        "oauth_error": error_text[:800],
                    },
                )
            mail_provider.mark_mailbox_result(mailbox, success=keep_as_oauth_partial, error=error)
            raise
        mail_provider.mark_mailbox_result(mailbox, success=True)
        result = {
            "email": email,
            "password": password,
            "access_token": str(tokens.get("access_token") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
            "id_token": str(tokens.get("id_token") or "").strip(),
            "source_type": "web",
            "register_mode": str(config.get("register_mode") or ("k12" if config.get("k12_mode") else "free")),
            "mailbox_group": _mailbox_group(mailbox),
            "oauth_status": "ok",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if chat_test_result:
            result.update(chat_test_result)
        if k12_result:
            result.update({
                "k12_mode": True,
                "k12_team_id": str(k12_result.get("team_id") or ""),
                "workspace_id": str(k12_result.get("workspace_id") or k12_result.get("team_id") or ""),
                "k12_join_status": str(k12_result.get("status") or ""),
                "k12_join_result": k12_result,
            })
        return result



def _maybe_import_registered_account_to_warehouses(index: int, result: dict) -> None:
    register_mode = str(config.get("register_mode") or "free").strip().lower()
    product_mode = str(result.get("product_mode") or config.get("product_mode") or "partial").strip().lower()
    force_finished_import = register_mode == "free" and product_mode == "finished"
    try:
        from services import sub2api_store

        sub2api_cfg = sub2api_store.get_sub2api_config()
        legacy_import = register_mode == "k12" or bool(result.get("k12_mode"))
        if sub2api_cfg.get("enabled") and (sub2api_cfg.get("auto_import") or legacy_import or force_finished_import):
            step(index, f"{register_mode} 模式：开始导入 sub2api")
            import_result = sub2api_store.import_account_dicts_to_sub2api(result)
            account_ids = import_result.get("account_ids") if isinstance(import_result.get("account_ids"), list) else []
            updates = {
                "sub2api_imported_at": import_result.get("imported_at") or datetime.now(timezone.utc).isoformat(),
                "sub2api_import_status": "ok" if import_result.get("ok") else "failed",
            }
            if account_ids:
                updates["sub2api_account_id"] = account_ids[0]
            if not import_result.get("ok"):
                updates["sub2api_last_error"] = str(import_result.get("last_error") or import_result)[:800]
            result.update({key: value for key, value in updates.items() if value not in ("", None)})
            update_registered_account(result, updates)
            step(index, f"sub2api 导入完成：成功={import_result.get('success', 0)}，失败={import_result.get('fail', 0)}", "green" if import_result.get("ok") else "yellow")
            if import_result.get("ok") and account_ids:
                try:
                    test_result = sub2api_store.test_sub2api_account(account_ids[0])
                    healthy = bool(test_result.get("ok"))
                    health_updates = {
                        "sub2api_health_status": "ok" if healthy else "failed",
                        "sub2api_health_checked_at": test_result.get("tested_at") or datetime.now(timezone.utc).isoformat(),
                        "sub2api_health_code": int(test_result.get("status_code") or 0),
                    }
                    result.update(health_updates)
                    update_registered_account(result, health_updates)
                    step(index, f"sub2api 导入后测活：{'通过' if healthy else '失败'} / HTTP {health_updates['sub2api_health_code']}", "green" if healthy else "yellow")
                except Exception as test_error:
                    step(index, f"sub2api 导入成功，但导入后测活失败：{test_error}", "yellow")
    except Exception as error:
        updates = {
            "sub2api_import_status": "failed",
            "sub2api_last_error": str(error)[:800],
            "sub2api_imported_at": datetime.now(timezone.utc).isoformat(),
        }
        result.update(updates)
        try:
            update_registered_account(result, updates)
        except Exception:
            pass
        step(index, f"sub2api 自动导入失败：{error}", "yellow")

    try:
        from services import cpa_store

        cpa_cfg = cpa_store.get_cpa_config()
        if cpa_cfg.get("enabled") and (cpa_cfg.get("auto_import") or force_finished_import):
            step(index, f"{register_mode} 模式：开始导入 CPA 仓库")
            import_result = cpa_store.import_account_dicts_to_cpa(result)
            first = next((item for item in import_result.get("results") or [] if isinstance(item, dict)), {})
            updates = {
                "cpa_imported_at": import_result.get("imported_at") or datetime.now(timezone.utc).isoformat(),
                "cpa_import_status": "ok" if import_result.get("ok") else "failed",
                "cpa_auth_file": first.get("filename") or "",
            }
            if not import_result.get("ok"):
                updates["cpa_last_error"] = str(import_result.get("results") or import_result)[:800]
            result.update({key: value for key, value in updates.items() if value not in ("", None)})
            update_registered_account(result, updates)
            step(index, f"CPA 导入完成：成功={import_result.get('success', 0)}，失败={import_result.get('fail', 0)}", "green" if import_result.get("ok") else "yellow")
    except Exception as error:
        updates = {
            "cpa_import_status": "failed",
            "cpa_last_error": str(error)[:800],
            "cpa_imported_at": datetime.now(timezone.utc).isoformat(),
        }
        result.update(updates)
        try:
            update_registered_account(result, updates)
        except Exception:
            pass
        step(index, f"CPA 自动导入失败：{error}", "yellow")


def _save_free_partial(index: int, result: dict, *, reason: str, oauth_attempted: bool) -> dict:
    product_mode = str(config.get("product_mode") or "partial").strip().lower()
    reason_text = str(reason or "")[:800]
    payload = {
        **result,
        "register_mode": "free",
        "product_mode": product_mode,
        "status": "oauth_failed" if oauth_attempted else "oauth_missing",
        "stage": "sub2api_oauth_failed" if oauth_attempted else "registered_partial",
        "reason": reason_text,
        "oauth_status": "oauth_failed" if oauth_attempted else "not_requested",
        "oauth_error": reason_text if oauth_attempted else "",
        "web_login_ok": True,
    }
    lowered_reason = reason_text.lower()
    if oauth_attempted and any(
        marker in lowered_reason
        for marker in ("account_deactivated", "account_disabled", "account_suspended", "account_closed")
    ):
        died_at = datetime.now(timezone.utc).isoformat()
        payload.update(
            {
                "health_status": "banned",
                "health_checked_at": died_at,
                "health_method": "oauth",
                "health_error": reason_text,
                "died_at": died_at,
            }
        )
    saved = partial_account_store.upsert_partial_account(payload)
    if not saved:
        raise RuntimeError("半成品账号写入失败")
    step(index, "账号已进入半成品库存" + (f"：{reason}" if reason else ""), "yellow" if oauth_attempted else "green")
    return saved

def worker(index: int, proxy_override: str | None = None, proxy_assignment: dict | None = None) -> dict:
    start = time.time()
    assignment = proxy_assignment if isinstance(proxy_assignment, dict) else {}
    active_proxy = str(proxy_override if proxy_override is not None else config.get("proxy") or "").strip()
    registrar = PlatformRegistrar(active_proxy)
    try:
        step(index, "任务启动")
        if assignment.get("source") not in {"", None, "fallback"}:
            label = assignment.get("label") or assignment.get("node_id") or "代理节点"
            slot = assignment.get("slot") or "-"
            step(index, f"使用代理节点：线程{slot} / {label}", "yellow")
            region = str(assignment.get("region") or "未知")
            latency = int(assignment.get("probe_latency_ms") or assignment.get("latency_ms") or 0)
            openai_status = int(assignment.get("openai_status") or 0)
            status_text = f" / OpenAI HTTP {openai_status}" if openai_status else ""
            step(index, f"节点预检通过：地区 {region} / 延迟 {latency}ms{status_text}", "yellow")
        register_mode = str(config.get("register_mode") or "free").strip().lower()
        if register_mode == "free_wu":
            register_mode = "free"
        if register_mode == "free":
            result = registrar.register_free_wu(index)
        else:
            result = registrar.register(index)
        cost = time.time() - start
        if active_proxy:
            result["proxy"] = active_proxy
        registration_proxy = proxy_pool_store.registration_proxy_record(assignment, proxy=active_proxy)
        if registration_proxy:
            result.update(
                {
                    "registration_proxy": registration_proxy,
                    "registration_proxy_source": str(registration_proxy.get("source") or ""),
                    "registration_proxy_node_id": str(registration_proxy.get("node_id") or ""),
                    "registration_proxy_label": str(registration_proxy.get("label") or registration_proxy.get("node_name") or ""),
                }
            )
        product_mode = str(config.get("product_mode") or "partial").strip().lower()
        if register_mode == "free" and product_mode == "partial":
            partial = _save_free_partial(index, result, reason="半成品模式未请求 sub2api OAuth", oauth_attempted=False)
            with stats_lock:
                stats["done"] += 1
                stats["success"] += 1
                avg = (time.time() - stats["start_time"]) / stats["success"]
            log(f'[SUCCESS] 半成品账号已保存: {result["email"]}，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
            return {"ok": True, "index": index, "result": partial, "storage": "partial", "proxy_assignment": assignment}
        if register_mode == "free" and product_mode == "finished":
            try:
                result = registrar.send_post_registration_hi(index, result)
            except Exception as conversation_error:
                partial = _save_free_partial(
                    index,
                    result,
                    reason=f"OAuth 前网页对话失败：{conversation_error}",
                    oauth_attempted=False,
                )
                with stats_lock:
                    stats["done"] += 1
                    stats["success"] += 1
                    avg = (time.time() - stats["start_time"]) / stats["success"]
                log(
                    f'[WARNING] 注册成功但 OAuth 前网页对话失败，已转半成品: {result["email"]}，原因: {conversation_error}',
                    "yellow",
                )
                return {
                    "ok": True,
                    "index": index,
                    "result": partial,
                    "storage": "partial",
                    "conversation_failed": True,
                    "proxy_assignment": assignment,
                }
            try:
                result = registrar.acquire_sub2api_oauth(index, result)
            except Exception as oauth_error:
                partial = _save_free_partial(index, result, reason=str(oauth_error), oauth_attempted=True)
                with stats_lock:
                    stats["done"] += 1
                    stats["success"] += 1
                    avg = (time.time() - stats["start_time"]) / stats["success"]
                log(f'[WARNING] 注册成功但 sub2api OAuth 失败，已转半成品: {result["email"]}，原因: {oauth_error}', "yellow")
                return {"ok": True, "index": index, "result": partial, "storage": "partial", "oauth_failed": True, "proxy_assignment": assignment}
        add_registered_account(result)
        try:
            partial_account_store.delete_partial_account(result.get("email"))
        except Exception:
            pass
        step(index, "账号已保存到成品账号库存")
        _maybe_import_registered_account_to_warehouses(index, result)
        with stats_lock:
            stats["done"] += 1
            stats["success"] += 1
            avg = (time.time() - stats["start_time"]) / stats["success"]
        log(f'[SUCCESS] 账号密码与 Token 已安全存入: {result["email"]}，本次耗时{cost:.1f}s，全局平均每个号注册耗时{avg:.1f}s', "green")
        return {"ok": True, "index": index, "result": result, "proxy_assignment": assignment}
    except Exception as e:
        cost = time.time() - start
        with stats_lock:
            stats["done"] += 1
            stats["fail"] += 1
        error_msg = str(e)
        if "phone_required" in error_msg or "add-phone" in error_msg:
            log(f"[WARNING] 任务{index} 注册失败，本次耗时{cost:.1f}s，原因: OpenAI 要求添加手机号", "yellow")
        elif "account_creation_failed" in error_msg:
            log(f"[WARNING] 任务{index} 注册失败，本次耗时{cost:.1f}s，原因: 账号创建被拦截 (IP/域名可能已黑)", "yellow")
        elif "Cloudflare" in error_msg:
            log(f"[WARNING] 任务{index} 注册失败，本次耗时{cost:.1f}s，原因: Cloudflare 拦截", "yellow")
        elif "TLS connect error" in error_msg or "curl: (35)" in error_msg or "Connection closed abruptly" in error_msg:
            log(f"[WARNING] 任务{index} 注册失败，本次耗时{cost:.1f}s，原因: 代理/TLS 连接失败", "yellow")
        elif "403" in error_msg:
            log(f"[WARNING] 任务{index} 注册失败，本次耗时{cost:.1f}s，原因: 403 频率限制，稍作等待后重试", "yellow")
        else:
            log(f"[ERROR] 任务{index} 注册失败，本次耗时{cost:.1f}s，原因: {e}", "red")
        return {"ok": False, "index": index, "error": str(e), "proxy_assignment": assignment}
    finally:
        registrar.close()
