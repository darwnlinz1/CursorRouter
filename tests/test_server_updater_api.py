"""
Integration Test Suite for Server Auto-Updater Endpoints
========================================================
Tests:
1. GET /api/updater/status returns status dictionary.
2. POST /api/updater/check triggers update check.
3. POST /api/updater/download handles available vs no-update cases.
4. POST /api/updater/apply triggers detached updater.
"""

import os
import sys
import json
import unittest
from unittest.mock import patch, MagicMock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import server
from updater import UpdateInfo, UpdaterState


class TestServerUpdaterAPI(unittest.TestCase):
    def setUp(self):
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def test_01_api_updater_status(self):
        resp = self.client.get("/api/updater/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("state", data)
        self.assertIn("current_version", data)

    def test_02_api_updater_check(self):
        mock_info = UpdateInfo(
            current_version="2.4.0",
            latest_version="2.5.0",
            update_available=True,
            download_url="https://example.com/test.zip",
            sha256="abc",
            changelog="Update details"
        )
        with patch("updater.AutoUpdater.check_for_updates", return_value=mock_info):
            resp = self.client.post("/api/updater/check")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("success"))
            self.assertTrue(data.get("has_update"))

    def test_03_api_updater_download_success(self):
        mock_info = UpdateInfo(
            current_version="2.4.0",
            latest_version="2.5.0",
            update_available=True,
            download_url="https://example.com/test.zip",
            sha256="abc",
            changelog="Update details"
        )
        with patch("updater.AutoUpdater.check_for_updates", return_value=mock_info):
            with patch("updater.AutoUpdater.last_info", mock_info):
                with patch("updater.AutoUpdater.download_update", return_value="/tmp/test.zip"):
                    resp = self.client.post("/api/updater/download")
                    self.assertEqual(resp.status_code, 200)
                    data = resp.get_json()
                    self.assertTrue(data.get("success"))
                    self.assertEqual(data.get("staged_path"), "/tmp/test.zip")

    def test_04_api_updater_download_no_update(self):
        with patch("updater.AutoUpdater.check_for_updates", return_value=None):
            with patch("updater.AutoUpdater.last_info", None):
                resp = self.client.post("/api/updater/download")
                self.assertEqual(resp.status_code, 400)
                data = resp.get_json()
                self.assertFalse(data.get("success"))

    def test_05_api_updater_apply(self):
        mock_res = {"success": True, "message": "Detached update process spawned."}
        with patch("updater.AutoUpdater.apply_update", return_value=mock_res):
            resp = self.client.post("/api/updater/apply")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("success"))


if __name__ == "__main__":
    unittest.main()
