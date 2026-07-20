from __future__ import annotations

import hashlib
import html
import imaplib
import json
import random
import re
import string
import time
from datetime import datetime, timezone
from email import message_from_bytes, message_from_string, policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any, Callable, TypeVar

from curl_cffi import requests


from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.proxy_service import proxy_settings

DDG_ALIASES_FILE = DATA_DIR / "ddg_aliases.json"
_ddg_aliases_lock = Lock()

OUTLOOK_TOKEN_USED_FILE = DATA_DIR / "outlook_token_used.json"
_outlook_token_state_lock = Lock()
# in_use 超过该秒数视为陈旧（注册进程崩溃残留），可被重新领用
OUTLOOK_IN_USE_STALE_SECONDS = 3600
OUTLOOK_RECORDED_STATES = {"used", "in_use", "token_invalid", "failed", "region_blocked", "existing_account", "flow_blocked", "cloudflare_blocked"}
OUTLOOK_UNAVAILABLE_STATES = {"used", "token_invalid", "failed", "region_blocked", "existing_account", "flow_blocked", "cloudflare_blocked"}
OUTLOOK_ALIAS_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}


def _outlook_base_key(address: str) -> str:
    raw = str(address or "").strip().lower()
    local, sep, domain = raw.partition("@")
    if not sep:
        return raw
    return f"{local.split('+', 1)[0]}@{domain}"


def _load_ddg_aliases() -> set[str]:
    data = read_json(DDG_ALIASES_FILE, [], expected_schema_version=1)
    if isinstance(data, dict):
        data = data.get("items") or []
    if isinstance(data, list):
        return {str(item).strip().lower() for item in data if str(item).strip()}
    return set()


def _save_ddg_aliases(aliases: set[str]) -> None:
    DDG_ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(DDG_ALIASES_FILE, sorted(aliases), schema_version=1)


def _is_ddg_alias_duplicate(address: str) -> bool:
    target = str(address or "").strip().lower()
    if not target:
        return False
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        return target in used


def _record_ddg_alias(address: str) -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _ddg_aliases_lock:
        used = _load_ddg_aliases()
        used.add(target)
        _save_ddg_aliases(used)


def _load_outlook_token_state() -> dict[str, dict[str, Any]]:
    """读取邮箱池状态文件，返回 {email_lower: {state, reason, updated_at}}。

    兼容旧格式：纯字符串列表（历史的“已用邮箱”）会被解释为 used。
    """
    data = read_json(OUTLOOK_TOKEN_USED_FILE, {}, expected_schema_version=1)
    state: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        for item in data:
            key = str(item).strip().lower()
            if key:
                state[key] = {"state": "used", "reason": "", "updated_at": ""}
    elif isinstance(data, dict):
        for key, value in data.items():
            if key == "schema_version":
                continue
            email = str(key).strip().lower()
            if not email:
                continue
            if isinstance(value, dict):
                state[email] = {
                    "state": str(value.get("state") or "used").strip() or "used",
                    "reason": str(value.get("reason") or ""),
                    "updated_at": str(value.get("updated_at") or ""),
                }
            else:
                state[email] = {"state": str(value or "used").strip() or "used", "reason": "", "updated_at": ""}
    return state


def _save_outlook_token_state(state: dict[str, dict[str, Any]]) -> None:
    OUTLOOK_TOKEN_USED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ordered = {key: state[key] for key in sorted(state)}
    write_json(OUTLOOK_TOKEN_USED_FILE, ordered, schema_version=1)


def outlook_token_pool_state() -> dict[str, dict[str, Any]]:
    """Return a copy of the Outlook-token mailbox state map for local pool management."""
    with _outlook_token_state_lock:
        return json.loads(json.dumps(_load_outlook_token_state(), ensure_ascii=False))


def clear_outlook_token_state_for(address: str) -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        if target in store:
            store.pop(target, None)
            _save_outlook_token_state(store)


def _outlook_entry_available(entry: dict[str, Any] | None) -> bool:
    """该邮箱当前是否可领用：未记录、或 in_use 已陈旧、或非终态时可用。"""
    if not isinstance(entry, dict):
        return True
    current = str(entry.get("state") or "")
    if current in OUTLOOK_UNAVAILABLE_STATES:
        return False
    if current == "in_use":
        updated_at = str(entry.get("updated_at") or "")
        try:
            ts = datetime.fromisoformat(updated_at)
            age = (datetime.now(timezone.utc) - (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))).total_seconds()
            return age >= OUTLOOK_IN_USE_STALE_SECONDS
        except Exception:
            return True
    return True


def _outlook_entry_in_use(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    if str(entry.get("state") or "") != "in_use":
        return False
    updated_at = str(entry.get("updated_at") or "")
    try:
        ts = datetime.fromisoformat(updated_at)
        age = (datetime.now(timezone.utc) - (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))).total_seconds()
        return age < OUTLOOK_IN_USE_STALE_SECONDS
    except Exception:
        return False


def _set_outlook_token_state(address: str, state: str, reason: str = "") -> None:
    target = str(address or "").strip().lower()
    if not target:
        return
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        store[target] = {"state": str(state), "reason": str(reason or ""), "updated_at": datetime.now(timezone.utc).isoformat()}
        _save_outlook_token_state(store)


def _release_outlook_token_state(address: str) -> None:
    """把 in_use 释放回未使用（仅当当前确实是 in_use 时）。"""
    target = str(address or "").strip().lower()
    if not target:
        return
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        entry = store.get(target)
        if isinstance(entry, dict) and str(entry.get("state") or "") == "in_use":
            store.pop(target, None)
            _save_outlook_token_state(store)


def reset_outlook_token_pool_state(scope: str = "all") -> int:
    """重置邮箱池状态文件。

    scope=all 清空所有记录；scope=failed 仅清除 failed/token_invalid/in_use（保留 used）。
    返回被清除的条目数。
    """
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        if not store:
            return 0
        if str(scope) == "failed":
            remove = {key for key, value in store.items() if str(value.get("state") or "") in {"failed", "in_use", "region_blocked", "flow_blocked", "cloudflare_blocked"}}
            for key in remove:
                store.pop(key, None)
            _save_outlook_token_state(store)
            return len(remove)
        count = len(store)
        _save_outlook_token_state({})
        return count


