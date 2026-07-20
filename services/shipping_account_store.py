from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from curl_cffi import requests

from services import account_inventory_service, grok2api_store, proxy_pool_store, sub2api_store
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.registered_account_store import account_inventory_id, account_matches_selector
from services.sqlite_store import collection_page, document_signature


SHIPPING_ACCOUNTS_FILE = DATA_DIR / "shipping_accounts.json"
SHIPPING_CONFIG_FILE = DATA_DIR / "shipping_store_config.json"
ALIVE_STATUSES = {"ok", "alive", "healthy"}
TERMINAL_STATUSES = {"banned", "dead", "invalid", "disabled", "token_invalid", "auth_invalid"}

_lock = threading.RLock()
_config_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview(value: object, keep: int = 8) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}...{text[-keep:]}"


def _default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "card_prefix": "GROK-",
        "request_timeout": 60,
        "updated_at": "",
        "last_publish": None,
    }


def _normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = _default_config()
    cfg.update({key: value for key, value in (raw or {}).items() if key in cfg})
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["base_url"] = str(cfg.get("base_url") or "").strip().rstrip("/")
    cfg["api_key"] = str(cfg.get("api_key") or "").strip()
    prefix = "".join(ch for ch in str(cfg.get("card_prefix") or "GROK-").upper() if ch.isalnum() or ch in "_-")
    cfg["card_prefix"] = (prefix[:16] or "GROK-")
    cfg["request_timeout"] = max(10, min(300, int(cfg.get("request_timeout") or 60)))
    cfg["updated_at"] = str(cfg.get("updated_at") or "")
    return cfg


def _load_config() -> dict[str, Any]:
    raw = read_json(SHIPPING_CONFIG_FILE, {}, expected_schema_version=1)
    return _normalize_config(raw if isinstance(raw, dict) else {})


def _save_config(cfg: dict[str, Any]) -> None:
    SHIPPING_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(SHIPPING_CONFIG_FILE, _normalize_config(cfg), schema_version=1)


def get_config(*, include_secret: bool = False) -> dict[str, Any]:
    with _config_lock:
        cfg = _load_config()
    public = deepcopy(cfg)
    public["has_api_key"] = bool(public.get("api_key"))
    public["api_key_preview"] = _preview(public.get("api_key"))
    if not include_secret:
        public.pop("api_key", None)
    return public


def update_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _config_lock:
        cfg = _load_config()
        for key, value in (updates or {}).items():
            if key not in cfg:
                continue
            if key == "api_key" and not str(value or "").strip():
                continue
            cfg[key] = value
        cfg["updated_at"] = _now()
        _save_config(cfg)
    return get_config()


def _load() -> list[dict[str, Any]]:
    raw = read_json(SHIPPING_ACCOUNTS_FILE, [], expected_schema_version=1)
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("accounts") or []
    return raw if isinstance(raw, list) else []


def _save(items: list[dict[str, Any]]) -> None:
    SHIPPING_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(SHIPPING_ACCOUNTS_FILE, items, schema_version=1)


