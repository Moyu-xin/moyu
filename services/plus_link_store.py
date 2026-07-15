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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from services import partial_account_store, proxy_pool_store
from services.account_inventory_service import probe_plus_link_eligibility
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.registered_account_store import account_matches_selector, account_pool, list_registered_accounts, update_registered_account

try:
    from services.k12_inventory_service import _refresh_platform_tokens
except Exception:  # pragma: no cover - optional runtime helper
    _refresh_platform_tokens = None  # type: ignore[assignment]


PLUS_LINK_FILE = DATA_DIR / "plus_link.json"
PLUS_ENGINE_APP = Path(__file__).resolve().parent / "plus_engine" / "app.py"
PLUS_ENGINE_DATA_DIR = DATA_DIR / "plus_engine"
PROVIDER_COUNTRY_CODES = {"US", "DE", "FR", "IT", "ES", "NL", "CA", "AU", "GB"}
BILLING_COUNTRY_CODES = PROVIDER_COUNTRY_CODES
PROMOTION_COUNTRY_CODES = {"JP", "TR", "BR", "VN"}
RESIDENTIAL_PROXY_COUNTRY_CODES = BILLING_COUNTRY_CODES | PROMOTION_COUNTRY_CODES
DEFAULT_BILLING_COUNTRY_ROTATION = ["US", "DE", "FR", "IT", "ES", "NL", "CA", "AU", "GB"]
DEFAULT_PROXY_COUNTRY_ROTATION = ["JP", "TR", "BR", "VN"]
PAID_PLAN_TYPES = {"plus", "pro", "team", "business", "enterprise"}
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
    "preparing": (2, "准备矩阵代理"),
    "refresh": (5, "刷新令牌"),
    "run": (6, "启动提链链路"),
    "eligibility": (8, "检测账号提链资格"),
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
LEFT_PROXY_STAGES = {"checkout", "provider", "approve"}
RIGHT_PROXY_STAGES = {"promotion", "update", "poll", "redirect"}


class _TaskCancelled(RuntimeError):
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
    text = re.sub(r"(https?://[^\s]+)", lambda m: m.group(1)[:160] + ("..." if len(m.group(1)) > 160 else ""), text)
    return text[:800]


