from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services import managed_mihomo_service, partial_account_store, proxy_pool_store
from services.account_inventory_service import _local_health_and_quota, _retryable_proxy_result, _sub2api_health
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR


PARTIAL_HEALTH_SETTINGS_FILE = DATA_DIR / "partial_account_health_settings.json"
_check_lock = threading.Lock()
UNVERIFIABLE_HEALTH_STATUSES = {
    "browser_check_required",
    "missing_registration_proxy",
    "missing_sub2api_id",
    "missing_token",
    "skipped",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_schedule_settings() -> dict[str, Any]:
    return {
        "scheduled_enabled": False,
        "interval_minutes": 60,
        "refresh_quota": False,
        "method": "auto",
        "concurrency": 8,
        "updated_at": "",
    }


def _normalize_schedule_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    settings = _default_schedule_settings()
    if isinstance(raw, dict):
        settings.update({key: value for key, value in raw.items() if key in settings})
    settings["scheduled_enabled"] = bool(settings.get("scheduled_enabled"))
    settings["interval_minutes"] = max(1, int(settings.get("interval_minutes") or 60))
    settings["refresh_quota"] = bool(settings.get("refresh_quota"))
    settings["method"] = str(settings.get("method") or "auto").strip().lower()
    if settings["method"] not in {"auto", "local", "sub2api"}:
        settings["method"] = "auto"
    settings["concurrency"] = max(1, min(32, int(settings.get("concurrency") or 8)))
    settings["updated_at"] = str(settings.get("updated_at") or "")
    return settings


def _selector(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("email") or "").strip()


def _selected_accounts(selectors: list[str] | None) -> list[dict[str, Any]]:
    accounts = partial_account_store.list_partial_accounts(include_secret=True)
    wanted = {str(value or "").strip().lower() for value in (selectors or []) if str(value or "").strip()}
    if not wanted:
        return accounts
    return [
        account
        for account in accounts
        if wanted & {str(account.get("id") or "").lower(), str(account.get("email") or "").lower()}
    ]


def _run_one(
    index: int,
    account: dict[str, Any],
    *,
    total: int,
    concurrency: int,
    refresh_quota: bool,
    mode: str = "",
    method: str = "auto",
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    assignment: dict[str, Any] = {}
    proxy_context = dict(account)
    method = str(method or "auto").strip().lower()
    if method not in {"auto", "local", "sub2api"}:
        method = "auto"

    if method == "sub2api" and not account.get("sub2api_account_id"):
        result = {
            "ok": False,
            "quota_ok": False,
            "method": "sub2api",
            "status": "missing_sub2api_id",
            "status_code": 0,
            "error": "账号没有 sub2api_account_id",
            "updates": {
                "health_status": "missing_sub2api_id",
                "health_error": "账号没有 sub2api_account_id",
                "health_checked_at": _now(),
                "health_method": "sub2api",
            },
        }
    elif method in {"auto", "sub2api"} and account.get("sub2api_account_id") and not (method == "auto" and refresh_quota):
        try:
            result = _sub2api_health(account, mode=mode)
        except Exception as error:
            result = {
                "ok": False,
                "quota_ok": False,
                "method": "sub2api",
                "status": "failed",
                "status_code": 0,
                "error": str(error)[:800],
                "updates": {
                    "health_status": "failed",
                    "health_error": str(error)[:800],
                    "health_checked_at": _now(),
                    "health_method": "sub2api",
                },
            }
    elif method in {"auto", "local"} and not any(
        str(account.get(key) or "").strip() for key in ("access_token", "refresh_token")
    ):
        browser_only = str(account.get("source") or "").strip().lower() == "browser_ui_chatgpt"
        status = "browser_check_required" if browser_only else "missing_token"
        error = "浏览器注册账号未保存 Token，需要使用原浏览器会话测活" if browser_only else "缺少 access_token 和 refresh_token，无法执行本地 Token 测活"
        result = {
            "ok": False,
            "quota_ok": False,
            "method": "browser" if browser_only else "local",
            "status": status,
            "status_code": 0,
            "error": error,
            "updates": {
                "health_status": status,
                "health_error": error,
                "health_checked_at": _now(),
                "health_method": "browser_required" if browser_only else "local",
            },
        }

    if result:
        result_method = str(result.get("method") or "").strip().lower()
        assignment = {
            "source": "sub2api" if result_method == "sub2api" else "none",
            "label": "sub2api" if result_method == "sub2api" else "",
            "node_id": "",
            "sticky": False,
        }

    for attempt in range(1, 4) if not result else []:
        assignment = proxy_pool_store.assign_proxy(
            index + (attempt - 1) * max(1, total),
            total_threads=concurrency,
            slot_index=(index - 1) % concurrency,
            purpose="partial_account_health",
            account=proxy_context,
            validate_node=False,
        )
        if assignment.get("sticky_failure"):
            error = str(assignment.get("error") or "注册代理节点不可用")
            status = "missing_registration_proxy" if assignment.get("legacy_proxy") else "proxy_error"
            result = {
                "ok": False,
                "quota_ok": False,
                "method": "local",
                "status": status,
                "status_code": 0,
                "error": error[:800],
                "updates": {
                    "health_status": status,
                    "health_error": error[:800],
                    "health_checked_at": datetime.now(timezone.utc).isoformat(),
                    "health_method": "local",
                },
            }
        else:
            try:
                result = _local_health_and_quota(
                    account,
                    refresh_quota=refresh_quota,
                    proxy=str(assignment.get("proxy") or ""),
                    force_refresh_token=str(mode or "").strip().lower() in {"refresh_token", "force_refresh_token", "refresh_type"},
                )
            except Exception as error:
                result = {
                    "ok": False,
                    "quota_ok": False,
                    "method": "local",
                    "status": "network_error",
                    "status_code": 0,
                    "error": str(error)[:800],
                    "updates": {
                        "health_status": "network_error",
                        "health_error": str(error)[:800],
                        "health_checked_at": datetime.now(timezone.utc).isoformat(),
                        "health_method": "local",
                    },
                }
        if assignment.get("sticky_failure") or attempt >= 3 or not _retryable_proxy_result(result):
            break
        if assignment.get("sticky"):
            continue
        if str(assignment.get("source") or "") != "managed_mihomo":
            break
        reason = result.get("error") or result.get("status") or "partial account health failed"
        try:
            proxy_pool_store.report_proxy_failure(assignment, reason)
        except Exception:
            pass

    updates = dict(result.get("updates") or {})
    updates.pop("status", None)
    attempt_checked_at = str(updates.get("health_checked_at") or _now())
    attempt_status = str(result.get("status") or updates.get("health_status") or "unknown").strip().lower()
    updates["last_health_attempt_status"] = attempt_status
    updates["last_health_attempt_code"] = int(result.get("status_code") or updates.get("health_code") or 0)
    updates["last_health_attempt_error"] = str(result.get("error") or updates.get("health_error") or "")[:800]
    updates["last_health_attempt_at"] = attempt_checked_at

    previous_status = str(account.get("health_status") or "unknown").strip().lower()
    conclusive_statuses = partial_account_store.ALIVE_HEALTH_STATUSES | partial_account_store.TERMINAL_HEALTH_STATUSES
    if attempt_status not in conclusive_statuses and previous_status in conclusive_statuses:
        updates["health_status"] = previous_status
        updates["health_checked_at"] = str(account.get("health_checked_at") or "")
        updates["health_method"] = str(account.get("health_method") or "")
        updates["health_code"] = account.get("health_code") if account.get("health_code") is not None else 0
        updates["health_latency_ms"] = account.get("health_latency_ms") if account.get("health_latency_ms") is not None else 0
        updates["health_error"] = str(account.get("health_error") or "")
        health_status = previous_status
        checked_at = str(account.get("health_checked_at") or attempt_checked_at)
    else:
        health_status = attempt_status
        checked_at = attempt_checked_at
    updates["registered_at"] = str(account.get("registered_at") or account.get("created_at") or checked_at)
    if health_status in partial_account_store.ALIVE_HEALTH_STATUSES:
        updates["died_at"] = ""
    elif health_status in partial_account_store.TERMINAL_HEALTH_STATUSES:
        updates["died_at"] = str(account.get("died_at") or checked_at)
    updates["health_proxy_source"] = str(assignment.get("source") or "")
    updates["health_proxy_label"] = str(assignment.get("label") or "")
    updates["health_proxy_node_id"] = str(assignment.get("node_id") or "")
    updates["health_proxy_sticky"] = bool(assignment.get("sticky"))
    partial_account_store.update_partial_account(_selector(account), updates)
    public_result = {key: value for key, value in result.items() if key != "updates"}
    public_result.update(
        {
            "id": str(account.get("id") or ""),
            "email": str(account.get("email") or ""),
            "proxy_source": str(assignment.get("source") or ""),
            "proxy_label": str(assignment.get("label") or ""),
        }
    )
    return public_result


def _check_partial_accounts_unlocked(
    selectors: list[str] | None = None,
    *,
    refresh_quota: bool = False,
    concurrency: int = 8,
    mode: str = "",
    method: str = "auto",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    accounts = _selected_accounts(selectors)
    concurrency = max(1, min(32, int(concurrency or 8)))
    proxy_store = proxy_pool_store.get_proxy_pool_config(include_secret=True)
    managed = proxy_store.get("managed_mihomo") if isinstance(proxy_store.get("managed_mihomo"), dict) else {}
    if (
        accounts
        and bool(proxy_store.get("enabled"))
        and str(proxy_store.get("node_mode") or "").lower() == "managed_mihomo"
        and bool(managed.get("enabled", True))
    ):
        try:
            managed_mihomo_service.managed_mihomo_manager.start(min(concurrency, len(accounts)), proxy_store)
        except Exception:
            pass
    results: list[dict[str, Any]] = []
    if accounts:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(accounts))) as executor:
            futures = {
                executor.submit(
                    _run_one,
                    index,
                    account,
                    total=len(accounts),
                    concurrency=concurrency,
                    refresh_quota=bool(refresh_quota),
                    mode=mode,
                    method=method,
                ): account
                for index, account in enumerate(accounts, start=1)
            }
            for future in as_completed(futures):
                if stop_event and stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                account = futures[future]
                try:
                    results.append(future.result())
                except Exception as error:
                    failure = {
                        "ok": False,
                        "status": "failed",
                        "status_code": 0,
                        "error": str(error)[:800],
                        "id": str(account.get("id") or ""),
                        "email": str(account.get("email") or ""),
                    }
                    partial_account_store.update_partial_account(
                        _selector(account),
                        {
                            "health_status": "failed",
                            "health_error": failure["error"],
                            "health_checked_at": datetime.now(timezone.utc).isoformat(),
                            "health_method": "local",
                            "last_health_result": failure,
                        },
                    )
                    results.append(failure)

    results.sort(key=lambda item: str(item.get("email") or "").lower())
    alive = sum(1 for item in results if bool(item.get("ok")))
    quota_ok = sum(1 for item in results if bool(item.get("quota_ok")))
    dead = sum(
        1
        for item in results
        if str(item.get("status") or "").lower() in partial_account_store.TERMINAL_HEALTH_STATUSES
    )
    unverifiable = sum(
        1
        for item in results
        if str(item.get("status") or "").lower() in UNVERIFIABLE_HEALTH_STATUSES
    )
    return {
        "ok": True,
        "stopped": bool(stop_event and stop_event.is_set()),
        "stats": {
            "total": len(results),
            "alive": alive,
            "dead": dead,
            "unverifiable": unverifiable,
            "failed": max(0, len(results) - alive - dead - unverifiable),
            "quota_ok": quota_ok,
            "quota_failed": max(0, len(results) - quota_ok),
        },
        "results": results,
        "partial_account_pool": partial_account_store.partial_account_pool(include_secret=True),
    }


def check_partial_accounts(
    selectors: list[str] | None = None,
    *,
    refresh_quota: bool = False,
    concurrency: int = 8,
    mode: str = "",
    method: str = "auto",
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    with _check_lock:
        return _check_partial_accounts_unlocked(
            selectors,
            refresh_quota=refresh_quota,
            concurrency=concurrency,
            mode=mode,
            method=method,
            stop_event=stop_event,
        )


class PartialAccountHealthScheduler:
    def __init__(self, settings_file: Path = PARTIAL_HEALTH_SETTINGS_FILE) -> None:
        self._settings_file = settings_file
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._schedule_running = False
        self._last_scheduled_run_at = ""
        self._next_scheduled_run_at = ""
        self._last_scheduled_stats: dict[str, Any] = {}
        self._last_error = ""
        self._task: threading.Thread | None = None
        self._task_stop = threading.Event()
        self._task_enabled = False
        self._task_started_at = ""
        self._task_finished_at = ""
        self._task_stats: dict[str, Any] = {}
        self._task_error = ""
        self._settings = self._load_settings()

    def _load_settings(self) -> dict[str, Any]:
        raw = read_json(self._settings_file, {}, expected_schema_version=1)
        return _normalize_schedule_settings(raw if isinstance(raw, dict) else {})

    def _save_settings(self) -> None:
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._settings_file, self._settings, schema_version=1)

    def _ensure_scheduler(self) -> None:
        with self._lock:
            if not self._settings.get("scheduled_enabled"):
                return
            if self._scheduler and self._scheduler.is_alive():
                return
            self._wake.clear()
            self._scheduler = threading.Thread(
                target=self._schedule_loop,
                daemon=True,
                name="partial-account-health-scheduler",
            )
            self._scheduler.start()

    def get_state(self) -> dict[str, Any]:
        self._ensure_scheduler()
        with self._lock:
            scheduler_alive = bool(self._scheduler and self._scheduler.is_alive())
            return {
                "settings": dict(self._settings),
                "schedule_running": scheduler_alive and self._schedule_running,
                "last_scheduled_run_at": self._last_scheduled_run_at,
                "next_scheduled_run_at": self._next_scheduled_run_at,
                "last_scheduled_stats": dict(self._last_scheduled_stats),
                "last_error": self._last_error,
                "task": {
                    "enabled": self._task_enabled,
                    "running": bool(self._task and self._task.is_alive()),
                    "stop_requested": self._task_stop.is_set() and self._task_enabled,
                    "started_at": self._task_started_at,
                    "finished_at": self._task_finished_at,
                    "stats": dict(self._task_stats),
                    "error": self._task_error,
                },
            }

    def start_task(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._task and self._task.is_alive():
                state = self.get_state()
                state["busy"] = True
                state["message"] = "已有半成品测活任务正在运行"
                return state
            task_options = dict(options or {})
            self._task_stop.clear()
            self._task_enabled = True
            self._task_started_at = _now()
            self._task_finished_at = ""
            self._task_stats = {}
            self._task_error = ""
            self._task = threading.Thread(
                target=self._run_task,
                args=(task_options,),
                daemon=True,
                name="partial-account-health-task",
            )
            self._task.start()
        return self.get_state()

    def stop_task(self) -> dict[str, Any]:
        with self._lock:
            self._task_stop.set()
        return self.get_state()

    def _run_task(self, options: dict[str, Any]) -> None:
        try:
            result = check_partial_accounts(
                list(options.get("selectors") or []),
                refresh_quota=bool(options.get("refresh_quota", True)),
                concurrency=int(options.get("concurrency") or 8),
                mode=str(options.get("mode") or ""),
                method=str(options.get("method") or "auto"),
                stop_event=self._task_stop,
            )
            with self._lock:
                self._task_stats = dict(result.get("stats") or {})
                self._task_error = ""
        except Exception as error:
            with self._lock:
                self._task_error = str(error)[:800]
        finally:
            with self._lock:
                self._task_enabled = False
                self._task_finished_at = _now()

    def update_settings(self, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._settings = _normalize_schedule_settings(
                {**self._settings, **(updates or {}), "updated_at": _now()}
            )
            self._save_settings()
            enabled = bool(self._settings.get("scheduled_enabled"))
            self._wake.set()
        if enabled:
            self._ensure_scheduler()
        return self.get_state()

    def start_schedule(self, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.update_settings({**(updates or {}), "scheduled_enabled": True})

    def stop_schedule(self) -> dict[str, Any]:
        return self.update_settings({"scheduled_enabled": False})

    def _schedule_loop(self) -> None:
        with self._lock:
            self._schedule_running = True
        try:
            while True:
                with self._lock:
                    settings = dict(self._settings)
                if not settings.get("scheduled_enabled"):
                    return
                interval_minutes = max(1, int(settings.get("interval_minutes") or 60))
                with self._lock:
                    self._next_scheduled_run_at = (
                        datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)
                    ).isoformat()
                if self._wake.wait(interval_minutes * 60):
                    self._wake.clear()
                    continue
                try:
                    result = check_partial_accounts(
                        refresh_quota=bool(settings.get("refresh_quota")),
                        concurrency=int(settings.get("concurrency") or 8),
                        method=str(settings.get("method") or "auto"),
                    )
                    with self._lock:
                        self._last_scheduled_run_at = _now()
                        self._last_scheduled_stats = dict(result.get("stats") or {})
                        self._last_error = ""
                except Exception as error:
                    with self._lock:
                        self._last_scheduled_run_at = _now()
                        self._last_scheduled_stats = {}
                        self._last_error = str(error)[:800]
        finally:
            with self._lock:
                self._schedule_running = False
                self._next_scheduled_run_at = ""


partial_account_health_scheduler = PartialAccountHealthScheduler()
