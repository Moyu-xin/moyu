from __future__ import annotations

import base64
import json
import random
import secrets
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import unquote, urlencode, urlparse

from curl_cffi import requests

from services.proxy_service import proxy_settings
from services.register import mail_provider
from utils.sentinel import build_sentinel_headers as _build_sentinel_headers_bundle

AUTH_BASE = "https://auth.openai.com"
CHATGPT_BASE = "https://chatgpt.com"

AUTH_AUTHORIZE_CONTINUE_URL = f"{AUTH_BASE}/api/accounts/authorize/continue"
AUTH_PASSWORD_VERIFY_URL = f"{AUTH_BASE}/api/accounts/password/verify"
AUTH_PASSWORDLESS_SEND_URL = f"{AUTH_BASE}/api/accounts/passwordless/send-otp"
AUTH_EMAIL_OTP_VALIDATE_URL = f"{AUTH_BASE}/api/accounts/email-otp/validate"
AUTH_EMAIL_OTP_SEND_URL = f"{AUTH_BASE}/api/accounts/email-otp/send"
AUTH_CREATE_ACCOUNT_URL = f"{AUTH_BASE}/api/accounts/create_account"
AUTH_WORKSPACE_URL = f"{AUTH_BASE}/workspace"
AUTH_WORKSPACE_SELECT_URL = f"{AUTH_BASE}/api/accounts/workspace/select"
AUTH_CREATE_ACCOUNT_PASSWORD_URL = f"{AUTH_BASE}/create-account/password"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
SEC_CH_UA_FULL = '"Chromium";v="145.0.0.0", "Not:A-Brand";v="99.0.0.0", "Google Chrome";v="145.0.0.0"'
CHATGPT_WEB_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview(value: object, keep: int = 8) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}...{text[-keep:]}"


def _jwt_payload(token: object) -> dict[str, Any]:
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def token_context(token: object) -> dict[str, Any]:
    payload = _jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    profile = payload.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    exp = 0
    try:
        exp = int(payload.get("exp") or 0)
    except Exception:
        exp = 0
    return {
        "email": str(profile.get("email") or payload.get("email") or ""),
        "chatgpt_account_id": str(auth.get("chatgpt_account_id") or auth.get("account_id") or ""),
        "chatgpt_user_id": str(auth.get("user_id") or payload.get("sub") or ""),
        "plan_type": str(auth.get("chatgpt_plan_type") or profile.get("plan_type") or ""),
        "client_id": str(payload.get("client_id") or CHATGPT_WEB_CLIENT_ID),
        "exp": exp or None,
        "expires_in": max(0, exp - int(time.time())) if exp else None,
        "preview": _preview(token),
    }


def is_token_for_workspace(token: object, workspace_id: str) -> bool:
    return bool(str(token_context(token).get("chatgpt_account_id") or "").lower() == str(workspace_id or "").strip().lower())


def _decode_b64_json(encoded: str) -> dict[str, Any]:
    padded = str(encoded or "").replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    data = json.loads(base64.b64decode(padded.encode("ascii")).decode("utf-8"))
    return data if isinstance(data, dict) else {}


def _safe_response_detail(resp: Any, limit: int = 600) -> str:
    if resp is None:
        return "no_response"
    status = getattr(resp, "status_code", "unknown")
    url = str(getattr(resp, "url", "") or "")[:240]
    try:
        data = resp.json()
        body = json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        body = str(getattr(resp, "text", "") or "")[:limit]
    return f"HTTP {status}, url={url}, body={body}"


def _is_cloudflare_challenge(resp: Any) -> bool:
    if resp is None:
        return False
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        status = 0
    if status not in (403, 503):
        return False
    text = str(getattr(resp, "text", "") or "").lower()
    return (
        "<title>just a moment" in text
        or "cf-chl-" in text
        or "__cf_chl_" in text
        or "cloudflare" in text and "challenge" in text
    )


