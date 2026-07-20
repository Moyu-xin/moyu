from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from services.atomic_json_store import read_json, write_json
from services.account_token_profile import token_profile
from services.config import DATA_DIR
from services.sqlite_store import collection_page, document_signature

REGISTERED_ACCOUNTS_FILE = DATA_DIR / "registered_accounts.json"
_lock = Lock()
_pool_cache_lock = Lock()
_pool_cache: tuple[tuple[str, int, int], float, dict[str, Any]] | None = None
_stats_cache: tuple[tuple[str, int, int], float, dict[str, Any]] | None = None


SECRET_KEYS = {"access_token", "refresh_token", "id_token", "password", "session_token", "sso", "sso_token"}
TRANSIENT_KEYS = {
    "access_token_preview",
    "refresh_token_preview",
    "id_token_preview",
    "has_access_token",
    "has_refresh_token",
    "has_id_token",
    "oauth_capable",
    "oauth_token_status",
    "oauth_client_id",
    "oauth_client_kind",
    "oauth_provenance_verified",
    "refresh_token_kind",
    "refresh_token_label",
    "sub2api_healthy",
    "warehouse_status",
    "finished_account",
    "finished_reason",
    "needs_oauth",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(email: object) -> str:
    return str(email or "").strip().lower()


def _scope_id(account: dict[str, Any]) -> str:
    """账号实例空间 ID。

    同一邮箱可能对应多个 ChatGPT/K12 workspace。库存里必须按“邮箱 + 空间”
    区分，否则 sub2api JSON 里同邮箱的多空间记录会互相覆盖。
    """
    return _first_non_empty(
        account.get("chatgpt_account_id"),
        account.get("workspace_id"),
        account.get("k12_team_id"),
        account.get("organization_id"),
    )


def account_identity_key(account: dict[str, Any]) -> str:
    email = _key(account.get("email"))
    if not email:
        return ""
    platform = str(account.get("platform") or "openai").strip().lower()
    platform_prefix = f"{platform}:" if platform and platform != "openai" else ""
    scope = _scope_id(account)
    identity = f"{email}::{scope}" if scope else email
    return platform_prefix + identity


def account_inventory_id(account: dict[str, Any]) -> str:
    identity = account_identity_key(account)
    if not identity:
        return ""
    return hashlib.sha256(identity.encode("utf-8", errors="ignore")).hexdigest()[:24]


def account_matches_selector(account: dict[str, Any], selector: object) -> bool:
    if isinstance(selector, dict):
        selector_identity = account_identity_key(selector).lower()
        selector_inventory_id = str(selector.get("inventory_id") or account_inventory_id(selector)).strip().lower()
        identity = account_identity_key(account).lower()
        inventory_id = str(account.get("inventory_id") or account_inventory_id(account)).strip().lower()
        return bool(selector_identity and selector_identity == identity) or bool(selector_inventory_id and selector_inventory_id == inventory_id)
    text = str(selector or "").strip()
    if not text:
        return False
    text_l = text.lower()
    identity = account_identity_key(account).lower()
    inventory_id = str(account.get("inventory_id") or account_inventory_id(account)).strip().lower()
    return text_l in {identity, inventory_id, _key(account.get("email"))}


def _load() -> list[dict[str, Any]]:
    data = read_json(REGISTERED_ACCOUNTS_FILE, [], expected_schema_version=1)
    if isinstance(data, dict):
        data = data.get("items") or data.get("accounts") or data.get("data")
    return data if isinstance(data, list) else []


def _save(items: list[dict[str, Any]]) -> None:
    REGISTERED_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(REGISTERED_ACCOUNTS_FILE, items, schema_version=1)


def _source_signature() -> tuple[str, int, int]:
    return document_signature(REGISTERED_ACCOUNTS_FILE)


def _safe_json(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
            try:
                return json.loads(text)
            except Exception:
                return value
    return value


def _jwt_payload(token: object) -> dict[str, Any]:
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


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _as_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        number = int(float(str(value)))
        return number
    except Exception:
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        number = float(str(value))
        return number
    except Exception:
        return None


def _registration_batch_id(account: dict[str, Any]) -> str:
    explicit = _first_non_empty(
        account.get("registration_batch_id"),
        account.get("registration_batch"),
        account.get("batch_id"),
    )
    if explicit:
        return explicit
    created = str(account.get("registered_at") or account.get("created_at") or "").strip()
    return f"legacy-{created[:10]}" if len(created) >= 10 else "legacy-unknown"


def _token_context(access_token: object = "", id_token: object = "") -> dict[str, Any]:
    access_claims = _jwt_payload(access_token)
    id_claims = _jwt_payload(id_token)
    auth = access_claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    profile = access_claims.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    id_auth = id_claims.get("https://api.openai.com/auth")
    id_auth = id_auth if isinstance(id_auth, dict) else {}
    exp = None
    for claims in (access_claims, id_claims):
        try:
            value = int(claims.get("exp") or 0)
        except Exception:
            value = 0
        if value:
            exp = value
            break
    return {
        "email": _first_non_empty(profile.get("email"), access_claims.get("email"), id_claims.get("email")),
        "chatgpt_account_id": _first_non_empty(auth.get("chatgpt_account_id"), auth.get("account_id")),
        "chatgpt_user_id": _first_non_empty(auth.get("chatgpt_user_id"), auth.get("user_id"), id_auth.get("user_id"), access_claims.get("sub"), id_claims.get("sub")),
        "plan_type": _first_non_empty(auth.get("chatgpt_plan_type"), profile.get("plan_type")),
        "client_id": _first_non_empty(access_claims.get("client_id"), id_claims.get("client_id")),
        "expires_at": exp,
        "expires_in": max(0, exp - int(time.time())) if exp else None,
    }


def _infer_account_type(account: dict[str, Any]) -> str:
    raw = _first_non_empty(account.get("plan_type"), account.get("account_type"), account.get("inventory_type"), account.get("type"))
    plan = str(raw or "").strip().lower()
    if plan in {"plus", "chatgpt_plus", "paid", "premium"}:
        return "plus"
    if plan in {"pro", "team", "teams", "business", "enterprise"}:
        return "team" if plan == "teams" else plan
    if bool(account.get("k12_mode")) or plan == "k12" or str(account.get("k12_join_status") or ""):
        return "k12"
    if str(account.get("workspace_id") or account.get("chatgpt_account_id") or "").strip() and plan not in {"", "free"}:
        return plan
    return plan or "free"


def _normalize_quota_fields(base: dict[str, Any], source: dict[str, Any]) -> None:
    quota = source.get("quota") if isinstance(source.get("quota"), dict) else {}
    usage = source.get("usage") if isinstance(source.get("usage"), dict) else {}
    quota_details = source.get("quota_details") if isinstance(source.get("quota_details"), dict) else {}
    base["quota_status"] = str(source.get("quota_status") or quota.get("status") or usage.get("status") or base.get("quota_status") or "unknown")
    for target, candidates in {
        "quota_used": (source.get("quota_used"), quota.get("used"), usage.get("used"), usage.get("total_used"), quota_details.get("used")),
        "quota_total": (source.get("quota_total"), quota.get("total"), quota.get("limit"), usage.get("total"), quota_details.get("total")),
        "quota_remaining": (source.get("quota_remaining"), quota.get("remaining"), usage.get("remaining"), quota_details.get("remaining")),
    }.items():
        number = None
        for candidate in candidates:
            number = _as_float(candidate)
            if number is not None:
                break
        if number is not None:
            base[target] = number
    for key in ("quota_reset_at", "quota_checked_at", "quota_source"):
        value = source.get(key) or quota.get(key.replace("quota_", "")) or quota_details.get(key.replace("quota_", ""))
        if value:
            base[key] = value
    if quota_details:
        base["quota_details"] = quota_details
    elif quota:
        base["quota_details"] = quota


def _normalize(account: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(account, dict):
        return None
    access_token = str(account.get("access_token") or "").strip()
    refresh_token = str(account.get("refresh_token") or "").strip()
    id_token = str(account.get("id_token") or "").strip()
    ctx = _token_context(access_token, id_token)
    email = _first_non_empty(account.get("email"), account.get("username"), account.get("name") if "@" in str(account.get("name") or "") else "", ctx.get("email"))
    if "@" not in email:
        return None
    now = _now()
    base: dict[str, Any] = {
        "platform": str(account.get("platform") or "openai").strip().lower() or "openai",
        "email": email,
        "password": str(account.get("password") or ""),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "sso_token": str(account.get("sso_token") or account.get("sso") or ""),
        "source_type": str(account.get("source_type") or account.get("source") or "web"),
        "created_at": str(account.get("created_at") or account.get("created") or now),
        "updated_at": str(account.get("updated_at") or account.get("updated") or now),
        "status": str(account.get("status") or "active"),
        "note": str(account.get("note") or account.get("notes") or ""),
        "token_source": str(account.get("token_source") or ""),
        "client_id": _first_non_empty(account.get("client_id"), ctx.get("client_id")),
        "chatgpt_account_id": _first_non_empty(account.get("chatgpt_account_id"), account.get("account_id"), ctx.get("chatgpt_account_id")),
        "chatgpt_user_id": _first_non_empty(account.get("chatgpt_user_id"), account.get("user_id"), ctx.get("chatgpt_user_id")),
        "plan_type": _first_non_empty(account.get("plan_type"), ctx.get("plan_type"), "free"),
        "token_expires_at": account.get("token_expires_at") or ctx.get("expires_at"),
        "token_expires_in": account.get("token_expires_in") if account.get("token_expires_in") is not None else ctx.get("expires_in"),
        "health_status": str(account.get("health_status") or account.get("alive_status") or account.get("sub2api_health_status") or "unknown"),
        "health_checked_at": str(account.get("health_checked_at") or account.get("sub2api_health_checked_at") or ""),
        "health_code": account.get("health_code") if account.get("health_code") is not None else account.get("sub2api_health_code"),
        "health_error": str(account.get("health_error") or account.get("sub2api_last_error") or ""),
        "quota_status": str(account.get("quota_status") or "unknown"),
        "registration_batch_id": _registration_batch_id(account),
        "registration_batch_label": str(account.get("registration_batch_label") or ""),
        "outbound_at": str(account.get("outbound_at") or ""),
        "outbound_batch_id": str(account.get("outbound_batch_id") or ""),
        "outbound_format": str(account.get("outbound_format") or ""),
    }
    for bool_key in ("k12_mode", "sub2api_disabled", "cpa_disabled"):
        if bool_key in account:
            base[bool_key] = bool(account.get(bool_key))
    passthrough = {
        "k12_team_id", "workspace_id", "k12_join_status", "sub2api_imported_at", "sub2api_import_status",
        "sub2api_account_id", "sub2api_health_status", "sub2api_health_checked_at", "sub2api_health_code",
        "sub2api_update_status", "sub2api_updated_at", "sub2api_import_method", "sub2api_type", "sub2api_platform",
        "cpa_imported_at", "cpa_import_status", "cpa_auth_file", "cpa_last_error", "cpa_health_status",
        "cpa_health_checked_at", "cpa_health_code", "cpa_update_status", "cpa_updated_at",
        "organization_id", "group_ids", "tags", "rate_multiplier", "concurrency", "priority", "load_factor", "last_used_at", "proxy",
        "quota_model", "quota_reset_at", "quota_checked_at", "quota_source", "quota_details", "last_quota_error", "quota_error_status",
        "health_method", "health_latency_ms", "last_health_result", "deleted_reason",
        "token_type", "token_endpoint", "base_url", "headers", "scope", "subject", "oauth_status", "oauth_error",
    }
    for key in passthrough:
        if key in account and account.get(key) not in (None, ""):
            base[key] = account.get(key)
    _normalize_quota_fields(base, account)
    for key, value in account.items():
        if key in base or key in TRANSIENT_KEYS or str(key).startswith("_"):
            continue
        if key in SECRET_KEYS:
            continue
        if key not in passthrough and key not in {"username", "source", "created", "updated", "notes", "account_id", "user_id", "type"}:
            base[key] = value
    base["account_type"] = _infer_account_type(base)
    base["inventory_key"] = account_identity_key(base)
    base["inventory_id"] = account_inventory_id(base)
    return base


def _preview(value: object, keep: int = 8) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}...{text[-keep:]}"


def _public(account: dict[str, Any], *, include_secret: bool = True) -> dict[str, Any]:
    item = dict(account)
    item.update(token_profile(item))
    item["access_token_preview"] = _preview(item.get("access_token"))
    item["refresh_token_preview"] = _preview(item.get("refresh_token"))
    item["id_token_preview"] = _preview(item.get("id_token"))
    item["has_access_token"] = bool(item.get("access_token"))
    item["has_refresh_token"] = bool(item.get("refresh_token"))
    item["has_id_token"] = bool(item.get("id_token"))
    item["sso_token_preview"] = _preview(item.get("sso_token"))
    item["has_sso_token"] = bool(item.get("sso_token"))
    if not include_secret:
        for key in SECRET_KEYS:
            item.pop(key, None)
    return item


def list_registered_accounts(*, include_secret: bool = True) -> list[dict[str, Any]]:
    with _lock:
        items = [_normalize(item) for item in _load() if isinstance(item, dict)]
    return [_public(item, include_secret=include_secret) for item in items if item]


def _stats(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "total": len(accounts),
        "active": 0,
        "disabled": 0,
        "banned": 0,
        "with_access_token": 0,
        "with_refresh_token": 0,
        "quota_known": 0,
        "quota_used_total": 0.0,
        "quota_remaining_total": 0.0,
        "health_ok": 0,
        "health_failed": 0,
        "health_unknown": 0,
        "failed_or_banned": 0,
        "sub2api_imported": 0,
        "cpa_imported": 0,
        "finished": 0,
        "needs_oauth": 0,
        "oauth_capable": 0,
        "sub2api_healthy": 0,
        "unavailable": 0,
    }
    by_type: dict[str, int] = {}
    by_plan: dict[str, int] = {}
    by_health: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    by_refresh_token_kind: dict[str, int] = {}
    by_oauth_token_status: dict[str, int] = {}
    failed_health = {
        "failed", "dead", "banned", "invalid", "disabled", "missing_token", "missing_sub2api_id",
        "token_expired", "token_invalid", "auth_invalid", "cloudflare_blocked", "region_blocked",
        "rate_limited", "proxy_error", "timeout", "network_error", "upstream_error", "blocked",
        "quota_failed", "quota_unknown", "refresh_token_missing", "refresh_token_expired",
        "refresh_token_invalid", "refresh_token_refresh_failed",
    }
    for item in accounts:
        platform = str(item.get("platform") or "openai").strip().lower()
        platform = "grok" if platform == "grok" else "openai"
        by_platform[platform] = by_platform.get(platform, 0) + 1
        status = str(item.get("status") or "active").lower()
        status_unavailable = False
        if status in {"disabled", "inactive", "blocked"}:
            stats["disabled"] += 1
            status_unavailable = True
        elif status in {"banned", "dead", "invalid"}:
            stats["banned"] += 1
            status_unavailable = True
        else:
            stats["active"] += 1
        if item.get("has_access_token"):
            stats["with_access_token"] += 1
        if item.get("has_refresh_token"):
            stats["with_refresh_token"] += 1
        health = str(item.get("health_status") or "unknown").lower()
        by_health[health] = by_health.get(health, 0) + 1
        is_health_failed = False
        if health in {"ok", "alive", "healthy"}:
            stats["health_ok"] += 1
        elif health in failed_health:
            stats["health_failed"] += 1
            is_health_failed = True
        else:
            stats["health_unknown"] += 1
        if status_unavailable or is_health_failed:
            stats["failed_or_banned"] += 1
        if item.get("sub2api_account_id"):
            stats["sub2api_imported"] += 1
        if item.get("cpa_auth_file"):
            stats["cpa_imported"] += 1
        if item.get("finished_account"):
            stats["finished"] += 1
        if item.get("needs_oauth"):
            stats["needs_oauth"] += 1
        if item.get("oauth_capable"):
            stats["oauth_capable"] += 1
        if item.get("sub2api_healthy"):
            stats["sub2api_healthy"] += 1
        if not item.get("finished_account") and not item.get("needs_oauth"):
            stats["unavailable"] += 1
        refresh_kind = str(item.get("refresh_token_kind") or "missing")
        by_refresh_token_kind[refresh_kind] = by_refresh_token_kind.get(refresh_kind, 0) + 1
        oauth_token_status = str(item.get("oauth_token_status") or "missing")
        by_oauth_token_status[oauth_token_status] = by_oauth_token_status.get(oauth_token_status, 0) + 1
        account_type = str(item.get("account_type") or "free")
        by_type[account_type] = by_type.get(account_type, 0) + 1
        plan = str(item.get("plan_type") or account_type or "free")
        by_plan[plan] = by_plan.get(plan, 0) + 1
        if str(item.get("quota_status") or "").lower() in {"known", "ok"} or item.get("quota_used") is not None or item.get("quota_remaining") is not None:
            stats["quota_known"] += 1
        used = _as_float(item.get("quota_used"))
        remaining = _as_float(item.get("quota_remaining"))
        if used is not None:
            stats["quota_used_total"] += used
        if remaining is not None:
            stats["quota_remaining_total"] += remaining
    stats["by_type"] = by_type
    stats["by_plan"] = by_plan
    stats["by_health"] = by_health
    stats["by_platform"] = by_platform
    stats["by_refresh_token_kind"] = by_refresh_token_kind
    stats["by_oauth_token_status"] = by_oauth_token_status
    stats["quota_used_total"] = round(float(stats["quota_used_total"]), 2)
    stats["quota_remaining_total"] = round(float(stats["quota_remaining_total"]), 2)
    return stats


def account_pool() -> dict[str, Any]:
    global _pool_cache, _stats_cache
    signature = _source_signature()
    now = time.monotonic()
    with _pool_cache_lock:
        if _pool_cache and _pool_cache[0] == signature and _pool_cache[1] > now:
            return deepcopy(_pool_cache[2])
    accounts = list_registered_accounts(include_secret=True)
    result = {"accounts": accounts, "stats": _stats(accounts)}
    with _pool_cache_lock:
        _pool_cache = (signature, now + 5.0, deepcopy(result))
        _stats_cache = (signature, now + 5.0, deepcopy(result["stats"]))
    return result


def account_pool_stats() -> dict[str, Any]:
    global _stats_cache
    signature = _source_signature()
    now = time.monotonic()
    with _pool_cache_lock:
        if _pool_cache and _pool_cache[0] == signature and _pool_cache[1] > now:
            return deepcopy(_pool_cache[2]["stats"])
        if _stats_cache and _stats_cache[0] == signature and _stats_cache[1] > now:
            return deepcopy(_stats_cache[2])
    stats = _stats(list_registered_accounts(include_secret=True))
    with _pool_cache_lock:
        _stats_cache = (signature, now + 5.0, deepcopy(stats))
    return stats


def account_pool_page(limit: int = 100, offset: int = 0, *, include_secret: bool = True) -> dict[str, Any]:
    page_limit = max(1, min(500, int(limit or 100)))
    page_offset = max(0, int(offset or 0))
    with _lock:
        page = collection_page(REGISTERED_ACCOUNTS_FILE, limit=page_limit, offset=page_offset)
        if page is None:
            raw_items = [item for item in _load() if isinstance(item, dict)]
            migrated_page = collection_page(REGISTERED_ACCOUNTS_FILE, limit=page_limit, offset=page_offset)
            if migrated_page is None:
                selected = raw_items[page_offset:page_offset + page_limit]
                total = len(raw_items)
            else:
                selected, total = migrated_page
        else:
            selected, total = page
    accounts = [_public(item, include_secret=include_secret) for item in (_normalize(raw) for raw in selected) if item]
    return {
        "accounts": accounts,
        "stats": account_pool_stats(),
        "page_info": {
            "offset": page_offset,
            "limit": page_limit,
            "returned": len(accounts),
            "total": total,
            "partial": page_offset + len(accounts) < total,
        },
    }


def add_registered_account(account: dict[str, Any]) -> None:
    normalized = _normalize(account)
    if not normalized:
        return
    normalized_key = account_identity_key(normalized)
    with _lock:
        items = [_normalize(item) for item in _load() if isinstance(item, dict)]
        items = [item for item in items if item and account_identity_key(item) != normalized_key]
        items.append(normalized)
        _save([item for item in items if item])


def update_registered_account(selector: object, updates: dict[str, Any]) -> dict[str, Any] | None:
    if not selector:
        return None
    now = _now()
    updated: dict[str, Any] | None = None
    with _lock:
        items: list[dict[str, Any]] = []
        for raw in _load():
            if not isinstance(raw, dict):
                continue
            item = _normalize(raw)
            if not item:
                continue
            if account_matches_selector(item, selector):
                item.update({key: value for key, value in updates.items() if not str(key).startswith("_")})
                item["updated_at"] = now
                item = _normalize(item) or item
                updated = item
            items.append(item)
        _save(items)
    return _public(updated) if updated else None


def update_registered_accounts_bulk(updates_by_email: dict[str, dict[str, Any]]) -> dict[str, Any]:
    normalized_updates = {str(email).strip().lower(): dict(updates or {}) for email, updates in (updates_by_email or {}).items() if str(email or "").strip()}
    if not normalized_updates:
        return account_pool()
    now = _now()
    updated = 0
    with _lock:
        items: list[dict[str, Any]] = []
        for raw in _load():
            if not isinstance(raw, dict):
                continue
            item = _normalize(raw)
            if not item:
                continue
            changes = (
                normalized_updates.get(str(item.get("inventory_id") or "").strip().lower())
                or normalized_updates.get(account_identity_key(item).lower())
                or normalized_updates.get(_key(item.get("email")))
            )
            if changes:
                item.update({key: value for key, value in changes.items() if not str(key).startswith("_")})
                item["updated_at"] = now
                item = _normalize(item) or item
                updated += 1
            items.append(item)
        _save(items)
    result = account_pool()
    result["updated"] = updated
    return result


def delete_registered_account(email: str) -> dict[str, Any]:
    return delete_registered_accounts([email])


def delete_registered_accounts(emails: list[object], *, reason: str = "") -> dict[str, Any]:
    targets = [item for item in emails if isinstance(item, dict) or str(item or "").strip()]
    removed = 0
    if not targets:
        result = account_pool()
        result["removed"] = 0
        return result
    with _lock:
        items = []
        for item in (_normalize(raw) for raw in _load() if isinstance(raw, dict)):
            if not item:
                continue
            if any(account_matches_selector(item, target) for target in targets):
                removed += 1
            else:
                items.append(item)
        _save(items)
    result = account_pool()
    result["removed"] = removed
    if reason:
        result["reason"] = reason
    return result


def prune_banned_accounts() -> dict[str, Any]:
    accounts = list_registered_accounts(include_secret=False)
    selectors = [str(item.get("inventory_id") or item.get("email") or "") for item in accounts if str(item.get("status") or "").lower() in {"banned", "dead", "invalid"} or str(item.get("health_status") or "").lower() in {"banned", "dead", "invalid", "disabled", "token_invalid", "auth_invalid"}]
    return delete_registered_accounts(selectors, reason="prune_banned")


def clear_registered_accounts() -> dict[str, Any]:
    with _lock:
        count = len(_load())
        _save([])
    result = account_pool()
    result["removed"] = count
    return result


def registration_batches() -> list[dict[str, Any]]:
    accounts = list_registered_accounts(include_secret=False)
    batches: dict[str, dict[str, Any]] = {}
    for item in accounts:
        batch_id = _registration_batch_id(item)
        bucket = batches.setdefault(batch_id, {
            "id": batch_id,
            "label": str(item.get("registration_batch_label") or batch_id),
            "created_at": str(item.get("created_at") or ""),
            "total": 0,
            "available": 0,
            "outbound": 0,
        })
        bucket["total"] += 1
        created = str(item.get("created_at") or "")
        if created and (not bucket["created_at"] or created < bucket["created_at"]):
            bucket["created_at"] = created
        if item.get("outbound_at"):
            bucket["outbound"] += 1
        else:
            bucket["available"] += 1
    return sorted(batches.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)


def select_accounts_for_outbound(
    *,
    quantity: int = 0,
    registration_batch_id: str = "",
    include_outbound: bool = False,
) -> list[dict[str, Any]]:
    batch_id = str(registration_batch_id or "").strip()
    accounts = list_registered_accounts(include_secret=True)
    selected = [
        item for item in accounts
        if (not batch_id or _registration_batch_id(item) == batch_id)
        and (include_outbound or not str(item.get("outbound_at") or "").strip())
        and bool(str(item.get("access_token") or "").strip())
    ]
    selected.sort(key=lambda item: str(item.get("created_at") or ""))
    limit = max(0, int(quantity or 0))
    return selected[:limit] if limit else selected


def mark_accounts_outbound(accounts: list[dict[str, Any]], *, export_format: str) -> dict[str, Any]:
    selectors = [account_identity_key(item) or str(item.get("inventory_id") or "") for item in accounts]
    selectors = [item for item in selectors if item]
    if not selectors:
        return {"marked": 0, "outbound_batch_id": "", "outbound_at": ""}
    outbound_at = _now()
    outbound_batch_id = f"out-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    marked = 0
    with _lock:
        items: list[dict[str, Any]] = []
        for raw in _load():
            normalized = _normalize(raw) if isinstance(raw, dict) else None
            if not normalized:
                continue
            if any(account_matches_selector(normalized, selector) for selector in selectors):
                normalized.update({
                    "outbound_at": outbound_at,
                    "outbound_batch_id": outbound_batch_id,
                    "outbound_format": str(export_format or "json").strip().lower(),
                    "updated_at": outbound_at,
                })
                marked += 1
            items.append(normalized)
        _save(items)
    return {
        "marked": marked,
        "outbound_batch_id": outbound_batch_id,
        "outbound_at": outbound_at,
    }


def _parse_credentials_map(value: Any) -> dict[str, Any]:
    value = _safe_json(value)
    return value if isinstance(value, dict) else {}


def _parse_sub2api_item(item: dict[str, Any]) -> dict[str, Any]:
    credentials = _parse_credentials_map(item.get("credentials"))
    extra = _parse_credentials_map(item.get("extra"))
    metadata = _parse_credentials_map(item.get("metadata"))
    email = _first_non_empty(credentials.get("email"), extra.get("email"), item.get("email"), item.get("name") if "@" in str(item.get("name") or "") else "")
    plan_type = _first_non_empty(credentials.get("plan_type"), extra.get("plan_type"), item.get("plan_type"), metadata.get("plan_type"), "free")
    enabled_value = item.get("enabled")
    status = _first_non_empty(item.get("status"), "active" if enabled_value is not False else "disabled")
    account: dict[str, Any] = {
        "email": email,
        "password": _first_non_empty(credentials.get("password"), extra.get("password"), item.get("password")),
        "access_token": _first_non_empty(credentials.get("access_token"), item.get("access_token")),
        "refresh_token": _first_non_empty(credentials.get("refresh_token"), item.get("refresh_token")),
        "id_token": _first_non_empty(credentials.get("id_token"), item.get("id_token")),
        "source_type": "sub2api-json",
        "status": status,
        "note": _first_non_empty(item.get("notes"), item.get("note"), extra.get("note")),
        "plan_type": plan_type,
        "account_type": plan_type,
        "sub2api_account_id": item.get("id") or item.get("account_id") or item.get("accountId"),
        "sub2api_import_status": "json_imported",
        "sub2api_type": item.get("type") or item.get("account_type") or "",
        "sub2api_platform": item.get("platform") or "",
        "group_ids": item.get("group_ids") or item.get("groups") or [],
        "concurrency": item.get("concurrency"),
        "priority": item.get("priority"),
        "rate_multiplier": item.get("rate_multiplier"),
        "load_factor": item.get("load_factor"),
        "chatgpt_account_id": _first_non_empty(credentials.get("chatgpt_account_id"), credentials.get("account_id"), extra.get("chatgpt_account_id")),
        "chatgpt_user_id": _first_non_empty(credentials.get("chatgpt_user_id"), credentials.get("user_id"), extra.get("chatgpt_user_id")),
        "organization_id": _first_non_empty(credentials.get("organization_id"), extra.get("organization_id")),
        "workspace_id": _first_non_empty(credentials.get("workspace_id"), credentials.get("organization_id"), extra.get("workspace_id")),
        "k12_team_id": _first_non_empty(extra.get("k12_team_id"), credentials.get("workspace_id"), credentials.get("organization_id")),
        "k12_mode": bool(extra.get("k12_mode")) or str(plan_type).lower() == "k12",
        "k12_join_status": extra.get("k12_join_status") or "",
        "token_source": extra.get("token_source") or "",
        "client_id": credentials.get("client_id") or "",
        "token_expires_at": credentials.get("expires_at") or item.get("expires_at"),
        "created_at": item.get("created_at") or item.get("createdAt") or _now(),
        "updated_at": item.get("updated_at") or item.get("updatedAt") or _now(),
    }
    _normalize_quota_fields(account, {**item, **extra, **metadata})
    return {key: value for key, value in account.items() if value not in (None, "")}


def _walk_json_accounts(data: Any) -> list[dict[str, Any]]:
    data = _safe_json(data)
    result: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            result.extend(_walk_json_accounts(item))
        return result
    if not isinstance(data, dict):
        return result
    if isinstance(data.get("credentials"), (dict, str)) or data.get("platform") or data.get("type") in {"oauth", "openai", "sub2api-data"}:
        if data.get("type") == "sub2api-data" and isinstance(data.get("accounts"), list):
            result.extend(_walk_json_accounts(data.get("accounts")))
        elif data.get("credentials") is not None:
            result.append(_parse_sub2api_item(data))
        elif data.get("email") or data.get("username"):
            result.append(data)
    for key in ("accounts", "items", "rows", "list"):
        if isinstance(data.get(key), list):
            result.extend(_walk_json_accounts(data.get(key)))
    nested = data.get("data")
    if isinstance(nested, (dict, list)) and nested is not data:
        result.extend(_walk_json_accounts(nested))
    if not result and (data.get("email") or data.get("username")):
        result.append(data)
    # 去重但保留顺序
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in result:
        normalized = _normalize(item if isinstance(item, dict) else {})
        marker = account_identity_key(normalized) if normalized else ""
        marker = marker or json.dumps(item, sort_keys=True, ensure_ascii=False)[:200]
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(item)
    return deduped


def _parse_line_account(line: str) -> dict[str, Any] | None:
    if not line or "@" not in line:
        return None
    if "----" in line:
        parts = [part.strip() for part in line.split("----")]
    elif "|" in line:
        parts = [part.strip() for part in line.split("|")]
    elif "," in line and line.count(",") <= 6:
        parts = [part.strip() for part in line.split(",")]
    else:
        parts = [line.strip()]
    return {
        "email": parts[0].strip(),
        "password": parts[1].strip() if len(parts) > 1 else "",
        "refresh_token": parts[2].strip() if len(parts) > 2 else "",
        "access_token": parts[3].strip() if len(parts) > 3 else "",
        "id_token": parts[4].strip() if len(parts) > 4 else "",
    }


def import_registered_accounts(text: str, *, source_type: str = "manual") -> dict[str, Any]:
    imported = 0
    updated = 0
    parsed: list[dict[str, Any]] = []
    raw = str(text or "").strip()
    if raw.startswith("[") or raw.startswith("{"):
        try:
            data = json.loads(raw)
            parsed.extend(_walk_json_accounts(data))
        except Exception:
            parsed = []
    if not parsed:
        for line in raw.splitlines():
            item = _parse_line_account(line.strip())
            if item:
                parsed.append(item)
    changed_emails: list[str] = []
    changed_account_ids: list[str] = []
    skipped = 0
    with _lock:
        existing = {account_identity_key(item): item for item in (_normalize(raw_item) for raw_item in _load() if isinstance(raw_item, dict)) if item and account_identity_key(item)}
        now = _now()
        for item in parsed:
            if source_type and not item.get("source_type"):
                item["source_type"] = source_type
            normalized = _normalize(item)
            if not normalized:
                skipped += 1
                continue
            key = account_identity_key(normalized)
            old = existing.get(key)
            if old:
                updated += 1
                normalized["created_at"] = old.get("created_at") or normalized["created_at"]
                # 空密钥不覆盖旧密钥
                for secret_key in SECRET_KEYS:
                    if not normalized.get(secret_key) and old.get(secret_key):
                        normalized[secret_key] = old.get(secret_key)
            else:
                imported += 1
            normalized["updated_at"] = now
            normalized = _normalize(normalized) or normalized
            existing[key] = normalized
            changed_emails.append(str(normalized.get("email") or ""))
            if normalized.get("inventory_id"):
                changed_account_ids.append(str(normalized.get("inventory_id") or ""))
        _save(list(existing.values()))
    result = account_pool()
    result.update({"imported": imported, "updated": updated, "skipped": skipped, "parsed": len(parsed), "changed_emails": changed_emails, "changed_account_ids": changed_account_ids})
    return result
