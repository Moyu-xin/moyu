
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.proxy_service import normalize_proxy_url, test_proxy

PROXY_POOL_FILE = DATA_DIR / "proxy_pool.json"
MAX_SUBSCRIPTION_BYTES = 5 * 1024 * 1024
_lock = RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def registration_proxy_record(assignment: dict[str, Any] | None, *, proxy: str = "") -> dict[str, Any]:
    current = assignment if isinstance(assignment, dict) else {}
    node = current.get("node") if isinstance(current.get("node"), dict) else {}
    proxy_url = normalize_proxy_url(str(current.get("proxy") or proxy or "").strip())
    source = str(current.get("source") or ("explicit" if proxy_url else "direct")).strip()
    node_name = str(current.get("label") or node.get("name") or "").strip()
    node_id = str(current.get("node_id") or "").strip()
    if not any((proxy_url, node_id, node_name)):
        return {}
    record = {
        "source": source,
        "proxy": proxy_url,
        "node_id": node_id,
        "node_name": node_name,
        "label": node_name,
        "slot": int(current.get("slot") or 0),
        "managed_slot": int(current.get("managed_slot") or 0),
        "region": str(current.get("region") or "").strip(),
        "recorded_at": _now(),
    }
    return {key: value for key, value in record.items() if value not in (None, "", 0)}


