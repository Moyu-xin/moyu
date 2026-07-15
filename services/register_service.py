from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from services import mailbox_pool_store, partial_account_store, proxy_controller, proxy_pool_store
from services.atomic_json_store import read_json, write_json
from services.managed_mihomo_service import managed_mihomo_manager
from services.config import DATA_DIR
from services.register import mail_provider, openai_register
from services.registered_account_store import list_registered_accounts
from services.proxy_service import normalize_proxy_url

REGISTER_FILE = DATA_DIR / "register.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict:
    return {
        **openai_register.config,
        "mode": "total",
        "register_mode": "free",
        "product_mode": "partial",
        "mailbox_group": "",
        "target_quota": 100,
        "target_available": 10,
        "check_interval": 5,
        "enabled": False,
        "stats": {
            "success": 0,
            "fail": 0,
            "done": 0,
            "running": 0,
            "threads": openai_register.config["threads"],
            "elapsed_seconds": 0,
            "avg_seconds": 0,
            "success_rate": 0,
            "current_quota": 0,
            "current_available": 0,
            "registered_total": 0,
            "partial_total": 0,
            "stored_total": 0,
        },
    }


def _is_region_blocked_error(value: object) -> bool:
    text = str(value or "")
    return (
        "unsupported_country_region_territory" in text
        or "Country, region, or territory not supported" in text
        or "platform_authorize_http_403" in text
    )


def _is_auth_flow_blocked_error(value: object) -> bool:
    text = str(value or "")
    return (
        "invalid_auth_step" in text
        or "Invalid authorization step" in text
        or "auth_flow_invalid_step" in text
        or "phone_required" in text
        or "add-phone" in text
    )


def _is_cloudflare_blocked_error(value: object) -> bool:
    text = str(value or "")
    if "registration_disallowed" in text or "user_already_exists" in text:
        return False
    return (
        "Cloudflare" in text
        or "Just a moment" in text
        or "cf-ray" in text
        or "cf-chl" in text
        or "被 Cloudflare" in text
    )


def _is_proxy_transport_error(value: object) -> bool:
    text = str(value or "")
    return (
        "TLS connect error" in text
        or "curl: (35)" in text
        or "Connection closed abruptly" in text
        or "Failed to perform" in text and "curl:" in text
    )


def _safe_bool(value: object, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return fallback


def _normalize(raw: dict) -> dict:
    cfg = _default_config()
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, min(64, int(cfg.get("threads") or 1)))
    mode = str(cfg.get("mode") or "total").strip()
    cfg["mode"] = mode if mode in {"total", "quota", "available"} else "total"
    register_mode = str(cfg.get("register_mode") or ("k12" if cfg.get("k12_mode") else "free")).strip().lower()
    legacy_free_crash = register_mode == "free_crash"
    if register_mode == "free_wu":
        register_mode = "free"
    if legacy_free_crash:
        register_mode = "free"
    cfg["register_mode"] = register_mode if register_mode in {"free", "k12"} else "free"
    product_mode = str(cfg.get("product_mode") or "partial").strip().lower()
    cfg["product_mode"] = product_mode if product_mode in {"partial", "finished"} else "partial"
    if legacy_free_crash:
        cfg["product_mode"] = "finished"
    mailbox_group = str(cfg.get("mailbox_group") or "").strip()
    if mailbox_group.lower() in {"all", "__all__", "*"} or mailbox_group == "\u5168\u90e8":
        mailbox_group = ""
    cfg["mailbox_group"] = mailbox_group[:80]
    cfg["k12_mode"] = cfg["register_mode"] == "k12" or _safe_bool(cfg.get("k12_mode"), False)
    if cfg["register_mode"] == "free":
        cfg["k12_mode"] = False
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg["proxy"] = normalize_proxy_url(str(cfg.get("proxy") or "").strip())
    default_mail = _default_config()["mail"] if isinstance(_default_config().get("mail"), dict) else {}
    mail = cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    cfg["mail"] = {**default_mail, **mail}
    cfg["mail"]["api_use_register_proxy"] = _safe_bool(cfg["mail"].get("api_use_register_proxy"), True)
    cfg["mail"].pop("proxy", None)
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["stats"] = {
        **_default_config()["stats"],
        **(raw.get("stats") if isinstance(raw.get("stats"), dict) else {}),
        "threads": cfg["threads"],
    }
    return cfg


