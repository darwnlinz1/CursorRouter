"""
Unit Test Suite: Safe Cursor Prompt Dispatching, Clipboard Preservation, & Always Mode
======================================================================================
Verifies:
1. Setting auto_resend_action to 'always' or 'force' triggers auto-continue unconditionally.
2. cursor_reloader.is_cursor_window strictly validates process name 'cursor.exe' and class 'Chrome_WidgetWin_1'.
3. cursor_reloader.send_continue_prompt aborts safely when Cursor cannot be found or focused.
4. cursor_reloader.send_continue_prompt aborts if foreground window is not Cursor before paste.
5. System clipboard is backed up and faithfully restored so user clipboard is never corrupted.
"""

import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

# Ensure root directory is in sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from smart_task_filter import SmartTaskCompletionFilter, TaskStateTracker
import cursor_reloader


class TestAlwaysSettingAndTaskFilter(unittest.TestCase):
    def setUp(self):
        TaskStateTracker().reset()
        self.filter = SmartTaskCompletionFilter()

    def tearDown(self):
        TaskStateTracker().reset()

    def test_always_mode_triggers_without_interruption(self):
        """When auto_resend_cfg is 'always', should_auto_continue returns True even in idle state."""
        TaskStateTracker().reset()
        should_cont, reason = self.filter.should_auto_continue(
            auto_continue=None,
            auto_resend_cfg="always"
        )
        self.assertTrue(should_cont, "always setting must trigger continuation even when idle")
        self.assertIn("always", reason.lower())

    def test_force_mode_triggers_without_interruption(self):
        """When auto_resend_cfg is 'force', should_auto_continue returns True."""
        TaskStateTracker().reset()
        should_cont, reason = self.filter.should_auto_continue(
            auto_continue=None,
            auto_resend_cfg="force"
        )
        self.assertTrue(should_cont, "force setting must trigger continuation")
        self.assertIn("force", reason.lower())

    def test_explicit_false_overrides_always_setting(self):
        """When caller explicitly passes auto_continue=False, it overrides 'always' setting."""
        should_cont, reason = self.filter.should_auto_continue(
            auto_continue=False,
            auto_resend_cfg="always"
        )
        self.assertFalse(should_cont, "Explicit auto_continue=False must override 'always'")
        self.assertIn("explicitly suppressed", reason.lower())

    def test_auto_mode_without_interruption_suppresses(self):
        """In default 'auto' mode without any interruption, prompt resend is suppressed."""
        should_cont, reason = self.filter.should_auto_continue(
            auto_continue=None,
            auto_resend_cfg="auto",
            max_age_seconds=0.01
        )
        self.assertFalse(should_cont)
        self.assertIn("suppressed", reason.lower())


