from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

from services import mailbox_pool_store, partial_account_store, proxy_pool_store, sub2api_store
from services.account_token_profile import SUB2API_OAUTH_CLIENT_ID, jwt_payload, token_profile
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.registered_account_store import (
    account_matches_selector,
    account_pool,
    add_registered_account,
    list_registered_accounts,
    update_registered_account,
)


OAUTH_SESSIONS_FILE = DATA_DIR / "account_oauth_sessions.json"
OAUTH_SESSION_TTL_SECONDS = 30 * 60
_lock = Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_sessions() -> dict[str, dict[str, Any]]:
    raw = read_json(OAUTH_SESSIONS_FILE, {}, expected_schema_version=1)
    sessions = raw if isinstance(raw, dict) else {}
    now = time.time()
    return {
        str(key): value
        for key, value in sessions.items()
        if isinstance(value, dict) and float(value.get("expires_at_epoch") or 0) > now
    }


def _save_sessions(sessions: dict[str, dict[str, Any]]) -> None:
    OAUTH_SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(OAUTH_SESSIONS_FILE, sessions, schema_version=1)


def _find_registered(selector: str) -> dict[str, Any] | None:
    for account in list_registered_accounts(include_secret=True):
        if account_matches_selector(account, selector):
            return account
    return None


def _find_partial(selector: str) -> dict[str, Any] | None:
    target = str(selector or "").strip().lower()
    for account in partial_account_store.list_partial_accounts(include_secret=True):
        if target in {str(account.get("id") or "").lower(), str(account.get("email") or "").strip().lower()}:
            return account
    return None


def _target_account(source: str, selector: str) -> dict[str, Any]:
    account = _find_partial(selector) if source == "partial" else _find_registered(selector)
    if not account:
        raise RuntimeError("账号不存在或已被移动")
    return account


def _state_from_auth_url(auth_url: str) -> str:
    try:
        return str(parse_qs(urlparse(auth_url).query).get("state", [""])[0]).strip()
    except Exception:
        return ""


def start_account_oauth(*, source: str, selector: str, redirect_uri: str = "") -> dict[str, Any]:
    source = str(source or "registered").strip().lower()
    if source not in {"registered", "partial"}:
        raise RuntimeError("OAuth 来源必须是 registered 或 partial")
    account = _target_account(source, selector)
    registration_proxy = account.get("registration_proxy") if isinstance(account.get("registration_proxy"), dict) else {}
    proxy_assignment: dict[str, Any] = {}
    if registration_proxy or str(account.get("registration_proxy_node_id") or "").strip():
        proxy_assignment = proxy_pool_store.assign_proxy(
            1,
            total_threads=1,
            slot_index=0,
            purpose="account_oauth",
            account=account,
            validate_node=False,
        )
        if proxy_assignment.get("sticky_failure"):
            raise RuntimeError(f"注册代理节点不可用，OAuth 未启动: {proxy_assignment.get('error') or proxy_assignment.get('label') or '-'}")
    oauth = sub2api_store.generate_openai_oauth_url(redirect_uri=redirect_uri)
    auth_url = str(oauth.get("auth_url") or "").strip()
    state = _state_from_auth_url(auth_url)
    if not state:
        raise RuntimeError("sub2api OAuth 链接缺少 state")
    job_id = secrets.token_urlsafe(24)
    expires_epoch = time.time() + OAUTH_SESSION_TTL_SECONDS
    session = {
        "job_id": job_id,
        "source": source,
        "selector": str(selector or "").strip(),
        "email": str(account.get("email") or "").strip(),
        "sub2api_session_id": str(oauth.get("session_id") or "").strip(),
        "state": state,
        "auth_url": auth_url,
        "proxy_source": str(proxy_assignment.get("source") or ""),
        "proxy_node_id": str(proxy_assignment.get("node_id") or ""),
        "proxy_label": str(proxy_assignment.get("label") or ""),
        "proxy_sticky": bool(proxy_assignment.get("sticky")),
        "created_at": _now(),
        "expires_at_epoch": expires_epoch,
    }
    with _lock:
        sessions = _load_sessions()
        sessions[job_id] = session
        _save_sessions(sessions)
    return {
        "job_id": job_id,
        "source": source,
        "email": session["email"],
        "auth_url": auth_url,
        "proxy_source": session["proxy_source"],
        "proxy_node_id": session["proxy_node_id"],
        "proxy_label": session["proxy_label"],
        "proxy_sticky": session["proxy_sticky"],
        "expires_at": datetime.fromtimestamp(expires_epoch, tz=timezone.utc).isoformat(),
    }


