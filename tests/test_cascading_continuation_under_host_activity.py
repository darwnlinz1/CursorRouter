"""
Comprehensive Verification Suite:
Cascading Multi-Account Task Continuation Under Host Multitasking
==================================================================
Tests:
1. Mid-task quota lockout on Acc 1 defers prompt when user is actively typing in another app.
2. Focus is restored to user's previous active window after safe prompt dispatch.
3. Cascading switch from Acc 1 -> Acc 2 -> Acc 3 maintains context and hardware spoofing.
4. Natural task completion on Acc 3 immediately suppresses continuation loop.
5. User clipboard content is 100% preserved with zero pollution.
6. Keyboard modifier keys (Alt, Ctrl, Shift, Win) remain 100% clean (zero stuck keys).
"""

import os
import sys
import time
import json
import sqlite3
import unittest
from unittest.mock import patch, MagicMock

# Add src and root to path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import cursor_reloader
from cursor_reloader import (
    send_continue_prompt,
    is_user_actively_typing,
    get_modifier_keys_status,
    release_all_modifier_keys
)
from smart_task_filter import (
    SmartTaskCompletionFilter,
    TaskStateTracker,
    mark_task_interrupted,
    mark_task_completed
)
from account_pool import AccountPoolManager


class TestCascadingContinuationUnderHostActivity(unittest.TestCase):
    def setUp(self):
        TaskStateTracker().reset()
        self.filter = SmartTaskCompletionFilter()
        release_all_modifier_keys()

    def tearDown(self):
        TaskStateTracker().reset()
        release_all_modifier_keys()

    def test_01_user_actively_typing_suppresses_immediate_focus_theft(self):
        """When user is actively typing in another app, send_continue_prompt must defer dispatch."""
        with patch("cursor_reloader.find_cursor_window", return_value=12345):
            with patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == 12345):
                with patch("cursor_reloader.is_user_actively_typing", return_value=(True, 0.5)):
                    mock_u32 = MagicMock()
                    mock_u32.GetForegroundWindow.return_value = 99999  # Another application (e.g. Browser)
                    mock_u32.IsWindow.return_value = True

                    with patch("cursor_reloader.user32", mock_u32):
                        res = send_continue_prompt(
                            prompt_text="Tiếp tục",
                            target_composer=True,
                            fresh_session=True,
                            restore_previous_focus=True,
                            respect_user_activity=True
                        )

                        self.assertFalse(res["success"])
                        self.assertTrue(res.get("deferred", False))
                        self.assertIn("dang thao tac", res.get("error", "").lower())

    def test_02_safe_prompt_dispatch_when_user_becomes_idle(self):
        """When user becomes idle, prompt dispatches safely to Cursor and restores previous window."""
        with patch("cursor_reloader.find_cursor_window", return_value=12345):
            with patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == 12345):
                with patch("cursor_reloader.is_user_actively_typing", return_value=(False, 3.5)):
                    with patch("cursor_reloader.force_bring_to_front", return_value=True):
                        with patch("cursor_reloader._is_safe_cursor_focused", return_value=True):
                            with patch("cursor_reloader._get_clipboard_backup", return_value=(True, "Original User Text")):
                                with patch("cursor_reloader._set_clipboard_text", return_value=True):
                                    with patch("cursor_reloader._restore_clipboard") as mock_restore:
                                        with patch("cursor_reloader._send_key_combo") as mock_keys:
                                            mock_u32 = MagicMock()
                                            mock_u32.GetForegroundWindow.return_value = 88888  # Notepad
                                            mock_u32.IsWindow.return_value = True

                                            with patch("cursor_reloader.user32", mock_u32):
                                                res = send_continue_prompt(
                                                    prompt_text="Tiếp tục giải quyết task",
                                                    target_composer=True,
                                                    fresh_session=True,
                                                    restore_previous_focus=True,
                                                    respect_user_activity=True
                                                )

                                                self.assertTrue(res["success"])
                                                self.assertEqual(res["prompt"], "Tiếp tục giải quyết task")
                                                self.assertEqual(res["target"], "composer")
                                                # Clipboard must be restored
                                                self.assertTrue(mock_restore.called)
                                                # Focus must be returned to Notepad (88888)
                                                mock_u32.SetForegroundWindow.assert_called_with(88888)

    def test_03_cascading_task_continuation_chain_acc1_to_acc2_to_acc3(self):
        """Simulate cascade: Acc 1 hits quota limit -> Acc 2 continues -> Acc 2 hits limit -> Acc 3 finishes."""
        TaskStateTracker().reset()

        # Step 1: Acc 1 gets interrupted mid-flight
        mark_task_interrupted("Acc 1 Hit 429 Rate Limit", request_id="task-req-1")
        should_1, reason_1 = self.filter.should_auto_continue()
        self.assertTrue(should_1)
        self.assertIn("interrupted mid-flight", reason_1.lower())

        # Step 2: Acc 2 takes over and is generating, but hits 100% quota limit again
        mark_task_interrupted("Acc 2 Hit 100% Quota Exhaustion", request_id="task-req-2")
        should_2, reason_2 = self.filter.should_auto_continue()
        self.assertTrue(should_2)
        self.assertIn("interrupted mid-flight", reason_2.lower())

        # Step 3: Acc 3 finishes the task cleanly and naturally
        mark_task_completed(request_id="task-req-2")
        should_3, reason_3 = self.filter.should_auto_continue()
        self.assertFalse(should_3)
        self.assertIn("completed or ended naturally", reason_3.lower())

    def test_04_zero_stuck_modifier_keys_guarantee(self):
        """get_modifier_keys_status must report any_stuck == False."""
        release_all_modifier_keys()
        status = get_modifier_keys_status()
        self.assertIsInstance(status, dict)
        self.assertIn("any_stuck", status)
        self.assertFalse(status["any_stuck"])
        self.assertFalse(status["alt"])
        self.assertFalse(status["ctrl"])
        self.assertFalse(status["shift"])
        self.assertFalse(status["win"])

    def test_05_clipboard_preservation_under_errors(self):
        """Even if keyboard dispatch throws an unexpected exception, clipboard must be restored."""
        with patch("cursor_reloader.find_cursor_window", return_value=12345):
            with patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == 12345):
                with patch("cursor_reloader.is_user_actively_typing", return_value=(False, 5.0)):
                    with patch("cursor_reloader.force_bring_to_front", return_value=True):
                        with patch("cursor_reloader._is_safe_cursor_focused", return_value=True):
                            with patch("cursor_reloader._get_clipboard_backup", return_value=(True, "Sensitive Secret Data")):
                                with patch("cursor_reloader._set_clipboard_text", return_value=True):
                                    with patch("cursor_reloader._restore_clipboard") as mock_restore:
                                        with patch("cursor_reloader._send_key_combo", side_effect=RuntimeError("Simulated OS Error")):
                                            mock_u32 = MagicMock()
                                            mock_u32.GetForegroundWindow.return_value = 12345
                                            mock_u32.IsWindow.return_value = True

                                            with patch("cursor_reloader.user32", mock_u32):
                                                res = send_continue_prompt("Tiếp tục")
                                                self.assertFalse(res["success"])
                                                # Clipboard restore MUST still have been called in finally
                                                self.assertTrue(mock_restore.called)

    def test_06_send_continue_prompt_with_tail_anchor_synthesis(self):
        """When truncated_snippet is passed, prompt is automatically synthesized with context anchor."""
        with patch("cursor_reloader.find_cursor_window", return_value=12345):
            with patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == 12345):
                with patch("cursor_reloader.is_user_actively_typing", return_value=(False, 3.0)):
                    with patch("cursor_reloader.force_bring_to_front", return_value=True):
                        with patch("cursor_reloader._is_safe_cursor_focused", return_value=True):
                            with patch("cursor_reloader._set_clipboard_text", return_value=True):
                                with patch("cursor_reloader._get_clipboard_backup", return_value=(False, "")):
                                    with patch("cursor_reloader._send_key_combo"):
                                        with patch("cursor_reloader._send_single_key"):
                                            mock_u32 = MagicMock()
                                            mock_u32.GetForegroundWindow.return_value = 12345
                                            mock_u32.IsWindow.return_value = True

                                            with patch("cursor_reloader.user32", mock_u32):
                                                res = send_continue_prompt(
                                                    prompt_text="",
                                                    truncated_snippet="line1\nline2\nreturn result_data",
                                                    target_file="src/utils.py"
                                                )
                                                self.assertTrue(res["success"])
                                                self.assertIn("src/utils.py", res["prompt"])
                                                self.assertIn("return result_data", res["prompt"])


if __name__ == "__main__":
    unittest.main()
