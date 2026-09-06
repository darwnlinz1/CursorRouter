"""
Test Suite: Keyboard Unstick and Security Robustness
=====================================================
Verifies:
1. release_all_modifier_keys safely sends KEYEVENTF_KEYUP for Alt, Ctrl, Shift, Win.
2. force_bring_to_front does NOT press fake Alt down without release.
3. send_continue_prompt isolates strictly to Cursor window and cleans modifiers.
4. /api/unstick-keys and /api/keyboard/unstick endpoints function properly.
5. /api/accounts returns masked_token without exposing full raw access token inadvertently.
6. Concurrency and rate limiting protect against spamming switch and scan endpoints.
"""

import os
import sys
import time
import json
import unittest
from unittest.mock import patch, MagicMock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in [ROOT_DIR, SRC_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

import cursor_reloader
from cursor_reloader import (
    release_all_modifier_keys,
    force_bring_to_front,
    send_continue_prompt,
    KEYEVENTF_KEYUP,
    VK_MENU,
    VK_CONTROL,
    VK_SHIFT,
    VK_LWIN,
    VK_RWIN
)
import server


class TestKeyboardUnstick(unittest.TestCase):
    def test_release_all_modifier_keys_calls_keyup_on_all_modifiers(self):
        """release_all_modifier_keys must issue KEYEVENTF_KEYUP for Alt, Ctrl, Shift, Win."""
        mock_u32 = MagicMock()
        ok = release_all_modifier_keys(u32=mock_u32)
        self.assertTrue(ok)
        self.assertTrue(mock_u32.keybd_event.called)

        # Inspect all calls to keybd_event: (vk, scan, flags, extra)
        calls = mock_u32.keybd_event.call_args_list
        released_vks = set()
        for call in calls:
            args = call[0]
            vk, scan, flags, extra = args
            self.assertEqual(flags, KEYEVENTF_KEYUP, f"Expected KEYEVENTF_KEYUP (0x0002), got {flags}")
            released_vks.add(vk)

        expected_vks = {
            VK_MENU, 0xA4, 0xA5,        # Alt, LAlt, RAlt
            VK_CONTROL, 0xA2, 0xA3,     # Ctrl, LCtrl, RCtrl
            VK_SHIFT, 0xA0, 0xA1,       # Shift, LShift, RShift
            VK_LWIN, VK_RWIN            # LWin, RWin
        }
        for vk in expected_vks:
            self.assertIn(vk, released_vks, f"VK code {hex(vk)} was not released")

    def test_release_all_modifier_keys_handles_exception_gracefully(self):
        """Must return False and not crash if keybd_event throws an error."""
        mock_u32 = MagicMock()
        mock_u32.keybd_event.side_effect = RuntimeError("Win32 error")
        ok = release_all_modifier_keys(u32=mock_u32)
        self.assertFalse(ok)

    def test_force_bring_to_front_does_not_send_fake_alt_down(self):
        """force_bring_to_front must not send keybd_event(0x12, 0, 0, 0) which causes stuck Alt keys."""
        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True
        mock_u32.GetForegroundWindow.return_value = 9999
        mock_u32.GetWindowThreadProcessId.side_effect = lambda hwnd, ptr: 100
        mock_u32.AttachThreadInput.return_value = True

        force_bring_to_front(12345, u32=mock_u32)

        # Verify keybd_event was NEVER called with KEYEVENTF_KEYDOWN (0) for VK_MENU (0x12)
        for call in mock_u32.keybd_event.call_args_list:
            args = call[0]
            vk = args[0]
            flags = args[2]
            if vk in (VK_MENU, 0xA4, 0xA5):
                self.assertNotEqual(flags, 0, "force_bring_to_front must NOT send key down for Alt key!")


class TestSendContinuePromptSafety(unittest.TestCase):
    def test_prompt_not_sent_if_window_not_cursor(self):
        """Keystrokes must not be leaked if the active window does not match genuine Cursor."""
        fake_cursor_hwnd = 12345
        chrome_hwnd = 99999
        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True
        mock_u32.GetForegroundWindow.return_value = chrome_hwnd

        with patch("cursor_reloader.find_cursor_window", return_value=fake_cursor_hwnd), \
             patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == fake_cursor_hwnd), \
             patch("cursor_reloader.force_bring_to_front", return_value=False), \
             patch("cursor_reloader._set_clipboard_text") as mock_set:
            res = send_continue_prompt("Tiếp tục")
            self.assertFalse(res.get("success"))
            mock_u32.keybd_event.assert_not_called()
            mock_set.assert_not_called()

    def test_prompt_cleans_modifiers_when_dispatched(self):
        """When prompt keystrokes are dispatched, modifier keys are guaranteed released in finally block."""
        fake_cursor_hwnd = 12345
        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True
        mock_u32.GetForegroundWindow.return_value = fake_cursor_hwnd

        with patch("cursor_reloader.find_cursor_window", return_value=fake_cursor_hwnd), \
             patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.is_cursor_window", return_value=True), \
             patch("cursor_reloader.force_bring_to_front", return_value=True), \
             patch("cursor_reloader._is_safe_cursor_focused", return_value=True), \
             patch("cursor_reloader._get_clipboard_backup", return_value=(True, "backup")), \
             patch("cursor_reloader._set_clipboard_text", return_value=True), \
             patch("cursor_reloader.release_all_modifier_keys") as mock_release, \
             patch("time.sleep"):
            res = send_continue_prompt("Tiếp tục")
            self.assertTrue(res.get("success"))
            mock_release.assert_called()


