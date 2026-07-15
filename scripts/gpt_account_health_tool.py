from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    from curl_cffi import requests as http_requests  # type: ignore
    import requests as std_requests  # type: ignore

    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover - fallback
    import requests as http_requests  # type: ignore
    std_requests = http_requests  # type: ignore

    HAS_CURL_CFFI = False

try:
    import urllib3  # type: ignore

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")


CHATGPT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"
PLATFORM_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_OAUTH_REDIRECT_URI = "https://platform.openai.com/auth/callback"
PLATFORM_AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

SECRET_KEYS = {"access_token", "refresh_token", "id_token", "session_token", "password"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact(value: Any, limit: int = 800) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value or "")
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return text[:limit]


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def jwt_payload(token: object) -> dict[str, Any]:
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


def timestamp_from_unix(value: Any) -> str:
    try:
        number = int(float(str(value)))
    except Exception:
        return ""
    if number <= 0:
        return ""
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def token_context(access_token: object = "", id_token: object = "") -> dict[str, Any]:
    access_claims = jwt_payload(access_token)
    id_claims = jwt_payload(id_token)
    auth = access_claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    profile = access_claims.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    id_auth = id_claims.get("https://api.openai.com/auth")
    id_auth = id_auth if isinstance(id_auth, dict) else {}
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
        "email": first_non_empty(profile.get("email"), access_claims.get("email"), id_claims.get("email")),
        "chatgpt_account_id": first_non_empty(auth.get("chatgpt_account_id"), auth.get("account_id"), id_auth.get("chatgpt_account_id"), id_auth.get("account_id")),
        "chatgpt_user_id": first_non_empty(auth.get("chatgpt_user_id"), auth.get("user_id"), id_auth.get("chatgpt_user_id"), id_auth.get("user_id"), access_claims.get("sub"), id_claims.get("sub")),
        "plan_type": first_non_empty(auth.get("chatgpt_plan_type"), profile.get("plan_type"), id_auth.get("chatgpt_plan_type")),
        "client_id": first_non_empty(access_claims.get("client_id"), id_claims.get("client_id")),
        "expires_at": timestamp_from_unix(exp) if exp else "",
        "expires_in": max(0, exp - int(time.time())) if exp else None,
    }


def token_hash(token: object, length: int = 16) -> str:
    text = str(token or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:length]


def redact_account(account: dict[str, Any]) -> dict[str, Any]:
    out = dict(account)
    for key in SECRET_KEYS:
        value = str(out.get(key) or "")
        if value:
            out[f"{key}_sha"] = token_hash(value)
            out[f"{key}_preview"] = f"{value[:10]}...{value[-6:]}" if len(value) > 22 else "***"
        out.pop(key, None)
    return out


def normalize_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    if "@" in proxy:
        return f"http://{proxy}"
    parts = proxy.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        host, port = parts[0], parts[1]
        username = parts[2]
        password = ":".join(parts[3:])
        return f"http://{quote(username, safe='-._~')}:{quote(password, safe='-._~')}@{host}:{port}"
    if len(parts) >= 4 and parts[-1].isdigit():
        username, password = parts[0], parts[1]
        host = ":".join(parts[2:-1])
        port = parts[-1]
        return f"http://{quote(username, safe='-._~')}:{quote(password, safe='-._~')}@{host}:{port}"
    if re.match(r"^[^:/\s]+:\d+$", proxy):
        return f"http://{proxy}"
    return proxy


def new_session(proxy: str = "", *, backend: str = "auto") -> Any:
    backend = str(backend or "auto").lower()
    use_curl = HAS_CURL_CFFI and backend in {"auto", "curl", "curl_cffi"}
    if use_curl:
        session = http_requests.Session(impersonate="chrome136")
    else:
        session = std_requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    proxy = normalize_proxy_url(proxy)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def response_json(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"body": str(getattr(resp, "text", "") or "")[:1200]}


def auth_headers(access_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Origin": CHATGPT_BASE,
        "Referer": f"{CHATGPT_BASE}/",
        "User-Agent": USER_AGENT,
    }


def looks_banned(status_code: int, body: str) -> bool:
    lowered = str(body or "").lower()
    if any(marker in lowered for marker in ["deactivated", "suspended", "disabled", "banned", "account_closed", "account deactivated", "封禁"]):
        return True
    return status_code == 401 and any(marker in lowered for marker in ["invalid", "unauthorized", "expired"])


def classify_failure(status_code: int = 0, body: Any = "", *, default: str = "failed") -> str:
    lowered = compact(body, 2000).lower()
    if any(marker in lowered for marker in ["missing_token", "missing access_token", "missing access token", "缺少 access_token", "缺少 token", "no access_token"]):
        return "missing_token"
    if looks_banned(status_code, lowered):
        return "banned"
    if any(marker in lowered for marker in ["just a moment", "cf-ray", "cloudflare", "turnstile", "challenge-platform"]):
        return "cloudflare_blocked"
    if status_code == 429 or any(marker in lowered for marker in ["rate_limit", "rate limit", "too many requests", "限流", "频率"]):
        return "rate_limited"
    if any(marker in lowered for marker in ["unsupported_country", "not available in your country", "country not supported", "地区", "国家"]):
        return "region_blocked"
    if any(marker in lowered for marker in ["expired", "token has expired", "jwt expired", "过期"]):
        return "token_expired"
    if status_code == 401 or any(marker in lowered for marker in ["invalid token", "invalid access token", "unauthorized", "invalid_grant", "invalid_request", "鉴权"]):
        return "token_invalid" if "token" in lowered or status_code == 401 else "auth_invalid"
    if any(marker in lowered for marker in ["proxy", "socks", "tunnel", "connect proxy", "proxy connect"]):
        return "proxy_error"
    if any(marker in lowered for marker in ["curl: (28)", "timed out", "timeout", "read timed out", "请求超时"]):
        return "timeout"
    if any(marker in lowered for marker in ["curl:", "failed to connect", "could not connect", "connection reset", "connection aborted", "远程主机强迫关闭", "tls", "ssl", "dns", "name resolution", "network"]):
        return "network_error"
    if status_code == 403:
        return "blocked"
    if status_code >= 500:
        return "upstream_error"
    return default or "failed"


