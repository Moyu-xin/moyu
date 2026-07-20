from __future__ import annotations

import tempfile
from pathlib import Path

from services import plus_link_store


def proxy_template(country: str) -> str:
    return f"http://user-zone-custom-region-{country}-session-{{session}}:pass@proxy.test:8080"


old_config = plus_link_store._normalize_config(
    {
        "country_rotation_enabled": True,
        "billing_country_rotation": ["US", "DE", "GB"],
        "proxy_country_rotation": ["JP", "TH", "BR", "VN", "TR"],
    }
)
assert old_config["billing_country_rotation"] == ["US", "DE", "GB"]
assert old_config["proxy_country_rotation"] == ["JP", "TH", "BR", "VN", "TR"]
assert old_config["use_proxy_pool"] is False
assert old_config["use_residential_proxy_pool"] is True

country_pool = {
    code: proxy_template(code)
    for code in plus_link_store.RESIDENTIAL_PROXY_COUNTRY_CODES
}
cfg = plus_link_store._normalize_config(
    {
        "use_proxy_pool": False,
        "use_residential_proxy_pool": True,
        "residential_country_proxy_pool": country_pool,
        "country_rotation_enabled": True,
        "billing_country_rotation": plus_link_store.DEFAULT_BILLING_COUNTRY_ROTATION,
        "proxy_country_rotation": plus_link_store.DEFAULT_PROXY_COUNTRY_ROTATION,
        "target_country": "US",
        "checkout_country": "US",
        "payment_method_country": "US",
        "provider_country": "JP",
        "approve_country": "US",
        "promotion_country": "JP",
        "residential_parent_node_filter": "anytls-aw",
    }
)

service = plus_link_store.PlusLinkService(
    Path(tempfile.mkdtemp(prefix="plus-matrix-")) / "plus_link.json"
)
account = {"email": "matrix@example.com", "access_token": "token", "status": "active"}
parent_assignments: list[dict[str, object]] = []


def assign_parent(task_index: int, **kwargs: object) -> dict[str, object]:
    parent_assignments.append({"task_index": task_index, **kwargs})
    return {
        "proxy": "http://127.0.0.1:42000",
        "source": "managed_mihomo",
        "label": "Airport US Transit",
        "slot": 1,
        "node_id": "airport-test",
    }


original_assign_proxy = plus_link_store.proxy_pool_store.assign_proxy
original_get_proxy_pool_config = plus_link_store.proxy_pool_store.get_proxy_pool_config
original_probe_proxy_chain = plus_link_store.proxy_chain_service.probe_proxy_chain
plus_link_store.proxy_pool_store.assign_proxy = assign_parent
plus_link_store.proxy_pool_store.get_proxy_pool_config = lambda **_kwargs: {
    "plus_subscription_id": "sub_plus",
    "subscriptions": [{"id": "sub_plus", "name": "ex-girlfriend-cache", "enabled": True}],
}
plus_link_store.proxy_chain_service.probe_proxy_chain = lambda *_args, **_kwargs: {"ok": True, "status_code": 200}
expected = plus_link_store._matrix_country_pairs(
    plus_link_store.DEFAULT_BILLING_COUNTRY_ROTATION,
    plus_link_store.DEFAULT_PROXY_COUNTRY_ROTATION,
)
assert expected[0] == ("US", "TR")

actual: list[tuple[str, str]] = []
for index in range(1, 37):
    payload, proxy_info = service._payload_for_account(account, cfg, index)
    pair = (
        str(payload["batch_target_country"]),
        str(payload["promotion_proxy_country"]),
    )
    actual.append(pair)
    assert payload["billing_country"] == "US"
    assert payload["payment_method_country"] == pair[0]
    assert payload["run_mode"] == "combo_fallback"
    assert payload["strict_same_proxy_ip"] is False
    assert payload["batch_target_country"] == pair[0]
    assert payload["batch_checkout_proxy_country"] == "US"
    assert payload["batch_approve_proxy_country"] == "US"
    assert payload["checkout_proxy_override"] != payload["provider_proxy_override"]
    assert payload["checkout_proxy_override"] == payload["approve_proxy_override"]
    assert payload["promotion_proxy_override"] != payload["checkout_proxy_override"]
    assert payload["promotion_proxy_override"] != payload["provider_proxy_override"]
    assert payload["stripe_poll_egress"] == "provider"
    assert payload["strict_right_same_proxy_ip"] is False
    assert payload["residential_via_clash"] is True
    assert payload["residential_clash_proxy"] == "http://127.0.0.1:42000"
    assert "proxy.test:8080" in payload["checkout_proxy_override"]
    assert payload["checkout_proxy_override"] != payload["residential_clash_proxy"]
    assert "region-US" in payload["checkout_proxy_override"]
    assert f"region-{pair[0]}" in payload["provider_proxy_override"]
    assert f"region-{pair[1]}" in payload["promotion_proxy_override"]
    assert proxy_info["matrix_index"] == index
    assert proxy_info["matrix_total"] == 36

