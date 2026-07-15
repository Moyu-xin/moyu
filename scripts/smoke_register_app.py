from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_AUTH_KEY = "chatgpt2api"


@dataclass
class SmokeResult:
    name: str
    ok: bool
    detail: str = ""


class SmokeClient:
    def __init__(self, base_url: str, auth_key: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.auth_key = auth_key
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any | None = None,
        auth: bool = True,
        expected: set[int] | None = None,
    ) -> tuple[int, Any, bytes]:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}
        data: bytes | None = None
        if auth:
            headers["Authorization"] = f"Bearer {self.auth_key}"
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        status = 0
        raw = b""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = int(resp.status)
                raw = resp.read()
        except urllib.error.HTTPError as error:
            status = int(error.code)
            raw = error.read()
        expected = expected or {200}
        if status not in expected:
            preview = raw.decode("utf-8", errors="replace")[:500]
            raise AssertionError(f"HTTP {status}, expected {sorted(expected)}, body={preview}")
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = raw.decode("utf-8", errors="replace")
        return status, parsed, raw


def assert_key(data: Any, key: str) -> None:
    if not isinstance(data, dict) or key not in data:
        raise AssertionError(f"missing key {key!r}")


def run_step(results: list[SmokeResult], name: str, func) -> None:
    started = time.perf_counter()
    try:
        detail = func() or ""
        elapsed = (time.perf_counter() - started) * 1000
        results.append(SmokeResult(name, True, f"{detail} ({elapsed:.0f}ms)"))
    except Exception as error:
        elapsed = (time.perf_counter() - started) * 1000
        results.append(SmokeResult(name, False, f"{error} ({elapsed:.0f}ms)"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the local register-only app.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--auth-key", default=DEFAULT_AUTH_KEY)
    parser.add_argument("--read-only", action="store_true", help="Skip safe temporary import/delete checks.")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    client = SmokeClient(args.base_url, args.auth_key, timeout=args.timeout)
    results: list[SmokeResult] = []
    stamp = str(int(time.time() * 1000))
    temp_mail = f"codex-smoke-mail-{stamp}@example.invalid"
    temp_account = f"codex-smoke-account-{stamp}@example.invalid"

    def check_version() -> str:
        _, data, _ = client.request("GET", "/version", auth=False)
        assert_key(data, "version")
        return str(data.get("edition") or data.get("version"))

    def check_login_ok() -> str:
        _, data, _ = client.request("POST", "/auth/login")
        assert data.get("ok") is True
        return str(data.get("role"))

    def check_login_bad() -> str:
        bad = SmokeClient(args.base_url, "__bad_key__", timeout=args.timeout)
        bad.request("POST", "/auth/login", expected={401})
        return "401 as expected"

    def check_page() -> str:
        status, _, raw = client.request("HEAD", "/register/", auth=False)
        return f"HTTP {status}, bytes={len(raw)}"

    for name, func in [
        ("version", check_version),
        ("auth login ok", check_login_ok),
        ("auth login bad", check_login_bad),
        ("register page", check_page),
    ]:
        run_step(results, name, func)

    read_endpoints: list[tuple[str, str, str, set[int], str | None, int | None]] = [
        ("register config", "GET", "/api/register", {200}, "register", None),
        ("mailboxes", "GET", "/api/mailboxes", {200}, "mailboxes", None),
        ("mail provider configs", "GET", "/api/mail-provider-configs", {200}, "provider_configs", None),
        ("accounts", "GET", "/api/accounts", {200}, "account_pool", None),
        ("account health state", "GET", "/api/accounts/health", {200}, "inventory", 400_000),
        ("account inventory", "GET", "/api/accounts/inventory", {200}, "inventory", 400_000),
        ("sub2api config", "GET", "/api/sub2api-config", {200}, "sub2api_config", None),
        ("sub2api groups", "GET", "/api/sub2api/groups", {200, 400}, None, None),
        ("sub2api accounts", "GET", "/api/sub2api/accounts?page=1&page_size=1", {200, 400}, None, None),
        ("k12", "GET", "/api/k12", {200}, "k12", 750_000),
        ("plus link", "GET", "/api/plus-link", {200}, "plus_link", 750_000),
        ("proxy pool", "GET", "/api/proxy-pool", {200}, "proxy_pool", None),
        ("proxy controller", "GET", "/api/proxy-controller", {200}, "proxy_controller", None),
    ]

    for name, method, path, expected, key, max_bytes in read_endpoints:
        def make_check(method=method, path=path, expected=expected, key=key, max_bytes=max_bytes) -> str:
            status, data, raw = client.request(method, path, expected=expected)
            if key and status == 200:
                assert_key(data, key)
            if max_bytes is not None and len(raw) > max_bytes:
                raise AssertionError(f"payload too large: {len(raw)} > {max_bytes}")
            if path == "/api/accounts/health" and status == 200:
                inventory = data.get("inventory") if isinstance(data, dict) else {}
                logs = len(inventory.get("logs") or [])
                results_count = len(inventory.get("results") or [])
                if logs > 300 or results_count > 300:
                    raise AssertionError(f"health payload not trimmed: logs={logs}, results={results_count}")
                return f"HTTP {status}, bytes={len(raw)}, logs={logs}, results={results_count}"
            return f"HTTP {status}, bytes={len(raw)}"

        run_step(results, name, make_check)

    cleanup_errors: list[str] = []
    if not args.read_only:
        def import_mailbox() -> str:
            line = f"{temp_mail}----pw----cid----secret----refresh-token"
            _, data, _ = client.request("POST", "/api/mailboxes/import", body={"text": line, "provider_type": "gmail_oauth", "note": "codex-smoke"})
            mailboxes = data.get("mailboxes", {})
            if not any(item.get("email") == temp_mail for item in mailboxes.get("items", [])):
                raise AssertionError("temp mailbox not found after import")
            return temp_mail

        def delete_mailbox() -> str:
            client.request("DELETE", f"/api/mailboxes/{urllib.parse.quote(temp_mail)}")
            return "deleted"

        def import_account() -> str:
            line = f"{temp_account}----pw----"
            _, data, _ = client.request("POST", "/api/accounts/import", body={"text": line, "auto_health_check": False})
            pool = data.get("account_pool", {})
            if not any(item.get("email") == temp_account for item in pool.get("accounts", [])):
                raise AssertionError("temp account not found after import")
            return temp_account

        def check_temp_health_missing_token() -> str:
            _, data, _ = client.request("POST", "/api/accounts/health/check", body={"emails": [temp_account], "method": "local", "refresh_quota": False, "concurrency": 1})
            health = data.get("inventory_health", {})
            if int(health.get("total") or 0) != 1:
                raise AssertionError(f"expected total=1, got {health}")
            result = (health.get("results") or [{}])[0]
            if result.get("status") != "missing_token":
                raise AssertionError(f"expected missing_token, got {result}")
            return "missing_token"

        def check_temp_quota_missing_token() -> str:
            _, data, _ = client.request("POST", "/api/accounts/quota/check", body={"emails": [temp_account], "method": "local", "concurrency": 1})
            health = data.get("inventory_health", {})
            if int(health.get("total") or 0) != 1:
                raise AssertionError(f"expected total=1, got {health}")
            result = (health.get("results") or [{}])[0]
            if result.get("status") != "missing_token":
                raise AssertionError(f"expected missing_token, got {result}")
            return "missing_token"

        def export_temp_sub2api() -> str:
            _, data, _ = client.request("POST", "/api/accounts/export-sub2api", body={"emails": [temp_account]})
            sub2api = data.get("sub2api", {})
            if int(sub2api.get("count") or 0) != 0:
                raise AssertionError(f"temp account without token should not export: {sub2api}")
            return "count=0"

        def delete_account() -> str:
            client.request("DELETE", f"/api/accounts/{urllib.parse.quote(temp_account)}")
            return "deleted"

        mutation_steps = [
            ("mailbox import temp", import_mailbox),
            ("mailbox delete temp", delete_mailbox),
            ("account import temp", import_account),
            ("account health missing token", check_temp_health_missing_token),
            ("account quota missing token", check_temp_quota_missing_token),
            ("account export sub2api no-token", export_temp_sub2api),
            ("account delete temp", delete_account),
        ]
        try:
            for name, func in mutation_steps:
                run_step(results, name, func)
        finally:
            try:
                client.request("DELETE", f"/api/mailboxes/{urllib.parse.quote(temp_mail)}", expected={200, 404})
            except Exception as error:
                cleanup_errors.append(f"mailbox cleanup: {error}")
            try:
                client.request("DELETE", f"/api/accounts/{urllib.parse.quote(temp_account)}", expected={200, 404})
            except Exception as error:
                cleanup_errors.append(f"account cleanup: {error}")

    task_file = ROOT / "data" / "account_inventory_task.json"

    def check_state_file_trimmed() -> str:
        data = json.loads(task_file.read_text(encoding="utf-8"))
        logs = len(data.get("logs") or [])
        results_count = len(data.get("results") or [])
        running = int((data.get("stats") or {}).get("running") or 0)
        if logs > 1500 or results_count > 1500:
            raise AssertionError(f"state file not trimmed: logs={logs}, results={results_count}")
        return f"logs={logs}, results={results_count}, running={running}"

    run_step(results, "state file trimmed", check_state_file_trimmed)

    for error in cleanup_errors:
        results.append(SmokeResult(f"cleanup warning", False, error))

    width = max(len(item.name) for item in results) if results else 10
    failed = [item for item in results if not item.ok]
    for item in results:
        marker = "OK" if item.ok else "FAIL"
        print(f"{marker:<4} {item.name:<{width}}  {item.detail}")
    print(f"\nSmoke summary: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
