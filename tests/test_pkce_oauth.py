"""
Pure Official OAuth 2.0 PKCE Test Suite (RFC 7636)
==================================================
Tests:
1. RFC 7636 S256 verifier, SHA-256 challenge, and UUID generation
2. Official Cursor loginDeepControl URL construction
3. CursorOAuthPKCEClient.create_login_session()
4. CursorOAuthPKCEClient.poll_login_once() (200 completed / 404 pending / 403 rejected)
5. CursorOAuthPKCEClient.poll_until_complete() with stop_event & retries
6. End-to-end interactive OAuth API routes (/api/auth/pkce/start, status, cancel, sessions)
"""

import base64
import hashlib
import threading
import unittest
from unittest.mock import MagicMock, patch

import server
from pkce_auth import (
    CursorOAuthPKCEClient,
    base64url_encode,
    build_oauth_login_url,
    generate_pkce_credentials,
    is_valid_jwt_structure,
)


class TestPureOAuthPKCE(unittest.TestCase):
    def test_01_base64url_encode_no_padding(self):
        encoded = base64url_encode(b"\xfb\xff\xfe\x00\x01")
        self.assertNotIn("=", encoded)
        self.assertNotIn("+", encoded)
        self.assertNotIn("/", encoded)

    def test_02_pkce_credentials_rfc7636_s256(self):
        creds = generate_pkce_credentials()
        self.assertIn("verifier", creds)
        self.assertIn("challenge", creds)
        self.assertIn("uuid", creds)
        self.assertEqual(len(creds["verifier"]), 43)
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(creds["verifier"].encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        self.assertEqual(creds["challenge"], expected)

        # Uniqueness across calls
        creds2 = generate_pkce_credentials()
        self.assertNotEqual(creds["verifier"], creds2["verifier"])
        self.assertNotEqual(creds["uuid"], creds2["uuid"])

    def test_03_build_oauth_login_url(self):
        url = build_oauth_login_url("CHAL_123", "UUID_456")
        self.assertEqual(
            url,
            "https://cursor.com/loginDeepControl?challenge=CHAL_123&uuid=UUID_456&mode=login",
        )

    def test_04_create_login_session(self):
        client = CursorOAuthPKCEClient()
        sess = client.create_login_session()
        self.assertIn("login_url", sess)
        self.assertIn(sess["challenge"], sess["login_url"])
        self.assertIn(sess["uuid"], sess["login_url"])

    def test_05_poll_login_once_states(self):
        client = CursorOAuthPKCEClient()

        with patch.object(client.session, "get") as mock_get:
            r404 = MagicMock(status_code=404, text="")
            mock_get.return_value = r404
            self.assertEqual(client.poll_login_once("u1", "v1")["status"], "pending")

            r403 = MagicMock(status_code=403, text="Forbidden")
            mock_get.return_value = r403
            self.assertEqual(client.poll_login_once("u1", "v1")["status"], "rejected")

            r200 = MagicMock(status_code=200, text='{"accessToken":"tok_a","refreshToken":"tok_r"}')
            r200.json.return_value = {"accessToken": "tok_a", "refreshToken": "tok_r"}
            mock_get.return_value = r200
            done = client.poll_login_once("u1", "v1")
            self.assertEqual(done["status"], "completed")
            self.assertEqual(done["accessToken"], "tok_a")
            self.assertEqual(done["refreshToken"], "tok_r")

    def test_06_poll_until_complete_succeeds_after_retries(self):
        client = CursorOAuthPKCEClient()
        r404 = MagicMock(status_code=404, text="")
        r200 = MagicMock(status_code=200, text='{"accessToken":"sess_tok","refreshToken":"sess_ref"}')
        r200.json.return_value = {"accessToken": "sess_tok", "refreshToken": "sess_ref"}

        with patch.object(client.session, "get", side_effect=[r404, r404, r200]) as mock_get:
            res = client.poll_until_complete("u2", "v2", timeout_sec=5.0, poll_interval=0.01)
            self.assertIsNotNone(res)
            self.assertEqual(res["accessToken"], "sess_tok")
            self.assertEqual(res["refreshToken"], "sess_ref")
            self.assertEqual(res["authMethod"], "oauth_pkce")
            self.assertEqual(mock_get.call_count, 3)

    def test_07_poll_until_complete_honors_stop_event(self):
        client = CursorOAuthPKCEClient()
        stop = threading.Event()
        stop.set()
        res = client.poll_until_complete("u3", "v3", timeout_sec=10.0, stop_event=stop)
        self.assertIsNone(res)

    def test_08_jwt_structure_validator(self):
        self.assertTrue(is_valid_jwt_structure("eyJhbGciOiJIUzI1NiJ9.eyJ0eXBlIjoic2Vzc2lvbiJ9.signature1234"))
        self.assertFalse(is_valid_jwt_structure("short.bad"))
        self.assertFalse(is_valid_jwt_structure(None))

    def test_09_oauth_pkce_api_endpoints(self):
        api = server.app.test_client()
        with patch("webbrowser.open", return_value=True), \
             patch("server._pkce_poll_worker", return_value=None):
            res = api.post("/api/auth/pkce/start", json={"open_browser": False})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            sid = data["session_id"]
            self.assertIn("https://cursor.com/loginDeepControl?", data["login_url"])

            lst = api.get("/api/auth/pkce/sessions").get_json()
            self.assertTrue(lst["success"])
            self.assertTrue(any(s["session_id"] == sid for s in lst["sessions"]))

            st = api.get(f"/api/auth/pkce/status/{sid}").get_json()
            self.assertEqual(st["status"], "waiting")

            cn = api.post(f"/api/auth/pkce/cancel/{sid}").get_json()
            self.assertEqual(cn["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
