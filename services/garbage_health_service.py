from __future__ import annotations

import json
import re
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services import proxy_pool_store
from services.account_token_profile import token_profile
from services.atomic_json_store import read_json, write_json
from services.config import DATA_DIR

from scripts import gpt_account_health_tool as tool

GARBAGE_HEALTH_FILE = DATA_DIR / "garbage_health_task.json"
TERMINAL_DEAD_STATUSES = {"banned", "token_invalidated", "unauthorized", "missing_token", "invalid", "dead", "refresh_token_invalid"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict[str, Any]:
    return {
        "input_dir": "",
        "output_dir": "",
        "proxy": "",
        "concurrency": 12,
        "timeout": 45,
        "backend": "auto",
        "refresh_tokens": True,
        "include_archives": True,
        "check_models": False,
        "check_quota": True,
        "keep_alive_only": True,
        "keep_with_quota_only": False,
        "realtime_import_sub2api": False,
        "sub2api_group_id": "",
        "auto_delete_dead": False,
        "limit": 0,
    }


def _default_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "config": _default_config(),
        "stats": {
            "total": 0,
            "checked": 0,
            "alive": 0,
            "failed": 0,
            "with_quota": 0,
            "kept_accounts": 0,
            "realtime_import_success": 0,
            "realtime_import_failed": 0,
            "realtime_import_skipped": 0,
            "deleted_dead_accounts": 0,
            "deleted_source_objects": 0,
            "delete_failed": 0,
            "delete_archive_skipped": 0,
            "running": 0,
            "elapsed_seconds": 0,
            "started_at": "",
            "updated_at": "",
            "status_counts": {},
            "json_documents": 0,
            "raw_account_objects": 0,
            "deduped_accounts": 0,
        },
        "logs": [],
        "results": [],
        "reports": {},
        "last_scan": {},
    }


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(float(str(value)))
    except Exception:
        number = default
    return max(minimum, min(maximum, number))


def _normalize_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _default_config()
    if isinstance(raw, dict):
        cfg.update({key: value for key, value in raw.items() if key in cfg})
    cfg["input_dir"] = str(cfg.get("input_dir") or "").strip()
    cfg["output_dir"] = str(cfg.get("output_dir") or "").strip()
    cfg["proxy"] = str(cfg.get("proxy") or "").strip()
    cfg["concurrency"] = _clamp_int(cfg.get("concurrency"), 12, 1, 64)
    cfg["timeout"] = _clamp_int(cfg.get("timeout"), 45, 5, 180)
    cfg["backend"] = str(cfg.get("backend") or "auto").strip().lower()
    if cfg["backend"] not in {"auto", "curl", "requests"}:
        cfg["backend"] = "auto"
    cfg["refresh_tokens"] = bool(cfg.get("refresh_tokens"))
    cfg["include_archives"] = bool(cfg.get("include_archives"))
    cfg["check_models"] = bool(cfg.get("check_models"))
    cfg["check_quota"] = bool(cfg.get("check_quota", True))
    cfg["keep_alive_only"] = bool(cfg.get("keep_alive_only", True))
    cfg["keep_with_quota_only"] = bool(cfg.get("keep_with_quota_only"))
    cfg["realtime_import_sub2api"] = bool(cfg.get("realtime_import_sub2api"))
    cfg["sub2api_group_id"] = str(cfg.get("sub2api_group_id") or "").strip()
    cfg["auto_delete_dead"] = bool(cfg.get("auto_delete_dead"))
    if cfg["realtime_import_sub2api"]:
        cfg["check_quota"] = True
    cfg["limit"] = _clamp_int(cfg.get("limit"), 0, 0, 200000)
    return cfg


