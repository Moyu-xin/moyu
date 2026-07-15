from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import psutil

from services.atomic_json_store import read_json, write_json
from services.config import BASE_DIR, DATA_DIR


SYSTEM_SETTINGS_FILE = DATA_DIR / "system_settings.json"
DEFAULT_SYSTEM_SETTINGS = {
    "poll_interval_seconds": 3,
    "memory_warning_percent": 80,
    "memory_baseline_mb": 220,
    "memory_per_worker_mb": 96,
}


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(minimum, min(maximum, number))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_remote_url(value: str) -> str:
    remote = str(value or "").strip()
    remote = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", remote, flags=re.IGNORECASE)
    remote = re.sub(r"^(?:[^@/\s]+)@([^:]+:)", r"***@\1", remote)
    return remote[:500]


class SystemMaintenanceService:
    CLEANUP_CATEGORIES = ("logs", "python_cache", "frontend_cache")

    def __init__(self, root: Path = BASE_DIR, settings_file: Path = SYSTEM_SETTINGS_FILE):
        self.root = Path(root).resolve()
        self.settings_file = Path(settings_file)
        self._lock = threading.RLock()
        self._git_lock = threading.Lock()
        self._history: deque[dict[str, object]] = deque(maxlen=120)
        self._process = psutil.Process(os.getpid())

    def get_settings(self) -> dict[str, int]:
        raw = read_json(self.settings_file, {}, expected_schema_version=1)
        source = raw if isinstance(raw, dict) else {}
        return {
            "poll_interval_seconds": _bounded_int(
                source.get("poll_interval_seconds"),
                int(DEFAULT_SYSTEM_SETTINGS["poll_interval_seconds"]),
                2,
                30,
            ),
            "memory_warning_percent": _bounded_int(
                source.get("memory_warning_percent"),
                int(DEFAULT_SYSTEM_SETTINGS["memory_warning_percent"]),
                50,
                98,
            ),
            "memory_baseline_mb": _bounded_int(
                source.get("memory_baseline_mb"),
                int(DEFAULT_SYSTEM_SETTINGS["memory_baseline_mb"]),
                32,
                8192,
            ),
            "memory_per_worker_mb": _bounded_int(
                source.get("memory_per_worker_mb"),
                int(DEFAULT_SYSTEM_SETTINGS["memory_per_worker_mb"]),
                8,
                2048,
            ),
        }

    def update_settings(self, updates: dict[str, object]) -> dict[str, int]:
        with self._lock:
            merged: dict[str, object] = {**self.get_settings(), **dict(updates or {})}
            normalized = {
                "poll_interval_seconds": _bounded_int(merged.get("poll_interval_seconds"), 3, 2, 30),
                "memory_warning_percent": _bounded_int(merged.get("memory_warning_percent"), 80, 50, 98),
                "memory_baseline_mb": _bounded_int(merged.get("memory_baseline_mb"), 220, 32, 8192),
                "memory_per_worker_mb": _bounded_int(merged.get("memory_per_worker_mb"), 96, 8, 2048),
            }
            write_json(self.settings_file, normalized, schema_version=1)
            return normalized

    def reset_settings(self) -> dict[str, int]:
        with self._lock:
            normalized = {key: int(value) for key, value in DEFAULT_SYSTEM_SETTINGS.items()}
            write_json(self.settings_file, normalized, schema_version=1)
            self._history.clear()
            return normalized

    @staticmethod
    def _registration_threads() -> int:
        try:
            from services.register_service import register_service

            return _bounded_int(register_service.get().get("threads"), 1, 1, 64)
        except Exception:
            return 1

    def memory_snapshot(self, registration_threads: int | None = None) -> dict[str, object]:
        settings = self.get_settings()
        system_memory = psutil.virtual_memory()
        process_memory = self._process.memory_info()
        rss_mb = process_memory.rss / (1024 * 1024)
        vms_mb = process_memory.vms / (1024 * 1024)
        threads = _bounded_int(
            registration_threads if registration_threads is not None else self._registration_threads(),
            1,
            1,
            64,
        )
        predicted_peak_mb = max(
            rss_mb,
            float(settings["memory_baseline_mb"] + settings["memory_per_worker_mb"] * threads),
        )
        additional_mb = max(0.0, predicted_peak_mb - rss_mb)
        projected_used = min(float(system_memory.total), float(system_memory.used) + additional_mb * 1024 * 1024)
        projected_percent = (projected_used / float(system_memory.total) * 100) if system_memory.total else 0.0
        warning_percent = float(settings["memory_warning_percent"])
        risk = "high" if projected_percent >= warning_percent else "medium" if projected_percent >= warning_percent - 10 else "low"

        sample: dict[str, object] = {
            "timestamp": _utc_now(),
            "process": {
                "rss_mb": round(rss_mb, 1),
                "vms_mb": round(vms_mb, 1),
                "cpu_percent": round(float(self._process.cpu_percent(interval=None)), 1),
                "threads": int(self._process.num_threads()),
                "uptime_seconds": max(0, int(datetime.now().timestamp() - self._process.create_time())),
            },
            "system": {
                "total_mb": round(system_memory.total / (1024 * 1024), 1),
                "available_mb": round(system_memory.available / (1024 * 1024), 1),
                "used_mb": round(system_memory.used / (1024 * 1024), 1),
                "percent": round(float(system_memory.percent), 1),
            },
            "prediction": {
                "registration_threads": threads,
                "predicted_peak_mb": round(predicted_peak_mb, 1),
                "projected_system_percent": round(projected_percent, 1),
                "risk": risk,
                "model": "baseline + registration_threads * per_worker",
            },
        }
        with self._lock:
            self._history.append(sample)
            history = list(self._history)
        return {**sample, "history": history}

    def _is_within_root(self, path: Path) -> bool:
        try:
            Path(path).resolve(strict=False).relative_to(self.root)
            return True
        except (OSError, ValueError):
            return False

    def _python_cache_targets(self) -> list[Path]:
        targets: list[Path] = []
        for relative in ("api", "services", "scripts", "utils", "tests"):
            base = self.root / relative
            if not base.is_dir() or base.is_symlink():
                continue
            for current, directories, _files in os.walk(base, topdown=True, followlinks=False):
                current_path = Path(current)
                kept: list[str] = []
                for name in directories:
                    candidate = current_path / name
                    if candidate.is_symlink():
                        continue
                    if name == "__pycache__":
                        targets.append(candidate)
                    else:
                        kept.append(name)
                directories[:] = kept
        for relative in (".pytest_cache", ".mypy_cache", ".ruff_cache"):
            candidate = self.root / relative
            if candidate.exists() and not candidate.is_symlink():
                targets.append(candidate)
        return targets

    def _category_targets(self, category: str) -> list[Path]:
        if category == "logs":
            log_root = Path(os.getenv("CHATGPT2API_LOG_DIR") or (self.root / "logs")).resolve()
            if not self._is_within_root(log_root):
                return []
            if not log_root.is_dir() or log_root.is_symlink():
                return []
            files: list[Path] = []
            for current, directories, names in os.walk(log_root, topdown=True, followlinks=False):
                current_path = Path(current)
                directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
                files.extend(current_path / name for name in names if not (current_path / name).is_symlink())
            return files
        if category == "python_cache":
            return self._python_cache_targets()
        if category == "frontend_cache":
            return [
                candidate
                for candidate in (
                    self.root / "web" / ".next",
                    self.root / "web" / "out",
                    self.root / "web" / "tsconfig.tsbuildinfo",
                )
                if candidate.exists() and not candidate.is_symlink()
            ]
        raise ValueError(f"Unsupported cleanup category: {category}")

    def _iter_regular_files(self, target: Path) -> Iterable[Path]:
        if target.is_symlink() or not self._is_within_root(target):
            return []
        if target.is_file():
            return [target]
        files: list[Path] = []
        if target.is_dir():
            for current, directories, names in os.walk(target, topdown=True, followlinks=False):
                current_path = Path(current)
                directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
                files.extend(
                    current_path / name
                    for name in names
                    if not (current_path / name).is_symlink() and self._is_within_root(current_path / name)
                )
        return files

    def cleanup_status(self) -> dict[str, object]:
        categories: dict[str, dict[str, int]] = {}
        total_bytes = 0
        total_files = 0
        for category in self.CLEANUP_CATEGORIES:
            category_bytes = 0
            category_files = 0
            targets = self._category_targets(category)
            for target in targets:
                for file_path in self._iter_regular_files(target):
                    try:
                        category_bytes += file_path.stat().st_size
                        category_files += 1
                    except OSError:
                        continue
            categories[category] = {
                "bytes": category_bytes,
                "files": category_files,
                "targets": len(targets),
            }
            total_bytes += category_bytes
            total_files += category_files
        return {"categories": categories, "total_bytes": total_bytes, "total_files": total_files}

    def cleanup(self, categories: list[str], dry_run: bool = False) -> dict[str, object]:
        requested = list(dict.fromkeys(str(item or "").strip() for item in categories))
        invalid = [item for item in requested if item not in self.CLEANUP_CATEGORIES]
        if invalid:
            raise ValueError(f"Unsupported cleanup categories: {', '.join(invalid)}")
        if not requested:
            raise ValueError("Select at least one cleanup category")

        removed_files = 0
        removed_bytes = 0
        errors: list[str] = []
        with self._lock:
            for category in requested:
                for target in self._category_targets(category):
                    if target.is_symlink() or not self._is_within_root(target):
                        errors.append(f"Skipped unsafe path: {target.name}")
                        continue
                    files = list(self._iter_regular_files(target))
                    size = 0
                    for file_path in files:
                        try:
                            size += file_path.stat().st_size
                        except OSError:
                            pass
                    if dry_run:
                        removed_files += len(files)
                        removed_bytes += size
                        continue
                    try:
                        if category == "logs" and target.is_file():
                            target.write_bytes(b"")
                        elif target.is_dir():
                            shutil.rmtree(target)
                        elif target.exists():
                            target.unlink()
                        removed_files += len(files)
                        removed_bytes += size
                    except OSError as exc:
                        errors.append(f"{target.name}: {exc}")

        return {
            "ok": not errors,
            "dry_run": bool(dry_run),
            "categories": requested,
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
            "errors": errors[:20],
            "remaining": self.cleanup_status(),
        }

    def _run_git(self, arguments: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                shell=False,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Git is not installed or not available on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Git command timed out after {timeout} seconds") from exc

    @staticmethod
    def _git_output(result: subprocess.CompletedProcess[str]) -> str:
        return _redact_remote_url((result.stdout or result.stderr or "").strip())[:4000]

    def git_status(self) -> dict[str, object]:
        try:
            inside = self._run_git(["rev-parse", "--is-inside-work-tree"], timeout=10)
        except RuntimeError as exc:
            return {"is_repo": False, "update_supported": False, "reason": str(exc)}
        if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
            return {
                "is_repo": False,
                "update_supported": False,
                "reason": "Current directory is not a Git working tree",
            }

        branch_result = self._run_git(["branch", "--show-current"], timeout=10)
        branch = branch_result.stdout.strip()
        commit_result = self._run_git(["rev-parse", "--short", "HEAD"], timeout=10)
        commit = commit_result.stdout.strip() if commit_result.returncode == 0 else ""
        remote_result = self._run_git(["remote", "get-url", "origin"], timeout=10)
        remote = _redact_remote_url(remote_result.stdout) if remote_result.returncode == 0 else ""
        status_result = self._run_git(["status", "--porcelain", "--untracked-files=normal"], timeout=20)
        dirty_lines = [line for line in status_result.stdout.splitlines() if line.strip()]
        upstream_result = self._run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=10)
        upstream = upstream_result.stdout.strip() if upstream_result.returncode == 0 else ""
        ahead = 0
        behind = 0
        if upstream:
            counts = self._run_git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], timeout=20)
            if counts.returncode == 0:
                parts = counts.stdout.strip().split()
                if len(parts) == 2:
                    ahead, behind = int(parts[0]), int(parts[1])

        reason = ""
        if not branch:
            reason = "Detached HEAD or no initial commit"
        elif not remote:
            reason = "Origin remote is not configured"
        elif not upstream:
            reason = "Current branch has no upstream tracking branch"
        elif dirty_lines:
            reason = "Working tree has uncommitted changes"
        update_supported = not reason
        return {
            "is_repo": True,
            "branch": branch,
            "commit": commit,
            "remote": remote,
            "upstream": upstream,
            "dirty": bool(dirty_lines),
            "dirty_count": len(dirty_lines),
            "ahead": ahead,
            "behind": behind,
            "update_available": behind > 0,
            "update_supported": update_supported,
            "reason": reason,
        }

    def git_check(self) -> dict[str, object]:
        with self._git_lock:
            status = self.git_status()
            if not status.get("is_repo") or not status.get("remote"):
                return status
            fetched = self._run_git(["fetch", "--prune", "origin"], timeout=90)
            if fetched.returncode != 0:
                raise RuntimeError(self._git_output(fetched) or "Git fetch failed")
            return self.git_status()

    def git_update(self) -> dict[str, object]:
        with self._git_lock:
            before = self.git_status()
            if not before.get("update_supported"):
                raise RuntimeError(str(before.get("reason") or "Git update is not available"))
            fetched = self._run_git(["fetch", "--prune", "origin"], timeout=90)
            if fetched.returncode != 0:
                raise RuntimeError(self._git_output(fetched) or "Git fetch failed")
            checked = self.git_status()
            if not checked.get("update_supported"):
                raise RuntimeError(str(checked.get("reason") or "Git update is not available"))
            if int(checked.get("behind") or 0) <= 0:
                return {"ok": True, "updated": False, "restart_required": False, "git": checked}
            merged = self._run_git(["merge", "--ff-only", str(checked["upstream"])], timeout=90)
            if merged.returncode != 0:
                raise RuntimeError(self._git_output(merged) or "Fast-forward update failed")
            return {"ok": True, "updated": True, "restart_required": True, "git": self.git_status()}

    def restart_status(self) -> dict[str, object]:
        if str(os.getenv("CHATGPT2API_DEPLOYMENT_MODE") or "").strip().lower() == "docker":
            return {"supported": True, "mode": "docker-supervisor", "reason": ""}
        script = self.root / "scripts" / "restart_service.py"
        pid_file = self.root / "data" / "chatgpt2api-local.pid"
        managed_process = False
        try:
            import json

            record = json.loads(pid_file.read_text(encoding="utf-8-sig"))
            managed_process = int(record.get("pid") or 0) == os.getpid()
        except (OSError, TypeError, ValueError):
            managed_process = False
        supported = os.name == "nt" and script.is_file() and managed_process
        return {
            "supported": supported,
            "mode": "windows-local" if supported else "external-supervisor",
            "reason": "" if supported else "Restart this deployment with its process manager",
        }

    def schedule_restart(self) -> dict[str, object]:
        status = self.restart_status()
        if not status["supported"]:
            raise RuntimeError(str(status["reason"]))
        if status["mode"] == "docker-supervisor":
            def delayed_exit() -> None:
                time.sleep(2)
                os._exit(0)

            threading.Thread(target=delayed_exit, name="system-restart", daemon=True).start()
            return {"ok": True, "scheduled": True, "delay_seconds": 2}
        script = self.root / "scripts" / "restart_service.py"
        creation_flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        subprocess.Popen(
            [sys.executable, str(script), str(self.root)],
            cwd=str(self.root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
        return {"ok": True, "scheduled": True, "delay_seconds": 2}


system_maintenance_service = SystemMaintenanceService()
