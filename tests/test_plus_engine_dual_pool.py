from __future__ import annotations

from typing import Any

from services.plus_engine import app as engine


records: list[tuple[str, str]] = []
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
    promotion_country="JP",
    approve_same_as_checkout=True,
    provider_same_as_target=False,
)


class FakeCheckoutResponse(FakeResponse):
    def json(self) -> dict[str, Any]:
        return {"checkout_session_id": "cs_test", "processor_entity": "openai"}


class FakeCheckoutSession(FakeSession):
    def post(self, url: str, **_kwargs: Any) -> FakeCheckoutResponse:
        assert url.endswith("/backend-api/payments/checkout")
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
assert engine.is_retryable_provider_flow_exception(
    engine.HTTPException(status_code=504, detail="provider failed: curl: (56) CONNECT tunnel failed, response 502")
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

engine.new_session = lambda: FakeSession()
engine.billing_for_country = lambda _country: dict(billing)
engine.build_stripe_session = lambda _req, proxy_override="": FakeSession(proxy_override)


def fake_stripe_init(*_args: Any, stripe_session: FakeSession, **_kwargs: Any) -> dict[str, str]:
    records.append(("stripe_init", stripe_session.proxy))
    return {"test_amount": "0", "stripe_hosted_url": "https://chatgpt.com/pay/test"}


engine.stripe_init = fake_stripe_init
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

init_payload, hosted_url, prepared_billing = engine.prepare_paypal_after_sync(
    chatgpt,
    checkout,
    req,
    provider_proxy=left_proxy,
    promotion_proxy=right_proxy,
)
assert init_payload["test_amount"] == "0"
assert hosted_url == "https://chatgpt.com/pay/test"
assert prepared_billing["country"] == "US"
assert records == [
    ("checkout_update", right_proxy),
    ("checkout_taxes", right_proxy),
    ("stripe_init", left_proxy),
]
assert http_calls[0]["json"]["promo_campaign"]["promo_campaign_id"] == "plus-1-month-free"
assert http_calls[1]["json"]["billing_country"] == "US"
assert http_calls[1]["json"]["currency"] == "usd"
for call in http_calls:
    assert call["session"].headers["oai-device-id"] == "device-test"
    assert call["session"].cookies["session"] == "test-session"

records.clear()
engine.stripe_context = lambda *_args, **_kwargs: {"checkout_amount": "0", "currency": "usd"}
engine.stripe_amount_info = lambda _payload: ("0", "test")
engine.stripe_create_payment_method = lambda session, *_args, **_kwargs: records.append(("payment_method", session.proxy)) or "pm_test"
engine.stripe_confirm = lambda session, *_args, **_kwargs: records.append(("confirm", session.proxy)) or {}
engine.stripe_payment_page_redirect_url = lambda session, *_args, **_kwargs: records.append(("poll", session.proxy)) or "https://www.paypal.com/agreements/approve?ba_token=test"
result = engine.create_provider_link(
    chatgpt,
    checkout,
    {"test_amount": "0"},
    hosted_url,
    req,
    provider_proxy=left_proxy,
    promotion_proxy=right_proxy,
    billing=billing,
)
assert result["long_url"].startswith("https://www.paypal.com/agreements/approve")
assert records == [
    ("payment_method", left_proxy),
    ("confirm", left_proxy),
    ("poll", right_proxy),
]

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
]
print("plus engine dual pool routing ok")
