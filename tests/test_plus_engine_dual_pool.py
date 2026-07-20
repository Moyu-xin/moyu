from __future__ import annotations

from typing import Any

from services.plus_engine import app as engine


records: list[tuple[str, str]] = []
stripe_sessions: list[tuple[str, "FakeSession"]] = []
http_calls: list[dict[str, Any]] = []
http_status_by_path: dict[str, int] = {}


class FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = "{}" if status_code < 400 else '{"detail":"test failure"}'

    def json(self) -> dict[str, Any]:
        return {"checkout_session": {"id": "cs_test"}}


class FakeSession:
    def __init__(self, proxy: str = "") -> None:
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        if proxy:
            self.proxies = {"http": proxy, "https": proxy}

    @property
    def proxy(self) -> str:
        return self.proxies.get("https", "")

    def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> FakeResponse:
        if url.endswith("/payments/checkout/update"):
            label = "checkout_update"
        elif url.endswith("/payments/checkout/taxes"):
            label = "checkout_taxes"
        else:
            label = "post"
        records.append((label, self.proxy))
        http_calls.append(
            {
                "session": self,
                "url": url,
                "json": dict(json or {}),
                "headers": dict(headers or {}),
            }
        )
        return FakeResponse(http_status_by_path.get(url, 200))


left_proxy = "http://user:pass-US@proxy.test:8080"
right_proxy = "http://user:pass-JP@proxy.test:8080"
parent_proxy = "http://127.0.0.1:42000"
base_711_proxy = "USER123456-zone-custom:fixturepass@global.example.711proxy.invalid:10000"
rotated_711_proxy = engine.rotate_kookeey_proxy_session(base_711_proxy, "US", stage="checkout", slot=1)
assert "USER123456-zone-custom-region-US-session-" in rotated_711_proxy
assert "-sessTime-5" in rotated_711_proxy
assert "sessAuto" not in rotated_711_proxy
assert ":fixturepass@global.example.711proxy.invalid:10000" in rotated_711_proxy
req = engine.LongLinkRequest(
    accessToken="token",
    checkout_proxy_override=left_proxy,
    provider_proxy_override=left_proxy,
    approve_proxy_override=left_proxy,
    promotion_proxy_override=right_proxy,
    promotion_proxy_country="JP",
    batch_target_country="US",
    batch_checkout_proxy_country="US",
    billing_country="US",
    payment_method_country="US",
    paypal_confirm_mode="payment_method",
    promotion_country="JP",
    approve_same_as_checkout=True,
    provider_same_as_target=False,
)


class FakeCheckoutResponse(FakeResponse):
    def json(self) -> dict[str, Any]:
        return {"checkout_session_id": "cs_test", "processor_entity": "openai"}


class FakeCheckoutSession(FakeSession):
    def post(self, url: str, **kwargs: Any) -> FakeCheckoutResponse:
        assert url.endswith("/backend-api/payments/checkout")
        assert "promo_campaign" not in dict(kwargs.get("json") or {})
        return FakeCheckoutResponse()


checkout_result = engine.create_checkout(req, FakeCheckoutSession(left_proxy))
assert checkout_result["cs_id"] == "cs_test"
assert checkout_result["billing_country"] == "US"


class FakeStripeInitSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.get_params: list[dict[str, Any]] = []
        self.post_data: list[dict[str, Any]] = []

    def get(self, _url: str, params: dict[str, Any], **_kwargs: Any) -> FakeResponse:
        self.get_params.append(dict(params))
        return FakeResponse(500)

    def post(self, _url: str, data: dict[str, Any], **_kwargs: Any) -> FakeResponse:
        self.post_data.append(dict(data))
        return FakeResponse(200)


