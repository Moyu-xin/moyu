"""Stable adapter between Plus-link tasks and the embedded PayPal workers."""
from __future__ import annotations

import hashlib
import re
import time
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .br_new_bridge import BrNewProtocolBridge
from .phone_pool import (
    PhonePoolCodeTimeout,
    PhonePoolFetchError,
    PhonePoolStore,
)
from .smsbower import SmsBowerActivation, SmsBowerClient, SmsBowerCodeTimeout


PAYPAL_PROTOCOL_COUNTRIES = frozenset({"BR", "GB", "US", "TH", "ID"})
_MODE_BY_COUNTRY = {country: f"{country}_NEW" for country in PAYPAL_PROTOCOL_COUNTRIES}
_PHONE_EXAMPLES = {
    "BR": "+5511999999999",
    "GB": "+447911123456",
    "US": "+12025550123",
    "TH": "+66912345678",
    "ID": "+6281212345678",
}
_BA_TOKEN_RE = re.compile(r"^BA-[A-Z0-9]{8,40}$", re.IGNORECASE)


class PaypalProtocolConfigError(ValueError):
    pass


def parse_ba_token(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise PaypalProtocolConfigError("PayPal BA 链为空")
    if _BA_TOKEN_RE.fullmatch(text):
        return text.upper()
    parsed = urlsplit(text)
    if parsed.scheme.lower() != "https" or parsed.hostname not in {
        "paypal.com",
        "www.paypal.com",
    }:
        raise PaypalProtocolConfigError("不是有效的 PayPal agreements approve 链")
    if parsed.path.rstrip("/").lower() != "/agreements/approve":
        raise PaypalProtocolConfigError("PayPal 链路径不是 /agreements/approve")
    token = str((parse_qs(parsed.query).get("ba_token") or [""])[0]).strip().upper()
    if not _BA_TOKEN_RE.fullmatch(token):
        raise PaypalProtocolConfigError("PayPal BA 链缺少有效 ba_token")
    return token


def _masked_token(value: object) -> str:
    text = str(value or "").strip()
    if len(text) <= 10:
        return "<redacted>" if text else ""
    return f"{text[:4]}…{text[-4:]}"


def _token_hash(value: object) -> str:
    text = str(value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""


def _sanitize_result(result: dict[str, Any], *, country: str, ba_token: str) -> dict[str, Any]:
    identity = result.get("identity_elevation")
    if not isinstance(identity, dict):
        identity = {}
    returned_ba = str(result.get("ba_token") or ba_token).strip()
    ec_token = str(result.get("ec_token") or "").strip()
    return {
        "status": str(result.get("status") or "").strip().lower(),
        "country": country,
        "mode": _MODE_BY_COUNTRY[country],
        "ba_token_masked": _masked_token(returned_ba),
        "ba_token_hash": _token_hash(returned_ba),
        "ec_token_masked": _masked_token(ec_token),
        "ec_token_hash": _token_hash(ec_token),
        "user_id": str(result.get("user_id") or identity.get("user_id") or "").strip(),
        "payment_action": str(result.get("payment_action") or "").strip(),
        "buyer_mode": str(result.get("buyer_mode") or "").strip(),
        "redirect_status": str(result.get("redirect_status") or "").strip(),
        "settlement_status": str(result.get("settlement_status") or "").strip(),
        "buyer_ready": bool(identity.get("buyer_ready")),
        "funding_selected": bool(identity.get("funding_selected")),
        "funding_available": bool(identity.get("funding_available")),
        "funding_available_count": int(identity.get("funding_available_count") or 0),
    }


def run_paypal_payment(
    *,
    ba_url: str,
    country: str,
    proxy_url: str,
    sms_provider: str = "smsbower",
    smsbower_api_key: str = "",
    smsbower_max_price: float | Decimal = 0.014,
    phone_pool: PhonePoolStore | None = None,
    phone_pool_owner_id: str = "admin",
    phone_pool_job_id: str = "",
    max_card_attempts: int = 5,
    sms_max_attempts: int = 5,
    max_flow_attempts: int = 2,
    on_log: Callable[[str, str], None] | None = None,
    on_stage: Callable[[str, str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Authorize a freshly extracted BA with the matching country protocol."""
    normalized_country = str(country or "").strip().upper()
    if normalized_country not in PAYPAL_PROTOCOL_COUNTRIES:
        supported = "/".join(sorted(PAYPAL_PROTOCOL_COUNTRIES))
        raise PaypalProtocolConfigError(
            f"PayPal 自动支付不支持国家 {normalized_country or '<empty>'}；仅支持 {supported}"
        )
    ba_token = parse_ba_token(ba_url)
    if not str(proxy_url or "").strip():
        raise PaypalProtocolConfigError("PayPal 自动支付缺少对应国家的 Approve 代理")

    normalized_provider = str(sms_provider or "smsbower").strip().lower()
    if normalized_provider not in {"smsbower", "phone_pool"}:
        raise PaypalProtocolConfigError(f"不支持的验证码来源：{sms_provider}")
    if normalized_provider == "phone_pool" and phone_pool is None:
        raise PaypalProtocolConfigError("手机号池验证码来源未初始化")

    logger = on_log or (lambda _level, _message: None)
    stage = on_stage or (lambda _step, _message: None)
    cancel_check = check_cancelled or (lambda: None)
    client = (
        SmsBowerClient(smsbower_api_key, smsbower_max_price)
        if normalized_provider == "smsbower"
        else None
    )
    activation: SmsBowerActivation | None = None
    pool_entry_id = ""
    pool_excluded_ids: set[str] = set()
    pool_job_id = str(phone_pool_job_id or f"paypal-{_token_hash(ba_token)}")
    code_received = False
    finalized_ids: set[str] = set()

    def check() -> None:
        cancel_check()

    def finalize_current(success: bool, reason: str = "") -> None:
        nonlocal pool_entry_id
        if normalized_provider == "smsbower":
            current = activation
            if current is None or current.activation_id in finalized_ids or client is None:
                return
            try:
                if success or code_received:
                    client.complete(current.activation_id)
                else:
                    client.cancel(current.activation_id, ignore_errors=True)
            finally:
                finalized_ids.add(current.activation_id)
            return

        entry_id = pool_entry_id
        if not entry_id or entry_id in finalized_ids or phone_pool is None:
            return
        pool_excluded_ids.add(entry_id)
        try:
            if reason == "paypal_initiation_failed":
                phone_pool.mark(
                    entry_id,
                    pool_job_id,
                    "send_failed",
                    "PayPal 无法向该号码发送验证码",
                    release=True,
                    increment_failure=True,
                )
            elif reason == "paypal_otp_rejected":
                phone_pool.mark(
                    entry_id,
                    pool_job_id,
                    "otp_rejected",
                    "验证码被 PayPal 拒绝",
                    release=True,
                    increment_failure=True,
                )
            else:
                phone_pool.release(
                    entry_id,
                    pool_job_id,
                    message="流程完成，可再次使用" if success else "任务结束，已释放",
                )
        finally:
            finalized_ids.add(entry_id)
            pool_entry_id = ""

    def reserve_number() -> dict[str, Any]:
        nonlocal activation, pool_entry_id, code_received
        check()
        if activation is not None or pool_entry_id:
            finalize_current(False)
        stage("paypal_sms", f"正在获取 {normalized_country} PayPal 验证号码")
        code_received = False
        if normalized_provider == "phone_pool":
            assert phone_pool is not None
            entry = phone_pool.reserve(
                phone_pool_owner_id,
                normalized_country,
                pool_job_id,
                pool_excluded_ids,
            )
            pool_entry_id = entry.id
            logger("INFO", f"手机号池已分配 {normalized_country} 号码")
            return {
                "activation_id": entry.id,
                "phone": entry.phone,
                "provider_id": "phone_pool",
                "price": 0.0,
                "expires_at": 0.0,
                "reused": True,
            }

        assert client is not None
        activation = client.acquire_paypal_number(normalized_country)
        logger("INFO", f"SMSBower 已获取 {normalized_country} 号码，价格 ${float(activation.activation_cost):.3f}")
        return {
            "activation_id": activation.activation_id,
            "phone": activation.phone_number,
            "provider_id": "smsbower",
            "price": float(activation.activation_cost),
            "expires_at": time.time() + 20 * 60,
            "reused": False,
        }

    def wait_for_code(_prompt: str) -> str:
        nonlocal code_received
        check()
        stage("paypal_otp", "等待 PayPal 短信验证码")

        def status_changed(status: str) -> None:
            check()
            provider_label = "手机号池" if normalized_provider == "phone_pool" else "SMSBower"
            logger("DEBUG", f"{provider_label} 状态：{status}")

        try:
            if normalized_provider == "phone_pool":
                if not pool_entry_id or phone_pool is None:
                    raise RuntimeError("当前任务尚未分配手机号池号码")
                code = phone_pool.wait_for_code(
                    pool_entry_id,
                    pool_job_id,
                    timeout_seconds=300,
                    poll_interval=3.0,
                    on_status=status_changed,
                )
            else:
                if activation is None or client is None:
                    raise RuntimeError("SMSBower activation is not initialized")
                code = client.wait_for_code(
                    activation.activation_id,
                    timeout_seconds=300,
                    poll_interval=3.0,
                    on_status=status_changed,
                )
        except (SmsBowerCodeTimeout, PhonePoolCodeTimeout, PhonePoolFetchError):
            logger("WARNING", "验证码等待失败或超时，协议将更换号码")
            finalize_current(False)
            return ""
        code_received = True
        return code

    def sms_sent() -> None:
        check()
        provider_label = "手机号池" if normalized_provider == "phone_pool" else "SMSBower"
        stage("paypal_otp", f"PayPal 验证码已发送，等待{provider_label}返回")

    def confirmation(value: bool) -> None:
        check()
        if value:
            stage("paypal_signup", "短信验证成功，正在创建 PayPal 账户")
        else:
            logger("WARNING", "PayPal 拒绝了当前短信验证码，协议将更换号码")
            finalize_current(False, "paypal_otp_rejected")

    def abandon(reason: str) -> None:
        check()
        provider_label = "手机号池" if normalized_provider == "phone_pool" else "SMSBower"
        logger("WARNING", f"放弃当前{provider_label}号码：{reason}")
        finalize_current(False, reason)

    bridge = BrNewProtocolBridge(
        ba_token=ba_token,
        phone=_PHONE_EXAMPLES[normalized_country],
        max_card_attempts=max(1, min(20, int(max_card_attempts))),
        proxy_url=str(proxy_url).strip(),
        sms_provider=normalized_provider,
        reserve_number=reserve_number,
        wait_for_code=wait_for_code,
        on_log=lambda level, message: (check(), logger(level, message))[-1],
        on_generated=lambda _data: check(),
        on_sms_sent=sms_sent,
        on_confirmation=confirmation,
        on_abandon=abandon,
        sms_max_attempts=max(1, min(20, int(sms_max_attempts))),
        max_flow_attempts=max(1, min(5, int(max_flow_attempts))),
        mode=_MODE_BY_COUNTRY[normalized_country],
    )

    stage("paypal_payment", f"启动 {normalized_country} New PayPal 协议")
    try:
        raw_result = bridge.run()
        safe_result = _sanitize_result(raw_result, country=normalized_country, ba_token=ba_token)
        success = safe_result["status"] == "success"
        finalize_current(success)
        if not success:
            raise RuntimeError(f"{normalized_country} New 协议未返回 success")
        stage("paypal_done", "PayPal BA 授权成功")
        return safe_result
    except BaseException:
        finalize_current(False)
        raise
    finally:
        if client is not None:
            client.close()
