from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from services.config import DATA_DIR


class GrokSolverManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._started_at = 0.0
        self._last_error = ""
        self._browser_type = "camoufox"

    @staticmethod
    def _pid_file() -> Path:
        return DATA_DIR / "grok_solver" / "solver.pid"

    def _write_pid(self, process: subprocess.Popen[bytes], endpoint: str) -> None:
        path = self._pid_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": process.pid, "url": endpoint, "started_at": self._started_at}),
            encoding="utf-8",
        )

    def _clear_pid(self) -> None:
        try:
            self._pid_file().unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _endpoint(url: str) -> tuple[str, int, str]:
        value = str(url or "http://127.0.0.1:5072").strip().rstrip("/")
        parsed = urlparse(value)
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or 5072)
        return host, port, value

    @staticmethod
    def _alive(url: str, timeout: float = 0.2) -> bool:
        try:
            with urllib.request.urlopen(str(url).rstrip("/") + "/", timeout=timeout) as response:
                return 200 <= int(response.status or 0) < 500
        except Exception:
            return False

    @staticmethod
    def _health(url: str, timeout: float = 0.5) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(str(url).rstrip("/") + "/health", timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
                return data if isinstance(data, dict) else None
        except urllib.error.HTTPError as error:
            try:
                data = json.loads(error.read().decode("utf-8", errors="replace"))
                return data if isinstance(data, dict) else None
            except Exception:
                return None
        except Exception:
            return None

    @staticmethod
    def _solver_script() -> Path:
        return Path(__file__).resolve().parent / "turnstile_solver" / "api_solver.py"

    def _listener_info(self, url: str) -> dict[str, Any]:
        host, port, _endpoint = self._endpoint(url)
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return {}
        try:
            import psutil

            connection = next(
                (
                    item
                    for item in psutil.net_connections(kind="inet")
                    if item.status == psutil.CONN_LISTEN and item.laddr and int(item.laddr.port) == port
                ),
                None,
            )
            if not connection or not connection.pid:
                return {}
            process = psutil.Process(int(connection.pid))
            command = [str(item) for item in process.cmdline()]
            expected = str(self._solver_script()).replace("\\", "/").lower()
            normalized = [item.replace("\\", "/").lower() for item in command]
            verified = any(item == expected or item.endswith("/services/grok/turnstile_solver/api_solver.py") for item in normalized)
            threads = None
            if "--thread" in command:
                index = command.index("--thread")
                if index + 1 < len(command):
                    try:
                        threads = int(command[index + 1])
                    except (TypeError, ValueError):
                        threads = None
            return {
                "listener_pid": int(connection.pid),
                "listener_verified": verified,
                "actual_threads": threads,
                "command": command if verified else [],
            }
        except Exception:
            return {}

    @staticmethod
    def _terminate_tree(pid: int) -> None:
        import psutil

        process = psutil.Process(int(pid))
        targets = [*process.children(recursive=True), process]
        for item in reversed(targets):
            try:
                item.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        _gone, alive = psutil.wait_procs(targets, timeout=8)
        for item in alive:
            try:
                item.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def status(self, url: str = "http://127.0.0.1:5072") -> dict[str, Any]:
        with self._lock:
            process = self._process
            running = bool(process and process.poll() is None)
            pid = int(process.pid) if running and process else None
            started_at = self._started_at
            error = self._last_error
            browser_type = self._browser_type
        reachable = self._alive(url)
        health = self._health(url) if reachable else None
        listener = self._listener_info(url) if reachable else {}
        browser_ready = bool(health.get("ok")) if isinstance(health, dict) else None
        listener_managed = bool(reachable and listener.get("listener_verified"))
        managed = bool(running or listener_managed)
        managed_pid = pid or (listener.get("listener_pid") if listener_managed else None)
        if managed and reachable and browser_ready is not False:
            state = "managed"
        elif reachable:
            state = "external" if not managed else "degraded"
        elif running:
            state = "starting"
        else:
            state = "stopped"
        return {
            "running": managed,
            "reachable": reachable,
            "pid": managed_pid,
            "url": str(url).rstrip("/"),
            "managed": managed,
            "state": state,
            "browser_ready": browser_ready,
            "health_supported": health is not None,
            "listener_pid": listener.get("listener_pid"),
            "listener_verified": bool(listener.get("listener_verified")),
            "actual_threads": listener.get("actual_threads"),
            "health": health or {},
            "started_at": started_at,
            "last_error": error,
            "browser_type": browser_type,
        }

    def start(self, url: str = "http://127.0.0.1:5072", *, threads: int = 1) -> dict[str, Any]:
        host, port, endpoint = self._endpoint(url)
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("自动启动仅支持本机 Turnstile Solver 地址")
        if self._alive(endpoint):
            return self.status(endpoint)
        with self._lock:
            if self._process and self._process.poll() is None:
                return self.status(endpoint)
            solver_dir = Path(__file__).resolve().parent / "turnstile_solver"
            script = self._solver_script()
            if not script.is_file():
                raise RuntimeError(f"本地 Turnstile Solver 文件不存在: {script}")
            if importlib.util.find_spec("camoufox") is None:
                raise RuntimeError("Camoufox is not installed; run setup_local.ps1 first")
            log_dir = DATA_DIR / "grok_solver"
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout = open(log_dir / "solver.out.log", "ab", buffering=0)
            stderr = open(log_dir / "solver.err.log", "ab", buffering=0)
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
            command = [
                sys.executable,
                str(script),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--browser_type",
                "camoufox",
                "--thread",
                str(max(1, min(int(threads or 1), 4))),
            ]
            try:
                process_env = dict(os.environ)
                process_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "TERM": "xterm"})
                self._process = subprocess.Popen(
                    command,
                    cwd=str(solver_dir),
                    stdout=stdout,
                    stderr=stderr,
                    creationflags=creationflags,
                    env=process_env,
                )
                self._started_at = time.time()
                self._last_error = ""
                self._browser_type = "camoufox"
                self._write_pid(self._process, endpoint)
            except Exception as error:
                self._last_error = str(error)
                raise RuntimeError(f"启动本地 Turnstile Solver 失败: {error}") from error

        deadline = time.time() + 35.0
        while time.time() < deadline:
            if self._alive(endpoint):
                return self.status(endpoint)
            with self._lock:
                if self._process and self._process.poll() is not None:
                    self._last_error = f"solver process exited with code {self._process.returncode}"
                    self._clear_pid()
                    break
            time.sleep(0.5)
        state = self.status(endpoint)
        detail = state.get("last_error") or "solver startup timeout"
        raise RuntimeError(f"本地 Turnstile Solver 未就绪: {detail}")

    def stop(self, url: str = "http://127.0.0.1:5072") -> dict[str, Any]:
        with self._lock:
            process = self._process
            self._process = None
        if process and process.poll() is None:
            try:
                self._terminate_tree(process.pid)
            except Exception:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        listener = self._listener_info(url)
        listener_pid = listener.get("listener_pid")
        if self._alive(url) and listener_pid:
            if not listener.get("listener_verified"):
                raise RuntimeError(f"端口由非本项目 Solver 进程占用，拒绝停止: {url}")
            try:
                self._terminate_tree(int(listener_pid))
            except Exception as error:
                raise RuntimeError(f"停止本地 Turnstile Solver 失败: {error}") from error
        self._clear_pid()
        deadline = time.time() + 10.0
        while time.time() < deadline and self._alive(url):
            time.sleep(0.2)
        return self.status(url)

    def restart(self, url: str = "http://127.0.0.1:5072", *, threads: int = 1) -> dict[str, Any]:
        self.stop(url)
        return self.start(url, threads=threads)

    def ensure_started(self, config: dict[str, Any]) -> dict[str, Any]:
        url = str(config.get("local_solver_url") or "http://127.0.0.1:5072")
        threads = int(config.get("solver_threads") or 1)
        if self._alive(url):
            state = self.status(url)
            desired_threads = max(1, min(threads, 4))
            thread_matches = state.get("actual_threads") in {None, desired_threads}
            health = state.get("health") if isinstance(state.get("health"), dict) else {}
            connected = int(health.get("connected_browsers") or 0)
            inflight = int(health.get("inflight_tasks") or 0)
            queued = int(health.get("queued_tasks") or 0)
            supports_backpressure = "inflight_tasks" in health and "queued_tasks" in health
            clean_pool = connected >= desired_threads and inflight == 0 and queued == 0
            if state.get("browser_ready") is True and thread_matches and supports_backpressure and clean_pool:
                return state
            if not bool(config.get("solver_auto_start", True)):
                raise RuntimeError(f"本地 Turnstile Solver 状态异常，请在打码配置中重启: {url}")
            if not state.get("listener_verified"):
                raise RuntimeError(f"本地 Solver 端口被其他进程占用，请检查: {url}")
            return self.restart(url, threads=threads)
        if not bool(config.get("solver_auto_start", True)):
            raise RuntimeError(f"本地 Turnstile Solver 不可用: {url}")
        return self.start(url, threads=threads)


grok_solver_manager = GrokSolverManager()
