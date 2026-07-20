from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services import account_inventory_service, plus_link_store


def _jwt(payload: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.signature"


class PlusLinkModeTests(unittest.TestCase):
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

    def test_fixed_and_matrix_retry_limits_are_independent(self) -> None:
        fixed = plus_link_store._normalize_config(
            {
                "retry_limits_version": 2,
                "country_rotation_enabled": False,
                "provider_retry_limit": 7,
                "matrix_combo_retry_limit": 2,
            }
        )
        matrix = plus_link_store._normalize_config(
            {
                **fixed,
                "country_rotation_enabled": True,
            }
        )
        self.assertEqual(plus_link_store._extraction_mode(fixed), "fixed_pair")
        self.assertEqual(plus_link_store._retry_limit_for_mode(fixed), 7)
        self.assertEqual(plus_link_store._extraction_mode(matrix), "matrix")
        self.assertEqual(plus_link_store._retry_limit_for_mode(matrix), 2)

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

        fixed = plus_link_store._normalize_config(
            {
                "retry_limits_version": 2,
                "country_rotation_enabled": False,
                "target_country": "US",
                "promotion_country": "TR",
                "provider_retry_limit": 9,
                "matrix_combo_retry_limit": 3,
            }
        )
        matrix = plus_link_store._normalize_config(
            {
                **fixed,
                "country_rotation_enabled": True,
                "billing_country_rotation": ["US"],
                "proxy_country_rotation": ["TR"],
            }
        )
        fixed_payload, _ = service._payload_for_account(account, fixed, 1)
        matrix_payload, _ = service._payload_for_account(account, matrix, 1)
        self.assertEqual(fixed_payload["provider_retry_limit"], 9)
        self.assertEqual(matrix_payload["provider_retry_limit"], 3)

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
        fake_session = object()

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
