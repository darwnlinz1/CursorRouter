"""
Comprehensive End-to-End Test Suite for Cursor Manager
======================================================
Tests:
1. Account Sync & Transfer Accuracy:
   - get_active_account accurately retrieves '13vladoprea@gmail.com'
   - sync_active_from_cursor syncs with cursor_accounts.db with is_active: True
   - switch_to_account updates state.vscdb and maintains is_active in pool
   - auto-rotate respects manual switches within cooldown period
2. Cursor Settings & Hardware ID Management:
   - Read & update settings.json (model preference, privacy mode, ghost mode)
   - Read, spoof, and restore storage.json machine IDs
   - Read & save .cursorrules
3. Flask Endpoints & Template Integration:
   - All /api/* endpoints return 200 with valid schema
   - HTML template contains all Cursor Manager elements
"""

import os
import sys
import json
import unittest
import tempfile
import shutil
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(__file__))

from server import app, pool, settings_manager
from cursor_storage import CursorStorageManager, decode_jwt_payload
from cursor_settings import CursorSettingsManager
import account_pool

class TestCursorManagerFullSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()
        pool.switch_to_account(1, auto_reload=False)

    @classmethod
    def tearDownClass(cls):
        pool.switch_to_account(1, auto_reload=False)

    def test_01_jwt_decoding_utility(self):
        """Test standalone JWT payload decoding."""
        # Standard test token
        # header: {"alg":"HS256","typ":"JWT"}
        # payload: {"sub":"google-oauth2|user_test123","email":"testuser@gmail.com","exp":1890000000}
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJnb29nbGUtb2F1dGgyfHVzZXJfdGVzdDEyMyIsImVtYWlsIjoidGVzdHVzZXJAZ21haWwuY29tIiwiZXhwIjoxODkwMDAwMDAwfQ.signature"
        decoded = decode_jwt_payload(token)
        self.assertEqual(decoded.get("sub"), "google-oauth2|user_test123")
        self.assertEqual(decoded.get("email"), "testuser@gmail.com")
        self.assertEqual(decoded.get("exp"), 1890000000)

        # Invalid token returns empty dict
        self.assertEqual(decode_jwt_payload("invalid-token"), {})
        self.assertEqual(decode_jwt_payload(None), {})

    def test_02_active_account_detection_13vladoprea(self):
        """Test that get_active_account accurately detects the user's active account."""
        active = pool.storage.get_active_account()
        self.assertIsNotNone(active.get("email"))
        self.assertEqual(active.get("email"), "13vladoprea@gmail.com")
        self.assertTrue(active.get("has_token"))

    def test_03_sync_active_from_cursor(self):
        """Test sync_active_from_cursor syncs with DB and returns is_active."""
        synced = pool.sync_active_from_cursor()
        self.assertIsNotNone(synced)
        self.assertEqual(synced.get("email"), "13vladoprea@gmail.com")
        self.assertTrue(synced.get("is_active"))

        # Verify in get_all_accounts
        all_accounts = pool.get_all_accounts()
        matching = [a for a in all_accounts if a.get("email") == "13vladoprea@gmail.com"]
        self.assertTrue(len(matching) >= 1)
        self.assertTrue(matching[0].get("is_active"))

    def test_04_switch_to_account_reliability(self):
        """Test switch_to_account writes keys and updates active tracking."""
        # Switch to account 1 without triggering GUI reload in test
        ok = pool.switch_to_account(1, auto_reload=False)
        self.assertTrue(ok)
        self.assertEqual(pool.manual_active_account_id, 1)

        # Check in storage
        active = pool.storage.get_active_account()
        self.assertEqual(active.get("email"), "13vladoprea@gmail.com")

    def test_05_cursor_settings_manager(self):
        """Test CursorSettingsManager functions."""
        settings = settings_manager.get_settings()
        self.assertIn("default_model", settings)
        self.assertIn("available_models", settings)

        # Test updating model
        updated = settings_manager.update_settings({"default_model": "claude-3.5-sonnet"})
        self.assertEqual(updated.get("default_model"), "claude-3.5-sonnet")

        # Test updating privacy mode
        updated_priv = settings_manager.update_settings({"privacy_mode": True})
        self.assertTrue(updated_priv.get("privacy_mode"))
        self.assertEqual(updated_priv.get("telemetry_level"), "off")

    def test_06_storage_ids_spoof_and_restore(self):
        """Test hardware ID spoofing and backup restoration."""
        ids_before = settings_manager.get_storage_ids()
        self.assertTrue(ids_before.get("exists"))
        orig_machine = ids_before.get("machineId")
        self.assertTrue(len(orig_machine) > 0)

        # Spoof IDs
        spoofed = settings_manager.spoof_storage_ids()
        self.assertNotEqual(spoofed.get("machineId"), orig_machine)
        self.assertEqual(len(spoofed.get("machineId")), 64)
        self.assertEqual(len(spoofed.get("macMachineId")), 64)
        self.assertTrue(spoofed.get("has_backup"))

        # Restore IDs
        restored = settings_manager.restore_storage_ids()
        self.assertTrue(restored)
        ids_after = settings_manager.get_storage_ids()
        self.assertEqual(ids_after.get("machineId"), orig_machine)

    def test_07_cursorrules_get_and_save(self):
        """Test .cursorrules retrieval and persistence."""
        rules = settings_manager.get_cursorrules()
        self.assertIn("content", rules)
        self.assertIn("path", rules)

        test_content = "# Cursor Test Rules\n- Be awesome.\n"
        save_res = settings_manager.save_cursorrules(test_content)
        self.assertTrue(save_res.get("success"))

        read_back = settings_manager.get_cursorrules()
        self.assertEqual(read_back.get("content"), test_content)

    def test_08_flask_api_endpoints(self):
        """Test Flask REST API routes."""
        # GET /api/status
        res = self.client.get('/api/status')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("email"), "13vladoprea@gmail.com")

        # GET /api/stats
        res = self.client.get('/api/stats')
        self.assertEqual(res.status_code, 200)
        stats = res.get_json()
        self.assertIn("total", stats)
        self.assertIn("ready", stats)
        self.assertIn("exhausted", stats)

        # GET /api/cursor/settings
        res = self.client.get('/api/cursor/settings')
        self.assertEqual(res.status_code, 200)

        # POST /api/cursor/settings
        res = self.client.post('/api/cursor/settings', json={"default_model": "claude-3.5-sonnet"})
        self.assertEqual(res.status_code, 200)

        # GET /api/cursor/storage-ids
        res = self.client.get('/api/cursor/storage-ids')
        self.assertEqual(res.status_code, 200)

        # GET /api/cursor/cursorrules
        res = self.client.get('/api/cursor/cursorrules')
        self.assertEqual(res.status_code, 200)

        # POST /api/switch/1
        res = self.client.post('/api/switch/1?reload=false')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json().get("success"))

    def test_09_html_template_elements(self):
        """Test index.html template contains modern Cursor Manager components."""
        res = self.client.get('/')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        # Branding
        self.assertIn("Cursor Manager", html)
        
        # Hero card elements
        self.assertIn("id=\"active-banner\"", html)
        self.assertIn("id=\"active-email\"", html)
        self.assertIn("id=\"active-quota-bar\"", html)
        self.assertIn("id=\"active-quota-text\"", html)

        # 4-card metric grid
        self.assertIn("id=\"stat-total\"", html)
        self.assertIn("id=\"stat-ready\"", html)
        self.assertIn("id=\"stat-high\"", html)
        self.assertIn("id=\"stat-pending\"", html)

        # Segmented Filter Bar
        self.assertIn("filter-btn", html)
        self.assertIn("filter-count-all", html)
        self.assertIn("filter-count-ready", html)
        self.assertIn("filter-count-high", html)

        # Cursor Settings Modal & components
        self.assertIn("id=\"cursor-settings-modal\"", html)
        self.assertIn("id=\"select-default-model\"", html)
        self.assertIn("id=\"toggle-privacy-mode\"", html)
        self.assertIn("id=\"hw-machine-id\"", html)
        self.assertIn("id=\"cursorrules-textarea\"", html)


if __name__ == "__main__":
    unittest.main()