class TestCursorWindowTargetingAndClipboardSafety(unittest.TestCase):
    def test_is_cursor_window_valid_cursor_process(self):
        """is_cursor_window returns True only when process is cursor.exe and class is Chrome_WidgetWin_1."""
        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True

        def mock_get_pid(hwnd, byref_pid):
            byref_pid._obj.value = 1234
            return 1234
        mock_u32.GetWindowThreadProcessId.side_effect = mock_get_pid

        mock_proc = MagicMock()
        mock_proc.name.return_value = "Cursor.exe"

        mock_psutil = MagicMock()
        mock_psutil.Process.return_value = mock_proc

        with patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.psutil", mock_psutil), \
             patch("cursor_reloader.ctypes.create_unicode_buffer") as mock_buf:
            mock_buf_instance = MagicMock()
            mock_buf_instance.value = "Chrome_WidgetWin_1"
            mock_buf.return_value = mock_buf_instance

            self.assertTrue(cursor_reloader.is_cursor_window(12345))

    def test_is_cursor_window_rejects_other_processes(self):
        """is_cursor_window returns False if process is chrome.exe, code.exe, or powershell."""
        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True

        for bad_pname in ("chrome.exe", "code.exe", "powershell.exe", "explorer.exe"):
            mock_proc = MagicMock()
            mock_proc.name.return_value = bad_pname
            mock_psutil = MagicMock()
            mock_psutil.Process.return_value = mock_proc

            with patch("cursor_reloader.user32", mock_u32), \
                 patch("cursor_reloader.psutil", mock_psutil):
                self.assertFalse(
                    cursor_reloader.is_cursor_window(12345),
                    f"Process {bad_pname} must NOT be accepted as Cursor window"
                )

    def test_send_continue_prompt_aborts_when_no_cursor_window(self):
        """send_continue_prompt returns error immediately if find_cursor_window returns None."""
        with patch("cursor_reloader.find_cursor_window", return_value=None):
            res = cursor_reloader.send_continue_prompt("Tiếp tục")
            self.assertFalse(res["success"])
            self.assertIn("Khong tim thay cua so Cursor", res["error"])

    def test_send_continue_prompt_aborts_when_hwnd_not_cursor(self):
        """send_continue_prompt aborts if target HWND is not a genuine Cursor window."""
        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True

        with patch("cursor_reloader.find_cursor_window", return_value=99999), \
             patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.is_cursor_window", return_value=False):
            res = cursor_reloader.send_continue_prompt("Tiếp tục")
            self.assertFalse(res["success"])
            self.assertIn("khong phai la Cursor IDE", res["error"])

    def test_send_continue_prompt_aborts_when_focus_fails(self):
        """send_continue_prompt does not paste or send keys if Cursor cannot be brought to foreground."""
        target_hwnd = 88888
        other_hwnd = 11111

        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True
        mock_u32.GetForegroundWindow.return_value = other_hwnd

        with patch("cursor_reloader.find_cursor_window", return_value=target_hwnd), \
             patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == target_hwnd), \
             patch("cursor_reloader.force_bring_to_front", return_value=False), \
             patch("cursor_reloader._set_clipboard_text") as mock_set_clip:
            res = cursor_reloader.send_continue_prompt("Tiếp tục")
            self.assertFalse(res["success"])
            self.assertIn("Khong the focus", res["error"])
            # Clipboard must NOT be touched
            mock_set_clip.assert_not_called()

    def test_send_continue_prompt_aborts_if_focus_lost_before_paste(self):
        """If user clicks another window before Ctrl+V, send_continue_prompt aborts immediately."""
        fake_hwnd = 77777
        other_hwnd = 66666

        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True
        mock_u32.GetForegroundWindow.return_value = other_hwnd

        with patch("cursor_reloader.find_cursor_window", return_value=fake_hwnd), \
             patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == fake_hwnd), \
             patch("cursor_reloader.force_bring_to_front", return_value=True), \
             patch("cursor_reloader._get_clipboard_backup", return_value=(True, "user_secret_data")), \
             patch("cursor_reloader._set_clipboard_text", return_value=True), \
             patch("cursor_reloader._restore_clipboard") as mock_restore:
            res = cursor_reloader.send_continue_prompt("Tiếp tục")
            self.assertFalse(res["success"])
            self.assertIn("mat focus truoc khi paste", res["error"])
            # Keystrokes like Ctrl+V must NOT have been sent
            mock_u32.keybd_event.assert_not_called()
            # Original clipboard must have been restored
            mock_restore.assert_called_with(True, "user_secret_data")

    def test_send_continue_prompt_preserves_user_clipboard_on_success(self):
        """On successful prompt send, original clipboard content is restored in finally block."""
        fake_hwnd = 55555

        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True
        mock_u32.GetForegroundWindow.return_value = fake_hwnd

        with patch("cursor_reloader.find_cursor_window", return_value=fake_hwnd), \
             patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.is_cursor_window", return_value=True), \
             patch("cursor_reloader.force_bring_to_front", return_value=True), \
             patch("cursor_reloader._get_clipboard_backup", return_value=(True, "original_user_clipboard_text")), \
             patch("cursor_reloader._set_clipboard_text", return_value=True), \
             patch("cursor_reloader._restore_clipboard") as mock_restore:
            res = cursor_reloader.send_continue_prompt("Tiếp tục")
            self.assertTrue(res["success"])
            self.assertEqual(res["prompt"], "Tiếp tục")
            # Must faithfully restore user clipboard
            mock_restore.assert_called_with(True, "original_user_clipboard_text")


if __name__ == "__main__":
    unittest.main()
