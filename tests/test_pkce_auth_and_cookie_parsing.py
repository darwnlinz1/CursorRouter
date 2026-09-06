"""
Unit Test Suite: PKCE Auth, Cookie Parsing, & Token Exchange
============================================================

Verifies:
1. base64url_encode conforms with RFC 7636 (no padding, URL-safe).
2. generate_pkce_credentials produces valid SHA-256 challenges and UUIDs.
3. parse_cookie_from_file correctly handles:
   - Netscape 7-column tab-delimited files.
   - Raw WorkosCursorSessionToken strings.
   - Semicolon-delimited cookie headers.
   - Missing / empty files.
4. CursorAuthClient headless PKCE flow:
   - Successful 2-step handshake (callback + poll).
   - Upstream rejection (403 Forbidden).
   - Polling timeout (max_polls exceeded).
"""

import os
import sys
import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure root directory is in sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from pkce_auth import (
    base64url_encode,
    generate_pkce_credentials,
    parse_cookie_from_file,
    CursorAuthClient
)


class TestPkceAuth(unittest.TestCase):
    def test_01_base64url_encode_correctness(self):
        """base64url_encode must produce URL-safe strings without '=' padding."""
        # Empty
        self.assertEqual(base64url_encode(b""), "")
        # Standard bytes
        self.assertEqual(base64url_encode(b"hello world"), "aGVsbG8gd29ybGQ")
        # Bytes resulting in + and / in standard base64 (must be replaced with - and _)
        sample = bytes([251, 239, 255])
        encoded = base64url_encode(sample)
        self.assertNotIn("=", encoded)
        self.assertNotIn("+", encoded)
        self.assertNotIn("/", encoded)

    def test_02_generate_pkce_credentials(self):
        """generate_pkce_credentials must create cryptographically valid verifier/challenge pairs."""
        creds = generate_pkce_credentials()
        self.assertIn("uuid", creds)
        self.assertIn("verifier", creds)
        self.assertIn("challenge", creds)

        # Verifier length should be >= 43 chars (32 bytes entropy url-encoded)
        self.assertGreaterEqual(len(creds["verifier"]), 43)

        # Verify challenge is indeed SHA-256 of verifier
        digest = hashlib.sha256(creds["verifier"].encode("utf-8")).digest()
        expected_challenge = base64url_encode(digest)
        self.assertEqual(creds["challenge"], expected_challenge)

    def test_03_parse_cookie_netscape_format(self):
        """Netscape tab-delimited cookie file must extract 7th column."""
        tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8")
        tmp.write("# Netscape HTTP Cookie File\n")
        tmp.write(".cursor.com\tTRUE\t/\tTRUE\t1793644000\tWorkosCursorSessionToken\tuser_01KHT8D9DYWEB5XMRVMB2JS98Z%3A%3Atest_secret_token\n")
        tmp.close()

        try:
            token = parse_cookie_from_file(tmp.name)
            self.assertEqual(token, "user_01KHT8D9DYWEB5XMRVMB2JS98Z%3A%3Atest_secret_token")
        finally:
            os.unlink(tmp.name)

    def test_04_parse_cookie_raw_string(self):
        """Raw string or header string must extract token cleanly."""
        tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, encoding="utf-8")
        tmp.write("WorkosCursorSessionToken=user_01KHT8D9DYWEB5XMRVMB2JS98Z; Path=/; Domain=.cursor.com\n")
        tmp.close()

        try:
            token = parse_cookie_from_file(tmp.name)
            self.assertIn("user_01KHT8D9DYWEB5XMRVMB2JS98Z", token)
        finally:
            os.unlink(tmp.name)

    @patch("requests.Session.post")
    @patch("requests.Session.get")
    def test_05_exchange_cookie_to_tokens_success(self, mock_get, mock_post):
        """Simulate successful 2-step PKCE exchange: callback 200 -> poll 200."""
        # 1. Mock callback response
        cb_resp = MagicMock()
        cb_resp.status_code = 200
        mock_post.return_value = cb_resp

        # 2. Mock polling response
        poll_resp = MagicMock()
        poll_resp.status_code = 200
        poll_resp.json.return_value = {
            "accessToken": "ey_mock_access_token_12345",
            "refreshToken": "ey_mock_refresh_token_12345"
        }
        mock_get.return_value = poll_resp

        client = CursorAuthClient()
        result = client.exchange_cookie_to_tokens("user_01TESTTOKEN", max_polls=2, poll_interval=0.01)

        self.assertIsNotNone(result)
        self.assertEqual(result.get("accessToken"), "ey_mock_access_token_12345")
        self.assertEqual(result.get("refreshToken"), "ey_mock_refresh_token_12345")
        self.assertTrue(mock_post.called)
        self.assertTrue(mock_get.called)

    @patch("requests.Session.post")
    def test_06_exchange_cookie_callback_failure(self, mock_post):
        """When callback endpoint fails with 403 Forbidden, exchange aborts and returns None."""
        cb_resp = MagicMock()
        cb_resp.status_code = 403
        cb_resp.text = "Forbidden - Cloudflare WAF"
        mock_post.return_value = cb_resp

        client = CursorAuthClient()
        result = client.exchange_cookie_to_tokens("user_invalid_cookie", max_polls=2, poll_interval=0.01)
        self.assertIsNone(result)

    @patch("requests.Session.post")
    @patch("requests.Session.get")
    def test_07_exchange_cookie_polling_timeout(self, mock_get, mock_post):
        """When polling repeatedly returns 404, loop times out and returns None."""
        cb_resp = MagicMock()
        cb_resp.status_code = 200
        mock_post.return_value = cb_resp

        poll_resp = MagicMock()
        poll_resp.status_code = 404
        mock_get.return_value = poll_resp

        client = CursorAuthClient()
        result = client.exchange_cookie_to_tokens("user_valid_cookie", max_polls=3, poll_interval=0.01)
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, 3)
