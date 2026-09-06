"""
Unit and integration tests for isolated desktop_app module.
==========================================================
"""

import os
import sys
import tempfile
import hashlib
import unittest
from unittest.mock import patch, MagicMock

# Pre-import PyInstaller so platform.win32_ver() executes before mocks
try:
    import PyInstaller
except ImportError:
    pass

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DESKTOP_DIR = os.path.join(ROOT_DIR, 'desktop_app')
for p in (ROOT_DIR, DESKTOP_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import desktop_app.build as desktop_builder
import desktop_app.desktop_main as desktop_launcher


class TestDesktopAppModule(unittest.TestCase):
    def test_01_build_calculate_sha256(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b'DESKTOP_BUILDER_INTEGRITY_CHECK')
            f_path = f.name
        try:
            expected = hashlib.sha256(b'DESKTOP_BUILDER_INTEGRITY_CHECK').hexdigest()
            calc = desktop_builder.calculate_sha256(f_path)
            self.assertEqual(calc, expected)
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_02_build_desktop_app_dry_run(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with patch('desktop_app.build.subprocess.run', return_value=mock_proc) as mock_run:
            with patch('os.path.exists', return_value=True):
                with patch('os.path.getsize', return_value=18 * 1024 * 1024):
                    with patch('desktop_app.build.calculate_sha256', return_value='fake_hash_123'):
                        with patch('builtins.open', unittest.mock.mock_open()):
                            success = desktop_builder.build_desktop_app(onefile=True, windowed=True)
                            self.assertTrue(success)
                            self.assertTrue(mock_run.called)
                            args, _ = mock_run.call_args
                            cmd = args[0]
                            self.assertIn('--onefile', cmd)
                            self.assertIn('--windowed', cmd)
                            self.assertIn('CursorRouter', cmd)

    def test_03_desktop_main_controller_init(self):
        ctrl = desktop_launcher.DesktopAppController(port=7869, headless=True)
        self.assertEqual(ctrl.port, 7869)
        self.assertTrue(ctrl.headless)
        self.assertFalse(ctrl.running)

    @patch('desktop_app.desktop_main.release_all_modifier_keys')
    def test_04_desktop_main_window_close_cleanup(self, mock_release):
        ctrl = desktop_launcher.DesktopAppController(port=7869, headless=True)
        ctrl.running = True
        ctrl.on_window_closed()
        self.assertFalse(ctrl.running)
        self.assertTrue(mock_release.called)


if __name__ == '__main__':
    unittest.main()
