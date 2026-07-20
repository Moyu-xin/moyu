from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from curl_cffi import requests

from services import mailbox_pool_store, proxy_pool_store, sub2api_store
from services.account_token_profile import SUB2API_OAUTH_CLIENT_ID
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR, DEFAULT_PROXY_RUNTIME_USER_AGENT
from services.grok.oauth import CLI_BASE_URL as GROK_CLI_BASE_URL
from services.grok.oauth import CLI_HEADERS as GROK_CLI_HEADERS
from services.grok.oauth import GROK_CLI_CLIENT_ID, TOKEN_ENDPOINT as GROK_TOKEN_ENDPOINT
from services.proxy_service import proxy_settings
from services.security_redaction import redact_text, redact_value
from services.registered_account_store import (
    account_pool,
    account_matches_selector,
    delete_registered_accounts,
    list_registered_accounts,
    prune_banned_accounts,
    update_registered_account,
)

ACCOUNT_INVENTORY_TASK_FILE = DATA_DIR / "account_inventory_task.json"
ACCOUNT_INVENTORY_SETTINGS_FILE = DATA_DIR / "account_inventory_settings.json"
MAX_TASK_LOGS = 1500
MAX_TASK_RESULTS = 1500
AUTH_BASE = "https://auth.openai.com"
CHATGPT_BASE = "https://chatgpt.com"
CHATGPT_SESSION_URL = f"{CHATGPT_BASE}/api/auth/session"
PLATFORM_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_OAUTH_REDIRECT_URI = "https://platform.openai.com/auth/callback"
SUB2API_OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
PLATFORM_AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _default_settings() -> dict[str, Any]:
    return {
        "scheduled_enabled": False,
        "interval_minutes": 60,
        "stale_after_minutes": 240,
        "method": "auto",
        "refresh_quota_on_health": True,
        "import_auto_health_check": False,
        "import_drop_banned": False,
        "auto_drop_banned": False,
        "model_id": "",
        "prompt": "hi",
        "mode": "",
        "concurrency": 10,
        "updated_at": "",
    }


def _normalize_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = _default_settings()
    if isinstance(raw, dict):
        cfg.update({key: value for key, value in raw.items() if key in cfg})
    cfg["scheduled_enabled"] = bool(cfg.get("scheduled_enabled"))
    cfg["interval_minutes"] = max(1, int(cfg.get("interval_minutes") or 60))
    cfg["stale_after_minutes"] = max(1, int(cfg.get("stale_after_minutes") or 240))
    cfg["method"] = str(cfg.get("method") or "auto").strip().lower()
    if cfg["method"] not in {"auto", "local", "sub2api"}:
        cfg["method"] = "auto"
    for key in ("refresh_quota_on_health", "import_auto_health_check", "import_drop_banned", "auto_drop_banned"):
        cfg[key] = bool(cfg.get(key))
    cfg["model_id"] = str(cfg.get("model_id") or "")
    cfg["prompt"] = str(cfg.get("prompt") or "hi") or "hi"
    cfg["mode"] = str(cfg.get("mode") or "")
    cfg["concurrency"] = max(1, min(64, int(cfg.get("concurrency") or 10)))
    cfg["updated_at"] = str(cfg.get("updated_at") or "")
    return cfg


def _default_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "schedule_running": False,
        "current_job": None,
        "stats": {"success": 0, "fail": 0, "done": 0, "running": 0, "total": 0, "started_at": "", "updated_at": "", "finished_at": ""},
        "logs": [],
        "results": [],
        "last_scheduled_run_at": "",
    }


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


def _token_context(access_token: object = "", id_token: object = "") -> dict[str, Any]:
    access_claims = _jwt_payload(access_token)
    id_claims = _jwt_payload(id_token)
    auth = access_claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    profile = access_claims.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
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
        "chatgpt_user_id": _first_non_empty(auth.get("chatgpt_account_user_id"), auth.get("user_id"), access_claims.get("sub"), id_claims.get("sub")),
        "plan_type": _first_non_empty(auth.get("chatgpt_plan_type"), profile.get("plan_type")),
        "client_id": _first_non_empty(access_claims.get("client_id"), id_claims.get("client_id")),
        "expires_at": exp,
        "expires_in": max(0, exp - int(time.time())) if exp else None,
    }


def _is_stale(checked_at: str, stale_after_minutes: int) -> bool:
    if not checked_at:
        return True
    try:
        checked = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - checked).total_seconds() >= stale_after_minutes * 60
    except Exception:
        return True


def _response_json(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"body": str(getattr(resp, "text", "") or "")[:1200]}


def _session(proxy: str = "") -> requests.Session:
    kwargs = proxy_settings.build_session_kwargs(proxy=proxy, upstream=True, impersonate="chrome", verify=proxy_settings.verify_tls())
    return requests.Session(**kwargs)


def _refresh_platform_tokens(refresh_token: str, *, client_id: str = "", proxy: str = "") -> dict[str, Any]:
    if not str(refresh_token or "").strip():
        raise RuntimeError("缺少 refresh_token")
    session = _session(proxy)
    resolved_client_id = str(client_id or PLATFORM_OAUTH_CLIENT_ID).strip()
    redirect_uri = SUB2API_OAUTH_REDIRECT_URI if resolved_client_id == SUB2API_OAUTH_CLIENT_ID else PLATFORM_OAUTH_REDIRECT_URI
    resp = session.post(
        f"{AUTH_BASE}/api/accounts/oauth/token",
        headers={
            "accept": "*/*",
            "auth0-client": PLATFORM_AUTH0_CLIENT,
            "content-type": "application/json",
            "origin": "https://platform.openai.com",
            "referer": "https://platform.openai.com/",
            "user-agent": DEFAULT_PROXY_RUNTIME_USER_AGENT,
        },
        json={"client_id": resolved_client_id, "grant_type": "refresh_token", "refresh_token": refresh_token, "redirect_uri": redirect_uri},
        timeout=60,
        verify=proxy_settings.verify_tls(),
    )
    data = _response_json(resp)
    if int(getattr(resp, "status_code", 0) or 0) != 200:
        raise RuntimeError(f"刷新 OAuth token 失败: HTTP {getattr(resp, 'status_code', 0)}, {json.dumps(data, ensure_ascii=False)[:500]}")
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("刷新响应缺少 access_token")
    return {"access_token": access_token, "refresh_token": str(data.get("refresh_token") or refresh_token).strip(), "id_token": str(data.get("id_token") or "").strip(), "client_id": resolved_client_id, "refreshed_at": _now()}