class TestServerEndpointsAndSecurity(unittest.TestCase):
    def setUp(self):
        self.app = server.app.test_client()
        self.app.testing = True

    def test_api_unstick_keys_endpoint(self):
        """Both GET and POST on /api/unstick-keys must return success: True."""
        res_post = self.app.post("/api/unstick-keys")
        self.assertEqual(res_post.status_code, 200)
        data_post = json.loads(res_post.data)
        self.assertTrue(data_post.get("success"))
        self.assertIn("giải phóng", data_post.get("message", "").lower())

        res_get = self.app.get("/api/unstick-keys")
        self.assertEqual(res_get.status_code, 200)
        data_get = json.loads(res_get.data)
        self.assertTrue(data_get.get("success"))

    def test_api_keyboard_unstick_endpoint(self):
        """Alias /api/keyboard/unstick must also return success: True."""
        res = self.app.get("/api/keyboard/unstick")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data.get("success"))

    def test_api_accounts_masked_token(self):
        """Accounts returned by /api/accounts must have masked_token."""
        res = self.app.get("/api/accounts")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertIsInstance(data, list)
        if len(data) > 0:
            first = data[0]
            self.assertIn("masked_token", first)
            tok = first.get("access_token")
            if tok and len(tok) > 12:
                self.assertEqual(first["masked_token"], f"{tok[:6]}...{tok[-4:]}")

    def test_api_scan_debounce(self):
        """Rapid calls to /api/scan within debounce window must be throttled gracefully."""
        # First call might scan or be throttled depending on previous run
        res1 = self.app.post("/api/scan")
        self.assertEqual(res1.status_code, 200)
        # Immediate second call must be throttled
        res2 = self.app.post("/api/scan")
        self.assertEqual(res2.status_code, 200)
        data2 = json.loads(res2.data)
        self.assertTrue(data2.get("throttled"))

    def test_api_cursor_send_continue_rate_limited(self):
        """Calling /api/cursor/send-continue rapidly must be rate limited."""
        with patch.object(cursor_reloader, "send_continue_prompt", return_value={"success": True}):
            res1 = self.app.post("/api/cursor/send-continue", json={"hwnd": 0})
            self.assertEqual(res1.status_code, 200)
            res2 = self.app.post("/api/cursor/send-continue", json={"hwnd": 0})
            self.assertEqual(res2.status_code, 429)
            data2 = json.loads(res2.data)
            self.assertTrue(data2.get("throttled"))


