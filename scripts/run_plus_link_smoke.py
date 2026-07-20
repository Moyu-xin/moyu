from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.plus_link_store import PlusLinkService, _normalize_config


SOURCE_STATE = ROOT / "data" / "plus_link.json"
LOCAL_CONFIG = ROOT / "config.json"
DEFAULT_API_BASE = "http://127.0.0.1:8000/api"


def load_config() -> dict[str, object]:
    data = json.loads(SOURCE_STATE.read_text(encoding="utf-8"))
    raw = data.get("config") if isinstance(data, dict) else {}
    config = _normalize_config(raw if isinstance(raw, dict) else {})
    matrix_mode = str(os.environ.get("PLUS_LINK_SMOKE_MODE") or "fixed_pair").strip().lower() == "matrix"
    left_country = os.environ.get("PLUS_LINK_SMOKE_LEFT_COUNTRY", "US").upper()
    provider_country = os.environ.get("PLUS_LINK_SMOKE_PROVIDER_COUNTRY", left_country).upper()
    approve_country = os.environ.get("PLUS_LINK_SMOKE_APPROVE_COUNTRY", left_country).upper()
    billing_country = os.environ.get("PLUS_LINK_SMOKE_BILLING_COUNTRY", left_country).upper()
    payment_method_country = os.environ.get("PLUS_LINK_SMOKE_PM_COUNTRY", billing_country).upper()
    config.update(
        {
            "country_rotation_enabled": matrix_mode,
            "target_country": billing_country,
            "checkout_country": left_country,
            "provider_country": provider_country,
            "approve_country": approve_country,
            "payment_method_country": payment_method_country,
            "checkout_ui_mode": os.environ.get("PLUS_LINK_SMOKE_CHECKOUT_UI_MODE", "custom").lower(),
            "stripe_provider_egress": os.environ.get("PLUS_LINK_SMOKE_PROVIDER_EGRESS", "left_residential").lower(),
            "stripe_poll_egress": os.environ.get("PLUS_LINK_SMOKE_POLL_EGRESS", "promotion").lower(),
            "paypal_confirm_mode": os.environ.get("PLUS_LINK_SMOKE_CONFIRM_MODE", "payment_method").lower(),
            "paypal_confirm_protocol": os.environ.get("PLUS_LINK_SMOKE_CONFIRM_PROTOCOL", "standalone").lower(),
            "promotion_country": os.environ.get("PLUS_LINK_SMOKE_RIGHT_COUNTRY", "TR").upper(),
            "provider_retry_limit": max(1, min(20, int(os.environ.get("PLUS_LINK_SMOKE_RETRIES", "15")))),
            "matrix_combo_retry_limit": max(1, min(20, int(os.environ.get("PLUS_LINK_SMOKE_MATRIX_RETRIES", "1")))),
            "retry_limits_version": 2,
            "concurrency": 1,
            "refresh_tokens": False,
        }
    )
    if "PLUS_LINK_SMOKE_PARENT_FILTER" in os.environ:
        config["residential_parent_node_filter"] = str(os.environ.get("PLUS_LINK_SMOKE_PARENT_FILTER") or "").strip()
    if "PLUS_LINK_SMOKE_PARENT_PROXY" in os.environ:
        config["residential_parent_proxy_override"] = str(os.environ.get("PLUS_LINK_SMOKE_PARENT_PROXY") or "").strip()
    if "PLUS_LINK_SMOKE_VIA_PROJECT_PROXY" in os.environ:
        config["residential_via_project_proxy"] = str(
            os.environ.get("PLUS_LINK_SMOKE_VIA_PROJECT_PROXY") or ""
        ).strip().lower() not in {"0", "false", "no", "off"}
    return _normalize_config(config)


