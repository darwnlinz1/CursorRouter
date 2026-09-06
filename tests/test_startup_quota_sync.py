import os
import sys
import unittest
import tempfile
import shutil
import sqlite3
import time
from unittest.mock import patch, MagicMock

# Setup path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
for p in (SRC_DIR, ROOT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import account_pool
from account_pool import AccountPoolManager, QUOTA_EXHAUSTION_THRESHOLD, QUOTA_WARNING_THRESHOLD


class TestStartupQuotaSync(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.test_dir, "test_cursor_accounts.db")
        self.test_cookies = os.path.join(self.test_dir, "Cookies")
        os.makedirs(self.test_cookies, exist_ok=True)
        
        self.original_db = account_pool.DB_FILE
        account_pool.DB_FILE = self.test_db

    def tearDown(self):
        account_pool.DB_FILE = self.original_db
        try:
            import server
            server.DB_FILE = self.original_db
            server.pool = AccountPoolManager()
        except Exception:
            pass
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    def test_01_init_db_reconciles_bogus_exhausted_statuses(self):
        """Kiem tra _init_db tu dong hoan nguyen READY/HIGH_USAGE cho tai khoan bi danh dau nham EXHAUSTED."""
        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT UNIQUE,
                email TEXT,
                display_name TEXT,
                auth_id TEXT,
                cookie_snippet TEXT,
                cookie_full TEXT,
                access_token TEXT,
                refresh_token TEXT,
                usage_percent REAL DEFAULT 0.0,
                total_spend REAL DEFAULT 0.0,
                display_message TEXT,
                status TEXT DEFAULT 'PENDING',
                last_checked INTEGER DEFAULT 0
            )
        """)
        # Account 1: 0% usage, but marked EXHAUSTED by mistake -> must revert to READY
        cur.execute("INSERT INTO accounts (file_name, email, usage_percent, total_spend, status) VALUES ('f1', 'zero@test.com', 0.0, 0.0, 'EXHAUSTED')")
        # Account 2: 15% usage, but marked EXHAUSTED by mistake -> must revert to READY
        cur.execute("INSERT INTO accounts (file_name, email, usage_percent, total_spend, status) VALUES ('f2', 'low@test.com', 15.0, 30.0, 'EXHAUSTED')")
        # Account 3: 75% usage, but marked EXHAUSTED -> must revert to HIGH_USAGE
        cur.execute("INSERT INTO accounts (file_name, email, usage_percent, total_spend, status) VALUES ('f3', 'high@test.com', 75.0, 150.0, 'EXHAUSTED')")
        # Account 4: 100% usage, status READY -> must migrate to EXHAUSTED
        cur.execute("INSERT INTO accounts (file_name, email, usage_percent, total_spend, status) VALUES ('f4', 'over@test.com', 100.0, 200.0, 'READY')")
        # Account 5: 10% usage, status EXPIRED -> must stay EXPIRED
        cur.execute("INSERT INTO accounts (file_name, email, usage_percent, total_spend, status) VALUES ('f5', 'expired@test.com', 10.0, 20.0, 'EXPIRED')")
        con.commit()
        con.close()

        mgr = AccountPoolManager(cookies_dir=self.test_cookies)

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("SELECT email, status FROM accounts")
        statuses = dict(cur.fetchall())
        con.close()

        self.assertEqual(statuses["zero@test.com"], "READY")
        self.assertEqual(statuses["low@test.com"], "READY")
        self.assertEqual(statuses["high@test.com"], "HIGH_USAGE")
        self.assertEqual(statuses["over@test.com"], "EXHAUSTED")
        self.assertEqual(statuses["expired@test.com"], "EXPIRED")

    def test_02_startup_sync_configuration_and_5_workers(self):
        """Kiem tra khoi tao tien trinh chay ngam voi dung 5 luong."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)
        self.assertFalse(mgr.get_startup_sync_progress()["running"])

        # Insert 10 mock accounts
        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        for i in range(10):
            cur.execute("""
                INSERT INTO accounts (file_name, email, access_token, usage_percent, status)
                VALUES (?, ?, ?, ?, ?)
            """, (f"f_{i}.txt", f"acc{i}@test.com", f"token_{i}", 0.0, "PENDING"))
        con.commit()
        con.close()

        with patch("account_pool.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "planUsage": {"totalPercentUsed": 10.0, "autoPercentUsed": 10.0, "totalSpend": 20.0},
                "displayMessage": "OK"
            }
            mock_post.return_value = mock_resp

            started = mgr.start_startup_quota_sync(max_workers=5, blocking=True)
            self.assertTrue(started)

            progress = mgr.get_startup_sync_progress()
            self.assertFalse(progress["running"])
            self.assertEqual(progress["total"], 10)
            self.assertEqual(progress["current"], 10)
            self.assertEqual(progress["ready_count"], 10)
            self.assertEqual(progress["max_workers"], 5)
            self.assertIsNotNone(progress["finished_at"])

    def test_03_startup_sync_quota_evaluation_and_db_persistence(self):
        """Kiem tra tinh dung dan cua quota va trang thai duoc cap nhat vao DB qua 5 luong."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, cookie_full, usage_percent, total_spend, status)
            VALUES 
                ('acc1', 'ready@test.com', 'tok_ready', 'cookie_1', 0.0, 0.0, 'EXHAUSTED'),
                ('acc2', 'high@test.com', 'tok_high', 'cookie_2', 0.0, 0.0, 'READY'),
                ('acc3', 'exhausted@test.com', 'tok_exhausted', 'cookie_3', 0.0, 0.0, 'READY'),
                ('acc4', 'expired@test.com', 'tok_expired', '', 0.0, 0.0, 'READY')
        """)
        con.commit()
        con.close()

        def mock_post_side_effect(url, headers=None, json=None, timeout=None):
            token = headers.get("Authorization", "").replace("Bearer ", "")
            resp = MagicMock()
            if token == "tok_ready":
                resp.status_code = 200
                resp.json.return_value = {
                    "planUsage": {"totalPercentUsed": 5.0, "autoPercentUsed": 5.0, "totalSpend": 10.0},
                    "displayMessage": "Normal"
                }
            elif token == "tok_high":
                resp.status_code = 200
                resp.json.return_value = {
                    "planUsage": {"totalPercentUsed": 75.0, "autoPercentUsed": 75.0, "totalSpend": 150.0},
                    "displayMessage": "High usage warning"
                }
            elif token == "tok_exhausted":
                resp.status_code = 200
                resp.json.return_value = {
                    "planUsage": {"totalPercentUsed": 100.0, "autoPercentUsed": 100.0, "totalSpend": 200.0},
                    "displayMessage": "Out of quota"
                }
            elif token == "tok_expired":
                resp.status_code = 401
                resp.json.return_value = {"error": "unauthorized"}
            return resp

        with patch("account_pool.requests.post", side_effect=mock_post_side_effect):
            mgr.start_startup_quota_sync(max_workers=5, blocking=True)

        con = sqlite3.connect(self.test_db)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT email, usage_percent, total_spend, status, display_message FROM accounts")
        rows = {r["email"]: dict(r) for r in cur.fetchall()}
        con.close()

        # ready@test.com was incorrectly marked EXHAUSTED, but live check returned 5.0% -> MUST be READY!
        self.assertEqual(rows["ready@test.com"]["status"], "READY")
        self.assertEqual(rows["ready@test.com"]["usage_percent"], 5.0)
        self.assertEqual(rows["ready@test.com"]["total_spend"], 10.0)

        # high@test.com returned 75.0% -> HIGH_USAGE
        self.assertEqual(rows["high@test.com"]["status"], "HIGH_USAGE")
        self.assertEqual(rows["high@test.com"]["usage_percent"], 75.0)

        # exhausted@test.com returned 100.0% -> EXHAUSTED
        self.assertEqual(rows["exhausted@test.com"]["status"], "EXHAUSTED")
        self.assertEqual(rows["exhausted@test.com"]["usage_percent"], 100.0)

        # expired@test.com returned 401 and had no cookie -> EXPIRED
        self.assertEqual(rows["expired@test.com"]["status"], "EXPIRED")

        # Check snapshots recorded
        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM quota_snapshots WHERE source = 'startup_sync'")
        snap_count = cur.fetchone()[0]
        con.close()
        self.assertGreaterEqual(snap_count, 3)

    def test_04_startup_sync_token_refresh_via_cookie_on_401(self):
        """Kiem tra co che tu dong doi token qua cookie neu gap 401 trong luong startup."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, cookie_full, usage_percent, status)
            VALUES ('acc_refresh', 'refresh@test.com', 'old_tok', 'valid_cookie_123', 0.0, 'PENDING')
        """)
        con.commit()
        con.close()

        call_count = {"calls": 0}
        def mock_post(url, headers=None, json=None, timeout=None):
            resp = MagicMock()
            token = headers.get("Authorization", "").replace("Bearer ", "")
            if token == "old_tok":
                resp.status_code = 401
            elif token == "new_tok":
                call_count["calls"] += 1
                resp.status_code = 200
                resp.json.return_value = {
                    "planUsage": {"totalPercentUsed": 8.0, "autoPercentUsed": 8.0, "totalSpend": 16.0},
                    "displayMessage": "Refreshed OK"
                }
            return resp

        with patch("account_pool.requests.post", side_effect=mock_post), \
             patch.object(mgr.auth_client, "exchange_cookie_to_tokens", return_value={"accessToken": "new_tok", "refreshToken": "new_ref"}):
            mgr.start_startup_quota_sync(max_workers=5, blocking=True)

        con = sqlite3.connect(self.test_db)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT * FROM accounts WHERE email = 'refresh@test.com'")
        acc = dict(cur.fetchone())
        con.close()

        self.assertEqual(acc["access_token"], "new_tok")
        self.assertEqual(acc["status"], "READY")
        self.assertEqual(acc["usage_percent"], 8.0)
        self.assertEqual(call_count["calls"], 1)

    def test_05_api_endpoints_startup_sync(self):
        """Kiem tra cac endpoint /api/startup-sync/status va /api/startup-sync/start."""
        import server
        server.DB_FILE = self.test_db
        server.pool = AccountPoolManager(cookies_dir=self.test_cookies)
        client = server.app.test_client()

        res = client.get("/api/startup-sync/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("startup_sync", data)
        self.assertIn("max_workers", data["startup_sync"])
        self.assertEqual(data["startup_sync"]["max_workers"], 5)

        # Test POST start
        res_post = client.post("/api/startup-sync/start", json={"max_workers": 5})
        self.assertEqual(res_post.status_code, 200)
        post_data = res_post.get_json()
        self.assertIn("startup_sync", post_data)

        # Test api/status includes startup_sync
        res_status = client.get("/api/status")
        self.assertEqual(res_status.status_code, 200)
        status_data = res_status.get_json()
        self.assertIn("startup_sync", status_data)


if __name__ == "__main__":
    unittest.main()
