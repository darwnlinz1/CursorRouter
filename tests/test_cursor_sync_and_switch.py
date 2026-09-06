"""
Automated Integration Test Suite: Cursor Bidirectional Sync, 1-Click Launch,
Account Auto-Switching, Auto-Resend Prompt Toggle, and Native Settings Fidelity.
"""

import os
import sys
import json
import sqlite3
import unittest
import tempfile
import shutil
from typing import Dict, Any

from server import app
import cursor_reloader
from cursor_settings import CursorSettingsManager
from account_pool import AccountPoolManager as CursorAccountPool, QUOTA_EXHAUSTION_THRESHOLD
from cursor_storage import CursorStorageManager
from rotating_proxy import RotatingCursorProxy

class TestCursorSyncAndSwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()
        cls.settings_mgr = CursorSettingsManager()
        cls.storage_mgr = CursorStorageManager()
        cls.pool = CursorAccountPool()

    # =========================================================================
    # 1. Bidirectional Sync & App State Detection
    # =========================================================================
    def test_01_cursor_status_detection(self):
        """Test cursor_reloader detection and exe discovery."""
        status = cursor_reloader.get_cursor_status()
        self.assertIsInstance(status, dict)
        self.assertIn("is_running", status)
        self.assertIn("has_window", status)
        self.assertIn("hwnd", status)
        self.assertIn("window_title", status)
        self.assertIn("exe_path", status)
        self.assertIsInstance(status["is_running"], bool)
        self.assertIsInstance(status["has_window"], bool)

    def test_02_api_cursor_app_status(self):
        """Test /api/cursor/app-status endpoint for live bidirectional sync."""
        res = self.client.get("/api/cursor/app-status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("cursor", data)
        self.assertIn("active_account", data)
        self.assertIn("default_model", data)
        self.assertIn("is_proxy_routed", data)
        self.assertIsInstance(data["cursor"]["is_running"], bool)

    def test_03_api_status_bidirectional_payload(self):
        """Test /api/status returning full synchronization payload for dashboard."""
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("cursor_running", data)
        self.assertIn("cursor_window", data)
        self.assertIn("cursor_title", data)
        self.assertIn("cursor_exe", data)
        self.assertIn("cursor_model", data)
        self.assertIn("is_proxy_routed", data)
        self.assertIn("auto_switch_config", data)
        self.assertIsInstance(data["cursor_running"], bool)

    def test_04_api_cursor_launch_endpoint(self):
        """Test /api/cursor/launch endpoint returns valid JSON and execution status."""
        res = self.client.post("/api/cursor/launch")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("success", data)
        self.assertIn("status", data)
        self.assertIn("message", data)

    # =========================================================================
    # 2. Auto-Resend vs Manual Action Configuration
    # =========================================================================
    def test_05_auto_switch_config_crud(self):
        """Test reading and writing auto-switch action config."""
        # 1. Read existing config (default must be manual)
        res = self.client.get("/api/cursor/auto-switch-config")
        self.assertEqual(res.status_code, 200)
        initial_cfg = res.get_json().get("config", {})
        self.assertIn("auto_resend_action", initial_cfg)
        self.assertIn("continue_prompt", initial_cfg)
        self.assertIn("target_mode", initial_cfg)
        self.assertEqual(initial_cfg.get("auto_resend_action"), "manual")

        # 2. Set to 'auto' to verify toggling to auto works
        payload_auto = {
            "auto_resend_action": "auto",
            "continue_prompt": "Tiếp tục code",
            "target_mode": "chat"
        }
        res = self.client.post("/api/cursor/auto-switch-config", json=payload_auto)
        self.assertEqual(res.status_code, 200)
        saved_cfg = res.get_json().get("config", {})
        self.assertEqual(saved_cfg.get("auto_resend_action"), "auto")
        self.assertEqual(saved_cfg.get("continue_prompt"), "Tiếp tục code")
        self.assertEqual(saved_cfg.get("target_mode"), "chat")

        # Verify disk persistence via manager
        mgr_cfg = self.settings_mgr.get_auto_switch_config()
        self.assertEqual(mgr_cfg.get("auto_resend_action"), "auto")
        self.assertEqual(mgr_cfg.get("continue_prompt"), "Tiếp tục code")

        # 3. Restore to 'manual' (the system default)
        payload_manual = {
            "auto_resend_action": "manual",
            "continue_prompt": "Tiếp tục",
            "target_mode": "composer"
        }
        res = self.client.post("/api/cursor/auto-switch-config", json=payload_manual)
        self.assertEqual(res.status_code, 200)
        restored_cfg = res.get_json().get("config", {})
        self.assertEqual(restored_cfg.get("auto_resend_action"), "manual")
        self.assertEqual(restored_cfg.get("continue_prompt"), "Tiếp tục")
        self.assertEqual(restored_cfg.get("target_mode"), "composer")

    def test_06_send_continue_prompt_endpoint(self):
        """Test /api/cursor/send-continue handles requests gracefully."""
        res = self.client.post("/api/cursor/send-continue", json={
            "prompt": "Tiếp tục unit test",
            "target_mode": "composer"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("success", data)
        # If Cursor is open, success is True. If headless, returns handled error.
        if data.get("success"):
            self.assertEqual(data.get("prompt"), "Tiếp tục unit test")
            self.assertEqual(data.get("target"), "composer")
        else:
            self.assertIn("error", data)

    # =========================================================================
    # 3. 100% Native Cursor Settings Fidelity
    # =========================================================================
    def test_07_native_settings_read(self):
        """Test reading full native Cursor settings."""
        res = self.client.get("/api/cursor/settings")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("available_models", data)
        self.assertIn("default_model", data)
        self.assertIn("cpp_enable", data)
        self.assertIn("cpp_partial_accepts", data)
        self.assertIn("composer_auto_apply", data)
        self.assertIn("use_inline_diffs", data)
        self.assertIn("always_search_codebase", data)
        self.assertIn("auto_scroll", data)
        self.assertIn("is_proxy_routed", data)
        self.assertGreaterEqual(len(data["available_models"]), 10)

    def test_08_native_settings_model_update(self):
        """Test updating default model preference across Chat, Composer, and General."""
        original_model = self.settings_mgr.get_settings().get("default_model", "default")
        
        test_model = "claude-3.7-sonnet"
        res = self.client.post("/api/cursor/settings", json={"default_model": test_model})
        self.assertEqual(res.status_code, 200)
        new_settings = res.get_json().get("settings", {})
        self.assertEqual(new_settings.get("default_model"), test_model)

        # Verify in raw settings.json
        raw = self.settings_mgr.get_settings().get("raw", {})
        self.assertEqual(raw.get("cursor.chat.defaultModel"), test_model)
        self.assertEqual(raw.get("cursor.composer.defaultModel"), test_model)
        self.assertEqual(raw.get("cursor.general.model"), test_model)

        # Restore
        self.client.post("/api/cursor/settings", json={"default_model": original_model})

    def test_09_native_features_toggle(self):
        """Test toggling native Cursor features (CPP, Composer auto-apply, inline diffs)."""
        feature_payload = {
            "cpp_enable": True,
            "cpp_partial_accepts": True,
            "composer_auto_apply": True,
            "use_inline_diffs": True,
            "always_search_codebase": False,
            "auto_scroll": True
        }
        res = self.client.post("/api/cursor/settings", json=feature_payload)
        self.assertEqual(res.status_code, 200)
        settings = res.get_json().get("settings", {})
        self.assertTrue(settings.get("cpp_enable"))
        self.assertTrue(settings.get("cpp_partial_accepts"))
        self.assertTrue(settings.get("composer_auto_apply"))
        self.assertTrue(settings.get("use_inline_diffs"))
        self.assertFalse(settings.get("always_search_codebase"))
        self.assertTrue(settings.get("auto_scroll"))

    def test_10_native_proxy_routing_toggle(self):
        """Test enabling and disabling http.proxy routing in Cursor settings.json."""
        # 1. Enable proxy routing
        res = self.client.post("/api/cursor/settings", json={"route_via_proxy": True, "proxy_port": 8999})
        self.assertEqual(res.status_code, 200)
        settings = res.get_json().get("settings", {})
        self.assertTrue(settings.get("is_proxy_routed"))
        self.assertEqual(settings.get("http_proxy"), "http://127.0.0.1:8999")
        self.assertFalse(settings.get("proxy_strict_ssl"))

        # 2. Disable proxy routing
        res = self.client.post("/api/cursor/settings", json={"route_via_proxy": False})
        self.assertEqual(res.status_code, 200)
        settings = res.get_json().get("settings", {})
        self.assertFalse(settings.get("is_proxy_routed"))
        self.assertEqual(settings.get("http_proxy"), "")

    def test_11_hardware_fingerprint_spoofer_and_restore(self):
        """Test hardware ID generation, backup, and restoration."""
        # 1. Read existing hardware IDs
        res = self.client.get("/api/cursor/storage-ids")
        self.assertEqual(res.status_code, 200)
        initial_hw = res.get_json()
        self.assertIn("machineId", initial_hw)
        self.assertIn("macMachineId", initial_hw)
        self.assertIn("devDeviceId", initial_hw)
        self.assertIn("sqmId", initial_hw)

        # 2. Spoof IDs
        res = self.client.post("/api/cursor/storage-ids/spoof")
        self.assertEqual(res.status_code, 200)
        spoofed_hw = res.get_json().get("storage_ids", {})
        self.assertEqual(len(spoofed_hw.get("machineId", "")), 64)
        self.assertEqual(len(spoofed_hw.get("macMachineId", "")), 64)
        self.assertTrue(spoofed_hw.get("has_backup"))

        # 3. Restore IDs from backup
        res = self.client.post("/api/cursor/storage-ids/restore")
        self.assertEqual(res.status_code, 200)
        restored_hw = res.get_json().get("storage_ids", {})
        self.assertEqual(restored_hw.get("machineId"), initial_hw.get("machineId"))

    def test_12_cursorrules_get_and_save(self):
        """Test .cursorrules read and write."""
        res = self.client.get("/api/cursor/cursorrules")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("content", data)
        self.assertIn("template", data)

        test_content = "# Cursor Rules\n- Unit test pass."
        res = self.client.post("/api/cursor/cursorrules", json={"content": test_content})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("success"))

        res = self.client.get("/api/cursor/cursorrules")
        self.assertEqual(res.get_json().get("content"), test_content)

    # =========================================================================
    # 4. Account Auto-Switching & Quota Threshold Enforcement
    # =========================================================================
    def test_13_auto_switch_best_account_selection(self):
        """Test that auto_switch_best_account selects only accounts with usage < 50%."""
        # Find candidate with usage < 50%
        best = self.pool.auto_switch_best_account(notify=False, auto_continue=False)
        if best:
            self.assertLess(best.get("usage_percent", 100), QUOTA_EXHAUSTION_THRESHOLD)
            self.assertNotEqual(best.get("status"), "EXHAUSTED")
            
            # Verify state.vscdb updated with this account
            active = self.storage_mgr.get_active_account()
            self.assertEqual(active.get("email", "").lower(), best.get("email", "").lower())
            self.assertTrue(len(active.get("access_token", "")) > 10)

    def test_14_proxy_in_flight_token_sync_to_storage(self):
        """Test that proxy swapping tokens writes them directly to state.vscdb."""
        temp_dir = tempfile.mkdtemp()
        try:
            temp_vscdb = os.path.join(temp_dir, "state.vscdb")
            mock_acc = {
                "id": 999999,
                "email": "proxy_swap_test@cursor.internal",
                "name": "Proxy Swap Tester",
                "access_token": "test_access_token_proxy_swap_12345",
                "refresh_token": "test_refresh_token_proxy_swap_67890"
            }
            
            proxy_inst = RotatingCursorProxy(state_vscdb_path=temp_vscdb)
            # Directly invoke the synchronization function
            ok = proxy_inst._sync_swapped_token_to_cursor_state(mock_acc, wait=True)
            self.assertTrue(ok)

            # Verify the isolated state.vscdb has the new active account
            temp_storage = CursorStorageManager(db_path=temp_vscdb)
            active = temp_storage.get_active_account()
            self.assertEqual(active.get("email"), mock_acc["email"])
            self.assertEqual(active.get("name"), mock_acc["name"])
            self.assertEqual(active.get("access_token"), mock_acc["access_token"])
            self.assertEqual(active.get("refresh_token"), mock_acc["refresh_token"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

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
    unittest.main(verbosity=2)