def summary(result: dict[str, object]) -> dict[str, object]:
    return {
        "status": result.get("status"),
        "stage": result.get("stage"),
        "engine_step": result.get("engine_step"),
        "progress_percent": result.get("progress_percent"),
        "eligibility_status": result.get("eligibility_status"),
        "eligibility_plan": result.get("eligibility_plan"),
        "eligibility_has_email": result.get("eligibility_has_email"),
        "extraction_mode": result.get("extraction_mode"),
        "active_retry_limit": result.get("active_retry_limit"),
        "matrix_index": result.get("matrix_index"),
        "matrix_total": result.get("matrix_total"),
        "matrix_failures": result.get("matrix_failures"),
        "matrix_skipped": result.get("matrix_skipped"),
        "left_proxy_country": result.get("left_proxy_country"),
        "checkout_proxy_country": result.get("checkout_proxy_country"),
        "provider_country": result.get("provider_country"),
        "approve_country": result.get("approve_country"),
        "right_proxy_country": result.get("right_proxy_country"),
        "link_attempt_count": result.get("link_attempt_count"),
        "link_attempt_limit": result.get("link_attempt_limit"),
        "stripe_amount": result.get("stripe_amount"),
        "stripe_amount_source": result.get("stripe_amount_source"),
        "currency": result.get("currency"),
        "billing_profile_source": result.get("billing_profile_source"),
        "billing_email_domain": result.get("billing_email_domain"),
        "stripe_poll_network_errors": result.get("stripe_poll_network_errors"),
        "paypal_availability": result.get("paypal_availability"),
        "paypal_availability_by_country": result.get("paypal_availability_by_country"),
        "last_combo_failure_category": result.get("last_combo_failure_category"),
        "failure_category": result.get("failure_category"),
        "last_engine_message": result.get("last_engine_message"),
        "last_engine_detail": result.get("last_engine_detail"),
        "last_redirect_message": result.get("last_redirect_message"),
        "stripe_session_reused": result.get("stripe_session_reused"),
        "redirect_session_state_copied": result.get("redirect_session_state_copied"),
        "stripe_poll_egress": result.get("stripe_poll_egress"),
        "left_sticky_rotated": result.get("left_sticky_rotated"),
        "right_sticky_rotated": result.get("right_sticky_rotated"),
        "paypal_confirm_mode": result.get("paypal_confirm_mode"),
        "chatgpt_approval_endpoint": result.get("chatgpt_approval_endpoint"),
        "approval_session_state_copied": result.get("approval_session_state_copied"),
        "chatgpt_confirm_profile": result.get("chatgpt_confirm_profile"),
        "paypal_confirm_protocol": result.get("paypal_confirm_protocol"),
        "stripe_confirm_prep_profile": result.get("stripe_confirm_prep_profile"),
        "stripe_confirm_request_profile": result.get("stripe_confirm_request_profile"),
        "stripe_confirm_response_profile": result.get("stripe_confirm_response_profile"),
        "stripe_confirm_error_profile": result.get("stripe_confirm_error_profile"),
        "stripe_submission_profile": result.get("stripe_submission_profile"),
        "long_url_present": bool(result.get("long_url_present")),
        "long_url_hash": result.get("long_url_hash"),
        "error": result.get("error"),
        "duration_seconds": result.get("duration_seconds"),
    }


