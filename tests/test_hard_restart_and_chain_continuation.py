"""
Automated Integration Test Suite:
1. Hard App Reset / Restart Workflow (Process Kill -> Clean Storage Injection -> Clean Relaunch)
2. Chain Continuation Pipeline Across Multiple Accounts (Acc 1 -> Acc 2 -> Acc 3 Cascade)
3. Configuration Endpoints & UI Integration (reset_mode: hard_restart vs soft_reload)
4. Edge Cases: Unicode Prompts, App Not Running, All Quota Exhausted
"""

import os
import sys
import json
import time
import sqlite3
import unittest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.dirname(__file__))

from server import app, pool, settings_manager
import cursor_reloader
from cursor_settings import CursorSettingsManager
from cursor_storage import CursorStorageManager
from account_pool import AccountPoolManager, DB_FILE, DB_LOCK, QUOTA_EXHAUSTION_THRESHOLD
from smart_task_filter import SmartTaskCompletionFilter, TaskStateTracker, mark_task_interrupted, mark_task_completed


class TestHardRestartAndChainContinuation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()
        cls.settings_mgr = CursorSettingsManager()
        cls.storage_mgr = CursorStorageManager()
        cls.pool = pool

    def setUp(self):
        TaskStateTracker().reset()

    def tearDown(self):
        TaskStateTracker().reset()

    # =========================================================================
    # 1. PROCESS TERMINATION AND RELAUNCH LOGIC
    # =========================================================================

    def test_01_terminate_cursor_when_not_running(self):
        """terminate_cursor should return True immediately if Cursor is not running."""
        with patch("cursor_reloader.is_cursor_running", return_value=False):
            with patch("subprocess.run") as mock_sub:
                res = cursor_reloader.terminate_cursor(timeout_sec=1.0)
                self.assertTrue(res)
                mock_sub.assert_not_called()

    def test_02_terminate_cursor_when_running(self):
        """terminate_cursor should invoke taskkill /F /T /IM Cursor.exe and wait for exit."""
        running_states = [True, True, False]  # Running initially, then exits
        def mock_is_running():
            return running_states.pop(0) if running_states else False

        with patch("cursor_reloader.is_cursor_running", side_effect=mock_is_running):
            with patch("subprocess.run") as mock_sub:
                res = cursor_reloader.terminate_cursor(timeout_sec=3.0)
                self.assertTrue(res)
                self.assertTrue(mock_sub.called)
                args = mock_sub.call_args[0][0]
                self.assertIn("taskkill", args[0].lower())
                self.assertIn("cursor.exe", [a.lower() for a in args])

    def test_03_launch_cursor_app_execution(self):
        """launch_cursor_app should execute CIM or startfile to launch Cursor."""
        with patch("cursor_reloader.get_cursor_exe_path", return_value="C:\\Fake\\Cursor.exe"):
            with patch("os.path.exists", return_value=True):
                with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ProcessId: 1234")):
                    with patch("cursor_reloader.find_cursor_window", return_value=99999):
                        with patch("cursor_reloader.force_bring_to_front", return_value=True):
                            ok = cursor_reloader.launch_cursor_app(wait_for_window=True, timeout_sec=2.0)
                            self.assertTrue(ok)

    def test_04_hard_restart_cursor_sequence(self):
        """hard_restart_cursor must execute terminate -> inject_callback -> launch in exact order."""
        call_order = []

        def mock_terminate(timeout_sec=8.0):
            call_order.append("terminate")
            return True

        def mock_callback():
            call_order.append("inject_callback")
            return True

        def mock_launch(wait_for_window=True, timeout_sec=15.0):
            call_order.append("launch")
            return True

        with patch("cursor_reloader.is_cursor_running", return_value=True):
            with patch("cursor_reloader.terminate_cursor", side_effect=mock_terminate):
                with patch("cursor_reloader.launch_cursor_app", side_effect=mock_launch):
                    res = cursor_reloader.hard_restart_cursor(inject_callback=mock_callback, wait_for_window=True)
                    self.assertTrue(res["success"])
                    self.assertEqual(res["method"], "hard_restart")
                    self.assertEqual(call_order, ["terminate", "inject_callback", "launch"])

    # =========================================================================
    # 2. TOKEN INJECTION & HARDWARE SPOOFING PRECEDING RELAUNCH
    # =========================================================================

    def test_05_switch_to_account_hard_restart_order(self):
        """switch_to_account with hard_restart must terminate Cursor before writing tokens, then relaunch."""
        call_order = []

        def mock_terminate(timeout_sec=8.0):
            call_order.append("terminate")
            return True

        def mock_inject(token, refresh, profile):
            call_order.append("inject_profile")
            return True

        def mock_spoof():
            call_order.append("spoof_hw")
            return {}

        def mock_launch(wait_for_window=True, timeout_sec=15.0):
            call_order.append("relaunch")
            return True

        # Grab any existing account from DB
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute("SELECT id FROM accounts LIMIT 1")
        row = cur.fetchone()
        con.close()
        self.assertIsNotNone(row, "Need at least one account in DB for test")
        acc_id = row[0]

        with patch("cursor_reloader.is_cursor_running", return_value=True):
            with patch("cursor_reloader.terminate_cursor", side_effect=mock_terminate):
                with patch.object(self.pool.storage, "inject_full_profile", side_effect=mock_inject):
                    with patch("cursor_settings.CursorSettingsManager.spoof_storage_ids", side_effect=mock_spoof):
                        with patch("cursor_reloader.launch_cursor_app", side_effect=mock_launch):
                            with patch.object(self.pool.storage, "get_active_account", return_value={"email": "test@example.com", "has_token": True}):
                                ok = self.pool.switch_to_account(acc_id, auto_reload=True, reset_mode="hard_restart", verify=False)
                                self.assertTrue(ok)
                                # Strict ordering: terminate MUST precede inject_profile, which MUST precede relaunch
                                self.assertIn("terminate", call_order)
                                self.assertIn("inject_profile", call_order)
                                self.assertIn("relaunch", call_order)
                                term_idx = call_order.index("terminate")
                                inj_idx = call_order.index("inject_profile")
                                relaunch_idx = call_order.index("relaunch")
                                self.assertLess(term_idx, inj_idx, "terminate must happen before inject_profile")
                                self.assertLess(inj_idx, relaunch_idx, "inject_profile must happen before relaunch")

    # =========================================================================
    # 3. CASCADING CONTINUATION ACROSS MULTIPLE ACCOUNTS
    # =========================================================================

    def test_06_cascading_continuation_three_accounts(self):
        """
        Simulate full multi-account cascading loop:
        Acc 1 hits quota limit -> hard restart -> Acc 2 continues prompt.
        Acc 2 hits quota limit mid-task -> hard restart -> Acc 3 continues prompt.
        Acc 3 finishes task naturally -> prompt resend suppressed, cascade terminates cleanly.
        """
        prompt_dispatches = []
        app_restarts = []

        def mock_send_prompt(prompt_text="Tiếp tục", target_composer=True, fresh_session=True):
            prompt_dispatches.append({
                "prompt": prompt_text,
                "target_composer": target_composer,
                "fresh_session": fresh_session,
                "time": time.time()
            })
            return {"success": True, "prompt": prompt_text}

        def mock_hard_restart(*args, **kwargs):
            app_restarts.append(time.time())
            return {"success": True, "method": "hard_restart"}

        # Step 1: Acc 1 runs out of quota mid-task
        mark_task_interrupted(reason="Account 1 429 Quota Exhaustion", source="proxy")
        should_c1, r1 = SmartTaskCompletionFilter().should_auto_continue(auto_resend_cfg="auto", consume=True)
        self.assertTrue(should_c1, f"Acc 1 interrupted should trigger continue: {r1}")

        # Simulate Account 1 -> Account 2 switch
        with patch("cursor_reloader.send_continue_prompt", side_effect=mock_send_prompt):
            mock_hard_restart()
            cursor_reloader.send_continue_prompt(prompt_text="Tiếp tục", target_composer=True, fresh_session=True)

        self.assertEqual(len(prompt_dispatches), 1)
        self.assertEqual(prompt_dispatches[0]["prompt"], "Tiếp tục")
        self.assertTrue(prompt_dispatches[0]["fresh_session"])

        # Step 2: Acc 2 subsequently hits quota limit while generating
        time.sleep(0.05)
        mark_task_interrupted(reason="Account 2 monthly limit reached", source="log_detector")
        should_c2, r2 = SmartTaskCompletionFilter().should_auto_continue(auto_resend_cfg="auto", consume=True)
        self.assertTrue(should_c2, f"Acc 2 interrupted should trigger continue: {r2}")

        # Simulate Account 2 -> Account 3 cascade
        with patch("cursor_reloader.send_continue_prompt", side_effect=mock_send_prompt):
            mock_hard_restart()
            cursor_reloader.send_continue_prompt(prompt_text="Tiếp tục", target_composer=True, fresh_session=True)

        self.assertEqual(len(prompt_dispatches), 2)
        self.assertEqual(len(app_restarts), 2)

        # Step 3: Acc 3 finishes naturally (no quota error, task completed)
        time.sleep(0.05)
        mark_task_completed(source="normal")
        should_c3, r3 = SmartTaskCompletionFilter().should_auto_continue(auto_resend_cfg="auto", consume=True)
        self.assertFalse(should_c3, f"Acc 3 finished naturally must NOT continue: {r3}")

        # Prompt dispatches must remain at 2 (no third dispatch)
        self.assertEqual(len(prompt_dispatches), 2)

    # =========================================================================
    # 4. CONFIGURATION ENDPOINTS & UI INTEGRATION
    # =========================================================================

    def test_07_api_auto_switch_config_reset_mode(self):
        """Test GET and POST /api/cursor/auto-switch-config with reset_mode."""
        # 1. GET initial
        res = self.client.get("/api/cursor/auto-switch-config")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        cfg = data.get("config", {})
        self.assertIn("reset_mode", cfg)

        # 2. POST change to soft_reload
        res = self.client.post("/api/cursor/auto-switch-config", json={
            "reset_mode": "soft_reload",
            "auto_resend_action": "manual",
            "continue_prompt": "Continue please"
        })
        self.assertEqual(res.status_code, 200)
        saved = res.get_json().get("config", {})
        self.assertEqual(saved.get("reset_mode"), "soft_reload")
        self.assertEqual(saved.get("auto_resend_action"), "manual")

        # Verify disk persistence
        disk_cfg = self.settings_mgr.get_auto_switch_config()
        self.assertEqual(disk_cfg.get("reset_mode"), "soft_reload")

        # 3. Restore to hard_restart
        res = self.client.post("/api/cursor/auto-switch-config", json={
            "reset_mode": "hard_restart",
            "auto_resend_action": "manual",
            "continue_prompt": "Tiếp tục"
        })
        self.assertEqual(res.status_code, 200)
        restored = res.get_json().get("config", {})
        self.assertEqual(restored.get("reset_mode"), "hard_restart")
        self.assertEqual(restored.get("auto_resend_action"), "manual")

    def test_08_api_cursor_hard_restart_endpoints(self):
        """Test POST /api/cursor/hard-restart and /api/cursor/restart endpoints."""
        with patch("server.cursor_reloader.hard_restart_cursor", return_value={"success": True, "method": "hard_restart", "message": "OK"}):
            # Endpoint 1: /api/cursor/hard-restart
            res1 = self.client.post("/api/cursor/hard-restart")
            self.assertEqual(res1.status_code, 200)
            data1 = res1.get_json()
            self.assertTrue(data1.get("success"))
            self.assertEqual(data1.get("method"), "hard_restart")

            # Endpoint 2: /api/cursor/restart
            res2 = self.client.post("/api/cursor/restart")
            self.assertEqual(res2.status_code, 200)
            data2 = res2.get_json()
            self.assertTrue(data2.get("success"))

    def test_09_api_reload_window_with_mode(self):
        """Test /api/reload-window supports mode=hard_restart."""
        with patch("server.cursor_reloader.hard_restart_cursor", return_value={"success": True, "method": "hard_restart"}) as mock_hr:
            with patch("server.cursor_reloader.trigger_cursor_reload") as mock_soft:
                res = self.client.post("/api/reload-window?mode=hard_restart")
                self.assertEqual(res.status_code, 200)
                mock_hr.assert_called_once()
                mock_soft.assert_not_called()

    def test_10_api_switch_accepts_reset_mode(self):
        """Test /api/switch/<id> respects reset_mode parameter in payload."""
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()
        cur.execute("SELECT id FROM accounts LIMIT 1")
        acc_id = cur.fetchone()[0]
        con.close()

        with patch("server.pool.switch_to_account", return_value=True) as mock_sw:
            res = self.client.post(f"/api/switch/{acc_id}", json={"reset_mode": "hard_restart", "auto_continue": True})
            self.assertEqual(res.status_code, 200)
            mock_sw.assert_called_with(acc_id, auto_reload=True, auto_continue=True, reset_mode="hard_restart")

    # =========================================================================
    # 5. EDGE CASES
    # =========================================================================

    def test_11_unicode_vietnamese_prompt_handling(self):
        """Ensure Vietnamese Unicode text like 'Tiếp tục' is handled without charmap encoding error."""
        text = "Tiếp tục hoàn thành task và fix lỗi"
        # Test PowerShell base64 snippet generation
        import base64
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        decoded = base64.b64decode(b64.encode("ascii")).decode("utf-8")
        self.assertEqual(decoded, text)

        # Test safe display string without crashing
        safe = text.encode("ascii", errors="replace").decode("ascii")
        self.assertIn("?", safe)  # Accented chars replaced safely

    def test_12_hard_restart_when_cursor_not_running(self):
        """hard_restart_cursor when Cursor is not running should not call terminate, but should launch."""
        with patch("cursor_reloader.is_cursor_running", return_value=False):
            with patch("cursor_reloader.terminate_cursor") as mock_term:
                with patch("cursor_reloader.launch_cursor_app", return_value=True) as mock_launch:
                    res = cursor_reloader.hard_restart_cursor(wait_for_window=False)
                    self.assertTrue(res["success"])
                    self.assertFalse(res["was_running"])
                    mock_term.assert_not_called()
                    mock_launch.assert_called_once()

    def test_13_send_continue_prompt_fresh_session_sends_ctrl_n(self):
        """send_continue_prompt with fresh_session=True must send Ctrl+N to clear frozen error thread."""
        keybd_calls = []

        def mock_keybd_event(bVk, bScan, dwFlags, dwExtraInfo):
            keybd_calls.append((bVk, dwFlags))

        with patch("sys.platform", "win32"):
            with patch("cursor_reloader.find_cursor_window", return_value=12345):
                with patch("cursor_reloader.force_bring_to_front", return_value=True):
                    with patch("cursor_reloader._is_safe_cursor_focused", return_value=True):
                        with patch("ctypes.windll.user32.keybd_event", side_effect=mock_keybd_event):
                            # 1. Test fresh_session=True
                            res = cursor_reloader.send_continue_prompt(prompt_text="Tiếp tục", target_composer=True, fresh_session=True)
                            self.assertTrue(res["success"])

                        VK_I = 0x49
                        VK_N = 0x4E
                        VK_V = 0x56
                        VK_RETURN = 0x0D

                        keys_pressed = [k[0] for k in keybd_calls if k[1] == 0]
                        self.assertIn(VK_I, keys_pressed, "Composer key (Ctrl+I) must be pressed")
                        self.assertIn(VK_N, keys_pressed, "Fresh session key (Ctrl+N) must be pressed when fresh_session=True")
                        self.assertIn(VK_V, keys_pressed, "Paste key (Ctrl+V) must be pressed")
                        self.assertIn(VK_RETURN, keys_pressed, "Enter key must be pressed")

                        # Verify order: VK_I before VK_N, and VK_N before VK_V
                        idx_i = keys_pressed.index(VK_I)
                        idx_n = keys_pressed.index(VK_N)
                        idx_v = keys_pressed.index(VK_V)
                        self.assertLess(idx_i, idx_n, "Composer must be focused before fresh session Ctrl+N")
                        self.assertLess(idx_n, idx_v, "Fresh session Ctrl+N must precede pasting prompt")

                        # 2. Test fresh_session=False
                        keybd_calls.clear()
                        res2 = cursor_reloader.send_continue_prompt(prompt_text="Tiếp tục", target_composer=True, fresh_session=False)
                        self.assertTrue(res2["success"])
                        keys_pressed_no_fresh = [k[0] for k in keybd_calls if k[1] == 0]
                        self.assertNotIn(VK_N, keys_pressed_no_fresh, "VK_N must NOT be pressed when fresh_session=False")

    def test_14_real_pool_cascading_continuation_flow(self):
        """
        Verify real cascading continuation flow through AccountPoolManager:
        Acc 1 hits quota limit -> trigger_immediate_auto_switch -> switches to Acc 2 + hard restart + dispatches continue prompt.
        Acc 2 hits quota limit mid-task -> trigger_immediate_auto_switch -> switches to Acc 3 + hard restart + dispatches continue prompt.
        Acc 3 finishes task naturally -> prompt resend suppressed.
        """
        import tempfile
        import shutil
        test_dir = tempfile.mkdtemp(prefix="cursor_cascade_test_")
        test_db = os.path.join(test_dir, "test_accounts.db")
        test_cookies = os.path.join(test_dir, "Cookies")
        os.makedirs(test_cookies, exist_ok=True)

        con = sqlite3.connect(test_db)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT UNIQUE,
                email TEXT,
                display_name TEXT,
                auth_id TEXT,
                cookie_snippet TEXT,
                cookie_full TEXT,
                access_token TEXT,
                refresh_token TEXT,
                usage_percent REAL DEFAULT 0.0,
                total_spend REAL DEFAULT 0.0,
                display_message TEXT,
                status TEXT DEFAULT 'READY',
                last_checked INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE quota_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                email TEXT,
                usage_percent REAL DEFAULT 0.0,
                total_spend REAL DEFAULT 0.0,
                display_message TEXT,
                status TEXT,
                source TEXT DEFAULT 'sync',
                recorded_at INTEGER DEFAULT 0
            )
        """)
        cur.executemany("""
            INSERT INTO accounts (id, email, access_token, refresh_token, usage_percent, total_spend, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            (1, "acc1@cascade.com", "token1_12345678901234567890", "ref1", 52.0, 110.0, "EXHAUSTED"),
            (2, "acc2@cascade.com", "token2_12345678901234567890", "ref2", 10.0, 20.0, "READY"),
            (3, "acc3@cascade.com", "token3_12345678901234567890", "ref3", 15.0, 30.0, "READY"),
        ])
        con.commit()
        con.close()

        mgr = AccountPoolManager(cookies_dir=test_cookies)

        dispatched_prompts = []
        terminated_calls = []
        relaunched_calls = []

        def mock_send(prompt_text="Tiếp tục", target_composer=True, fresh_session=True):
            dispatched_prompts.append({"prompt": prompt_text, "fresh_session": fresh_session})
            return {"success": True}

        def mock_terminate(*args, **kwargs):
            terminated_calls.append(time.time())
            return True

        def mock_launch(*args, **kwargs):
            relaunched_calls.append(time.time())
            return True

        with patch("account_pool.DB_FILE", test_db):
            with patch("cursor_reloader.is_cursor_running", return_value=True):
                with patch("cursor_reloader.terminate_cursor", side_effect=mock_terminate):
                    with patch("cursor_reloader.launch_cursor_app", side_effect=mock_launch):
                        with patch("cursor_reloader.send_continue_prompt", side_effect=mock_send):
                            with patch.object(mgr.storage, "inject_full_profile", return_value=True):
                                with patch.object(mgr.storage, "get_active_account", side_effect=[
                                    {"email": "acc1@cascade.com", "access_token": "token1"},
                                    {"email": "acc2@cascade.com", "access_token": "token2", "has_token": True},
                                    {"email": "acc2@cascade.com", "access_token": "token2"},
                                    {"email": "acc3@cascade.com", "access_token": "token3", "has_token": True},
                                ]):
                                    with patch("cursor_settings.CursorSettingsManager.spoof_storage_ids", return_value={}):
                                        with patch("cursor_settings.CursorSettingsManager.get_auto_switch_config", side_effect=lambda: {"reset_mode": "hard_restart", "auto_resend_action": "auto", "continue_prompt": "Tiếp tục", "target_mode": "composer"}):
                                            # Stage 1: Acc 1 runs out of quota, triggers immediate auto-switch
                                            best1 = mgr.trigger_immediate_auto_switch(reason="Acc 1 429 Quota Exhaustion")
                                            self.assertIsNotNone(best1)
                                            self.assertEqual(best1["email"], "acc2@cascade.com")
                                            self.assertEqual(len(terminated_calls), 1)
                                            self.assertEqual(len(relaunched_calls), 1)

                                            time.sleep(2.8)
                                            self.assertEqual(len(dispatched_prompts), 1)
                                            self.assertEqual(dispatched_prompts[0]["prompt"], "Tiếp tục")
                                            self.assertTrue(dispatched_prompts[0]["fresh_session"])

                                            # Stage 2: Acc 2 hits quota limit mid-task
                                            best2 = mgr.trigger_immediate_auto_switch(reason="Acc 2 quota threshold hit")
                                            self.assertIsNotNone(best2)
                                            self.assertEqual(best2["email"], "acc3@cascade.com")
                                            self.assertEqual(len(terminated_calls), 2)
                                            self.assertEqual(len(relaunched_calls), 2)

                                            time.sleep(2.8)
                                            self.assertEqual(len(dispatched_prompts), 2)

                                            # Stage 3: Task completes naturally
                                            mark_task_completed(source="task_finished")
                                            should_cont, reason = SmartTaskCompletionFilter().should_auto_continue()
                                            self.assertFalse(should_cont, f"Natural completion must suppress auto-continue: {reason}")

        shutil.rmtree(test_dir, ignore_errors=True)

    def test_15_auto_rotate_watcher_marks_interruption(self):
        """auto_rotate_watcher must mark task interrupted when locked=True, allowing continue cascade."""
        TaskStateTracker().reset()

        lock_reason = "Usage 52.0% >= 50.0%"
        from smart_task_filter import mark_task_interrupted
        mark_task_interrupted(
            reason=f"Auto-rotate watcher quota exhaustion: {lock_reason}",
            source="auto_rotate_watcher"
        )

        f = SmartTaskCompletionFilter()
        status = f.get_latest_task_status()
        self.assertTrue(status["is_interrupted"])
        self.assertIn("Auto-rotate watcher quota exhaustion", status["interruption_reason"])

        should_cont, r = f.should_auto_continue(auto_resend_cfg="auto", consume=True)
        self.assertTrue(should_cont)

    @classmethod
    def tearDownClass(cls):
        cls.settings_mgr.update_auto_switch_config({
            "auto_rotate_enabled": True,
            "quota_threshold": 100.0,
            "reset_mode": "hard_restart",
            "auto_resend_action": "manual",
            "continue_prompt": "Tiếp tục",
            "target_mode": "composer"
        })


if __name__ == "__main__":
    unittest.main()