stripe_init_session = FakeStripeInitSession()
stripe_init_result = engine.stripe_init("cs_test", req, checkout=checkout_result, stripe_session=stripe_init_session)
assert stripe_init_result["stripe_hosted_url"].startswith("https://checkout.stripe.com/c/pay/cs_test")
assert len(stripe_init_session.get_params) == 2
assert len(stripe_init_session.post_data) == 1
stripe_js_id_key = "elements_session_client[stripe_js_id]"
assert stripe_init_session.get_params[0][stripe_js_id_key]
assert stripe_init_session.get_params[0][stripe_js_id_key] == stripe_init_session.post_data[0][stripe_js_id_key]
minimal_init_session = FakeStripeInitSession()
minimal_req = req.model_copy(update={"paypal_confirm_protocol": "minimal"})
minimal_init_result = engine.stripe_init(
    "cs_test",
    minimal_req,
    checkout=checkout_result,
    stripe_session=minimal_init_session,
)
assert minimal_init_result["stripe_hosted_url"].startswith("https://checkout.stripe.com/c/pay/cs_test")
assert minimal_init_session.get_params == []
assert len(minimal_init_session.post_data) == 1
assert minimal_init_session.post_data[0]["redirect_type"] == "url"
assert "_stripe_version" not in minimal_init_session.post_data[0]
assert engine.is_retryable_provider_flow_exception(
    engine.HTTPException(status_code=504, detail="provider failed: curl: (56) CONNECT tunnel failed, response 502")
)
assert engine.classify_paypal_availability(
    {"payment_method_preference": {"ordered_payment_method_types": ["card", "paypal"]}}
)["status"] == "supported"
assert engine.classify_paypal_availability(
    {"payment_method_preference": {"ordered_payment_method_types": ["card", "link"]}}
)["status"] == "unsupported"
assert engine.classify_paypal_availability({"config_id": "cfg_test"})["status"] == "unknown"
assert not engine.is_retryable_provider_flow_exception(
    engine.HTTPException(
        status_code=422,
        detail="paypal_unavailable_for_left_country: country=US; paypal_availability=unsupported",
    )
)


class FakeChainManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def proxy_for(self, upstream_proxy: str, selected_parent_proxy: str) -> str:
        self.calls.append((upstream_proxy, selected_parent_proxy))
        return "http://127.0.0.1:45000"


original_chain_manager = engine.proxy_chain_manager
fake_chain_manager = FakeChainManager()
engine.proxy_chain_manager = fake_chain_manager
chained_session = FakeSession()
engine.set_proxy_url(chained_session, left_proxy, parent_proxy)
assert chained_session.proxy == "http://127.0.0.1:45000"
assert fake_chain_manager.calls == [(left_proxy, parent_proxy)]
engine.proxy_chain_manager = original_chain_manager
billing = {
    "name": "Test User",
    "email": "test@example.com",
    "phone": "+10000000000",
    "country": "US",
    "line1": "1 Test Street",
    "city": "New York",
    "state": "NY",
    "postal_code": "10001",
}
checkout = {
    "cs_id": "cs_test",
    "billing_country": "US",
    "processor_entity": "openai",
    "currency": "USD",
}

patched_engine_names = (
    "new_session",
    "billing_for_country",
    "build_stripe_session",
    "stripe_init",
    "stripe_amount_info",
    "stripe_context",
    "stripe_create_payment_method",
    "stripe_confirm",
    "stripe_payment_page_redirect_url",
)
original_engine_values = {name: getattr(engine, name) for name in patched_engine_names}

engine.new_session = lambda: FakeSession()
engine.billing_for_country = lambda country: {**billing, "country": country}
def fake_build_stripe_session(
    _req: Any,
    proxy_override: str = "",
    source_session: FakeSession | None = None,
) -> FakeSession:
    session = FakeSession(proxy_override)
    engine.copy_session_state(source_session, session)
    stripe_sessions.append(("build", session))
    return session


engine.build_stripe_session = fake_build_stripe_session


def fake_stripe_init(*_args: Any, stripe_session: FakeSession, **_kwargs: Any) -> dict[str, str]:
    records.append(("stripe_init", stripe_session.proxy))
    stripe_session.cookies["stripe_init_cookie"] = "preserved"
    return {
        "test_amount": "0",
        "stripe_hosted_url": "https://chatgpt.com/pay/test",
        "payment_method_types": ["card", "paypal"],
    }


engine.stripe_init = fake_stripe_init
engine.stripe_update_tax_region = lambda session, *_args, **_kwargs: (
    records.append(("stripe_tax", session.proxy)) or "http_200"
)
engine.stripe_amount_info = lambda payload: (str(payload.get("test_amount", "0")), "test")
chatgpt = FakeSession(left_proxy)
chatgpt.headers.update(
    {
        "Authorization": "Bearer token",
        "oai-device-id": "device-test",
        "Cookie": "oai-did=device-test; session=test-session",
    }
)
chatgpt.cookies["session"] = "test-session"

