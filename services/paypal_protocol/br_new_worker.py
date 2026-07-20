"""Worker process that loads the immutable BR New vendor package."""
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


ROOT = Path(__file__).resolve().parent
VENDOR_DIR = ROOT / "vendor" / "openai-paypal-br-new"
RUNTIME_DIR = ROOT / ".runtime" / "br-new"
EVENT_PREFIX = "BRNEW_EVENT "
EMIT_LOCK = threading.Lock()


def emit(event_type: str, **payload: Any) -> None:
    event = {"type": event_type, **payload}
    line = EVENT_PREFIX + json.dumps(event, ensure_ascii=False, default=str)
    with EMIT_LOCK:
        print(line, flush=True)


def read_message() -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise RuntimeError("BR New bridge closed the input pipe")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("BR New bridge returned an invalid message")
    return value


def request(event_type: str, **payload: Any) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    emit(event_type, request_id=request_id, **payload)
    response = read_message()
    if response.get("request_id") != request_id:
        raise RuntimeError("BR New bridge response did not match the request")
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "BR New bridge request failed"))
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
        emit(
            "sms_confirmation",
            activation_id=activation.activation_id,
            confirmed=confirmed,
        )


def configure_vendor_path() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(RUNTIME_DIR)
    sys.path.insert(0, str(VENDOR_DIR))


def main() -> int:
    config = read_message()
    configure_vendor_path()

    from loguru import logger
    from paypal.flow import PayPalFlow
    from paypal.models import generate_address, generate_card, generate_user
    from paypal.proxy import build_proxy_config

    logger.remove()

    def log_sink(message: Any) -> None:
        record = message.record
        emit(
            "log",
            level=record["level"].name,
            message=str(record["message"]),
        )

    logger.add(log_sink, level="DEBUG")

    phone = str(config.get("phone") or "+5511999999999")
    proxy_url = str(config.get("proxy_url") or "").strip()
    environment_proxy = bool(
        os.getenv("PAYPAL_PROXY_URL", "").strip()
        or os.getenv("PAYPAL_PROXY_POOL", "").strip()
    )
    if proxy_url:
        proxy_config = build_proxy_config(enabled=True, proxy_url=proxy_url)
        logger.info("BR New proxy source: job configuration")
    elif environment_proxy:
        proxy_config = build_proxy_config(enabled=None)
        logger.info("BR New proxy source: PAYPAL_PROXY_URL/PAYPAL_PROXY_POOL environment")
    else:
        proxy_config = build_proxy_config(enabled=False)
        logger.info("BR New proxy source: disabled")
    user = generate_user(phone)
    card = generate_card(proxy_url=proxy_config.url)
    address = generate_address()
    emit(
        "generated",
        data={
            "user": asdict(user),
            "card": asdict(card),
            "address": asdict(address),
        },
    )

    provider = BridgeSmsProvider(config)
    flow = PayPalFlow(
        ba_token=str(config.get("ba_token") or ""),
        user=user,
        card=card,
        address=address,
        max_card_attempts=int(config.get("max_card_attempts") or 5),
        max_flow_attempts=max(1, int(config.get("max_flow_attempts") or 2)),
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
                mode="BR_NEW",
                country="BR",
            )
            snapshot_path = save_session_snapshot(snapshot, directory=ROOT / ".runtime" / "br-new" / "sessions")
            emit(
                "session_snapshot_saved",
                snapshot_id=snapshot["snapshot_id"],
                captured_at=snapshot["captured_at"],
                cookie_count=len(snapshot["cookies"]),
                path=str(snapshot_path),
            )
        except Exception as exc:
            logger.warning("BR New successful session snapshot could not be saved: {}", exc)
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
