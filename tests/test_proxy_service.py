from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services import proxy_service
from services.proxy_service import ProxySettingsStore


class StubConfig:
    def __init__(self, *, runtime: dict | None = None, legacy_proxy: str = "") -> None:
        self.runtime = runtime or {}
        self.legacy_proxy = legacy_proxy

    def get_proxy_runtime_settings(self) -> dict:
        return self.runtime

    def get_proxy_settings(self) -> str:
        return self.legacy_proxy


class ProxySettingsStoreTests(unittest.TestCase):
    def _store(self, *, runtime: dict | None = None, legacy_proxy: str = "") -> ProxySettingsStore:
        return ProxySettingsStore(StubConfig(runtime=runtime, legacy_proxy=legacy_proxy))

    def test_explicit_task_proxy_overrides_runtime_single_proxy(self) -> None:
        store = self._store(runtime={
            "enabled": True,
            "egress_mode": "single_proxy",
            "proxy_url": "http://runtime.example:7897",
        })

        profile = store.get_profile(proxy="http://assigned.example:42000", upstream=True)

        self.assertEqual(profile.proxy_url, "http://assigned.example:42000")
        self.assertEqual(profile.proxy_source, "explicit")

    def test_account_proxy_overrides_explicit_task_proxy(self) -> None:
        store = self._store(runtime={
            "enabled": True,
            "egress_mode": "single_proxy",
            "proxy_url": "http://runtime.example:7897",
        })

        profile = store.get_profile(
            account={"proxy": "http://account.example:41000"},
            proxy="http://assigned.example:42000",
            upstream=True,
        )

        self.assertEqual(profile.proxy_url, "http://account.example:41000")
        self.assertEqual(profile.proxy_source, "account")

    def test_runtime_proxy_is_used_as_fallback_without_task_assignment(self) -> None:
        store = self._store(runtime={
            "enabled": True,
            "egress_mode": "single_proxy",
            "proxy_url": "http://runtime.example:7897",
        })

        profile = store.get_profile(upstream=True)

        self.assertEqual(profile.proxy_url, "http://runtime.example:7897")
        self.assertEqual(profile.proxy_source, "runtime")

    def test_resource_runtime_proxy_is_used_only_as_fallback(self) -> None:
        store = self._store(runtime={
            "enabled": True,
            "egress_mode": "single_proxy",
            "proxy_url": "http://runtime.example:7897",
            "resource_proxy_url": "http://resource.example:7898",
        })

        fallback = store.get_profile(resource=True, upstream=True)
        explicit = store.get_profile(proxy="http://assigned.example:42000", resource=True, upstream=True)

        self.assertEqual(fallback.proxy_url, "http://resource.example:7898")
        self.assertEqual(fallback.proxy_source, "runtime_resource")
        self.assertEqual(explicit.proxy_url, "http://assigned.example:42000")
        self.assertEqual(explicit.proxy_source, "explicit")

    def test_legacy_proxy_remains_last_fallback(self) -> None:
        store = self._store(legacy_proxy="http://legacy.example:7890")

        profile = store.get_profile(upstream=True)

        self.assertEqual(profile.proxy_url, "http://legacy.example:7890")
        self.assertEqual(profile.proxy_source, "global")

    def test_tls_verification_requires_explicit_runtime_opt_out(self) -> None:
        self.assertTrue(self._store().verify_tls())
        self.assertTrue(self._store(runtime={"enabled": True, "skip_ssl_verify": False}).verify_tls())
        self.assertFalse(self._store(runtime={"enabled": True, "skip_ssl_verify": True}).verify_tls())

    def _test_proxy_status(self, status_code: int) -> dict:
        session = MagicMock()
        session.get.return_value = MagicMock(status_code=status_code)
        with patch.object(proxy_service, "Session", return_value=session):
            result = proxy_service.test_proxy("http://proxy.example:8080")
        session.close.assert_called_once()
        return result

    def test_proxy_204_is_healthy(self) -> None:
        result = self._test_proxy_status(204)

        self.assertTrue(result["ok"])
        self.assertTrue(result["network_ok"])
        self.assertTrue(result["chatgpt_ok"])
        self.assertEqual(result["status_label"], "healthy")

    def test_proxy_403_is_blocked_not_healthy(self) -> None:
        result = self._test_proxy_status(403)

        self.assertFalse(result["ok"])
        self.assertTrue(result["network_ok"])
        self.assertFalse(result["region_ok"])
        self.assertEqual(result["status_label"], "blocked")

    def test_proxy_429_is_rate_limited_not_healthy(self) -> None:
        result = self._test_proxy_status(429)

        self.assertFalse(result["ok"])
        self.assertTrue(result["network_ok"])
        self.assertTrue(result["rate_limited"])
        self.assertEqual(result["status_label"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