def refresh_platform_tokens(refresh_token: str, *, proxy: str = "", timeout: int = 60, backend: str = "auto") -> dict[str, Any]:
    if not str(refresh_token or "").strip():
        raise RuntimeError("缺少 refresh_token")
    session = new_session(proxy, backend=backend)
    resp = session.post(
        f"{AUTH_BASE}/api/accounts/oauth/token",
        headers={
            "accept": "*/*",
            "auth0-client": PLATFORM_AUTH0_CLIENT,
            "content-type": "application/json",
            "origin": "https://platform.openai.com",
            "referer": "https://platform.openai.com/",
            "user-agent": USER_AGENT,
        },
        json={
            "client_id": PLATFORM_OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
        },
        timeout=timeout,
        verify=True,
    )
    data = response_json(resp)
    status_code = int(getattr(resp, "status_code", 0) or 0)
    if status_code != 200:
        raise RuntimeError(f"刷新 OAuth token 失败: HTTP {status_code}, {compact(data, 500)}")
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("刷新响应缺少 access_token")
    return {
        "access_token": access_token,
        "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
        "id_token": str(data.get("id_token") or "").strip(),
        "refreshed_at": now_iso(),
    }


def normalize_account(raw: dict[str, Any], *, source: str, source_path: str = "") -> dict[str, Any] | None:
    credentials = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else {}
    token = raw.get("token") if isinstance(raw.get("token"), dict) else {}
    extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
    provider_data = raw.get("providerSpecificData") if isinstance(raw.get("providerSpecificData"), dict) else {}
    account_obj = raw.get("account") if isinstance(raw.get("account"), dict) else {}
    user_obj = raw.get("user") if isinstance(raw.get("user"), dict) else {}

    access_token = first_non_empty(raw.get("accessToken"), raw.get("access_token"), token.get("accessToken"), token.get("access_token"), credentials.get("accessToken"), credentials.get("access_token"))
    refresh_token = first_non_empty(raw.get("refreshToken"), raw.get("refresh_token"), token.get("refreshToken"), token.get("refresh_token"), credentials.get("refreshToken"), credentials.get("refresh_token"))
    id_token = first_non_empty(raw.get("idToken"), raw.get("id_token"), token.get("idToken"), token.get("id_token"), credentials.get("idToken"), credentials.get("id_token"))
    session_token = first_non_empty(raw.get("sessionToken"), raw.get("session_token"), token.get("sessionToken"), token.get("session_token"), credentials.get("sessionToken"), credentials.get("session_token"))

    ctx = token_context(access_token, id_token)
    email = first_non_empty(
        user_obj.get("email"),
        raw.get("email"),
        raw.get("username"),
        credentials.get("email"),
        extra.get("email"),
        provider_data.get("email"),
        raw.get("name") if "@" in str(raw.get("name") or "") else "",
        ctx.get("email"),
    )
    if "@" not in email:
        return None
    if not access_token and not refresh_token:
        # 没有可用于测活或刷新的 token，就不当作可测 GPT 账号。
        return None

    account_id = first_non_empty(
        account_obj.get("id"),
        raw.get("account_id"),
        raw.get("chatgpt_account_id"),
        raw.get("chatgptAccountId"),
        credentials.get("chatgpt_account_id"),
        credentials.get("account_id"),
        extra.get("chatgpt_account_id"),
        provider_data.get("chatgptAccountId"),
        provider_data.get("chatgpt_account_id"),
        ctx.get("chatgpt_account_id"),
        raw.get("id") if raw.get("provider") == "codex" else "",
    )
    user_id = first_non_empty(
        user_obj.get("id"),
        raw.get("user_id"),
        raw.get("chatgpt_user_id"),
        raw.get("chatgptUserId"),
        credentials.get("chatgpt_user_id"),
        credentials.get("user_id"),
        extra.get("chatgpt_user_id"),
        provider_data.get("chatgptUserId"),
        provider_data.get("chatgpt_user_id"),
        ctx.get("chatgpt_user_id"),
    )
    plan_type = first_non_empty(
        account_obj.get("planType"),
        account_obj.get("plan_type"),
        raw.get("planType"),
        raw.get("plan_type"),
        raw.get("chatgpt_plan_type"),
        raw.get("chatgptPlanType"),
        credentials.get("plan_type"),
        extra.get("plan_type"),
        provider_data.get("chatgptPlanType"),
        provider_data.get("chatgpt_plan_type"),
        ctx.get("plan_type"),
        "unknown",
    )
    expires_at = first_non_empty(raw.get("expired"), raw.get("expires"), raw.get("expiresAt"), raw.get("expires_at"), credentials.get("expires_at"), ctx.get("expires_at"))
    source_type = detect_format(raw)
    client_id = first_non_empty(raw.get("client_id"), credentials.get("client_id"), ctx.get("client_id"))
    token_source = first_non_empty(raw.get("token_source"), extra.get("token_source"))
    oauth_provenance = first_non_empty(raw.get("oauth_provenance"), extra.get("oauth_provenance"))
    if source_type == "sub2api" and refresh_token and str(client_id) == "app_EMoamEEZ73f0CkXaXp7hrann":
        token_source = token_source or "sub2api_openai_oauth"
        oauth_provenance = oauth_provenance or "sub2api_garbage_source"
    return {
        "email": email,
        "name": first_non_empty(raw.get("name"), email),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "session_token": session_token,
        "chatgpt_account_id": account_id,
        "chatgpt_user_id": user_id,
        "plan_type": plan_type,
        "token_expires_at": expires_at,
        "source_type": source_type,
        "source": source,
        "source_path": source_path,
        "disabled": bool(raw.get("disabled")) if "disabled" in raw else False,
        "sub2api_account_id": raw.get("id") or raw.get("account_id") if raw.get("platform") or raw.get("credentials") else "",
        "client_id": client_id,
        "token_source": token_source,
        "oauth_provenance": oauth_provenance,
        "token_sha": token_hash(access_token or refresh_token, 16),
    }