def prune_outlook_unused_credentials(credentials: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Return credentials with recorded state, plus the number pruned as unused."""
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
    kept: list[dict[str, str]] = []
    removed = 0
    for credential in credentials:
        key = str(credential.get("email") or "").strip().lower()
        entry = store.get(key) if key else None
        state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
        if state in OUTLOOK_RECORDED_STATES:
            kept.append(credential)
        else:
            removed += 1
    return kept, removed


def outlook_token_pool_stats(pool: list[dict[str, str]] | None = None) -> dict[str, int]:
    """统计邮箱池各状态数量。pool 为该 provider 当前导入的邮箱列表（用于算 unused）。"""
    store = _load_outlook_token_state()
    counts = {"unused": 0, "in_use": 0, "used": 0, "token_invalid": 0, "failed": 0, "region_blocked": 0, "existing_account": 0, "flow_blocked": 0, "cloudflare_blocked": 0}
    if pool:
        for credential in pool:
            entry = store.get(str(credential.get("email") or "").strip().lower())
            state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
            if state in counts:
                counts[state] += 1
            else:
                counts["unused"] += 1
    else:
        for entry in store.values():
            state = str(entry.get("state") or "") if isinstance(entry, dict) else ""
            if state in counts:
                counts[state] += 1
    return counts


def available_outlook_token_count(pool: list[dict[str, str]] | None = None) -> int:
    """Return the number of credentials that can be acquired right now."""
    with _outlook_token_state_lock:
        store = _load_outlook_token_state()
        return sum(
            1
            for credential in pool or []
            if _outlook_entry_available(store.get(str(credential.get("email") or "").strip().lower()))
        )


ResultT = TypeVar("ResultT")
domain_lock = Lock()
provider_lock = Lock()
domain_index = 0
provider_index = 0
cloudmail_token_lock = Lock()
cloudmail_token_cache: dict[str, tuple[str, float]] = {}
openai_cpa_pool_lock = Lock()
openai_cpa_code_pool: dict[str, list[dict[str, Any]]] = {}


def _config(mail_config: dict) -> dict:
    return {
        "request_timeout": float(mail_config.get("request_timeout") or 30),
        "wait_timeout": float(mail_config.get("wait_timeout") or 30),
        "wait_interval": float(mail_config.get("wait_interval") or 2),
        "user_agent": str(mail_config.get("user_agent") or "Mozilla/5.0"),
        "proxy": str(mail_config.get("proxy") or "").strip(),
        "register_mode": str(mail_config.get("register_mode") or "").strip().lower(),
        "mailbox_group": str(mail_config.get("mailbox_group") or "").strip(),
    }


def _random_mailbox_name() -> str:
    return f"{''.join(random.choices(string.ascii_lowercase, k=5))}{''.join(random.choices(string.digits, k=random.randint(1, 3)))}{''.join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))}"


def _random_subdomain_label() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(4, 10)))


def push_openai_cpa_message(address: str, payload: dict[str, Any]) -> int:
    """接收 OpenAI-CPA 内存池 Worker/Webhook 转发来的邮件内容。"""
    target = str(address or payload.get("email") or payload.get("to") or payload.get("address") or "").strip().lower()
    if not target:
        return 0
    message = {
        "provider": "openai_cpa",
        "mailbox": target,
        "message_id": str(payload.get("message_id") or payload.get("id") or hashlib.sha256(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8", errors="ignore")).hexdigest()),
        "subject": str(payload.get("subject") or ""),
        "sender": str(payload.get("sender") or payload.get("from") or ""),
        "text_content": str(payload.get("text_content") or payload.get("text") or payload.get("body") or ""),
        "html_content": str(payload.get("html_content") or payload.get("html") or payload.get("body_html") or ""),
        "received_at": datetime.now(timezone.utc),
        "to": target,
        "raw": payload,
    }
    with openai_cpa_pool_lock:
        bucket = openai_cpa_code_pool.setdefault(target, [])
        bucket.append(message)
        openai_cpa_code_pool[target] = bucket[-20:]
        return len(openai_cpa_code_pool[target])


def _next_domain(domains: list[str]) -> str:
    global domain_index
    domains = [str(item).strip() for item in domains if str(item).strip()]
    if not domains:
        raise RuntimeError("mail.domain 不能为空")
    if len(domains) == 1:
        return domains[0]
    with domain_lock:
        value = domains[domain_index % len(domains)]
        domain_index = (domain_index + 1) % len(domains)
        return value


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _create_session(conf: dict):
    proxy = str(conf.get("proxy") or "").strip()
    kwargs = {"impersonate": "chrome", "verify": False}
    if proxy:
        kwargs["proxy"] = proxy
    return requests.Session(**kwargs)


def _parse_received_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        date = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        date = parsedate_to_datetime(text)
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_content(data: dict[str, Any]) -> tuple[str, str]:
    text_content = str(data.get("text_content") or data.get("text") or data.get("body") or data.get("content") or "")
    html_content = str(data.get("html_content") or data.get("html") or data.get("html_body") or data.get("body_html") or "")
    if text_content or html_content:
        return text_content, html_content
    raw = data.get("raw")
    if not isinstance(raw, str) or not raw.strip():
        return "", ""
    try:
        parsed = message_from_string(raw, policy=policy.default)
    except Exception:
        return raw, ""
    plain: list[str] = []
    html: list[str] = []
    for part in parsed.walk() if parsed.is_multipart() else [parsed]:
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_content()
        except Exception:
            payload = ""
        if not payload:
            continue
        if part.get_content_type() == "text/html":
            html.append(str(payload))
        else:
            plain.append(str(payload))
    return "\n".join(plain).strip(), "\n".join(html).strip()


def _extract_text_candidates(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key in ("address", "email", "name", "value"):
            if value.get(key):
                out.extend(_extract_text_candidates(value.get(key)))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_extract_text_candidates(item))
        return out
    return []


def _message_matches_email(data: dict[str, Any], email: str) -> bool:
    target = str(email or "").strip().lower()
    candidates: list[str] = []
    for key in ("to", "toEmail", "mailTo", "receiver", "receivers", "address", "email", "envelope_to"):
        if key in data:
            candidates.extend(_extract_text_candidates(data.get(key)))
    return not target or not candidates or any(target in str(item).strip().lower() for item in candidates if str(item).strip())


def _extract_code(message: dict[str, Any]) -> str | None:
    content = f"{message.get('subject', '')}\n{message.get('text_content', '')}\n{message.get('html_content', '')}".strip()
    if not content:
        return None
    match = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>", content, re.I)
    if match:
        return match.group(1)
    match = re.search(r"(?:Verification code|code is|代码为|验证码)[:\s]*(\d{6})", content, re.I)
    if match and match.group(1) != "177010":
        return match.group(1)
    for code in re.findall(r">\s*(\d{6})\s*<|(?<![#&])\b(\d{6})\b", content):
        value = code[0] or code[1]
        if value and value != "177010":
            return value
    return None


def _extract_link(message: dict[str, Any], keyword: str = "") -> str | None:
    content = f"{message.get('subject', '')}\n{message.get('text_content', '')}\n{message.get('html_content', '')}".strip()
    if not content:
        return None
    lowered = content.lower()
    keyword_l = str(keyword or "").strip().lower()
    if keyword_l and keyword_l not in lowered:
        return None
    combined = html.unescape(content)
    urls = [html.unescape(raw).rstrip(").,;\"'<>") for raw in re.findall(r"https?://[^\s<>\"']+", combined, re.I)]
    if not urls:
        return None
    hints = ("k12-invite", "invite", "workspace", "join", "continue", "callback", "auth", "login")
    for url in urls:
        url_l = url.lower()
        if keyword_l and keyword_l in url_l:
            return url
    for url in urls:
        url_l = url.lower()
        if any(hint in url_l for hint in hints):
            return url
    return urls[0]


def _message_tracking_ref(message: dict[str, Any]) -> str:
    provider = str(message.get("provider") or "").strip()
    mailbox = str(message.get("mailbox") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        return f"id:{provider}:{mailbox}:{message_id}"
    received_at = message.get("received_at")
    received_value = received_at.isoformat() if isinstance(received_at, datetime) else str(received_at or "")
    content = "\n".join(str(message.get(key) or "") for key in ("subject", "sender", "text_content", "html_content"))
    digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    return f"content:{provider}:{mailbox}:{received_value}:{digest}"


def _message_before_code_boundary(mailbox: dict[str, Any], message: dict[str, Any]) -> bool:
    boundary = mailbox.get("_code_not_before")
    received_at = message.get("received_at")
    if not isinstance(boundary, datetime) or not isinstance(received_at, datetime):
        return False
    if not received_at.tzinfo:
        received_at = received_at.replace(tzinfo=timezone.utc)
    return received_at < boundary


class BaseMailProvider:
    name = "unknown"

    def __init__(self, conf: dict, provider_ref: str = ""):
        self.conf = conf
        self.provider_ref = provider_ref
        self.mailbox_group = str(conf.get("mailbox_group") or "").strip()

    def wait_for(self, mailbox: dict[str, Any], on_message: Callable[[dict[str, Any]], ResultT | None]) -> ResultT | None:
        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            message = self.fetch_latest_message(mailbox)
            if message:
                result = on_message(message)
                if result is not None:
                    return result
            time.sleep(max(0.2, self.conf["wait_interval"]))
        return None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}

        def extract_unseen_code(message: dict[str, Any]) -> str | None:
            if _message_before_code_boundary(mailbox, message):
                return None
            ref = _message_tracking_ref(message)
            if ref in seen_refs:
                return None
            code = _extract_code(message)
            if code:
                seen_value.append(ref)
                seen_refs.add(ref)
            return code

        return self.wait_for(mailbox, extract_unseen_code)

    def wait_for_link(self, mailbox: dict[str, Any], keyword: str = "") -> str | None:
        seen_value = mailbox.setdefault("_seen_link_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_link_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}

        def extract_unseen_link(message: dict[str, Any]) -> str | None:
            if _message_before_code_boundary(mailbox, message):
                return None
            ref = _message_tracking_ref(message)
            if ref in seen_refs:
                return None
            link = _extract_link(message, keyword)
            seen_value.append(ref)
            seen_refs.add(ref)
            return link

        return self.wait_for(mailbox, extract_unseen_link)

    def close(self) -> None:
        pass


class CloudflareTempMailProvider(BaseMailProvider):
    name = "cloudflare_temp_email"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_password = str(entry["admin_password"]).strip()
        self.domain = entry.get("domain") or []
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"Content-Type": "application/json", "User-Agent": self.conf["user_agent"], **(headers or {})}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=proxy_settings.verify_tls())
        if resp.status_code not in expected:
            raise RuntimeError(f"CloudflareTempMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request("POST", "/admin/new_address", headers={"x-admin-auth": self.admin_password}, payload={"enablePrefix": True, "name": username or _random_mailbox_name(), "domain": _next_domain(self.domain)})
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError("CloudflareTempMail 缺少 address 或 jwt")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def get_existing_mailbox(self, email: str) -> dict[str, Any]:
        """通过管理员密码获取已有邮箱地址的 JWT，用于查询邮件。"""
        data = self._request("POST", "/admin/get_address", headers={"x-admin-auth": self.admin_password}, payload={"address": email})
        address = str(data.get("address") or "").strip()
        token = str(data.get("jwt") or "").strip()
        if not address or not token:
            raise RuntimeError(f"CloudflareTempMail 无法获取已有邮箱 {email} 的 JWT")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/api/mails", headers={"Authorization": f"Bearer {mailbox['token']}"}, params={"limit": 10, "offset": 0})
        raw = list(data.get("results") or []) if isinstance(data, dict) else data if isinstance(data, list) else []
        messages = [item for item in raw if isinstance(item, dict) and _message_matches_email(item, str(mailbox.get("address") or ""))]
        if not messages:
            return None
        item = messages[0]
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or item.get("_id") or ""), "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


class DDGMailProvider(BaseMailProvider):
    name = "ddg_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.ddg_token = str(entry["ddg_token"]).strip()
        self.cf_api_base = str(entry.get("api_base") or entry.get("cf_api_base") or "").rstrip("/")
        self.cf_inbox_jwt = str(entry.get("cf_inbox_jwt") or "").strip()
        self.cf_admin_password = str(entry.get("admin_password") or "").strip()
        self.cf_api_key = str(entry.get("cf_api_key") or "").strip()
        self.cf_auth_mode = str(entry.get("cf_auth_mode") or "none").strip().lower()
        self.cf_domain = entry.get("cf_domain") or []
        self.cf_create_path = str(entry.get("cf_create_path") or "/api/new_address").strip()
        self.cf_messages_path = str(entry.get("cf_messages_path") or "/api/mails").strip()
        self.session = _create_session(conf)

    def _cf_build_headers(self, content_type: bool = False) -> dict:
        headers = {"Content-Type": "application/json"} if content_type else {}
        if self.cf_api_key:
            if self.cf_auth_mode == "x-api-key":
                headers["X-API-Key"] = self.cf_api_key
            elif self.cf_auth_mode != "none":
                headers["Authorization"] = f"Bearer {self.cf_api_key}"
        return headers

    def _cf_request(self, method: str, path: str, headers: dict | None = None, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)) -> dict:
        merged_headers = {**self._cf_build_headers(True), **(headers or {}), "User-Agent": self.conf["user_agent"]}
        if self.cf_admin_password and method.upper() in ("POST",):
            merged_headers["x-admin-auth"] = self.cf_admin_password
        if self.cf_api_key and self.cf_auth_mode == "query-key":
            params = {**(params or {}), "key": self.cf_api_key}
        resp = self.session.request(method.upper(), f"{self.cf_api_base}{path}", headers=merged_headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=proxy_settings.verify_tls())
        if resp.status_code not in expected:
            raise RuntimeError(f"DDGMail CF请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    def _ddg_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        resp = self.session.request(method.upper(), f"https://quack.duckduckgo.com{path}", headers={"Authorization": f"Bearer {self.ddg_token}", "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, json=payload, timeout=self.conf["request_timeout"], verify=proxy_settings.verify_tls())
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"DDG API请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return resp.json()

    def _cf_list_payload(self, data: Any) -> list:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "hydra:member", "data", "messages"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
                if isinstance(value, dict) and isinstance(value.get("messages"), list):
                    return value["messages"]
        return []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        ddg_data = self._ddg_request("POST", "/api/email/addresses", payload={})
        ddg_address_part = str(ddg_data.get("address") or "").strip()
        if not ddg_address_part:
            raise RuntimeError("DDG API 返回无 address 字段")
        ddg_address = f"{ddg_address_part}@duck.com"

        if _is_ddg_alias_duplicate(ddg_address):
            raise RuntimeError(f"[{self.label}] DDG日上限已达，别名 {ddg_address} 已存在，自动切换邮箱提供商")

        _record_ddg_alias(ddg_address)

        if not self.cf_inbox_jwt:
            raise RuntimeError("DDGMail 需要 cf_inbox_jwt（DDG 转发目标的固定收件箱 JWT），请在邮箱配置中填写 CF Inbox JWT")

        return {"provider": self.name, "provider_ref": self.provider_ref, "address": ddg_address, "token": self.cf_inbox_jwt, "label": self.label}

    def _parse_raw_recipient(self, raw_text: str) -> str:
        if not raw_text:
            return ""
        match = re.search(r"^To:\s*(.+?)$", raw_text, re.MULTILINE | re.IGNORECASE)
        if match:
            addr = match.group(1).strip()
            addr = re.sub(r"\s*<[^>]*>", "", addr)
            return addr.strip().lower()
        try:
            parsed = message_from_string(raw_text, policy=policy.default)
            return str(parsed.get("To") or "").strip().lower()
        except Exception:
            return ""

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        target_address = str(mailbox.get("address") or "").strip().lower()
        data = self._cf_request("GET", self.cf_messages_path, headers={"Authorization": f"Bearer {mailbox['token']}"}, params={"limit": 30, "offset": 0})
        raw_list = self._cf_list_payload(data)
        messages = [item for item in raw_list if isinstance(item, dict)]
        if not messages:
            return None

        for item in messages:
            message_id = str(item.get("id") or item.get("msgid") or item.get("_id") or "")
            raw_text = str(item.get("raw") or "")
            raw_recipient = self._parse_raw_recipient(raw_text)
            if target_address and raw_recipient and target_address not in raw_recipient:
                continue
            text_content, html_content = _extract_content(item)
            subject = str(item.get("subject") or "")
            sender = item.get("from") or item.get("sender") or item.get("source") or ""
            if isinstance(sender, dict):
                sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
            if raw_text and (not subject or not sender or subject == sender == ""):
                try:
                    parsed = message_from_string(raw_text, policy=policy.default)
                    if not subject:
                        subject = str(parsed.get("Subject") or "")
                    if not sender:
                        sender = str(parsed.get("From") or "")
                except Exception:
                    pass
            return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": subject, "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

        return None

    def close(self) -> None:
        self.session.close()


class _NonRetryableCloudMailGenError(RuntimeError):
    pass


class CloudMailGenProvider(BaseMailProvider):
    name = "cloudmail_gen"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.admin_email = str(entry.get("admin_email") or "").strip()
        self.admin_password = str(entry.get("admin_password") or "").strip()
        self.domain = _normalize_string_list(entry.get("domain"))
        self.subdomain = _normalize_string_list(entry.get("subdomain"))
        self.email_prefix = str(entry.get("email_prefix") or "").strip()
        self.session = _create_session(conf)

    def _clear_token_cache(self) -> None:
        with cloudmail_token_lock:
            cloudmail_token_cache.pop(self._cache_key(), None)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    def _request(
        self,
        method: str,
        path: str,
        headers: dict | None = None,
        params: dict | None = None,
        payload: dict | None = None,
        expected: tuple[int, ...] = (200,),
    ):
        last_error = ""
        attempts = 3
        for attempt in range(attempts):
            try:
                resp = self.session.request(
                    method.upper(),
                    f"{self.api_base}{path}",
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": self.conf["user_agent"],
                        **(headers or {}),
                    },
                    params=params,
                    json=payload,
                    timeout=self.conf["request_timeout"],
                    verify=proxy_settings.verify_tls(),
                )
                if resp.status_code in expected:
                    return {} if resp.status_code == 204 else resp.json()
                message = f"CloudMailGen 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}"
                if not self._is_retryable_status(int(resp.status_code)):
                    raise _NonRetryableCloudMailGenError(message)
                last_error = message
            except _NonRetryableCloudMailGenError as error:
                raise RuntimeError(str(error)) from error
            except Exception as error:
                last_error = f"CloudMailGen 请求异常: {method} {path}, error={error}"
            if attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(last_error or f"CloudMailGen 请求失败: {method} {path}")

    def _cache_key(self) -> str:
        return f"{self.api_base}|{self.admin_email}"

    @staticmethod
    def _is_success_payload(data: Any) -> bool:
        return isinstance(data, dict) and data.get("code") == 200

    def _fetch_email_list(self, token: str, address: str) -> dict:
        data = self._request(
            "POST",
            "/api/public/emailList",
            headers={"Authorization": token},
            payload={"toEmail": address, "size": 20, "timeSort": "desc"},
        )
        if not isinstance(data, dict):
            raise RuntimeError(f"CloudMailGen emailList 返回异常: {data}")
        return data

    def _get_token(self) -> str:
        if not self.admin_email or not self.admin_password:
            raise RuntimeError("CloudMailGen 缺少 admin_email 或 admin_password")
        cache_key = self._cache_key()
        now = time.time()
        with cloudmail_token_lock:
            cached = cloudmail_token_cache.get(cache_key)
            if cached and now < cached[1] - 300:
                return cached[0]
        data = self._request(
            "POST",
            "/api/public/genToken",
            payload={"email": self.admin_email, "password": self.admin_password},
        )
        token = ""
        if isinstance(data, dict) and data.get("code") == 200:
            token = str((data.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError(f"CloudMailGen genToken 返回异常: {data}")
        with cloudmail_token_lock:
            cloudmail_token_cache[cache_key] = (token, now + 24 * 3600)
        return token

    def _resolve_address(self, username: str | None = None) -> str:
        domain = _next_domain(self.domain)
        if self.subdomain:
            domain = f"{random.choice(self.subdomain)}.{domain}"
        if username:
            local_part = username
        elif self.email_prefix:
            local_part = f"{self.email_prefix}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
        else:
            local_part = _random_mailbox_name()
        return f"{local_part}@{domain}"

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.domain:
            raise RuntimeError("CloudMailGen 需要至少配置一个 domain")
        address = self._resolve_address(username)
        token = self._get_token()
        self._request(
            "POST",
            "/api/public/addUser",
            headers={"Authorization": token},
            payload={"list": [{"email": address}]},
        )
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        address = str(mailbox.get("address") or "").strip()
        if not address:
            raise RuntimeError("CloudMailGen 缺少 address")
        token = self._get_token()
        data = self._fetch_email_list(token, address)
        if not self._is_success_payload(data):
            self._clear_token_cache()
            token = self._get_token()
            data = self._fetch_email_list(token, address)
        if not self._is_success_payload(data):
            raise RuntimeError(f"CloudMailGen emailList 返回异常: {data}")
        items = data.get("data") or []
        messages = [item for item in items if isinstance(item, dict) and _message_matches_email(item, address)]
        if not messages:
            return None
        item = messages[0]
        text_content, html_content = _extract_content(item)
        return {
            "provider": self.name,
            "mailbox": address,
            "message_id": str(item.get("id") or item.get("_id") or item.get("messageId") or item.get("emailId") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": str(item.get("from") or item.get("sender") or item.get("sendEmail") or ""),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(
                item.get("createdAt") or item.get("created_at") or item.get("createTime") or item.get("receivedAt") or item.get("date") or item.get("timestamp")
            ),
            "to": item.get("to") or item.get("toEmail") or item.get("mailTo"),
            "raw": item,
        }

    def close(self) -> None:
        self.session.close()


class TempMailLolProvider(BaseMailProvider):
    name = "tempmail_lol"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry.get("api_key") or "").strip()
        self.domain = [str(item).strip() for item in (entry.get("domain") or []) if str(item).strip()]
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    @staticmethod
    def _resolve_domain(domain: str) -> tuple[str, bool]:
        text = str(domain or "").strip().lower()
        if text.startswith("*.") and len(text) > 2:
            return f"{_random_subdomain_label()}.{text[2:]}", True
        return text, False

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"https://api.tempmail.lol/v2{path}", params=params, json=payload, timeout=self.conf["request_timeout"], verify=proxy_settings.verify_tls())
        if resp.status_code not in expected:
            raise RuntimeError(f"TempMail.lol 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"TempMail.lol {method} {path} 返回结构不是对象")
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.domain:
            domain, force_random_prefix = self._resolve_domain(random.choice(self.domain))
            payload["domain"] = domain
            if force_random_prefix:
                payload["prefix"] = _random_mailbox_name()
        if username and "prefix" not in payload:
            payload["prefix"] = username
        data = self._request("POST", "/inbox/create", payload=payload, expected=(200, 201))
        address = str(data.get("address") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise RuntimeError("TempMail.lol 缺少 address 或 token")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/inbox", params={"token": mailbox["token"]})
        items = data.get("emails") or data.get("messages") or []
        messages = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if not messages:
            return None
        item = max(messages, key=lambda value: ((_parse_received_at(value.get("created_at") or value.get("createdAt") or value.get("date") or value.get("received_at") or value.get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(), str(value.get("id") or value.get("token") or "")))
        text_content, html_content = _extract_content(item)
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or item.get("token") or ""), "subject": str(item.get("subject") or ""), "sender": str(item.get("from") or item.get("from_address") or ""), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("created_at") or item.get("createdAt") or item.get("date") or item.get("received_at") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


class DuckMailProvider(BaseMailProvider):
    name = "duckmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry["api_key"]).strip()
        self.default_domain = str(entry.get("default_domain") or "duckmail.sbs").strip() or "duckmail.sbs"
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", use_api_key: bool = False, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {self.api_key if use_api_key else token}"} if use_api_key or token else {}
        resp = self.session.request(method.upper(), f"https://api.duckmail.sbs{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=proxy_settings.verify_tls())
        if resp.status_code not in expected:
            raise RuntimeError(f"DuckMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        return {} if resp.status_code == 204 else resp.json()

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("hydra:member") or data.get("member") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        password = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        address = f"{username or _random_mailbox_name()}@{self.default_domain}"
        payload = {"address": address, "password": password}
        account = self._request("POST", "/accounts", use_api_key=True, payload=payload)
        token_data = self._request("POST", "/token", use_api_key=True, payload=payload)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": str(token_data.get("token") or ""), "password": password, "account_id": str(account.get("id") or "")}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"page": 1})
        items = self._items(data)
        if not items:
            return None
        item = items[0]
        message_id = str(item.get("id") or item.get("@id") or "").replace("/messages/", "")
        if message_id:
            item = self._request("GET", f"/messages/{message_id}", token=str(mailbox.get("token") or ""))
        sender = item.get("from") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("name") or ""
        html_content = item.get("html") or ""
        if isinstance(html_content, list):
            html_content = "".join(str(value) for value in html_content)
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": str(item.get("text") or item.get("text_content") or ""), "html_content": str(html_content), "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date")), "raw": item}

    def close(self) -> None:
        self.session.close()


class GptMailProvider(BaseMailProvider):
    name = "gptmail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_key = str(entry["api_key"]).strip()
        self.default_domain = str(entry.get("default_domain") or "").strip()
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json", "X-API-Key": self.api_key})

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None):
        query = dict(params or {})
        resp = self.session.request(method.upper(), f"https://mail.chatgpt.org.uk{path}", params=query, json=payload, timeout=self.conf["request_timeout"], verify=proxy_settings.verify_tls())
        if resp.status_code != 200:
            raise RuntimeError(f"GPTMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        return data["data"] if isinstance(data, dict) and "data" in data else data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload = {key: value for key, value in {"prefix": username, "domain": self.default_domain}.items() if value}
        data = self._request("POST" if payload else "GET", "/api/generate-email", payload=payload or None)
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": str(data["email"])}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/api/emails", params={"email": mailbox["address"]})
        emails = data if isinstance(data, list) else data.get("emails") or []
        if not emails:
            return None
        item = max(emails, key=lambda value: (float(value.get("timestamp") or 0), str(value.get("id") or "")))
        if item.get("id"):
            item = self._request("GET", f"/api/email/{item['id']}")
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": str(item.get("id") or ""), "subject": str(item.get("subject") or ""), "sender": str(item.get("from_address") or ""), "text_content": str(item.get("content") or ""), "html_content": str(item.get("html_content") or ""), "received_at": _parse_received_at(item.get("timestamp") or item.get("created_at")), "raw": item}

    def close(self) -> None:
        self.session.close()


class MoEmailProvider(BaseMailProvider):
    name = "moemail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.expiry_time = int(entry.get("expiry_time") or 0)
        self.session = _create_session(conf)

    def _request(self, method: str, path: str, params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers={"X-API-Key": self.api_key, "Content-Type": "application/json", "User-Agent": self.conf["user_agent"]}, params=params, json=payload, timeout=self.conf["request_timeout"], verify=proxy_settings.verify_tls())
        if resp.status_code not in expected:
            raise RuntimeError(f"MoEmail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"MoEmail {method} {path} 返回结构不是对象")
        return data

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        data = self._request("POST", "/api/emails/generate", payload={"name": username or _random_mailbox_name(), "expiryTime": self.expiry_time, "domain": _next_domain(self.domain)}, expected=(200, 201))
        address = str(data.get("email") or "").strip()
        email_id = str(data.get("id") or data.get("email_id") or "").strip()
        if not address or not email_id:
            raise RuntimeError("MoEmail 缺少 email 或 id")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "email_id": email_id}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        email_id = str(mailbox.get("email_id") or "").strip()
        if not email_id:
            raise RuntimeError("MoEmail 缺少 email_id")
        data = self._request("GET", f"/api/emails/{email_id}")
        items = data.get("messages") or []
        messages = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        if not messages:
            return None
        _, item = max(enumerate(messages), key=lambda pair: (((_parse_received_at(pair[1].get("createdAt") or pair[1].get("created_at") or pair[1].get("receivedAt") or pair[1].get("date") or pair[1].get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp()), pair[0]))
        message_id = str(item.get("id") or item.get("message_id") or item.get("_id") or "").strip()
        detail = self._request("GET", f"/api/emails/{email_id}/{message_id}") if message_id else {"message": item}
        message = detail.get("message") if isinstance(detail.get("message"), dict) else detail
        text_content, html_content = _extract_content(message)
        sender = message.get("from") or message.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(message.get("subject") or item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(message.get("createdAt") or message.get("created_at") or message.get("receivedAt") or message.get("date") or message.get("timestamp") or item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": detail}

    def close(self) -> None:
        self.session.close()


class InbucketMailProvider(BaseMailProvider):
    name = "inbucket"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry["api_base"]).rstrip("/")
        raw_domains = entry.get("domain") or []
        if isinstance(raw_domains, list):
            self.domain = [str(item).strip() for item in raw_domains if str(item).strip()]
        else:
            self.domain = [str(raw_domains).strip()] if str(raw_domains).strip() else []
        self.random_subdomain = bool(entry.get("random_subdomain", True))
        self.session = _create_session(conf)
        self.session.headers.update({
            "User-Agent": conf["user_agent"],
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, expected: tuple[int, ...] = (200,)):
        resp = self.session.request(
            method.upper(),
            f"{self.api_base}{path}",
            timeout=self.conf["request_timeout"],
            verify=proxy_settings.verify_tls(),
        )
        if resp.status_code not in expected:
            raise RuntimeError(f"Inbucket 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        content_type = str(resp.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            return resp.json()
        return resp.text

    def _resolve_domain(self) -> str:
        if self.domain:
            return _next_domain(self.domain)
        raise RuntimeError("Inbucket 需要至少配置一个 domain")

    def _mailbox_name(self, address: str) -> str:
        local_part, _, _ = str(address or "").partition("@")
        return local_part.strip()

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        local_part = username or _random_mailbox_name()
        base_domain = self._resolve_domain()
        domain = f"{_random_subdomain_label()}.{base_domain}" if self.random_subdomain else base_domain
        address = f"{local_part}@{domain}"
        mailbox_name = self._mailbox_name(address)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "base_domain": base_domain,
            "mailbox_name": mailbox_name,
        }

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        mailbox_name = str(mailbox.get("mailbox_name") or self._mailbox_name(str(mailbox.get("address") or ""))).strip()
        if not mailbox_name:
            raise RuntimeError("Inbucket 缺少 mailbox_name")
        data = self._request("GET", f"/api/v1/mailbox/{mailbox_name}")
        items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        if not items:
            return None
        items.sort(
            key=lambda value: (
                (_parse_received_at(value.get("date")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(),
                str(value.get("id") or ""),
            ),
            reverse=True,
        )
        address = str(mailbox.get("address") or "").strip()
        for item in items:
            message_id = str(item.get("id") or "").strip()
            if not message_id:
                continue
            detail = self._request("GET", f"/api/v1/mailbox/{mailbox_name}/{message_id}")
            if not isinstance(detail, dict):
                continue
            header = detail.get("header") if isinstance(detail.get("header"), dict) else {}
            body = detail.get("body") if isinstance(detail.get("body"), dict) else {}
            normalized = {
                "provider": self.name,
                "mailbox": mailbox_name,
                "message_id": message_id,
                "subject": str(detail.get("subject") or item.get("subject") or ""),
                "sender": str(detail.get("from") or item.get("from") or ""),
                "text_content": str(body.get("text") or ""),
                "html_content": str(body.get("html") or ""),
                "received_at": _parse_received_at(detail.get("date") or item.get("date")),
                "to": header.get("To") if isinstance(header, dict) else None,
                "raw": detail,
            }
            if _message_matches_email(normalized, address):
                return normalized
        return None

    def close(self) -> None:
        self.session.close()


class YydsMailProvider(BaseMailProvider):
    name = "yyds_mail"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.api_base = str(entry.get("api_base") or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(entry["api_key"]).strip()
        self.domain = [str(item).strip() for item in (entry.get("domain") or []) if str(item).strip()]
        self.subdomain = str(entry.get("subdomain") or "").strip()
        self.wildcard = bool(entry.get("wildcard"))
        self.session = _create_session(conf)
        self.session.headers.update({"User-Agent": conf["user_agent"], "Accept": "application/json", "Content-Type": "application/json"})

    def _request(self, method: str, path: str, token: str = "", params: dict | None = None, payload: dict | None = None, expected: tuple[int, ...] = (200, 201, 204)):
        headers = {"Authorization": f"Bearer {token}"} if token else {"X-API-Key": self.api_key}
        resp = self.session.request(method.upper(), f"{self.api_base}{path}", headers=headers, params=params, json=payload, timeout=self.conf["request_timeout"], verify=proxy_settings.verify_tls())
        if resp.status_code not in expected:
            raise RuntimeError(f"YYDSMail 请求失败: {method} {path}, HTTP {resp.status_code}, body={resp.text[:300]}")
        if resp.status_code == 204:
            return {}
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"YYDSMail 请求失败: {data.get('errorCode') or data.get('error')}")
        return data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)) else data

    @staticmethod
    def _items(data):
        return data if isinstance(data, list) else data.get("items") or data.get("messages") or data.get("data") or []

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        payload = {"localPart": username or _random_mailbox_name()}
        if self.domain:
            payload["domain"] = _next_domain(self.domain)
        if self.subdomain:
            payload["subdomain"] = self.subdomain
        data = self._request("POST", "/accounts/wildcard" if self.wildcard else "/accounts", payload=payload)
        address = str(data.get("address") or data.get("email") or "").strip()
        token = str(data.get("token") or data.get("temp_token") or data.get("tempToken") or data.get("access_token") or "").strip()
        if not address or not token:
            raise RuntimeError("YYDSMail 缺少 address 或 token")
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "token": token, "account_id": str(data.get("id") or "")}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        data = self._request("GET", "/messages", token=str(mailbox.get("token") or ""), params={"address": mailbox["address"]})
        messages = [item for item in self._items(data) if isinstance(item, dict)]
        if not messages:
            return None
        item = max(messages, key=lambda value: ((_parse_received_at(value.get("createdAt") or value.get("created_at") or value.get("receivedAt") or value.get("date") or value.get("timestamp")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(), str(value.get("id") or "")))
        message_id = str(item.get("id") or item.get("message_id") or "").strip()
        if message_id:
            item = self._request("GET", f"/messages/{message_id}", token=str(mailbox.get("token") or ""), params={"address": mailbox["address"]})
        text_content, html_content = _extract_content(item)
        sender = item.get("from") or item.get("sender") or ""
        if isinstance(sender, dict):
            sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
        return {"provider": self.name, "mailbox": mailbox["address"], "message_id": message_id, "subject": str(item.get("subject") or ""), "sender": str(sender), "text_content": text_content, "html_content": html_content, "received_at": _parse_received_at(item.get("createdAt") or item.get("created_at") or item.get("receivedAt") or item.get("date") or item.get("timestamp")), "raw": item}

    def close(self) -> None:
        self.session.close()


OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
OUTLOOK_GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
OUTLOOK_IMAP_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
OUTLOOK_DEFAULT_IMAP_HOST = "outlook.office365.com"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_IMAP_SCOPE = "https://mail.google.com/"
GMAIL_DEFAULT_IMAP_HOST = "imap.gmail.com"
GMAIL_ALIAS_DOMAINS = {"gmail.com", "googlemail.com"}


class OutlookTokenError(RuntimeError):
    """refresh_token 换取 access_token 失败（凭据失效/权限不对），与“读邮件失败”区分。"""


def _clean_outlook_value(value: str) -> str:
    return str(value or "").replace("﻿", "").replace(" ", " ").strip()


def parse_outlook_credentials(text: str) -> list[dict[str, str]]:
    """解析邮箱池文本，每行格式：email----password----client_id----refresh_token。"""
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = _clean_outlook_value(raw_line)
        if not line or "----" not in line:
            continue
        parts = [_clean_outlook_value(part) for part in line.split("----", 3)]
        if len(parts) != 4:
            continue
        email, password, client_id, refresh_token = parts
        if "@" not in email or not client_id or not refresh_token:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        credentials.append({"email": email, "password": password, "client_id": client_id, "refresh_token": refresh_token})
    return credentials


def parse_gmail_credentials(text: str) -> list[dict[str, str]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    credentials: list[dict[str, str]] = []
    if raw.startswith(("[", "{")):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = data.get("items") or data.get("mailboxes") or [data]
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    email = _clean_outlook_value(item.get("email") or item.get("address") or "")
                    client_id = _clean_outlook_value(item.get("client_id") or "")
                    refresh_token = _clean_outlook_value(item.get("refresh_token") or "")
                    if "@" in email and client_id and refresh_token:
                        credentials.append({
                            "email": email,
                            "password": _clean_outlook_value(item.get("password") or ""),
                            "client_id": client_id,
                            "client_secret": _clean_outlook_value(item.get("client_secret") or ""),
                            "refresh_token": refresh_token,
                        })
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if credentials:
        return credentials
    seen: set[str] = set()
    for raw_line in raw.splitlines():
        line = _clean_outlook_value(raw_line)
        if not line or "@" not in line:
            continue
        parts = [_clean_outlook_value(part) for part in re.split(r"\s*----\s*", line)]
        if len(parts) >= 5:
            email, password, client_id, client_secret, refresh_token = parts[:5]
        elif len(parts) >= 4:
            email, client_id, client_secret, refresh_token = parts[:4]
            password = ""
        else:
            continue
        key = email.lower()
        if key in seen or "@" not in email or not client_id or not refresh_token:
            continue
        seen.add(key)
        credentials.append({
            "email": email,
            "password": password,
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        })
    return credentials


def _normalize_outlook_pool(value: Any) -> list[dict[str, str]]:
    """邮箱池既支持纯文本（每行一条），也支持已解析的对象列表。"""
    if isinstance(value, str):
        return parse_outlook_credentials(value)
    if isinstance(value, list):
        items: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, str):
                items.extend(parse_outlook_credentials(item))
            elif isinstance(item, dict):
                email = _clean_outlook_value(item.get("email") or item.get("address") or "")
                client_id = _clean_outlook_value(item.get("client_id") or "")
                refresh_token = _clean_outlook_value(item.get("refresh_token") or "")
                if "@" in email and client_id and refresh_token:
                    items.append({"email": email, "password": _clean_outlook_value(item.get("password") or ""), "client_id": client_id, "refresh_token": refresh_token})
        return items
    return []


def _normalize_gmail_pool(value: Any) -> list[dict[str, str]]:
    if isinstance(value, str):
        return parse_gmail_credentials(value)
    if isinstance(value, list):
        items: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, str):
                items.extend(parse_gmail_credentials(item))
            elif isinstance(item, dict):
                items.extend(parse_gmail_credentials(json.dumps(item, ensure_ascii=False)))
        return items
    return []


class OutlookTokenProvider(BaseMailProvider):
    """使用 refresh_token 读取 Outlook/Hotmail 邮箱验证码。

    邮箱池在应用配置里维护（mailboxes 字段，每行 email----password----client_id----refresh_token），
    create_mailbox() 从池中取下一个未使用的邮箱，wait_for_code() 用 refresh_token 换取 access_token
    后通过 Graph/IMAP 读取最新邮件。
    """

    name = "outlook_token"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.pool = _normalize_outlook_pool(entry.get("mailboxes") or entry.get("pool"))
        self.mode = str(entry.get("mode") or "auto").strip().lower() or "auto"
        if self.mode not in {"graph", "imap", "auto"}:
            self.mode = "auto"
        self.imap_host = str(entry.get("imap_host") or OUTLOOK_DEFAULT_IMAP_HOST).strip() or OUTLOOK_DEFAULT_IMAP_HOST
        self.message_limit = max(1, int(entry.get("message_limit") or 10))
        self.split_aliases = bool(entry.get("split_aliases") or entry.get("enable_split_aliases") or entry.get("alias_split"))
        self.split_count_per_mailbox = max(0, int(entry.get("split_count_per_mailbox") or entry.get("alias_count") or 0))
        self.register_mode = str(conf.get("register_mode") or "").strip().lower()
        self.session = _create_session(conf)

    def close(self) -> None:
        self.session.close()

    def _exchange_refresh_token(self, client_id: str, refresh_token: str, scope: str) -> str:
        data = self._exchange_refresh_token_payload(client_id, refresh_token, scope)
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise OutlookTokenError("OutlookToken 刷新响应缺少 access_token")
        return access_token

    def _exchange_refresh_token_payload(self, client_id: str, refresh_token: str, scope: str) -> dict[str, Any]:
        resp = self.session.post(
            OUTLOOK_TOKEN_URL,
            data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": scope},
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.conf["user_agent"]},
            timeout=self.conf["request_timeout"],
            verify=proxy_settings.verify_tls(),
        )
        try:
            parsed = resp.json()
            data = parsed if isinstance(parsed, dict) else {}
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = data.get("error_description") or data.get("error") or resp.text[:300]
            raise OutlookTokenError(f"OutlookToken 刷新失败: HTTP {resp.status_code}, {detail}")
        return data

    def refresh_credentials(self, mailbox: dict[str, Any], mode: str = "auto") -> dict[str, Any]:
        client_id = str(mailbox.get("client_id") or "").strip()
        refresh_token = str(mailbox.get("refresh_token") or "").strip()
        if not client_id or not refresh_token:
            raise OutlookTokenError("OutlookToken mailbox 缺少 client_id 或 refresh_token")
        selected_mode = str(mode or self.mode or "auto").strip().lower()
        if selected_mode not in {"graph", "imap", "auto"}:
            selected_mode = "auto"
        candidates = [("graph", OUTLOOK_GRAPH_SCOPE), ("imap", OUTLOOK_IMAP_SCOPE)]
        if selected_mode != "auto":
            candidates = [item for item in candidates if item[0] == selected_mode]
        errors: list[str] = []
        for method, scope in candidates:
            try:
                data = self._exchange_refresh_token_payload(client_id, refresh_token, scope)
                access_token = str(data.get("access_token") or "").strip()
                if not access_token:
                    raise OutlookTokenError("OutlookToken 刷新响应缺少 access_token")
                new_refresh_token = str(data.get("refresh_token") or refresh_token).strip()
                return {
                    "ok": True,
                    "method": method,
                    "scope": str(data.get("scope") or scope),
                    "expires_in": int(data.get("expires_in") or 0),
                    "refresh_token": new_refresh_token,
                    "rotated": bool(new_refresh_token and new_refresh_token != refresh_token),
                }
            except Exception as error:
                errors.append(f"{method}: {error}")
        raise OutlookTokenError("; ".join(errors) or "OutlookToken 刷新失败")

    def _access_token(self, mailbox: dict[str, Any], client_id: str, refresh_token: str, scope: str) -> str:
        """缓存 access_token 复用：避免 wait_for_code 轮询时每次都换 token 触发限流。"""
        cache = mailbox.get("_outlook_token_cache")
        if not isinstance(cache, dict):
            cache = {}
            mailbox["_outlook_token_cache"] = cache
        cached = cache.get(scope)
        if isinstance(cached, tuple) and len(cached) == 2 and time.monotonic() < cached[1]:
            return str(cached[0])
        token = self._exchange_refresh_token(client_id, refresh_token, scope)
        cache[scope] = (token, time.monotonic() + 600)
        return token

    def _alias_addresses(self, email: str) -> list[str]:
        address = str(email or "").strip()
        local, sep, domain = address.partition("@")
        if not sep:
            return []
        domain_l = domain.lower()
        aliases = [f"{local}@{domain}"]
        if self.split_aliases and self.split_count_per_mailbox > 0 and domain_l in OUTLOOK_ALIAS_DOMAINS:
            base_local = local.split("+", 1)[0]
            for index in range(1, self.split_count_per_mailbox + 1):
                aliases.append(f"{base_local}+cgpt{index:03d}@{domain}")
        # preserve order while removing duplicates caused by an imported plus-address
        seen: set[str] = set()
        result: list[str] = []
        for item in aliases:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def _base_has_active_alias(self, store: dict[str, dict[str, Any]], aliases: list[str]) -> bool:
        return any(_outlook_entry_in_use(store.get(alias.strip().lower())) for alias in aliases)

    def _free_wu_blocked_bases(self, store: dict[str, dict[str, Any]]) -> set[str]:
        """Skip an Outlook base inbox for Free_Wu after this mode has already
        hit user_already_exists on one of its aliases.
        """
        blocked: set[str] = set()
        for key, entry in store.items():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("state") or "") != "existing_account":
                continue
            reason = str(entry.get("reason") or "").lower()
            if "free_wu" in reason or "free_wu_bad_base" in reason:
                blocked.add(_outlook_base_key(key))
        return blocked

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.pool:
            raise RuntimeError("OutlookToken 邮箱池为空，请在邮箱配置中导入 email----password----client_id----refresh_token")
        with _outlook_token_state_lock:
            store = _load_outlook_token_state()
            free_wu_blocked_bases = self._free_wu_blocked_bases(store) if self.register_mode == "free_wu" else set()
            selected: tuple[dict[str, str], str, list[str]] | None = None
            for credential in self.pool:
                base_address = str(credential.get("email") or "").strip()
                if not base_address:
                    continue
                if self.register_mode == "free_wu" and _outlook_base_key(base_address) in free_wu_blocked_bases:
                    continue
                aliases = self._alias_addresses(base_address)
                base_state = str((store.get(base_address.lower()) or {}).get("state") or "")
                # A broken OAuth token belongs to the real inbox, so all aliases would fail too.
                if base_state == "token_invalid":
                    continue
                # Do not run two aliases from the same inbox concurrently; otherwise the latest OTP
                # in that inbox can be picked for the wrong alias.
                if self._base_has_active_alias(store, aliases):
                    continue
                for address in aliases:
                    if _outlook_entry_available(store.get(address.strip().lower())):
                        selected = (credential, address, aliases)
                        break
                if selected is not None:
                    break
            if selected is None:
                alias_note = f"，已启用分裂别名 x{self.split_count_per_mailbox + 1}" if self.split_aliases and self.split_count_per_mailbox else ""
                raise RuntimeError(f"[{self.label}] OutlookToken 邮箱池暂无可用邮箱（共 {len(self.pool)} 个{alias_note}，已用尽或全部占用/失效），请导入新邮箱或重置池状态")
            credential, address, _aliases = selected
            store[address.strip().lower()] = {"state": "in_use", "reason": "", "updated_at": datetime.now(timezone.utc).isoformat()}
            _save_outlook_token_state(store)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "inbox_address": credential["email"],
            "label": self.label,
            "client_id": credential["client_id"],
            "refresh_token": credential["refresh_token"],
            "password": credential.get("password", ""),
        }

    def _read_graph(self, access_token: str) -> list[dict[str, Any]]:
        resp = self.session.get(
            OUTLOOK_GRAPH_MESSAGES_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": self.conf["user_agent"]},
            params={"$top": self.message_limit, "$orderby": "receivedDateTime desc", "$select": "subject,receivedDateTime,from,toRecipients,body,bodyPreview"},
            timeout=self.conf["request_timeout"],
            verify=proxy_settings.verify_tls(),
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else resp.text[:300]
            raise RuntimeError(f"OutlookToken Graph 失败: HTTP {resp.status_code}, {detail}")
        items = data.get("value") if isinstance(data, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @staticmethod
    def _graph_sender(message: dict[str, Any]) -> str:
        sender = message.get("from") or {}
        if isinstance(sender, dict):
            address = sender.get("emailAddress") or {}
            if isinstance(address, dict):
                return str(address.get("address") or address.get("name") or "")
        return ""

    def _normalize_graph_item(self, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        content_type = str(body.get("contentType") or "").lower()
        content = str(body.get("content") or "")
        text_content = content if content_type != "html" else str(item.get("bodyPreview") or "")
        html_content = content if content_type == "html" else ""
        recipients: list[str] = []
        for recipient in item.get("toRecipients") or []:
            if isinstance(recipient, dict):
                email_address = recipient.get("emailAddress") or {}
                if isinstance(email_address, dict):
                    value = str(email_address.get("address") or "").strip()
                    if value:
                        recipients.append(value)
        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": str(item.get("id") or ""),
            "subject": str(item.get("subject") or ""),
            "sender": self._graph_sender(item),
            "text_content": text_content,
            "html_content": html_content,
            "received_at": _parse_received_at(item.get("receivedDateTime")),
            "to": recipients,
            "raw": item,
        }

    def _graph_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        """返回最近 N 封邮件（Graph 已按 receivedDateTime desc 排序，最新在前）。"""
        return [self._normalize_graph_item(mailbox, item) for item in self._read_graph(access_token)]

    def _imap_messages(self, mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
        """返回最近 N 封邮件，最新在前。"""
        login_address = str(mailbox.get("inbox_address") or mailbox.get("address") or "").strip()
        auth_string = f"user={login_address}\x01auth=Bearer {access_token}\x01\x01"
        imap = imaplib.IMAP4_SSL(self.imap_host)
        try:
            imap.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
            status, _ = imap.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("OutlookToken IMAP select INBOX 失败")
            status, data = imap.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()[-self.message_limit :]
            messages: list[dict[str, Any]] = []
            for uid in reversed(uids):  # 最新在前
                status, fetched = imap.uid("fetch", uid, "(RFC822)")
                if status != "OK":
                    continue
                raw_payload = next((part[1] for part in fetched if isinstance(part, tuple) and isinstance(part[1], bytes)), b"")
                if raw_payload:
                    messages.append(self._parse_imap_message(mailbox, raw_payload))
            return messages
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _parse_imap_message(self, mailbox: dict[str, Any], raw: bytes) -> dict[str, Any]:
        message = message_from_bytes(raw, policy=policy.default)
        try:
            received = _parse_received_at(parsedate_to_datetime(str(message.get("Date") or "")))
        except Exception:
            received = None
        plain: list[str] = []
        html: list[str] = []
        for part in (message.walk() if message.is_multipart() else [message]):
            if part.get_content_maintype() == "multipart":
                continue
            try:
                payload = part.get_content()
            except Exception:
                continue
            if not payload:
                continue
            if part.get_content_type() == "text/html":
                html.append(str(payload))
            else:
                plain.append(str(payload))

        def _decode(value: str | None) -> str:
            if not value:
                return ""
            try:
                return str(make_header(decode_header(value)))
            except Exception:
                return value

        return {
            "provider": self.name,
            "mailbox": mailbox["address"],
            "message_id": _decode(str(message.get("Message-ID") or "")),
            "subject": _decode(str(message.get("Subject") or "")),
            "sender": _decode(str(message.get("From") or "")),
            "text_content": "\n".join(plain).strip(),
            "html_content": "\n".join(html).strip(),
            "received_at": received,
            "to": _decode(str(message.get("To") or "")),
            "raw": None,
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        """拉取最近 N 封邮件（最新在前），供 wait_for_code 逐封扫描验证码。"""
        client_id = str(mailbox.get("client_id") or "").strip()
        refresh_token = str(mailbox.get("refresh_token") or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError("OutlookToken mailbox 缺少 client_id 或 refresh_token")
        errors: list[str] = []
        if self.mode in {"graph", "auto"}:
            try:
                access_token = self._access_token(mailbox, client_id, refresh_token, OUTLOOK_GRAPH_SCOPE)
                return self._graph_messages(mailbox, access_token)
            except Exception as error:
                if self.mode == "graph":
                    raise
                errors.append(f"graph: {error}")
        if self.mode in {"imap", "auto"}:
            try:
                access_token = self._access_token(mailbox, client_id, refresh_token, OUTLOOK_IMAP_SCOPE)
                return self._imap_messages(mailbox, access_token)
            except Exception as error:
                if self.mode == "imap":
                    raise
                errors.append(f"imap: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return []

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        messages = self.fetch_recent_messages(mailbox)
        return messages[0] if messages else None

    def wait_for_code(self, mailbox: dict[str, Any]) -> str | None:
        """轮询时遍历最近 N 封邮件，逐封提取验证码，避免最新一封是广告/安全提醒时错过验证码。"""
        seen_value = mailbox.setdefault("_seen_code_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_code_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}
        seen_codes_value = mailbox.setdefault("_seen_otp_codes", [])
        if not isinstance(seen_codes_value, list):
            seen_codes_value = []
            mailbox["_seen_otp_codes"] = seen_codes_value
        seen_codes = {str(item) for item in seen_codes_value}

        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            for message in self.fetch_recent_messages(mailbox):
                if not _message_matches_email(message, str(mailbox.get("address") or "")):
                    continue
                if _message_before_code_boundary(mailbox, message):
                    continue
                ref = _message_tracking_ref(message)
                if ref in seen_refs:
                    continue
                code = _extract_code(message)
                if code:
                    seen_value.append(ref)
                    seen_refs.add(ref)
                    if code in seen_codes:
                        continue
                    seen_codes_value.append(code)
                    seen_codes.add(code)
                    return code
                seen_refs.add(ref)
            time.sleep(max(0.2, self.conf["wait_interval"]))
        return None

    def wait_for_link(self, mailbox: dict[str, Any], keyword: str = "") -> str | None:
        """轮询最近邮件，提取邀请/验证链接。"""
        seen_value = mailbox.setdefault("_seen_link_message_refs", [])
        if not isinstance(seen_value, list):
            seen_value = []
            mailbox["_seen_link_message_refs"] = seen_value
        seen_refs = {str(item) for item in seen_value}
        deadline = time.monotonic() + self.conf["wait_timeout"]
        while time.monotonic() < deadline:
            for message in self.fetch_recent_messages(mailbox):
                if not _message_matches_email(message, str(mailbox.get("address") or "")):
                    continue
                if _message_before_code_boundary(mailbox, message):
                    continue
                ref = _message_tracking_ref(message)
                if ref in seen_refs:
                    continue
                link = _extract_link(message, keyword)
                seen_value.append(ref)
                seen_refs.add(ref)
                if link:
                    return link
            time.sleep(max(0.2, self.conf["wait_interval"]))
        return None


class GmailOAuthError(RuntimeError):
    """Gmail refresh_token 换取 access_token 失败。"""


class GmailOAuthProvider(OutlookTokenProvider):
    """使用 Gmail OAuth refresh_token 通过 IMAP XOAUTH2 读取验证码。"""

    name = "gmail_oauth"

    def __init__(self, entry: dict, conf: dict):
        BaseMailProvider.__init__(self, conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.pool = _normalize_gmail_pool(entry.get("mailboxes") or entry.get("pool"))
        self.mode = "imap"
        self.imap_host = str(entry.get("imap_host") or GMAIL_DEFAULT_IMAP_HOST).strip() or GMAIL_DEFAULT_IMAP_HOST
        self.message_limit = max(1, int(entry.get("message_limit") or 10))
        self.split_aliases = bool(entry.get("split_aliases") or entry.get("enable_split_aliases") or entry.get("alias_split"))
        self.split_count_per_mailbox = max(0, int(entry.get("split_count_per_mailbox") or entry.get("alias_count") or 0))
        self.session = _create_session(conf)

    def _exchange_refresh_token(self, client_id: str, client_secret: str, refresh_token: str) -> str:
        payload = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token}
        if client_secret:
            payload["client_secret"] = client_secret
        resp = self.session.post(
            GMAIL_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": self.conf["user_agent"]},
            timeout=self.conf["request_timeout"],
            verify=proxy_settings.verify_tls(),
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200:
            detail = data.get("error_description") or data.get("error") or resp.text[:300]
            raise GmailOAuthError(f"GmailOAuth 刷新失败: HTTP {resp.status_code}, {detail}")
        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise GmailOAuthError("GmailOAuth 响应缺少 access_token")
        return access_token

    def _access_token(self, mailbox: dict[str, Any], client_id: str, client_secret: str, refresh_token: str) -> str:
        cache = mailbox.get("_gmail_token_cache")
        if not isinstance(cache, dict):
            cache = {}
            mailbox["_gmail_token_cache"] = cache
        cached = cache.get(GMAIL_IMAP_SCOPE)
        if isinstance(cached, tuple) and len(cached) == 2 and time.monotonic() < cached[1]:
            return str(cached[0])
        token = self._exchange_refresh_token(client_id, client_secret, refresh_token)
        cache[GMAIL_IMAP_SCOPE] = (token, time.monotonic() + 600)
        return token

    def _alias_addresses(self, email: str) -> list[str]:
        address = str(email or "").strip()
        local, sep, domain = address.partition("@")
        if not sep:
            return []
        domain_l = domain.lower()
        aliases = [f"{local}@{domain}"]
        if self.split_aliases and self.split_count_per_mailbox > 0 and domain_l in GMAIL_ALIAS_DOMAINS:
            base_local = local.split("+", 1)[0]
            for index in range(1, self.split_count_per_mailbox + 1):
                aliases.append(f"{base_local}+cgpt{index:03d}@{domain}")
        seen: set[str] = set()
        result: list[str] = []
        for item in aliases:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.pool:
            raise RuntimeError("GmailOAuth 邮箱池为空，请先在邮箱配置中导入 Gmail OAuth 邮箱")
        with _outlook_token_state_lock:
            store = _load_outlook_token_state()
            selected: tuple[dict[str, str], str, list[str]] | None = None
            for credential in self.pool:
                base_address = str(credential.get("email") or "").strip()
                if not base_address:
                    continue
                aliases = self._alias_addresses(base_address)
                base_state = str((store.get(base_address.lower()) or {}).get("state") or "")
                if base_state == "token_invalid":
                    continue
                if self._base_has_active_alias(store, aliases):
                    continue
                for address in aliases:
                    if _outlook_entry_available(store.get(address.strip().lower())):
                        selected = (credential, address, aliases)
                        break
                if selected is not None:
                    break
            if selected is None:
                alias_note = f"，Plus Alias x{self.split_count_per_mailbox + 1}" if self.split_aliases and self.split_count_per_mailbox else ""
                raise RuntimeError(f"[{self.label}] GmailOAuth 没有可用邮箱：共 {len(self.pool)} 个{alias_note}，请释放失败/占用邮箱或继续导入")
            credential, address, _aliases = selected
            store[address.strip().lower()] = {"state": "in_use", "reason": "", "updated_at": datetime.now(timezone.utc).isoformat()}
            _save_outlook_token_state(store)
        return {
            "provider": self.name,
            "provider_ref": self.provider_ref,
            "address": address,
            "inbox_address": credential["email"],
            "label": self.label,
            "client_id": credential["client_id"],
            "client_secret": credential.get("client_secret", ""),
            "refresh_token": credential["refresh_token"],
            "password": credential.get("password", ""),
        }

    def fetch_recent_messages(self, mailbox: dict[str, Any]) -> list[dict[str, Any]]:
        client_id = str(mailbox.get("client_id") or "").strip()
        client_secret = str(mailbox.get("client_secret") or "").strip()
        refresh_token = str(mailbox.get("refresh_token") or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError("GmailOAuth mailbox 缺少 client_id 或 refresh_token")
        access_token = self._access_token(mailbox, client_id, client_secret, refresh_token)
        return self._imap_messages(mailbox, access_token)


class OpenAICPAMemoryProvider(BaseMailProvider):
    """OpenAI-CPA 专属内存池邮箱：Worker/Webhook 把邮件推到本进程内存池。"""

    name = "openai_cpa"

    def __init__(self, entry: dict, conf: dict):
        super().__init__(conf, str(entry.get("provider_ref") or ""))
        self.label = str(entry.get("label") or self.provider_ref)
        self.domains = _normalize_string_list(entry.get("domains") or entry.get("domain"))
        self.webhook_secret = str(entry.get("webhook_secret") or "").strip()
        self.suffix_mode = str(entry.get("suffix_mode") or "fixed").strip() or "fixed"
        self.suffix_len_min = max(1, int(entry.get("suffix_len_min") or 8))
        self.suffix_len_max = max(self.suffix_len_min, int(entry.get("suffix_len_max") or self.suffix_len_min))

    def _prefix(self, username: str | None = None) -> str:
        if username:
            return re.sub(r"[^a-zA-Z0-9._+-]", "", username.split("@", 1)[0]) or _random_mailbox_name()
        length = self.suffix_len_min if self.suffix_mode == "fixed" else random.randint(self.suffix_len_min, self.suffix_len_max)
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

    def create_mailbox(self, username: str | None = None) -> dict[str, Any]:
        if not self.webhook_secret:
            raise RuntimeError("OpenAI-CPA 内存池缺少 webhook_secret")
        domain = _next_domain(self.domains)
        address = f"{self._prefix(username)}@{domain}"
        return {"provider": self.name, "provider_ref": self.provider_ref, "address": address, "label": self.label}

    def fetch_latest_message(self, mailbox: dict[str, Any]) -> dict[str, Any] | None:
        target = str(mailbox.get("address") or "").strip().lower()
        if not target:
            return None
        with openai_cpa_pool_lock:
            bucket = list(openai_cpa_code_pool.get(target) or [])
        return bucket[-1] if bucket else None


def _entries(mail_config: dict) -> list[dict]:
    result: list[dict] = []
    counters: dict[str, int] = {}
    for item in mail_config["providers"]:
        idx = len(result) + 1
        t = item.get("type", "")
        cnt = counters.get(t, 0) + 1
        counters[t] = cnt
        label = f"DDG-{cnt}" if t == "ddg_mail" else f"{t}#{idx}"
        result.append({**item, "provider_ref": f"{item['type']}#{idx}", "label": label})
    return result


def _enabled_entries(mail_config: dict) -> list[dict]:
    items = [item for item in _entries(mail_config) if item.get("enable")]
    if not items:
        raise RuntimeError("mail.providers 没有启用的 provider")
    return items


def _next_entry(mail_config: dict) -> dict:
    global provider_index
    items = _enabled_entries(mail_config)
    if len(items) == 1:
        return dict(items[0])
    with provider_lock:
        value = dict(items[provider_index % len(items)])
        provider_index = (provider_index + 1) % len(items)
        return value


def _create_provider(mail_config: dict, provider: str = "", provider_ref: str = "") -> BaseMailProvider:
    entry = next((dict(item) for item in _entries(mail_config) if provider_ref and item["provider_ref"] == provider_ref), None)
    entry = entry or next((dict(item) for item in _enabled_entries(mail_config) if provider and item["type"] == provider), None) or _next_entry(mail_config)
    conf = _config(mail_config)
    if entry["type"] == "cloudmail_gen":
        return CloudMailGenProvider(entry, conf)
    if entry["type"] == "cloudflare_temp_email":
        return CloudflareTempMailProvider(entry, conf)
    if entry["type"] == "ddg_mail":
        return DDGMailProvider(entry, conf)
    if entry["type"] == "tempmail_lol":
        return TempMailLolProvider(entry, conf)
    if entry["type"] == "duckmail":
        return DuckMailProvider(entry, conf)
    if entry["type"] == "gptmail":
        return GptMailProvider(entry, conf)
    if entry["type"] == "moemail":
        return MoEmailProvider(entry, conf)
    if entry["type"] == "inbucket":
        return InbucketMailProvider(entry, conf)
    if entry["type"] == "yyds_mail":
        return YydsMailProvider(entry, conf)
    if entry["type"] == "gmail_oauth":
        return GmailOAuthProvider(entry, conf)
    if entry["type"] == "openai_cpa":
        return OpenAICPAMemoryProvider(entry, conf)
    if entry["type"] == "outlook_token":
        return OutlookTokenProvider(entry, conf)
    raise RuntimeError(f"不支持的 mail.provider: {entry['type']}")


def create_mailbox(mail_config: dict, username: str | None = None) -> dict:
    enabled = _enabled_entries(mail_config)
    tried: set[str] = set()
    last_error = ""
    for _ in range(len(enabled)):
        provider = _create_provider(mail_config)
        provider_key = f"{provider.name}#{provider.provider_ref}"
        try:
            if provider_key in tried:
                continue
            tried.add(provider_key)
            mailbox = provider.create_mailbox(username)
            mailbox["_code_not_before"] = datetime.now(timezone.utc)
            mailbox.setdefault("mailbox_group", str(getattr(provider, "mailbox_group", "") or ""))
            return mailbox
        except RuntimeError as error:
            last_error = str(error)
            if "DDG日上限已达" not in last_error:
                raise
        finally:
            provider.close()
    raise RuntimeError(last_error or "所有启用的邮箱提供商均无法创建邮箱")


def wait_for_code(mail_config: dict, mailbox: dict) -> str | None:
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    try:
        return provider.wait_for_code(mailbox)
    finally:
        provider.close()


def _message_public_summary(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    received_at = message.get("received_at")
    if isinstance(received_at, datetime):
        received_value = received_at.isoformat()
    else:
        received_value = str(received_at or "")
    body = f"{message.get('text_content', '')}\n{message.get('html_content', '')}"
    body = html.unescape(re.sub(r"<[^>]+>", " ", str(body or "")))
    body_preview = " ".join(body.replace("\r", " ").replace("\n", " ").split())[:500]
    return {
        "provider": str(message.get("provider") or ""),
        "mailbox": str(message.get("mailbox") or ""),
        "message_id": str(message.get("message_id") or ""),
        "subject": str(message.get("subject") or ""),
        "sender": str(message.get("sender") or ""),
        "received_at": received_value,
        "body_preview": body_preview,
        "code": _extract_code(message) or "",
    }


def latest_verification_code(mail_config: dict, mailbox: dict, *, scan_recent: bool = True) -> dict[str, Any]:
    """读取指定邮箱最近邮件并提取验证码，不修改 seen 状态、不等待新邮件。"""
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    try:
        messages: list[dict[str, Any]] = []
        if scan_recent and hasattr(provider, "fetch_recent_messages"):
            raw_messages = provider.fetch_recent_messages(mailbox)  # type: ignore[attr-defined]
            messages = [item for item in raw_messages if isinstance(item, dict)]
        else:
            latest = provider.fetch_latest_message(mailbox)
            messages = [latest] if isinstance(latest, dict) else []
        target = str(mailbox.get("address") or "").strip()
        if target:
            messages = [item for item in messages if _message_matches_email(item, target)]
        latest_message = messages[0] if messages else None
        for index, message in enumerate(messages):
            code = _extract_code(message)
            if code:
                return {
                    "ok": True,
                    "found": True,
                    "code": code,
                    "email": target,
                    "provider": provider.name,
                    "provider_ref": provider.provider_ref,
                    "scanned": len(messages),
                    "message_index": index,
                    "message": _message_public_summary(message),
                    "latest_message": _message_public_summary(latest_message),
                }
        return {
            "ok": True,
            "found": False,
            "code": "",
            "email": target,
            "provider": provider.name,
            "provider_ref": provider.provider_ref,
            "scanned": len(messages),
            "message_index": -1,
            "message": None,
            "latest_message": _message_public_summary(latest_message),
        }
    finally:
        provider.close()


def recent_messages(mail_config: dict, mailbox: dict, *, limit: int = 20, mode: str = "auto") -> dict[str, Any]:
    """读取指定 OAuth 邮箱的最近邮件，供库存页面手动查看。"""
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    try:
        if hasattr(provider, "message_limit"):
            provider.message_limit = max(1, min(50, int(limit or 20)))  # type: ignore[attr-defined]
        if isinstance(provider, OutlookTokenProvider):
            selected_mode = str(mode or "auto").strip().lower()
            provider.mode = selected_mode if selected_mode in {"graph", "imap", "auto"} else "auto"
        if not hasattr(provider, "fetch_recent_messages"):
            raise RuntimeError(f"{provider.name} 不支持读取最近邮件")
        raw_messages = provider.fetch_recent_messages(mailbox)  # type: ignore[attr-defined]
        messages = [summary for summary in (_message_public_summary(item) for item in raw_messages) if summary]
        return {
            "ok": True,
            "email": str(mailbox.get("address") or ""),
            "inbox_address": str(mailbox.get("inbox_address") or mailbox.get("address") or ""),
            "provider": provider.name,
            "provider_ref": provider.provider_ref,
            "count": len(messages),
            "messages": messages,
        }
    finally:
        provider.close()


def refresh_outlook_token(mail_config: dict, mailbox: dict, *, mode: str = "auto") -> dict[str, Any]:
    """验证 Outlook refresh_token，并返回微软可能轮换的新 refresh_token。"""
    provider = _create_provider(mail_config, "outlook_token", str(mailbox.get("provider_ref") or ""))
    try:
        if not isinstance(provider, OutlookTokenProvider) or isinstance(provider, GmailOAuthProvider):
            raise RuntimeError("该邮箱不是 Outlook OAuth 邮箱")
        result = provider.refresh_credentials(mailbox, mode=mode)
        result.update({
            "email": str(mailbox.get("address") or ""),
            "provider": provider.name,
            "provider_ref": provider.provider_ref,
        })
        return result
    finally:
        provider.close()


def wait_for_link(mail_config: dict, mailbox: dict, keyword: str = "", timeout: int | float | None = None) -> str | None:
    provider = _create_provider(mail_config, str(mailbox.get("provider") or ""), str(mailbox.get("provider_ref") or ""))
    try:
        if timeout is not None:
            provider.conf["wait_timeout"] = float(timeout)
        if hasattr(provider, "wait_for_link"):
            return provider.wait_for_link(mailbox, keyword)  # type: ignore[attr-defined]
        return None
    finally:
        provider.close()


def mark_mailbox_result(mailbox: dict, *, success: bool, error: Exception | str | None = None) -> None:
    """注册流程结束后更新邮箱池状态。

    对本地 OAuth 邮箱池生效：成功标记 used；失败时若是 token 失效标记
    token_invalid，其余失败标记 failed（保留邮箱占用以便排查，可通过重置释放）。
    """
    provider_name = str(mailbox.get("provider") or "")
    if provider_name not in {OutlookTokenProvider.name, GmailOAuthProvider.name}:
        return
    address = str(mailbox.get("address") or "").strip()
    if not address:
        return
    if success:
        _set_outlook_token_state(address, "used")
        return
    reason = str(error or "").strip()
    if (
        "auth_flow_login_existing_account" in reason
        or "/log-in/password" in reason
        or "log-in/password" in reason
        or "account_deactivated" in reason
        or "deleted or deactivated" in reason
        or "user_already_exists" in reason
        or "An account already exists for this email address" in reason
    ):
        _set_outlook_token_state(address, "existing_account", reason[:300])
        if "free_wu" in reason.lower():
            inbox_address = str(mailbox.get("inbox_address") or "").strip()
            if inbox_address and inbox_address.lower() != address.lower():
                _set_outlook_token_state(inbox_address, "existing_account", ("free_wu_bad_base: " + reason)[:300])
        return
    if (
        "invalid_auth_step" in reason
        or "Invalid authorization step" in reason
        or "auth_flow_invalid_step" in reason
        or "phone_required" in reason
        or "add-phone" in reason
    ):
        _set_outlook_token_state(address, "flow_blocked", reason[:300])
        return
    if (
        "Cloudflare" in reason
        or "Just a moment" in reason
        or "cf-ray" in reason
        or "cf-chl" in reason
        or "被 Cloudflare" in reason
    ):
        _set_outlook_token_state(address, "cloudflare_blocked", reason[:300])
        return
    if (
        "unsupported_country_region_territory" in reason
        or "Country, region, or territory not supported" in reason
        or "platform_authorize_http_403" in reason
    ):
        # 代理 IP / 地区被 OpenAI 拒绝时，不标记 token 失效，便于释放后重试。
        _set_outlook_token_state(address, "region_blocked", reason[:300])
        return
    if (
        isinstance(error, (OutlookTokenError, GmailOAuthError))
        or "OutlookToken 刷新失败" in reason
        or "GmailOAuth 刷新失败" in reason
        or "access_token" in reason
    ):
        _set_outlook_token_state(address, "token_invalid", reason[:300])
        inbox_address = str(mailbox.get("inbox_address") or "").strip()
        if inbox_address and inbox_address.lower() != address.lower():
            _set_outlook_token_state(inbox_address, "token_invalid", reason[:300])
    else:
        _set_outlook_token_state(address, "failed", reason[:300])


def release_mailbox(mailbox: dict) -> None:
    """把本地 OAuth 邮箱从 in_use 释放回未使用。"""
    if str(mailbox.get("provider") or "") not in {OutlookTokenProvider.name, GmailOAuthProvider.name}:
        return
    _release_outlook_token_state(str(mailbox.get("address") or ""))


def get_existing_mailbox(mail_config: dict, email: str, provider: str = "") -> dict:
    """通过管理员密码获取已有邮箱地址的 JWT，用于查询邮件。"""
    enabled = [item for item in _enabled_entries(mail_config) if not provider or str(item.get("type") or "") == provider]
    tried: set[str] = set()
    last_error = ""
    for entry in enabled:
        provider_obj = _create_provider(mail_config, provider_ref=str(entry.get("provider_ref") or ""))
        provider_key = f"{provider_obj.name}#{provider_obj.provider_ref}"
        try:
            if provider_key in tried:
                continue
            tried.add(provider_key)
            if hasattr(provider_obj, "get_existing_mailbox"):
                mailbox = provider_obj.get_existing_mailbox(email)
                return mailbox
            last_error = f"邮箱提供商 {provider_obj.name} 不支持查询已有邮箱"
        except RuntimeError as error:
            last_error = str(error)
            if "DDG日上限已达" not in last_error:
                continue
        finally:
            provider_obj.close()
    raise RuntimeError(last_error or "所有启用的邮箱提供商均无法查询已有邮箱")
