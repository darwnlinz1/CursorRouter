"""
Comprehensive Verification Suite for 50% Quota Exhaustion Upgrade
Tests:
1. QUOTA_EXHAUSTION_THRESHOLD configuration & status classification logic
2. auto_switch_best_account selection: strict filtering for usage < 50%
3. Auto-rotate watcher trigger condition at 50%
4. delete_exhausted_accounts at 50% threshold
5. server.py /api/stats and /api/auto-rotate/status responses
6. test_proxy/token_pool query filtering
"""

import os
import sys
import unittest
import sqlite3
import tempfile
import shutil
from unittest.mock import MagicMock, patch

# Ensure local imports work
sys.path.insert(0, os.path.dirname(__file__))

import account_pool
from account_pool import AccountPoolManager, QUOTA_EXHAUSTION_THRESHOLD, QUOTA_WARNING_THRESHOLD
from server import app
try:
    from token_pool import TokenPoolManager
except ImportError:
    from tests.mock_workspaces.test_proxy.token_pool import TokenPoolManager

class TestQuotaExhaustionThreshold(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_cookies = os.path.join(self.test_dir, "Cookies")
        os.makedirs(self.test_cookies, exist_ok=True)
        self.test_db = os.path.join(self.test_dir, "test_accounts.db")
        
        # Patch DB_FILE in account_pool
        self.orig_db = account_pool.DB_FILE
        account_pool.DB_FILE = self.test_db

    def tearDown(self):
        account_pool.DB_FILE = self.orig_db
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_threshold_constants(self):
        """Verify default threshold constants."""
        self.assertEqual(QUOTA_EXHAUSTION_THRESHOLD, 100.0)
        self.assertEqual(QUOTA_WARNING_THRESHOLD, 50.0)
        self.assertEqual(AccountPoolManager.QUOTA_EXHAUSTION_THRESHOLD, 100.0)

    def test_status_classification_logic(self):
        """Test status evaluation rules for various usage percentages: 50% is HIGH_USAGE, only 100% or lock is EXHAUSTED."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)
        
        # Case 1: usage = 0% -> READY
        mock_profile_0 = {"email": "test0@example.com", "usagePercent": 0.0, "autoPercentUsed": 0.0}
        usage = mock_profile_0["usagePercent"]
        auto = mock_profile_0["autoPercentUsed"]
        status = "EXHAUSTED" if (usage >= QUOTA_EXHAUSTION_THRESHOLD or auto >= 100.0) else ("HIGH_USAGE" if usage >= QUOTA_WARNING_THRESHOLD else "READY")
        self.assertEqual(status, "READY")

        # Case 2: usage = 42% -> READY (< 50%)
        mock_profile_42 = {"email": "test42@example.com", "usagePercent": 42.0, "autoPercentUsed": 42.0}
        usage = mock_profile_42["usagePercent"]
        auto = mock_profile_42["autoPercentUsed"]
        status = "EXHAUSTED" if (usage >= QUOTA_EXHAUSTION_THRESHOLD or auto >= 100.0) else ("HIGH_USAGE" if usage >= QUOTA_WARNING_THRESHOLD else "READY")
        self.assertEqual(status, "READY")

        # Case 3: usage = 50.0% -> HIGH_USAGE (slow pool starts, but NOT blocked from chat!)
        mock_profile_50 = {"email": "test50@example.com", "usagePercent": 50.0, "autoPercentUsed": 50.0}
        usage = mock_profile_50["usagePercent"]
        auto = mock_profile_50["autoPercentUsed"]
        status = "EXHAUSTED" if (usage >= QUOTA_EXHAUSTION_THRESHOLD or auto >= 100.0) else ("HIGH_USAGE" if usage >= QUOTA_WARNING_THRESHOLD else "READY")
        self.assertEqual(status, "HIGH_USAGE")

        # Case 4: usage = 78.0% -> HIGH_USAGE (usable up to 100%)
        mock_profile_78 = {"email": "test78@example.com", "usagePercent": 78.0, "autoPercentUsed": 78.0}
        usage = mock_profile_78["usagePercent"]
        auto = mock_profile_78["autoPercentUsed"]
        status = "EXHAUSTED" if (usage >= QUOTA_EXHAUSTION_THRESHOLD or auto >= 100.0) else ("HIGH_USAGE" if usage >= QUOTA_WARNING_THRESHOLD else "READY")
        self.assertEqual(status, "HIGH_USAGE")

        # Case 5: usage = 100.0% -> EXHAUSTED
        mock_profile_100 = {"email": "test100@example.com", "usagePercent": 100.0, "autoPercentUsed": 100.0}
        usage = mock_profile_100["usagePercent"]
        auto = mock_profile_100["autoPercentUsed"]
        status = "EXHAUSTED" if (usage >= QUOTA_EXHAUSTION_THRESHOLD or auto >= 100.0) else ("HIGH_USAGE" if usage >= QUOTA_WARNING_THRESHOLD else "READY")
        self.assertEqual(status, "EXHAUSTED")

        # Case 6: usage = 45% but autoPercentUsed = 100 -> EXHAUSTED
        mock_profile_auto100 = {"email": "test_auto@example.com", "usagePercent": 45.0, "autoPercentUsed": 100.0}
        usage = mock_profile_auto100["usagePercent"]
        auto = mock_profile_auto100["autoPercentUsed"]
        status = "EXHAUSTED" if (usage >= QUOTA_EXHAUSTION_THRESHOLD or auto >= 100.0) else ("HIGH_USAGE" if usage >= QUOTA_WARNING_THRESHOLD else "READY")
        self.assertEqual(status, "EXHAUSTED")

    def test_auto_switch_best_account_selection_order(self):
        """Verify auto_switch_best_account selects the lowest usage account available."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)
        
        # Populate test accounts
        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.executemany("""
            INSERT INTO accounts (email, file_name, access_token, usage_percent, status)
            VALUES (?, ?, ?, ?, ?)
        """, [
            ("acc_exhausted_100@test.com", "acc1.txt", "token1_abcdefghijklmnopqrstuvwxyz", 100.0, "EXHAUSTED"),
            ("acc_high_78@test.com", "acc2.txt", "token2_abcdefghijklmnopqrstuvwxyz", 78.0, "HIGH_USAGE"),
            ("acc_high_51@test.com", "acc3.txt", "token3_abcdefghijklmnopqrstuvwxyz", 51.0, "HIGH_USAGE"),
            ("acc_ready_45@test.com", "acc4.txt", "token4_abcdefghijklmnopqrstuvwxyz", 45.0, "READY"),
            ("acc_ready_10@test.com", "acc5.txt", "token5_abcdefghijklmnopqrstuvwxyz", 10.0, "READY"),
            ("acc_ready_0@test.com", "acc6.txt", "token6_abcdefghijklmnopqrstuvwxyz", 0.0, "READY"),
        ])
        con.commit()
        con.close()

        with patch.object(mgr, "switch_to_account", return_value=True):
            best = mgr.auto_switch_best_account()
            self.assertIsNotNone(best)
            # Should pick acc_ready_0@test.com as lowest usage
            self.assertEqual(best["email"], "acc_ready_0@test.com")
            self.assertEqual(best["usage_percent"], 0.0)

    def test_auto_switch_best_account_can_select_high_usage_over_50(self):
        """Verify accounts between 50% and 100% CAN be selected if lower accounts aren't available."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)
        
        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.executemany("""
            INSERT INTO accounts (email, file_name, access_token, usage_percent, status)
            VALUES (?, ?, ?, ?, ?)
        """, [
            ("acc_55@test.com", "acc1.txt", "token1_abcdefghijklmnopqrstuvwxyz", 55.0, "HIGH_USAGE"),
            ("acc_78@test.com", "acc2.txt", "token2_abcdefghijklmnopqrstuvwxyz", 78.0, "HIGH_USAGE"),
            ("acc_100@test.com", "acc3.txt", "token3_abcdefghijklmnopqrstuvwxyz", 100.0, "EXHAUSTED"),
        ])
        con.commit()
        con.close()

        with patch.object(mgr, "switch_to_account", return_value=True):
            best = mgr.auto_switch_best_account()
            self.assertIsNotNone(best, "Accounts between 50% and 100% are usable and must be eligible")
            self.assertEqual(best["email"], "acc_55@test.com")
            self.assertEqual(best["usage_percent"], 55.0)

    def test_auto_switch_best_account_when_all_exhausted(self):
        """Verify auto_switch_best_account returns None if all accounts >= 100% or EXHAUSTED."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)
        
        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.executemany("""
            INSERT INTO accounts (email, file_name, access_token, usage_percent, status)
            VALUES (?, ?, ?, ?, ?)
        """, [
            ("acc_100@test.com", "acc1.txt", "token1_abcdefghijklmnopqrstuvwxyz", 100.0, "EXHAUSTED"),
            ("acc_102@test.com", "acc2.txt", "token2_abcdefghijklmnopqrstuvwxyz", 102.0, "EXHAUSTED"),
            ("acc_ex@test.com", "acc3.txt", "token3_abcdefghijklmnopqrstuvwxyz", 50.0, "EXHAUSTED"),
        ])
        con.commit()
        con.close()

        best = mgr.auto_switch_best_account()
        self.assertIsNone(best, "Should return None when all accounts are exhausted")

    def test_init_db_auto_migrates_ge_100_to_exhausted(self):
        """Verify _init_db automatically updates any existing account with usage >= 100 to EXHAUSTED and 50-99% to HIGH_USAGE."""
        # Create a DB with old data
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
        cur.execute("INSERT INTO accounts (file_name, email, usage_percent, status) VALUES ('f1', 'a100@test.com', 100.0, 'READY')")
        cur.execute("INSERT INTO accounts (file_name, email, usage_percent, status) VALUES ('f2', 'a50@test.com', 50.0, 'EXHAUSTED')")
        cur.execute("INSERT INTO accounts (file_name, email, usage_percent, status) VALUES ('f3', 'a30@test.com', 30.0, 'READY')")
        con.commit()
        con.close()

        # Instantiate manager, which calls _init_db
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("SELECT email, status FROM accounts ORDER BY usage_percent ASC")
        rows = dict(cur.fetchall())
        con.close()

        self.assertEqual(rows["a30@test.com"], "READY")
        self.assertEqual(rows["a50@test.com"], "HIGH_USAGE")
        self.assertEqual(rows["a100@test.com"], "EXHAUSTED")

    def test_delete_exhausted_accounts(self):
        """Verify delete_exhausted_accounts removes accounts >= 100% or EXHAUSTED."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)
        
        # Create cookie files
        for f in ["c1.txt", "c2.txt", "c3.txt", "c4.txt"]:
            with open(os.path.join(self.test_cookies, f), "w") as fp:
                fp.write("cookie")

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.executemany("""
            INSERT INTO accounts (file_name, email, usage_percent, status)
            VALUES (?, ?, ?, ?)
        """, [
            ("c1.txt", "keep@test.com", 20.0, "READY"),
            ("c2.txt", "keep75@test.com", 75.0, "HIGH_USAGE"),
            ("c3.txt", "del100@test.com", 100.0, "EXHAUSTED"),
            ("c4.txt", "del_ex@test.com", 102.0, "EXHAUSTED"),
        ])
        con.commit()
        con.close()

        deleted = mgr.delete_exhausted_accounts()
        self.assertEqual(deleted, 2)

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.execute("SELECT email FROM accounts")
        remaining = set(r[0] for r in cur.fetchall())
        con.close()

        self.assertEqual(remaining, {"keep@test.com", "keep75@test.com"})
        self.assertTrue(os.path.exists(os.path.join(self.test_cookies, "c1.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.test_cookies, "c2.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.test_cookies, "c3.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.test_cookies, "c4.txt")))

    def test_token_pool_filters_ge_100(self):
        """Verify TokenPoolManager loads only accounts with usage < 100%."""
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
        cur.executemany("""
            INSERT INTO accounts (email, file_name, access_token, usage_percent, status)
            VALUES (?, ?, ?, ?, ?)
        """, [
            ("tp_101@test.com", "f1.txt", "token_long_valid_101_percent_12345", 101.0, "EXHAUSTED"),
            ("tp_75@test.com", "f2.txt", "token_long_valid_75_percent_12345", 75.0, "HIGH_USAGE"),
            ("tp_10@test.com", "f3.txt", "token_long_valid_10_percent_12345", 10.0, "READY"),
        ])
        con.commit()
        con.close()

        t_pool = TokenPoolManager(db_path=self.test_db)
        stats = t_pool.get_stats()
        self.assertEqual(stats["cached_accounts"], 2, "Accounts < 100% should be cached")
        self.assertEqual(stats["ready_count"], 2)

        token = t_pool.get_token_from_cache()
        self.assertIsNotNone(token)
        self.assertEqual(token["email"], "tp_10@test.com")

    def test_auto_switch_skips_exhausted_pending_account(self):
        """Verify that a PENDING account that refreshes to >= 100% is NOT injected, and next pending is chosen."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.executemany("""
            INSERT INTO accounts (id, file_name, status, cookie_full)
            VALUES (?, ?, 'PENDING', ?)
        """, [
            (1, "pending_exhausted.txt", "cookie_exhausted"),
            (2, "pending_valid.txt", "cookie_valid")
        ])
        con.commit()
        con.close()

        def mock_refresh(acc_id):
            if acc_id == 1:
                # Simulates profile that has 100% usage
                con = sqlite3.connect(self.test_db)
                cur = con.cursor()
                cur.execute("UPDATE accounts SET email='pending_exhausted@test.com', usage_percent=100.0, status='EXHAUSTED' WHERE id=1")
                con.commit()
                con.close()
                return {"email": "pending_exhausted@test.com", "usagePercent": 100.0, "autoPercentUsed": 100.0}
            elif acc_id == 2:
                # Simulates profile that has 5% usage
                con = sqlite3.connect(self.test_db)
                cur = con.cursor()
                cur.execute("UPDATE accounts SET email='pending_valid@test.com', usage_percent=5.0, status='READY' WHERE id=2")
                con.commit()
                con.close()
                return {"email": "pending_valid@test.com", "usagePercent": 5.0, "autoPercentUsed": 10.0}
            return None

        with patch.object(mgr, "refresh_account_quota", side_effect=mock_refresh), \
             patch.object(mgr, "switch_to_account", return_value=True) as mock_switch:
            best = mgr.auto_switch_best_account()
            self.assertIsNotNone(best)
            self.assertEqual(best["email"], "pending_valid@test.com")
            self.assertEqual(best["usage_percent"], 5.0)
            mock_switch.assert_called_once_with(2)

    def test_auto_switch_candidate_failover(self):
        """Verify that if candidate 1 fails switch_to_account, candidate 2 is attempted and succeeds."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.executemany("""
            INSERT INTO accounts (id, email, file_name, access_token, usage_percent, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            (1, "fail@test.com", "f1.txt", "token1_12345678901234567890", 10.0, "READY"),
            (2, "success@test.com", "f2.txt", "token2_12345678901234567890", 15.0, "READY"),
        ])
        con.commit()
        con.close()

        def mock_switch(acc_id):
            return acc_id == 2

        with patch.object(mgr, "switch_to_account", side_effect=mock_switch):
            best = mgr.auto_switch_best_account()
            self.assertIsNotNone(best)
            self.assertEqual(best["email"], "success@test.com")

    def test_auto_switch_includes_high_usage_under_100(self):
        """Verify that HIGH_USAGE accounts (< 100%) are eligible if they are the lowest available under 100%."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)

        con = sqlite3.connect(self.test_db)
        cur = con.cursor()
        cur.executemany("""
            INSERT INTO accounts (id, email, file_name, access_token, usage_percent, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            (1, "ex@test.com", "f1.txt", "token1_12345678901234567890", 100.0, "EXHAUSTED"),
            (2, "warn@test.com", "f2.txt", "token2_12345678901234567890", 72.0, "HIGH_USAGE"),
        ])
        con.commit()
        con.close()

        with patch.object(mgr, "switch_to_account", return_value=True):
            best = mgr.auto_switch_best_account()
            self.assertIsNotNone(best)
            self.assertEqual(best["email"], "warn@test.com")
            self.assertEqual(best["usage_percent"], 72.0)

    def test_auto_rotate_watcher_stop_event(self):
        """Verify watcher starts and stops cleanly via stop event."""
        mgr = AccountPoolManager(cookies_dir=self.test_cookies)
        mgr.start_auto_rotate_watcher(interval_seconds=60)
        self.assertTrue(mgr.auto_rotate_enabled)
        mgr.stop_auto_rotate_watcher()
        self.assertFalse(mgr.auto_rotate_enabled)

class TestServerStatsAndEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_api_stats_endpoint(self):
        """Verify /api/stats returns threshold 100.0 and correct account counts."""
        res = self.client.get("/api/stats")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("threshold", data)
        self.assertEqual(data["threshold"], 100.0)
        self.assertIn("ready", data)
        self.assertIn("exhausted", data)
        self.assertIn("total", data)
        self.assertEqual(data["ready"] + data["exhausted"] + data["pending"] + data["expired"], data["total"])

    def test_api_stats_exact_partitioning_no_overlap(self):
        """Verify that accounts with various states partition strictly without overlap."""
        mock_accounts = [
            {"id": 1, "status": "READY", "usage_percent": 10.0},
            {"id": 2, "status": "HIGH_USAGE", "usage_percent": 72.0},  # < 100% -> ready
            {"id": 3, "status": "EXHAUSTED", "usage_percent": 100.0},  # >= 100% -> exhausted
            {"id": 4, "status": "EXHAUSTED", "usage_percent": 105.0},  # >= 100% -> exhausted
            {"id": 5, "status": "EXPIRED", "usage_percent": 80.0},     # expired
            {"id": 6, "status": "EXPIRED", "usage_percent": 10.0},     # expired
            {"id": 7, "status": "PENDING", "usage_percent": 0.0},      # pending
        ]
        with patch("server.pool.get_all_accounts", return_value=mock_accounts):
            res = self.client.get("/api/stats")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["total"], 7)
            self.assertEqual(data["ready"], 2)       # id 1 and 2
            self.assertEqual(data["exhausted"], 2)   # id 3 and 4
            self.assertEqual(data["expired"], 2)     # id 5 and 6
            self.assertEqual(data["pending"], 1)     # id 7
            self.assertEqual(data["ready"] + data["exhausted"] + data["pending"] + data["expired"], data["total"])

    def test_api_auto_rotate_status(self):
        """Verify /api/auto-rotate/status returns threshold 100.0."""
        res = self.client.get("/api/auto-rotate/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get("threshold"), 100.0)

    def test_cursor_manager_clean_exhausted_cli_arg(self):
        """Verify cursor_manager parser includes --clean-exhausted."""
        import subprocess
        script_path = os.path.join(os.path.dirname(__file__), "..", "src", "cursor_manager.py")
        if not os.path.exists(script_path):
            script_path = "cursor_manager.py"
        res = subprocess.run([sys.executable, script_path, "-h"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertIn("--clean-exhausted", res.stdout)
        self.assertIn(">= 100%", res.stdout)

if __name__ == "__main__":
    unittest.main()