def _headers(access_token: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {access_token}", "Content-Type": "application/json", "Origin": CHATGPT_BASE, "Referer": f"{CHATGPT_BASE}/", "User-Agent": DEFAULT_PROXY_RUNTIME_USER_AGENT}


def _safe_backend_get(session: requests.Session, access_token: str, path: str, *, timeout: int = 60) -> tuple[int, dict[str, Any], str]:
    try:
        resp = session.get(f"{CHATGPT_BASE}{path}", headers=_headers(access_token), timeout=timeout, verify=proxy_settings.verify_tls())
        text = str(getattr(resp, "text", "") or "")
        return int(getattr(resp, "status_code", 0) or 0), _response_json(resp), text
    except Exception as error:
        text = " ".join(str(error or "").replace("\r", " ").replace("\n", " ").split())[:800]
        return 0, {"error": text}, text


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


PLAN_ALIASES = {
    "chatgpt_plus": "plus",
    "plus": "plus",
    "paid": "plus",
    "premium": "plus",
    "pro": "pro",
    "team": "team",
    "teams": "team",
    "business": "business",
    "enterprise": "enterprise",
    "edu": "team",
    "education": "team",
    "k12": "k12",
    "free": "free",
}
PLAN_RANK = {"free": 0, "plus": 10, "pro": 20, "k12": 25, "team": 30, "business": 40, "enterprise": 50}


def _normalize_plan(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9_+-]+", "_", text).strip("_")
    if text in PLAN_ALIASES:
        return PLAN_ALIASES[text]
    if "enterprise" in text:
        return "enterprise"
    if "business" in text:
        return "business"
    if "team" in text or "edu" in text:
        return "team"
    if "k12" in text:
        return "k12"
    if "pro" in text:
        return "pro"
    if "plus" in text or "paid" in text or "premium" in text:
        return "plus"
    if "free" in text:
        return "free"
    return text


def _plan_rank(value: Any) -> int:
    return PLAN_RANK.get(_normalize_plan(value), 1)


def _extract_plan(*payloads: dict[str, Any]) -> str:
    candidates: list[str] = []

    def add(value: Any) -> None:
        plan = _normalize_plan(value)
        if plan:
            candidates.append(plan)

    for payload in payloads:
        for item in _walk_dicts(payload):
            for key in (
                "plan_type",
                "planType",
                "chatgpt_plan_type",
                "account_plan",
                "accountPlan",
                "account_plan_type",
                "account_type",
                "accountType",
                "subscription_plan",
                "subscriptionPlan",
                "subscription_tier",
                "subscriptionTier",
                "billing_plan",
                "billingPlan",
                "current_plan",
                "currentPlan",
                "current_plan_type",
                "currentPlanType",
                "active_plan",
                "activePlan",
                "workspace_plan",
                "workspacePlan",
                "plan_name",
                "planName",
            ):
                value = item.get(key)
                if isinstance(value, dict):
                    nested = _first_non_empty(value.get("type"), value.get("name"), value.get("title"), value.get("label"), value.get("slug"))
                    if nested:
                        add(nested)
                elif value not in (None, ""):
                    add(value)
    if not candidates:
        return ""
    return max(candidates, key=_plan_rank)


def _account_has_k12_marker(account: dict[str, Any] | None) -> bool:
    account = account or {}
    return (
        _normalize_plan(account.get("plan_type")) == "k12"
        or _normalize_plan(account.get("account_type")) == "k12"
        or bool(account.get("k12_mode"))
        or bool(str(account.get("k12_join_status") or "").strip())
    )


def _account_type_for_plan(plan: Any, account: dict[str, Any] | None = None) -> str:
    normalized = _normalize_plan(plan) or "free"
    account = account or {}
    if _account_has_k12_marker(account):
        return "k12"
    if normalized in {"plus", "pro", "team", "business", "enterprise"}:
        return normalized
    if normalized == "k12":
        return "k12"
    return normalized


def _plan_updates(plan: Any, account: dict[str, Any] | None = None, *, allow_downgrade: bool = True) -> dict[str, str]:
    normalized = _normalize_plan(plan) or "free"
    current = _normalize_plan((account or {}).get("plan_type") or (account or {}).get("account_type"))
    if current and _plan_rank(current) > _plan_rank(normalized) and not allow_downgrade:
        return {
            "plan_type": current,
            "account_type": _account_type_for_plan(current, account),
            "plan_refresh_note": f"skip_downgrade_from_{current}_to_{normalized}",
        }
    return {"plan_type": normalized, "account_type": _account_type_for_plan(normalized, account)}


def _quota_candidates_from_dict(item: dict[str, Any]) -> list[dict[str, Any]]:
    lowered_keys = {str(k).lower(): k for k in item.keys()}
    total_key = next((lowered_keys[k] for k in lowered_keys if k in {"limit", "total", "cap", "max", "quota", "message_cap"} or k.endswith("_limit")), None)
    used_key = next((lowered_keys[k] for k in lowered_keys if k in {"used", "usage", "consumed", "current"} or k.endswith("_used")), None)
    remaining_key = next((lowered_keys[k] for k in lowered_keys if k in {"remaining", "left", "available"} or k.endswith("_remaining")), None)
    if not any([total_key, used_key, remaining_key]):
        return []
    total = _num(item.get(total_key)) if total_key else None
    used = _num(item.get(used_key)) if used_key else None
    remaining = _num(item.get(remaining_key)) if remaining_key else None
    if total is None and used is not None and remaining is not None:
        total = used + remaining
    if remaining is None and total is not None and used is not None:
        remaining = max(0.0, total - used)
    if used is None and total is not None and remaining is not None:
        used = max(0.0, total - remaining)
    if total is None and used is None and remaining is None:
        return []
    label = _first_non_empty(item.get("model"), item.get("model_slug"), item.get("slug"), item.get("name"), item.get("title"), item.get("type"), "quota")
    return [{"label": label, "used": used, "total": total, "remaining": remaining, "reset_at": _first_non_empty(item.get("reset_at"), item.get("reset_time"), item.get("resets_at"), item.get("next_reset_at"))}]


def _summarize_quota(*payloads: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for payload in payloads:
        for item in _walk_dicts(payload):
            candidates.extend(_quota_candidates_from_dict(item))
    compact: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if marker not in seen:
            seen.add(marker)
            compact.append(item)
        if len(compact) >= 30:
            break
    primary = next((item for item in compact if item.get("total") is not None or item.get("remaining") is not None), compact[0] if compact else {})
    return {"quota_status": "known" if compact else "unknown", "quota_used": primary.get("used"), "quota_total": primary.get("total"), "quota_remaining": primary.get("remaining"), "quota_reset_at": primary.get("reset_at") or "", "quota_details": {"items": compact, "source": "chatgpt-backend"}}


def _quota_reset_at(value: Any) -> str:
    numeric = _num(value)
    if numeric is None:
        return str(value or "")
    try:
        if numeric > 10_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return str(value or "")


def _wham_window(label: str, window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    used = _num(window.get("used_percent"))
    remaining = _num(window.get("remaining_percent"))
    if used is None and remaining is not None:
        used = max(0.0, 100.0 - remaining)
    if remaining is None and used is not None:
        remaining = max(0.0, 100.0 - used)
    if used is None and remaining is None:
        return None
    return {
        "label": label,
        "used": round(float(used or 0.0), 2),
        "total": 100.0,
        "remaining": round(float(remaining or 0.0), 2),
        "reset_at": _quota_reset_at(window.get("reset_at")),
        "window_seconds": _num(window.get("limit_window_seconds")),
        "reset_after_seconds": _num(window.get("reset_after_seconds")),
    }


def _summarize_wham_usage(payload: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    def add_rate(label: str, rate: Any) -> None:
        if not isinstance(rate, dict):
            return
        for window_name in ("primary_window", "secondary_window"):
            item = _wham_window(f"{label}.{window_name}", rate.get(window_name))
            if item:
                items.append(item)

    rate_limit = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
    code_review = payload.get("code_review_rate_limit") if isinstance(payload.get("code_review_rate_limit"), dict) else {}
    add_rate("rate_limit", rate_limit)
    add_rate("code_review_rate_limit", code_review)
    additional = payload.get("additional_rate_limits")
    if isinstance(additional, dict):
        for name, rate in additional.items():
            add_rate(f"additional.{name}", rate)
    elif isinstance(additional, list):
        for index, rate in enumerate(additional[:20]):
            label = _first_non_empty((rate or {}).get("name") if isinstance(rate, dict) else "", f"additional.{index + 1}")
            add_rate(label, rate)

    primary = next((item for item in items if item.get("label") == "rate_limit.primary_window"), items[0] if items else {})
    limit_reached = bool(rate_limit.get("limit_reached"))
    allowed = rate_limit.get("allowed")
    if not items and (limit_reached or allowed is False):
        primary = {"label": "rate_limit.primary_window", "used": 100.0, "total": 100.0, "remaining": 0.0, "reset_at": ""}
        items.append(primary)
    return {
        "quota_status": "known" if items else "unknown",
        "quota_used": primary.get("used"),
        "quota_total": primary.get("total"),
        "quota_remaining": primary.get("remaining"),
        "quota_reset_at": primary.get("reset_at") or "",
        "quota_details": {
            "items": items[:40],
            "source": "chatgpt-wham",
            "allowed": allowed,
            "limit_reached": limit_reached,
            "plan_type": str(payload.get("plan_type") or ""),
        },
    }


def _extract_model_summary(models_payload: dict[str, Any]) -> dict[str, Any]:
    models = []
    for item in _walk_dicts(models_payload):
        for key in ("models", "items"):
            value = item.get(key)
            if isinstance(value, list):
                for model in value:
                    if isinstance(model, dict):
                        slug = _first_non_empty(model.get("slug"), model.get("id"), model.get("model"), model.get("name"))
                        title = _first_non_empty(model.get("title"), model.get("name"), slug)
                        if slug:
                            models.append({"slug": slug, "title": title})
                if models:
                    return {"available_model_count": len(models), "models": models[:80]}
    return {"available_model_count": 0, "models": []}


def _infer_plan_from_models(models_summary: dict[str, Any]) -> str:
    """ChatGPT 的套餐接口经常不直接返回 plan，用模型权限做兜底判断。

    当前项目已有历史测活数据能看到明显的两档：
    - Free 通常只有 8 个基础模型/auto/research。
    - Plus 账号会多出 o3、instant、thinking、wm/cca-wm 等模型，数量通常明显更多。

    这里仅在看到强 Plus 特征时返回 plus；没有证据时返回空，避免把 Free 误判成 Plus。
    """
    if not isinstance(models_summary, dict):
        return ""
    models = models_summary.get("models")
    if not isinstance(models, list):
        models = []
    slugs: list[str] = []
    titles: list[str] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        slug = str(model.get("slug") or model.get("id") or model.get("model") or model.get("name") or "").strip().lower()
        title = str(model.get("title") or model.get("name") or "").strip().lower()
        if slug:
            slugs.append(slug)
        if title:
            titles.append(title)
    count = int(models_summary.get("available_model_count") or len(slugs) or 0)
    plus_slug_markers = (
        "o3",
        "o4",
        "gpt-4",
        "gpt-5-6",
        "gpt-5.6",
        "-instant",
        "-thinking",
        "-wm",
        ".5-wm",
        ".6-",
        "cca-wm",
        "sol-wm",
        "terra-wm",
        "luna-wm",
    )
    if any(slug == marker or slug.startswith(f"{marker}-") or marker in slug for slug in slugs for marker in plus_slug_markers):
        return "plus"
    # 模型列表特别长时，通常也是 Plus；阈值高一点，避免普通 Free 被误判。
    if count >= 12:
        return "plus"
    # title 只作为弱兜底，排除 Free 常见的 "Thinking Mini"。
    if any(("plus" in title or "pro" in title) for title in titles):
        return "plus"
    return ""


TYPE_REFRESH_ENDPOINTS = (
    ("models", "/backend-api/models"),
    ("accounts_check", "/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480"),
    ("billing_page_config", "/backend-api/pageConfigs/billing"),
    ("accounts_optimized", "/backend-api/accounts/optimized/check"),
    ("settings_user", "/backend-api/settings/user"),
)


def _probe_account_type(session: requests.Session, access_token: str) -> dict[str, Any]:
    """读取 ChatGPT 账号类型。

    Free / Plus 以 `/backend-api/models` 的实际模型权限为准：
    普通 Free 也可能在其它账号检查接口里出现可购买的 Plus/订阅文案，不能把这些
    嵌套字段当成当前账号类型，否则会把整池误刷成 Plus。
    """
    payloads: list[dict[str, Any]] = []
    endpoints: list[dict[str, Any]] = []
    metadata_plans: list[str] = []
    model_checked = False
    model_plan = ""
    ok_count = 0
    for name, path in TYPE_REFRESH_ENDPOINTS:
        started = time.perf_counter()
        item: dict[str, Any] = {"name": name, "path": path, "status_code": 0, "ok": False}
        try:
            resp = session.get(f"{CHATGPT_BASE}{path}", headers=_headers(access_token), timeout=45, verify=proxy_settings.verify_tls())
            status_code = int(getattr(resp, "status_code", 0) or 0)
            data = _response_json(resp)
            item.update({"status_code": status_code, "ok": status_code == 200, "latency_ms": int((time.perf_counter() - started) * 1000)})
            if status_code == 200:
                ok_count += 1
                payloads.append(data)
                if name == "models":
                    model_checked = True
                    model_summary = _extract_model_summary(data)
                    item["available_model_count"] = model_summary.get("available_model_count", 0)
                    inferred_from_models = _infer_plan_from_models(model_summary)
                    if inferred_from_models:
                        model_plan = inferred_from_models
                        item["model_inferred_plan"] = inferred_from_models
                        item["plan_type"] = inferred_from_models
                    else:
                        item["plan_type"] = "free"
                else:
                    plan = _extract_plan(data)
                    if plan:
                        # plus/free 在这些接口里经常来自营销、可购买套餐或页面配置；
                        # 只有比 Plus 更高的企业/团队/K12 等强类型才作为元数据兜底。
                        if plan in {"pro", "team", "business", "enterprise", "k12"}:
                            metadata_plans.append(plan)
                            item["plan_type"] = plan
                        else:
                            item["metadata_plan_ignored"] = plan
            else:
                item["error"] = _compact_error(data, 240)
        except Exception as error:
            item.update({"error": _compact_error(error, 240), "latency_ms": int((time.perf_counter() - started) * 1000)})
        endpoints.append(item)
    if model_checked:
        plan = model_plan or "free"
    elif metadata_plans:
        plan = max(metadata_plans, key=_plan_rank)
    else:
        plan = ""
    return {"plan_type": plan, "endpoints": endpoints, "ok_count": ok_count, "model_checked": model_checked}


def _extract_email_from_payload(payload: Any) -> str:
    for item in _walk_dicts(payload):
        for key in ("email", "account_email", "preferred_username"):
            text = str(item.get(key) or "").strip()
            if "@" in text:
                return text
    return ""


def probe_plus_link_eligibility(
    access_token: str,
    *,
    session: Any | None = None,
    proxy: str = "",
) -> dict[str, Any]:
    """Verify that an access token belongs to an email-bound Free account.

    The check deliberately uses ChatGPT upstream endpoints through the supplied
    session/proxy. JWT claims are only used for expiry and email fallback; plan
    eligibility is based on a lightweight `/backend-api/me` + models check.
    """

    token = str(access_token or "").strip()
    if not token:
        return {
            "ok": False,
            "eligible": False,
            "reason": "missing_token",
            "detail": "账号缺少 access_token",
            "account_type": "unknown",
            "has_email": False,
            "expires_at": 0,
        }

    context = _token_context(token)
    expires_at = int(context.get("expires_at") or 0)
    if expires_at and expires_at <= int(time.time()) + 30:
        return {
            "ok": False,
            "eligible": False,
            "reason": "token_expired",
            "detail": "access_token 已过期",
            "account_type": _normalize_plan(context.get("plan_type")) or "unknown",
            "has_email": bool(str(context.get("email") or "").strip()),
            "expires_at": expires_at,
        }

    upstream = session or _session(proxy)
    me_status, me_data, me_text = _safe_backend_get(upstream, token, "/backend-api/me", timeout=25)
    if me_status in {401, 403}:
        return {
            "ok": False,
            "eligible": False,
            "reason": "token_invalid",
            "detail": f"access_token 上游验证失败: HTTP {me_status}",
            "account_type": "unknown",
            "has_email": bool(str(context.get("email") or "").strip()),
            "expires_at": expires_at,
            "me_status_code": me_status,
        }

    # `/backend-api/me` can be temporarily unavailable while the models endpoint
    # remains healthy. Continue with the independent models check so a transient
    # ChatGPT account-service circuit breaker does not kill the entire task.
    models_status, models_data, models_text = _safe_backend_get(
        upstream,
        token,
        "/backend-api/models",
        timeout=25,
    )
    if models_status in {401, 403}:
        return {
            "ok": False,
            "eligible": False,
            "reason": "token_invalid",
            "detail": f"access_token 模型权限验证失败: HTTP {models_status}",
            "account_type": "unknown",
            "has_email": bool(str(context.get("email") or _extract_email_from_payload(me_data)).strip()),
            "expires_at": expires_at,
            "me_status_code": me_status,
            "models_status_code": models_status,
        }

    model_checked = models_status == 200
    if me_status != 200 and not model_checked:
        return {
            "ok": False,
            "eligible": False,
            "reason": "upstream_unavailable",
            "detail": (
                f"账号资格检测无法访问 ChatGPT: me HTTP {me_status or 0} "
                f"{_compact_error(me_text, 120)}; models HTTP {models_status or 0} "
                f"{_compact_error(models_text, 120)}"
            ),
            "account_type": "unknown",
            "has_email": bool(str(context.get("email") or "").strip()),
            "expires_at": expires_at,
            "me_status_code": me_status,
            "models_status_code": models_status,
            "type_ok_count": 0,
            "model_checked": False,
        }

    model_summary = _extract_model_summary(models_data) if model_checked else {}
    model_plan = _infer_plan_from_models(model_summary) if model_checked else ""
    explicit_plan = _normalize_plan(_extract_plan(me_data) or context.get("plan_type"))
    plan = model_plan or explicit_plan or ("free" if model_checked else "")
    if not plan:
        return {
            "ok": False,
            "eligible": False,
            "reason": "account_type_unknown",
            "detail": f"账号类型检测未得到可靠结果: models HTTP {models_status or 0} {_compact_error(models_text, 160)}",
            "account_type": "unknown",
            "has_email": bool(str(context.get("email") or _extract_email_from_payload(me_data)).strip()),
            "expires_at": expires_at,
            "me_status_code": me_status,
            "models_status_code": models_status,
            "type_ok_count": int(me_status == 200) + int(model_checked),
            "model_checked": model_checked,
            "degraded_validation": me_status != 200 or models_status != 200,
        }

    has_email = bool(str(context.get("email") or _extract_email_from_payload(me_data)).strip())
    eligible = plan == "free" and has_email
    if plan != "free":
        reason = "account_not_free"
        detail = f"当前账号类型为 {plan}，仅 Free 账号可执行 Plus 提链"
    elif not has_email:
        reason = "email_missing"
        detail = "模式要求账号已绑定邮箱"
    else:
        reason = "eligible"
        detail = "账号提链资格检测通过"
    return {
        "ok": True,
        "eligible": eligible,
        "reason": reason,
        "detail": detail,
        "account_type": plan,
        "has_email": has_email,
        "expires_at": expires_at,
        "me_status_code": me_status,
        "models_status_code": models_status,
        "available_model_count": int(model_summary.get("available_model_count") or 0),
        "type_ok_count": int(me_status == 200) + int(model_checked),
        "model_checked": model_checked,
        "degraded_validation": me_status != 200 or models_status != 200,
    }


def _looks_banned(status_code: int, body: str) -> bool:
    lowered = str(body or "").lower()
    return any(marker in lowered for marker in ["deactivated", "suspended", "disabled", "banned", "account_closed", "account deactivated", "封禁"])


HEALTH_STATUS_LABELS: dict[str, str] = {
    "ok": "存活",
    "missing_token": "缺少 Token",
    "missing_sub2api_id": "缺少 sub2api ID",
    "browser_check_required": "需浏览器测活",
    "missing_registration_proxy": "缺少注册节点",
    "token_expired": "Token 过期",
    "token_invalid": "Token 无效",
    "auth_invalid": "鉴权失败",
    "refresh_token_missing": "缺少 RT",
    "refresh_token_expired": "RT 过期/需重登",
    "refresh_token_invalid": "RT 失效/需重登",
    "refresh_token_refresh_failed": "RT 刷新失败",
    "web_relogin_failed": "重登失败",
    "relogin_missing_password": "缺密码/邮箱配置",
    "banned": "封禁/停用",
    "invalid": "无效账号",
    "disabled": "已禁用",
    "cloudflare_blocked": "Cloudflare 拦截",
    "region_blocked": "地区受限",
    "rate_limited": "频率限制",
    "proxy_error": "代理错误",
    "timeout": "请求超时",
    "network_error": "网络错误",
    "upstream_error": "上游错误",
    "blocked": "请求受阻",
    "quota_failed": "额度失败",
    "quota_unknown": "额度未知",
    "failed": "失败",
    "unknown": "未知",
    "skipped": "跳过",
}


def _compact_error(value: Any, limit: int = 800) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(redact_value(value), ensure_ascii=False, default=str)
    else:
        text = redact_text(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return text[:limit]


def _classify_failure(status_code: int = 0, body: Any = "", *, default: str = "failed") -> str:
    lowered = _compact_error(body, 2000).lower()
    if any(marker in lowered for marker in ["refresh_token", "refresh token", "刷新 oauth token", "刷新响应缺少 access_token"]):
        if any(marker in lowered for marker in ["missing", "缺少", "no refresh"]):
            return "refresh_token_missing"
        if any(marker in lowered for marker in ["expired", "unauthorized or expired", "过期"]):
            return "refresh_token_expired"
        if any(marker in lowered for marker in ["invalid_grant", "invalid", "unauthorized", "denied", "revoked", "reuse", "rotation"]):
            return "refresh_token_invalid"
        if any(marker in lowered for marker in ["curl:", "failed to connect", "could not connect", "connection reset", "tls", "ssl", "dns", "timeout", "timed out", "network"]):
            return "network_error"
        return "refresh_token_refresh_failed"
    if any(marker in lowered for marker in ["missing_token", "missing access_token", "missing access token", "缺少 access_token", "缺少 token", "no access_token"]):
        return "missing_token"
    if any(marker in lowered for marker in ["sub2api_account_id", "没有 sub2api", "缺少 sub2api"]):
        return "missing_sub2api_id"
    if _looks_banned(status_code, lowered):
        return "banned"
    if any(marker in lowered for marker in ["just a moment", "cf-ray", "cloudflare", "turnstile", "challenge-platform"]):
        return "cloudflare_blocked"
    if status_code == 429 or any(marker in lowered for marker in ["rate_limit", "rate limit", "too many requests", "限流", "频率"]):
        return "rate_limited"
    if any(marker in lowered for marker in ["unsupported_country", "not available in your country", "country not supported", "region", "地区", "国家"]):
        return "region_blocked"
    if any(marker in lowered for marker in ["expired", "token has expired", "jwt expired", "过期"]):
        return "token_expired"
    if status_code == 401 or any(marker in lowered for marker in ["invalid token", "invalid access token", "unauthorized", "invalid_grant", "invalid_request", "鉴权"]):
        return "token_invalid" if "token" in lowered or status_code == 401 else "auth_invalid"
    if any(marker in lowered for marker in ["proxy", "socks", "tunnel", "connect proxy", "proxy connect"]):
        return "proxy_error"
    if any(marker in lowered for marker in ["curl: (28)", "timed out", "timeout", "read timed out", "请求超时"]):
        return "timeout"
    if any(marker in lowered for marker in ["curl:", "failed to connect", "could not connect", "connection reset", "tls", "ssl", "dns", "name resolution", "network"]):
        return "network_error"
    if status_code == 403:
        return "blocked"
    if status_code >= 500:
        return "upstream_error"
    return default or "failed"


def _status_label(status: Any) -> str:
    key = str(status or "unknown").strip().lower()
    return HEALTH_STATUS_LABELS.get(key, key or "未知")


def _retryable_proxy_result(result: dict[str, Any]) -> bool:
    quota = result.get("quota") if isinstance(result.get("quota"), dict) else {}
    statuses = {
        str(result.get("status") or "").strip().lower(),
        str(quota.get("quota_error_status") or "").strip().lower(),
    }
    return bool(statuses & {"network_error", "proxy_error", "timeout", "cloudflare_blocked", "region_blocked", "blocked"})


def _quota_failure_updates(error: Any, status: str = "quota_failed") -> dict[str, Any]:
    return {
        "quota_status": "failed",
        "quota_checked_at": _now(),
        "last_quota_error": _compact_error(error, 800),
        "quota_error_status": status,
    }


def _local_health_and_quota(account: dict[str, Any], *, refresh_quota: bool = True, proxy: str = "", force_refresh_token: bool = False) -> dict[str, Any]:
    started = time.perf_counter()
    access_token = str(account.get("access_token") or "").strip()
    refresh_token = str(account.get("refresh_token") or "").strip()
    id_token = str(account.get("id_token") or "").strip()
    token_updates: dict[str, Any] = {}
    ctx = _token_context(access_token, id_token)
    refresh_failed_status = ""
    refresh_failed_error = ""
    if force_refresh_token and not refresh_token:
        refresh_failed_status = "refresh_token_missing"
        refresh_failed_error = "缺少 refresh_token，无法刷新账号类型；需要重新登录或重新导入有效 OAuth"
        token_updates.update({"oauth_status": "missing_refresh_token", "oauth_refresh_status": refresh_failed_status, "oauth_refresh_error": refresh_failed_error, "token_refresh_required": True})
    elif refresh_token and (force_refresh_token or not access_token or int(ctx.get("expires_in") or 0) < 300):
        try:
            refreshed = _refresh_platform_tokens(refresh_token, client_id=str(account.get("client_id") or ""), proxy=proxy)
            access_token = str(refreshed.get("access_token") or access_token)
            id_token = str(refreshed.get("id_token") or id_token)
            token_updates.update({"access_token": access_token, "refresh_token": str(refreshed.get("refresh_token") or refresh_token), "id_token": id_token, "client_id": str(refreshed.get("client_id") or account.get("client_id") or ""), "oauth_refreshed_at": refreshed.get("refreshed_at"), "oauth_status": "refreshed_for_inventory", "oauth_refresh_status": "ok", "oauth_refresh_error": "", "token_refresh_required": False})
            ctx = _token_context(access_token, id_token)
        except Exception as error:
            refresh_failed_error = _compact_error(error, 500)
            refresh_failed_status = _classify_failure(0, f"refresh_token {refresh_failed_error}", default="refresh_token_refresh_failed")
            if not access_token:
                updates = {"health_status": refresh_failed_status, "health_error": refresh_failed_error, "health_checked_at": _now(), "health_method": "local", **token_updates}
                updates.update({"oauth_status": "refresh_failed", "oauth_refresh_status": refresh_failed_status, "oauth_refresh_error": refresh_failed_error, "token_refresh_required": True})
                if refresh_quota:
                    updates.update(_quota_failure_updates(refresh_failed_error, refresh_failed_status))
                return {"ok": False, "quota_ok": False, "method": "local", "status": refresh_failed_status, "status_label": _status_label(refresh_failed_status), "status_code": 0, "error": refresh_failed_error, "oauth_status": updates.get("oauth_status"), "oauth_refresh_status": updates.get("oauth_refresh_status"), "oauth_refresh_error": updates.get("oauth_refresh_error"), "updates": updates}
            token_updates.update({"oauth_status": "refresh_failed_used_existing", "oauth_refresh_status": refresh_failed_status, "oauth_refresh_error": refresh_failed_error, "token_refresh_required": True})
    if not access_token:
        missing_status = refresh_failed_status or "missing_token"
        missing_error = refresh_failed_error or "缺少 access_token"
        updates = {**token_updates, "health_status": missing_status, "health_error": missing_error, "health_checked_at": _now(), "health_method": "local"}
        if refresh_quota:
            updates.update(_quota_failure_updates(missing_error, missing_status))
        return {"ok": False, "quota_ok": False, "method": "local", "status": missing_status, "status_label": _status_label(missing_status), "status_code": 0, "error": missing_error, "oauth_status": updates.get("oauth_status"), "oauth_refresh_status": updates.get("oauth_refresh_status"), "oauth_refresh_error": updates.get("oauth_refresh_error"), "updates": updates}

    session = _session(proxy)
    me_resp = session.get(f"{CHATGPT_BASE}/backend-api/me", headers=_headers(access_token), timeout=45, verify=proxy_settings.verify_tls())
    me_text = str(getattr(me_resp, "text", "") or "")
    me_data = _response_json(me_resp)
    status_code = int(getattr(me_resp, "status_code", 0) or 0)
    initial_access_status_code = status_code
    nextauth_session_ok: bool | None = None
    nextauth_session_status = 0
    nextauth_session_error = ""
    session_token = str(account.get("session_token") or "").strip()
    if status_code != 200 and session_token:
        try:
            session_resp = session.get(
                CHATGPT_SESSION_URL,
                headers={"Accept": "application/json", "Referer": f"{CHATGPT_BASE}/"},
                cookies={
                    "__Secure-next-auth.session-token": session_token,
                    "next-auth.session-token": session_token,
                },
                timeout=45,
                verify=proxy_settings.verify_tls(),
            )
            nextauth_session_status = int(getattr(session_resp, "status_code", 0) or 0)
            session_data = _response_json(session_resp)
            session_access_token = str(session_data.get("accessToken") or session_data.get("access_token") or "").strip()
            if nextauth_session_status == 200 and session_access_token:
                nextauth_session_ok = True
                access_token = session_access_token
                token_updates.update({
                    "access_token": access_token,
                    "oauth_status": "chatgpt_nextauth_session_refreshed",
                    "token_refresh_required": False,
                })
                ctx = _token_context(access_token, id_token)
                retry_resp = session.get(
                    f"{CHATGPT_BASE}/backend-api/me",
                    headers=_headers(access_token),
                    timeout=45,
                    verify=proxy_settings.verify_tls(),
                )
                me_resp = retry_resp
                me_text = str(getattr(retry_resp, "text", "") or "")
                me_data = _response_json(retry_resp)
                status_code = int(getattr(retry_resp, "status_code", 0) or 0)
            elif nextauth_session_status in {401, 404} or (nextauth_session_status == 200 and not session_access_token):
                nextauth_session_ok = False
            else:
                nextauth_session_error = _compact_error(session_data, 500)
        except Exception as error:
            nextauth_session_error = _compact_error(error, 500)
    updates: dict[str, Any] = {**token_updates}
    health_via_nextauth = nextauth_session_ok is True
    effective_health_code = 200 if health_via_nextauth else status_code
    updates.update({"health_checked_at": _now(), "health_method": "nextauth_session" if health_via_nextauth else "local", "health_code": effective_health_code, "health_latency_ms": int((time.perf_counter() - started) * 1000)})
    preserve_existing_plan = bool(force_refresh_token and refresh_failed_status)
    base_plan = _normalize_plan((account.get("plan_type") or account.get("account_type")) if preserve_existing_plan else (ctx.get("plan_type") or account.get("plan_type") or account.get("account_type")))
    updates.update({key: value for key, value in {"chatgpt_account_id": ctx.get("chatgpt_account_id"), "chatgpt_user_id": ctx.get("chatgpt_user_id"), "client_id": ctx.get("client_id") or account.get("client_id"), "token_expires_at": ctx.get("expires_at"), "token_expires_in": ctx.get("expires_in")}.items() if value not in (None, "")})
    if base_plan:
        updates.update(_plan_updates(base_plan, account, allow_downgrade=not force_refresh_token and not preserve_existing_plan))

    if status_code == 200 or health_via_nextauth:
        updates["health_status"] = "ok"
        updates["status"] = "active"
        models_summary: dict[str, Any] = {}
        type_refresh_summary: dict[str, Any] = {}
        probe_plan = ""
        if force_refresh_token:
            type_refresh_summary = _probe_account_type(session, access_token)
            probe_plan = _normalize_plan(type_refresh_summary.get("plan_type"))
        if force_refresh_token:
            # 类型刷新只使用本次探测结果；不要在探测不明确时回退到旧 plan，
            # 否则一次误判成 plus 后会一直保留。
            if _account_has_k12_marker(account) and probe_plan in {"plus", "free"}:
                detected_plan = _normalize_plan(account.get("plan_type")) or "free"
            else:
                detected_plan = probe_plan
        else:
            detected_plan = _extract_plan(me_data) or ctx.get("plan_type") or account.get("plan_type") or account.get("account_type") or "free"
        if detected_plan:
            previous_plan = _normalize_plan(account.get("plan_type") or account.get("account_type"))
            updates.update(_plan_updates(detected_plan, account, allow_downgrade=bool(force_refresh_token and probe_plan) or (not force_refresh_token and not preserve_existing_plan)))
            updates["plan_refreshed_at"] = _now()
            updates["plan_refresh_source"] = "chatgpt-type-refresh" if force_refresh_token else "chatgpt-health"
            if previous_plan and previous_plan != _normalize_plan(updates.get("plan_type")):
                updates["plan_refresh_previous"] = previous_plan
            if force_refresh_token and probe_plan:
                updates["plan_refresh_note"] = "detected_by_model_access" if type_refresh_summary.get("model_checked") else "detected_by_account_metadata"
        if refresh_quota:
            model_status_code, model_data, model_text = _safe_backend_get(session, access_token, "/backend-api/models")
            models_summary = _extract_model_summary(model_data)
            model_inferred_plan = _infer_plan_from_models(models_summary)
            usage_status_code, usage_data, usage_text = _safe_backend_get(session, access_token, "/backend-api/wham/usage")
            usage_plan = _normalize_plan(usage_data.get("plan_type")) if usage_status_code == 200 else ""
            if model_status_code == 200:
                if _account_has_k12_marker(account) and model_inferred_plan in {"plus", "free"}:
                    plan = _normalize_plan(account.get("plan_type")) or "free"
                else:
                    plan = model_inferred_plan or usage_plan or _extract_plan(me_data) or ctx.get("plan_type") or "free"
            else:
                plan = usage_plan or probe_plan or _extract_plan(me_data, model_data) or ctx.get("plan_type") or account.get("plan_type") or account.get("account_type") or "free"
            updates["quota_checked_at"] = _now()
            updates["quota_source"] = "chatgpt-wham" if usage_status_code == 200 else "chatgpt-backend"
            updates.update(_plan_updates(plan, account, allow_downgrade=(model_status_code == 200) or (not force_refresh_token and not preserve_existing_plan)))
            if model_inferred_plan:
                updates["plan_refreshed_at"] = _now()
                updates["plan_refresh_source"] = "chatgpt-model-access"
                updates["plan_refresh_note"] = f"model_access_inferred_{model_inferred_plan}"
            if usage_status_code == 200:
                quota_result = _summarize_wham_usage(usage_data)
                if quota_result.get("quota_status") == "unknown":
                    quota_result = _summarize_quota(me_data, model_data, usage_data)
                updates.update({k: v for k, v in quota_result.items() if v not in (None, "")})
            else:
                quota_status = _classify_failure(usage_status_code, usage_text or usage_data, default="quota_failed")
                updates.update(_quota_failure_updates(usage_text or usage_data, quota_status))
            details = updates.get("quota_details") if isinstance(updates.get("quota_details"), dict) else {}
            details["models"] = models_summary
            details["me_status_code"] = status_code
            details["models_status_code"] = model_status_code
            details["models_error"] = _compact_error(model_text, 240) if model_status_code != 200 else ""
            details["usage_status_code"] = usage_status_code
            if type_refresh_summary:
                details["type_refresh"] = type_refresh_summary
            updates["quota_details"] = details
        elif type_refresh_summary:
            updates["quota_details"] = {**(updates.get("quota_details") if isinstance(updates.get("quota_details"), dict) else {}), "type_refresh": type_refresh_summary, "me_status_code": status_code}
        quota_status_value = str(updates.get("quota_status") or "unknown").lower()
        quota_ok = (not refresh_quota) or quota_status_value in {"known", "ok"}
        if refresh_quota and quota_status_value == "unknown":
            updates["last_quota_error"] = "接口返回成功，但未解析到额度字段"
            updates["quota_error_status"] = "quota_unknown"
        result = {"ok": True, "quota_ok": quota_ok, "type_refresh_ok": bool(force_refresh_token and updates.get("plan_type")), "method": "local", "status": "ok", "status_label": _status_label("ok"), "status_code": effective_health_code, "access_token_status_code": initial_access_status_code, "nextauth_session_ok": nextauth_session_ok, "nextauth_session_status": nextauth_session_status, "latency_ms": updates["health_latency_ms"], "plan_type": updates.get("plan_type"), "account_type": updates.get("account_type"), "plan_refresh_source": updates.get("plan_refresh_source"), "oauth_status": updates.get("oauth_status"), "oauth_refresh_status": updates.get("oauth_refresh_status"), "oauth_refresh_error": updates.get("oauth_refresh_error"), "quota": {key: updates.get(key) for key in ("quota_status", "quota_used", "quota_total", "quota_remaining", "quota_reset_at", "last_quota_error", "quota_error_status") if key in updates}, "models": models_summary, "type_refresh": type_refresh_summary, "updates": updates}
        updates["last_health_result"] = {k: v for k, v in result.items() if k != "updates"}
        return result

    error_text = json.dumps(me_data, ensure_ascii=False)
    if session_token and nextauth_session_ok is None:
        health_status = _classify_failure(
            nextauth_session_status,
            nextauth_session_error,
            default="blocked" if nextauth_session_status == 403 else "network_error" if nextauth_session_status == 0 else "failed",
        )
    else:
        health_status = _classify_failure(status_code, me_text or error_text)
    status = "banned" if health_status == "banned" else "invalid" if health_status in {"invalid", "token_invalid", "token_expired", "auth_invalid"} else str(account.get("status") or "active")
    updates.update({"health_status": health_status, "status": status, "health_error": _compact_error(error_text, 800)})
    if refresh_quota:
        updates.update(_quota_failure_updates(updates["health_error"], health_status))
    result = {"ok": False, "quota_ok": False, "method": "local", "status": health_status, "status_label": _status_label(health_status), "status_code": status_code, "access_token_status_code": initial_access_status_code, "nextauth_session_ok": nextauth_session_ok, "nextauth_session_status": nextauth_session_status, "latency_ms": updates["health_latency_ms"], "error": updates["health_error"], "oauth_status": updates.get("oauth_status"), "oauth_refresh_status": updates.get("oauth_refresh_status"), "oauth_refresh_error": updates.get("oauth_refresh_error"), "updates": updates}
    updates["last_health_result"] = {k: v for k, v in result.items() if k != "updates"}
    return result


def _grok_refresh_tokens(account: dict[str, Any], *, proxy: str = "") -> dict[str, Any]:
    refresh_token = str(account.get("refresh_token") or "").strip()
    if not refresh_token:
        raise RuntimeError("Grok refresh_token missing")
    endpoint = str(account.get("token_endpoint") or GROK_TOKEN_ENDPOINT).strip()
    client_id = str(account.get("client_id") or GROK_CLI_CLIENT_ID).strip()
    session = _session(proxy)
    response = session.post(
        endpoint,
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": refresh_token},
        headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
        timeout=45,
        verify=proxy_settings.verify_tls(),
    )
    data = _response_json(response)
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code != 200:
        detail = _compact_error(data or getattr(response, "text", ""), 500)
        raise RuntimeError(f"Grok refresh_token failed: HTTP {status_code}{(': ' + detail) if detail else ''}")
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Grok refresh_token response missing access_token")
    expires_in = max(0, int(data.get("expires_in") or 21600))
    return {
        "access_token": access_token,
        "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
        "id_token": str(data.get("id_token") or account.get("id_token") or "").strip(),
        "token_type": str(data.get("token_type") or account.get("token_type") or "Bearer").strip(),
        "client_id": client_id,
        "token_endpoint": endpoint,
        "token_expires_in": expires_in,
        "token_expires_at": int(time.time()) + expires_in if expires_in else 0,
        "oauth_refreshed_at": _now(),
    }


def _grok_health_and_quota(account: dict[str, Any], *, refresh_quota: bool = True, proxy: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    access_token = str(account.get("access_token") or "").strip()
    refresh_token = str(account.get("refresh_token") or "").strip()
    sso_token = str(account.get("sso_token") or account.get("sso") or "").strip()
    token_updates: dict[str, Any] = {}
    refresh_error = ""
    claims = _jwt_payload(access_token)
    try:
        expires_at = int(account.get("token_expires_at") or claims.get("exp") or 0)
    except Exception:
        expires_at = 0
    token_expiring = bool(expires_at and expires_at <= int(time.time()) + 300)

    if refresh_token and (not access_token or token_expiring):
        try:
            refreshed = _grok_refresh_tokens(account, proxy=proxy)
            access_token = str(refreshed.get("access_token") or "").strip()
            token_expiring = False
            token_updates.update(refreshed)
            token_updates.update({
                "oauth_status": "ready",
                "oauth_refresh_status": "ok",
                "oauth_refresh_error": "",
                "token_refresh_required": False,
            })
        except Exception as error:
            refresh_error = _compact_error(error, 500)
            token_updates.update({
                "oauth_status": "refresh_failed",
                "oauth_refresh_status": _classify_failure(0, f"refresh_token {refresh_error}", default="refresh_token_refresh_failed"),
                "oauth_refresh_error": refresh_error,
                "token_refresh_required": True,
            })

    method = "grok_oauth" if access_token and not token_expiring else "grok_sso"
    status_code = 0
    response_data: dict[str, Any] = {}
    response_text = ""
    final_url = ""

    if method == "grok_oauth":
        session = _session(proxy)
        headers = {str(key): str(value) for key, value in GROK_CLI_HEADERS.items()}
        account_headers = account.get("headers") if isinstance(account.get("headers"), dict) else {}
        headers.update({str(key): str(value) for key, value in account_headers.items() if str(key).strip() and value is not None})
        headers.update({"accept": "application/json", "authorization": f"Bearer {access_token}"})
        base_url = str(account.get("base_url") or GROK_CLI_BASE_URL).strip().rstrip("/")
        response = session.get(
            f"{base_url}/models",
            headers=headers,
            timeout=45,
            verify=proxy_settings.verify_tls(),
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        response_text = str(getattr(response, "text", "") or "")
        response_data = _response_json(response)
        final_url = f"{base_url}/models"
        # A stale OAuth token does not mean the xAI account itself is dead. Fall
        # back to the SSO session so SSO-only inventory can still be classified.
        if status_code == 401 and sso_token:
            token_updates.update({
                "oauth_status": "token_invalid",
                "oauth_refresh_status": "token_invalid",
                "oauth_refresh_error": _compact_error(response_data or response_text, 500),
                "token_refresh_required": True,
            })
            method = "grok_sso"

    if method == "grok_sso":
        if not sso_token:
            error_text = refresh_error or "缺少 Grok OAuth access_token 和 xAI SSO"
            health_status = token_updates.get("oauth_refresh_status") or "missing_token"
            updates = {
                **token_updates,
                "health_status": health_status,
                "health_error": error_text,
                "health_checked_at": _now(),
                "health_method": method,
                "health_code": 0,
                "health_latency_ms": int((time.perf_counter() - started) * 1000),
            }
            if refresh_quota:
                updates.update(_quota_failure_updates(error_text, str(health_status)))
            return {
                "ok": False,
                "quota_ok": False,
                "method": method,
                "status": health_status,
                "status_label": _status_label(health_status),
                "status_code": 0,
                "error": error_text,
                "updates": updates,
            }
        session = _session(proxy)
        session.cookies.set("sso", sso_token, domain=".x.ai")
        response = session.get(
            "https://accounts.x.ai/",
            headers={"accept": "text/html,application/xhtml+xml", "user-agent": DEFAULT_PROXY_RUNTIME_USER_AGENT},
            allow_redirects=True,
            timeout=45,
            verify=proxy_settings.verify_tls(),
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        response_text = str(getattr(response, "text", "") or "")
        response_data = _response_json(response)
        final_url = str(getattr(response, "url", "") or "https://accounts.x.ai/")

    login_redirect = method == "grok_sso" and any(marker in final_url.lower() for marker in ("/sign-in", "/sign-up", "/login"))
    ok = 200 <= status_code < 400 and not login_redirect
    latency_ms = int((time.perf_counter() - started) * 1000)
    quota = {"quota_status": "unavailable"} if refresh_quota else {}
    updates: dict[str, Any] = {
        **token_updates,
        "health_checked_at": _now(),
        "health_method": method,
        "health_code": status_code,
        "health_latency_ms": latency_ms,
    }
    if ok:
        models = response_data.get("data") if isinstance(response_data.get("data"), list) else []
        model_ids = [str(item.get("id") or "") for item in models if isinstance(item, dict) and item.get("id")]
        updates.update({
            "health_status": "ok",
            "health_error": "",
            "status": "active",
            "plan_type": "grok",
            "oauth_status": "ready" if method == "grok_oauth" else "sso_only",
        })
        if refresh_quota:
            updates.update({
                "quota_status": "unavailable",
                "quota_source": "grok-health",
                "quota_checked_at": _now(),
                "quota_error_status": "",
                "last_quota_error": "",
            })
        result = {
            "ok": True,
            "quota_ok": False,
            "method": method,
            "status": "ok",
            "status_label": _status_label("ok"),
            "status_code": status_code,
            "latency_ms": latency_ms,
            "oauth_status": updates.get("oauth_status"),
            "quota": quota,
            "models": {"available_model_count": len(model_ids), "model_ids": model_ids[:50]},
            "updates": updates,
        }
        updates["last_health_result"] = {key: value for key, value in result.items() if key != "updates"}
        return result

    error_body: Any = response_data or response_text or final_url or refresh_error
    health_status = "token_invalid" if login_redirect else _classify_failure(status_code, error_body)
    error_text = "xAI SSO 已跳转到登录页" if login_redirect else _compact_error(error_body, 800)
    account_status = "invalid" if health_status in {"token_invalid", "token_expired", "auth_invalid"} else str(account.get("status") or "active")
    updates.update({"health_status": health_status, "health_error": error_text, "status": account_status})
    if refresh_quota:
        updates.update(_quota_failure_updates(error_text, health_status))
    result = {
        "ok": False,
        "quota_ok": False,
        "method": method,
        "status": health_status,
        "status_label": _status_label(health_status),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "error": error_text,
        "updates": updates,
    }
    updates["last_health_result"] = {key: value for key, value in result.items() if key != "updates"}
    return result


def _sub2api_health(account: dict[str, Any], *, model_id: str = "", prompt: str = "hi", mode: str = "") -> dict[str, Any]:
    raw_id = account.get("sub2api_account_id") or account.get("account_id")
    if not raw_id:
        raise RuntimeError("账号没有 sub2api_account_id")
    started = time.perf_counter()
    test = sub2api_store.test_sub2api_account(raw_id, model_id=model_id, prompt=prompt or "hi", mode=mode or "")
    ok = bool(test.get("ok"))
    status_code = int(test.get("status_code") or 0)
    body_preview = str(test.get("body_preview") or test.get("error") or "")
    health_status = "ok" if ok else _classify_failure(status_code, body_preview)
    account_status = "active" if ok else ("banned" if health_status == "banned" else "invalid" if health_status in {"token_invalid", "token_expired", "auth_invalid"} else str(account.get("status") or "active"))
    updates = {"health_status": health_status, "health_checked_at": test.get("tested_at") or _now(), "health_method": "sub2api", "health_code": status_code, "health_latency_ms": int((time.perf_counter() - started) * 1000), "health_error": "" if ok else _compact_error(body_preview, 800), "sub2api_health_status": health_status, "sub2api_health_checked_at": test.get("tested_at") or _now(), "sub2api_health_code": status_code, "status": account_status}
    result = {"ok": ok, "method": "sub2api", "status": health_status, "status_label": _status_label(health_status), "status_code": status_code, "account_id": raw_id, "latency_ms": updates["health_latency_ms"], "response": {k: v for k, v in test.items() if k != "body_preview"}, "updates": updates}
    updates["last_health_result"] = {k: v for k, v in result.items() if k != "updates"}
    return result


class AccountInventoryService:
    def __init__(self, state_file: Path, settings_file: Path):
        self._state_file = state_file
        self._settings_file = settings_file
        self._lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._runner: threading.Thread | None = None
        self._scheduler: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._bulk_active = False
        self._last_progress_save_at = 0.0
        self._state = self._load_state()
        if self._state.get("enabled") or int((self._state.get("stats") or {}).get("running") or 0):
            self._state["enabled"] = False
            self._state.setdefault("stats", {})["running"] = 0
            self._state.setdefault("stats", {})["finished_at"] = self._state.setdefault("stats", {}).get("finished_at") or _now()
            self._save_state()
        self._settings = self._load_settings()

    def _load_state(self) -> dict[str, Any]:
        data = read_json(self._state_file, {}, expected_schema_version=1)
        state = _default_state()
        if isinstance(data, dict):
            state.update({k: v for k, v in data.items() if k in state})
            state["stats"] = {**_default_state()["stats"], **(data.get("stats") if isinstance(data.get("stats"), dict) else {})}
            state["logs"] = data.get("logs") if isinstance(data.get("logs"), list) else []
            state["results"] = data.get("results") if isinstance(data.get("results"), list) else []
        return state

    def _load_settings(self) -> dict[str, Any]:
        data = read_json(self._settings_file, {}, expected_schema_version=1)
        return _normalize_settings(data if isinstance(data, dict) else {})

    def _trim_state_buffers(self) -> None:
        logs = self._state.get("logs")
        if isinstance(logs, list) and len(logs) > MAX_TASK_LOGS:
            self._state["logs"] = logs[-MAX_TASK_LOGS:]
        results = self._state.get("results")
        if isinstance(results, list) and len(results) > MAX_TASK_RESULTS:
            self._state["results"] = results[-MAX_TASK_RESULTS:]

    def _save_state(self) -> None:
        self._trim_state_buffers()
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._state_file, self._state, indent=None, default=str, schema_version=1)

    def _save_progress_state(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_progress_save_at >= 0.75:
            self._save_state()
            self._last_progress_save_at = now

    def _save_settings(self) -> None:
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._settings_file, self._settings, default=str, schema_version=1)

    def _append_log(self, text: str, level: str = "info") -> None:
        with self._lock:
            self._state.setdefault("logs", []).append({"time": _now(), "text": str(text), "level": str(level or "info")})
            self._state.setdefault("stats", {})["updated_at"] = _now()
            if self._bulk_active:
                self._save_progress_state()
            else:
                self._save_state()

    def _bump(self, **updates: Any) -> None:
        with self._lock:
            stats = self._state.setdefault("stats", {})
            stats.update(updates)
            stats["updated_at"] = _now()
            if self._bulk_active:
                self._save_progress_state()
            else:
                self._save_state()

    def _ensure_scheduler(self) -> None:
        with self._lock:
            if not self._settings.get("scheduled_enabled") or (self._scheduler and self._scheduler.is_alive()):
                return
            self._scheduler = threading.Thread(target=self._schedule_loop, daemon=True, name="account-inventory-scheduler")
            self._scheduler.start()

    def get(self, *, include_pool: bool = True) -> dict[str, Any]:
        self._ensure_scheduler()
        with self._lock:
            if include_pool:
                snap = _json_clone(self._state)
            else:
                # /api/accounts/health is polled by the frontend. Large batches can
                # leave thousands of result rows in state, so keep this endpoint
                # lightweight and avoid sending multi-MB payloads every few seconds.
                snap = _json_clone({k: v for k, v in self._state.items() if k not in {"logs", "results"}})
                snap["logs"] = _json_clone((self._state.get("logs") or [])[-300:])
                snap["results"] = _json_clone((self._state.get("results") or [])[-300:])
            runner_alive = bool(self._runner and self._runner.is_alive())
            scheduler_alive = bool(self._scheduler and self._scheduler.is_alive())
            settings = _json_clone(self._settings)
        snap["enabled"] = bool(snap.get("enabled")) and runner_alive
        if not runner_alive and isinstance(snap.get("stats"), dict):
            snap["stats"]["running"] = 0
        snap["schedule_running"] = scheduler_alive and bool(settings.get("scheduled_enabled"))
        snap["job_running"] = self._job_lock.locked()
        snap["settings"] = settings
        if include_pool:
            snap["account_pool"] = account_pool()
        return snap

    def update_settings(self, updates: dict[str, Any], *, include_pool: bool = True) -> dict[str, Any]:
        with self._lock:
            self._settings = _normalize_settings({**self._settings, **(updates or {}), "updated_at": _now()})
            self._save_settings()
        if self._settings.get("scheduled_enabled"):
            self._ensure_scheduler()
        return self.get(include_pool=include_pool)

    def forget_account(self, selector: str) -> dict[str, Any]:
        needle = str(selector or "").strip().lower()
        if not needle:
            return self.get(include_pool=False)

        def matches(value: Any) -> bool:
            try:
                return needle in json.dumps(value, ensure_ascii=False, default=str).lower()
            except (TypeError, ValueError):
                return needle in str(value).lower()

        with self._lock:
            self._state["logs"] = [item for item in self._state.get("logs") or [] if not matches(item)]
            self._state["results"] = [item for item in self._state.get("results") or [] if not matches(item)]
            if matches(self._state.get("current_job")):
                self._state["current_job"] = None
                if not self._state.get("enabled"):
                    self._state["stats"] = _default_state()["stats"]
            self._save_state()
        return self.get(include_pool=False)

    def start_schedule(self, updates: dict[str, Any] | None = None, *, include_pool: bool = True) -> dict[str, Any]:
        payload = dict(updates or {})
        payload["scheduled_enabled"] = True
        state = self.update_settings(payload, include_pool=include_pool)
        self._append_log("定时测活已开启", "green")
        return state

    def stop_schedule(self, *, include_pool: bool = True) -> dict[str, Any]:
        with self._lock:
            self._settings["scheduled_enabled"] = False
            self._settings["updated_at"] = _now()
            self._save_settings()
        self._append_log("定时测活已停止", "yellow")
        return self.get(include_pool=include_pool)

    def start(self, options: dict[str, Any] | None = None, *, include_pool: bool = True) -> dict[str, Any]:
        options = options or {}
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._state["enabled"] = True
                self._save_state()
                state = self.get(include_pool=include_pool)
                state["busy"] = True
                state["message"] = "已有账号库存任务正在运行"
                return state
            if not self._job_lock.acquire(blocking=False):
                state = self.get(include_pool=include_pool)
                state["busy"] = True
                state["message"] = "已有账号库存任务正在运行"
                return state
            self._stop_event.clear()
            self._state = _default_state()
            self._state["enabled"] = True
            self._state["current_job"] = {"job_id": uuid.uuid4().hex, "options": options, "started_at": _now()}
            self._state["stats"].update({"started_at": _now(), "updated_at": _now()})
            self._save_state()
            try:
                self._runner = threading.Thread(target=self._run_once, args=(options, True), daemon=True, name="account-inventory-health")
                self._runner.start()
            except Exception:
                self._job_lock.release()
                raise
        self._append_log("账号测活任务已启动", "green")
        return self.get(include_pool=include_pool)

    def stop(self, *, include_pool: bool = True) -> dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            self._state["enabled"] = False
            self._save_state()
        self._append_log("账号测活任务已请求停止", "yellow")
        return self.get(include_pool=include_pool)

    def check_accounts(self, *, emails: list[str] | None = None, method: str = "auto", drop_banned: bool = False, refresh_quota: bool = True, model_id: str = "", prompt: str = "hi", mode: str = "", task_label: str = "", concurrency: int | None = None) -> dict[str, Any]:
        return self._execute_batch({"emails": emails or [], "method": method or "auto", "drop_banned": drop_banned, "refresh_quota": refresh_quota, "model_id": model_id, "prompt": prompt or "hi", "mode": mode or "", "task_label": task_label, "concurrency": concurrency}, respect_enabled=False)

    def check_account(self, account: dict[str, Any], *, method: str = "auto", refresh_quota: bool = True, model_id: str = "", prompt: str = "hi", mode: str = "", proxy: str = "") -> dict[str, Any]:
        method = str(method or "auto").strip().lower()
        email = str(account.get("email") or "").strip()
        platform = str(account.get("platform") or "openai").strip().lower()
        # Grok registration historically stored only a temporary local Mihomo
        # port. Health checks must use the freshly assigned pool proxy instead.
        effective_proxy = str(proxy or ("" if platform == "grok" else account.get("proxy")) or "").strip()
        result: dict[str, Any] | None = None
        errors: list[str] = []
        if method == "sub2api" and not account.get("sub2api_account_id"):
            status = "missing_sub2api_id"
            result = {"ok": False, "quota_ok": False, "method": "sub2api", "status": status, "status_label": _status_label(status), "status_code": 0, "error": "账号没有 sub2api_account_id", "updates": {"health_status": status, "health_error": "账号没有 sub2api_account_id", "health_checked_at": _now(), "health_method": "sub2api"}}
        if result is None and method in {"auto", "sub2api"} and account.get("sub2api_account_id") and not (method == "auto" and refresh_quota and platform != "grok"):
            try:
                result = _sub2api_health(account, model_id=model_id, prompt=prompt or "hi", mode=mode or "")
            except Exception as error:
                errors.append(f"sub2api: {error}")
                if method == "sub2api":
                    status = _classify_failure(0, str(error))
                    result = {"ok": False, "quota_ok": False, "method": "sub2api", "status": status, "status_label": _status_label(status), "status_code": 0, "error": str(error), "updates": {"health_status": status, "health_error": _compact_error(error, 800), "health_checked_at": _now(), "health_method": "sub2api"}}
        if result is None and method in {"auto", "local"}:
            try:
                force_refresh_token = str(mode or "").strip().lower() in {"refresh_token", "force_refresh_token", "refresh_type"}
                if platform == "grok":
                    result = _grok_health_and_quota(account, refresh_quota=refresh_quota, proxy=effective_proxy)
                else:
                    result = _local_health_and_quota(account, refresh_quota=refresh_quota, proxy=effective_proxy, force_refresh_token=force_refresh_token)
            except Exception as error:
                errors.append(f"local: {error}")
                status = _classify_failure(0, str(error))
                updates = {"health_status": status, "health_error": _compact_error(error, 800), "health_checked_at": _now(), "health_method": "local"}
                if refresh_quota:
                    updates.update(_quota_failure_updates(str(error), status))
                result = {"ok": False, "quota_ok": False, "method": "local", "status": status, "status_label": _status_label(status), "status_code": 0, "error": str(error), "updates": updates}
        if result is None:
            error_text = "; ".join(errors) or "没有可用测活方式"
            status = _classify_failure(0, error_text, default="skipped")
            result = {"ok": False, "quota_ok": False, "method": method, "status": status, "status_label": _status_label(status), "status_code": 0, "error": error_text, "updates": {"health_status": status, "health_error": error_text, "health_checked_at": _now(), "health_method": method}}
        result["email"] = email
        if errors:
            result["fallback_errors"] = errors
        updates = result.get("updates") if isinstance(result.get("updates"), dict) else {}
        if updates:
            update_registered_account(account, updates)
        return {k: v for k, v in result.items() if k != "updates"}

    def _selected_accounts(self, options: dict[str, Any]) -> list[dict[str, Any]]:
        selected = {str(item).strip().lower() for item in (options.get("emails") or []) if str(item).strip()}
        only_stale = bool(options.get("only_stale"))
        stale_after = max(1, int(options.get("stale_after_minutes") or self._settings.get("stale_after_minutes") or 240))
        accounts = list_registered_accounts(include_secret=True)
        if selected:
            accounts = [item for item in accounts if any(account_matches_selector(item, selector) for selector in selected)]
        if only_stale:
            accounts = [item for item in accounts if _is_stale(str(item.get("health_checked_at") or ""), stale_after)]
        return accounts

    def _run_once(self, options: dict[str, Any], owns_job_lock: bool = False) -> None:
        try:
            self._execute_batch(options, respect_enabled=True, owns_job_lock=owns_job_lock)
        finally:
            with self._lock:
                self._state["enabled"] = False
                self._state.setdefault("stats", {})["finished_at"] = _now()
                self._save_state()

    def _busy_result(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": False,
                "busy": True,
                "message": "已有账号库存任务正在运行",
                "current_job": _json_clone(self._state.get("current_job")),
                "stats": _json_clone(self._state.get("stats") or {}),
            }

    def _execute_batch(self, options: dict[str, Any], *, respect_enabled: bool, owns_job_lock: bool = False) -> dict[str, Any]:
        acquired = bool(owns_job_lock)
        if not acquired:
            acquired = self._job_lock.acquire(blocking=False)
        if not acquired:
            return self._busy_result()
        with self._lock:
            previous_bulk_active = self._bulk_active
            self._bulk_active = True
            if not respect_enabled:
                self._state["current_job"] = {"job_id": uuid.uuid4().hex, "options": _json_clone(options), "started_at": _now()}
        try:
            return self._execute_batch_inner(options, respect_enabled=respect_enabled)
        finally:
            with self._lock:
                self._save_progress_state(force=True)
                self._bulk_active = previous_bulk_active
                current_job = self._state.get("current_job") if isinstance(self._state.get("current_job"), dict) else {}
                if current_job:
                    current_job["finished_at"] = _now()
                    self._state["current_job"] = current_job
                    self._save_state()
            self._job_lock.release()

    def _execute_batch_inner(self, options: dict[str, Any], *, respect_enabled: bool) -> dict[str, Any]:
        method = str(options.get("method") or self._settings.get("method") or "auto").lower()
        refresh_quota = bool(options.get("refresh_quota", self._settings.get("refresh_quota_on_health", True)))
        task_label = str(options.get("task_label") or ("同步额度" if refresh_quota and method == "local" else "账号测活")).strip()
        drop_banned = bool(options.get("drop_banned", self._settings.get("auto_drop_banned", False)))
        model_id = str(options.get("model_id") or self._settings.get("model_id") or "")
        prompt = str(options.get("prompt") or self._settings.get("prompt") or "hi")
        mode = str(options.get("mode") or self._settings.get("mode") or "")
        try:
            concurrency = int(options.get("concurrency") or self._settings.get("concurrency") or 10)
        except Exception:
            concurrency = 10
        concurrency = max(1, min(64, concurrency))
        accounts = self._selected_accounts(options)
        with self._lock:
            self._state.setdefault("stats", {}).update({"total": len(accounts), "done": 0, "success": 0, "fail": 0, "running": 0, "started_at": _now(), "updated_at": _now()})
            self._save_state()
        self._append_log(f"{task_label}任务开始：目标账号={len(accounts)}，方式={method}，并发={concurrency}，同步额度={'是' if refresh_quota else '否'}", "green" if accounts else "yellow")
        if not accounts:
            self._append_log(f"{task_label}任务无可处理账号", "yellow")
        results: list[dict[str, Any]] = []
        success = 0
        fail = 0
        banned_accounts: list[dict[str, Any]] = []
        quota_only_task = task_label == "同步额度"
        type_task = "类型" in task_label

        def is_enabled() -> bool:
            if respect_enabled:
                with self._lock:
                    return bool(self._state.get("enabled")) and not self._stop_event.is_set()
            return True

        def run_one(index: int, account: dict[str, Any]) -> dict[str, Any]:
            email = str(account.get("email") or "")
            try:
                result: dict[str, Any] = {}
                assignment: dict[str, Any] = {}
                for attempt in range(1, 4):
                    if respect_enabled and not is_enabled():
                        return {"_cancelled": True, "_account": account, "email": email}
                    platform = str(account.get("platform") or "openai").strip().lower()
                    assignment = proxy_pool_store.assign_proxy(
                        index + (attempt - 1) * max(1, len(accounts)),
                        total_threads=concurrency,
                        slot_index=(index - 1) % concurrency,
                        purpose="inventory_health",
                        account=None if platform == "grok" else account,
                        validate_node=False,
                    )
                    if assignment.get("sticky_failure"):
                        result = {
                            "ok": False,
                            "quota_ok": False,
                            "method": method,
                            "status": "network_error",
                            "status_code": 0,
                            "error": str(assignment.get("error") or "注册代理节点不可用")[:800],
                        }
                    else:
                        result = self.check_account(account, method=method, refresh_quota=refresh_quota, model_id=model_id, prompt=prompt, mode=mode, proxy=str(assignment.get("proxy") or ""))
                    result["proxy_source"] = assignment.get("source") or ""
                    result["proxy_label"] = assignment.get("label") or ""
                    result["proxy_node_id"] = assignment.get("node_id") or ""
                    result["proxy_sticky"] = bool(assignment.get("sticky"))
                    if assignment.get("sticky_failure") or attempt >= 3 or not _retryable_proxy_result(result):
                        break
                    if assignment.get("sticky"):
                        self._append_log(
                            f"{email} {task_label}固定注册节点请求失败，继续使用同一节点重试 {attempt + 1}/3",
                            "yellow",
                        )
                        continue
                    if str(assignment.get("source") or "") != "managed_mihomo":
                        break
                    quota = result.get("quota") if isinstance(result.get("quota"), dict) else {}
                    reason = quota.get("last_quota_error") or result.get("error") or result.get("status") or "proxy request failed"
                    try:
                        proxy_pool_store.report_proxy_failure(assignment, reason)
                    except Exception:
                        pass
                    self._append_log(
                        f"{email} {task_label}代理失败，已隔离节点 {assignment.get('label') or '-'}，切换节点重试 {attempt + 1}/3",
                        "yellow",
                    )
                rt_status = str(result.get("oauth_refresh_status") or "").strip().lower()
                ok = bool(result.get("quota_ok")) if quota_only_task else bool(result.get("ok"))
                if type_task:
                    ok = bool(result.get("type_refresh_ok") or result.get("ok"))
                result["_ok_for_task"] = ok
                if ok:
                    quota = result.get("quota") if isinstance(result.get("quota"), dict) else {}
                    quota_text = ""
                    if refresh_quota:
                        quota_text = f"，额度={quota.get('quota_used', '-')}/{quota.get('quota_total', '-')}" if quota.get("quota_status") in {"known", "ok"} else f"，额度状态={quota.get('quota_status') or 'unknown'}"
                    type_text = f"，类型={result.get('account_type') or result.get('plan_type')}" if "类型" in task_label else ""
                    level = "green" if not refresh_quota or result.get("quota_ok") else "yellow"
                    self._append_log(f"{email} {task_label}成功，方式={result.get('method')}，HTTP={result.get('status_code')}{type_text}{quota_text}", level)
                else:
                    quota = result.get("quota") if isinstance(result.get("quota"), dict) else {}
                    error_text = _compact_error(quota.get("last_quota_error") or result.get("error") or result.get("status"), 240)
                    if type_task and rt_status and rt_status != "ok":
                        status = rt_status
                        error_text = _compact_error(result.get("oauth_refresh_error") or error_text, 240)
                    else:
                        status = quota.get("quota_error_status") if quota_only_task and quota.get("quota_error_status") else result.get("status")
                    self._append_log(f"{email} {task_label}失败：{_status_label(status)}（{status or 'failed'}）{('，' + error_text) if error_text else ''}", "red")
                result["_banned_for_task"] = str(result.get("status") or "").lower() in {"banned", "invalid", "dead", "disabled"}
                result["_account"] = account
                return result
            except Exception as error:
                result = {"email": email, "ok": False, "status": "failed", "error": str(error), "tested_at": _now()}
                update_registered_account(account, {"health_status": "failed", "health_error": str(error), "health_checked_at": _now(), "health_method": method})
                self._append_log(f"{email} {task_label}异常：{error}", "red")
                result["_ok_for_task"] = False
                result["_banned_for_task"] = False
                result["_account"] = account
                return result

        def record_result(result: dict[str, Any]) -> None:
            nonlocal success, fail
            if result.pop("_cancelled", False):
                return
            account = result.pop("_account", None)
            ok_for_task = bool(result.pop("_ok_for_task", False))
            banned_for_task = bool(result.pop("_banned_for_task", False))
            if ok_for_task:
                success += 1
            else:
                fail += 1
            if banned_for_task and isinstance(account, dict):
                banned_accounts.append(account)
            results.append(result)
            with self._lock:
                self._state.setdefault("results", []).append(result)
                remaining = max(0, len(accounts) - len(results))
                running = min(concurrency, remaining) if is_enabled() else 0
                self._state.setdefault("stats", {}).update({"done": len(results), "success": success, "fail": fail, "running": running, "updated_at": _now()})
                self._save_state()

        next_index = 0
        futures: dict[Any, tuple[int, dict[str, Any]]] = {}

        def submit_next(executor: ThreadPoolExecutor) -> bool:
            nonlocal next_index
            if next_index >= len(accounts) or not is_enabled():
                return False
            account = accounts[next_index]
            index = next_index + 1
            next_index += 1
            email = str(account.get("email") or "")
            self._append_log(f"开始{task_label} {email} ({index}/{len(accounts)})")
            future = executor.submit(run_one, index, account)
            futures[future] = (index, account)
            with self._lock:
                self._state.setdefault("stats", {})["running"] = len(futures)
                self._state.setdefault("stats", {})["updated_at"] = _now()
                self._save_state()
            return True

        if accounts:
            with ThreadPoolExecutor(max_workers=min(concurrency, len(accounts)), thread_name_prefix="account-health") as executor:
                while len(futures) < min(concurrency, len(accounts)) and submit_next(executor):
                    pass
                while futures:
                    done, _pending = wait(set(futures.keys()), timeout=1.0, return_when=FIRST_COMPLETED)
                    if not done:
                        if not is_enabled():
                            for future in list(futures):
                                future.cancel()
                            break
                        continue
                    for future in done:
                        futures.pop(future, None)
                        if future.cancelled():
                            continue
                        try:
                            record_result(future.result())
                        except Exception as error:
                            fail += 1
                            result = {"email": "", "ok": False, "status": "failed", "error": str(error), "tested_at": _now()}
                            results.append(result)
                            self._append_log(f"{task_label}线程异常：{error}", "red")
                            self._bump(done=len(results), success=success, fail=fail, running=len(futures))
                        while len(futures) < concurrency and submit_next(executor):
                            pass
                    if not is_enabled():
                        for future in list(futures):
                            future.cancel()
                        break
                if futures and not is_enabled():
                    executor.shutdown(wait=False, cancel_futures=True)
                    futures.clear()

        stopped = bool(respect_enabled and self._stop_event.is_set())
        with self._lock:
            self._state.setdefault("stats", {}).update({"done": len(results), "success": success, "fail": fail, "running": 0, "updated_at": _now()})
            self._save_state()
        removed = 0
        if drop_banned and banned_accounts:
            removed_result = delete_registered_accounts(banned_accounts, reason="drop_banned_after_health_check")
            removed = int(removed_result.get("removed") or 0)
            self._append_log(f"已剔除不可用账号 {removed} 个", "yellow")
        if stopped:
            self._append_log(f"{task_label}任务已停止：已完成={len(results)}/{len(accounts)}，成功={success}，失败={fail}", "yellow")
        else:
            self._append_log(f"{task_label}任务结束：成功={success}，失败={fail}，剔除={removed}", "green" if fail == 0 else "yellow")
        return {"ok": fail == 0 and not stopped, "stopped": stopped, "success": success, "fail": fail, "total": len(accounts), "processed": len(results), "removed": removed, "results": results, "checked_at": _now(), "account_pool": account_pool()}

    def _schedule_loop(self) -> None:
        while True:
            with self._lock:
                settings = dict(self._settings)
                if not settings.get("scheduled_enabled"):
                    self._state["schedule_running"] = False
                    self._save_state()
                    return
                self._state["schedule_running"] = True
                self._save_state()
            try:
                self._append_log("开始定时账号测活", "green")
                self._execute_batch({"method": settings.get("method"), "refresh_quota": settings.get("refresh_quota_on_health"), "drop_banned": settings.get("auto_drop_banned"), "only_stale": True, "stale_after_minutes": settings.get("stale_after_minutes"), "model_id": settings.get("model_id"), "prompt": settings.get("prompt"), "mode": settings.get("mode")}, respect_enabled=False)
                with self._lock:
                    self._state["last_scheduled_run_at"] = _now()
                    self._save_state()
            except Exception as error:
                self._append_log(f"定时测活异常：{error}", "red")
            sleep_seconds = max(60, int(settings.get("interval_minutes") or 60) * 60)
            for _ in range(sleep_seconds):
                time.sleep(1)
                with self._lock:
                    if not self._settings.get("scheduled_enabled"):
                        break

    def prune_banned(self) -> dict[str, Any]:
        result = prune_banned_accounts()
        self._append_log(f"手动剔除不可用账号 {result.get('removed') or 0} 个", "yellow")
        return {"account_pool": result, "inventory": self.get()}


account_inventory_service = AccountInventoryService(ACCOUNT_INVENTORY_TASK_FILE, ACCOUNT_INVENTORY_SETTINGS_FILE)
