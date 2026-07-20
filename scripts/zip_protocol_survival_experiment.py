from __future__ import annotations

import argparse
import asyncio
import atexit
import copy
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_DIR = ROOT / "scripts" / "protocols" / "zip_20260712_no_cpa"
REFERENCE_NODE_MODULES = ROOT.parent / "注册机" / "node_modules"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PROTOCOL_DIR))

from scripts import legacy_free_survival_experiment as harness  # noqa: E402


MONITOR_PROXY_PORT = 48000
MONITOR_API_PORT = 49000
MONITOR_ROOT = harness.OUTPUT_DIR / "protocol-sticky-mihomo"
_monitor_process: subprocess.Popen[Any] | None = None
_monitor_log: Any = None


class InventoryMailAdapter:
    def __init__(self, protocol_module: ModuleType, mailbox: dict[str, Any], proxy: str) -> None:
        self.protocol_module = protocol_module
        self.mailbox = mailbox
        self.proxy = proxy

    async def poll_verify_code(
        self,
        email: str,
        timeout: int = 180,
        interval: int = 3,
        since: float | None = None,
    ) -> str | None:
        del email, timeout, interval, since
        return await asyncio.to_thread(
            self.protocol_module.wait_for_code,
            self.mailbox,
            register_proxy=self.proxy,
            register_mode="free_wu",
        )


