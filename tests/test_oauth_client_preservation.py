from __future__ import annotations

import unittest
from unittest.mock import patch

from services import account_inventory_service
from services.account_token_profile import SUB2API_OAUTH_CLIENT_ID


class OAuthClientPreservationTests(unittest.TestCase):
    def test_inventory_refresh_preserves_sub2api_oauth_client(self) -> None:
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"access_token": "at-new", "refresh_token": "rt.new", "id_token": "id-new"}

        class Session:
            def __init__(self):
                self.payload = None

            def post(self, _url, **kwargs):
                self.payload = kwargs.get("json")
                return Response()

        session = Session()
        with patch.object(account_inventory_service, "_session", return_value=session):
            result = account_inventory_service._refresh_platform_tokens(
                "rt.old",
                client_id=SUB2API_OAUTH_CLIENT_ID,
            )

        self.assertEqual(session.payload["client_id"], SUB2API_OAUTH_CLIENT_ID)
        self.assertEqual(session.payload["redirect_uri"], "http://localhost:1455/auth/callback")
        self.assertEqual(result["client_id"], SUB2API_OAUTH_CLIENT_ID)


if __name__ == "__main__":
    unittest.main()
