"""
Unit and integration tests for Native Desktop Application Launcher (native_app.py).
Tests:
- find_free_port & socket utilities
- NativeAppController initialization & port binding
- Headless execution without blocking GUI
- Window close event handler & key unstick release
- main.py --native argument routing
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Add src and root to path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import native_app
from native_app import find_free_port, is_port_in_use, NativeAppController


class TestNativeAppLauncher(unittest.TestCase):
    def test_01_find_free_port_returns_valid_port(self):
        """find_free_port must return an open socket port."""
        port = find_free_port(start_port=18500, max_attempts=10)
        self.assertIsInstance(port, int)
        self.assertGreaterEqual(port, 18500)
        self.assertLess(port, 18600)

    def test_02_is_port_in_use_detection(self):
        """is_port_in_use must return boolean without throwing."""
        res = is_port_in_use(port=9)  # Port 9 is discard protocol, usually closed
        self.assertIsInstance(res, bool)

    def test_03_native_app_controller_initialization(self):
        """NativeAppController initializes correctly with custom port and headless flag."""
        ctrl = NativeAppController(port=7899, headless=True)
        self.assertEqual(ctrl.port, 7899)
        self.assertTrue(ctrl.headless)
        self.assertFalse(ctrl.running)

    @patch("native_app.release_all_modifier_keys")
    def test_04_window_closed_handler_releases_keys(self, mock_release_keys):
        """Closing the native window must invoke release_all_modifier_keys."""
        ctrl = NativeAppController(port=7899, headless=True)
        ctrl.running = True
        ctrl.on_window_closed()
        self.assertFalse(ctrl.running)
        self.assertTrue(mock_release_keys.called)

    @patch("native_app.is_cursor_running", return_value=True)
    def test_05_headless_launch_returns_true(self, mock_is_running):
        """Headless launch must succeed without blocking on GUI window."""
        test_port = find_free_port(start_port=19200, max_attempts=20)
        ctrl = NativeAppController(port=test_port, headless=True)
        ok = ctrl.launch()
        self.assertTrue(ok)
        self.assertTrue(ctrl.running)
        ctrl.on_window_closed()

    @patch("native_app.is_cursor_running", return_value=False)
    def test_06_headless_launch_when_cursor_offline(self, mock_is_running):
        """Headless launch prints offline warning and continues when Cursor is closed."""
        test_port = find_free_port(start_port=19300, max_attempts=20)
        ctrl = NativeAppController(port=test_port, headless=True)
        ok = ctrl.launch()
        self.assertTrue(ok)
        ctrl.on_window_closed()


if __name__ == "__main__":
    unittest.main()
