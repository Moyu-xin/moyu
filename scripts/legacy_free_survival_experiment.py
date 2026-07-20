from __future__ import annotations

import argparse
import calendar
import copy
import hashlib
import importlib.util
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

from curl_cffi import requests


ROOT = Path(__file__).resolve().parents[1]
LEGACY_SOURCE = (
    ROOT.parent
    / "PP协议"
    / "paypal-pay-public-nocdk"
    / "analysis"
    / "chatgpt2api-local-v16-migration"
    / "services"
    / "register"
    / "openai_register.py"
)
V16_LEGACY_SOURCE = (
    ROOT.parent
    / "chatgpt2api_register_extract"
    / "v1.6.0_related_files"
    / "services"
    / "register"
    / "openai_register.py"
)
CURRENT_FREE_SOURCE = ROOT / "services" / "register" / "openai_register.py"
OUTPUT_DIR = ROOT / "data" / "experiments"
CHATGPT_ME_URL = "https://chatgpt.com/backend-api/me"
CHATGPT_SESSION_URL = "https://chatgpt.com/api/auth/session"
TRACE_URL = "https://cloudflare.com/cdn-cgi/trace"
EARLY_FREE_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
EARLY_FREE_OAUTH_REDIRECT_URI = "https://platform.openai.com/auth/callback"
ALIAS_WORDS = (
    "Adele",
    "Alice",
    "Bella",
    "Chloe",
    "Daisy",
    "Elise",
    "Flora",
    "Grace",
    "Hazel",
    "Iris",
    "Julia",
    "Lily",
    "Milly",
    "Nora",
    "Olive",
    "Pearl",
    "Ruby",
    "Sophie",
    "Tessa",
    "Violet",
)

sys.path.insert(0, str(ROOT))

from services import mailbox_pool_store, partial_account_store, proxy_pool_store  # noqa: E402
from services.register import openai_register as current_register  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def append_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{now_iso()} {message}\n")


class ExperimentPartialStore:
    OAUTH_PARTIAL_STATUSES = partial_account_store.OAUTH_PARTIAL_STATUSES

    def __init__(self, path: Path) -> None:
        self.path = path

    def upsert_partial_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        safe_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"access_token", "refresh_token", "id_token", "session_token", "password"}
        }
        append_log(self.path, "PARTIAL " + json.dumps(safe_payload, ensure_ascii=False, default=str))
        return payload


