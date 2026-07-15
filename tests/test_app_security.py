from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from api.app import create_app


class AppSecurityTests(unittest.TestCase):
    def test_cors_rejects_non_local_origins(self) -> None:
        client = TestClient(create_app())
        response = client.options(
            "/version",
            headers={
                "origin": "https://example.com",
                "access-control-request-method": "GET",
            },
        )

        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_cors_allows_local_development_origins(self) -> None:
        client = TestClient(create_app())
        origin = "http://127.0.0.1:3000"
        response = client.options(
            "/version",
            headers={
                "origin": origin,
                "access-control-request-method": "GET",
            },
        )

        self.assertEqual(response.headers.get("access-control-allow-origin"), origin)


if __name__ == "__main__":
    unittest.main()
