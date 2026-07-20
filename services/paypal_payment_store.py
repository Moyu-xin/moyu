from __future__ import annotations

import hashlib
import random
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR
from services.paypal_protocol import PAYPAL_PROTOCOL_COUNTRIES, parse_ba_token
from services.paypal_protocol.phone_pool import PhonePoolError, PhonePoolStore


PAYPAL_PAYMENT_FILE = DATA_DIR / "paypal_payments.json"
PAYPAL_PHONE_POOL_FILE = DATA_DIR / "paypal_phone_pool.json"
PHONE_POOL_OWNER_ID = "paypal-payment-admin"
TERMINAL_STATUSES = {"success", "failed", "cancelled"}
RUNNING_STATUSES = {"queued", "running", "cancelling"}
STAGE_PROGRESS = {
    "queued": (0, "排队中"),
    "preparing": (5, "准备国家协议与代理"),
    "paypal_payment": (15, "启动 PayPal 协议"),
    "paypal_sms": (25, "获取验证号码"),
    "paypal_otp": (45, "等待短信验证码"),
    "paypal_signup": (70, "创建 PayPal 账户并授权"),
    "paypal_done": (100, "PayPal BA 授权成功"),
}


class _PaymentCancelled(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: object) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16] if text else ""


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _redact(value: object, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]\s*[^,\s\"']+", r"\1=[redacted]", text)
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(https?|socks5h?)://[^@\s/]+@", r"\1://[redacted]@", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _default_config() -> dict[str, Any]:
    return {
        "auto_pay_new_links": False,
        "protocol_country": "AUTO",
        "sms_provider": "smsbower",
        "proxy_override": "",
        "smsbower_api_key": "",
        "smsbower_max_price": 0.014,
        "max_card_attempts": 5,
        "sms_max_attempts": 5,
        "flow_attempts": 2,
        "concurrency": 1,
    }


def _normalize_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _default_config()
    if isinstance(raw, dict):
        cfg.update({key: value for key, value in raw.items() if key in cfg})
    cfg["auto_pay_new_links"] = bool(cfg.get("auto_pay_new_links"))
    country = str(cfg.get("protocol_country") or "AUTO").strip().upper()
    cfg["protocol_country"] = country if country in PAYPAL_PROTOCOL_COUNTRIES else "AUTO"
    provider = str(cfg.get("sms_provider") or "smsbower").strip().lower()
    cfg["sms_provider"] = provider if provider in {"smsbower", "phone_pool"} else "smsbower"
    cfg["proxy_override"] = str(cfg.get("proxy_override") or "").strip()[:1000]
    cfg["smsbower_api_key"] = str(cfg.get("smsbower_api_key") or "").strip()[:256]
    try:
        price = float(cfg.get("smsbower_max_price") or 0.014)
    except (TypeError, ValueError):
        price = 0.014
    cfg["smsbower_max_price"] = round(max(0.001, min(10.0, price)), 3)
    cfg["max_card_attempts"] = _clamp_int(cfg.get("max_card_attempts"), 5, 1, 20)
    cfg["sms_max_attempts"] = _clamp_int(cfg.get("sms_max_attempts"), 5, 1, 20)
    cfg["flow_attempts"] = _clamp_int(cfg.get("flow_attempts"), 2, 1, 5)
    cfg["concurrency"] = _clamp_int(cfg.get("concurrency"), 1, 1, 5)
    return cfg


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
            "updated_at": "",
        },
        "logs": [],
        "results": [],
    }


