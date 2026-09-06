import json
import unittest
from unittest.mock import patch
from server import app, pool

class TestCursorSettingsEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_cursor_settings_get(self):
        res = self.client.get('/api/cursor/settings')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn('default_model', data)
        self.assertIn('available_models', data)
        self.assertIn('privacy_mode', data)

    def test_cursor_storage_ids_get(self):
        res = self.client.get('/api/cursor/storage-ids')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn('machineId', data)
        self.assertIn('macMachineId', data)
        self.assertIn('devDeviceId', data)

    def test_cursor_rules_get(self):
        res = self.client.get('/api/cursor/cursorrules')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn('content', data)
        self.assertIn('template', data)

    def test_status_endpoint_returns_active(self):
        with patch.object(pool.storage, "get_active_account", return_value={"email": "mock_active@cursor.sh", "has_token": True}):
            res = self.client.get('/api/status')
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertIn('email', data)
            self.assertEqual(data.get('email'), 'mock_active@cursor.sh')

    def test_cursor_network_test(self):
        res = self.client.get('/api/cursor/network-test')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertIn('backend_url', data)
        self.assertIn('latency_ms', data)
        self.assertIn('proxy_running', data)

if __name__ == '__main__':
    unittest.main()