def _redact_result(result: dict[str, Any], account: dict[str, Any] | None = None) -> dict[str, Any]:
    account = account or {}
    allowed = {
        "ok",
        "status",
        "status_code",
        "email",
        "identity",
        "plan_type",
        "chatgpt_account_id",
        "chatgpt_user_id",
        "latency_ms",
        "refreshed",
        "refresh_error",
        "checked_at",
        "token_sha",
        "error",
        "http_backend",
        "models_status_code",
        "models_ok",
        "available_model_count",
        "quota_status",
        "quota_used",
        "quota_total",
        "quota_remaining",
        "quota_reset_at",
        "quota_5h_used",
        "quota_5h_total",
        "quota_5h_remaining",
        "quota_5h_reset_at",
        "quota_7d_used",
        "quota_7d_total",
        "quota_7d_remaining",
        "quota_7d_reset_at",
        "quota_error_status",
        "proxy_source",
        "proxy_label",
        "sub2api_import_status",
        "sub2api_account_id",
        "sub2api_import_error",
        "sub2api_group_id",
        "sub2api_imported_at",
        "source_delete_status",
        "deleted_source_objects",
        "delete_error",
    }
    safe = {key: result.get(key) for key in allowed if key in result}
    safe["source_type"] = account.get("source_type") or result.get("source_type") or ""
    safe["sources"] = (account.get("sources") or result.get("sources") or [])[:5]
    return safe


def _retryable_proxy_result(result: dict[str, Any]) -> bool:
    statuses = {
        str(result.get("status") or "").strip().lower(),
        str(result.get("quota_error_status") or "").strip().lower(),
    }
    return bool(statuses & {"network_error", "proxy_error", "timeout", "cloudflare_blocked", "region_blocked", "blocked"})


def _json_path_tokens(path: str) -> list[str | int] | None:
    text = str(path or "").strip()
    if not text.startswith("$"):
        return None
    tokens: list[str | int] = []
    index = 1
    while index < len(text):
        if text[index] == ".":
            match = re.match(r"\.([^\.\[]+)", text[index:])
            if not match:
                return None
            tokens.append(match.group(1))
            index += len(match.group(0))
            continue
        if text[index] == "[":
            match = re.match(r"\[(\d+)\]", text[index:])
            if not match:
                return None
            tokens.append(int(match.group(1)))
            index += len(match.group(0))
            continue
        return None
    return tokens


def _remove_json_path(data: Any, tokens: list[str | int]) -> bool:
    if not tokens:
        return False
    parent = data
    for token in tokens[:-1]:
        if isinstance(token, int) and isinstance(parent, list) and 0 <= token < len(parent):
            parent = parent[token]
        elif isinstance(token, str) and isinstance(parent, dict) and token in parent:
            parent = parent[token]
        else:
            return False
    last = tokens[-1]
    if isinstance(last, int) and isinstance(parent, list) and 0 <= last < len(parent):
        parent.pop(last)
        return True
    if isinstance(last, str) and isinstance(parent, dict) and last in parent:
        parent.pop(last, None)
        return True
    return False


