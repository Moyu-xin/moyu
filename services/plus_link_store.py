from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from services import partial_account_store, proxy_chain_service, proxy_pool_store
from services.account_inventory_service import probe_plus_link_eligibility
from services.account_token_profile import jwt_payload
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.paypal_country_catalog import SMSBOWER_PAYPAL_COUNTRY_CODES
from services.registered_account_store import account_matches_selector, account_pool, account_pool_page, list_registered_accounts, update_registered_account

try:
    from services.k12_inventory_service import _refresh_platform_tokens
except Exception:  # pragma: no cover - optional runtime helper
    _refresh_platform_tokens = None  # type: ignore[assignment]


PLUS_LINK_FILE = DATA_DIR / "plus_link.json"
PLUS_ENGINE_APP = Path(__file__).resolve().parent / "plus_engine" / "app.py"
PLUS_ENGINE_DATA_DIR = DATA_DIR / "plus_engine"
CHECKOUT_BILLING_COUNTRY_CODES = {
    "US", "IN", "EG", "ID", "TH", "MX", "CO", "ZA", "DE", "FR", "IT", "ES", "NL",
    "DK", "CA", "AU", "NZ", "KR", "AE", "IL", "RO", "GB", "BR", "JP", "PH", "TR", "VN",
}
BILLING_COUNTRY_CODES = set(SMSBOWER_PAYPAL_COUNTRY_CODES)
PROMOTION_COUNTRY_CODES = {"JP", "TH", "TR", "BR", "VN"}
CHECKOUT_COUNTRY_CODES = set(CHECKOUT_BILLING_COUNTRY_CODES) - {"PH"}
PROVIDER_COUNTRY_CODES = set(BILLING_COUNTRY_CODES)
APPROVE_COUNTRY_CODES = set(CHECKOUT_COUNTRY_CODES)
RESIDENTIAL_PROXY_COUNTRY_CODES = BILLING_COUNTRY_CODES | PROMOTION_COUNTRY_CODES
DEFAULT_BILLING_COUNTRY_ROTATION = ["US", "DE", "FR", "IT", "ES", "NL", "CA", "AU", "GB"]
DEFAULT_PROXY_COUNTRY_ROTATION = ["JP", "TR", "BR", "VN"]
MATRIX_PRIORITY_PAIRS = [("US", "TR")]
PAYPAL_AVAILABILITY_STATES = {"supported", "unsupported", "unknown"}
PAYPAL_UNAVAILABLE_MARKER = "paypal_unavailable_for_left_country"
PAID_PLAN_TYPES = {"plus", "pro", "team", "business", "enterprise"}
ELIGIBILITY_NETWORK_ATTEMPTS = 3
PROXY_QUALITY_TARGETS = {
    "checkout": "https://chatgpt.com/backend-api/payments/checkout",
    "provider": "https://api.stripe.com/v1/payment_methods",
    "promotion": "https://chatgpt.com/backend-api/payments/checkout/update",
}
PLAN_ALIASES = {
    "chatgpt_plus": "plus",
    "paid": "plus",
    "premium": "plus",
    "teams": "team",
    "edu": "team",
    "education": "team",
}
_engine_lock = threading.RLock()
_engine_module: Any | None = None

PLUS_STEP_PROGRESS = {
    "queued": (0, "排队中"),
    "preparing": (2, "准备国家组合"),
    "refresh": (5, "刷新令牌"),
    "run": (6, "启动提链链路"),
    "eligibility": (8, "检测账号提链资格"),
    "quality_gate": (12, "检测三出口质量"),
    "billing": (10, "准备账单资料"),
    "checkout": (18, "创建 Checkout"),
    "sync": (28, "同步优惠与税区"),
    "link_attempt": (35, "开始本轮提链"),
    "provider": (40, "创建支付会话"),
    "proxy": (48, "检测左右池出口"),
    "stripe_init": (56, "初始化 Stripe"),
    "stripe_methods": (62, "确认支付方式"),
    "payment_method": (68, "创建支付方式"),
    "stripe_amount": (74, "校验支付金额"),
    "confirm": (80, "确认 Checkout"),
    "approve": (88, "提交授权"),
    "stripe_redirect": (93, "获取中转链接"),
    "redirect": (97, "解析最终跳转"),
    "combo_result": (99, "本轮链路结束"),
    "done": (100, "提链完成"),
}
LEFT_PROXY_STAGES = {"checkout", "approve"}
RIGHT_PROXY_STAGES = {"provider", "promotion", "update", "poll", "redirect"}
COMBO_DIAGNOSTIC_FIELDS = (
    "promotion_error",
    "provider_error",
    "stripe_confirm_error",
    "approve_probe_error",
    "approve_response",
)
ATTEMPT_DIAGNOSTIC_LIMIT = 200


class _TaskCancelled(RuntimeError):
    pass


class _TaskRetryable(RuntimeError):
    def __init__(self, category: str, detail: str):
        super().__init__(detail)
        self.category = str(category or "unknown")
        self.detail = str(detail or "临时错误")


class _QualityComboUnavailable(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _normalize_plan_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9_+-]+", "_", text).strip("_")
    text = PLAN_ALIASES.get(text, text)
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
    if text in {"free", "free_plan", "none", "unknown"}:
        return "free"
    return text


def _account_has_k12_marker(account: dict[str, Any]) -> bool:
    return (
        _normalize_plan_value(account.get("plan_type")) == "k12"
        or _normalize_plan_value(account.get("account_type")) == "k12"
        or bool(account.get("k12_mode"))
        or bool(str(account.get("k12_join_status") or "").strip())
        or bool(str(account.get("k12_team_id") or "").strip())
    )


def _model_count(value: Any) -> int:
    try:
        return int(float(str(value or 0)))
    except Exception:
        return 0


def _iter_plan_roots(account: dict[str, Any]):
    for key in ("quota_details", "last_health_result"):
        value = account.get(key)
        if isinstance(value, dict):
            yield key, value


def _strong_model_access_plan(account: dict[str, Any]) -> tuple[str, str]:
    """Return a paid plan only from strong model-access evidence.

    Do not use metadata_plan_ignored=plus here. That field is kept by the
    account health checker exactly because non-model endpoints often expose
    purchasable/marketing Plus metadata for Free accounts.
    """
    for root_name, root in _iter_plan_roots(account):
        type_refresh = root.get("type_refresh") if isinstance(root.get("type_refresh"), dict) else {}
        if type_refresh:
            plan = _normalize_plan_value(type_refresh.get("plan_type"))
            if plan in PAID_PLAN_TYPES:
                return plan, f"{root_name}.type_refresh.plan_type"
            endpoints = type_refresh.get("endpoints")
            if isinstance(endpoints, list):
                for index, endpoint in enumerate(endpoints):
                    if not isinstance(endpoint, dict):
                        continue
                    inferred = _normalize_plan_value(endpoint.get("model_inferred_plan"))
                    if inferred in PAID_PLAN_TYPES:
                        return inferred, f"{root_name}.type_refresh.endpoints[{index}].model_inferred_plan"
                    endpoint_plan = _normalize_plan_value(endpoint.get("plan_type"))
                    if endpoint_plan in PAID_PLAN_TYPES:
                        return endpoint_plan, f"{root_name}.type_refresh.endpoints[{index}].plan_type"
                    if _model_count(endpoint.get("available_model_count")) >= 12:
                        return "plus", f"{root_name}.type_refresh.endpoints[{index}].available_model_count"
        models = root.get("models") if isinstance(root.get("models"), dict) else {}
        if models and _model_count(models.get("available_model_count")) >= 12:
            return "plus", f"{root_name}.models.available_model_count"
    return "", ""


def _effective_account_plan(account: dict[str, Any]) -> tuple[str, bool, str]:
    if _account_has_k12_marker(account):
        return "k12", False, "k12_marker"

    for key in ("plan_type", "account_type"):
        plan = _normalize_plan_value(account.get(key))
        if plan in PAID_PLAN_TYPES:
            return plan, True, key

    model_plan, source = _strong_model_access_plan(account)
    if model_plan in PAID_PLAN_TYPES:
        return model_plan, True, source

    for key in ("account_type", "plan_type"):
        plan = _normalize_plan_value(account.get(key))
        if plan and plan not in {"free", "unknown", "none"}:
            return plan, False, key

    return "free", False, "default"


def _with_effective_account_plan(account: dict[str, Any]) -> dict[str, Any]:
    item = dict(account)
    effective_plan, is_paid, reason = _effective_account_plan(item)
    item["effective_plan_type"] = effective_plan
    item["display_plan_type"] = effective_plan
    item["is_plus_for_filter"] = bool(is_paid)
    item["plan_filter_reason"] = reason
    return item


def _redact_log(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(accessToken|access_token|authorization|bearer)\s*[:=]\s*[^,\s\"']+", r"\1=[redacted]", text)
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(https?|socks5h?)://[^@\s/]+@", r"\1://[redacted]@", text)
    text = re.sub(r"(https?://[^\s]+)", lambda m: m.group(1)[:160] + ("..." if len(m.group(1)) > 160 else ""), text)
    return text[:800]


def _compact_proxy_reason(text: str) -> str:
    reasons: list[str] = []
    checks = [
        ("CONNECT tunnel failed", "代理 CONNECT 隧道失败"),
        ("Proxy CONNECT aborted", "代理连接被中断"),
        ("Empty reply from server", "代理无响应"),
        ("Connection refused", "连接被拒绝"),
        ("Could not resolve host", "DNS 解析失败"),
        ("timeout", "请求超时"),
        ("timed out", "请求超时"),
        ("TLS connect error", "TLS 连接失败"),
        ("SSL_ERROR", "TLS 连接失败"),
        ("Cloudflare", "Cloudflare 拦截"),
        ("status=403", "403 拦截"),
        ("status 403", "403 拦截"),
        ("401", "Token 无效/过期"),
        ("token", "Token 问题"),
    ]
    lowered = text.lower()
    for needle, label in checks:
        if needle.lower() in lowered and label not in reasons:
            reasons.append(label)
    return "、".join(reasons) or "检测失败"


def _compact_plus_log(value: object, *, limit: int = 220) -> str:
    text = str(value or "")
    text = text.replace("See https://curl.se/libcurl/c/libcurl-errors.html first for more details.", "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    prefix = ""
    match = re.match(r"^(\[[^\]]+\]\s*)(.*)$", text)
    if match:
        prefix, text = match.group(1), match.group(2).strip()

    if "开始执行 PP 0元订阅提链链路" in text:
        return f"{prefix}开始 PP 0元提链"
    match = re.search(r"Step 1 precheck_ip：checkout=([A-Z]{2,3})，provider=([A-Z]{2,3})，approve=([A-Z]{2,3})", text)
    if match:
        return f"{prefix}代理分段：checkout={match.group(1)} / provider={match.group(2)} / approve={match.group(3)}"
    match = re.search(r"Step 2 create_checkout：.*账单国家=([A-Z]{2,3})，PM账单=([A-Z]{2,3})，优惠/税区=([A-Z]{2,3})", text)
    if match:
        return f"{prefix}创建 checkout：账单={match.group(1)} / PM={match.group(2)} / 优惠={match.group(3)}"
    match = re.search(r"checkout 第 (\d+)/(\d+) 次：正在检测 (checkout|统一代理)\s*([A-Z]{2,3})?", text)
    if match:
        country = match.group(4) or ""
        target = f"checkout/{country}" if country else match.group(3)
        return f"{prefix}检测 {target} 代理 {match.group(1)}/{match.group(2)}"
    match = re.search(r"checkout 第 (\d+) 次 (checkout|统一代理)\s*([A-Z]{2,3})? 不可用", text)
    if match:
        country = match.group(3) or ""
        target = f"checkout/{country}" if country else match.group(2)
        return f"{prefix}{target} 代理不可用（第 {match.group(1)} 次）：{_compact_proxy_reason(text)}"
    match = re.search(r"checkout 第 (\d+)/(\d+) 次：正在创建 ChatGPT checkout", text)
    if match:
        return f"{prefix}创建 checkout {match.group(1)}/{match.group(2)}"
    match = re.search(r"checkout 第 (\d+) 次返回 HTML/风控/限流", text)
    if match:
        return f"{prefix}checkout 返回 HTML/风控/限流（第 {match.group(1)} 次），换 session 重试"
    if text.startswith("Plus 提链失败："):
        detail = _compact_plus_log(text.split("：", 1)[1], limit=limit)
        return f"{prefix}Plus 提链失败：{detail}"
    match = re.search(r"ChatGPT checkout 连续失败.*session\s+(\d+)\s+次.*?(checkout\s+([A-Z]{2,3}))?", text)
    if match:
        country = match.group(3) or ""
        target = f"checkout/{country} " if country else "checkout "
        return f"{prefix}{target}{match.group(1)} 次重试失败：{_compact_proxy_reason(text)}"
    if "检测失败:" in text and len(text) > 120:
        head = text.split("检测失败:", 1)[0].strip()
        return f"{prefix}{head}检测失败：{_compact_proxy_reason(text)}"
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return f"{prefix}{text}"


def _classify_plus_failure(value: object) -> str:
    text = str(value or "").lower()
    if (
        "账号资格检测" in text
        and any(
            marker in text
            for marker in (
                "http 500",
                "http 502",
                "http 503",
                "http 504",
                "internal server error",
                "service unavailable",
                "biscuit_baker_service_me_circuit_open",
            )
        )
    ):
        return "eligibility_upstream"
    if "proxy_quality_gate_failed" in text or "三出口质量" in text:
        return "proxy_quality_gate"
    if PAYPAL_UNAVAILABLE_MARKER in text or "paypal_availability=unsupported" in text:
        return "paypal_unsupported"
    if "checkout promotion update failed" in text:
        return "promotion_update"
    if "checkout taxes update failed" in text:
        return "taxes_update"
    if "checkout" in text and any(marker in text for marker in ("403", "forbidden", "拦截", "风控")):
        return "checkout_blocked"
    if "checkout" in text and any(
        marker in text
        for marker in (
            "检测失败",
            "tls 连接失败",
            "tls connect error",
            "网络",
            "timeout",
            "timed out",
            "connect",
            "connection",
            "隧道",
        )
    ):
        return "checkout_network"
    if "stripe init failed" in text:
        return "stripe_init"
    if "stripe_amount_nonzero" in text:
        return "stripe_amount_nonzero"
    if "stripe_amount_unknown" in text:
        return "stripe_amount_unknown"
    if "stripe payment_methods failed" in text or "stripe payment_methods bad response" in text:
        return "stripe_payment_method"
    if "stripe confirm failed" in text or "checkout_confirm_error" in text:
        return "stripe_confirm"
    if "generic_decline" in text:
        return "generic_decline"
    if "stripe submission failed" in text or "submission_attempt" in text and "state=failed" in text:
        return "stripe_submission"
    if "approve blocked" in text or "blocked/exception" in text:
        return "approve_blocked"
    if "chatgpt approve" in text or "requires_approval" in text:
        return "chatgpt_approve"
    if "出口不是 ipv6" in text:
        return "proxy_ip_family_mismatch"
    if "proxy_ip_mismatch" in text or "ip 一致性检查失败" in text:
        return "proxy_ip_mismatch"
    if "出口不是" in text:
        return "proxy_country_mismatch"
    if (
        "stripe_pm_redirect_not_ba" in text
        or "redirect url resolution timeout" in text
        or "未提取到最终 paypal ba approve" in text
    ):
        return "paypal_redirect"
    if any(
        marker in text
        for marker in (
            "failed to perform, curl:",
            "tls connect error",
            "connection refused",
            "connect tunnel failed",
            "could not resolve host",
            "proxy connect aborted",
            "empty reply from server",
            "connection was reset",
            "recv failure",
            "timed out",
            "timeout",
            "network",
            "网络",
            "代理检测",
        )
    ):
        return "proxy_network"
    if "access token" in text or "access_token" in text or "账号提链资格" in text or "account_not_free" in text:
        return "eligibility"
    return "unknown"


def _eligibility_failure_message(last_error: str, *, attempts: int, checkout_proxy: str = "") -> str:
    detail = _compact_plus_log(last_error, limit=240) or "代理请求失败"
    category = _classify_plus_failure(detail)
    if category == "eligibility_upstream":
        return (
            f"ChatGPT 账号资格服务连续 {attempts} 次返回临时错误；"
            "这不是账号不合格，也未消耗正式提链次数。"
            f"最后错误：{detail}"
        )
    if category != "proxy_network":
        return f"账号提链资格检测连续失败 {attempts} 次：{detail}"

    lowered = detail.lower()
    if "connect tunnel failed" in lowered or "proxy connect aborted" in lowered:
        reason = "住宅代理 CONNECT 被上游拒绝"
    elif "empty reply from server" in lowered:
        reason = "住宅代理已连接但未返回任何响应"
    elif "tls connect error" in lowered:
        reason = "住宅代理 TLS 建连失败"
    else:
        reason = _compact_proxy_reason(detail)

    hostname = ""
    try:
        hostname = str(urlsplit(str(checkout_proxy or "")).hostname or "").lower()
    except Exception:
        hostname = ""
    if hostname == "711proxy.com" or hostname.endswith(".711proxy.com"):
        action = (
            "请在 711Proxy 控制台确认套餐仍有效且有剩余流量、子用户密码正确，"
            "并重新生成与套餐匹配的 Host/端口后更新 Plus 专用住宅代理"
        )
    else:
        action = "请更新 Plus 专用住宅代理，并确认代理账号、密码、Host 和端口均可用"
    return (
        f"提链前住宅代理检测失败（{attempts}/{attempts}）：{reason}；"
        "尚未完成 access_token 有效性检测，也未进入国家矩阵或 Stripe。"
        f"{action}。最后错误：{detail}"
    )


def _result_failure_category(result: dict[str, Any], fallback: str) -> str:
    profile = result.get("stripe_submission_profile")
    if isinstance(profile, dict):
        reason = str(profile.get("submission_reason") or "").strip().lower()
        if reason == "generic_decline":
            return "generic_decline"
    emitted = str(result.get("failure_category") or "").strip().lower()
    return emitted or fallback


def _attempt_event_status(step: str, message: str, extra: dict[str, Any]) -> str:
    if str(step or "").strip().lower() != "link_attempt":
        return ""
    explicit = str(extra.get("attempt_status") or "").strip().lower()
    if explicit in {"success", "failed"}:
        return explicit
    lowered = str(message or "").lower()
    if "提链尝试" in lowered and "成功" in lowered:
        return "success"
    if "提链尝试" in lowered and ("失败" in lowered or "网络异常" in lowered):
        return "failed"
    return ""


def _attempt_error_text(value: object) -> str:
    text = _compact_plus_log(_redact_log(value), limit=360)
    text = re.sub(r"^提链尝试\s+\d+/\d+\s+(?:失败|网络异常)[：:]\s*", "", text)
    text = re.sub(r"^provider\s+提链已达到上限\s+\d+\s+次仍失败[：:]\s*", "", text, flags=re.IGNORECASE)
    return text[:360]


def _attempt_duration_seconds(started_at: object, finished_at: str) -> float | None:
    try:
        started = datetime.fromisoformat(str(started_at or ""))
        finished = datetime.fromisoformat(finished_at)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (finished - started).total_seconds()), 1)


