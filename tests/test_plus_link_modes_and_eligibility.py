from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from services import account_inventory_service, plus_link_store
from services.paypal_country_catalog import SMSBOWER_PAYPAL_COUNTRY_CODES, SMSBOWER_PAYPAL_COUNTRY_META


def _jwt(payload: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.signature"


class PlusLinkModeTests(unittest.TestCase):
    def test_proxy_quality_gate_defaults_to_enabled(self) -> None:
        config = plus_link_store._normalize_config({})

        self.assertTrue(config["proxy_quality_gate_enabled"])
        self.assertEqual(config["proxy_quality_candidate_limit"], 3)
        self.assertEqual(config["proxy_quality_probe_rounds"], 2)

    def test_kookeey_global_template_builds_independent_checkout_and_provider_sessions(self) -> None:
        kookeey = "gate.kookeey.info:1000:test-user:test-password-global-12345678-5m"
        config = plus_link_store._normalize_config({
            "residential_proxy_pool": kookeey,
            "residential_country_proxy_pool": {
                "DE": kookeey,
                "BR": kookeey,
                "TR": kookeey,
            },
            "residential_checkout_proxy": kookeey,
            "residential_provider_proxy": kookeey,
            "residential_approve_proxy": kookeey,
            "residential_promotion_proxy": kookeey,
            "checkout_country": "DE",
            "country_rotation_enabled": True,
            "billing_country_rotation": ["AF"],
            "proxy_country_rotation": ["TR"],
        })
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-kookeey-independent-")) / "plus_link.json"
        )

        with patch.object(service, "_assign_project_parent_proxy", return_value=None):
            payload, _proxy_info = service._payload_for_account(
                {"email": "free@example.com", "access_token": "token"},
                config,
                1,
                proxy_index=1,
            )

        checkout_proxy = payload["checkout_proxy_override"]
        provider_proxy = payload["provider_proxy_override"]
        self.assertIn("-DE-", checkout_proxy)
        self.assertIn("-AF-", provider_proxy)
        self.assertNotEqual(checkout_proxy, provider_proxy)

    def test_legacy_provider_stage_preserves_non_promotion_country(self) -> None:
        kookeey = "gate.kookeey.info:1000:test-user:test-password-global-12345678-5m"
        config = plus_link_store._normalize_config({
            "provider_country": "AF",
            "residential_provider_proxy": kookeey,
        })

        self.assertIn("AF", config["residential_country_proxy_pool"])

    def test_proxy_quality_gate_checks_three_stable_target_reachable_exits(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-quality-")) / "plus_link.json"
        )
        payload = {
            "checkout_proxy_override": "http://checkout.test:8080",
            "provider_proxy_override": "http://provider.test:8080",
            "promotion_proxy_override": "http://promotion.test:8080",
            "batch_checkout_proxy_country": "DE",
            "batch_target_country": "TH",
            "promotion_proxy_country": "TR",
        }
        config = plus_link_store._normalize_config({
            "proxy_quality_probe_rounds": 2,
            "proxy_quality_max_latency_ms": 12000,
        })
        ips = {
            "Checkout/Approve": ("198.51.100.10", "DE"),
            "Provider/PM/Poll": ("198.51.100.20", "TH"),
            "Promotion/Taxes": ("198.51.100.30", "TR"),
        }

        def probe(label, *_args, **_kwargs):
            ip, country = ips[label]
            return SimpleNamespace(ok=True, ip=ip, country_code=country, error="")

        fake_engine = SimpleNamespace(
            probe_proxy=probe,
            probe_proxy_target=lambda label, *_args, **_kwargs: {
                "ok": True,
                "label": label,
                "status_code": 401,
                "latency_ms": 45,
                "error": "",
            },
        )
        with patch.object(plus_link_store, "_load_plus_engine", return_value=fake_engine):
            result = service._check_payload_proxy_quality(
                payload,
                config,
                task_id="quality-task",
                email="free@example.com",
            )

        self.assertTrue(result["ok"])
        self.assertEqual([item["stage"] for item in result["stages"]], ["checkout", "provider", "promotion"])
        self.assertTrue(all(item["sticky"] for item in result["stages"]))
        self.assertEqual([item["ip"] for item in result["stages"]], list(ip for ip, _ in ips.values()))

    def test_proxy_quality_gate_allows_same_country_provider_and_promotion_ip(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-quality-shared-")) / "plus_link.json"
        )
        payload = {
            "checkout_proxy_override": "http://checkout.test:8080",
            "provider_proxy_override": "http://thailand.test:8080",
            "promotion_proxy_override": "http://thailand.test:8080",
            "batch_checkout_proxy_country": "GB",
            "batch_target_country": "TH",
            "payment_method_country": "TH",
            "promotion_proxy_country": "TH",
            "promotion_country": "TH",
        }
        config = plus_link_store._normalize_config({"proxy_quality_probe_rounds": 1})
        ips = {
            "Checkout/Approve": ("198.51.100.10", "GB"),
            "Provider/PM/Poll": ("198.51.100.20", "TH"),
            "Promotion/Taxes": ("198.51.100.20", "TH"),
        }

        def probe(label, *_args, **_kwargs):
            ip, country = ips[label]
            return SimpleNamespace(ok=True, ip=ip, country_code=country, error="")

        fake_engine = SimpleNamespace(
            probe_proxy=probe,
            probe_proxy_target=lambda label, *_args, **_kwargs: {
                "ok": True,
                "label": label,
                "status_code": 204,
                "latency_ms": 20,
                "error": "",
            },
        )
        with patch.object(plus_link_store, "_load_plus_engine", return_value=fake_engine):
            result = service._check_payload_proxy_quality(
                payload,
                config,
                task_id="quality-task",
                email="free@example.com",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["stages"][1]["ip"], result["stages"][2]["ip"])

    def test_proxy_quality_gate_rejects_sticky_ip_drift_before_target_probe(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-quality-drift-")) / "plus_link.json"
        )
        payload = {
            "checkout_proxy_override": "http://checkout.test:8080",
            "provider_proxy_override": "http://provider.test:8080",
            "promotion_proxy_override": "http://promotion.test:8080",
            "batch_checkout_proxy_country": "DE",
            "batch_target_country": "TH",
            "promotion_proxy_country": "TR",
        }
        config = plus_link_store._normalize_config({"proxy_quality_probe_rounds": 2})
        checkout_ips = iter(["198.51.100.10", "198.51.100.11"])

        def probe(label, *_args, **_kwargs):
            if label == "Checkout/Approve":
                return SimpleNamespace(ok=True, ip=next(checkout_ips), country_code="DE", error="")
            raise AssertionError("later stages must not run after checkout drift")

        target_probe = Mock(return_value={"ok": True, "status_code": 401, "latency_ms": 10})
        fake_engine = SimpleNamespace(probe_proxy=probe, probe_proxy_target=target_probe)
        with patch.object(plus_link_store, "_load_plus_engine", return_value=fake_engine):
            result = service._check_payload_proxy_quality(
                payload,
                config,
                task_id="quality-task",
                email="free@example.com",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_stage"], "checkout")
        self.assertIn("sticky IP 漂移", result["error"])
        target_probe.assert_not_called()

    def test_checkout_failures_have_actionable_categories(self) -> None:
        self.assertEqual(
            plus_link_store._classify_plus_failure("checkout 10 次重试失败：403 拦截"),
            "checkout_blocked",
        )
        self.assertEqual(
            plus_link_store._classify_plus_failure("checkout 10 次重试失败：TLS 连接失败"),
            "checkout_network",
        )
        self.assertEqual(
            plus_link_store._classify_plus_failure("checkout promotion update failed: status=403"),
            "promotion_update",
        )

    def test_left_and_right_country_universes_match_the_unified_model(self) -> None:
        source_countries = {
            "US", "IN", "EG", "ID", "TH", "MX", "CO", "ZA", "DE", "FR", "IT", "ES", "NL",
            "DK", "CA", "AU", "NZ", "KR", "AE", "IL", "RO", "PH",
        }
        self.assertTrue(source_countries.issubset(plus_link_store.BILLING_COUNTRY_CODES))
        self.assertEqual(plus_link_store.PROMOTION_COUNTRY_CODES, {"BR", "JP", "TH", "TR", "VN"})
        self.assertEqual(plus_link_store.PROVIDER_COUNTRY_CODES, plus_link_store.BILLING_COUNTRY_CODES)

    def test_smsbower_paypal_countries_are_provider_pm_and_ip_candidates(self) -> None:
        self.assertEqual(len(SMSBOWER_PAYPAL_COUNTRY_CODES), 194)
        self.assertEqual(plus_link_store.BILLING_COUNTRY_CODES, set(SMSBOWER_PAYPAL_COUNTRY_CODES))
        self.assertTrue({"AF", "AR", "HK", "MA", "TW", "XK"}.issubset(plus_link_store.PROVIDER_COUNTRY_CODES))
        self.assertTrue(plus_link_store.PROVIDER_COUNTRY_CODES.issubset(plus_link_store.RESIDENTIAL_PROXY_COUNTRY_CODES))
        self.assertNotIn("AF", plus_link_store.CHECKOUT_COUNTRY_CODES)

    def test_smsbower_pm_price_limit_is_persisted_and_catalog_is_filterable(self) -> None:
        config = plus_link_store._normalize_config({"smsbower_pm_max_price": 0.2})
        self.assertEqual(config["smsbower_pm_max_price"], 0.2)
        self.assertEqual(plus_link_store._normalize_config({"smsbower_pm_max_price": 99})["smsbower_pm_max_price"], 10.0)

        matched = {
            code
            for code, item in SMSBOWER_PAYPAL_COUNTRY_META.items()
            if float(item["lowest_price"]) <= config["smsbower_pm_max_price"]
            and int(item["available_count"]) > 0
        }
        self.assertTrue(matched)
        self.assertLess(len(matched), len(SMSBOWER_PAYPAL_COUNTRY_CODES))
        self.assertIn("BR", matched)
        self.assertNotIn("AI", matched)

    def test_philippines_is_supported_only_for_provider_and_pm_billing(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "checkout_country": "PH",
                "target_country": "PH",
                "approve_country": "PH",
                "provider_country": "PH",
                "payment_method_country": "PH",
            }
        )

        self.assertEqual(config["provider_country"], "PH")
        self.assertEqual(config["payment_method_country"], "PH")
        self.assertEqual(config["checkout_country"], "US")
        self.assertEqual(config["target_country"], "US")
        self.assertEqual(config["approve_country"], "US")
        self.assertIn("PH", plus_link_store.RESIDENTIAL_PROXY_COUNTRY_CODES)
        self.assertNotIn("PH", plus_link_store.PROMOTION_COUNTRY_CODES)

        matrix = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": True,
                "checkout_country": "GB",
                "billing_country_rotation": ["PH"],
                "proxy_country_rotation": ["TR"],
            }
        )
        self.assertEqual(matrix["billing_country_rotation"], ["PH"])
        self.assertEqual(matrix["provider_country"], "PH")
        self.assertEqual(matrix["payment_method_country"], "PH")

    def test_thailand_is_supported_for_provider_and_promotion_stages(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "provider_country": "TH",
                "promotion_country": "TH",
            }
        )

        self.assertEqual(config["provider_country"], "TH")
        self.assertEqual(config["promotion_country"], "TH")

    def test_provider_supports_global_pm_billing_country_independently_from_promotion(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "provider_country": "DE",
                "payment_method_country": "DE",
                "promotion_country": "TH",
            }
        )

        self.assertEqual(config["provider_country"], "DE")
        self.assertEqual(config["payment_method_country"], "DE")
        self.assertEqual(config["promotion_country"], "TH")

    def test_refresh_failure_falls_back_to_existing_access_token(self) -> None:
        account = {"refresh_token": "refresh-token"}
        with patch.object(
            plus_link_store,
            "_refresh_platform_tokens",
            side_effect=RuntimeError("refresh_token_reused"),
        ):
            access_token, refreshed, error = plus_link_store._refresh_access_token_with_fallback(
                account,
                "existing-access-token",
            )

        self.assertEqual(access_token, "existing-access-token")
        self.assertEqual(refreshed, {})
        self.assertIn("refresh_token_reused", error)

    def test_refresh_failure_without_access_token_is_fatal(self) -> None:
        account = {"refresh_token": "refresh-token"}
        with (
            patch.object(
                plus_link_store,
                "_refresh_platform_tokens",
                side_effect=RuntimeError("refresh_token_reused"),
            ),
            self.assertRaisesRegex(RuntimeError, "refresh_token_reused"),
        ):
            plus_link_store._refresh_access_token_with_fallback(account, "")

    def test_plus_failure_categories_are_stable(self) -> None:
        cases = {
            "paypal_unavailable_for_left_country: paypal_availability=unsupported": "paypal_unsupported",
            "checkout promotion update failed via /backend-api/payments/checkout/update": "promotion_update",
            "checkout taxes update failed via /backend-api/payments/checkout/taxes": "taxes_update",
            "stripe init failed: resource_missing": "stripe_init",
            "stripe payment_methods failed: bad request": "stripe_payment_method",
            "stripe confirm failed: checkout_confirm_error": "stripe_confirm",
            "stripe submission failed: submission_reason=generic_decline": "generic_decline",
            "stripe submission failed: submission_attempt state=failed": "stripe_submission",
            "approve blocked/exception 复投失败": "approve_blocked",
            "provider BR 出口不是 IPv6: BR 203.0.113.10": "proxy_ip_family_mismatch",
            "right_proxy_ip_mismatch: provider=2001:db8::1, promotion=2001:db8::2": "proxy_ip_mismatch",
            "promotion/update/poll JP 出口不是 JP: IN 103.38.183.212": "proxy_country_mismatch",
            "redirect url resolution timeout": "paypal_redirect",
            "TLS connect error": "proxy_network",
            "Failed to perform, curl: (...)": "proxy_network",
            "curl: (56) Recv failure: Connection was reset": "proxy_network",
            "checkout 1 次重试失败：代理 CONNECT 隧道失败": "checkout_network",
            "账号资格检测无法访问 ChatGPT: HTTP 503 Service Unavailable": "eligibility_upstream",
            "access_token 上游验证失败: HTTP 401": "eligibility",
        }
        for detail, expected in cases.items():
            with self.subTest(detail=detail):
                self.assertEqual(plus_link_store._classify_plus_failure(detail), expected)

    def test_retryable_failure_audit_distinguishes_transient_and_terminal_errors(self) -> None:
        for category in plus_link_store.AUTO_RETRY_FAILURE_CATEGORIES:
            with self.subTest(category=category):
                self.assertTrue(plus_link_store._is_auto_retryable_failure(category))

        transient_unknown = (
            "HTTP 429 rate limited",
            "HTTP 503 Service Unavailable",
            "request failed: status=502",
            "request failed: status_code: 504",
            "upstream returned Bad Gateway",
            "Cloudflare cf-ray blocked",
            "proxy CONNECT tunnel failed",
            "server disconnected without sending a response",
            "curl: (28) Operation timed out",
            "Empty reply from server",
        )
        for detail in transient_unknown:
            with self.subTest(detail=detail):
                self.assertTrue(plus_link_store._is_auto_retryable_failure("unknown", detail))

        terminal = (
            ("eligibility", "access_token 上游验证失败: HTTP 401"),
            ("paypal_unsupported", "当前国家明确没有 PayPal"),
            ("unknown", "配置字段格式无效"),
        )
        for category, detail in terminal:
            with self.subTest(category=category):
                self.assertFalse(plus_link_store._is_auto_retryable_failure(category, detail))

    def test_submission_profile_preserves_generic_decline_category(self) -> None:
        result = {
            "failure_category": "stripe_submission",
            "stripe_submission_profile": {
                "submission_state": "failed",
                "submission_reason": "generic_decline",
            },
        }

        self.assertEqual(
            plus_link_store._result_failure_category(result, "stripe_submission"),
            "generic_decline",
        )

    def test_default_fixed_pair_matches_verified_us_br_chain(self) -> None:
        config = plus_link_store._normalize_config({"country_rotation_enabled": False})
        self.assertEqual(config["target_country"], "US")
        self.assertEqual(config["checkout_country"], "US")
        self.assertEqual(config["provider_country"], "BR")
        self.assertEqual(config["approve_country"], "US")
        self.assertEqual(config["promotion_country"], "BR")
        self.assertEqual(config["paypal_confirm_mode"], "payment_method")
        self.assertEqual(config["checkout_ui_mode"], "custom")
        self.assertEqual(config["stripe_poll_egress"], "provider")

    def test_residential_proxy_can_connect_directly_without_project_parent(self) -> None:
        config = plus_link_store._normalize_config({
            "country_rotation_enabled": False,
            "residential_via_project_proxy": False,
            "residential_parent_node_filter": "ex-girlfriend-cache",
        })
        service = plus_link_store.PlusLinkService(Path(tempfile.mkdtemp()) / "plus_link.json")
        service._assign_residential_proxy = lambda *_args, **_kwargs: {
            "proxy": "http://residential.invalid:8080",
            "checkout_proxy": "http://residential.invalid:8080",
            "provider_proxy": "http://residential.invalid:8080",
            "approve_proxy": "http://residential.invalid:8080",
            "promotion_proxy": "http://promotion.invalid:8080",
        }

        payload, proxy_info = service._payload_for_account(
            {"email": "free@example.com", "access_token": "token"},
            config,
            1,
        )

        self.assertFalse(config["residential_via_project_proxy"])
        self.assertFalse(config["residential_via_clash"])
        self.assertIsNone(service._assign_project_parent_proxy(config, 1))
        self.assertFalse(payload["residential_via_clash"])
        self.assertEqual(payload["residential_clash_proxy"], "")
        self.assertNotIn("parent_proxy", proxy_info)

    def test_brazil_is_supported_as_a_left_pool_country(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "target_country": "BR",
                "checkout_country": "BR",
                "payment_method_country": "BR",
                "promotion_country": "TR",
            }
        )

        self.assertEqual(config["target_country"], "BR")
        self.assertEqual(config["checkout_country"], "BR")
        self.assertEqual(config["provider_country"], "BR")
        self.assertEqual(config["approve_country"], "BR")
        self.assertEqual(config["payment_method_country"], "BR")
        self.assertEqual(config["promotion_country"], "TR")

    def test_fixed_mode_preserves_independent_stage_countries(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "checkout_country": "GB",
                "provider_country": "JP",
                "approve_country": "CA",
                "target_country": "DE",
                "payment_method_country": "JP",
                "promotion_country": "BR",
            }
        )

        self.assertEqual(config["checkout_country"], "GB")
        self.assertEqual(config["provider_country"], "JP")
        self.assertEqual(config["approve_country"], "CA")
        self.assertEqual(config["target_country"], "DE")
        self.assertEqual(config["payment_method_country"], "JP")
        self.assertEqual(config["promotion_country"], "BR")
        self.assertEqual(plus_link_store._extraction_mode(config), "fixed_pair")
        self.assertEqual(plus_link_store._public_config(config)["matrix_total"], 1)

    def test_minimal_confirm_protocol_is_preserved(self) -> None:
        config = plus_link_store._normalize_config({"paypal_confirm_protocol": "minimal"})
        self.assertEqual(config["paypal_confirm_protocol"], "minimal")

    def test_chatgpt_confirm_protocol_is_preserved(self) -> None:
        config = plus_link_store._normalize_config({"paypal_confirm_protocol": "chatgpt_confirm"})
        self.assertEqual(config["paypal_confirm_protocol"], "chatgpt_confirm")

    def test_fixed_pair_keeps_exit_and_billing_countries_independent(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "target_country": "US",
                "checkout_country": "BR",
                "payment_method_country": "US",
                "promotion_country": "TR",
                "stripe_poll_egress": "promotion",
            }
        )
        service = plus_link_store.PlusLinkService(Path(tempfile.mkdtemp()) / "plus_link.json")
        proxy_info = {
            "proxy": "http://left.invalid:8080",
            "checkout_proxy": "http://left.invalid:8080",
            "provider_proxy": "http://left.invalid:8080",
            "approve_proxy": "http://left.invalid:8080",
            "promotion_proxy": "http://right.invalid:8080",
        }

        with (
            patch.object(service, "_assign_residential_proxy", return_value=proxy_info),
            patch.object(service, "_assign_project_parent_proxy", return_value=None),
        ):
            payload, _ = service._payload_for_account(
                {"email": "free@example.com", "access_token": "token"},
                config,
                1,
            )

        self.assertEqual(payload["billing_country"], "US")
        self.assertEqual(payload["payment_method_country"], "US")
        self.assertEqual(payload["batch_checkout_proxy_country"], "BR")
        self.assertEqual(payload["batch_approve_proxy_country"], "BR")
        self.assertEqual(payload["batch_target_country"], "BR")
        self.assertEqual(payload["promotion_proxy_country"], "TR")
        self.assertEqual(payload["checkout_ui_mode"], "custom")
        self.assertEqual(payload["stripe_poll_egress"], "provider")

    def test_fixed_pair_can_use_jp_checkout_and_approve_with_br_provider(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "target_country": "US",
                "checkout_country": "JP",
                "provider_country": "BR",
                "approve_country": "JP",
                "payment_method_country": "US",
                "promotion_country": "BR",
            }
        )
        service = plus_link_store.PlusLinkService(Path(tempfile.mkdtemp()) / "plus_link.json")

        def assignment(_cfg, _index, *, left_country, promotion_country):
            return {
                "proxy": f"http://{left_country.lower()}.invalid:8080",
                "checkout_proxy": f"http://{left_country.lower()}.invalid:8080",
                "provider_proxy": f"http://{left_country.lower()}.invalid:8080",
                "approve_proxy": f"http://{left_country.lower()}.invalid:8080",
                "promotion_proxy": f"http://{promotion_country.lower()}.invalid:8080",
                "checkout_proxy_pool": [f"http://{left_country.lower()}.invalid:8080"],
                "provider_proxy_pool": [f"http://{left_country.lower()}.invalid:8080"],
                "approve_proxy_pool": [f"http://{left_country.lower()}.invalid:8080"],
                "promotion_proxy_pool": [f"http://{promotion_country.lower()}.invalid:8080"],
            }

        with (
            patch.object(service, "_assign_residential_proxy", side_effect=assignment),
            patch.object(service, "_assign_project_parent_proxy", return_value=None),
        ):
            payload, proxy_info = service._payload_for_account(
                {"email": "free@example.com", "access_token": "token"},
                config,
                1,
            )

        self.assertEqual(payload["checkout_proxy_override"], "http://jp.invalid:8080")
        self.assertEqual(payload["provider_proxy_override"], "http://br.invalid:8080")
        self.assertEqual(payload["approve_proxy_override"], "http://jp.invalid:8080")
        self.assertEqual(payload["promotion_proxy_override"], "http://br.invalid:8080")
        self.assertEqual(payload["batch_checkout_proxy_country"], "JP")
        self.assertEqual(payload["batch_target_country"], "BR")
        self.assertEqual(payload["batch_approve_proxy_country"], "JP")
        self.assertTrue(payload["approve_same_as_checkout"])
        self.assertTrue(payload["provider_same_as_target"])
        self.assertFalse(payload["strict_same_proxy_ip"])
        self.assertTrue(payload["strict_approve_same_proxy_ip"])
        self.assertTrue(payload["strict_right_same_proxy_ip"])
        self.assertFalse(payload["require_ipv6_egress"])
        self.assertEqual(proxy_info["provider_country"], "BR")
        self.assertEqual(proxy_info["approve_country"], "JP")

    def test_fixed_pair_preserves_explicit_strict_approve_ip_lock(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "target_country": "US",
                "checkout_country": "JP",
                "provider_country": "BR",
                "approve_country": "JP",
                "promotion_country": "BR",
                "strict_approve_same_proxy_ip": True,
                "require_ipv6_egress": True,
            }
        )
        service = plus_link_store.PlusLinkService(Path(tempfile.mkdtemp()) / "plus_link.json")
        with (
            patch.object(
                service,
                "_assign_residential_proxy",
                return_value={
                    "proxy": "http://br.invalid:8080",
                    "checkout_proxy": "http://br.invalid:8080",
                    "provider_proxy": "http://br.invalid:8080",
                    "approve_proxy": "http://br.invalid:8080",
                    "promotion_proxy": "http://br.invalid:8080",
                    "checkout_proxy_pool": ["http://br.invalid:8080"],
                    "provider_proxy_pool": ["http://br.invalid:8080"],
                    "approve_proxy_pool": ["http://br.invalid:8080"],
                    "promotion_proxy_pool": ["http://br.invalid:8080"],
                },
            ),
            patch.object(service, "_assign_project_parent_proxy", return_value=None),
        ):
            payload, _ = service._payload_for_account(
                {"email": "free@example.com", "access_token": "token"},
                config,
                1,
            )

        self.assertTrue(payload["strict_approve_same_proxy_ip"])
        self.assertTrue(payload["strict_right_same_proxy_ip"])
        self.assertTrue(payload["require_ipv6_egress"])

    def test_same_provider_and_promotion_country_share_one_right_session(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "checkout_country": "DE",
                "provider_country": "TH",
                "promotion_country": "TH",
            }
        )
        service = plus_link_store.PlusLinkService(Path(tempfile.mkdtemp()) / "plus_link.json")
        assignments = [
            {
                "proxy": "http://checkout.invalid:8080",
                "checkout_proxy": "http://checkout.invalid:8080",
                "provider_proxy": "http://checkout.invalid:8080",
                "approve_proxy": "http://checkout.invalid:8080",
                "promotion_proxy": "http://promotion-old.invalid:8080",
                "checkout_proxy_pool": ["http://checkout.invalid:8080"],
                "provider_proxy_pool": ["http://checkout.invalid:8080"],
                "approve_proxy_pool": ["http://checkout.invalid:8080"],
                "promotion_proxy_pool": ["http://promotion-old.invalid:8080"],
            },
            {
                "proxy": "http://provider.invalid:8080",
                "checkout_proxy": "http://provider.invalid:8080",
                "provider_proxy": "http://provider.invalid:8080",
                "approve_proxy": "http://provider.invalid:8080",
                "promotion_proxy": "http://promotion-new.invalid:8080",
                "checkout_proxy_pool": ["http://provider.invalid:8080"],
                "provider_proxy_pool": ["http://provider.invalid:8080"],
                "approve_proxy_pool": ["http://provider.invalid:8080"],
                "promotion_proxy_pool": ["http://promotion-new.invalid:8080"],
            },
        ]

        with (
            patch.object(service, "_assign_residential_proxy", side_effect=assignments),
            patch.object(service, "_assign_project_parent_proxy", return_value=None),
        ):
            payload, _ = service._payload_for_account(
                {"email": "free@example.com", "access_token": "token"},
                config,
                1,
            )

        self.assertEqual(payload["provider_proxy_override"], "http://provider.invalid:8080")
        self.assertEqual(payload["promotion_proxy_override"], payload["provider_proxy_override"])
        self.assertEqual(payload["promotion_proxy_pool"], payload["provider_proxy_pool"])
        self.assertTrue(payload["strict_right_same_proxy_ip"])
        self.assertEqual(payload["stripe_poll_egress"], "provider")

    def test_br_711_template_is_reused_without_requiring_a_separate_us_pool(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "target_country": "US",
                "checkout_country": "JP",
                "provider_country": "BR",
                "approve_country": "JP",
                "payment_method_country": "US",
                "promotion_country": "BR",
                "residential_country_proxy_pool": {
                    "BR": "http://USER-zone-custom-region-BR:fixture@global.example.invalid:10000"
                },
            }
        )
        service = plus_link_store.PlusLinkService(Path(tempfile.mkdtemp()) / "plus_link.json")

        with patch.object(service, "_assign_project_parent_proxy", return_value=None):
            payload, _ = service._payload_for_account(
                {"email": "free@example.com", "access_token": "token"},
                config,
                1,
            )

        self.assertIn("region-JP", payload["checkout_proxy_override"])
        self.assertIn("region-BR", payload["provider_proxy_override"])
        self.assertIn("region-JP", payload["approve_proxy_override"])
        self.assertIn("region-BR", payload["promotion_proxy_override"])
        self.assertNotEqual(payload["checkout_proxy_override"], payload["provider_proxy_override"])

    def test_project_parent_provider_egress_keeps_chatgpt_on_residential_pools(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "target_country": "US",
                "checkout_country": "BR",
                "payment_method_country": "US",
                "promotion_country": "BR",
                "stripe_provider_egress": "project_parent",
            }
        )
        service = plus_link_store.PlusLinkService(Path(tempfile.mkdtemp()) / "plus_link.json")
        proxy_info = {
            "proxy": "http://left.invalid:8080",
            "checkout_proxy": "http://left.invalid:8080",
            "provider_proxy": "http://left.invalid:8080",
            "approve_proxy": "http://left.invalid:8080",
            "promotion_proxy": "http://right.invalid:8080",
            "provider_proxy_pool": ["http://left.invalid:8080"],
        }
        parent = {
            "proxy": "http://127.0.0.1:42000",
            "source": "managed_mihomo",
            "label": "US parent",
            "slot": 1,
            "assignment_index": 7,
            "node_name_filter": "[ex-girlfriend]",
        }

        with (
            patch.object(service, "_assign_residential_proxy", return_value=proxy_info),
            patch.object(service, "_assign_project_parent_proxy", return_value=parent),
        ):
            payload, _ = service._payload_for_account(
                {"email": "free@example.com", "access_token": "token"},
                config,
                1,
            )

        self.assertEqual(payload["checkout_proxy_override"], "http://left.invalid:8080")
        self.assertEqual(payload["promotion_proxy_override"], "http://right.invalid:8080")
        self.assertEqual(payload["approve_proxy_override"], "http://left.invalid:8080")
        self.assertEqual(payload["provider_proxy_override"], "http://127.0.0.1:42000")
        self.assertTrue(payload["provider_parent_only"])
        self.assertEqual(payload["provider_proxy_pool"], [])
        self.assertTrue(payload["provider_same_as_target"])
        self.assertFalse(payload["strict_same_proxy_ip"])
        self.assertTrue(payload["recreate_checkout_per_attempt"])
        self.assertTrue(payload["parent_proxy_rotation_enabled"])
        self.assertEqual(payload["parent_proxy_rotation_task_index"], 7)
        self.assertEqual(payload["parent_proxy_rotation_slot"], 0)
        self.assertEqual(payload["parent_proxy_rotation_node_filter"], "[ex-girlfriend]")

    def test_us_residential_provider_egress_uses_a_distinct_us_sticky_proxy(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "target_country": "US",
                "checkout_country": "BR",
                "payment_method_country": "US",
                "promotion_country": "BR",
                "stripe_provider_egress": "us_residential",
            }
        )
        service = plus_link_store.PlusLinkService(Path(tempfile.mkdtemp()) / "plus_link.json")

        def assignment(_cfg, _index, *, left_country, promotion_country):
            return {
                "proxy": f"http://{left_country.lower()}.invalid:8080",
                "checkout_proxy": f"http://{left_country.lower()}.invalid:8080",
                "provider_proxy": f"http://{left_country.lower()}.invalid:8080",
                "approve_proxy": f"http://{left_country.lower()}.invalid:8080",
                "promotion_proxy": f"http://{promotion_country.lower()}.invalid:8080",
                "provider_proxy_pool": [f"http://{left_country.lower()}.invalid:8080"],
            }

        with (
            patch.object(service, "_assign_residential_proxy", side_effect=assignment),
            patch.object(service, "_assign_project_parent_proxy", return_value={"proxy": "http://127.0.0.1:42000"}),
        ):
            payload, _ = service._payload_for_account(
                {"email": "free@example.com", "access_token": "token"},
                config,
                1,
            )

        self.assertEqual(payload["checkout_proxy_override"], "http://br.invalid:8080")
        self.assertEqual(payload["approve_proxy_override"], "http://br.invalid:8080")
        self.assertEqual(payload["provider_proxy_override"], "http://us.invalid:8080")
        self.assertEqual(payload["provider_proxy_pool"], ["http://us.invalid:8080"])
        self.assertFalse(payload["provider_parent_only"])
        self.assertTrue(payload["provider_same_as_target"])
        self.assertFalse(payload["strict_same_proxy_ip"])

    def test_legacy_retry_limits_are_decoupled(self) -> None:
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "provider_retry_limit": 1,
                "matrix_combo_retry_limit": 1,
            }
        )
        self.assertEqual(config["provider_retry_limit"], 15)
        self.assertEqual(config["matrix_combo_retry_limit"], 1)
        self.assertEqual(config["retry_limits_version"], 2)

    def test_fixed_and_matrix_modes_use_separate_retry_limits(self) -> None:
        single = plus_link_store._normalize_config(
            {
                "retry_limits_version": 2,
                "country_rotation_enabled": False,
                "provider_retry_limit": 7,
                "matrix_combo_retry_limit": 2,
            }
        )
        multiple = plus_link_store._normalize_config(
            {
                "retry_limits_version": 2,
                "country_rotation_enabled": True,
                "billing_country_rotation": ["US", "DE"],
                "proxy_country_rotation": ["BR", "JP"],
                "matrix_combo_retry_limit": 2,
            }
        )
        self.assertEqual(plus_link_store._extraction_mode(single), "fixed_pair")
        self.assertEqual(plus_link_store._retry_limit_for_mode(single), 7)
        self.assertEqual(plus_link_store._public_config(single)["matrix_total"], 1)
        self.assertEqual(plus_link_store._extraction_mode(multiple), "matrix")
        self.assertEqual(plus_link_store._retry_limit_for_mode(multiple), 2)

    def test_payload_uses_mode_specific_retry_limit(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-modes-")) / "plus_link.json"
        )
        service._assign_residential_proxy = lambda *_args, **_kwargs: {
            "proxy": "http://left.test:8080",
            "checkout_proxy": "http://left.test:8080",
            "provider_proxy": "http://left.test:8080",
            "approve_proxy": "http://left.test:8080",
            "promotion_proxy": "http://right.test:8080",
            "checkout_proxy_pool": ["http://left.test:8080"],
            "provider_proxy_pool": ["http://left.test:8080"],
            "approve_proxy_pool": ["http://left.test:8080"],
            "promotion_proxy_pool": ["http://right.test:8080"],
            "label": "test dual pool",
            "single_proxy_mode": False,
        }
        service._assign_project_parent_proxy = lambda *_args, **_kwargs: None
        account = {"email": "free@example.com", "access_token": "token"}

        single = plus_link_store._normalize_config(
            {
                "retry_limits_version": 2,
                "country_rotation_enabled": False,
                "target_country": "US",
                "promotion_country": "TR",
                "provider_retry_limit": 9,
                "matrix_combo_retry_limit": 3,
            }
        )
        multiple = plus_link_store._normalize_config(
            {
                "retry_limits_version": 2,
                "country_rotation_enabled": True,
                "billing_country_rotation": ["US"],
                "proxy_country_rotation": ["TR"],
                "matrix_combo_retry_limit": 3,
            }
        )
        single_payload, _ = service._payload_for_account(account, single, 1)
        multiple_payload, _ = service._payload_for_account(account, multiple, 1)
        self.assertEqual(single_payload["provider_retry_limit"], 9)
        self.assertEqual(multiple_payload["provider_retry_limit"], 3)

    def test_five_by_four_country_selection_creates_twenty_combinations(self) -> None:
        config = plus_link_store._normalize_config({
            "country_rotation_enabled": True,
            "billing_country_rotation": ["US", "IN", "TH", "NZ", "RO"],
            "proxy_country_rotation": ["BR", "JP", "TR", "VN"],
        })

        self.assertEqual(plus_link_store._public_config(config)["matrix_total"], 20)
        self.assertEqual(config["checkout_country"], "US")
        self.assertEqual(config["approve_country"], "US")
        self.assertEqual(config["target_country"], "US")
        self.assertEqual(config["payment_method_country"], "US")
        self.assertEqual(config["provider_country"], "US")
        self.assertEqual(config["promotion_country"], "BR")
        self.assertEqual(len(plus_link_store._matrix_country_pairs(config["billing_country_rotation"], config["proxy_country_rotation"])), 20)

    def test_matrix_uses_fixed_checkout_and_independent_provider_and_promotion_pools(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-matrix-stages-")) / "plus_link.json"
        )

        def assignment(_cfg, _index, *, left_country, promotion_country):
            left = f"http://user-zone-custom-region-{left_country}:pass@proxy.invalid:8080"
            right = f"http://user-zone-custom-region-{promotion_country}:pass@proxy.invalid:8080"
            return {
                "proxy": left,
                "checkout_proxy": left,
                "provider_proxy": left,
                "approve_proxy": left,
                "promotion_proxy": right,
                "checkout_proxy_pool": [left],
                "provider_proxy_pool": [left],
                "approve_proxy_pool": [left],
                "promotion_proxy_pool": [right],
            }

        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": True,
                "checkout_country": "GB",
                "target_country": "GB",
                "payment_method_country": "GB",
                "provider_country": "JP",
                "approve_country": "GB",
                "billing_country_rotation": ["US", "DE"],
                "proxy_country_rotation": ["TR"],
            }
        )
        service._assign_residential_proxy = assignment
        service._assign_project_parent_proxy = lambda *_args, **_kwargs: None

        first, _ = service._payload_for_account(
            {"email": "free@example.com", "access_token": "token"},
            config,
            1,
        )
        second, _ = service._payload_for_account(
            {"email": "free@example.com", "access_token": "token"},
            config,
            2,
        )

        self.assertEqual(first["batch_checkout_proxy_country"], "GB")
        self.assertEqual(first["batch_target_country"], "US")
        self.assertEqual(first["batch_approve_proxy_country"], "GB")
        self.assertEqual(first["payment_method_country"], "US")
        self.assertEqual(second["batch_checkout_proxy_country"], "GB")
        self.assertEqual(second["batch_target_country"], "DE")
        self.assertEqual(second["batch_approve_proxy_country"], "GB")
        self.assertEqual(second["payment_method_country"], "DE")
        self.assertEqual(first["promotion_proxy_country"], "TR")
        self.assertNotEqual(first["checkout_proxy_override"], first["provider_proxy_override"])
        self.assertNotEqual(first["provider_proxy_override"], first["promotion_proxy_override"])
        self.assertEqual(first["checkout_proxy_override"], first["approve_proxy_override"])
        self.assertFalse(first["strict_same_proxy_ip"])
        self.assertFalse(first["strict_right_same_proxy_ip"])
        self.assertEqual(first["stripe_poll_egress"], "provider")

    def test_matrix_keeps_provider_and_promotion_sticky_sessions_distinct_for_same_country(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-three-exits-")) / "plus_link.json"
        )
        calls = 0

        def assignment(_cfg, _index, *, left_country, promotion_country):
            nonlocal calls
            calls += 1
            left = f"http://{left_country.lower()}-sticky-{calls}.invalid:8080"
            right = f"http://{promotion_country.lower()}-promo-{calls}.invalid:8080"
            return {
                "proxy": left,
                "checkout_proxy": left,
                "provider_proxy": left,
                "approve_proxy": left,
                "promotion_proxy": right,
                "checkout_proxy_pool": [left],
                "provider_proxy_pool": [left],
                "approve_proxy_pool": [left],
                "promotion_proxy_pool": [right],
            }

        config = plus_link_store._normalize_config({
            "country_rotation_enabled": True,
            "checkout_country": "GB",
            "billing_country_rotation": ["JP"],
            "proxy_country_rotation": ["JP"],
        })
        service._assign_residential_proxy = assignment
        service._assign_project_parent_proxy = lambda *_args, **_kwargs: None

        payload, _ = service._payload_for_account(
            {"email": "free@example.com", "access_token": "token"},
            config,
            1,
        )

        self.assertEqual(calls, 2)
        self.assertEqual(payload["batch_checkout_proxy_country"], "GB")
        self.assertEqual(payload["batch_target_country"], "JP")
        self.assertEqual(payload["promotion_proxy_country"], "JP")
        self.assertNotEqual(payload["provider_proxy_override"], payload["promotion_proxy_override"])
        self.assertFalse(payload["strict_right_same_proxy_ip"])
        self.assertEqual(payload["stripe_poll_egress"], "provider")

    def test_precheck_uses_checkout_session(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-precheck-")) / "plus_link.json"
        )
        service._payload_for_account = lambda *_args, **_kwargs: (
            {
                "accessToken": "token",
                "checkout_proxy_override": "http://left.test:8080",
            },
            {"label": "left pool"},
        )
        service._wait_for_task_ready = lambda *_args, **_kwargs: None
        service._set_task_result = lambda *_args, **_kwargs: None
        fake_session = Mock()

        class FakeRequest:
            @classmethod
            def model_validate(cls, value):
                return value

        fake_engine = SimpleNamespace(
            LongLinkRequest=FakeRequest,
            build_chatgpt_session=lambda request, proxy_override="": fake_session,
        )
        config = plus_link_store._normalize_config(
            {
                "retry_limits_version": 2,
                "country_rotation_enabled": False,
                "checkout_retry_limit": 2,
            }
        )
        with patch.object(plus_link_store, "_load_plus_engine", return_value=fake_engine), patch.object(
            plus_link_store,
            "probe_plus_link_eligibility",
            return_value={
                "ok": True,
                "eligible": True,
                "reason": "eligible",
                "account_type": "free",
                "has_email": True,
            },
        ) as eligibility_probe:
            result = service._precheck_account_eligibility(
                {"email": "free@example.com", "access_token": "token"},
                config,
                "token",
                task_id="task",
                email="free@example.com",
            )
        self.assertTrue(result["eligible"])
        self.assertIs(eligibility_probe.call_args.kwargs["session"], fake_session)
        fake_session.close.assert_called_once()

    def test_precheck_has_independent_network_retries_and_task_proxy_slots(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-precheck-retry-")) / "plus_link.json"
        )
        proxy_indices: list[int] = []

        def payload_for_account(_account, _cfg, index):
            proxy_indices.append(index)
            return (
                {
                    "accessToken": "token",
                    "checkout_proxy_override": "http://user:pass@global.example.711proxy.com:10000",
                },
                {
                    "label": f"left slot {index}",
                    "checkout_slot": index,
                    "parent_proxy_source": "managed_mihomo",
                },
            )

        service._payload_for_account = payload_for_account
        service._wait_for_task_ready = lambda *_args, **_kwargs: None
        service._set_task_result = lambda *_args, **_kwargs: None
        service._wait_for_eligibility_retry = lambda *_args, **_kwargs: None

        class FakeRequest:
            @classmethod
            def model_validate(cls, value):
                return value

        fake_engine = SimpleNamespace(
            LongLinkRequest=FakeRequest,
            build_chatgpt_session=lambda request, proxy_override="": object(),
        )
        probe_results = [
            {
                "ok": False,
                "eligible": False,
                "reason": "upstream_unavailable",
                "detail": "账号资格检测无法访问 ChatGPT: HTTP 0 Proxy CONNECT aborted",
            },
            {
                "ok": False,
                "eligible": False,
                "reason": "upstream_unavailable",
                "detail": "账号资格检测无法访问 ChatGPT: HTTP 0 CONNECT tunnel failed, response 502",
            },
            {
                "ok": True,
                "eligible": True,
                "reason": "eligible",
                "account_type": "free",
                "has_email": True,
            },
        ]
        config = plus_link_store._normalize_config(
            {
                "country_rotation_enabled": False,
                "checkout_retry_limit": 1,
            }
        )
        with patch.object(plus_link_store, "_load_plus_engine", return_value=fake_engine), patch.object(
            plus_link_store,
            "probe_plus_link_eligibility",
            side_effect=probe_results,
        ):
            result = service._precheck_account_eligibility(
                {"email": "free@example.com", "access_token": "token"},
                config,
                "token",
                task_id="task",
                email="free@example.com",
                task_index=2,
            )

        self.assertTrue(result["eligible"])
        self.assertEqual(proxy_indices, [2, 3, 4])

    def test_precheck_uses_separate_retry_budget_without_consuming_link_attempts(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-precheck-budget-")) / "plus_link.json"
        )
        updates: list[dict[str, object]] = []
        service._payload_for_account = lambda *_args, **_kwargs: (
            {"accessToken": "token", "checkout_proxy_override": "http://proxy.example:10000"},
            {"label": "checkout slot", "checkout_slot": 1},
        )
        service._wait_for_task_ready = lambda *_args, **_kwargs: None
        service._wait_for_eligibility_retry = lambda *_args, **_kwargs: None
        service._set_task_result = lambda _task_id, update: updates.append(dict(update)) or True

        class FakeRequest:
            @classmethod
            def model_validate(cls, value):
                return value

        fake_engine = SimpleNamespace(
            LongLinkRequest=FakeRequest,
            build_chatgpt_session=lambda request, proxy_override="": object(),
        )
        transient = {
            "ok": False,
            "eligible": False,
            "reason": "upstream_unavailable",
            "detail": "账号资格检测无法访问 ChatGPT: HTTP 503 Service Unavailable",
        }
        eligible = {
            "ok": True,
            "eligible": True,
            "reason": "eligible",
            "account_type": "free",
            "has_email": True,
        }
        config = plus_link_store._normalize_config(
            {"country_rotation_enabled": False, "fixed_retry_limit": 10}
        )
        with patch.object(plus_link_store, "_load_plus_engine", return_value=fake_engine), patch.object(
            plus_link_store,
            "probe_plus_link_eligibility",
            side_effect=[transient] * 9 + [eligible],
        ) as probe:
            result = service._precheck_account_eligibility(
                {"email": "free@example.com", "access_token": "token"},
                config,
                "token",
                task_id="task",
                email="free@example.com",
            )

        self.assertTrue(result["eligible"])
        self.assertEqual(probe.call_count, 10)
        self.assertTrue(any(update.get("eligibility_attempt") == 10 for update in updates))
        self.assertFalse(any("link_attempt_count" in update for update in updates))

    def test_precheck_reports_residential_proxy_failure_before_token_validation(self) -> None:
        message = plus_link_store._eligibility_failure_message(
            "账号资格检测无法访问 ChatGPT: HTTP 0 Failed to perform, curl: (56) CONNECT tunnel failed, response 502",
            attempts=3,
            checkout_proxy="http://user:pass@global.example.711proxy.com:10000",
        )

        self.assertIn("提链前住宅代理检测失败（3/3）", message)
        self.assertIn("尚未完成 access_token 有效性检测", message)
        self.assertIn("711Proxy 控制台", message)
        self.assertIn("未进入国家矩阵或 Stripe", message)

    def test_engine_diagnostics_keep_sanitized_failure_detail(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-diagnostics-")) / "plus_link.json"
        )
        service._state["results"] = [{"task_id": "task", "status": "running"}]

        class FakeRequest:
            @classmethod
            def model_validate(cls, value):
                return value

        def generate(_request, emit):
            emit(
                "combo_result",
                "combination failed",
                detail="checkout promotion update failed: Bearer secret-token",
                stripe_amount="0",
                stripe_amount_source="checkout.total",
                currency="usd",
                billing_profile_source="formatted",
                billing_email_domain="example.com",
            )
            return {"ok": False}

        fake_engine = SimpleNamespace(
            LongLinkRequest=FakeRequest,
            CANCEL_CONTEXT=SimpleNamespace(event=None),
            generate_long_link_payload=generate,
        )
        with patch.object(plus_link_store, "_load_plus_engine", return_value=fake_engine):
            service._generate_local_long_link({}, task_id="task", email="free@example.com")

        result = service._result_for_task("task") or {}
        self.assertEqual(result["last_engine_message"], "combination failed")
        self.assertIn("checkout promotion update failed", result["last_engine_detail"])
        self.assertNotIn("secret-token", result["last_engine_detail"])
        self.assertEqual(result["stripe_amount"], "0")
        self.assertEqual(result["stripe_amount_source"], "checkout.total")
        self.assertEqual(result["currency"], "usd")
        self.assertEqual(result["billing_profile_source"], "formatted")
        self.assertEqual(result["billing_email_domain"], "example.com")

    def test_new_link_attempt_clears_previous_payment_diagnostics(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-attempt-reset-")) / "plus_link.json"
        )
        service._state["results"] = [{"task_id": "task", "status": "running"}]

        class FakeRequest:
            @classmethod
            def model_validate(cls, value):
                return value

        def generate(_request, emit):
            emit(
                "confirm",
                "first attempt",
                paypal_availability="supported",
                stripe_amount="0",
                failure_category="generic_decline",
                stripe_submission_profile={"submission_reason": "generic_decline"},
            )
            emit("link_attempt", "second attempt", reset_attempt_diagnostics=True)
            return {"ok": False}

        fake_engine = SimpleNamespace(
            LongLinkRequest=FakeRequest,
            CANCEL_CONTEXT=SimpleNamespace(event=None),
            generate_long_link_payload=generate,
        )
        with patch.object(plus_link_store, "_load_plus_engine", return_value=fake_engine):
            service._generate_local_long_link({}, task_id="task", email="free@example.com")

        result = service._result_for_task("task") or {}
        self.assertIsNone(result["paypal_availability"])
        self.assertIsNone(result["stripe_amount"])
        self.assertIsNone(result["failure_category"])
        self.assertIsNone(result["stripe_submission_profile"])

    def test_link_attempt_history_persists_each_round_and_deduplicates_nested_failures(self) -> None:
        service = plus_link_store.PlusLinkService(
            Path(tempfile.mkdtemp(prefix="plus-link-attempt-history-")) / "plus_link.json"
        )
        service._state["results"] = [{
            "task_id": "task",
            "status": "running",
            "matrix_index": 1,
            "matrix_total": 1,
            "billing_country": "DE",
            "checkout_proxy_country": "DE",
            "provider_country": "JP",
            "approve_country": "DE",
            "promotion_country": "JP",
            "payment_method_country": "JP",
        }]

        class FakeRequest:
            @classmethod
            def model_validate(cls, value):
                return value

        def generate(_request, emit):
            emit(
                "link_attempt",
                "提链尝试 1/2：开始",
                link_attempt_count=1,
                link_attempt_limit=2,
                reset_attempt_diagnostics=True,
            )
            emit("proxy", "provider JP", proxy_stage="provider", proxy_ip="203.0.113.10", proxy_country_code="JP")
            emit("approve", "ChatGPT approve 请求成功", proxy_stage="approve", proxy_ip="198.51.100.20", proxy_country_code="DE")
            emit(
                "redirect",
                "Stripe submission 已失败：state=failed，reason=generic_decline。",
                stripe_submission_profile={
                    "submission_state": "failed",
                    "submission_reason": "generic_decline",
                    "submission_code": "checkout_approval_payment_failure_with_payment_error",
                },
                failure_category="generic_decline",
            )
            emit(
                "link_attempt",
                "提链尝试 1/2 失败：stripe submission failed; reason=generic_decline",
                link_attempt_count=1,
                link_attempt_limit=2,
                attempt_status="failed",
                attempt_error="stripe submission failed; reason=generic_decline",
            )
            emit(
                "link_attempt",
                "提链尝试 1/2 失败：provider 提链已达到上限 1 次仍失败: stripe submission failed; reason=generic_decline",
                link_attempt_count=1,
                link_attempt_limit=2,
                attempt_status="failed",
                attempt_error="provider 提链已达到上限 1 次仍失败: stripe submission failed; reason=generic_decline",
            )
            emit(
                "link_attempt",
                "提链尝试 2/2：开始",
                link_attempt_count=2,
                link_attempt_limit=2,
                reset_attempt_diagnostics=True,
            )
            emit(
                "sync",
                "checkout promotion update failed; status=403",
                detail="checkout promotion update failed; status=403; content_type=text/html",
            )
            emit(
                "link_attempt",
                "提链尝试 2/2 失败：checkout promotion update failed; status=403",
                link_attempt_count=2,
                link_attempt_limit=2,
                attempt_status="failed",
                attempt_error="checkout promotion update failed; status=403",
            )
            return {"ok": False}

        fake_engine = SimpleNamespace(
            LongLinkRequest=FakeRequest,
            CANCEL_CONTEXT=SimpleNamespace(event=None),
            generate_long_link_payload=generate,
        )
        with patch.object(plus_link_store, "_load_plus_engine", return_value=fake_engine):
            service._generate_local_long_link({}, task_id="task", email="free@example.com")

        result = service._result_for_task("task") or {}
        history = result.get("attempt_diagnostics_history")
        self.assertIsInstance(history, list)
        assert isinstance(history, list)
        self.assertEqual(len(history), 2)
        first, second = history
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["failure_step"], "redirect")
        self.assertEqual(first["failure_category"], "generic_decline")
        self.assertEqual(first["submission_reason"], "generic_decline")
        self.assertEqual(first["provider_proxy_ip"], "203.0.113.10")
        self.assertEqual(first["approve_proxy_ip"], "198.51.100.20")
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(second["failure_step"], "sync")
        self.assertEqual(second["failure_category"], "promotion_update")
        self.assertIn("status=403", second["promotion_error"])
        self.assertEqual(result["last_redirect_message"], "")


