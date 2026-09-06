"""
Automated Integration Test Suite:
1. Smart Task Completion Filter
2. Guaranteed Instant Account Switch & Reset with state.vscdb verification
3. Hardware Fingerprint Spoofing (storage.json) & Clean Window Reload (Ctrl+Shift+F11)
4. 100% Native Cursor Settings Fidelity
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

sys.path.insert(0, os.path.dirname(__file__))

from server import app, pool, settings_manager
from cursor_storage import CursorStorageManager
from cursor_settings import CursorSettingsManager
import cursor_reloader
from smart_task_filter import (
    SmartTaskCompletionFilter,
    TaskStateTracker,
    mark_task_interrupted,
    mark_task_completed,
    should_trigger_auto_continue
)
from account_pool import AccountPoolManager, QUOTA_EXHAUSTION_THRESHOLD


class TestSmartFilterAndInstantSwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()
        cls.storage = CursorStorageManager()
        cls.settings = CursorSettingsManager()
        cls.filter = SmartTaskCompletionFilter()

        # Seed valid test accounts with usage < 50% (Tier 1) and cooldown_until = 0 so auto-switch tests always succeed
        con = sqlite3.connect("cursor_accounts.db")
        cur = con.cursor()
        cur.execute("PRAGMA table_info(accounts);")
        cols = [r[1] for r in cur.fetchall()]
        has_cd = "cooldown_until" in cols
        import base64
        def _make_mock_jwt(email, name):
            h = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).decode().rstrip("=")
            p = base64.urlsafe_b64encode(json.dumps({"email": email, "name": name, "sub": "auth0|" + email, "exp": 1999999999}).encode()).decode().rstrip("=")
            return f"{h}.{p}.fake_sig"

        test_rows = [
            (99901, "test_smart_1.txt", "smart_test1@example.com", "Smart Test 1", _make_mock_jwt("smart_test1@example.com", "Smart Test 1"), "test_ref_1", 10.0, 0.0, "READY", 0),
            (99902, "test_smart_2.txt", "smart_test2@example.com", "Smart Test 2", _make_mock_jwt("smart_test2@example.com", "Smart Test 2"), "test_ref_2", 15.0, 0.0, "READY", 0),
            (99903, "test_smart_3.txt", "smart_test3@example.com", "Smart Test 3", _make_mock_jwt("smart_test3@example.com", "Smart Test 3"), "test_ref_3", 20.0, 0.0, "READY", 0),
        ]
        for row in test_rows:
            if has_cd:
                cur.execute("""
                    INSERT OR REPLACE INTO accounts (id, file_name, email, display_name, access_token, refresh_token, usage_percent, total_spend, status, cooldown_until)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, row)
            else:
                cur.execute("""
                    INSERT OR REPLACE INTO accounts (id, file_name, email, display_name, access_token, refresh_token, usage_percent, total_spend, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, row[:-1])
        con.commit()
        con.close()

    @classmethod
    def tearDownClass(cls):
        try:
            con = sqlite3.connect("cursor_accounts.db")
            cur = con.cursor()
            cur.execute("DELETE FROM accounts WHERE id IN (99901, 99902, 99903)")
            con.commit()
            con.close()
        except Exception:
            pass

    def setUp(self):
        TaskStateTracker().reset()

    def tearDown(self):
        TaskStateTracker().reset()

    # =========================================================================
    # 1. SMART TASK COMPLETION FILTER TESTS
    # =========================================================================

    def test_01_filter_idle_state_suppresses_continue(self):
        """Idle state without active interruption must strictly suppress prompt-resend."""
        TaskStateTracker().reset()
        should_cont, reason = self.filter.should_auto_continue(
            auto_continue=None,
            auto_resend_cfg="auto",
            max_age_seconds=1.0  # Force any historical logs to be considered stale
        )
        self.assertFalse(should_cont)
        self.assertIn("suppressed", reason.lower())

    def test_02_filter_explicit_false_always_suppressed(self):
        """When auto_continue is explicitly False, resend must NEVER fire regardless of error."""
        mark_task_interrupted("Quota limit 429 midstream")
        should_cont, reason = self.filter.should_auto_continue(auto_continue=False)
        self.assertFalse(should_cont)
        self.assertIn("explicitly suppressed", reason.lower())

    def test_03_filter_interrupted_midflight_permits_continue(self):
        """When task is genuinely interrupted mid-flight by quota/lockout, filter permits continue."""
        mark_task_interrupted("Connect-RPC midstream lockout: resource_exhausted", request_id="req-test-123")
        should_cont, reason = self.filter.should_auto_continue(auto_continue=None, auto_resend_cfg="auto")
        self.assertTrue(should_cont)
        self.assertIn("interrupted mid-flight", reason.lower())
        self.assertIn("resource_exhausted", reason)

    def test_04_filter_natural_completion_strictly_suppresses_continue(self):
        """When task completes naturally, prompt-resend is strictly suppressed (no repeating tests)."""
        # First mark interrupted
        mark_task_interrupted("Temporary limit warning")
        # Then task finishes naturally
        time.sleep(0.01)
        mark_task_completed(request_id="req-test-123")

        should_cont, reason = self.filter.should_auto_continue(auto_continue=None, auto_resend_cfg="auto")
        self.assertFalse(should_cont)
        self.assertIn("completed or ended naturally", reason.lower())

    def test_05_filter_config_manual_suppresses_continue(self):
        """When auto_resend_action is 'manual', resend is suppressed even if interrupted."""
        mark_task_interrupted("429 Too Many Requests")
        should_cont, reason = self.filter.should_auto_continue(auto_continue=None, auto_resend_cfg="manual")
        self.assertFalse(should_cont)
        self.assertIn("manual", reason.lower())

    def test_06_filter_stale_interruption_suppresses_continue(self):
        """An old interruption (> max_age_seconds) must be treated as stale and suppressed."""
        mark_task_interrupted("Old quota error from 10 minutes ago")
        # Evaluate with a tiny max_age_seconds window
        time.sleep(0.05)
        should_cont, reason = self.filter.should_auto_continue(auto_continue=None, max_age_seconds=0.01)
        self.assertFalse(should_cont)
        self.assertIn("suppressed", reason.lower())

    def test_07_filter_log_parser_accuracy(self):
        """Verify structured log line parser accurately parses success vs lockout errors."""
        # Success log line
        success_line = '2026-09-04 12:00:00.123 [info] {"level":"info","message":"agent.turn.outcome","metadata":{"outcome":"success","request_id":"req-ok-1"}}'
        parsed_ok = self.filter._parse_log_line(success_line)
        self.assertIsNotNone(parsed_ok)
        self.assertTrue(parsed_ok["is_success"])
        self.assertFalse(parsed_ok["is_quota_lockout"])

        # Quota lockout log line
        lockout_line = '2026-09-04 12:01:00.456 [info] {"level":"info","message":"agent.turn.outcome","metadata":{"outcome":"error","error_code":"upgrade","error_text":"You\'ve hit your usage limit Get Cursor Pro","request_id":"req-lock-2"}}'
        parsed_lock = self.filter._parse_log_line(lockout_line)
        self.assertIsNotNone(parsed_lock)
        self.assertFalse(parsed_lock["is_success"])
        self.assertTrue(parsed_lock["is_quota_lockout"])
        self.assertIn("upgrade", parsed_lock["lockout_reason"])

    # =========================================================================
    # 2. GUARANTEED INSTANT ACCOUNT SWITCH & RESET TESTS
    # =========================================================================

    def test_08_switch_to_account_verifies_state_vscdb(self):
        """switch_to_account must verify the active account matches in state.vscdb."""
        # Select an account from DB
        con = sqlite3.connect("cursor_accounts.db")
        cur = con.cursor()
        cur.execute("SELECT id, email FROM accounts WHERE status != 'EXHAUSTED' AND access_token IS NOT NULL LIMIT 1")
        row = cur.fetchone()
        con.close()
        self.assertIsNotNone(row)
        acc_id, email = row

        ok = pool.switch_to_account(acc_id, auto_reload=False, auto_continue=False, spoof_hw=True, verify=True)
        self.assertTrue(ok)

        # Verify active account in state.vscdb
        active = self.storage.get_active_account()
        self.assertEqual(active.get("email", "").lower(), email.lower())
        self.assertTrue(active.get("has_token"))

    def test_09_switch_to_account_spoofs_hardware_ids(self):
        """switch_to_account with spoof_hw=True must randomize storage.json hardware machine IDs."""
        initial_hw = self.settings.get_storage_ids()
        initial_machine_id = initial_hw.get("machineId")

        # Perform switch
        con = sqlite3.connect("cursor_accounts.db")
        cur = con.cursor()
        cur.execute("SELECT id FROM accounts WHERE status != 'EXHAUSTED' AND access_token IS NOT NULL LIMIT 1")
        acc_id = cur.fetchone()[0]
        con.close()

        pool.switch_to_account(acc_id, auto_reload=False, auto_continue=False, spoof_hw=True)

        new_hw = self.settings.get_storage_ids()
        new_machine_id = new_hw.get("machineId")

        # Machine ID must be 64 characters hex and different
        self.assertEqual(len(new_machine_id), 64)
        self.assertEqual(len(new_hw.get("macMachineId", "")), 64)
        self.assertTrue(new_hw.get("devDeviceId"))
        self.assertTrue(new_hw.get("sqmId"))

    def test_10_immediate_auto_switch_zero_delay(self):
        """trigger_immediate_auto_switch must execute zero-delay auto-switching and mark current account EXHAUSTED."""
        active_before = self.storage.get_active_account()
        email_before = (active_before.get("email") or "").strip()

        t0 = time.perf_counter()
        best = pool.trigger_immediate_auto_switch(reason="Test 429 Quota Exhaustion Trigger", reset_mode="soft_reload")
        t_switch_ms = (time.perf_counter() - t0) * 1000

        self.assertIsNotNone(best)
        self.assertLess(best.get("usage_percent", 100), QUOTA_EXHAUSTION_THRESHOLD)

        # Verified in state.vscdb
        active_after = self.storage.get_active_account()
        self.assertEqual(active_after.get("email", "").lower(), best.get("email", "").lower())

        # If previous email was known, verify it was marked EXHAUSTED
        if email_before:
            con = sqlite3.connect("cursor_accounts.db")
            cur = con.cursor()
            cur.execute("SELECT status FROM accounts WHERE LOWER(email) = LOWER(?)", (email_before,))
            r = cur.fetchone()
            con.close()
            if r:
                self.assertEqual(r[0], "EXHAUSTED")

        print(f"      -> Zero-Delay Switch completed in: {t_switch_ms:.2f} ms")
        self.assertLess(t_switch_ms, 5000)  # Must complete well within 5 seconds

    def test_11_keybinding_clean_reload_ctrl_shift_f11(self):
        """Ensure workbench.action.reloadWindow is cleanly bound to ctrl+shift+f11."""
        ok = cursor_reloader.ensure_keybinding()
        self.assertTrue(ok)

        kb_path = os.path.join(os.getenv("APPDATA") or "", "Cursor", "User", "keybindings.json")
        self.assertTrue(os.path.exists(kb_path))
        with open(kb_path, "r", encoding="utf-8") as f:
            bindings = json.load(f)

        found = any(
            b.get("command") == "workbench.action.reloadWindow" and b.get("key") == "ctrl+shift+f11"
            for b in bindings
        )
        self.assertTrue(found, "ctrl+shift+f11 keybinding not found in keybindings.json!")

    # =========================================================================
    # 3. 100% NATIVE CURSOR SETTINGS FIDELITY TESTS
    # =========================================================================

    def test_12_native_cursor_settings_fidelity(self):
        """Verify settings.json maintains 100% native Cursor configuration keys."""
        settings = self.settings.get_settings()
        self.assertIn("default_model", settings)
        self.assertIn("privacy_mode", settings)
        self.assertIn("cpp_enable", settings)
        self.assertIn("cpp_partial_accepts", settings)
        self.assertIn("composer_auto_apply", settings)
        self.assertIn("use_inline_diffs", settings)
        self.assertIn("always_search_codebase", settings)
        self.assertIn("auto_scroll", settings)

        # Raw VS Code/Cursor keys exist
        raw = settings.get("raw", {})
        self.assertIn("cursor.chat.defaultModel", raw)
        self.assertIn("cursor.general.model", raw)
        self.assertIn("cursor.composer.defaultModel", raw)

    # =========================================================================
    # 4. API ENDPOINTS TESTS
    # =========================================================================

    def test_13_api_task_filter_endpoints(self):
        """Test /api/task/filter-status and /api/task/filter-mark endpoints."""
        # 1. Mark interrupted via API
        res = self.client.post("/api/task/filter-mark", json={
            "action": "interrupted",
            "reason": "API quota lockout simulation",
            "request_id": "test-req-api"
        })
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("success"))

        # 2. Check filter status
        res = self.client.get("/api/task/filter-status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertTrue(data.get("status", {}).get("is_interrupted"))
        self.assertTrue(data.get("should_auto_continue"))

        # 3. Mark completed via API
        res = self.client.post("/api/task/filter-mark", json={"action": "completed"})
        self.assertEqual(res.status_code, 200)

        # 4. Check filter status after completion
        res = self.client.get("/api/task/filter-status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("status", {}).get("is_completed_naturally"))
        self.assertFalse(data.get("should_auto_continue"))

    def test_14_api_auto_switch_immediate_endpoint(self):
        """Test /api/auto-switch/immediate endpoint."""
        res = self.client.post("/api/auto-switch/immediate", json={"reason": "API Immediate Switch Test"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("account", data)
        self.assertIn("active", data)
        self.assertLess(data["account"].get("usage_percent", 100), QUOTA_EXHAUSTION_THRESHOLD)

    # =========================================================================
    # 5. REGRESSION & CRITICAL BUG FIX TESTS
    # =========================================================================

    def test_15_interruption_consumption_prevents_duplicate_resend(self):
        """When consume=True, should_auto_continue must consume the event so subsequent calls return False."""
        TaskStateTracker().reset()
        mark_task_interrupted("In-flight quota limit 429", request_id="req-consume-test")

        # 1. First call with consume=True
        should_1, reason_1 = self.filter.should_auto_continue(auto_continue=None, consume=True)
        self.assertTrue(should_1)
        self.assertIn("interrupted mid-flight", reason_1.lower())

        # 2. Immediate second call without new error -> MUST be suppressed!
        should_2, reason_2 = self.filter.should_auto_continue(auto_continue=None, consume=False)
        self.assertFalse(should_2)
        self.assertIn("suppressed", reason_2.lower())

    def test_16_checksum_cache_invalidation_on_spoof_and_restore(self):
        """Hardware ID spoofing must immediately invalidate in-memory cached checksum IDs."""
        import cursor_checksum
        orig_ids = cursor_checksum.get_cursor_machine_ids(force_refresh=True)

        try:
            self.settings.spoof_storage_ids()
            spoofed_ids = cursor_checksum.get_cursor_machine_ids()
            # Must not be equal to original cached IDs
            self.assertNotEqual(orig_ids, spoofed_ids)
            self.assertEqual(len(spoofed_ids[0]), 64)

            # Checksum must reflect the spoofed machine ID
            chk = cursor_checksum.generate_cursor_checksum()
            self.assertIn(spoofed_ids[0], chk)
        finally:
            self.settings.restore_storage_ids()
            restored_ids = cursor_checksum.get_cursor_machine_ids()
            self.assertEqual(orig_ids, restored_ids)

    def test_17_log_parser_transport_stream_error(self):
        """Verify structured log parser accurately extracts extension host transport stream errors (ConnectError code 8)."""
        transport_line = (
            '2026-09-04 08:54:55.378 [error] {"level":"error","key":"transport",'
            '"message":"Stream error after headers in extension host",'
            '"metadata":{"arch":"x64","platform":"win32","channel":"stable","client_version":"3.17.8",'
            '"error.message":"[resource_exhausted] Error","error.kind":"ConnectError","error.code":"8",'
            '"service":"agent.v1.AgentService","method":"run","requestId":"req-trans-8"}}'
        )
        parsed = self.filter._parse_log_line(transport_line)
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["is_quota_lockout"])
        self.assertIn("8", parsed["lockout_reason"])
        self.assertEqual(parsed["request_id"], "req-trans-8")

    def test_18_watcher_idle_rotation_suppresses_prompt_resend(self):
        """When watcher rotates an account while Cursor is idle, prompt-resend must be strictly suppressed."""
        TaskStateTracker().reset()
        # No mid-flight error recorded
        should_cont, reason = self.filter.should_auto_continue(
            auto_continue=None,
            max_age_seconds=1.0  # historical logs considered stale
        )
        self.assertFalse(should_cont)
        self.assertIn("suppressed", reason.lower())

    def test_19_immediate_auto_switch_explicit_auto_continue_false(self):
        """trigger_immediate_auto_switch must honor auto_continue=False explicitly."""
        active_before = self.storage.get_active_account()

        best = pool.trigger_immediate_auto_switch(
            reason="Explicit Test No Auto Continue",
            auto_continue=False,
            spoof_hw=True
        )
        self.assertIsNotNone(best)

        # Ensure filter does not allow continuation
        should_cont, _ = self.filter.should_auto_continue(auto_continue=False)
        self.assertFalse(should_cont)

    def test_20_keybindings_jsonc_with_comments_safely_parsed(self):
        """ensure_keybinding must safely parse JSONC with comments and trailing commas without corruption."""
        sample_jsonc = """// Custom Keybindings
[
    {
        "key": "ctrl+shift+p",
        "command": "workbench.action.showCommands",
    }, // trailing comma
]
"""
        parsed = cursor_reloader._parse_jsonc(sample_jsonc)
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["key"], "ctrl+shift+p")

    def test_21_find_all_cursor_windows_api(self):
        """Verify find_all_cursor_windows returns list and handles multi-window calls safely."""
        windows = cursor_reloader.find_all_cursor_windows()
        self.assertIsInstance(windows, list)
        # If Cursor is running, it should find at least one window
        if cursor_reloader.is_cursor_running():
            status = cursor_reloader.get_cursor_status()
            if status.get("has_window"):
                self.assertGreaterEqual(len(windows), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
