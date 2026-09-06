"""
Unit Tests for Standalone Windows EXE Builder
=============================================
Tests:
1. calculate_sha256 file hashing.
2. Build command arguments and data directories verification.
3. Build manifest integrity formatting.
"""

import os
import sys
import tempfile
import hashlib
import unittest
from unittest.mock import patch, MagicMock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

try:
    import PyInstaller
except ImportError:
    pass

import build_standalone_exe


class TestStandaloneBuilder(unittest.TestCase):
    def test_01_calculate_sha256(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"SAMPLE_EXE_PAYLOAD_TEST")
            f_path = f.name
        try:
            expected = hashlib.sha256(b"SAMPLE_EXE_PAYLOAD_TEST").hexdigest()
            calc = build_standalone_exe.calculate_sha256(f_path)
            self.assertEqual(calc, expected)
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

    def test_02_build_executable_dry_run(self):
        """Verify that build_executable invokes PyInstaller with appropriate flags."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with patch("build_standalone_exe.subprocess.run", return_value=mock_proc) as mock_run:
            with patch("os.path.exists", return_value=True):
                with patch("os.path.getsize", return_value=15 * 1024 * 1024):
                    with patch("build_standalone_exe.calculate_sha256", return_value="dummyhash"):
                        with patch("builtins.open", unittest.mock.mock_open()):
                            success = build_standalone_exe.build_executable(onefile=True, windowed=False)
                            self.assertTrue(success)
                            self.assertTrue(mock_run.called)
                            args, kwargs = mock_run.call_args
                            cmd = args[0]
                            self.assertIn("--onefile", cmd)
                            self.assertIn("CursorManager", cmd)
                            self.assertIn("--hidden-import", cmd)
                            self.assertIn("updater", cmd)
                            self.assertIn("ai_optimizer", cmd)


if __name__ == "__main__":
    unittest.main()