def detect_format(item: dict[str, Any]) -> str:
    if item.get("type") == "codex" and (item.get("access_token") or item.get("refresh_token")):
        return "cpa"
    if item.get("platform") or item.get("credentials") is not None:
        return "sub2api"
    if item.get("provider") == "codex":
        return "9router"
    if item.get("accessToken") or item.get("sessionToken"):
        return "chatgpt_session"
    return "json"


def is_plain_object(value: Any) -> bool:
    return isinstance(value, dict)


def object_has_token(value: dict[str, Any]) -> bool:
    credentials = value.get("credentials") if isinstance(value.get("credentials"), dict) else {}
    token = value.get("token") if isinstance(value.get("token"), dict) else {}
    return bool(
        first_non_empty(
            value.get("accessToken"),
            value.get("access_token"),
            value.get("refreshToken"),
            value.get("refresh_token"),
            token.get("accessToken"),
            token.get("access_token"),
            token.get("refreshToken"),
            token.get("refresh_token"),
            credentials.get("accessToken"),
            credentials.get("access_token"),
            credentials.get("refreshToken"),
            credentials.get("refresh_token"),
        )
    )


def object_has_identity(value: dict[str, Any]) -> bool:
    credentials = value.get("credentials") if isinstance(value.get("credentials"), dict) else {}
    provider_data = value.get("providerSpecificData") if isinstance(value.get("providerSpecificData"), dict) else {}
    return bool(
        isinstance(value.get("user"), dict)
        or first_non_empty(
            value.get("email"),
            value.get("username"),
            value.get("name") if "@" in str(value.get("name") or "") else "",
            value.get("account_id"),
            value.get("chatgpt_account_id"),
            value.get("chatgptAccountId"),
            credentials.get("email"),
            credentials.get("chatgpt_account_id"),
            provider_data.get("chatgptAccountId"),
            provider_data.get("chatgpt_account_id"),
        )
    )


def collect_account_like_objects(value: Any, *, source_name: str, path: str = "$") -> list[tuple[dict[str, Any], str]]:
    found: list[tuple[dict[str, Any], str]] = []
    seen: set[int] = set()

    def visit(item: Any, item_path: str) -> None:
        if isinstance(item, dict):
            ident = id(item)
            if ident in seen:
                return
            seen.add(ident)
            if object_has_token(item) and object_has_identity(item):
                found.append((item, item_path))
                return
            for key, child in item.items():
                if key in {"accessToken", "access_token", "refreshToken", "refresh_token", "sessionToken", "session_token", "idToken", "id_token"}:
                    continue
                visit(child, f"{item_path}.{key}")
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{item_path}[{index}]")

    visit(value, path)
    return found


@dataclass
class ParsedDocument:
    source: str
    source_path: str
    data: Any


@dataclass
class ParseStats:
    json_files: int = 0
    zip_files: int = 0
    sevenz_files: int = 0
    json_documents: int = 0
    skipped_files: list[dict[str, Any]] = field(default_factory=list)


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except Exception:
            pass
    return data.decode("utf-8", errors="replace")


def load_json_bytes(data: bytes) -> Any:
    return json.loads(decode_bytes(data))


def iter_documents(root: Path, *, include_archives: bool = True, stats: ParseStats | None = None) -> Iterable[ParsedDocument]:
    stats = stats or ParseStats()
    root = root.resolve()
    for path in sorted(root.rglob("*"), key=lambda p: str(p).lower()):
        if not path.is_file():
            continue
        rel_parts = [part.lower() for part in path.relative_to(root).parts]
        if any(part in {"health_reports", ".git", "__macosx"} for part in rel_parts):
            continue
        suffix = path.suffix.lower()
        rel = str(path.relative_to(root))
        if suffix == ".json":
            stats.json_files += 1
            try:
                data = load_json_bytes(path.read_bytes())
                stats.json_documents += 1
                yield ParsedDocument(source=rel, source_path="$", data=data)
            except Exception as exc:
                stats.skipped_files.append({"source": rel, "reason": f"json_parse_error: {type(exc).__name__}: {exc}"})
        elif include_archives and suffix == ".zip":
            stats.zip_files += 1
            try:
                with zipfile.ZipFile(path) as zf:
                    for name in zf.namelist():
                        if not name.lower().endswith(".json"):
                            continue
                        name_parts = [part for part in re.split(r"[\\/]+", name) if part]
                        if "__MACOSX" in name_parts or any(part.startswith("._") for part in name_parts):
                            continue
                        source = f"{rel}!{name}"
                        try:
                            data = load_json_bytes(zf.read(name))
                            stats.json_documents += 1
                            yield ParsedDocument(source=source, source_path="$", data=data)
                        except Exception as exc:
                            stats.skipped_files.append({"source": source, "reason": f"zip_json_parse_error: {type(exc).__name__}: {exc}"})
            except Exception as exc:
                stats.skipped_files.append({"source": rel, "reason": f"zip_error: {type(exc).__name__}: {exc}"})
        elif include_archives and suffix == ".7z":
            stats.sevenz_files += 1
            yield from iter_7z_documents(path, rel, stats)


