"""Runtime guards for buyer-authenticated PayPal authorization."""
from __future__ import annotations

import time
from typing import Any


REUSABLE_BUYER_IDENTITY_QUERY = """
query ReusableBuyerIdentityQuery($token: String!) {
  checkoutSession(token: $token) {
    buyer {
      auth {
        __typename
      }
      userId
      __typename
    }
    __typename
  }
}
"""


def _state_value(flow: Any, name: str) -> str:
    return str(getattr(getattr(flow, "state", None), name, "") or "").strip()


def _valid_ec_token(flow: Any, token: str) -> bool:
    validator = getattr(flow, "_is_ec_token", None)
    if callable(validator):
        return bool(validator(token))
    return token.startswith("EC-") and len(token) > 3


def authorize_context_missing(flow: Any) -> list[str]:
    missing: list[str] = []
    ec_token = _state_value(flow, "ec_token")
    if not _valid_ec_token(flow, ec_token):
        missing.append("ec_token")
    if not _state_value(flow, "euat_token"):
        missing.append("euat_token")
    if not _state_value(flow, "user_id"):
        missing.append("buyer_user_id")
    return missing


def direct_agree_ec_error(flow: Any, snapshot_ec_token: str) -> str:
    current_ec = _state_value(flow, "ec_token")
    if not _valid_ec_token(flow, current_ec):
        return "Phase 0 did not produce a valid EC token for the current BA"
    snapshot_ec = str(snapshot_ec_token or "").strip()
    if snapshot_ec and current_ec == snapshot_ec:
        return "Phase 0 reused the historical snapshot EC instead of producing an EC for the current BA"
    return ""


