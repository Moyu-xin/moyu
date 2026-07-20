from __future__ import annotations

import base64
import json
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from curl_cffi import CurlMime, requests

from services import task_proxy_service
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.registered_account_store import account_matches_selector, list_registered_accounts, update_registered_account

CPA_CONFIG_FILE = DATA_DIR / "cpa_config.json"
CPA_HEALTH_FILE = DATA_DIR / "cpa_warehouse_health.json"

_config_lock = threading.RLock()
_state_lock = threading.RLock()
_scheduler_lock = threading.Lock()
_scheduler: threading.Thread | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "base_url": "",
        "management_key": "",
        "skip_ssl_verify": False,
        "auto_import": False,
        "scheduled_enabled": False,
        "check_interval_minutes": 60,
        "concurrency": 5,
        "quota_threshold": 0,
        "disable_on_failure": True,
        "delete_dead_accounts": False,
        "delete_quota_exhausted": False,
        "notes": "imported from chatgpt2api-local",
        "last_import": None,
        "updated_at": "",
    }


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = _default_config()
    cfg.update({key: value for key, value in raw.items() if key in cfg})
    for key in (
        "enabled",
        "skip_ssl_verify",
        "auto_import",
        "scheduled_enabled",
        "disable_on_failure",
        "delete_dead_accounts",
        "delete_quota_exhausted",
    ):
        cfg[key] = bool(cfg.get(key))
    cfg["base_url"] = str(cfg.get("base_url") or "").strip().rstrip("/")
    cfg["management_key"] = str(cfg.get("management_key") or "").strip()
    cfg["check_interval_minutes"] = max(1, int(cfg.get("check_interval_minutes") or 60))
    cfg["concurrency"] = max(1, min(50, int(cfg.get("concurrency") or 5)))
    cfg["quota_threshold"] = max(0.0, min(100.0, float(cfg.get("quota_threshold") or 0)))
    cfg["notes"] = str(cfg.get("notes") or "")
    cfg["updated_at"] = str(cfg.get("updated_at") or "")
    return cfg


def _load_config() -> dict[str, Any]:
    raw = read_json(CPA_CONFIG_FILE, {}, expected_schema_version=1)
    return _normalize(raw if isinstance(raw, dict) else {})


def _save_config(cfg: dict[str, Any]) -> None:
    CPA_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(CPA_CONFIG_FILE, _normalize(cfg), schema_version=1)


def _default_state() -> dict[str, Any]:
    return {
        "accounts": {},
        "last_scheduled_run_at": "",
        "last_scheduled_result": None,
        "updated_at": "",
    }


def _load_state() -> dict[str, Any]:
    raw = read_json(CPA_HEALTH_FILE, {}, expected_schema_version=1)
    state = _default_state()
    if isinstance(raw, dict):
        state.update({key: value for key, value in raw.items() if key in state})
    if not isinstance(state.get("accounts"), dict):
        state["accounts"] = {}
    return state


def _save_state(state: dict[str, Any]) -> None:
    CPA_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    write_json(CPA_HEALTH_FILE, state, schema_version=1)


def _preview(value: Any, keep: int = 8) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}...{text[-keep:]}"


def _public_config(cfg: dict[str, Any], *, include_secret: bool = False) -> dict[str, Any]:
    item = json.loads(json.dumps(cfg, ensure_ascii=False))
    item["has_management_key"] = bool(item.get("management_key"))
    item["management_key_preview"] = _preview(item.get("management_key"))
    with _state_lock:
        state = _load_state()
    item["schedule_status"] = {
        "running": bool(item.get("scheduled_enabled")) and bool(_scheduler and _scheduler.is_alive()),
        "last_run_at": state.get("last_scheduled_run_at") or "",
        "last_result": state.get("last_scheduled_result"),
    }
    if not include_secret:
        item.pop("management_key", None)
    return item


def get_cpa_config(*, include_secret: bool = False) -> dict[str, Any]:
    with _config_lock:
        cfg = _load_config()
    _ensure_scheduler(cfg)
    return _public_config(cfg, include_secret=include_secret)