def select_local_directory(initial_dir: str = "") -> dict[str, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as error:
        raise RuntimeError(f"本机目录选择器不可用: {error}") from error

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update()
        initial = str(initial_dir or "").strip()
        if not initial or not Path(initial).expanduser().exists():
            initial = str(Path.home())
        selected = filedialog.askdirectory(parent=root, initialdir=initial, mustexist=True, title="选择账号扫描目录")
    except Exception as error:
        raise RuntimeError(f"打开本机目录选择器失败: {error}") from error
    finally:
        root.destroy()
    path = str(Path(selected).resolve()) if selected else ""
    return {"selected": bool(path), "path": path}


class GarbageHealthService:
    def __init__(self, state_file: Path):
        self._state_file = state_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state = self._load()
        if self._state.get("enabled"):
            self._state["enabled"] = False
            self._state.setdefault("stats", {})["running"] = 0
            self._save()

    def _load(self) -> dict[str, Any]:
        data = read_json(self._state_file, {}, expected_schema_version=1)
        if isinstance(data, dict):
            state = _default_state()
            state.update({key: value for key, value in data.items() if key in state})
            state["config"] = _normalize_config(data.get("config") if isinstance(data.get("config"), dict) else {})
            state["stats"] = {**_default_state()["stats"], **(data.get("stats") if isinstance(data.get("stats"), dict) else {})}
            state["logs"] = data.get("logs") if isinstance(data.get("logs"), list) else []
            state["results"] = data.get("results") if isinstance(data.get("results"), list) else []
            state["reports"] = data.get("reports") if isinstance(data.get("reports"), dict) else {}
            state["last_scan"] = data.get("last_scan") if isinstance(data.get("last_scan"), dict) else {}
            return state
        return _default_state()

    def _save(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._state_file, self._state, schema_version=1)

    def _append_log(self, text: str, level: str = "info") -> None:
        with self._lock:
            self._state.setdefault("logs", []).append({"time": _now(), "text": str(text), "level": level})
            self._state["logs"] = self._state["logs"][-800:]
            self._state.setdefault("stats", {})["updated_at"] = _now()
            self._save()

    def _bump_stats(self) -> None:
        stats = self._state.setdefault("stats", {})
        started_at = str(stats.get("started_at") or "")
        if started_at:
            try:
                elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
            except Exception:
                elapsed = 0.0
            stats["elapsed_seconds"] = round(elapsed, 1)
        checked = int(stats.get("checked") or 0)
        alive = int(stats.get("alive") or 0)
        total = int(stats.get("total") or 0)
        stats["failed"] = max(0, checked - alive)
        stats["running"] = max(0, total - checked) if self._state.get("enabled") else 0
        stats["updated_at"] = _now()

    def get(self) -> dict[str, Any]:
        with self._lock:
            snap = json.loads(json.dumps(self._state, ensure_ascii=False, default=str))
            runner_alive = bool(self._runner and self._runner.is_alive())
        snap["enabled"] = bool(snap.get("enabled")) and runner_alive
        if not snap["enabled"]:
            snap.setdefault("stats", {})["running"] = 0
        snap["config"] = _normalize_config(snap.get("config") if isinstance(snap.get("config"), dict) else {})
        return snap

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cfg = _normalize_config(self._state.get("config") if isinstance(self._state.get("config"), dict) else {})
            cfg.update({key: value for key, value in (updates or {}).items() if key in cfg})
            self._state["config"] = _normalize_config(cfg)
            self._save()
        return self.get()

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._state["logs"] = []
            self._state["results"] = []
            self._state["reports"] = {}
            self._state["last_scan"] = {}
            self._state["stats"] = {**_default_state()["stats"]}
            self._save()
        return self.get()

    def scan(self, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        cfg = _normalize_config({**(self._state.get("config") if isinstance(self._state.get("config"), dict) else {}), **(updates or {})})
        input_dir = str(cfg.get("input_dir") or "").strip()
        if not input_dir:
            raise RuntimeError("请先选择账号扫描目录")
        root = Path(input_dir).expanduser()
        if not root.is_dir():
            raise RuntimeError(f"账号扫描目录不存在: {root}")
        accounts, stats = tool.collect_accounts(root, include_archives=bool(cfg.get("include_archives")))
        limit = int(cfg.get("limit") or 0)
        selected = accounts[:limit] if limit > 0 else accounts
        scan = {
            "input_dir": str(root.resolve()),
            "json_documents": stats.get("json_documents", 0),
            "raw_account_objects": stats.get("raw_account_objects", 0),
            "deduped_accounts": stats.get("deduped_accounts", len(accounts)),
            "selected_accounts": len(selected),
            "skipped_files": len(stats.get("skipped_files") or []),
            "skipped_objects": len(stats.get("skipped_objects") or []),
            "scanned_at": _now(),
        }
        with self._lock:
            self._state["config"] = cfg
            self._state["last_scan"] = scan
            self._state.setdefault("stats", {}).update({
                "json_documents": scan["json_documents"],
                "raw_account_objects": scan["raw_account_objects"],
                "deduped_accounts": scan["deduped_accounts"],
                "total": scan["selected_accounts"],
                "updated_at": _now(),
            })
            self._save()
        return self.get()

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            self._state["enabled"] = False
            self._state.setdefault("stats", {})["running"] = 0
            self._save()
        self._append_log("捡垃圾测活任务已请求停止", "warning")
        return self.get()

    def start(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options if isinstance(options, dict) else {}
        if self._runner and self._runner.is_alive():
            self._append_log("捡垃圾测活任务已在运行", "warning")
            return self.get()
        cfg_updates = options.get("config") if isinstance(options.get("config"), dict) else {}
        cfg = _normalize_config({**(self._state.get("config") if isinstance(self._state.get("config"), dict) else {}), **cfg_updates})
        if cfg.get("realtime_import_sub2api"):
            from services import sub2api_store

            warehouse = sub2api_store.get_sub2api_config()
            if not warehouse.get("enabled"):
                raise RuntimeError("请先在“中转仓库”中启用并配置 sub2api")
            group_id = str(cfg.get("sub2api_group_id") or "").strip()
            if group_id:
                try:
                    if int(group_id) <= 0:
                        raise ValueError
                except ValueError as error:
                    raise RuntimeError("实时导入的 sub2api 分组 ID 无效") from error
        with self._lock:
            self._state["config"] = cfg
            if options.get("clear_results", True):
                self._state["logs"] = []
                self._state["results"] = []
                self._state["reports"] = {}
            self._state["enabled"] = True
            self._state["stats"] = {
                **_default_state()["stats"],
                "started_at": _now(),
                "updated_at": _now(),
            }
            self._save()
        self._stop_event.clear()
        self._runner = threading.Thread(target=self._run_main, args=(cfg,), daemon=True, name="garbage-health")
        self._runner.start()
        return self.get()

    @staticmethod
    def _sub2api_group_ids(cfg: dict[str, Any]) -> list[int]:
        raw = str(cfg.get("sub2api_group_id") or "").strip()
        if not raw:
            return []
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return []
        return [value] if value > 0 else []

    def _import_realtime_account(
        self,
        account: dict[str, Any],
        result: dict[str, Any],
        safe_result: dict[str, Any],
        cfg: dict[str, Any],
    ) -> None:
        from services import sub2api_store

        identity = str(account.get("email") or account.get("identity") or "-")
        group_ids = self._sub2api_group_ids(cfg)
        try:
            imported = sub2api_store.import_account_dicts_to_sub2api(account, group_ids=group_ids, allow_access_token_fallback=True)
            item = next((entry for entry in imported.get("results") or [] if isinstance(entry, dict)), {})
            account_id = next((value for value in imported.get("account_ids") or [] if value not in (None, "")), None)
            account_id = account_id or item.get("account_id")
            ok = bool(imported.get("ok"))
            error = str(imported.get("last_error") or item.get("error") or "")[:800]
        except Exception as exc:
            ok = False
            account_id = None
            error = tool.compact(exc, 800)
        updates = {
            "sub2api_import_status": "ok" if ok else "failed",
            "sub2api_account_id": account_id,
            "sub2api_import_error": error if not ok else "",
            "sub2api_group_id": group_ids[0] if group_ids else "",
            "sub2api_imported_at": _now(),
        }
        result.update(updates)
        safe_result.update({key: value for key, value in updates.items() if value not in (None, "")})
        with self._lock:
            stats = self._state.setdefault("stats", {})
            key = "realtime_import_success" if ok else "realtime_import_failed"
            stats[key] = int(stats.get(key) or 0) + 1
            self._bump_stats()
            self._save()
        target = f"分组 {group_ids[0]}" if group_ids else "默认分组"
        self._append_log(
            f"实时导入 sub2api {'成功' if ok else '失败'}：{identity} -> {target}" + (f"，账号ID={account_id}" if account_id else "") + (f"，原因={error}" if error else ""),
            "success" if ok else "warning",
        )

    def _delete_dead_sources(
        self,
        candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
        cfg: dict[str, Any],
    ) -> None:
        if not candidates:
            return
        root = Path(str(cfg.get("input_dir") or "")).expanduser().resolve()
        grouped: dict[Path, list[tuple[str, list[str | int]]]] = {}
        outcomes: dict[str, dict[str, Any]] = {}

        def outcome(identity: str) -> dict[str, Any]:
            return outcomes.setdefault(identity, {"removed": 0, "skipped": 0, "failed": 0, "errors": []})

        for account, _result, _safe in candidates:
            identity = str(account.get("identity") or account.get("email") or "")
            for source in account.get("sources") or []:
                source_name = str((source or {}).get("source") or "").strip()
                source_path = str((source or {}).get("path") or "$").strip()
                if not source_name:
                    outcome(identity)["failed"] += 1
                    continue
                if "!" in source_name:
                    outcome(identity)["skipped"] += 1
                    continue
                tokens = _json_path_tokens(source_path)
                if tokens is None:
                    outcome(identity)["failed"] += 1
                    outcome(identity)["errors"].append(f"unsupported_path:{source_path}")
                    continue
                path = (root / source_name).resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    outcome(identity)["failed"] += 1
                    outcome(identity)["errors"].append("source_outside_input_dir")
                    continue
                grouped.setdefault(path, []).append((identity, tokens))

        for path, entries in grouped.items():
            if not path.exists() or path.suffix.lower() != ".json":
                for identity, _tokens in entries:
                    outcome(identity)["failed"] += 1
                    outcome(identity)["errors"].append(f"source_missing:{path.name}")
                continue
            if any(not tokens for _identity, tokens in entries):
                try:
                    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
                    path.unlink()
                    for identity, _tokens in entries:
                        outcome(identity)["removed"] += 1
                except Exception as exc:
                    for identity, _tokens in entries:
                        outcome(identity)["failed"] += 1
                        outcome(identity)["errors"].append(tool.compact(exc, 300))
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                for identity, _tokens in entries:
                    outcome(identity)["failed"] += 1
                    outcome(identity)["errors"].append(tool.compact(exc, 300))
                continue

            token_owners: dict[tuple[str | int, ...], list[str]] = {}
            for identity, tokens in entries:
                token_owners.setdefault(tuple(tokens), []).append(identity)

            def deletion_order(tokens: tuple[str | int, ...]) -> tuple[int, str, int]:
                last_index = tokens[-1] if tokens and isinstance(tokens[-1], int) else -1
                return len(tokens), json.dumps(tokens[:-1], ensure_ascii=False), int(last_index)

            changed = False
            for tokens, identities in sorted(token_owners.items(), key=lambda item: deletion_order(item[0]), reverse=True):
                if _remove_json_path(data, list(tokens)):
                    changed = True
                    for identity in identities:
                        outcome(identity)["removed"] += 1
                else:
                    for identity in identities:
                        outcome(identity)["failed"] += 1
                        outcome(identity)["errors"].append(f"path_not_found:{tokens}")
            if changed:
                try:
                    write_json(path, data, backup=True)
                except Exception as exc:
                    for identity, _tokens in entries:
                        outcome(identity)["failed"] += 1
                        outcome(identity)["errors"].append(tool.compact(exc, 300))

        deleted_accounts = 0
        deleted_objects = 0
        failed_accounts = 0
        archive_skipped = 0
        for account, result, safe_result in candidates:
            identity = str(account.get("identity") or account.get("email") or "")
            item = outcome(identity)
            removed = int(item.get("removed") or 0)
            failed = int(item.get("failed") or 0)
            skipped = int(item.get("skipped") or 0)
            deleted_objects += removed
            archive_skipped += skipped
            if removed:
                deleted_accounts += 1
            if failed:
                failed_accounts += 1
            status = "deleted" if removed and not failed and not skipped else "partial" if removed else "skipped" if skipped and not failed else "failed"
            updates = {
                "source_delete_status": status,
                "deleted_source_objects": removed,
                "delete_error": "; ".join(item.get("errors") or [])[:800],
            }
            result.update(updates)
            safe_result.update({key: value for key, value in updates.items() if value not in (None, "")})
        with self._lock:
            stats = self._state.setdefault("stats", {})
            stats["deleted_dead_accounts"] = deleted_accounts
            stats["deleted_source_objects"] = deleted_objects
            stats["delete_failed"] = failed_accounts
            stats["delete_archive_skipped"] = archive_skipped
            self._save()
        self._append_log(
            f"自动删除失效账号完成：账号={deleted_accounts}，源对象={deleted_objects}，失败={failed_accounts}，归档跳过={archive_skipped}",
            "success" if deleted_accounts and not failed_accounts else "warning",
        )

    def _run_main(self, cfg: dict[str, Any]) -> None:
        full_accounts: list[dict[str, Any]] = []
        full_results: list[dict[str, Any]] = []
        try:
            input_dir = str(cfg.get("input_dir") or "").strip()
            if not input_dir:
                raise RuntimeError("请先选择账号扫描目录")
            root = Path(input_dir).expanduser()
            if not root.is_dir():
                raise RuntimeError(f"账号扫描目录不存在: {root}")
            self._append_log(f"开始扫描：{root}", "info")
            accounts, parse_stats = tool.collect_accounts(root, include_archives=bool(cfg.get("include_archives")))
            limit = int(cfg.get("limit") or 0)
            if limit > 0:
                accounts = accounts[:limit]
            full_accounts = accounts
            total = len(accounts)
            with self._lock:
                stats = self._state.setdefault("stats", {})
                stats.update({
                    "total": total,
                    "running": total,
                    "json_documents": parse_stats.get("json_documents", 0),
                    "raw_account_objects": parse_stats.get("raw_account_objects", 0),
                    "deduped_accounts": parse_stats.get("deduped_accounts", total),
                    "updated_at": _now(),
                })
                self._state["last_scan"] = {
                    "input_dir": str(root.resolve()),
                    "json_documents": stats["json_documents"],
                    "raw_account_objects": stats["raw_account_objects"],
                    "deduped_accounts": stats["deduped_accounts"],
                    "selected_accounts": total,
                    "skipped_files": len(parse_stats.get("skipped_files") or []),
                    "skipped_objects": len(parse_stats.get("skipped_objects") or []),
                    "scanned_at": _now(),
                }
                self._save()
            if not accounts:
                self._append_log("没有扫描到可测账号", "warning")
                return
            self._append_log(f"扫描完成：JSON文档={parse_stats.get('json_documents', 0)}，账号对象={parse_stats.get('raw_account_objects', 0)}，去重后={parse_stats.get('deduped_accounts', total)}，本次测活={total}", "success")
            self._execute_checks(accounts, cfg, full_results)
            if self._stop_event.is_set():
                self._append_log("任务已停止，写出已完成部分报告", "warning")
            self._write_final_reports(cfg, full_accounts, full_results, parse_stats)
        except Exception as exc:
            self._append_log(f"捡垃圾任务失败：{tool.compact(exc, 600)}", "danger")
        finally:
            with self._lock:
                self._state["enabled"] = False
                self._bump_stats()
                self._state.setdefault("stats", {})["running"] = 0
                self._save()

    def _execute_checks(self, accounts: list[dict[str, Any]], cfg: dict[str, Any], full_results: list[dict[str, Any]]) -> None:
        concurrency = int(cfg.get("concurrency") or 12)
        timeout = int(cfg.get("timeout") or 45)
        backend = str(cfg.get("backend") or "auto")
        refresh = bool(cfg.get("refresh_tokens"))
        check_models = bool(cfg.get("check_models"))
        check_quota = bool(cfg.get("check_quota", True))
        realtime_import = bool(cfg.get("realtime_import_sub2api"))
        auto_delete_dead = bool(cfg.get("auto_delete_dead"))
        status_counts: dict[str, int] = {}
        alive = 0
        with_quota = 0
        checked = 0
        dead_candidates: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        account_by_identity = {str(item.get("identity") or ""): item for item in accounts}
        group_text = str(cfg.get("sub2api_group_id") or "默认").strip()
        proxy_text = "按任务从代理配置节点池分配"
        self._append_log(f"测活代理：{proxy_text}；额度检查={'是' if check_quota else '否'}；实时导入={'是，分组=' + group_text if realtime_import else '否'}；自动删除确认失效={'是' if auto_delete_dead else '否'}；保留存活={'是' if cfg.get('keep_alive_only', True) else '否'}；只保留有额度={'是' if cfg.get('keep_with_quota_only') else '否'}", "info")

        def run_check(index: int, account: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            email = str(account.get("email") or "").strip().lower()
            if email.endswith(".invalid"):
                return {
                    "ok": False,
                    "status": "invalid",
                    "status_code": 0,
                    "email": account.get("email"),
                    "identity": account.get("identity"),
                    "error": "保留域 .invalid 测试账号，本地判定为失效",
                    "checked_at": _now(),
                    "latency_ms": 0,
                }, {"proxy": "", "source": "local", "label": "本地规则"}
            assignment: dict[str, Any] = {}
            result: dict[str, Any] = {}
            for attempt in range(1, 3):
                assignment = proxy_pool_store.assign_proxy(
                    index + (attempt - 1) * max(1, len(accounts)),
                    fallback="",
                    total_threads=concurrency,
                    slot_index=(index - 1) % concurrency,
                    purpose="garbage_health",
                    account=None,
                    validate_node=False,
                )
                result = tool.health_check(
                    account,
                    proxy=str(assignment.get("proxy") or ""),
                    timeout=timeout,
                    refresh=refresh,
                    check_models=check_models,
                    check_quota=check_quota,
                    backend=backend,
                )
                if attempt >= 2 or str(assignment.get("source") or "") != "managed_mihomo" or not _retryable_proxy_result(result):
                    break
                reason = result.get("quota_error") or result.get("error") or result.get("status") or "proxy request failed"
                try:
                    proxy_pool_store.report_proxy_failure(assignment, reason)
                except Exception:
                    pass
                self._append_log(
                    f"{account.get('email') or account.get('identity') or '-'} 代理失败，已隔离节点 {assignment.get('label') or '-'}，切换节点重试 {attempt + 1}/2",
                    "warning",
                )
            return result, assignment

        def submit(executor: ThreadPoolExecutor, index: int, account: dict[str, Any]):
            return executor.submit(run_check, index, account)

        iterator = iter(accounts)
        next_index = 1
        import_executor = ThreadPoolExecutor(max_workers=min(2, max(1, concurrency)), thread_name_prefix="garbage-import") if realtime_import else None
        try:
            with ThreadPoolExecutor(max_workers=min(concurrency, len(accounts)), thread_name_prefix="garbage-health") as executor:
                pending = {}
                for _ in range(min(concurrency, len(accounts))):
                    try:
                        account = next(iterator)
                    except StopIteration:
                        break
                    future = submit(executor, next_index, account)
                    pending[future] = account
                    next_index += 1
                while pending:
                    if self._stop_event.is_set():
                        for future in pending:
                            future.cancel()
                        break
                    done, _ = wait(pending.keys(), timeout=1, return_when=FIRST_COMPLETED)
                    if not done:
                        continue
                    for future in done:
                        account = pending.pop(future)
                        assignment: dict[str, Any] = {}
                        try:
                            result, assignment = future.result()
                        except Exception as exc:
                            result = {
                                "ok": False,
                                "status": tool.classify_failure(0, tool.compact(exc, 800)),
                                "status_code": 0,
                                "email": account.get("email"),
                                "identity": account.get("identity"),
                                "error": tool.compact(exc, 800),
                                "checked_at": _now(),
                            }
                        result["proxy_source"] = assignment.get("source") or ""
                        result["proxy_label"] = assignment.get("label") or ""
                        quota_available = tool.has_available_quota(result)
                        import_compatible = False
                        if realtime_import and quota_available:
                            profile = token_profile(account)
                            import_compatible = bool(profile.get("oauth_capable")) or bool(str(account.get("access_token") or "").strip())
                            result["sub2api_import_status"] = "queued" if import_compatible else "skipped_incompatible"
                            result["sub2api_group_id"] = str(cfg.get("sub2api_group_id") or "")
                            if not import_compatible:
                                result["sub2api_import_error"] = "缺少可导入的 access_token"
                        full_results.append(result)
                        safe_result = _redact_result(result, account_by_identity.get(str(result.get("identity") or "")) or account)
                        checked += 1
                        status = str(result.get("status") or "unknown")
                        status_counts[status] = status_counts.get(status, 0) + 1
                        if result.get("ok"):
                            alive += 1
                        if quota_available:
                            with_quota += 1
                        if auto_delete_dead and status.lower() in TERMINAL_DEAD_STATUSES:
                            dead_candidates.append((account, result, safe_result))
                        with self._lock:
                            self._state.setdefault("results", []).append(safe_result)
                            self._state["results"] = self._state["results"][-5000:]
                            stats = self._state.setdefault("stats", {})
                            stats["checked"] = checked
                            stats["alive"] = alive
                            stats["with_quota"] = with_quota
                            if realtime_import and quota_available and not import_compatible:
                                stats["realtime_import_skipped"] = int(stats.get("realtime_import_skipped") or 0) + 1
                            stats["failed"] = max(0, checked - alive)
                            stats["status_counts"] = dict(status_counts)
                            self._bump_stats()
                            self._save()
                        if import_executor is not None and quota_available and import_compatible:
                            import_executor.submit(self._import_realtime_account, account, result, safe_result, cfg)
                        if checked == 1 or checked % 20 == 0 or checked == len(accounts):
                            self._append_log(f"进度 {checked}/{len(accounts)}，存活={alive}，状态={status_counts}", "success" if alive else "info")
                        if not self._stop_event.is_set():
                            try:
                                next_account = next(iterator)
                                future = submit(executor, next_index, next_account)
                                pending[future] = next_account
                                next_index += 1
                            except StopIteration:
                                pass
        finally:
            if import_executor is not None:
                import_executor.shutdown(wait=True, cancel_futures=False)
        if auto_delete_dead:
            self._delete_dead_sources(dead_candidates, cfg)

    def _write_final_reports(self, cfg: dict[str, Any], accounts: list[dict[str, Any]], results: list[dict[str, Any]], parse_stats: dict[str, Any]) -> None:
        if not results:
            return
        root = Path(str(cfg.get("input_dir") or "")).expanduser()
        if str(cfg.get("output_dir") or "").strip():
            out_dir = Path(str(cfg.get("output_dir"))).expanduser()
        else:
            out_dir = root / "health_reports" / f"gpt_health_ui_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        stats = {
            **parse_stats,
            "checked_accounts": len(results),
            "keep_alive_only": bool(cfg.get("keep_alive_only", True)),
            "keep_with_quota_only": bool(cfg.get("keep_with_quota_only")),
            "finished_at": _now(),
        }
        reports = tool.write_reports(out_dir, accounts, results, stats)
        with self._lock:
            self._state["reports"] = reports
            status_counts = {}
            for item in results:
                status = str(item.get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            self._state.setdefault("stats", {}).update({
                "checked": len(results),
                "alive": int(stats.get("alive") or 0),
                "failed": int(stats.get("failed") or 0),
                "kept_accounts": int(stats.get("kept_accounts") or 0),
                "with_quota": int(stats.get("with_quota") or 0),
                "status_counts": status_counts,
                "updated_at": _now(),
            })
            self._save()
        self._append_log(f"测活完成：存活={stats.get('alive', 0)}，失败={stats.get('failed', 0)}，报告={out_dir}", "success")


    def _load_export_accounts(self) -> list[dict[str, Any]]:
        reports = self._state.get("reports") if isinstance(self._state.get("reports"), dict) else {}
        path_text = str(reports.get("alive_accounts_json") or "").strip()
        if not path_text:
            raise RuntimeError("no filtered export result yet; run garbage health first")
        path = Path(path_text)
        if not path.exists():
            raise RuntimeError(f"filtered export file not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise RuntimeError("filtered export file is not an account list")
        return [item for item in data if isinstance(item, dict)]

    def export_data(self, export_format: str = "sub2api") -> dict[str, Any]:
        accounts = self._load_export_accounts()
        fmt = str(export_format or "sub2api").strip().lower()
        if fmt in {"sub2api", "sub2api-json"}:
            data: Any = tool.build_sub2api_document(accounts)
            filename = "garbage-alive-sub2api.json"
        elif fmt in {"cpa", "cpa-json"}:
            data = tool.build_cpa_document(accounts)
            filename = "garbage-alive-cpa.json"
        elif fmt in {"full", "accounts", "raw"}:
            data = accounts
            filename = "garbage-alive-accounts.full.json"
        else:
            raise RuntimeError(f"unsupported export format: {export_format}")
        return {"format": fmt, "count": len(accounts), "filename": filename, "data": data}

    def import_to_sub2api(self, group_id: int | str | None = None) -> dict[str, Any]:
        accounts = self._load_export_accounts()
        from services import sub2api_store

        selected_group = str(group_id if group_id not in (None, "") else (self._state.get("config") or {}).get("sub2api_group_id") or "").strip()
        group_ids: list[int] = []
        if selected_group:
            try:
                value = int(selected_group)
            except (TypeError, ValueError) as error:
                raise RuntimeError("sub2api 分组 ID 无效") from error
            if value > 0:
                group_ids = [value]
        result = sub2api_store.import_account_dicts_to_sub2api(accounts, group_ids=group_ids, allow_access_token_fallback=True)
        return {"count": len(accounts), "sub2api": result}

    def import_to_cpa_inventory(self) -> dict[str, Any]:
        accounts = self._load_export_accounts()
        from services import cpa_store

        result = cpa_store.import_account_dicts_to_cpa(accounts)
        return {"count": len(accounts), "cpa": result}


garbage_health_service = GarbageHealthService(GARBAGE_HEALTH_FILE)