def _compact_proxy_reason(text: str) -> str:
    reasons: list[str] = []
    checks = [
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
    providers = provider_rotation or list(DEFAULT_BILLING_COUNTRY_ROTATION)
    promotions = promotion_rotation or list(DEFAULT_PROXY_COUNTRY_ROTATION)
    total = len(providers) * len(promotions)
    slot = (max(1, int(index or 1)) - 1) % total
    provider_country = providers[slot // len(promotions)]
    promotion_country = promotions[slot % len(promotions)]
    return provider_country, promotion_country, slot + 1, total


def _extraction_mode(cfg: dict[str, Any]) -> str:
    return "matrix" if bool(cfg.get("country_rotation_enabled")) else "fixed_pair"


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
        ):
            continue
        if item not in portable:
            portable.append(item)
    return portable


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
        BILLING_COUNTRY_CODES,
    )
    default_provider = _country_in(
        cfg.get("payment_method_country") or cfg.get("target_country"),
        "US",
        BILLING_COUNTRY_CODES,
    )
    default_promotion = _country_in(
        cfg.get("promotion_country"),
        "JP",
        PROMOTION_COUNTRY_CODES,
    )
    stage_defaults = (
        ("residential_checkout_proxy", default_checkout),
        ("residential_provider_proxy", default_provider),
        ("residential_approve_proxy", default_provider),
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
            rotated_username = f"{rotated_username}-session-{session_id}-sessTime-5-sessAuto-1"
        return _proxy_url_with_auth(parsed, rotated_username, password)
    # 如果用户已显式写了 {session} 模板，避免再改写密码。
    if any(token in str(proxy) for token in ("{session}", "{session_id}", "{sticky}")):
        return normalized
    match = re.match(r"^(?P<base>.+?)-(?P<country>global|[A-Za-z]{2,3})(?:-[A-Za-z0-9]+)?$", password, flags=re.IGNORECASE)
    password_base = match.group("base") if match else password
    return _proxy_url_with_auth(parsed, username, f"{password_base}-{country}-{session_id}")


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
        "residential_via_project_proxy": True,
        "residential_parent_node_filter": "",
        "residential_via_clash": True,
        "residential_clash_proxy": "",
        "proxy": "",
        "target_country": "US",
        "checkout_country": "US",
        "promotion_country": "JP",
        "payment_method_country": "US",
        "payment_locale": "en",
        "concurrency": 1,
        "timeout_seconds": 360,
        "checkout_retry_limit": 10,
        "provider_retry_limit": 15,
        "matrix_combo_retry_limit": 1,
        "retry_limits_version": 2,
        "refresh_tokens": False,
        "provider_same_as_target": False,
        "approve_same_as_checkout": True,
        "country_rotation_enabled": True,
        "billing_country_rotation": list(DEFAULT_BILLING_COUNTRY_ROTATION),
        "proxy_country_rotation": list(DEFAULT_PROXY_COUNTRY_ROTATION),
        "require_zero_stripe_amount": True,
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
    cfg["residential_via_project_proxy"] = bool(cfg.get("residential_via_project_proxy", True))
    cfg["residential_parent_node_filter"] = str(cfg.get("residential_parent_node_filter") or "").strip()[:160]
    cfg["residential_via_clash"] = cfg["residential_via_project_proxy"]
    # Runtime parent proxies are assigned from the project's proxy configuration.
    # Do not persist or reuse the legacy manually entered Clash address.
    cfg["residential_clash_proxy"] = ""
    cfg["proxy"] = proxy_pool_store.normalize_proxy_url(str(cfg.get("proxy") or "").strip())
    cfg["target_country"] = _country_in(cfg.get("target_country"), "US", BILLING_COUNTRY_CODES)
    cfg["promotion_country"] = _country_in(cfg.get("promotion_country"), "JP", PROMOTION_COUNTRY_CODES)
    cfg["checkout_country"] = _country_in(cfg.get("checkout_country"), cfg["target_country"], BILLING_COUNTRY_CODES)
    cfg["payment_method_country"] = _country_in(cfg.get("payment_method_country"), cfg["target_country"], BILLING_COUNTRY_CODES)
    cfg["payment_locale"] = str(cfg.get("payment_locale") or "en").strip() or "en"
    cfg["concurrency"] = _clamp_int(cfg.get("concurrency"), 1, 1, 10)
    cfg["timeout_seconds"] = _clamp_int(cfg.get("timeout_seconds"), 360, 30, 1800)
    cfg["checkout_retry_limit"] = _clamp_int(cfg.get("checkout_retry_limit"), 10, 1, 50)
    cfg["matrix_combo_retry_limit"] = _clamp_int(cfg.get("matrix_combo_retry_limit"), 1, 1, 20)
    legacy_retry_limits = isinstance(raw, dict) and _clamp_int(raw.get("retry_limits_version"), 0, 0, 2) < 2
    fixed_pair_retry_limit = _clamp_int(cfg.get("provider_retry_limit"), 15, 1, 20)
    if legacy_retry_limits and fixed_pair_retry_limit == cfg["matrix_combo_retry_limit"]:
        fixed_pair_retry_limit = 15
    cfg["provider_retry_limit"] = fixed_pair_retry_limit
    cfg["retry_limits_version"] = 2
    cfg["country_rotation_enabled"] = True if cfg.get("country_rotation_enabled") is None else bool(cfg.get("country_rotation_enabled"))
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
    if cfg["country_rotation_enabled"] and cfg["billing_country_rotation"] == ["US", "DE", "GB"]:
        cfg["billing_country_rotation"] = list(DEFAULT_BILLING_COUNTRY_ROTATION)
    raw_proxy_text = "/".join(str(item).upper() for item in raw_proxy_rotation) if isinstance(raw_proxy_rotation, list) else str(raw_proxy_rotation or "").upper()
    if cfg["country_rotation_enabled"] and "TH" in raw_proxy_text:
        cfg["proxy_country_rotation"] = list(DEFAULT_PROXY_COUNTRY_ROTATION)
    for key in (
        "refresh_tokens",
        "provider_same_as_target",
        "approve_same_as_checkout",
        "require_zero_stripe_amount",
        "strict_same_proxy_ip",
        "strict_approve_same_proxy_ip",
    ):
        cfg[key] = bool(cfg.get(key))
    if cfg["country_rotation_enabled"]:
        cfg["provider_same_as_target"] = False
        cfg["approve_same_as_checkout"] = True
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
        if item.get("country_rotation_enabled")
        else 1
    )
    return item


def _public_result(item: dict[str, Any], *, include_secret: bool = True) -> dict[str, Any]:
    result = dict(item)
    long_url = str(result.get("long_url") or "")
    result["long_url_present"] = bool(long_url or result.get("long_url_present"))
    result["long_url_hash"] = str(result.get("long_url_hash") or _sha(long_url))
    if result.get("error"):
        result["error"] = _compact_plus_log(_redact_log(result.get("error")), limit=360)
    if not include_secret:
        result.pop("long_url", None)
    return result