def acquire_fresh_direct_agree_ec(
    flow: Any,
    snapshot_ec_token: str,
    *,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> tuple[int, str]:
    """Retry Phase 0 in one jar so DataDome state can carry into the next attempt."""
    attempts = max(1, int(max_attempts))
    last_error = ""
    for attempt in range(1, attempts + 1):
        flow.state.ec_token = ""
        flow._phase0_initial_load()
        last_error = direct_agree_ec_error(flow, snapshot_ec_token)
        if not last_error:
            return attempt, ""
        if "historical snapshot EC" in last_error:
            return attempt, last_error
        if attempt < attempts and retry_delay_seconds > 0:
            time.sleep(float(retry_delay_seconds))
    return attempts, last_error


def _result_object(result: Any) -> dict[str, Any]:
    if isinstance(result, list):
        first = result[0] if result else {}
        return first if isinstance(first, dict) else {}
    return result if isinstance(result, dict) else {}


def _error_messages(result: Any) -> list[str]:
    errors = _result_object(result).get("errors")
    if not isinstance(errors, list):
        return []
    messages: list[str] = []
    for error in errors:
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
        else:
            message = str(error or "").strip()
        if message:
            messages.append(message)
    return messages


def _load_reused_buyer_review(flow: Any) -> bool:
    ensure_euat = getattr(flow, "_ensure_euat_cookie", None)
    if callable(ensure_euat):
        ensure_euat()

    build_url = getattr(flow, "_hermes_url", None)
    load_context = getattr(flow, "_load_hagrid_review_context", None)
    if not callable(build_url) or not callable(load_context):
        return False
    try:
        return bool(
            load_context(
                build_url(),
                build_url(add_fi_contingency=True),
                build_url(billing_lite=True),
            )
        )
    except Exception:
        return False


def prepare_reused_buyer_identity(
    flow: Any,
    expected_user_id: str,
    *,
    max_attempts: int = 2,
) -> dict[str, object]:
    """Bind restored identity material to the fresh EC and verify it server-side."""
    expected_user_id = str(expected_user_id or "").strip()
    ec_token = _state_value(flow, "ec_token")
    euat_token = _state_value(flow, "euat_token")
    if not expected_user_id or not _valid_ec_token(flow, ec_token) or not euat_token:
        missing = []
        if not expected_user_id:
            missing.append("snapshot_buyer_user_id")
        if not _valid_ec_token(flow, ec_token):
            missing.append("fresh_ec_token")
        if not euat_token:
            missing.append("euat_token")
        return {
            "status": "error",
            "reason": "IDENTITY_ELEVATION_CONTEXT_INCOMPLETE",
            "error": "reused buyer identity context is incomplete: " + ", ".join(missing),
            "retryable": False,
            "missing_context": missing,
        }

    graphql = getattr(getattr(flow, "session", None), "graphql", None)
    if not callable(graphql):
        return {
            "status": "error",
            "reason": "IDENTITY_ELEVATION_UNAVAILABLE",
            "error": "PayPal session does not support buyer identity verification",
            "retryable": False,
        }

    attempts = max(1, int(max_attempts))
    last_messages: list[str] = []
    review_loaded = False
    for attempt in range(1, attempts + 1):
        review_loaded = _load_reused_buyer_review(flow) or review_loaded
        review_referer = ""
        build_url = getattr(flow, "_hermes_url", None)
        if callable(build_url):
            try:
                review_referer = str(build_url(billing_lite=True) or "")
            except Exception:
                review_referer = ""
        try:
            result = graphql(
                "ReusableBuyerIdentityQuery",
                REUSABLE_BUYER_IDENTITY_QUERY,
                {"token": ec_token},
                extra_headers={
                    "Referer": review_referer,
                    "PayPal-Client-Metadata-Id": ec_token,
                },
                graphql_error_level="WARNING",
            )
        except Exception as exc:
            last_messages = [str(exc)]
            continue

        result_object = _result_object(result)
        data = result_object.get("data")
        checkout_session = data.get("checkoutSession") if isinstance(data, dict) else None
        buyer = checkout_session.get("buyer") if isinstance(checkout_session, dict) else None
        actual_user_id = str(buyer.get("userId") or "").strip() if isinstance(buyer, dict) else ""
        auth_context = buyer.get("auth") if isinstance(buyer, dict) else None
        last_messages = _error_messages(result)

        if actual_user_id and actual_user_id != expected_user_id:
            return {
                "status": "error",
                "reason": "BUYER_MISMATCH",
                "error": "the fresh EC is bound to a different PayPal buyer",
                "retryable": False,
                "expected_user_id": expected_user_id,
                "actual_user_id": actual_user_id,
                "attempt_count": attempt,
                "review_loaded": review_loaded,
            }

        if actual_user_id == expected_user_id and isinstance(auth_context, dict):
            verification = {
                "ec_token": ec_token,
                "user_id": actual_user_id,
                "auth_context_present": True,
            }
            flow.state.user_id = actual_user_id
            flow._verified_reused_buyer_context = verification
            return {
                "status": "success",
                "reason": "REUSED_BUYER_VERIFIED",
                "user_id": actual_user_id,
                "ec_token": ec_token,
                "attempt_count": attempt,
                "review_loaded": review_loaded,
                "auth_context_present": True,
            }

    combined_errors = " | ".join(last_messages)
    upper_errors = combined_errors.upper()
    if "ANONYMOUS" in upper_errors:
        reason = "IDENTITY_ELEVATION_ANONYMOUS"
        error = "PayPal still reports the restored session as ANONYMOUS"
    elif "BUYER_NOT_SET" in upper_errors:
        reason = "BUYER_NOT_SET"
        error = "the fresh EC has no PayPal buyer bound"
    else:
        reason = "IDENTITY_ELEVATION_UNVERIFIED"
        error = "PayPal did not verify the restored buyer on the fresh EC"
    return {
        "status": "error",
        "reason": reason,
        "error": error,
        "retryable": True,
        "expected_user_id": expected_user_id,
        "attempt_count": attempts,
        "review_loaded": review_loaded,
        "server_errors": last_messages,
    }


def _try_resolve_buyer_from_review(flow: Any) -> None:
    if _state_value(flow, "user_id"):
        return
    build_url = getattr(flow, "_hermes_url", None)
    load_context = getattr(flow, "_load_hagrid_review_context", None)
    if not callable(build_url) or not callable(load_context):
        return
    try:
        load_context(
            build_url(),
            build_url(add_fi_contingency=True),
            build_url(billing_lite=True),
        )
    except Exception:
        return


def install_authorize_identity_guard(
    flow: Any,
    *,
    require_verified_reused_buyer: bool = False,
) -> Any:
    if require_verified_reused_buyer:
        flow._require_verified_reused_buyer = True
    if getattr(flow, "_authorize_identity_guard_installed", False):
        return flow

    original_authorize = flow._phase4_authorize

    if callable(getattr(flow, "_authorize_metadata_candidates", None)):
        def stable_metadata_candidates() -> list[str]:
            ec_token = _state_value(flow, "ec_token")
            return [ec_token] if ec_token else []

        flow._authorize_metadata_candidates = stable_metadata_candidates

    def guarded_authorize() -> dict[str, object]:
        _try_resolve_buyer_from_review(flow)
        missing = authorize_context_missing(flow)
        if missing:
            buyer_missing = "buyer_user_id" in missing
            return {
                "status": "error",
                "error": "authorization identity context is incomplete: " + ", ".join(missing),
                "reason": "BUYER_NOT_SET" if buyer_missing else "AUTHORIZE_CONTEXT_INCOMPLETE",
                "retryable": buyer_missing,
                "preauthorize_guard": True,
                "missing_context": missing,
            }
        if getattr(flow, "_require_verified_reused_buyer", False):
            verified = getattr(flow, "_verified_reused_buyer_context", None)
            verified_ec = str(verified.get("ec_token") or "") if isinstance(verified, dict) else ""
            verified_user_id = str(verified.get("user_id") or "") if isinstance(verified, dict) else ""
            if (
                verified_ec != _state_value(flow, "ec_token")
                or verified_user_id != _state_value(flow, "user_id")
            ):
                return {
                    "status": "error",
                    "error": "the restored PayPal buyer was not verified on the current EC",
                    "reason": "IDENTITY_ELEVATION_UNVERIFIED",
                    "retryable": True,
                    "preauthorize_guard": True,
                }
        return original_authorize()

    flow._phase4_authorize = guarded_authorize
    flow._authorize_identity_guard_installed = True
    return flow