class PlusLinkEligibilityTests(unittest.TestCase):
    def test_free_email_account_is_eligible(self) -> None:
        token = _jwt(
            {
                "exp": int(time.time()) + 3600,
                "https://api.openai.com/profile": {"email": "free@example.com"},
            }
        )
        with patch.object(
            account_inventory_service,
            "_safe_backend_get",
            side_effect=[
                (200, {"email": "free@example.com"}, "{}"),
                (200, {"models": []}, "{}"),
            ],
        ):
            result = account_inventory_service.probe_plus_link_eligibility(token, session=object())
        self.assertTrue(result["ok"])
        self.assertTrue(result["eligible"])
        self.assertEqual(result["account_type"], "free")

    def test_paid_account_is_rejected(self) -> None:
        token = _jwt(
            {
                "exp": int(time.time()) + 3600,
                "https://api.openai.com/profile": {"email": "paid@example.com"},
            }
        )
        with patch.object(
            account_inventory_service,
            "_safe_backend_get",
            side_effect=[
                (200, {"email": "paid@example.com"}, "{}"),
                (200, {"models": [{"slug": "o3", "title": "o3"}]}, "{}"),
            ],
        ):
            result = account_inventory_service.probe_plus_link_eligibility(token, session=object())
        self.assertTrue(result["ok"])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "account_not_free")

    def test_me_503_falls_back_to_models_and_token_profile(self) -> None:
        token = _jwt(
            {
                "exp": int(time.time()) + 3600,
                "https://api.openai.com/profile": {"email": "fallback@example.com"},
            }
        )
        with patch.object(
            account_inventory_service,
            "_safe_backend_get",
            side_effect=[
                (503, {}, '{"error":{"message":"Service Unavailable","code":"biscuit_baker_service_me_circuit_open"}}'),
                (200, {"models": []}, "{}"),
            ],
        ):
            result = account_inventory_service.probe_plus_link_eligibility(token, session=object())

        self.assertTrue(result["ok"])
        self.assertTrue(result["eligible"])
        self.assertTrue(result["degraded_validation"])
        self.assertEqual(result["me_status_code"], 503)
        self.assertEqual(result["models_status_code"], 200)

    def test_both_eligibility_endpoints_unavailable_remains_retryable(self) -> None:
        token = _jwt(
            {
                "exp": int(time.time()) + 3600,
                "https://api.openai.com/profile": {"email": "retry@example.com"},
            }
        )
        with patch.object(
            account_inventory_service,
            "_safe_backend_get",
            side_effect=[
                (500, {}, "Internal Server Error"),
                (503, {}, "Service Unavailable"),
            ],
        ):
            result = account_inventory_service.probe_plus_link_eligibility(token, session=object())

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "upstream_unavailable")
        self.assertIn("me HTTP 500", result["detail"])
        self.assertIn("models HTTP 503", result["detail"])

    def test_expired_token_is_rejected_before_upstream(self) -> None:
        token = _jwt(
            {
                "exp": int(time.time()) - 60,
                "https://api.openai.com/profile": {"email": "expired@example.com"},
            }
        )
        with patch.object(account_inventory_service, "_safe_backend_get") as upstream:
            result = account_inventory_service.probe_plus_link_eligibility(token, session=object())
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "token_expired")
        upstream.assert_not_called()

    def test_email_is_required(self) -> None:
        token = _jwt({"exp": int(time.time()) + 3600})
        with patch.object(
            account_inventory_service,
            "_safe_backend_get",
            side_effect=[
                (200, {}, "{}"),
                (200, {"models": []}, "{}"),
            ],
        ):
            result = account_inventory_service.probe_plus_link_eligibility(token, session=object())
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "email_missing")


if __name__ == "__main__":
    unittest.main()
