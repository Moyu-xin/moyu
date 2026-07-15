from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.plus_link_store import PlusLinkService, _normalize_config


SOURCE_STATE = ROOT / "data" / "plus_link.json"


def load_config() -> dict[str, object]:
    data = json.loads(SOURCE_STATE.read_text(encoding="utf-8"))
    raw = data.get("config") if isinstance(data, dict) else {}
    config = _normalize_config(raw if isinstance(raw, dict) else {})
    matrix_mode = str(os.environ.get("PLUS_LINK_SMOKE_MODE") or "fixed_pair").strip().lower() == "matrix"
    config.update(
        {
            "country_rotation_enabled": matrix_mode,
            "target_country": os.environ.get("PLUS_LINK_SMOKE_LEFT_COUNTRY", "US").upper(),
            "checkout_country": os.environ.get("PLUS_LINK_SMOKE_LEFT_COUNTRY", "US").upper(),
            "payment_method_country": os.environ.get("PLUS_LINK_SMOKE_LEFT_COUNTRY", "US").upper(),
            "promotion_country": os.environ.get("PLUS_LINK_SMOKE_RIGHT_COUNTRY", "TR").upper(),
            "provider_retry_limit": max(1, min(20, int(os.environ.get("PLUS_LINK_SMOKE_RETRIES", "15")))),
            "matrix_combo_retry_limit": max(1, min(20, int(os.environ.get("PLUS_LINK_SMOKE_MATRIX_RETRIES", "1")))),
            "retry_limits_version": 2,
            "concurrency": 1,
            "refresh_tokens": False,
        }
    )
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
        "link_attempt_count": result.get("link_attempt_count"),
        "link_attempt_limit": result.get("link_attempt_limit"),
        "stripe_amount": result.get("stripe_amount"),
        "long_url_present": bool(result.get("long_url_present")),
        "long_url_hash": result.get("long_url_hash"),
        "error": result.get("error"),
        "duration_seconds": result.get("duration_seconds"),
    }


def main() -> int:
    access_token = str(os.environ.get("PLUS_LINK_SMOKE_ACCESS_TOKEN") or "").strip()
    if not access_token:
        raise RuntimeError("PLUS_LINK_SMOKE_ACCESS_TOKEN is required")
    timeout_seconds = max(60, int(os.environ.get("PLUS_LINK_SMOKE_TIMEOUT", "1800")))
    config = load_config()

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
