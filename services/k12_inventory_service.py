from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from curl_cffi import requests

from services import chatgpt_web_service, cpa_store, k12_config_store, k12_service, mailbox_pool_store, proxy_pool_store, sub2api_store
from services.atomic_json_store import read_json, write_json
from services.account_token_profile import SUB2API_OAUTH_CLIENT_ID, token_profile
from services.config import DATA_DIR
from services.proxy_service import proxy_settings
from services.registered_account_store import account_matches_selector, account_pool, list_registered_accounts, update_registered_account

K12_TASK_FILE = DATA_DIR / "k12_task.json"
AUTH_BASE = "https://auth.openai.com"
PLATFORM_BASE = "https://platform.openai.com"
PLATFORM_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_OAUTH_REDIRECT_URI = f"{PLATFORM_BASE}/auth/callback"
SUB2API_OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
PLATFORM_AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview(value: object, keep: int = 8) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}...{text[-keep:]}"


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


def _token_context(token: object) -> dict[str, Any]:
    payload = _jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    profile = payload.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    exp = int(payload.get("exp") or 0) if str(payload.get("exp") or "").isdigit() else 0
    return {
        "email": str(profile.get("email") or payload.get("email") or ""),
        "chatgpt_account_id": str(auth.get("chatgpt_account_id") or auth.get("account_id") or ""),
        "chatgpt_user_id": str(auth.get("user_id") or payload.get("sub") or ""),
        "plan_type": str(auth.get("chatgpt_plan_type") or profile.get("plan_type") or ""),
        "exp": exp or None,
        "expires_in": max(0, exp - int(time.time())) if exp else None,
        "preview": _preview(token),
    }


def _session(proxy: str = "") -> requests.Session:
    kwargs = proxy_settings.build_session_kwargs(proxy=proxy, upstream=True, impersonate="chrome", verify=proxy_settings.verify_tls())
    return requests.Session(**kwargs)


def _response_json(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"body": str(getattr(resp, "text", "") or "")[:800]}


def _refresh_platform_tokens(refresh_token: str, *, client_id: str = "", proxy: str = "") -> dict[str, Any]:
    refresh_token = str(refresh_token or "").strip()
    if not refresh_token:
        raise RuntimeError("missing refresh_token: cannot refresh OAuth access token")
    session = _session(proxy)
    resolved_client_id = str(client_id or PLATFORM_OAUTH_CLIENT_ID).strip()
    redirect_uri = SUB2API_OAUTH_REDIRECT_URI if resolved_client_id == SUB2API_OAUTH_CLIENT_ID else PLATFORM_OAUTH_REDIRECT_URI
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "auth0-client": PLATFORM_AUTH0_CLIENT,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": PLATFORM_BASE,
        "pragma": "no-cache",
        "referer": f"{PLATFORM_BASE}/",
        "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": USER_AGENT,
    }
    resp = session.post(
        f"{AUTH_BASE}/api/accounts/oauth/token",
        headers=headers,
        json={
            "client_id": resolved_client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "redirect_uri": redirect_uri,
        },
        verify=proxy_settings.verify_tls(),
        timeout=60,
    )
    data = _response_json(resp)
    if int(getattr(resp, "status_code", 0) or 0) != 200:
        raise RuntimeError(f"refresh OAuth token failed: HTTP {getattr(resp, 'status_code', 'unknown')}, {json.dumps(data, ensure_ascii=False)[:600]}")
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError(f"refresh OAuth token response missing access_token: {json.dumps(data, ensure_ascii=False)[:600]}")
    return {
        "access_token": access_token,
        "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
        "id_token": str(data.get("id_token") or "").strip(),
        "scope": str(data.get("scope") or ""),
        "expires_in": data.get("expires_in"),
        "token_type": str(data.get("token_type") or ""),
        "client_id": resolved_client_id,
        "refreshed_at": _now(),
    }


