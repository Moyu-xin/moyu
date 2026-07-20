"""Worker that runs the immutable BR New engine with ID request adapters."""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from br_new_session_store import save_session_snapshot, snapshot_from_flow
from buyer_identity import install_authorize_identity_guard
from id_new_profile import (
    adapt_id_graphql_variables,
    adapt_id_signup_variables,
    apply_id_browser_profile,
    generate_id_address,
    generate_id_user,
    id_billing_line1,
    normalize_id_phone,
)


ROOT = Path(__file__).resolve().parent
VENDOR_DIR = ROOT / "vendor" / "openai-paypal-br-new"
RUNTIME_DIR = ROOT / ".runtime" / "id-new"
SESSION_DIR = RUNTIME_DIR / "sessions"
EVENT_PREFIX = "BRNEW_EVENT "
EMIT_LOCK = threading.Lock()


def emit(event_type: str, **payload: Any) -> None:
    line = EVENT_PREFIX + json.dumps(
        {"type": event_type, **payload},
        ensure_ascii=False,
        default=str,
    )
    with EMIT_LOCK:
        print(line, flush=True)


def read_message() -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise RuntimeError("ID New bridge closed the input pipe")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("ID New bridge returned an invalid message")
    return value


def request(event_type: str, **payload: Any) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    emit(event_type, request_id=request_id, **payload)
    response = read_message()
    if response.get("request_id") != request_id:
        raise RuntimeError("ID New bridge response did not match the request")
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "ID New bridge request failed"))
    return response


@dataclass
class BridgeActivation:
    activation_id: str
    phone_number: str
    provider_id: str
    price: float
    expires_at: float
    reused: bool


class BridgeSmsProvider:
    def __init__(self, config: dict[str, Any]):
        self.max_attempts = max(1, int(config.get("sms_max_attempts") or 1))
        self.wait_seconds = 300.0

    def reserve_number(self) -> BridgeActivation:
        response = request("sms_reserve")
        return BridgeActivation(
            activation_id=str(response.get("activation_id") or "manual"),
            phone_number=str(response.get("phone") or ""),
            provider_id=str(response.get("provider_id") or "manual"),
            price=float(response.get("price") or 0),
            expires_at=float(response.get("expires_at") or 0),
            reused=bool(response.get("reused")),
        )

    def mark_sms_sent(self, activation: BridgeActivation) -> None:
        emit("sms_sent", activation_id=activation.activation_id)

    def wait_for_code(
        self,
        activation: BridgeActivation,
        timeout_seconds: float | None = None,
    ) -> str | None:
        response = request(
            "sms_wait_code",
            activation_id=activation.activation_id,
            timeout_seconds=timeout_seconds,
            prompt="请输入 PayPal 短信验证码",
        )
        return str(response.get("code") or "").strip() or None

    def abandon(self, activation: BridgeActivation, reason: str) -> None:
        emit("sms_abandon", activation_id=activation.activation_id, reason=reason)

    def register_confirmation_result(
        self,
        activation: BridgeActivation,
        confirmed: bool,
    ) -> None:
        emit("sms_confirmation", activation_id=activation.activation_id, confirmed=confirmed)


def configure_vendor_path() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(RUNTIME_DIR)
    sys.path.insert(0, str(VENDOR_DIR))


