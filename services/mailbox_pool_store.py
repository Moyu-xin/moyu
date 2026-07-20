
from __future__ import annotations

import json
import re
import time
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from services import task_proxy_service
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.register import mail_provider
from services.sqlite_store import collection_page, document_signature

MAILBOX_POOL_FILE = DATA_DIR / "mailbox_pool.json"
_lock = Lock()
_pool_cache_lock = Lock()
_pool_cache: dict[tuple[str, bool], tuple[tuple, float, dict[str, Any]]] = {}
DEFAULT_MAILBOX_GROUP = "默认"
ALL_GROUP_SENTINELS = {"", "all", "__all__", "全部", "*"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_signature() -> tuple:
    return (
        document_signature(MAILBOX_POOL_FILE),
        document_signature(mail_provider.OUTLOOK_TOKEN_USED_FILE),
    )


def _cached_pool(key: tuple[str, bool], signature: tuple) -> dict[str, Any] | None:
    with _pool_cache_lock:
        cached = _pool_cache.get(key)
        if cached and cached[0] == signature and cached[1] > time.monotonic():
            return deepcopy(cached[2])
    return None


def _store_cached_pool(key: tuple[str, bool], signature: tuple, result: dict[str, Any]) -> dict[str, Any]:
    with _pool_cache_lock:
        _pool_cache[key] = (signature, time.monotonic() + 5.0, deepcopy(result))
    return result


def _key(email: object) -> str:
    return str(email or "").strip().lower()


def _normalize_group(value: object, *, default: str = DEFAULT_MAILBOX_GROUP) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if text.lower() in {"all", "__all__", "*"} or text == "全部":
        return "" if default == "" else default
    return text[:80]


def _selected_group(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in ALL_GROUP_SENTINELS or text in ALL_GROUP_SENTINELS:
        return ""
    return text[:80]


def _group_matches(item: dict[str, Any], group: str) -> bool:
    selected = _selected_group(group)
    if not selected:
        return True
    return _normalize_group(item.get("group")) == selected


def _default_provider_configs() -> dict[str, Any]:
    return {
        "registration_source": "outlook_token",
        "outlook_token": {
            "mailbox_group": DEFAULT_MAILBOX_GROUP,
            "mode": "auto",
            "imap_host": "outlook.office365.com",
            "message_limit": 20,
            "split_aliases": True,
            "split_count_per_mailbox": 9999,
        },
        "cloudflare_temp_email": {
            "enable": False,
            "api_base": "",
            "admin_password": "",
            "domain": "",
        },
        "gmail_oauth": {
            "enable": True,
            "mode": "imap",
            "imap_host": "imap.gmail.com",
            "message_limit": 20,
            "split_aliases": True,
            "split_count_per_mailbox": 0,
        },
        "ddg_mail": {
            "enable": False,
            "ddg_token": "",
            "api_base": "",
            "cf_inbox_jwt": "",
            "admin_password": "",
            "cf_api_key": "",
            "cf_auth_mode": "none",
            "cf_domain": "",
            "cf_create_path": "/admin/new_address",
            "cf_messages_path": "/api/mails",
        },
        "duckmail": {
            "enable": False,
            "api_key": "",
            "default_domain": "duckmail.sbs",
        },
        "yyds_mail": {
            "enable": False,
            "api_base": "https://maliapi.215.im/v1",
            "api_key": "",
            "domain": "",
            "subdomain": "",
            "wildcard": False,
        },
        "openai_cpa": {
            "enable": False,
            "domains": "",
            "webhook_secret": "",
            "use_original_password_flow": False,
            "suffix_mode": "fixed",
            "suffix_len_min": 8,
            "suffix_len_max": 8,
            "enable_multi_level_subdomain": False,
            "multi_level_subdomain_levels": 3,
            "enable_domain_grouping": False,
            "domain_group_count": 2,
            "domain_group_strategy": "round_robin",
            "domain_fail_threshold": 3,
            "domain_fail_cooldown_sec": 600,
            "disabled_domains": "",
        },
    }


def _load_store() -> dict[str, Any]:
    data = read_json(MAILBOX_POOL_FILE, {}, expected_schema_version=1)
    if isinstance(data, list):
        data = {"items": data}
    if not isinstance(data, dict):
        data = {}
    return {
        "items": data.get("items") if isinstance(data.get("items"), list) else [],
        "provider_configs": _normalize_provider_configs(data.get("provider_configs")),
        "groups": _normalize_group_records(data.get("groups"), data.get("items")),
    }


def _normalize_group_records(raw: Any, raw_items: Any = None) -> list[dict[str, str]]:
    now = _now()
    records: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: object, created_at: object = "", updated_at: object = "") -> None:
        name = _normalize_group(value)
        key = name.lower()
        if not name or key in seen:
            return
        seen.add(key)
        records.append({
            "name": name,
            "created_at": str(created_at or now),
            "updated_at": str(updated_at or created_at or now),
        })

    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            add(item.get("name") or item.get("group"), item.get("created_at"), item.get("updated_at"))
        else:
            add(item)
    for item in raw_items if isinstance(raw_items, list) else []:
        if isinstance(item, dict):
            add(item.get("group") or item.get("mailbox_group"))
    add(DEFAULT_MAILBOX_GROUP)
    return sorted(records, key=lambda item: (item["name"] != DEFAULT_MAILBOX_GROUP, item["name"].lower()))


def _normalize_provider_configs(raw: Any) -> dict[str, Any]:
    configs = _default_provider_configs()
    raw = raw if isinstance(raw, dict) else {}
    allowed_sources = {"outlook_token", "gmail_oauth", "cloudflare_temp_email", "ddg_mail", "duckmail", "yyds_mail", "openai_cpa"}
    registration_source = str(raw.get("registration_source") or configs["registration_source"]).strip().lower()
    configs["registration_source"] = registration_source if registration_source in allowed_sources else "outlook_token"
    for key, defaults in [(key, value) for key, value in configs.items() if isinstance(value, dict)]:
        value = raw.get(key) if isinstance(raw.get(key), dict) else {}
        merged = {**defaults, **value}
        if key == "outlook_token":
            merged["mailbox_group"] = _normalize_group(merged.get("mailbox_group"))
            merged["mode"] = str(merged.get("mode") or "auto").strip().lower()
            if merged["mode"] not in {"auto", "graph", "imap"}:
                merged["mode"] = "auto"
            merged["imap_host"] = str(merged.get("imap_host") or "outlook.office365.com").strip() or "outlook.office365.com"
            merged["message_limit"] = max(1, min(50, int(merged.get("message_limit") or 20)))
            merged["split_aliases"] = bool(merged.get("split_aliases"))
            merged["split_count_per_mailbox"] = max(0, int(merged.get("split_count_per_mailbox") or 0))
        if key == "cloudflare_temp_email":
            merged["enable"] = bool(merged.get("enable"))
            merged["api_base"] = str(merged.get("api_base") or "").strip().rstrip("/")
            merged["admin_password"] = str(merged.get("admin_password") or "").strip()
            merged["domain"] = str(merged.get("domain") or "").strip()
        if key == "gmail_oauth":
            merged["enable"] = bool(merged.get("enable", True))
            merged["mode"] = str(merged.get("mode") or "imap").strip().lower() or "imap"
            merged["imap_host"] = str(merged.get("imap_host") or "imap.gmail.com").strip() or "imap.gmail.com"
            merged["message_limit"] = max(1, int(merged.get("message_limit") or 20))
            merged["split_aliases"] = bool(merged.get("split_aliases"))
            merged["split_count_per_mailbox"] = max(0, int(merged.get("split_count_per_mailbox") or 0))
        if key == "ddg_mail":
            merged["enable"] = bool(merged.get("enable"))
            merged["ddg_token"] = str(merged.get("ddg_token") or "").strip()
            merged["api_base"] = str(merged.get("api_base") or "").strip().rstrip("/")
            merged["cf_inbox_jwt"] = str(merged.get("cf_inbox_jwt") or "").strip()
            merged["admin_password"] = str(merged.get("admin_password") or "").strip()
            merged["cf_api_key"] = str(merged.get("cf_api_key") or "").strip()
            merged["cf_auth_mode"] = str(merged.get("cf_auth_mode") or "none").strip().lower() or "none"
            merged["cf_domain"] = str(merged.get("cf_domain") or "").strip()
            merged["cf_create_path"] = str(merged.get("cf_create_path") or "/admin/new_address").strip() or "/admin/new_address"
            merged["cf_messages_path"] = str(merged.get("cf_messages_path") or "/api/mails").strip() or "/api/mails"
        if key == "duckmail":
            merged["enable"] = bool(merged.get("enable"))
            merged["api_key"] = str(merged.get("api_key") or "").strip()
            merged["default_domain"] = str(merged.get("default_domain") or "duckmail.sbs").strip() or "duckmail.sbs"
        if key == "yyds_mail":
            merged["enable"] = bool(merged.get("enable"))
            merged["api_base"] = str(merged.get("api_base") or "https://maliapi.215.im/v1").strip().rstrip("/") or "https://maliapi.215.im/v1"
            merged["api_key"] = str(merged.get("api_key") or "").strip()
            merged["domain"] = str(merged.get("domain") or "").strip()
            merged["subdomain"] = str(merged.get("subdomain") or "").strip()
            merged["wildcard"] = bool(merged.get("wildcard"))
        if key == "openai_cpa":
            merged["enable"] = bool(merged.get("enable"))
            merged["domains"] = str(merged.get("domains") or "").strip()
            merged["webhook_secret"] = str(merged.get("webhook_secret") or "").strip()
            merged["use_original_password_flow"] = bool(merged.get("use_original_password_flow"))
            merged["suffix_mode"] = str(merged.get("suffix_mode") or "fixed").strip() or "fixed"
            merged["suffix_len_min"] = max(1, int(merged.get("suffix_len_min") or 8))
            merged["suffix_len_max"] = max(merged["suffix_len_min"], int(merged.get("suffix_len_max") or merged["suffix_len_min"]))
            merged["enable_multi_level_subdomain"] = bool(merged.get("enable_multi_level_subdomain"))
            merged["multi_level_subdomain_levels"] = max(2, min(7, int(merged.get("multi_level_subdomain_levels") or 3)))
            merged["enable_domain_grouping"] = bool(merged.get("enable_domain_grouping"))
            merged["domain_group_count"] = max(1, int(merged.get("domain_group_count") or 2))
            merged["domain_group_strategy"] = str(merged.get("domain_group_strategy") or "round_robin").strip().lower() or "round_robin"
            if merged["domain_group_strategy"] not in {"round_robin", "exhaust_then_next"}:
                merged["domain_group_strategy"] = "round_robin"
            merged["domain_fail_threshold"] = max(1, int(merged.get("domain_fail_threshold") or 3))
            merged["domain_fail_cooldown_sec"] = max(10, int(merged.get("domain_fail_cooldown_sec") or 600))
            merged["disabled_domains"] = str(merged.get("disabled_domains") or "").strip()
        configs[key] = merged
    return configs


def _load_items(store: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    store = store if isinstance(store, dict) else _load_store()
    data = store.get("items") if isinstance(store.get("items"), list) else []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        if not isinstance(raw, dict):
            continue
        email = str(raw.get("email") or raw.get("address") or "").strip()
        if "@" not in email:
            continue
        provider_type = str(raw.get("provider_type") or raw.get("type") or "outlook_token").strip() or "outlook_token"
        normalized = f"{provider_type}:{email.lower()}"
        if normalized in seen:
            continue
        seen.add(normalized)
        items.append({
            "provider_type": provider_type,
            "email": email,
            "group": _normalize_group(raw.get("group") or raw.get("mailbox_group") or raw.get("tag") or raw.get("note_group")),
            "password": str(raw.get("password") or ""),
            "client_id": str(raw.get("client_id") or ""),
            "client_secret": str(raw.get("client_secret") or ""),
            "refresh_token": str(raw.get("refresh_token") or ""),
            "token_refreshed_at": str(raw.get("token_refreshed_at") or ""),
            "token_refresh_status": str(raw.get("token_refresh_status") or ""),
            "token_refresh_error": str(raw.get("token_refresh_error") or ""),
            "token_refresh_mode": str(raw.get("token_refresh_mode") or ""),
            "note": str(raw.get("note") or ""),
            "created_at": str(raw.get("created_at") or _now()),
            "updated_at": str(raw.get("updated_at") or _now()),
            "inventory_batch_id": str(raw.get("inventory_batch_id") or raw.get("import_batch_id") or ""),
            "outbound_at": str(raw.get("outbound_at") or ""),
            "outbound_batch_id": str(raw.get("outbound_batch_id") or ""),
            "outbound_format": str(raw.get("outbound_format") or ""),
        })
    return items


def _save_store(items: list[dict[str, Any]], provider_configs: dict[str, Any] | None = None, groups: list[dict[str, Any]] | list[str] | None = None) -> None:
    MAILBOX_POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    current = _load_store()
    configs = _normalize_provider_configs(provider_configs if provider_configs is not None else current.get("provider_configs"))
    normalized_items = []
    for item in items:
        normalized_items.append({**item, "group": _normalize_group(item.get("group"))})
    ordered = sorted(normalized_items, key=lambda item: (str(item.get("provider_type") or ""), _normalize_group(item.get("group")), _key(item.get("email"))))
    group_records = _normalize_group_records(groups if groups is not None else current.get("groups"), ordered)
    write_json(MAILBOX_POOL_FILE, {"items": ordered, "provider_configs": configs, "groups": group_records}, schema_version=1)


def _save_items(items: list[dict[str, Any]]) -> None:
    _save_store(items)


def _state_map() -> dict[str, dict[str, Any]]:
    try:
        return mail_provider.outlook_token_pool_state()
    except AttributeError:
        try:
            return mail_provider._load_outlook_token_state()  # type: ignore[attr-defined]
        except Exception:
            return {}


def _line(item: dict[str, Any]) -> str:
    provider_type = str(item.get("provider_type") or "outlook_token")
    if provider_type == "gmail_oauth":
        return "----".join([
            str(item.get("email") or "").strip(),
            str(item.get("password") or ""),
            str(item.get("client_id") or ""),
            str(item.get("client_secret") or ""),
            str(item.get("refresh_token") or ""),
        ])
    return "----".join([
        str(item.get("email") or "").strip(),
        str(item.get("password") or ""),
        str(item.get("client_id") or ""),
        str(item.get("refresh_token") or ""),
    ])


def _mailbox_batch_id(item: dict[str, Any]) -> str:
    explicit = str(item.get("inventory_batch_id") or item.get("import_batch_id") or "").strip()
    if explicit:
        return explicit
    created = str(item.get("created_at") or "").strip()
    return f"legacy-{created[:10]}" if len(created) >= 10 else "legacy-unknown"


def _split_alias_counts(states: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for address, entry in states.items():
        local, separator, domain = str(address or "").strip().lower().partition("@")
        if not separator or "+" not in local:
            continue
        base = f"{local.split('+', 1)[0]}@{domain}"
        bucket = counts.setdefault(base, {"generated": 0, "used": 0, "failed": 0, "in_use": 0})
        bucket["generated"] += 1
        state = str((entry or {}).get("state") or "")
        if state == "in_use":
            bucket["in_use"] += 1
        elif state in {"used", "existing_account"}:
            bucket["used"] += 1
        elif state:
            bucket["failed"] += 1
    return counts


def _with_state(
    item: dict[str, Any],
    states: dict[str, dict[str, Any]] | None = None,
    alias_counts: dict[str, dict[str, int]] | None = None,
    *,
    include_secret: bool = True,
) -> dict[str, Any]:
    states = states or _state_map()
    alias_counts = alias_counts if alias_counts is not None else _split_alias_counts(states)
    entry = states.get(_key(item.get("email"))) or {}
    state = str(entry.get("state") or "unused")
    split_counts = alias_counts.get(_base_address(str(item.get("email") or "")), {})
    result = {
        "provider_type": item.get("provider_type") or "outlook_token",
        "email": item.get("email") or "",
        "group": _normalize_group(item.get("group")),
        "state": state,
        "state_reason": str(entry.get("reason") or ""),
        "state_updated_at": str(entry.get("updated_at") or ""),
        "note": item.get("note") or "",
        "created_at": item.get("created_at") or "",
        "updated_at": item.get("updated_at") or "",
        "inventory_batch_id": item.get("inventory_batch_id") or _mailbox_batch_id(item),
        "outbound_at": item.get("outbound_at") or "",
        "outbound_batch_id": item.get("outbound_batch_id") or "",
        "outbound_format": item.get("outbound_format") or "",
        "split_alias_generated": int(split_counts.get("generated") or 0),
        "split_alias_used": int(split_counts.get("used") or 0),
        "split_alias_failed": int(split_counts.get("failed") or 0),
        "split_alias_in_use": int(split_counts.get("in_use") or 0),
        "client_id": item.get("client_id") or "",
        "has_client_secret": bool(str(item.get("client_secret") or "")),
        "client_secret_preview": _preview(item.get("client_secret")),
        "has_refresh_token": bool(str(item.get("refresh_token") or "")),
        "refresh_token_preview": _preview(item.get("refresh_token")),
        "token_refreshed_at": item.get("token_refreshed_at") or "",
        "token_refresh_status": item.get("token_refresh_status") or "",
        "token_refresh_error": item.get("token_refresh_error") or "",
        "token_refresh_mode": item.get("token_refresh_mode") or "",
    }
    if include_secret:
        result.update({
            "password": item.get("password") or "",
            "client_secret": item.get("client_secret") or "",
            "refresh_token": item.get("refresh_token") or "",
            "line": _line(item),
        })
    return result


def _preview(value: object, keep: int = 8) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}...{text[-keep:]}"


def _stats(items: list[dict[str, Any]], states: dict[str, dict[str, Any]] | None = None) -> dict[str, int]:
    states = states if states is not None else _state_map()
    counts = {
        "total": len(items),
        "outlook_token": 0,
        "gmail_oauth": 0,
        "ddg_mail": 0,
        "duckmail": 0,
        "openai_cpa": 0,
        "unused": 0,
        "in_use": 0,
        "used": 0,
        "token_invalid": 0,
        "failed": 0,
        "region_blocked": 0,
        "existing_account": 0,
        "flow_blocked": 0,
        "cloudflare_blocked": 0,
    }
    for item in items:
        provider_type = str(item.get("provider_type") or "outlook_token")
        counts[provider_type] = counts.get(provider_type, 0) + 1
        state = str((states.get(_key(item.get("email"))) or {}).get("state") or "unused")
        if state not in counts:
            state = "unused"
        counts[state] += 1
    return counts


def _groups(items: list[dict[str, Any]], records: Any = None) -> list[str]:
    seen = {_normalize_group(item.get("group")) for item in items}
    for record in records if isinstance(records, list) else []:
        seen.add(_normalize_group(record.get("name") if isinstance(record, dict) else record))
    return sorted(seen, key=lambda value: (value != DEFAULT_MAILBOX_GROUP, value.lower()))


def _group_records_with_counts(
    items: list[dict[str, Any]],
    records: Any = None,
    group_stats: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    counts = group_stats if group_stats is not None else _group_stats(items)
    normalized = _normalize_group_records(records, items)
    return [{**record, "count": int((counts.get(record["name"]) or {}).get("total") or 0)} for record in normalized]


def _group_stats(
    items: list[dict[str, Any]],
    states: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, int]]:
    states = states if states is not None else _state_map()
    result: dict[str, dict[str, int]] = {}
    for item in items:
        group = _normalize_group(item.get("group"))
        bucket = result.setdefault(group, {"total": 0})
        bucket["total"] += 1
        provider_type = str(item.get("provider_type") or "outlook_token")
        bucket[provider_type] = bucket.get(provider_type, 0) + 1
        state = str((states.get(_key(item.get("email"))) or {}).get("state") or "unused")
        bucket[state] = bucket.get(state, 0) + 1
    return result


def _public_provider_configs(configs: dict[str, Any], *, include_secret: bool = False) -> dict[str, Any]:
    data = json.loads(json.dumps(_normalize_provider_configs(configs), ensure_ascii=False))
    cf = data.get("cloudflare_temp_email") if isinstance(data.get("cloudflare_temp_email"), dict) else {}
    if cf.get("admin_password"):
        cf["has_admin_password"] = True
        cf["admin_password_preview"] = _preview(cf.get("admin_password"), keep=4)
        if not include_secret:
            cf.pop("admin_password", None)
    else:
        cf["has_admin_password"] = False
        cf["admin_password_preview"] = ""
    for section, keys in {
        "ddg_mail": ("ddg_token", "cf_inbox_jwt", "admin_password", "cf_api_key"),
        "duckmail": ("api_key",),
        "yyds_mail": ("api_key",),
        "openai_cpa": ("webhook_secret",),
    }.items():
        cfg = data.get(section) if isinstance(data.get(section), dict) else {}
        for key in keys:
            value = str(cfg.get(key) or "")
            cfg[f"has_{key}"] = bool(value)
            cfg[f"{key}_preview"] = _preview(value, keep=4)
            if value and not include_secret:
                cfg.pop(key, None)
    return data


def get_provider_configs(*, include_secret: bool = False) -> dict[str, Any]:
    with _lock:
        return _public_provider_configs(_load_store().get("provider_configs") or {}, include_secret=include_secret)


def update_provider_configs(updates: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        store = _load_store()
        configs = _normalize_provider_configs(store.get("provider_configs"))
        if "registration_source" in updates:
            configs["registration_source"] = str(updates.get("registration_source") or "outlook_token").strip().lower()
        secret_keys = {
            "cloudflare_temp_email": {"admin_password"},
            "ddg_mail": {"ddg_token", "cf_inbox_jwt", "admin_password", "cf_api_key"},
            "duckmail": {"api_key"},
            "yyds_mail": {"api_key"},
            "openai_cpa": {"webhook_secret"},
        }
        for key in ("outlook_token", "cloudflare_temp_email", "gmail_oauth", "ddg_mail", "duckmail", "yyds_mail", "openai_cpa"):
            if isinstance(updates.get(key), dict):
                current = dict(configs.get(key) or {})
                for update_key, update_value in updates[key].items():
                    if update_key in secret_keys.get(key, set()) and not str(update_value or "").strip():
                        continue
                    current[update_key] = update_value
                configs[key] = current
        _save_store(_load_items(store), _normalize_provider_configs(configs), store.get("groups"))
    return get_provider_configs()


def list_mailboxes(*, include_secret: bool = True) -> dict[str, Any]:
    signature = _source_signature()
    cache_key = ("all", bool(include_secret))
    cached = _cached_pool(cache_key, signature)
    if cached is not None:
        return cached
    with _lock:
        store = _load_store()
        items = _load_items(store)
        configs = store.get("provider_configs") or {}
    states = _state_map()
    alias_counts = _split_alias_counts(states)
    group_stats = _group_stats(items, states)
    result = {
        "items": [_with_state(item, states, alias_counts, include_secret=include_secret) for item in items],
        "stats": _stats(items, states),
        "groups": _groups(items, store.get("groups")),
        "group_records": _group_records_with_counts(items, store.get("groups"), group_stats),
        "group_stats": group_stats,
        "default_group": DEFAULT_MAILBOX_GROUP,
        "provider_configs": _public_provider_configs(configs, include_secret=False),
    }
    return _store_cached_pool(cache_key, signature, result)


def _parse_gmail_credentials(text: str) -> list[dict[str, str]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    parsed: list[dict[str, str]] = []
    if raw.startswith("[") or raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = data.get("items") or data.get("mailboxes") or [data]
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    email = str(item.get("email") or item.get("address") or "").strip()
                    client_id = str(item.get("client_id") or "").strip()
                    client_secret = str(item.get("client_secret") or "").strip()
                    refresh_token = str(item.get("refresh_token") or "").strip()
                    if "@" in email and client_id and refresh_token:
                        parsed.append({"email": email, "password": str(item.get("password") or ""), "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token})
        except Exception:
            pass
    if parsed:
        return parsed
    for line in raw.splitlines():
        line = line.strip()
        if not line or "@" not in line:
            continue
        parts = [part.strip() for part in re.split(r"\s*----\s*", line)]
        if len(parts) >= 5:
            email, password, client_id, client_secret, refresh_token = parts[:5]
        elif len(parts) >= 4:
            email, client_id, client_secret, refresh_token = parts[:4]
            password = ""
        else:
            continue
        if "@" in email and client_id and refresh_token:
            parsed.append({"email": email, "password": password, "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token})
    return parsed


def import_mailbox_text(text: str, *, provider_type: str = "outlook_token", note: str = "", group: str = DEFAULT_MAILBOX_GROUP) -> dict[str, Any]:
    provider_type = str(provider_type or "outlook_token").strip() or "outlook_token"
    group = _normalize_group(group)
    if provider_type == "gmail_oauth":
        credentials = _parse_gmail_credentials(text)
    else:
        provider_type = "outlook_token"
        credentials = mail_provider.parse_outlook_credentials(text)
    with _lock:
        existing = {f"{item.get('provider_type')}:{_key(item.get('email'))}": item for item in _load_items()}
        imported = 0
        updated = 0
        now = _now()
        import_batch_id = f"mail-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        for credential in credentials:
            email_key = f"{provider_type}:{_key(credential.get('email'))}"
            if not _key(credential.get("email")):
                continue
            old = existing.get(email_key)
            if old:
                updated += 1
            else:
                imported += 1
            existing[email_key] = {
                "provider_type": provider_type,
                "email": credential["email"],
                "group": group or _normalize_group((old or {}).get("group")),
                "password": credential.get("password", ""),
                "client_id": credential.get("client_id", ""),
                "client_secret": credential.get("client_secret", ""),
                "refresh_token": credential.get("refresh_token", ""),
                "note": note or (old or {}).get("note", ""),
                "created_at": (old or {}).get("created_at") or now,
                "updated_at": now,
                "inventory_batch_id": (old or {}).get("inventory_batch_id") or import_batch_id,
                "outbound_at": (old or {}).get("outbound_at") or "",
                "outbound_batch_id": (old or {}).get("outbound_batch_id") or "",
                "outbound_format": (old or {}).get("outbound_format") or "",
            }
        items = list(existing.values())
        _save_items(items)
    result = list_mailboxes()
    result.update({"imported": imported, "updated": updated, "skipped": max(0, len(str(text or '').splitlines()) - imported - updated)})
    return result


def import_outlook_text(text: str, *, note: str = "", group: str = DEFAULT_MAILBOX_GROUP) -> dict[str, Any]:
    return import_mailbox_text(text, provider_type="outlook_token", note=note, group=group)


def import_from_register_config(register_config: dict[str, Any]) -> int:
    mail = register_config.get("mail") if isinstance(register_config.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    merged_outlook: list[tuple[str, str]] = []
    merged_gmail: list[tuple[str, str]] = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        text = str(provider.get("mailboxes") or "")
        if not text.strip():
            continue
        group = _normalize_group(provider.get("mailbox_group") or provider.get("group") or mail.get("mailbox_group"))
        if provider.get("type") == "outlook_token":
            merged_outlook.append((text, group))
        elif provider.get("type") == "gmail_oauth":
            merged_gmail.append((text, group))
    total = 0
    for text, group in merged_outlook:
        result = import_mailbox_text(text, provider_type="outlook_token", note="migrated from register config", group=group)
        total += int(result.get("imported") or 0) + int(result.get("updated") or 0)
    for text, group in merged_gmail:
        result = import_mailbox_text(text, provider_type="gmail_oauth", note="migrated from register config", group=group)
        total += int(result.get("imported") or 0) + int(result.get("updated") or 0)
    return total


def serialize_pool(provider_type: str, group: str = "") -> str:
    selected_group = _selected_group(group)
    with _lock:
        items = [
            item
            for item in _load_items()
            if str(item.get("provider_type") or "outlook_token") == provider_type and _group_matches(item, selected_group)
        ]
    return "\n".join(_line(item) for item in items)


def serialize_outlook_pool(group: str = "") -> str:
    return serialize_pool("outlook_token", group=group)


def serialize_gmail_pool(group: str = "") -> str:
    return serialize_pool("gmail_oauth", group=group)


def mailbox_batches(*, provider_type: str = "") -> list[dict[str, Any]]:
    selected_provider = str(provider_type or "").strip().lower()
    with _lock:
        items = _load_items()
    batches: dict[str, dict[str, Any]] = {}
    for item in items:
        if selected_provider and str(item.get("provider_type") or "").lower() != selected_provider:
            continue
        batch_id = _mailbox_batch_id(item)
        bucket = batches.setdefault(batch_id, {
            "id": batch_id,
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


def select_mailboxes_for_outbound(
    *,
    quantity: int = 0,
    inventory_batch_id: str = "",
    provider_type: str = "outlook_token",
    include_outbound: bool = False,
) -> list[dict[str, Any]]:
    batch_id = str(inventory_batch_id or "").strip()
    selected_provider = str(provider_type or "").strip().lower()
    with _lock:
        items = _load_items()
    selected = [
        item for item in items
        if (not selected_provider or str(item.get("provider_type") or "").lower() == selected_provider)
        and (not batch_id or _mailbox_batch_id(item) == batch_id)
        and (include_outbound or not str(item.get("outbound_at") or "").strip())
    ]
    selected.sort(key=lambda item: str(item.get("created_at") or ""))
    limit = max(0, int(quantity or 0))
    return selected[:limit] if limit else selected


def mark_mailboxes_outbound(items: list[dict[str, Any]], *, export_format: str) -> dict[str, Any]:
    identities = {
        f"{str(item.get('provider_type') or '').lower()}:{_key(item.get('email'))}"
        for item in items
        if _key(item.get("email"))
    }
    if not identities:
        return {"marked": 0, "outbound_batch_id": "", "outbound_at": ""}
    outbound_at = _now()
    outbound_batch_id = f"mail-out-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    marked = 0
    with _lock:
        stored = _load_items()
        for item in stored:
            identity = f"{str(item.get('provider_type') or '').lower()}:{_key(item.get('email'))}"
            if identity not in identities:
                continue
            item.update({
                "outbound_at": outbound_at,
                "outbound_batch_id": outbound_batch_id,
                "outbound_format": str(export_format or "credentials").strip().lower(),
                "updated_at": outbound_at,
            })
            marked += 1
        _save_items(stored)
    return {
        "marked": marked,
        "outbound_batch_id": outbound_batch_id,
        "outbound_at": outbound_at,
    }


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").replace("\n", ",").split(",") if item.strip()]


def apply_to_register_config(register_config: dict[str, Any]) -> dict[str, Any]:
    """把邮箱池与邮箱服务配置写入注册运行配置。"""
    cfg = json.loads(json.dumps(register_config, ensure_ascii=False))
    configs = get_provider_configs(include_secret=True)
    mail = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    registration_source = str(configs.get("registration_source") or "outlook_token").strip().lower()
    outlook_cfg = configs.get("outlook_token") if isinstance(configs.get("outlook_token"), dict) else {}
    selected_group = _selected_group(outlook_cfg.get("mailbox_group")) if registration_source == "outlook_token" else ""
    cfg["mailbox_group"] = selected_group
    mail["mailbox_group"] = selected_group
    mail["registration_source"] = registration_source
    providers: list[dict[str, Any]] = []

    cpa_cfg = configs.get("openai_cpa") if isinstance(configs.get("openai_cpa"), dict) else {}
    cf_cfg = configs.get("cloudflare_temp_email") if isinstance(configs.get("cloudflare_temp_email"), dict) else {}
    ddg_cfg = configs.get("ddg_mail") if isinstance(configs.get("ddg_mail"), dict) else {}
    duck_cfg = configs.get("duckmail") if isinstance(configs.get("duckmail"), dict) else {}
    yyds_cfg = configs.get("yyds_mail") if isinstance(configs.get("yyds_mail"), dict) else {}
    gmail_cfg = configs.get("gmail_oauth") if isinstance(configs.get("gmail_oauth"), dict) else {}

    if registration_source == "outlook_token":
        outlook_text = serialize_outlook_pool(selected_group)
        if outlook_text.strip():
            providers.append({
                "enable": True,
                "type": "outlook_token",
                "mailbox_group": selected_group,
                "mode": str(outlook_cfg.get("mode") or "auto"),
                "imap_host": str(outlook_cfg.get("imap_host") or "outlook.office365.com"),
                "message_limit": int(outlook_cfg.get("message_limit") or 20),
                "split_aliases": bool(outlook_cfg.get("split_aliases", True)),
                "split_count_per_mailbox": int(outlook_cfg.get("split_count_per_mailbox") or 0),
                "mailboxes": outlook_text,
            })
    elif registration_source == "gmail_oauth" and bool(gmail_cfg.get("enable", True)):
        gmail_text = serialize_gmail_pool()
        if gmail_text.strip():
            providers.append({
                "enable": True,
                "type": "gmail_oauth",
                "mailbox_group": "",
                "mode": str(gmail_cfg.get("mode") or "imap"),
                "imap_host": str(gmail_cfg.get("imap_host") or "imap.gmail.com"),
                "message_limit": int(gmail_cfg.get("message_limit") or 20),
                "split_aliases": bool(gmail_cfg.get("split_aliases")),
                "split_count_per_mailbox": int(gmail_cfg.get("split_count_per_mailbox") or 0),
                "mailboxes": gmail_text,
            })
    elif registration_source == "cloudflare_temp_email" and bool(cf_cfg.get("enable")) and cf_cfg.get("api_base") and cf_cfg.get("admin_password"):
        providers.append({
            "enable": True,
            "type": "cloudflare_temp_email",
            "mailbox_group": "",
            "api_base": str(cf_cfg.get("api_base") or ""),
            "admin_password": str(cf_cfg.get("admin_password") or ""),
            "domain": _split_csv(cf_cfg.get("domain")),
        })
    elif registration_source == "ddg_mail" and bool(ddg_cfg.get("enable")) and ddg_cfg.get("ddg_token") and ddg_cfg.get("api_base") and ddg_cfg.get("cf_inbox_jwt"):
        providers.append({
            "enable": True,
            "type": "ddg_mail",
            "mailbox_group": "",
            "ddg_token": str(ddg_cfg.get("ddg_token") or ""),
            "api_base": str(ddg_cfg.get("api_base") or ""),
            "cf_inbox_jwt": str(ddg_cfg.get("cf_inbox_jwt") or ""),
            "admin_password": str(ddg_cfg.get("admin_password") or ""),
            "cf_api_key": str(ddg_cfg.get("cf_api_key") or ""),
            "cf_auth_mode": str(ddg_cfg.get("cf_auth_mode") or "none"),
            "cf_domain": _split_csv(ddg_cfg.get("cf_domain")),
            "cf_create_path": str(ddg_cfg.get("cf_create_path") or "/admin/new_address"),
            "cf_messages_path": str(ddg_cfg.get("cf_messages_path") or "/api/mails"),
        })
    elif registration_source == "duckmail" and bool(duck_cfg.get("enable")) and duck_cfg.get("api_key"):
        providers.append({
            "enable": True,
            "type": "duckmail",
            "mailbox_group": "",
            "api_key": str(duck_cfg.get("api_key") or ""),
            "default_domain": str(duck_cfg.get("default_domain") or "duckmail.sbs"),
        })
    elif registration_source == "yyds_mail" and bool(yyds_cfg.get("enable")) and yyds_cfg.get("api_key"):
        providers.append({
            "enable": True,
            "type": "yyds_mail",
            "mailbox_group": "",
            "api_base": str(yyds_cfg.get("api_base") or "https://maliapi.215.im/v1"),
            "api_key": str(yyds_cfg.get("api_key") or ""),
            "domain": _split_csv(yyds_cfg.get("domain")),
            "subdomain": str(yyds_cfg.get("subdomain") or ""),
            "wildcard": bool(yyds_cfg.get("wildcard")),
        })
    elif registration_source == "openai_cpa" and bool(cpa_cfg.get("enable")) and cpa_cfg.get("domains") and cpa_cfg.get("webhook_secret"):
        all_domains = _split_csv(cpa_cfg.get("domains"))
        disabled = {d.strip().lower() for d in _split_csv(cpa_cfg.get("disabled_domains"))}
        active_domains = [d for d in all_domains if d.strip().lower() not in disabled]
        if active_domains:
            providers.append({
                "enable": True,
                "type": "openai_cpa",
                "mailbox_group": "",
                "domains": active_domains,
                "webhook_secret": str(cpa_cfg.get("webhook_secret") or ""),
                "use_original_password_flow": bool(cpa_cfg.get("use_original_password_flow")),
                "suffix_mode": str(cpa_cfg.get("suffix_mode") or "fixed"),
                "suffix_len_min": int(cpa_cfg.get("suffix_len_min") or 8),
                "suffix_len_max": int(cpa_cfg.get("suffix_len_max") or 8),
                "enable_multi_level_subdomain": bool(cpa_cfg.get("enable_multi_level_subdomain")),
                "multi_level_subdomain_levels": max(2, min(7, int(cpa_cfg.get("multi_level_subdomain_levels") or 3))),
                "enable_domain_grouping": bool(cpa_cfg.get("enable_domain_grouping")),
                "domain_group_count": int(cpa_cfg.get("domain_group_count") or 2),
                "domain_group_strategy": str(cpa_cfg.get("domain_group_strategy") or "round_robin"),
                "disabled_domains": str(cpa_cfg.get("disabled_domains") or ""),
            })
    mail["providers"] = providers
    cfg["mail"] = mail
    return cfg


def delete_mailbox(email: str, *, remove_state: bool = True, provider_type: str = "") -> dict[str, Any]:
    target = _key(email)
    removed = 0
    with _lock:
        items = []
        for item in _load_items():
            if _key(item.get("email")) == target and (not provider_type or str(item.get("provider_type")) == provider_type):
                removed += 1
            else:
                items.append(item)
        _save_items(items)
    if remove_state and target:
        try:
            mail_provider.clear_outlook_token_state_for(target)
        except AttributeError:
            pass
    result = list_mailboxes()
    result["removed"] = removed
    return result


def update_mailbox_groups(emails: list[str], group: str, *, provider_type: str = "") -> dict[str, Any]:
    targets = {_key(email) for email in emails if _key(email)}
    new_group = _normalize_group(group)
    if not targets:
        result = list_mailboxes()
        result["updated"] = 0
        return result
    updated = 0
    with _lock:
        items = []
        for item in _load_items():
            if _key(item.get("email")) in targets and (not provider_type or str(item.get("provider_type")) == provider_type):
                item = {**item, "group": new_group, "updated_at": _now()}
                updated += 1
            items.append(item)
        _save_items(items)
    result = list_mailboxes()
    result["updated"] = updated
    return result


def mailbox_group_catalog() -> dict[str, Any]:
    pool = list_outlook_mailboxes(include_secret=False)
    return {
        "groups": pool.get("group_records") or [],
        "default_group": DEFAULT_MAILBOX_GROUP,
        "selected_group": str((get_provider_configs().get("outlook_token") or {}).get("mailbox_group") or DEFAULT_MAILBOX_GROUP),
    }


def create_mailbox_group(name: str) -> dict[str, Any]:
    raw_name = str(name or "").strip()
    if not raw_name or raw_name.lower() in ALL_GROUP_SENTINELS or raw_name in ALL_GROUP_SENTINELS:
        raise RuntimeError("请输入有效的分组名称")
    new_name = _normalize_group(raw_name)
    with _lock:
        store = _load_store()
        groups = _normalize_group_records(store.get("groups"), store.get("items"))
        if any(str(item.get("name") or "").lower() == new_name.lower() for item in groups):
            raise RuntimeError("分组名称已存在")
        groups.append({"name": new_name, "created_at": _now(), "updated_at": _now()})
        _save_store(_load_items(), store.get("provider_configs"), groups)
    return mailbox_group_catalog()


def rename_mailbox_group(name: str, new_name: str) -> dict[str, Any]:
    source = _normalize_group(name)
    raw_target = str(new_name or "").strip()
    if source == DEFAULT_MAILBOX_GROUP:
        raise RuntimeError("默认分组不能重命名")
    if not raw_target or raw_target.lower() in ALL_GROUP_SENTINELS or raw_target in ALL_GROUP_SENTINELS:
        raise RuntimeError("请输入有效的新分组名称")
    target = _normalize_group(raw_target)
    with _lock:
        store = _load_store()
        groups = _normalize_group_records(store.get("groups"), store.get("items"))
        source_record = next((item for item in groups if str(item.get("name") or "").lower() == source.lower()), None)
        if not source_record:
            raise RuntimeError("分组不存在")
        if any(str(item.get("name") or "").lower() == target.lower() and item is not source_record for item in groups):
            raise RuntimeError("新分组名称已存在")
        now = _now()
        source_record["name"] = target
        source_record["updated_at"] = now
        items = []
        for item in _load_items():
            if _normalize_group(item.get("group")).lower() == source.lower():
                item = {**item, "group": target, "updated_at": now}
            items.append(item)
        configs = _normalize_provider_configs(store.get("provider_configs"))
        outlook_cfg = dict(configs.get("outlook_token") or {})
        if _normalize_group(outlook_cfg.get("mailbox_group")).lower() == source.lower():
            outlook_cfg["mailbox_group"] = target
            configs["outlook_token"] = outlook_cfg
        _save_store(items, configs, groups)
    return mailbox_group_catalog()


def delete_mailbox_group(name: str, *, move_to: str = DEFAULT_MAILBOX_GROUP) -> dict[str, Any]:
    source = _normalize_group(name)
    target = _normalize_group(move_to)
    if source == DEFAULT_MAILBOX_GROUP:
        raise RuntimeError("默认分组不能删除")
    if source.lower() == target.lower():
        raise RuntimeError("迁移目标不能与待删除分组相同")
    with _lock:
        store = _load_store()
        groups = _normalize_group_records(store.get("groups"), store.get("items"))
        if not any(str(item.get("name") or "").lower() == source.lower() for item in groups):
            raise RuntimeError("分组不存在")
        if not any(str(item.get("name") or "").lower() == target.lower() for item in groups):
            raise RuntimeError("迁移目标分组不存在")
        groups = [item for item in groups if str(item.get("name") or "").lower() != source.lower()]
        now = _now()
        items = []
        for item in _load_items():
            if _normalize_group(item.get("group")).lower() == source.lower():
                item = {**item, "group": target, "updated_at": now}
            items.append(item)
        configs = _normalize_provider_configs(store.get("provider_configs"))
        outlook_cfg = dict(configs.get("outlook_token") or {})
        if _normalize_group(outlook_cfg.get("mailbox_group")).lower() == source.lower():
            outlook_cfg["mailbox_group"] = target
            configs["outlook_token"] = outlook_cfg
        _save_store(items, configs, groups)
    return mailbox_group_catalog()


def clear_mailboxes(*, remove_states: bool = False) -> dict[str, Any]:
    with _lock:
        count = len(_load_items())
        _save_items([])
    if remove_states:
        mail_provider.reset_outlook_token_pool_state("all")
    result = list_mailboxes()
    result["removed"] = count
    return result


def reset_states(scope: str = "failed") -> dict[str, Any]:
    removed = mail_provider.reset_outlook_token_pool_state(scope)
    result = list_mailboxes()
    result["state_removed"] = removed
    return result


def prune_unused_mailboxes() -> dict[str, Any]:
    states = _state_map()
    removed = 0
    with _lock:
        kept: list[dict[str, Any]] = []
        for item in _load_items():
            state = str((states.get(_key(item.get("email"))) or {}).get("state") or "unused")
            if state == "unused":
                removed += 1
            else:
                kept.append(item)
        _save_items(kept)
    result = list_mailboxes()
    result["removed"] = removed
    return result


def _base_address(address: str) -> str:
    local, sep, domain = str(address or "").strip().lower().partition("@")
    if not sep:
        return ""
    domain_l = domain.lower()
    if domain_l in {"outlook.com", "hotmail.com", "live.com", "msn.com", "gmail.com", "googlemail.com"} and "+" in local:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def find_mailbox_for_address(address: str, *, include_secret: bool = True, provider_type: str = "") -> dict[str, Any] | None:
    """按实际地址查找邮箱池条目，支持 Outlook/Gmail plus alias。"""
    target = _key(address)
    base = _key(_base_address(address))
    if not target:
        return None
    with _lock:
        items = _load_items()
    selected: dict[str, Any] | None = None
    for item in items:
        if provider_type and str(item.get("provider_type") or "") != provider_type:
            continue
        item_key = _key(item.get("email"))
        if item_key == target or (base and item_key == base):
            selected = item
            break
    if not selected:
        return None
    provider_type = str(selected.get("provider_type") or "outlook_token")
    inbox = str(selected.get("email") or "").strip()
    result: dict[str, Any] = {
        "provider": provider_type,
        "provider_ref": f"{provider_type}#1",
        "address": str(address or inbox).strip(),
        "inbox_address": inbox,
        "mailbox_group": _normalize_group(selected.get("group")),
        "label": f"{provider_type}#1",
        "client_id": str(selected.get("client_id") or ""),
        "client_secret": str(selected.get("client_secret") or ""),
        "password": str(selected.get("password") or ""),
        "refresh_token": str(selected.get("refresh_token") or ""),
    }
    if not include_secret:
        result.pop("password", None)
        result.pop("client_secret", None)
        result.pop("refresh_token", None)
    return result


def mail_config_from_pool(*, proxy: str = "", wait_timeout: int | float = 180, wait_interval: int | float = 2, request_timeout: int | float = 30, mode: str = "auto", mailbox_group: str = "") -> dict[str, Any]:
    """构造可直接交给 mail_provider 使用的 K12 邮箱配置。"""
    base = {
        "request_timeout": request_timeout,
        "wait_timeout": wait_timeout,
        "wait_interval": wait_interval,
        "api_use_register_proxy": bool(proxy),
        "proxy": str(proxy or "").strip(),
        "mailbox_group": _selected_group(mailbox_group),
        "providers": [],
    }
    return apply_to_register_config({"mail": base})["mail"]


def list_outlook_mailboxes(*, include_secret: bool = True) -> dict[str, Any]:
    signature = _source_signature()
    cache_key = ("outlook", bool(include_secret))
    cached = _cached_pool(cache_key, signature)
    if cached is not None:
        return cached
    with _lock:
        store = _load_store()
        items = [item for item in _load_items(store) if str(item.get("provider_type") or "") == "outlook_token"]
    states = _state_map()
    alias_counts = _split_alias_counts(states)
    group_stats = _group_stats(items, states)
    result = {
        "items": [_with_state(item, states, alias_counts, include_secret=include_secret) for item in items],
        "stats": _stats(items, states),
        "groups": _groups(items, store.get("groups")),
        "group_records": _group_records_with_counts(items, store.get("groups"), group_stats),
        "group_stats": group_stats,
        "default_group": DEFAULT_MAILBOX_GROUP,
    }
    return _store_cached_pool(cache_key, signature, result)


def list_outlook_mailboxes_page(limit: int = 100, offset: int = 0, *, include_secret: bool = True) -> dict[str, Any]:
    page_limit = max(1, min(500, int(limit or 100)))
    page_offset = max(0, int(offset or 0))
    with _lock:
        store = _load_store()
        items = [item for item in _load_items(store) if str(item.get("provider_type") or "") == "outlook_token"]
    states = _state_map()
    alias_counts = _split_alias_counts(states)
    group_stats = _group_stats(items, states)
    selected = items[page_offset:page_offset + page_limit]
    total = len(items)
    return {
        "items": [_with_state(item, states, alias_counts, include_secret=include_secret) for item in selected],
        "stats": _stats(items, states),
        "groups": _groups(items, store.get("groups")),
        "group_records": _group_records_with_counts(items, store.get("groups"), group_stats),
        "group_stats": group_stats,
        "default_group": DEFAULT_MAILBOX_GROUP,
        "page_info": {
            "offset": page_offset,
            "limit": page_limit,
            "returned": len(selected),
            "total": total,
            "partial": page_offset + len(selected) < total,
        },
    }


def _mail_query_config(provider_type: str = "", *, request_timeout: int | float = 30, proxy: str = "") -> dict[str, Any]:
    """为“读取指定邮箱最新验证码”构造配置；不会因动态邮箱开启而屏蔽 Outlook/Gmail 池。"""
    provider_type = str(provider_type or "").strip()
    configs = get_provider_configs(include_secret=True)
    base: dict[str, Any] = {
        "request_timeout": request_timeout,
        "wait_timeout": 1,
        "wait_interval": 1,
        "api_use_register_proxy": bool(proxy),
        "proxy": str(proxy or "").strip(),
        "providers": [],
    }

    def want(name: str) -> bool:
        return not provider_type or provider_type == name

    if want("outlook_token"):
        outlook_text = serialize_outlook_pool()
        if outlook_text.strip():
            base["providers"].append({
                "enable": True,
                "type": "outlook_token",
                "mode": "auto",
                "imap_host": "outlook.office365.com",
                "message_limit": 20,
                "split_aliases": True,
                "split_count_per_mailbox": 9999,
                "mailboxes": outlook_text,
            })
    if want("gmail_oauth"):
        gmail_text = serialize_gmail_pool()
        gmail_cfg = configs.get("gmail_oauth") if isinstance(configs.get("gmail_oauth"), dict) else {}
        if gmail_text.strip():
            base["providers"].append({
                "enable": True,
                "type": "gmail_oauth",
                "mode": str(gmail_cfg.get("mode") or "imap"),
                "imap_host": str(gmail_cfg.get("imap_host") or "imap.gmail.com"),
                "message_limit": int(gmail_cfg.get("message_limit") or 20),
                "split_aliases": bool(gmail_cfg.get("split_aliases")),
                "split_count_per_mailbox": int(gmail_cfg.get("split_count_per_mailbox") or 0),
                "mailboxes": gmail_text,
            })
    if want("cloudflare_temp_email"):
        cf_cfg = configs.get("cloudflare_temp_email") if isinstance(configs.get("cloudflare_temp_email"), dict) else {}
        if bool(cf_cfg.get("enable")) and cf_cfg.get("api_base") and cf_cfg.get("admin_password"):
            base["providers"].append({
                "enable": True,
                "type": "cloudflare_temp_email",
                "api_base": str(cf_cfg.get("api_base") or ""),
                "admin_password": str(cf_cfg.get("admin_password") or ""),
                "domain": _split_csv(cf_cfg.get("domain")),
            })
    if want("ddg_mail"):
        ddg_cfg = configs.get("ddg_mail") if isinstance(configs.get("ddg_mail"), dict) else {}
        if bool(ddg_cfg.get("enable")) and ddg_cfg.get("api_base") and ddg_cfg.get("cf_inbox_jwt"):
            base["providers"].append({
                "enable": True,
                "type": "ddg_mail",
                "ddg_token": str(ddg_cfg.get("ddg_token") or ""),
                "api_base": str(ddg_cfg.get("api_base") or ""),
                "cf_inbox_jwt": str(ddg_cfg.get("cf_inbox_jwt") or ""),
                "admin_password": str(ddg_cfg.get("admin_password") or ""),
                "cf_api_key": str(ddg_cfg.get("cf_api_key") or ""),
                "cf_auth_mode": str(ddg_cfg.get("cf_auth_mode") or "none"),
                "cf_domain": _split_csv(ddg_cfg.get("cf_domain")),
                "cf_create_path": str(ddg_cfg.get("cf_create_path") or "/admin/new_address"),
                "cf_messages_path": str(ddg_cfg.get("cf_messages_path") or "/api/mails"),
            })
    if want("openai_cpa"):
        cpa_cfg = configs.get("openai_cpa") if isinstance(configs.get("openai_cpa"), dict) else {}
        domains = [d for d in _split_csv(cpa_cfg.get("domains")) if d.strip().lower() not in {x.lower() for x in _split_csv(cpa_cfg.get("disabled_domains"))}]
        if bool(cpa_cfg.get("enable")) and domains and cpa_cfg.get("webhook_secret"):
            base["providers"].append({
                "enable": True,
                "type": "openai_cpa",
                "domains": domains,
                "webhook_secret": str(cpa_cfg.get("webhook_secret") or ""),
                "suffix_mode": str(cpa_cfg.get("suffix_mode") or "fixed"),
                "suffix_len_min": int(cpa_cfg.get("suffix_len_min") or 8),
                "suffix_len_max": int(cpa_cfg.get("suffix_len_max") or 8),
            })
    return base


def latest_verification_code(email: str, *, provider_type: str = "", scan_recent: bool = True) -> dict[str, Any]:
    """对指定邮箱读取最近邮件并返回验证码与邮件摘要。"""
    target = str(email or "").strip()
    if "@" not in target:
        raise RuntimeError("请输入有效邮箱地址")
    provider_type = str(provider_type or "").strip()
    assignment = task_proxy_service.assign_task_proxy(1, purpose="mailbox_code")
    proxy = str(assignment.get("proxy") or "")
    attempts: list[str] = []
    no_code_result: dict[str, Any] | None = None

    def query(mailbox: dict[str, Any], source: str) -> dict[str, Any]:
        cfg = _mail_query_config(str(mailbox.get("provider") or provider_type), proxy=proxy)
        if not cfg.get("providers"):
            raise RuntimeError(f"未找到可用于 {mailbox.get('provider') or provider_type} 的邮箱配置")
        result = mail_provider.latest_verification_code(cfg, mailbox, scan_recent=scan_recent)
        result["source"] = source
        return result

    mailbox = find_mailbox_for_address(target)
    if mailbox and (not provider_type or provider_type == str(mailbox.get("provider") or "")):
        try:
            return query(mailbox, "mailbox_pool")
        except Exception as error:
            attempts.append(f"{mailbox.get('provider')}: {error}")

    candidates = [provider_type] if provider_type else ["cloudflare_temp_email", "ddg_mail", "openai_cpa"]
    for candidate in [item for item in candidates if item]:
        try:
            cfg = _mail_query_config(candidate, proxy=proxy)
            if not cfg.get("providers"):
                continue
            if candidate == "cloudflare_temp_email":
                mailbox = mail_provider.get_existing_mailbox(cfg, target, provider="cloudflare_temp_email")
            elif candidate == "ddg_mail":
                ddg_cfg = get_provider_configs(include_secret=True).get("ddg_mail") or {}
                mailbox = {"provider": "ddg_mail", "provider_ref": "ddg_mail#1", "address": target, "token": str(ddg_cfg.get("cf_inbox_jwt") or "")}
            elif candidate == "openai_cpa":
                mailbox = {"provider": "openai_cpa", "provider_ref": "openai_cpa#1", "address": target}
            else:
                continue
            result = mail_provider.latest_verification_code(cfg, mailbox, scan_recent=scan_recent)
            result["source"] = candidate
            if result.get("found"):
                return result
            no_code_result = no_code_result or result
        except Exception as error:
            attempts.append(f"{candidate}: {error}")
    if no_code_result is not None:
        no_code_result["attempts"] = attempts
        return no_code_result
    raise RuntimeError("; ".join(attempts) or "未找到该邮箱对应的邮箱配置")


def recent_outlook_messages(email: str, *, limit: int = 20, mode: str = "auto", request_timeout: int | float = 30) -> dict[str, Any]:
    target = str(email or "").strip()
    mailbox = find_mailbox_for_address(target, provider_type="outlook_token")
    if not mailbox or str(mailbox.get("provider") or "") != "outlook_token":
        raise RuntimeError("未在微软本地库存中找到该邮箱")
    assignment = task_proxy_service.assign_task_proxy(1, purpose="outlook_messages", account=mailbox)
    cfg = _outlook_action_config(mailbox, request_timeout=request_timeout, proxy=str(assignment.get("proxy") or ""))
    result = mail_provider.recent_messages(cfg, mailbox, limit=max(1, min(50, int(limit or 20))), mode=mode)
    result["group"] = str(mailbox.get("mailbox_group") or DEFAULT_MAILBOX_GROUP)
    return result


def _is_invalid_refresh_error(error: object) -> bool:
    text = str(error or "").lower()
    return any(marker in text for marker in ("invalid_grant", "aadsts70000", "grant is expired", "refresh token has expired", "token is expired"))


def _outlook_action_config(mailbox: dict[str, Any], *, request_timeout: int | float = 30, proxy: str = "") -> dict[str, Any]:
    return {
        "request_timeout": request_timeout,
        "wait_timeout": 1,
        "wait_interval": 1,
        "api_use_register_proxy": bool(proxy),
        "proxy": str(proxy or "").strip(),
        "providers": [{
            "enable": True,
            "type": "outlook_token",
            "mode": "auto",
            "imap_host": "outlook.office365.com",
            "message_limit": 20,
            "split_aliases": False,
            "split_count_per_mailbox": 0,
            "mailboxes": [{
                "email": str(mailbox.get("inbox_address") or mailbox.get("address") or ""),
                "password": str(mailbox.get("password") or ""),
                "client_id": str(mailbox.get("client_id") or ""),
                "refresh_token": str(mailbox.get("refresh_token") or ""),
            }],
        }],
    }


def refresh_outlook_tokens(emails: list[str], *, mode: str = "auto", request_timeout: int | float = 30, concurrency: int = 5) -> dict[str, Any]:
    requested = []
    seen: set[str] = set()
    for email in emails or []:
        value = str(email or "").strip()
        key = _key(value)
        if key and key not in seen:
            seen.add(key)
            requested.append(value)
    if not requested:
        raise RuntimeError("请先选择要刷新令牌的微软邮箱")
    requested = requested[:100]
    selected_mode = str(mode or "auto").strip().lower()
    if selected_mode not in {"graph", "imap", "auto"}:
        selected_mode = "auto"
    max_workers = max(1, min(int(concurrency or 5), 10, len(requested)))

    def refresh_one(item: tuple[int, str]) -> dict[str, Any]:
        index, email = item
        mailbox = find_mailbox_for_address(email, provider_type="outlook_token")
        if not mailbox or str(mailbox.get("provider") or "") != "outlook_token":
            return {"email": email, "ok": False, "error": "未在微软本地库存中找到该邮箱", "invalid": False}
        try:
            assignment = task_proxy_service.assign_task_proxy(
                index,
                purpose="outlook_token_refresh",
                account=mailbox,
                total_threads=max_workers,
                slot_index=index - 1,
            )
            result = mail_provider.refresh_outlook_token(
                _outlook_action_config(mailbox, request_timeout=request_timeout, proxy=str(assignment.get("proxy") or "")),
                mailbox,
                mode=selected_mode,
            )
            new_refresh_token = str(result.pop("refresh_token", "") or "")
            return {**result, "email": email, "ok": True, "_new_refresh_token": new_refresh_token, "error": "", "invalid": False}
        except Exception as error:
            return {"email": email, "ok": False, "error": str(error), "invalid": _is_invalid_refresh_error(error)}

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(refresh_one, item): item[1] for item in enumerate(requested, start=1)}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as error:
                email = futures[future]
                results.append({"email": email, "ok": False, "error": str(error), "invalid": _is_invalid_refresh_error(error)})
    order = {_key(email): index for index, email in enumerate(requested)}
    results.sort(key=lambda item: order.get(_key(item.get("email")), len(order)))

    refreshed_at = _now()
    by_email = {_key(item.get("email")): item for item in results}
    with _lock:
        items = _load_items()
        updated_items: list[dict[str, Any]] = []
        for item in items:
            result = by_email.get(_key(item.get("email"))) if str(item.get("provider_type") or "") == "outlook_token" else None
            if not result:
                updated_items.append(item)
                continue
            updated = {
                **item,
                "token_refreshed_at": refreshed_at,
                "token_refresh_status": "success" if result.get("ok") else "failed",
                "token_refresh_error": str(result.get("error") or "")[:1000],
                "token_refresh_mode": str(result.get("method") or selected_mode),
                "updated_at": refreshed_at,
            }
            new_refresh_token = str(result.get("_new_refresh_token") or "").strip()
            if result.get("ok") and new_refresh_token:
                updated["refresh_token"] = new_refresh_token
            updated_items.append(updated)
        _save_items(updated_items)

    states = _state_map()
    for result in results:
        email = str(result.get("email") or "")
        if result.get("ok") and str((states.get(_key(email)) or {}).get("state") or "") == "token_invalid":
            mail_provider.clear_outlook_token_state_for(email)
        elif result.get("invalid"):
            mail_provider._set_outlook_token_state(email, "token_invalid", str(result.get("error") or "")[:300])  # type: ignore[attr-defined]

    public_results = []
    for result in results:
        public_result = {key: value for key, value in result.items() if not key.startswith("_")}
        public_results.append(public_result)
    return {
        "results": public_results,
        "summary": {
            "total": len(public_results),
            "success": sum(1 for item in public_results if item.get("ok")),
            "failed": sum(1 for item in public_results if not item.get("ok")),
            "invalid": sum(1 for item in public_results if item.get("invalid")),
        },
        "mailboxes": list_outlook_mailboxes(),
    }
