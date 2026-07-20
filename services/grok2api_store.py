from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from curl_cffi import CurlMime, requests

from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.registered_account_store import (
    account_matches_selector,
    list_registered_accounts,
    update_registered_accounts_bulk,
)

GROK2API_CONFIG_FILE = DATA_DIR / "grok2api_config.json"

_config_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "base_url": "",
        "username": "",
        "password": "",
        "admin_token": "",
        "skip_ssl_verify": False,
        "notes": "Grok Web SSO imported from chatgpt2api-local",
        "last_import": None,
        "updated_at": "",
    }


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = _default_config()
    cfg.update({key: value for key, value in (raw or {}).items() if key in cfg})
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["skip_ssl_verify"] = bool(cfg.get("skip_ssl_verify"))
    cfg["base_url"] = str(cfg.get("base_url") or "").strip().rstrip("/")
    for key in ("username", "password", "admin_token", "notes", "updated_at"):
        cfg[key] = str(cfg.get(key) or "").strip()
    return cfg


def _load_config() -> dict[str, Any]:
    raw = read_json(GROK2API_CONFIG_FILE, {}, expected_schema_version=1)
    return _normalize(raw if isinstance(raw, dict) else {})


def _save_config(cfg: dict[str, Any]) -> None:
    GROK2API_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(GROK2API_CONFIG_FILE, _normalize(cfg), schema_version=1)


def _preview(value: Any, keep: int = 8) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}...{text[-keep:]}"


def _public_config(cfg: dict[str, Any], *, include_secret: bool = False) -> dict[str, Any]:
    item = json.loads(json.dumps(cfg, ensure_ascii=False))
    item["has_password"] = bool(item.get("password"))
    item["has_admin_token"] = bool(item.get("admin_token"))
    item["admin_token_preview"] = _preview(item.get("admin_token"))
    if not include_secret:
        item.pop("password", None)
        item.pop("admin_token", None)
    return item


def get_grok2api_config(*, include_secret: bool = False) -> dict[str, Any]:
    with _config_lock:
        cfg = _load_config()
    return _public_config(cfg, include_secret=include_secret)


def update_grok2api_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _config_lock:
        cfg = _load_config()
        for key, value in (updates or {}).items():
            if key not in cfg:
                continue
            if key in {"password", "admin_token"} and not str(value or "").strip():
                continue
            cfg[key] = value
        cfg["updated_at"] = _now()
        cfg = _normalize(cfg)
        _save_config(cfg)
    return _public_config(cfg)