def update_cpa_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _config_lock:
        cfg = _load_config()
        for key, value in (updates or {}).items():
            if key not in cfg:
                continue
            if key == "management_key" and not str(value or "").strip():
                continue
            cfg[key] = value
        cfg["updated_at"] = _now()
        cfg = _normalize(cfg)
        _save_config(cfg)
    _ensure_scheduler(cfg)
    return _public_config(cfg)


def _auth_files_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    lower = normalized.lower()
    if not normalized:
        return ""
    if lower.endswith("/auth-files"):
        return normalized
    if lower.endswith("/v0/management") or lower.endswith("/management"):
        return f"{normalized}/auth-files"
    if lower.endswith("/v0"):
        return f"{normalized}/management/auth-files"
    return f"{normalized}/v0/management/auth-files"


def _api_call_url(base_url: str) -> str:
    auth_url = _auth_files_url(base_url)
    return auth_url[: -len("/auth-files")] + "/api-call" if auth_url else ""


def _session(
    *,
    cfg: dict[str, Any] | None = None,
    purpose: str = "cpa",
    index: int = 1,
    account: dict[str, Any] | None = None,
    total_threads: int = 1,
) -> requests.Session:
    current = cfg or _load_config()
    kwargs, _assignment = task_proxy_service.build_task_session_kwargs(
        index,
        purpose=purpose,
        target_url=str(current.get("base_url") or ""),
        account=account,
        total_threads=total_threads,
        slot_index=max(0, int(index or 1) - 1),
        verify=not bool(current.get("skip_ssl_verify")),
    )
    return requests.Session(**kwargs)


def _verify_tls(cfg: dict[str, Any]) -> bool:
    return not bool(cfg.get("skip_ssl_verify")) and task_proxy_service.proxy_settings.verify_tls()


def _headers(cfg: dict[str, Any], *, content_type: bool = True) -> dict[str, str]:
    key = str(cfg.get("management_key") or "").strip()
    if not key:
        raise RuntimeError("请填写 CPA/CLIProxyAPI 管理密钥")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _json_response(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"raw": str(getattr(resp, "text", "") or "")[:2000]}


def _require_config() -> dict[str, Any]:
    with _config_lock:
        cfg = _load_config()
    if not cfg.get("enabled"):
        raise RuntimeError("请先启用 CPA 仓库")
    if not _auth_files_url(str(cfg.get("base_url") or "")):
        raise RuntimeError("请填写 CPA/CLIProxyAPI 地址")
    _headers(cfg)
    return cfg


def _is_cpa_account(item: dict[str, Any]) -> bool:
    provider = str(item.get("provider") or item.get("type") or "").strip().lower()
    return "codex" in provider or "openai" in provider or "grok" in provider or "xai" in provider


def _status_of(item: dict[str, Any]) -> str:
    if bool(item.get("disabled")):
        return "disabled"
    status = str(item.get("status") or "").strip().lower()
    if status in {"disabled", "unavailable", "error", "failed"} or bool(item.get("unavailable")):
        return "disabled" if status == "disabled" else "failed"
    return "active"


