from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, Request

from services.plus_engine import app as plus_engine


def _request_from(host: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/local-run-key",
        "headers": [],
        "client": (host, 12345),
        "server": ("127.0.0.1", 8000),
        "scheme": "http",
        "query_string": b"",
    })


class PlusEngineSecurityTests(unittest.TestCase):
    @staticmethod
    def _jwt(payload: dict[str, object]) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
        return f"header.{encoded}.signature"

    def test_session_close_releases_local_proxy_chain_lease(self) -> None:
        class Session:
            def __init__(self) -> None:
                self.proxies: dict[str, str] = {}
                self.closed = False

            def close(self) -> None:
                self.closed = True

        manager = MagicMock()
        manager.proxy_for.return_value = "http://127.0.0.1:45000"
        session = Session()
        with patch.object(plus_engine, "proxy_chain_manager", manager):
            plus_engine.set_proxy_url(
                session,
                "http://user:pass@residential.example:10000",
                "http://parent.example:7890",
            )
            plus_engine.close_session(session)

        self.assertTrue(session.closed)
        self.assertEqual(session.proxies["https"], "http://127.0.0.1:45000")
        manager.release.assert_called_once_with("http://127.0.0.1:45000")
        self.assertEqual(session._moyu_proxy_chain_url, "")

    def test_checkout_retry_keeps_one_device_identity(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_retry_limit=2,
            batch_checkout_proxy_country="DE",
            checkout_proxy_override="http://checkout.test:8080",
        )
        attempt_device_ids: list[str] = []

        def create_checkout(attempt_request, _session):
            attempt_device_ids.append(attempt_request.device_id)
            if len(attempt_device_ids) == 1:
                raise HTTPException(
                    status_code=403,
                    detail="checkout create failed: status=403; content_type=text/html",
                )
            return {
                "cs_id": "cs_test",
                "processor_entity": "openai_ie",
                "stripe_publishable_key": "",
                "billing_country": "DE",
                "currency": "eur",
            }

        probe = plus_engine.ProxyProbeResult(
            ok=True,
            label="checkout DE",
            proxy="http://checkout.test:8080",
            ip="203.0.113.10",
            country="Germany",
            country_code="DE",
        )
        with (
            patch.object(plus_engine, "probe_proxy", return_value=probe),
            patch.object(plus_engine, "build_chatgpt_session", side_effect=lambda _request: object()),
            patch.object(plus_engine, "create_checkout", side_effect=create_checkout),
            patch.object(plus_engine, "cancellable_sleep"),
        ):
            result_request, _session, checkout = plus_engine.create_checkout_with_retry(request)

        self.assertEqual(checkout["cs_id"], "cs_test")
        self.assertTrue(result_request.device_id)
        self.assertEqual(len(set(attempt_device_ids)), 1)

    def test_locked_provider_attempt_reuses_exact_provider_and_promotion_sessions(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            provider_proxy_override="http://provider-session.test:8080",
            promotion_proxy_override="http://promotion-session.test:8080",
            batch_target_country="TH",
            promotion_proxy_country="TR",
            promotion_country="TR",
            provider_retry_limit=1,
            lock_proxy_sessions=True,
            strict_same_proxy_ip=False,
        )
        provider_probe = plus_engine.ProxyProbeResult(
            ok=True,
            label="provider TH",
            proxy=request.provider_proxy_override,
            ip="198.51.100.20",
            country="Thailand",
            country_code="TH",
        )
        promotion_probe = plus_engine.ProxyProbeResult(
            ok=True,
            label="promotion TR",
            proxy=request.promotion_proxy_override,
            ip="198.51.100.30",
            country="Turkey",
            country_code="TR",
        )
        seen: dict[str, str] = {}
        stripe_session = MagicMock()

        def prepare(_chatgpt, _checkout, _request, *, provider_proxy, promotion_proxy, emit=None):
            seen["provider"] = provider_proxy
            seen["promotion"] = promotion_proxy
            return {}, "https://checkout.stripe.com/test", {}, stripe_session

        provider_result = {
            "payment_method_id": "pm_test",
            "stripe_redirect_url": "https://pm-redirects.stripe.com/test",
            "provider_redirect_url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
            "long_url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
        }
        with (
            patch.object(plus_engine, "rotated_stage_proxy_for_attempt") as rotate_provider,
            patch.object(plus_engine, "promotion_proxy_for_provider_attempt") as rotate_promotion,
            patch.object(plus_engine, "check_provider_proxy", return_value=provider_probe),
            patch.object(plus_engine, "promotion_probe_for_provider_attempt", return_value=promotion_probe),
            patch.object(plus_engine, "prepare_paypal_after_sync", side_effect=prepare),
            patch.object(plus_engine, "create_provider_link", return_value=provider_result),
        ):
            result = plus_engine.create_provider_link_with_retry(
                object(),
                {},
                request,
                max_attempts_override=1,
                attempt_offset=1,
                attempt_limit=2,
            )

        self.assertEqual(seen["provider"], request.provider_proxy_override)
        self.assertEqual(seen["promotion"], request.promotion_proxy_override)
        self.assertEqual(result["long_url"], provider_result["long_url"])
        stripe_session.close.assert_called_once()
        rotate_provider.assert_not_called()
        rotate_promotion.assert_not_called()

    def test_proxy_quality_uses_options_for_promotion_update_route(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://checkout.test:8080",
            provider_proxy_override="http://provider.test:8080",
            promotion_proxy_override="http://promotion.test:8080",
            batch_checkout_proxy_country="DE",
            batch_target_country="ID",
            promotion_proxy_country="TR",
        )
        ips = {
            "Checkout/Approve": ("198.51.100.10", "DE"),
            "Provider/PM/Poll": ("198.51.100.20", "ID"),
            "Promotion/Taxes": ("198.51.100.30", "TR"),
        }
        target_methods: list[tuple[str, str]] = []

        def probe(label, proxy, expected_country="", required=False, parent_proxy="", required_ip_version=0):
            ip, country = ips[label]
            return plus_engine.ProxyProbeResult(
                ok=True,
                label=label,
                proxy=proxy,
                ip=ip,
                country_code=country,
            )

        def target(label, _proxy, _url, **kwargs):
            target_methods.append((label, kwargs.get("method", "GET")))
            return {"ok": True, "status_code": 204 if kwargs.get("method") == "OPTIONS" else 401, "latency_ms": 20}

        with (
            patch.object(plus_engine, "probe_proxy", side_effect=probe),
            patch.object(plus_engine, "probe_proxy_target", side_effect=target),
        ):
            result = plus_engine.check_request_proxy_quality(request, rounds=1)

        self.assertTrue(result["ok"])
        self.assertEqual(target_methods, [
            ("Checkout/Approve", "GET"),
            ("Provider/PM/Poll", "GET"),
            ("Promotion/Taxes", "OPTIONS"),
        ])

    def test_proxy_quality_allows_provider_and_promotion_to_share_same_country_ip(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://checkout.test:8080",
            provider_proxy_override="http://provider.test:8080",
            promotion_proxy_override="http://provider.test:8080",
            batch_checkout_proxy_country="GB",
            batch_target_country="TH",
            promotion_proxy_country="TH",
        )
        ips = {
            "Checkout/Approve": ("198.51.100.10", "GB"),
            "Provider/PM/Poll": ("198.51.100.20", "TH"),
            "Promotion/Taxes": ("198.51.100.20", "TH"),
        }

        def probe(label, proxy, expected_country="", required=False, parent_proxy="", required_ip_version=0):
            ip, country = ips[label]
            return plus_engine.ProxyProbeResult(
                ok=True,
                label=label,
                proxy=proxy,
                ip=ip,
                country_code=country,
            )

        with (
            patch.object(plus_engine, "probe_proxy", side_effect=probe),
            patch.object(
                plus_engine,
                "probe_proxy_target",
                return_value={"ok": True, "status_code": 204, "latency_ms": 20},
            ),
        ):
            result = plus_engine.check_request_proxy_quality(request, rounds=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["stages"][1]["ip"], result["stages"][2]["ip"])

    def test_proxy_quality_rejects_provider_and_promotion_shared_ip_for_different_countries(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://checkout.test:8080",
            provider_proxy_override="http://provider.test:8080",
            promotion_proxy_override="http://promotion.test:8080",
            batch_checkout_proxy_country="GB",
            batch_target_country="TH",
            promotion_proxy_country="JP",
        )
        ips = {
            "Checkout/Approve": ("198.51.100.10", "GB"),
            "Provider/PM/Poll": ("198.51.100.20", "TH"),
            "Promotion/Taxes": ("198.51.100.20", "JP"),
        }

        def probe(label, proxy, expected_country="", required=False, parent_proxy="", required_ip_version=0):
            ip, country = ips[label]
            return plus_engine.ProxyProbeResult(
                ok=True,
                label=label,
                proxy=proxy,
                ip=ip,
                country_code=country,
            )

        with (
            patch.object(plus_engine, "probe_proxy", side_effect=probe),
            patch.object(
                plus_engine,
                "probe_proxy_target",
                return_value={"ok": True, "status_code": 204, "latency_ms": 20},
            ),
        ):
            result = plus_engine.check_request_proxy_quality(request, rounds=1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], "promotion")
        self.assertIn("共用出口", result["error"])

    def test_parent_proxy_rotation_switches_project_node_for_retry(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            residential_via_clash=True,
            residential_clash_proxy="http://127.0.0.1:42000",
            parent_proxy_rotation_enabled=True,
            parent_proxy_rotation_task_index=40,
            parent_proxy_rotation_total_threads=3,
            parent_proxy_rotation_slot=1,
            parent_proxy_rotation_node_filter="[ex-girlfriend]",
        )
        assignment = {
            "proxy": "http://127.0.0.1:42001",
            "source": "managed_mihomo",
            "label": "[ex-girlfriend] jp-anytls-aw",
        }
        with patch.object(plus_engine.proxy_pool_store, "assign_proxy", return_value=assignment) as assign:
            rotated, selected = plus_engine.rotate_parent_proxy_for_attempt(request, 2)

        self.assertEqual(rotated.residential_clash_proxy, assignment["proxy"])
        self.assertEqual(selected["label"], assignment["label"])
        assign.assert_called_once_with(
            41,
            fallback="",
            total_threads=3,
            slot_index=1,
            node_name_filter="[ex-girlfriend]",
            validate_node=False,
            purpose="plus_parent_proxy",
        )

    def test_promotion_retry_emits_specific_failure_category(self) -> None:
        request = plus_engine.LongLinkRequest(accessToken="token")
        action = MagicMock(side_effect=[RuntimeError("operation timed out"), {"ok": True}])
        events: list[tuple[str, str, dict[str, object]]] = []
        with (
            patch.object(plus_engine, "build_chatgpt_session", side_effect=[object(), object()]),
            patch.object(plus_engine, "cancellable_sleep"),
        ):
            _session, result = plus_engine.run_checkout_action_with_sticky_retry(
                request,
                object(),
                "http://promotion.test:8080",
                action,
                label="Promotion update",
                attempts=2,
                emit=lambda step, message, **extra: events.append((step, message, extra)),
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(action.call_count, 2)
        self.assertEqual(events[0][2]["failure_category"], "promotion_update")

    def test_strict_retry_quality_checks_and_locks_new_sessions_before_checkout(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            billing_country="DE",
            payment_method_country="ID",
            promotion_country="TR",
            checkout_proxy_override="http://USER-zone-custom-region-DE-session-111-sessTime-5:pass@proxy.test:8080",
            provider_proxy_override="http://USER-zone-custom-region-ID-session-222-sessTime-5:pass@proxy.test:8080",
            promotion_proxy_override="http://USER-zone-custom-region-TR-session-333-sessTime-5:pass@proxy.test:8080",
            checkout_proxy_pool=["http://USER-zone-custom-region-DE-session-111-sessTime-5:pass@proxy.test:8080"],
            provider_proxy_pool=["http://USER-zone-custom-region-ID-session-222-sessTime-5:pass@proxy.test:8080"],
            promotion_proxy_pool=["http://USER-zone-custom-region-TR-session-333-sessTime-5:pass@proxy.test:8080"],
            batch_checkout_proxy_country="DE",
            batch_target_country="ID",
            promotion_proxy_country="TR",
            provider_retry_limit=2,
            prevalidated_proxy_sessions=True,
            proxy_quality_gate_enabled=True,
            strict_same_proxy_ip=False,
        )
        checkout_calls: list[plus_engine.LongLinkRequest] = []
        provider_calls: list[plus_engine.LongLinkRequest] = []
        quality_calls: list[plus_engine.LongLinkRequest] = []

        def create_checkout(req, **_kwargs):
            checkout_calls.append(req)
            if len(checkout_calls) == 1:
                raise HTTPException(status_code=502, detail="TLS connect error")
            return req, object(), {
                "cs_id": "cs_test",
                "processor_entity": "openai_ie",
                "stripe_publishable_key": "",
                "billing_country": "DE",
                "currency": "eur",
            }

        def quality(req, **_kwargs):
            quality_calls.append(req)
            if len(quality_calls) < 3:
                return {
                    "ok": False,
                    "failed_stage": "checkout",
                    "error": "candidate proxy failed",
                    "stages": [],
                }
            return {
                "ok": True,
                "stages": [
                    {"stage": "checkout", "label": "Checkout/Approve", "ok": True, "ip": "198.51.100.10", "country": "DE"},
                    {"stage": "provider", "label": "Provider/PM/Poll", "ok": True, "ip": "198.51.100.20", "country": "ID"},
                    {"stage": "promotion", "label": "Promotion/Taxes", "ok": True, "ip": "198.51.100.30", "country": "TR"},
                ],
            }

        def create_provider(_session, _checkout, req, **_kwargs):
            provider_calls.append(req)
            return {
                "payment_method_id": "pm_test",
                "stripe_redirect_url": "https://pm-redirects.stripe.com/test",
                "provider_redirect_url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                "stripe_hosted_url": "https://checkout.stripe.com/test",
                "long_url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                "link_attempt_count": 2,
                "link_attempt_limit": 2,
            }

        with (
            patch.object(plus_engine, "create_checkout_with_retry", side_effect=create_checkout),
            patch.object(plus_engine, "check_request_proxy_quality", side_effect=quality),
            patch.object(plus_engine, "create_provider_link_with_retry", side_effect=create_provider),
            patch.object(plus_engine, "cancellable_sleep"),
        ):
            result = plus_engine.run_strict_sticky_combo(request, "DE", "ID", False, [])

        self.assertTrue(result.ok)
        self.assertEqual(len(checkout_calls), 2)
        self.assertEqual(len(quality_calls), 3)
        self.assertEqual(len(provider_calls), 1)
        self.assertTrue(provider_calls[0].lock_proxy_sessions)
        self.assertNotEqual(quality_calls[0].checkout_proxy_override, request.checkout_proxy_override)

    def test_strict_retry_rebuilds_checkout_after_nonzero_amount(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            billing_country="DE",
            payment_method_country="TH",
            promotion_country="JP",
            checkout_proxy_override="http://USER-zone-custom-region-DE-session-111-sessTime-5:pass@proxy.test:8080",
            provider_proxy_override="http://USER-zone-custom-region-TH-session-222-sessTime-5:pass@proxy.test:8080",
            promotion_proxy_override="http://USER-zone-custom-region-JP-session-333-sessTime-5:pass@proxy.test:8080",
            checkout_proxy_pool=["http://USER-zone-custom-region-DE-session-111-sessTime-5:pass@proxy.test:8080"],
            provider_proxy_pool=["http://USER-zone-custom-region-TH-session-222-sessTime-5:pass@proxy.test:8080"],
            promotion_proxy_pool=["http://USER-zone-custom-region-JP-session-333-sessTime-5:pass@proxy.test:8080"],
            batch_checkout_proxy_country="DE",
            batch_target_country="TH",
            promotion_proxy_country="JP",
            provider_retry_limit=2,
            prevalidated_proxy_sessions=True,
            proxy_quality_gate_enabled=False,
            strict_same_proxy_ip=False,
        )
        checkout_calls: list[plus_engine.LongLinkRequest] = []
        checkout_sessions: list[MagicMock] = []
        provider_calls: list[plus_engine.LongLinkRequest] = []

        def create_checkout(req, **_kwargs):
            checkout_calls.append(req)
            session = MagicMock()
            checkout_sessions.append(session)
            return req, session, {
                "cs_id": f"cs_test_{len(checkout_calls)}",
                "processor_entity": "openai_ie",
                "stripe_publishable_key": "",
                "billing_country": "DE",
                "currency": "eur",
            }

        def create_provider(_session, _checkout, req, **_kwargs):
            provider_calls.append(req)
            if len(provider_calls) == 1:
                raise HTTPException(
                    status_code=402,
                    detail="stripe_amount_nonzero: amount=1933, currency=eur, source=elements_options.amount",
                )
            return {
                "payment_method_id": "pm_test",
                "stripe_redirect_url": "https://pm-redirects.stripe.com/test",
                "provider_redirect_url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                "stripe_hosted_url": "https://checkout.stripe.com/test",
                "long_url": "https://www.paypal.com/agreements/approve?ba_token=BA-test",
                "link_attempt_count": 2,
                "link_attempt_limit": 2,
            }

        with (
            patch.object(plus_engine, "create_checkout_with_retry", side_effect=create_checkout),
            patch.object(plus_engine, "create_provider_link_with_retry", side_effect=create_provider),
            patch.object(plus_engine, "cancellable_sleep"),
        ):
            result = plus_engine.run_strict_sticky_combo(request, "DE", "TH", False, [])

        self.assertTrue(result.ok)
        self.assertEqual(result.cs_id, "cs_test_2")
        self.assertEqual(len(checkout_calls), 2)
        self.assertEqual(len(provider_calls), 2)
        self.assertTrue(all(session.close.call_count == 1 for session in checkout_sessions))

    def test_checkout_retry_override_prevents_nested_request_burst(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_retry_limit=10,
            device_id="stable-device",
            batch_checkout_proxy_country="DE",
            checkout_proxy_override="http://checkout.test:8080",
        )
        probe = plus_engine.ProxyProbeResult(
            ok=True,
            label="checkout DE",
            proxy="http://checkout.test:8080",
            ip="203.0.113.10",
            country="Germany",
            country_code="DE",
        )
        blocked = HTTPException(
            status_code=403,
            detail="checkout create failed: status=403; content_type=text/html",
        )
        with (
            patch.object(plus_engine, "probe_proxy", return_value=probe),
            patch.object(plus_engine, "build_chatgpt_session", return_value=object()),
            patch.object(plus_engine, "create_checkout", side_effect=blocked) as create_checkout,
        ):
            with self.assertRaises(HTTPException):
                plus_engine.create_checkout_with_retry(
                    request,
                    max_attempts_override=1,
                    rotate_first_attempt=False,
                )

        create_checkout.assert_called_once()
        attempted_request = create_checkout.call_args.args[0]
        self.assertEqual(attempted_request.checkout_proxy_override, request.checkout_proxy_override)

    def test_checkout_html_403_uses_browser_fallback_without_inner_burst(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_retry_limit=10,
            device_id="stable-device",
            batch_checkout_proxy_country="DE",
            checkout_proxy_override="http://checkout.test:8080",
        )
        probe = plus_engine.ProxyProbeResult(
            ok=True,
            label="checkout DE",
            proxy="http://checkout.test:8080",
            ip="203.0.113.10",
            country="Germany",
            country_code="DE",
        )
        blocked = HTTPException(
            status_code=403,
            detail="checkout create failed: status=403; content_type=text/html",
        )
        browser_result = {
            "cs_id": "cs_browser",
            "processor_entity": "openai_ie",
            "stripe_publishable_key": "pk_test",
            "billing_country": "DE",
            "currency": "eur",
        }
        with (
            patch.object(plus_engine, "probe_proxy", return_value=probe),
            patch.object(plus_engine, "build_chatgpt_session", return_value=object()),
            patch.object(plus_engine, "create_checkout", side_effect=blocked) as create_checkout,
            patch.object(
                plus_engine,
                "create_checkout_via_playwright",
                return_value=browser_result,
            ) as browser_checkout,
        ):
            result_request, _session, checkout = plus_engine.create_checkout_with_retry(
                request,
                max_attempts_override=1,
                rotate_first_attempt=False,
            )

        create_checkout.assert_called_once()
        browser_checkout.assert_called_once_with(result_request)
        self.assertEqual(checkout["cs_id"], "cs_browser")
        self.assertEqual(checkout["checkout_proxy_ip"], "203.0.113.10")

    def test_ipv6_egress_mode_rejects_ipv4_and_accepts_ipv6(self) -> None:
        session = MagicMock()
        with (
            patch.object(plus_engine, "new_session", return_value=session),
            patch.object(plus_engine, "fetch_proxy_geo", return_value=("203.0.113.10", "BR", "Brazil")),
        ):
            ipv4_result = plus_engine.probe_proxy(
                "provider BR",
                "http://proxy.invalid:8080",
                "BR",
                required=True,
                required_ip_version=6,
            )

        self.assertFalse(ipv4_result.ok)
        self.assertIn("出口不是 IPv6", ipv4_result.error)

        with (
            patch.object(plus_engine, "new_session", return_value=session),
            patch.object(plus_engine, "fetch_proxy_geo", return_value=("2001:db8::10", "BR", "Brazil")),
        ):
            ipv6_result = plus_engine.probe_proxy(
                "provider BR",
                "http://proxy.invalid:8080",
                "BR",
                required=True,
                required_ip_version=6,
            )

        self.assertTrue(ipv6_result.ok)
        self.assertEqual(ipv6_result.error, "")
        self.assertEqual(session.close.call_count, 2)

    def test_ipv6_requirement_is_derived_from_request(self) -> None:
        self.assertEqual(plus_engine.required_proxy_ip_version(plus_engine.LongLinkRequest(accessToken="token")), 0)
        self.assertEqual(
            plus_engine.required_proxy_ip_version(
                plus_engine.LongLinkRequest(accessToken="token", require_ipv6_egress=True)
            ),
            6,
        )

    def test_right_side_ip_lock_is_independent_from_ipv6_mode(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            require_ipv6_egress=False,
            strict_right_same_proxy_ip=True,
        )
        self.assertEqual(plus_engine.required_proxy_ip_version(request), 0)
        self.assertTrue(plus_engine.right_stage_same_proxy_required(request))
        self.assertEqual(
            plus_engine.promotion_proxy_for_provider_attempt(
                request.model_copy(
                    update={
                        "provider_proxy_override": "http://right-sticky.test:8080",
                        "promotion_proxy_override": "http://right-old.test:8080",
                        "batch_target_country": "BR",
                        "promotion_proxy_country": "BR",
                    }
                ),
                "http://right-sticky.test:8080",
                "BR",
            ),
            "http://right-sticky.test:8080",
        )

    def test_shared_right_stage_reuses_provider_probe_without_second_connection(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            batch_target_country="TH",
            promotion_proxy_country="TH",
            strict_right_same_proxy_ip=True,
        )
        provider_probe = plus_engine.ProxyProbeResult(
            ok=True,
            label="provider TH",
            proxy="http://right-sticky.test:8080",
            ip="203.0.113.20",
            country="Thailand",
            country_code="TH",
        )

        with patch.object(plus_engine, "probe_proxy") as probe:
            promotion_probe = plus_engine.promotion_probe_for_provider_attempt(
                request,
                provider_probe,
                "http://right-sticky.test:8080",
                "TH",
            )

        probe.assert_not_called()
        self.assertEqual(promotion_probe.ip, provider_probe.ip)
        self.assertEqual(promotion_probe.country_code, "TH")

    def test_provider_poll_reuse_skips_another_geo_connection(self) -> None:
        request = plus_engine.LongLinkRequest(accessToken="token")
        session = MagicMock()
        session.proxies = {"https": "http://right-sticky.test:8080"}

        with patch.object(plus_engine, "verify_right_session_exit") as verify:
            result = plus_engine.verify_poll_session_exit(
                request,
                session,
                expected_ip="203.0.113.20",
                expected_country="TH",
                label="right session before poll",
                reuse_provider_session=True,
            )

        verify.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(result.ip, "203.0.113.20")

    def test_ipv6_match_normalizes_equivalent_text_forms(self) -> None:
        self.assertTrue(plus_engine.proxy_ip_matches("2001:0db8:0:0::1", "2001:db8::1"))
        self.assertFalse(plus_engine.proxy_ip_matches("2001:db8::1", "2001:db8::2"))

    def test_combo_failure_summary_preserves_stage_and_http_status(self) -> None:
        summary = plus_engine.summarize_combo_failure(
            "provider failed: checkout promotion update failed via /checkout/update: "
            "status=403, content_type=text/html; charset=utf-8, title=Just a moment"
        )
        self.assertIn("checkout promotion update failed", summary)
        self.assertIn("status=403", summary)
        self.assertIn("content_type=text/html", summary)
        self.assertIn("title=Just a moment", summary)

    def test_combo_failure_summary_preserves_submission_failure_fields(self) -> None:
        summary = plus_engine.summarize_combo_failure(
            "stripe submission failed: submission_state=failed, "
            "submission_reason=provider_declined, submission_code=paypal_error, "
            "submission_message=provider rejected"
        )
        self.assertIn("stripe submission failed", summary)
        self.assertIn("state=failed", summary)
        self.assertIn("reason=provider_declined", summary)
        self.assertIn("code=paypal_error", summary)
        self.assertIn("message=provider rejected", summary)

    def test_confirm_request_profile_does_not_store_identifiers(self) -> None:
        profile = plus_engine.stripe_confirm_request_profile(
            {
                "eid": "NA",
                "payment_method": "pm_secret_payment_method",
                "init_checksum": "checksum-secret",
                "expected_amount": "0",
                "expected_payment_method_type": "paypal",
                "return_url": (
                    "https://pay.openai.com/c/pay/cs_live_secret_session"
                    "?redirect_pm_type=paypal&lid=secret-lid&ui_mode=custom"
                ),
                "referrer": "https://pay.openai.com/c/pay/cs_live_secret_session",
                "guid": "guid-secret",
                "muid": "muid-secret",
                "sid": "sid-secret",
                "last_displayed_line_item_group_details[subtotal]": "0",
            }
        )
        serialized = json.dumps(profile, sort_keys=True)
        self.assertNotIn("pm_secret", serialized)
        self.assertNotIn("cs_live_secret", serialized)
        self.assertNotIn("checksum-secret", serialized)
        self.assertNotIn("secret-lid", serialized)
        self.assertEqual(profile["return_url_path"], "/c/pay/{checkout_session}")
        self.assertEqual(profile["return_url_query_keys"], ["lid", "redirect_pm_type", "ui_mode"])

    def test_submission_profile_preserves_failure_codes_without_urls(self) -> None:
        profile = plus_engine.stripe_submission_profile(
            {
                "submission_attempt": {
                    "state": "failed",
                    "error": {
                        "reason": "generic_decline",
                        "code": "checkout_approval_payment_failure_with_payment_error",
                    },
                },
                "next_action": {
                    "url": "https://pm-redirects.stripe.com/authorize/acct_secret/sa_secret"
                },
            }
        )
        serialized = json.dumps(profile, sort_keys=True)
        self.assertEqual(profile["submission_state"], "failed")
        self.assertEqual(profile["submission_reason"], "generic_decline")
        self.assertNotIn("acct_secret", serialized)
        self.assertNotIn("sa_secret", serialized)
        self.assertIn("pm-redirects.stripe.com", profile["url_hosts"])

    def test_stripe_confirm_refreshes_state_and_matches_paypal_shape(self) -> None:
        class FakeResponse:
            def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
                self._payload = payload
                self.status_code = status_code
                self.text = json.dumps(payload)

            def json(self) -> dict[str, object]:
                return self._payload

        class FakeStripe:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.responses = [
                    FakeResponse(
                        {
                            "init_checksum": "checksum-after-tax",
                            "total_summary": {"due": 0, "total": 0, "subtotal": 2000},
                            "invoice": {
                                "amount_due": 2000,
                                "billing_cycle_anchor": "2026-08-15T00:00:00Z",
                            },
                        }
                    ),
                    FakeResponse({"init_checksum": "checksum-after-pre-confirm"}),
                    FakeResponse({"submission_attempt": {"state": "requires_approval"}}),
                ]

            def post(self, url: str, **kwargs: object) -> FakeResponse:
                self.calls.append({"url": url, **kwargs})
                return self.responses.pop(0)

        stripe = FakeStripe()
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            billing_country="US",
            paypal_confirm_protocol="enhanced",
        )
        checkout = {
            "cs_id": "cs_live_secret",
            "billing_country": "US",
            "processor_entity": "openai_llc",
        }
        init_payload = {
            "init_checksum": "checksum-from-init",
            "config_id": "config-secret",
            "stripe_hosted_url": (
                "https://checkout.stripe.com/c/pay/cs_live_secret"
                "?returned_from_redirect=true&ui_mode=custom"
            ),
        }
        ctx = {
            "guid": "guid-fixed",
            "muid": "muid-fixed",
            "sid": "sid-fixed",
            "stripe_js_id": "stripe-js-fixed",
            "elements_session_id": "elements-session-fixed",
            "elements_session_config_id": "elements-config-fixed",
            "config_id": "config-secret",
            "init_checksum": "checksum-from-init",
            "checkout_amount": "0",
            "locale": "en",
            "runtime_version": "runtime-fixed",
        }
        events: list[dict[str, object]] = []

        payload = plus_engine.stripe_confirm(
            stripe,
            "cs_live_secret",
            "pm_secret",
            "pk_secret",
            init_payload,
            ctx,
            checkout,
            request,
            str(init_payload["stripe_hosted_url"]),
            billing={
                "country": "US",
                "state": "NY",
                "postal_code": "10001",
                "line1": "1 Test Street",
                "city": "New York",
            },
            emit=lambda step, message, **extra: events.append({"step": step, "message": message, **extra}),
        )

        self.assertEqual(payload["submission_attempt"]["state"], "requires_approval")
        self.assertEqual(
            [str(call["url"]).rsplit("/", 1)[-1] for call in stripe.calls],
            ["cs_live_secret", "pre_confirm", "confirm"],
        )
        confirm_call = stripe.calls[-1]
        body = confirm_call["data"]
        self.assertIsInstance(body, dict)
        assert isinstance(body, dict)
        self.assertEqual(body["eid"], "NA")
        self.assertEqual(body["init_checksum"], "checksum-after-pre-confirm")
        self.assertEqual(body["expected_amount"], "0")
        self.assertEqual(body["expected_amount_on_bca"], "2000")
        self.assertEqual(body["last_displayed_line_item_group_details[subtotal]"], "2000")
        self.assertEqual(body["last_displayed_line_item_group_details[total_discount_amount]"], "2000")
        self.assertEqual(body["guid"], "guid-fixed")
        return_query = dict(plus_engine.parse_qsl(plus_engine.urlsplit(str(body["return_url"])).query))
        self.assertEqual(return_query["ui_mode"], "custom")
        self.assertNotIn("redirect_pm_type", return_query)
        self.assertNotIn("lid", return_query)
        self.assertTrue(return_query["success_return_url"].startswith("https://chatgpt.com/checkout/verify?"))
        confirm_headers = confirm_call["headers"]
        self.assertIsInstance(confirm_headers, dict)
        assert isinstance(confirm_headers, dict)
        self.assertTrue(str(confirm_headers["Referer"]).startswith("https://pay.openai.com/c/pay/"))
        request_profile = next(
            event["stripe_confirm_request_profile"]
            for event in events
            if "stripe_confirm_request_profile" in event
        )
        self.assertEqual(request_profile["confirm_mode"], "payment_method")
        self.assertNotIn("pm_secret", json.dumps(request_profile, sort_keys=True))
        self.assertNotIn("cs_live_secret", json.dumps(request_profile, sort_keys=True))

        direct_stripe = FakeStripe()
        direct_events: list[dict[str, object]] = []
        plus_engine.stripe_confirm(
            direct_stripe,
            "cs_live_secret",
            "",
            "pk_secret",
            init_payload,
            dict(ctx),
            checkout,
            request,
            str(init_payload["stripe_hosted_url"]),
            billing={
                "email": "secret@example.com",
                "country": "US",
                "state": "NY",
                "postal_code": "10001",
                "line1": "1 Test Street",
                "city": "New York",
            },
            emit=lambda step, message, **extra: direct_events.append({"step": step, "message": message, **extra}),
        )
        direct_body = direct_stripe.calls[-1]["data"]
        self.assertIsInstance(direct_body, dict)
        assert isinstance(direct_body, dict)
        self.assertNotIn("payment_method", direct_body)
        self.assertEqual(direct_body["payment_method_data[type]"], "paypal")
        self.assertEqual(direct_body["payment_method_data[billing_details][email]"], "secret@example.com")
        self.assertEqual(direct_body["elements_session_client[session_id]"], "elements-session-fixed")
        self.assertEqual(
            direct_body["client_attribution_metadata[checkout_session_id]"],
            "cs_live_secret",
        )
        direct_profile = next(
            event["stripe_confirm_request_profile"]
            for event in direct_events
            if "stripe_confirm_request_profile" in event
        )
        self.assertEqual(direct_profile["confirm_mode"], "direct")
        self.assertTrue(direct_profile["payment_method_data_present"])
        self.assertNotIn("secret@example.com", json.dumps(direct_profile, sort_keys=True))
        self.assertNotIn("1 Test Street", json.dumps(direct_profile, sort_keys=True))

    def test_promotion_html_403_retries_with_clean_right_pool_session(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            billing_country="US",
            payment_method_country="US",
            promotion_country="TR",
            promotion_proxy_override="http://right.test:8080",
            provider_proxy_override="http://left.test:8080",
        )
        inherited_session = object()
        clean_session = object()
        stripe_session = object()
        checkout_session = object()
        checkout = {
            "cs_id": "cs_test",
            "billing_country": "US",
            "processor_entity": "openai_llc",
            "currency": "usd",
        }
        forbidden = HTTPException(
            status_code=403,
            detail="checkout promotion update failed: status=403, content_type=text/html",
        )
        with (
            patch.object(plus_engine, "billing_for_country", return_value={"country": "US"}),
            patch.object(
                plus_engine,
                "build_chatgpt_session",
                side_effect=[inherited_session, clean_session],
            ) as build_chatgpt,
            patch.object(
                plus_engine,
                "update_checkout_promotion",
                side_effect=[forbidden, {}],
            ) as update_promotion,
            patch.object(plus_engine, "update_checkout_taxes", return_value={}) as update_taxes,
            patch.object(plus_engine, "build_stripe_session", return_value=stripe_session),
            patch.object(plus_engine, "stripe_update_tax_region", return_value="http_200"),
            patch.object(
                plus_engine,
                "stripe_init",
                return_value={"stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_test"},
            ),
        ):
            plus_engine.prepare_paypal_after_sync(
                checkout_session,
                checkout,
                request,
                provider_proxy="http://left.test:8080",
                promotion_proxy="http://right.test:8080",
            )

        self.assertIs(build_chatgpt.call_args_list[0].kwargs["source_session"], checkout_session)
        self.assertNotIn("source_session", build_chatgpt.call_args_list[1].kwargs)
        self.assertEqual(update_promotion.call_args_list[1].args[0], clean_session)
        self.assertEqual(update_taxes.call_args.args[0], clean_session)

    def test_promotion_connect_failure_retries_same_sticky_proxy(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            device_id="stable-device",
            promotion_proxy_override="http://right-sticky.test:8080",
        )
        inherited_session = object()
        clean_session = object()
        action = MagicMock(side_effect=[RuntimeError("CONNECT tunnel failed, response 502"), {"ok": True}])
        with (
            patch.object(
                plus_engine,
                "build_chatgpt_session",
                side_effect=[inherited_session, clean_session],
            ) as build_session,
            patch.object(plus_engine, "cancellable_sleep"),
        ):
            returned_session, result = plus_engine.run_checkout_action_with_sticky_retry(
                request,
                object(),
                "http://right-sticky.test:8080",
                action,
                label="Promotion update",
            )

        self.assertIs(returned_session, clean_session)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(action.call_args_list[0].args[0], inherited_session)
        self.assertEqual(action.call_args_list[1].args[0], clean_session)
        self.assertEqual(
            [call.kwargs["proxy_override"] for call in build_session.call_args_list],
            ["http://right-sticky.test:8080", "http://right-sticky.test:8080"],
        )

    def test_standalone_confirm_uses_checkout_return_url_not_path_suffix(self) -> None:
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, object]:
                return {"submission_attempt": {"state": "requires_approval"}}

        class Stripe:
            def __init__(self) -> None:
                self.data: dict[str, object] = {}

            def post(self, _url: str, **kwargs: object) -> Response:
                self.data = dict(kwargs.get("data") or {})
                return Response()

        stripe = Stripe()
        request = plus_engine.LongLinkRequest(accessToken="token", paypal_confirm_protocol="standalone")
        plus_engine.stripe_confirm(
            stripe,
            "cs_live_test",
            "pm_test",
            "pk_live_test",
            {
                "init_checksum": "checksum",
                "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_test",
            },
            {
                "stripe_js_id": "stripe-js",
                "elements_session_id": "elements-session",
                "elements_session_config_id": "elements-config",
                "config_id": "config",
                "checkout_amount": "0",
                "locale": "en",
            },
            {"billing_country": "US", "processor_entity": "openai_llc"},
            request,
            "https://checkout.stripe.com/c/pay/cs_live_test",
        )

        return_url = str(stripe.data["return_url"])
        self.assertFalse(return_url.endswith("/confirm"))
        query = dict(plus_engine.parse_qsl(plus_engine.urlsplit(return_url).query))
        self.assertNotIn("redirect_pm_type", query)
        self.assertTrue(query["success_return_url"].startswith("https://chatgpt.com/checkout/verify?"))
        self.assertEqual(stripe.data["version"], plus_engine.DEFAULT_STRIPE_RUNTIME_VERSION)
        self.assertEqual(len(str(stripe.data["guid"])), 32)
        self.assertNotIn("-", str(stripe.data["guid"]))

    def test_standalone_payment_method_matches_target_attribution_shape(self) -> None:
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, str]:
                return {"id": "pm_test"}

        class Stripe:
            def __init__(self) -> None:
                self.data: dict[str, object] = {}

            def post(self, _url: str, **kwargs: object) -> Response:
                self.data = dict(kwargs.get("data") or {})
                return Response()

        stripe = Stripe()
        payment_method = plus_engine.stripe_create_payment_method(
            stripe,
            "cs_test",
            "pk_test",
            {"name": "Test User", "email": "test@example.com", "country": "US"},
            {
                "runtime_version": "runtime-test",
                "stripe_js_id": "stripe-js",
                "elements_session_id": "elements-session",
                "elements_session_config_id": "elements-config",
                "config_id": "config",
            },
            protocol="standalone",
        )

        self.assertEqual(payment_method, "pm_test")
        self.assertIn("stripe.js/runtime-test", str(stripe.data["payment_user_agent"]))
        self.assertEqual(
            stripe.data["client_attribution_metadata[merchant_integration_additional_elements][0]"],
            "payment",
        )
        self.assertEqual(
            stripe.data["client_attribution_metadata[merchant_integration_additional_elements][1]"],
            "address",
        )
        self.assertEqual(stripe.data["_stripe_version"], plus_engine.STRIPE_VERSION_FULL)

    def test_payment_page_failed_state_has_three_sync_grace_polls(self) -> None:
        class Response:
            status_code = 200
            text = ""

            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload

            def json(self) -> dict[str, object]:
                return self.payload

        class Stripe:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, _url: str, **_kwargs: object) -> Response:
                self.calls += 1
                if self.calls <= 3:
                    return Response({"submission_attempt": {"state": "failed"}})
                return Response(
                    {
                        "next_action": {
                            "type": "redirect_to_url",
                            "redirect_to_url": {"url": "https://pm-redirects.stripe.com/authorize/test"},
                        }
                    }
                )

        stripe = Stripe()
        request = plus_engine.LongLinkRequest(accessToken="token")
        with patch.object(plus_engine, "cancellable_sleep"):
            redirect = plus_engine.stripe_payment_page_redirect_url(
                stripe,
                "cs_test",
                "pk_test",
                request,
                {"elements_session_id": "elements", "stripe_js_id": "stripe-js", "locale": "en"},
                timeout_seconds=30,
            )

        self.assertEqual(redirect, "https://pm-redirects.stripe.com/authorize/test")
        self.assertEqual(stripe.calls, 4)

    def test_payment_page_retries_transient_poll_network_error(self) -> None:
        class Response:
            status_code = 200
            text = ""

            def json(self) -> dict[str, object]:
                return {
                    "next_action": {
                        "type": "redirect_to_url",
                        "redirect_to_url": {"url": "https://pm-redirects.stripe.com/authorize/test"},
                    }
                }

        class Stripe:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, _url: str, **_kwargs: object) -> Response:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("Failed to perform, curl: (56) connection reset")
                return Response()

        events: list[tuple[str, dict[str, object]]] = []
        stripe = Stripe()
        request = plus_engine.LongLinkRequest(accessToken="token")
        with patch.object(plus_engine, "cancellable_sleep"):
            redirect = plus_engine.stripe_payment_page_redirect_url(
                stripe,
                "cs_test",
                "pk_test",
                request,
                {"elements_session_id": "elements", "stripe_js_id": "stripe-js", "locale": "en"},
                timeout_seconds=30,
                emit=lambda step, _message, **extra: events.append((step, extra)),
            )

        self.assertEqual(redirect, "https://pm-redirects.stripe.com/authorize/test")
        self.assertEqual(stripe.calls, 2)
        self.assertIn(("redirect", {"stripe_poll_network_errors": 1}), events)

    def test_first_approve_attempt_reuses_checkout_session(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://left.test:8080",
            approve_proxy_override="http://left.test:8080",
            batch_checkout_proxy_country="US",
            billing_country="US",
            strict_approve_same_proxy_ip=True,
        )
        checkout_session = object()
        checkout = {
            "cs_id": "cs_test",
            "billing_country": "US",
            "processor_entity": "openai_llc",
            "checkout_proxy_ip": "203.0.113.10",
        }
        probe = plus_engine.ProxyProbeResult(
            ok=True,
            label="approve US",
            proxy="http://left.test:8080",
            ip="203.0.113.10",
            country="United States",
            country_code="US",
        )
        with (
            patch.object(plus_engine, "probe_proxy", return_value=probe),
            patch.object(plus_engine, "chatgpt_approve") as approve,
            patch.object(plus_engine, "build_chatgpt_session") as build_session,
        ):
            result = plus_engine.chatgpt_approve_with_retry(
                request,
                "cs_test",
                checkout,
                source_session=checkout_session,
            )

        self.assertIs(result, checkout_session)
        approve.assert_called_once_with(checkout_session, "cs_test", checkout)
        build_session.assert_not_called()

    def test_distinct_approve_proxy_copies_checkout_state_into_provider_session(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://checkout.test:8080",
            approve_proxy_override="http://provider.test:8080",
            batch_checkout_proxy_country="US",
            batch_target_country="BR",
            batch_approve_proxy_country="BR",
            billing_country="US",
            strict_approve_same_proxy_ip=True,
        )
        checkout_session = object()
        provider_session = object()
        checkout = {
            "cs_id": "cs_test",
            "billing_country": "US",
            "processor_entity": "openai_llc",
            "provider_proxy_ip": "198.51.100.20",
        }
        probe = plus_engine.ProxyProbeResult(
            ok=True,
            label="approve BR",
            proxy="http://provider.test:8080",
            ip="198.51.100.20",
            country="Brazil",
            country_code="BR",
        )
        with (
            patch.object(plus_engine, "probe_proxy", return_value=probe),
            patch.object(plus_engine, "chatgpt_approve") as approve,
            patch.object(plus_engine, "build_chatgpt_session", return_value=provider_session) as build_session,
        ):
            result = plus_engine.chatgpt_approve_with_retry(
                request,
                "cs_test",
                checkout,
                source_session=checkout_session,
            )

        self.assertIs(result, provider_session)
        approve.assert_called_once_with(provider_session, "cs_test", checkout)
        build_session.assert_called_once_with(
            request,
            proxy_override="http://provider.test:8080",
            source_session=checkout_session,
        )

    def test_rotated_provider_proxy_keeps_strict_approve_on_the_same_sticky(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            provider_proxy_override="http://provider-old.test:8080",
            approve_proxy_override="http://provider-old.test:8080",
            batch_target_country="BR",
            strict_approve_same_proxy_ip=True,
        )

        updated = plus_engine.request_with_stage_proxy(
            request,
            "provider",
            "http://provider-new.test:8080",
            "BR",
        )

        self.assertEqual(updated.provider_proxy_override, "http://provider-new.test:8080")
        self.assertEqual(updated.approve_proxy_override, "http://provider-new.test:8080")

    def test_ipv6_mode_keeps_checkout_and_approve_on_the_same_sticky(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://left-old.test:8080",
            approve_proxy_override="http://left-old.test:8080",
            batch_checkout_proxy_country="GB",
            batch_approve_proxy_country="GB",
            approve_same_as_checkout=True,
            strict_approve_same_proxy_ip=True,
            require_ipv6_egress=True,
        )

        updated = plus_engine.request_with_stage_proxy(
            request,
            "checkout",
            "http://left-new.test:8080",
            "GB",
        )

        self.assertEqual(updated.checkout_proxy_override, "http://left-new.test:8080")
        self.assertEqual(updated.approve_proxy_override, "http://left-new.test:8080")

    def test_checkout_owned_approve_is_not_overwritten_by_right_provider_rotation(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://left.test:8080",
            provider_proxy_override="http://right-old.test:8080",
            approve_proxy_override="http://left.test:8080",
            batch_target_country="BR",
            approve_same_as_checkout=True,
            strict_approve_same_proxy_ip=True,
            require_ipv6_egress=True,
        )

        updated = plus_engine.request_with_stage_proxy(
            request,
            "provider",
            "http://right-new.test:8080",
            "BR",
        )

        self.assertEqual(updated.provider_proxy_override, "http://right-new.test:8080")
        self.assertEqual(updated.approve_proxy_override, "http://left.test:8080")

    def test_target_chain_keeps_jp_approve_when_provider_rotates(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://jp.test:8080",
            provider_proxy_override="http://br-old.test:8080",
            approve_proxy_override="http://jp.test:8080",
            batch_checkout_proxy_country="JP",
            batch_target_country="BR",
            batch_approve_proxy_country="JP",
            approve_same_as_checkout=True,
        )

        updated = plus_engine.request_with_stage_proxy(
            request,
            "provider",
            "http://br-new.test:8080",
            "BR",
        )

        self.assertEqual(plus_engine.approve_stage_country(updated), "JP")
        self.assertEqual(updated.approve_proxy_override, "http://jp.test:8080")
        self.assertEqual(plus_engine.approve_stage_proxy(updated), "http://jp.test:8080")

    def test_approve_blocked_burst_uses_approve_proxy_and_copies_checkout_state(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://checkout.test:8080",
            approve_proxy_override="http://provider.test:8080",
            batch_checkout_proxy_country="US",
            batch_target_country="BR",
            strict_approve_same_proxy_ip=True,
        )
        checkout_session = object()
        approved_session = object()
        checkout = {
            "cs_id": "cs_test",
            "billing_country": "US",
            "processor_entity": "openai_llc",
        }
        with (
            patch.object(plus_engine, "APPROVE_BLOCKED_BURST_ATTEMPTS", 1),
            patch.object(plus_engine, "build_chatgpt_session", return_value=approved_session) as build_session,
            patch.object(plus_engine, "chatgpt_approve"),
        ):
            result = plus_engine.chatgpt_approve_blocked_burst(
                request,
                "cs_test",
                checkout,
                source_session=checkout_session,
            )

        self.assertIs(result, approved_session)
        build_session.assert_called_once_with(
            request,
            proxy_override="http://provider.test:8080",
            source_session=checkout_session,
        )

    def test_approve_blocked_burst_splits_global_slots_across_batch_concurrency(self) -> None:
        checkout = {
            "cs_id": "cs_test",
            "billing_country": "US",
            "processor_entity": "openai_llc",
        }
        emitted: list[str] = []
        request = plus_engine.LongLinkRequest(accessToken="token", batch_concurrency=3)

        with (
            patch.object(plus_engine, "build_chatgpt_session", return_value=object()),
            patch.object(
                plus_engine,
                "chatgpt_approve",
                side_effect=plus_engine.ChatgptApproveBlocked("blocked"),
            ) as approve,
        ):
            with self.assertRaises(HTTPException):
                plus_engine.chatgpt_approve_blocked_burst(
                    request,
                    "cs_test",
                    checkout,
                    emit=lambda _step, message, **_extra: emitted.append(message),
                )

        self.assertEqual(approve.call_count, 50)
        self.assertTrue(any("当前任务累计 50 次" in message for message in emitted))
        self.assertTrue(any("并行 16" in message for message in emitted))
        self.assertTrue(any("全局 50 槽" in message for message in emitted))

    def test_approve_blocked_uses_playwright_before_burst(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            checkout_proxy_override="http://checkout.test:8080",
            approve_proxy_override="http://checkout.test:8080",
            batch_checkout_proxy_country="US",
            batch_approve_proxy_country="US",
            approve_same_as_checkout=True,
            enable_playwright_approve_fallback=True,
            enable_approve_blocked_burst=True,
        )
        checkout_session = object()
        checkout = {
            "cs_id": "cs_test",
            "billing_country": "US",
            "processor_entity": "openai_llc",
            "checkout_proxy_ip": "203.0.113.10",
        }
        probe = plus_engine.ProxyProbeResult(
            ok=True,
            label="approve US",
            proxy="http://checkout.test:8080",
            ip="203.0.113.10",
            country="United States",
            country_code="US",
        )
        with (
            patch.object(plus_engine, "request_with_rotated_approve_session", return_value=request),
            patch.object(plus_engine, "probe_proxy", return_value=probe),
            patch.object(plus_engine, "chatgpt_approve", side_effect=plus_engine.ChatgptApproveBlocked("blocked")),
            patch.object(
                plus_engine,
                "chatgpt_approve_via_playwright",
                return_value="payments_checkout_playwright",
            ) as browser_approve,
            patch.object(plus_engine, "chatgpt_approve_blocked_burst") as burst,
        ):
            result = plus_engine.chatgpt_approve_with_retry(
                request,
                "cs_test",
                checkout,
                source_session=checkout_session,
            )

        self.assertIs(result, checkout_session)
        browser_approve.assert_called_once_with(request, checkout_session, "cs_test", checkout)
        burst.assert_not_called()

    def test_playwright_proxy_config_supports_authenticated_proxy(self) -> None:
        session = MagicMock()
        session.proxies = {
            "https": "http://proxy-user:proxy-pass@127.0.0.1:18080",
        }

        config = plus_engine._playwright_proxy_config(session)

        self.assertEqual(
            config,
            {
                "server": "http://127.0.0.1:18080",
                "username": "proxy-user",
                "password": "proxy-pass",
            },
        )

    def test_hosted_chatgpt_approve_uses_payments_endpoint_without_generated_sentinel(self) -> None:
        class Response:
            def __init__(self, status_code: int, payload: dict[str, object] | None = None) -> None:
                self.status_code = status_code
                self._payload = payload or {}
                self.text = json.dumps(self._payload)
                self.headers = {"content-type": "application/json"}

            def json(self) -> dict[str, object]:
                return self._payload

        class Session:
            def __init__(self) -> None:
                self.urls: list[str] = []
                self.headers = {"oai-device-id": "device-test", "User-Agent": "test-agent"}
                self.cookies = MagicMock()
                self.request_headers: list[dict[str, str]] = []

            def post(self, url: str, **kwargs: object) -> Response:
                self.urls.append(url)
                self.request_headers.append(dict(kwargs.get("headers") or {}))
                if url.endswith("/sentinel/ping"):
                    return Response(200)
                return Response(200, {"result": "approved"})

        checkout = {"billing_country": "US", "processor_entity": "openai_llc"}
        with patch.object(
            plus_engine,
            "build_sentinel_headers_bundle",
            return_value={"openai-sentinel-token": "sentinel-test", "oai-sc": "cookie-test"},
        ) as build_sentinel:
            session = Session()
            endpoint = plus_engine.chatgpt_approve(session, "cs_test", checkout)

        self.assertEqual(endpoint, "payments_checkout")
        self.assertTrue(session.urls[-1].endswith("/payments/checkout/approve"))
        self.assertFalse(any(f"/checkout/cs_test/approve" in url for url in session.urls))
        self.assertNotIn("openai-sentinel-token", session.request_headers[-1])
        build_sentinel.assert_not_called()

    def test_chatgpt_checkout_confirm_uses_paypal_and_sentinel_headers(self) -> None:
        class Response:
            status_code = 200
            text = ""
            headers = {"content-type": "application/json"}

            @staticmethod
            def json() -> dict[str, object]:
                return {
                    "status": "requires_confirmation",
                    "type": "setup_intent",
                    "client_secret": "seti_test_secret_value",
                    "confirm_return_url": "https://chatgpt.com/checkout/verify",
                }

        class Session:
            def __init__(self) -> None:
                self.headers = {"oai-device-id": "device-test", "User-Agent": "test-agent"}
                self.cookies = MagicMock()
                self.calls: list[tuple[str, dict[str, object]]] = []

            def post(self, url: str, **kwargs: object) -> Response:
                self.calls.append((url, kwargs))
                return Response()

        events: list[dict[str, object]] = []
        session = Session()
        checkout = {"billing_country": "US", "processor_entity": "openai_llc"}
        with patch.object(
            plus_engine,
            "build_sentinel_headers_bundle",
            return_value={"openai-sentinel-token": "sentinel-test", "oai-sc": "cookie-test"},
        ):
            result = plus_engine.chatgpt_checkout_confirm(
                session,
                "cs_test",
                checkout,
                emit=lambda _step, _message, **extra: events.append(extra),
            )

        self.assertEqual(result["type"], "setup_intent")
        self.assertTrue(session.calls[0][0].endswith("/payments/checkout/confirm"))
        self.assertEqual(session.calls[0][1]["json"]["selected_payment_method_type"], "paypal")
        self.assertEqual(session.calls[0][1]["headers"]["openai-sentinel-token"], "sentinel-test")
        self.assertTrue(events[0]["chatgpt_confirm_profile"]["client_secret_present"])
        self.assertNotIn("seti_test_secret_value", json.dumps(events[0], sort_keys=True))

    def test_stripe_confirm_intent_confirms_setup_intent_with_existing_payment_method(self) -> None:
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def json() -> dict[str, object]:
                return {
                    "status": "requires_action",
                    "next_action": {
                        "type": "redirect_to_url",
                        "redirect_to_url": {"url": "https://pm-redirects.stripe.com/authorize/test"},
                    },
                }

        class Session:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def post(self, url: str, **kwargs: object) -> Response:
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        result = plus_engine.stripe_confirm_intent(
            session,
            "pm_test",
            "pk_live_test",
            {
                "type": "setup_intent",
                "client_secret": "seti_test_secret_value",
                "confirm_return_url": "https://chatgpt.com/checkout/verify",
            },
        )

        self.assertEqual(result["status"], "requires_action")
        self.assertTrue(session.calls[0][0].endswith("/v1/setup_intents/seti_test/confirm"))
        self.assertEqual(session.calls[0][1]["data"]["payment_method"], "pm_test")
        self.assertEqual(session.calls[0][1]["data"]["client_secret"], "seti_test_secret_value")

    def test_checkout_retry_exhaustion_rotates_the_outer_sticky_combo(self) -> None:
        error = HTTPException(status_code=504, detail="checkout 10 retries exhausted")
        self.assertTrue(plus_engine.is_retryable_provider_flow_exception(error))

    def test_nonzero_or_unknown_amount_rotates_the_outer_sticky_combo(self) -> None:
        nonzero = HTTPException(
            status_code=402,
            detail="stripe_amount_nonzero: amount=1933, currency=eur, source=elements_options.amount",
        )
        unknown = HTTPException(
            status_code=402,
            detail="stripe_amount_unknown: amount=unknown, currency=eur",
        )

        self.assertTrue(plus_engine.is_retryable_provider_flow_exception(nonzero))
        self.assertTrue(plus_engine.is_retryable_provider_flow_exception(unknown))

    def test_proxy_country_mismatch_rotates_the_outer_sticky_combo(self) -> None:
        error = HTTPException(
            status_code=502,
            detail="promotion/update/poll JP 出口不是 JP: IN 103.38.183.212",
        )
        self.assertTrue(plus_engine.is_retryable_provider_flow_exception(error))

    def test_approve_blocked_exhaustion_rotates_the_outer_sticky_combo(self) -> None:
        retryable_result = HTTPException(
            status_code=502,
            detail="chatgpt approve retryable result: blocked/exception",
        )
        burst_exhausted = HTTPException(
            status_code=502,
            detail="chatgpt approve blocked/exception burst failed: attempts=3, timeout=15s",
        )

        self.assertTrue(plus_engine.is_retryable_provider_flow_exception(retryable_result))
        self.assertTrue(plus_engine.is_retryable_provider_flow_exception(burst_exhausted))

    def test_requires_approval_uses_ninety_second_post_approve_poll(self) -> None:
        request = plus_engine.LongLinkRequest(accessToken="token")
        chatgpt = object()
        stripe = object()
        poll_stripe = object()
        checkout = {"cs_id": "cs_test", "billing_country": "US"}

        with (
            patch.object(plus_engine, "chatgpt_approve_with_retry", return_value=chatgpt) as approve,
            patch.object(
                plus_engine,
                "stripe_payment_page_redirect_url",
                return_value="https://www.paypal.com/agreements/approve?ba_token=BA-test",
            ) as poll,
        ):
            redirect = plus_engine.redirect_url_after_confirm(
                chatgpt,
                stripe,
                {"submission_attempt": {"state": "requires_approval"}},
                "cs_test",
                "pk_test",
                {"elements_session_id": "elements"},
                checkout,
                request,
                poll_stripe=poll_stripe,
            )

        self.assertIn("ba_token=BA-test", redirect)
        approve.assert_called_once()
        self.assertIs(poll.call_args.args[0], poll_stripe)
        self.assertEqual(poll.call_args.kwargs["timeout_seconds"], 90)

    def test_right_session_exit_verifies_same_ip_country_and_ip_version(self) -> None:
        session = MagicMock()
        session.proxies = {"https": "http://right.test:8080"}
        with patch.object(
            plus_engine,
            "fetch_proxy_geo",
            return_value=("2001:db8::20", "BR", "Brazil"),
        ):
            matched = plus_engine.verify_right_session_exit(
                session,
                expected_ip="2001:0db8:0:0::20",
                expected_country="BR",
                label="right session",
                required_ip_version=6,
            )
            mismatched = plus_engine.verify_right_session_exit(
                session,
                expected_ip="2001:db8::21",
                expected_country="BR",
                label="right session",
                required_ip_version=6,
            )

        self.assertTrue(matched.ok)
        self.assertEqual(matched.ip, "2001:db8::20")
        self.assertFalse(mismatched.ok)
        self.assertIn("right_session_ip_mismatch", mismatched.error)

    def test_provider_redirect_uses_playwright_fallback(self) -> None:
        request = plus_engine.LongLinkRequest(
            accessToken="token",
            paypal_confirm_mode="direct",
            enable_playwright_redirect_fallback=True,
        )
        self.assertEqual(request.stripe_poll_egress, "provider")
        stripe = MagicMock()
        intermediate = "https://pm-redirects.stripe.com/authorize/acct_test/sa_test"
        ba_url = "https://www.paypal.com/agreements/approve?ba_token=BA-test"
        events: list[dict[str, object]] = []

        with (
            patch.object(plus_engine, "stripe_context", return_value={"checkout_amount": "0", "currency": "usd"}),
            patch.object(plus_engine, "stripe_amount_info", return_value=("0", "test")),
            patch.object(plus_engine, "stripe_confirm", return_value={}),
            patch.object(plus_engine, "redirect_url_after_confirm", return_value=intermediate),
            patch.object(plus_engine, "resolve_external_redirect", return_value=intermediate),
            patch.object(plus_engine, "resolve_external_redirect_playwright", return_value=ba_url) as browser,
        ):
            result = plus_engine.create_provider_link(
                object(),
                {"cs_id": "cs_test", "billing_country": "US"},
                {"test_amount": "0"},
                "https://checkout.stripe.com/c/pay/cs_test",
                request,
                billing={"name": "Test User", "email": "test@example.com", "country": "US", "city": "New York"},
                stripe_session=stripe,
                emit=lambda step, message, **extra: events.append({"step": step, "message": message, **extra}),
            )

        self.assertEqual(result["long_url"], ba_url)
        self.assertTrue(result["playwright_fallback_used"])
        browser.assert_called_once_with(stripe, intermediate, timeout_seconds=60.0)
        self.assertTrue(any(event.get("playwright_fallback_started") for event in events))

    def test_intermediate_url_is_returned_only_when_switch_is_enabled(self) -> None:
        intermediate = "https://pm-redirects.stripe.com/authorize/acct_test/sa_test"
        base_request = plus_engine.LongLinkRequest(
            accessToken="token",
            stripe_poll_egress="provider",
            paypal_confirm_mode="direct",
            enable_playwright_redirect_fallback=False,
        )
        common_patches = (
            patch.object(plus_engine, "stripe_context", return_value={"checkout_amount": "0", "currency": "usd"}),
            patch.object(plus_engine, "stripe_amount_info", return_value=("0", "test")),
            patch.object(plus_engine, "stripe_confirm", return_value={}),
            patch.object(plus_engine, "redirect_url_after_confirm", return_value=intermediate),
            patch.object(plus_engine, "resolve_external_redirect", return_value=intermediate),
        )

        with common_patches[0], common_patches[1], common_patches[2], common_patches[3], common_patches[4]:
            with self.assertRaises(HTTPException):
                plus_engine.create_provider_link(
                    object(),
                    {"cs_id": "cs_test", "billing_country": "US"},
                    {"test_amount": "0"},
                    "https://checkout.stripe.com/c/pay/cs_test",
                    base_request,
                    billing={"name": "Test User", "email": "test@example.com", "country": "US", "city": "New York"},
                    stripe_session=MagicMock(),
                )

        enabled_request = base_request.model_copy(update={"allow_intermediate_url_fallback": True})
        with (
            patch.object(plus_engine, "stripe_context", return_value={"checkout_amount": "0", "currency": "usd"}),
            patch.object(plus_engine, "stripe_amount_info", return_value=("0", "test")),
            patch.object(plus_engine, "stripe_confirm", return_value={}),
            patch.object(plus_engine, "redirect_url_after_confirm", return_value=intermediate),
            patch.object(plus_engine, "resolve_external_redirect", return_value=intermediate),
        ):
            result = plus_engine.create_provider_link(
                object(),
                {"cs_id": "cs_test", "billing_country": "US"},
                {"test_amount": "0"},
                "https://checkout.stripe.com/c/pay/cs_test",
                enabled_request,
                billing={"name": "Test User", "email": "test@example.com", "country": "US", "city": "New York"},
                stripe_session=MagicMock(),
            )

        self.assertEqual(result["long_url"], "")
        self.assertEqual(result["intermediate_url"], intermediate)
        self.assertTrue(result["intermediate_fallback_used"])

    def test_us_billing_profile_uses_provider_compatible_contact_fields(self) -> None:
        billing = plus_engine.billing_for_country("US")
        self.assertTrue(billing["email"].endswith("@example.com"))
        self.assertRegex(billing["phone"], r"^\+1\d{10}$")

    def test_all_left_country_billing_profiles_are_complete(self) -> None:
        expected = {
            "US", "IN", "EG", "ID", "TH", "MX", "CO", "ZA", "DE", "FR", "IT", "ES", "NL",
            "DK", "CA", "AU", "NZ", "KR", "AE", "IL", "RO", "GB", "BR", "JP", "PH", "TR", "VN",
        }
        self.assertEqual(plus_engine.OPENAI_SUPPORTED_COUNTRY_CODES, expected)
        for country in sorted(expected):
            with self.subTest(country=country):
                billing = plus_engine.billing_for_country(country)
                self.assertEqual(billing["country"], country)
                self.assertTrue(billing["line1"])
                self.assertTrue(billing["city"])
                self.assertTrue(billing["state"])
                self.assertTrue(billing["postal_code"])
                self.assertTrue(billing["phone"].startswith(plus_engine.COUNTRY_PHONE_PREFIX[country]))
                self.assertIn(country, plus_engine.COUNTRY_CURRENCY)

        philippines = plus_engine.billing_for_country("PH")
        self.assertEqual(plus_engine.COUNTRY_CURRENCY["PH"], "PHP")
        self.assertRegex(philippines["postal_code"], r"^\d{4}$")
        self.assertTrue(philippines["phone"].startswith("+63"))
        self.assertEqual(plus_engine.billing_profile_source("PH"), "precise")

    def test_smsbower_paypal_pm_billing_profiles_are_complete(self) -> None:
        self.assertEqual(len(plus_engine.PM_BILLING_COUNTRY_CODES), 194)
        self.assertTrue({"AF", "AR", "HK", "MA", "TW", "XK"}.issubset(plus_engine.PM_BILLING_COUNTRY_CODES))
        for country in sorted(plus_engine.PM_BILLING_COUNTRY_CODES):
            with self.subTest(country=country):
                billing = plus_engine.billing_for_country(country)
                self.assertEqual(billing["country"], country)
                self.assertTrue(billing["line1"])
                self.assertTrue(billing["city"])
                self.assertTrue(billing["postal_code"])
                self.assertTrue(billing["phone"].startswith(plus_engine.COUNTRY_PHONE_PREFIX[country]))
                self.assertIn(country, plus_engine.COUNTRY_CURRENCY)

        argentina = plus_engine.billing_for_country("AR")
        self.assertTrue(argentina["phone"].startswith("+54"))
        self.assertEqual(plus_engine.currency_for_country("AR"), "ARS")

    def test_pm_billing_identity_is_aligned_with_access_token_profile(self) -> None:
        token = self._jwt(
            {
                "https://api.openai.com/profile": {
                    "email": "account.owner@example.net",
                    "name": "Account Owner",
                }
            }
        )
        request = plus_engine.LongLinkRequest(accessToken=token, payment_method_country="US")

        billing = plus_engine.billing_for_request(request, "US")

        self.assertEqual(billing["email"], "account.owner@example.net")
        self.assertEqual(billing["name"], "Account Owner")
        self.assertEqual(billing["country"], "US")

    def test_pm_billing_name_falls_back_to_access_token_email(self) -> None:
        token = self._jwt({"https://api.openai.com/profile": {"email": "jane.doe+tag@example.net"}})
        request = plus_engine.LongLinkRequest(accessToken=token)

        billing = plus_engine.billing_for_request(request, "US")

        self.assertEqual(billing["email"], "jane.doe+tag@example.net")
        self.assertEqual(billing["name"], "Jane Doe")

    def test_local_run_key_is_available_from_loopback(self) -> None:
        response = plus_engine.local_run_key(_request_from("127.0.0.1"))
        self.assertTrue(response["ok"])
        self.assertEqual(response["run_key"], plus_engine.LOCAL_RUN_KEY)

    def test_local_run_key_is_rejected_for_remote_clients(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            plus_engine.local_run_key(_request_from("192.168.1.20"))
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