class TestTokenCipherSecurity(unittest.TestCase):
    def test_encrypt_and_decrypt_roundtrip(self):
        """Plaintext tokens and Unicode strings must encrypt and decrypt identically."""
        import token_cipher
        raw = "eyJhGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test_payload_123456"
        encrypted = token_cipher.encrypt_token(raw)
        self.assertIsNotNone(encrypted)
        self.assertTrue(encrypted.startswith("enc:v1:"))
        self.assertNotEqual(encrypted, raw)

        decrypted = token_cipher.decrypt_token(encrypted)
        self.assertEqual(decrypted, raw)

        # Unicode / Vietnamese
        unicode_str = "Tài khoản kiểm tra bảo mật & tiếng Việt: 123"
        enc_u = token_cipher.encrypt_token(unicode_str)
        dec_u = token_cipher.decrypt_token(enc_u)
        self.assertEqual(dec_u, unicode_str)

    def test_encrypt_handles_none_empty_and_already_encrypted(self):
        """None, empty, or already encrypted strings must be handled cleanly."""
        import token_cipher
        self.assertIsNone(token_cipher.encrypt_token(None))
        self.assertEqual(token_cipher.encrypt_token(""), "")
        
        enc = token_cipher.encrypt_token("my_token")
        re_enc = token_cipher.encrypt_token(enc)
        self.assertEqual(re_enc, enc)

    def test_decrypt_handles_none_and_legacy_plaintext(self):
        """Legacy plaintext tokens without enc:v1: prefix must return as-is."""
        import token_cipher
        self.assertIsNone(token_cipher.decrypt_token(None))
        self.assertEqual(token_cipher.decrypt_token(""), "")
        legacy = "eyJhbGciOiJIUzI1NiJ9.legacy_token"
        self.assertEqual(token_cipher.decrypt_token(legacy), legacy)

    def test_decrypt_tampered_ciphertext_returns_none(self):
        """Tampered or corrupted ciphertext must fail HMAC integrity verification and return None."""
        import token_cipher
        enc = token_cipher.encrypt_token("sensitive_token_payload")
        tampered = enc[:-2] + ("AA" if not enc.endswith("AA") else "BB")
        self.assertIsNone(token_cipher.decrypt_token(tampered))

    def test_mask_token_variations(self):
        """mask_token must produce clean masked output for both plaintext and encrypted strings."""
        import token_cipher
        self.assertEqual(token_cipher.mask_token(None), "None")
        self.assertEqual(token_cipher.mask_token(""), "None")
        self.assertEqual(token_cipher.mask_token("short"), "••••••••")

        raw = "eyJhGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload"
        self.assertEqual(token_cipher.mask_token(raw), f"{raw[:6]}...{raw[-4:]}")

        enc = token_cipher.encrypt_token(raw)
        self.assertEqual(token_cipher.mask_token(enc), f"{raw[:6]}...{raw[-4:]}")


class TestModifierKeysAndHostActivity(unittest.TestCase):
    def test_get_modifier_keys_status_detects_pressed_keys(self):
        """get_modifier_keys_status must detect pressed modifier keys based on 0x8000 bit."""
        mock_u32 = MagicMock()
        mock_u32.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk == 0x12 else 0
        st = cursor_reloader.get_modifier_keys_status(u32=mock_u32)
        self.assertTrue(st["alt"])
        self.assertFalse(st["ctrl"])
        self.assertFalse(st["shift"])
        self.assertFalse(st["win"])
        self.assertTrue(st["any_stuck"])

    def test_get_modifier_keys_status_all_clean(self):
        """get_modifier_keys_status must report clean when no modifier keys are pressed."""
        mock_u32 = MagicMock()
        mock_u32.GetAsyncKeyState.return_value = 0
        st = cursor_reloader.get_modifier_keys_status(u32=mock_u32)
        self.assertFalse(st["alt"])
        self.assertFalse(st["ctrl"])
        self.assertFalse(st["shift"])
        self.assertFalse(st["win"])
        self.assertFalse(st["any_stuck"])

    def test_is_user_actively_typing_detection(self):
        """is_user_actively_typing correctly identifies active vs idle users."""
        import ctypes
        mock_u32 = MagicMock()
        mock_k32 = MagicMock()
        
        def fake_gli_active(byref_lii):
            byref_lii._obj.dwTime = 10000
            return 1
        mock_u32.GetLastInputInfo.side_effect = fake_gli_active
        mock_k32.GetTickCount.return_value = 10500

        active, idle_s = cursor_reloader.is_user_actively_typing(idle_threshold_sec=2.5, u32=mock_u32, k32=mock_k32)
        self.assertTrue(active)
        self.assertAlmostEqual(idle_s, 0.5, places=1)

        mock_k32.GetTickCount.return_value = 15000
        active, idle_s = cursor_reloader.is_user_actively_typing(idle_threshold_sec=2.5, u32=mock_u32, k32=mock_k32)
        self.assertFalse(active)
        self.assertAlmostEqual(idle_s, 5.0, places=1)