def _merge_cached_health(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with _state_lock:
        cached = _load_state().get("accounts") or {}
    result: list[dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        name = str(item.get("name") or item.get("id") or "")
        state = cached.get(name) if isinstance(cached, dict) else None
        if isinstance(state, dict):
            item["warehouse_health"] = state
        item["warehouse_status"] = _status_of(item)
        result.append(item)
    return result


def test_cpa_connection() -> dict[str, Any]:
    cfg = _require_config()
    session = _session()
    url = _auth_files_url(str(cfg.get("base_url") or ""))
    resp = session.get(url, headers=_headers(cfg, content_type=False), timeout=30, verify=_verify_tls(cfg))
    data = _json_response(resp)
    files = data.get("files") if isinstance(data.get("files"), list) else []
    if resp.status_code != 200:
        raise RuntimeError(f"CPA 仓库连接失败: HTTP {resp.status_code}, {json.dumps(data, ensure_ascii=False)[:800]}")
    return {
        "ok": True,
        "status_code": int(resp.status_code),
        "accounts": sum(1 for item in files if isinstance(item, dict) and _is_cpa_account(item)),
        "tested_at": _now(),
        "url": url,
    }


def list_cpa_accounts(*, page: int = 1, page_size: int = 20, search: str = "", status: str = "all") -> dict[str, Any]:
    cfg = _require_config()
    session = _session()
    resp = session.get(
        _auth_files_url(str(cfg.get("base_url") or "")),
        headers=_headers(cfg, content_type=False),
        timeout=45,
        verify=_verify_tls(cfg),
    )
    data = _json_response(resp)
    if resp.status_code != 200:
        raise RuntimeError(f"CPA 账号列表读取失败: HTTP {resp.status_code}, {json.dumps(data, ensure_ascii=False)[:800]}")
    files = data.get("files") if isinstance(data.get("files"), list) else []
    items = _merge_cached_health([item for item in files if isinstance(item, dict) and _is_cpa_account(item)])
    stats = {
        "total": len(items),
        "active": sum(1 for item in items if item.get("warehouse_status") == "active"),
        "disabled": sum(1 for item in items if item.get("warehouse_status") == "disabled"),
        "failed": sum(1 for item in items if item.get("warehouse_status") == "failed"),
        "tested": sum(1 for item in items if isinstance(item.get("warehouse_health"), dict)),
    }
    needle = str(search or "").strip().lower()
    status_filter = str(status or "all").strip().lower()
    if needle:
        items = [item for item in items if needle in json.dumps(item, ensure_ascii=False).lower()]
    if status_filter not in {"", "all"}:
        items = [item for item in items if str(item.get("warehouse_status") or "") == status_filter]
    items.sort(key=lambda item: str(item.get("email") or item.get("name") or "").lower())
    total = len(items)
    page_number = max(1, int(page or 1))
    size = max(1, int(page_size or 20))
    start = (page_number - 1) * size
    return {
        "ok": True,
        "page": page_number,
        "page_size": size,
        "total": total,
        "items": items[start : start + size],
        "stats": stats,
    }


def _decode_body(payload: dict[str, Any]) -> dict[str, Any]:
    body = payload.get("body")
    if isinstance(body, dict):
        return body
    if isinstance(body, str) and body.strip():
        try:
            data = json.loads(body)
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return {"raw": body[:2000]}
    return payload


def _window_remaining(window: Any) -> float | None:
    if not isinstance(window, dict):
        return None
    value = window.get("remaining_percent")
    if isinstance(value, (int, float)):
        return max(0.0, min(100.0, float(value)))
    used = window.get("used_percent")
    if isinstance(used, (int, float)):
        return max(0.0, min(100.0, 100.0 - float(used)))
    return None


def _usage_details(payload: dict[str, Any]) -> dict[str, Any]:
    body = _decode_body(payload)
    rate = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}
    code_rate = body.get("code_review_rate_limit") if isinstance(body.get("code_review_rate_limit"), dict) else {}
    primary_remaining = _window_remaining(rate.get("primary_window"))
    secondary_remaining = _window_remaining(rate.get("secondary_window"))
    code_remaining = _window_remaining(code_rate.get("primary_window"))
    return {
        "plan_type": str(body.get("plan_type") or body.get("account_plan") or ""),
        "allowed": rate.get("allowed"),
        "limit_reached": bool(rate.get("limit_reached")),
        "primary_remaining_percent": primary_remaining,
        "primary_used_percent": round(100.0 - primary_remaining, 2) if primary_remaining is not None else None,
        "secondary_remaining_percent": secondary_remaining,
        "code_review_remaining_percent": code_remaining,
        "raw": body,
    }


def _failure_reason(status_code: int, body: dict[str, Any], usage: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str]:
    if status_code >= 400:
        error = body.get("error")
        if isinstance(error, dict):
            return "failed", str(error.get("message") or error.get("type") or f"HTTP {status_code}")
        return "failed", str(body.get("message") or body.get("raw") or f"HTTP {status_code}")[:800]
    if usage.get("limit_reached") or usage.get("allowed") is False:
        return "quota_exhausted", "周限额已耗尽"
    remaining = usage.get("primary_remaining_percent")
    threshold = float(cfg.get("quota_threshold") or 0)
    if isinstance(remaining, (int, float)) and threshold > 0 and float(remaining) < threshold:
        return "quota_low", f"周限额剩余 {float(remaining):.1f}%，低于阈值 {threshold:g}%"
    raw_text = json.dumps(body, ensure_ascii=False).lower()
    for marker in ("token_expired", "invalid_grant", "unauthorized", "account_deactivated"):
        if marker in raw_text:
            return "dead", marker
    return "ok", "正常"


def _find_cpa_account(name: str) -> dict[str, Any]:
    listing = list_cpa_accounts(page=1, page_size=100000)
    target = str(name or "").strip()
    for item in listing.get("items") or []:
        if target in {str(item.get("name") or ""), str(item.get("id") or ""), str(item.get("auth_index") or "")}:
            return item
    raise RuntimeError(f"CPA 账号不存在: {target}")


def _cache_test_result(name: str, result: dict[str, Any]) -> None:
    with _state_lock:
        state = _load_state()
        state.setdefault("accounts", {})[name] = result
        _save_state(state)


def set_cpa_account_status(name: str, disabled: bool) -> dict[str, Any]:
    cfg = _require_config()
    session = _session()
    url = f"{_auth_files_url(str(cfg.get('base_url') or ''))}/status"
    resp = session.patch(
        url,
        headers=_headers(cfg),
        json={"name": str(name or "").strip(), "disabled": bool(disabled)},
        timeout=30,
        verify=_verify_tls(cfg),
    )
    data = _json_response(resp)
    return {"ok": resp.status_code in (200, 204), "status_code": int(resp.status_code), "response": data, "name": name}


def delete_cpa_account(name: str) -> dict[str, Any]:
    cfg = _require_config()
    session = _session()
    resp = session.delete(
        _auth_files_url(str(cfg.get("base_url") or "")),
        headers=_headers(cfg, content_type=False),
        params={"name": str(name or "").strip()},
        timeout=30,
        verify=_verify_tls(cfg),
    )
    data = _json_response(resp)
    return {"ok": resp.status_code in (200, 204), "status_code": int(resp.status_code), "response": data, "name": name}


def test_cpa_account(name: str, *, apply_policy: bool = False) -> dict[str, Any]:
    cfg = _require_config()
    item = _find_cpa_account(name)
    auth_index = str(item.get("auth_index") or "").strip()
    if not auth_index:
        raise RuntimeError(f"CPA 账号缺少 auth_index: {name}")
    headers = {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "codex_cli_rs/0.1.0",
    }
    account_id = str(item.get("account_id") or item.get("account") or "").strip()
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    payload = {
        "authIndex": auth_index,
        "method": "GET",
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "header": headers,
    }
    session = _session()
    resp = session.post(
        _api_call_url(str(cfg.get("base_url") or "")),
        headers=_headers(cfg),
        json=payload,
        timeout=90,
        verify=_verify_tls(cfg),
    )
    envelope = _json_response(resp)
    upstream_code = int(envelope.get("status_code") or resp.status_code or 0)
    body = _decode_body(envelope)
    usage = _usage_details(envelope)
    health_status, reason = _failure_reason(upstream_code, body, usage, cfg)
    result = {
        "ok": health_status == "ok",
        "name": str(item.get("name") or name),
        "auth_index": auth_index,
        "status": health_status,
        "error": "" if health_status == "ok" else reason,
        "status_code": upstream_code,
        "usage": usage,
        "tested_at": _now(),
    }
    if apply_policy and health_status != "ok":
        if health_status in {"quota_exhausted", "quota_low"} and cfg.get("delete_quota_exhausted"):
            result["policy_action"] = "delete"
            result["policy_result"] = delete_cpa_account(result["name"])
        elif health_status in {"dead", "failed"} and cfg.get("delete_dead_accounts"):
            result["policy_action"] = "delete"
            result["policy_result"] = delete_cpa_account(result["name"])
        elif cfg.get("disable_on_failure"):
            result["policy_action"] = "disable"
            result["policy_result"] = set_cpa_account_status(result["name"], True)
    _cache_test_result(result["name"], result)
    return result


def _clean_names(names: list[str] | None) -> list[str]:
    result: list[str] = []
    for raw in names or []:
        name = str(raw or "").strip()
        if name and name not in result:
            result.append(name)
    return result


def test_cpa_accounts(names: list[str] | None = None, *, apply_policy: bool = False) -> dict[str, Any]:
    cfg = _require_config()
    selected = _clean_names(names)
    if not selected:
        selected = [str(item.get("name") or "") for item in list_cpa_accounts(page=1, page_size=100000).get("items") or []]
    selected = [name for name in selected if name]
    if not selected:
        raise RuntimeError("CPA 仓库中没有可测活账号")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(len(selected), int(cfg.get("concurrency") or 5))) as executor:
        futures = {executor.submit(test_cpa_account, name, apply_policy=apply_policy): name for name in selected}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                result = {"ok": False, "name": name, "status": "failed", "error": str(error), "tested_at": _now()}
                _cache_test_result(name, result)
                results.append(result)
    success = sum(1 for item in results if item.get("ok"))
    return {
        "ok": success == len(results),
        "total": len(results),
        "success": success,
        "fail": len(results) - success,
        "results": results,
        "tested_at": _now(),
    }


