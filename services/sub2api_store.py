from __future__ import annotations

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from curl_cffi import requests

from services import k12_config_store, task_proxy_service
from services.account_token_profile import SUB2API_OAUTH_CLIENT_ID, token_profile
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.registered_account_store import account_matches_selector, list_registered_accounts, update_registered_account

SUB2API_CONFIG_FILE = DATA_DIR / "sub2api_config.json"
SUB2API_HEALTH_FILE = DATA_DIR / "sub2api_warehouse_health.json"
_lock = Lock()
_state_lock = threading.RLock()
_scheduler_lock = threading.Lock()
_scheduler: threading.Thread | None = None
OPENAI_PLATFORM_OAUTH_CLIENT_ID = SUB2API_OAUTH_CLIENT_ID


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict[str, Any]:
    return {
        "enabled": False,
        "base_url": "",
        "admin_token": "",
        "username": "",
        "password": "",
        "skip_ssl_verify": False,
        "import_method": "batch",
        "group_ids": [],
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1.0,
        "load_factor": 10,
        "auto_pause_on_expired": True,
        "auto_import": False,
        "scheduled_enabled": False,
        "check_interval_minutes": 60,
        "test_concurrency": 5,
        "test_model_id": "",
        "test_prompt": "hi",
        "test_mode": "",
        "disable_on_failure": False,
        "delete_on_failure": False,
        "notes": "imported from chatgpt2api-local",
        "last_import": None,
        "updated_at": "",
    }


def _as_int_list(value: Any) -> list[int]:
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    result: list[int] = []
    for item in raw_items:
        try:
            number = int(str(item).strip())
        except Exception:
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = _default_config()
    cfg.update({key: value for key, value in raw.items() if key in cfg})
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["base_url"] = str(cfg.get("base_url") or "").strip().rstrip("/")
    cfg["admin_token"] = str(cfg.get("admin_token") or "").strip()
    cfg["username"] = str(cfg.get("username") or "").strip()
    cfg["password"] = str(cfg.get("password") or "").strip()
    cfg["skip_ssl_verify"] = bool(cfg.get("skip_ssl_verify"))
    cfg["import_method"] = str(cfg.get("import_method") or "batch").strip().lower()
    if cfg["import_method"] not in {"batch", "data"}:
        cfg["import_method"] = "batch"
    cfg["group_ids"] = _as_int_list(cfg.get("group_ids"))
    cfg["concurrency"] = max(1, min(50, int(cfg.get("concurrency") or 10)))
    cfg["priority"] = int(cfg.get("priority") or 1)
    try:
        cfg["rate_multiplier"] = max(0.0, float(cfg.get("rate_multiplier") or 1.0))
    except Exception:
        cfg["rate_multiplier"] = 1.0
    cfg["load_factor"] = max(1, int(cfg.get("load_factor") or cfg["concurrency"]))
    cfg["auto_pause_on_expired"] = bool(cfg.get("auto_pause_on_expired"))
    cfg["auto_import"] = bool(cfg.get("auto_import"))
    cfg["scheduled_enabled"] = bool(cfg.get("scheduled_enabled"))
    cfg["check_interval_minutes"] = max(1, int(cfg.get("check_interval_minutes") or 60))
    cfg["test_concurrency"] = max(1, min(50, int(cfg.get("test_concurrency") or 5)))
    cfg["test_model_id"] = str(cfg.get("test_model_id") or "").strip()
    cfg["test_prompt"] = str(cfg.get("test_prompt") or "hi")
    cfg["test_mode"] = str(cfg.get("test_mode") or "").strip()
    cfg["disable_on_failure"] = bool(cfg.get("disable_on_failure"))
    cfg["delete_on_failure"] = bool(cfg.get("delete_on_failure"))
    cfg["notes"] = str(cfg.get("notes") or "")
    cfg["updated_at"] = str(cfg.get("updated_at") or "")
    return cfg


def _load() -> dict[str, Any]:
    data = read_json(SUB2API_CONFIG_FILE, {}, expected_schema_version=1)
    return _normalize(data if isinstance(data, dict) else {})


def _save(cfg: dict[str, Any]) -> None:
    k12_config_store.ensure_migrated()
    SUB2API_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(SUB2API_CONFIG_FILE, _normalize(cfg), schema_version=1)


def _preview(value: Any, keep: int = 8) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}...{text[-keep:]}"


def _public(cfg: dict[str, Any], *, include_secret: bool = False) -> dict[str, Any]:
    item = json.loads(json.dumps(cfg, ensure_ascii=False))
    item["has_admin_token"] = bool(item.get("admin_token"))
    item["has_password"] = bool(item.get("password"))
    item["admin_token_preview"] = _preview(item.get("admin_token"))
    item["password_preview"] = "********" if item.get("password") else ""
    with _state_lock:
        state = _load_health_state()
    item["schedule_status"] = {
        "running": bool(item.get("scheduled_enabled")) and bool(_scheduler and _scheduler.is_alive()),
        "last_run_at": state.get("last_scheduled_run_at") or "",
        "last_result": state.get("last_scheduled_result"),
    }
    if not include_secret:
        item.pop("admin_token", None)
        item.pop("password", None)
    return item


def get_sub2api_config(*, include_secret: bool = False) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    _ensure_scheduler(cfg)
    return _public(cfg, include_secret=include_secret)


def update_sub2api_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        cfg = _load()
        for key, value in updates.items():
            if key not in cfg:
                continue
            if key in {"admin_token", "password"} and not str(value or "").strip():
                continue
            cfg[key] = value
        cfg["updated_at"] = _now()
        _save(cfg)
    _ensure_scheduler(cfg)
    return _public(cfg)


def _default_health_state() -> dict[str, Any]:
    return {"accounts": {}, "last_scheduled_run_at": "", "last_scheduled_result": None, "updated_at": ""}


def _load_health_state() -> dict[str, Any]:
    raw = read_json(SUB2API_HEALTH_FILE, {}, expected_schema_version=1)
    state = _default_health_state()
    if isinstance(raw, dict):
        state.update({key: value for key, value in raw.items() if key in state})
    if not isinstance(state.get("accounts"), dict):
        state["accounts"] = {}
    return state


