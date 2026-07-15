from __future__ import annotations

import unittest
from unittest.mock import patch

from services import task_proxy_service


class TaskProxyServiceTests(unittest.TestCase):
    def test_local_targets_bypass_proxy_pool(self) -> None:
        with patch.object(task_proxy_service.proxy_pool_store, "assign_proxy") as assign:
            result = task_proxy_service.assign_task_proxy(
                1,
                purpose="sub2api",
                target_url="http://127.0.0.1:8080/api/v1",
                account={"proxy": "http://account-proxy.invalid:8080"},
            )

        assign.assert_not_called()
        self.assertEqual(result["source"], "local_direct")
        self.assertEqual(result["proxy"], "")

    def test_remote_targets_use_shared_assignment_with_account_context(self) -> None:
        account = {"email": "user@example.com", "proxy": "http://account-proxy.invalid:8080"}
        expected = {"proxy": account["proxy"], "source": "account"}
        with patch.object(task_proxy_service.proxy_pool_store, "assign_proxy", return_value=expected) as assign:
            result = task_proxy_service.assign_task_proxy(
                2,
                purpose="cpa_import",
                target_url="https://warehouse.example.com",
                account=account,
                total_threads=4,
                slot_index=1,
            )

        self.assertEqual(result, expected)
        assign.assert_called_once_with(
            2,
            fallback="",
            total_threads=4,
            slot_index=1,
            purpose="cpa_import",
            account=account,
            validate_node=False,
        )

    def test_private_network_targets_are_direct(self) -> None:
        for url in ("http://localhost:8000", "http://192.168.1.10:8080", "http://10.0.0.3"):
            self.assertTrue(task_proxy_service.is_local_target(url), url)
        self.assertFalse(task_proxy_service.is_local_target("https://example.com"))

    def test_local_session_kwargs_cannot_inherit_account_or_runtime_proxy(self) -> None:
        with patch.object(task_proxy_service.proxy_settings, "build_session_kwargs") as build:
            kwargs, assignment = task_proxy_service.build_task_session_kwargs(
                1,
                purpose="sub2api_admin",
                target_url="http://127.0.0.1:8080/api/v1",
                account={"proxy": "http://account-proxy.invalid:8080"},
                impersonate="chrome",
                verify=False,
            )

        build.assert_not_called()
        self.assertEqual(assignment["source"], "local_direct")
        self.assertEqual(kwargs, {"impersonate": "chrome", "verify": False})
        self.assertNotIn("proxy", kwargs)

    def test_local_session_verifies_tls_by_default(self) -> None:
        kwargs, assignment = task_proxy_service.build_task_session_kwargs(
            1,
            purpose="sub2api_admin",
            target_url="http://127.0.0.1:8080/api/v1",
        )

        self.assertEqual(assignment["source"], "local_direct")
        self.assertTrue(kwargs["verify"])


if __name__ == "__main__":
    unittest.main()
