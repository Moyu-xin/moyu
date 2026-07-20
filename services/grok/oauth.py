from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any

from curl_cffi import requests

OIDC_ISSUER = "https://auth.x.ai"
GROK_CLI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_SCOPES = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)
TOKEN_ENDPOINT = f"{OIDC_ISSUER}/oauth2/token"
CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CLI_HEADERS = {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell",
}


def _jwt_payload(token: Any) -> dict[str, Any]:
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return {}
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _proxy_kwargs(proxy: str) -> dict[str, Any]:
    value = str(proxy or "").strip()
    return {"proxies": {"http": value, "https": value}} if value else {}


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel and should_cancel():
        raise RuntimeError("Grok registration cancelled")


def exchange_sso_for_oauth(
    sso_token: str,
    *,
    proxy: str = "",
    timeout: float = 45.0,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Convert an accounts.x.ai SSO session into renewable Grok CLI OAuth tokens."""
    sso = str(sso_token or "").strip()
    if not sso:
        raise RuntimeError("missing xAI SSO token")

    notify = progress or (lambda _message: None)
    proxy_kwargs = _proxy_kwargs(proxy)
    session = requests.Session()
    session.cookies.set("sso", sso, domain=".x.ai")
    http_timeout = max(8.0, min(float(timeout or 45.0), 90.0))

    _check_cancel(should_cancel)
    notify("验证 xAI SSO 会话")
    response = session.get(
        "https://accounts.x.ai/",
        impersonate="chrome",
        timeout=http_timeout,
        **proxy_kwargs,
    )
    if "sign-in" in str(response.url) or "sign-up" in str(response.url):
        raise RuntimeError("xAI SSO token is invalid or expired")

    _check_cancel(should_cancel)
    notify("申请 Grok CLI Device Code")
    response = session.post(
        f"{OIDC_ISSUER}/oauth2/device/code",
        data={"client_id": GROK_CLI_CLIENT_ID, "scope": OIDC_SCOPES},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome",
        timeout=http_timeout,
        **proxy_kwargs,
    )
    if int(response.status_code or 0) >= 400:
        raise RuntimeError(f"xAI device/code failed: HTTP {response.status_code}")
    device = response.json()
    if not isinstance(device, dict) or not device.get("device_code") or not device.get("user_code"):
        raise RuntimeError("xAI device/code response is incomplete")

    _check_cancel(should_cancel)
    session.get(
        str(device.get("verification_uri_complete") or ""),
        impersonate="chrome",
        timeout=http_timeout,
        **proxy_kwargs,
    )
    response = session.post(
        f"{OIDC_ISSUER}/oauth2/device/verify",
        data={"user_code": device["user_code"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome",
        timeout=http_timeout,
        allow_redirects=True,
        **proxy_kwargs,
    )
    if "consent" not in str(response.url):
        raise RuntimeError(f"xAI device verify failed: {response.url}")

    _check_cancel(should_cancel)
    notify("确认 Grok CLI OAuth 授权")
    response = session.post(
        f"{OIDC_ISSUER}/oauth2/device/approve",
        data={
            "user_code": device["user_code"],
            "action": "allow",
            "principal_type": "User",
            "principal_id": "",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome",
        timeout=http_timeout,
        allow_redirects=True,
        **proxy_kwargs,
    )
    if "done" not in str(response.url):
        raise RuntimeError(f"xAI device approve failed: {response.url}")

    deadline = time.time() + min(float(device.get("expires_in") or 1800), http_timeout)
    interval = max(0.4, min(float(device.get("interval") or 1), 1.5))
    token: dict[str, Any] | None = None
    while time.time() < deadline:
        _check_cancel(should_cancel)
        response = session.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": GROK_CLI_CLIENT_ID,
                "device_code": device["device_code"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=http_timeout,
            **proxy_kwargs,
        )
        if int(response.status_code or 0) < 400:
            value = response.json()
            token = value if isinstance(value, dict) else None
            break
        try:
            error = str((response.json() or {}).get("error") or "")
        except Exception:
            error = ""
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval = min(10.0, interval + 1.0)
            time.sleep(interval)
            continue
        raise RuntimeError(f"xAI OAuth token exchange failed: {error or response.status_code}")

    if not token or not str(token.get("access_token") or "").strip():
        raise RuntimeError("xAI OAuth token polling timed out")

    expires_in = int(token.get("expires_in") or 21600)
    token["expires_at"] = int(time.time()) + expires_in
    token["client_id"] = GROK_CLI_CLIENT_ID
    token["token_endpoint"] = TOKEN_ENDPOINT
    token["base_url"] = CLI_BASE_URL
    token["headers"] = dict(CLI_HEADERS)
    claims = _jwt_payload(token.get("id_token")) or _jwt_payload(token.get("access_token"))
    token["email"] = str(claims.get("email") or "")
    token["sub"] = str(claims.get("sub") or claims.get("principal_id") or "")
    notify("Grok CLI OAuth 凭据已生成")
    return token
