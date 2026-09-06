import os
import shutil
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

from account_pool import AccountPoolManager, DB_FILE
import server


class TestQuarantineAndPrediction(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.pool = AccountPoolManager(cookies_dir=self.tmp_dir)
        self.app = server.app.test_client()
        self.app.testing = True

    def tearDown(self):
        server.pool.stop_auto_rotate_watcher()
        self.pool.stop_auto_rotate_watcher()
        if os.path.exists(self.tmp_dir):
            try:
                shutil.rmtree(self.tmp_dir)
            except Exception:
                pass

    def test_01_quarantine_empty_and_blank_cookie_files(self):
        """0-byte and blank cookie files must be quarantined to Archive_Corrupted."""
        empty_file = os.path.join(self.tmp_dir, "empty_cookie.txt")
        with open(empty_file, "w", encoding="utf-8") as f:
            pass  # 0 bytes

        blank_file = os.path.join(self.tmp_dir, "blank_cookie.txt")
        with open(blank_file, "w", encoding="utf-8") as f:
            f.write("   \n\t   \n")  # whitespace only

        valid_file = os.path.join(self.tmp_dir, "valid_cookie.txt")
        with open(valid_file, "w", encoding="utf-8") as f:
            f.write("valid_session_cookie_token_1234567890")

        quarantined = self.pool.quarantine_corrupted_cookies()
        self.assertEqual(quarantined, 2)

        arch_dir = os.path.join(self.tmp_dir, "Archive_Corrupted")
        self.assertTrue(os.path.exists(arch_dir))
        self.assertTrue(os.path.exists(os.path.join(arch_dir, "empty_cookie.txt")))
        self.assertTrue(os.path.exists(os.path.join(arch_dir, "blank_cookie.txt")))
        # Valid file stays in cookies dir
        self.assertTrue(os.path.exists(valid_file))

    def test_02_quarantine_expired_account_cookie(self):
        """Cookie file corresponding to an EXPIRED account in DB must be quarantined."""
        exp_file = os.path.join(self.tmp_dir, "expired_user@test.com.txt")
        with open(exp_file, "w", encoding="utf-8") as f:
            f.write("expired_cookie_token_sample")

        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute("DELETE FROM accounts WHERE email = 'expired_user@test.com'")
        cur.execute("""
            INSERT INTO accounts (file_name, email, usage_percent, total_spend, status)
            VALUES ('expired_user@test.com.txt', 'expired_user@test.com', 0.0, 0.0, 'EXPIRED')
        """)
        con.commit()
        con.close()

        try:
            count = self.pool.quarantine_corrupted_cookies()
            self.assertGreaterEqual(count, 1)

            arch_dir = os.path.join(self.tmp_dir, "Archive_Corrupted")
            self.assertTrue(os.path.exists(os.path.join(arch_dir, "expired_user@test.com.txt")))
            self.assertFalse(os.path.exists(exp_file))
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute("DELETE FROM accounts WHERE email = 'expired_user@test.com'")
            con.commit()
            con.close()

    def test_03_quarantine_api_endpoint(self):
        """POST /api/cookies/quarantine triggers quarantine and returns count."""
        with patch.object(server.pool, "quarantine_corrupted_cookies", return_value=4):
            resp = self.app.post("/api/cookies/quarantine")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["quarantined"], 4)
            self.assertIn("Archive_Corrupted", data["message"])

    def test_04_cookies_info_includes_archived_count(self):
        """GET /api/cookies/info must return archived_count."""
        resp = self.app.get("/api/cookies/info")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("archived_count", data)
        self.assertIsInstance(data["archived_count"], int)

    def test_05_predict_account_exhaustion_burn_rate(self):
        """predict_account_exhaustion computes burn rate and time-to-exhaustion."""
        from ai_optimizer import get_quota_predictor
        predictor = get_quota_predictor()

        test_acc_id = "test_predict_acc_99"
        # Record usage progression over time
        predictor.record_usage(test_acc_id, 20.0, 0.0)
        predictor.record_usage(test_acc_id, 30.0, 0.0)

        pred = predictor.predict_exhaustion(test_acc_id, 30.0)
        self.assertEqual(pred["account_id"], test_acc_id)
        self.assertIn("burn_rate_percent_per_min", pred)
        self.assertIn("tte_seconds", pred)
        self.assertIn("should_proactively_swap", pred)
        self.assertEqual(pred["remaining_percent"], 70.0)

    def test_06_ai_predict_quota_api_endpoint(self):
        """GET /api/ai/predict-quota returns prediction payload."""
        with patch.object(server.pool, "predict_account_exhaustion", return_value={
            "account_id": "1",
            "burn_rate_percent_per_min": 5.2,
            "tte_seconds": 360.0,
            "is_critical": False,
            "should_proactively_swap": False,
            "remaining_percent": 45.0
        }):
            resp = self.app.get("/api/ai/predict-quota?id=1")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertIn("prediction", data)
            self.assertEqual(data["prediction"]["burn_rate_percent_per_min"], 5.2)