def main() -> int:
    config_value = read_message()
    configure_vendor_path()

    import config as vendor_config

    apply_id_browser_profile(vendor_config.BROWSER_PROFILE)

    from loguru import logger
    import paypal.flow as flow_module
    import paypal.session as session_module
    from paypal.models import BillingAddress, UserInfo, generate_card
    from paypal.proxy import build_proxy_config

    original_common_headers = session_module.build_common_headers

    def id_common_headers(state=None):
        headers = original_common_headers(state)
        profile = getattr(state, "browser_profile", None) or vendor_config.BROWSER_PROFILE
        if str(profile.get("country") or "").upper() == "ID":
            headers["Accept-Language"] = "id-ID,id;q=0.9,en;q=0.8"
        return headers

    session_module.build_common_headers = id_common_headers
    flow_module.build_common_headers = id_common_headers

    original_weasley_log = flow_module.send_weasley_log

    def id_weasley_log(*args, **kwargs):
        if str(kwargs.get("country") or "").upper() == "ID":
            kwargs["lang"] = "id"
        return original_weasley_log(*args, **kwargs)

    flow_module.send_weasley_log = id_weasley_log

    class IdNewPayPalFlow(flow_module.PayPalFlow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            apply_id_browser_profile(self.state.browser_profile)
            original_graphql = self.session.graphql

            def id_graphql(operation_name, query, variables, *call_args, **call_kwargs):
                return original_graphql(
                    operation_name,
                    query,
                    adapt_id_graphql_variables(operation_name, variables),
                    *call_args,
                    **call_kwargs,
                )

            self.session.graphql = id_graphql

        def _update_user_phone(self, phone: str):
            full, country_code, local = normalize_id_phone(phone)
            self.user.phone = full
            self.user.phone_country_code = country_code
            self.user.phone_local = local
            logger.info("Phone updated for OTP retry: {}", self._masked_phone())
            self._on_phone_updated()

        def _billing_line1(self) -> str:
            return id_billing_line1(self.address.street, self.address.house_number)

        def _build_signup_variables(self, token: str) -> dict[str, object]:
            variables = super()._build_signup_variables(token)
            return adapt_id_signup_variables(
                variables,
                first_name=self.user.first_name,
                last_name=self.user.last_name,
            )

        def _reset_for_full_retry(self, flow_attempt: int):
            current_phone = self.user.phone
            super()._reset_for_full_retry(flow_attempt)
            self.user = generate_id_user(UserInfo, current_phone)
            apply_id_browser_profile(self.state.browser_profile)
            self._on_full_retry_generated(flow_attempt)

        def _wait_and_rotate_signup_identity(self, reason: str, signup_attempt: int):
            current_phone = self.user.phone
            super()._wait_and_rotate_signup_identity(reason, signup_attempt)
            self.user = generate_id_user(UserInfo, current_phone)
            self._on_signup_retry_generated(signup_attempt, reason)

    logger.remove()

    def log_sink(message: Any) -> None:
        record = message.record
        emit("log", level=record["level"].name, message=str(record["message"]))

    logger.add(log_sink, level="DEBUG")

    phone = str(config_value.get("phone") or "+628123456789")
    proxy_url = str(config_value.get("proxy_url") or "").strip()
    environment_proxy = bool(
        os.getenv("PAYPAL_PROXY_URL", "").strip()
        or os.getenv("PAYPAL_PROXY_POOL", "").strip()
    )
    if proxy_url:
        proxy_config = build_proxy_config(enabled=True, proxy_url=proxy_url)
        logger.info("ID New proxy source: job configuration")
    elif environment_proxy:
        proxy_config = build_proxy_config(enabled=None)
        logger.info("ID New proxy source: PAYPAL_PROXY_URL/PAYPAL_PROXY_POOL environment")
    else:
        proxy_config = build_proxy_config(enabled=False)
        logger.info("ID New proxy source: disabled")

    user = generate_id_user(UserInfo, phone)
    card = generate_card(proxy_url=proxy_config.url)
    address = generate_id_address(BillingAddress)
    emit(
        "generated",
        data={
            "user": asdict(user),
            "card": asdict(card),
            "address": asdict(address),
        },
    )

    provider = BridgeSmsProvider(config_value)
    flow = IdNewPayPalFlow(
        ba_token=str(config_value.get("ba_token") or ""),
        user=user,
        card=card,
        address=address,
        max_card_attempts=int(config_value.get("max_card_attempts") or 5),
        max_flow_attempts=max(1, int(config_value.get("max_flow_attempts") or 2)),
        proxy_config=proxy_config,
        fingerprint_source="headless",
        datadome_mode="headless",
        mtr_runtime="headless",
        risk_signals_mode="headless",
        sms_provider=provider,
    )
    install_authorize_identity_guard(flow)
    result = flow.run()
    if isinstance(result, dict) and result.get("status") == "success":
        try:
            snapshot = snapshot_from_flow(
                flow,
                proxy_url=proxy_config.url or "",
                mode="ID_NEW",
                country="ID",
            )
            snapshot_path = save_session_snapshot(snapshot, directory=SESSION_DIR)
            emit(
                "session_snapshot_saved",
                snapshot_id=snapshot["snapshot_id"],
                captured_at=snapshot["captured_at"],
                cookie_count=len(snapshot["cookies"]),
                path=str(snapshot_path),
            )
        except Exception as exc:
            logger.warning("ID New successful session snapshot could not be saved: {}", exc)
    emit("result", data=result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        emit("error", message=f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)
