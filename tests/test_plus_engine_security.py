from __future__ import annotations

import unittest

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