def _attempt_diagnostic_record(
    current: dict[str, Any],
    updates: dict[str, Any],
    message: str,
    extra: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    projected = {**current, **updates}
    finished_at = _now()
    attempt = int(extra.get("link_attempt_count") or projected.get("link_attempt_count") or 0)
    attempt_limit = int(extra.get("link_attempt_limit") or projected.get("link_attempt_limit") or 0)
    error = "" if status == "success" else _attempt_error_text(extra.get("attempt_error") or message)
    failure_category = ""
    if status == "failed":
        failure_category = str(extra.get("failure_category") or "").strip().lower()
        if not failure_category:
            failure_category = _result_failure_category(projected, _classify_plus_failure(error))

    profile = projected.get("stripe_submission_profile")
    if not isinstance(profile, dict):
        profile = projected.get("stripe_confirm_response_profile")
    profile = profile if isinstance(profile, dict) else {}
    started_at = str(projected.get("attempt_started_at") or "")
    duration = _attempt_duration_seconds(started_at, finished_at)
    record: dict[str, Any] = {
        "run_number": int(projected.get("retry_count") or 0) + 1,
        "matrix_index": int(projected.get("matrix_index") or 1),
        "matrix_total": int(projected.get("matrix_total") or 1),
        "attempt": attempt,
        "attempt_limit": attempt_limit,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "failure_step": "" if status == "success" else str(current.get("engine_step") or "unknown"),
        "failure_category": failure_category,
        "error": error,
        "checkout_country": str(projected.get("checkout_proxy_country") or projected.get("billing_country") or ""),
        "provider_country": str(projected.get("provider_country") or ""),
        "approve_country": str(projected.get("approve_country") or ""),
        "promotion_country": str(projected.get("promotion_country") or ""),
        "payment_method_country": str(projected.get("payment_method_country") or ""),
        "checkout_proxy_ip": str(projected.get("checkout_proxy_ip") or ""),
        "provider_proxy_ip": str(projected.get("provider_proxy_ip") or ""),
        "approve_proxy_ip": str(projected.get("approve_proxy_ip") or ""),
        "promotion_proxy_ip": str(projected.get("promotion_proxy_ip") or ""),
        "paypal_availability": str(projected.get("paypal_availability") or ""),
        "stripe_amount": str(projected.get("stripe_amount") or ""),
        "currency": str(projected.get("currency") or ""),
        "submission_state": str(profile.get("submission_state") or ""),
        "submission_reason": str(profile.get("submission_reason") or ""),
        "submission_code": str(profile.get("submission_code") or ""),
        "promotion_error": str(projected.get("promotion_error") or "")[:360],
        "provider_error": str(projected.get("provider_error") or "")[:360],
        "stripe_confirm_error": str(projected.get("stripe_confirm_error") or "")[:360],
        "approve_probe_error": str(projected.get("approve_probe_error") or "")[:360],
    }
    if duration is not None:
        record["duration_seconds"] = duration
    return record


def _merge_attempt_diagnostic(history: object, record: dict[str, Any]) -> list[dict[str, Any]]:
    items = [dict(item) for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    key = (
        int(record.get("run_number") or 1),
        int(record.get("matrix_index") or 1),
        int(record.get("attempt") or 0),
    )
    for index, existing in enumerate(items):
        existing_key = (
            int(existing.get("run_number") or 1),
            int(existing.get("matrix_index") or 1),
            int(existing.get("attempt") or 0),
        )
        if existing_key != key:
            continue
        merged = dict(existing)
        for field, value in record.items():
            if value in (None, "", [], {}):
                continue
            if field == "failure_step" and merged.get(field):
                continue
            if field == "error" and merged.get(field) and len(str(value)) >= len(str(merged[field])):
                continue
            merged[field] = value
        items[index] = merged
        return items[-ATTEMPT_DIAGNOSTIC_LIMIT:]
    items.append(record)
    return items[-ATTEMPT_DIAGNOSTIC_LIMIT:]


def _stage_diagnostic_updates(
    step: str,
    message: str,
    detail: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    step = str(step or "").strip().lower()
    message = str(message or "").strip()
    detail = str(detail or "").strip()
    text = detail or message
    lowered = text.lower()
    has_failure = any(
        marker in lowered
        for marker in (
            "failed",
            "failure",
            "error",
            "blocked",
            "exception",
            "unavailable",
            "mismatch",
            "timeout",
            "403",
            "失败",
            "异常",
            "不可用",
            "不一致",
            "不是 ipv",
        )
    )
    updates: dict[str, Any] = {}
    if extra.get("approve_response") not in (None, ""):
        updates["approve_response"] = str(extra.get("approve_response"))
    elif extra.get("chatgpt_approval_endpoint") not in (None, ""):
        updates["approve_response"] = "approved"

    if "promotion update" in lowered or "taxes update" in lowered or (
        step in {"sync", "promotion", "update"} and has_failure
    ):
        updates["promotion_error"] = text
    elif "stripe confirm" in lowered or "checkout_confirm" in lowered or (
        step == "confirm" and has_failure
    ):
        updates["stripe_confirm_error"] = text
    elif step == "approve" and has_failure:
        if "出口" in text or "proxy" in lowered or "检测" in text or "mismatch" in lowered:
            updates["approve_probe_error"] = text
        else:
            updates["approve_response"] = text
    elif step in {"provider", "proxy", "stripe_init", "stripe_methods", "payment_method"} and has_failure:
        updates["provider_error"] = text
    elif "provider" in lowered and has_failure:
        updates["provider_error"] = text
    return updates


def _load_plus_engine() -> Any:
    global _engine_module
    with _engine_lock:
        if _engine_module is not None:
            return _engine_module
        if not PLUS_ENGINE_APP.is_file():
            raise RuntimeError(f"内置 Plus 提链引擎缺失: {PLUS_ENGINE_APP}")
        PLUS_ENGINE_DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("OPENAI_PAY_DATA_DIR", str(PLUS_ENGINE_DATA_DIR))
        os.environ.setdefault("OPENAI_PAY_LOG_DIR", str(PLUS_ENGINE_DATA_DIR / "logs"))
        deps_dir = Path(__file__).resolve().parents[1] / ".deps"
        if deps_dir.exists() and str(deps_dir) not in sys.path:
            sys.path.insert(0, str(deps_dir))
        spec = importlib.util.spec_from_file_location("moyu_local_plus_engine", PLUS_ENGINE_APP)
        if spec is None or spec.loader is None:
            raise RuntimeError("内置 Plus 提链引擎加载失败")
        module = importlib.util.module_from_spec(spec)
        sys.modules["moyu_local_plus_engine"] = module
        spec.loader.exec_module(module)
        _engine_module = module
        return module


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return max(minimum, min(maximum, number))


def _country(value: object, default: str = "US") -> str:
    text = str(value or default).strip().upper()
    aliases = {"UK": "GB"}
    text = aliases.get(text, text)
    return text[:2] if len(text) >= 2 else default


def _country_in(value: object, default: str, allowed: set[str]) -> str:
    country = _country(value, default)
    return country if country in allowed else default


def _country_sequence(value: object, default: list[str], allowed: set[str]) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw_items = [str(item or "") for item in value]
    else:
        raw_items = re.split(r"[,;\s|/]+", str(value or ""))
    items: list[str] = []
    for raw in raw_items:
        code = _country(raw, "")
        if code in allowed and code not in items:
            items.append(code)
    return items or [code for code in default if code in allowed]


def _matrix_country_pair(
    index: int,
    provider_rotation: list[str],
    promotion_rotation: list[str],
) -> tuple[str, str, int, int]:
    pairs = _matrix_country_pairs(provider_rotation, promotion_rotation)
    total = len(pairs)
    slot = (max(1, int(index or 1)) - 1) % total
    provider_country, promotion_country = pairs[slot]
    return provider_country, promotion_country, slot + 1, total


def _matrix_country_pairs(
    provider_rotation: list[str],
    promotion_rotation: list[str],
) -> list[tuple[str, str]]:
    providers = provider_rotation or list(DEFAULT_BILLING_COUNTRY_ROTATION)
    promotions = promotion_rotation or list(DEFAULT_PROXY_COUNTRY_ROTATION)
    pairs = [(provider, promotion) for provider in providers for promotion in promotions]
    priority = [pair for pair in MATRIX_PRIORITY_PAIRS if pair in pairs]
    return priority + [pair for pair in pairs if pair not in priority]


def _extraction_mode(cfg: dict[str, Any]) -> str:
    return "matrix" if cfg.get("country_rotation_enabled") is not False else "fixed_pair"


def _retry_limit_for_mode(cfg: dict[str, Any]) -> int:
    if _extraction_mode(cfg) == "matrix":
        return _clamp_int(cfg.get("matrix_combo_retry_limit"), 1, 1, 20)
    return _clamp_int(cfg.get("provider_retry_limit"), 15, 1, 20)


def _split_residential_pool(value: object) -> list[str]:
    if isinstance(value, list):
        raw_items = [str(item or "") for item in value]
    else:
        raw_items = str(value or "").replace("\r", "\n").split("\n")
    items: list[str] = []
    for raw in raw_items:
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and not line.lower().startswith(("http://", "https://", "socks://", "socks5://", "socks5h://")):
            _name, line = [part.strip() for part in line.split("=", 1)]
        if line:
            items.append(line)
    return items


def _normalize_country_proxy_pool(value: object) -> dict[str, str]:
    """规范化按国家代码分组的通用住宅代理池。

    前端以 dict 发送；这里也兼容简单文本格式：
    US=proxy
    BR=proxy1
    BR=proxy2
    """
    grouped: dict[str, list[str]] = {}
    if isinstance(value, dict):
        for raw_key, raw_value in value.items():
            raw_code = str(raw_key or "").strip().upper()
            code = _country(raw_key, "")
            if not code or raw_code in {"EU", "EUR"} or code not in RESIDENTIAL_PROXY_COUNTRY_CODES:
                continue
            items = _split_residential_pool(raw_value)
            if items:
                grouped.setdefault(code, []).extend(items)
    else:
        current_code = ""
        for raw in str(value or "").replace("\r", "\n").split("\n"):
            line = str(raw or "").strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^([A-Za-z]{2,3})\s*[:=]\s*(.+)$", line)
            if match:
                current_code = _country(match.group(1), "")
                payload = match.group(2).strip()
            else:
                payload = line
            if current_code in RESIDENTIAL_PROXY_COUNTRY_CODES and payload:
                grouped.setdefault(current_code, []).extend(_split_residential_pool(payload))
    return {code: "\n".join(items) for code, items in grouped.items() if items}


def _country_proxy_items(cfg: dict[str, Any], country: str) -> list[str]:
    pools = _normalize_country_proxy_pool(cfg.get("residential_country_proxy_pool"))
    code = _country(country, "")
    candidates = [code]
    for candidate in candidates:
        items = _split_residential_pool(pools.get(candidate))
        if items:
            return items
    return []


def _is_711_proxy_template(proxy: str) -> bool:
    normalized = proxy_pool_store.normalize_proxy_url(str(proxy or "").strip())
    if not normalized:
        return False
    if "://" not in normalized and "@" in normalized:
        normalized = f"http://{normalized}"
    try:
        parsed = urlsplit(normalized)
    except Exception:
        return False
    username = unquote(parsed.username or "").lower()
    hostname = str(parsed.hostname or "").lower()
    return "-zone-custom" in username and (hostname.endswith("711proxy.com") or "711proxy" in hostname)


def _is_kookeey_proxy_template(proxy: str) -> bool:
    normalized = proxy_pool_store.normalize_proxy_url(str(proxy or "").strip())
    try:
        parsed = urlsplit(normalized)
    except Exception:
        return False
    hostname = str(parsed.hostname or "").lower()
    password = unquote(parsed.password or "")
    return bool(
        "kookeey" in hostname
        and re.search(
            r"-(?:global|[A-Za-z]{2,3})-[A-Za-z0-9]+-\d+[mhd]$",
            password,
            flags=re.IGNORECASE,
        )
    )


def _portable_country_proxy_pool(country_pools: dict[str, str], common_pool: list[str]) -> list[str]:
    candidates = list(common_pool)
    for value in country_pools.values():
        candidates.extend(_split_residential_pool(value))
    portable: list[str] = []
    for item in candidates:
        lowered = str(item or "").lower()
        if (
            "{country}" not in lowered
            and "{country_lower}" not in lowered
            and not re.search(r"region-[a-z]{2,3}", lowered)
            and not _is_711_proxy_template(item)
            and not _is_kookeey_proxy_template(item)
        ):
            continue
        if item not in portable:
            portable.append(item)
    return portable


def _proxy_country_hint(proxy: str) -> str:
    normalized = proxy_pool_store.normalize_proxy_url(str(proxy or "").strip())
    try:
        parsed = urlsplit(normalized)
        searchable = " ".join([
            unquote(parsed.username or ""),
            unquote(parsed.password or ""),
            str(parsed.hostname or ""),
        ]).upper()
    except Exception:
        searchable = normalized.upper()
    for code in sorted(RESIDENTIAL_PROXY_COUNTRY_CODES):
        if re.search(rf"(?:^|[-_.]){re.escape(code)}(?:[-_.]|$)", searchable):
            return code
    return ""


def _matching_common_proxy_pool(common_pool: list[str], country: str) -> list[str]:
    code = _country(country, "")
    matched = [item for item in common_pool if _proxy_country_hint(item) in {"", code}]
    return matched or _portable_country_proxy_pool({}, common_pool)


def _infer_residential_proxy_country(proxy: str) -> str:
    text = str(proxy or "").strip().upper()
    for pattern in (
        r"(?:^|[-_.])REGION[-_]?([A-Z]{2})(?:[-_.:]|$)",
        r"(?:^|[-_.])COUNTRY[-_]?([A-Z]{2})(?:[-_.:]|$)",
        r"(?:^|[-_.])([A-Z]{2})[-_]SESSION[-_]",
    ):
        match = re.search(pattern, text)
        if match:
            code = _country(match.group(1), "")
            if code in RESIDENTIAL_PROXY_COUNTRY_CODES:
                return code
            return "__UNSUPPORTED__"
    return ""


def _legacy_stage_proxy_country_pool(cfg: dict[str, Any]) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    default_checkout = _country_in(
        cfg.get("checkout_country") or cfg.get("target_country"),
        "US",
        CHECKOUT_COUNTRY_CODES,
    )
    default_provider = _country_in(
        cfg.get("provider_country") or cfg.get("promotion_country"),
        "BR",
        PROVIDER_COUNTRY_CODES,
    )
    default_approve = _country_in(
        cfg.get("approve_country") or cfg.get("checkout_country") or cfg.get("target_country"),
        default_checkout,
        APPROVE_COUNTRY_CODES,
    )
    default_promotion = _country_in(
        cfg.get("promotion_country"),
        "JP",
        PROMOTION_COUNTRY_CODES,
    )
    stage_defaults = (
        ("residential_checkout_proxy", default_checkout),
        ("residential_provider_proxy", default_provider),
        ("residential_approve_proxy", default_approve),
        ("residential_promotion_proxy", default_promotion),
    )
    for key, fallback_code in stage_defaults:
        for line in _split_residential_pool(cfg.get(key)):
            code = _infer_residential_proxy_country(line) or fallback_code
            if code == "__UNSUPPORTED__":
                continue
            if code in RESIDENTIAL_PROXY_COUNTRY_CODES:
                grouped.setdefault(code, []).append(line)
    return {code: "\n".join(items) for code, items in grouped.items() if items}


def _proxy_session_id() -> str:
    return str(random.randint(10_000_000, 99_999_999))


def _proxy_host_port(hostname: str, port: int | None) -> str:
    host = str(hostname or "").strip().strip("[]")
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}" if port else host


def _proxy_url_with_auth(parsed: Any, username: str, password: str) -> str:
    host = _proxy_host_port(parsed.hostname or "", parsed.port)
    if not host:
        return parsed.geturl()
    auth = f"{quote(username, safe='-._~')}:{quote(password or '', safe='-._~')}@" if username else ""
    return urlunsplit((parsed.scheme or "http", f"{auth}{host}", parsed.path, parsed.query, parsed.fragment))


def _fill_residential_template(proxy: str, *, country: str, session_id: str, index: int, stage: str) -> str:
    country = _country(country, "US")
    replacements = {
        "{country}": country,
        "{COUNTRY}": country,
        "{country_lower}": country.lower(),
        "{session}": session_id,
        "{session_id}": session_id,
        "{sticky}": session_id,
        "{slot}": str(index),
        "{index}": str(index),
        "{stage}": stage,
    }
    text = str(proxy or "").strip()
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _rotate_residential_proxy(proxy: str, *, country: str, session_id: str, index: int, stage: str) -> str:
    """按全球混播里的 sticky/session 思路生成住宅代理 URL。

    支持：
    - 模板占位符：{country}/{COUNTRY}/{session}/{slot}/{stage}
    - 711Proxy：用户名里的 region-XX 与 -session-xxx
    - Kookeey/类似格式：密码末尾追加 -COUNTRY-session
    """
    templated = _fill_residential_template(proxy, country=country, session_id=session_id, index=index, stage=stage)
    normalized = proxy_pool_store.normalize_proxy_url(templated)
    country = _country(country, "US")
    if normalized and "://" not in normalized and "@" in normalized:
        normalized = f"http://{normalized}"
    if not normalized or "://" not in normalized:
        return normalized
    try:
        parsed = urlsplit(normalized)
    except Exception:
        return normalized
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username or not password:
        return normalized
    if _is_711_proxy_template(normalized) or re.search(r"region-[A-Za-z]{2,3}", username):
        if re.search(r"region-[A-Za-z]{2,3}", username):
            rotated_username = re.sub(r"region-[A-Za-z]{2,3}", f"region-{country}", username, count=1, flags=re.IGNORECASE)
        else:
            rotated_username = f"{username}-region-{country}"
        if re.search(r"-session-[A-Za-z0-9]+", rotated_username, flags=re.IGNORECASE):
            rotated_username = re.sub(r"-session-[A-Za-z0-9]+", f"-session-{session_id}", rotated_username, count=1, flags=re.IGNORECASE)
        elif re.search(r"-sessTime-", rotated_username, flags=re.IGNORECASE):
            rotated_username = re.sub(r"(?=-sessTime-)", f"-session-{session_id}", rotated_username, count=1, flags=re.IGNORECASE)
        else:
            rotated_username = f"{rotated_username}-session-{session_id}-sessTime-5"
        return _proxy_url_with_auth(parsed, rotated_username, password)
    # 如果用户已显式写了 {session} 模板，避免再改写密码。
    if any(token in str(proxy) for token in ("{session}", "{session_id}", "{sticky}")):
        return normalized
    kookeey_match = re.match(
        r"^(?P<base>.+?)-(?P<country>global|[A-Za-z]{2,3})-"
        r"(?P<session>[A-Za-z0-9]+)-(?P<ttl>\d+[mhd])$",
        password,
        flags=re.IGNORECASE,
    )
    if kookeey_match:
        rotated_password = (
            f"{kookeey_match.group('base')}-{country}-{session_id}-"
            f"{kookeey_match.group('ttl')}"
        )
        return _proxy_url_with_auth(parsed, username, rotated_password)
    match = re.match(r"^(?P<base>.+?)-(?P<country>global|[A-Za-z]{2,3})(?:-[A-Za-z0-9]+)?$", password, flags=re.IGNORECASE)
    password_base = match.group("base") if match else password
    return _proxy_url_with_auth(parsed, username, f"{password_base}-{country}-{session_id}")


AUTO_RETRY_FAILURE_CATEGORIES = {
    "eligibility_upstream",
    "proxy_quality_gate",
    "proxy_network",
    "checkout_network",
    "checkout_blocked",
    "promotion_update",
    "taxes_update",
    "stripe_init",
    "stripe_amount_unknown",
    "stripe_amount_nonzero",
    "stripe_payment_method",
    "stripe_confirm",
    "generic_decline",
    "stripe_submission",
    "approve_blocked",
    "chatgpt_approve",
    "proxy_ip_family_mismatch",
    "proxy_ip_mismatch",
    "proxy_country_mismatch",
    "paypal_redirect",
}


def _has_transient_failure_marker(detail: object) -> bool:
    text = str(detail or "").strip().lower()
    if not text:
        return False
    if re.search(r"(?:http(?:\s+error)?|status(?:_code)?)\s*[:=]?\s*(?:408|425|429|5\d\d)\b", text):
        return True
    return any(
        marker in text
        for marker in (
            "service unavailable",
            "internal server error",
            "bad gateway",
            "gateway timeout",
            "temporarily unavailable",
            "rate limit",
            "too many requests",
            "biscuit_baker_service_me_circuit_open",
            "connect tunnel failed",
            "代理 connect 隧道失败",
            "tls connect error",
            "tls 连接失败",
            "empty reply from server",
            "timed out",
            "timeout",
            "cloudflare",
            "cf-ray",
            "connection refused",
            "connection aborted",
            "connection closed",
            "connection was reset",
            "connection reset",
            "server disconnected",
            "remote disconnected",
            "could not resolve host",
            "name resolution",
            "proxy connect aborted",
            "recv failure",
            "unexpected eof",
            "eof occurred",
            "curl: (5)",
            "curl: (6)",
            "curl: (7)",
            "curl: (18)",
            "curl: (28)",
            "curl: (35)",
            "curl: (52)",
            "curl: (55)",
            "curl: (56)",
            "curl: (92)",
        )
    )


def _is_auto_retryable_failure(category: object, detail: object = "") -> bool:
    normalized = str(category or "").strip().lower()
    if normalized in AUTO_RETRY_FAILURE_CATEGORIES:
        return True
    return normalized == "unknown" and _has_transient_failure_marker(detail)


def _deepcopy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _default_config() -> dict[str, Any]:
    return {
        "engine_mode": "local",
        "use_proxy_pool": False,
        "use_residential_proxy_pool": True,
        "residential_proxy_pool": "",
        "residential_country_proxy_pool": {},
        "residential_checkout_proxy": "",
        "residential_provider_proxy": "",
        "residential_approve_proxy": "",
        "residential_promotion_proxy": "",
        "residential_single_proxy_mode": False,
        "residential_session_ttl_minutes": 5,
        "proxy_quality_gate_enabled": True,
        "proxy_quality_candidate_limit": 3,
        "proxy_quality_probe_rounds": 2,
        "proxy_quality_max_latency_ms": 12000,
        "proxy_quality_quarantine_seconds": 300,
        "residential_via_project_proxy": True,
        "residential_parent_node_filter": "",
        "residential_parent_proxy_override": "",
        "residential_via_clash": True,
        "residential_clash_proxy": "",
        "proxy": "",
        "target_country": "US",
        "checkout_country": "US",
        "provider_country": "BR",
        "approve_country": "US",
        "promotion_country": "BR",
        "payment_method_country": "US",
        "payment_locale": "en",
        "checkout_ui_mode": "custom",
        "stripe_provider_egress": "left_residential",
        "stripe_poll_egress": "provider",
        "post_approve_poll_timeout_seconds": 90,
        "enable_playwright_approve_fallback": True,
        "enable_playwright_redirect_fallback": True,
        "allow_intermediate_url_fallback": False,
        "paypal_confirm_mode": "payment_method",
        "paypal_confirm_protocol": "standalone",
        "concurrency": 1,
        "timeout_seconds": 360,
        "checkout_retry_limit": 10,
        "provider_retry_limit": 15,
        "matrix_combo_retry_limit": 1,
        "smsbower_pm_max_price": 0.2,
        "retry_limits_version": 2,
        "refresh_tokens": False,
        "provider_same_as_target": False,
        "approve_same_as_checkout": True,
        "country_rotation_enabled": True,
        "billing_country_rotation": list(DEFAULT_BILLING_COUNTRY_ROTATION),
        "proxy_country_rotation": list(DEFAULT_PROXY_COUNTRY_ROTATION),
        "require_zero_stripe_amount": True,
        "require_ipv6_egress": False,
        "strict_same_proxy_ip": False,
        "strict_approve_same_proxy_ip": False,
    }


def _default_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "config": _default_config(),
        "stats": {
            "success": 0,
            "fail": 0,
            "cancelled": 0,
            "done": 0,
            "running": 0,
            "total": 0,
            "elapsed_seconds": 0,
            "success_rate": 0,
            "started_at": "",
            "updated_at": "",
        },
        "logs": [],
        "results": [],
    }


def _normalize_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _default_config()
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in cfg})
    cfg["engine_mode"] = "local"
    # Plus extraction owns a dedicated residential pool. Never fall back to the
    # registration/global proxy inventory when residential configuration is absent.
    cfg["use_proxy_pool"] = False
    cfg["use_residential_proxy_pool"] = True
    cfg["residential_proxy_pool"] = "\n".join(_split_residential_pool(cfg.get("residential_proxy_pool")))
    country_proxy_pool = _normalize_country_proxy_pool(cfg.get("residential_country_proxy_pool"))
    if not country_proxy_pool:
        country_proxy_pool = _legacy_stage_proxy_country_pool(cfg)
    cfg["residential_country_proxy_pool"] = country_proxy_pool
    cfg["residential_checkout_proxy"] = "\n".join(_split_residential_pool(cfg.get("residential_checkout_proxy")))
    cfg["residential_provider_proxy"] = "\n".join(_split_residential_pool(cfg.get("residential_provider_proxy")))
    cfg["residential_approve_proxy"] = "\n".join(_split_residential_pool(cfg.get("residential_approve_proxy")))
    cfg["residential_promotion_proxy"] = "\n".join(_split_residential_pool(cfg.get("residential_promotion_proxy")))
    cfg["residential_single_proxy_mode"] = bool(cfg.get("residential_single_proxy_mode"))
    cfg["residential_session_ttl_minutes"] = _clamp_int(cfg.get("residential_session_ttl_minutes"), 5, 1, 120)
    cfg["proxy_quality_gate_enabled"] = bool(cfg.get("proxy_quality_gate_enabled", True))
    cfg["proxy_quality_candidate_limit"] = _clamp_int(cfg.get("proxy_quality_candidate_limit"), 3, 1, 10)
    cfg["proxy_quality_probe_rounds"] = _clamp_int(cfg.get("proxy_quality_probe_rounds"), 2, 1, 3)
    cfg["proxy_quality_max_latency_ms"] = _clamp_int(cfg.get("proxy_quality_max_latency_ms"), 12000, 2000, 30000)
    cfg["proxy_quality_quarantine_seconds"] = _clamp_int(
        cfg.get("proxy_quality_quarantine_seconds"), 300, 30, 3600
    )
    cfg["residential_via_project_proxy"] = bool(cfg.get("residential_via_project_proxy", True))
    cfg["residential_parent_node_filter"] = str(cfg.get("residential_parent_node_filter") or "").strip()[:160]
    cfg["residential_parent_proxy_override"] = proxy_pool_store.normalize_proxy_url(
        str(cfg.get("residential_parent_proxy_override") or "").strip()
    )
    cfg["residential_via_clash"] = cfg["residential_via_project_proxy"]
    # Runtime parent proxies are assigned from the project's proxy configuration.
    # Do not persist or reuse the legacy manually entered Clash address.
    cfg["residential_clash_proxy"] = ""
    cfg["proxy"] = proxy_pool_store.normalize_proxy_url(str(cfg.get("proxy") or "").strip())
    cfg["target_country"] = _country_in(cfg.get("target_country"), "US", CHECKOUT_COUNTRY_CODES)
    cfg["promotion_country"] = _country_in(cfg.get("promotion_country"), "TR", PROMOTION_COUNTRY_CODES)
    cfg["checkout_country"] = _country_in(cfg.get("checkout_country"), "US", CHECKOUT_COUNTRY_CODES)
    raw_provider_country = raw.get("provider_country") if isinstance(raw, dict) else None
    provider_fallback = _country_in(cfg.get("provider_country"), "BR", PROVIDER_COUNTRY_CODES)
    cfg["provider_country"] = _country_in(raw_provider_country, provider_fallback, PROVIDER_COUNTRY_CODES)
    raw_approve_country = raw.get("approve_country") if isinstance(raw, dict) else None
    cfg["approve_country"] = _country_in(raw_approve_country, cfg["checkout_country"], APPROVE_COUNTRY_CODES)
    cfg["payment_method_country"] = _country_in(cfg.get("payment_method_country"), cfg["target_country"], BILLING_COUNTRY_CODES)
    cfg["payment_locale"] = str(cfg.get("payment_locale") or "en").strip() or "en"
    checkout_ui_mode = str(cfg.get("checkout_ui_mode") or "custom").strip().lower()
    cfg["checkout_ui_mode"] = "custom" if checkout_ui_mode == "custom" else "hosted"
    provider_egress = str(cfg.get("stripe_provider_egress") or "left_residential").strip().lower()
    cfg["stripe_provider_egress"] = (
        provider_egress if provider_egress in {"project_parent", "us_residential"} else "left_residential"
    )
    # Older configs allowed Stripe poll/redirect to switch to the promotion
    # egress. Migrate them to the Provider session to preserve Stripe state and
    # IP continuity across init -> PM -> confirm -> poll -> redirect.
    cfg["stripe_poll_egress"] = "provider"
    cfg["post_approve_poll_timeout_seconds"] = _clamp_int(
        cfg.get("post_approve_poll_timeout_seconds"), 90, 30, 180
    )
    confirm_mode = str(cfg.get("paypal_confirm_mode") or "payment_method").strip().lower()
    cfg["paypal_confirm_mode"] = "direct" if confirm_mode == "direct" else "payment_method"
    confirm_protocol = str(cfg.get("paypal_confirm_protocol") or "standalone").strip().lower()
    cfg["paypal_confirm_protocol"] = (
        confirm_protocol if confirm_protocol in {"enhanced", "minimal", "chatgpt_confirm"} else "standalone"
    )
    cfg["concurrency"] = _clamp_int(cfg.get("concurrency"), 1, 1, 10)
    cfg["timeout_seconds"] = _clamp_int(cfg.get("timeout_seconds"), 360, 30, 1800)
    cfg["checkout_retry_limit"] = _clamp_int(cfg.get("checkout_retry_limit"), 10, 1, 50)
    cfg["matrix_combo_retry_limit"] = _clamp_int(cfg.get("matrix_combo_retry_limit"), 1, 1, 20)
    try:
        smsbower_pm_max_price = float(cfg.get("smsbower_pm_max_price") or 0.2)
    except (TypeError, ValueError):
        smsbower_pm_max_price = 0.2
    cfg["smsbower_pm_max_price"] = round(max(0.001, min(10.0, smsbower_pm_max_price)), 3)
    legacy_retry_limits = isinstance(raw, dict) and _clamp_int(raw.get("retry_limits_version"), 0, 0, 2) < 2
    fixed_pair_retry_limit = _clamp_int(cfg.get("provider_retry_limit"), 15, 1, 20)
    if legacy_retry_limits and fixed_pair_retry_limit == cfg["matrix_combo_retry_limit"]:
        fixed_pair_retry_limit = 15
    cfg["provider_retry_limit"] = fixed_pair_retry_limit
    cfg["retry_limits_version"] = 2
    matrix_enabled = cfg.get("country_rotation_enabled") is not False
    raw_billing_rotation = cfg.get("billing_country_rotation")
    raw_proxy_rotation = cfg.get("proxy_country_rotation")
    cfg["billing_country_rotation"] = _country_sequence(
        raw_billing_rotation,
        DEFAULT_BILLING_COUNTRY_ROTATION,
        BILLING_COUNTRY_CODES,
    )
    cfg["proxy_country_rotation"] = _country_sequence(
        raw_proxy_rotation,
        DEFAULT_PROXY_COUNTRY_ROTATION,
        PROMOTION_COUNTRY_CODES,
    )
    cfg["country_rotation_enabled"] = matrix_enabled
    if matrix_enabled:
        primary_provider = cfg["billing_country_rotation"][0]
        primary_promotion = cfg["proxy_country_rotation"][0]
        fixed_checkout = cfg["checkout_country"]
        cfg.update({
            "target_country": fixed_checkout,
            "approve_country": fixed_checkout,
            "payment_method_country": primary_provider,
            "provider_country": primary_provider,
            "promotion_country": primary_promotion,
        })
    for key in (
        "refresh_tokens",
        "provider_same_as_target",
        "approve_same_as_checkout",
        "require_zero_stripe_amount",
        "require_ipv6_egress",
        "strict_same_proxy_ip",
        "strict_approve_same_proxy_ip",
        "enable_playwright_approve_fallback",
        "enable_playwright_redirect_fallback",
        "allow_intermediate_url_fallback",
    ):
        cfg[key] = bool(cfg.get(key))
    cfg["provider_same_as_target"] = cfg["provider_country"] != cfg["checkout_country"]
    cfg["approve_same_as_checkout"] = cfg["approve_country"] == cfg["checkout_country"]
    cfg["residential_single_proxy_mode"] = False
    return cfg