def _callback_params(callback: str, expected_state: str) -> tuple[str, str]:
    raw = str(callback or "").strip()
    if not raw:
        raise RuntimeError("请粘贴完整回调链接或授权 Code")
    code = raw
    state = expected_state
    if "://" in raw or "code=" in raw or raw.startswith("?"):
        parsed = urlparse(raw if "://" in raw else f"http://localhost/?{raw.lstrip('?')}")
        query = parse_qs(parsed.query)
        error = str(query.get("error_description", query.get("error", [""]))[0]).strip()
        if error:
            raise RuntimeError(f"OpenAI OAuth 返回错误: {error}")
        code = str(query.get("code", [""])[0]).strip()
        supplied_state = str(query.get("state", [""])[0]).strip()
        if supplied_state and supplied_state != expected_state:
            raise RuntimeError("OAuth state 不匹配，请重新生成授权链接")
        state = supplied_state or expected_state
    if not code:
        raise RuntimeError("回调中没有找到授权 Code")
    return code, state


def oauth_token_updates(credentials: dict[str, Any], *, provenance: str = "sub2api_manual_authorization") -> dict[str, Any]:
    access_token = str(credentials.get("access_token") or "").strip()
    refresh_token = str(credentials.get("refresh_token") or "").strip()
    id_token = str(credentials.get("id_token") or "").strip()
    if not access_token or not refresh_token:
        raise RuntimeError("OAuth 换码未返回完整的 access_token/refresh_token，请重新授权")
    access_claims = jwt_payload(access_token)
    id_claims = jwt_payload(id_token)
    auth = access_claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    profile = access_claims.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    client_id = str(credentials.get("client_id") or access_claims.get("client_id") or id_claims.get("client_id") or SUB2API_OAUTH_CLIENT_ID).strip()
    try:
        expires_at = int(credentials.get("expires_at") or access_claims.get("exp") or 0)
    except Exception:
        expires_at = 0
    updates: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "client_id": client_id,
        "token_source": "sub2api_openai_oauth",
        "oauth_status": "sub2api_oauth_acquired",
        "oauth_refresh_status": "ok",
        "oauth_refresh_error": "",
        "oauth_provenance": str(provenance or "sub2api_manual_authorization"),
        "oauth_acquired_at": _now(),
        "token_refresh_required": False,
        "status": "active",
    }
    email = str(credentials.get("email") or profile.get("email") or id_claims.get("email") or "").strip()
    if email:
        updates["email"] = email
    if expires_at:
        updates["token_expires_at"] = expires_at
        updates["token_expires_in"] = max(0, expires_at - int(time.time()))
    for target, value in {
        "chatgpt_account_id": credentials.get("chatgpt_account_id") or auth.get("chatgpt_account_id") or auth.get("account_id"),
        "chatgpt_user_id": credentials.get("chatgpt_user_id") or auth.get("user_id") or access_claims.get("sub"),
        "plan_type": credentials.get("plan_type") or auth.get("chatgpt_plan_type") or profile.get("plan_type"),
        "organization_id": credentials.get("organization_id"),
    }.items():
        if value not in (None, ""):
            updates[target] = value
    return updates


def _latest_account(email: str, chatgpt_account_id: str = "") -> dict[str, Any] | None:
    candidates = [
        item
        for item in list_registered_accounts(include_secret=True)
        if str(item.get("email") or "").strip().lower() == str(email or "").strip().lower()
    ]
    if chatgpt_account_id:
        for item in candidates:
            if str(item.get("chatgpt_account_id") or "") == chatgpt_account_id:
                return item
    return candidates[0] if candidates else None