def load_legacy_module(log_path: Path, source: Path = LEGACY_SOURCE, *, mailbox_group: str = "") -> ModuleType:
    if not source.exists():
        raise FileNotFoundError(f"Legacy Free source is missing: {source}")
    if source == V16_LEGACY_SOURCE and "services.account_service" not in sys.modules:
        # PlatformRegistrar does not use account_service; only the v1.6 worker does.
        # The stub lets the original protocol module load against current runtime services.
        account_service_stub = ModuleType("services.account_service")
        account_service_stub.account_service = object()
        sys.modules["services.account_service"] = account_service_stub
    spec = importlib.util.spec_from_file_location(f"legacy_free_protocol_{source.stat().st_size}", source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load the legacy Free protocol module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    selected_mailbox_group = str(mailbox_group or current_register.config.get("mailbox_group") or "").strip()
    if mailbox_group:
        experiment_mail = mailbox_pool_store.mail_config_from_pool(mailbox_group=selected_mailbox_group)
        serialized_group = mailbox_pool_store.serialize_outlook_pool(selected_mailbox_group)
        if not serialized_group.strip():
            raise RuntimeError(f"Mailbox group has no Outlook credentials: {selected_mailbox_group}")
        providers = experiment_mail.get("providers") if isinstance(experiment_mail.get("providers"), list) else []
        outlook_provider = next(
            (item for item in providers if isinstance(item, dict) and item.get("type") == "outlook_token"),
            None,
        )
        if outlook_provider is None:
            outlook_provider = {"enable": True, "type": "outlook_token", "mode": "auto"}
            providers.append(outlook_provider)
        outlook_provider.update(
            {
                "enable": True,
                "mailbox_group": selected_mailbox_group,
                "mailboxes": serialized_group,
                "split_aliases": False,
                "split_count_per_mailbox": 0,
            }
        )
        experiment_mail["providers"] = providers
        experiment_mail["mailbox_group"] = selected_mailbox_group
    else:
        experiment_mail = copy.deepcopy(current_register.config.get("mail") or {})
    module.config.update(
        {
            "mail": experiment_mail,
            "proxy": "",
            "total": 1,
            "threads": 1,
            "k12_mode": False,
            "register_mode": "free",
            "mailbox_group": selected_mailbox_group,
        }
    )
    module.partial_account_store = ExperimentPartialStore(log_path)
    module.register_log_sink = lambda text, color="": append_log(log_path, f"[{color or 'info'}] {text}")
    return module


def rotate_outlook_base(module: ModuleType, attempt: int) -> str:
    mail = module.config.get("mail") if isinstance(module.config.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    for provider in providers:
        if not isinstance(provider, dict) or str(provider.get("type") or "").strip().lower() != "outlook_token":
            continue
        all_lines = provider.get("_experiment_all_mailboxes")
        if not isinstance(all_lines, list):
            all_lines = [line.strip() for line in str(provider.get("mailboxes") or "").splitlines() if line.strip()]
            provider["_experiment_all_mailboxes"] = all_lines
        if not all_lines:
            raise RuntimeError("The Outlook experiment pool is empty")
        selected = str(all_lines[(attempt - 1) % len(all_lines)])
        provider["mailboxes"] = selected
        address = selected.split("----", 1)[0].strip()
        return address
    raise RuntimeError("No Outlook token provider is enabled for the experiment")


def prepare_fresh_raw_outlook_bases(module: ModuleType) -> int:
    """Match v1.6: consume one untouched base inbox, without plus aliases."""
    state = current_register.mail_provider.outlook_token_pool_state()
    mail = module.config.get("mail") if isinstance(module.config.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    total = 0
    for provider in providers:
        if not isinstance(provider, dict) or str(provider.get("type") or "").strip().lower() != "outlook_token":
            continue
        fresh_lines: list[str] = []
        for raw_line in str(provider.get("mailboxes") or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            address = line.split("----", 1)[0].strip().lower()
            if address and address not in state:
                fresh_lines.append(line)
        provider["_experiment_all_mailboxes"] = fresh_lines
        provider["split_aliases"] = False
        provider["split_count_per_mailbox"] = 0
        total += len(fresh_lines)
    if total == 0:
        raise RuntimeError("No untouched Outlook base inbox remains for the v1.6-style experiment")
    return total


def prepare_random_named_outlook_aliases(module: ModuleType) -> int:
    """Use unique Outlook plus aliases such as base+Milly0326@outlook.com."""
    state = current_register.mail_provider.outlook_token_pool_state()
    tainted_bases: set[str] = set()
    for used_address, entry in state.items():
        if not isinstance(entry, dict) or str(entry.get("state") or "") not in {"used", "existing_account"}:
            continue
        used_local, separator, used_domain = str(used_address).lower().partition("@")
        if separator:
            tainted_bases.add(f"{used_local.split('+', 1)[0]}@{used_domain}")
    mail = module.config.get("mail") if isinstance(module.config.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    total = 0
    generated: set[str] = set()
    for provider in providers:
        if not isinstance(provider, dict) or str(provider.get("type") or "").strip().lower() != "outlook_token":
            continue
        alias_lines: list[str] = []
        for raw_line in str(provider.get("mailboxes") or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            address, separator, credential_tail = line.partition("----")
            local, at, domain = address.strip().partition("@")
            if not separator or not at:
                continue
            base_local = local.split("+", 1)[0]
            if f"{base_local}@{domain}".lower() in tainted_bases:
                continue
            alias = ""
            for _ in range(50):
                month = secrets.randbelow(12) + 1
                day = secrets.randbelow(calendar.monthrange(2000, month)[1]) + 1
                tag = f"{secrets.choice(ALIAS_WORDS)}{month:02d}{day:02d}"
                candidate = f"{base_local}+{tag}@{domain}"
                key = candidate.lower()
                if key not in state and key not in generated:
                    alias = candidate
                    generated.add(key)
                    break
            if alias:
                alias_lines.append(f"{alias}----{credential_tail}")
        provider["_experiment_all_mailboxes"] = alias_lines
        alias_lines.sort(key=lambda _line: secrets.randbits(64))
        provider["split_aliases"] = False
        provider["split_count_per_mailbox"] = 0
        total += len(alias_lines)
    if total == 0:
        raise RuntimeError("Unable to generate an unused Outlook word-and-date alias")
    return total


def response_code(response: Any) -> str:
    try:
        data = response.json()
    except Exception:
        return ""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in {"code", "error_code"} and isinstance(item, (str, int)):
                    found.append(str(item)[:120])
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found[0] if found else ""


def egress_fingerprint(proxy: str) -> dict[str, Any]:
    try:
        response = requests.get(
            TRACE_URL,
            proxy=proxy,
            impersonate="chrome",
            timeout=30,
        )
        fields = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
        ip = str(fields.get("ip") or "")
        return {
            "country": str(fields.get("loc") or ""),
            "ip_hash": hashlib.sha256(ip.encode("utf-8")).hexdigest()[:12] if ip else "",
        }
    except Exception as error:
        return {"country": "", "ip_hash": "", "error": f"{type(error).__name__}: {error}"[:300]}


def assign_registration_proxy(attempt: int, slots: int) -> dict[str, Any]:
    last_assignment: dict[str, Any] = {}
    for offset in range(6):
        assignment = proxy_pool_store.assign_proxy(
            attempt + offset * max(1, slots),
            total_threads=max(1, slots),
            slot_index=(attempt - 1) % max(1, slots),
            purpose="legacy_free_survival_register",
            validate_node=True,
        )
        last_assignment = assignment
        if not assignment.get("proxy") or not assignment.get("openai_restricted"):
            return assignment
        proxy_pool_store.report_proxy_failure(
            assignment,
            "OpenAI Auth precheck returned HTTP 403",
            quarantine_seconds=900,
        )
    last_assignment["proxy"] = ""
    last_assignment["error"] = "No non-403 OpenAI registration node was found after 6 assignments"
    return last_assignment


def restore_registration_proxy(
    account: dict[str, Any],
    index: int,
    *,
    validate_node: bool = True,
) -> dict[str, Any]:
    registration = account.get("registration_proxy") if isinstance(account.get("registration_proxy"), dict) else {}
    try:
        slot = max(0, int(registration.get("slot") or 1) - 1)
    except Exception:
        slot = 0
    assignment = proxy_pool_store.assign_proxy(
        index,
        total_threads=2,
        slot_index=slot,
        purpose="legacy_free_survival_health",
        account=account,
        validate_node=validate_node,
    )
    if assignment.get("proxy") or str(registration.get("source") or "") != "managed_mihomo":
        return assignment
    node_name = str(registration.get("node_name") or registration.get("label") or "").strip()
    if not node_name:
        return assignment
    try:
        from services.managed_mihomo_service import managed_mihomo_manager

        shifted_store = copy.deepcopy(proxy_pool_store.get_proxy_pool_config(include_secret=True))
        managed = shifted_store.get("managed_mihomo") if isinstance(shifted_store.get("managed_mihomo"), dict) else {}
        managed["proxy_port_base"] = 42100
        shifted_store["managed_mihomo"] = managed
        shifted = managed_mihomo_manager.assign(
            slot=slot,
            task_index=max(1, index),
            total_threads=2,
            store=shifted_store,
            purpose="legacy_free_survival_health_shifted",
            exact_node_name=node_name,
            validate_node=validate_node,
        )
        shifted["sticky"] = True
        shifted["sticky_source"] = "registration_shifted_local_port"
        return shifted
    except Exception as shifted_error:
        assignment["shifted_port_error"] = str(shifted_error)[:300]
        return assignment


def check_account(account: dict[str, Any], index: int, *, refresh_oauth: bool = False) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc)
    registered_at = parse_time(account.get("registered_at"))
    assignment = restore_registration_proxy(account, index, validate_node=False)
    proxy = str(assignment.get("proxy") or "").strip()
    if not proxy:
        return {
            "checked_at": checked_at.isoformat(),
            "age_seconds": max(0, int((checked_at - registered_at).total_seconds())),
            "alive": None,
            "status_code": 0,
            "response_code": "proxy_restore_failed",
            "error": str(assignment.get("error") or "registration proxy unavailable")[:300],
        }

    current_egress = egress_fingerprint(proxy)
    baseline = account.get("registration_egress") if isinstance(account.get("registration_egress"), dict) else {}
    try:
        response = requests.get(
            CHATGPT_ME_URL,
            headers={
                "Authorization": "Bearer " + str(account.get("access_token") or ""),
                "Accept": "application/json",
            },
            proxy=proxy,
            impersonate="chrome",
            timeout=45,
        )
        status = int(response.status_code)
        code = response_code(response)
        if status == 200:
            alive: bool | None = True
        elif status == 401 or code in {"token_revoked", "invalid_token", "unauthorized"}:
            alive = False
        else:
            alive = None
        error = ""
    except Exception as exc:
        status = 0
        code = type(exc).__name__
        alive = None
        error = str(exc)[:300]
    nextauth_session_ok: bool | None = None
    nextauth_session_status = 0
    session_token = str(account.get("session_token") or "").strip()
    if alive is not True and session_token:
        primary_alive = alive
        primary_code = code
        try:
            session_response = requests.get(
                CHATGPT_SESSION_URL,
                headers={"Accept": "application/json", "Referer": "https://chatgpt.com/"},
                cookies={
                    "__Secure-next-auth.session-token": session_token,
                    "next-auth.session-token": session_token,
                },
                proxy=proxy,
                impersonate="chrome",
                timeout=45,
            )
            nextauth_session_status = int(session_response.status_code or 0)
            try:
                session_data = session_response.json()
            except Exception:
                session_data = {}
            session_access_token = str(session_data.get("accessToken") or session_data.get("access_token") or "")
            if nextauth_session_status == 200 and session_access_token:
                account["access_token"] = session_access_token
                nextauth_session_ok = True
                alive = True
                code = "nextauth_session_ok"
            elif nextauth_session_status in {401, 404} or (nextauth_session_status == 200 and not session_access_token):
                nextauth_session_ok = False
                alive = False
                code = "nextauth_session_invalid"
        except Exception as session_error:
            nextauth_session_ok = None
            if primary_alive is False:
                alive = None
                code = f"{primary_code or 'access_token_invalid'}_session_check_error"
            if not error:
                error = str(session_error)[:300]
    oauth_refresh_ok: bool | None = None
    oauth_refresh_error = ""
    if refresh_oauth and str(account.get("refresh_token") or "").strip():
        try:
            from services.account_inventory_service import _refresh_platform_tokens

            refreshed = _refresh_platform_tokens(
                str(account.get("refresh_token") or ""),
                client_id=str(account.get("oauth_client_id") or account.get("client_id") or ""),
                proxy=proxy,
            )
            account["access_token"] = str(refreshed.get("access_token") or account.get("access_token") or "")
            account["refresh_token"] = str(refreshed.get("refresh_token") or account.get("refresh_token") or "")
            if refreshed.get("id_token"):
                account["id_token"] = str(refreshed.get("id_token") or "")
            account["oauth_refreshed_at"] = str(refreshed.get("refreshed_at") or now_iso())
            oauth_refresh_ok = True
            alive = True
            code = "oauth_refresh_ok"
        except Exception as refresh_error:
            oauth_refresh_error = f"{type(refresh_error).__name__}: {refresh_error}"[:500]
            lowered = oauth_refresh_error.lower()
            oauth_refresh_ok = False
            if any(
                marker in lowered
                for marker in (
                    "invalid_grant",
                    "refresh_token_invalidated",
                    "session has ended",
                    "account_deactivated",
                    "deleted or deactivated",
                )
            ):
                alive = False
                code = "oauth_refresh_invalid"
    return {
        "checked_at": checked_at.isoformat(),
        "age_seconds": max(0, int((checked_at - registered_at).total_seconds())),
        "alive": alive,
        "status_code": status,
        "response_code": code,
        "same_egress": bool(
            baseline.get("ip_hash")
            and current_egress.get("ip_hash")
            and baseline.get("ip_hash") == current_egress.get("ip_hash")
        ),
        "egress": current_egress,
        "proxy_label": str(assignment.get("label") or ""),
        "error": error,
        "oauth_refresh_ok": oauth_refresh_ok,
        "oauth_refresh_error": oauth_refresh_error,
        "nextauth_session_ok": nextauth_session_ok,
        "nextauth_session_status": nextauth_session_status,
    }


def latest_result_file() -> Path:
    files = sorted(OUTPUT_DIR.glob("legacy-free-survival-*.json"), key=lambda item: item.stat().st_mtime)
    if not files:
        raise FileNotFoundError("No legacy Free survival experiment exists")
    return files[-1]


def latest_register_only_file() -> Path:
    files = sorted(OUTPUT_DIR.glob("no-oauth-register-only-*.json"), key=lambda item: item.stat().st_mtime)
    if not files:
        raise FileNotFoundError("No no-OAuth register-only experiment exists")
    return files[-1]


def mailbox_for_existing_account(module: ModuleType, account: dict[str, Any]) -> dict[str, Any]:
    inbox_address = str(account.get("inbox_address") or "").strip().lower()
    target_address = str(account.get("email") or "").strip()
    mail = module.config.get("mail") if isinstance(module.config.get("mail"), dict) else {}
    providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
    for provider in providers:
        if not isinstance(provider, dict) or str(provider.get("type") or "").strip().lower() != "outlook_token":
            continue
        credentials = module.mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
        for credential in credentials:
            if str(credential.get("email") or "").strip().lower() != inbox_address:
                continue
            return {
                "provider": "outlook_token",
                "provider_ref": str(provider.get("provider_ref") or ""),
                "address": target_address,
                "inbox_address": str(credential.get("email") or ""),
                "label": str(provider.get("label") or "outlook_token"),
                "client_id": str(credential.get("client_id") or ""),
                "refresh_token": str(credential.get("refresh_token") or ""),
                "password": str(credential.get("password") or ""),
                "_code_not_before": datetime.now(timezone.utc),
            }
    raise RuntimeError("The Outlook credential for this account is no longer in the configured mailbox pool")


def check_register_only_account(module: ModuleType, account: dict[str, Any], index: int) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc)
    registered_at = parse_time(account.get("registered_at"))
    assignment = restore_registration_proxy(account, index, validate_node=False)
    proxy = str(assignment.get("proxy") or "").strip()
    if not proxy:
        return {
            "checked_at": checked_at.isoformat(),
            "age_seconds": max(0, int((checked_at - registered_at).total_seconds())),
            "alive": None,
            "otp_verified": False,
            "callback_ready": False,
            "tokens_collected": False,
            "error": str(assignment.get("error") or "registration proxy unavailable")[:300],
        }

    mailbox = mailbox_for_existing_account(module, account)
    registrar = module.PlatformRegistrar(proxy=proxy)
    registrar.current_mailbox = mailbox
    registrar.email = str(account.get("email") or "")
    registrar.password = ""
    try:
        module.step(index, "Register-only 测活：初始化邮箱登录，不跟随 callback")
        registrar._free_wu_init_page_email(registrar.email, index)
        code = module.wait_for_code(mailbox, register_proxy=proxy, register_mode="free_wu")
        if not code:
            raise RuntimeError("health_email_otp_timeout")
        validate_data = registrar._validate_otp(code, index)
        continue_url = str(validate_data.get("continue_url") or "").strip()
        parsed = urlparse(continue_url)
        next_path = str(parsed.path or "")[:160]
        callback_ready = "/callback/" in next_path.lower() or "oauth_callback" in next_path.lower()
        page = str(validate_data.get("page") or "")[:120]
        # OTP validation proves the account can authenticate. A callback path further
        # confirms it is treated as an existing account rather than a new profile.
        alive = bool(callback_ready or (continue_url and "about-you" not in continue_url.lower()))
        return {
            "checked_at": checked_at.isoformat(),
            "age_seconds": max(0, int((checked_at - registered_at).total_seconds())),
            "alive": alive,
            "otp_verified": True,
            "callback_ready": callback_ready,
            "next_host": str(parsed.hostname or ""),
            "next_path": next_path,
            "page": page,
            "tokens_collected": False,
            "oauth_callback_visited": False,
            "session_requested": False,
            "proxy_label": str(assignment.get("label") or ""),
            "egress": egress_fingerprint(proxy),
            "error": "",
        }
    except Exception as error:
        text = str(error)
        terminal = any(marker in text.lower() for marker in ("deactivated", "disabled", "deleted", "suspended"))
        return {
            "checked_at": checked_at.isoformat(),
            "age_seconds": max(0, int((checked_at - registered_at).total_seconds())),
            "alive": False if terminal else None,
            "otp_verified": False,
            "callback_ready": False,
            "tokens_collected": False,
            "oauth_callback_visited": False,
            "session_requested": False,
            "error": f"{type(error).__name__}: {error}"[:500],
        }
    finally:
        registrar.close()


def check_register_only_batch(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    log_path = path.with_suffix(".health.log")
    module = load_legacy_module(log_path)
    alive = 0
    dead = 0
    unknown = 0
    for index, account in enumerate(state.get("accounts") or [], 1):
        previous_checks = account.get("login_checks") if isinstance(account.get("login_checks"), list) else []
        previous = previous_checks[-1] if previous_checks and isinstance(previous_checks[-1], dict) else {}
        terminal = previous.get("alive") is False and "account_deactivated" in str(previous.get("error") or "").lower()
        check = previous if terminal else check_register_only_account(module, account, index)
        if not terminal:
            account.setdefault("login_checks", []).append(check)
        if check.get("alive") is True:
            alive += 1
        elif check.get("alive") is False:
            dead += 1
        else:
            unknown += 1
        print(
            "account={} age_min={:.1f} alive={} otp={} callback={} tokens={} error={}".format(
                index,
                int(check.get("age_seconds") or 0) / 60,
                check.get("alive"),
                check.get("otp_verified"),
                check.get("callback_ready"),
                check.get("tokens_collected"),
                bool(check.get("error")),
            )
        )
        write_json(path, state)
    summary = {
        "checked_at": now_iso(),
        "total": alive + dead + unknown,
        "alive": alive,
        "dead": dead,
        "unknown": unknown,
        "tokens_collected": False,
    }
    state["last_login_check_summary"] = summary
    write_json(path, state)
    print(f"summary total={summary['total']} alive={alive} dead={dead} unknown={unknown} tokens=False")
    return summary


def register_batch(
    target: int,
    max_attempts: int,
    slots: int,
    *,
    source: Path = LEGACY_SOURCE,
    label: str = "legacy-free-survival",
    early_free_oauth: bool = False,
    mailbox_group: str = "",
    protocol_label: str = "original_free",
    named_aliases: bool = False,
    registration_method: str = "platform",
    token_delay_seconds: int = 0,
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = OUTPUT_DIR / f"{label}-{stamp}.json"
    log_path = OUTPUT_DIR / f"{label}-{stamp}.log"
    state: dict[str, Any] = {
        "schema_version": 1,
        "protocol": protocol_label,
        "legacy_source": str(source),
        "created_at": now_iso(),
        "requested_successes": target,
        "max_attempts": max_attempts,
        "mailbox_group": str(mailbox_group or ""),
        "registration_method": registration_method,
        "token_delay_seconds": max(0, int(token_delay_seconds or 0)),
        "accounts": [],
        "attempts": [],
    }
    write_json(output_path, state)
    legacy = load_legacy_module(log_path, source=source, mailbox_group=mailbox_group)
    if early_free_oauth:
        legacy.platform_oauth_client_id = EARLY_FREE_OAUTH_CLIENT_ID
        legacy.platform_oauth_redirect_uri = EARLY_FREE_OAUTH_REDIRECT_URI
    if named_aliases or early_free_oauth:
        named_aliases = prepare_random_named_outlook_aliases(legacy)
        state["mailbox_strategy"] = "random_word_mmdd_alias"
        state["named_aliases_available"] = named_aliases
    state["oauth_client_id"] = str(getattr(legacy, "platform_oauth_client_id", "") or "")
    state["oauth_redirect_uri"] = str(getattr(legacy, "platform_oauth_redirect_uri", "") or "")
    if registration_method == "free_wu" and token_delay_seconds > 0:
        original_complete_session = legacy.PlatformRegistrar._free_wu_complete_chatgpt_session

        def delayed_complete_session(registrar: Any, continue_url: str, index: int) -> dict[str, Any]:
            legacy.step(index, f"Partial mode: waiting {token_delay_seconds}s before requesting Web token")
            time.sleep(token_delay_seconds)
            return original_complete_session(registrar, continue_url, index)

        legacy.PlatformRegistrar._free_wu_complete_chatgpt_session = delayed_complete_session
    write_json(output_path, state)

    attempt = 0
    while len(state["accounts"]) < target and attempt < max_attempts:
        attempt += 1
        mailbox_base = rotate_outlook_base(legacy, attempt)
        assignment = assign_registration_proxy(attempt, slots)
        proxy = str(assignment.get("proxy") or "").strip()
        attempt_record: dict[str, Any] = {
            "attempt": attempt,
            "started_at": now_iso(),
            "proxy_source": str(assignment.get("source") or ""),
            "proxy_label": str(assignment.get("label") or ""),
            "proxy_region": str(assignment.get("region") or ""),
            "mailbox_base": mailbox_base,
            "success": False,
        }
        if not proxy:
            attempt_record["error"] = str(assignment.get("error") or "proxy assignment failed")[:500]
            attempt_record["finished_at"] = now_iso()
            state["attempts"].append(attempt_record)
            write_json(output_path, state)
            print(f"attempt={attempt} skipped proxy_error completed={len(state['accounts'])}/{target}")
            continue

        registrar = legacy.PlatformRegistrar(proxy=proxy)
        started = time.monotonic()
        try:
            result = registrar.register_free_wu(attempt) if registration_method == "free_wu" else registrar.register(attempt)
            registration_proxy = proxy_pool_store.registration_proxy_record(assignment, proxy=proxy)
            protocol_client_id = str(getattr(legacy, "platform_oauth_client_id", "") or "")
            result_client_id = str(result.get("client_id") or protocol_client_id)
            result.update(
                {
                    "experiment_protocol": protocol_label,
                    "client_id": result_client_id,
                    "oauth_client_id": protocol_client_id if registration_method == "platform" else "",
                    "oauth_redirect_uri": (
                        str(getattr(legacy, "platform_oauth_redirect_uri", "") or "")
                        if registration_method == "platform"
                        else ""
                    ),
                    "registered_at": now_iso(),
                    "proxy": proxy,
                    "registration_proxy": registration_proxy,
                    "registration_proxy_source": str(registration_proxy.get("source") or ""),
                    "registration_proxy_node_id": str(registration_proxy.get("node_id") or ""),
                    "registration_proxy_label": str(
                        registration_proxy.get("label") or registration_proxy.get("node_name") or ""
                    ),
                    "registration_egress": egress_fingerprint(proxy),
                    "health_checks": [],
                }
            )
            immediate = check_account(result, len(state["accounts"]) + 1)
            result["health_checks"].append(immediate)
            state["accounts"].append(result)
            attempt_record["success"] = True
            attempt_record["email"] = str(result.get("email") or "")
            print(
                "attempt={} success=True completed={}/{} alive={} status={} region={}".format(
                    attempt,
                    len(state["accounts"]),
                    target,
                    immediate.get("alive"),
                    immediate.get("status_code"),
                    attempt_record["proxy_region"],
                )
            )
        except Exception as error:
            attempt_record["error"] = f"{type(error).__name__}: {error}"[:500]
            print(f"attempt={attempt} success=False completed={len(state['accounts'])}/{target} error={type(error).__name__}")
        finally:
            registrar.close()
            attempt_record["elapsed_seconds"] = round(time.monotonic() - started, 1)
            attempt_record["finished_at"] = now_iso()
            state["attempts"].append(attempt_record)
            write_json(output_path, state)

    state["registration_finished_at"] = now_iso()
    state["registered"] = len(state["accounts"])
    write_json(output_path, state)
    print(f"result_file={output_path}")
    print(f"registered={len(state['accounts'])} attempts={len(state['attempts'])}")
    return output_path


def register_only_one(module: ModuleType, index: int, proxy: str) -> dict[str, Any]:
    registrar = module.PlatformRegistrar(proxy=proxy)
    mailbox: dict[str, Any] | None = None
    try:
        mailbox = module.create_mailbox(register_proxy=proxy, register_mode="free_wu")
        email = str(mailbox.get("address") or "").strip()
        if not email:
            raise RuntimeError("Outlook mailbox has no address")
        registrar.current_mailbox = mailbox
        registrar.email = email
        registrar.password = ""
        first_name, last_name = module._random_name()
        name = f"{first_name} {last_name}"
        birthdate = module._random_birthdate()

        module.step(index, "Register-only：初始化 ChatGPT 注册上下文")
        registrar._free_wu_init_page_email(email, index)
        module.step(index, "Register-only：等待邮箱验证码")
        code = module.wait_for_code(mailbox, register_proxy=proxy, register_mode="free_wu")
        if not code:
            raise RuntimeError("register_only_email_otp_timeout")
        validate_data = registrar._validate_otp(code, index)
        continue_url = str(validate_data.get("continue_url") or "").strip()
        if continue_url:
            registrar._free_wu_follow_redirects(
                continue_url,
                index,
                referer=f"{module.auth_base}/email-verification",
            )

        create_result: dict[str, Any] = {}
        last_error = ""
        for attempt in range(1, 4):
            try:
                create_result = registrar._free_wu_create_account(name, birthdate, index)
                break
            except Exception as error:
                last_error = str(error)
                if "registration_disallowed" in last_error and attempt < 3:
                    time.sleep(2)
                    continue
                raise
        if not create_result:
            raise RuntimeError(last_error or "register_only_create_account_empty")

        # Stop here deliberately. Do not visit create_result.continue_url and do not
        # call /api/auth/session or any token endpoint.
        module.mail_provider.mark_mailbox_result(mailbox, success=True)
        return {
            "email": email,
            "name": name,
            "birthdate": birthdate,
            "password": "",
            "password_set": False,
            "registered_at": now_iso(),
            "register_mode": "register_only_no_oauth",
            "account_created": True,
            "continue_url_received": bool(create_result.get("continue_url")),
            "oauth_callback_visited": False,
            "session_requested": False,
            "tokens_collected": False,
            "mailbox_group": str(mailbox.get("mailbox_group") or ""),
            "inbox_address": str(mailbox.get("inbox_address") or ""),
        }
    except Exception as error:
        if mailbox is not None:
            module.mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
        raise
    finally:
        registrar.close()


def register_only_batch(target: int, max_attempts: int, slots: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = OUTPUT_DIR / f"no-oauth-register-only-{stamp}.json"
    log_path = OUTPUT_DIR / f"no-oauth-register-only-{stamp}.log"
    state: dict[str, Any] = {
        "schema_version": 1,
        "protocol": "chatgpt_register_only_no_oauth",
        "legacy_source": str(LEGACY_SOURCE),
        "created_at": now_iso(),
        "requested_successes": target,
        "max_attempts": max_attempts,
        "oauth_callback_disabled": True,
        "session_request_disabled": True,
        "token_collection_disabled": True,
        "accounts": [],
        "attempts": [],
    }
    write_json(output_path, state)
    legacy = load_legacy_module(log_path)

    attempt = 0
    while len(state["accounts"]) < target and attempt < max_attempts:
        attempt += 1
        mailbox_base = rotate_outlook_base(legacy, attempt)
        assignment = assign_registration_proxy(attempt, slots)
        proxy = str(assignment.get("proxy") or "").strip()
        attempt_record: dict[str, Any] = {
            "attempt": attempt,
            "started_at": now_iso(),
            "mailbox_base": mailbox_base,
            "proxy_source": str(assignment.get("source") or ""),
            "proxy_label": str(assignment.get("label") or ""),
            "proxy_region": str(assignment.get("region") or ""),
            "success": False,
        }
        if not proxy:
            attempt_record["error"] = str(assignment.get("error") or "proxy assignment failed")[:500]
            attempt_record["finished_at"] = now_iso()
            state["attempts"].append(attempt_record)
            write_json(output_path, state)
            print(f"attempt={attempt} skipped proxy_error completed={len(state['accounts'])}/{target}")
            continue

        started = time.monotonic()
        try:
            result = register_only_one(legacy, attempt, proxy)
            registration_proxy = proxy_pool_store.registration_proxy_record(assignment, proxy=proxy)
            result.update(
                {
                    "proxy": proxy,
                    "registration_proxy": registration_proxy,
                    "registration_proxy_source": str(registration_proxy.get("source") or ""),
                    "registration_proxy_node_id": str(registration_proxy.get("node_id") or ""),
                    "registration_proxy_label": str(
                        registration_proxy.get("label") or registration_proxy.get("node_name") or ""
                    ),
                    "registration_egress": egress_fingerprint(proxy),
                }
            )
            state["accounts"].append(result)
            attempt_record["success"] = True
            attempt_record["email"] = str(result.get("email") or "")
            print(
                f"attempt={attempt} success=True completed={len(state['accounts'])}/{target} "
                f"region={attempt_record['proxy_region']} tokens=False"
            )
        except Exception as error:
            attempt_record["error"] = f"{type(error).__name__}: {error}"[:500]
            print(f"attempt={attempt} success=False completed={len(state['accounts'])}/{target} error={type(error).__name__}")
        finally:
            attempt_record["elapsed_seconds"] = round(time.monotonic() - started, 1)
            attempt_record["finished_at"] = now_iso()
            state["attempts"].append(attempt_record)
            write_json(output_path, state)

    state["registration_finished_at"] = now_iso()
    state["registered"] = len(state["accounts"])
    write_json(output_path, state)
    print(f"result_file={output_path}")
    print(f"registered={len(state['accounts'])} attempts={len(state['attempts'])}")
    return output_path


def run_health_check(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    alive = 0
    dead = 0
    unknown = 0
    for index, account in enumerate(state.get("accounts") or [], 1):
        checks = account.setdefault("health_checks", [])
        previous = checks[-1] if checks and isinstance(checks[-1], dict) else {}
        terminal_check = next(
            (
                item
                for item in reversed(checks)
                if isinstance(item, dict)
                and (
                    item.get("alive") is False
                    or str(item.get("response_code") or "") in {"token_revoked", "oauth_refresh_invalid"}
                    or any(
                        marker in str(item.get("oauth_refresh_error") or "").lower()
                        for marker in ("invalid_grant", "refresh_token_invalidated", "session has ended")
                    )
                )
            ),
            None,
        )
        terminal = terminal_check is not None
        if terminal_check is not None:
            terminal_code = str(terminal_check.get("response_code") or "")
            if terminal_code not in {"token_revoked", "oauth_refresh_invalid"}:
                terminal_code = "oauth_refresh_invalid"
            check = {**terminal_check, "alive": False, "response_code": terminal_code}
        else:
            check = check_account(account, index, refresh_oauth=True)
        if not terminal:
            checks.append(check)
        if check.get("alive") is True:
            alive += 1
        elif check.get("alive") is False:
            dead += 1
            if not account.get("first_dead_at"):
                account["first_dead_at"] = check.get("checked_at")
                account["observed_lifetime_seconds"] = check.get("age_seconds")
        else:
            unknown += 1
        print(
            "account={} age_min={:.1f} alive={} status={} code={} same_egress={}".format(
                index,
                int(check.get("age_seconds") or 0) / 60,
                check.get("alive"),
                int(check.get("status_code") or 0),
                str(check.get("response_code") or ""),
                check.get("same_egress"),
            )
        )
    state["last_health_summary"] = {
        "checked_at": now_iso(),
        "total": alive + dead + unknown,
        "alive": alive,
        "dead": dead,
        "unknown": unknown,
    }
    write_json(path, state)
    print(f"summary total={alive + dead + unknown} alive={alive} dead={dead} unknown={unknown}")
    return state["last_health_summary"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run and monitor the original three-mode Free registration protocol")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--target", type=int, default=3)
    register_parser.add_argument("--max-attempts", type=int, default=6)
    register_parser.add_argument("--slots", type=int, default=2)

    register_v16_parser = subparsers.add_parser("register-v16")
    register_v16_parser.add_argument("--target", type=int, default=3)
    register_v16_parser.add_argument("--max-attempts", type=int, default=10)
    register_v16_parser.add_argument("--slots", type=int, default=2)

    register_early_free_parser = subparsers.add_parser("register-early-free")
    register_early_free_parser.add_argument("--target", type=int, default=3)
    register_early_free_parser.add_argument("--max-attempts", type=int, default=15)
    register_early_free_parser.add_argument("--slots", type=int, default=2)
    register_early_free_parser.add_argument("--mailbox-group", default="")

    register_current_free_parser = subparsers.add_parser("register-current-free")
    register_current_free_parser.add_argument("--target", type=int, default=3)
    register_current_free_parser.add_argument("--max-attempts", type=int, default=10)
    register_current_free_parser.add_argument("--slots", type=int, default=2)
    register_current_free_parser.add_argument("--mailbox-group", default="")

    register_current_partial_parser = subparsers.add_parser("register-current-partial")
    register_current_partial_parser.add_argument("--target", type=int, default=3)
    register_current_partial_parser.add_argument("--max-attempts", type=int, default=10)
    register_current_partial_parser.add_argument("--slots", type=int, default=2)
    register_current_partial_parser.add_argument("--mailbox-group", default="")
    register_current_partial_parser.add_argument("--token-delay-seconds", type=int, default=120)

    register_only_parser = subparsers.add_parser("register-only")
    register_only_parser.add_argument("--target", type=int, default=3)
    register_only_parser.add_argument("--max-attempts", type=int, default=8)
    register_only_parser.add_argument("--slots", type=int, default=2)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--file", default="")

    register_only_check_parser = subparsers.add_parser("check-register-only")
    register_only_check_parser.add_argument("--file", default="")

    args = parser.parse_args()
    if args.command in {"register", "register-v16", "register-early-free", "register-current-free", "register-current-partial", "register-only"}:
        target = max(1, int(args.target))
        if args.command == "register-only":
            register_only_batch(target, max(target, int(args.max_attempts)), max(1, int(args.slots)))
        elif args.command == "register-v16":
            register_batch(
                target,
                max(target, int(args.max_attempts)),
                max(1, int(args.slots)),
                source=V16_LEGACY_SOURCE,
                label="v16-platform-survival",
            )
        elif args.command == "register-early-free":
            register_batch(
                target,
                max(target, int(args.max_attempts)),
                max(1, int(args.slots)),
                label="early-free-app2sk-survival",
                early_free_oauth=True,
                mailbox_group=str(args.mailbox_group or ""),
                protocol_label="early_free_app2sk",
            )
        elif args.command == "register-current-free":
            register_batch(
                target,
                max(target, int(args.max_attempts)),
                max(1, int(args.slots)),
                source=CURRENT_FREE_SOURCE,
                label="current-free-appemo-survival",
                mailbox_group=str(args.mailbox_group or ""),
                protocol_label="current_free_appemo",
                named_aliases=True,
            )
        elif args.command == "register-current-partial":
            register_batch(
                target,
                max(target, int(args.max_attempts)),
                max(1, int(args.slots)),
                source=CURRENT_FREE_SOURCE,
                label="current-free-partial-survival",
                mailbox_group=str(args.mailbox_group or ""),
                protocol_label="current_free_partial",
                named_aliases=True,
                registration_method="free_wu",
                token_delay_seconds=max(0, int(args.token_delay_seconds)),
            )
        else:
            register_batch(target, max(target, int(args.max_attempts)), max(1, int(args.slots)))
        return
    if args.command == "check-register-only":
        path = Path(args.file).resolve() if args.file else latest_register_only_file()
        check_register_only_batch(path)
    else:
        path = Path(args.file).resolve() if args.file else latest_result_file()
        run_health_check(path)


if __name__ == "__main__":
    main()