def _normalize(account: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(account, dict):
        return None
    email = str(account.get("email") or account.get("name") or "").strip()
    if "@" not in email:
        return None
    if str(account.get("platform") or "grok").strip().lower() != "grok":
        return None
    now = _now()
    base = dict(account)
    base.update(
        {
            "platform": "grok",
            "email": email,
            "shipping_id": str(account.get("shipping_id") or account.get("inventory_id") or account_inventory_id({**account, "platform": "grok", "email": email})),
            "source_type": str(account.get("source_type") or "grok_register"),
            "created_at": str(account.get("created_at") or account.get("registered_at") or now),
            "registered_at": str(account.get("registered_at") or account.get("created_at") or now),
            "updated_at": str(account.get("updated_at") or now),
            "registration_batch_id": str(account.get("registration_batch_id") or account.get("registration_batch") or f"legacy-{now[:10]}"),
            "registration_batch_label": str(account.get("registration_batch_label") or ""),
            "health_status": str(account.get("health_status") or "unknown").strip().lower(),
            "health_checked_at": str(account.get("health_checked_at") or ""),
            "health_method": str(account.get("health_method") or ""),
            "health_error": str(account.get("health_error") or ""),
            "outbound_at": str(account.get("outbound_at") or ""),
            "outbound_batch_id": str(account.get("outbound_batch_id") or ""),
            "outbound_format": str(account.get("outbound_format") or ""),
            "store_listing_id": str(account.get("store_listing_id") or ""),
            "store_card_code": str(account.get("store_card_code") or ""),
        }
    )
    return base


def _public(account: dict[str, Any], *, include_secret: bool = True) -> dict[str, Any]:
    item = deepcopy(account)
    for key in ("password", "sso_token", "access_token", "refresh_token", "id_token"):
        item[f"has_{key}"] = bool(item.get(key))
        item[f"{key}_preview"] = _preview(item.get(key))
        if not include_secret:
            item.pop(key, None)
    return item


def list_accounts(*, include_secret: bool = True) -> list[dict[str, Any]]:
    with _lock:
        items = [_normalize(item) for item in _load()]
    return [_public(item, include_secret=include_secret) for item in items if item]


def select_accounts(selectors: list[str] | None = None, *, include_secret: bool = True) -> list[dict[str, Any]]:
    selected = {str(value or "").strip().lower() for value in selectors or [] if str(value or "").strip()}
    accounts = list_accounts(include_secret=True)
    if selected:
        accounts = [
            account
            for account in accounts
            if str(account.get("shipping_id") or "").lower() in selected
            or any(account_matches_selector(account, selector) for selector in selected)
        ]
    return accounts if include_secret else [_public(account, include_secret=False) for account in accounts]


def add_account(account: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize(account)
    if normalized is None:
        raise ValueError("出货库存仅接受带邮箱的 Grok 账号")
    with _lock:
        current = [item for item in (_normalize(entry) for entry in _load()) if item]
        key = str(normalized.get("shipping_id") or "")
        replaced = False
        for index, old in enumerate(current):
            if str(old.get("shipping_id") or "") != key:
                continue
            current[index] = _normalize({**old, **normalized, "created_at": old.get("created_at") or normalized.get("created_at"), "updated_at": _now()}) or normalized
            normalized = current[index]
            replaced = True
            break
        if not replaced:
            normalized["updated_at"] = _now()
            current.append(normalized)
        current.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        _save(current)
    return _public(normalized)


def update_account(selector: object, updates: dict[str, Any]) -> dict[str, Any] | None:
    text = str(selector or "").strip().lower()
    if not text:
        return None
    with _lock:
        current = [item for item in (_normalize(entry) for entry in _load()) if item]
        updated = None
        for index, old in enumerate(current):
            if text not in {str(old.get("shipping_id") or "").lower(), str(old.get("email") or "").lower()}:
                continue
            merged = _normalize({**old, **{key: value for key, value in updates.items() if value is not None}, "updated_at": _now()})
            if merged is None:
                return None
            current[index] = merged
            updated = merged
            break
        if updated is None:
            return None
        current.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        _save(current)
    return _public(updated)


def delete_accounts(selectors: list[str]) -> dict[str, Any]:
    selected = {str(value or "").strip().lower() for value in selectors or [] if str(value or "").strip()}
    with _lock:
        current = [item for item in (_normalize(entry) for entry in _load()) if item]
        kept = [
            item
            for item in current
            if str(item.get("shipping_id") or "").lower() not in selected
            and str(item.get("email") or "").lower() not in selected
        ]
        removed = len(current) - len(kept)
        _save(kept)
    result = pool(include_secret=True)
    result["removed"] = removed
    return result


def _stats(accounts: list[dict[str, Any]]) -> dict[str, int]:
    alive = sum(1 for item in accounts if str(item.get("health_status") or "").lower() in ALIVE_STATUSES)
    outbound = sum(1 for item in accounts if bool(str(item.get("outbound_at") or "").strip()))
    terminal = sum(1 for item in accounts if str(item.get("health_status") or "").lower() in TERMINAL_STATUSES)
    return {
        "total": len(accounts),
        "alive": alive,
        "terminal": terminal,
        "unknown": max(0, len(accounts) - alive - terminal),
        "available": sum(1 for item in accounts if str(item.get("health_status") or "").lower() in ALIVE_STATUSES and not item.get("outbound_at")),
        "outbound": outbound,
        "sub2api_imported": sum(1 for item in accounts if item.get("sub2api_import_status") == "ok"),
        "grok2api_imported": sum(1 for item in accounts if item.get("grok2api_import_status") == "ok"),
        "store_listed": sum(1 for item in accounts if bool(item.get("store_listing_id") or item.get("store_card_code"))),
    }


def registration_batches() -> list[dict[str, Any]]:
    accounts = list_accounts(include_secret=False)
    grouped: dict[str, dict[str, Any]] = {}
    for item in accounts:
        batch_id = str(item.get("registration_batch_id") or "legacy-unknown")
        row = grouped.setdefault(batch_id, {"id": batch_id, "label": str(item.get("registration_batch_label") or batch_id), "total": 0, "alive": 0, "available": 0})
        row["total"] += 1
        if str(item.get("health_status") or "").lower() in ALIVE_STATUSES:
            row["alive"] += 1
            if not item.get("outbound_at"):
                row["available"] += 1
    return sorted(grouped.values(), key=lambda item: str(item.get("id") or ""), reverse=True)


def pool(*, include_secret: bool = True, limit: int = 0, offset: int = 0) -> dict[str, Any]:
    page_info = None
    if limit > 0:
        page = collection_page(SHIPPING_ACCOUNTS_FILE, limit=max(1, min(1000, int(limit))), offset=max(0, int(offset)))
        if page is not None:
            rows, total = page
            items = [_normalize(item) for item in rows]
            accounts = [_public(item, include_secret=include_secret) for item in items if item]
            page_info = {"offset": max(0, int(offset)), "limit": max(1, min(1000, int(limit))), "returned": len(accounts), "total": total, "partial": max(0, int(offset)) + len(accounts) < total}
        else:
            accounts = list_accounts(include_secret=include_secret)[offset : offset + limit]
    else:
        accounts = list_accounts(include_secret=include_secret)
    all_accounts = list_accounts(include_secret=False)
    result: dict[str, Any] = {"accounts": accounts, "stats": _stats(all_accounts), "batches": registration_batches(), "signature": document_signature(SHIPPING_ACCOUNTS_FILE)}
    if page_info:
        result["page_info"] = page_info
    return result


def check_health(selectors: list[str] | None = None, *, concurrency: int = 8) -> dict[str, Any]:
    accounts = select_accounts(selectors, include_secret=True)
    workers = max(1, min(32, int(concurrency or 8)))
    results: list[dict[str, Any]] = []

    def run_one(index: int, account: dict[str, Any]) -> dict[str, Any]:
        assignment = proxy_pool_store.assign_proxy(
            index,
            total_threads=workers,
            slot_index=(index - 1) % workers,
            purpose="shipping_health",
            account=account,
            validate_node=False,
        )
        checked_at = _now()
        if assignment.get("sticky_failure"):
            result = {"ok": False, "status": "network_error", "status_code": 0, "error": str(assignment.get("error") or "注册代理节点不可用")[:800], "updates": {"health_status": "network_error", "health_checked_at": checked_at, "health_method": "local", "health_error": str(assignment.get("error") or "注册代理节点不可用")[:800]}}
        else:
            try:
                result = account_inventory_service._grok_health_and_quota(account, refresh_quota=False, proxy=str(assignment.get("proxy") or ""))
            except Exception as error:
                result = {"ok": False, "status": "network_error", "status_code": 0, "error": str(error)[:800], "updates": {"health_status": "network_error", "health_checked_at": checked_at, "health_method": "local", "health_error": str(error)[:800]}}
        updates = dict(result.get("updates") or {})
        status = str(result.get("status") or updates.get("health_status") or "unknown").lower()
        updates.update(
            {
                "last_health_attempt_status": status,
                "last_health_attempt_code": int(result.get("status_code") or 0),
                "last_health_attempt_error": str(result.get("error") or "")[:800],
                "last_health_attempt_at": str(updates.get("health_checked_at") or checked_at),
                "health_proxy_source": str(assignment.get("source") or ""),
                "health_proxy_label": str(assignment.get("label") or ""),
                "health_proxy_node_id": str(assignment.get("node_id") or ""),
            }
        )
        if status in ALIVE_STATUSES:
            updates["died_at"] = ""
        elif status in TERMINAL_STATUSES:
            updates["died_at"] = str(account.get("died_at") or updates.get("health_checked_at") or checked_at)
        update_account(account.get("shipping_id") or account.get("email"), updates)
        return {key: value for key, value in result.items() if key != "updates"} | {"shipping_id": account.get("shipping_id"), "email": account.get("email"), "proxy_label": assignment.get("label") or ""}

    if accounts:
        with ThreadPoolExecutor(max_workers=min(workers, len(accounts))) as executor:
            futures = [executor.submit(run_one, index, account) for index, account in enumerate(accounts, start=1)]
            for future in as_completed(futures):
                results.append(future.result())
    results.sort(key=lambda item: str(item.get("email") or "").lower())
    return {"ok": True, "stats": {"total": len(results), "alive": sum(1 for item in results if item.get("ok")), "failed": sum(1 for item in results if not item.get("ok"))}, "results": results, "shipping_pool": pool(include_secret=True)}


def _eligible(*, selectors: list[str] | None = None, registration_batch_id: str = "", quantity: int = 0, include_outbound: bool = False, require_alive: bool = True) -> list[dict[str, Any]]:
    accounts = select_accounts(selectors, include_secret=True)
    batch = str(registration_batch_id or "").strip()
    if batch:
        accounts = [item for item in accounts if str(item.get("registration_batch_id") or "") == batch]
    if require_alive:
        accounts = [item for item in accounts if str(item.get("health_status") or "").lower() in ALIVE_STATUSES]
    else:
        accounts = [item for item in accounts if str(item.get("health_status") or "").lower() not in TERMINAL_STATUSES]
    if not include_outbound:
        accounts = [item for item in accounts if not str(item.get("outbound_at") or "").strip()]
    accounts.sort(key=lambda item: str(item.get("registered_at") or item.get("created_at") or ""))
    if int(quantity or 0) > 0:
        accounts = accounts[: int(quantity)]
    return accounts


def export_accounts(*, selectors: list[str] | None = None, registration_batch_id: str = "", quantity: int = 0, format: str = "sub2api", include_outbound: bool = False) -> dict[str, Any]:
    export_format = str(format or "sub2api").strip().lower()
    if export_format not in {"sub2api", "grok2api"}:
        raise ValueError("出库格式必须是 sub2api 或 grok2api")
    accounts = _eligible(selectors=selectors, registration_batch_id=registration_batch_id, quantity=quantity, include_outbound=include_outbound)
    if not accounts:
        raise ValueError("没有符合条件且测活成功的可出库账号")
    if export_format == "sub2api":
        exported = sub2api_store.export_account_dicts_to_sub2api(accounts)
        content = exported.get("data") or {}
        filename = f"grok-sub2api-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    else:
        exported = grok2api_store.export_account_dicts_to_grok2api(accounts)
        content = exported.get("data") or {}
        filename = f"grok2api-sso-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    if int(exported.get("count") or 0) != len(accounts):
        raise ValueError(f"仅生成 {int(exported.get('count') or 0)}/{len(accounts)} 个账号，未执行出库标记")
    outbound_at = _now()
    outbound_batch_id = f"ship-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    for account in accounts:
        update_account(account.get("shipping_id") or account.get("email"), {"outbound_at": outbound_at, "outbound_batch_id": outbound_batch_id, "outbound_format": export_format})
    return {"ok": True, "count": len(accounts), "filename": filename, "format": export_format, "content": content, "outbound_batch_id": outbound_batch_id, "shipping_ids": [item.get("shipping_id") for item in accounts], "shipping_pool": pool(include_secret=True)}


def import_accounts(target: str, selectors: list[str] | None = None) -> dict[str, Any]:
    target_name = str(target or "").strip().lower()
    if target_name not in {"sub2api", "grok2api"}:
        raise ValueError("导入目标必须是 sub2api 或 grok2api")
    accounts = _eligible(selectors=selectors, include_outbound=True)
    if not accounts:
        raise ValueError("选择中没有测活成功的账号")
    result = sub2api_store.import_account_dicts_to_sub2api(accounts) if target_name == "sub2api" else grok2api_store.import_account_dicts_to_grok2api(accounts)
    ok = bool(result.get("ok", True)) and not bool(result.get("error"))
    imported_at = _now()
    for account in accounts:
        update_account(account.get("shipping_id") or account.get("email"), {f"{target_name}_import_status": "ok" if ok else "failed", f"{target_name}_imported_at": imported_at if ok else "", f"{target_name}_last_error": "" if ok else str(result.get("error") or "导入失败")[:800]})
    return {"ok": ok, "target": target_name, "count": len(accounts), "result": result, "shipping_pool": pool(include_secret=True)}


def _require_store_config() -> dict[str, Any]:
    with _config_lock:
        cfg = _load_config()
    if not cfg.get("enabled"):
        raise RuntimeError("请先启用商店连接")
    if not cfg.get("base_url") or not cfg.get("api_key"):
        raise RuntimeError("请填写商店地址和接入密钥")
    return cfg


def _store_request(method: str, path: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _require_store_config()
    response = requests.request(
        method,
        f"{cfg['base_url']}/{path.lstrip('/')}",
        headers={"X-Integration-Key": str(cfg["api_key"]), "Accept": "application/json", "Content-Type": "application/json"},
        json=payload,
        timeout=int(cfg.get("request_timeout") or 60),
    )
    try:
        data = response.json()
    except Exception:
        data = {"raw": str(response.text or "")[:1000]}
    if int(response.status_code) not in range(200, 300):
        raise RuntimeError(str(data.get("error") or data.get("message") or f"商店 HTTP {response.status_code}"))
    return data if isinstance(data, dict) else {"data": data}


def test_store_connection() -> dict[str, Any]:
    result = _store_request("GET", "/api/integration/health")
    return {"ok": bool(result.get("success", True)), "store": result, "tested_at": _now()}


def publish_to_store(*, selectors: list[str] | None = None, registration_batch_id: str = "", quantity: int = 0, include_outbound: bool = False, allow_unverified: bool = False) -> dict[str, Any]:
    _require_store_config()
    accounts = _eligible(selectors=selectors, registration_batch_id=registration_batch_id, quantity=quantity, include_outbound=include_outbound, require_alive=not allow_unverified)
    if not accounts:
        raise ValueError("没有符合条件的可同步账号")
    items: list[dict[str, Any]] = []
    for account in accounts:
        sub = sub2api_store.export_account_dicts_to_sub2api(account)
        grok = grok2api_store.export_account_dicts_to_grok2api(account)
        if int(sub.get("count") or 0) != 1 or int(grok.get("count") or 0) != 1:
            continue
        items.append(
            {
                "sourceId": str(account.get("shipping_id") or ""),
                "email": str(account.get("email") or ""),
                "sub2apiName": f"{account.get('shipping_id')}-sub2api.json",
                "sub2apiData": sub.get("data") or {},
                "grok2apiName": f"{account.get('shipping_id')}-grok2api.json",
                "grok2apiData": grok.get("data") or {},
            }
        )
    if not items:
        raise ValueError("账号缺少生成商店双格式商品所需的 OAuth 或 SSO")
    result = _store_request("POST", "/api/integration/grok/stock", payload={"items": items})
    published = result.get("stockItems") if isinstance(result.get("stockItems"), list) else []
    by_source = {str(item.get("sourceId") or ""): item for item in published if isinstance(item, dict)}
    published_at = _now()
    for account in accounts:
        source_id = str(account.get("shipping_id") or "")
        stock = by_source.get(source_id)
        if not stock:
            continue
        update_account(source_id, {"store_listing_id": str(stock.get("id") or ""), "store_card_code": "", "store_synced_at": published_at, "store_sync_batch_id": str(result.get("publishBatchId") or "")})
    publish_result = {"ok": True, "requested": len(accounts), "published": len(by_source), "publish_batch_id": str(result.get("publishBatchId") or ""), "stock_items": published, "published_at": published_at, "stock_stats": result.get("stockStats") or {}}
    with _config_lock:
        latest = _load_config()
        latest["last_publish"] = publish_result
        latest["updated_at"] = published_at
        _save_config(latest)
    return {**publish_result, "shipping_pool": pool(include_secret=True)}