def act_on_cpa_accounts(names: list[str], action: str) -> dict[str, Any]:
    selected = _clean_names(names)
    action_name = str(action or "").strip().lower()
    if action_name not in {"enable", "disable", "delete"}:
        raise RuntimeError("CPA 账号操作无效")
    if not selected:
        raise RuntimeError("请选择 CPA 账号")
    results: list[dict[str, Any]] = []
    for name in selected:
        if action_name == "delete":
            result = delete_cpa_account(name)
        else:
            result = set_cpa_account_status(name, action_name == "disable")
        result["action"] = action_name
        results.append(result)
    success = sum(1 for item in results if item.get("ok"))
    return {"ok": success == len(results), "total": len(results), "success": success, "fail": len(results) - success, "results": results}


def _jwt_payload(token: Any) -> dict[str, Any]:
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


def build_cpa_auth_file(account: dict[str, Any]) -> dict[str, Any]:
    if str(account.get("platform") or "").strip().lower() == "grok":
        return build_grok_cpa_auth_file(account)
    access_claims = _jwt_payload(account.get("access_token"))
    id_claims = _jwt_payload(account.get("id_token"))
    auth = access_claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    email = str(account.get("email") or id_claims.get("email") or "").strip()
    account_id = str(account.get("chatgpt_account_id") or account.get("workspace_id") or auth.get("chatgpt_account_id") or "").strip()
    expires_at = account.get("token_expires_at") or access_claims.get("exp") or id_claims.get("exp")
    try:
        expired = datetime.fromtimestamp(int(expires_at), timezone.utc).isoformat() if expires_at else ""
    except Exception:
        expired = ""
    result = {
        "type": "codex",
        "email": email,
        "access_token": str(account.get("access_token") or "").strip(),
        "refresh_token": str(account.get("refresh_token") or "").strip(),
        "id_token": str(account.get("id_token") or "").strip(),
        "account_id": account_id,
        "last_refresh": _now(),
        "expired": expired,
    }
    note = str(account.get("note") or "").strip()
    if note:
        result["note"] = note
    return {key: value for key, value in result.items() if value not in (None, "")}


