import os
import shutil
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

from account_pool import AccountPoolManager, DB_FILE
import server


class TestOAuthCleanupAndPrediction(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.pool = AccountPoolManager()
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

    def test_01_delete_invalid_accounts_removes_expired_and_tokenless_rows(self):
        """EXPIRED and empty-token OAuth rows must be cleaned up cleanly from SQLite."""
        con = sqlite3.connect(DB_FILE, timeout=30.0)
        cur = con.cursor()
        cur.execute("DELETE FROM accounts WHERE email IN ('expired_oauth@test.com', 'valid_oauth@test.com')")
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, total_spend, status)
            VALUES
                ('oauth_expired_clean.oauth', 'expired_oauth@test.com', '', 0.0, 0.0, 'EXPIRED'),
                ('oauth_valid_keep.oauth', 'valid_oauth@test.com', 'eyJhbGciOiJIUzI1NiJ9.eyJ0eXBlIjoic2Vzc2lvbiJ9.sig', 15.0, 30.0, 'READY')
        """)
        con.commit()
        con.close()

        try:
            deleted = self.pool.delete_invalid_accounts()
            self.assertGreaterEqual(deleted, 1)

            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute("SELECT email FROM accounts WHERE email IN ('expired_oauth@test.com', 'valid_oauth@test.com')")
            remaining = {r[0] for r in cur.fetchall()}
            con.close()

            self.assertNotIn("expired_oauth@test.com", remaining)
            self.assertIn("valid_oauth@test.com", remaining)
        finally:
            con = sqlite3.connect(DB_FILE, timeout=30.0)
            cur = con.cursor()
            cur.execute("DELETE FROM accounts WHERE email IN ('expired_oauth@test.com', 'valid_oauth@test.com')")
            con.commit()
            con.close()

    def test_02_clean_invalid_api_endpoint(self):
        """POST /api/accounts/clean-invalid triggers expired OAuth session cleanup."""
        with patch.object(server.pool, "delete_invalid_accounts", return_value=4):
            resp = self.app.post("/api/accounts/clean-invalid")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data["success"])
            self.assertEqual(data["deleted"], 4)

    def test_03_pool_summary_returns_oauth_metrics(self):
        """GET /api/pool/summary must return auth_mode='oauth_pkce' and pool tier metrics."""
        resp = self.app.get("/api/pool/summary")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["auth_mode"], "oauth_pkce")
        for key in ("total_accounts", "valid_tokens", "session_tokens", "ready_count", "tier1_count", "tier2_count", "exhausted_count", "pending_count", "expired_count"):
            self.assertIn(key, data)
            self.assertIsInstance(data[key], int)

    def test_04_legacy_cookie_routes_removed(self):
        """Legacy /api/cookies/* endpoints must no longer exist in v3.1 Pure OAuth."""
        self.assertEqual(self.app.get("/api/cookies/info").status_code, 404)
        self.assertEqual(self.app.post("/api/cookies/quarantine").status_code, 404)

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