def _import_and_verify(account: dict[str, Any]) -> dict[str, Any]:
    try:
        imported = sub2api_store.import_account_dicts_to_sub2api(account)
    except Exception as error:
        return {"ok": False, "import": {"ok": False, "error": str(error)[:800]}, "test": None}
    account_id = next((item for item in imported.get("account_ids", []) if item), None)
    if not account_id:
        account_id = next((item.get("account_id") for item in imported.get("results", []) if item.get("account_id")), None)
    test_result: dict[str, Any] | None = None
    if account_id:
        try:
            test_result = sub2api_store.test_sub2api_account(account_id)
            healthy = bool(test_result.get("ok"))
            update_registered_account(account, {
                "sub2api_account_id": account_id,
                "sub2api_health_status": "ok" if healthy else "failed",
                "sub2api_health_checked_at": test_result.get("tested_at") or _now(),
                "sub2api_health_code": int(test_result.get("status_code") or 0),
                "health_status": "ok" if healthy else "failed",
                "health_method": "sub2api",
                "health_checked_at": test_result.get("tested_at") or _now(),
                "health_code": int(test_result.get("status_code") or 0),
                "health_error": "" if healthy else str(test_result.get("body_preview") or "sub2api test failed")[:800],
            })
        except Exception as error:
            test_result = {"ok": False, "status_code": 0, "error": str(error)[:800]}
    return {"ok": bool(imported.get("ok")) and bool(test_result and test_result.get("ok")), "import": imported, "test": test_result}


def complete_account_oauth(*, job_id: str, callback: str) -> dict[str, Any]:
    with _lock:
        sessions = _load_sessions()
        session = sessions.get(str(job_id or "").strip())
    if not session:
        raise RuntimeError("OAuth 会话不存在或已过期，请重新生成授权链接")
    source = str(session.get("source") or "registered")
    target = _target_account(source, str(session.get("selector") or ""))
    code, state = _callback_params(callback, str(session.get("state") or ""))
    credentials = sub2api_store.exchange_openai_oauth_code(
        session_id=str(session.get("sub2api_session_id") or ""),
        code=code,
        state=state,
    )
    updates = oauth_token_updates(credentials)
    expected_email = str(target.get("email") or "").strip().lower()
    actual_email = str(updates.get("email") or expected_email).strip().lower()
    if expected_email and actual_email and expected_email != actual_email:
        raise RuntimeError(f"授权账号不匹配：当前操作 {expected_email}，实际登录 {actual_email}")

    if source == "partial":
        promoted = {**target, **updates, "source_type": "partial_oauth_promoted"}
        add_registered_account(promoted)
        account = _latest_account(str(updates.get("email") or target.get("email") or ""), str(updates.get("chatgpt_account_id") or ""))
        if not account:
            raise RuntimeError("OAuth 已完成，但写入成品账号库存失败")
        partial_account_store.delete_partial_account(str(target.get("id") or target.get("email") or ""))
    else:
        account = update_registered_account(target, updates)
        if not account:
            raise RuntimeError("OAuth 已完成，但更新账号库存失败")

    warehouse = _import_and_verify(account)
    with _lock:
        sessions = _load_sessions()
        sessions.pop(str(job_id or "").strip(), None)
        _save_sessions(sessions)
    latest = _latest_account(str(account.get("email") or ""), str(account.get("chatgpt_account_id") or "")) or account
    return {
        "ok": True,
        "account": latest,
        "token_profile": token_profile(latest),
        "warehouse": warehouse,
        "account_pool": account_pool(),
        "partial_account_pool": partial_account_store.partial_account_pool(include_secret=True),
    }