def build_grok_cpa_auth_file(account: dict[str, Any]) -> dict[str, Any]:
    access_claims = _jwt_payload(account.get("access_token"))
    id_claims = _jwt_payload(account.get("id_token"))
    email = str(account.get("email") or id_claims.get("email") or access_claims.get("email") or "").strip()
    subject = str(account.get("subject") or id_claims.get("sub") or access_claims.get("sub") or "").strip()
    expires_at = account.get("token_expires_at") or id_claims.get("exp") or access_claims.get("exp")
    try:
        expired = datetime.fromtimestamp(int(expires_at), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if expires_at else ""
    except Exception:
        expired = str(expires_at or "")
    headers = account.get("headers") if isinstance(account.get("headers"), dict) else {
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": "0.2.93",
        "x-grok-client-identifier": "grok-shell",
    }
    result = {
        "type": "xai",
        "auth_kind": "oauth",
        "email": email,
        "sub": subject,
        "access_token": str(account.get("access_token") or "").strip(),
        "refresh_token": str(account.get("refresh_token") or "").strip(),
        "id_token": str(account.get("id_token") or "").strip(),
        "token_type": str(account.get("token_type") or "Bearer").strip(),
        "expires_in": account.get("token_expires_in"),
        "expired": expired,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "token_endpoint": str(account.get("token_endpoint") or "https://auth.x.ai/oauth2/token").strip(),
        "base_url": str(account.get("base_url") or "https://cli-chat-proxy.grok.com/v1").strip(),
        "disabled": bool(account.get("cpa_disabled", False)),
        "headers": headers,
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _select_accounts(selectors: list[str] | None = None) -> list[dict[str, Any]]:
    selected = [str(item or "").strip() for item in selectors or [] if str(item or "").strip()]
    accounts = list_registered_accounts(include_secret=True)
    if selected:
        accounts = [item for item in accounts if any(account_matches_selector(item, selector) for selector in selected)]
    return [item for item in accounts if str(item.get("access_token") or "").strip()]


def _cpa_filename(account: dict[str, Any]) -> str:
    email = str(account.get("email") or "unknown").strip()
    scope = str(account.get("chatgpt_account_id") or account.get("workspace_id") or "").strip()
    suffix = f"-{scope[:10]}" if scope else ""
    safe = "".join(char if char.isalnum() or char in {"@", ".", "_", "-"} else "_" for char in email)
    prefix = "xai-" if str(account.get("platform") or "").strip().lower() == "grok" else ""
    return f"{prefix}{safe}{suffix}.json"


def export_account_dicts_to_cpa(accounts: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    selected = [accounts] if isinstance(accounts, dict) else [item for item in accounts if isinstance(item, dict)]
    files = []
    for account in selected:
        document = build_cpa_auth_file(account)
        if not document.get("access_token"):
            continue
        files.append({
            "filename": _cpa_filename(account),
            "data": document,
        })
    return {
        "type": "cpa-auth-files",
        "version": 1,
        "exported_at": _now(),
        "count": len(files),
        "files": files,
    }


def _upload_cpa_account(account: dict[str, Any], cfg: dict[str, Any], *, index: int = 1, total_threads: int = 1) -> dict[str, Any]:
    filename = _cpa_filename(account)
    document = build_cpa_auth_file(account)
    if not document.get("access_token"):
        return {"ok": False, "filename": filename, "error": "missing_access_token"}
    session = _session(cfg=cfg, purpose="cpa_import", index=index, account=account, total_threads=total_threads)
    url = _auth_files_url(str(cfg.get("base_url") or ""))
    content = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    mime = CurlMime()
    mime.addpart(name="file", data=content, filename=filename, content_type="application/json")
    resp = session.post(url, multipart=mime, headers=_headers(cfg, content_type=False), timeout=45, verify=_verify_tls(cfg))
    if resp.status_code in (404, 405, 415):
        resp = session.post(
            f"{url}?name={urllib.parse.quote(filename)}",
            data=content,
            headers={**_headers(cfg, content_type=False), "Content-Type": "application/json"},
            timeout=45,
            verify=_verify_tls(cfg),
        )
    data = _json_response(resp)
    return {
        "ok": resp.status_code in (200, 201, 204),
        "status_code": int(resp.status_code),
        "filename": filename,
        "response": data,
    }


def import_accounts_to_cpa(selectors: list[str] | None = None) -> dict[str, Any]:
    cfg = _require_config()
    accounts = _select_accounts(selectors)
    return _import_account_list_to_cpa(accounts, cfg)


def import_account_dicts_to_cpa(accounts: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    cfg = _require_config()
    selected = [accounts] if isinstance(accounts, dict) else [item for item in accounts if isinstance(item, dict)]
    selected = [item for item in selected if str(item.get("access_token") or "").strip()]
    return _import_account_list_to_cpa(selected, cfg)


def _import_account_list_to_cpa(accounts: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    if not accounts:
        raise RuntimeError("账户库存中没有可导入 CPA 的 access_token 账号")
    results: list[dict[str, Any]] = []

    workers = max(1, min(len(accounts), int(cfg.get("concurrency") or 5), 50))

    def import_one(item: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        index, account = item
        try:
            result = _upload_cpa_account(account, cfg, index=index, total_threads=workers)
        except Exception as error:
            result = {"ok": False, "status_code": 0, "error": str(error)[:800]}
        return {**result, "email": str(account.get("email") or ""), "_account": account}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(import_one, item): item[1] for item in enumerate(accounts, start=1)}
        for future in as_completed(futures):
            account = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                results.append({
                    "ok": False,
                    "status_code": 0,
                    "error": str(error)[:800],
                    "email": str(account.get("email") or ""),
                    "_account": account,
                })

    imported_at = _now()
    for result in results:
        account = result.get("_account") if isinstance(result.get("_account"), dict) else {}
        updates = {
            "cpa_imported_at": imported_at,
            "cpa_import_status": "ok" if result.get("ok") else "failed",
            "cpa_auth_file": result.get("filename") or "",
            "cpa_last_error": "" if result.get("ok") else str(result.get("response") or result.get("error") or "unknown")[:800],
        }
        update_registered_account(account, updates)
    success = sum(1 for item in results if item.get("ok"))
    final = {
        "ok": success == len(results),
        "partial": success > 0 and success < len(results),
        "total": len(results),
        "success": success,
        "fail": len(results) - success,
        "results": [{key: value for key, value in result.items() if key != "_account"} for result in results],
        "imported_at": imported_at,
    }
    with _config_lock:
        latest = _load_config()
        latest["last_import"] = final
        _save_config(latest)
    return final


def _is_due(last_run: str, interval_minutes: int) -> bool:
    if not last_run:
        return True
    try:
        previous = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - previous).total_seconds() >= interval_minutes * 60
    except Exception:
        return True


def _schedule_loop() -> None:
    while True:
        with _config_lock:
            cfg = _load_config()
        if cfg.get("enabled") and cfg.get("scheduled_enabled"):
            with _state_lock:
                state = _load_state()
                last_run = str(state.get("last_scheduled_run_at") or "")
            if _is_due(last_run, int(cfg.get("check_interval_minutes") or 60)):
                try:
                    result = test_cpa_accounts(apply_policy=True)
                except Exception as error:
                    result = {"ok": False, "error": str(error), "tested_at": _now()}
                with _state_lock:
                    state = _load_state()
                    state["last_scheduled_run_at"] = _now()
                    state["last_scheduled_result"] = result
                    _save_state(state)
        time.sleep(15)


def _ensure_scheduler(cfg: dict[str, Any] | None = None) -> None:
    global _scheduler
    current = cfg or _load_config()
    if not current.get("scheduled_enabled"):
        return
    with _scheduler_lock:
        if _scheduler and _scheduler.is_alive():
            return
        _scheduler = threading.Thread(target=_schedule_loop, daemon=True, name="cpa-warehouse-scheduler")
        _scheduler.start()
