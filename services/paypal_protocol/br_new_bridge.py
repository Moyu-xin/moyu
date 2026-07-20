"""Subprocess bridge for the immutable BR New protocol vendor tree."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
VENDOR_DIR = ROOT / "vendor" / "openai-paypal-br-new"
VENDOR_MANIFEST = ROOT / "vendor" / "openai-paypal-br-new.manifest.json"
WORKER_PATH = ROOT / "br_new_worker.py"
GB_NEW_WORKER_PATH = ROOT / "gb_new_worker.py"
US_NEW_WORKER_PATH = ROOT / "us_new_worker.py"
TH_NEW_WORKER_PATH = ROOT / "th_new_worker.py"
ID_NEW_WORKER_PATH = ROOT / "id_new_worker.py"
EVENT_PREFIX = "BRNEW_EVENT "


class BrNewProtocolError(RuntimeError):
    pass


def verify_vendor_integrity() -> int:
    """Verify every archived source file before loading the vendor protocol."""
    if not VENDOR_MANIFEST.is_file() or not VENDOR_DIR.is_dir():
        raise BrNewProtocolError("BR New 原版协议或校验清单不存在")
    try:
        manifest = json.loads(VENDOR_MANIFEST.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrNewProtocolError("BR New 原版协议校验清单无效") from exc

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BrNewProtocolError("BR New 原版协议校验清单为空")
    for entry in files:
        if not isinstance(entry, dict):
            raise BrNewProtocolError("BR New 原版协议校验项无效")
        relative = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "").casefold()
        path = (VENDOR_DIR / relative).resolve()
        try:
            path.relative_to(VENDOR_DIR.resolve())
        except ValueError as exc:
            raise BrNewProtocolError("BR New 原版协议校验路径越界") from exc
        if not path.is_file():
            raise BrNewProtocolError(f"BR New 原版协议文件缺失：{relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise BrNewProtocolError(f"BR New 原版协议文件已变化：{relative}")
    return len(files)


class BrNewProtocolBridge:
    """Run the BR New vendor package without importing it into this process."""

    def __init__(
        self,
        *,
        ba_token: str,
        phone: str,
        max_card_attempts: int,
        proxy_url: str | None,
        sms_provider: str,
        reserve_number: Callable[[], dict[str, Any]],
        wait_for_code: Callable[[str], str],
        on_log: Callable[[str, str], None],
        on_generated: Callable[[dict[str, Any]], None],
        on_sms_sent: Callable[[], None] | None = None,
        on_confirmation: Callable[[bool], None] | None = None,
        on_abandon: Callable[[str], None] | None = None,
        sms_max_attempts: int = 1,
        max_flow_attempts: int = 2,
        transport_max_attempts: int = 3,
        mode: str = "BR_NEW",
    ):
        normalized_mode = str(mode or "BR_NEW").strip().upper()
        if normalized_mode not in {"BR_NEW", "GB_NEW", "US_NEW", "TH_NEW", "ID_NEW"}:
            raise ValueError(f"unsupported New protocol mode: {mode}")
        self.mode = normalized_mode
        self.mode_label = {
            "BR_NEW": "BR New",
            "GB_NEW": "GB New",
            "US_NEW": "US New",
            "TH_NEW": "TH New",
            "ID_NEW": "ID New",
        }[normalized_mode]
        self.worker_path = {
            "BR_NEW": WORKER_PATH,
            "GB_NEW": GB_NEW_WORKER_PATH,
            "US_NEW": US_NEW_WORKER_PATH,
            "TH_NEW": TH_NEW_WORKER_PATH,
            "ID_NEW": ID_NEW_WORKER_PATH,
        }[normalized_mode]
        self.config = {
            "ba_token": ba_token,
            "phone": phone,
            "max_card_attempts": max_card_attempts,
            "proxy_url": proxy_url or "",
            "sms_provider": sms_provider,
            "sms_max_attempts": max(1, int(sms_max_attempts)),
            "max_flow_attempts": max(1, int(max_flow_attempts)),
        }
        self.reserve_number = reserve_number
        self.wait_for_code = wait_for_code
        self.on_log = on_log
        self.on_generated = on_generated
        self.on_sms_sent = on_sms_sent
        self.on_confirmation = on_confirmation
        self.on_abandon = on_abandon
        self.transport_max_attempts = max(1, int(transport_max_attempts))
        self._last_request = ""

    @staticmethod
    def _write_message(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise BrNewProtocolError("BR New 子进程输入管道不可用")
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _reply(
        self,
        process: subprocess.Popen[str],
        event: dict[str, Any],
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        response: dict[str, Any] = {
            "request_id": event.get("request_id", ""),
            "ok": error is None,
        }
        if payload:
            response.update(payload)
        if error is not None:
            response["error"] = str(error)
        self._write_message(process, response)

    def _handle_event(
        self,
        process: subprocess.Popen[str],
        event: dict[str, Any],
    ) -> dict[str, Any] | None:
        event_type = str(event.get("type") or "")
        if event_type == "log":
            message = str(event.get("message") or "")
            if message.startswith(("GET ", "POST ")):
                self._last_request = message
            self.on_log(str(event.get("level") or "INFO"), message)
        elif event_type == "generated":
            generated = event.get("data")
            if isinstance(generated, dict):
                self.on_generated(generated)
        elif event_type == "sms_reserve":
            try:
                self._reply(process, event, self.reserve_number())
            except BaseException as exc:
                self._reply(process, event, error=exc)
        elif event_type == "sms_wait_code":
            try:
                prompt = str(event.get("prompt") or "请输入 PayPal 短信验证码")
                self._reply(process, event, {"code": self.wait_for_code(prompt)})
            except BaseException as exc:
                self._reply(process, event, error=exc)
        elif event_type == "sms_sent":
            if self.on_sms_sent:
                self.on_sms_sent()
        elif event_type == "sms_confirmation":
            if self.on_confirmation:
                self.on_confirmation(bool(event.get("confirmed")))
        elif event_type == "session_snapshot_saved":
            self.on_log(
                "INFO",
                f"{self.mode_label} 成功会话已加密保存："
                f"snapshot={event.get('snapshot_id') or '<unknown>'} "
                f"cookies={int(event.get('cookie_count') or 0)}",
            )
        elif event_type == "sms_abandon":
            reason = str(event.get("reason") or "unknown")
            if self.on_abandon:
                self.on_abandon(reason)
            self.on_log("WARNING", f"{self.mode_label} 短信号码已放弃：{reason}")
        elif event_type == "result":
            result = event.get("data")
            if not isinstance(result, dict):
                raise BrNewProtocolError("BR New 返回了无效结果")
            return result
        elif event_type == "error":
            raise BrNewProtocolError(str(event.get("message") or "BR New 执行失败"))
        return None

    def _run_once(self, env: dict[str, str]) -> dict[str, Any]:
        process = subprocess.Popen(
            [sys.executable, str(self.worker_path)],
            cwd=str(VENDOR_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        def pump_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                text = line.strip()
                if text:
                    self.on_log("DEBUG", text)

        stderr_thread = threading.Thread(
            target=pump_stderr,
            name=f"{self.mode.casefold().replace('_', '-')}-stderr",
            daemon=True,
        )
        stderr_thread.start()
        result: dict[str, Any] | None = None
        try:
            self._write_message(process, self.config)
            if process.stdout is None:
                raise BrNewProtocolError("BR New 子进程输出管道不可用")
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                if not line.startswith(EVENT_PREFIX):
                    self.on_log("DEBUG", line)
                    continue
                try:
                    event = json.loads(line[len(EVENT_PREFIX) :])
                except json.JSONDecodeError:
                    self.on_log("WARNING", "BR New 子进程返回了无法解析的事件")
                    continue
                if not isinstance(event, dict):
                    continue
                event_result = self._handle_event(process, event)
                if event_result is not None:
                    result = event_result
            return_code = process.wait()
            stderr_thread.join(timeout=2)
            if result is None:
                raise BrNewProtocolError(f"BR New 子进程未返回结果，退出码 {return_code}")
            if return_code != 0:
                raise BrNewProtocolError(f"BR New 子进程异常退出，退出码 {return_code}")
            return result
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdin is not None:
                process.stdin.close()

    def _can_retry_transport_error(self, exc: BaseException) -> bool:
        message = str(exc).casefold()
        transient = any(marker in message for marker in (
            "curl: (35)",
            "tls connect error",
            "curl: (56)",
            "proxy connect aborted",
        ))
        initial_page = self._last_request.startswith(
            "GET https://www.paypal.com/agreements/approve?"
        )
        return transient and initial_page

    def run(self) -> dict[str, Any]:
        verified_count = verify_vendor_integrity()
        self.on_log(
            "INFO",
            f"{self.mode_label} 共用协议引擎完整性校验通过：{verified_count} 个文件",
        )

        env = os.environ.copy()
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PAYPAL_FINGERPRINT_SOURCE": "headless",
            "PAYPAL_DATADOME_MODE": "headless",
            "PAYPAL_MTR_RUNTIME": "headless",
            "PAYPAL_RISK_SIGNALS_MODE": "headless",
        })
        for attempt in range(1, self.transport_max_attempts + 1):
            self._last_request = ""
            try:
                return self._run_once(env)
            except BrNewProtocolError as exc:
                if attempt >= self.transport_max_attempts or not self._can_retry_transport_error(exc):
                    raise
                delay = min(2 * attempt, 5)
                self.on_log(
                    "WARNING",
                    f"{self.mode_label} 初始页面遇到临时 TLS/代理隧道错误，"
                    f"{delay} 秒后重试（{attempt + 1}/{self.transport_max_attempts}）",
                )
                time.sleep(delay)
        raise BrNewProtocolError(f"{self.mode_label} transport retry loop ended unexpectedly")