class PaypalPaymentService:
    def __init__(
        self,
        store_file: Path = PAYPAL_PAYMENT_FILE,
        phone_pool_file: Path | None = None,
    ):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pending: list[dict[str, Any]] = []
        self._controls: dict[str, dict[str, Any]] = {}
        self._state = self._load()
        resolved_phone_pool_file = phone_pool_file or (
            PAYPAL_PHONE_POOL_FILE
            if Path(store_file) == PAYPAL_PAYMENT_FILE
            else Path(store_file).with_name("paypal_phone_pool.json")
        )
        self.phone_pool = PhonePoolStore(resolved_phone_pool_file)
        self._phone_pool_load_error = ""
        try:
            self.phone_pool.load()
        except PhonePoolError as error:
            self._phone_pool_load_error = str(error)
        recovered = 0
        for item in self._state.get("results", []):
            if isinstance(item, dict) and str(item.get("status") or "") in RUNNING_STATUSES:
                item.update({
                    "status": "cancelled",
                    "stage": "服务重启后已结束",
                    "error": "服务重启时支付进程已终止",
                    "finished_at": _now(),
                })
                recovered += 1
        if recovered or self._state.get("enabled"):
            self._state["enabled"] = False
            self._recalculate_stats_locked()
            self._save()

    def _load(self) -> dict[str, Any]:
        data = read_json(self._store_file, {}, expected_schema_version=1)
        state = _default_state()
        if isinstance(data, dict):
            state.update({key: value for key, value in data.items() if key in state})
        state["config"] = _normalize_config(state.get("config") if isinstance(state.get("config"), dict) else {})
        state["stats"] = {**_default_state()["stats"], **(state.get("stats") if isinstance(state.get("stats"), dict) else {})}
        state["logs"] = state.get("logs") if isinstance(state.get("logs"), list) else []
        state["results"] = state.get("results") if isinstance(state.get("results"), list) else []
        return state

    def _save(self) -> None:
        write_json(self._store_file, self._state, schema_version=1)

    def _append_log(self, message: str, level: str = "info") -> None:
        with self._lock:
            self._state.setdefault("logs", []).append({
                "time": _now(),
                "text": _redact(message, 360),
                "level": str(level or "info"),
            })
            self._state["logs"] = self._state["logs"][-300:]
            self._state.setdefault("stats", {})["updated_at"] = _now()
            self._save()

    def _recalculate_stats_locked(self) -> None:
        results = [item for item in self._state.get("results", []) if isinstance(item, dict)]
        stats = self._state.setdefault("stats", {})
        stats.update({
            "success": sum(str(item.get("status") or "") == "success" for item in results),
            "fail": sum(str(item.get("status") or "") == "failed" for item in results),
            "cancelled": sum(str(item.get("status") or "") == "cancelled" for item in results),
            "running": sum(str(item.get("status") or "") in RUNNING_STATUSES for item in results),
            "total": len(results),
            "updated_at": _now(),
        })
        stats["done"] = int(stats["success"]) + int(stats["fail"]) + int(stats["cancelled"])

    def _plus_state(self) -> dict[str, Any]:
        from services.plus_link_store import plus_link_service

        return plus_link_service.get(include_secret=True, include_inventory=False)

    def _candidates(self) -> list[dict[str, Any]]:
        plus_state = self._plus_state()
        paid_hashes = {
            str(item.get("ba_url_hash") or "")
            for item in self._state.get("results", [])
            if isinstance(item, dict) and str(item.get("status") or "") == "success"
        }
        candidates: list[dict[str, Any]] = []
        for item in plus_state.get("results", []):
            if not isinstance(item, dict):
                continue
            ba_url = str(item.get("long_url") or "").strip()
            if not ba_url:
                continue
            try:
                parse_ba_token(ba_url)
            except ValueError:
                continue
            country = str(item.get("payment_method_country") or "").strip().upper()
            ba_hash = str(item.get("long_url_hash") or _sha(ba_url))
            candidates.append({
                "source_task_id": str(item.get("task_id") or ""),
                "email": str(item.get("account_email") or item.get("email") or ""),
                "ba_url": ba_url,
                "ba_url_hash": ba_hash,
                "country": country,
                "protocol_mode": f"{country}_NEW" if country in PAYPAL_PROTOCOL_COUNTRIES else "",
                "supported": country in PAYPAL_PROTOCOL_COUNTRIES,
                "paid": ba_hash in paid_hashes,
                "created_at": str(item.get("finished_at") or item.get("created_at") or ""),
                "plus_status": str(item.get("status") or ""),
            })
        return sorted(candidates, key=lambda item: item.get("created_at") or "", reverse=True)

    def get(self) -> dict[str, Any]:
        with self._lock:
            state = {
                "enabled": bool(self._runner and self._runner.is_alive()),
                "runner_alive": bool(self._runner and self._runner.is_alive()),
                "config": dict(self._state.get("config") or {}),
                "stats": dict(self._state.get("stats") or {}),
                "logs": [dict(item) for item in self._state.get("logs", []) if isinstance(item, dict)],
                "results": [dict(item) for item in self._state.get("results", []) if isinstance(item, dict)],
            }
        config = state["config"]
        config["smsbower_api_key_configured"] = bool(config.get("smsbower_api_key"))
        config["smsbower_api_key"] = ""
        phone_numbers = self.phone_pool.list(PHONE_POOL_OWNER_ID)
        phone_summary: dict[str, int] = {}
        for item in phone_numbers:
            status = str(item.get("status") or "unknown")
            phone_summary[status] = phone_summary.get(status, 0) + 1
        state["phone_pool"] = {
            "numbers": phone_numbers,
            "summary": phone_summary,
            "total": len(phone_numbers),
            "load_error": self._phone_pool_load_error,
        }
        state["candidates"] = self._candidates()
        return state

    def import_phone_pool(self, text: str) -> dict[str, Any]:
        result = self.phone_pool.import_text(PHONE_POOL_OWNER_ID, text)
        return {"import_result": result, "paypal_payments": self.get()}

    def reset_phone_pool(self, entry_id: str) -> dict[str, Any]:
        try:
            self.phone_pool.reset(PHONE_POOL_OWNER_ID, str(entry_id or "").strip())
        except KeyError as error:
            raise RuntimeError("手机号池号码不存在") from error
        return self.get()

    def delete_phone_pool(self, entry_id: str) -> dict[str, Any]:
        try:
            self.phone_pool.delete(PHONE_POOL_OWNER_ID, str(entry_id or "").strip())
        except KeyError as error:
            raise RuntimeError("手机号池号码不存在") from error
        return self.get()

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
            merged = dict(current)
            for key, value in updates.items():
                if key not in merged:
                    continue
                if key == "smsbower_api_key" and not str(value or "").strip():
                    continue
                merged[key] = value
            self._state["config"] = _normalize_config(merged)
            self._save()
        return self.get()

    def _set_result(self, task_id: str, updates: dict[str, Any]) -> None:
        with self._lock:
            for item in self._state.setdefault("results", []):
                if isinstance(item, dict) and str(item.get("task_id") or "") == task_id:
                    item.update(updates)
                    item["updated_at"] = _now()
                    break
            self._recalculate_stats_locked()
            self._save()

    def _resolve_proxy(self, country: str, config: dict[str, Any]) -> str:
        from services import plus_link_store

        plus_config = self._plus_state().get("config") or {}
        override = str(config.get("proxy_override") or "").strip()
        if override:
            candidates = plus_link_store._split_residential_pool(override)
        else:
            candidates = plus_link_store._country_proxy_items(plus_config, country)
            if not candidates:
                common = plus_link_store._split_residential_pool(plus_config.get("residential_proxy_pool"))
                candidates = plus_link_store._matching_common_proxy_pool(common, country)
        if not candidates:
            raise RuntimeError(f"{country} 协议没有可用代理；请配置协议代理或 Plus 的 {country} 国家代理池")
        raw = random.choice(candidates)
        return plus_link_store._rotate_residential_proxy(
            raw,
            country=country,
            session_id=plus_link_store._proxy_session_id(),
            index=1,
            stage="paypal",
        )

    def _check_cancelled(self, task_id: str) -> None:
        with self._lock:
            control = self._controls.get(task_id) or {}
            event = control.get("cancel_event")
        if self._stop_event.is_set() or (isinstance(event, threading.Event) and event.is_set()):
            raise _PaymentCancelled("用户取消协议支付")

    def _run_one(self, task: dict[str, Any]) -> None:
        task_id = str(task["task_id"])
        source = dict(task.get("source") or {})
        config = _normalize_config(task.get("config") if isinstance(task.get("config"), dict) else {})
        started = time.time()
        try:
            self._check_cancelled(task_id)
            self._set_result(task_id, {
                "status": "running",
                "stage": STAGE_PROGRESS["preparing"][1],
                "engine_step": "preparing",
                "progress_percent": STAGE_PROGRESS["preparing"][0],
                "started_at": _now(),
                "error": "",
            })
            country = str(config.get("protocol_country") or "AUTO").upper()
            if source.get("source_kind") == "manual" or country == "AUTO":
                country = str(source.get("country") or "").strip().upper()
            if country not in PAYPAL_PROTOCOL_COUNTRIES:
                raise RuntimeError(f"没有与国家 {country or '<empty>'} 对应的 PayPal 协议")
            sms_provider = str(config.get("sms_provider") or "smsbower").strip().lower()
            api_key = str(config.get("smsbower_api_key") or "").strip()
            if sms_provider == "smsbower" and not api_key:
                raise RuntimeError("未配置 SMSBower API Key")
            ba_url = str(source.get("ba_url") or "").strip()
            parse_ba_token(ba_url)
            proxy = self._resolve_proxy(country, config)
            parsed_proxy = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
            proxy_label = f"{parsed_proxy.hostname or 'proxy'}:{parsed_proxy.port or ''}".rstrip(":")
            self._set_result(task_id, {
                "country": country,
                "protocol_mode": f"{country}_NEW",
                "sms_provider": sms_provider,
                "proxy_label": proxy_label,
                "proxy_hash": _sha(proxy),
            })

            def check() -> None:
                self._check_cancelled(task_id)

            def stage(step: str, message: str) -> None:
                check()
                progress, label = STAGE_PROGRESS.get(step, STAGE_PROGRESS["paypal_payment"])
                self._set_result(task_id, {
                    "status": "running",
                    "stage": label,
                    "engine_step": step,
                    "progress_percent": progress,
                    "last_message": _redact(message, 280),
                })

            def log(level: str, message: str) -> None:
                check()
                safe = _redact(message, 360)
                self._set_result(task_id, {"last_log": safe})
                if str(level or "").upper() in {"WARNING", "ERROR", "CRITICAL"}:
                    self._append_log(
                        f"[{source.get('email') or task_id}] {country}：{safe}",
                        "warning" if str(level).upper() == "WARNING" else "danger",
                    )

            from services.paypal_protocol import run_paypal_payment

            result = run_paypal_payment(
                ba_url=ba_url,
                country=country,
                proxy_url=proxy,
                sms_provider=sms_provider,
                smsbower_api_key=api_key,
                smsbower_max_price=float(config.get("smsbower_max_price") or 0.014),
                phone_pool=self.phone_pool if sms_provider == "phone_pool" else None,
                phone_pool_owner_id=PHONE_POOL_OWNER_ID,
                phone_pool_job_id=task_id,
                max_card_attempts=int(config.get("max_card_attempts") or 5),
                sms_max_attempts=int(config.get("sms_max_attempts") or 5),
                max_flow_attempts=int(config.get("flow_attempts") or 2),
                on_log=log,
                on_stage=stage,
                check_cancelled=check,
            )
            self._set_result(task_id, {
                "status": "success",
                "stage": STAGE_PROGRESS["paypal_done"][1],
                "engine_step": "paypal_done",
                "progress_percent": 100,
                "payment_result": result,
                "finished_at": _now(),
                "duration_seconds": round(time.time() - started, 1),
                "error": "",
            })
            self._append_log(
                f"[{source.get('email') or task_id}] {country} New 协议支付成功",
                "success",
            )
        except _PaymentCancelled as error:
            self._set_result(task_id, {
                "status": "cancelled",
                "stage": "已取消",
                "error": str(error),
                "finished_at": _now(),
                "duration_seconds": round(time.time() - started, 1),
            })
        except BaseException as error:
            self._set_result(task_id, {
                "status": "failed",
                "stage": "协议支付失败",
                "error": _redact(error, 800),
                "finished_at": _now(),
                "duration_seconds": round(time.time() - started, 1),
            })
            self._append_log(
                f"[{source.get('email') or task_id}] 协议支付失败：{error}",
                "danger",
            )

    def _run_queue(self) -> None:
        active: dict[Any, dict[str, Any]] = {}
        try:
            with self._lock:
                concurrency = _clamp_int((self._state.get("config") or {}).get("concurrency"), 1, 1, 5)
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                while True:
                    with self._lock:
                        while not self._stop_event.is_set() and self._pending and len(active) < concurrency:
                            task = self._pending.pop(0)
                            active[executor.submit(self._run_one, task)] = task
                        pending_count = len(self._pending)
                    if not active and not pending_count:
                        break
                    if not active:
                        time.sleep(0.1)
                        continue
                    completed, _ = wait(set(active), timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in completed:
                        active.pop(future, None)
                        try:
                            future.result()
                        except BaseException as error:
                            self._append_log(f"支付 worker 异常：{error}", "danger")
        finally:
            with self._lock:
                self._state["enabled"] = False
                self._runner = None
                self._recalculate_stats_locked()
                self._save()
            self._stop_event.clear()

    def start(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options if isinstance(options, dict) else {}
        config_updates = options.get("config") if isinstance(options.get("config"), dict) else {}
        with self._lock:
            stored = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
            merged_updates = dict(config_updates)
            if not str(merged_updates.get("smsbower_api_key") or "").strip():
                merged_updates.pop("smsbower_api_key", None)
            config = _normalize_config({**stored, **merged_updates})
            if config_updates and options.get("persist_config", True):
                self._state["config"] = config
                self._save()
        selected_ids = {
            str(value or "").strip()
            for value in (options.get("source_task_ids") or [])
            if str(value or "").strip()
        }
        candidates = self._candidates()
        selected = [item for item in candidates if item["source_task_id"] in selected_ids]
        manual_items = options.get("manual_items") if isinstance(options.get("manual_items"), list) else []
        for index, raw_item in enumerate(manual_items[:100], start=1):
            if isinstance(raw_item, dict):
                raw_ba = raw_item.get("ba_url") or raw_item.get("url") or raw_item.get("ba_token")
                country = str(raw_item.get("country") or config.get("protocol_country") or "").strip().upper()
            else:
                raw_ba = raw_item
                country = str(config.get("protocol_country") or "").strip().upper()
            token = parse_ba_token(raw_ba)
            if country not in PAYPAL_PROTOCOL_COUNTRIES:
                raise RuntimeError(
                    f"手动 BA 第 {index} 条没有有效协议国家；请选择 BR/GB/US/TH/ID"
                )
            ba_url = f"https://www.paypal.com/agreements/approve?ba_token={token}"
            ba_hash = _sha(ba_url)
            selected.append({
                "source_task_id": f"manual-{_sha(f'{country}:{ba_url}')}",
                "source_kind": "manual",
                "email": f"手动 BA {index}",
                "ba_url": ba_url,
                "ba_url_hash": ba_hash,
                "country": country,
                "protocol_mode": f"{country}_NEW",
                "supported": True,
                "paid": False,
                "created_at": _now(),
                "plus_status": "manual",
            })
        selected = list({str(item["source_task_id"]): item for item in selected}.values())
        if not selected:
            raise RuntimeError("没有选择 Plus BA，也没有添加手动 BA 链")
        created = 0
        with self._lock:
            active_sources = {
                str(item.get("source_task_id") or "")
                for item in self._state.get("results", [])
                if isinstance(item, dict) and str(item.get("status") or "") in RUNNING_STATUSES
            }
            for source in selected:
                if source["source_task_id"] in active_sources:
                    continue
                task_id = f"paypal-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
                cancel_event = threading.Event()
                task = {
                    "task_id": task_id,
                    "source": dict(source),
                    "config": dict(config),
                }
                self._controls[task_id] = {"cancel_event": cancel_event, **task}
                self._pending.append(task)
                self._state.setdefault("results", []).append({
                    "task_id": task_id,
                    "source_task_id": source["source_task_id"],
                    "source_kind": str(source.get("source_kind") or "plus"),
                    "email": source["email"],
                    "ba_url": source["ba_url"],
                    "ba_url_hash": source["ba_url_hash"],
                    "country": source["country"],
                    "protocol_mode": source["protocol_mode"],
                    "status": "queued",
                    "stage": STAGE_PROGRESS["queued"][1],
                    "engine_step": "queued",
                    "progress_percent": 0,
                    "created_at": _now(),
                    "updated_at": _now(),
                    "error": "",
                })
                created += 1
            if not created:
                return self.get()
            self._state["enabled"] = True
            self._recalculate_stats_locked()
            self._save()
            runner_alive = bool(self._runner and self._runner.is_alive())
            if not runner_alive:
                self._stop_event.clear()
                self._runner = threading.Thread(target=self._run_queue, daemon=True, name="paypal-payments")
                self._runner.start()
        self._append_log(f"已加入 {created} 个 PayPal 协议支付任务", "success")
        return self.get()

    def enqueue_from_plus_result(self, source_task_id: str) -> None:
        with self._lock:
            config = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
        if not config.get("auto_pay_new_links"):
            return
        if config.get("sms_provider") == "smsbower" and not config.get("smsbower_api_key"):
            self._append_log("自动支付已开启，但 SMSBower API Key 未配置", "warning")
            return
        try:
            self.start({
                "source_task_ids": [source_task_id],
                "clear_results": False,
                "persist_config": False,
            })
        except Exception as error:
            self._append_log(f"Plus BA 自动入队失败：{error}", "warning")

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            queued_ids = {str(task.get("task_id") or "") for task in self._pending}
            self._pending = []
            for control in self._controls.values():
                event = control.get("cancel_event")
                if isinstance(event, threading.Event):
                    event.set()
            for item in self._state.get("results", []):
                if not isinstance(item, dict) or str(item.get("status") or "") not in {"queued", "running"}:
                    continue
                task_id = str(item.get("task_id") or "")
                if task_id in queued_ids:
                    item.update({
                        "status": "cancelled",
                        "stage": "已取消",
                        "error": "用户停止协议支付队列",
                        "finished_at": _now(),
                        "updated_at": _now(),
                    })
                else:
                    item.update({"status": "cancelling", "stage": "正在停止", "updated_at": _now()})
            self._recalculate_stats_locked()
            self._save()
        return self.get()

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            control = self._controls.get(str(task_id))
            if not control:
                raise RuntimeError("协议支付任务不存在或服务已重启")
            event = control.get("cancel_event")
            if isinstance(event, threading.Event):
                event.set()
        self._set_result(str(task_id), {"status": "cancelling", "stage": "正在停止"})
        return self.get()

    def retry_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            result = next((item for item in self._state.get("results", []) if isinstance(item, dict) and str(item.get("task_id") or "") == str(task_id)), None)
            if not result or str(result.get("status") or "") not in TERMINAL_STATUSES:
                raise RuntimeError("当前协议支付任务不能重试")
            source_task_id = str(result.get("source_task_id") or "")
            source_kind = str(result.get("source_kind") or "plus")
            manual_item = {
                "ba_url": str(result.get("ba_url") or ""),
                "country": str(result.get("country") or ""),
            }
        if source_kind == "manual":
            return self.start({"manual_items": [manual_item], "clear_results": False})
        return self.start({"source_task_ids": [source_task_id], "clear_results": False})

    def delete_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            result = next((item for item in self._state.get("results", []) if isinstance(item, dict) and str(item.get("task_id") or "") == str(task_id)), None)
            if not result:
                raise RuntimeError("协议支付任务不存在")
            if str(result.get("status") or "") in RUNNING_STATUSES:
                raise RuntimeError("运行中的协议支付任务不能删除")
            self._state["results"] = [item for item in self._state.get("results", []) if not isinstance(item, dict) or str(item.get("task_id") or "") != str(task_id)]
            self._controls.pop(str(task_id), None)
            self._recalculate_stats_locked()
            self._save()
        return self.get()

    def clear_finished(self) -> dict[str, Any]:
        with self._lock:
            self._state["results"] = [item for item in self._state.get("results", []) if not isinstance(item, dict) or str(item.get("status") or "") not in TERMINAL_STATUSES]
            self._recalculate_stats_locked()
            self._save()
        return self.get()


paypal_payment_service = PaypalPaymentService()