def _resolve_url(value: object, base: str = AUTH_BASE) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return f"{base}{text if text.startswith('/') else '/' + text}"


def _classify_url(url: object) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    try:
        parsed = urlparse(lowered)
        path = parsed.path or ""
    except Exception:
        path = lowered
    if lowered.startswith(f"{CHATGPT_BASE}/api/auth/callback/openai"):
        return "chatgpt_callback"
    if "code=" in lowered and "callback" in lowered:
        return "chatgpt_callback"
    if path in {"/log-in/password", "/login/password"} or path.endswith("/log-in/password"):
        return "login_password"
    if path in {"/log-in", "/login"} or path.endswith("/log-in"):
        return "login"
    if path == "/email-verification" or path.endswith("/email-verification"):
        return "email_verification"
    if path == "/about-you" or path.endswith("/about-you"):
        return "about_you"
    if path == "/add-phone" or path.endswith("/add-phone"):
        return "add_phone"
    if path == "/add-email" or path.endswith("/add-email"):
        return "add_email"
    if path == "/workspace" or path.endswith("/workspace"):
        return "workspace"
    if path.endswith("/api/accounts/email-otp/send"):
        return "email_otp_send"
    if path.endswith("/create-account/password"):
        return "create_password"
    return ""


def _random_profile() -> dict[str, str]:
    first = random.choice(["Alex", "Chris", "Taylor", "Jordan", "Morgan", "Casey", "Riley", "Jamie"])
    last = "".join(secrets.choice(string.ascii_letters) for _ in range(random.randint(5, 8))).capitalize()
    year = random.randint(1988, 2002)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return {"name": f"{first} {last}", "birthdate": f"{year:04d}-{month:02d}-{day:02d}"}