def _default_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "options": {
            "emails": [],
            "only_unprocessed": False,
            "join_k12": True,
            "import_to_sub2api": True,
            "import_to_cpa": False,
            "health_check": True,
        },
        "stats": {
            "success": 0,
            "fail": 0,
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


def _public_account_summary() -> dict[str, Any]:
    pool = account_pool()
    accounts = pool.get("accounts") if isinstance(pool, dict) else []
    slim_accounts: list[dict[str, Any]] = []
    if isinstance(accounts, list):
        for index, account in enumerate(accounts, start=1):
            if isinstance(account, dict):
                slim_accounts.append({
                    key: account.get(key)
                    for key in (
                        "inventory_id", "inventory_key", "email", "status", "account_type", "plan_type",
                        "chatgpt_account_id", "workspace_id", "k12_team_id", "k12_join_status",
                        "sub2api_account_id", "sub2api_import_status", "sub2api_health_status",
                        "cpa_auth_file", "cpa_import_status", "cpa_health_status",
                        "health_status",
                    )
                    if account.get(key) not in (None, "")
                })
    pool["accounts"] = slim_accounts
    return pool


class K12InventoryService:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._state = self._load()
        if self._state.get("enabled"):
            self._state["enabled"] = False
            self._save()

    def _load(self) -> dict[str, Any]:
        data = read_json(self._store_file, {}, expected_schema_version=1)
        if isinstance(data, dict):
            state = _default_state()
            state.update({k: v for k, v in data.items() if k in state})
            state["stats"] = {**_default_state()["stats"], **(data.get("stats") if isinstance(data.get("stats"), dict) else {})}
            state["options"] = {**_default_state()["options"], **(data.get("options") if isinstance(data.get("options"), dict) else {})}
            state["logs"] = data.get("logs") if isinstance(data.get("logs"), list) else []
            state["results"] = data.get("results") if isinstance(data.get("results"), list) else []
            return state
        return _default_state()

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._store_file, self._state, schema_version=1)

    def get(self) -> dict[str, Any]:
        with self._lock:
            snap = json.loads(json.dumps(self._state, ensure_ascii=False))
            runner_alive = bool(self._runner and self._runner.is_alive())
        snap["enabled"] = bool(snap.get("enabled")) and runner_alive
        snap["account_pool"] = _public_account_summary()
        snap["config"] = k12_config_store.get_k12_config()
        snap["sub2api_config"] = sub2api_store.get_sub2api_config()
        snap["cpa_config"] = cpa_store.get_cpa_config()
        return snap

    def _append_log(self, text: str, level: str = "info") -> None:
        with self._lock:
            self._state.setdefault("logs", []).append({"time": _now(), "text": str(text), "level": str(level or "info")})
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
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            stats["updated_at"] = _now()
            self._save()

    def start(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._state["enabled"] = True
                self._save()
                return self.get()
            merged = {**_default_state()["options"], **options}
            emails = [str(item).strip() for item in (merged.get("emails") or []) if str(item).strip()]
            merged["emails"] = emails
            self._state = _default_state()
            self._state["enabled"] = True
            self._state["options"] = merged
            self._state["stats"].update({"job_id": uuid.uuid4().hex, "started_at": _now(), "updated_at": _now()})
            self._save()
            self._runner = threading.Thread(target=self._run, daemon=True, name="k12-inventory")
            self._runner.start()
        self._append_log("K12 inventory task started", "yellow")
        return self.get()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._state["enabled"] = False
            self._save()
        self._append_log("Stop requested for K12 inventory task", "yellow")
        return self.get()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._runner and self._runner.is_alive())
            if running:
                self._state["enabled"] = False
            else:
                self._state = _default_state()
            self._save()
        return self.get()

    def _selected_accounts(self, options: dict[str, Any]) -> list[dict[str, Any]]:
        selected = {str(item).strip().lower() for item in (options.get("emails") or []) if str(item).strip()}
        accounts = list_registered_accounts(include_secret=True)
        if selected:
            accounts = [item for item in accounts if any(account_matches_selector(item, selector) for selector in selected)]
        if bool(options.get("only_unprocessed")):
            accounts = [item for item in accounts if str(item.get("k12_join_status") or "") not in {"accepted_attempted", "request_sent", "invite_received"}]
        return accounts

    def _run(self) -> None:
        with self._lock:
            options = dict(self._state.get("options") or {})
        accounts = self._selected_accounts(options)
        self._bump(total=len(accounts), running=0)
        if not accounts:
            self._append_log("No inventory accounts to process", "yellow")
        success = 0
        fail = 0
        done = 0
        for index, account in enumerate(accounts):
            with self._lock:
                if not self._state.get("enabled"):
                    break
            email = str(account.get("email") or "").strip()
            self._bump(running=1)
            self._append_log(f"Start processing {email}")
            try:
                assignment = proxy_pool_store.assign_proxy(index, total_threads=1, slot_index=0, purpose="k12_inventory", account=account, validate_node=False)
                result = self._process_account(account, options, proxy=str(assignment.get("proxy") or ""))
                result["proxy_source"] = assignment.get("source") or ""
                result["proxy_label"] = assignment.get("label") or ""
                ok = bool(result.get("ok"))
                if ok:
                    success += 1
                    self._append_log(f"{email} K12/import/health completed", "green")
                else:
                    fail += 1
                    self._append_log(f"{email} processing incomplete: {result.get('error') or result.get('status')}", "red")
                with self._lock:
                    self._state.setdefault("results", []).append(result)
                    self._state["results"] = self._state["results"][-200:]
                    self._save()
            except Exception as error:
                fail += 1
                result = {"email": email, "ok": False, "error": str(error), "finished_at": _now()}
                update_registered_account(account, {"k12_last_error": str(error)[:500], "k12_last_attempt_at": _now()})
                with self._lock:
                    self._state.setdefault("results", []).append(result)
                    self._state["results"] = self._state["results"][-200:]
                    self._save()
                self._append_log(f"{email} processing failed: {error}", "red")
            finally:
                done += 1
                self._bump(done=done, success=success, fail=fail, running=0)
        self._bump(running=0, done=done, success=success, fail=fail, finished_at=_now())
        with self._lock:
            self._state["enabled"] = False
            self._save()
        self._append_log(f"K12 inventory task finished: success={success}, fail={fail}", "yellow")

    def _process_account(self, account: dict[str, Any], options: dict[str, Any], *, proxy: str = "") -> dict[str, Any]:
        email = str(account.get("email") or "").strip()
        if not email:
            raise RuntimeError("account email is missing")
        selector = str(account.get("inventory_id") or account.get("inventory_key") or email)
        started_at = _now()
        result: dict[str, Any] = {"email": email, "ok": False, "started_at": started_at, "steps": []}
        preserve_sub2api_oauth = bool(token_profile(account).get("oauth_capable"))

        cfg = k12_config_store.get_k12_config()
        wait_timeout = max(120, int(cfg.get("invite_timeout") or 180))
        team_id = k12_config_store.choose_k12_team_id(email, cfg)
        mailbox = mailbox_pool_store.find_mailbox_for_address(email)
        if not mailbox:
            raise RuntimeError("mailbox pool has no matching inbox for this account; cannot login or receive K12 invite")
        mail_config = mailbox_pool_store.mail_config_from_pool(proxy=proxy, wait_timeout=wait_timeout)

        # 1) 优先复用仍有效的 K12 Web AT；没有可用 Web/K12 AT 时再重登录。
        web_client: chatgpt_web_service.ChatGPTWebClient | None = None
        access_token = str(account.get("access_token") or "").strip()
        web_context = chatgpt_web_service.token_context(access_token)
        can_reuse_web_at = (
            str(account.get("token_source") or "") == "chatgpt_web"
            and int(web_context.get("expires_in") or 0) > 300
            and (not team_id or str(web_context.get("chatgpt_account_id") or "").lower() == str(team_id).lower())
        )
        if can_reuse_web_at:
            result["token_context"] = web_context
            result["steps"].append({"name": "reuse_chatgpt_web_at", "ok": True, "at": _now(), "context": web_context})
            self._append_log(f"{email} reused valid K12 Web AT, context={web_context.get('chatgpt_account_id') or 'personal/free'}")
        else:
            web_client = chatgpt_web_service.ChatGPTWebClient(
                email=email,
                password=str(account.get("password") or ""),
                proxy=proxy,
                mailbox=mailbox,
                mail_config=mail_config,
                log=lambda text, level="info": self._append_log(f"{email} {text}", level or "info"),
            )
            web_login = web_client.login()
            access_token = str(web_login.get("access_token") or "")
            web_context = web_login.get("token_context") if isinstance(web_login.get("token_context"), dict) else chatgpt_web_service.token_context(access_token)
            token_updates = {
                "web_login_at": web_login.get("login_at") or _now(),
                "last_web_client_id": str(web_context.get("client_id") or chatgpt_web_service.CHATGPT_WEB_CLIENT_ID),
                "last_web_chatgpt_account_id": str(web_context.get("chatgpt_account_id") or ""),
            }
            if not preserve_sub2api_oauth:
                token_updates.update({
                    "access_token": access_token,
                    "token_source": "chatgpt_web",
                    "chatgpt_account_id": str(web_context.get("chatgpt_account_id") or ""),
                    "chatgpt_user_id": str(web_context.get("chatgpt_user_id") or ""),
                    "plan_type": str(web_context.get("plan_type") or ""),
                    "client_id": str(web_context.get("client_id") or chatgpt_web_service.CHATGPT_WEB_CLIENT_ID),
                    "oauth_status": "chatgpt_web_login",
                })
            update_registered_account(account, token_updates)
            result["token_context"] = web_context
            result["steps"].append({"name": "chatgpt_web_login", "ok": True, "at": _now(), "access_token_preview": _preview(access_token), "context": web_context})
            self._append_log(f"{email} got ChatGPT Web AT, context={web_context.get('chatgpt_account_id') or 'personal/free'}")

        # 2) K12 ??/??
        k12_result: dict[str, Any] = {"status": "skipped"}
        if bool(options.get("join_k12", True)):
            if can_reuse_web_at and team_id:
                k12_result = {
                    "enabled": True,
                    "team_id": team_id,
                    "workspace_id": team_id,
                    "route": "reuse",
                    "requested_at": _now(),
                    "status": "already_k12_token",
                }
            else:
                k12_result = k12_service.request_k12_join(
                    session=web_client.session if web_client is not None else _session(proxy),
                    access_token=access_token,
                    email=email,
                    mailbox=mailbox,
                    mail_config=mail_config,
                    index=0,
                    user_agent=USER_AGENT,
                    log_step=lambda _index, text, level="": self._append_log(f"{email} {text}", level or "info"),
                    force_enabled=True,
                )
            update_registered_account(account, {
                "k12_mode": True,
                "k12_team_id": str(k12_result.get("team_id") or team_id or ""),
                "workspace_id": str(k12_result.get("workspace_id") or k12_result.get("team_id") or team_id or ""),
                "k12_join_status": str(k12_result.get("status") or ""),
                "k12_join_result": k12_result,
                "k12_last_attempt_at": _now(),
            })
            result["steps"].append({"name": "join_k12", "ok": str(k12_result.get("status") or "") not in {"request_failed", "error"}, "status": k12_result.get("status"), "team_id": k12_result.get("team_id") or team_id})
            self._append_log(f"{email} K12 status={k12_result.get('status')} team={k12_result.get('team_id') or team_id}")
        else:
            update_registered_account(account, {"k12_team_id": team_id, "workspace_id": team_id})
        result["k12"] = {k: v for k, v in k12_result.items() if k not in {"invite_url"}}
        if bool(options.get("join_k12", True)) and str(k12_result.get("status") or "") not in {"request_failed", "error", "skipped_missing_access_token"}:
            try:
                workspace_id = str(k12_result.get("workspace_id") or k12_result.get("team_id") or team_id or "").strip()
                if chatgpt_web_service.is_token_for_workspace(access_token, workspace_id):
                    switched_context = chatgpt_web_service.token_context(access_token)
                else:
                    if web_client is None:
                        raise RuntimeError("current Web AT is not K12 and no active Web session is available for workspace switch")
                    switched = web_client.switch_workspace_access_token(workspace_id, access_token)
                    access_token = str(switched.get("access_token") or access_token)
                    switched_context = switched.get("token_context") if isinstance(switched.get("token_context"), dict) else chatgpt_web_service.token_context(access_token)
                token_updates_after = {
                    "web_workspace_switched_at": _now(),
                    "last_web_client_id": str(switched_context.get("client_id") or chatgpt_web_service.CHATGPT_WEB_CLIENT_ID),
                    "last_web_chatgpt_account_id": str(switched_context.get("chatgpt_account_id") or ""),
                }
                if not preserve_sub2api_oauth:
                    token_updates_after.update({
                        "access_token": access_token,
                        "token_source": "chatgpt_web",
                        "chatgpt_account_id": str(switched_context.get("chatgpt_account_id") or ""),
                        "chatgpt_user_id": str(switched_context.get("chatgpt_user_id") or ""),
                        "plan_type": str(switched_context.get("plan_type") or ""),
                        "client_id": str(switched_context.get("client_id") or chatgpt_web_service.CHATGPT_WEB_CLIENT_ID),
                        "oauth_status": "chatgpt_web_k12",
                    })
                update_registered_account(account, token_updates_after)
                result["token_context_after_k12"] = switched_context
                result["steps"].append({"name": "switch_k12_workspace", "ok": str(switched_context.get("chatgpt_account_id") or "").lower() == workspace_id.lower(), "context": switched_context})
                self._append_log(f"{email} K12 Web AT context={switched_context.get('chatgpt_account_id') or 'personal/free'}")
            except Exception as error:
                result["steps"].append({"name": "switch_k12_workspace", "ok": False, "error": str(error)})
                self._append_log(f"{email} switch K12 workspace failed: {error}", "yellow")
            workspace_id = str(k12_result.get("workspace_id") or k12_result.get("team_id") or team_id or "").strip()
            if workspace_id and not chatgpt_web_service.is_token_for_workspace(access_token, workspace_id):
                context = chatgpt_web_service.token_context(access_token)
                raise RuntimeError(
                    "K12 workspace AT not ready; blocked sub2api import to avoid unusable account: "
                    f"expected={workspace_id}, actual={context.get('chatgpt_account_id') or 'personal/free'}, plan={context.get('plan_type') or '?'}"
                )

        # 3) ?? sub2api
        import_result: dict[str, Any] | None = None
        account_ids: list[int] = []
        try:
            existing_id = int(account.get("sub2api_account_id") or 0)
        except Exception:
            existing_id = 0
        if bool(options.get("import_to_sub2api", True)) and existing_id > 0:
            update_result = sub2api_store.update_account_in_sub2api(selector, existing_id)
            if not update_result.get("ok"):
                if int(update_result.get("status_code") or 0) == 404:
                    self._append_log(f"{email} existing sub2api id={existing_id} not found; re-importing", "yellow")
                    update_registered_account(account, {"sub2api_account_id": "", "sub2api_update_status": "missing_reimport"})
                    existing_id = 0
                else:
                    raise RuntimeError(f"sub2api update failed: HTTP {update_result.get('status_code')}, {json.dumps(update_result.get('response') or {}, ensure_ascii=False)[:600]}")
            else:
                account_ids = [existing_id]
                import_result = {"ok": True, "skipped": "existing_sub2api_account_id", "account_ids": account_ids, "update": update_result}
                result["steps"].append({"name": "import_sub2api", "ok": True, "ids": account_ids, "skipped": "existing", "updated": True})
                self._append_log(f"{email} updated existing sub2api id={existing_id}")
        if existing_id <= 0 and bool(options.get("import_to_sub2api", True)):
            try:
                import_result = sub2api_store.import_accounts_to_sub2api([selector])
                if not import_result.get("ok"):
                    raise RuntimeError(f"sub2api import failed: HTTP {import_result.get('status_code')}, {json.dumps(import_result.get('response') or {}, ensure_ascii=False)[:600]}")
                account_ids = sub2api_store.extract_sub2api_account_ids(import_result, [email])
                if not account_ids:
                    found = sub2api_store.find_sub2api_account_ids([email])
                    account_ids = [int(item) for item in (found.get("found", {}).get(email) or [])]
                update_registered_account(account, {
                    "sub2api_import_status": "ok" if import_result.get("ok") else "failed",
                    "sub2api_account_id": account_ids[0] if account_ids else account.get("sub2api_account_id") or "",
                })
                result["steps"].append({"name": "import_sub2api", "ok": bool(import_result.get("ok")), "ids": account_ids, "status_code": import_result.get("status_code")})
                self._append_log(f"{email} sub2api import returned ids={account_ids or '-'}")
            except Exception as error:
                update_registered_account(account, {"sub2api_import_status": "failed", "sub2api_last_error": str(error)[:500]})
                result["steps"].append({"name": "import_sub2api", "ok": False, "error": str(error)})
                raise
        result["sub2api_import"] = import_result
        result["sub2api_account_ids"] = account_ids

        # 4) ??
        test_result: dict[str, Any] | None = None
        if bool(options.get("health_check", True)) and bool(options.get("import_to_sub2api", True)):
            if not account_ids:
                raise RuntimeError("sub2api import did not return account id; cannot health check")
            test_result = sub2api_store.test_sub2api_account(account_ids[0])
            update_registered_account(account, {
                "sub2api_health_status": "ok" if test_result.get("ok") else "failed",
                "sub2api_health_checked_at": test_result.get("tested_at") or _now(),
                "sub2api_health_code": test_result.get("status_code"),
            })
            result["steps"].append({"name": "test_sub2api", "ok": bool(test_result.get("ok")), "status_code": test_result.get("status_code")})
            self._append_log(f"{email} sub2api health={'ok' if test_result.get('ok') else 'failed'} HTTP {test_result.get('status_code')}", "green" if test_result.get("ok") else "yellow")
        result["sub2api_test"] = test_result

        cpa_import: dict[str, Any] | None = None
        cpa_test: dict[str, Any] | None = None
        if bool(options.get("import_to_cpa", False)):
            try:
                cpa_import = cpa_store.import_accounts_to_cpa([selector])
                first = next((item for item in cpa_import.get("results") or [] if isinstance(item, dict)), {})
                filename = str(first.get("filename") or account.get("cpa_auth_file") or "")
                result["steps"].append({"name": "import_cpa", "ok": bool(cpa_import.get("ok")), "filename": filename})
                self._append_log(f"{email} CPA import file={filename or '-'}", "green" if cpa_import.get("ok") else "yellow")
                if bool(options.get("health_check", True)) and filename:
                    cpa_test = cpa_store.test_cpa_account(filename)
                    update_registered_account(account, {
                        "cpa_health_status": str(cpa_test.get("status") or ("ok" if cpa_test.get("ok") else "failed")),
                        "cpa_health_checked_at": cpa_test.get("tested_at") or _now(),
                        "cpa_health_code": cpa_test.get("status_code"),
                    })
                    result["steps"].append({"name": "test_cpa", "ok": bool(cpa_test.get("ok")), "status_code": cpa_test.get("status_code")})
                    self._append_log(f"{email} CPA health={'ok' if cpa_test.get('ok') else 'failed'} HTTP {cpa_test.get('status_code')}", "green" if cpa_test.get("ok") else "yellow")
            except Exception as error:
                update_registered_account(account, {"cpa_import_status": "failed", "cpa_last_error": str(error)[:500]})
                result["steps"].append({"name": "import_cpa", "ok": False, "error": str(error)})
                raise
        result["cpa_import"] = cpa_import
        result["cpa_test"] = cpa_test
        sub_ok = not bool(options.get("import_to_sub2api", True)) or not bool(options.get("health_check", True)) or bool(test_result and test_result.get("ok"))
        cpa_ok = not bool(options.get("import_to_cpa", False)) or not bool(options.get("health_check", True)) or bool(cpa_test and cpa_test.get("ok"))
        result["ok"] = bool(sub_ok and cpa_ok)
        result["finished_at"] = _now()
        return result


k12_inventory_service = K12InventoryService(K12_TASK_FILE)