class RegisterService:
    def __init__(self, store_file: Path):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._logs: list[dict] = []
        openai_register.register_log_sink = self._append_log
        self._config = self._load()
        try:
            mailbox_pool_store.import_from_register_config(self._config)
            self._config = mailbox_pool_store.apply_to_register_config(self._config)
            self._save()
        except Exception:
            pass
        if self._config["enabled"]:
            self.start()

    def _load(self) -> dict:
        raw = read_json(self._store_file, {}, expected_schema_version=1)
        logs = raw.get("logs") if isinstance(raw, dict) else []
        if isinstance(logs, list):
            self._logs = [entry for entry in logs if isinstance(entry, dict)][-300:]
        return _normalize(raw if isinstance(raw, dict) else {})

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {**self._config, "logs": self._logs[-300:]}
        write_json(self._store_file, payload, schema_version=1)

    def _push_runtime_config(self) -> None:
        self._config = mailbox_pool_store.apply_to_register_config(self._config)
        self._drop_mail_proxy()
        openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads", "k12_mode", "register_mode", "product_mode", "mailbox_group")})

    def _validate_runtime_mail_config(self) -> None:
        mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
        providers = mail.get("providers") if isinstance(mail.get("providers"), list) else []
        enabled_providers = [item for item in providers if isinstance(item, dict) and item.get("enable")]
        for provider in enabled_providers:
            provider_type = str(provider.get("type") or "").strip().lower()
            if provider_type == "outlook_token":
                credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
                if mail_provider.available_outlook_token_count(credentials) > 0:
                    return
                continue
            if provider_type == "gmail_oauth":
                credentials = mail_provider.parse_gmail_credentials(str(provider.get("mailboxes") or ""))
                if mail_provider.available_outlook_token_count(credentials) > 0:
                    return
                continue
            return
        source = str(mail.get("registration_source") or "outlook_token").strip().lower()
        if source == "gmail_oauth":
            raise RuntimeError("Gmail OAuth 邮箱没有可领用凭据，可能已全部使用或失效，请补充邮箱或重置状态")
        if source == "outlook_token":
            group = str(self._config.get("mailbox_group") or mail.get("mailbox_group") or "").strip()
            label = f"“{group}”" if group else "全部"
            raise RuntimeError(f"微软邮箱分组{label}没有可用于注册的邮箱，请在“邮箱”中导入邮箱或切换分组")
        raise RuntimeError(f"邮箱来源 {source or 'unknown'} 配置不完整，请先在“邮箱配置”中补齐凭据")

    def get(self) -> dict:
        pool_metrics = self._pool_metrics()
        with self._lock:
            stats = self._config.get("stats") if isinstance(self._config.get("stats"), dict) else {}
            stats.update({key: value for key, value in pool_metrics.items() if key in {"registered_total", "partial_total", "stored_total"}})
            self._config["stats"] = stats
            snapshot = json.loads(json.dumps({**self._config, "logs": self._logs[-300:]}, ensure_ascii=False))
        self._redact_outlook_pools(snapshot)
        return snapshot

    @staticmethod
    def _mask_email(email: str) -> str:
        local, sep, domain = str(email or "").partition("@")
        if not sep:
            return "***"
        masked = (local[:2] + "***" + local[-1:]) if len(local) > 2 else (local[:1] + "***")
        return f"{masked}@{domain}"

    def _redact_outlook_pools(self, snapshot: dict) -> None:
        mail = snapshot.get("mail")
        if not isinstance(mail, dict):
            return
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            provider_type = str(provider.get("type") or "")
            if provider_type in {"outlook_token", "gmail_oauth"}:
                if provider_type == "gmail_oauth" and hasattr(mail_provider, "parse_gmail_credentials"):
                    credentials = mail_provider.parse_gmail_credentials(str(provider.get("mailboxes") or ""))
                else:
                    credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
                provider["mailboxes"] = ""
                provider["mailboxes_count"] = len(credentials)
                provider["mailboxes_preview"] = [self._mask_email(c["email"]) for c in credentials]
                if provider_type == "outlook_token":
                    provider["mailboxes_stats"] = mail_provider.outlook_token_pool_stats(credentials)
            if provider_type == "cloudflare_temp_email" and provider.get("admin_password"):
                provider["has_admin_password"] = True
                provider["admin_password"] = "***"
            for secret_key in ("ddg_token", "cf_inbox_jwt", "admin_password", "cf_api_key", "api_key", "webhook_secret"):
                if provider.get(secret_key):
                    provider[f"has_{secret_key}"] = True
                    provider[secret_key] = "***"

    def _drop_mail_proxy(self) -> None:
        if isinstance(self._config.get("mail"), dict):
            self._config["mail"].pop("proxy", None)

    def _import_outlook_text_from_updates(self, updates: dict) -> None:
        mail = updates.get("mail")
        if not isinstance(mail, dict) or not isinstance(mail.get("providers"), list):
            return
        for provider in mail["providers"]:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            text = str(provider.get("mailboxes") or "")
            if text.strip():
                mailbox_pool_store.import_outlook_text(text, note="imported from register provider", group=str(updates.get("mailbox_group") or mail.get("mailbox_group") or ""))
            for key in ("mailboxes", "mailboxes_count", "mailboxes_preview", "mailboxes_stats"):
                provider.pop(key, None)

    def sync_mailbox_pool(self) -> dict:
        with self._lock:
            self._push_runtime_config()
            self._save()
            return self.get()

    def update(self, updates: dict) -> dict:
        with self._lock:
            self._import_outlook_text_from_updates(updates)
            self._config = _normalize({**self._config, **updates})
            self._push_runtime_config()
            self._save()
            return self.get()

    def start(self) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                self._config["enabled"] = True
                self._push_runtime_config()
                self._save()
                return self.get()
            self._push_runtime_config()
            self._validate_runtime_mail_config()
            self._config["enabled"] = True
            self._logs = []
            metrics = self._pool_metrics()
            self._config["stats"] = {
                "job_id": uuid.uuid4().hex,
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "threads": self._config["threads"],
                **metrics,
                "current_quota": 0,
                "current_available": 0,
                "started_at": _now(),
                "updated_at": _now(),
            }
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            self._save()
            self._runner = threading.Thread(target=self._run, daemon=True, name="openai-register")
            self._runner.start()
            group_label = self._config.get("mailbox_group") or "全部"
            target_mode = str(self._config.get("mode") or "total")
            target_value = self._config.get("target_available") if target_mode == "available" else self._config.get("target_quota") if target_mode == "quota" else self._config.get("total")
            self._append_log(f"注册任务已启动：目标模式={target_mode}，目标={target_value}，最大尝试={self._config['total']}，注册模式={self._config.get('register_mode') or 'free'}，产物模式={self._config.get('product_mode') or 'partial'}，邮箱分组={group_label}，线程={self._config['threads']}", "yellow")
            return self.get()

    def stop(self) -> dict:
        with self._lock:
            self._config["enabled"] = False
            self._config["stats"]["updated_at"] = _now()
            self._save()
            self._append_log("已请求停止注册任务，等待当前线程结束", "yellow")
            return self.get()

    def reset(self) -> dict:
        with self._lock:
            self._logs = []
            self._config["stats"] = {
                "success": 0,
                "fail": 0,
                "done": 0,
                "running": 0,
                "threads": self._config["threads"],
                "elapsed_seconds": 0,
                "avg_seconds": 0,
                "success_rate": 0,
                **self._pool_metrics(),
                "updated_at": _now(),
            }
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 0.0})
            self._save()
            return self.get()

    def reset_outlook_pool(self, scope: str = "all") -> dict:
        scope = str(scope or "all").strip().lower()
        if scope == "unused":
            result = mailbox_pool_store.prune_unused_mailboxes()
            removed = int(result.get("removed") or 0)
            with self._lock:
                self._push_runtime_config()
                self._save()
                self._append_log(f"已清空未使用邮箱 {removed} 个", "yellow")
            return self.get()
        scope = "failed" if scope == "failed" else "all"
        cleared = mail_provider.reset_outlook_token_pool_state(scope)
        with self._lock:
            self._append_log(f"已重置 Outlook 邮箱状态：scope={scope}，清理 {cleared} 条", "yellow")
        return self.get()

    def _append_log(self, text: str, color: str = "") -> None:
        with self._lock:
            self._logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._logs = self._logs[-300:]
            try:
                self._save()
            except Exception:
                pass

    def _pool_metrics(self) -> dict:
        registered = list_registered_accounts()
        partial = partial_account_store.list_partial_accounts(include_secret=False)
        available = 0
        quota = 0.0
        for account in registered:
            if str(account.get("status") or "active").lower() in {"disabled", "inactive", "blocked", "banned", "dead", "invalid"}:
                continue
            if str(account.get("health_status") or "").lower() in {"ok", "alive", "healthy"}:
                available += 1
            try:
                remaining = float(account.get("quota_remaining"))
            except (TypeError, ValueError):
                remaining = 0.0
            if remaining > 0:
                quota += remaining
        return {
            "current_quota": round(quota, 2),
            "current_available": available,
            "registered_total": len(registered),
            "partial_total": len(partial),
            "stored_total": len(registered) + len(partial),
        }

    @staticmethod
    def _target_quota_increment(result: dict) -> float:
        quota = result.get("quota") if isinstance(result.get("quota"), dict) else {}
        for key in ("quota_remaining", "quota_total"):
            try:
                value = float(quota.get(key))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0.0

    def _target_reached(self, cfg: dict, submitted: int, current_available: int = 0, current_quota: float = 0.0) -> bool:
        if submitted >= int(cfg.get("total") or 1):
            return True
        mode = str(cfg.get("mode") or "total").strip().lower()
        if mode == "available":
            return current_available >= int(cfg.get("target_available") or 1)
        if mode == "quota":
            return current_quota >= float(cfg.get("target_quota") or 1)
        return False

    def _run_registration_task(self, task_index: int, proxy: str, assignment: dict, target_mode: str) -> dict:
        result = openai_register.worker(task_index, proxy, assignment)
        if not result.get("ok") or target_mode not in {"available", "quota"}:
            return result
        account = result.get("result") if isinstance(result.get("result"), dict) else {}
        if not account:
            return result
        try:
            from services.account_inventory_service import account_inventory_service

            target_check = account_inventory_service.check_account(
                account,
                method="local",
                refresh_quota=target_mode == "quota",
                proxy=proxy,
            )
        except Exception as error:
            target_check = {"ok": False, "quota_ok": False, "status": "failed", "error": str(error)}
        result["target_check"] = target_check
        return result

    def _bump(self, **updates) -> None:
        with self._lock:
            self._config["stats"].update(updates)
            stats = self._config["stats"]
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["avg_seconds"] = round(elapsed / success, 1) if success else 0
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            self._config["stats"]["updated_at"] = _now()
            self._save()

    def _rotate_proxy_for_error(self, reason: object) -> bool:
        try:
            result = proxy_controller.rotate_proxy(str(reason or "")[:200])
        except Exception as error:
            self._append_log(f"代理控制器切换失败：{error}", "yellow")
            return False
        if result.get("ok"):
            self._append_log(
                f"代理控制器已切换：{result.get('group')} {result.get('previous') or '-'} -> {result.get('selected')}",
                "yellow",
            )
            return True
        self._append_log(f"代理控制器未切换：{result.get('message') or result}", "yellow")
        return False

    def _quarantine_managed_proxy(self, result: dict, reason: object) -> bool:
        assignment = result.get("proxy_assignment") if isinstance(result.get("proxy_assignment"), dict) else {}
        if str(assignment.get("source") or "") != "managed_mihomo":
            return False
        try:
            quarantined = managed_mihomo_manager.report_failure(assignment, reason)
        except Exception as error:
            self._append_log(f"托管代理节点隔离失败：{error}", "yellow")
            return True
        if quarantined.get("ok"):
            minutes = max(1, int(quarantined.get("remaining_seconds") or 0) // 60)
            self._append_log(
                f"托管代理节点已隔离 {minutes} 分钟：{quarantined.get('name') or assignment.get('label') or '-'}；后续任务将自动换节点。",
                "yellow",
            )
        return True

    def _run(self) -> None:
        threads = int(self.get()["threads"])
        submitted, done, success, fail = 0, 0, 0, 0
        current_available = 0
        current_quota = 0.0
        with ThreadPoolExecutor(max_workers=threads) as executor:
            futures: dict[object, int] = {}
            while True:
                cfg = self.get()
                while self.get()["enabled"] and not self._target_reached(cfg, submitted, current_available, current_quota) and len(futures) < threads:
                    busy_slots = set(futures.values())
                    slot = next((candidate for candidate in range(threads) if candidate not in busy_slots), len(futures) % max(1, threads))
                    task_index = submitted + 1
                    assignment = proxy_pool_store.assign_proxy(task_index, fallback=str(cfg.get("proxy") or ""), total_threads=threads, slot_index=slot, purpose="registration")
                    if not self.get()["enabled"]:
                        self._append_log(f"[任务{task_index}/线程{slot + 1}] 已停止，取消提交已完成代理预检的任务", "yellow")
                        break
                    if assignment.get("error"):
                        self._append_log(f"[任务{task_index}/线程{slot + 1}] 代理分配失败：{assignment.get('error')}", "yellow")
                    if assignment.get("managed_failure") and not assignment.get("proxy"):
                        time.sleep(max(0.2, min(2.0, float(cfg.get("retry_delay") or 1.0))))
                        cfg = self.get()
                        continue
                    submitted = task_index
                    if assignment.get("warning"):
                        self._append_log(f"[任务{submitted}/线程{slot + 1}] 代理池提示：{assignment.get('warning')}", "yellow")
                    if assignment.get("source") not in {"", "fallback"}:
                        self._append_log(f"[任务{submitted}/线程{slot + 1}] 分配代理节点：{assignment.get('label') or assignment.get('node_id') or '代理节点'}", "yellow")
                    future = executor.submit(
                        self._run_registration_task,
                        submitted,
                        str(assignment.get("proxy") or ""),
                        assignment,
                        str(cfg.get("mode") or "total").strip().lower(),
                    )
                    futures[future] = slot
                self._bump(running=len(futures), done=done, success=success, fail=fail, current_available=current_available, current_quota=round(current_quota, 2), **{key: value for key, value in self._pool_metrics().items() if key in {"registered_total", "partial_total", "stored_total"}})
                if not futures and (not self.get()["enabled"] or self._target_reached(cfg, submitted, current_available, current_quota)):
                    break
                if not futures:
                    time.sleep(max(1, int(cfg.get("check_interval") or 5)))
                    continue
                finished, _pending = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
                for future in finished:
                    futures.pop(future, None)
                    done += 1
                    try:
                        result = future.result()
                        if result.get("ok"):
                            success += 1
                            target_check = result.get("target_check") if isinstance(result.get("target_check"), dict) else {}
                            mode = str(cfg.get("mode") or "total").strip().lower()
                            if mode == "available":
                                if target_check.get("ok"):
                                    current_available += 1
                                else:
                                    self._append_log(f"任务 {result.get('index')} 注册成功但测活未通过，不计入可用账号目标：{target_check.get('error') or target_check.get('status') or 'unknown'}", "yellow")
                            elif mode == "quota":
                                increment = self._target_quota_increment(target_check) if target_check.get("ok") and target_check.get("quota_ok") else 0.0
                                current_quota += increment
                                if increment <= 0:
                                    self._append_log(f"任务 {result.get('index')} 注册成功但未读取到可累计额度，不计入额度目标", "yellow")
                        else:
                            fail += 1
                            if _is_region_blocked_error(result.get("error")):
                                if self._quarantine_managed_proxy(result, result.get("error")):
                                    pass
                                elif not self._rotate_proxy_for_error(result.get("error")):
                                    with self._lock:
                                        self._config["enabled"] = False
                                        self._save()
                                    self._append_log("OpenAI/Auth0 返回地区不可用，已自动停止本批任务；请更换代理后释放失败/占用邮箱再重试。", "yellow")
                            elif _is_auth_flow_blocked_error(result.get("error")):
                                self._append_log("OpenAI/Auth0 注册流程状态不匹配，已记录该邮箱并继续尝试下一邮箱。", "yellow")
                            elif _is_cloudflare_blocked_error(result.get("error")):
                                if self._quarantine_managed_proxy(result, result.get("error")):
                                    pass
                                elif not self._rotate_proxy_for_error(result.get("error")):
                                    with self._lock:
                                        self._config["enabled"] = False
                                        self._save()
                                    self._append_log("OpenAI/Auth0 被 Cloudflare 拦截，已自动停止本批任务；请更换代理或配置 clearance 后再释放 CF/失败邮箱重试。", "yellow")
                            elif _is_proxy_transport_error(result.get("error")):
                                if self._quarantine_managed_proxy(result, result.get("error")):
                                    pass
                                elif not self._rotate_proxy_for_error(result.get("error")):
                                    self._append_log("代理/TLS 连接失败，代理控制器未能切换；请检查本地代理或节点可用性。", "yellow")
                    except Exception:
                        fail += 1
        self._bump(running=0, done=done, success=success, fail=fail, current_available=current_available, current_quota=round(current_quota, 2), finished_at=_now(), **{key: value for key, value in self._pool_metrics().items() if key in {"registered_total", "partial_total", "stored_total"}})
        with self._lock:
            self._config["enabled"] = False
            self._save()
        self._append_log(f"注册任务结束：成功={success}，失败={fail}", "yellow")


register_service = RegisterService(REGISTER_FILE)