class TestSendContinuePromptHostSafety(unittest.TestCase):
    def test_send_continue_defers_when_user_actively_typing_on_other_window(self):
        """send_continue_prompt must defer execution if user is actively working in another app."""
        cursor_hwnd = 11111
        chrome_hwnd = 22222
        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True
        mock_u32.GetForegroundWindow.return_value = chrome_hwnd

        with patch("cursor_reloader.find_cursor_window", return_value=cursor_hwnd), \
             patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == cursor_hwnd), \
             patch("cursor_reloader.is_user_actively_typing", return_value=(True, 0.4)), \
             patch("cursor_reloader._set_clipboard_text") as mock_set:
            res = send_continue_prompt("Tiếp tục", respect_user_activity=True)
            self.assertFalse(res.get("success"))
            self.assertTrue(res.get("deferred"))
            self.assertIn("da hoan", res.get("error", "").lower())
            mock_set.assert_not_called()

    def test_send_continue_restores_previous_focus(self):
        """send_continue_prompt must restore user's previous foreground window after paste."""
        cursor_hwnd = 11111
        terminal_hwnd = 33333
        mock_u32 = MagicMock()
        mock_u32.IsWindow.return_value = True
        
        # User starts on terminal, switches to cursor upon bring to front
        current_fg = [terminal_hwnd]
        def fake_force_front(h):
            current_fg[0] = h
            return True
        mock_u32.GetForegroundWindow.side_effect = lambda: current_fg[0]

        with patch("cursor_reloader.find_cursor_window", return_value=cursor_hwnd), \
             patch("cursor_reloader.user32", mock_u32), \
             patch("cursor_reloader.is_cursor_window", side_effect=lambda h: h == cursor_hwnd), \
             patch("cursor_reloader.is_user_actively_typing", return_value=(False, 10.0)), \
             patch("cursor_reloader.force_bring_to_front", side_effect=fake_force_front), \
             patch("cursor_reloader._is_safe_cursor_focused", return_value=True), \
             patch("cursor_reloader._get_clipboard_backup", return_value=(True, "orig_text")), \
             patch("cursor_reloader._set_clipboard_text", return_value=True), \
             patch("cursor_reloader._send_key_combo", return_value=True), \
             patch("cursor_reloader._send_single_key", return_value=True), \
             patch("cursor_reloader.release_all_modifier_keys"), \
             patch("cursor_reloader._restore_clipboard", return_value=True) as mock_restore, \
             patch("time.sleep"):
            res = send_continue_prompt("Tiếp tục", restore_previous_focus=True)
            self.assertTrue(res.get("success"))
            mock_u32.SetForegroundWindow.assert_called_with(terminal_hwnd)

    def test_restore_clipboard_preserves_non_text_clipboard(self):
        """_restore_clipboard must never call EmptyClipboard if non-text data (image/files) was present."""
        with patch("subprocess.run") as mock_ps:
            res = cursor_reloader._restore_clipboard(has_backup=False, backup_text=None)
            self.assertTrue(res)
            mock_ps.assert_not_called()


class TestServerDebugAndTokenEndpoints(unittest.TestCase):
    def setUp(self):
        self.app = server.app.test_client()
        self.app.testing = True

    def test_api_debug_system_info_structure(self):
        """GET /api/debug/system-info returns all telemetry subsystems."""
        res = self.app.get("/api/debug/system-info")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data.get("success"))
        for section in ["keyboard", "cursor", "chat_lock", "pool_stats", "security", "recent_events"]:
            self.assertIn(section, data, f"Missing section: {section}")
        self.assertIn("modifiers", data["keyboard"])
        self.assertIn("tokens_encrypted_at_rest", data["security"])
        self.assertTrue(data["security"]["tokens_encrypted_at_rest"])

    def test_api_account_token_decrypts_successfully(self):
        """GET /api/accounts/<id>/token returns decrypted token for legitimate copy."""
        accounts = server.pool.get_all_accounts()
        if accounts:
            first_id = accounts[0]["id"]
            res = self.app.get(f"/api/accounts/{first_id}/token")
            self.assertEqual(res.status_code, 200)
            data = json.loads(res.data)
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("account_id"), first_id)
            if data.get("token"):
                self.assertFalse(data["token"].startswith("enc:v1:"))

    def test_api_account_token_not_found(self):
        """GET /api/accounts/999999/token returns 404."""
        res = self.app.get("/api/accounts/999999/token")
        self.assertEqual(res.status_code, 404)
        data = json.loads(res.data)
        self.assertFalse(data.get("success"))

    def test_api_cursor_hard_restart_lock_and_debounce(self):
        """POST /api/cursor/hard-restart must be throttled with success: True, throttled: True when called within 2.5s."""
        with patch.object(cursor_reloader, "hard_restart_cursor", return_value={"success": True, "method": "hard_restart"}):
            res1 = self.app.post("/api/cursor/hard-restart")
            self.assertEqual(res1.status_code, 200)
            res2 = self.app.post("/api/cursor/hard-restart")
            self.assertEqual(res2.status_code, 200)
            data2 = json.loads(res2.data)
            self.assertTrue(data2.get("throttled"))


if __name__ == "__main__":
    unittest.main()