def _account_registration_proxy(account: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(account, dict):
        return {}
    record = account.get("registration_proxy")
    if isinstance(record, dict):
        return dict(record)
    node_id = str(account.get("registration_proxy_node_id") or "").strip()
    node_name = str(account.get("registration_proxy_label") or account.get("registration_proxy_node_name") or "").strip()
    source = str(account.get("registration_proxy_source") or "").strip()
    if not any((node_id, node_name, source)):
        return {}
    return {
        "source": source,
        "proxy": str(account.get("proxy") or "").strip(),
        "node_id": node_id,
        "node_name": node_name,
        "label": node_name,
    }


def _default_store() -> dict[str, Any]:
    return {
        "enabled": False,
        "assignment_version": 2,
        "mode": "per_task",
        "strategy": "round_robin",
        "node_mode": "direct_proxy",
        "fallback_proxy": "",
        "node_blacklist": [],
        "fastest_mode": False,
        "max_retry_count": 8,
        "retry_delay": 1.0,
        "mihomo": {
            "enabled": False,
            "api_base": "http://127.0.0.1:9090",
            "secret": "",
            "selector": "GLOBAL",
            "proxy_scheme": "http",
            "proxy_host": "127.0.0.1",
            "proxy_port_base": 41000,
            "thread_port_mode": True,
        },
        "managed_mihomo": {
            "enabled": False,
            "executable": "",
            "auto_download": True,
            "proxy_host": "127.0.0.1",
            "proxy_port_base": 42000,
            "api_port_base": 43000,
            "startup_timeout": 30,
        },
        "subscriptions": [],
        "nodes": [],
        "updated_at": "",
    }


def _load() -> dict[str, Any]:
    data = read_json(PROXY_POOL_FILE, {}, expected_schema_version=1)
    if not isinstance(data, dict):
        data = {}
    legacy_assignment = "assignment_version" not in data
    store = _default_store()
    store.update({k: v for k, v in data.items() if k in store})
    store["enabled"] = bool(store.get("enabled"))
    store["mode"] = str(store.get("mode") or "per_task").strip() or "per_task"
    if store["mode"] not in {"per_task", "per_thread"}:
        store["mode"] = "per_task"
    if legacy_assignment and store["mode"] == "per_thread":
        store["mode"] = "per_task"
    store["assignment_version"] = 2
    store["strategy"] = str(store.get("strategy") or "round_robin").strip() or "round_robin"
    if store["strategy"] not in {"round_robin", "random"}:
        store["strategy"] = "round_robin"
    store["node_mode"] = str(store.get("node_mode") or "direct_proxy").strip() or "direct_proxy"
    if store["node_mode"] not in {"direct_proxy", "mihomo_ports", "managed_mihomo"}:
        store["node_mode"] = "direct_proxy"
    store["fallback_proxy"] = normalize_proxy_url(str(store.get("fallback_proxy") or "").strip())
    raw_blacklist = store.get("node_blacklist")
    if isinstance(raw_blacklist, str):
        raw_blacklist = re.split(r"[,，\n]+", raw_blacklist)
    store["node_blacklist"] = [str(item).strip() for item in raw_blacklist if str(item).strip()] if isinstance(raw_blacklist, list) else []
    store["fastest_mode"] = bool(store.get("fastest_mode"))
    try:
        store["max_retry_count"] = max(1, min(20, int(store.get("max_retry_count") or 8)))
    except Exception:
        store["max_retry_count"] = 8
    try:
        store["retry_delay"] = max(0.0, min(30.0, float(store.get("retry_delay") or 1.0)))
    except Exception:
        store["retry_delay"] = 1.0
    mihomo = store.get("mihomo") if isinstance(store.get("mihomo"), dict) else {}
    default_mihomo = _default_store()["mihomo"]
    merged_mihomo = {**default_mihomo, **mihomo}
    merged_mihomo["enabled"] = bool(merged_mihomo.get("enabled"))
    merged_mihomo["api_base"] = str(merged_mihomo.get("api_base") or default_mihomo["api_base"]).strip().rstrip("/")
    merged_mihomo["secret"] = str(merged_mihomo.get("secret") or "").strip()
    merged_mihomo["selector"] = str(merged_mihomo.get("selector") or "GLOBAL").strip() or "GLOBAL"
    merged_mihomo["proxy_scheme"] = str(merged_mihomo.get("proxy_scheme") or "http").strip().lower() or "http"
    if merged_mihomo["proxy_scheme"] not in {"http", "socks5h", "socks5"}:
        merged_mihomo["proxy_scheme"] = "http"
    merged_mihomo["proxy_host"] = str(merged_mihomo.get("proxy_host") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        merged_mihomo["proxy_port_base"] = max(1, int(merged_mihomo.get("proxy_port_base") or 41000))
    except Exception:
        merged_mihomo["proxy_port_base"] = 41000
    store["mihomo"] = merged_mihomo
    managed = store.get("managed_mihomo") if isinstance(store.get("managed_mihomo"), dict) else {}
    default_managed = _default_store()["managed_mihomo"]
    merged_managed = {**default_managed, **managed}
    merged_managed["enabled"] = bool(merged_managed.get("enabled"))
    merged_managed["auto_download"] = bool(merged_managed.get("auto_download", True))
    merged_managed["executable"] = str(merged_managed.get("executable") or "").strip()
    merged_managed["proxy_host"] = str(merged_managed.get("proxy_host") or "127.0.0.1").strip() or "127.0.0.1"
    for key, fallback in (("proxy_port_base", 42000), ("api_port_base", 43000), ("startup_timeout", 30)):
        try:
            merged_managed[key] = max(1, int(merged_managed.get(key) or fallback))
        except Exception:
            merged_managed[key] = fallback
    store["managed_mihomo"] = merged_managed
    store["subscriptions"] = _normalize_subscriptions(store.get("subscriptions"))
    store["nodes"] = _normalize_nodes(store.get("nodes"))
    store["updated_at"] = str(store.get("updated_at") or "")
    return store


def _save(store: dict[str, Any]) -> None:
    normalized = _load() if store is None else _normalize_store(store)
    normalized["updated_at"] = _now()
    PROXY_POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(PROXY_POOL_FILE, normalized, schema_version=1)


def _normalize_store(raw: dict[str, Any]) -> dict[str, Any]:
    base = _default_store()
    base.update({k: v for k, v in (raw or {}).items() if k in base})
    base["subscriptions"] = _normalize_subscriptions(base.get("subscriptions"))
    base["nodes"] = _normalize_nodes(base.get("nodes"))
    return base


def _normalize_subscriptions(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value if isinstance(value, list) else []:
        if isinstance(raw, str):
            name, url = raw, raw
            enabled = True
            item_id = _id_for("sub", url)
        elif isinstance(raw, dict):
            url = str(raw.get("url") or "").strip()
            name = str(raw.get("name") or raw.get("label") or url or "未命名订阅").strip()
            enabled = bool(raw.get("enabled", True))
            item_id = str(raw.get("id") or _id_for("sub", url or name)).strip()
        else:
            continue
        if not url or url in seen:
            continue
        seen.add(url)
        items.append({
            "id": item_id,
            "name": name or url,
            "url": url,
            "enabled": enabled,
            "created_at": str((raw if isinstance(raw, dict) else {}).get("created_at") or _now()),
            "updated_at": str((raw if isinstance(raw, dict) else {}).get("updated_at") or _now()),
            "last_error": str((raw if isinstance(raw, dict) else {}).get("last_error") or ""),
            "last_imported": int((raw if isinstance(raw, dict) else {}).get("last_imported") or 0),
        })
    return items


def _normalize_nodes(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        proxy_url = normalize_proxy_url(str(raw.get("proxy_url") or raw.get("url") or "").strip())
        if proxy_url and _scheme_from_proxy(proxy_url) not in {"http", "https", "socks", "socks5", "socks5h"}:
            proxy_url = ""
        name = str(raw.get("name") or raw.get("label") or proxy_url or raw.get("server") or "未命名节点").strip()
        server = str(raw.get("server") or "").strip()
        port = str(raw.get("port") or "").strip()
        node_type = str(raw.get("type") or raw.get("scheme") or _scheme_from_proxy(proxy_url) or "unknown").strip().lower() or "unknown"
        subscription_id = str(raw.get("subscription_id") or "").strip()
        key = proxy_url or f"{subscription_id}:{name}:{server}:{port}:{node_type}"
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "id": str(raw.get("id") or _id_for("node", key)).strip(),
            "name": name,
            "type": node_type,
            "server": server,
            "port": port,
            "proxy_url": proxy_url,
            "subscription_id": subscription_id,
            "enabled": bool(raw.get("enabled", True)),
            "source": str(raw.get("source") or "manual").strip() or "manual",
            "status": str(raw.get("status") or "unknown"),
            "latency_ms": int(raw.get("latency_ms") or 0) if str(raw.get("latency_ms") or "").isdigit() else 0,
            "last_checked_at": str(raw.get("last_checked_at") or ""),
            "created_at": str(raw.get("created_at") or _now()),
            "updated_at": str(raw.get("updated_at") or _now()),
        })
    return items


def _id_for(prefix: str, value: str) -> str:
    digest = hashlib.sha1(str(value or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _scheme_from_proxy(value: str) -> str:
    try:
        return urllib.parse.urlparse(value).scheme.lower()
    except Exception:
        return ""


def _public(store: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(json.dumps(store, ensure_ascii=False))
    secret = str(((data.get("mihomo") or {}).get("secret")) or "")
    if secret:
        data["mihomo"]["has_secret"] = True
        data["mihomo"]["secret_preview"] = _preview(secret)
        data["mihomo"].pop("secret", None)
    else:
        data["mihomo"]["has_secret"] = False
        data["mihomo"]["secret_preview"] = ""
    managed = data.get("managed_mihomo") if isinstance(data.get("managed_mihomo"), dict) else {}
    executable = str(managed.get("executable") or "")
    managed["has_executable"] = bool(executable)
    data["managed_mihomo"] = managed
    data["stats"] = _stats(data.get("nodes") or [])
    return data


def _preview(value: object, keep: int = 4) -> str:
    text = str(value or "")
    if len(text) <= keep * 2:
        return "***" if text else ""
    return f"{text[:keep]}...{text[-keep:]}"


def _stats(nodes: list[dict[str, Any]]) -> dict[str, int]:
    enabled = [node for node in nodes if node.get("enabled")]
    direct = [node for node in enabled if str(node.get("proxy_url") or "").strip()]
    return {
        "total": len(nodes),
        "enabled": len(enabled),
        "direct_proxy": len(direct),
        "mihomo_only": max(0, len(enabled) - len(direct)),
    }


def get_proxy_pool_config(*, include_secret: bool = False) -> dict[str, Any]:
    with _lock:
        store = _load()
    return store if include_secret else _public(store)


def _mihomo_headers(mihomo: dict[str, Any], *, json_body: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "MoyuRegister/1.0",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    secret = str(mihomo.get("secret") or "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _mihomo_request_json(mihomo: dict[str, Any], path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    api_base = str(mihomo.get("api_base") or "").strip().rstrip("/")
    if not api_base:
        raise RuntimeError("请先填写 Mihomo 控制 API 地址")
    url = f"{api_base}{path if path.startswith('/') else '/' + path}"
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_mihomo_headers(mihomo, json_body=body is not None), method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Mihomo 控制 API 返回 HTTP {error.code}: {detail}") from error
    except Exception as error:
        raise RuntimeError(f"Mihomo 控制 API 请求失败: {error}") from error
    try:
        data_obj = json.loads(raw) if raw.strip() else {}
    except Exception as error:
        raise RuntimeError(f"Mihomo 控制 API 响应不是 JSON: {raw[:200]}") from error
    return data_obj if isinstance(data_obj, dict) else {}


def _mihomo_leaf_names(proxies: dict[str, Any], selector: str) -> list[str]:
    selector_key = str(selector or "").strip()
    group = proxies.get(selector_key) if selector_key else None
    if not isinstance(group, dict) and selector_key:
        lowered = selector_key.lower()
        for key, value in proxies.items():
            if str(key).lower() == lowered and isinstance(value, dict):
                group = value
                break
    raw_names = []
    if isinstance(group, dict):
        for key in ("all", "proxies", "children"):
            value = group.get(key)
            if isinstance(value, list):
                raw_names.extend(str(item) for item in value)
    if not raw_names:
        group_types = {"selector", "urltest", "fallback", "loadbalance", "relay", "direct", "reject"}
        for name, value in proxies.items():
            if not isinstance(value, dict):
                continue
            node_type = str(value.get("type") or "").replace(" ", "").lower()
            if node_type in group_types:
                continue
            raw_names.append(str(name))
    denied = {"DIRECT", "REJECT", "GLOBAL", ""}
    result: list[str] = []
    seen: set[str] = set()
    for name in raw_names:
        text = str(name or "").strip()
        if text.upper() in denied or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _mihomo_nodes_from_proxies(data: dict[str, Any], selector: str) -> list[dict[str, Any]]:
    proxies = data.get("proxies") if isinstance(data.get("proxies"), dict) else data
    if not isinstance(proxies, dict):
        return []
    nodes: list[dict[str, Any]] = []
    subscription_id = _id_for("mihomo", selector or "GLOBAL")
    for name in _mihomo_leaf_names(proxies, selector):
        item = proxies.get(name) if isinstance(proxies.get(name), dict) else {}
        node_type = str(item.get("type") or "mihomo").strip().lower() if isinstance(item, dict) else "mihomo"
        nodes.append({
            "name": name,
            "type": node_type,
            "server": str(item.get("server") or "") if isinstance(item, dict) else "",
            "port": str(item.get("port") or "") if isinstance(item, dict) else "",
            "proxy_url": "",
            "subscription_id": subscription_id,
            "source": "mihomo",
            "enabled": True,
        })
    return _normalize_nodes(nodes)


def _mihomo_proxy_for_slot(mihomo: dict[str, Any], slot: int = 0) -> str:
    base = int(mihomo.get("proxy_port_base") or 41000)
    scheme = str(mihomo.get("proxy_scheme") or "http")
    host = str(mihomo.get("proxy_host") or "127.0.0.1")
    port = base + max(0, int(slot or 0)) if bool(mihomo.get("thread_port_mode")) else base
    return normalize_proxy_url(f"{scheme}://{host}:{port}")


def _mihomo_select_node(mihomo: dict[str, Any], node_name: str) -> None:
    selector = urllib.parse.quote(str(mihomo.get("selector") or "GLOBAL"), safe="")
    _mihomo_request_json(mihomo, f"/proxies/{selector}", method="PUT", body={"name": str(node_name or "")})


def update_proxy_pool_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        store = _load()
        for key in ("enabled", "mode", "strategy", "node_mode", "fallback_proxy", "node_blacklist", "fastest_mode", "max_retry_count", "retry_delay"):
            if key in updates:
                store[key] = updates[key]
        if isinstance(updates.get("mihomo"), dict):
            mihomo = dict(store.get("mihomo") or {})
            for key, value in updates["mihomo"].items():
                if key == "secret" and not str(value or "").strip():
                    continue
                if key in _default_store()["mihomo"]:
                    mihomo[key] = value
            store["mihomo"] = mihomo
        if isinstance(updates.get("managed_mihomo"), dict):
            managed = dict(store.get("managed_mihomo") or {})
            for key, value in updates["managed_mihomo"].items():
                if key in _default_store()["managed_mihomo"]:
                    managed[key] = value
            store["managed_mihomo"] = managed
        if isinstance(updates.get("subscriptions"), list):
            incoming = {str(item.get("id") or ""): item for item in updates["subscriptions"] if isinstance(item, dict)}
            for subscription in store.get("subscriptions") or []:
                patch = incoming.get(str(subscription.get("id") or ""))
                if not patch:
                    continue
                if "enabled" in patch:
                    subscription["enabled"] = bool(patch.get("enabled"))
                if str(patch.get("name") or "").strip():
                    subscription["name"] = str(patch.get("name") or "").strip()
                subscription["updated_at"] = _now()
        _save(store)
        return get_proxy_pool_config()


def add_subscriptions(items: list[dict[str, Any]], *, fetch: bool = True) -> dict[str, Any]:
    imported_nodes = 0
    errors: list[dict[str, str]] = []
    prepared: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    fetched_nodes: dict[str, list[dict[str, Any]]] = {}
    fetch_errors: dict[str, str] = {}

    for raw in items:
        url = str(raw.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        allow_private = bool(raw.get("allow_private"))
        try:
            _validate_subscription_url(url, allow_private=allow_private, resolve=False)
        except RuntimeError as error:
            errors.append({"url": url, "error": str(error)[:300]})
            continue
        seen_urls.add(url)
        prepared.append({
            "url": url,
            "name": str(raw.get("name") or url).strip() or url,
            "enabled": bool(raw.get("enabled", True)),
            "allow_private": allow_private,
        })
        if fetch:
            try:
                text = _fetch_text(url, allow_private=allow_private)
                fetched_nodes[url] = _parse_subscription_nodes(text, subscription_id=_id_for("sub", url), source="subscription")
            except Exception as error:
                fetch_errors[url] = str(error)[:300]

    with _lock:
        store = _load()
        existing = {item["url"]: item for item in store["subscriptions"]}
        for raw in prepared:
            url = raw["url"]
            item = existing.get(url) or {
                "id": _id_for("sub", url),
                "created_at": _now(),
            }
            item.update({
                "name": str(raw.get("name") or item.get("name") or url).strip() or url,
                "url": url,
                "enabled": bool(raw.get("enabled", True)),
                "allow_private": bool(raw.get("allow_private")),
                "updated_at": _now(),
            })
            existing[url] = item
            if fetch:
                if url in fetched_nodes:
                    nodes = fetched_nodes[url]
                    imported_nodes += _merge_nodes(store, nodes)
                    item["last_error"] = ""
                    item["last_imported"] = len(nodes)
                else:
                    item["last_error"] = fetch_errors.get(url, "订阅下载失败")
                    errors.append({"url": url, "error": item["last_error"]})
        store["subscriptions"] = list(existing.values())
        _save(store)
    result = get_proxy_pool_config()
    result.update({"imported_nodes": imported_nodes, "errors": errors})
    return result


def import_subscription_text(text: str, *, name: str = "手动订阅") -> dict[str, Any]:
    nodes = _parse_subscription_nodes(text, subscription_id=_id_for("sub", name + str(time.time())), source="manual_subscription")
    with _lock:
        store = _load()
        imported = _merge_nodes(store, nodes)
        _save(store)
    result = get_proxy_pool_config()
    result["imported_nodes"] = imported
    return result


def import_node_text(text: str) -> dict[str, Any]:
    nodes = _parse_manual_proxy_lines(text)
    with _lock:
        store = _load()
        imported = _merge_nodes(store, nodes)
        _save(store)
    result = get_proxy_pool_config()
    result["imported_nodes"] = imported
    return result


def sync_mihomo_nodes() -> dict[str, Any]:
    with _lock:
        store = _load()
        mihomo = dict(store.get("mihomo") or {})
    if not bool(mihomo.get("enabled")):
        raise RuntimeError("请先启用 Mihomo 端口池")
    data = _mihomo_request_json(mihomo, "/proxies")
    nodes = _mihomo_nodes_from_proxies(data, str(mihomo.get("selector") or "GLOBAL"))
    with _lock:
        store = _load()
        imported = _merge_nodes(store, nodes)
        _save(store)
    result = get_proxy_pool_config()
    result["imported_nodes"] = imported
    return result


def clear_nodes() -> dict[str, Any]:
    with _lock:
        store = _load()
        removed = len(store.get("nodes") or [])
        store["nodes"] = []
        _save(store)
    result = get_proxy_pool_config()
    result["removed"] = removed
    return result


def delete_node(node_id: str) -> dict[str, Any]:
    with _lock:
        store = _load()
        before = len(store["nodes"])
        store["nodes"] = [node for node in store["nodes"] if str(node.get("id")) != str(node_id)]
        _save(store)
    result = get_proxy_pool_config()
    result["removed"] = before - len(result.get("nodes") or [])
    return result


def delete_subscription(subscription_id: str) -> dict[str, Any]:
    with _lock:
        store = _load()
        before = len(store["subscriptions"])
        store["subscriptions"] = [item for item in store["subscriptions"] if str(item.get("id")) != str(subscription_id)]
        store["nodes"] = [node for node in store["nodes"] if str(node.get("subscription_id")) != str(subscription_id)]
        _save(store)
    result = get_proxy_pool_config()
    result["removed"] = before - len(result.get("subscriptions") or [])
    return result


def assign_proxy(
    task_index: int,
    *,
    fallback: str = "",
    total_threads: int | None = None,
    slot_index: int | None = None,
    node_name_filter: str = "",
    validate_node: bool = True,
    purpose: str = "",
    account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registration_proxy = _account_registration_proxy(account)
    if registration_proxy:
        stored_source = str(registration_proxy.get("source") or "").strip()
        stored_proxy = normalize_proxy_url(str(registration_proxy.get("proxy") or (account or {}).get("proxy") or "").strip())
        stored_node_id = str(registration_proxy.get("node_id") or "").strip()
        stored_node_name = str(registration_proxy.get("node_name") or registration_proxy.get("label") or "").strip()
        try:
            sticky_slot = max(0, int(slot_index or 0))
        except Exception:
            sticky_slot = 0
        try:
            sticky_threads = max(1, int(total_threads or 1))
        except Exception:
            sticky_threads = 1
        if stored_source == "managed_mihomo" and stored_node_name:
            store = get_proxy_pool_config(include_secret=True)
            if not bool(store.get("enabled")) or str(store.get("node_mode") or "") != "managed_mihomo":
                return {
                    "proxy": "",
                    "source": "registration_sticky",
                    "sticky": True,
                    "sticky_failure": True,
                    "node_id": stored_node_id,
                    "label": stored_node_name,
                    "slot": sticky_slot + 1,
                    "purpose": str(purpose or ""),
                    "error": "注册节点要求托管 Mihomo，但当前代理池未启用托管模式",
                }
            try:
                from services.managed_mihomo_service import managed_mihomo_manager

                assigned = managed_mihomo_manager.assign(
                    slot=sticky_slot,
                    task_index=max(1, int(task_index or 1)),
                    total_threads=sticky_threads,
                    store=store,
                    purpose=str(purpose or ""),
                    exact_node_name=stored_node_name,
                    validate_node=bool(validate_node),
                )
                assigned.update({
                    "sticky": True,
                    "sticky_source": "registration",
                    "registration_node_id": stored_node_id,
                    "registration_node_name": stored_node_name,
                })
                return assigned
            except Exception as error:
                return {
                    "proxy": "",
                    "source": "registration_sticky",
                    "sticky": True,
                    "sticky_failure": True,
                    "node_id": stored_node_id,
                    "label": stored_node_name,
                    "slot": sticky_slot + 1,
                    "purpose": str(purpose or ""),
                    "error": str(error),
                }
        if stored_proxy:
            return {
                "proxy": stored_proxy,
                "source": "registration_proxy",
                "original_source": stored_source,
                "sticky": True,
                "sticky_source": "registration",
                "node_id": stored_node_id,
                "label": stored_node_name or "注册专用代理",
                "slot": sticky_slot + 1,
                "purpose": str(purpose or ""),
            }
        return {
            "proxy": "",
            "source": "registration_sticky",
            "sticky": True,
            "sticky_failure": True,
            "node_id": stored_node_id,
            "label": stored_node_name,
            "slot": sticky_slot + 1,
            "purpose": str(purpose or ""),
            "error": "账号存在注册节点记录，但缺少可恢复的节点名称或代理地址",
        }
    account_proxy = normalize_proxy_url(str((account or {}).get("proxy") or "").strip())
    if account_proxy:
        account_store = get_proxy_pool_config(include_secret=True)
        managed = account_store.get("managed_mihomo") if isinstance(account_store.get("managed_mihomo"), dict) else {}
        try:
            parsed_account_proxy = urllib.parse.urlparse(account_proxy)
            managed_host = str(managed.get("proxy_host") or "127.0.0.1").strip().lower()
            proxy_host = str(parsed_account_proxy.hostname or "").strip().lower()
            proxy_port = int(parsed_account_proxy.port or 0)
            base_port = int(managed.get("proxy_port_base") or 42000)
            managed_local_proxy = (
                str(account_store.get("node_mode") or "") == "managed_mihomo"
                and proxy_host in {managed_host, "127.0.0.1", "localhost"}
                and base_port <= proxy_port < base_port + 256
            )
        except Exception:
            managed_local_proxy = False
        if managed_local_proxy:
            return {
                "proxy": "",
                "source": "registration_sticky",
                "sticky": True,
                "sticky_failure": True,
                "legacy_proxy": True,
                "label": "旧账号缺少注册节点记录",
                "slot": max(1, int(slot_index or 0) + 1),
                "purpose": str(purpose or ""),
                "error": "旧账号只保存了托管 Mihomo 本地端口，无法确认注册时的真实节点，已跳过网络请求",
            }
        return {
            "proxy": account_proxy,
            "source": "account",
            "label": "账号专属代理",
            "slot": max(1, int(slot_index or 0) + 1),
            "purpose": str(purpose or ""),
        }
    store = get_proxy_pool_config(include_secret=True)
    node_mode = str(store.get("node_mode") or "direct_proxy")
    configured_fallback = normalize_proxy_url(str(store.get("fallback_proxy") or "").strip())
    legacy_fallback = normalize_proxy_url(str(fallback or "").strip())
    fallback_proxy = configured_fallback if node_mode == "managed_mihomo" else (legacy_fallback or configured_fallback)
    index = max(0, int(task_index or 1) - 1)
    try:
        slot = max(0, int(slot_index)) if slot_index is not None else index
    except Exception:
        slot = index
    try:
        thread_count = max(1, int(total_threads or 1))
    except Exception:
        thread_count = 1
    slot = slot % thread_count
    if not bool(store.get("enabled")):
        return {"proxy": fallback_proxy, "source": "fallback", "label": "默认代理" if fallback_proxy else "无代理", "slot": slot + 1, "purpose": str(purpose or "")}
    enabled_subscriptions = {str(item.get("id") or "") for item in store.get("subscriptions") or [] if item.get("enabled")}
    blacklist = [str(item).strip().lower() for item in store.get("node_blacklist") or [] if str(item).strip()]
    nodes = [
        node for node in store.get("nodes") or []
        if isinstance(node, dict)
        and node.get("enabled")
        and (not str(node.get("subscription_id") or "") or str(node.get("subscription_id") or "").startswith("mihomo_") or str(node.get("subscription_id") or "") in enabled_subscriptions)
        and not any(term in f"{node.get('name') or ''} {node.get('server') or ''}".lower() for term in blacklist)
    ]
    name_filter = str(node_name_filter or "").strip().lower()
    if name_filter:
        nodes = [node for node in nodes if name_filter in str(node.get("name") or "").lower()]
    if bool(store.get("fastest_mode")):
        nodes.sort(key=lambda node: (str(node.get("status") or "") != "ok", int(node.get("latency_ms") or 10**9)))
    assignment_index = index if str(store.get("mode") or "per_task") == "per_task" else slot
    strategy = str(store.get("strategy") or "round_robin")

    def choose(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not items:
            return None
        if strategy == "random":
            return random.choice(items)
        return items[assignment_index % len(items)]

    direct_nodes = [node for node in nodes if str(node.get("proxy_url") or "").strip()]
    if node_mode == "direct_proxy" and direct_nodes:
        node = choose(direct_nodes)
        assert node is not None
        return {
            "proxy": normalize_proxy_url(str(node.get("proxy_url") or "")),
            "source": "proxy_pool",
            "node_id": node.get("id"),
            "label": node.get("name") or node.get("proxy_url") or "代理节点",
            "node": node,
            "slot": slot + 1,
        }
    if node_mode == "managed_mihomo":
        try:
            from services.managed_mihomo_service import managed_mihomo_manager

            return managed_mihomo_manager.assign(
                slot=slot,
                task_index=max(1, int(task_index or 1)),
                total_threads=thread_count,
                store=store,
                purpose=str(purpose or ""),
                name_filter=name_filter,
                validate_node=bool(validate_node),
            )
        except Exception as error:
            return {
                "proxy": fallback_proxy,
                "source": "fallback",
                "requested_source": "managed_mihomo",
                "managed_failure": not bool(fallback_proxy),
                "label": "托管 Mihomo 分配失败",
                "slot": slot + 1,
                "error": str(error),
            }
    mihomo = store.get("mihomo") if isinstance(store.get("mihomo"), dict) else {}
    if node_mode == "mihomo_ports" and bool(mihomo.get("enabled")) and nodes:
        node = choose(nodes)
        assert node is not None
        if not bool(mihomo.get("thread_port_mode")):
            try:
                _mihomo_select_node(mihomo, str(node.get("name") or ""))
            except Exception as error:
                return {
                    "proxy": fallback_proxy,
                    "source": "fallback",
                    "label": "外部 Mihomo 切换失败",
                    "slot": slot + 1,
                    "error": str(error),
                }
        proxy_url = _mihomo_proxy_for_slot(mihomo, slot)
        return {
            "proxy": proxy_url,
            "source": "mihomo_pool",
            "node_id": node.get("id"),
            "label": node.get("name") or f"Mihomo-{slot + 1}",
            "node": node,
            "slot": slot + 1,
            "warning": "单端口外部 Mihomo 无法保证多线程节点隔离" if not bool(mihomo.get("thread_port_mode")) and thread_count > 1 else "",
        }
    return {"proxy": fallback_proxy, "source": "fallback", "label": "默认代理" if fallback_proxy else "无代理", "slot": slot + 1}


def report_proxy_failure(assignment: dict[str, Any], reason: object, quarantine_seconds: int = 300) -> dict[str, Any]:
    if str((assignment or {}).get("source") or "") != "managed_mihomo":
        return {"ok": False, "message": "assignment is not managed Mihomo"}
    from services.managed_mihomo_service import managed_mihomo_manager

    return managed_mihomo_manager.report_failure(assignment, reason, quarantine_seconds=quarantine_seconds)


def test_node(node_id: str = "", proxy_url: str = "") -> dict[str, Any]:
    target_url = normalize_proxy_url(proxy_url)
    node: dict[str, Any] | None = None
    with _lock:
        store = _load()
        mihomo = dict(store.get("mihomo") or {})
        if node_id:
            node = next((item for item in store["nodes"] if str(item.get("id")) == str(node_id)), None)
            if node:
                target_url = normalize_proxy_url(str(node.get("proxy_url") or ""))
                if not target_url and bool(mihomo.get("enabled")):
                    try:
                        _mihomo_select_node(mihomo, str(node.get("name") or ""))
                    except Exception as error:
                        result = {"ok": False, "error": str(error), "latency_ms": 0}
                        node["status"] = "failed"
                        node["latency_ms"] = 0
                        node["last_checked_at"] = _now()
                        node["updated_at"] = _now()
                        _save(store)
                        return result
                    target_url = _mihomo_proxy_for_slot(mihomo, 0)
        result = test_proxy(target_url)
        if node:
            node["status"] = "ok" if result.get("ok") else "failed"
            node["latency_ms"] = int(result.get("latency_ms") or 0)
            node["last_checked_at"] = _now()
            node["updated_at"] = _now()
            _save(store)
    return result


def _validate_subscription_url(url: str, *, allow_private: bool = False, resolve: bool = True) -> str:
    value = str(url or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise RuntimeError("Subscription URL must use http or https")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise RuntimeError("Subscription URL must have a valid host and no embedded credentials")
    host = parsed.hostname.strip().lower()
    if not allow_private and (host == "localhost" or host.endswith(".local")):
        raise RuntimeError("Private subscription hosts require allow_private=true")
    if not allow_private and resolve:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    host,
                    parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as error:
            raise RuntimeError("Subscription host could not be resolved") from error
        for raw_address in addresses:
            try:
                address = ipaddress.ip_address(raw_address.split("%", 1)[0])
            except ValueError:
                continue
            if address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
                raise RuntimeError("Private subscription hosts require allow_private=true")
    return value


class _SafeSubscriptionRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allow_private: bool) -> None:
        super().__init__()
        self.allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _validate_subscription_url(newurl, allow_private=self.allow_private, resolve=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_text(url: str, *, allow_private: bool = False) -> str:
    safe_url = _validate_subscription_url(url, allow_private=allow_private, resolve=True)
    req = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": "Mozilla/5.0 (MoyuRegister/1.0)",
            "Accept": "text/plain,application/yaml,application/json,*/*",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_SafeSubscriptionRedirectHandler(allow_private=allow_private))
    try:
        with opener.open(req, timeout=30) as resp:
            _validate_subscription_url(str(resp.geturl() or safe_url), allow_private=allow_private, resolve=True)
            try:
                content_length = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > MAX_SUBSCRIPTION_BYTES:
                raise RuntimeError("Subscription response exceeds 5 MiB")
            raw = resp.read(MAX_SUBSCRIPTION_BYTES + 1)
    except urllib.error.HTTPError as error:
        detail = ""
        raise RuntimeError(f"订阅下载失败 HTTP {error.code}: {detail}") from error
    if len(raw) > MAX_SUBSCRIPTION_BYTES:
        raise RuntimeError("Subscription response exceeds 5 MiB")
    return raw.decode("utf-8", errors="replace")


def _decode_possible_base64(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "\n" in raw or "://" in raw or "proxies:" in raw:
        return raw
    compact = re.sub(r"\s+", "", raw)
    try:
        padded = compact + "=" * (-len(compact) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
        if "://" in decoded or "\n" in decoded:
            return decoded
    except Exception:
        pass
    return raw


def _parse_subscription_nodes(text: str, *, subscription_id: str = "", source: str = "subscription") -> list[dict[str, Any]]:
    decoded = _decode_possible_base64(text)
    nodes: list[dict[str, Any]] = []
    nodes.extend(_parse_manual_proxy_lines(decoded, source=source, subscription_id=subscription_id))
    nodes.extend(_parse_clash_like_nodes(decoded, source=source, subscription_id=subscription_id))
    return _normalize_nodes(nodes)


def _parse_manual_proxy_lines(text: str, *, source: str = "manual", subscription_id: str = "") -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for raw_line in str(text or "").replace("\r", "\n").split("\n"):
        line = raw_line.strip().strip("- ").strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(":") or re.match(r"^[A-Za-z_][\w-]*:\s+", line):
            continue
        name = ""
        value = line
        if "=" in line and not line.lower().startswith(("http://", "https://", "socks://", "socks5://", "socks5h://", "ss://", "vmess://", "vless://", "trojan://", "hysteria://", "hysteria2://", "hy2://")):
            name, value = [part.strip() for part in line.split("=", 1)]
        elif "," in line and not line.lower().startswith(("ss://", "vmess://", "vless://", "trojan://", "hysteria://", "hysteria2://", "hy2://")):
            maybe_name, maybe_value = [part.strip() for part in line.split(",", 1)]
            if "://" in maybe_value:
                name, value = maybe_name, maybe_value
        parsed = urllib.parse.urlparse(value if "://" in value else f"http://{value}")
        try:
            parsed_port = str(parsed.port or "")
        except ValueError:
            continue
        scheme = parsed.scheme.lower()
        if scheme in {"http", "https", "socks", "socks5", "socks5h"} and parsed.netloc:
            proxy_url = normalize_proxy_url(value)
            nodes.append({
                "name": name or urllib.parse.unquote(parsed.fragment or "") or parsed.hostname or proxy_url,
                "type": scheme,
                "server": parsed.hostname or "",
                "port": parsed_port,
                "proxy_url": proxy_url,
                "subscription_id": subscription_id,
                "source": source,
                "enabled": True,
            })
            continue
        if scheme in {"ss", "vmess", "vless", "trojan", "hysteria", "hysteria2", "hy2"}:
            nodes.append({
                "name": name or urllib.parse.unquote(parsed.fragment or "") or parsed.hostname or scheme.upper(),
                "type": scheme,
                "server": parsed.hostname or "",
                "port": parsed_port,
                "proxy_url": "",
                "subscription_id": subscription_id,
                "source": source,
                "enabled": True,
            })
    return nodes


def _parse_clash_like_nodes(text: str, *, source: str = "subscription", subscription_id: str = "") -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    in_proxies = False
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("proxies:"):
            in_proxies = True
            continue
        if not in_proxies and not stripped.startswith("-"):
            continue
        if stripped.startswith("-"):
            if current.get("name"):
                nodes.append(_clash_current_to_node(current, source, subscription_id))
            current = {}
            stripped = stripped[1:].strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                for key, value in re.findall(r"([\w-]+)\s*:\s*([^,}]+)", stripped):
                    current[key.strip()] = value.strip().strip('"\'')
            elif stripped.startswith("name:"):
                current["name"] = stripped.split(":", 1)[1].strip().strip('"\'')
            continue
        match = re.match(r"([\w-]+)\s*:\s*(.+)$", stripped)
        if match and current is not None:
            current[match.group(1).strip()] = match.group(2).strip().strip('"\'')
    if current.get("name"):
        nodes.append(_clash_current_to_node(current, source, subscription_id))
    return [node for node in nodes if node.get("name")]


def _clash_current_to_node(current: dict[str, Any], source: str, subscription_id: str) -> dict[str, Any]:
    return {
        "name": str(current.get("name") or "未命名节点").strip(),
        "type": str(current.get("type") or "unknown").strip().lower() or "unknown",
        "server": str(current.get("server") or "").strip(),
        "port": str(current.get("port") or "").strip(),
        "proxy_url": "",
        "subscription_id": subscription_id,
        "source": source,
        "enabled": True,
    }


def _merge_nodes(store: dict[str, Any], nodes: list[dict[str, Any]]) -> int:
    existing = {str(node.get("id")): node for node in _normalize_nodes(store.get("nodes"))}
    imported = 0
    now = _now()
    for node in _normalize_nodes(nodes):
        old = existing.get(str(node.get("id")))
        if old:
            node["created_at"] = old.get("created_at") or node.get("created_at") or now
        else:
            imported += 1
        node["updated_at"] = now
        existing[str(node.get("id"))] = node
    store["nodes"] = list(existing.values())
    return imported