def acquire_account_oauth_automatically(*, source: str, selector: str) -> dict[str, Any]:
    source = str(source or "registered").strip().lower()
    if source not in {"registered", "partial"}:
        raise RuntimeError("OAuth 来源必须是 registered 或 partial")
    target = _target_account(source, selector)
    registration_proxy = target.get("registration_proxy") if isinstance(target.get("registration_proxy"), dict) else {}
    if not registration_proxy and not str(target.get("registration_proxy_node_id") or "").strip():
        raise RuntimeError("该账号没有注册节点记录，不能执行同节点自动 OAuth")
    assignment = proxy_pool_store.assign_proxy(
        1,
        total_threads=1,
        slot_index=0,
        purpose="account_oauth_automatic",
        account=target,
        validate_node=False,
    )
    if assignment.get("sticky_failure") or not str(assignment.get("proxy") or "").strip():
        raise RuntimeError(f"注册代理节点不可用，OAuth 未启动: {assignment.get('error') or assignment.get('label') or '-'}")

    email = str(target.get("email") or "").strip()
    mailbox = mailbox_pool_store.find_mailbox_for_address(
        email,
        include_secret=True,
        provider_type=str(target.get("provider") or ""),
    )
    if mailbox is None:
        mailbox = mailbox_pool_store.find_mailbox_for_address(email, include_secret=True)
    if mailbox is None:
        raise RuntimeError(f"找不到账号对应的收件邮箱，无法完成 OAuth OTP: {email}")

    from services.register.openai_register import PlatformRegistrar

    registrar = PlatformRegistrar(str(assignment.get("proxy") or ""))
    registrar.email = email
    registrar.password = str(target.get("password") or "")
    registrar.current_mailbox = mailbox
    try:
        finished = registrar.acquire_sub2api_oauth(1, target)
    finally:
        registrar.close()

    if source == "partial":
        promoted = {**target, **finished, "source_type": "partial_oauth_promoted"}
        add_registered_account(promoted)
        account = _latest_account(str(finished.get("email") or email), str(finished.get("chatgpt_account_id") or ""))
        if not account:
            raise RuntimeError("OAuth 已完成，但写入成品账号库存失败")
        partial_account_store.delete_partial_account(str(target.get("id") or email))
    else:
        account = update_registered_account(target, finished)
        if not account:
            raise RuntimeError("OAuth 已完成，但更新账号库存失败")

    warehouse = _import_and_verify(account)
    latest = _latest_account(str(account.get("email") or email), str(account.get("chatgpt_account_id") or "")) or account
    return {
        "ok": True,
        "account": latest,
        "token_profile": token_profile(latest),
        "warehouse": warehouse,
        "proxy_source": str(assignment.get("source") or ""),
        "proxy_node_id": str(assignment.get("node_id") or ""),
        "proxy_label": str(assignment.get("label") or ""),
        "proxy_sticky": bool(assignment.get("sticky")),
        "account_pool": account_pool(),
        "partial_account_pool": partial_account_store.partial_account_pool(include_secret=True),
    }


def import_partial_account_to_sub2api(selector: str) -> dict[str, Any]:
    target = _find_partial(selector)
    if not target:
        raise RuntimeError("半成品账号不存在")
    profile = token_profile(target)
    if not profile.get("oauth_capable"):
        raise RuntimeError("该半成品账号没有可用 OAuth Token，请先执行“获取 OAuth 并导入”")
    promoted = {**target, "source_type": "partial_warehouse_promoted", "status": "active"}
    add_registered_account(promoted)
    account = _latest_account(str(target.get("email") or ""), str(target.get("chatgpt_account_id") or ""))
    if not account:
        raise RuntimeError("写入成品账号库存失败")
    warehouse = _import_and_verify(account)
    partial_account_store.delete_partial_account(str(target.get("id") or target.get("email") or ""))
    latest = _latest_account(str(account.get("email") or ""), str(account.get("chatgpt_account_id") or "")) or account
    return {
        "ok": True,
        "account": latest,
        "warehouse": warehouse,
        "account_pool": account_pool(),
        "partial_account_pool": partial_account_store.partial_account_pool(include_secret=True),
    }


def import_partial_accounts_to_sub2api(selectors: list[str] | None = None) -> dict[str, Any]:
    accounts = partial_account_store.select_partial_accounts(selectors, include_secret=True)
    targets = [
        str(account.get("id") or account.get("email") or "")
        for account in accounts
        if token_profile(account).get("oauth_capable")
    ]
    if not targets:
        raise RuntimeError("当前选择中没有可直接导入 sub2api 的 OAuth 半成品账号")
    results: list[dict[str, Any]] = []
    for target in targets:
        try:
            result = import_partial_account_to_sub2api(target)
            warehouse = result.get("warehouse") if isinstance(result.get("warehouse"), dict) else {}
            account = result.get("account") if isinstance(result.get("account"), dict) else {}
            results.append({
                "ok": bool(warehouse.get("ok")),
                "email": str(account.get("email") or ""),
                "status_code": int(warehouse.get("status_code") or 0),
                "warehouse": warehouse,
            })
        except Exception as error:
            results.append({"ok": False, "selector": target, "status_code": 0, "error": str(error)[:800]})
    success = sum(1 for result in results if result.get("ok"))
    return {
        "ok": success == len(results),
        "partial": 0 < success < len(results),
        "total": len(results),
        "success": success,
        "fail": len(results) - success,
        "results": results,
        "account_pool": account_pool(),
        "partial_account_pool": partial_account_store.partial_account_pool(include_secret=True),
    }


