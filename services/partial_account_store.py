from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from services.atomic_json_store import read_json, write_json
from services.account_token_profile import token_profile
from services.config import DATA_DIR
from services.sqlite_store import collection_page, document_signature


PARTIAL_ACCOUNTS_FILE = DATA_DIR / "partial_accounts.json"
_lock = Lock()
_pool_cache_lock = Lock()
_pool_cache: dict[bool, tuple[tuple[str, int, int], float, dict[str, Any]]] = {}
OAUTH_PARTIAL_STATUSES = {"oauth_failed", "oauth_missing", "token_exchange_failed"}
ALIVE_HEALTH_STATUSES = {"ok", "alive", "healthy"}
TERMINAL_HEALTH_STATUSES = {
    "banned",
    "dead",
    "invalid",
    "disabled",
    "token_invalid",
    "token_expired",
    "auth_invalid",
    "refresh_token_expired",
    "refresh_token_invalid",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(value: object) -> str:
    return str(value or "").strip().lower()


def _parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _lifespan_seconds(registered_at: object, died_at: object = "") -> int | None:
    started = _parse_time(registered_at)
    if started is None:
        return None
    ended = _parse_time(died_at) or datetime.now(timezone.utc)
    return max(0, int((ended - started).total_seconds()))


def partial_account_id(item: dict[str, Any]) -> str:
    identity = _key(item.get("email"))
    if not identity:
        identity = str(item.get("id") or item.get("inventory_id") or "").strip().lower()
    if not identity:
        identity = json.dumps(item, ensure_ascii=False, sort_keys=True)[:200]
    return hashlib.sha256(identity.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _load() -> list[dict[str, Any]]:
    data = read_json(PARTIAL_ACCOUNTS_FILE, [], expected_schema_version=1)
    if isinstance(data, dict):
        data = data.get("items") or data.get("accounts") or data.get("data")
    return data if isinstance(data, list) else []


def _save(items: list[dict[str, Any]]) -> None:
    PARTIAL_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(PARTIAL_ACCOUNTS_FILE, items, schema_version=1)


def _source_signature() -> tuple[str, int, int]:
    return document_signature(PARTIAL_ACCOUNTS_FILE)


def _preview(value: object, keep: int = 6) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text[:keep] + "***"
    return f"{text[:keep]}...{text[-keep:]}"


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_oauth_partial(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").strip().lower()
    stage = str(item.get("stage") or "").strip().lower()
    return bool(item.get("web_login_ok")) and (status in OAUTH_PARTIAL_STATUSES or stage in OAUTH_PARTIAL_STATUSES)


def _walk_import_accounts(data: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            result.extend(_walk_import_accounts(item))
        return result
    if not isinstance(data, dict):
        return result
    credentials = data.get("credentials")
    if isinstance(credentials, str):
        try:
            credentials = json.loads(credentials)
        except Exception:
            credentials = {}
    if isinstance(credentials, dict):
        extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
        email = str(credentials.get("email") or extra.get("email") or data.get("name") or "").strip()
        if "@" in email:
            result.append({
                **extra,
                **credentials,
                "email": email,
                "source": str(extra.get("source") or "sub2api_import"),
            })
    if data.get("email") or data.get("address") or data.get("username"):
        result.append(data)
    for key in ("accounts", "items", "rows", "list", "data"):
        child = data.get(key)
        if isinstance(child, (dict, list)) and child is not data:
            result.extend(_walk_import_accounts(child))
    return result


def _parse_import_line(line: str) -> dict[str, Any] | None:
    text = str(line or "").strip()
    if not text or "@" not in text:
        return None
    if "----" in text:
        parts = [part.strip() for part in text.split("----")]
    elif "|" in text:
        parts = [part.strip() for part in text.split("|")]
    elif "," in text and text.count(",") <= 6:
        parts = [part.strip() for part in text.split(",")]
    else:
        parts = [text]
    return {
        "email": parts[0],
        "password": parts[1] if len(parts) > 1 else "",
        "refresh_token": parts[2] if len(parts) > 2 else "",
        "access_token": parts[3] if len(parts) > 3 else "",
        "id_token": parts[4] if len(parts) > 4 else "",
    }


def _normalize(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    email = str(item.get("email") or item.get("address") or "").strip()
    if "@" not in email:
        return None
    now = _now()
    registered_at = str(item.get("registered_at") or item.get("registration_time") or item.get("created_at") or now)
    base: dict[str, Any] = {
        "email": email,
        "password": str(item.get("password") or ""),
        "status": str(item.get("status") or "oauth_failed").strip().lower(),
        "stage": str(item.get("stage") or item.get("status") or "oauth_failed").strip().lower(),
        "reason": str(item.get("reason") or item.get("error") or ""),
        "source": str(item.get("source") or "register"),
        "register_mode": str(item.get("register_mode") or ""),
        "product_mode": str(item.get("product_mode") or ""),
        "mailbox_group": str(item.get("mailbox_group") or item.get("group") or ""),
        "proxy": str(item.get("proxy") or ""),
        "provider": str(item.get("provider") or ""),
        "provider_label": str(item.get("provider_label") or item.get("label") or ""),
        "inbox_address": str(item.get("inbox_address") or ""),
        "access_token": str(item.get("access_token") or item.get("accessToken") or item.get("token") or ""),
        "refresh_token": str(item.get("refresh_token") or item.get("refreshToken") or ""),
        "id_token": str(item.get("id_token") or item.get("idToken") or ""),
        "session_token": str(item.get("session_token") or item.get("sessionToken") or item.get("session") or ""),
        "created_at": str(item.get("created_at") or now),
        "registered_at": registered_at,
        "updated_at": str(item.get("updated_at") or now),
        "last_seen_at": str(item.get("last_seen_at") or now),
        "attempt_count": int(item.get("attempt_count") or 1),
        "web_login_ok": _truthy(item.get("web_login_ok") if "web_login_ok" in item else item.get("chat_test_ok")),
        "registration_batch_id": str(item.get("registration_batch_id") or item.get("registration_batch") or ""),
        "registration_batch_label": str(item.get("registration_batch_label") or ""),
    }
    if not base["registration_batch_id"]:
        base["registration_batch_id"] = f"legacy-{registered_at[:10]}" if len(registered_at) >= 10 else "legacy-unknown"
    for key in (
        "name",
        "birthdate",
        "continue_url",
        "landing",
        "oauth_status",
        "oauth_error",
        "chat_test_method",
        "chat_test_error",
        "chat_test_status_code",
        "conversation_verified_at",
        "chatgpt_user_id",
        "chatgpt_account_id",
        "client_id",
        "note",
        "health_status",
        "health_checked_at",
        "health_method",
        "health_code",
        "health_latency_ms",
        "health_error",
        "health_proxy_source",
        "health_proxy_label",
        "health_proxy_node_id",
        "health_proxy_sticky",
        "last_health_attempt_status",
        "last_health_attempt_code",
        "last_health_attempt_error",
        "last_health_attempt_at",
        "registration_proxy",
        "registration_proxy_source",
        "registration_proxy_node_id",
        "registration_proxy_label",
        "last_health_result",
        "plan_type",
        "account_type",
        "quota_status",
        "quota_checked_at",
        "quota_source",
        "quota_used",
        "quota_total",
        "quota_remaining",
        "quota_reset_at",
        "quota_details",
        "last_quota_error",
        "quota_error_status",
        "oauth_refresh_status",
        "oauth_refresh_error",
        "oauth_refreshed_at",
        "token_refresh_required",
        "sub2api_imported_at",
        "sub2api_import_status",
        "sub2api_account_id",
        "sub2api_last_error",
        "cpa_imported_at",
        "cpa_import_status",
        "cpa_auth_file",
        "cpa_last_error",
        "token_expires_at",
        "token_expires_in",
        "died_at",
    ):
        if item.get(key) not in (None, ""):
            base[key] = item.get(key)
    health_status = str(base.get("health_status") or "unknown").strip().lower()
    if health_status in TERMINAL_HEALTH_STATUSES and not base.get("died_at"):
        base["died_at"] = str(base.get("health_checked_at") or base.get("updated_at") or now)
    if not _is_oauth_partial(base):
        return None
    base["id"] = str(item.get("id") or partial_account_id(base))
    return base


def _public(item: dict[str, Any], include_secret: bool = True) -> dict[str, Any]:
    out = dict(item)
    lifespan_seconds = _lifespan_seconds(out.get("registered_at") or out.get("created_at"), out.get("died_at"))
    if lifespan_seconds is not None:
        out["lifespan_seconds"] = lifespan_seconds
    out.update(token_profile(out))
    password = str(out.get("password") or "")
    out["has_password"] = bool(password)
    out["password_preview"] = _preview(password, 4)
    access_token = str(out.get("access_token") or "")
    refresh_token = str(out.get("refresh_token") or "")
    id_token = str(out.get("id_token") or "")
    session_token = str(out.get("session_token") or "")
    out["has_access_token"] = bool(access_token)
    out["access_token_preview"] = _preview(access_token, 6)
    out["has_refresh_token"] = bool(refresh_token)
    out["refresh_token_preview"] = _preview(refresh_token, 6)
    out["has_id_token"] = bool(id_token)
    out["id_token_preview"] = _preview(id_token, 6)
    out["has_session_token"] = bool(session_token)
    out["session_token_preview"] = _preview(session_token, 6)
    if not include_secret:
        out.pop("password", None)
        out.pop("access_token", None)
        out.pop("refresh_token", None)
        out.pop("id_token", None)
        out.pop("session_token", None)
    return out


def list_partial_accounts(include_secret: bool = True) -> list[dict[str, Any]]:
    with _lock:
        raw_items = _load()
        items = [_normalize(item) for item in raw_items]
        normalized = [item for item in items if item is not None]
        if len(normalized) != len(raw_items):
            normalized.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
            _save(normalized)
        return [_public(item, include_secret=include_secret) for item in normalized]


def select_partial_accounts(selectors: list[object] | None = None, *, include_secret: bool = True) -> list[dict[str, Any]]:
    items = list_partial_accounts(include_secret=include_secret)
    wanted = {_key(value) for value in selectors or [] if _key(value)}
    if not wanted:
        return items
    return [
        item
        for item in items
        if wanted & {_key(item.get("id")), _key(item.get("email"))}
    ]


def partial_account_pool(include_secret: bool = True) -> dict[str, Any]:
    signature = _source_signature()
    now = time.monotonic()
    with _pool_cache_lock:
        cached = _pool_cache.get(bool(include_secret))
        if cached and cached[0] == signature and cached[1] > now:
            return deepcopy(cached[2])
    items = list_partial_accounts(include_secret=include_secret)
    stats: dict[str, int] = {
        "total": len(items),
        "with_password": 0,
        "with_token": 0,
        "oauth_capable": 0,
        "health_alive": 0,
        "health_dead": 0,
        "health_unknown": 0,
    }
    for item in items:
        status = str(item.get("status") or "partial")
        stage = str(item.get("stage") or "partial")
        stats[status] = stats.get(status, 0) + 1
        stats[f"stage_{stage}"] = stats.get(f"stage_{stage}", 0) + 1
        if item.get("has_password") or item.get("password"):
            stats["with_password"] += 1
        if item.get("has_access_token") or item.get("has_refresh_token") or item.get("has_session_token"):
            stats["with_token"] += 1
        if item.get("oauth_capable"):
            stats["oauth_capable"] += 1
        health_status = str(item.get("health_status") or "unknown").strip().lower()
        if health_status in ALIVE_HEALTH_STATUSES:
            stats["health_alive"] += 1
        elif health_status in TERMINAL_HEALTH_STATUSES:
            stats["health_dead"] += 1
        else:
            stats["health_unknown"] += 1
    result = {"items": items, "stats": stats}
    with _pool_cache_lock:
        _pool_cache[bool(include_secret)] = (signature, now + 5.0, deepcopy(result))
    return result


def partial_account_pool_page(limit: int = 100, offset: int = 0, *, include_secret: bool = True) -> dict[str, Any]:
    page_limit = max(1, min(500, int(limit or 100)))
    page_offset = max(0, int(offset or 0))
    with _lock:
        page = collection_page(PARTIAL_ACCOUNTS_FILE, limit=page_limit, offset=page_offset)
        if page is None:
            raw_items = [item for item in _load() if isinstance(item, dict)]
            migrated_page = collection_page(PARTIAL_ACCOUNTS_FILE, limit=page_limit, offset=page_offset)
            if migrated_page is None:
                selected = raw_items[page_offset:page_offset + page_limit]
                total = len(raw_items)
            else:
                selected, total = migrated_page
        else:
            selected, total = page
    items = [_public(item, include_secret=include_secret) for item in (_normalize(raw) for raw in selected) if item]
    return {
        "items": items,
        "stats": {"total": total},
        "page_info": {
            "offset": page_offset,
            "limit": page_limit,
            "returned": len(items),
            "total": total,
            "partial": page_offset + len(items) < total,
        },
    }


def update_partial_account(selector: object, updates: dict[str, Any]) -> dict[str, Any] | None:
    text = str(selector or "").strip().lower()
    if not text or not isinstance(updates, dict):
        return None
    with _lock:
        items = [_normalize(entry) for entry in _load()]
        current = [entry for entry in items if entry is not None]
        updated: dict[str, Any] | None = None
        for index, old in enumerate(current):
            if text not in {str(old.get("id") or "").lower(), _key(old.get("email"))}:
                continue
            merged = {
                **old,
                **{key: value for key, value in updates.items() if value is not None},
                "id": str(old.get("id") or partial_account_id(old)),
                "created_at": str(old.get("created_at") or _now()),
                "updated_at": _now(),
                "last_seen_at": _now(),
            }
            normalized = _normalize(merged)
            if normalized is None:
                return None
            current[index] = normalized
            updated = normalized
            break
        if updated is None:
            return None
        current.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
        _save(current)
        return _public(updated, include_secret=True)


def upsert_partial_account(item: dict[str, Any]) -> dict[str, Any] | None:
    normalized = _normalize(item)
    if normalized is None:
        return None
    with _lock:
        items = [_normalize(entry) for entry in _load()]
        current = [entry for entry in items if entry is not None]
        normalized_id = str(normalized.get("id") or partial_account_id(normalized))
        email_key = _key(normalized.get("email"))
        replaced = False
        for index, old in enumerate(current):
            if str(old.get("id") or "") == normalized_id or _key(old.get("email")) == email_key:
                merged = {
                    **old,
                    **{key: value for key, value in normalized.items() if value not in (None, "")},
                    "id": str(old.get("id") or normalized_id),
                    "created_at": str(old.get("created_at") or normalized.get("created_at") or _now()),
                    "updated_at": _now(),
                    "last_seen_at": _now(),
                    "attempt_count": int(old.get("attempt_count") or 0) + 1,
                }
                current[index] = merged
                normalized = merged
                replaced = True
                break
        if not replaced:
            normalized["id"] = normalized_id
            current.append(normalized)
        current.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
        _save(current)
        return _public(normalized, include_secret=True)


def delete_partial_account(selector: object) -> dict[str, Any]:
    text = str(selector or "").strip().lower()
    with _lock:
        items = [_normalize(entry) for entry in _load()]
        current = [entry for entry in items if entry is not None]
        kept = [
            entry
            for entry in current
            if text not in {str(entry.get("id") or "").lower(), _key(entry.get("email"))}
        ]
        removed = len(current) - len(kept)
        _save(kept)
    pool = partial_account_pool()
    pool["removed"] = removed
    return pool


def delete_partial_accounts(selectors: list[object]) -> dict[str, Any]:
    wanted = {_key(value) for value in selectors or [] if _key(value)}
    if not wanted:
        pool = partial_account_pool()
        pool["removed"] = 0
        return pool
    with _lock:
        items = [_normalize(entry) for entry in _load()]
        current = [entry for entry in items if entry is not None]
        kept = [
            entry
            for entry in current
            if not (wanted & {_key(entry.get("id")), _key(entry.get("email"))})
        ]
        removed = len(current) - len(kept)
        _save(kept)
    pool = partial_account_pool()
    pool["removed"] = removed
    return pool


def prune_terminal_partial_accounts() -> dict[str, Any]:
    with _lock:
        items = [_normalize(entry) for entry in _load()]
        current = [entry for entry in items if entry is not None]
        kept = [
            entry
            for entry in current
            if _key(entry.get("health_status")) not in TERMINAL_HEALTH_STATUSES
        ]
        removed = len(current) - len(kept)
        _save(kept)
    pool = partial_account_pool()
    pool["removed"] = removed
    return pool


def import_partial_accounts(text: str, *, source: str = "manual_partial") -> dict[str, Any]:
    raw = str(text or "").strip()
    parsed: list[dict[str, Any]] = []
    if raw.startswith("[") or raw.startswith("{"):
        try:
            parsed.extend(_walk_import_accounts(json.loads(raw)))
        except Exception:
            parsed = []
    if not parsed:
        for line in raw.splitlines():
            item = _parse_import_line(line)
            if item:
                parsed.append(item)

    imported = 0
    updated = 0
    skipped = 0
    changed_selectors: list[str] = []
    with _lock:
        items = [_normalize(entry) for entry in _load()]
        current = [entry for entry in items if entry is not None]
        existing = {_key(item.get("email")): item for item in current}
        for item in parsed:
            candidate = dict(item)
            candidate.setdefault("status", "oauth_failed")
            candidate.setdefault("stage", candidate.get("status") or "oauth_failed")
            candidate.setdefault("web_login_ok", True)
            candidate.setdefault("source", source)
            candidate.setdefault("reason", "手动导入半成品库存")
            normalized = _normalize(candidate)
            if normalized is None:
                skipped += 1
                continue
            email_key = _key(normalized.get("email"))
            old = existing.get(email_key)
            if old:
                merged = {
                    **old,
                    **{key: value for key, value in normalized.items() if value not in (None, "")},
                    "id": str(old.get("id") or normalized.get("id") or partial_account_id(normalized)),
                    "created_at": str(old.get("created_at") or normalized.get("created_at") or _now()),
                    "updated_at": _now(),
                    "last_seen_at": _now(),
                }
                existing[email_key] = _normalize(merged) or merged
                updated += 1
            else:
                existing[email_key] = normalized
                imported += 1
            changed_selectors.append(str(existing[email_key].get("id") or existing[email_key].get("email") or ""))
        saved = list(existing.values())
        saved.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        _save(saved)

    pool = partial_account_pool()
    pool.update({
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "parsed": len(parsed),
        "changed_selectors": changed_selectors,
    })
    return pool


def clear_partial_accounts() -> dict[str, Any]:
    with _lock:
        count = len(_load())
        _save([])
    return {
        "items": [],
        "stats": {
            "total": 0,
            "with_password": 0,
            "with_token": 0,
            "oauth_capable": 0,
            "health_alive": 0,
            "health_dead": 0,
            "health_unknown": 0,
        },
        "removed": count,
    }
