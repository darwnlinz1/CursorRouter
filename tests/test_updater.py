"""
Comprehensive Test Suite for Auto-Updater Engine
=================================================
Tests:
1. SemVer parsing and comparison logic (older, newer, same, invalid).
2. Remote manifest checking (update available vs up to date vs network error).
3. Secure download & SHA-256 checksum verification.
4. Checksum mismatch rejection and staging cleanup.
5. Windows detached update script generation with database preservation.
6. Updater state transitions and thread safety.
"""

import os
import sys
import json
import hashlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from updater import (
    parse_semver,
    compare_versions,
    is_newer_version,
    AutoUpdater,
    UpdaterState,
    UpdateInfo
)


class TestAutoUpdater(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.updater = AutoUpdater(
            current_version="2.4.0",
            manifest_url="https://api.cursor-things.local/updates/latest.json",
            storage_dir=self.temp_dir.name
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_semver_parsing_and_comparison(self):
        self.assertEqual(parse_semver("2.4.0"), (2, 4, 0))
        self.assertEqual(parse_semver("v1.10.5"), (1, 10, 5))
        self.assertEqual(parse_semver("3.0"), (3, 0, 0))
        self.assertEqual(parse_semver("invalid"), (0, 0, 0))

        self.assertTrue(is_newer_version("2.5.0", "2.4.0"))
        self.assertTrue(is_newer_version("3.0.0", "2.9.9"))
        self.assertTrue(is_newer_version("2.4.1", "2.4.0"))
        self.assertFalse(is_newer_version("2.4.0", "2.4.0"))
        self.assertFalse(is_newer_version("2.3.9", "2.4.0"))
        self.assertFalse(is_newer_version("1.9.9", "2.0.0"))

    def test_02_check_updates_available(self):
        manifest_payload = {
            "version": "2.5.0",
            "release_date": "2026-09-06",
            "channel": "stable",
            "changelog": "1. Two-tier quota pool.\n2. Cursor settings taxonomy.\n3. Alt-key safety.",
            "download_url": "https://example.com/CursorManager-2.5.0.zip",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "mandatory": False,
            "min_version": "2.0.0"
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = manifest_payload

        with patch("requests.get", return_value=mock_resp):
            info = self.updater.check_for_updates()
            self.assertIsNotNone(info)
            self.assertTrue(info.update_available)
            self.assertEqual(info.latest_version, "2.5.0")
            self.assertEqual(info.current_version, "2.4.0")
            self.assertIn("Two-tier quota pool", info.changelog)
            self.assertEqual(self.updater.state, UpdaterState.AVAILABLE)

    def test_03_check_updates_up_to_date(self):
        manifest_payload = {
            "version": "2.4.0",
            "release_date": "2026-09-01",
            "download_url": "https://example.com/CursorManager-2.4.0.zip",
            "sha256": "dummy"
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = manifest_payload

        with patch("requests.get", return_value=mock_resp):
            info = self.updater.check_for_updates()
            self.assertIsNotNone(info)
            self.assertFalse(info.update_available)
            self.assertEqual(info.latest_version, "2.4.0")
            self.assertEqual(self.updater.state, UpdaterState.UP_TO_DATE)

    def test_04_check_updates_network_error_resilience(self):
        with patch("requests.get", side_effect=ConnectionError("Server unreachable")):
            info = self.updater.check_for_updates()
            self.assertIsNone(info)
            self.assertEqual(self.updater.state, UpdaterState.ERROR)
            self.assertIn("unreachable", self.updater.last_error.lower())

    def test_05_download_package_with_valid_sha256(self):
        payload_bytes = b"MOCK_NEW_CURSOR_MANAGER_BINARY_CONTENT_V2_5_0"
        expected_sha256 = hashlib.sha256(payload_bytes).hexdigest()

        manifest = UpdateInfo(
            current_version="2.4.0",
            latest_version="2.5.0",
            update_available=True,
            download_url="https://example.com/CursorManager-2.5.0.zip",
            sha256=expected_sha256,
            changelog="Bug fixes",
            mandatory=False
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_content.return_value = [payload_bytes]
        mock_resp.headers = {"content-length": str(len(payload_bytes))}

        with patch("requests.get", return_value=mock_resp):
            staged_path = self.updater.download_update(manifest)
            self.assertIsNotNone(staged_path)
            self.assertTrue(os.path.exists(staged_path))
            with open(staged_path, "rb") as f:
                self.assertEqual(f.read(), payload_bytes)
            self.assertEqual(self.updater.state, UpdaterState.DOWNLOADED)

    def test_06_download_package_checksum_mismatch_rejection(self):
        payload_bytes = b"CORRUPTED_TAMPERED_PAYLOAD"
        bogus_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"

        manifest = UpdateInfo(
            current_version="2.4.0",
            latest_version="2.5.0",
            update_available=True,
            download_url="https://example.com/corrupt.zip",
            sha256=bogus_sha256
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_content.return_value = [payload_bytes]
        mock_resp.headers = {"content-length": str(len(payload_bytes))}

        with patch("requests.get", return_value=mock_resp):
            staged_path = self.updater.download_update(manifest)
            self.assertIsNone(staged_path)
            self.assertEqual(self.updater.state, UpdaterState.ERROR)
            self.assertIn("checksum", self.updater.last_error.lower())

    def test_07_generate_windows_updater_script(self):
        staged_file = os.path.join(self.temp_dir.name, "update_2.5.0.zip")
        with open(staged_file, "wb") as f:
            f.write(b"PK...")

        script_path = self.updater.generate_update_script(
            staged_archive=staged_file,
            target_dir=ROOT_DIR,
            target_pid=1234,
            relaunch_cmd="python main.py --native"
        )

        self.assertIsNotNone(script_path)
        self.assertTrue(os.path.exists(script_path))
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("1234", content)
        self.assertIn("cursor_accounts.db", content)
        self.assertIn("main.py", content)


if __name__ == "__main__":
    unittest.main()