def _public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    item = dict(_normalize_config(cfg))
    item["extraction_mode"] = _extraction_mode(item)
    item["active_retry_limit"] = _retry_limit_for_mode(item)
    item["residential_proxy_count"] = len(_split_residential_pool(item.get("residential_proxy_pool")))
    item["residential_country_proxy_count"] = {
        code: len(_split_residential_pool(pool))
        for code, pool in (item.get("residential_country_proxy_pool") or {}).items()
        if _split_residential_pool(pool)
    }
    item["residential_checkout_proxy_count"] = len(_split_residential_pool(item.get("residential_checkout_proxy")))
    item["residential_provider_proxy_count"] = len(_split_residential_pool(item.get("residential_provider_proxy")))
    item["residential_approve_proxy_count"] = len(_split_residential_pool(item.get("residential_approve_proxy")))
    item["residential_promotion_proxy_count"] = len(_split_residential_pool(item.get("residential_promotion_proxy")))
    item["matrix_total"] = (
        len(item.get("billing_country_rotation") or []) * len(item.get("proxy_country_rotation") or [])
        if _extraction_mode(item) == "matrix"
        else 1
    )
    return item


def _public_result(item: dict[str, Any], *, include_secret: bool = True) -> dict[str, Any]:
    result = dict(item)
    long_url = str(result.get("long_url") or "")
    result["long_url_present"] = bool(long_url or result.get("long_url_present"))
    result["long_url_hash"] = str(result.get("long_url_hash") or _sha(long_url))
    intermediate_url = str(result.get("intermediate_url") or "")
    result["intermediate_url_present"] = bool(
        intermediate_url or result.get("intermediate_url_present")
    )
    result["intermediate_url_hash"] = str(
        result.get("intermediate_url_hash") or _sha(intermediate_url)
    )
    if result.get("error"):
        result["error"] = _compact_plus_log(_redact_log(result.get("error")), limit=360)
    if not include_secret:
        result.pop("long_url", None)
        result.pop("intermediate_url", None)
    return result


def _public_account_summary(limit: int = 0) -> dict[str, Any]:
    page_limit = max(0, min(500, int(limit or 0)))
    if page_limit:
        pool = account_pool_page(page_limit, 0, include_secret=False)
        source_accounts = list(pool.get("accounts") or [])
    else:
        pool = account_pool()
        source_accounts = list_registered_accounts(include_secret=False)
    # Plus 页面使用和“账号库存”同一份库存数据。这里仅移除完整 token，
    # 保留类型、测活、额度、sub2api、空间等展示字段，避免两个页面看起来不一致。
    accounts = [_with_effective_account_plan(item) for item in source_accounts]
    pool["accounts"] = accounts
    stats = dict(pool.get("stats") or {})
    stats["plus_filter_hidden"] = sum(1 for item in accounts if item.get("is_plus_for_filter"))
    stats["plus_filter_visible"] = sum(1 for item in accounts if not item.get("is_plus_for_filter"))
    pool["stats"] = stats
    return pool


def _secret_value(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _refresh_access_token_with_fallback(
    account: dict[str, Any],
    current_access_token: str,
    *,
    proxy: str = "",
) -> tuple[str, dict[str, Any], str]:
    current_access_token = str(current_access_token or "").strip()
    refresh_token = str(account.get("refresh_token") or "").strip()
    if not refresh_token:
        return current_access_token, {}, ""
    try:
        if _refresh_platform_tokens is None:
            raise RuntimeError("当前环境无法刷新 OAuth token")
        refreshed = _refresh_platform_tokens(refresh_token, proxy=proxy)  # type: ignore[misc]
        refreshed = dict(refreshed) if isinstance(refreshed, dict) else {}
        refreshed_access_token = str(refreshed.get("access_token") or "").strip()
        if not refreshed_access_token:
            raise RuntimeError("刷新 OAuth token 未返回 access_token")
        return refreshed_access_token, refreshed, ""
    except Exception as error:
        if current_access_token:
            return current_access_token, {}, _compact_plus_log(error, limit=240)
        raise


def _has_usable_token(account: dict[str, Any]) -> bool:
    return bool(
        _secret_value(
            account,
            "access_token",
            "accessToken",
            "refresh_token",
            "refreshToken",
            "session",
            "session_token",
            "sessionToken",
        )
    )


def _task_account_metadata(account: dict[str, Any]) -> dict[str, Any]:
    access_token = _secret_value(account, "access_token", "accessToken", "session_token", "session")
    claims = jwt_payload(access_token)
    auth = claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    profile = claims.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}

    token_email = str(profile.get("email") or claims.get("email") or "").strip()
    configured_email = str(account.get("email") or account.get("username") or "").strip()
    email = token_email or configured_email

    account_type = _normalize_plan_value(
        auth.get("chatgpt_plan_type")
        or profile.get("plan_type")
        or account.get("effective_plan_type")
        or account.get("plan_type")
        or account.get("account_type")
    )
    expires_at: int | str | None = None
    for value in (claims.get("exp"), account.get("token_expires_at"), account.get("expires_at")):
        if value in (None, ""):
            continue
        try:
            expires_at = int(float(str(value)))
        except Exception:
            expires_at = str(value)
        break
    return {
        "account_email": email,
        "account_type": account_type or "unknown",
        "token_expires_at": expires_at,
    }


def _manual_account_from_item(item: Any, index: int) -> dict[str, Any] | None:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return _manual_account_from_item(parsed, index)
        except Exception:
            pass
        email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, flags=re.IGNORECASE)
        token = ""
        token_match = re.search(r"(?i)(?:access[_-]?token|accessToken|session[_-]?token|session|bearer)\s*[:=]\s*([A-Za-z0-9._~+/=-]{20,})", text)
        if token_match:
            token = token_match.group(1).strip()
        if not token:
            bearer_match = re.search(r"(?i)Bearer\s+([A-Za-z0-9._~+/=-]{20,})", text)
            if bearer_match:
                token = bearer_match.group(1).strip()
        if not token:
            parts = [part.strip() for part in re.split(r"[\s,|]+", text) if part.strip()]
            for part in parts:
                if "@" in part and email_match:
                    continue
                if len(part) >= 20:
                    token = part
                    break
        if not token:
            return None
        email = email_match.group(0) if email_match else f"manual-{index}@token.local"
        return {
            "email": email,
            "access_token": token,
            "session_token": token,
            "status": "active",
            "source_type": "manual",
            "_manual": True,
        }
    if not isinstance(item, dict):
        return None
    access_token = _secret_value(item, "access_token", "accessToken", "token")
    session_token = _secret_value(item, "session_token", "sessionToken", "session")
    refresh_token = _secret_value(item, "refresh_token", "refreshToken")
    token = access_token or session_token
    if not token and not refresh_token:
        return None
    email = str(item.get("email") or item.get("account") or item.get("username") or "").strip()
    if not email:
        email = f"manual-{index}@token.local"
    return {
        **item,
        "email": email,
        "access_token": token,
        "session_token": session_token or token,
        "refresh_token": refresh_token,
        "status": str(item.get("status") or "active"),
        "source_type": str(item.get("source_type") or item.get("source") or "manual"),
        "_manual": True,
    }


def _account_dedupe_key(account: dict[str, Any]) -> str:
    selector = str(account.get("inventory_id") or account.get("inventory_key") or account.get("id") or account.get("email") or "").strip().lower()
    token = _secret_value(account, "access_token", "refresh_token", "session_token")
    return selector or _sha(token)


