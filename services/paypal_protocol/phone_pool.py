"""Persistent user-owned phone pool with URL-based OTP polling."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from .countries import normalize_phone


PHONE_POOL_COUNTRIES = ("BR", "GB", "US", "TH", "ID")
PHONE_POOL_USABLE_STATUSES = frozenset({"ready", "code_received"})
PHONE_POOL_FAILURE_STATUSES = frozenset({
    "fetch_error",
    "no_code",
    "timeout",
    "send_failed",
    "otp_rejected",
})
PHONE_POOL_MAX_ENTRIES = 5000
PHONE_POOL_MAX_IMPORT_LINES = 2000
PHONE_POOL_DELIMITER_RE = re.compile(r"\s*-{4,}\s*", re.ASCII)
OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
OTP_PRIORITY_KEYS = frozenset({
    "code",
    "otp",
    "pin",
    "verificationcode",
    "verification_code",
    "smscode",
    "sms_code",
    "message",
    "content",
    "text",
    "body",
})


class PhonePoolError(RuntimeError):
    pass


class PhonePoolCodeTimeout(PhonePoolError):
    pass


class PhonePoolFetchError(PhonePoolError):
    pass


@dataclass
class PhonePoolEntry:
    id: str
    owner_device_id: str
    country: str
    phone: str
    code_url: str
    status: str = "ready"
    status_message: str = "可用"
    reserved_by: str = ""
    fail_count: int = 0
    last_code: str = ""
    last_checked_at: float = 0.0
    last_code_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_public_dict(self) -> dict[str, Any]:
        parsed = urlparse(self.code_url)
        url_path = parsed.path or "/"
        return {
            "id": self.id,
            "country": self.country,
            "phone": self.phone,
            "status": self.status,
            "status_message": self.status_message,
            "available": not self.reserved_by and self.status in PHONE_POOL_USABLE_STATUSES,
            "in_use": bool(self.reserved_by),
            "fail_count": self.fail_count,
            "last_checked_at": self.last_checked_at,
            "last_code_at": self.last_code_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "url_host": parsed.hostname or "",
            "url_hint": f"{parsed.hostname or ''}{url_path}",
        }


def _compact_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(value or "").casefold())


def _otp_candidates(value: Any, *, priority: bool = False) -> list[tuple[int, str]]:
    candidates: list[tuple[int, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_priority = _compact_key(key) in OTP_PRIORITY_KEYS
            candidates.extend(_otp_candidates(item, priority=priority or key_priority))
    elif isinstance(value, list):
        for item in value:
            candidates.extend(_otp_candidates(item, priority=priority))
    elif isinstance(value, (str, int, float)):
        text = str(value)
        lower = text.casefold()
        code_context = any(
            marker in lower
            for marker in ("paypal", "code", "codigo", "código", "otp", "pin", "verification")
        )
        for match in OTP_RE.finditer(text):
            exact_code = isinstance(value, str) and text.strip() == match.group(1)
            score = 2 if priority else (1 if code_context or exact_code else 0)
            if score:
                candidates.append((score, match.group(1)))
    return candidates


def extract_paypal_otp(payload: Any, *, previous_code: str = "") -> str:
    candidates = [
        (score, index, code)
        for index, (score, code) in enumerate(_otp_candidates(payload))
        if code != previous_code
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def _response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _validate_code_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("验证码 URL 必须是完整的 http/https 地址")
    if parsed.username or parsed.password:
        raise ValueError("验证码 URL 不支持 user:password 形式的凭据")
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("验证码 URL 不能指向本机地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("验证码 URL 不能指向内网或保留地址")
    return url


def normalize_pool_phone(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if not raw.startswith("+") and not str(value or "").strip().startswith("00"):
        raise ValueError("手机号必须包含国际区号，例如 +44、+55、+1、+66 或 +62")
    if digits.startswith("55"):
        country = "BR"
    elif digits.startswith("44"):
        country = "GB"
    elif digits.startswith("1"):
        country = "US"
    elif digits.startswith("66"):
        country = "TH"
    elif digits.startswith("62"):
        country = "ID"
    else:
        raise ValueError("仅支持 BR(+55)、GB(+44)、US(+1)、TH(+66)、ID(+62) 手机号")
    phone, _, _ = normalize_phone(f"+{digits}", country)
    return phone, country


def parse_phone_pool_line(line: str) -> tuple[str, str, str]:
    parts = PHONE_POOL_DELIMITER_RE.split(str(line or "").strip(), maxsplit=1)
    if len(parts) != 2:
        raise ValueError("格式应为 手机号----验证码URL")
    phone, country = normalize_pool_phone(parts[0])
    return phone, country, _validate_code_url(parts[1])


class PhonePoolStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries: dict[str, PhonePoolEntry] = {}

    def load(self) -> int:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, json.JSONDecodeError) as exc:
            raise PhonePoolError("手机号号池文件损坏或无法读取") from exc

        restored: dict[str, PhonePoolEntry] = {}
        values = payload.get("entries", []) if isinstance(payload, dict) else []
        for value in values:
            if not isinstance(value, dict):
                continue
            try:
                entry = PhonePoolEntry(**{
                    key: value[key]
                    for key in PhonePoolEntry.__dataclass_fields__
                    if key in value
                })
                entry.phone, entry.country = normalize_pool_phone(entry.phone)
                entry.code_url = _validate_code_url(entry.code_url)
                entry.reserved_by = ""
                if entry.status in {"in_use", "waiting_code"}:
                    entry.status = "ready"
                    entry.status_message = "服务重启后已释放"
                restored[entry.id] = entry
            except (TypeError, ValueError):
                continue
        with self._lock:
            self._entries = restored
            self._persist_locked()
        return len(restored)

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "entries": [asdict(item) for item in self._entries.values()],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def list(self, owner_device_id: str) -> list[dict[str, Any]]:
        with self._lock:
            entries = sorted(
                (item for item in self._entries.values() if item.owner_device_id == owner_device_id),
                key=lambda item: (item.country, -item.updated_at),
            )
            return [item.to_public_dict() for item in entries]

    def import_text(self, owner_device_id: str, text: str) -> dict[str, Any]:
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        if not lines:
            raise ValueError("请粘贴至少一行手机号和验证码 URL")
        if len(lines) > PHONE_POOL_MAX_IMPORT_LINES:
            raise ValueError(f"单次最多导入 {PHONE_POOL_MAX_IMPORT_LINES} 行")

        imported = 0
        updated = 0
        invalid: list[dict[str, Any]] = []
        with self._lock:
            owner_entries = [item for item in self._entries.values() if item.owner_device_id == owner_device_id]
            by_phone = {item.phone: item for item in owner_entries}
            for line_number, line in enumerate(lines, 1):
                try:
                    phone, country, code_url = parse_phone_pool_line(line)
                    existing = by_phone.get(phone)
                    if existing:
                        if existing.reserved_by:
                            raise ValueError("该号码正在被任务占用，暂不能更新")
                        existing.country = country
                        existing.code_url = code_url
                        existing.status = "ready"
                        existing.status_message = "已更新"
                        existing.fail_count = 0
                        existing.updated_at = time.time()
                        updated += 1
                        continue
                    if len(self._entries) >= PHONE_POOL_MAX_ENTRIES:
                        raise ValueError(f"号池最多保存 {PHONE_POOL_MAX_ENTRIES} 个号码")
                    entry = PhonePoolEntry(
                        id=uuid.uuid4().hex[:12],
                        owner_device_id=owner_device_id,
                        country=country,
                        phone=phone,
                        code_url=code_url,
                    )
                    self._entries[entry.id] = entry
                    by_phone[phone] = entry
                    imported += 1
                except ValueError as exc:
                    invalid.append({"line": line_number, "error": str(exc)})
            self._persist_locked()
            total = sum(1 for item in self._entries.values() if item.owner_device_id == owner_device_id)
        return {
            "imported": imported,
            "updated": updated,
            "invalid": invalid,
            "total": total,
        }

    def reserve(
        self,
        owner_device_id: str,
        country: str,
        job_id: str,
        excluded_ids: set[str] | None = None,
    ) -> PhonePoolEntry:
        excluded_ids = excluded_ids or set()
        with self._lock:
            candidates = sorted(
                (
                    item
                    for item in self._entries.values()
                    if item.owner_device_id == owner_device_id
                    and item.country == country
                    and item.id not in excluded_ids
                    and item.status in PHONE_POOL_USABLE_STATUSES
                    and item.reserved_by in {"", job_id}
                ),
                key=lambda item: (
                    0 if item.reserved_by == job_id else 1,
                    item.last_code_at,
                    item.created_at,
                ),
            )
            if not candidates:
                raise PhonePoolError(f"手机号号池没有可用的 {country} 号码")
            entry = candidates[0]
            entry.reserved_by = job_id
            entry.status = "in_use"
            entry.status_message = "任务占用中"
            entry.updated_at = time.time()
            self._persist_locked()
            return PhonePoolEntry(**asdict(entry))

    def _owned_entry_locked(self, entry_id: str, job_id: str) -> PhonePoolEntry:
        entry = self._entries.get(entry_id)
        if not entry or entry.reserved_by != job_id:
            raise PhonePoolError("手机号号池号码未被当前任务占用")
        return entry

    def mark(
        self,
        entry_id: str,
        job_id: str,
        status: str,
        message: str,
        *,
        release: bool = False,
        increment_failure: bool = False,
    ) -> None:
        with self._lock:
            entry = self._owned_entry_locked(entry_id, job_id)
            entry.status = status
            entry.status_message = message
            entry.updated_at = time.time()
            if increment_failure:
                entry.fail_count += 1
            if release:
                entry.reserved_by = ""
            self._persist_locked()

    def release(self, entry_id: str, job_id: str, *, message: str = "可再次使用") -> None:
        with self._lock:
            entry = self._owned_entry_locked(entry_id, job_id)
            entry.reserved_by = ""
            if entry.status in {"in_use", "waiting_code"}:
                entry.status = "ready"
                entry.status_message = message
            entry.updated_at = time.time()
            self._persist_locked()

    def reset(self, owner_device_id: str, entry_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._entries.get(entry_id)
            if not entry or entry.owner_device_id != owner_device_id:
                raise KeyError(entry_id)
            if entry.reserved_by:
                raise PhonePoolError("号码正在被任务占用，无法恢复")
            entry.status = "ready"
            entry.status_message = "已手动恢复"
            entry.fail_count = 0
            entry.updated_at = time.time()
            self._persist_locked()
            return entry.to_public_dict()

    def delete(self, owner_device_id: str, entry_id: str) -> None:
        with self._lock:
            entry = self._entries.get(entry_id)
            if not entry or entry.owner_device_id != owner_device_id:
                raise KeyError(entry_id)
            if entry.reserved_by:
                raise PhonePoolError("号码正在被任务占用，无法删除")
            self._entries.pop(entry_id, None)
            self._persist_locked()

    def count_available(self, owner_device_id: str, country: str) -> int:
        with self._lock:
            return sum(
                1
                for item in self._entries.values()
                if item.owner_device_id == owner_device_id
                and item.country == country
                and not item.reserved_by
                and item.status in PHONE_POOL_USABLE_STATUSES
            )

    def retry_status(self, entry_id: str, job_id: str) -> tuple[bool, str]:
        with self._lock:
            entry = self._entries.get(entry_id)
            if not entry or entry.reserved_by != job_id:
                return False, "当前任务没有已锁定的号池号码"
            if entry.status in PHONE_POOL_FAILURE_STATUSES:
                return False, f"当前号池号码不可继续使用：{entry.status_message}"
            if entry.status not in {"ready", "in_use", "waiting_code", "code_received"}:
                return False, f"当前号池号码状态不可重试：{entry.status}"
            return True, "当前号池号码仍可继续接收新的验证码"

    def wait_for_code(
        self,
        entry_id: str,
        job_id: str,
        *,
        timeout_seconds: float,
        poll_interval: float = 3.0,
        client: httpx.Client | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        owns_client = client is None
        http_client = client or httpx.Client(
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
            trust_env=False,
            headers={"Accept": "application/json,text/plain,*/*"},
        )
        with self._lock:
            entry = self._owned_entry_locked(entry_id, job_id)
            code_url = entry.code_url
            previous_code = entry.last_code
            entry.status = "waiting_code"
            entry.status_message = "等待验证码"
            entry.updated_at = time.time()
            self._persist_locked()

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        successful_reads = 0
        last_error = ""
        try:
            while True:
                try:
                    response = http_client.get(code_url)
                    response.raise_for_status()
                    successful_reads += 1
                    code = extract_paypal_otp(
                        _response_payload(response),
                        previous_code=previous_code,
                    )
                    with self._lock:
                        current = self._owned_entry_locked(entry_id, job_id)
                        current.last_checked_at = time.time()
                        current.updated_at = time.time()
                        if code:
                            current.last_code = code
                            current.last_code_at = time.time()
                            current.status = "code_received"
                            current.status_message = "已收到验证码"
                        self._persist_locked()
                    if code:
                        if on_status:
                            on_status("code_received")
                        return code
                    if on_status:
                        on_status("waiting_code")
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = type(exc).__name__
                    with self._lock:
                        current = self._owned_entry_locked(entry_id, job_id)
                        current.last_checked_at = time.time()
                        current.updated_at = time.time()
                        self._persist_locked()
                    if on_status:
                        on_status("fetch_retry")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(max(0.05, poll_interval), remaining))

            if successful_reads:
                self.mark(
                    entry_id,
                    job_id,
                    "timeout",
                    "等待验证码超时，未收到验证码",
                    increment_failure=True,
                )
                raise PhonePoolCodeTimeout("手机号接口可访问，但在超时时间内未收到验证码")
            self.mark(
                entry_id,
                job_id,
                "fetch_error",
                "验证码接口访问失败",
                increment_failure=True,
            )
            raise PhonePoolFetchError(
                f"手机号验证码接口持续访问失败（{last_error or 'unknown error'}）"
            )
        finally:
            if owns_client:
                http_client.close()