def _save_health_state(state: dict[str, Any]) -> None:
    SUB2API_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    write_json(SUB2API_HEALTH_FILE, state, schema_version=1)


def _cache_health_result(account_id: int | str, result: dict[str, Any]) -> None:
    with _state_lock:
        state = _load_health_state()
        previous = state.setdefault("accounts", {}).get(str(account_id))
        merged = dict(previous) if isinstance(previous, dict) else {}
        merged.update(result)
        state["accounts"][str(account_id)] = merged
        _save_health_state(state)


def _api_base(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return ""
    return value if value.endswith("/api/v1") else f"{value}/api/v1"


def _session(
    *,
    cfg: dict[str, Any] | None = None,
    purpose: str = "sub2api",
    index: int = 1,
    account: dict[str, Any] | None = None,
    total_threads: int = 1,
) -> requests.Session:
    current = cfg or _load()
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


def _verify_tls(cfg: dict[str, Any] | None = None) -> bool:
    current = cfg or _load()
    return not bool(current.get("skip_ssl_verify")) and task_proxy_service.proxy_settings.verify_tls()


def _json_response(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"raw": str(getattr(resp, "text", "") or "")[:1000]}


def _extract_data(payload: dict[str, Any]) -> Any:
    if "data" in payload:
        return payload.get("data")
    return payload


def _oauth_response_data(payload: dict[str, Any]) -> dict[str, Any]:
    body = _extract_data(payload)
    if isinstance(body, dict) and isinstance(body.get("credentials"), dict):
        return {**body, **body["credentials"]}
    return body if isinstance(body, dict) else payload


def _login_token(session: requests.Session, cfg: dict[str, Any]) -> str:
    if cfg.get("admin_token"):
        return str(cfg["admin_token"]).strip()
    api_base = _api_base(str(cfg.get("base_url") or ""))
    username = str(cfg.get("username") or "").strip()
    password = str(cfg.get("password") or "").strip()
    if not api_base:
        raise RuntimeError("请先填写 sub2api 地址")
    if not username or not password:
        raise RuntimeError("请填写 sub2api 管理员 Token，或填写登录邮箱和密码")
    resp = session.post(
        f"{api_base}/auth/login",
        json={"email": username, "password": password},
        headers={"Content-Type": "application/json"},
        timeout=30,
        verify=_verify_tls(cfg),
    )
    data = _json_response(resp)
    if resp.status_code != 200:
        raise RuntimeError(f"sub2api 登录失败: HTTP {resp.status_code}, {json.dumps(data, ensure_ascii=False)[:800]}")
    body = _extract_data(data)
    if not isinstance(body, dict):
        body = data
    token = str(body.get("access_token") or "").strip()
    if not token:
        raise RuntimeError(f"sub2api 登录响应缺少 access_token: {json.dumps(data, ensure_ascii=False)[:800]}")
    return token


def generate_openai_oauth_url(*, redirect_uri: str = "") -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先在“中转仓库配置”中启用 sub2api")
    api_base = _api_base(str(cfg.get("base_url") or ""))
    if not api_base:
        raise RuntimeError("请先填写 sub2api 地址")
    session = _session(cfg=cfg, purpose="sub2api_oauth")
    token = _login_token(session, cfg)
    payload: dict[str, Any] = {}
    if str(redirect_uri or "").strip():
        payload["redirect_uri"] = str(redirect_uri).strip()
    resp = session.post(
        f"{api_base}/admin/openai/generate-auth-url",
        json=payload,
        headers=_admin_auth_headers(cfg, token),
        timeout=45,
        verify=_verify_tls(cfg),
    )
    data = _json_response(resp)
    body = _oauth_response_data(data)
    if resp.status_code != 200:
        raise RuntimeError(f"sub2api 生成 OAuth 链接失败: HTTP {resp.status_code}, {json.dumps(data, ensure_ascii=False)[:800]}")
    auth_url = str(body.get("auth_url") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    if not auth_url or not session_id:
        raise RuntimeError(f"sub2api OAuth 响应缺少 auth_url/session_id: {json.dumps(data, ensure_ascii=False)[:800]}")
    return {"auth_url": auth_url, "session_id": session_id}


def exchange_openai_oauth_code(*, session_id: str, code: str, state: str, proxy_id: int | str | None = None) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先在“中转仓库配置”中启用 sub2api")
    api_base = _api_base(str(cfg.get("base_url") or ""))
    if not api_base:
        raise RuntimeError("请先填写 sub2api 地址")
    payload: dict[str, Any] = {
        "session_id": str(session_id or "").strip(),
        "code": str(code or "").strip(),
        "state": str(state or "").strip(),
    }
    if proxy_id not in (None, ""):
        payload["proxy_id"] = proxy_id
    if not payload["session_id"] or not payload["code"] or not payload["state"]:
        raise RuntimeError("OAuth 换码缺少 session_id/code/state")
    session = _session(cfg=cfg, purpose="sub2api_oauth_exchange")
    token = _login_token(session, cfg)
    resp = session.post(
        f"{api_base}/admin/openai/exchange-code",
        json=payload,
        headers=_admin_auth_headers(cfg, token),
        timeout=90,
        verify=_verify_tls(cfg),
    )
    data = _json_response(resp)
    body = _oauth_response_data(data)
    if resp.status_code != 200:
        detail = body.get("detail") or body.get("message") or json.dumps(data, ensure_ascii=False)[:800]
        raise RuntimeError(f"sub2api OAuth 换码失败: HTTP {resp.status_code}, {detail}")
    if not str(body.get("access_token") or "").strip():
        raise RuntimeError(f"sub2api OAuth 换码响应缺少 access_token: {json.dumps(data, ensure_ascii=False)[:800]}")
    return body


def _admin_auth_headers(cfg: dict[str, Any], token: str, *, content_type: str = "application/json", accept: str = "application/json") -> dict[str, str]:
    headers: dict[str, str] = {}
    if accept:
        headers["Accept"] = accept
    if content_type:
        headers["Content-Type"] = content_type
    if str(cfg.get("admin_token") or "").strip():
        headers["x-api-key"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    return headers


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


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _email_key(email: str) -> str:
    return str(email or "").strip().lower().replace("@", "_at_").replace(".", "_")


def _expires_from_claims(*claims: dict[str, Any]) -> tuple[int | None, int | None]:
    exp = None
    for claim in claims:
        try:
            value = int(claim.get("exp") or 0)
        except Exception:
            value = 0
        if value > 0:
            exp = value
            break
    expires_in = max(0, exp - int(time.time())) if exp else None
    return exp, expires_in


def build_sub2api_account(account: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _normalize(cfg or _load())
    access_claims = _jwt_payload(account.get("access_token"))
    id_claims = _jwt_payload(account.get("id_token"))
    access_auth = access_claims.get("https://api.openai.com/auth")
    access_auth = access_auth if isinstance(access_auth, dict) else {}
    access_profile = access_claims.get("https://api.openai.com/profile")
    access_profile = access_profile if isinstance(access_profile, dict) else {}
    email = _first_non_empty(
        account.get("email"),
        access_claims.get("https://api.openai.com/profile.email"),
        access_profile.get("email"),
        id_claims.get("email"),
    )
    user_id = _first_non_empty(
        account.get("chatgpt_user_id"),
        account.get("user_id"),
        access_claims.get("https://api.openai.com/auth.user_id"),
        access_auth.get("user_id"),
        id_claims.get("https://api.openai.com/auth", {}).get("user_id") if isinstance(id_claims.get("https://api.openai.com/auth"), dict) else "",
        access_claims.get("sub"),
        id_claims.get("sub"),
    )
    account_id = _first_non_empty(
        account.get("chatgpt_account_id"),
        account.get("account_id"),
        access_auth.get("chatgpt_account_id"),
        access_auth.get("account_id"),
    )
    exp, expires_in = _expires_from_claims(access_claims, id_claims)
    plan_type = _first_non_empty(
        account.get("plan_type"),
        access_auth.get("chatgpt_plan_type"),
        access_claims.get("https://api.openai.com/profile.plan_type"),
        access_profile.get("plan_type"),
        "free",
    )
    token_source = str(account.get("token_source") or "").strip()
    client_id = _first_non_empty(
        account.get("client_id"),
        access_claims.get("client_id"),
        id_claims.get("client_id"),
        "app_X8zY6vW2pQ9tR3dE7nK1jL5gH" if token_source == "chatgpt_web" else OPENAI_PLATFORM_OAUTH_CLIENT_ID,
    )
    credentials: dict[str, Any] = {
        "access_token": str(account.get("access_token") or "").strip(),
        "chatgpt_account_id": account_id,
        "chatgpt_user_id": user_id,
        "email": email,
        "plan_type": plan_type,
        "client_id": client_id,
    }
    if token_source != "chatgpt_web":
        credentials["refresh_token"] = str(account.get("refresh_token") or "").strip()
        credentials["id_token"] = str(account.get("id_token") or "").strip()
    if exp:
        credentials["expires_at"] = exp
    if expires_in is not None:
        credentials["expires_in"] = expires_in
    workspace_id = _first_non_empty(account.get("workspace_id"), account.get("k12_team_id"))
    if workspace_id:
        credentials["organization_id"] = workspace_id
        credentials["workspace_id"] = workspace_id
    extra = {
        "email": email,
        "email_key": _email_key(email),
        "source": "chatgpt2api-local",
        "last_refresh": _now(),
        "token_source": token_source,
        "k12_mode": bool(account.get("k12_mode")),
        "k12_join_status": account.get("k12_join_status") or "",
        "k12_team_id": account.get("k12_team_id") or "",
    }
    notes = str(cfg.get("notes") or "").strip() or None
    auto_pause = bool(cfg.get("auto_pause_on_expired"))
    item: dict[str, Any] = {
        "name": _first_non_empty(email, account.get("name"), "ChatGPT Account"),
        "notes": notes,
        "platform": "openai",
        "type": "oauth",
        "credentials": {key: value for key, value in credentials.items() if value not in ("", None)},
        "extra": {key: value for key, value in extra.items() if value not in ("", None)},
        "concurrency": int(cfg.get("concurrency") or 10),
        "priority": int(cfg.get("priority") or 1),
        "rate_multiplier": float(cfg.get("rate_multiplier") or 1.0),
        "load_factor": int(cfg.get("load_factor") or cfg.get("concurrency") or 10),
        "group_ids": _as_int_list(cfg.get("group_ids")),
        "auto_pause_on_expired": auto_pause,
        "confirm_mixed_channel_risk": True,
    }
    if exp:
        item["expires_at"] = exp
    return {key: value for key, value in item.items() if value not in (None, [], {})}


def build_sub2api_document(accounts: list[dict[str, Any]], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _normalize(cfg or _load())
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": _now(),
        "proxies": [],
        "accounts": [build_sub2api_account(account, cfg) for account in accounts],
    }


def _select_accounts(emails: list[str] | None = None) -> list[dict[str, Any]]:
    selected = {str(item).strip().lower() for item in emails or [] if str(item).strip()}
    accounts = list_registered_accounts(include_secret=True)
    if selected:
        accounts = [item for item in accounts if any(account_matches_selector(item, selector) for selector in selected)]
    return [item for item in accounts if token_profile(item).get("oauth_capable")]


def _build_oauth_create_payload(account: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    email = str(account.get("email") or "").strip()
    refresh_token = str(account.get("refresh_token") or "").strip()
    group_ids = _as_int_list(cfg.get("group_ids"))
    client_id = str(account.get("client_id") or OPENAI_PLATFORM_OAUTH_CLIENT_ID).strip()
    payload: dict[str, Any] = {
        "name": email or "ChatGPT Account",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "model_mapping": {
                "gpt-5.4": "gpt-5.4",
                "gpt-5.4-mini": "gpt-5.4-mini",
                "gpt-5.5": "gpt-5.5",
            },
        },
        "concurrency": int(cfg.get("concurrency") or 10),
        "priority": int(cfg.get("priority") or 1),
        "rate_multiplier": float(cfg.get("rate_multiplier") or 1.0),
        "extra": {
            "source": "chatgpt2api-local",
            "openai_oauth_responses_websockets_v2_enabled": True,
            "openai_oauth_responses_websockets_v2_mode": "passthrough",
        },
        "auto_pause_on_expired": bool(cfg.get("auto_pause_on_expired", True)),
    }
    if group_ids:
        payload["group_ids"] = group_ids
        payload["group_id"] = group_ids[0]
    workspace_id = str(account.get("workspace_id") or account.get("k12_team_id") or "").strip()
    if workspace_id:
        payload["credentials"]["organization_id"] = workspace_id
    return payload


def _oauth_update_credentials(account: dict[str, Any]) -> dict[str, Any]:
    credentials: dict[str, Any] = {
        "access_token": str(account.get("access_token") or "").strip(),
        "refresh_token": str(account.get("refresh_token") or "").strip(),
        "client_id": str(account.get("client_id") or OPENAI_PLATFORM_OAUTH_CLIENT_ID).strip(),
    }
    id_token = str(account.get("id_token") or "").strip()
    if id_token:
        credentials["id_token"] = id_token
    workspace_id = str(account.get("workspace_id") or account.get("k12_team_id") or "").strip()
    if workspace_id:
        credentials["organization_id"] = workspace_id
    return {key: value for key, value in credentials.items() if value}


def _refresh_sub2api_account(account_id: int, session: requests.Session, api_base: str, headers: dict[str, str]) -> None:
    for path in (f"{api_base}/admin/accounts/{account_id}/refresh", f"{api_base}/admin/openai/accounts/{account_id}/refresh"):
        try:
            resp = session.post(path, json={}, headers=headers, timeout=30, verify=_verify_tls())
            if resp.status_code in (200, 201, 204):
                return
        except Exception:
            pass


def _extract_account_id_from_response(response_data: dict[str, Any]) -> int | None:
    for item in _walk_values(response_data):
        raw_id = item.get("id") if isinstance(item, dict) else None
        if raw_id is not None:
            try:
                return int(raw_id)
            except (TypeError, ValueError):
                pass
    return None


def create_account_in_sub2api(
    account: dict[str, Any],
    session: requests.Session,
    api_base: str,
    token: str,
    cfg: dict[str, Any],
    *,
    allow_access_token_fallback: bool = False,
) -> dict[str, Any]:
    profile = token_profile(account)
    oauth_capable = bool(profile.get("oauth_capable"))
    if not oauth_capable and not (allow_access_token_fallback and str(account.get("access_token") or "").strip()):
        return {
            "ok": False,
            "method": "oauth_rejected",
            "status_code": 0,
            "error": f"账号不是 sub2api OAuth Token: {profile.get('refresh_token_label') or profile.get('oauth_token_status') or 'unknown'}",
        }
    headers = _admin_auth_headers(cfg, token, content_type="application/json", accept="application/json")
    refresh_token = str(account.get("refresh_token") or "").strip()
    email = str(account.get("email") or "").strip()
    group_ids = _as_int_list(cfg.get("group_ids"))

    existing_id = account.get("sub2api_account_id")
    try:
        existing_id = int(existing_id) if existing_id not in (None, "") else None
    except (TypeError, ValueError):
        existing_id = None
    if existing_id and refresh_token and oauth_capable:
        url = f"{api_base}/admin/accounts/{existing_id}"
        update_payload: dict[str, Any] = {"credentials": _oauth_update_credentials(account)}
        if group_ids:
            update_payload.update({"group_ids": group_ids, "group_id": group_ids[0]})
        resp = session.put(url, json=update_payload, headers=headers, timeout=30, verify=_verify_tls(cfg))
        if resp.status_code in (200, 201, 204):
            _refresh_sub2api_account(existing_id, session, api_base, headers)
            return {
                "ok": True,
                "method": "oauth_update",
                "status_code": int(resp.status_code),
                "response": _json_response(resp),
                "url": url,
                "account_id": existing_id,
            }
        if resp.status_code != 404:
            return {
                "ok": False,
                "method": "oauth_update",
                "status_code": int(resp.status_code),
                "response": _json_response(resp),
                "url": url,
                "account_id": existing_id,
            }

    if refresh_token and oauth_capable:
        oauth_payload = _build_oauth_create_payload(account, cfg)
        url = f"{api_base}/admin/accounts"
        resp = session.post(url, json=oauth_payload, headers=headers, timeout=30, verify=_verify_tls(cfg))
        if resp.status_code in (200, 201):
            data = _json_response(resp)
            account_id = _extract_account_id_from_response(data)
            if account_id:
                if group_ids:
                    try:
                        session.put(f"{api_base}/admin/accounts/{account_id}", json={"group_ids": group_ids, "group_id": group_ids[0]}, headers=headers, timeout=15, verify=_verify_tls(cfg))
                    except Exception:
                        pass
                _refresh_sub2api_account(account_id, session, api_base, headers)
                try:
                    session.put(f"{api_base}/admin/accounts/{account_id}", json={"credentials": _oauth_update_credentials(account)}, headers=headers, timeout=15, verify=_verify_tls(cfg))
                except Exception:
                    pass
            return {"ok": True, "method": "oauth_refresh", "status_code": int(resp.status_code), "response": data, "url": url, "account_id": account_id}

    fallback_account = account
    if not oauth_capable:
        fallback_account = {
            **account,
            "refresh_token": "",
            "id_token": "",
            "token_source": "chatgpt_web",
            "client_id": "app_X8zY6vW2pQ9tR3dE7nK1jL5gH",
        }
    fallback_payload = build_sub2api_account(fallback_account, cfg)
    url = f"{api_base}/admin/accounts/data"
    bundle = {
        "data": {
            "type": "sub2api-data",
            "version": 1,
            "exported_at": _now(),
            "proxies": [],
            "accounts": [fallback_payload],
        },
        "skip_default_group_bind": not bool(group_ids),
    }
    resp = session.post(url, json=bundle, headers=headers, timeout=60, verify=_verify_tls(cfg))
    ok = 200 <= int(resp.status_code) < 300
    return {"ok": ok, "method": "data_import", "status_code": int(resp.status_code), "response": _json_response(resp), "url": url}


def export_sub2api_data(emails: list[str] | None = None) -> dict[str, Any]:
    cfg = _load()
    accounts = _select_accounts(emails)
    return {"count": len(accounts), "data": build_sub2api_document(accounts, cfg)}


def export_account_dicts_to_sub2api(accounts: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    selected = [accounts] if isinstance(accounts, dict) else [item for item in accounts if isinstance(item, dict)]
    selected = [item for item in selected if str(item.get("access_token") or "").strip()]
    return {"count": len(selected), "data": build_sub2api_document(selected, cfg)}


def import_accounts_to_sub2api(emails: list[str] | None = None) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    accounts = _select_accounts(emails)
    return _import_account_list_to_sub2api(accounts, cfg)


def import_account_dicts_to_sub2api(
    accounts: list[dict[str, Any]] | dict[str, Any],
    *,
    group_ids: list[int | str] | None = None,
    allow_access_token_fallback: bool = False,
) -> dict[str, Any]:
    """导入刚注册完成的账号对象，避免只用 email 选择时误导入同邮箱不同空间账号。"""
    with _lock:
        cfg = _load()
    if group_ids is not None:
        cfg = {**cfg, "group_ids": _as_int_list(group_ids)}
    if isinstance(accounts, dict):
        selected_accounts = [accounts]
    else:
        selected_accounts = [item for item in accounts if isinstance(item, dict)]
    selected_accounts = [item for item in selected_accounts if str(item.get("access_token") or "").strip()]
    return _import_account_list_to_sub2api(selected_accounts, cfg, allow_access_token_fallback=allow_access_token_fallback)


def _import_account_list_to_sub2api(accounts: list[dict[str, Any]], cfg: dict[str, Any], *, allow_access_token_fallback: bool = False) -> dict[str, Any]:
    if not cfg.get("enabled"):
        raise RuntimeError("请先在“中转仓库配置”中启用 sub2api")
    api_base = _api_base(str(cfg.get("base_url") or ""))
    if not api_base:
        raise RuntimeError("请先填写 sub2api 地址")
    if not accounts:
        raise RuntimeError("当前选择中没有通过 sub2api 授权登录得到的 OAuth Token；请先执行“获取 OAuth 并导入”")

    session = _session(cfg=cfg, purpose="sub2api_admin")
    token = _login_token(session, cfg)
    account_results: list[dict[str, Any]] = []

    def import_one(item: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        index, account = item
        try:
            result = create_account_in_sub2api(
                account,
                _session(cfg=cfg, purpose="sub2api_import", index=index, account=account, total_threads=workers),
                api_base,
                token,
                cfg,
                allow_access_token_fallback=allow_access_token_fallback,
            )
        except Exception as error:
            result = {"ok": False, "status_code": 0, "error": str(error)[:800]}
        account_id = result.get("account_id")
        if not account_id:
            ids = extract_sub2api_account_ids(result, [str(account.get("email") or "")])
            account_id = ids[0] if ids else None
        return {
            "account": account,
            "ok": bool(result.get("ok")),
            "email": str(account.get("email") or ""),
            "account_id": account_id,
            "status_code": int(result.get("status_code") or 0),
            "error": str(result.get("response") or result.get("error") or result.get("status_code") or "") if not result.get("ok") else "",
        }

    workers = max(1, min(len(accounts), int(cfg.get("concurrency") or 10), 50))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(import_one, item): item[1] for item in enumerate(accounts, start=1)}
        for future in as_completed(futures):
            account = futures[future]
            try:
                account_results.append(future.result())
            except Exception as error:
                account_results.append({
                    "account": account,
                    "ok": False,
                    "email": str(account.get("email") or ""),
                    "account_id": None,
                    "status_code": 0,
                    "error": str(error)[:800],
                })

    success_count = sum(1 for r in account_results if r["ok"])
    fail_count = len(account_results) - success_count
    last_error = next((r["error"] for r in account_results if r["error"]), "")
    failed_status_code = next((int(r.get("status_code") or 0) for r in account_results if not r["ok"]), 0)
    imported_ids = [r["account_id"] for r in account_results if r["account_id"]]

    final_result: dict[str, Any] = {
        "ok": fail_count == 0,
        "partial": success_count > 0 and fail_count > 0,
        "total": len(accounts),
        "success": success_count,
        "fail": fail_count,
        "imported_at": _now(),
        "account_ids": imported_ids,
        "status_code": failed_status_code,
        "results": [
            {key: value for key, value in result.items() if key != "account"}
            for result in account_results
        ],
    }
    if last_error:
        final_result["last_error"] = last_error[:800]
        final_result["response"] = last_error[:800]

    with _lock:
        latest = _load()
        latest["last_import"] = final_result
        _save(latest)

    for ar in account_results:
        updates: dict[str, Any] = {
            "sub2api_imported_at": final_result["imported_at"],
            "sub2api_import_status": "ok" if ar["ok"] else "failed",
        }
        if ar.get("account_id"):
            updates["sub2api_account_id"] = ar["account_id"]
        if not ar["ok"]:
            updates["sub2api_last_error"] = (ar["error"] or "unknown")[:800]
        update_registered_account(ar["account"], updates)

    return final_result


def _walk_values(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)


def extract_sub2api_account_ids(result: dict[str, Any] | None, emails: list[str] | None = None) -> list[int]:
    """从 sub2api 导入/更新响应中提取账号 ID。"""
    wanted = {str(item).strip().lower() for item in (emails or []) if str(item).strip()}
    ids: list[int] = []
    for item in _walk_values(result or {}):
        raw_id = item.get("id") or item.get("account_id") or item.get("accountId")
        if raw_id is None:
            continue
        name = str(item.get("name") or item.get("email") or "").strip().lower()
        if wanted and name and name not in wanted:
            # sub2api 有些版本的 name 可能不是邮箱，这里只在明确是邮箱时过滤。
            if "@" in name:
                continue
        try:
            account_id = int(raw_id)
        except Exception:
            continue
        if account_id > 0 and account_id not in ids:
            ids.append(account_id)
    return ids


def find_sub2api_account_ids(emails: list[str] | None = None) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先在“中转仓库配置”中启用 sub2api")
    api_base = _api_base(str(cfg.get("base_url") or ""))
    if not api_base:
        raise RuntimeError("请先填写 sub2api 地址")
    targets = [str(item).strip() for item in (emails or []) if str(item).strip()]
    session = _session()
    token = _login_token(session, cfg)
    headers = _admin_auth_headers(cfg, token, content_type="", accept="application/json")
    found: dict[str, list[int]] = {}
    errors: list[dict[str, Any]] = []
    for email in targets or [""]:
        params = {"page": 1, "page_size": 50, "platform": "openai", "type": "oauth"}
        if email:
            params["search"] = email
        resp = session.get(f"{api_base}/admin/accounts", params=params, headers=headers, timeout=45, verify=_verify_tls(cfg))
        data = _json_response(resp)
        if resp.status_code != 200:
            errors.append({"email": email, "status_code": resp.status_code, "response": data})
            continue
        body = _extract_data(data)
        items = []
        if isinstance(body, dict):
            for key in ("items", "accounts", "list", "data"):
                if isinstance(body.get(key), list):
                    items = body[key]
                    break
        elif isinstance(body, list):
            items = body
        ids: list[int] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id") or item.get("account_id")
            haystack = json.dumps(item, ensure_ascii=False).lower()
            if email and email.lower() not in haystack:
                continue
            try:
                account_id = int(raw_id)
            except Exception:
                continue
            if account_id > 0 and account_id not in ids:
                ids.append(account_id)
        if email:
            found[email] = ids
        else:
            found["*"] = ids
    return {"found": found, "errors": errors}


def test_sub2api_account(account_id: int | str, *, model_id: str = "", prompt: str = "hi", mode: str = "") -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先在“中转仓库配置”中启用 sub2api")
    api_base = _api_base(str(cfg.get("base_url") or ""))
    if not api_base:
        raise RuntimeError("请先填写 sub2api 地址")
    try:
        account_id_int = int(account_id)
    except Exception as exc:
        raise RuntimeError("sub2api account_id 无效") from exc
    session = _session()
    token = _login_token(session, cfg)
    headers = _admin_auth_headers(cfg, token, content_type="application/json", accept="text/event-stream")
    payload = {"model_id": str(model_id or ""), "prompt": str(prompt or "hi"), "mode": str(mode or "")}
    url = f"{api_base}/admin/accounts/{account_id_int}/test"
    resp = session.post(url, json=payload, headers=headers, timeout=120, verify=_verify_tls(cfg))
    text = str(getattr(resp, "text", "") or "")[:4000]
    status_code = int(getattr(resp, "status_code", 0) or 0)
    lowered = text.lower()
    ok = 200 <= status_code < 300 and not any(marker in lowered for marker in ["error", "failed", "invalid", "unauthorized", "forbidden"])
    result = {
        "ok": ok,
        "status_code": status_code,
        "account_id": account_id_int,
        "url": url,
        "body_preview": text[:1000],
        "tested_at": _now(),
    }
    _cache_health_result(account_id_int, result)
    return result


def get_sub2api_account_usage(account_id: int | str, *, force: bool = True) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先启用 sub2api")
    try:
        account_id_int = int(account_id)
    except Exception as exc:
        raise RuntimeError("sub2api account_id 无效") from exc
    api_base = _api_base(str(cfg.get("base_url") or ""))
    session = _session()
    token = _login_token(session, cfg)
    headers = _admin_auth_headers(cfg, token, content_type="", accept="application/json")
    resp = session.get(
        f"{api_base}/admin/accounts/{account_id_int}/usage",
        params={"source": "active", "force": "true" if force else "false"},
        headers=headers,
        timeout=90,
        verify=_verify_tls(cfg),
    )
    data = _json_response(resp)
    result = {
        "ok": resp.status_code == 200,
        "status_code": int(resp.status_code),
        "account_id": account_id_int,
        "usage": _extract_data(data),
        "checked_at": _now(),
    }
    if resp.status_code != 200:
        result["error"] = json.dumps(data, ensure_ascii=False)[:800]
    with _state_lock:
        state = _load_health_state()
        previous = state.setdefault("accounts", {}).get(str(account_id_int))
        merged = dict(previous) if isinstance(previous, dict) else {}
        merged.update({"usage": result.get("usage"), "usage_status_code": result.get("status_code"), "usage_checked_at": result.get("checked_at")})
        if not result.get("ok"):
            merged["usage_error"] = result.get("error")
        state["accounts"][str(account_id_int)] = merged
        _save_health_state(state)
    return result


def set_sub2api_account_status(account_id: int | str, disabled: bool) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先启用 sub2api")
    try:
        account_id_int = int(account_id)
    except Exception as exc:
        raise RuntimeError("sub2api account_id 无效") from exc
    api_base = _api_base(str(cfg.get("base_url") or ""))
    session = _session()
    token = _login_token(session, cfg)
    headers = _admin_auth_headers(cfg, token, content_type="application/json", accept="application/json")
    status = "inactive" if disabled else "active"
    resp = session.put(
        f"{api_base}/admin/accounts/{account_id_int}",
        json={"status": status},
        headers=headers,
        timeout=45,
        verify=_verify_tls(cfg),
    )
    return {
        "ok": 200 <= int(resp.status_code) < 300,
        "status_code": int(resp.status_code),
        "account_id": account_id_int,
        "status": status,
        "response": _json_response(resp),
    }


def delete_sub2api_account(account_id: int | str) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先启用 sub2api")
    try:
        account_id_int = int(account_id)
    except Exception as exc:
        raise RuntimeError("sub2api account_id 无效") from exc
    api_base = _api_base(str(cfg.get("base_url") or ""))
    session = _session()
    token = _login_token(session, cfg)
    headers = _admin_auth_headers(cfg, token, content_type="", accept="application/json")
    resp = session.delete(f"{api_base}/admin/accounts/{account_id_int}", headers=headers, timeout=45, verify=_verify_tls(cfg))
    return {
        "ok": 200 <= int(resp.status_code) < 300,
        "status_code": int(resp.status_code),
        "account_id": account_id_int,
        "response": _json_response(resp),
    }


def update_account_in_sub2api(email: str, account_id: int | str) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先在“中转仓库配置”中启用 sub2api")
    api_base = _api_base(str(cfg.get("base_url") or ""))
    if not api_base:
        raise RuntimeError("请先填写 sub2api 地址")
    try:
        account_id_int = int(account_id)
    except Exception as exc:
        raise RuntimeError("sub2api account_id 无效") from exc
    accounts = _select_accounts([email])
    if not accounts:
        raise RuntimeError("本地账户库存中没有可同步的 sub2api OAuth Token")
    payload = build_sub2api_account(accounts[0], cfg)
    session = _session()
    token = _login_token(session, cfg)
    headers = _admin_auth_headers(cfg, token, content_type="application/json", accept="application/json")
    url = f"{api_base}/admin/accounts/{account_id_int}"
    resp = session.put(url, json=payload, headers=headers, timeout=60, verify=_verify_tls(cfg))
    data = _json_response(resp)
    ok = 200 <= int(resp.status_code) < 300
    result = {"ok": ok, "status_code": int(resp.status_code), "account_id": account_id_int, "response": data, "updated_at": _now()}
    update_registered_account(accounts[0], {"sub2api_update_status": "ok" if ok else "failed", "sub2api_updated_at": result["updated_at"]})
    return result



def _extract_account_items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    body = _extract_data(payload)
    total = 0
    items: list[dict[str, Any]] = []
    if isinstance(body, list):
        items = [item for item in body if isinstance(item, dict)]
        total = len(items)
    elif isinstance(body, dict):
        for key in ("items", "accounts", "list", "rows", "data"):
            if isinstance(body.get(key), list):
                items = [item for item in body[key] if isinstance(item, dict)]
                break
        for key in ("total", "count", "total_count"):
            try:
                value = int(body.get(key) or 0)
            except Exception:
                value = 0
            if value:
                total = value
                break
        if not total:
            total = len(items)
    return items, total


def _public_sub2api_account(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    credentials = result.get("credentials")
    if isinstance(credentials, dict):
        cleaned = dict(credentials)
        for key in ("access_token", "refresh_token", "id_token"):
            if cleaned.get(key):
                cleaned[f"{key}_preview"] = _preview(cleaned.get(key))
                cleaned.pop(key, None)
        result["credentials"] = cleaned
    return result


def list_sub2api_accounts(*, page: int = 1, page_size: int = 20, group_id: int | str | None = None, search: str = "", status: str = "", platform: str = "openai", account_type: str = "oauth") -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先启用 sub2api")
    api_base = _api_base(str(cfg.get("base_url") or ""))
    if not api_base:
        raise RuntimeError("请先填写 sub2api 地址")
    session = _session()
    token = _login_token(session, cfg)
    headers = _admin_auth_headers(cfg, token, content_type="", accept="application/json")
    params: dict[str, Any] = {
        "page": max(1, int(page or 1)),
        "page_size": max(1, int(page_size or 20)),
    }
    if platform:
        params["platform"] = platform
    if account_type:
        params["type"] = account_type
    if str(search or "").strip():
        params["search"] = str(search).strip()
    if str(status or "").strip() and str(status).strip().lower() not in {"all", "any"}:
        params["status"] = str(status).strip()
    if group_id not in (None, "", 0, "0"):
        params["group_id"] = str(group_id)
    resp = session.get(f"{api_base}/admin/accounts", params=params, headers=headers, timeout=45, verify=_verify_tls(cfg))
    data = _json_response(resp)
    if resp.status_code != 200:
        raise RuntimeError(f"sub2api 账号列表读取失败: HTTP {resp.status_code}, {json.dumps(data, ensure_ascii=False)[:800]}")
    items, total = _extract_account_items(data)
    with _state_lock:
        cached = _load_health_state().get("accounts") or {}
    public_items: list[dict[str, Any]] = []
    for raw_item in items:
        item = _public_sub2api_account(raw_item)
        raw_id = item.get("id") or item.get("account_id")
        health = cached.get(str(raw_id)) if isinstance(cached, dict) else None
        if isinstance(health, dict):
            item["warehouse_health"] = health
        public_items.append(item)
    return {
        "ok": True,
        "page": params["page"],
        "page_size": params["page_size"],
        "total": total,
        "items": public_items,
        "raw": data if total == 0 and not items else None,
    }


def list_sub2api_groups() -> dict[str, Any]:
    with _lock:
        cfg = _load()
    if not cfg.get("enabled"):
        raise RuntimeError("请先启用 sub2api")
    api_base = _api_base(str(cfg.get("base_url") or ""))
    if not api_base:
        raise RuntimeError("请先填写 sub2api 地址")
    session = _session()
    token = _login_token(session, cfg)
    headers = _admin_auth_headers(cfg, token, content_type="", accept="application/json")
    errors: list[str] = []
    for path in ("/admin/groups/all", "/admin/groups"):
        resp = session.get(f"{api_base}{path}", headers=headers, timeout=30, verify=_verify_tls(cfg))
        data = _json_response(resp)
        if resp.status_code == 200:
            body = _extract_data(data)
            groups = body if isinstance(body, list) else body.get("items") if isinstance(body, dict) and isinstance(body.get("items"), list) else body.get("groups") if isinstance(body, dict) and isinstance(body.get("groups"), list) else []
            return {"ok": True, "items": groups if isinstance(groups, list) else [], "path": path}
        errors.append(f"{path}: HTTP {resp.status_code}")
    return {"ok": False, "items": [], "errors": errors}


def test_sub2api_accounts(account_ids: list[int | str] | None = None, *, group_id: int | str | None = None, model_id: str = "", prompt: str = "hi", mode: str = "", concurrency: int | None = None, apply_policy: bool = False) -> dict[str, Any]:
    with _lock:
        cfg = _load()
    ids: list[int] = []
    for raw in account_ids or []:
        try:
            value = int(raw)
        except Exception:
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        page_number = 1
        page_size = 1000
        total_seen = 0
        while True:
            listing = list_sub2api_accounts(page=page_number, page_size=page_size, group_id=group_id)
            items = listing.get("items") if isinstance(listing.get("items"), list) else []
            if not items:
                break
            total_seen += len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_id = item.get("id") or item.get("account_id")
                try:
                    value = int(raw_id)
                except Exception:
                    continue
                if value > 0 and value not in ids:
                    ids.append(value)
            try:
                total = int(listing.get("total") or 0)
            except Exception:
                total = 0
            if (total and total_seen >= total) or len(items) < page_size:
                break
            page_number += 1
    if not ids:
        raise RuntimeError("请选择要测活的 sub2api 账号")
    results: list[dict[str, Any]] = []
    workers = max(1, min(len(ids), int(concurrency or cfg.get("test_concurrency") or 5)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(test_sub2api_account, account_id, model_id=model_id, prompt=prompt, mode=mode): account_id
            for account_id in ids
        }
        for future in as_completed(futures):
            account_id = futures[future]
            try:
                result = future.result()
            except Exception as error:
                result = {"ok": False, "account_id": account_id, "error": str(error), "tested_at": _now()}
                _cache_health_result(account_id, result)
            if apply_policy and not result.get("ok"):
                if cfg.get("delete_on_failure"):
                    result["policy_action"] = "delete"
                    result["policy_result"] = delete_sub2api_account(account_id)
                elif cfg.get("disable_on_failure"):
                    result["policy_action"] = "disable"
                    result["policy_result"] = set_sub2api_account_status(account_id, True)
            results.append(result)
    success = sum(1 for item in results if item.get("ok"))
    return {
        "ok": success == len(results),
        "success": success,
        "fail": len(results) - success,
        "total": len(results),
        "results": results,
        "tested_at": _now(),
    }


def get_sub2api_accounts_usage(account_ids: list[int | str]) -> dict[str, Any]:
    ids: list[int] = []
    for raw in account_ids or []:
        try:
            account_id = int(raw)
        except Exception:
            continue
        if account_id > 0 and account_id not in ids:
            ids.append(account_id)
    if not ids:
        raise RuntimeError("请选择要查看额度的 sub2api 账号")
    results: list[dict[str, Any]] = []
    with _lock:
        cfg = _load()
    workers = max(1, min(len(ids), int(cfg.get("test_concurrency") or 5)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(get_sub2api_account_usage, account_id): account_id for account_id in ids}
        for future in as_completed(futures):
            account_id = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                results.append({"ok": False, "account_id": account_id, "error": str(error), "checked_at": _now()})
    success = sum(1 for item in results if item.get("ok"))
    return {"ok": success == len(results), "total": len(results), "success": success, "fail": len(results) - success, "results": results}


def act_on_sub2api_accounts(account_ids: list[int | str], action: str) -> dict[str, Any]:
    ids: list[int] = []
    for raw in account_ids or []:
        try:
            account_id = int(raw)
        except Exception:
            continue
        if account_id > 0 and account_id not in ids:
            ids.append(account_id)
    action_name = str(action or "").strip().lower()
    if action_name not in {"enable", "disable", "delete"}:
        raise RuntimeError("sub2api 账号操作无效")
    if not ids:
        raise RuntimeError("请选择 sub2api 账号")
    results: list[dict[str, Any]] = []
    for account_id in ids:
        if action_name == "delete":
            result = delete_sub2api_account(account_id)
        else:
            result = set_sub2api_account_status(account_id, action_name == "disable")
        result["action"] = action_name
        results.append(result)
    success = sum(1 for item in results if item.get("ok"))
    return {"ok": success == len(results), "total": len(results), "success": success, "fail": len(results) - success, "results": results}


def test_sub2api_connection() -> dict[str, Any]:
    listing = list_sub2api_accounts(page=1, page_size=1)
    return {"ok": True, "accounts": int(listing.get("total") or 0), "tested_at": _now()}


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
        with _lock:
            cfg = _load()
        if cfg.get("enabled") and cfg.get("scheduled_enabled"):
            with _state_lock:
                state = _load_health_state()
                last_run = str(state.get("last_scheduled_run_at") or "")
            if _is_due(last_run, int(cfg.get("check_interval_minutes") or 60)):
                try:
                    result = test_sub2api_accounts(
                        model_id=str(cfg.get("test_model_id") or ""),
                        prompt=str(cfg.get("test_prompt") or "hi"),
                        mode=str(cfg.get("test_mode") or ""),
                        concurrency=int(cfg.get("test_concurrency") or 5),
                        apply_policy=True,
                    )
                except Exception as error:
                    result = {"ok": False, "error": str(error), "tested_at": _now()}
                with _state_lock:
                    state = _load_health_state()
                    state["last_scheduled_run_at"] = _now()
                    state["last_scheduled_result"] = result
                    _save_health_state(state)
        time.sleep(15)


def _ensure_scheduler(cfg: dict[str, Any] | None = None) -> None:
    global _scheduler
    current = cfg or _load()
    if not current.get("scheduled_enabled"):
        return
    with _scheduler_lock:
        if _scheduler and _scheduler.is_alive():
            return
        _scheduler = threading.Thread(target=_schedule_loop, daemon=True, name="sub2api-warehouse-scheduler")
        _scheduler.start()