init_payload, hosted_url, prepared_billing, prepared_stripe = engine.prepare_paypal_after_sync(
    chatgpt,
    checkout,
    req,
    provider_proxy=left_proxy,
    promotion_proxy=right_proxy,
)
assert init_payload["test_amount"] == "0"
assert hosted_url == "https://chatgpt.com/pay/test"
assert prepared_billing["country"] == "US"
assert prepared_stripe.cookies["stripe_init_cookie"] == "preserved"
assert records == [
    ("checkout_update", right_proxy),
    ("checkout_taxes", right_proxy),
    ("stripe_init", left_proxy),
    ("stripe_tax", left_proxy),
    ("stripe_init", left_proxy),
]
assert http_calls[0]["json"]["promo_campaign"]["promo_campaign_id"] == "plus-1-month-free"
assert http_calls[1]["json"]["billing_country"] == "JP"
assert http_calls[1]["json"]["currency"] == "usd"
assert http_calls[0]["session"] is http_calls[1]["session"]
for call in http_calls:
    assert call["session"].headers["oai-device-id"] == "device-test"
    assert call["session"].cookies["session"] == "test-session"

records.clear()
engine.stripe_context = lambda *_args, **_kwargs: {"checkout_amount": "0", "currency": "usd"}
engine.stripe_amount_info = lambda _payload: ("0", "test")


def fake_create_payment_method(session: FakeSession, *_args: Any, **_kwargs: Any) -> str:
    stripe_sessions.append(("payment_method", session))
    records.append(("payment_method", session.proxy))
    return "pm_test"


def fake_confirm(session: FakeSession, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    stripe_sessions.append(("confirm", session))
    records.append(("confirm", session.proxy))
    session.cookies["stripe_confirm_cookie"] = "preserved"
    return {}


def fake_poll(session: FakeSession, *_args: Any, **_kwargs: Any) -> str:
    stripe_sessions.append(("poll", session))
    records.append(("poll", session.proxy))
    return "https://www.paypal.com/agreements/approve?ba_token=test"


engine.stripe_create_payment_method = fake_create_payment_method
engine.stripe_confirm = fake_confirm
engine.stripe_payment_page_redirect_url = fake_poll
result = engine.create_provider_link(
    chatgpt,
    checkout,
    {"test_amount": "0"},
    hosted_url,
    req,
    provider_proxy=left_proxy,
    promotion_proxy=right_proxy,
    billing=billing,
    stripe_session=prepared_stripe,
)
assert result["long_url"].startswith("https://www.paypal.com/agreements/approve")
assert records == [
    ("payment_method", left_proxy),
    ("confirm", left_proxy),
    ("poll", right_proxy),
]
payment_session = next(session for label, session in stripe_sessions if label == "payment_method")
confirm_session = next(session for label, session in stripe_sessions if label == "confirm")
poll_session = next(session for label, session in stripe_sessions if label == "poll")
assert payment_session is prepared_stripe
assert confirm_session is prepared_stripe
assert poll_session is not prepared_stripe
assert poll_session.proxy == right_proxy
assert poll_session.cookies["stripe_init_cookie"] == "preserved"
assert poll_session.cookies["stripe_confirm_cookie"] == "preserved"

records.clear()
stripe_sessions.clear()
req.stripe_poll_egress = "provider"
result = engine.create_provider_link(
    chatgpt,
    checkout,
    {"test_amount": "0"},
    hosted_url,
    req,
    provider_proxy=left_proxy,
    promotion_proxy=right_proxy,
    billing=billing,
    stripe_session=prepared_stripe,
)
assert result["long_url"].startswith("https://www.paypal.com/agreements/approve")
assert records == [
    ("payment_method", left_proxy),
    ("confirm", left_proxy),
    ("poll", left_proxy),
]
provider_poll_session = next(session for label, session in stripe_sessions if label == "poll")
assert provider_poll_session is prepared_stripe
req.stripe_poll_egress = "promotion"

records.clear()
http_calls.clear()
taxes_url = "https://chatgpt.com/backend-api/payments/checkout/taxes"
http_status_by_path[taxes_url] = 500
try:
    engine.prepare_paypal_after_sync(
        chatgpt,
        checkout,
        req,
        provider_proxy=left_proxy,
        promotion_proxy=right_proxy,
    )
except engine.HTTPException as exc:
    assert exc.status_code == 500
else:
    raise AssertionError("taxes failure must stop before Stripe init")
assert records == [
    ("checkout_update", right_proxy),
    ("checkout_taxes", right_proxy),
    ("checkout_taxes", right_proxy),
    ("checkout_taxes", right_proxy),
]
for name, value in original_engine_values.items():
    setattr(engine, name, value)
print("plus engine dual pool routing ok")
