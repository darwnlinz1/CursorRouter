"""
Unit Test Suite: Cursor Storage Manager & SQLite state.vscdb
===========================================================

Verifies:
1. decode_jwt_payload decodes valid JWTs with and without padding.
2. decode_jwt_payload gracefully handles empty, invalid, and non-base64 tokens.
3. CursorStorageManager ItemTable creation, injection of full credentials:
   - cursorAuth/accessToken
   - cursorAuth/refreshToken
   - cursorAuth/cachedEmail
   - cursorAuth/cachedDisplayName
   - cursorAuth/stripeMembershipType
   - adminSettings.cachedAuthId
4. get_active_account accurately retrieves stored identity, calculates expiration,
   and handles fallback logic.
5. fetch_profile_from_api handles successful 200 responses, 429 rate limits, and network errors.
"""

import os
import sys
import json
import base64
import time
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure root directory is in sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from cursor_storage import CursorStorageManager, decode_jwt_payload


def create_fake_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8")).decode("utf-8").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    sig = "fake_sig_12345"
    return f"{header}.{body}.{sig}"


class TestCursorStorageManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "state.vscdb")
        self.mgr = CursorStorageManager(db_path=self.db_path)

    def test_01_decode_jwt_payload_valid(self):
        """decode_jwt_payload must correctly parse standard JWT payload without external libraries."""
        fake_payload = {
            "sub": "user_01TEST",
            "email": "test@cursor.com",
            "exp": 1793644000
        }
        token = create_fake_jwt(fake_payload)
        decoded = decode_jwt_payload(token)

        self.assertEqual(decoded.get("email"), "test@cursor.com")
        self.assertEqual(decoded.get("sub"), "user_01TEST")
        self.assertEqual(decoded.get("exp"), 1793644000)

    def test_02_decode_jwt_payload_invalid(self):
        """decode_jwt_payload on garbage or empty string must return empty dict without crashing."""
        self.assertEqual(decode_jwt_payload(""), {})
        self.assertEqual(decode_jwt_payload(None), {})
        self.assertEqual(decode_jwt_payload("not_a_jwt"), {})
        self.assertEqual(decode_jwt_payload("part1.not_base64.part3"), {})

    def test_03_inject_full_profile_and_readback(self):
        """inject_full_profile must create ItemTable and persist credentials reliably."""
        exp_time = int(time.time()) + 3600
        fake_token = create_fake_jwt({"sub": "user_999", "email": "dev@cursor.sh", "exp": exp_time})

        profile = {
            "email": "dev@cursor.sh",
            "displayName": "Lead Dev",
            "authId": "google-oauth2|user_999",
            "signUpType": "Google",
            "membershipType": "free"
        }

        ok = self.mgr.inject_full_profile(
            access_token=fake_token,
            refresh_token="ref_token_abc_123",
            profile=profile
        )
        self.assertTrue(ok, "inject_full_profile must return True")

        # Read back via get_active_account()
        active = self.mgr.get_active_account()
        self.assertEqual(active.get("email"), "dev@cursor.sh")
        self.assertEqual(active.get("displayName"), "Lead Dev")
        self.assertEqual(active.get("authId"), "google-oauth2|user_999")
        self.assertEqual(active.get("access_token"), fake_token)
        self.assertEqual(active.get("refresh_token"), "ref_token_abc_123")
        self.assertTrue(active.get("has_token"))
        self.assertFalse(active.get("is_expired"))
        self.assertEqual(active.get("token_exp"), exp_time)

    def test_04_detects_expired_jwt(self):
        """When JWT exp is in the past, get_active_account must flag is_expired=True."""
        past_exp = int(time.time()) - 3600  # 1 hour ago
        expired_token = create_fake_jwt({"sub": "user_old", "email": "old@cursor.com", "exp": past_exp})

        self.mgr.inject_full_profile(
            access_token=expired_token,
            refresh_token="ref_token_old",
            profile={"email": "old@cursor.com"}
        )

        active = self.mgr.get_active_account()
        self.assertTrue(active.get("is_expired"), "Token het han phai co is_expired=True!")

    @patch("requests.post")
    def test_05_fetch_profile_from_api_success(self, mock_post):
        """fetch_profile_from_api must query GetMe and GetCurrentPeriodUsage and return metrics."""
        def _side_effect(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "GetMe" in url:
                resp.json.return_value = {
                    "email": "api_user@test.com",
                    "authId": "google-oauth2|user_999",
                    "firstName": "API",
                    "lastName": "User"
                }
            elif "GetEmail" in url:
                resp.json.return_value = {"signUpType": "AUTH_GOOGLE"}
            elif "GetCurrentPeriodUsage" in url:
                resp.json.return_value = {
                    "planUsage": {
                        "totalSpend": 45.0,
                        "totalPercentUsed": 22.5,
                        "autoPercentUsed": 22.5
                    },
                    "displayThreshold": 200,
                    "displayMessage": "Good standing"
                }
            return resp

        mock_post.side_effect = _side_effect

        profile = self.mgr.fetch_profile_from_api("tok_mock_api_123")
        self.assertIsNotNone(profile)
        self.assertTrue(profile.get("usage_fetched"))
        self.assertEqual(profile.get("email"), "api_user@test.com")
        self.assertEqual(profile.get("totalSpend"), 45.0)
        self.assertEqual(profile.get("usagePercent"), 22.5)

    @patch("requests.post")
    def test_06_fetch_profile_from_api_429_locked(self, mock_post):
        """fetch_profile_from_api encountering 429 must return dict with locked status."""
        def _side_effect(url, **kwargs):
            resp = MagicMock()
            if "GetCurrentPeriodUsage" in url:
                resp.status_code = 429
                resp.headers = {"grpc-status": "8"}
                resp.text = "Too Many Requests"
            else:
                resp.status_code = 200
                resp.json.return_value = {"email": "rate_limited@test.com"}
            return resp

        mock_post.side_effect = _side_effect

        profile = self.mgr.fetch_profile_from_api("tok_mock_api_rate_limited")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.get("http_status"), 429)
        self.assertFalse(profile.get("usage_fetched"))