class PlusLinkService:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._task_controls: dict[str, dict[str, Any]] = {}
        self._retry_queue: list[dict[str, Any]] = []
        self._quality_quarantine: dict[str, float] = {}
        self._last_progress_save_at = 0.0
        self._progress_save_dirty = False
        self._state = self._load()
        self._interrupted_task_ids: list[str] = []
        recovered = 0
        for item in self._state.get("results", []):
            if not isinstance(item, dict) or str(item.get("status") or "") not in {"queued", "running", "paused", "pausing", "cancelling"}:
                continue
            task_id = str(item.get("task_id") or "").strip()
            if task_id:
                self._interrupted_task_ids.append(task_id)
            item.update({
                "status": "cancelled",
                "stage": "服务重启后已结束",
                "progress_label": "任务已结束",
                "error": "服务重启时未发现对应执行线程",
                "retryable": True,
                "cleanup_pending": False,
                "finished_at": _now(),
                "updated_at": _now(),
            })
            recovered += 1
        if self._state.get("enabled") or recovered:
            self._state["enabled"] = False
            if recovered:
                stats = self._state.setdefault("stats", {})
                stats["running"] = 0
                stats["cancelled"] = int(stats.get("cancelled") or 0) + recovered
                stats["done"] = int(stats.get("done") or 0) + recovered
                stats["updated_at"] = _now()
            self._save()

    def _load(self) -> dict[str, Any]:
        data = read_json(self._store_file, {}, expected_schema_version=1)
        if isinstance(data, dict):
            state = _default_state()
            state.update({k: v for k, v in data.items() if k in state})
            state["config"] = _normalize_config(data.get("config") if isinstance(data.get("config"), dict) else {})
            state["stats"] = {**_default_state()["stats"], **(data.get("stats") if isinstance(data.get("stats"), dict) else {})}
            state["logs"] = data.get("logs") if isinstance(data.get("logs"), list) else []
            state["results"] = data.get("results") if isinstance(data.get("results"), list) else []
            return state
        return _default_state()

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._store_file, self._state, schema_version=1)
        self._last_progress_save_at = time.monotonic()
        self._progress_save_dirty = False

    def _save_progress(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_progress_save_at >= 0.5:
            self._save()
        else:
            self._progress_save_dirty = True

    def resume_interrupted_tasks(self) -> dict[str, Any]:
        with self._lock:
            task_ids = list(dict.fromkeys(self._interrupted_task_ids))
            self._interrupted_task_ids = []
        if not task_ids:
            return {"requested": 0, "restarted": 0, "skipped": 0}

        restarted = 0
        for task_id in task_ids:
            try:
                self.retry_task(task_id)
                restarted += 1
            except RuntimeError as error:
                self._append_log(f"Plus 中断任务自动恢复失败：{task_id} · {error}", "warning")
        skipped = max(0, len(task_ids) - restarted)
        if restarted:
            self._append_log(
                f"检测到服务异常重启，已自动恢复 {restarted} 个 Plus 提链任务，跳过 {skipped} 个",
                "warning",
            )
        return {"requested": len(task_ids), "restarted": restarted, "skipped": skipped}

    def _append_log(self, text: str, level: str = "info") -> None:
        with self._lock:
            clean_text = _compact_plus_log(_redact_log(text))
            self._state.setdefault("logs", []).append({"time": _now(), "text": clean_text, "level": str(level or "info")})
            self._state["logs"] = self._state["logs"][-300:]
            self._state.setdefault("stats", {})["updated_at"] = _now()
            self._save()

    def _bump(self, **updates: Any) -> None:
        with self._lock:
            stats = self._state.setdefault("stats", {})
            stats.update(updates)
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                stats["elapsed_seconds"] = round(elapsed, 1)
            success = int(stats.get("success") or 0)
            attempted = success + int(stats.get("fail") or 0)
            stats["success_rate"] = round(success * 100 / attempted, 1) if attempted else 0
            stats["updated_at"] = _now()
            self._save()

    def get(
        self,
        *,
        include_secret: bool = True,
        include_inventory: bool = True,
        inventory_limit: int = 0,
        compact: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if compact:
                snap = _deepcopy({
                    key: self._state.get(key)
                    for key in ("enabled", "config", "stats", "results", "logs")
                })
            else:
                snap = _deepcopy(self._state)
            runner_alive = bool(self._runner and self._runner.is_alive())
        snap["enabled"] = bool(snap.get("enabled")) and runner_alive
        snap["runner_alive"] = runner_alive
        snap["stopping"] = bool(runner_alive and self._stop_event.is_set())
        snap["config"] = _public_config(snap.get("config") if isinstance(snap.get("config"), dict) else {})
        snap["results"] = [_public_result(item, include_secret=include_secret) for item in snap.get("results") or [] if isinstance(item, dict)]
        snap["logs"] = [
            {**item, "text": _compact_plus_log(_redact_log(item.get("text")), limit=260)}
            for item in ((snap.get("logs") or [])[-30:] if compact else (snap.get("logs") or []))
            if isinstance(item, dict)
        ]
        if include_inventory:
            page_limit = max(0, min(500, int(inventory_limit or 0)))
            snap["account_pool"] = _public_account_summary(page_limit)
            try:
                snap["partial_account_pool"] = (
                    partial_account_store.partial_account_pool_page(page_limit, 0, include_secret=False)
                    if page_limit
                    else partial_account_store.partial_account_pool(include_secret=False)
                )
            except Exception:
                snap["partial_account_pool"] = {"items": [], "stats": {"total": 0}}
        else:
            snap.pop("account_pool", None)
            snap.pop("partial_account_pool", None)
        if not compact:
            try:
                snap["proxy_pool"] = proxy_pool_store.get_proxy_pool_config()
            except Exception:
                snap["proxy_pool"] = {}
        return snap

    def _operation_response(self) -> dict[str, Any]:
        return self.get(include_inventory=False, compact=True)

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cfg = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
            for key, value in updates.items():
                if key not in cfg:
                    continue
                cfg[key] = value
            normalized_cfg = _normalize_config(cfg)
            self._state["config"] = normalized_cfg
            if "concurrency" in updates:
                concurrency = int(normalized_cfg["concurrency"])
                for task in self._task_controls.values():
                    if bool(task.get("submitted") or task.get("started")):
                        continue
                    task_cfg = task.get("cfg") if isinstance(task.get("cfg"), dict) else {}
                    task_cfg["concurrency"] = concurrency
                    task["cfg"] = task_cfg
            self._save()
        return self._operation_response()

    def clear_results(self) -> dict[str, Any]:
        with self._lock:
            self._state["results"] = []
            self._state["logs"] = []
            self._state["stats"] = {**_default_state()["stats"]}
            self._save()
        return self._operation_response()

    def clear_failed_results(self) -> dict[str, Any]:
        return self._clear_terminal_results({"failed", "cancelled"})

    def clear_finished_results(self) -> dict[str, Any]:
        return self._clear_terminal_results({"success", "failed", "cancelled"})

    def _recalculate_stats_locked(self) -> None:
        results = [item for item in self._state.get("results", []) if isinstance(item, dict)]
        success = sum(1 for item in results if str(item.get("status") or "") == "success")
        failed = sum(1 for item in results if str(item.get("status") or "") == "failed")
        cancelled = sum(1 for item in results if str(item.get("status") or "") == "cancelled")
        running = sum(
            1 for item in results
            if str(item.get("status") or "") in {"queued", "running", "paused", "pausing", "cancelling"}
        )
        stats = self._state.setdefault("stats", {})
        stats.update({
            "success": success,
            "fail": failed,
            "cancelled": cancelled,
            "done": success + failed + cancelled,
            "running": running,
            "total": len(results),
            "updated_at": _now(),
        })
        if not results:
            stats["elapsed_seconds"] = 0
            stats["started_at"] = ""

    def _clear_terminal_results(self, statuses: set[str]) -> dict[str, Any]:
        with self._lock:
            results = [item for item in self._state.get("results", []) if isinstance(item, dict)]
            self._state["results"] = [
                item for item in results
                if str(item.get("status") or "") not in statuses
            ]
            self._recalculate_stats_locked()
            self._save()
        return self._operation_response()

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        active_tasks: list[tuple[str, str, bool]] = []
        with self._lock:
            for task_id, control in self._task_controls.items():
                cancel_event = control.get("cancel_event")
                if isinstance(cancel_event, threading.Event):
                    cancel_event.set()
                result = next(
                    (
                        item for item in self._state.setdefault("results", [])
                        if isinstance(item, dict) and str(item.get("task_id") or "") == str(task_id)
                    ),
                    None,
                )
                if not result or str(result.get("status") or "") in {"success", "failed", "cancelled"}:
                    continue
                active_tasks.append((
                    str(task_id),
                    str(result.get("email") or control.get("email") or task_id),
                    bool(control.get("submitted") or control.get("started")),
                ))
            self._state["enabled"] = False
            self._save()
        for task_id, email, was_running in active_tasks:
            self._cancel_task(task_id, email, reason="用户一键终止任务", was_running=was_running)
        self._append_log(
            f"Plus 提链已终止：{len(active_tasks)} 个任务立即标记为已取消，进行中的网络请求在后台收尾",
            "warning",
        )
        return self._operation_response()

    def _select_accounts(
        self,
        emails: list[str] | None,
        *,
        partial_accounts: list[str] | None = None,
        manual_accounts: list[Any] | None = None,
        manual_tokens: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        selected = None if emails is None else {str(email or "").strip().lower() for email in emails if str(email or "").strip()}
        selected_partial = {str(item or "").strip().lower() for item in (partial_accounts or []) if str(item or "").strip()}
        accounts = list_registered_accounts(include_secret=True)
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for account in accounts:
            email = str(account.get("email") or "").strip()
            if not email:
                continue
            if selected is not None and (not selected or not any(account_matches_selector(account, selector) for selector in selected)):
                continue
            if str(account.get("status") or "active") not in ("", "active"):
                continue
            if not _has_usable_token(account):
                continue
            key = _account_dedupe_key(account)
            if key in seen:
                continue
            seen.add(key)
            result.append(account)
        if selected_partial:
            for item in partial_account_store.list_partial_accounts(include_secret=True):
                selector_values = {
                    str(item.get("id") or "").strip().lower(),
                    str(item.get("email") or "").strip().lower(),
                }
                if not selector_values.intersection(selected_partial):
                    continue
                if not _has_usable_token(item):
                    continue
                account = {
                    **item,
                    "access_token": _secret_value(item, "access_token", "accessToken", "session_token", "session"),
                    "refresh_token": _secret_value(item, "refresh_token", "refreshToken"),
                    "status": "active",
                    "source_type": "partial",
                    "_partial": True,
                }
                key = _account_dedupe_key(account)
                if key in seen:
                    continue
                seen.add(key)
                result.append(account)
        manual_items: list[Any] = []
        if isinstance(manual_accounts, list):
            manual_items.extend(manual_accounts)
        if isinstance(manual_tokens, list):
            manual_items.extend(manual_tokens)
        for index, item in enumerate(manual_items, start=1):
            account = _manual_account_from_item(item, index)
            if not account or not _has_usable_token(account):
                continue
            key = _account_dedupe_key(account)
            if key in seen:
                continue
            seen.add(key)
            result.append(account)
        return result

    def _set_task_result(self, task_id: str, updates: dict[str, Any]) -> bool:
        with self._lock:
            results = self._state.setdefault("results", [])
            for item in results:
                if isinstance(item, dict) and str(item.get("task_id")) == str(task_id):
                    current_status = str(item.get("status") or "")
                    if current_status in {"success", "failed", "cancelled"}:
                        return False
                    item.update(updates)
                    item["updated_at"] = _now()
                    break
            else:
                return False
            self._save_progress(force=str(updates.get("status") or "") in {"success", "failed", "cancelled"})
        return True

    def _result_for_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next(
                (
                    item
                    for item in self._state.setdefault("results", [])
                    if isinstance(item, dict) and str(item.get("task_id") or "") == str(task_id)
                ),
                None,
            )

    def _new_task(self, account: dict[str, Any], cfg: dict[str, Any], index: int, *, retry_of: str = "", task_id: str = "") -> dict[str, Any]:
        stamp = int(time.time() * 1000)
        suffix = uuid.uuid4().hex[:8]
        task_id = str(task_id or f"plus-{stamp}-{max(1, int(index or 1))}-{suffix}")
        account_metadata = _task_account_metadata(account)
        task_account = dict(account)
        if account_metadata.get("account_email"):
            task_account["email"] = str(account_metadata["account_email"])
        resume_event = threading.Event()
        resume_event.set()
        task = {
            "task_id": task_id,
            "account": task_account,
            "account_metadata": account_metadata,
            "cfg": _deepcopy(cfg),
            "index": max(1, int(index or 1)),
            "email": str(task_account.get("email") or "").strip(),
            "retry_of": str(retry_of or ""),
            "resume_event": resume_event,
            "cancel_event": threading.Event(),
            "started": False,
            "submitted": False,
            "settled": False,
        }
        with self._lock:
            self._task_controls[task_id] = task
        return task

    def _task_cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            control = self._task_controls.get(str(task_id)) or {}
            event = control.get("cancel_event")
        return bool(isinstance(event, threading.Event) and event.is_set())

    def _wait_for_task_ready(self, task_id: str, email: str, *, running: bool) -> None:
        while True:
            with self._lock:
                control = self._task_controls.get(str(task_id)) or {}
                resume_event = control.get("resume_event")
                cancel_event = control.get("cancel_event")
            if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
                raise _TaskCancelled("用户取消任务")
            if not isinstance(resume_event, threading.Event) or resume_event.is_set():
                return
            current = self._result_for_task(task_id) or {}
            if str(current.get("status") or "") != "paused":
                self._set_task_result(task_id, {
                    "status": "paused",
                    "stage": "已暂停",
                    "progress_label": "已暂停",
                })
            if isinstance(cancel_event, threading.Event) and cancel_event.wait(0.2):
                raise _TaskCancelled("用户取消任务")
            if not running:
                time.sleep(0.05)

    def pause_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        with self._lock:
            control = self._task_controls.get(task_id)
            result = self._result_for_task(task_id)
            if not control or not result:
                raise RuntimeError("Plus 提链任务不存在或服务已重启")
            status = str(result.get("status") or "")
            if status in {"success", "failed", "cancelled", "cancelling"}:
                raise RuntimeError("当前任务状态不能暂停")
            resume_event = control.get("resume_event")
            if isinstance(resume_event, threading.Event):
                resume_event.clear()
            next_status = "pausing" if bool(control.get("started")) else "paused"
            result.update({
                "status": next_status,
                "stage": "等待当前步骤结束" if next_status == "pausing" else "已暂停",
                "progress_label": "暂停中" if next_status == "pausing" else "已暂停",
                "updated_at": _now(),
            })
            self._save()
        return self._operation_response()

    def resume_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        with self._lock:
            control = self._task_controls.get(task_id)
            result = self._result_for_task(task_id)
            if not control or not result:
                raise RuntimeError("Plus 提链任务不存在或服务已重启")
            status = str(result.get("status") or "")
            if status not in {"paused", "pausing"}:
                raise RuntimeError("当前任务未暂停")
            resume_event = control.get("resume_event")
            if isinstance(resume_event, threading.Event):
                resume_event.set()
            started = bool(control.get("started"))
            result.update({
                "status": "running" if started else "queued",
                "stage": "继续提链" if started else "排队中",
                "progress_label": "继续提链" if started else "排队中",
                "updated_at": _now(),
            })
            self._save()
        return self._operation_response()

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        with self._lock:
            control = self._task_controls.get(task_id)
            result = self._result_for_task(task_id)
            if not control or not result:
                raise RuntimeError("Plus 提链任务不存在或服务已重启")
            status = str(result.get("status") or "")
            if status not in {"success", "failed", "cancelled"}:
                cancel_event = control.get("cancel_event")
                if isinstance(cancel_event, threading.Event):
                    cancel_event.set()
                started = bool(control.get("submitted") or control.get("started"))
                email = str(result.get("email") or control.get("email") or task_id)
                self._cancel_task(task_id, email, reason="用户取消任务", was_running=started)
        return self._operation_response()

    def delete_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        with self._lock:
            result = self._result_for_task(task_id)
            if not result:
                raise RuntimeError("Plus 提链任务不存在")
            control = self._task_controls.get(task_id)
            was_running = bool(control and (control.get("submitted") or control.get("started")))
            if control:
                cancel_event = control.get("cancel_event")
                if isinstance(cancel_event, threading.Event):
                    cancel_event.set()
                control["deleted"] = True
                if not was_running:
                    self._task_controls.pop(task_id, None)
            self._retry_queue = [
                task for task in self._retry_queue
                if str(task.get("task_id") or "") != task_id
            ]
            self._state["results"] = [
                item for item in self._state.setdefault("results", [])
                if not isinstance(item, dict) or str(item.get("task_id") or "") != task_id
            ]
            self._recalculate_stats_locked()
            self._save()
        self._append_log(f"Plus 提链任务已删除：{task_id}", "warning")
        return self._operation_response()

    def retry_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        with self._lock:
            previous = self._task_controls.get(task_id)
            result = self._result_for_task(task_id)
            if not result:
                raise RuntimeError("Plus 提链任务不存在")
            if str(result.get("status") or "") not in {"success", "failed", "cancelled"}:
                raise RuntimeError("任务尚未结束，不能重试")
            if bool(result.get("cleanup_pending")):
                raise RuntimeError("任务网络线程仍在后台收尾，请稍后重试")
            email = str(result.get("email") or "").strip()
            if previous:
                account = previous.get("account") if isinstance(previous.get("account"), dict) else {}
                cfg = previous.get("cfg") if isinstance(previous.get("cfg"), dict) else _normalize_config({})
            else:
                cfg = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
                account = {}
        if not account:
            candidates = self._select_accounts([email], partial_accounts=[email])
            if not candidates:
                raise RuntimeError("历史任务对应账号已不在库存/半成品中，无法重试")
            account = candidates[0]
        task = self._new_task(account, cfg, 1, retry_of=task_id, task_id=task_id)
        now = _now()
        with self._lock:
            previous_status = str(result.get("status") or "")
            created_at = str(result.get("created_at") or now)
            retry_count = int(result.get("retry_count") or 0) + 1
            attempt_history = [
                dict(item)
                for item in (result.get("attempt_diagnostics_history") or [])
                if isinstance(item, dict)
            ][-ATTEMPT_DIAGNOSTIC_LIMIT:]
            result.clear()
            result.update({
                "task_id": task_id,
                "email": str(task["email"]),
                **dict(task.get("account_metadata") or {}),
                "status": "queued",
                "stage": "重试排队中",
                "engine_step": "queued",
                "progress_percent": 0,
                "progress_label": "重试排队中",
                "matrix_index": 1,
                "matrix_total": _public_config(cfg).get("matrix_total", 1),
                "link_attempt_count": 0,
                "link_attempt_limit": _retry_limit_for_mode(cfg),
                "extraction_mode": _extraction_mode(cfg),
                "active_retry_limit": _retry_limit_for_mode(cfg),
                "task_left_countries": list(cfg.get("billing_country_rotation") or []),
                "task_right_countries": list(cfg.get("proxy_country_rotation") or []),
                "task_checkout_country": str(cfg.get("checkout_country") or "US"),
                "retryable": True,
                "retry_count": retry_count,
                "attempt_diagnostics_history": attempt_history,
                "created_at": created_at,
                "updated_at": now,
            })
            stats = self._state.setdefault("stats", {})
            if previous_status == "success":
                stats["success"] = max(0, int(stats.get("success") or 0) - 1)
            elif previous_status == "failed":
                stats["fail"] = max(0, int(stats.get("fail") or 0) - 1)
            elif previous_status == "cancelled":
                stats["cancelled"] = max(0, int(stats.get("cancelled") or 0) - 1)
            stats["done"] = max(0, int(stats.get("done") or 0) - 1)
            stats["total"] = len([item for item in self._state.setdefault("results", []) if isinstance(item, dict)])
            self._state["enabled"] = True
            self._retry_queue.append(task)
            self._stop_event.clear()
            self._save()
            runner_alive = bool(self._runner and self._runner.is_alive())
        if not runner_alive:
            self._stop_event.clear()
            self._runner = threading.Thread(target=self._run_task_batch, args=([],), daemon=True, name="plus-link-retry")
            self._runner.start()
        return self._operation_response()

    def retry_failed_tasks(self) -> dict[str, Any]:
        with self._lock:
            terminal_results = [
                item
                for item in self._state.setdefault("results", [])
                if isinstance(item, dict) and str(item.get("status") or "") in {"failed", "cancelled"}
            ]
            task_ids = [
                str(item.get("task_id") or "")
                for item in terminal_results
                if str(item.get("task_id") or "")
                and item.get("retryable") is not False
                and not bool(item.get("cleanup_pending"))
            ]
        if not task_ids:
            raise RuntimeError("暂无已完成后台收尾且可重启的失败/停止任务")

        restarted = 0
        for task_id in task_ids:
            try:
                self.retry_task(task_id)
                restarted += 1
            except RuntimeError:
                continue
        skipped = max(0, len(terminal_results) - restarted)
        if not restarted:
            raise RuntimeError("失败/停止任务对应账号已不可用，无法一键重启")

        self._append_log(f"一键重启已重新排队 {restarted} 个任务，跳过 {skipped} 个任务", "info")
        response = self._operation_response()
        response["bulk_retry"] = {
            "requested": len(task_ids),
            "restarted": restarted,
            "skipped": skipped,
        }
        return response

    def _cancel_task(self, task_id: str, email: str, *, reason: str, was_running: bool) -> bool:
        cancelled = False
        with self._lock:
            for item in self._state.setdefault("results", []):
                if not isinstance(item, dict) or str(item.get("task_id")) != str(task_id):
                    continue
                if str(item.get("status") or "") in {"success", "failed", "cancelled"}:
                    return False
                item.update({
                    "status": "cancelled",
                    "stage": "已停止",
                    "progress_label": "已取消",
                    "error": str(reason or "任务已停止")[:300],
                    "cleanup_pending": bool(was_running),
                    "finished_at": _now(),
                    "updated_at": _now(),
                })
                cancelled = True
                break
            if cancelled:
                stats = self._state.setdefault("stats", {})
                stats["cancelled"] = int(stats.get("cancelled") or 0) + 1
                stats["done"] = int(stats.get("done") or 0) + 1
                if was_running:
                    stats["running"] = max(0, int(stats.get("running") or 0) - 1)
                self._save()
        if cancelled:
            self._bump()
            self._append_log(f"[{email or task_id}] Plus 提链已取消：{reason}", "warning")
        return cancelled

    def _mark_task_settled(self, task_id: str) -> None:
        with self._lock:
            control = self._task_controls.get(str(task_id))
            if control is not None:
                control["settled"] = True
                if control.get("deleted"):
                    self._task_controls.pop(str(task_id), None)
            for item in self._state.setdefault("results", []):
                if not isinstance(item, dict) or str(item.get("task_id") or "") != str(task_id):
                    continue
                if item.get("cleanup_pending"):
                    item["cleanup_pending"] = False
                    item["updated_at"] = _now()
                break
            self._save()

    def _cancel_queued_tasks(self, tasks: list[dict[str, Any]], *, reason: str) -> int:
        task_ids = {str(item.get("task_id") or "") for item in tasks if str(item.get("task_id") or "")}
        if not task_ids:
            return 0
        cancelled = 0
        now = _now()
        with self._lock:
            for item in self._state.setdefault("results", []):
                if not isinstance(item, dict) or str(item.get("task_id") or "") not in task_ids:
                    continue
                if str(item.get("status") or "") not in {"queued", "paused"}:
                    continue
                item.update({
                    "status": "cancelled",
                    "stage": "已停止",
                    "progress_label": "已取消",
                    "error": str(reason or "任务已停止")[:300],
                    "finished_at": now,
                    "updated_at": now,
                })
                cancelled += 1
            if cancelled:
                stats = self._state.setdefault("stats", {})
                stats["cancelled"] = int(stats.get("cancelled") or 0) + cancelled
                stats["done"] = int(stats.get("done") or 0) + cancelled
                self._save()
        if cancelled:
            self._bump()
            self._append_log(f"已取消 {cancelled} 个尚未开始的 Plus 提链任务", "warning")
        return cancelled


    def _assign_residential_proxy(
        self,
        cfg: dict[str, Any],
        index: int,
        *,
        left_country: str,
        promotion_country: str,
    ) -> dict[str, Any] | None:
        common_pool = _split_residential_pool(cfg.get("residential_proxy_pool"))
        if not common_pool:
            common_pool = proxy_pool_store.residential_proxy_urls()
        country_pools = _normalize_country_proxy_pool(cfg.get("residential_country_proxy_pool"))
        has_country_pools = bool(country_pools)
        portable_pool = _portable_country_proxy_pool(country_pools, common_pool)
        left_country_pool = _split_residential_pool(country_pools.get(left_country))
        promotion_country_pool = _split_residential_pool(country_pools.get(promotion_country))
        checkout_pool = _split_residential_pool(cfg.get("residential_checkout_proxy"))
        provider_pool = _split_residential_pool(cfg.get("residential_provider_proxy"))
        approve_pool = _split_residential_pool(cfg.get("residential_approve_proxy"))
        promotion_pool = _split_residential_pool(cfg.get("residential_promotion_proxy"))
        if has_country_pools:
            left_pool_used = left_country_pool or portable_pool
            promotion_pool_used = promotion_country_pool or portable_pool
        else:
            legacy_portable = _portable_country_proxy_pool({}, common_pool + checkout_pool + provider_pool + approve_pool + promotion_pool)
            left_pool_used = checkout_pool or provider_pool or approve_pool or _matching_common_proxy_pool(common_pool, left_country) or legacy_portable
            promotion_pool_used = promotion_pool or _matching_common_proxy_pool(common_pool, promotion_country) or legacy_portable
        if not bool(cfg.get("use_residential_proxy_pool")) or not (left_pool_used and promotion_pool_used):
            return None
        safe_index = max(1, int(index or 1))

        def pick(pool: list[str]) -> tuple[str, int, int]:
            if not pool:
                return "", 0, 0
            slot = (safe_index - 1) % len(pool)
            return pool[slot], slot + 1, len(pool)

        left_base, left_slot, left_size = pick(left_pool_used)
        promotion_base, promotion_slot, promotion_size = pick(promotion_pool_used)
        left_session = _proxy_session_id()
        promotion_session = _proxy_session_id()
        checkout_proxy = _rotate_residential_proxy(
            left_base,
            country=left_country,
            session_id=left_session,
            index=left_slot or safe_index,
            stage="checkout",
        )
        promotion_proxy = _rotate_residential_proxy(
            promotion_base,
            country=promotion_country,
            session_id=promotion_session,
            index=promotion_slot or safe_index,
            stage="promotion",
        )
        return {
            "proxy": checkout_proxy,
            "checkout_proxy": checkout_proxy,
            "provider_proxy": checkout_proxy,
            "approve_proxy": checkout_proxy,
            "promotion_proxy": promotion_proxy,
            "checkout_proxy_pool": left_pool_used,
            "provider_proxy_pool": left_pool_used,
            "approve_proxy_pool": left_pool_used,
            "promotion_proxy_pool": promotion_pool_used,
            "source": "residential_proxy_pool",
            "label": f"双池 左{left_country}#{left_slot or '-'} / 右{promotion_country}#{promotion_slot or '-'}",
            "slot": left_slot,
            "pool_size": max(left_size, promotion_size),
            "checkout_slot": left_slot,
            "provider_slot": left_slot,
            "approve_slot": left_slot,
            "promotion_slot": promotion_slot,
            "checkout_pool_size": left_size,
            "provider_pool_size": left_size,
            "approve_pool_size": left_size,
            "promotion_pool_size": promotion_size,
            "single_proxy_mode": False,
            "left_country": left_country,
            "promotion_country": promotion_country,
        }

    def _assign_project_parent_proxy(
        self,
        cfg: dict[str, Any],
        index: int,
        *,
        upstream_proxy: str = "",
    ) -> dict[str, Any] | None:
        if not bool(cfg.get("residential_via_project_proxy")):
            return None
        override = proxy_pool_store.normalize_proxy_url(str(cfg.get("residential_parent_proxy_override") or "").strip())
        if override:
            return {
                "proxy": override,
                "source": "explicit_parent_proxy",
                "label": "显式父代理",
                "slot": 0,
                "node_id": "",
                "node_name_filter": "",
                "assignment_index": max(1, int(index or 1)),
            }
        pool = proxy_pool_store.get_proxy_pool_config(include_secret=True)
        plus_subscription_id = str(pool.get("plus_subscription_id") or "").strip()
        node_name_filter = str(cfg.get("residential_parent_node_filter") or "").strip()
        if plus_subscription_id:
            subscriptions = pool.get("subscriptions") if isinstance(pool.get("subscriptions"), list) else []
            subscription = next(
                (
                    item
                    for item in subscriptions
                    if isinstance(item, dict) and str(item.get("id") or "") == plus_subscription_id
                ),
                None,
            )
            if not subscription:
                raise RuntimeError("住宅代理父级分配失败：代理配置中的 Plus 专用订阅已不存在")
            if not bool(subscription.get("enabled")):
                raise RuntimeError("住宅代理父级分配失败：代理配置中的 Plus 专用订阅未启用")
            subscription_name = str(subscription.get("name") or "").strip()
            if not subscription_name:
                raise RuntimeError("住宅代理父级分配失败：Plus 专用订阅缺少名称")
            node_name_filter = f"[{subscription_name}]"
        concurrency = _clamp_int(cfg.get("concurrency"), 1, 1, 10)
        retry_limit = max(1, min(12, int(pool.get("max_retry_count") or 3)))
        last_error = ""
        for parent_attempt in range(retry_limit):
            assignment = proxy_pool_store.assign_proxy(
                max(1, int(index or 1)) + parent_attempt,
                fallback="",
                total_threads=concurrency,
                slot_index=(max(1, int(index or 1)) - 1) % concurrency,
                node_name_filter=node_name_filter,
                # The parent transports traffic to 711; validate that exact
                # two-hop path below instead of probing OpenAI directly.
                validate_node=False,
                purpose="plus_parent_proxy",
            )
            parent_proxy = proxy_pool_store.normalize_proxy_url(str(assignment.get("proxy") or "").strip())
            if not parent_proxy:
                last_error = _compact_plus_log(
                    assignment.get("error") or assignment.get("label") or "项目代理配置没有可用节点"
                )
                break
            if str(upstream_proxy or "").strip():
                probe = proxy_chain_service.probe_proxy_chain(
                    upstream_proxy,
                    parent_proxy,
                    timeout=12,
                )
                if not bool(probe.get("ok")):
                    last_error = _compact_plus_log(probe.get("error") or "父节点无法连接住宅代理")
                    proxy_pool_store.report_proxy_failure(
                        assignment,
                        last_error,
                        quarantine_seconds=300,
                    )
                    continue
            return {
                "proxy": parent_proxy,
                "source": str(assignment.get("source") or "project_proxy_pool"),
                "label": str(assignment.get("label") or "项目代理节点"),
                "slot": int(assignment.get("slot") or 0),
                "node_id": str(assignment.get("node_id") or ""),
                "node_name_filter": node_name_filter,
                "assignment_index": max(1, int(index or 1)) + parent_attempt,
            }
        raise RuntimeError(f"住宅代理父级分配失败：{last_error or '没有可用的双层代理链路'}")

    def _payload_for_account(
        self,
        account: dict[str, Any],
        cfg: dict[str, Any],
        index: int,
        *,
        proxy_index: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        allocation_index = max(1, int(proxy_index if proxy_index is not None else index))
        target_country = _country_in(cfg.get("target_country"), "US", CHECKOUT_COUNTRY_CODES)
        promotion_country = _country_in(cfg.get("promotion_country"), "TR", PROMOTION_COUNTRY_CODES)
        checkout_country = _country_in(cfg.get("checkout_country"), "US", CHECKOUT_COUNTRY_CODES)
        provider_country = _country_in(cfg.get("provider_country"), checkout_country, PROVIDER_COUNTRY_CODES)
        approve_country = _country_in(cfg.get("approve_country"), checkout_country, APPROVE_COUNTRY_CODES)
        payment_method_country = _country_in(cfg.get("payment_method_country"), target_country, BILLING_COUNTRY_CODES)
        matrix_index = 1
        matrix_total = 1
        matrix_enabled = bool(cfg.get("country_rotation_enabled"))
        if matrix_enabled:
            billing_rotation = _country_sequence(cfg.get("billing_country_rotation"), DEFAULT_BILLING_COUNTRY_ROTATION, BILLING_COUNTRY_CODES)
            proxy_rotation = _country_sequence(cfg.get("proxy_country_rotation"), DEFAULT_PROXY_COUNTRY_ROTATION, PROMOTION_COUNTRY_CODES)
            provider_country, promotion_country, matrix_index, matrix_total = _matrix_country_pair(
                index,
                billing_rotation,
                proxy_rotation,
            )
            target_country = checkout_country
            payment_method_country = provider_country
            approve_country = checkout_country
        provider_egress = str(cfg.get("stripe_provider_egress") or "left_residential")
        provider_uses_parent = provider_egress == "project_parent"
        provider_uses_us_residential = provider_egress == "us_residential"
        if provider_uses_us_residential:
            provider_country = "US"
        provider_uses_target = provider_uses_parent or provider_uses_us_residential or provider_country != checkout_country or matrix_enabled
        approve_same_as_checkout = approve_country == checkout_country
        proxy_info = self._assign_residential_proxy(
            cfg,
            allocation_index,
            left_country=checkout_country,
            promotion_country=promotion_country,
        )
        if proxy_info is None:
            raise RuntimeError(
                "Plus 提链必须配置独立住宅代理："
                f"左池 {checkout_country} 与右池 {promotion_country} 均需要可用代理或 Plus 专用公共模板"
            )
        proxy = str(proxy_info.get("proxy") or "").strip()
        checkout_proxy = str(proxy_info.get("checkout_proxy") or proxy).strip()
        parent_info = self._assign_project_parent_proxy(cfg, allocation_index, upstream_proxy=checkout_proxy)
        parent_proxy = str((parent_info or {}).get("proxy") or "").strip()
        if parent_info:
            proxy_info["parent_proxy"] = parent_proxy
            proxy_info["parent_proxy_source"] = str(parent_info.get("source") or "")
            proxy_info["parent_proxy_label"] = str(parent_info.get("label") or "")
            proxy_info["parent_proxy_slot"] = int(parent_info.get("slot") or 0)
            proxy_info["label"] = f"{parent_info.get('label') or '项目代理'} → {proxy_info.get('label') or '住宅代理'}"
        provider_proxy_info = (
            self._assign_residential_proxy(
                cfg,
                allocation_index,
                left_country=provider_country,
                promotion_country=promotion_country,
            )
            if not provider_uses_parent and (provider_country != checkout_country or matrix_enabled)
            else None
        )
        provider_proxy = (
            parent_proxy
            if provider_uses_parent
            else str((provider_proxy_info or proxy_info).get("provider_proxy") or proxy).strip()
        )
        provider_proxy_pool = (
            []
            if provider_uses_parent
            else list((provider_proxy_info or proxy_info).get("provider_proxy_pool") or [])
        )
        # Matrix mode models three independent exits. Do not collapse Provider and
        # Promotion onto one sticky session even when their country codes match.
        right_stage_shared = (
            not matrix_enabled
            and not provider_uses_parent
            and provider_country == promotion_country
        )
        approve_proxy_info = (
            self._assign_residential_proxy(
                cfg,
                allocation_index,
                left_country=approve_country,
                promotion_country=promotion_country,
            )
            if approve_country not in {checkout_country, provider_country}
            else None
        )
        if approve_same_as_checkout:
            approve_proxy = checkout_proxy
            approve_proxy_pool = list(proxy_info.get("checkout_proxy_pool") or [])
        elif approve_country == provider_country:
            approve_proxy = provider_proxy
            approve_proxy_pool = provider_proxy_pool
        else:
            approve_proxy = str((approve_proxy_info or {}).get("approve_proxy") or "").strip()
            approve_proxy_pool = list((approve_proxy_info or {}).get("approve_proxy_pool") or [])
        if not approve_proxy:
            raise RuntimeError(f"Approve {approve_country} 缺少可用住宅代理")
        promotion_proxy = (
            provider_proxy
            if right_stage_shared
            else str(proxy_info.get("promotion_proxy") or proxy).strip()
        )
        promotion_proxy_pool = (
            provider_proxy_pool
            if right_stage_shared
            else list(proxy_info.get("promotion_proxy_pool") or [])
        )
        access_token = _secret_value(account, "access_token", "accessToken", "session_token", "session")
        payload = {
            "accessToken": access_token,
            # Keep the ChatGPT browser identity stable across eligibility,
            # Checkout retries and approval for this account. Generating a new
            # oai-device-id for every POST is an avoidable risk signal.
            "device_id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"plus:{account.get('chatgpt_account_id') or account.get('email') or account.get('inventory_key') or index}",
                )
            ),
            "proxy": checkout_proxy,
            "jp_proxy": checkout_proxy,
            "checkout_proxy_override": checkout_proxy,
            "provider_proxy_override": provider_proxy,
            "provider_parent_only": provider_uses_parent,
            "recreate_checkout_per_attempt": True,
            "approve_proxy_override": approve_proxy,
            "promotion_proxy_override": promotion_proxy,
            "promotion_proxy_country": promotion_country,
            "checkout_proxy_pool": list(proxy_info.get("checkout_proxy_pool") or []),
            "provider_proxy_pool": provider_proxy_pool,
            "approve_proxy_pool": approve_proxy_pool,
            "promotion_proxy_pool": promotion_proxy_pool,
            "prevalidated_proxy_sessions": False,
            "lock_proxy_sessions": False,
            "proxy_quality_gate_enabled": bool(cfg.get("proxy_quality_gate_enabled", True)),
            "proxy_quality_probe_rounds": _clamp_int(cfg.get("proxy_quality_probe_rounds"), 2, 1, 3),
            "proxy_quality_max_latency_ms": _clamp_int(cfg.get("proxy_quality_max_latency_ms"), 12000, 2000, 30000),
            "billing_country": target_country,
            "payment_method_country": payment_method_country,
            "promotion_country": promotion_country,
            "payment_method_country_pool": [payment_method_country],
            "run_mode": "combo_fallback",
            "checkout_ui_mode": str(cfg.get("checkout_ui_mode") or "custom"),
            "payment_locale": str(cfg.get("payment_locale") or "en"),
            "enable_approve_blocked_burst": True,
            "single_proxy_mode": bool(proxy_info.get("single_proxy_mode")),
            "all_jp_proxy": False,
            "approve_same_as_checkout": approve_same_as_checkout,
            "provider_same_as_target": provider_uses_target,
            "residential_via_clash": bool(parent_proxy),
            "residential_clash_proxy": parent_proxy,
            "parent_proxy_rotation_enabled": bool(
                parent_info
                and str(parent_info.get("source") or "") in {"managed_mihomo", "proxy_pool"}
            ),
            "parent_proxy_rotation_task_index": int((parent_info or {}).get("assignment_index") or allocation_index),
            "parent_proxy_rotation_total_threads": _clamp_int(cfg.get("concurrency"), 1, 1, 10),
            "parent_proxy_rotation_slot": max(0, int((parent_info or {}).get("slot") or 1) - 1),
            "parent_proxy_rotation_node_filter": str((parent_info or {}).get("node_name_filter") or ""),
            "parent_proxy_rotation_purpose": "plus_parent_proxy",
            "batch_target_country": provider_country,
            "batch_checkout_proxy_country": checkout_country,
            "batch_approve_proxy_country": approve_country,
            "require_zero_stripe_amount": bool(cfg.get("require_zero_stripe_amount")),
            "require_ipv6_egress": bool(cfg.get("require_ipv6_egress")),
            "checkout_retry_limit": _clamp_int(cfg.get("checkout_retry_limit"), 10, 1, 50),
            "provider_retry_limit": _retry_limit_for_mode(cfg),
            "strict_same_proxy_ip": not provider_uses_target,
            "strict_approve_same_proxy_ip": True,
            "strict_right_same_proxy_ip": right_stage_shared,
            # Stripe init, PM, confirm, poll and redirect are one stateful
            # browser/payment session. Keep the same Provider sticky egress;
            # Promotion/Taxes uses its own ChatGPT-only country session.
            "stripe_poll_egress": "provider",
            "post_approve_poll_timeout_seconds": _clamp_int(
                cfg.get("post_approve_poll_timeout_seconds"), 90, 30, 180
            ),
            "enable_playwright_approve_fallback": bool(
                cfg.get("enable_playwright_approve_fallback", True)
            ),
            "enable_playwright_redirect_fallback": bool(
                cfg.get("enable_playwright_redirect_fallback", True)
            ),
            "allow_intermediate_url_fallback": bool(
                cfg.get("allow_intermediate_url_fallback", False)
            ),
            "paypal_confirm_mode": str(cfg.get("paypal_confirm_mode") or "payment_method"),
            "paypal_confirm_protocol": str(cfg.get("paypal_confirm_protocol") or "standalone"),
            "batch_concurrency": _clamp_int(cfg.get("concurrency"), 1, 1, 10),
            "matrix_index": matrix_index,
            "matrix_total": matrix_total,
        }
        proxy_info["matrix_index"] = matrix_index
        proxy_info["matrix_total"] = matrix_total
        proxy_info["provider_country"] = provider_country
        proxy_info["approve_country"] = approve_country
        proxy_info["promotion_country"] = promotion_country
        return payload, proxy_info

    def _quality_proxy_key(self, stage: str, proxy: str, parent_proxy: str) -> str:
        return _sha(f"{stage}\0{proxy}\0{parent_proxy}")

    def _quality_candidate_key(self, payload: dict[str, Any]) -> str:
        parent_proxy = str(payload.get("residential_clash_proxy") or "")
        return _sha("\0".join([
            str(payload.get("checkout_proxy_override") or ""),
            str(payload.get("provider_proxy_override") or ""),
            str(payload.get("promotion_proxy_override") or ""),
            parent_proxy,
        ]))

    def _check_payload_proxy_quality(
        self,
        payload: dict[str, Any],
        cfg: dict[str, Any],
        *,
        task_id: str,
        email: str,
    ) -> dict[str, Any]:
        engine = _load_plus_engine()
        rounds = _clamp_int(cfg.get("proxy_quality_probe_rounds"), 2, 1, 3)
        max_latency_ms = _clamp_int(cfg.get("proxy_quality_max_latency_ms"), 12000, 2000, 30000)
        quarantine_seconds = _clamp_int(cfg.get("proxy_quality_quarantine_seconds"), 300, 30, 3600)
        parent_proxy = str(payload.get("residential_clash_proxy") or "").strip()
        stages = [
            (
                "checkout",
                "Checkout/Approve",
                str(payload.get("checkout_proxy_override") or ""),
                str(payload.get("batch_checkout_proxy_country") or payload.get("billing_country") or ""),
            ),
            (
                "provider",
                "Provider/PM/Poll",
                str(payload.get("provider_proxy_override") or ""),
                "" if bool(payload.get("provider_parent_only")) else str(payload.get("batch_target_country") or payload.get("payment_method_country") or ""),
            ),
            (
                "promotion",
                "Promotion/Taxes",
                str(payload.get("promotion_proxy_override") or ""),
                str(payload.get("promotion_proxy_country") or payload.get("promotion_country") or ""),
            ),
        ]
        results: list[dict[str, Any]] = []
        seen_ips: dict[str, tuple[str, str]] = {}
        now = time.time()
        with self._lock:
            self._quality_quarantine = {
                key: expires_at
                for key, expires_at in self._quality_quarantine.items()
                if expires_at > now
            }

        for stage, label, proxy, expected_country in stages:
            self._wait_for_task_ready(task_id, email, running=True)
            if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                raise _TaskCancelled("用户取消任务")
            proxy_key = self._quality_proxy_key(stage, proxy, parent_proxy)
            with self._lock:
                quarantine_until = float(self._quality_quarantine.get(proxy_key) or 0)
            if quarantine_until > now:
                error = f"{label} 候选仍在质量隔离期"
                results.append({"stage": stage, "label": label, "ok": False, "country": expected_country, "ip": "", "error": error})
                return {"ok": False, "candidate_id": self._quality_candidate_key(payload), "failed_stage": stage, "error": error, "stages": results}

            stage_ips: list[str] = []
            stage_country = ""
            geo_latencies: list[int] = []
            stage_error = ""
            for round_index in range(1, rounds + 1):
                started_at = time.perf_counter()
                probe = engine.probe_proxy(
                    label,
                    proxy,
                    expected_country,
                    True,
                    parent_proxy,
                    6 if bool(cfg.get("require_ipv6_egress")) else 0,
                )
                geo_latencies.append(int((time.perf_counter() - started_at) * 1000))
                if not bool(getattr(probe, "ok", False)):
                    stage_error = str(getattr(probe, "error", "") or f"{label} 出口检测失败")
                    break
                probe_ip = str(getattr(probe, "ip", "") or "").strip()
                stage_country = str(getattr(probe, "country_code", "") or "").strip().upper()
                if not probe_ip:
                    stage_error = f"{label} 未返回出口 IP"
                    break
                stage_ips.append(probe_ip)
                if round_index > 1 and probe_ip != stage_ips[0]:
                    stage_error = f"{label} sticky IP 漂移: {stage_ips[0]} → {probe_ip}"
                    break

            if not stage_error and geo_latencies and max(geo_latencies) > max_latency_ms:
                stage_error = f"{label} 出口检测延迟过高: {max(geo_latencies)}ms"

            target_result: dict[str, Any] = {}
            if not stage_error:
                target_result = engine.probe_proxy_target(
                    label,
                    proxy,
                    PROXY_QUALITY_TARGETS[stage],
                    parent_proxy=parent_proxy,
                    timeout=max(3, int(max_latency_ms / 1000) + 2),
                    max_latency_ms=max_latency_ms,
                    method="OPTIONS" if stage == "promotion" else "GET",
                    headers=(
                        {
                            "Authorization": f"Bearer {str(payload.get('accessToken') or '').removeprefix('Bearer ').strip()}",
                            "Origin": "https://chatgpt.com",
                            "Referer": "https://chatgpt.com/",
                            "Access-Control-Request-Method": "POST",
                            "Access-Control-Request-Headers": "authorization,content-type,oai-device-id,x-openai-target-path,x-openai-target-route",
                            "x-openai-target-path": "/backend-api/payments/checkout/update",
                            "x-openai-target-route": "/backend-api/payments/checkout/update",
                        }
                        if stage == "promotion"
                        else {}
                    ),
                )
                if not bool(target_result.get("ok")):
                    stage_error = str(target_result.get("error") or f"{label} 目标站不可达")
            stage_ip = stage_ips[0] if stage_ips else ""
            if not stage_error and stage_ip in seen_ips:
                previous_stage, previous_label = seen_ips[stage_ip]
                provider_country = str(
                    payload.get("batch_target_country") or payload.get("payment_method_country") or ""
                ).strip().upper()
                promotion_country = str(
                    payload.get("promotion_proxy_country") or payload.get("promotion_country") or ""
                ).strip().upper()
                provider_promotion_share = (
                    previous_stage == "provider"
                    and stage == "promotion"
                    and bool(provider_country)
                    and provider_country == promotion_country
                )
                if not provider_promotion_share:
                    stage_error = f"{label} 与 {previous_label} 共用出口 {stage_ip}，未形成所需独立出口"
            stage_result = {
                "stage": stage,
                "label": label,
                "ok": not stage_error,
                "country": stage_country or expected_country,
                "expected_country": expected_country,
                "ip": stage_ip,
                "sticky": bool(stage_ips) and len(set(stage_ips)) == 1,
                "rounds": len(stage_ips),
                "geo_latency_ms": max(geo_latencies) if geo_latencies else 0,
                "target_status": int(target_result.get("status_code") or 0),
                "target_latency_ms": int(target_result.get("latency_ms") or 0),
                "error": _compact_plus_log(_redact_log(stage_error), limit=280),
            }
            results.append(stage_result)
            if stage_error:
                with self._lock:
                    self._quality_quarantine[proxy_key] = time.time() + quarantine_seconds
                return {
                    "ok": False,
                    "candidate_id": self._quality_candidate_key(payload),
                    "failed_stage": stage,
                    "error": stage_result["error"],
                    "stages": results,
                }
            seen_ips[stage_ip] = (stage, label)

        return {
            "ok": True,
            "candidate_id": self._quality_candidate_key(payload),
            "failed_stage": "",
            "error": "",
            "stages": results,
        }

    def _precheck_account_eligibility(
        self,
        account: dict[str, Any],
        cfg: dict[str, Any],
        access_token: str,
        *,
        task_id: str,
        email: str,
        task_index: int = 1,
    ) -> dict[str, Any]:
        engine = _load_plus_engine()
        precheck_cfg = _deepcopy(cfg)
        if bool(precheck_cfg.get("country_rotation_enabled")):
            billing_rotation = _country_sequence(
                precheck_cfg.get("billing_country_rotation"),
                DEFAULT_BILLING_COUNTRY_ROTATION,
                BILLING_COUNTRY_CODES,
            )
            promotion_rotation = _country_sequence(
                precheck_cfg.get("proxy_country_rotation"),
                DEFAULT_PROXY_COUNTRY_ROTATION,
                PROMOTION_COUNTRY_CODES,
            )
            precheck_cfg.update(
                {
                    "target_country": precheck_cfg["checkout_country"],
                    "approve_country": precheck_cfg["checkout_country"],
                    "provider_country": billing_rotation[0],
                    "payment_method_country": billing_rotation[0],
                    "promotion_country": promotion_rotation[0],
                }
            )
        precheck_cfg["country_rotation_enabled"] = False
        precheck_cfg = _normalize_config(precheck_cfg)

        last_error = "账号资格检测未执行"
        last_checkout_proxy = ""
        attempt = 0
        while True:
            attempt += 1
            self._wait_for_task_ready(task_id, email, running=True)
            if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                raise _TaskCancelled("用户取消任务")
            self._set_task_result(
                task_id,
                {
                    "status": "running",
                    "stage": "检测账号提链资格",
                    "engine_step": "eligibility",
                    "progress_percent": PLUS_STEP_PROGRESS["eligibility"][0],
                    "progress_label": f"检测账号提链资格 · 第 {attempt} 次",
                    "eligibility_attempt": attempt,
                    "eligibility_attempt_limit": 0,
                },
            )
            payload: dict[str, Any] = {}
            proxy_info: dict[str, Any] = {}
            chatgpt: Any | None = None
            try:
                proxy_attempt_index = max(1, int(task_index or 1)) + attempt - 1
                payload, proxy_info = self._payload_for_account(account, precheck_cfg, proxy_attempt_index)
                payload["accessToken"] = access_token
                last_checkout_proxy = str(payload.get("checkout_proxy_override") or "")
                self._set_task_result(
                    task_id,
                    {
                        "eligibility_proxy_label": str(proxy_info.get("label") or ""),
                        "eligibility_parent_source": str(proxy_info.get("parent_proxy_source") or ""),
                        "eligibility_proxy_slot": int(proxy_info.get("checkout_slot") or proxy_info.get("slot") or 0),
                    },
                )
                request_model = engine.LongLinkRequest.model_validate(payload)
                chatgpt = engine.build_chatgpt_session(
                    request_model,
                    proxy_override=str(payload.get("checkout_proxy_override") or ""),
                )
                result = probe_plus_link_eligibility(access_token, session=chatgpt)
            except _TaskCancelled:
                raise
            except Exception as error:
                last_error = _compact_plus_log(error, limit=280) or "账号资格检测请求失败"
                self._set_task_result(
                    task_id,
                    {
                        "eligibility_last_error": last_error,
                        "eligibility_failure_category": _classify_plus_failure(error),
                    },
                )
                self._wait_for_eligibility_retry(
                    task_id,
                    email,
                    attempt=attempt,
                    task_index=task_index,
                )
                continue
            finally:
                if chatgpt is not None:
                    try:
                        chatgpt.close()
                    except Exception:
                        pass

            if bool(result.get("eligible")):
                result = dict(result)
                result["proxy_label"] = str(proxy_info.get("label") or "")
                self._set_task_result(
                    task_id,
                    {
                        "eligibility_last_error": "",
                        "eligibility_failure_category": "",
                    },
                )
                return result

            reason = str(result.get("reason") or "eligibility_failed")
            last_error = _compact_plus_log(result.get("detail") or reason, limit=280)
            self._set_task_result(
                task_id,
                {
                    "eligibility_last_error": last_error,
                    "eligibility_failure_category": _classify_plus_failure(last_error),
                },
            )
            if bool(result.get("ok")) or reason in {
                "missing_token",
                "token_expired",
                "token_invalid",
                "account_not_free",
                "email_missing",
            }:
                raise RuntimeError(last_error)
            self._wait_for_eligibility_retry(
                task_id,
                email,
                attempt=attempt,
                task_index=task_index,
            )

    def _wait_for_eligibility_retry(
        self,
        task_id: str,
        email: str,
        *,
        attempt: int,
        task_index: int,
    ) -> None:
        # Stagger concurrent cards so a temporary upstream circuit breaker is
        # not hit again by every worker at exactly the same moment.
        delay = min(12.0, 1.5 * (2 ** max(0, min(3, int(attempt) - 1))))
        delay += (max(1, int(task_index or 1)) - 1) % 3 * 0.45
        deadline = time.monotonic() + delay
        self._set_task_result(
            task_id,
            {
                "status": "running",
                "stage": "等待资格检测重试",
                "engine_step": "eligibility",
                "progress_percent": PLUS_STEP_PROGRESS["eligibility"][0],
                "progress_label": f"上游临时异常，{delay:.1f} 秒后自动重试",
            },
        )
        while time.monotonic() < deadline:
            self._wait_for_task_ready(task_id, email, running=True)
            if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                raise _TaskCancelled("用户取消任务")
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def _wait_for_quality_candidate_retry(
        self,
        task_id: str,
        email: str,
        *,
        quality_attempt: int,
    ) -> None:
        delay = min(3.0, 0.6 + (max(1, int(quality_attempt)) % 5) * 0.35)
        deadline = time.monotonic() + delay
        self._set_task_result(
            task_id,
            {
                "status": "running",
                "stage": "更换代理候选",
                "engine_step": "quality_gate",
                "progress_percent": PLUS_STEP_PROGRESS["quality_gate"][0],
                "progress_label": f"候选第 {quality_attempt} 组未通过，正在更换代理",
            },
        )
        while time.monotonic() < deadline:
            self._wait_for_task_ready(task_id, email, running=True)
            if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                raise _TaskCancelled("用户取消任务")
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def _generate_local_long_link(self, payload: dict[str, Any], *, task_id: str, email: str) -> dict[str, Any]:
        engine = _load_plus_engine()

        def emit(step: str, message: str, **extra: Any) -> None:
            self._wait_for_task_ready(task_id, email, running=True)
            safe_message = _compact_plus_log(_redact_log(message))
            step_key = str(step or "").strip()[:80]
            current = self._result_for_task(task_id) or {}
            event_now = _now()
            progress, label = PLUS_STEP_PROGRESS.get(
                step_key,
                (
                    int(current.get("progress_percent") or 0),
                    str(current.get("progress_label") or current.get("stage") or "提链处理中"),
                ),
            )
            updates: dict[str, Any] = {
                "status": "running",
                "stage": label,
                "engine_step": step_key,
                "progress_percent": progress,
                "progress_label": label,
                "last_engine_message": safe_message,
            }
            if step_key != str(current.get("engine_step") or ""):
                updates["last_engine_detail"] = ""
            if extra.get("reset_attempt_diagnostics"):
                for key in (
                    "paypal_availability",
                    "paypal_methods",
                    "paypal_evidence_paths",
                    "stripe_amount",
                    "stripe_amount_source",
                    "currency",
                    "billing_profile_source",
                    "billing_email_domain",
                    "stripe_poll_network_errors",
                    "stripe_confirm_prep_profile",
                    "stripe_confirm_request_profile",
                    "stripe_confirm_response_profile",
                    "stripe_confirm_error_profile",
                    "stripe_submission_profile",
                    "failure_category",
                    "chatgpt_approval_endpoint",
                    "approval_session_state_copied",
                    "last_engine_detail",
                ):
                    updates[key] = None
                for key in COMBO_DIAGNOSTIC_FIELDS:
                    updates[key] = ""
                for key in (
                    "last_redirect_message",
                    "current_proxy_ip",
                    "checkout_proxy_ip",
                    "provider_proxy_ip",
                    "approve_proxy_ip",
                    "promotion_proxy_ip",
                    "left_proxy_ip",
                    "right_proxy_ip",
                    "right_session_expected_ip",
                    "right_session_pre_ip",
                    "right_session_pre_country",
                    "right_session_pre_consistent",
                    "right_session_post_ip",
                    "right_session_post_country",
                    "right_session_post_consistent",
                ):
                    updates[key] = "" if not key.endswith("consistent") else None
                updates["attempt_started_at"] = event_now
                updates["intermediate_url"] = ""
                updates["intermediate_url_present"] = False
                updates["intermediate_fallback_used"] = False
            if extra.get("detail") not in (None, ""):
                updates["last_engine_detail"] = _compact_plus_log(_redact_log(extra.get("detail")), limit=360)
            if step_key == "redirect":
                updates["last_redirect_message"] = _compact_plus_log(_redact_log(message), limit=500)
            for key in (
                "link_attempt_count",
                "link_attempt_limit",
                "proxy_stage",
                "paypal_availability",
                "paypal_methods",
                "paypal_evidence_paths",
                "stripe_amount",
                "stripe_amount_source",
                "currency",
                "billing_profile_source",
                "billing_email_domain",
                "stripe_poll_network_errors",
                "stripe_session_reused",
                "redirect_session_state_copied",
                "stripe_poll_egress",
                "left_sticky_rotated",
                "right_sticky_rotated",
                "stripe_confirm_prep_profile",
                "stripe_confirm_request_profile",
                "stripe_confirm_response_profile",
                "stripe_confirm_error_profile",
                "stripe_submission_profile",
                "failure_category",
                "paypal_confirm_mode",
                "chatgpt_approval_endpoint",
                "approval_session_state_copied",
                "chatgpt_confirm_profile",
                "paypal_confirm_protocol",
                "approve_response",
                "right_session_expected_ip",
                "right_session_pre_ip",
                "right_session_pre_country",
                "right_session_pre_consistent",
                "right_session_post_ip",
                "right_session_post_country",
                "right_session_post_consistent",
                "playwright_approve_fallback_started",
                "playwright_approve_fallback_used",
                "playwright_approve_fallback_error",
                "playwright_fallback_started",
                "playwright_fallback_used",
                "playwright_redirect_host",
                "playwright_redirect_error",
                "intermediate_url",
                "intermediate_url_present",
                "intermediate_fallback_used",
            ):
                if extra.get(key) not in (None, ""):
                    updates[key] = extra.get(key)

            diagnostic_detail = str(updates.get("last_engine_detail") or "")
            updates.update(
                _stage_diagnostic_updates(
                    step_key,
                    safe_message,
                    diagnostic_detail,
                    extra,
                )
            )

            proxy_stage = str(extra.get("proxy_stage") or "").strip().lower()
            proxy_ip = str(extra.get("proxy_ip") or "").strip()
            proxy_country = str(
                extra.get("proxy_country_code")
                or extra.get("proxy_country")
                or extra.get("expected_country")
                or ""
            ).strip().upper()
            if proxy_ip:
                updates["current_proxy_ip"] = proxy_ip
                if proxy_stage == "checkout":
                    updates["checkout_proxy_ip"] = proxy_ip
                    updates["checkout_proxy_country"] = proxy_country or str(current.get("checkout_proxy_country") or current.get("billing_country") or "")
                elif proxy_stage == "provider":
                    updates["provider_proxy_ip"] = proxy_ip
                    updates["provider_proxy_country"] = proxy_country or str(current.get("provider_proxy_country") or current.get("provider_country") or "")
                    updates["left_proxy_ip"] = proxy_ip
                    updates["left_proxy_country"] = proxy_country or str(current.get("left_proxy_country") or current.get("billing_country") or "")
                elif proxy_stage == "approve":
                    updates["approve_proxy_ip"] = proxy_ip
                    updates["approve_proxy_country"] = proxy_country or str(current.get("approve_proxy_country") or current.get("approve_country") or "")
                    updates["left_proxy_ip"] = proxy_ip
                    updates["left_proxy_country"] = proxy_country or str(current.get("left_proxy_country") or current.get("billing_country") or "")
                elif proxy_stage in RIGHT_PROXY_STAGES:
                    updates["promotion_proxy_ip"] = proxy_ip
                    updates["promotion_proxy_country"] = proxy_country or str(current.get("promotion_proxy_country") or current.get("promotion_country") or "")
                    updates["right_proxy_ip"] = proxy_ip
                    updates["right_proxy_country"] = proxy_country or str(current.get("right_proxy_country") or current.get("promotion_country") or "")
            attempt_status = _attempt_event_status(step_key, safe_message, extra)
            if attempt_status:
                record = _attempt_diagnostic_record(current, updates, safe_message, extra, attempt_status)
                updates["attempt_diagnostics_history"] = _merge_attempt_diagnostic(
                    current.get("attempt_diagnostics_history"),
                    record,
                )
            self._set_task_result(task_id, updates)

        req = engine.LongLinkRequest.model_validate(payload)
        with self._lock:
            control = self._task_controls.get(str(task_id)) or {}
            cancel_event = control.get("cancel_event")
        engine.CANCEL_CONTEXT.event = cancel_event if isinstance(cancel_event, threading.Event) else None
        try:
            result = engine.generate_long_link_payload(req, emit=emit)
        finally:
            engine.CANCEL_CONTEXT.event = None
        if hasattr(result, "model_dump"):
            data = result.model_dump()
        elif isinstance(result, dict):
            data = dict(result)
        else:
            data = json.loads(json.dumps(result, default=str, ensure_ascii=False))
        return data if isinstance(data, dict) else {"data": data}

    def _run_one(self, task_id: str, account: dict[str, Any], cfg: dict[str, Any], index: int) -> None:
        email = str(account.get("email") or "").strip()
        while True:
            try:
                self._run_one_cycle(task_id, account, cfg, index)
                return
            except _TaskRetryable as retryable:
                if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                    self._cancel_task(task_id, email, reason="用户取消任务", was_running=True)
                    self._mark_task_settled(task_id)
                    return
                current = self._result_for_task(task_id) or {}
                retry_count = int(current.get("auto_retry_count") or 0) + 1
                delay = min(45.0, 3.0 * (2 ** min(4, retry_count - 1)))
                self._set_task_result(task_id, {
                    "status": "running",
                    "stage": "等待自动重试",
                    "engine_step": "auto_retry",
                    "progress_percent": 2,
                    "progress_label": f"临时失败，{delay:.0f} 秒后自动重试第 {retry_count} 轮",
                    "auto_retry_count": retry_count,
                    "auto_retry_category": retryable.category,
                    "auto_retry_last_error": retryable.detail[:360],
                    "failure_category": retryable.category,
                    "error": retryable.detail[:1200],
                })
                self._append_log(
                    f"[{email}] 临时失败将自动重试第 {retry_count} 轮："
                    f"{retryable.category} / {retryable.detail}",
                    "warning",
                )
                try:
                    self._wait_for_task_auto_retry(
                        task_id,
                        email,
                        delay=delay,
                    )
                except _TaskCancelled:
                    self._cancel_task(task_id, email, reason="用户取消任务", was_running=True)
                    self._mark_task_settled(task_id)
                    return

    def _wait_for_task_auto_retry(
        self,
        task_id: str,
        email: str,
        *,
        delay: float,
    ) -> None:
        deadline = time.monotonic() + max(0.0, float(delay))
        while time.monotonic() < deadline:
            self._wait_for_task_ready(task_id, email, running=True)
            if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                raise _TaskCancelled("用户取消任务")
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def _run_one_cycle(self, task_id: str, account: dict[str, Any], cfg: dict[str, Any], index: int) -> None:
        email = str(account.get("email") or "").strip()
        start_ts = time.time()
        failure_stage = "提链失败"
        if self._stop_event.is_set() or self._task_cancel_requested(task_id):
            self._cancel_task(task_id, email, reason="任务在开始前收到停止请求", was_running=True)
            self._mark_task_settled(task_id)
            return
        try:
            self._wait_for_task_ready(task_id, email, running=False)
            with self._lock:
                control = self._task_controls.get(str(task_id))
                if control is not None:
                    control["started"] = True
            previous_result = self._result_for_task(task_id) or {}
            if not self._set_task_result(task_id, {
                "status": "running",
                "stage": "准备代理",
                "engine_step": "preparing",
                "progress_percent": PLUS_STEP_PROGRESS["preparing"][0],
                "progress_label": PLUS_STEP_PROGRESS["preparing"][1],
                "started_at": str(previous_result.get("started_at") or _now()),
                "last_combo_failure_category": "",
                "failure_category": "",
                "error": "",
            }):
                self._mark_task_settled(task_id)
                return
            normalized_cfg = _normalize_config(cfg)
            extraction_mode = _extraction_mode(normalized_cfg)
            matrix_enabled = extraction_mode == "matrix"
            matrix_total = int(_public_config(normalized_cfg).get("matrix_total") or 1)
            active_retry_limit = _retry_limit_for_mode(normalized_cfg)
            mode_label = "国家矩阵" if matrix_enabled else "固定分阶段"
            failure_stage = "全部国家组合失败" if matrix_enabled else "固定分阶段提链失败"
            working_access_token = _secret_value(account, "access_token", "accessToken", "session_token", "session")
            if bool(cfg.get("refresh_tokens")) and str(account.get("refresh_token") or "").strip():
                self._set_task_result(task_id, {
                    "stage": "刷新令牌",
                    "engine_step": "refresh",
                    "progress_percent": PLUS_STEP_PROGRESS["refresh"][0],
                    "progress_label": PLUS_STEP_PROGRESS["refresh"][1],
                })
                parent_info = self._assign_project_parent_proxy(normalized_cfg, 1)
                refresh_proxy = str((parent_info or {}).get("proxy") or "")
                working_access_token, refreshed, refresh_error = _refresh_access_token_with_fallback(
                    account,
                    working_access_token,
                    proxy=refresh_proxy,
                )
                if refreshed and not account.get("_manual") and not account.get("_partial"):
                    update_registered_account(account, refreshed)
                if refresh_error:
                    self._append_log(
                        f"[{email}] OAuth token 刷新失败，继续使用现有 access_token：{refresh_error}",
                        "warning",
                    )
                    self._set_task_result(task_id, {"refresh_fallback": True})
            if not working_access_token:
                raise RuntimeError("账号缺少 access_token")

            failure_stage = "账号资格或代理预检失败"
            eligibility = self._precheck_account_eligibility(
                account,
                normalized_cfg,
                working_access_token,
                task_id=task_id,
                email=email,
                task_index=index,
            )
            self._set_task_result(
                task_id,
                {
                    "status": "running",
                    "stage": "账号资格检测通过",
                    "engine_step": "eligibility",
                    "progress_percent": PLUS_STEP_PROGRESS["eligibility"][0],
                    "progress_label": "账号资格检测通过",
                    "eligibility_status": "eligible",
                    "eligibility_plan": str(eligibility.get("account_type") or "free"),
                    "account_type": str(eligibility.get("account_type") or "free"),
                    "eligibility_has_email": bool(eligibility.get("has_email")),
                    "eligibility_checked_at": _now(),
                    "extraction_mode": extraction_mode,
                    "active_retry_limit": active_retry_limit,
                },
            )
            self._append_log(
                f"[{email}] AT 提链资格检测通过：plan={eligibility.get('account_type') or 'free'}，模式={mode_label}，本配置重试={active_retry_limit}",
                "success",
            )
            failure_stage = "全部国家组合失败" if matrix_enabled else "固定分阶段提链失败"

            last_combo_error = ""
            failed_combo_count = 0
            skipped_combo_count = 0
            paypal_availability_by_country: dict[str, str] = {}
            billing_rotation = _country_sequence(
                normalized_cfg.get("billing_country_rotation"),
                DEFAULT_BILLING_COUNTRY_ROTATION,
                BILLING_COUNTRY_CODES,
            )
            promotion_rotation = _country_sequence(
                normalized_cfg.get("proxy_country_rotation"),
                DEFAULT_PROXY_COUNTRY_ROTATION,
                PROMOTION_COUNTRY_CODES,
            )
            fixed_checkout_country = _country_in(
                normalized_cfg.get("checkout_country"),
                _country_in(normalized_cfg.get("target_country"), "US", CHECKOUT_COUNTRY_CODES),
                CHECKOUT_COUNTRY_CODES,
            )
            for matrix_index in range(1, matrix_total + 1):
                self._wait_for_task_ready(task_id, email, running=True)
                if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                    raise _TaskCancelled("用户取消任务")
                if matrix_enabled:
                    candidate_provider, candidate_promotion, _, _ = _matrix_country_pair(
                        matrix_index,
                        billing_rotation,
                        promotion_rotation,
                    )
                else:
                    candidate_provider = _country_in(
                        normalized_cfg.get("provider_country"),
                        fixed_checkout_country,
                        PROVIDER_COUNTRY_CODES,
                    )
                    candidate_promotion = _country_in(normalized_cfg.get("promotion_country"), "TR", PROMOTION_COUNTRY_CODES)
                if matrix_enabled and paypal_availability_by_country.get(fixed_checkout_country) == "unsupported":
                    skipped_combo_count += 1
                    last_combo_error = (
                        f"{PAYPAL_UNAVAILABLE_MARKER}: country={fixed_checkout_country}; "
                        "paypal_availability=unsupported; skipped_by_matrix"
                    )
                    self._set_task_result(task_id, {
                        "status": "running",
                        "stage": "跳过无 PayPal 左池组合",
                        "engine_step": "combo_result",
                        "progress_percent": PLUS_STEP_PROGRESS["combo_result"][0],
                        "progress_label": (
                            f"跳过 Provider {candidate_provider} + Promotion {candidate_promotion}："
                            f"固定 Checkout {fixed_checkout_country} 已确认不支持 PayPal"
                        ),
                        "matrix_index": matrix_index,
                        "matrix_total": matrix_total,
                        "matrix_failures": failed_combo_count,
                        "matrix_skipped": skipped_combo_count,
                        "billing_country": fixed_checkout_country,
                        "payment_method_country": candidate_provider,
                        "promotion_country": candidate_promotion,
                        "provider_country": candidate_provider,
                        "approve_country": fixed_checkout_country,
                        "matrix_checkout_country": fixed_checkout_country,
                        "matrix_provider_country": candidate_provider,
                        "matrix_promotion_country": candidate_promotion,
                        "left_proxy_country": fixed_checkout_country,
                        "right_proxy_country": candidate_promotion,
                        "paypal_availability": "unsupported",
                        "paypal_availability_by_country": dict(paypal_availability_by_country),
                        "last_combo_failure_category": "paypal_unsupported_skipped",
                        "failure_category": "paypal_unsupported",
                        "error": last_combo_error,
                    })
                    self._append_log(
                        f"[{email}] 跳过国家组合 {matrix_index}/{matrix_total}："
                        f"Checkout={fixed_checkout_country}, Provider={candidate_provider}, Promotion={candidate_promotion}；"
                        "固定 Checkout 的 Stripe init 已明确无 PayPal",
                        "warning",
                    )
                    continue
                config_label = (
                    f"国家组合 {matrix_index}/{matrix_total}"
                    if matrix_enabled
                    else "固定分阶段配置"
                )
                self._set_task_result(task_id, {
                    "status": "running",
                    "stage": f"准备{config_label}",
                    "engine_step": "preparing",
                    "progress_percent": PLUS_STEP_PROGRESS["preparing"][0],
                    "progress_label": f"准备{config_label}",
                    "matrix_index": matrix_index,
                    "matrix_total": matrix_total,
                    "matrix_checkout_country": fixed_checkout_country,
                    "matrix_provider_country": candidate_provider,
                    "matrix_promotion_country": candidate_promotion,
                    "matrix_failures": failed_combo_count,
                    "matrix_skipped": skipped_combo_count,
                    "link_attempt_count": 0,
                    "link_attempt_limit": active_retry_limit,
                    "extraction_mode": extraction_mode,
                    "active_retry_limit": active_retry_limit,
                    "left_proxy_ip": "",
                    "right_proxy_ip": "",
                    "checkout_proxy_ip": "",
                    "provider_proxy_ip": "",
                    "provider_proxy_country": "",
                    "approve_proxy_ip": "",
                    "approve_proxy_country": "",
                    "promotion_proxy_ip": "",
                    "promotion_proxy_country": "",
                    "long_url": "",
                    "long_url_present": False,
                    "intermediate_url": "",
                    "intermediate_url_present": False,
                    "intermediate_fallback_used": False,
                    "playwright_approve_fallback_started": False,
                    "playwright_approve_fallback_used": False,
                    "playwright_approve_fallback_error": "",
                    "playwright_fallback_started": False,
                    "playwright_fallback_used": False,
                    "playwright_redirect_host": "",
                    "playwright_redirect_error": "",
                    "right_session_expected_ip": "",
                    "right_session_pre_ip": "",
                    "right_session_pre_country": "",
                    "right_session_pre_consistent": None,
                    "right_session_post_ip": "",
                    "right_session_post_country": "",
                    "right_session_post_consistent": None,
                    "last_engine_message": "",
                    "last_engine_detail": "",
                    "last_redirect_message": "",
                    "promotion_error": "",
                    "provider_error": "",
                    "stripe_confirm_error": "",
                    "approve_probe_error": "",
                    "approve_response": "",
                    "paypal_availability": "",
                    "paypal_methods": [],
                    "paypal_evidence_paths": [],
                    "paypal_availability_by_country": dict(paypal_availability_by_country),
                    "quality_gate_status": "pending" if bool(normalized_cfg.get("proxy_quality_gate_enabled")) else "disabled",
                    "quality_gate_attempt": 0,
                    "quality_gate_attempt_limit": 0,
                    "quality_gate_results": [],
                    "quality_gate_history": [],
                    "quality_gate_failure_stage": "",
                    "quality_gate_error": "",
                    "last_combo_failure_category": "",
                    "failure_category": "",
                    "error": "",
                })
                try:
                    quality_enabled = bool(normalized_cfg.get("proxy_quality_gate_enabled"))
                    payload: dict[str, Any] = {}
                    proxy_info: dict[str, Any] = {}
                    quality_result: dict[str, Any] = {}
                    quality_history: list[dict[str, Any]] = []
                    quality_attempt = 0
                    quality_country_mismatches = 0
                    quality_country_mismatch_limit = _clamp_int(
                        normalized_cfg.get("proxy_quality_candidate_limit"), 3, 1, 10
                    )
                    quality_candidate_hard_limit = max(12, quality_country_mismatch_limit * 4)
                    while True:
                        quality_attempt += 1
                        self._wait_for_task_ready(task_id, email, running=True)
                        if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                            raise _TaskCancelled("用户取消任务")
                        proxy_index = (
                            (max(1, int(index or 1)) - 1) * max(1, matrix_total) * 1000
                            + (matrix_index - 1) * 1000
                            + quality_attempt
                        )
                        self._set_task_result(task_id, {
                            "status": "running",
                            "stage": "检测三出口质量" if quality_enabled else f"准备{config_label}",
                            "engine_step": "quality_gate" if quality_enabled else "preparing",
                            "progress_percent": PLUS_STEP_PROGRESS["quality_gate"][0] if quality_enabled else PLUS_STEP_PROGRESS["preparing"][0],
                            "progress_label": f"检测三出口候选第 {quality_attempt} 组（直到通过）" if quality_enabled else f"准备{config_label}",
                            "quality_gate_status": "checking" if quality_enabled else "disabled",
                            "quality_gate_attempt": quality_attempt if quality_enabled else 0,
                            "quality_gate_attempt_limit": quality_candidate_hard_limit if quality_enabled and matrix_enabled else 0,
                            "quality_gate_error": "",
                            "quality_gate_failure_stage": "",
                        })
                        try:
                            payload, proxy_info = self._payload_for_account(
                                account,
                                normalized_cfg,
                                matrix_index,
                                proxy_index=proxy_index,
                            )
                            payload["accessToken"] = working_access_token
                            payload["provider_retry_limit"] = active_retry_limit
                            quality_result = (
                                self._check_payload_proxy_quality(
                                    payload,
                                    normalized_cfg,
                                    task_id=task_id,
                                    email=email,
                                )
                                if quality_enabled
                                else {"ok": True, "candidate_id": self._quality_candidate_key(payload), "stages": []}
                            )
                        except _TaskCancelled:
                            raise
                        except Exception as quality_error:
                            quality_result = {
                                "ok": False,
                                "candidate_id": self._quality_candidate_key(payload) if payload else "",
                                "failed_stage": "allocation",
                                "error": _compact_plus_log(_redact_log(quality_error), limit=280),
                                "stages": [],
                            }
                        quality_history.append({
                            "attempt": quality_attempt,
                            "candidate_id": str(quality_result.get("candidate_id") or ""),
                            "ok": bool(quality_result.get("ok")),
                            "failed_stage": str(quality_result.get("failed_stage") or ""),
                            "error": str(quality_result.get("error") or "")[:280],
                            "stages": list(quality_result.get("stages") or []),
                            "checked_at": _now(),
                        })
                        stage_results = list(quality_result.get("stages") or [])
                        stage_by_name = {
                            str(item.get("stage") or ""): item
                            for item in stage_results
                            if isinstance(item, dict)
                        }
                        failed_stage_name = str(quality_result.get("failed_stage") or "")
                        failed_stage_result = stage_by_name.get(failed_stage_name) or {}
                        expected_quality_country = str(failed_stage_result.get("expected_country") or "").strip().upper()
                        actual_quality_country = str(failed_stage_result.get("country") or "").strip().upper()
                        if (
                            not bool(quality_result.get("ok"))
                            and expected_quality_country
                            and actual_quality_country
                            and expected_quality_country != actual_quality_country
                        ):
                            quality_country_mismatches += 1
                        self._set_task_result(task_id, {
                            "quality_gate_status": "passed" if bool(quality_result.get("ok")) else "retrying",
                            "quality_gate_results": stage_results,
                            "quality_gate_history": quality_history[-20:],
                            "quality_gate_failure_stage": str(quality_result.get("failed_stage") or ""),
                            "quality_gate_error": str(quality_result.get("error") or "")[:360],
                            "quality_gate_candidate_id": str(quality_result.get("candidate_id") or ""),
                            "quality_gate_checked_at": _now(),
                            "checkout_proxy_ip": str((stage_by_name.get("checkout") or {}).get("ip") or ""),
                            "checkout_proxy_country": str((stage_by_name.get("checkout") or {}).get("country") or payload.get("batch_checkout_proxy_country") or ""),
                            "provider_proxy_ip": str((stage_by_name.get("provider") or {}).get("ip") or ""),
                            "provider_proxy_country": str((stage_by_name.get("provider") or {}).get("country") or payload.get("batch_target_country") or ""),
                            "promotion_proxy_ip": str((stage_by_name.get("promotion") or {}).get("ip") or ""),
                            "promotion_proxy_country": str((stage_by_name.get("promotion") or {}).get("country") or payload.get("promotion_proxy_country") or ""),
                        })
                        if bool(quality_result.get("ok")):
                            break
                        self._append_log(
                            f"[{email}] 三出口质量候选第 {quality_attempt} 组未通过，自动更换下一组："
                            f"{quality_result.get('failed_stage') or 'unknown'} / {quality_result.get('error') or '检测失败'}",
                            "warning",
                        )
                        if matrix_enabled and (
                            quality_country_mismatches >= quality_country_mismatch_limit
                            or quality_attempt >= quality_candidate_hard_limit
                        ):
                            mismatch_detail = (
                                f"{failed_stage_result.get('label') or failed_stage_name or '出口'} "
                                f"连续 {quality_country_mismatches} 次国家不匹配："
                                f"期望 {expected_quality_country}，实际 {actual_quality_country}"
                                if quality_country_mismatches >= quality_country_mismatch_limit
                                else f"连续 {quality_attempt} 个质量候选均未通过"
                            )
                            raise _QualityComboUnavailable(mismatch_detail)
                        self._wait_for_quality_candidate_retry(
                            task_id,
                            email,
                            quality_attempt=quality_attempt,
                        )

                    if quality_enabled:
                        # The first engine attempt must use the exact sessions that passed
                        # the gate. Later recovery attempts may rotate sticky sessions.
                        payload["prevalidated_proxy_sessions"] = True
                        payload["checkout_proxy_pool"] = [str(payload.get("checkout_proxy_override") or "")]
                        payload["provider_proxy_pool"] = [str(payload.get("provider_proxy_override") or "")]
                        payload["approve_proxy_pool"] = [str(payload.get("approve_proxy_override") or "")]
                        payload["promotion_proxy_pool"] = [str(payload.get("promotion_proxy_override") or "")]
                        self._append_log(
                            f"[{email}] 三出口质量筛选通过（候选第 {quality_attempt} 组）："
                            + " / ".join(
                                f"{item.get('label')} {item.get('country')} {item.get('ip')}"
                                for item in (quality_result.get("stages") or [])
                                if isinstance(item, dict)
                            ),
                            "success",
                        )
                    self._set_task_result(task_id, {
                        "billing_country": str(payload.get("billing_country") or ""),
                        "payment_method_country": str(payload.get("payment_method_country") or ""),
                        "promotion_country": str(payload.get("promotion_country") or ""),
                        "provider_country": str(proxy_info.get("provider_country") or payload.get("batch_target_country") or ""),
                        "approve_country": str(proxy_info.get("approve_country") or payload.get("batch_approve_proxy_country") or ""),
                        "checkout_proxy_country": str(payload.get("batch_checkout_proxy_country") or payload.get("billing_country") or ""),
                        "left_proxy_country": str(payload.get("batch_checkout_proxy_country") or payload.get("billing_country") or ""),
                        "right_proxy_country": str(payload.get("promotion_proxy_country") or payload.get("promotion_country") or ""),
                        "proxy_label": str(proxy_info.get("label") or ""),
                        "proxy_source": str(proxy_info.get("source") or ""),
                    })
                    data = self._generate_local_long_link(payload, task_id=task_id, email=email)
                    if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                        raise _TaskCancelled("用户取消任务")
                    long_url = str(data.get("long_url") or "").strip()
                    intermediate_url = str(data.get("intermediate_url") or "").strip()
                    if intermediate_url:
                        self._set_task_result(
                            task_id,
                            {
                                "intermediate_url": intermediate_url,
                                "intermediate_url_present": True,
                                "intermediate_url_hash": _sha(intermediate_url),
                                "intermediate_fallback_used": bool(
                                    data.get("intermediate_fallback_used")
                                ),
                                "playwright_fallback_used": bool(
                                    data.get("playwright_fallback_used")
                                ),
                            },
                        )
                    if not bool(data.get("ok")) or not long_url:
                        if intermediate_url:
                            raise RuntimeError("未提取到 PayPal BA；已保留 intermediate_url 备用结果")
                        raise RuntimeError("内置引擎未返回 long_url")
                except _TaskCancelled:
                    raise
                except _QualityComboUnavailable as quality_combo_error:
                    skipped_combo_count += 1
                    last_combo_error = _compact_plus_log(_redact_log(quality_combo_error), limit=360)
                    has_next = matrix_index < matrix_total
                    self._set_task_result(task_id, {
                        "status": "running",
                        "stage": "跳过代理国家不可用组合",
                        "engine_step": "preparing" if has_next else "combo_result",
                        "progress_percent": 0 if has_next else PLUS_STEP_PROGRESS["combo_result"][0],
                        "progress_label": (
                            f"Provider {candidate_provider} + Promotion {candidate_promotion} 质检不可用，切换下一组"
                            if has_next
                            else "全部国家组合质检不可用"
                        ),
                        "matrix_failures": failed_combo_count,
                        "matrix_skipped": skipped_combo_count,
                        "last_combo_failure_category": "proxy_quality_combo_skipped",
                        "failure_category": "proxy_quality_gate",
                        "quality_gate_status": "skipped",
                        "quality_gate_error": last_combo_error,
                        "error": last_combo_error,
                    })
                    self._append_log(
                        f"[{email}] 跳过国家组合 {matrix_index}/{matrix_total}："
                        f"Checkout={fixed_checkout_country}, Provider={candidate_provider}, Promotion={candidate_promotion}；"
                        f"{last_combo_error}",
                        "warning",
                    )
                    continue
                except Exception as combo_error:
                    failed_combo_count += 1
                    last_combo_error = _compact_plus_log(_redact_log(combo_error), limit=360)
                    current_result = self._result_for_task(task_id) or {}
                    failure_category = _result_failure_category(
                        current_result,
                        _classify_plus_failure(combo_error),
                    )
                    availability = str(current_result.get("paypal_availability") or "").strip().lower()
                    if failure_category == "paypal_unsupported":
                        availability = "unsupported"
                    if availability == "supported" or failure_category == "paypal_unsupported":
                        previous = paypal_availability_by_country.get(fixed_checkout_country)
                        if previous != "supported" or availability == "supported":
                            paypal_availability_by_country[fixed_checkout_country] = availability
                    has_next = matrix_index < matrix_total
                    exhausted_label = "全部国家组合失败"
                    history = list(current_result.get("combo_diagnostics_history") or [])
                    history.append(
                        {
                            "matrix_index": matrix_index,
                            "left_country": candidate_provider,
                            "right_country": candidate_promotion,
                            "checkout_country": fixed_checkout_country,
                            "provider_country": candidate_provider,
                            "promotion_country": candidate_promotion,
                            "failure_category": failure_category,
                            "error": last_combo_error[:360],
                            **{
                                key: str(current_result.get(key) or "")[:360]
                                for key in COMBO_DIAGNOSTIC_FIELDS
                            },
                        }
                    )
                    self._set_task_result(task_id, {
                        "status": "running",
                        "stage": "切换下一国家组合" if has_next else exhausted_label,
                        "engine_step": "preparing" if has_next else "combo_result",
                        "progress_percent": 0 if has_next else PLUS_STEP_PROGRESS["combo_result"][0],
                        "progress_label": f"配置 {matrix_index}/{matrix_total} 失败，切换下一组" if has_next else exhausted_label,
                        "matrix_failures": failed_combo_count,
                        "matrix_skipped": skipped_combo_count,
                        "paypal_availability": availability or "unknown",
                        "paypal_availability_by_country": dict(paypal_availability_by_country),
                        "last_combo_failure_category": failure_category,
                        "failure_category": failure_category,
                        "combo_diagnostics_history": history[-50:],
                        "error": last_combo_error[:1200],
                    })
                    if has_next:
                        continue
                    if matrix_enabled:
                        raise RuntimeError(f"全部 {matrix_total} 个国家组合均未提取到长链；最后错误：{last_combo_error}") from combo_error
                    raise RuntimeError(f"固定分阶段配置重试 {active_retry_limit} 次仍未提取到长链；最后错误：{last_combo_error}") from combo_error

                current_result = self._result_for_task(task_id) or {}
                availability = str(current_result.get("paypal_availability") or "").strip().lower()
                if availability in PAYPAL_AVAILABILITY_STATES:
                    paypal_availability_by_country[fixed_checkout_country] = availability
                self._set_task_result(task_id, {
                    "long_url": long_url,
                    "long_url_hash": _sha(long_url),
                    "long_url_present": True,
                })
                completed = self._set_task_result(task_id, {
                    "status": "success",
                    "stage": "完成",
                    "finished_at": _now(),
                    "duration_seconds": round(time.time() - start_ts, 1),
                    "proxy_label": str(proxy_info.get("label") or ""),
                    "proxy_source": str(proxy_info.get("source") or ""),
                    "billing_country": str(data.get("billing_country") or payload.get("billing_country") or ""),
                    "payment_method_country": str(data.get("payment_method_country") or payload.get("payment_method_country") or ""),
                    "promotion_country": str(data.get("promotion_country") or payload.get("promotion_country") or ""),
                    "provider_country": str(proxy_info.get("provider_country") or payload.get("batch_target_country") or ""),
                    "checkout_proxy_country": str(payload.get("batch_checkout_proxy_country") or payload.get("billing_country") or ""),
                    "matrix_index": matrix_index,
                    "matrix_total": matrix_total,
                    "matrix_failures": failed_combo_count,
                    "matrix_skipped": skipped_combo_count,
                    "paypal_availability_by_country": dict(paypal_availability_by_country),
                    "failure_category": "",
                    "processor_entity": str(data.get("processor_entity") or ""),
                    "currency": str(data.get("currency") or ""),
                    "cs_id": str(data.get("cs_id") or ""),
                    "stripe_amount": str(data.get("stripe_amount") or ""),
                    "link_attempt_count": int(data.get("link_attempt_count") or 0),
                    "link_attempt_limit": int(data.get("link_attempt_limit") or active_retry_limit),
                    "stripe_hosted_url": str(data.get("stripe_hosted_url") or ""),
                    "long_url": long_url,
                    "long_url_hash": _sha(long_url),
                    "long_url_present": True,
                    "intermediate_url": "",
                    "intermediate_url_present": False,
                    "intermediate_fallback_used": False,
                    "error": "",
                    "progress_percent": 100,
                    "progress_label": "提链成功",
                })
                if not completed:
                    self._mark_task_settled(task_id)
                    return
                with self._lock:
                    stats = self._state.setdefault("stats", {})
                    stats["success"] = int(stats.get("success") or 0) + 1
                    stats["done"] = int(stats.get("done") or 0) + 1
                    stats["running"] = max(0, int(stats.get("running") or 0) - 1)
                self._bump()
                self._append_log(f"[{email}] Plus 提链成功，模式={mode_label}，配置={matrix_index}/{matrix_total}，耗时 {round(time.time() - start_ts, 1)}s", "success")
                try:
                    from services.paypal_payment_store import paypal_payment_service

                    paypal_payment_service.enqueue_from_plus_result(task_id)
                except Exception as payment_enqueue_error:
                    self._append_log(
                        f"[{email}] BA 已保留，但协议支付自动入队失败：{payment_enqueue_error}",
                        "warning",
                    )
                self._mark_task_settled(task_id)
                return
            if matrix_enabled:
                raise RuntimeError(
                    f"全部 {matrix_total} 个国家组合均未提取到长链；"
                    f"失败={failed_combo_count}，跳过={skipped_combo_count}；最后错误：{last_combo_error}"
                )
        except Exception as error:
            if isinstance(error, _TaskCancelled) or self._task_cancel_requested(task_id):
                self._cancel_task(task_id, email, reason="用户取消任务", was_running=True)
                self._mark_task_settled(task_id)
                return
            compact_error = _compact_plus_log(_redact_log(error), limit=360)
            current_result = self._result_for_task(task_id) or {}
            failure_category = str(
                current_result.get("last_combo_failure_category")
                or current_result.get("failure_category")
                or _classify_plus_failure(error)
            )
            if _is_auto_retryable_failure(failure_category, compact_error):
                raise _TaskRetryable(failure_category, compact_error) from error
            completed = self._set_task_result(task_id, {
                "status": "failed",
                "stage": failure_stage,
                "progress_label": "提链失败",
                "failure_category": failure_category,
                "error": compact_error[:1200],
                "finished_at": _now(),
                "duration_seconds": round(time.time() - start_ts, 1),
            })
            if not completed:
                self._mark_task_settled(task_id)
                return
            with self._lock:
                stats = self._state.setdefault("stats", {})
                stats["fail"] = int(stats.get("fail") or 0) + 1
                stats["done"] = int(stats.get("done") or 0) + 1
                stats["running"] = max(0, int(stats.get("running") or 0) - 1)
            self._bump()
            self._append_log(f"[{email}] Plus 提链失败：{compact_error}", "danger")
            self._mark_task_settled(task_id)

    def _run_task_batch(self, tasks: list[dict[str, Any]]) -> None:
        try:
            _load_plus_engine()
        except Exception as error:
            self._append_log(f"Plus 提链启动失败：{error}", "danger")
            with self._lock:
                self._state["enabled"] = False
                stats = self._state.setdefault("stats", {})
                stats["fail"] = int(stats.get("fail") or 0) + len(tasks)
                stats["done"] = int(stats.get("done") or 0) + len(tasks)
                stats["running"] = 0
                self._save()
            return
        with self._lock:
            active_cfg = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
        pending = list(tasks)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures: dict[Any, dict[str, Any]] = {}
            while futures or pending or self._retry_queue:
                with self._lock:
                    active_cfg = _normalize_config(
                        self._state.get("config") if isinstance(self._state.get("config"), dict) else {}
                    )
                    concurrency = _clamp_int(active_cfg.get("concurrency"), 1, 1, 10)
                    if self._retry_queue:
                        pending.extend(self._retry_queue)
                        self._retry_queue = []
                if self._stop_event.is_set() and pending:
                    self._cancel_queued_tasks(pending, reason="用户停止任务")
                    pending = []
                filtered_pending: list[dict[str, Any]] = []
                for task in pending:
                    pending_result = self._result_for_task(str(task.get("task_id") or ""))
                    if not pending_result:
                        continue
                    if str(pending_result.get("status") or "") in {"success", "failed", "cancelled"}:
                        continue
                    filtered_pending.append(task)
                pending = filtered_pending
                while not self._stop_event.is_set() and pending and len(futures) < concurrency:
                    runnable_index = next(
                        (
                            idx
                            for idx, task in enumerate(pending)
                            if isinstance(task.get("resume_event"), threading.Event)
                            and task["resume_event"].is_set()
                            and isinstance(task.get("cancel_event"), threading.Event)
                            and not task["cancel_event"].is_set()
                        ),
                        None,
                    )
                    if runnable_index is None:
                        break
                    task = pending.pop(runnable_index)
                    with self._lock:
                        task["submitted"] = True
                        stats = self._state.setdefault("stats", {})
                        stats["running"] = int(stats.get("running") or 0) + 1
                        self._save()
                    future = executor.submit(
                        self._run_one,
                        str(task["task_id"]),
                        task["account"],
                        task.get("cfg") if isinstance(task.get("cfg"), dict) else active_cfg,
                        int(task["index"]),
                    )
                    futures[future] = task
                if not futures:
                    if pending:
                        time.sleep(0.2)
                        continue
                    with self._lock:
                        if self._retry_queue:
                            continue
                    break
                completed, _ = wait(set(futures), timeout=0.5, return_when=FIRST_COMPLETED)
                for future in completed:
                    futures.pop(future, None)
                    try:
                        future.result()
                    except Exception as error:
                        self._append_log(f"Plus worker 异常：{error}", "danger")
        successor: threading.Thread | None = None
        with self._lock:
            late_tasks = list(self._retry_queue)
            self._retry_queue = []
            if late_tasks:
                successor = threading.Thread(
                    target=self._run_task_batch,
                    args=(late_tasks,),
                    daemon=True,
                    name="plus-link-continued",
                )
                self._runner = successor
                self._state["enabled"] = True
                successor.start()
            else:
                stats = self._state.setdefault("stats", {})
                stats["running"] = 0
                self._state["enabled"] = False
            self._save()
        if successor is not None:
            return
        self._bump()
        final = self.get(include_secret=False, include_inventory=False, compact=True).get("stats", {})
        self._append_log(
            f"Plus 提链任务结束：成功={final.get('success', 0)}，失败={final.get('fail', 0)}，取消={final.get('cancelled', 0)}",
            "info",
        )
        with self._lock:
            late_tasks = list(self._retry_queue)
            self._retry_queue = []
            if late_tasks:
                successor = threading.Thread(
                    target=self._run_task_batch,
                    args=(late_tasks,),
                    daemon=True,
                    name="plus-link-continued",
                )
                self._runner = successor
                self._state["enabled"] = True
                successor.start()
            elif self._runner is threading.current_thread():
                self._save()
                self._runner = None
                return
            self._save()

    def _register_tasks(self, accounts: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
        tasks = [
            self._new_task(account, cfg, index)
            for index, account in enumerate(accounts, start=1)
        ]
        with self._lock:
            self._state.setdefault("results", []).extend([
                {
                    "task_id": str(task["task_id"]),
                    "email": str(task["email"]),
                    **dict(task.get("account_metadata") or {}),
                    "status": "queued",
                    "stage": "排队中",
                    "engine_step": "queued",
                    "progress_percent": 0,
                    "progress_label": "排队中",
                    "matrix_index": 1,
                    "matrix_total": _public_config(cfg).get("matrix_total", 1),
                    "link_attempt_count": 0,
                    "link_attempt_limit": _retry_limit_for_mode(cfg),
                    "extraction_mode": _extraction_mode(cfg),
                    "active_retry_limit": _retry_limit_for_mode(cfg),
                    "task_left_countries": list(cfg.get("billing_country_rotation") or []),
                    "task_right_countries": list(cfg.get("proxy_country_rotation") or []),
                    "task_checkout_country": str(cfg.get("checkout_country") or "US"),
                    "retryable": True,
                    "created_at": _now(),
                    "updated_at": _now(),
                }
                for task in tasks
            ])
            stats = self._state.setdefault("stats", {})
            stats["total"] = len([item for item in self._state.get("results", []) if isinstance(item, dict)])
            stats["updated_at"] = _now()
            self._save()
        return tasks

    def _runner_main(self, accounts: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
        tasks = self._register_tasks(accounts, cfg)
        self._run_task_batch(tasks)

    def start(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options if isinstance(options, dict) else {}
        config_updates = options.get("config") if isinstance(options.get("config"), dict) else {}
        with self._lock:
            stored_cfg = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
            cfg = _normalize_config({**stored_cfg, **config_updates}) if config_updates else stored_cfg
            if config_updates and options.get("persist_config", True):
                self._state["config"] = cfg
                self._save()
        accounts = self._select_accounts(
            options.get("emails") if isinstance(options.get("emails"), list) else None,
            partial_accounts=options.get("partial_accounts") if isinstance(options.get("partial_accounts"), list) else None,
            manual_accounts=options.get("manual_accounts") if isinstance(options.get("manual_accounts"), list) else None,
            manual_tokens=options.get("manual_tokens") if isinstance(options.get("manual_tokens"), list) else None,
        )
        if not accounts:
            self._append_log("没有可提链账号：请从库存/半成品选择账号，或粘贴 access_token/session", "warning")
            return self._operation_response()
        with self._lock:
            runner_alive = bool(self._runner and self._runner.is_alive())
            if not runner_alive and options.get("clear_results", True):
                self._state["results"] = []
                self._state["logs"] = []
                self._task_controls = {}
                self._retry_queue = []
            now = _now()
            if not runner_alive:
                if options.get("clear_results", True):
                    self._state["stats"] = {**_default_state()["stats"]}
                else:
                    self._recalculate_stats_locked()
                self._state.setdefault("stats", {})["started_at"] = now
                self._state.setdefault("stats", {})["updated_at"] = now
            self._state["enabled"] = True
            self._stop_event.clear()
            self._save()
        tasks = self._register_tasks(accounts, cfg)
        new_runner: threading.Thread | None = None
        with self._lock:
            runner_alive = bool(self._runner and self._runner.is_alive())
            if runner_alive:
                self._retry_queue.extend(tasks)
            else:
                new_runner = threading.Thread(
                    target=self._run_task_batch,
                    args=(tasks,),
                    daemon=True,
                    name="plus-link",
                )
                self._runner = new_runner
                new_runner.start()
            self._state["enabled"] = True
            self._save()
        mode = _extraction_mode(cfg)
        matrix_total = int(_public_config(cfg).get("matrix_total") or 1)
        mode_label = "国家矩阵" if mode == "matrix" else "固定分阶段"
        self._append_log(
            f"Plus 提链任务{'已添加' if runner_alive else '已启动'}：账号={len(accounts)}，并发={cfg.get('concurrency')}，模式={mode_label}，国家组合={matrix_total}，单组合重试={_retry_limit_for_mode(cfg)}，出口模式={'IPv6' if cfg.get('require_ipv6_egress') else '自动'}",
            "success",
        )
        return self._operation_response()


plus_link_service = PlusLinkService(PLUS_LINK_FILE)