def _public_account_summary() -> dict[str, Any]:
    pool = account_pool()
    # Plus 页面使用和“账号库存”同一份库存数据。这里仅移除完整 token，
    # 保留类型、测活、额度、sub2api、空间等展示字段，避免两个页面看起来不一致。
    accounts = [_with_effective_account_plan(item) for item in list_registered_accounts(include_secret=False)]
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
        self._state = self._load()
        recovered = 0
        for item in self._state.get("results", []):
            if not isinstance(item, dict) or str(item.get("status") or "") not in {"queued", "running", "paused", "pausing", "cancelling"}:
                continue
            item.update({
                "status": "cancelled",
                "stage": "服务重启后已结束",
                "progress_label": "任务已结束",
                "error": "服务重启时未发现对应执行线程",
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

    def get(self, *, include_secret: bool = True) -> dict[str, Any]:
        with self._lock:
            snap = _deepcopy(self._state)
            runner_alive = bool(self._runner and self._runner.is_alive())
        snap["enabled"] = bool(snap.get("enabled")) and runner_alive
        snap["config"] = _public_config(snap.get("config") if isinstance(snap.get("config"), dict) else {})
        snap["results"] = [_public_result(item, include_secret=include_secret) for item in snap.get("results") or [] if isinstance(item, dict)]
        snap["logs"] = [
            {**item, "text": _compact_plus_log(_redact_log(item.get("text")), limit=260)}
            for item in (snap.get("logs") or [])
            if isinstance(item, dict)
        ]
        snap["account_pool"] = _public_account_summary()
        try:
            snap["partial_account_pool"] = partial_account_store.partial_account_pool(include_secret=False)
        except Exception:
            snap["partial_account_pool"] = {"items": [], "stats": {"total": 0}}
        try:
            snap["proxy_pool"] = proxy_pool_store.get_proxy_pool_config()
        except Exception:
            snap["proxy_pool"] = {}
        return snap

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cfg = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
            for key, value in updates.items():
                if key not in cfg:
                    continue
                cfg[key] = value
            self._state["config"] = _normalize_config(cfg)
            self._save()
        return self.get()

    def clear_results(self) -> dict[str, Any]:
        with self._lock:
            self._state["results"] = []
            self._state["logs"] = []
            self._state["stats"] = {**_default_state()["stats"]}
            self._save()
        return self.get()

    def clear_failed_results(self) -> dict[str, Any]:
        return self._clear_terminal_results({"failed", "cancelled"})

    def clear_finished_results(self) -> dict[str, Any]:
        return self._clear_terminal_results({"success", "failed", "cancelled"})

    def _clear_terminal_results(self, statuses: set[str]) -> dict[str, Any]:
        with self._lock:
            results = [item for item in self._state.get("results", []) if isinstance(item, dict)]
            self._state["results"] = [
                item for item in results
                if str(item.get("status") or "") not in statuses
            ]
            remaining = self._state["results"]
            success = sum(1 for item in remaining if str(item.get("status") or "") == "success")
            failed = sum(1 for item in remaining if str(item.get("status") or "") == "failed")
            cancelled = sum(1 for item in remaining if str(item.get("status") or "") == "cancelled")
            running = sum(
                1 for item in remaining
                if str(item.get("status") or "") in {"queued", "running", "paused", "pausing", "cancelling"}
            )
            stats = self._state.setdefault("stats", {})
            stats.update({
                "success": success,
                "fail": failed,
                "cancelled": cancelled,
                "done": success + failed + cancelled,
                "running": running,
                "total": len(remaining),
                "updated_at": _now(),
            })
            if not remaining:
                stats["elapsed_seconds"] = 0
                stats["started_at"] = ""
            self._save()
        return self.get()

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            for control in self._task_controls.values():
                cancel_event = control.get("cancel_event")
                if isinstance(cancel_event, threading.Event):
                    cancel_event.set()
            self._state["enabled"] = False
            self._save()
        self._append_log("Plus 提链任务已请求停止：不再提交新账号，等待当前任务结束", "warning")
        return self.get()

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

    def _set_task_result(self, task_id: str, updates: dict[str, Any]) -> None:
        with self._lock:
            results = self._state.setdefault("results", [])
            for item in results:
                if isinstance(item, dict) and str(item.get("task_id")) == str(task_id):
                    item.update(updates)
                    item["updated_at"] = _now()
                    break
            self._save()

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
        suffix = f"-{random.randrange(1000, 9999)}" if retry_of else ""
        task_id = str(task_id or f"plus-{stamp}-{max(1, int(index or 1))}{suffix}")
        resume_event = threading.Event()
        resume_event.set()
        task = {
            "task_id": task_id,
            "account": account,
            "cfg": _deepcopy(cfg),
            "index": max(1, int(index or 1)),
            "email": str(account.get("email") or "").strip(),
            "retry_of": str(retry_of or ""),
            "resume_event": resume_event,
            "cancel_event": threading.Event(),
            "started": False,
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
        return self.get()

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
        return self.get()

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        with self._lock:
            control = self._task_controls.get(task_id)
            result = self._result_for_task(task_id)
            if not control or not result:
                raise RuntimeError("Plus 提链任务不存在或服务已重启")
            status = str(result.get("status") or "")
            if status in {"success", "failed", "cancelled"}:
                return self.get()
            cancel_event = control.get("cancel_event")
            if isinstance(cancel_event, threading.Event):
                cancel_event.set()
            started = bool(control.get("started"))
            email = str(result.get("email") or control.get("email") or task_id)
            if started:
                result.update({"status": "cancelling", "stage": "正在取消", "progress_label": "正在取消", "updated_at": _now()})
                self._save()
            else:
                self._cancel_task(task_id, email, reason="用户取消任务", was_running=False)
        return self.get()

    def retry_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        with self._lock:
            previous = self._task_controls.get(task_id)
            result = self._result_for_task(task_id)
            if not result:
                raise RuntimeError("Plus 提链任务不存在")
            if str(result.get("status") or "") not in {"success", "failed", "cancelled"}:
                raise RuntimeError("任务尚未结束，不能重试")
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
            result.clear()
            result.update({
                "task_id": task_id,
                "email": str(task["email"]),
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
                "retry_count": retry_count,
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
            self._save()
            runner_alive = bool(self._runner and self._runner.is_alive())
        if not runner_alive:
            self._stop_event.clear()
            self._runner = threading.Thread(target=self._run_task_batch, args=([],), daemon=True, name="plus-link-retry")
            self._runner.start()
        return self.get()

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
            left_pool_used = checkout_pool or provider_pool or approve_pool or legacy_portable
            promotion_pool_used = promotion_pool or legacy_portable
        if not bool(cfg.get("use_residential_proxy_pool")) or not (left_pool_used and promotion_pool_used):
            return None
        safe_index = max(1, int(index or 1))

        def pick(pool: list[str]) -> tuple[str, int, int]:
            if not pool:
                return "", 0, 0
            slot = random.randrange(len(pool))
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

    def _assign_project_parent_proxy(self, cfg: dict[str, Any], index: int) -> dict[str, Any] | None:
        if not bool(cfg.get("residential_via_project_proxy")):
            return None
        concurrency = _clamp_int(cfg.get("concurrency"), 1, 1, 10)
        assignment = proxy_pool_store.assign_proxy(
            index,
            fallback="",
            total_threads=concurrency,
            slot_index=(max(1, int(index or 1)) - 1) % concurrency,
            node_name_filter=str(cfg.get("residential_parent_node_filter") or ""),
            validate_node=False,
            purpose="plus_parent_proxy",
        )
        parent_proxy = proxy_pool_store.normalize_proxy_url(str(assignment.get("proxy") or "").strip())
        if not parent_proxy:
            detail = _compact_plus_log(assignment.get("error") or assignment.get("label") or "项目代理配置没有可用节点")
            raise RuntimeError(f"住宅代理父级分配失败：{detail}")
        return {
            "proxy": parent_proxy,
            "source": str(assignment.get("source") or "project_proxy_pool"),
            "label": str(assignment.get("label") or "项目代理节点"),
            "slot": int(assignment.get("slot") or 0),
            "node_id": str(assignment.get("node_id") or ""),
        }

    def _payload_for_account(self, account: dict[str, Any], cfg: dict[str, Any], index: int) -> tuple[dict[str, Any], dict[str, Any]]:
        target_country = _country_in(cfg.get("target_country"), "US", BILLING_COUNTRY_CODES)
        promotion_country = _country_in(cfg.get("promotion_country"), "JP", PROMOTION_COUNTRY_CODES)
        checkout_country = target_country
        payment_method_country = _country_in(cfg.get("payment_method_country"), target_country, BILLING_COUNTRY_CODES)
        matrix_index = 1
        matrix_total = 1
        if bool(cfg.get("country_rotation_enabled")):
            billing_rotation = _country_sequence(cfg.get("billing_country_rotation"), DEFAULT_BILLING_COUNTRY_ROTATION, BILLING_COUNTRY_CODES)
            proxy_rotation = _country_sequence(cfg.get("proxy_country_rotation"), DEFAULT_PROXY_COUNTRY_ROTATION, PROMOTION_COUNTRY_CODES)
            target_country, promotion_country, matrix_index, matrix_total = _matrix_country_pair(
                index,
                billing_rotation,
                proxy_rotation,
            )
            payment_method_country = target_country
            checkout_country = target_country
        provider_uses_target = False
        approve_same_as_checkout = True
        provider_country = checkout_country
        proxy_info = self._assign_residential_proxy(
            cfg,
            index,
            left_country=checkout_country,
            promotion_country=promotion_country,
        )
        if proxy_info is None:
            raise RuntimeError(
                "Plus 提链必须配置独立住宅代理："
                f"左池 {checkout_country} 与右池 {promotion_country} 均需要可用代理或 Plus 专用公共模板"
            )
        parent_info = self._assign_project_parent_proxy(cfg, index)
        parent_proxy = str((parent_info or {}).get("proxy") or "").strip()
        if parent_info:
            proxy_info["parent_proxy"] = parent_proxy
            proxy_info["parent_proxy_source"] = str(parent_info.get("source") or "")
            proxy_info["parent_proxy_label"] = str(parent_info.get("label") or "")
            proxy_info["parent_proxy_slot"] = int(parent_info.get("slot") or 0)
            proxy_info["label"] = f"{parent_info.get('label') or '项目代理'} → {proxy_info.get('label') or '住宅代理'}"
        proxy = str(proxy_info.get("proxy") or "").strip()
        checkout_proxy = str(proxy_info.get("checkout_proxy") or proxy).strip()
        provider_proxy = str(proxy_info.get("provider_proxy") or proxy).strip()
        approve_proxy = str(proxy_info.get("approve_proxy") or (checkout_proxy if approve_same_as_checkout else provider_proxy)).strip()
        promotion_proxy = str(proxy_info.get("promotion_proxy") or proxy).strip()
        access_token = _secret_value(account, "access_token", "accessToken", "session_token", "session")
        payload = {
            "accessToken": access_token,
            "proxy": checkout_proxy,
            "jp_proxy": checkout_proxy,
            "checkout_proxy_override": checkout_proxy,
            "provider_proxy_override": provider_proxy,
            "approve_proxy_override": approve_proxy,
            "promotion_proxy_override": promotion_proxy,
            "promotion_proxy_country": promotion_country,
            "checkout_proxy_pool": list(proxy_info.get("checkout_proxy_pool") or []),
            "provider_proxy_pool": list(proxy_info.get("provider_proxy_pool") or []),
            "approve_proxy_pool": list(proxy_info.get("approve_proxy_pool") or []),
            "promotion_proxy_pool": list(proxy_info.get("promotion_proxy_pool") or []),
            "billing_country": target_country,
            "payment_method_country": payment_method_country,
            "promotion_country": promotion_country,
            "payment_method_country_pool": [payment_method_country],
            "run_mode": "combo_fallback",
            "checkout_ui_mode": "custom",
            "payment_locale": str(cfg.get("payment_locale") or "en"),
            "enable_approve_blocked_burst": True,
            "single_proxy_mode": bool(proxy_info.get("single_proxy_mode")),
            "all_jp_proxy": False,
            "approve_same_as_checkout": approve_same_as_checkout,
            "provider_same_as_target": provider_uses_target,
            "residential_via_clash": bool(parent_proxy),
            "residential_clash_proxy": parent_proxy,
            "batch_target_country": payment_method_country,
            "batch_checkout_proxy_country": checkout_country,
            "require_zero_stripe_amount": bool(cfg.get("require_zero_stripe_amount")),
            "checkout_retry_limit": _clamp_int(cfg.get("checkout_retry_limit"), 10, 1, 50),
            "provider_retry_limit": _retry_limit_for_mode(cfg),
            "strict_same_proxy_ip": True,
            "strict_approve_same_proxy_ip": bool(cfg.get("strict_approve_same_proxy_ip")),
            "matrix_index": matrix_index,
            "matrix_total": matrix_total,
        }
        proxy_info["matrix_index"] = matrix_index
        proxy_info["matrix_total"] = matrix_total
        proxy_info["provider_country"] = provider_country
        proxy_info["promotion_country"] = promotion_country
        return payload, proxy_info

    def _precheck_account_eligibility(
        self,
        account: dict[str, Any],
        cfg: dict[str, Any],
        access_token: str,
        *,
        task_id: str,
        email: str,
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
                    "target_country": billing_rotation[0],
                    "checkout_country": billing_rotation[0],
                    "payment_method_country": billing_rotation[0],
                    "promotion_country": promotion_rotation[0],
                }
            )
        precheck_cfg["country_rotation_enabled"] = False
        precheck_cfg = _normalize_config(precheck_cfg)

        max_attempts = min(3, _clamp_int(cfg.get("checkout_retry_limit"), 10, 1, 50))
        last_error = "账号资格检测未执行"
        for attempt in range(1, max_attempts + 1):
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
                    "progress_label": f"检测账号提链资格 {attempt}/{max_attempts}",
                    "eligibility_attempt": attempt,
                    "eligibility_attempt_limit": max_attempts,
                },
            )
            try:
                payload, proxy_info = self._payload_for_account(account, precheck_cfg, attempt)
                payload["accessToken"] = access_token
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
                if attempt < max_attempts:
                    continue
                break

            if bool(result.get("eligible")):
                result = dict(result)
                result["proxy_label"] = str(proxy_info.get("label") or "")
                return result

            reason = str(result.get("reason") or "eligibility_failed")
            last_error = _compact_plus_log(result.get("detail") or reason, limit=280)
            if bool(result.get("ok")) or reason in {
                "missing_token",
                "token_expired",
                "token_invalid",
                "account_not_free",
                "email_missing",
            }:
                raise RuntimeError(last_error)
            if attempt >= max_attempts:
                break

        raise RuntimeError(f"账号提链资格检测连续失败 {max_attempts} 次：{last_error}")

    def _generate_local_long_link(self, payload: dict[str, Any], *, task_id: str, email: str) -> dict[str, Any]:
        engine = _load_plus_engine()

        def emit(step: str, message: str, **extra: Any) -> None:
            self._wait_for_task_ready(task_id, email, running=True)
            safe_message = _compact_plus_log(_redact_log(message))
            step_key = str(step or "").strip()[:80]
            current = self._result_for_task(task_id) or {}
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
            }
            for key in ("link_attempt_count", "link_attempt_limit", "proxy_stage"):
                if extra.get(key) not in (None, ""):
                    updates[key] = extra.get(key)

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
                if proxy_stage in LEFT_PROXY_STAGES:
                    updates["left_proxy_ip"] = proxy_ip
                    updates["left_proxy_country"] = proxy_country or str(current.get("left_proxy_country") or current.get("billing_country") or "")
                elif proxy_stage in RIGHT_PROXY_STAGES:
                    updates["right_proxy_ip"] = proxy_ip
                    updates["right_proxy_country"] = proxy_country or str(current.get("right_proxy_country") or current.get("promotion_country") or "")
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
        start_ts = time.time()
        failure_stage = "提链失败"
        if self._stop_event.is_set() or self._task_cancel_requested(task_id):
            self._cancel_task(task_id, email, reason="任务在开始前收到停止请求", was_running=True)
            return
        try:
            self._wait_for_task_ready(task_id, email, running=False)
            with self._lock:
                control = self._task_controls.get(str(task_id))
                if control is not None:
                    control["started"] = True
            self._set_task_result(task_id, {
                "status": "running",
                "stage": "准备代理",
                "engine_step": "preparing",
                "progress_percent": PLUS_STEP_PROGRESS["preparing"][0],
                "progress_label": PLUS_STEP_PROGRESS["preparing"][1],
                "started_at": _now(),
            })
            normalized_cfg = _normalize_config(cfg)
            extraction_mode = _extraction_mode(normalized_cfg)
            matrix_enabled = extraction_mode == "matrix"
            matrix_total = int(_public_config(normalized_cfg).get("matrix_total") or 1)
            active_retry_limit = _retry_limit_for_mode(normalized_cfg)
            mode_label = "矩阵" if matrix_enabled else "固定双 IP"
            failure_stage = "全部矩阵配置失败" if matrix_enabled else "固定双 IP 配置失败"
            working_access_token = _secret_value(account, "access_token", "accessToken", "session_token", "session")
            if bool(cfg.get("refresh_tokens")) and str(account.get("refresh_token") or "").strip():
                if _refresh_platform_tokens is None:
                    raise RuntimeError("当前环境无法刷新 OAuth token")
                self._set_task_result(task_id, {
                    "stage": "刷新令牌",
                    "engine_step": "refresh",
                    "progress_percent": PLUS_STEP_PROGRESS["refresh"][0],
                    "progress_label": PLUS_STEP_PROGRESS["refresh"][1],
                })
                parent_info = self._assign_project_parent_proxy(normalized_cfg, 1)
                refresh_proxy = str((parent_info or {}).get("proxy") or "")
                refreshed = _refresh_platform_tokens(str(account.get("refresh_token") or ""), proxy=refresh_proxy)  # type: ignore[misc]
                working_access_token = str(refreshed.get("access_token") or "").strip()
                if not account.get("_manual") and not account.get("_partial"):
                    update_registered_account(account, refreshed)
            if not working_access_token:
                raise RuntimeError("账号缺少 access_token")

            eligibility = self._precheck_account_eligibility(
                account,
                normalized_cfg,
                working_access_token,
                task_id=task_id,
                email=email,
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

            last_combo_error = ""
            for matrix_index in range(1, matrix_total + 1):
                self._wait_for_task_ready(task_id, email, running=True)
                if self._stop_event.is_set() or self._task_cancel_requested(task_id):
                    raise _TaskCancelled("用户取消任务")
                config_label = f"矩阵配置 {matrix_index}/{matrix_total}" if matrix_enabled else "固定双 IP 配置"
                self._set_task_result(task_id, {
                    "status": "running",
                    "stage": f"准备{config_label}",
                    "engine_step": "preparing",
                    "progress_percent": PLUS_STEP_PROGRESS["preparing"][0],
                    "progress_label": f"准备{config_label}",
                    "matrix_index": matrix_index,
                    "matrix_total": matrix_total,
                    "matrix_failures": matrix_index - 1,
                    "link_attempt_count": 0,
                    "link_attempt_limit": active_retry_limit,
                    "extraction_mode": extraction_mode,
                    "active_retry_limit": active_retry_limit,
                    "left_proxy_ip": "",
                    "right_proxy_ip": "",
                    "long_url": "",
                    "long_url_present": False,
                    "error": "",
                })
                try:
                    payload, proxy_info = self._payload_for_account(account, normalized_cfg, matrix_index)
                    payload["accessToken"] = working_access_token
                    payload["provider_retry_limit"] = active_retry_limit
                    self._set_task_result(task_id, {
                        "billing_country": str(payload.get("billing_country") or ""),
                        "payment_method_country": str(payload.get("payment_method_country") or ""),
                        "promotion_country": str(payload.get("promotion_country") or ""),
                        "provider_country": str(proxy_info.get("provider_country") or payload.get("batch_checkout_proxy_country") or ""),
                        "left_proxy_country": str(payload.get("batch_checkout_proxy_country") or payload.get("billing_country") or ""),
                        "right_proxy_country": str(payload.get("promotion_proxy_country") or payload.get("promotion_country") or ""),
                        "proxy_label": str(proxy_info.get("label") or ""),
                        "proxy_source": str(proxy_info.get("source") or ""),
                    })
                    data = self._generate_local_long_link(payload, task_id=task_id, email=email)
                    long_url = str(data.get("long_url") or "").strip()
                    if not bool(data.get("ok")) or not long_url:
                        raise RuntimeError("内置引擎未返回 long_url")
                except _TaskCancelled:
                    raise
                except Exception as combo_error:
                    last_combo_error = _compact_plus_log(_redact_log(combo_error), limit=360)
                    has_next = matrix_index < matrix_total
                    exhausted_label = "全部矩阵配置失败" if matrix_enabled else "固定双 IP 配置失败"
                    self._set_task_result(task_id, {
                        "status": "running",
                        "stage": "切换下一矩阵配置" if has_next else exhausted_label,
                        "engine_step": "preparing" if has_next else "combo_result",
                        "progress_percent": 0 if has_next else PLUS_STEP_PROGRESS["combo_result"][0],
                        "progress_label": f"配置 {matrix_index}/{matrix_total} 失败，切换下一组" if has_next else exhausted_label,
                        "matrix_failures": matrix_index,
                        "error": last_combo_error[:1200],
                    })
                    if has_next:
                        continue
                    if matrix_enabled:
                        raise RuntimeError(f"全部 {matrix_total} 种矩阵配置均未提取到长链；最后错误：{last_combo_error}") from combo_error
                    raise RuntimeError(f"固定双 IP 配置重试 {active_retry_limit} 次仍未提取到长链；最后错误：{last_combo_error}") from combo_error

                self._set_task_result(task_id, {
                    "status": "success",
                    "stage": "完成",
                    "finished_at": _now(),
                    "duration_seconds": round(time.time() - start_ts, 1),
                    "proxy_label": str(proxy_info.get("label") or ""),
                    "proxy_source": str(proxy_info.get("source") or ""),
                    "billing_country": str(data.get("billing_country") or payload.get("billing_country") or ""),
                    "payment_method_country": str(data.get("payment_method_country") or payload.get("payment_method_country") or ""),
                    "promotion_country": str(data.get("promotion_country") or payload.get("promotion_country") or ""),
                    "provider_country": str(proxy_info.get("provider_country") or payload.get("batch_checkout_proxy_country") or ""),
                    "matrix_index": matrix_index,
                    "matrix_total": matrix_total,
                    "matrix_failures": matrix_index - 1,
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
                    "error": "",
                    "progress_percent": 100,
                    "progress_label": "提链成功",
                })
                with self._lock:
                    stats = self._state.setdefault("stats", {})
                    stats["success"] = int(stats.get("success") or 0) + 1
                    stats["done"] = int(stats.get("done") or 0) + 1
                    stats["running"] = max(0, int(stats.get("running") or 0) - 1)
                self._bump()
                self._append_log(f"[{email}] Plus 提链成功，模式={mode_label}，配置={matrix_index}/{matrix_total}，耗时 {round(time.time() - start_ts, 1)}s", "success")
                return
        except Exception as error:
            if isinstance(error, _TaskCancelled) or self._task_cancel_requested(task_id):
                self._cancel_task(task_id, email, reason="用户取消任务", was_running=True)
                return
            compact_error = _compact_plus_log(_redact_log(error), limit=360)
            self._set_task_result(task_id, {"status": "failed", "stage": failure_stage, "progress_label": "提链失败", "error": compact_error[:1200], "finished_at": _now(), "duration_seconds": round(time.time() - start_ts, 1)})
            with self._lock:
                stats = self._state.setdefault("stats", {})
                stats["fail"] = int(stats.get("fail") or 0) + 1
                stats["done"] = int(stats.get("done") or 0) + 1
                stats["running"] = max(0, int(stats.get("running") or 0) - 1)
            self._bump()
            self._append_log(f"[{email}] Plus 提链失败：{compact_error}", "danger")

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
        concurrency = _clamp_int(active_cfg.get("concurrency"), 1, 1, 10)
        pending = list(tasks)
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures: dict[Any, dict[str, Any]] = {}
            while futures or pending or self._retry_queue:
                with self._lock:
                    if self._retry_queue:
                        pending.extend(self._retry_queue)
                        self._retry_queue = []
                if self._stop_event.is_set() and pending:
                    self._cancel_queued_tasks(pending, reason="用户停止任务")
                    pending = []
                pending = [
                    task
                    for task in pending
                    if str((self._result_for_task(str(task.get("task_id") or "")) or {}).get("status") or "")
                    not in {"success", "failed", "cancelled"}
                ]
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
        with self._lock:
            stats = self._state.setdefault("stats", {})
            stats["running"] = 0
            self._state["enabled"] = False
            self._save()
        self._bump()
        final = self.get(include_secret=False).get("stats", {})
        self._append_log(
            f"Plus 提链任务结束：成功={final.get('success', 0)}，失败={final.get('fail', 0)}，取消={final.get('cancelled', 0)}",
            "info",
        )

    def _runner_main(self, accounts: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
        tasks = [
            self._new_task(account, cfg, index)
            for index, account in enumerate(accounts, start=1)
        ]
        with self._lock:
            self._state.setdefault("results", []).extend([
                {
                    "task_id": str(task["task_id"]),
                    "email": str(task["email"]),
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
                    "created_at": _now(),
                    "updated_at": _now(),
                }
                for task in tasks
            ])
            self._save()
        self._run_task_batch(tasks)

    def start(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options if isinstance(options, dict) else {}
        if self._runner and self._runner.is_alive():
            self._append_log("Plus 提链任务已在运行", "warning")
            return self.get()
        config_updates = options.get("config") if isinstance(options.get("config"), dict) else {}
        if config_updates:
            self.update_config(config_updates)
        with self._lock:
            cfg = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
        accounts = self._select_accounts(
            options.get("emails") if isinstance(options.get("emails"), list) else None,
            partial_accounts=options.get("partial_accounts") if isinstance(options.get("partial_accounts"), list) else None,
            manual_accounts=options.get("manual_accounts") if isinstance(options.get("manual_accounts"), list) else None,
            manual_tokens=options.get("manual_tokens") if isinstance(options.get("manual_tokens"), list) else None,
        )
        if not accounts:
            self._append_log("没有可提链账号：请从库存/半成品选择账号，或粘贴 access_token/session", "warning")
            return self.get()
        if options.get("clear_results", True):
            with self._lock:
                self._state["results"] = []
                self._state["logs"] = []
                self._task_controls = {}
                self._retry_queue = []
        now = _now()
        with self._lock:
            self._state["enabled"] = True
            self._state["stats"] = {
                **_default_state()["stats"],
                "total": len(accounts),
                "started_at": now,
                "updated_at": now,
            }
            self._save()
        self._stop_event.clear()
        matrix_total = (
            len(cfg.get("billing_country_rotation") or []) * len(cfg.get("proxy_country_rotation") or [])
            if cfg.get("country_rotation_enabled")
            else 1
        )
        self._append_log(
            f"Plus 提链任务已启动：账号={len(accounts)}，并发={cfg.get('concurrency')}，模式={'矩阵轮询' if _extraction_mode(cfg) == 'matrix' else '固定双 IP'}，配置数={matrix_total}，单配置重试={_retry_limit_for_mode(cfg)}",
            "success",
        )
        self._runner = threading.Thread(target=self._runner_main, args=(accounts, cfg), daemon=True, name="plus-link")
        self._runner.start()
        return self.get()


plus_link_service = PlusLinkService(PLUS_LINK_FILE)