def iter_7z_documents(path: Path, rel: str, stats: ParseStats) -> Iterable[ParsedDocument]:
    tar_bin = shutil.which("tar")
    if not tar_bin:
        stats.skipped_files.append({"source": rel, "reason": "7z_skipped: tar not found"})
        return
    try:
        listed = subprocess.run([tar_bin, "-tf", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except Exception as exc:
        stats.skipped_files.append({"source": rel, "reason": f"7z_list_error: {type(exc).__name__}: {exc}"})
        return
    if listed.returncode != 0:
        stats.skipped_files.append({"source": rel, "reason": f"7z_list_error: {decode_bytes(listed.stderr)[:300]}"})
        return
    names = [line.strip() for line in decode_bytes(listed.stdout).splitlines() if line.strip().lower().endswith(".json")]
    for name in names:
        name_parts = [part for part in re.split(r"[\\/]+", name) if part]
        if "__MACOSX" in name_parts or any(part.startswith("._") for part in name_parts):
            continue
        source = f"{rel}!{name}"
        try:
            extracted = subprocess.run([tar_bin, "-xOf", str(path), name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if extracted.returncode != 0:
                raise RuntimeError(decode_bytes(extracted.stderr)[:300])
            data = load_json_bytes(extracted.stdout)
            stats.json_documents += 1
            yield ParsedDocument(source=source, source_path="$", data=data)
        except Exception as exc:
            stats.skipped_files.append({"source": source, "reason": f"7z_json_parse_error: {type(exc).__name__}: {exc}"})


def account_identity(account: dict[str, Any]) -> str:
    email = str(account.get("email") or "").strip().lower()
    scope = first_non_empty(account.get("chatgpt_account_id"), account.get("sub2api_account_id"))
    if email and scope:
        return f"{email}::{scope}"
    if email and account.get("token_sha"):
        return f"{email}::{account.get('token_sha')}"
    if email:
        return email
    return str(account.get("token_sha") or "")


def collect_accounts(root: Path, *, include_archives: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    parse_stats = ParseStats()
    by_identity: dict[str, dict[str, Any]] = {}
    skipped_objects: list[dict[str, Any]] = []
    raw_objects = 0
    for doc in iter_documents(root, include_archives=include_archives, stats=parse_stats):
        objects = collect_account_like_objects(doc.data, source_name=doc.source, path=doc.source_path)
        if not objects:
            skipped_objects.append({"source": doc.source, "reason": "no_account_like_object"})
            continue
        for raw, path in objects:
            raw_objects += 1
            account = normalize_account(raw, source=doc.source, source_path=path)
            if not account:
                skipped_objects.append({"source": doc.source, "path": path, "reason": "normalize_failed_or_missing_token"})
                continue
            ident = account_identity(account)
            if not ident:
                skipped_objects.append({"source": doc.source, "path": path, "reason": "missing_identity"})
                continue
            existing = by_identity.get(ident)
            if existing:
                existing.setdefault("sources", []).append({"source": doc.source, "path": path})
                # 如果重复项中有更完整 token，补齐。
                for key in SECRET_KEYS | {"chatgpt_account_id", "chatgpt_user_id", "plan_type", "token_expires_at", "sub2api_account_id"}:
                    if not existing.get(key) and account.get(key):
                        existing[key] = account.get(key)
                continue
            account["identity"] = ident
            account["sources"] = [{"source": doc.source, "path": path}]
            by_identity[ident] = account
    stats = {
        "json_files": parse_stats.json_files,
        "zip_files": parse_stats.zip_files,
        "sevenz_files": parse_stats.sevenz_files,
        "json_documents": parse_stats.json_documents,
        "raw_account_objects": raw_objects,
        "deduped_accounts": len(by_identity),
        "skipped_files": parse_stats.skipped_files,
        "skipped_objects": skipped_objects,
    }
    return list(by_identity.values()), stats


def health_check(account: dict[str, Any], *, proxy: str = "", timeout: int = 45, refresh: bool = True, check_models: bool = False, check_quota: bool = True, backend: str = "auto") -> dict[str, Any]:
    started = time.perf_counter()
    access_token = str(account.get("access_token") or "").strip()
    refresh_token = str(account.get("refresh_token") or "").strip()
    refreshed = False
    refresh_error = ""
    ctx = token_context(access_token, account.get("id_token"))
    if refresh and refresh_token and (not access_token or int(ctx.get("expires_in") or 0) < 300):
        try:
            try:
                data = refresh_platform_tokens(refresh_token, proxy=proxy, timeout=max(timeout, 60), backend=backend)
            except Exception as first_exc:
                if HAS_CURL_CFFI and str(backend or "auto").lower() in {"auto", "curl", "curl_cffi"} and "invalid library" in str(first_exc).lower():
                    data = refresh_platform_tokens(refresh_token, proxy=proxy, timeout=max(timeout, 60), backend="requests")
                else:
                    raise
            access_token = str(data.get("access_token") or access_token).strip()
            account["access_token"] = access_token
            account["refresh_token"] = str(data.get("refresh_token") or refresh_token).strip()
            if data.get("id_token"):
                account["id_token"] = data.get("id_token")
            account["oauth_refreshed_at"] = data.get("refreshed_at")
            refreshed = True
        except Exception as exc:
            refresh_error = compact(exc, 500)
            if not access_token:
                return {
                    "ok": False,
                    "status": classify_failure(0, refresh_error, default="token_invalid"),
                    "status_code": 0,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "email": account.get("email"),
                    "identity": account.get("identity"),
                    "error": refresh_error,
                    "refreshed": False,
                }
    if not access_token:
        return {
            "ok": False,
            "status": "missing_token",
            "status_code": 0,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "email": account.get("email"),
            "identity": account.get("identity"),
            "error": "缺少 access_token",
            "refreshed": refreshed,
        }
    try:
        session = new_session(proxy, backend=backend)
        try:
            resp = session.get(f"{CHATGPT_BASE}/backend-api/me", headers=auth_headers(access_token), timeout=timeout, verify=True)
            used_backend = "curl_cffi" if HAS_CURL_CFFI and str(backend or "auto").lower() in {"auto", "curl", "curl_cffi"} else "requests"
        except Exception as first_exc:
            if HAS_CURL_CFFI and str(backend or "auto").lower() in {"auto", "curl", "curl_cffi"} and "invalid library" in str(first_exc).lower():
                session = new_session(proxy, backend="requests")
                resp = session.get(f"{CHATGPT_BASE}/backend-api/me", headers=auth_headers(access_token), timeout=timeout, verify=True)
                used_backend = "requests"
            else:
                raise
        status_code = int(getattr(resp, "status_code", 0) or 0)
        body_text = str(getattr(resp, "text", "") or "")
        body_json = response_json(resp)
        elapsed = int((time.perf_counter() - started) * 1000)
        result: dict[str, Any] = {
            "ok": status_code == 200,
            "status": "ok" if status_code == 200 else classify_failure(status_code, body_text or body_json),
            "status_code": status_code,
            "latency_ms": elapsed,
            "email": account.get("email"),
            "identity": account.get("identity"),
            "plan_type": account.get("plan_type"),
            "chatgpt_account_id": account.get("chatgpt_account_id"),
            "token_sha": token_hash(access_token),
            "refreshed": refreshed,
            "refresh_error": refresh_error,
            "checked_at": now_iso(),
            "http_backend": used_backend,
        }
        if status_code == 200:
            next_ctx = token_context(access_token, account.get("id_token"))
            result.update({
                "plan_type": first_non_empty(extract_plan(body_json), next_ctx.get("plan_type"), account.get("plan_type")),
                "chatgpt_account_id": first_non_empty(account.get("chatgpt_account_id"), next_ctx.get("chatgpt_account_id")),
                "chatgpt_user_id": first_non_empty(account.get("chatgpt_user_id"), next_ctx.get("chatgpt_user_id")),
                "token_expires_at": first_non_empty(next_ctx.get("expires_at"), account.get("token_expires_at")),
            })
            def auxiliary_get(path: str):
                nonlocal used_backend
                try:
                    return session.get(f"{CHATGPT_BASE}{path}", headers=auth_headers(access_token), timeout=max(timeout, 60), verify=True)
                except Exception as first_exc:
                    if used_backend == "curl_cffi" and "invalid library" in str(first_exc).lower():
                        fallback_session = new_session(proxy, backend="requests")
                        used_backend = "requests"
                        result["http_backend"] = used_backend
                        return fallback_session.get(f"{CHATGPT_BASE}{path}", headers=auth_headers(access_token), timeout=max(timeout, 60), verify=True)
                    raise

            if check_models:
                model_resp = auxiliary_get("/backend-api/models")
                result["models_status_code"] = int(getattr(model_resp, "status_code", 0) or 0)
                result["models_ok"] = result["models_status_code"] == 200
                if result["models_ok"]:
                    models_json = response_json(model_resp)
                    result["available_model_count"] = extract_model_count(models_json)
                else:
                    result["models_error"] = compact(response_json(model_resp), 500)
            if check_quota:
                usage_resp = auxiliary_get("/backend-api/wham/usage")
                result["usage_status_code"] = int(getattr(usage_resp, "status_code", 0) or 0)
                usage_json = response_json(usage_resp)
                if result["usage_status_code"] == 200:
                    quota = summarize_wham_usage(usage_json)
                    if quota.get("quota_status") == "unknown":
                        quota = summarize_quota(body_json, usage_json)
                    result.update({k: v for k, v in quota.items() if v not in (None, "")})
                    result["quota_source"] = "chatgpt-wham"
                    usage_plan = extract_plan(usage_json)
                    if usage_plan:
                        result["plan_type"] = usage_plan
                else:
                    result["quota_status"] = "failed"
                    result["quota_error_status"] = classify_failure(result["usage_status_code"], usage_json, default="quota_failed")
                    result["quota_error"] = compact(usage_json, 500)
        else:
            result["error"] = compact(body_json if body_json else body_text, 800)
        return result
    except Exception as exc:
        text = compact(exc, 800)
        return {
            "ok": False,
            "status": classify_failure(0, text),
            "status_code": 0,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "email": account.get("email"),
            "identity": account.get("identity"),
            "error": text,
            "refreshed": refreshed,
            "refresh_error": refresh_error,
            "checked_at": now_iso(),
        }


def extract_plan(*payloads: dict[str, Any]) -> str:
    for payload in payloads:
        for item in walk_dicts(payload):
            for key in ("plan_type", "planType", "chatgpt_plan_type", "account_plan_type"):
                value = str(item.get(key) or "").strip()
                if value:
                    return value
            account = item.get("account") if isinstance(item.get("account"), dict) else {}
            value = str(account.get("planType") or account.get("plan_type") or "").strip()
            if value:
                return value
    return ""


def extract_model_count(payload: dict[str, Any]) -> int:
    for item in walk_dicts(payload):
        for key in ("models", "items"):
            value = item.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return None


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


def summarize_wham_usage(payload: dict[str, Any]) -> dict[str, Any]:
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
            label = first_non_empty((rate or {}).get("name") if isinstance(rate, dict) else "", f"additional.{index + 1}")
            add_rate(label, rate)

    primary = next((item for item in items if item.get("label") == "rate_limit.primary_window"), items[0] if items else {})
    secondary = next((item for item in items if item.get("label") == "rate_limit.secondary_window"), {})
    limit_reached = bool(rate_limit.get("limit_reached"))
    allowed = rate_limit.get("allowed")
    if not items and (limit_reached or allowed is False):
        primary = {"label": "rate_limit.primary_window", "used": 100.0, "total": 100.0, "remaining": 0.0, "reset_at": ""}
        items.append(primary)
    known_windows = [item for item in (primary, secondary) if item and item.get("remaining") is not None]
    overall_remaining = min((float(item.get("remaining") or 0.0) for item in known_windows), default=None)
    overall_used = max((float(item.get("used") or 0.0) for item in known_windows), default=None)
    return {
        "quota_status": "known" if items else "unknown",
        "quota_used": overall_used if overall_used is not None else primary.get("used"),
        "quota_total": primary.get("total"),
        "quota_remaining": overall_remaining if overall_remaining is not None else primary.get("remaining"),
        "quota_reset_at": primary.get("reset_at") or "",
        "quota_5h_used": primary.get("used"),
        "quota_5h_total": primary.get("total"),
        "quota_5h_remaining": primary.get("remaining"),
        "quota_5h_reset_at": primary.get("reset_at") or "",
        "quota_7d_used": secondary.get("used"),
        "quota_7d_total": secondary.get("total"),
        "quota_7d_remaining": secondary.get("remaining"),
        "quota_7d_reset_at": secondary.get("reset_at") or "",
        "quota_details": {
            "items": items[:40],
            "source": "chatgpt-wham",
            "allowed": allowed,
            "limit_reached": limit_reached,
            "plan_type": str(payload.get("plan_type") or ""),
        },
    }


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
    label = first_non_empty(item.get("model"), item.get("model_slug"), item.get("slug"), item.get("name"), item.get("title"), item.get("type"), "quota")
    return [{"label": label, "used": used, "total": total, "remaining": remaining, "reset_at": first_non_empty(item.get("reset_at"), item.get("reset_time"), item.get("resets_at"), item.get("next_reset_at"))}]


def summarize_quota(*payloads: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for payload in payloads:
        for item in walk_dicts(payload):
            candidates.extend(_quota_candidates_from_dict(item))
    compact_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if marker in seen:
            continue
        seen.add(marker)
        compact_items.append(item)
        if len(compact_items) >= 30:
            break
    primary = next((item for item in compact_items if item.get("total") is not None or item.get("remaining") is not None), compact_items[0] if compact_items else {})
    return {
        "quota_status": "known" if compact_items else "unknown",
        "quota_used": primary.get("used"),
        "quota_total": primary.get("total"),
        "quota_remaining": primary.get("remaining"),
        "quota_reset_at": primary.get("reset_at") or "",
        "quota_details": {"items": compact_items, "source": "chatgpt-backend"},
    }


def has_available_quota(result: dict[str, Any]) -> bool:
    window_remaining = [
        _num(result.get("quota_5h_remaining")),
        _num(result.get("quota_7d_remaining")),
    ]
    known_windows = [value for value in window_remaining if value is not None]
    if known_windows:
        return all(value > 0 for value in known_windows)
    details = result.get("quota_details") if isinstance(result.get("quota_details"), dict) else {}
    detail_items = details.get("items") if isinstance(details.get("items"), list) else []
    rate_windows = [
        _num(item.get("remaining"))
        for item in detail_items
        if isinstance(item, dict) and str(item.get("label") or "") in {"rate_limit.primary_window", "rate_limit.secondary_window"}
    ]
    known_detail_windows = [value for value in rate_windows if value is not None]
    if known_detail_windows:
        return all(value > 0 for value in known_detail_windows)
    remaining = _num(result.get("quota_remaining"))
    if remaining is not None:
        return remaining > 0
    total = _num(result.get("quota_total"))
    used = _num(result.get("quota_used"))
    if total is not None and used is not None:
        return total > used
    return str(result.get("quota_status") or "").lower() in {"known", "ok"} and bool(result.get("ok"))


def walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def build_sub2api_document(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for account in accounts:
        items.append({
            "name": first_non_empty(account.get("name"), account.get("email"), "ChatGPT Account"),
            "platform": "openai",
            "type": "oauth",
            "concurrency": 10,
            "priority": 1,
            "credentials": {
                "access_token": account.get("access_token") or "",
                "refresh_token": account.get("refresh_token") or "",
                "id_token": account.get("id_token") or "",
                "chatgpt_account_id": account.get("chatgpt_account_id") or "",
                "chatgpt_user_id": account.get("chatgpt_user_id") or "",
                "email": account.get("email") or "",
                "expires_at": account.get("token_expires_at") or "",
                "plan_type": account.get("plan_type") or "",
            },
            "extra": {
                "email": account.get("email") or "",
                "source": account.get("source_type") or "",
                "health_checked_at": account.get("health_checked_at") or "",
            },
        })
    return {"exported_at": now_iso(), "proxies": [], "accounts": items}


def build_cpa_document(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    for account in accounts:
        items.append({
            "type": "codex",
            "provider": "codex",
            "email": account.get("email") or "",
            "name": first_non_empty(account.get("name"), account.get("email"), "ChatGPT Account"),
            "access_token": account.get("access_token") or "",
            "refresh_token": account.get("refresh_token") or "",
            "id_token": account.get("id_token") or "",
            "session_token": account.get("session_token") or "",
            "account_id": account.get("chatgpt_account_id") or "",
            "user_id": account.get("chatgpt_user_id") or "",
            "plan_type": account.get("plan_type") or "",
            "expires_at": account.get("token_expires_at") or "",
            "source_type": account.get("source_type") or "",
        })
    return {"type": "cpa_accounts", "exported_at": now_iso(), "accounts": items}


def write_reports(out_dir: Path, accounts: list[dict[str, Any]], results: list[dict[str, Any]], stats: dict[str, Any]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    by_identity = {str(item.get("identity")): item for item in accounts}
    keep_alive_only = bool(stats.get("keep_alive_only", True))
    keep_with_quota_only = bool(stats.get("keep_with_quota_only", False))
    filtered_identities = {
        str(item.get("identity"))
        for item in results
        if (item.get("ok") or not keep_alive_only) and (not keep_with_quota_only or has_available_quota(item))
    }
    alive_accounts = []
    safe_results = []
    for result in results:
        account = by_identity.get(str(result.get("identity"))) or {}
        safe = dict(result)
        safe["source_type"] = account.get("source_type")
        safe["sources"] = account.get("sources", [])[:5]
        safe_results.append(safe)
        if str(result.get("identity")) in filtered_identities and account:
            full = dict(account)
            full["health_status"] = "ok" if result.get("ok") else str(result.get("status") or "unknown")
            full["health_checked_at"] = result.get("checked_at") or now_iso()
            for key in ("plan_type", "chatgpt_account_id", "chatgpt_user_id", "token_expires_at", "quota_status", "quota_used", "quota_total", "quota_remaining", "quota_reset_at"):
                if result.get(key):
                    full[key] = result.get(key)
            alive_accounts.append(full)

    paths = {
        "summary_csv": str(out_dir / "summary.csv"),
        "alive_safe_csv": str(out_dir / "alive_accounts.safe.csv"),
        "results_json": str(out_dir / "results.json"),
        "alive_accounts_json": str(out_dir / "alive_accounts.full.json"),
        "alive_sub2api_json": str(out_dir / "alive_sub2api.json"),
        "alive_cpa_json": str(out_dir / "alive_cpa.json"),
        "skipped_json": str(out_dir / "skipped.json"),
    }
    stats["alive"] = sum(1 for item in results if item.get("ok"))
    stats["failed"] = len(results) - int(stats["alive"])
    stats["kept_accounts"] = len(alive_accounts)
    stats["with_quota"] = sum(1 for item in results if has_available_quota(item))
    with (out_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "ok",
                "status",
                "status_code",
                "email",
                "identity",
                "plan_type",
                "quota_status",
                "quota_used",
                "quota_total",
                "quota_remaining",
                "chatgpt_account_id",
                "latency_ms",
                "refreshed",
                "source_type",
                "token_sha",
                "error",
                "sources",
            ],
        )
        writer.writeheader()
        for item in safe_results:
            row = {key: item.get(key, "") for key in writer.fieldnames or []}
            row["sources"] = "; ".join(str(s.get("source") or "") for s in item.get("sources") or [])
            writer.writerow(row)
    with (out_dir / "alive_accounts.safe.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["email", "identity", "plan_type", "quota_status", "quota_used", "quota_total", "quota_remaining", "chatgpt_account_id", "token_sha", "source_type", "sources"],
        )
        writer.writeheader()
        for account in alive_accounts:
            writer.writerow({
                "email": account.get("email", ""),
                "identity": account.get("identity", ""),
                "plan_type": account.get("plan_type", ""),
                "quota_status": account.get("quota_status", ""),
                "quota_used": account.get("quota_used", ""),
                "quota_total": account.get("quota_total", ""),
                "quota_remaining": account.get("quota_remaining", ""),
                "chatgpt_account_id": account.get("chatgpt_account_id", ""),
                "token_sha": account.get("token_sha", ""),
                "source_type": account.get("source_type", ""),
                "sources": "; ".join(str(s.get("source") or "") for s in account.get("sources") or []),
            })
    (out_dir / "results.json").write_text(json.dumps({"stats": stats, "results": safe_results}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "alive_accounts.full.json").write_text(json.dumps(alive_accounts, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "alive_sub2api.json").write_text(json.dumps(build_sub2api_document(alive_accounts), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "alive_cpa.json").write_text(json.dumps(build_cpa_document(alive_accounts), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "skipped.json").write_text(json.dumps({"skipped_files": stats.get("skipped_files", []), "skipped_objects": stats.get("skipped_objects", [])}, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描 ChatGPT/CPA/sub2api JSON，转换并测活账号。")
    parser.add_argument("--input", "-i", default=str(Path.cwd() / "垃圾"), help="账号 JSON 文件夹，默认当前目录下的 垃圾")
    parser.add_argument("--output", "-o", default="", help="报告输出目录；默认 input\\health_reports\\gpt_health_时间")
    parser.add_argument("--concurrency", "-c", type=int, default=8, help="测活并发，默认 8")
    parser.add_argument("--timeout", type=int, default=45, help="单请求超时秒数，默认 45")
    parser.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "", help="测活代理，可填 http://user:pass@host:port")
    parser.add_argument("--no-refresh", action="store_true", help="不使用 refresh_token 刷新 access_token")
    parser.add_argument("--check-models", action="store_true", help="存活后额外请求 backend-api/models")
    parser.add_argument("--backend", choices=["auto", "curl", "requests"], default="auto", help="HTTP 后端，默认 auto(curl_cffi，TLS异常时自动回退 requests)")
    parser.add_argument("--no-archives", action="store_true", help="不读取 zip/7z 内 JSON")
    parser.add_argument("--dry-run", action="store_true", help="只解析账号，不发起网络测活")
    parser.add_argument("--limit", type=int, default=0, help="只测前 N 个账号，0 表示全部")
    parser.add_argument("--only-from-report", default="", help="只重测某个 results.json 中出现的账号")
    parser.add_argument("--only-status", default="", help="配合 --only-from-report，只重测这些状态，逗号分隔，如 network_error,timeout")
    args = parser.parse_args(argv)

    root = Path(args.input).expanduser().resolve()
    if not root.exists():
        print(f"[ERROR] 输入目录不存在：{root}", file=sys.stderr)
        return 2
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output).expanduser().resolve() if args.output else root / "health_reports" / f"gpt_health_{timestamp}"

    print(f"[1/3] 扫描目录：{root}")
    accounts, stats = collect_accounts(root, include_archives=not args.no_archives)
    if args.only_from_report:
        report_path = Path(args.only_from_report).expanduser().resolve()
        try:
            report_data = json.loads(report_path.read_text(encoding="utf-8"))
            report_results = report_data.get("results") if isinstance(report_data, dict) else []
            wanted_statuses = {item.strip() for item in str(args.only_status or "").split(",") if item.strip()}
            wanted_identities = {
                str(item.get("identity") or "")
                for item in (report_results or [])
                if isinstance(item, dict) and item.get("identity") and (not wanted_statuses or str(item.get("status") or "") in wanted_statuses)
            }
            before = len(accounts)
            accounts = [item for item in accounts if str(item.get("identity") or "") in wanted_identities]
            stats["filtered_from_report"] = str(report_path)
            stats["filtered_statuses"] = sorted(wanted_statuses)
            stats["filtered_accounts_before"] = before
            stats["filtered_accounts_after"] = len(accounts)
        except Exception as exc:
            print(f"[ERROR] 读取 --only-from-report 失败：{exc}", file=sys.stderr)
            return 2
    if args.limit and args.limit > 0:
        accounts = accounts[: args.limit]
    stats["selected_accounts"] = len(accounts)
    print(
        f"      JSON文档={stats['json_documents']}，原始账号对象={stats['raw_account_objects']}，"
        f"去重后={stats['deduped_accounts']}，本次选择={len(accounts)}"
    )
    if stats.get("skipped_files"):
        print(f"      跳过文件/压缩项={len(stats['skipped_files'])}，详见 skipped.json")
    if not accounts:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "skipped.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[DONE] 没有可测账号，报告：{out_dir}")
        return 1

    results: list[dict[str, Any]] = []
    if args.dry_run:
        print("[2/3] dry-run：跳过网络测活")
        for account in accounts:
            results.append({
                "ok": False,
                "status": "not_checked",
                "status_code": 0,
                "email": account.get("email"),
                "identity": account.get("identity"),
                "plan_type": account.get("plan_type"),
                "chatgpt_account_id": account.get("chatgpt_account_id"),
                "token_sha": account.get("token_sha"),
                "checked_at": "",
            })
    else:
        concurrency = max(1, min(64, int(args.concurrency or 8)))
        print(f"[2/3] 开始测活：账号={len(accounts)}，并发={concurrency}，刷新token={'否' if args.no_refresh else '是'}")
        done = 0
        ok_count = 0
        status_counts: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=min(concurrency, len(accounts))) as executor:
            future_map = {
                executor.submit(
                    health_check,
                    account,
                    proxy=args.proxy,
                    timeout=max(5, int(args.timeout or 45)),
                    refresh=not args.no_refresh,
                    check_models=bool(args.check_models),
                    backend=args.backend,
                ): account
                for account in accounts
            }
            for future in as_completed(future_map):
                account = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "ok": False,
                        "status": "failed",
                        "status_code": 0,
                        "email": account.get("email"),
                        "identity": account.get("identity"),
                        "error": compact(exc, 800),
                        "checked_at": now_iso(),
                    }
                results.append(result)
                done += 1
                status = str(result.get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
                if result.get("ok"):
                    ok_count += 1
                if done == 1 or done % 20 == 0 or done == len(accounts):
                    print(f"      进度 {done}/{len(accounts)}，存活={ok_count}，状态={status_counts}")

    stats["checked_accounts"] = len(results)
    stats["status_counts"] = {}
    for item in results:
        status = str(item.get("status") or "unknown")
        stats["status_counts"][status] = stats["status_counts"].get(status, 0) + 1
    stats["finished_at"] = now_iso()
    print(f"[3/3] 写出报告：{out_dir}")
    paths = write_reports(out_dir, accounts, results, stats)
    print(f"[DONE] 存活={stats.get('alive', 0)}，失败={stats.get('failed', 0)}")
    for key, value in paths.items():
        print(f"      {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