def _root_url(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    lower = value.lower()
    for suffix in ("/api/admin/v1", "/v1"):
        if lower.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            break
    return value


def _admin_url(cfg: dict[str, Any], path: str) -> str:
    root = _root_url(str(cfg.get("base_url") or ""))
    if not root:
        return ""
    return f"{root}/api/admin/v1/{str(path or '').lstrip('/')}"


def _session() -> requests.Session:
    return requests.Session()


def _verify_tls(cfg: dict[str, Any]) -> bool:
    return not bool(cfg.get("skip_ssl_verify"))


def _json_response(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"raw": str(getattr(resp, "text", "") or "")[:4000]}


def _extract_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _require_config() -> dict[str, Any]:
    with _config_lock:
        cfg = _load_config()
    if not cfg.get("enabled"):
        raise RuntimeError("请先启用 grok2api 仓库")
    if not _root_url(str(cfg.get("base_url") or "")):
        raise RuntimeError("请填写 grok2api 地址")
    if not str(cfg.get("admin_token") or "").strip() and not (
        str(cfg.get("username") or "").strip() and str(cfg.get("password") or "").strip()
    ):
        raise RuntimeError("请填写 grok2api 管理员用户名和密码，或管理员 Token")
    return cfg


def _login_token(session: requests.Session, cfg: dict[str, Any]) -> str:
    username = str(cfg.get("username") or "").strip()
    password = str(cfg.get("password") or "").strip()
    if username and password:
        url = _admin_url(cfg, "auth/login")
        try:
            resp = session.post(
                url,
                json={"username": username, "password": password},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=30,
                verify=_verify_tls(cfg),
            )
        except Exception as error:
            raise RuntimeError(f"grok2api 管理员登录连接失败: {error}") from error
        data = _json_response(resp)
        body = _extract_data(data)
        tokens = body.get("tokens") if isinstance(body.get("tokens"), dict) else {}
        token = str(tokens.get("accessToken") or body.get("accessToken") or "").strip()
        if int(resp.status_code) not in range(200, 300) or not token:
            raise RuntimeError(
                f"grok2api 管理员登录失败: HTTP {resp.status_code}, {json.dumps(data, ensure_ascii=False)[:800]}"
            )
        return token
    token = str(cfg.get("admin_token") or "").strip()
    if not token:
        raise RuntimeError("grok2api 管理员 Token 为空")
    return token


def _auth_headers(token: str, *, content_type: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _parse_sse(value: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    event_name = "message"
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = "message"
            return
        raw = "\n".join(data_lines)
        try:
            payload: Any = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        events.append({"event": event_name, "data": payload})
        event_name = "message"
        data_lines = []

    for raw_line in str(value or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    flush()
    return events


def _complete_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("event") == "complete" and isinstance(event.get("data"), dict):
            return event["data"]
    return {}


def _error_message(resp: Any, data: dict[str, Any]) -> str:
    body = _extract_data(data)
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    return str(
        error.get("message")
        or body.get("message")
        or body.get("raw")
        or f"HTTP {getattr(resp, 'status_code', 0)}"
    )[:800]


def test_grok2api_connection() -> dict[str, Any]:
    cfg = _require_config()
    session = _session()
    token = _login_token(session, cfg)
    url = _admin_url(cfg, "accounts/summary")
    try:
        resp = session.get(url, headers=_auth_headers(token, content_type=False), timeout=30, verify=_verify_tls(cfg))
    except Exception as error:
        raise RuntimeError(f"grok2api 仓库连接失败: {error}") from error
    data = _json_response(resp)
    if int(resp.status_code) not in range(200, 300):
        raise RuntimeError(f"grok2api 仓库连接失败: {_error_message(resp, data)}")
    body = _extract_data(data)
    providers = body.get("providers") if isinstance(body.get("providers"), dict) else {}
    web = providers.get("grok_web") if isinstance(providers.get("grok_web"), dict) else {}
    return {
        "ok": True,
        "status_code": int(resp.status_code),
        "accounts": int(web.get("total") or body.get("total") or 0),
        "available": int(web.get("available") or body.get("available") or 0),
        "tested_at": _now(),
        "url": url,
    }


def _select_accounts(selectors: list[str] | None = None) -> list[dict[str, Any]]:
    selected = {str(item).strip().lower() for item in selectors or [] if str(item).strip()}
    accounts = list_registered_accounts(include_secret=True)
    if selected:
        accounts = [item for item in accounts if any(account_matches_selector(item, selector) for selector in selected)]
    return [
        item
        for item in accounts
        if str(item.get("platform") or "").strip().lower() == "grok"
        and bool(str(item.get("sso_token") or item.get("sso") or "").strip())
    ]


def _build_import_document(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    for account in accounts:
        token = str(account.get("sso_token") or account.get("sso") or "").strip()
        if not token:
            continue
        name = str(account.get("email") or account.get("name") or "Grok Web").strip()
        items.append({"name": name, "sso_token": token, "tier": "auto"})
    return {"provider": "grok_web", "accounts": items}


def _import_account_list_to_grok2api(accounts: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    document = _build_import_document(accounts)
    import_items = document["accounts"]
    if not import_items:
        raise RuntimeError("当前选择中没有带 xAI SSO 的 Grok 账号")

    session = _session()
    token = _login_token(session, cfg)
    content = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    mime = CurlMime()
    mime.addpart(name="file", data=content, filename="grok-web-sso.json", content_type="application/json")
    url = _admin_url(cfg, "accounts/web/import")
    try:
        resp = session.post(
            url,
            multipart=mime,
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
            timeout=180,
            verify=_verify_tls(cfg),
        )
    except Exception as error:
        raise RuntimeError(f"grok2api SSO 导入连接失败: {error}") from error
    events = _parse_sse(str(getattr(resp, "text", "") or ""))
    complete = _complete_event(events)
    status_ok = int(resp.status_code) in range(200, 300)
    imported = int(complete.get("created") or 0) + int(complete.get("updated") or 0)
    ok = status_ok and bool(complete) and imported > 0
    error = ""
    if not ok:
        error_event = next(
            (event.get("data") for event in reversed(events) if event.get("event") == "error" and isinstance(event.get("data"), dict)),
            {},
        )
        error = str(error_event.get("message") or error_event.get("code") or "")
        if not error:
            error = _error_message(resp, _json_response(resp)) if not status_ok else "grok2api 未返回有效的 complete 导入事件"

    imported_at = _now()
    status_updates = {
        str(account.get("inventory_id") or account.get("email") or "").strip(): {
                "grok2api_import_status": "ok" if ok else "failed",
                "grok2api_imported_at": imported_at if ok else "",
                "grok2api_last_error": error if not ok else "",
        }
        for account in accounts
        if str(account.get("inventory_id") or account.get("email") or "").strip()
    }
    if status_updates:
        update_registered_accounts_bulk(status_updates)

    result = {
        "ok": ok,
        "total": len(import_items),
        "created": int(complete.get("created") or 0),
        "updated": int(complete.get("updated") or 0),
        "synced": int(complete.get("synced") or 0),
        "sync_failed": int(complete.get("syncFailed") or 0),
        "status_code": int(resp.status_code),
        "error": error,
        "events": events[-20:],
        "url": url,
        "imported_at": imported_at,
    }
    with _config_lock:
        stored = _load_config()
        stored["last_import"] = {key: value for key, value in result.items() if key not in {"events"}}
        stored["updated_at"] = imported_at
        _save_config(stored)
    return result


def import_accounts_to_grok2api(selectors: list[str] | None = None) -> dict[str, Any]:
    cfg = _require_config()
    return _import_account_list_to_grok2api(_select_accounts(selectors), cfg)


def export_account_dicts_to_grok2api(accounts: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    selected = [accounts] if isinstance(accounts, dict) else [item for item in accounts if isinstance(item, dict)]
    selected = [
        item
        for item in selected
        if str(item.get("platform") or "").strip().lower() == "grok"
        and bool(str(item.get("sso_token") or item.get("sso") or "").strip())
    ]
    document = _build_import_document(selected)
    return {"count": len(document.get("accounts") or []), "data": document}


def import_account_dicts_to_grok2api(accounts: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    cfg = _require_config()
    selected = [accounts] if isinstance(accounts, dict) else [item for item in accounts if isinstance(item, dict)]
    selected = [
        item
        for item in selected
        if str(item.get("platform") or "").strip().lower() == "grok"
        and bool(str(item.get("sso_token") or item.get("sso") or "").strip())
    ]
    return _import_account_list_to_grok2api(selected, cfg)


def list_grok2api_accounts(*, page: int = 1, page_size: int = 20, search: str = "", status: str = "") -> dict[str, Any]:
    cfg = _require_config()
    session = _session()
    token = _login_token(session, cfg)
    url = _admin_url(cfg, "accounts")
    params: dict[str, Any] = {
        "page": max(1, int(page or 1)),
        "pageSize": max(1, min(200, int(page_size or 20))),
        "provider": "grok_web",
    }
    if str(search or "").strip():
        params["search"] = str(search).strip()
    if str(status or "").strip().lower() not in {"", "all"}:
        params["status"] = str(status).strip()
    try:
        resp = session.get(
            url,
            params=params,
            headers=_auth_headers(token, content_type=False),
            timeout=45,
            verify=_verify_tls(cfg),
        )
    except Exception as error:
        raise RuntimeError(f"grok2api 账号列表连接失败: {error}") from error
    data = _json_response(resp)
    if int(resp.status_code) not in range(200, 300):
        raise RuntimeError(f"grok2api 账号列表读取失败: {_error_message(resp, data)}")
    body = _extract_data(data)
    items = body.get("items") if isinstance(body.get("items"), list) else []
    return {
        "ok": True,
        "page": int(body.get("page") or params["page"]),
        "page_size": int(body.get("pageSize") or params["pageSize"]),
        "total": int(body.get("total") or 0),
        "items": [item for item in items if isinstance(item, dict)],
    }


def refresh_grok2api_accounts(account_ids: list[int | str]) -> dict[str, Any]:
    cfg = _require_config()
    ids = [str(item).strip() for item in account_ids or [] if str(item).strip()]
    if not ids:
        raise RuntimeError("请选择 grok2api 账号")
    session = _session()
    token = _login_token(session, cfg)
    url = _admin_url(cfg, "accounts/batch/refresh-quotas")
    try:
        resp = session.post(
            url,
            json={"ids": ids, "provider": "grok_web"},
            headers=_auth_headers(token),
            timeout=180,
            verify=_verify_tls(cfg),
        )
    except Exception as error:
        raise RuntimeError(f"grok2api 额度刷新连接失败: {error}") from error
    data = _json_response(resp)
    if int(resp.status_code) not in range(200, 300):
        raise RuntimeError(f"grok2api 额度刷新失败: {_error_message(resp, data)}")
    return {"ok": True, "total": len(ids), **_extract_data(data)}


def act_on_grok2api_accounts(account_ids: list[int | str], action: str) -> dict[str, Any]:
    cfg = _require_config()
    ids = [str(item).strip() for item in account_ids or [] if str(item).strip()]
    if not ids:
        raise RuntimeError("请选择 grok2api 账号")
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"enable", "disable", "delete"}:
        raise RuntimeError("grok2api 账号操作必须是 enable、disable 或 delete")
    session = _session()
    token = _login_token(session, cfg)
    url = _admin_url(cfg, "accounts")
    payload: dict[str, Any] = {"ids": ids, "provider": "grok_web"}
    try:
        if normalized_action == "delete":
            resp = session.delete(url, json=payload, headers=_auth_headers(token), timeout=60, verify=_verify_tls(cfg))
        else:
            payload["enabled"] = normalized_action == "enable"
            resp = session.patch(
                f"{url}/batch",
                json=payload,
                headers=_auth_headers(token),
                timeout=60,
                verify=_verify_tls(cfg),
            )
    except Exception as error:
        raise RuntimeError(f"grok2api 账号操作连接失败: {error}") from error
    data = _json_response(resp)
    if int(resp.status_code) not in range(200, 300):
        raise RuntimeError(f"grok2api 账号操作失败: {_error_message(resp, data)}")
    body = _extract_data(data)
    changed = int(body.get("deleted") or body.get("updated") or len(ids))
    return {"ok": True, "total": len(ids), "success": changed, "action": normalized_action, "response": body}