def load_zip_protocol() -> ModuleType:
    if REFERENCE_NODE_MODULES.exists():
        existing = str(os.environ.get("NODE_PATH") or "").strip()
        os.environ["NODE_PATH"] = str(REFERENCE_NODE_MODULES) + (os.pathsep + existing if existing else "")
    source = PROTOCOL_DIR / "chatgpt_register.py"
    spec = importlib.util.spec_from_file_location("zip_20260712_no_cpa_protocol", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load zip protocol: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def register_zip_batch(
    target: int,
    max_attempts: int,
    slots: int,
    *,
    mailbox_group: str,
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = harness.OUTPUT_DIR / f"zip-20260712-no-cpa-survival-{stamp}.json"
    log_path = output_path.with_suffix(".log")
    state: dict[str, Any] = {
        "schema_version": 1,
        "protocol": "zip_20260712_no_cpa_firefox144",
        "protocol_source": str(PROTOCOL_DIR / "chatgpt_register.py"),
        "created_at": harness.now_iso(),
        "requested_successes": target,
        "max_attempts": max_attempts,
        "mailbox_group": mailbox_group,
        "cpa_upload_enabled": False,
        "platform_oauth_enabled": False,
        "accounts": [],
        "attempts": [],
    }
    harness.write_json(output_path, state)

    mail_protocol = harness.load_legacy_module(
        log_path,
        source=harness.CURRENT_FREE_SOURCE,
        mailbox_group=mailbox_group,
    )
    state["mailbox_strategy"] = "random_word_mmdd_alias"
    state["named_aliases_available"] = harness.prepare_random_named_outlook_aliases(mail_protocol)
    zip_protocol = load_zip_protocol()
    harness.write_json(output_path, state)

    attempt = 0
    while len(state["accounts"]) < target and attempt < max_attempts:
        attempt += 1
        mailbox_base = harness.rotate_outlook_base(mail_protocol, attempt)
        assignment = harness.assign_registration_proxy(attempt, slots)
        proxy = str(assignment.get("proxy") or "").strip()
        attempt_record: dict[str, Any] = {
            "attempt": attempt,
            "started_at": harness.now_iso(),
            "proxy_source": str(assignment.get("source") or ""),
            "proxy_label": str(assignment.get("label") or ""),
            "proxy_region": str(assignment.get("region") or ""),
            "mailbox_base": mailbox_base,
            "success": False,
        }
        if not proxy:
            attempt_record["error"] = str(assignment.get("error") or "proxy assignment failed")[:500]
            attempt_record["finished_at"] = harness.now_iso()
            state["attempts"].append(attempt_record)
            harness.write_json(output_path, state)
            print(f"zip_attempt={attempt} skipped proxy_error completed={len(state['accounts'])}/{target}")
            continue

        mailbox: dict[str, Any] | None = None
        started = time.monotonic()
        try:
            mailbox = mail_protocol.create_mailbox(register_proxy=proxy, register_mode="free_wu")
            email = str(mailbox.get("address") or "").strip()
            if not email:
                raise RuntimeError("Outlook mailbox has no address")
            name = zip_protocol.random_name()
            age = zip_protocol.random_age()
            birthdate = (datetime.now() - timedelta(days=age * 365)).strftime("%Y-%m-%d")
            password = zip_protocol.random_password()
            sentinel = zip_protocol.SentinelTokenProvider(proxy=proxy)
            adapter = InventoryMailAdapter(mail_protocol, mailbox, proxy)
            result = asyncio.run(
                zip_protocol._try_register_one_email(
                    email,
                    password,
                    name,
                    birthdate,
                    sentinel,
                    proxy,
                    adapter,
                    time.time(),
                    otp_timeout=180,
                )
            )
            if not isinstance(result, dict) or not str(result.get("access_token") or "").strip():
                raise RuntimeError("Zip protocol did not return an access token")

            registration_proxy = harness.proxy_pool_store.registration_proxy_record(assignment, proxy=proxy)
            result.update(
                {
                    "birthdate": birthdate,
                    "password_set": False,
                    "registered_at": harness.now_iso(),
                    "register_mode": "zip_20260712_no_cpa",
                    "product_mode": "partial",
                    "mailbox_group": mailbox_group,
                    "inbox_address": str(mailbox.get("inbox_address") or ""),
                    "experiment_protocol": "zip_20260712_no_cpa_firefox144",
                    "proxy": proxy,
                    "registration_proxy": registration_proxy,
                    "registration_proxy_source": str(registration_proxy.get("source") or ""),
                    "registration_proxy_node_id": str(registration_proxy.get("node_id") or ""),
                    "registration_proxy_label": str(
                        registration_proxy.get("label") or registration_proxy.get("node_name") or ""
                    ),
                    "registration_egress": harness.egress_fingerprint(proxy),
                    "health_checks": [],
                }
            )
            immediate = harness.check_account(result, len(state["accounts"]) + 1)
            result["health_checks"].append(immediate)
            state["accounts"].append(result)
            attempt_record["success"] = True
            attempt_record["email"] = email
            mail_protocol.mail_provider.mark_mailbox_result(mailbox, success=True)
            print(
                "zip_attempt={} success=True completed={}/{} alive={} status={} region={}".format(
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
            if mailbox is not None:
                mail_protocol.mail_provider.mark_mailbox_result(mailbox, success=False, error=error)
            print(
                f"zip_attempt={attempt} success=False completed={len(state['accounts'])}/{target} "
                f"error={type(error).__name__}"
            )
        finally:
            attempt_record["elapsed_seconds"] = round(time.monotonic() - started, 1)
            attempt_record["finished_at"] = harness.now_iso()
            state["attempts"].append(attempt_record)
            harness.write_json(output_path, state)

    state["registration_finished_at"] = harness.now_iso()
    state["registered"] = len(state["accounts"])
    harness.write_json(output_path, state)
    print(f"zip_result_file={output_path}")
    print(f"zip_registered={len(state['accounts'])} attempts={len(state['attempts'])}")
    return output_path


def summarize(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    accounts = state.get("accounts") if isinstance(state.get("accounts"), list) else []
    latest = []
    for account in accounts:
        checks = account.get("health_checks") if isinstance(account.get("health_checks"), list) else []
        latest.append(checks[-1] if checks else {})
    return {
        "protocol": str(state.get("protocol") or ""),
        "registered": len(accounts),
        "alive": sum(1 for item in latest if item.get("alive") is True),
        "dead": sum(1 for item in latest if item.get("alive") is False),
        "unknown": sum(1 for item in latest if item.get("alive") is None),
    }


def _monitor_request(path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{MONITOR_API_PORT}{path}",
        data=payload,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read().decode("utf-8").strip()
        return json.loads(raw) if raw else {}


def _monitor_source_config(node_name: str) -> dict[str, Any]:
    runtime_root = ROOT / "data" / "managed_mihomo"
    for path in sorted(runtime_root.glob("thread-*/config.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        groups = data.get("proxy-groups") if isinstance(data, dict) else []
        names = {
            str(name)
            for group in groups if isinstance(group, dict) and str(group.get("name") or "") == "REGISTER"
            for name in group.get("proxies") or []
        }
        if node_name in names:
            return data
    raise RuntimeError(f"Registration node is not present in a local Mihomo config: {node_name}")


def _stop_monitor_proxy() -> None:
    global _monitor_process, _monitor_log
    if _monitor_process is not None and _monitor_process.poll() is None:
        _monitor_process.terminate()
        try:
            _monitor_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _monitor_process.kill()
            _monitor_process.wait(timeout=3)
    _monitor_process = None
    if _monitor_log is not None:
        _monitor_log.close()
        _monitor_log = None


atexit.register(_stop_monitor_proxy)


def select_monitor_node(node_name: str) -> str:
    global _monitor_process, _monitor_log
    if _monitor_process is None or _monitor_process.poll() is not None:
        config = _monitor_source_config(node_name)
        config["mixed-port"] = MONITOR_PROXY_PORT
        config["external-controller"] = f"127.0.0.1:{MONITOR_API_PORT}"
        config["allow-lan"] = False
        MONITOR_ROOT.mkdir(parents=True, exist_ok=True)
        config_path = MONITOR_ROOT / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        _monitor_log = (MONITOR_ROOT / "mihomo.log").open("ab")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _monitor_process = subprocess.Popen(
            [
                str(ROOT / "data" / "managed_mihomo" / "mihomo.exe"),
                "-d",
                str(MONITOR_ROOT),
                "-f",
                str(config_path),
            ],
            cwd=str(MONITOR_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=_monitor_log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if _monitor_process.poll() is not None:
                raise RuntimeError("Experiment Mihomo exited during startup")
            try:
                _monitor_request("/version")
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("Experiment Mihomo startup timed out")
    selector = _monitor_request("/proxies/REGISTER")
    if node_name not in [str(name) for name in selector.get("all") or []]:
        raise RuntimeError(f"Registration node is missing from experiment Mihomo: {node_name}")
    _monitor_request(
        "/proxies/" + urllib.parse.quote("REGISTER", safe=""),
        method="PUT",
        body={"name": node_name},
    )
    return f"http://127.0.0.1:{MONITOR_PROXY_PORT}"


def sticky_check_account(account: dict[str, Any], index: int) -> dict[str, Any]:
    registration = account.get("registration_proxy") if isinstance(account.get("registration_proxy"), dict) else {}
    node_name = str(registration.get("node_name") or registration.get("label") or "").strip()
    if str(registration.get("source") or "") != "managed_mihomo" or not node_name:
        return harness.check_account(account, index)

    monitor_proxy = select_monitor_node(node_name)
    target = copy.deepcopy(account)
    target["registration_proxy"] = {
        "source": "experiment_sticky_proxy",
        "proxy": monitor_proxy,
        "node_id": str(registration.get("node_id") or ""),
        "node_name": node_name,
        "label": node_name,
    }
    attempts: list[dict[str, Any]] = []
    check: dict[str, Any] = {}
    for retry in range(3):
        check = harness.check_account(target, index)
        attempts.append(
            {
                "status_code": int(check.get("status_code") or 0),
                "response_code": str(check.get("response_code") or ""),
                "error": str(check.get("error") or "")[:300],
            }
        )
        has_session = bool(str(target.get("session_token") or "").strip())
        session_conclusively_invalid = check.get("nextauth_session_ok") is False
        if check.get("alive") is True or (
            check.get("alive") is False and (not has_session or session_conclusively_invalid)
        ):
            break
        if retry < 2:
            time.sleep(2)
    check["retry_attempts"] = attempts
    check["sticky_node_restored"] = True
    check["sticky_node_id"] = str(registration.get("node_id") or "")
    return check


def check_result_file(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text(encoding="utf-8"))
    alive = 0
    dead = 0
    unknown = 0
    for index, account in enumerate(state.get("accounts") or [], 1):
        check = sticky_check_account(account, index)
        account.setdefault("health_checks", []).append(check)
        if check.get("alive") is True:
            alive += 1
        elif check.get("alive") is False:
            dead += 1
        else:
            unknown += 1
        print(
            "sticky_account={} age_min={:.1f} alive={} status={} code={} same_egress={}".format(
                index,
                int(check.get("age_seconds") or 0) / 60,
                check.get("alive"),
                int(check.get("status_code") or 0),
                str(check.get("response_code") or ""),
                check.get("same_egress"),
            )
        )
        harness.write_json(path, state)
    summary = {
        "checked_at": harness.now_iso(),
        "total": alive + dead + unknown,
        "alive": alive,
        "dead": dead,
        "unknown": unknown,
    }
    state["last_health_summary"] = summary
    harness.write_json(path, state)
    return summary


def run_comparison(target: int, max_attempts: int, slots: int, mailbox_group: str) -> Path:
    current_path = harness.register_batch(
        target,
        max_attempts,
        slots,
        source=harness.CURRENT_FREE_SOURCE,
        label="current-free-partial-ab",
        mailbox_group=mailbox_group,
        protocol_label="current_free_partial_firefox133",
        named_aliases=True,
        registration_method="free_wu",
        token_delay_seconds=0,
    )
    zip_path = register_zip_batch(target, max_attempts, slots, mailbox_group=mailbox_group)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest = harness.OUTPUT_DIR / f"current-vs-zip-protocol-{stamp}.json"
    payload = {
        "schema_version": 1,
        "created_at": harness.now_iso(),
        "mailbox_group": mailbox_group,
        "target_per_protocol": target,
        "current_result": str(current_path),
        "zip_result": str(zip_path),
        "checks": [
            {
                "checked_at": harness.now_iso(),
                "current": summarize(current_path),
                "zip": summarize(zip_path),
            }
        ],
    }
    harness.write_json(manifest, payload)
    print(f"comparison_manifest={manifest}")
    return manifest


def check_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    current_path = Path(str(payload.get("current_result") or ""))
    zip_path = Path(str(payload.get("zip_result") or ""))
    check_result_file(current_path)
    check_result_file(zip_path)
    check = {
        "checked_at": harness.now_iso(),
        "current": summarize(current_path),
        "zip": summarize(zip_path),
    }
    payload.setdefault("checks", []).append(check)
    harness.write_json(path, payload)
    print(json.dumps(check, ensure_ascii=False))
    return check


def monitor_manifest(path: Path, minutes: list[int]) -> None:
    started = time.monotonic()
    for minute in sorted(set(max(0, value) for value in minutes)):
        wait_seconds = minute * 60 - (time.monotonic() - started)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        check_manifest(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare the current partial protocol with the sanitized zip protocol")
    subparsers = parser.add_subparsers(dest="command", required=True)

    compare = subparsers.add_parser("compare")
    compare.add_argument("--target", type=int, default=3)
    compare.add_argument("--max-attempts", type=int, default=9)
    compare.add_argument("--slots", type=int, default=2)
    compare.add_argument("--mailbox-group", default="7.19")

    check = subparsers.add_parser("check")
    check.add_argument("--manifest", required=True)

    monitor = subparsers.add_parser("monitor")
    monitor.add_argument("--manifest", required=True)
    monitor.add_argument("--minutes", default="20,60,180")

    args = parser.parse_args()
    if args.command == "compare":
        run_comparison(
            max(1, int(args.target)),
            max(int(args.target), int(args.max_attempts)),
            max(1, int(args.slots)),
            str(args.mailbox_group or "7.19"),
        )
    elif args.command == "check":
        check_manifest(Path(args.manifest).resolve())
    else:
        minutes = [int(value.strip()) for value in str(args.minutes).split(",") if value.strip()]
        monitor_manifest(Path(args.manifest).resolve(), minutes)


if __name__ == "__main__":
    main()
