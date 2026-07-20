#!/usr/bin/env python3
"""Local web UI for the PayPal Billing Agreement flow.

Run:
    python web.py --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import argparse
import importlib
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from loguru import logger

from paypal.flow import PayPalFlow
from paypal.models import BillingAddress, CardInfo, UserInfo, generate_address, generate_card, generate_user
from paypal.proxy import ProxyConfig, build_proxy_config
from paypal.traffic_recorder import (
    TrafficRecorder,
    clear_current_traffic_recorder,
    set_current_traffic_recorder,
)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "web_static"
CAPTURES_ROOT = (ROOT / "captures").resolve()


def _smsbower_module():
    return importlib.import_module("paypal.smsbower")


def _build_smsbower_provider(enabled: bool):
    return getattr(_smsbower_module(), "build_smsbower_provider")(enabled=enabled)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, "")
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(min_value, min(value, max_value))


def _prepare_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except Exception:
        pass


def resolve_traffic_dir(raw: str, job_id: str) -> Path:
    if raw.strip():
        requested = Path(raw).expanduser()
        candidate = requested if requested.is_absolute() else CAPTURES_ROOT / requested
        candidate = candidate.resolve()
    else:
        candidate = (CAPTURES_ROOT / f"program-paypal-{job_id}").resolve()
    if candidate != CAPTURES_ROOT and CAPTURES_ROOT not in candidate.parents:
        raise ValueError("发包记录目录必须位于项目 captures 目录内")
    _prepare_private_dir(CAPTURES_ROOT)
    _prepare_private_dir(candidate)
    return candidate


PRODUCTION_MODE = env_bool("PAYPAL_WEB_PRODUCTION", False)
MAX_LOG_LINES = env_int("PAYPAL_WEB_MAX_LOG_LINES", 300, 50, 2000)
MAX_TOTAL_JOBS = env_int("PAYPAL_WEB_MAX_TOTAL_JOBS", 200, 10, 5000)
MAX_ACTIVE_JOBS = env_int("PAYPAL_WEB_MAX_ACTIVE_JOBS", 4, 1, 100)
MAX_ACTIVE_JOBS_PER_DEVICE = env_int("PAYPAL_WEB_MAX_ACTIVE_JOBS_PER_DEVICE", 2, 1, 20)
JOB_RETENTION_SECONDS = env_int("PAYPAL_WEB_JOB_RETENTION_SECONDS", 24 * 60 * 60, 60, 30 * 24 * 60 * 60)
OTP_INPUT_TIMEOUT_SECONDS = env_int("PAYPAL_WEB_OTP_TIMEOUT_SECONDS", 30 * 60, 60, 24 * 60 * 60)
ALLOW_DEBUG_LOGS = env_bool("PAYPAL_WEB_ALLOW_DEBUG_LOGS", False)
COOKIE_SECURE = env_bool("PAYPAL_WEB_COOKIE_SECURE", False)
DEVICE_COOKIE_NAME = "paypal_web_device_id"
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
DEVICE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
BA_TOKEN_RE = re.compile(r"^BA-[A-Za-z0-9]{8,80}$")
PHONE_RE = re.compile(r"^\+?\d{8,20}$")
FINGERPRINT_SOURCE_CHOICES = {"random", "program", "python", "synthetic", "roxy", "browser", "headless", "local_headless", "playwright", "local_playwright", "auto"}
DATADOME_MODE_CHOICES = {"protocol", "edge", "roxy", "browser", "headless", "local_headless", "playwright", "local_playwright", "auto", "off"}
MTR_RUNTIME_CHOICES = {"python_generated", "python", "protocol", "roxy", "browser", "headless", "local_headless", "playwright", "local_playwright", "auto", "block", "off"}
RISK_SIGNALS_MODE_CHOICES = {"protocol", "python", "synthetic", "template", "roxy", "browser", "headless", "local_headless", "playwright", "local_playwright", "auto", "off"}
SMS_PROVIDER_CHOICES = {"manual", "smsbower"}
ROXY_LIKE_MODE_VALUES = {"roxy", "browser", "real_browser", "chrome", "chromium", "roxy_browser", "roxybrowser"}

ACTIVE_STATUSES = {"queued", "running", "awaiting_otp"}
RUNNER_SEMAPHORE = threading.BoundedSemaphore(MAX_ACTIVE_JOBS)
RATE_LOCK = threading.RLock()
RATE_BUCKETS: dict[tuple[str, str], list[float]] = {}


# ----------------------------- helpers -----------------------------


def implicit_risk_signals_mode(
    fingerprint_source: object,
    datadome_mode: object,
    mtr_runtime: object,
    explicit: object = "",
) -> str:
    value = str(explicit or "").strip().lower().replace("-", "_")
    if value:
        return value
    modes = {
        str(fingerprint_source or "").strip().lower().replace("-", "_"),
        str(datadome_mode or "").strip().lower().replace("-", "_"),
        str(mtr_runtime or "").strip().lower().replace("-", "_"),
    }
    if modes & ROXY_LIKE_MODE_VALUES:
        return "roxy"
    if "auto" in modes:
        return "auto"
    return "headless"


def now_ts() -> float:
    return time.time()


def mask_middle(value: str, left: int = 6, right: int = 4) -> str:
    value = value or ""
    if len(value) <= left + right:
        return "***" if value else ""
    return f"{value[:left]}…{value[-right:]}"


def mask_card(number: str) -> str:
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if len(digits) <= 4:
        return "••••"
    grouped = " ".join([digits[i : i + 4] for i in range(0, len(digits), 4)])
    return f"•••• •••• •••• {grouped[-4:]}"


def mask_email(value: str) -> str:
    if "@" not in (value or ""):
        return "***"
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[:2]}***{local[-1:]}@{domain}"


def mask_digits(value: str, keep: int = 4) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) <= keep:
        return "*" * len(digits)
    return f"{'*' * (len(digits) - keep)}{digits[-keep:]}"


def mask_phone(value: str) -> str:
    return mask_digits(value, keep=4)


def redact_text(value: Any) -> str:
    """Best-effort redaction for logs/UI errors. Keep status information, hide secrets/PII."""
    text = str(value or "")
    if not text:
        return text

    # Proxy/basic-auth URLs, e.g. http://user:pass@host:port.
    text = re.sub(
        r"(?i)\b((?:https?|socks5h?|socks4)://)([^/\s:@]+)(?::([^/\s@]*))?@([^/\s]+)",
        lambda m: f"{m.group(1)}***:***@{m.group(4)}",
        text,
    )

    # URL query parameters and JSON-ish key/value pairs.
    text = re.sub(
        r"(?i)([?&](?:ba_token|token|ec_token|billingAgreementId|billingAgreementToken|billing_agreement_token|access_token|code|pin|password|otp|ssrt|ctxId|ctx_id|cmid|clientMetadataId|client_metadata_id|correlationId|correlation_id|requestId|request_id|sealedResult|sealed_result|visitorToken|visitor_token)=)([^&\s\"']+)",
        lambda m: f"{m.group(1)}{mask_middle(m.group(2), 4, 4)}",
        text,
    )
    text = re.sub(
        r"(?i)([\"']?\b(?:ba_token|ec_token|billingAgreementId|billingAgreementToken|billing_agreement_token|token|accessToken|password|securityCode|cvv|pin|otp|ssrt|ctxId|ctx_id|cmid|clientMetadataId|client_metadata_id|correlationId|correlation_id|requestId|request_id|sealedResult|sealed_result|visitorToken|visitor_token)\b[\"']?\s*[:=]\s*)([\"']?)([^&,\"'\s}{]+)([\"']?)",
        lambda m: f"{m.group(1)}{m.group(2)}<redacted>{m.group(4)}",
        text,
    )

    # Common token formats.
    text = re.sub(r"\bBA-[A-Za-z0-9]{8,80}\b", lambda m: mask_middle(m.group(0), 4, 4), text)
    text = re.sub(r"\bEC-[A-Za-z0-9]{8,80}\b", lambda m: mask_middle(m.group(0), 4, 4), text)

    # Email, CPF, card-like long digit sequences, Brazil/international phone-like values.
    text = re.sub(
        r"\b([A-Za-z0-9._%+\-]{1,64})@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b",
        lambda m: mask_email(m.group(0)),
        text,
    )
    text = re.sub(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", "<redacted-cpf>", text)
    text = re.sub(
        r"(?<!\w)(?:\d[ -]?){13,19}(?!\w)",
        lambda m: mask_digits(m.group(0), keep=4),
        text,
    )
    text = re.sub(
        r"(?<!\w)\+?\d[\d(). -]{7,18}\d(?!\w)",
        lambda m: mask_phone(m.group(0)),
        text,
    )
    return text


WEB_PHASE1_RISK_TEXT_RE = re.compile(r"(?i)\bphase\s*1\b|\brisk\b|风控")


def sanitize_web_visible_text(value: object, *, fallback: str = "执行前置准备") -> str:
    text = redact_text(value).rstrip()
    if WEB_PHASE1_RISK_TEXT_RE.search(text):
        return fallback
    return text


def sanitize_payload(value: Any, key: str = "") -> Any:
    """Redact sensitive values before returning API payloads to the browser."""
    compact_key = key.lower().replace("_", "").replace("-", "")
    if isinstance(value, dict):
        return {k: sanitize_payload(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item, key) for item in value]
    if not isinstance(value, str):
        return value

    if compact_key in {"password", "securitycode", "cvv", "pin", "otp", "authorization", "cookie", "accesstoken", "euat"}:
        return "<redacted>"
    if compact_key in {
        "sealedresult",
        "visitortoken",
        "requestid",
        "correlationid",
        "clientmetadataid",
        "cmid",
        "ssrt",
        "ctxid",
    }:
        return mask_middle(value, 4, 4)
    if compact_key in {"token", "batoken", "ectoken", "billingagreementid", "billingagreementtoken"}:
        return mask_middle(value, 4, 4)
    if compact_key in {"cardnumber", "encryptednumber"}:
        return mask_digits(value, keep=4)
    if compact_key in {"cpf", "identitydocument", "document"}:
        return "<redacted>"
    if compact_key == "email":
        return mask_email(value)
    if compact_key in {"phonenumber", "phone", "number", "phonelocal"} and sum(ch.isdigit() for ch in value) >= 8:
        return mask_phone(value)
    if compact_key.endswith("url") or compact_key in {"href", "referer", "location"}:
        return truncate_text(redact_text(value))
    return truncate_text(redact_text(value))


def truncate_text(value: str, max_chars: int = 1000) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}…<truncated>"


def safe_result_payload(value: Any) -> Any:
    sanitized = sanitize_payload(value)
    if isinstance(sanitized, dict) and "raw_response" in sanitized:
        sanitized["raw_response"] = "<redacted>"
    if isinstance(sanitized, dict):
        sanitized.pop("risk_runtime", None)
        sanitized.pop("synthetic_risk_families", None)
    return sanitized



def parse_cookie_header(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key:
            cookies[key] = value
    return cookies


def public_generated_payload(user: UserInfo, card: CardInfo, address: BillingAddress) -> dict[str, Any]:
    """Data shown in the browser. Keep secrets/PII masked in API responses."""
    return {
        "user": {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": mask_email(user.email),
            "phone": mask_phone(user.phone),
            "phone_country_code": user.phone_country_code,
            "phone_local": mask_phone(user.phone_local),
            "password": "<redacted>",
            "dob": "<redacted>",
            "cpf": "<redacted>",
        },
        "card": {
            "number": mask_card(card.number),
            "expiry": card.expiry,
            "cvv": "***",
            "card_type": card.card_type,
        },
        "address": sanitize_payload(asdict(address)),
    }



# ----------------------------- job model -----------------------------


@dataclass
class WebJob:
    id: str
    owner_device_id: str
    ba_token: str
    phone: str
    sms_provider: str = "manual"
    debug: bool = False
    max_card_attempts: int = 5
    max_flow_attempts: int = 1
    max_authorize_attempts: int = 3
    card_retry_delay_seconds: float = 6.0
    card_retry_jitter_seconds: float = 2.0
    proxy_enabled: bool = False
    proxy_mode: str = "environment"
    proxy_label: str = "代理关闭"
    fingerprint_source: str = "headless"
    datadome_mode: str = "headless"
    mtr_runtime: str = "headless"
    risk_signals_mode: str = "headless"
    record_traffic: bool = False
    traffic_dir: str = ""
    compare_roxy_capture: str = ""
    traffic_report_json: str = ""
    traffic_report_md: str = ""
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    started_at: float | None = None
    finished_at: float | None = None
    status: str = "queued"  # queued | running | awaiting_otp | completed | failed
    stage: str = "排队中"
    result: dict[str, Any] | None = None
    error: str = ""
    traceback_text: str = ""
    generated: dict[str, Any] | None = None
    awaiting_prompt: str = ""
    logs: list[dict[str, Any]] = field(default_factory=list)
    _condition: threading.Condition = field(default_factory=threading.Condition, repr=False)
    _input_queue: list[str] = field(default_factory=list, repr=False)
    _proxy_config: ProxyConfig | None = field(default=None, repr=False)

    def set_status(self, status: str, stage: str | None = None) -> None:
        with self._condition:
            self.status = status
            if stage is not None:
                self.stage = stage
            self.updated_at = now_ts()
            self._condition.notify_all()

    def set_generated(self, generated: dict[str, Any]) -> None:
        with self._condition:
            self.generated = generated
            self.updated_at = now_ts()
            self._condition.notify_all()

    def add_log(self, level: str, message: str, ts: float | None = None) -> None:
        with self._condition:
            self.logs.append({
                "time": ts or now_ts(),
                "level": level,
                "message": sanitize_web_visible_text(message),
            })
            if len(self.logs) > MAX_LOG_LINES:
                del self.logs[: len(self.logs) - MAX_LOG_LINES]
            self.updated_at = now_ts()
            self._condition.notify_all()

    def wait_for_input(self, prompt: str) -> str:
        with self._condition:
            self.status = "awaiting_otp"
            self.stage = "等待短信验证码 / 新手机号"
            self.awaiting_prompt = redact_text(prompt)
            self.updated_at = now_ts()
            self._condition.notify_all()
            deadline = now_ts() + OTP_INPUT_TIMEOUT_SECONDS
            while not self._input_queue:
                remaining = deadline - now_ts()
                if remaining <= 0:
                    raise TimeoutError("等待验证码/手机号输入超时")
                self._condition.wait(timeout=min(0.5, remaining))
            value = self._input_queue.pop(0).strip()
            self.status = "running"
            self.stage = "已收到输入，继续执行"
            self.awaiting_prompt = ""
            self.updated_at = now_ts()
            self._condition.notify_all()
            return value

    def submit_input(self, value: str) -> None:
        value = (value or "").strip()
        if not value:
            raise ValueError("输入不能为空")
        with self._condition:
            self._input_queue.append(value)
            self.stage = "已提交验证码/手机号，等待程序处理"
            self.updated_at = now_ts()
            self._condition.notify_all()

    def complete(self, result: dict[str, Any]) -> None:
        with self._condition:
            self.status = "completed"
            self.stage = "已完成"
            self.result = result
            self.finished_at = now_ts()
            self.updated_at = now_ts()
            self.awaiting_prompt = ""
            self._condition.notify_all()

    def fail(self, exc: BaseException) -> None:
        with self._condition:
            self.status = "failed"
            self.stage = "执行失败"
            self.error = sanitize_web_visible_text(str(exc), fallback="执行前置准备失败")
            self.traceback_text = (
                sanitize_web_visible_text(traceback.format_exc(), fallback="调试堆栈已隐藏")
                if (self.debug and ALLOW_DEBUG_LOGS)
                else ""
            )
            self.finished_at = now_ts()
            self.updated_at = now_ts()
            self.awaiting_prompt = ""
            self._condition.notify_all()

    def to_dict(self, *, include_logs: bool = True, log_offset: int = 0) -> dict[str, Any]:
        with self._condition:
            logs = self.logs[max(0, log_offset) :] if include_logs else []
            return {
                "id": self.id,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration": (self.finished_at or now_ts()) - (self.started_at or self.created_at),
                "status": self.status,
                "stage": self.stage,
                "ba_token": mask_middle(self.ba_token),
                "phone": mask_phone(self.phone),
                "sms_provider": self.sms_provider,
                "debug": self.debug and ALLOW_DEBUG_LOGS,
                "max_card_attempts": self.max_card_attempts,
                "max_flow_attempts": self.max_flow_attempts,
                "max_authorize_attempts": self.max_authorize_attempts,
                "card_retry_delay_seconds": self.card_retry_delay_seconds,
                "card_retry_jitter_seconds": self.card_retry_jitter_seconds,
                "proxy_enabled": self.proxy_enabled,
                "proxy_mode": self.proxy_mode,
                "proxy_label": self.proxy_label,
                "fingerprint_source": self.fingerprint_source,
                "datadome_mode": self.datadome_mode,
                "mtr_runtime": self.mtr_runtime,
                "record_traffic": self.record_traffic,
                "traffic_dir": self.traffic_dir,
                "compare_roxy_capture": self.compare_roxy_capture,
                "traffic_report_json": self.traffic_report_json,
                "traffic_report_md": self.traffic_report_md,
                "generated": sanitize_payload(self.generated),
                "awaiting_otp": self.status == "awaiting_otp",
                "awaiting_prompt": sanitize_web_visible_text(self.awaiting_prompt),
                "result": safe_result_payload(self.result),
                "error": sanitize_web_visible_text(self.error, fallback="执行前置准备失败"),
                "traceback": (
                    sanitize_web_visible_text(self.traceback_text, fallback="调试堆栈已隐藏")
                    if (self.debug and ALLOW_DEBUG_LOGS)
                    else ""
                ),
                "logs": logs,
                "log_count": len(self.logs),
            }


JOBS: dict[str, WebJob] = {}
JOBS_LOCK = threading.RLock()


def client_rate_limit(bucket: str, key: str, *, limit: int, window_seconds: int) -> bool:
    current_ts = now_ts()
    with RATE_LOCK:
        cutoff = current_ts - window_seconds
        values = [ts for ts in RATE_BUCKETS.get((bucket, key), []) if ts >= cutoff]
        if len(values) >= limit:
            RATE_BUCKETS[(bucket, key)] = values
            return False
        values.append(current_ts)
        RATE_BUCKETS[(bucket, key)] = values
        return True


def prune_jobs_locked() -> None:
    """Drop old finished jobs and keep the in-memory job list bounded."""
    current_ts = now_ts()
    for job_id, job in list(JOBS.items()):
        if job.status in ACTIVE_STATUSES:
            continue
        finished_or_updated = job.finished_at or job.updated_at
        if current_ts - finished_or_updated > JOB_RETENTION_SECONDS:
            JOBS.pop(job_id, None)

    if len(JOBS) <= MAX_TOTAL_JOBS:
        return

    removable = sorted(
        [job for job in JOBS.values() if job.status not in ACTIVE_STATUSES],
        key=lambda item: item.updated_at,
    )
    while len(JOBS) > MAX_TOTAL_JOBS and removable:
        JOBS.pop(removable.pop(0).id, None)


def active_job_count(owner_device_id: str | None = None) -> int:
    with JOBS_LOCK:
        return sum(
            1
            for job in JOBS.values()
            if job.status in ACTIVE_STATUSES and (owner_device_id is None or job.owner_device_id == owner_device_id)
        )


# ----------------------------- PayPal flow adapter -----------------------------


class WebPayPalFlow(PayPalFlow):
    """PayPalFlow adapter that asks the web page for OTP/new-phone input."""

    def __init__(self, *args: Any, job: WebJob, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.job = job

    def _set_stage(self, stage: str) -> None:
        self.job.set_status("running", stage)

    def _phase0_initial_load(self):
        self._set_stage("Phase 0：打开协议页")
        return super()._phase0_initial_load()

    def _phase2_create_account(self):
        self._set_stage("Phase 2：进入创建账号流程")
        return super()._phase2_create_account()

    def _phase3_signup_and_2fa(self):
        self._set_stage("Phase 3：短信验证与注册")
        return super()._phase3_signup_and_2fa()

    def _phase4_authorize(self):
        self._set_stage("Phase 4：最终授权")
        return super()._phase4_authorize()

    def _on_full_retry_generated(self, flow_attempt: int):
        self.job.set_status(
            "running",
            f"整套流程重试 {flow_attempt}/{self.max_flow_attempts}：已重新生成资料",
        )
        self.job.set_generated(public_generated_payload(self.user, self.card, self.address))

    def _on_signup_retry_generated(self, signup_attempt: int, reason: str):
        self.job.set_status(
            "running",
            f"注册资料重试 {signup_attempt}/{self.max_card_attempts}：已更换账号/卡信息",
        )
        self.job.set_generated(public_generated_payload(self.user, self.card, self.address))

    def _prompt_operator(self, prompt: str) -> str:
        logger.info(prompt)
        return self.job.wait_for_input(prompt)

    def _on_phone_updated(self) -> None:
        self.job.phone = self.user.phone
        self.job.set_generated(public_generated_payload(self.user, self.card, self.address))

    def _confirm_phone_with_retry(self, token: str, signup_url: str):
        """Web version of the CLI input loop."""
        if self.sms_provider is not None:
            return self._confirm_phone_with_sms_provider(token, signup_url)

        while True:
            try:
                auth_id, challenge_id = self._initiate_2fa_phone_confirmation(token, signup_url)
            except Exception as e:
                logger.error("Failed to initiate OTP for {}: {}", self._masked_phone(), e)
                while True:
                    value = self._prompt_operator(
                        "发送验证码失败。请输入新的手机号重新发送（如 +5591980133818）；输入 q 退出。"
                    )
                    if value.lower() in {"q", "quit", "exit"}:
                        raise RuntimeError("OTP confirmation cancelled by user") from e
                    try:
                        self._update_user_phone(value)
                        break
                    except ValueError as phone_error:
                        logger.warning("手机号无效：{}。请重新输入。", phone_error)
                continue

            logger.info("SMS verification code sent to phone: {}", self._masked_phone())

            while True:
                value = self._prompt_operator(
                    "请输入6位短信验证码；如需换号，输入新手机号（如 +5591980133818 或 phone:+5591980133818）；输入 q 退出。"
                )

                if value.lower() in {"q", "quit", "exit"}:
                    raise RuntimeError("OTP confirmation cancelled by user")

                if len(value) == 6 and value.isdigit():
                    if self._confirm_2fa_phone_confirmation(
                        token,
                        signup_url,
                        auth_id,
                        challenge_id,
                        value,
                    ):
                        return
                    logger.warning("验证码验证失败。可以继续输入新的6位验证码，或输入新手机号重新发送验证码。")
                    continue

                try:
                    self._update_user_phone(value)
                    logger.info("Re-sending OTP to the new phone...")
                    break
                except ValueError as e:
                    logger.warning("输入既不是6位验证码，也不是有效手机号：{}。请重新输入。", e)


# ----------------------------- logging -----------------------------


def _job_log_sink(message: Any) -> None:
    record = message.record
    job_id = record["extra"].get("job_id")
    if not job_id:
        return
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return
    level = record["level"].name
    if level == "DEBUG" and not job.debug:
        return
    job.add_log(level, record["message"], record["time"].timestamp())


def _console_log_sink(message: Any) -> None:
    record = message.record
    level = record["level"].name
    ts = record["time"].strftime("%H:%M:%S")
    text = redact_text(record["message"])
    sys.stderr.write(f"{ts} | {level:<8} | {text}\n")


def configure_logging() -> None:
    logger.remove()
    logger.add(_console_log_sink, level="INFO")
    logger.add(_job_log_sink, level="DEBUG", filter=lambda r: bool(r["extra"].get("job_id")))


# ----------------------------- runner -----------------------------


def create_job(
    owner_device_id: str,
    ba_token: str,
    phone: str,
    debug: bool,
    max_card_attempts: int,
    sms_provider: str = "manual",
    max_flow_attempts: int = 1,
    max_authorize_attempts: int = 3,
    card_retry_delay_seconds: float = 6.0,
    card_retry_jitter_seconds: float = 2.0,
    proxy_enabled: bool = False,
    proxy_mode: str = "environment",
    proxy_url: str = "",
    fingerprint_source: str = "headless",
    datadome_mode: str = "headless",
    mtr_runtime: str = "headless",
    risk_signals_mode: str = "headless",
    record_traffic: bool = False,
    traffic_dir: str = "",
    compare_roxy_capture: str = "",
) -> WebJob:
    ba_token = (ba_token or "").strip()
    phone = re.sub(r"[\s().-]+", "", (phone or "").strip())
    sms_provider = (sms_provider or "manual").strip().lower()
    if sms_provider not in SMS_PROVIDER_CHOICES:
        raise ValueError("短信接码方式不正确")
    if not ba_token:
        raise ValueError("BA Token 不能为空")
    if not BA_TOKEN_RE.fullmatch(ba_token):
        raise ValueError("BA Token 格式不正确")
    if not phone and sms_provider == "manual":
        raise ValueError("手机号不能为空")
    if phone and not PHONE_RE.fullmatch(phone):
        raise ValueError("手机号格式不正确")
    if sms_provider == "smsbower":
        _build_smsbower_provider(enabled=True)
    try:
        max_card_attempts = int(max_card_attempts)
    except Exception as exc:
        raise ValueError("最大换卡次数必须是数字") from exc
    max_card_attempts = max(1, min(max_card_attempts, 20))
    try:
        max_flow_attempts = int(max_flow_attempts)
    except Exception as exc:
        raise ValueError("最大整套流程重试次数必须是数字") from exc
    max_flow_attempts = max(1, min(max_flow_attempts, 5))
    try:
        max_authorize_attempts = int(max_authorize_attempts)
    except Exception as exc:
        raise ValueError("最大授权重试次数必须是数字") from exc
    max_authorize_attempts = max(1, min(max_authorize_attempts, 10))
    try:
        card_retry_delay_seconds = float(card_retry_delay_seconds)
    except Exception as exc:
        raise ValueError("换卡等待秒数必须是数字") from exc
    card_retry_delay_seconds = max(0.0, min(card_retry_delay_seconds, 60.0))
    try:
        card_retry_jitter_seconds = float(card_retry_jitter_seconds)
    except Exception as exc:
        raise ValueError("换卡随机抖动秒数必须是数字") from exc
    card_retry_jitter_seconds = max(0.0, min(card_retry_jitter_seconds, 30.0))
    debug = bool(debug) and ALLOW_DEBUG_LOGS
    proxy_mode = (proxy_mode or "environment").strip().lower()
    if proxy_mode not in {"environment", "custom"}:
        raise ValueError("代理来源不正确")
    proxy_url = (proxy_url or "").strip()
    if proxy_url and len(proxy_url) > 2048:
        raise ValueError("链式代理 URL 太长")
    if bool(proxy_enabled) and proxy_mode == "custom" and not proxy_url:
        raise ValueError("启用自定义链式代理时必须填写代理 URL")
    fingerprint_source = (fingerprint_source or "headless").strip().lower().replace("-", "_")
    if fingerprint_source not in FINGERPRINT_SOURCE_CHOICES:
        raise ValueError("浏览器指纹来源不正确")
    datadome_mode = (datadome_mode or "headless").strip().lower().replace("-", "_")
    if datadome_mode not in DATADOME_MODE_CHOICES:
        raise ValueError("DataDome 模式不正确")
    mtr_runtime = (mtr_runtime or "headless").strip().lower().replace("-", "_")
    if mtr_runtime not in MTR_RUNTIME_CHOICES:
        raise ValueError("MTR 模式不正确")
    risk_signals_mode = (risk_signals_mode or "headless").strip().lower().replace("-", "_")
    if risk_signals_mode not in RISK_SIGNALS_MODE_CHOICES:
        raise ValueError("browser risk 模式不正确")
    record_traffic = bool(record_traffic)
    traffic_dir = (traffic_dir or "").strip()
    compare_roxy_capture = (compare_roxy_capture or "").strip()
    if traffic_dir and len(traffic_dir) > 2048:
        raise ValueError("发包记录目录太长")
    if compare_roxy_capture and len(compare_roxy_capture) > 2048:
        raise ValueError("roxy 抓包目录太长")
    if compare_roxy_capture:
        record_traffic = True
    proxy_config = build_proxy_config(
        enabled=bool(proxy_enabled),
        proxy_url=proxy_url if proxy_mode == "custom" else None,
    )
    job_id = uuid.uuid4().hex[:12]
    if record_traffic:
        traffic_dir = str(resolve_traffic_dir(traffic_dir, job_id))

    job = WebJob(
        id=job_id,
        owner_device_id=owner_device_id,
        ba_token=ba_token,
        phone=phone,
        sms_provider=sms_provider,
        debug=debug,
        max_card_attempts=max_card_attempts,
        max_flow_attempts=max_flow_attempts,
        max_authorize_attempts=max_authorize_attempts,
        card_retry_delay_seconds=card_retry_delay_seconds,
        card_retry_jitter_seconds=card_retry_jitter_seconds,
        proxy_enabled=proxy_config.enabled,
        proxy_mode=proxy_mode if proxy_config.enabled else "environment",
        proxy_label=proxy_config.label,
        fingerprint_source=fingerprint_source,
        datadome_mode=datadome_mode,
        mtr_runtime=mtr_runtime,
        risk_signals_mode=risk_signals_mode,
        record_traffic=record_traffic,
        traffic_dir=traffic_dir,
        compare_roxy_capture=compare_roxy_capture,
        _proxy_config=proxy_config,
    )
    with JOBS_LOCK:
        prune_jobs_locked()
        total_active = sum(1 for item in JOBS.values() if item.status in ACTIVE_STATUSES)
        user_active = sum(
            1
            for item in JOBS.values()
            if item.status in ACTIVE_STATUSES and item.owner_device_id == owner_device_id
        )
        if total_active >= MAX_TOTAL_JOBS:
            raise ValueError("当前任务队列已满，请稍后再试")
        if user_active >= MAX_ACTIVE_JOBS_PER_DEVICE:
            raise ValueError(f"当前浏览器已有 {user_active} 个未完成任务，请等待完成后再启动")
        if len(JOBS) >= MAX_TOTAL_JOBS:
            raise ValueError("历史任务数量已达上限，请稍后再试")
        JOBS[job.id] = job
    thread = threading.Thread(target=run_job, args=(job,), name=f"paypal-web-{job.id}", daemon=True)
    thread.start()
    return job


def run_job(job: WebJob) -> None:
    with logger.contextualize(job_id=job.id):
        acquired = False
        traffic_recorder: TrafficRecorder | None = None
        try:
            if not RUNNER_SEMAPHORE.acquire(blocking=False):
                job.set_status("queued", "等待可用执行槽")
                logger.info("Job queued, waiting for execution slot")
                RUNNER_SEMAPHORE.acquire()
            acquired = True
            job.started_at = now_ts()
            job.set_status("running", "生成用户、卡片和地址")
            if job.record_traffic:
                traffic_root = resolve_traffic_dir(job.traffic_dir, job.id)
                traffic_recorder = TrafficRecorder(traffic_root)
                set_current_traffic_recorder(traffic_recorder)
                with job._condition:
                    job.traffic_dir = str(traffic_recorder.root)
                    job.updated_at = now_ts()
                    job._condition.notify_all()
                logger.info("Program traffic recording enabled: {}", traffic_recorder.root)
            proxy_config = job._proxy_config or build_proxy_config(enabled=job.proxy_enabled)
            sms_provider = None
            if job.sms_provider == "smsbower":
                sms_provider = _build_smsbower_provider(enabled=True)
                logger.info("SMS provider: SMSBower auto mode")
            job.proxy_enabled = proxy_config.enabled
            job.proxy_label = proxy_config.label
            user = generate_user(job.phone or "+5500000000000")
            card = generate_card(proxy_url=proxy_config.url)
            address = generate_address()
            job.set_generated(public_generated_payload(user, card, address))

            logger.info("Web job started: {}", job.id)
            logger.info("Proxy: {}", proxy_config.label)
            logger.info(
                "Runtime modes: fingerprint={} datadome={} mtr={}",
                job.fingerprint_source,
                job.datadome_mode,
                job.mtr_runtime,
            )
            logger.info("User: {} {}", user.first_name, user.last_name)
            logger.info("Email: {}", mask_email(user.email))
            if sms_provider is None:
                logger.info("Phone: {}", mask_phone(user.phone))
            else:
                logger.info("Phone: SMSBower auto mode will reserve a Brazil PayPal number before OTP")
            logger.info(
                "Address generated: {}, {}-{}",
                address.district,
                address.city,
                address.state,
            )

            flow = WebPayPalFlow(
                ba_token=job.ba_token,
                user=user,
                card=card,
                address=address,
                max_card_attempts=job.max_card_attempts,
                max_flow_attempts=job.max_flow_attempts,
                max_authorize_attempts=job.max_authorize_attempts,
                card_retry_delay_seconds=job.card_retry_delay_seconds,
                card_retry_jitter_seconds=job.card_retry_jitter_seconds,
                proxy_config=proxy_config,
                fingerprint_source=job.fingerprint_source,
                datadome_mode=job.datadome_mode,
                mtr_runtime=job.mtr_runtime,
                risk_signals_mode=job.risk_signals_mode,
                sms_provider=sms_provider,
                job=job,
            )
            result = flow.run()
            job.complete(result)
        except BaseException as exc:  # keep worker alive and expose details in UI
            logger.error("Web job failed: {}", redact_text(exc))
            job.fail(exc)
        finally:
            if traffic_recorder is not None:
                try:
                    traffic_recorder.close()
                    with job._condition:
                        job.traffic_dir = str(traffic_recorder.root)
                        job.updated_at = now_ts()
                        job._condition.notify_all()
                    logger.info("Program traffic saved: {}", traffic_recorder.root)
                except Exception as close_error:
                    logger.warning("Saving program traffic summary failed: {}", close_error)
                if job.compare_roxy_capture:
                    try:
                        from tools.compare_paypal_traffic import compare, write_markdown

                        roxy_dir = Path(job.compare_roxy_capture).expanduser().resolve()
                        report = compare(traffic_recorder.root, roxy_dir)
                        report_path = traffic_recorder.root / "traffic_diff_report.json"
                        report_path.write_text(
                            json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        write_markdown(report, report_path.with_suffix(".md"))
                        with job._condition:
                            job.traffic_report_json = str(report_path)
                            job.traffic_report_md = str(report_path.with_suffix(".md"))
                            job.updated_at = now_ts()
                            job._condition.notify_all()
                        logger.info("Traffic diff report saved: {}", report_path)
                        if report.get("findings"):
                            logger.warning(
                                "Traffic diff findings: {}",
                                json.dumps(report.get("findings"), ensure_ascii=False, indent=2),
                            )
                    except Exception as diff_error:
                        logger.warning("Traffic diff failed: {}", diff_error)
                clear_current_traffic_recorder()
            if acquired:
                RUNNER_SEMAPHORE.release()


# ----------------------------- HTTP server -----------------------------


class WebHandler(BaseHTTPRequestHandler):
    server_version = "PayPalWebUI/1.0"
    _set_device_cookie: str = ""
    _device_id: str = ""

    def log_message(self, format: str, *args: Any) -> None:  # quieter stdlib server logs
        try:
            text = format % args
        except Exception:
            text = format
        logger.debug("HTTP {}", redact_text(text))

    def client_key(self) -> str:
        host = self.client_address[0] if self.client_address else "unknown"
        return f"{host}:{self.get_device_id()}"

    def check_rate_limit(self, bucket: str, *, limit: int, window_seconds: int) -> bool:
        key = self.client_key()
        if client_rate_limit(bucket, key, limit=limit, window_seconds=window_seconds):
            return True
        self.send_error_json(HTTPStatus.TOO_MANY_REQUESTS, "请求过于频繁，请稍后再试")
        return False

    def validate_post_request(self) -> bool:
        host = self.headers.get("Host", "")
        for header_name in ("Origin", "Referer"):
            raw = self.headers.get(header_name, "")
            if not raw:
                continue
            parsed = urlparse(raw)
            if parsed.netloc and parsed.netloc != host:
                self.send_error_json(HTTPStatus.FORBIDDEN, "跨站请求被拒绝")
                return False

        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Content-Length 无效")
            return False
        content_type = self.headers.get("Content-Type", "")
        if content_length > 0 and "application/json" not in content_type.lower():
            self.send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type 必须是 application/json")
            return False
        return True

    def get_device_id(self) -> str:
        cached = getattr(self, "_device_id", "")
        if cached:
            return cached
        cookies = parse_cookie_header(self.headers.get("Cookie", ""))
        device_id = cookies.get(DEVICE_COOKIE_NAME, "").strip()
        if not DEVICE_ID_RE.fullmatch(device_id):
            device_id = uuid.uuid4().hex
            self._set_device_cookie = device_id
        self._device_id = device_id
        return device_id


    def get_authorized_job(self, job_id: str) -> WebJob | None:
        job = get_job(job_id)
        if not job or job.owner_device_id != self.get_device_id():
            self.send_error_json(HTTPStatus.NOT_FOUND, "任务不存在")
            return None
        return job

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            return self.send_json({"ok": True, "time": now_ts()})
        if path == "/api/jobs":
            device_id = self.get_device_id()
            with JOBS_LOCK:
                prune_jobs_locked()
                jobs = sorted(
                    [job for job in JOBS.values() if job.owner_device_id == device_id],
                    key=lambda j: j.created_at,
                    reverse=True,
                )
            return self.send_json({"jobs": [j.to_dict(include_logs=False) for j in jobs]})
        if path.startswith("/api/jobs/"):
            job_id = path.split("/", 3)[3]
            job = self.get_authorized_job(job_id)
            if not job:
                return
            query = self.parse_query(parsed.query)
            try:
                log_offset = int(query.get("log_offset", "0") or 0)
            except Exception:
                log_offset = 0
            return self.send_json(job.to_dict(include_logs=True, log_offset=log_offset))
        if path.startswith("/api/"):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
        return self.serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not self.validate_post_request():
            return
        if path == "/api/jobs":
            if not self.check_rate_limit("job_create", limit=20, window_seconds=600):
                return
            try:
                data = self.read_json()
                fingerprint_source = str(data.get("fingerprint_source", "headless") or "headless")
                datadome_mode = str(data.get("datadome_mode", "headless") or "headless")
                mtr_runtime = str(data.get("mtr_runtime", "headless") or "headless")
                job = create_job(
                    owner_device_id=self.get_device_id(),
                    ba_token=data.get("ba_token", ""),
                    phone=data.get("phone", ""),
                    debug=bool(data.get("debug", False)),
                    max_card_attempts=int(data.get("max_card_attempts", 5) or 5),
                    sms_provider=str(data.get("sms_provider", "manual") or "manual"),
                    max_flow_attempts=int(data.get("max_flow_attempts", 1) or 1),
                    max_authorize_attempts=int(data.get("max_authorize_attempts", 3) or 3),
                    card_retry_delay_seconds=float(data.get("card_retry_delay_seconds", 6) or 0),
                    card_retry_jitter_seconds=float(data.get("card_retry_jitter_seconds", 2) or 0),
                    proxy_enabled=bool(data.get("proxy_enabled", False)),
                    proxy_mode=str(data.get("proxy_mode", "environment") or "environment"),
                    proxy_url=str(data.get("proxy_url", "") or ""),
                    fingerprint_source=fingerprint_source,
                    datadome_mode=datadome_mode,
                    mtr_runtime=mtr_runtime,
                    risk_signals_mode=implicit_risk_signals_mode(
                        fingerprint_source,
                        datadome_mode,
                        mtr_runtime,
                        data.get("risk_signals_mode", ""),
                    ),
                    record_traffic=bool(data.get("record_traffic", False)),
                    traffic_dir=str(data.get("traffic_dir", "") or ""),
                    compare_roxy_capture=str(data.get("compare_roxy_capture", "") or ""),
                )
                return self.send_json({"job": job.to_dict(include_logs=False)}, status=HTTPStatus.CREATED)
            except Exception as exc:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

        if path.startswith("/api/jobs/") and path.endswith("/otp"):
            parts = path.split("/")
            job_id = parts[3] if len(parts) > 3 else ""
            job = self.get_authorized_job(job_id)
            if not job:
                return
            try:
                data = self.read_json()
                value = str(data.get("value", "")).strip()
                job.submit_input(value)
                job.add_log("INFO", "已从网页提交验证码/手机号。")
                return self.send_json({"ok": True, "job": job.to_dict(include_logs=False)})
            except Exception as exc:
                return self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

        return self.send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")

    @staticmethod
    def parse_query(query: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in query.split("&"):
            if not part:
                continue
            key, _, value = part.partition("=")
            result[unquote(key)] = unquote(value)
        return result

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except Exception as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise ValueError("请求体太大")
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON 必须是对象")
        return data

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            file_path = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            rel = path.removeprefix("/static/")
            file_path = STATIC_DIR / rel
        else:
            file_path = STATIC_DIR / "index.html"

        try:
            resolved = file_path.resolve()
            resolved.relative_to(STATIC_DIR.resolve())
        except Exception:
            return self.send_error_json(HTTPStatus.FORBIDDEN, "非法路径")

        if not resolved.exists() or not resolved.is_file():
            return self.send_error_json(HTTPStatus.NOT_FOUND, "文件不存在")

        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        data = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'",
        )

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        device_cookie = getattr(self, "_set_device_cookie", "")
        if device_cookie:
            cookie_attrs = [
                f"{DEVICE_COOKIE_NAME}={device_cookie}",
                "Path=/",
                f"Max-Age={DEVICE_COOKIE_MAX_AGE}",
                "SameSite=Strict",
                "HttpOnly",
            ]
            if COOKIE_SECURE:
                cookie_attrs.append("Secure")
            self.send_header(
                "Set-Cookie",
                "; ".join(cookie_attrs),
            )
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(
        self,
        status: HTTPStatus,
        message: str,
        *,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"ok": False, "error": message}
        if code:
            payload["code"] = code
        if extra:
            payload.update(extra)
        self.send_json(payload, status=status)


def get_job(job_id: str) -> WebJob | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)


class WebThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True



def main() -> None:
    parser = argparse.ArgumentParser(description="PayPal Billing Agreement Web UI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8080, help="监听端口，默认 8080")
    args = parser.parse_args()

    configure_logging()
    STATIC_DIR.mkdir(exist_ok=True)

    if PRODUCTION_MODE and not COOKIE_SECURE:
        logger.warning("生产模式建议设置 PAYPAL_WEB_COOKIE_SECURE=1，并通过 HTTPS 反向代理访问。")
    if not ALLOW_DEBUG_LOGS:
        logger.info("DEBUG 日志已在网页端关闭；设置 PAYPAL_WEB_ALLOW_DEBUG_LOGS=1 才允许显示。")

    server = WebThreadingHTTPServer((args.host, args.port), WebHandler)
    url_host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0"} else args.host
    logger.info("Web UI running: http://{}:{}", url_host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Web UI...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