def refresh_partial_account_token(selector: str) -> dict[str, Any]:
    target = _find_partial(selector)
    if not target:
        raise RuntimeError("半成品账号不存在")
    refresh_token = str(target.get("refresh_token") or "").strip()
    if not refresh_token:
        partial_account_store.update_partial_account(selector, {
            "oauth_status": "missing_refresh_token",
            "oauth_refresh_status": "refresh_token_missing",
            "oauth_refresh_error": "账号没有 refresh_token",
            "token_refresh_required": True,
        })
        raise RuntimeError("账号没有 refresh_token")

    assignment = proxy_pool_store.assign_proxy(
        1,
        total_threads=1,
        slot_index=0,
        purpose="partial_account_refresh_token",
        account=target,
        validate_node=False,
    )
    if assignment.get("sticky_failure"):
        raise RuntimeError(f"注册代理节点不可用，Token 未刷新: {assignment.get('error') or assignment.get('label') or '-'}")

    from services.account_inventory_service import (
        _classify_failure,
        _compact_error,
        _local_health_and_quota,
        _refresh_platform_tokens,
        _token_context,
    )

    proxy = str(assignment.get("proxy") or "")
    try:
        refreshed = _refresh_platform_tokens(
            refresh_token,
            client_id=str(target.get("client_id") or ""),
            proxy=proxy,
        )
    except Exception as error:
        error_text = _compact_error(error, 800)
        status = _classify_failure(0, f"refresh_token {error_text}", default="refresh_token_refresh_failed")
        partial_account_store.update_partial_account(selector, {
            "oauth_status": "refresh_failed",
            "oauth_refresh_status": status,
            "oauth_refresh_error": error_text,
            "token_refresh_required": True,
        })
        raise RuntimeError(f"{status}: {error_text}") from error

    access_token = str(refreshed.get("access_token") or "").strip()
    updates: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": str(refreshed.get("refresh_token") or refresh_token).strip(),
        "id_token": str(refreshed.get("id_token") or target.get("id_token") or "").strip(),
        "client_id": str(refreshed.get("client_id") or target.get("client_id") or ""),
        "oauth_refreshed_at": str(refreshed.get("refreshed_at") or _now()),
        "oauth_status": "refreshed_manual",
        "oauth_refresh_status": "ok",
        "oauth_refresh_error": "",
        "token_refresh_required": False,
    }
    if access_token:
        context = _token_context(access_token, updates.get("id_token"))
        if context.get("plan_type"):
            updates["plan_type"] = context["plan_type"]
            updates["account_type"] = context["plan_type"]
        for key in ("chatgpt_account_id", "chatgpt_user_id"):
            if context.get(key) not in (None, ""):
                updates[key] = context[key]
        if context.get("expires_at") not in (None, ""):
            updates["token_expires_at"] = context["expires_at"]
        if context.get("expires_in") not in (None, ""):
            updates["token_expires_in"] = context["expires_in"]
    try:
        probe = _local_health_and_quota(
            {**target, **updates},
            refresh_quota=True,
            proxy=proxy,
            force_refresh_token=False,
        )
        probe_updates = probe.get("updates") if isinstance(probe, dict) else {}
        if isinstance(probe_updates, dict):
            updates.update(probe_updates)
    except Exception:
        pass
    updated = partial_account_store.update_partial_account(selector, updates)
    if not updated:
        raise RuntimeError("Token 已刷新，但更新半成品库存失败")
    return {
        "ok": True,
        "partial_account": updated,
        "partial_account_pool": partial_account_store.partial_account_pool(include_secret=True),
        "proxy_source": str(assignment.get("source") or ""),
        "proxy_node_id": str(assignment.get("node_id") or ""),
        "proxy_label": str(assignment.get("label") or ""),
        "proxy_sticky": bool(assignment.get("sticky")),
    }