assert actual == expected
assert len(set(actual)) == 36

portable_cfg = plus_link_store._normalize_config(
    {
        "use_proxy_pool": False,
        "use_residential_proxy_pool": True,
        "residential_country_proxy_pool": {
            "US": proxy_template("US"),
            "JP": proxy_template("JP"),
        },
        "country_rotation_enabled": True,
    }
)
portable_payload, _ = service._payload_for_account(account, portable_cfg, 8)
assert portable_payload["batch_checkout_proxy_country"] == "US"
assert portable_payload["batch_target_country"] == "DE"
assert portable_payload["promotion_proxy_country"] == "VN"
assert "region-US" in portable_payload["checkout_proxy_override"]
assert "region-DE" in portable_payload["provider_proxy_override"]
assert "region-VN" in portable_payload["promotion_proxy_override"]

base_711_cfg = plus_link_store._normalize_config(
    {
        "residential_proxy_pool": "USER123456-zone-custom:fixturepass@global.example.711proxy.invalid:10000",
        "residential_country_proxy_pool": {},
        "residential_checkout_proxy": "",
        "residential_provider_proxy": "",
        "residential_approve_proxy": "",
        "residential_promotion_proxy": "",
        "country_rotation_enabled": True,
    }
)
base_711_payload, _ = service._payload_for_account(account, base_711_cfg, 1)
assert "USER123456-zone-custom-region-US-session-" in base_711_payload["checkout_proxy_override"]
assert "USER123456-zone-custom-region-TR-session-" in base_711_payload["promotion_proxy_override"]
assert "sessAuto" not in base_711_payload["checkout_proxy_override"]
assert "sessAuto" not in base_711_payload["promotion_proxy_override"]
assert ":fixturepass@global.example.711proxy.invalid:10000" in base_711_payload["checkout_proxy_override"]
assert ":fixturepass@global.example.711proxy.invalid:10000" in base_711_payload["promotion_proxy_override"]
assert parent_assignments
assert all(item.get("fallback") == "" for item in parent_assignments)
assert all(item.get("node_name_filter") == "[ex-girlfriend-cache]" for item in parent_assignments[:36])
assert all(item.get("validate_node") is False for item in parent_assignments[:36])

missing_residential_cfg = plus_link_store._normalize_config(
    {
        "use_proxy_pool": True,
        "use_residential_proxy_pool": False,
        "proxy": "http://global-proxy.example:8080",
        "residential_proxy_pool": "",
        "residential_country_proxy_pool": {},
    }
)
try:
    service._payload_for_account(account, missing_residential_cfg, 1)
except RuntimeError as error:
    assert "必须配置独立住宅代理" in str(error)
else:
    raise AssertionError("Plus extraction must not fall back to the global proxy pool")

plus_link_store.proxy_pool_store.assign_proxy = lambda *_args, **_kwargs: {
    "proxy": "",
    "source": "fallback",
    "label": "无代理",
    "error": "no project proxy",
}
try:
    service._payload_for_account(account, cfg, 1)
except RuntimeError as error:
    assert "住宅代理父级分配失败" in str(error)
else:
    raise AssertionError("Two-hop mode must fail when the project proxy parent is unavailable")
finally:
    plus_link_store.proxy_pool_store.assign_proxy = original_assign_proxy
    plus_link_store.proxy_pool_store.get_proxy_pool_config = original_get_proxy_pool_config
    plus_link_store.proxy_chain_service.probe_proxy_chain = original_probe_proxy_chain
print("plus link selected-country matrix ok")