def _local_api() -> tuple[str, str] | None:
    mode = str(os.environ.get("PLUS_LINK_SMOKE_USE_API") or "auto").strip().lower()
    if mode in {"0", "false", "no", "off", "local"}:
        return None
    api_base = str(os.environ.get("PLUS_LINK_SMOKE_API_BASE") or DEFAULT_API_BASE).strip().rstrip("/")
    auth_key = str(os.environ.get("PLUS_LINK_SMOKE_AUTH_KEY") or "").strip()
    if not auth_key and LOCAL_CONFIG.is_file():
        try:
            config_data = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
            auth_key = str(config_data.get("auth-key") or "").strip() if isinstance(config_data, dict) else ""
        except Exception:
            auth_key = ""
    if not auth_key:
        return None
    try:
        response = requests.get(
            f"{api_base}/plus-link",
            headers={"Authorization": f"Bearer {auth_key}"},
            timeout=5,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    return api_base, auth_key


def _run_via_api(access_token: str, timeout_seconds: int, config: dict[str, object], api_base: str, auth_key: str) -> int:
    headers = {"Authorization": f"Bearer {auth_key}"}
    before_response = requests.get(f"{api_base}/plus-link", headers=headers, timeout=15)
    before_response.raise_for_status()
    before_state = (before_response.json() or {}).get("plus_link") or {}
    before_results = before_state.get("results") if isinstance(before_state, dict) else []
    before_ids = {
        str(item.get("task_id") or "")
        for item in before_results or []
        if isinstance(item, dict) and item.get("task_id")
    }
    original_config = before_state.get("config") if isinstance(before_state, dict) else {}
    original_config = dict(original_config) if isinstance(original_config, dict) else {}

    if bool(config.get("residential_via_project_proxy")) and not str(
        config.get("residential_parent_proxy_override") or ""
    ).strip():
        threads = max(1, min(10, int(config.get("concurrency") or 1)))
        managed = requests.post(
            f"{api_base}/proxy-pool/managed-mihomo/start",
            headers=headers,
            json={"threads": threads},
            timeout=180,
        )
        managed.raise_for_status()

    task_id = ""
    try:
        started = requests.post(
            f"{api_base}/plus-link/start",
            headers=headers,
            json={
                "emails": [],
                "partial_accounts": [],
                "manual_accounts": [],
                "manual_tokens": [access_token],
                "clear_results": False,
                "config": config,
            },
            timeout=60,
        )
        started.raise_for_status()
        started_state = (started.json() or {}).get("plus_link") or {}
        new_results = [
            item
            for item in (started_state.get("results") or [])
            if isinstance(item, dict) and str(item.get("task_id") or "") not in before_ids
        ]
        if len(new_results) != 1:
            raise RuntimeError(f"expected one API smoke task, got {len(new_results)}")
        task_id = str(new_results[0].get("task_id") or "")

        started_at = time.time()
        previous = ""
        while time.time() - started_at < timeout_seconds:
            try:
                response = requests.get(f"{api_base}/plus-link", headers=headers, timeout=30)
                response.raise_for_status()
            except requests.RequestException:
                time.sleep(1)
                continue
            state = (response.json() or {}).get("plus_link") or {}
            result = next(
                (
                    item
                    for item in (state.get("results") or [])
                    if isinstance(item, dict) and str(item.get("task_id") or "") == task_id
                ),
                {},
            )
            current = json.dumps(summary(result), ensure_ascii=False, sort_keys=True)
            if current != previous:
                print(current, flush=True)
                previous = current
            status = str(result.get("status") or "")
            if status in {"success", "failed", "cancelled"}:
                return 0 if status == "success" and bool(result.get("long_url_present")) else 1
            time.sleep(1)
        if task_id:
            requests.post(f"{api_base}/plus-link/tasks/{task_id}/cancel", headers=headers, timeout=15)
        print(json.dumps({"status": "timeout", "timeout_seconds": timeout_seconds}, ensure_ascii=False), flush=True)
        return 2
    finally:
        if original_config:
            try:
                requests.post(
                    f"{api_base}/plus-link/config",
                    headers=headers,
                    json=original_config,
                    timeout=30,
                )
            except requests.RequestException:
                pass


def main() -> int:
    access_token = str(os.environ.get("PLUS_LINK_SMOKE_ACCESS_TOKEN") or "").strip()
    if not access_token:
        raise RuntimeError("PLUS_LINK_SMOKE_ACCESS_TOKEN is required")
    timeout_seconds = max(60, int(os.environ.get("PLUS_LINK_SMOKE_TIMEOUT", "1800")))
    config = load_config()
    local_api = _local_api()
    if local_api is not None:
        return _run_via_api(access_token, timeout_seconds, config, *local_api)

    with tempfile.TemporaryDirectory(prefix="plus-link-smoke-", ignore_cleanup_errors=True) as temp_dir:
        service = PlusLinkService(Path(temp_dir) / "plus_link.json")
        service.start(
            {
                "emails": [],
                "partial_accounts": [],
                "manual_tokens": [access_token],
                "clear_results": True,
                "config": config,
            }
        )
        started = time.time()
        previous = ""
        while time.time() - started < timeout_seconds:
            state = service.get(include_secret=False)
            results = state.get("results") if isinstance(state, dict) else []
            result = results[0] if isinstance(results, list) and results and isinstance(results[0], dict) else {}
            current = json.dumps(summary(result), ensure_ascii=False, sort_keys=True)
            if current != previous:
                print(current, flush=True)
                previous = current
            status = str(result.get("status") or "")
            if status in {"success", "failed", "cancelled"}:
                runner = service._runner
                if runner and runner.is_alive():
                    runner.join(timeout=10)
                return 0 if status == "success" and bool(result.get("long_url_present")) else 1
            time.sleep(1)
        service.stop()
        print(json.dumps({"status": "timeout", "timeout_seconds": timeout_seconds}, ensure_ascii=False), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
