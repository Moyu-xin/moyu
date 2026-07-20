from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services import proxy_chain_service


class ProxyChainManagerTests(unittest.TestCase):
    def test_shared_chain_is_destroyed_after_last_lease_is_released(self) -> None:
        manager = proxy_chain_service.ProxyChainManager()
        server = MagicMock()
        server.server_address = ("127.0.0.1", 45000)
        thread = MagicMock()

        with (
            patch.object(proxy_chain_service, "_ChainServer", return_value=server) as server_factory,
            patch.object(proxy_chain_service.threading, "Thread", return_value=thread),
        ):
            first = manager.proxy_for(
                "http://user:pass@residential.example:10000",
                "http://parent.example:7890",
            )
            second = manager.proxy_for(
                "http://user:pass@residential.example:10000",
                "http://parent.example:7890",
            )

            self.assertEqual(first, "http://127.0.0.1:45000")
            self.assertEqual(second, first)
            self.assertEqual(manager.snapshot()["items"][0]["leases"], 2)
            server_factory.assert_called_once()
            thread.start.assert_called_once()

            manager.release(first)
            self.assertEqual(manager.snapshot()["total"], 1)
            self.assertEqual(manager.snapshot()["items"][0]["leases"], 1)

            manager.release(second)
            self.assertEqual(manager.snapshot()["total"], 0)
            server.shutdown.assert_called_once()
            server.server_close.assert_called_once()
            thread.join.assert_called_once_with(timeout=2)


if __name__ == "__main__":
    unittest.main()