class ChatGPTWebClient:
    def __init__(
        self,
        *,
        email: str,
        password: str = "",
        proxy: str = "",
        mailbox: dict[str, Any] | None = None,
        mail_config: dict[str, Any] | None = None,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self.email = str(email or "").strip()
        self.password = str(password or "")
        self.proxy = str(proxy or "").strip()
        self.mailbox = mailbox or {}
        self.mail_config = mail_config or {}
        self.log = log
        self.device_id = str(uuid.uuid4())
        kwargs = proxy_settings.build_session_kwargs(proxy=self.proxy, upstream=True, impersonate="chrome", verify=proxy_settings.verify_tls())
        self.session = requests.Session(**kwargs)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def emit(self, text: str, level: str = "info") -> None:
        if callable(self.log):
            self.log(text, level)

    def _headers(self, target_url: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "user-agent": USER_AGENT,
            "accept-language": "en-US,en;q=0.9",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-arch": '"x86_64"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version-list": SEC_CH_UA_FULL,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"10.0.0"',
            "sec-gpc": "1",
        }
        headers.update(extra or {})
        return proxy_settings.build_headers(headers=headers, target_url=target_url, proxy=self.proxy, upstream=True)

    def _request(self, method: str, url: str, *, headers: dict[str, str] | None = None, **kwargs):
        request_headers = self._headers(url, headers)
        resp = self.session.request(method.upper(), url, headers=request_headers, timeout=kwargs.pop("timeout", 45), verify=proxy_settings.verify_tls(), **kwargs)
        if _is_cloudflare_challenge(resp):
            raise RuntimeError(f"ChatGPT/OpenAI 被 Cloudflare 拦截: {_safe_response_detail(resp, 500)}")
        return resp

    def _cookie(self, name: str, domain: str = "") -> str:
        domains = []
        if domain:
            domains.extend([domain, f".{domain.lstrip('.')}"])
        domains.extend(["chatgpt.com", ".chatgpt.com", "auth.openai.com", ".auth.openai.com", "openai.com", ".openai.com"])
        for item in domains:
            try:
                value = self.session.cookies.get(name, domain=item)
                if value:
                    return str(value)
            except Exception:
                pass
        try:
            for cookie in getattr(self.session.cookies, "jar", []) or []:
                if getattr(cookie, "name", "") == name:
                    return str(getattr(cookie, "value", "") or "")
        except Exception:
            pass
        try:
            value = self.session.cookies.get(name)
            return str(value or "")
        except Exception:
            return ""

    def _set_oai_did(self) -> None:
        for domain in ("chatgpt.com", ".chatgpt.com", "auth.openai.com", ".auth.openai.com", "openai.com", ".openai.com"):
            try:
                self.session.cookies.set("oai-did", self.device_id, domain=domain)
            except Exception:
                pass

    def _json(self, resp: Any) -> dict[str, Any]:
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _next_url_from_response(self, resp: Any, fallback: str = "") -> str:
        data = self._json(resp)
        page = data.get("page") if isinstance(data, dict) else None
        payload = page.get("payload") if isinstance(page, dict) else None
        next_url = ""
        if isinstance(payload, dict):
            next_url = str(payload.get("url") or "")
        next_url = next_url or str(data.get("continue_url") or data.get("redirect_url") or data.get("url") or fallback or "")
        return _resolve_url(next_url, AUTH_BASE)

    def _sentinel_headers(self, flow: str) -> dict[str, str]:
        self._set_oai_did()
        bundle = _build_sentinel_headers_bundle(self.session, self.device_id, flow, user_agent=USER_AGENT, sec_ch_ua=SEC_CH_UA)
        oai_sc = str(bundle.get("oai-sc") or "")
        if oai_sc:
            try:
                self.session.cookies.set("oai-sc", oai_sc, domain=".auth.openai.com")
                self.session.cookies.set("oai-sc", oai_sc, domain="auth.openai.com")
            except Exception:
                pass
        return {key: value for key, value in bundle.items() if key.startswith("openai-sentinel-") and value}

    def boot(self) -> None:
        self.emit("初始化 ChatGPT Web 会话")
        resp = self._request(
            "GET",
            f"{CHATGPT_BASE}/",
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
            },
            allow_redirects=True,
            timeout=60,
        )
        if not (200 <= int(getattr(resp, "status_code", 0) or 0) < 400):
            raise RuntimeError(f"打开 chatgpt.com 失败: {_safe_response_detail(resp)}")
        did = self._cookie("oai-did", "chatgpt.com") or self._cookie("oai-did", "openai.com")
        if did:
            self.device_id = did
        self._set_oai_did()
        self.ensure_csrf()

    def ensure_csrf(self) -> str:
        cookie = self._cookie("__Host-next-auth.csrf-token", "chatgpt.com")
        token = unquote(cookie).split("|", 1)[0] if cookie else ""
        if token:
            return token
        resp = self._request(
            "GET",
            f"{CHATGPT_BASE}/api/auth/csrf",
            headers={
                "accept": "application/json",
                "referer": f"{CHATGPT_BASE}/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            allow_redirects=True,
        )
        data = self._json(resp)
        token = str(data.get("csrfToken") or "").strip()
        if not token:
            cookie = self._cookie("__Host-next-auth.csrf-token", "chatgpt.com")
            token = unquote(cookie).split("|", 1)[0] if cookie else ""
        if not token:
            raise RuntimeError(f"ChatGPT CSRF token 获取失败: {_safe_response_detail(resp)}")
        return token

    def open_auth_entry(self) -> str:
        csrf = self.ensure_csrf()
        self._set_oai_did()
        query = urlencode({
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "ext-passkey-client-capabilities": "0111",
            "screen_hint": "login_or_signup",
            "login_hint": self.email,
        })
        body = urlencode({"callbackUrl": f"{CHATGPT_BASE}/", "csrfToken": csrf, "json": "true"})
        url = f"{CHATGPT_BASE}/api/auth/signin/openai?{query}"
        self.emit("打开 ChatGPT Web 登录入口")
        resp = self._request(
            "POST",
            url,
            headers={
                "accept": "*/*",
                "content-type": "application/x-www-form-urlencoded",
                "origin": CHATGPT_BASE,
                "referer": f"{CHATGPT_BASE}/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            data=body,
            allow_redirects=True,
            timeout=60,
        )
        if not (200 <= int(getattr(resp, "status_code", 0) or 0) < 300):
            raise RuntimeError(f"ChatGPT 登录入口失败: {_safe_response_detail(resp)}")
        data = self._json(resp)
        authorize_url = str(data.get("url") or "").strip()
        if not authorize_url:
            raise RuntimeError(f"ChatGPT 登录入口响应缺少 url: {_safe_response_detail(resp)}")
        auth_resp = self._request(
            "GET",
            authorize_url,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "referer": f"{CHATGPT_BASE}/",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-site",
            },
            allow_redirects=True,
            timeout=60,
        )
        if not (200 <= int(getattr(auth_resp, "status_code", 0) or 0) < 400):
            raise RuntimeError(f"OpenAI authorize 打开失败: {_safe_response_detail(auth_resp)}")
        return str(getattr(auth_resp, "url", "") or "")

    def authorize_continue(self) -> str:
        self.emit("提交登录邮箱")
        sentinel_headers = self._sentinel_headers("authorize_continue")
        resp = self._request(
            "POST",
            AUTH_AUTHORIZE_CONTINUE_URL,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": AUTH_BASE,
                "referer": f"{AUTH_BASE}/log-in",
                **sentinel_headers,
                "oai-device-id": self.device_id,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            json={"username": {"kind": "email", "value": self.email}},
            allow_redirects=True,
        )
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            raise RuntimeError(f"authorize/continue 失败: {_safe_response_detail(resp)}")
        return self._next_url_from_response(resp, f"{AUTH_BASE}/email-verification")

    def send_email_otp_for_login(self, referer: str = f"{AUTH_BASE}/log-in/password") -> str:
        self.emit("发送邮箱登录验证码")
        if self.mailbox is not None:
            self.mailbox["_code_not_before"] = datetime.now(timezone.utc)
        resp = self._request(
            "POST",
            AUTH_PASSWORDLESS_SEND_URL,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": AUTH_BASE,
                "referer": referer,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            data="",
            allow_redirects=True,
        )
        if not (200 <= int(getattr(resp, "status_code", 0) or 0) < 300):
            raise RuntimeError(f"passwordless/send-otp 失败: {_safe_response_detail(resp)}")
        return self._next_url_from_response(resp, f"{AUTH_BASE}/email-verification")

    def password_verify(self) -> str:
        if not self.password:
            raise RuntimeError("当前账号需要密码登录，但库存中没有密码")
        self.emit("提交登录密码")
        sentinel_headers = self._sentinel_headers("password_verify")
        resp = self._request(
            "POST",
            AUTH_PASSWORD_VERIFY_URL,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": AUTH_BASE,
                "referer": f"{AUTH_BASE}/log-in/password",
                **sentinel_headers,
                "oai-device-id": self.device_id,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            json={"password": self.password},
            allow_redirects=True,
        )
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            raise RuntimeError(f"password/verify 失败: {_safe_response_detail(resp)}")
        return self._next_url_from_response(resp)

    def _validate_email_otp(self, code: str) -> str:
        resp = self._request(
            "POST",
            AUTH_EMAIL_OTP_VALIDATE_URL,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": AUTH_BASE,
                "referer": f"{AUTH_BASE}/email-verification",
                "oai-device-id": self.device_id,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            json={"code": str(code or "").strip()},
            allow_redirects=True,
        )
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            raise RuntimeError(f"email-otp/validate 失败: {_safe_response_detail(resp)}")
        return self._next_url_from_response(resp)

    def wait_and_validate_email_otp(self, attempts: int = 3) -> str:
        if not self.mailbox or not self.mail_config:
            raise RuntimeError("当前登录需要邮箱验证码，但没有匹配的邮箱池配置")
        last_error = ""
        for attempt in range(1, max(1, attempts) + 1):
            self.emit("等待邮箱验证码" if attempt == 1 else f"等待新的邮箱验证码({attempt}/{attempts})")
            code = mail_provider.wait_for_code(self.mail_config, self.mailbox)
            if not code:
                last_error = "等待邮箱验证码超时"
                break
            self.emit(f"读取到验证码 {code}")
            try:
                return self._validate_email_otp(code)
            except Exception as error:
                last_error = str(error)
                if "wrong_email_otp_code" not in last_error and "Wrong code" not in last_error:
                    raise
                self.emit(f"验证码 {code} 被判定错误，重新发送后继续等待", "yellow")
                if attempt < max(1, attempts):
                    self.send_email_otp_for_login(referer=f"{AUTH_BASE}/email-verification")
        raise RuntimeError(last_error or "等待邮箱验证码超时")

    def complete_about_you(self) -> str:
        self.emit("填写 about-you 基础资料")
        sentinel_headers = self._sentinel_headers("oauth_create_account")
        resp = self._request(
            "POST",
            AUTH_CREATE_ACCOUNT_URL,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": AUTH_BASE,
                "referer": f"{AUTH_BASE}/about-you",
                **sentinel_headers,
                "oai-device-id": self.device_id,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            json=_random_profile(),
            allow_redirects=True,
        )
        if int(getattr(resp, "status_code", 0) or 0) not in (200, 302):
            raise RuntimeError(f"create_account 失败: {_safe_response_detail(resp)}")
        return self._next_url_from_response(resp)

    def auth_workspaces(self) -> list[dict[str, Any]]:
        cookie = self._cookie("oai-client-auth-session", "auth.openai.com")
        if not cookie:
            return []
        try:
            payload = _decode_b64_json(cookie.split(".", 1)[0])
        except Exception:
            return []
        workspaces = payload.get("workspaces")
        return [item for item in workspaces if isinstance(item, dict)] if isinstance(workspaces, list) else []

    def choose_workspace_id(self, target: str = "") -> str:
        if target:
            return str(target).strip()
        workspaces = self.auth_workspaces()
        for item in workspaces:
            if str(item.get("kind") or "").lower() == "personal" and str(item.get("id") or "").strip():
                return str(item.get("id")).strip()
        for item in workspaces:
            if str(item.get("id") or "").strip():
                return str(item.get("id")).strip()
        raise RuntimeError("auth session 中没有可选择的 workspace")

    def select_workspace(self, workspace_id: str = "", referer: str = AUTH_WORKSPACE_URL) -> str:
        self._request(
            "GET",
            AUTH_WORKSPACE_URL,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "referer": AUTH_BASE,
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-origin",
            },
            allow_redirects=False,
        )
        selected = self.choose_workspace_id(workspace_id)
        self.emit(f"选择 workspace {selected[:8]}...")
        resp = self._request(
            "POST",
            AUTH_WORKSPACE_SELECT_URL,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": AUTH_BASE,
                "referer": referer,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            json={"workspace_id": selected},
            allow_redirects=True,
        )
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            raise RuntimeError(f"workspace/select 失败: {_safe_response_detail(resp)}")
        next_url = self._next_url_from_response(resp)
        if not next_url:
            raise RuntimeError(f"workspace/select 响应缺少 continue_url: {_safe_response_detail(resp)}")
        return next_url

    def finish_chatgpt_callback(self, callback_url: str, referer: str = AUTH_BASE) -> None:
        self.emit("完成 ChatGPT callback，建立 Web session")
        resp = self._request(
            "GET",
            callback_url,
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "referer": referer,
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "same-site",
            },
            allow_redirects=True,
            timeout=60,
        )
        if not (200 <= int(getattr(resp, "status_code", 0) or 0) < 400):
            raise RuntimeError(f"ChatGPT callback 失败: {_safe_response_detail(resp)}")

    def continue_auth_flow(self, start_url: str, *, finish_callback: bool = True) -> str:
        current = _resolve_url(start_url, AUTH_BASE)
        for _ in range(14):
            state = _classify_url(current)
            self.emit(f"Auth step: {current[:180]}")
            if state == "chatgpt_callback":
                if finish_callback:
                    self.finish_chatgpt_callback(current)
                return current
            if state in {"login", ""} and current.startswith(AUTH_BASE):
                current = self.authorize_continue()
                continue
            if state == "login_password":
                if self.password:
                    try:
                        current = self.password_verify()
                    except Exception as password_error:
                        self.emit(f"密码登录不可用，改用邮箱验证码登录: {password_error}", "yellow")
                        current = self.send_email_otp_for_login()
                else:
                    current = self.send_email_otp_for_login()
                continue
            if state == "email_verification":
                current = self.wait_and_validate_email_otp()
                continue
            if state == "about_you":
                current = self.complete_about_you()
                continue
            if state == "workspace":
                current = self.select_workspace(referer=current)
                continue
            if state == "add_phone":
                raise RuntimeError("登录后触发 add-phone 手机验证，无法自动完成")
            if state == "add_email":
                raise RuntimeError("登录后触发 add-email，当前库存账号流程不处理补绑邮箱")
            if state == "email_otp_send":
                current = self.send_email_otp_for_login(referer=AUTH_CREATE_ACCOUNT_PASSWORD_URL)
                continue
            if current.startswith(AUTH_BASE):
                resp = self._request(
                    "GET",
                    current,
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": CHATGPT_BASE,
                        "sec-fetch-dest": "document",
                        "sec-fetch-mode": "navigate",
                        "sec-fetch-site": "same-site",
                    },
                    allow_redirects=True,
                    timeout=60,
                )
                next_url = str(getattr(resp, "url", "") or "")
                if next_url and next_url != current:
                    current = next_url
                    continue
            if current.startswith(CHATGPT_BASE):
                resp = self._request(
                    "GET",
                    current,
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": AUTH_BASE,
                        "sec-fetch-dest": "document",
                        "sec-fetch-mode": "navigate",
                        "sec-fetch-site": "same-site",
                    },
                    allow_redirects=True,
                    timeout=60,
                )
                if 200 <= int(getattr(resp, "status_code", 0) or 0) < 400:
                    return str(getattr(resp, "url", "") or current)
            raise RuntimeError(f"未识别的 auth step: {current}")
        raise RuntimeError(f"auth step 处理次数过多，最后停在 {current}")

    def get_access_token(self) -> str:
        resp = self._request(
            "GET",
            f"{CHATGPT_BASE}/api/auth/session",
            headers={
                "accept": "application/json",
                "referer": f"{CHATGPT_BASE}/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            allow_redirects=True,
        )
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            raise RuntimeError(f"获取 ChatGPT Web session 失败: {_safe_response_detail(resp)}")
        data = self._json(resp)
        token = str(data.get("accessToken") or data.get("access_token") or "").strip()
        if not token:
            safe = dict(data)
            safe.pop("accessToken", None)
            safe.pop("access_token", None)
            raise RuntimeError(f"ChatGPT Web session 缺少 accessToken: {json.dumps(safe, ensure_ascii=False)[:400]}")
        return token

    def login(self) -> dict[str, Any]:
        self.boot()
        entry_url = self.open_auth_entry()
        state = _classify_url(entry_url)
        if state == "chatgpt_callback":
            self.finish_chatgpt_callback(entry_url)
        else:
            start = self.authorize_continue()
            self.continue_auth_flow(start, finish_callback=True)
        token = self.get_access_token()
        context = token_context(token)
        self.emit(f"已获取 ChatGPT Web AT: plan={context.get('plan_type') or '?'} account={context.get('chatgpt_account_id') or 'personal/free'}")
        return {
            "access_token": token,
            "token_context": context,
            "token_source": "chatgpt_web",
            "login_at": _now(),
            "cookie_present": bool(self._cookie("__Secure-next-auth.session-token", "chatgpt.com") or self._cookie("__Secure-next-auth.session-token.0", "chatgpt.com")),
        }

    def refresh_auth_entry_for_workspace(self) -> str:
        self.emit("刷新 auth session 以切换 workspace")
        self._request(
            "GET",
            f"{CHATGPT_BASE}/",
            headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
            },
            allow_redirects=True,
            timeout=60,
        )
        return self.open_auth_entry()

    def switch_workspace_access_token(self, workspace_id: str, current_access_token: str = "", retries: int = 6) -> dict[str, Any]:
        workspace_id = str(workspace_id or "").strip()
        if not workspace_id:
            raise RuntimeError("缺少 K12 workspace_id，无法切换 Web AT")
        if current_access_token and is_token_for_workspace(current_access_token, workspace_id):
            return {"access_token": current_access_token, "token_context": token_context(current_access_token), "switched": False}
        self.emit(f"切换到 K12 workspace {workspace_id[:8]}...")
        try:
            next_url = self.select_workspace(workspace_id=workspace_id)
            self.continue_auth_flow(next_url, finish_callback=True)
        except Exception as error:
            message = str(error)
            if "invalid_auth" not in message and "Invalid authorization" not in message and "auth session" not in message:
                raise
            entry = self.refresh_auth_entry_for_workspace()
            state = _classify_url(entry)
            if state == "workspace":
                next_url = self.select_workspace(workspace_id=workspace_id, referer=entry)
            else:
                next_url = self.continue_auth_flow(entry, finish_callback=False)
                if _classify_url(next_url) == "workspace":
                    next_url = self.select_workspace(workspace_id=workspace_id, referer=next_url)
            self.continue_auth_flow(next_url, finish_callback=True)

        latest = ""
        for attempt in range(1, max(1, retries) + 1):
            latest = self.get_access_token()
            ctx = token_context(latest)
            self.emit(f"K12 Web AT 检查 {attempt}/{retries}: plan={ctx.get('plan_type') or '?'} account={ctx.get('chatgpt_account_id') or '?'}")
            if is_token_for_workspace(latest, workspace_id):
                return {"access_token": latest, "token_context": ctx, "switched": True}
            if attempt < retries:
                time.sleep(1)
        return {"access_token": latest or current_access_token, "token_context": token_context(latest or current_access_token), "switched": False}

    def check_backend_me(self, access_token: str = "") -> dict:
        me_token = access_token or self.get_access_token()
        resp = self._request(
            "GET",
            f"{CHATGPT_BASE}/backend-api/me",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {me_token}",
                "referer": f"{CHATGPT_BASE}/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
            timeout=30,
        )
        status = int(getattr(resp, "status_code", 0) or 0)
        text = str(getattr(resp, "text", "") or "")
        ok = 200 <= status < 300
        return {"ok": ok, "status_code": status, "text": text[:500]}

    def send_test_conversation(self, prompt: str = "Hi") -> dict:
        access_token = self.get_access_token()
        parent_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        body = {
            "action": "next",
            "messages": [{
                "id": message_id,
                "role": "user",
                "content": {"content_type": "text", "parts": [prompt]},
            }],
            "conversation_id": None,
            "parent_message_id": parent_id,
            "model": "gpt-4o-mini",
            "timezone_offset_min": -480,
            "history_and_training_disabled": True,
            "conversation_mode": {"kind": "primary_assistant"},
        }
        headers = {
            "accept": "text/event-stream",
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
            "referer": f"{CHATGPT_BASE}/",
            "oai-device-id": self.device_id,
        }
        resp = self._request("POST", f"{CHATGPT_BASE}/backend-api/conversation", json=body, headers=headers, timeout=30)
        status = int(getattr(resp, "status_code", 0) or 0)
        text = str(getattr(resp, "text", "") or "")
        ok = 200 <= status < 300 and "error" not in text[:400].lower()
        return {"ok": ok, "status_code": status, "text_preview": text[:500]}
