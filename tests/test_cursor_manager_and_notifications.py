import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import cursor_manager
import windows_notifier


class TestCursorManagerCli(unittest.TestCase):
    def test_01_cli_arg_parser_definitions(self):
        """CursorManager arg parser includes all operational commands."""
        import argparse
        parser = argparse.ArgumentParser()
        # Parse -h via subprocess to ensure clean exit code 0
        import subprocess
        script_path = os.path.join(os.path.dirname(__file__), "..", "src", "cursor_manager.py")
        if not os.path.exists(script_path):
            script_path = "cursor_manager.py"
        res = subprocess.run([sys.executable, script_path, "-h"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("--status", res.stdout)
        self.assertIn("--sync-profile", res.stdout)
        self.assertIn("--serve", res.stdout)
        self.assertIn("--scan", res.stdout)
        self.assertIn("--auto-switch", res.stdout)
        self.assertIn("--test-cookie", res.stdout)
        self.assertIn("--inject-only", res.stdout)
        self.assertIn("--clean-exhausted", res.stdout)

    @patch("cursor_manager.CursorStorageManager")
    @patch("cursor_manager.AccountPoolManager")
    def test_02_cli_status_action(self, mock_pool_cls, mock_storage_cls):
        """--status queries get_active_account and prints current state."""
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.get_active_account.return_value = {
            "email": "user@example.com",
            "displayName": "User Example",
            "authId": "auth0|12345",
            "has_token": True,
            "membership_type": "Pro"
        }
        mock_storage.db_path = "mock/path/state.vscdb"

        with patch.object(sys, "argv", ["cursor_manager.py", "--status"]):
            with patch("builtins.print") as mock_print:
                cursor_manager.main()
                printed = " ".join([call.args[0] for call in mock_print.call_args_list if call.args])
                self.assertIn("user@example.com", printed)
                self.assertIn("User Example", printed)
                self.assertIn("Pro", printed)

    @patch("cursor_manager.CursorStorageManager")
    @patch("cursor_manager.AccountPoolManager")
    def test_03_cli_sync_profile_action(self, mock_pool_cls, mock_storage_cls):
        """--sync-profile calls inject_full_profile when access token is found."""
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.get_active_account.side_effect = [
            {"access_token": "valid_token", "refresh_token": "valid_refresh"},
            {"email": "synced@example.com", "displayName": "Synced User", "authId": "auth|999"}
        ]
        mock_storage.inject_full_profile.return_value = True

        with patch.object(sys, "argv", ["cursor_manager.py", "--sync-profile"]):
            with patch("builtins.print") as mock_print:
                cursor_manager.main()
                mock_storage.inject_full_profile.assert_called_once_with("valid_token", "valid_refresh")
                printed = " ".join([call.args[0] for call in mock_print.call_args_list if call.args])
                self.assertIn("synced@example.com", printed)

    @patch("cursor_manager.CursorStorageManager")
    @patch("cursor_manager.AccountPoolManager")
    def test_04_cli_scan_action(self, mock_pool_cls, mock_storage_cls):
        """--scan invokes pool.scan_cookies_folder."""
        mock_pool = MagicMock()
        mock_pool_cls.return_value = mock_pool
        mock_pool.scan_cookies_folder.return_value = 5

        with patch.object(sys, "argv", ["cursor_manager.py", "--scan"]):
            with patch("builtins.print") as mock_print:
                cursor_manager.main()
                mock_pool.scan_cookies_folder.assert_called_once()
                printed = " ".join([call.args[0] for call in mock_print.call_args_list if call.args])
                self.assertIn("Them moi 5 tai khoan", printed)

    @patch("cursor_manager.CursorStorageManager")
    @patch("cursor_manager.AccountPoolManager")
    def test_05_cli_auto_switch_action(self, mock_pool_cls, mock_storage_cls):
        """--auto-switch invokes pool.auto_switch_best_account."""
        mock_pool = MagicMock()
        mock_pool_cls.return_value = mock_pool
        mock_pool.auto_switch_best_account.return_value = {
            "email": "best@test.com",
            "usage_percent": 15.5
        }

        with patch.object(sys, "argv", ["cursor_manager.py", "--auto-switch"]):
            with patch("builtins.print") as mock_print:
                cursor_manager.main()
                mock_pool.auto_switch_best_account.assert_called_once()
                printed = " ".join([call.args[0] for call in mock_print.call_args_list if call.args])
                self.assertIn("best@test.com", printed)
                self.assertIn("15.5%", printed)

    @patch("cursor_manager.CursorStorageManager")
    @patch("cursor_manager.AccountPoolManager")
    def test_06_cli_inject_only_action(self, mock_pool_cls, mock_storage_cls):
        """--inject-only calls storage.inject_full_profile with provided token."""
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage

        raw_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy_token_payload"
        with patch.object(sys, "argv", ["cursor_manager.py", "--inject-only", raw_token]):
            with patch("builtins.print"):
                cursor_manager.main()
                mock_storage.inject_full_profile.assert_called_once_with(raw_token)

    @patch("cursor_manager.CursorStorageManager")
    @patch("cursor_manager.AccountPoolManager")
    def test_07_cli_clean_exhausted_action(self, mock_pool_cls, mock_storage_cls):
        """--clean-exhausted calls pool.delete_exhausted_accounts."""
        mock_pool = MagicMock()
        mock_pool_cls.return_value = mock_pool
        mock_pool.QUOTA_EXHAUSTION_THRESHOLD = 50.0
        mock_pool.delete_exhausted_accounts.return_value = 3

        with patch.object(sys, "argv", ["cursor_manager.py", "--clean-exhausted"]):
            with patch("builtins.print") as mock_print:
                cursor_manager.main()
                mock_pool.delete_exhausted_accounts.assert_called_once()
                printed = " ".join([call.args[0] for call in mock_print.call_args_list if call.args])
                self.assertIn("3 tai khoan", printed)

    @patch("cursor_manager.CursorAuthClient")
    @patch("cursor_manager.CursorStorageManager")
    @patch("cursor_manager.AccountPoolManager")
    def test_08_cli_test_cookie_action(self, mock_pool_cls, mock_storage_cls, mock_auth_cls):
        """--test-cookie attempts exchange and declines injection upon prompt."""
        mock_client = MagicMock()
        mock_auth_cls.return_value = mock_client
        mock_client.exchange_cookie_to_tokens.return_value = {
            "accessToken": "ey_access_token_1234567890",
            "refreshToken": "refresh_token_12345"
        }

        with patch.object(sys, "argv", ["cursor_manager.py", "--test-cookie", "test_cookie_string"]):
            with patch("builtins.input", return_value="n"):
                with patch("builtins.print") as mock_print:
                    cursor_manager.main()
                    mock_client.exchange_cookie_to_tokens.assert_called_once_with("test_cookie_string")
                    printed = " ".join([call.args[0] for call in mock_print.call_args_list if call.args])
                    self.assertIn("DOI TOKEN THANH CONG", printed)


class TestWindowsNotifierExtended(unittest.TestCase):
    @patch("subprocess.run")
    def test_01_xml_escaping_in_powershell_toast(self, mock_run):
        """Special XML characters in title and message are properly escaped."""
        mock_run.return_value = MagicMock(returncode=0)

        with patch("sys.platform", "win32"):
            ok = windows_notifier._run_powershell_toast(
                title="Account <Alert> & Notice",
                message="Switched from 'User 1' to \"User 2\" (Quota: 25% > 10%)"
            )
            self.assertTrue(ok)
            self.assertTrue(mock_run.called)

            # Check that encoded PowerShell script decoded contains escaped entities
            call_args = mock_run.call_args[0][0]
            encoded_cmd = call_args[call_args.index("-EncodedCommand") + 1]
            import base64
            decoded_ps = base64.b64decode(encoded_cmd).decode("utf-16le")
            self.assertIn("&lt;Alert&gt;", decoded_ps)
            self.assertIn("&amp;", decoded_ps)
            self.assertIn("&gt;", decoded_ps)

    @patch("windows_notifier._run_powershell_toast", return_value=True)
    def test_02_windows_notifier_sync_and_async(self, mock_toast):
        """send_windows_notification works both asynchronously and synchronously."""
        with patch("sys.platform", "win32"):
            # Synchronous
            res_sync = windows_notifier.send_windows_notification("Title", "Msg", async_exec=False)
            self.assertTrue(res_sync)
            mock_toast.assert_called_once_with("Title", "Msg", "Cursor Manager")

            # Asynchronous
            res_async = windows_notifier.send_windows_notification("Title 2", "Msg 2", async_exec=True)
            self.assertTrue(res_async)

    def test_03_windows_notifier_non_windows_handling(self):
        """Non-Windows platforms return False gracefully without attempting execution."""
        with patch("sys.platform", "linux"):
            res = windows_notifier.send_windows_notification("Title", "Msg")
            self.assertFalse(res)

            res_toast = windows_notifier._run_powershell_toast("Title", "Msg")
            self.assertFalse(res_toast)

    @patch("subprocess.run", side_effect=Exception("Subprocess failed"))
    def test_04_windows_notifier_exception_handling(self, mock_run):
        """Exceptions in subprocess.run are caught safely and return False."""
        with patch("sys.platform", "win32"):
            res = windows_notifier._run_powershell_toast("Title", "Msg")
            self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
