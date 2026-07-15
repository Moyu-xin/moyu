from __future__ import annotations

import base64
import json
import time
from typing import Any


SUB2API_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
INVENTORY_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
CHATGPT_WEB_CLIENT_ID = "app_X8zY6vW2pQ9tR3dE7nK1jL5gH"
OPENAI_API_AUDIENCE = "https://api.openai.com/v1"


def jwt_payload(token: Any) -> dict[str, Any]:
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _string_set(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    text = str(value or "").strip()
    return {part for part in text.replace(",", " ").split() if part}


def _refresh_token_family(token: Any) -> str:
    text = str(token or "").strip()
    if not text:
        return "missing"
    if text.startswith("rt."):
        return "openai"
    if text.startswith(("M.R3_", "0.A", "MC")):
        return "microsoft"
    if text.startswith("1//"):
        return "google"
    return "unknown"


def _unavailable(account: dict[str, Any]) -> bool:
    account_status = str(account.get("status") or "active").strip().lower()
    health_status = str(account.get("health_status") or account.get("sub2api_health_status") or "unknown").strip().lower()
    return account_status in {"disabled", "inactive", "blocked", "banned", "dead", "invalid"} or health_status in {
        "banned",
        "dead",
        "invalid",
        "disabled",
        "token_invalid",
        "auth_invalid",
        "refresh_token_invalid",
        "refresh_token_expired",
    }


def token_profile(account: dict[str, Any]) -> dict[str, Any]:
    access_claims = jwt_payload(account.get("access_token"))
    id_claims = jwt_payload(account.get("id_token"))
    client_id = str(
        account.get("client_id")
        or access_claims.get("client_id")
        or id_claims.get("client_id")
        or ""
    ).strip()
    audiences = _string_set(access_claims.get("aud"))
    scopes = _string_set(access_claims.get("scp") or access_claims.get("scope"))
    refresh_family = _refresh_token_family(account.get("refresh_token"))
    has_access_token = bool(str(account.get("access_token") or "").strip())
    has_refresh_token = refresh_family != "missing"
    platform_access = has_access_token and OPENAI_API_AUDIENCE in audiences and client_id != CHATGPT_WEB_CLIENT_ID
    web_session = has_access_token and client_id == CHATGPT_WEB_CLIENT_ID

    try:
        expires_at = int(access_claims.get("exp") or account.get("token_expires_at") or 0)
    except Exception:
        expires_at = 0
    access_unexpired = platform_access and (not expires_at or expires_at > int(time.time()) + 30)

    refresh_status = str(account.get("oauth_refresh_status") or "").strip().lower()
    refresh_failed = bool(account.get("token_refresh_required")) or refresh_status in {
        "refresh_token_missing",
        "refresh_token_expired",
        "refresh_token_invalid",
        "refresh_token_refresh_failed",
        "token_invalid",
        "auth_invalid",
        "failed",
    }
    sub2api_oauth_client = client_id == SUB2API_OAUTH_CLIENT_ID
    oauth_provenance = str(account.get("oauth_provenance") or "").strip().lower()
    token_source = str(account.get("token_source") or "").strip().lower()
    sub2api_provenance = token_source == "sub2api_openai_oauth" or oauth_provenance.startswith("sub2api_")
    openai_refresh = refresh_family == "openai"
    offline_scope = "offline_access" in scopes
    refresh_capable = openai_refresh and sub2api_oauth_client and sub2api_provenance and platform_access and offline_scope and not refresh_failed
    access_only_capable = access_unexpired and sub2api_oauth_client and sub2api_provenance and not has_refresh_token and offline_scope
    oauth_capable = refresh_capable or access_only_capable

    if openai_refresh and client_id == SUB2API_OAUTH_CLIENT_ID and sub2api_provenance:
        refresh_kind = "sub2api_oauth_rt"
        refresh_label = "sub2api OAuth RT"
    elif openai_refresh and client_id == SUB2API_OAUTH_CLIENT_ID:
        refresh_kind = "openai_rt_unverified_provenance"
        refresh_label = "app_EMo RT（未确认经 sub2api 授权）"
    elif openai_refresh and client_id == INVENTORY_OAUTH_CLIENT_ID:
        refresh_kind = "inventory_oauth_rt"
        refresh_label = "本地库存刷新 RT（不可用于 sub2api）"
    elif openai_refresh:
        refresh_kind = "openai_rt_unknown_client"
        refresh_label = "OpenAI RT（客户端未知）"
    elif refresh_family == "microsoft":
        refresh_kind = "mailbox_microsoft_rt"
        refresh_label = "微软邮箱 RT"
    elif refresh_family == "google":
        refresh_kind = "mailbox_google_rt"
        refresh_label = "Google 邮箱 RT"
    elif refresh_family == "unknown":
        refresh_kind = "unknown_rt"
        refresh_label = "未知 RT"
    elif web_session:
        refresh_kind = "web_session_only"
        refresh_label = "仅网页会话 AT"
    else:
        refresh_kind = "missing"
        refresh_label = "无 RT"

    if oauth_capable and refresh_capable:
        oauth_status = "valid_refresh"
    elif oauth_capable:
        oauth_status = "valid_access_only"
    elif refresh_failed and openai_refresh:
        oauth_status = "invalid_refresh"
    elif openai_refresh and client_id == INVENTORY_OAUTH_CLIENT_ID:
        oauth_status = "incompatible_refresh"
    elif openai_refresh and client_id == SUB2API_OAUTH_CLIENT_ID and not sub2api_provenance:
        oauth_status = "unverified_provenance"
    elif web_session:
        oauth_status = "web_session_only"
    elif refresh_family in {"microsoft", "google"}:
        oauth_status = "mailbox_refresh_token"
    elif has_refresh_token:
        oauth_status = "unverified_refresh"
    else:
        oauth_status = "missing"

    sub2api_healthy = bool(account.get("sub2api_account_id")) and str(account.get("sub2api_health_status") or "").lower() in {
        "ok",
        "alive",
        "healthy",
    }
    available = not _unavailable(account)
    finished = sub2api_healthy or (oauth_capable and available)
    finished_reason = "sub2api_healthy" if sub2api_healthy else "oauth_token" if oauth_capable and available else ""
    if sub2api_healthy:
        warehouse_status = "healthy"
    elif str(account.get("sub2api_import_status") or "").lower() in {"ok", "json_imported"} or account.get("sub2api_account_id"):
        warehouse_status = "imported_unverified"
    elif str(account.get("sub2api_import_status") or "").lower() == "failed":
        warehouse_status = "failed"
    else:
        warehouse_status = "not_imported"

    return {
        "oauth_capable": oauth_capable,
        "oauth_token_status": oauth_status,
        "oauth_client_id": client_id,
        "oauth_client_kind": (
            "sub2api"
            if client_id == SUB2API_OAUTH_CLIENT_ID
            else "inventory"
            if client_id == INVENTORY_OAUTH_CLIENT_ID
            else "web"
            if client_id == CHATGPT_WEB_CLIENT_ID
            else "unknown"
        ),
        "oauth_provenance_verified": sub2api_provenance,
        "refresh_token_kind": refresh_kind,
        "refresh_token_label": refresh_label,
        "sub2api_healthy": sub2api_healthy,
        "warehouse_status": warehouse_status,
        "finished_account": finished,
        "finished_reason": finished_reason,
        "needs_oauth": available and not oauth_capable and not sub2api_healthy,
    }
