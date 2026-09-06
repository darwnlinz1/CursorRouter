"""
Test Suite: Chat Lockout Account Switching and Prompt Resend Device Isolation
==============================================================================
Verifies all requirements from user:
1. Quota > 50% does NOT mean chat is blocked; account can still be used up to 100% in slow pool.
2. Account switching ONLY occurs when account is actually blocked from chat (HTTP 429/402/403/401,
   Connect-RPC resource_exhausted/failed_precondition, HARD_BLOCK, or 100% quota).
3. Auto-switch selects next best account (< 100% usage), including HIGH_USAGE (50%-99%) accounts.
4. When user enables 'always' send continue prompt setting, prompt is dispatched upon account switch.
5. Prompt dispatch strictly isolates to genuine Cursor window and NEVER pastes into user's other applications.
6. User clipboard is never polluted with 'Tiếp tục'.
"""

import os
import sys
import json
import time
import sqlite3
import tempfile
import shutil
import unittest
from unittest.mock import patch, MagicMock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in [ROOT_DIR, SRC_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from chat_lock_detector import is_chat_locked, make_connect_frame, FRAME_FLAG_TRAILER
from account_pool import AccountPoolManager
import cursor_reloader
from smart_task_filter import SmartTaskCompletionFilter, TaskStateTracker


class TestChatLockoutRules(unittest.TestCase):
    def test_quota_over_50_is_not_locked(self):
        """Account with 51%-99% usage is in slow pool and NOT locked from chat."""
        for pct in (51.0, 75.0, 90.0, 99.0):
            profile = {
                "email": f"user{int(pct)}@test.com",
                "usagePercent": pct,
                "totalSpend": pct * 2.0,
                "autoPercentUsed": 100.0,
                "displayThreshold": 200.0
            }
            locked, reason = is_chat_locked(usage_data=profile)
            self.assertFalse(locked, f"Account at {pct}% usage must NOT be marked locked (slow pool is active)")

    def test_slow_pool_policy_status_is_not_locked(self):
        """SLOW_POOL policy status is the 5-hour soft window and must NOT trigger chat lockout."""
        body = json.dumps({
            "usageLimitPolicyStatus": {
                "stage": "SLOW_POOL",
                "isInSlowPool": True,
                "trayLabel": "Switched to standard pool"
            }
        })
        locked, reason = is_chat_locked(status_code=200, body_or_chunks=body)
        self.assertFalse(locked, "SLOW_POOL stage must NOT trigger chat lock")

    def test_actual_chat_lockouts_are_reliably_detected(self):
        """Verify all true chat lockout scenarios trigger lockout detection."""
        # 1. HTTP Status Lockouts
        for code in (429, 402, 403, 401):
            locked, reason = is_chat_locked(status_code=code)
            self.assertTrue(locked, f"HTTP {code} must trigger chat lock")

        # 2. Connect-RPC Trailer / Error Code
        trailer_err = {"error": {"code": "resource_exhausted", "message": "Rate limit exceeded"}}
        frame = make_connect_frame(trailer_err, FRAME_FLAG_TRAILER)
        locked, reason = is_chat_locked(status_code=200, body_or_chunks=frame)
        self.assertTrue(locked, "resource_exhausted Connect-RPC error must trigger chat lock")

        # 3. Policy HARD_BLOCK
        hard_block_body = json.dumps({
            "usageLimitPolicyStatus": {
                "stage": "HARD_BLOCK",
                "trayLabel": "Account is blocked"
            }
        })
        locked, reason = is_chat_locked(status_code=200, body_or_chunks=hard_block_body)
        self.assertTrue(locked, "HARD_BLOCK stage must trigger chat lock")

        # 4. Total Quota Exhaustion (>= 100.0%)
        exh_profile = {
            "email": "exh@test.com",
            "usagePercent": 100.0,
            "totalSpend": 200.0,
            "displayThreshold": 200.0
        }
        locked, reason = is_chat_locked(usage_data=exh_profile)
        self.assertTrue(locked, "100% total quota must trigger chat lock")


class TestAccountSwitchingSelection(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_cookies = os.path.join(self.test_dir, "Cookies")
        os.makedirs(self.test_cookies, exist_ok=True)
        self.test_db = os.path.join(self.test_dir, "cursor_accounts.db")

        import account_pool
        self.orig_db = account_pool.DB_FILE
        account_pool.DB_FILE = self.test_db

        # Initialize schema properly via AccountPoolManager
        self.mgr = AccountPoolManager(cookies_dir=self.test_cookies)

    def tearDown(self):
        import account_pool
        account_pool.DB_FILE = self.orig_db
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_switch_selects_high_usage_account_before_exhausted(self):
        """Account pool should pick a HIGH_USAGE account (e.g. 60%) when ready accounts are not available, but skip EXHAUSTED."""
        now = int(time.time())
        con = sqlite3.connect(self.test_db)
        con.execute("INSERT INTO accounts (file_name, email, access_token, usage_percent, status, last_checked) VALUES ('acc1.txt', 'locked@test.com', 'tok_locked_12345678901234567890', 100.0, 'EXHAUSTED', ?)", (now,))
        con.execute("INSERT INTO accounts (file_name, email, access_token, usage_percent, status, last_checked) VALUES ('acc2.txt', 'slow_pool@test.com', 'tok_slow_12345678901234567890', 60.0, 'HIGH_USAGE', ?)", (now,))
        con.execute("INSERT INTO accounts (file_name, email, access_token, usage_percent, status, last_checked) VALUES ('acc3.txt', 'dead@test.com', 'tok_dead_12345678901234567890', 100.0, 'EXHAUSTED', ?)", (now,))
        con.commit()
        con.close()

        with patch.object(self.mgr, "switch_to_account", return_value=True):
            best = self.mgr.auto_switch_best_account(notify=False, prev_email="locked@test.com")
            self.assertIsNotNone(best)
            self.assertEqual(best["email"], "slow_pool@test.com", "Should select 60% HIGH_USAGE account since it is usable up to 100%")


class TestPromptDispatchIsolation(unittest.TestCase):
    def test_dispatch_aborts_immediately_if_user_in_other_window(self):
        """If user is active in Chrome, Terminal, or another window, send_continue_prompt cancels dispatch."""
        fake_cursor_hwnd = 12345
        chrome_browser_hwnd = 99999

        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True
        # Foreground is Chrome, NOT Cursor
        mock_u32.GetForegroundWindow.return_value = chrome_browser_hwnd

        with patch("cursor_reloader.find_cursor_window", return_value=fake_cursor_hwnd), \
             patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == fake_cursor_hwnd), \
             patch("cursor_reloader.force_bring_to_front", return_value=False), \
             patch("cursor_reloader._set_clipboard_text") as mock_set_clip:
            
            res = cursor_reloader.send_continue_prompt("Tiếp tục")
            self.assertFalse(res["success"])
            self.assertIn("Khong the focus", res["error"])
            mock_u32.keybd_event.assert_not_called()
            mock_set_clip.assert_not_called()

    def test_always_mode_triggers_continuation(self):
        """When auto_resend_action is 'always', continuation is permitted."""
        TaskStateTracker().reset()
        filter_inst = SmartTaskCompletionFilter()
        should_cont, reason = filter_inst.should_auto_continue(auto_continue=None, auto_resend_cfg="always")
        self.assertTrue(should_cont)
        self.assertIn("always", reason.lower())


if __name__ == "__main__":
    unittest.main()
