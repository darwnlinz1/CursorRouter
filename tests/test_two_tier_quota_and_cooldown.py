"""
Unit and integration tests for Two-Tier Quota Priority Pool and 7-day Cooldown Blacklist.
Tests:
- Tier 1 (<50%) priority over Tier 2 (50%-99%)
- Tier 2 fallback when Tier 1 is empty
- 7-day cooldown blacklist for accounts >= 100% or locked
- Auto-recovery when cooldown timestamp has expired
- switch_to_next_account alias
- get_all_accounts metadata (tier, in_cooldown, cooldown_remaining_seconds)
- /api/stats two-tier counter metrics
"""
import os
import sys
import time
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

# Add src to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import account_pool
from account_pool import AccountPoolManager
from server import app
import server


@pytest.fixture
def clean_pool(tmp_path):
    temp_cookies = tmp_path / "Cookies"
    temp_cookies.mkdir()
    test_db = str(tmp_path / "test_accounts.db")
    orig_db = account_pool.DB_FILE
    orig_server_pool = getattr(server, "pool", None)
    
    account_pool.DB_FILE = test_db
    pool = AccountPoolManager(cookies_dir=str(temp_cookies))
    server.pool = pool

    yield pool

    account_pool.DB_FILE = orig_db
    if orig_server_pool is not None:
        server.pool = orig_server_pool


class TestTwoTierQuotaAndCooldown:
    """Test suite for two-tier quota rotation and 7-day cooldown blacklist."""

    def test_tier_classification_in_get_all_accounts(self, clean_pool):
        now_ts = int(time.time())
        con = sqlite3.connect(account_pool.DB_FILE)
        cur = con.cursor()
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("t1.txt", "tier1@example.com", "tok1", 25.0, "READY", 0))
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("t2.txt", "tier2@example.com", "tok2", 75.0, "HIGH_USAGE", 0))
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("bl.txt", "blacklist@example.com", "tok3", 100.0, "EXHAUSTED", now_ts + 500000))
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("exp.txt", "expired@example.com", "tok4", 10.0, "EXPIRED", 0))
        
        con.commit()
        con.close()

        accounts = clean_pool.get_all_accounts()
        acc_map = {a["email"]: a for a in accounts if a.get("email")}

        assert "tier1@example.com" in acc_map
        assert acc_map["tier1@example.com"]["tier"] == "tier1"
        assert not acc_map["tier1@example.com"]["in_cooldown"]

        assert "tier2@example.com" in acc_map
        assert acc_map["tier2@example.com"]["tier"] == "tier2"
        assert not acc_map["tier2@example.com"]["in_cooldown"]

        assert "blacklist@example.com" in acc_map
        assert acc_map["blacklist@example.com"]["tier"] == "blacklist"
        assert acc_map["blacklist@example.com"]["in_cooldown"] is True
        assert acc_map["blacklist@example.com"]["cooldown_remaining_seconds"] > 0

        assert "expired@example.com" in acc_map
        assert acc_map["expired@example.com"]["tier"] == "expired"

    def test_tier1_prioritized_over_tier2_in_auto_switch(self, clean_pool):
        con = sqlite3.connect(account_pool.DB_FILE)
        cur = con.cursor()
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("high.txt", "tier2_cand@example.com", "tok_t2", 65.0, "HIGH_USAGE", 0))
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("low.txt", "tier1_cand@example.com", "tok_t1", 30.0, "READY", 0))
        
        con.commit()
        con.close()

        with patch.object(clean_pool, "switch_to_account", return_value=True):
            best = clean_pool.auto_switch_best_account(notify=False, prev_email="active@example.com")
            assert best is not None
            assert best["email"] == "tier1_cand@example.com"

    def test_tier2_fallback_when_tier1_empty(self, clean_pool):
        con = sqlite3.connect(account_pool.DB_FILE)
        cur = con.cursor()
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("t2_only.txt", "tier2_only@example.com", "tok_t2_only", 80.0, "HIGH_USAGE", 0))
        
        con.commit()
        con.close()

        with patch.object(clean_pool, "switch_to_account", return_value=True):
            best = clean_pool.auto_switch_best_account(notify=False, prev_email="active@example.com")
            assert best is not None
            assert best["email"] == "tier2_only@example.com"

    def test_cooldown_accounts_skipped_in_auto_switch(self, clean_pool):
        now_ts = int(time.time())
        con = sqlite3.connect(account_pool.DB_FILE)
        cur = con.cursor()
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("cd.txt", "cooldown@example.com", "tok_cd", 20.0, "READY", now_ts + 600000))
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("ok.txt", "eligible@example.com", "tok_ok", 40.0, "READY", 0))
        
        con.commit()
        con.close()

        with patch.object(clean_pool, "switch_to_account", return_value=True):
            best = clean_pool.auto_switch_best_account(notify=False, prev_email="active@example.com")
            assert best is not None
            assert best["email"] == "eligible@example.com"

    def test_expired_cooldown_is_automatically_eligible_again(self, clean_pool):
        past_ts = int(time.time()) - 3600
        con = sqlite3.connect(account_pool.DB_FILE)
        cur = con.cursor()
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("rec.txt", "recovered@example.com", "tok_rec", 45.0, "READY", past_ts))
        
        con.commit()
        con.close()

        with patch.object(clean_pool, "switch_to_account", return_value=True):
            best = clean_pool.auto_switch_best_account(notify=False, prev_email="active@example.com")
            assert best is not None
            assert best["email"] == "recovered@example.com"

    def test_exhausted_candidate_gets_7_day_cooldown(self, clean_pool):
        now_ts = int(time.time())
        con = sqlite3.connect(account_pool.DB_FILE)
        cur = con.cursor()
        
        cur.execute("""
            INSERT INTO accounts (file_name, email, access_token, usage_percent, status, cooldown_until)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("exh.txt", "exhausting@example.com", "tok_exh", 90.0, "HIGH_USAGE", 0))
        
        con.commit()
        con.close()

        with patch.object(clean_pool, "get_persisted_account_quota", return_value={
            "usage_percent": 100.0,
            "total_spend": 50.0,
            "status": "EXHAUSTED"
        }):
            clean_pool.auto_switch_best_account(notify=False, prev_email="active@example.com")

        con = sqlite3.connect(account_pool.DB_FILE)
        cur = con.cursor()
        cur.execute("SELECT status, cooldown_until FROM accounts WHERE email = 'exhausting@example.com'")
        row = cur.fetchone()
        con.close()

        assert row is not None
        assert row[0] == "EXHAUSTED"
        assert row[1] >= now_ts + (7 * 86400 - 60)

    def test_switch_to_next_account_alias(self, clean_pool):
        assert hasattr(clean_pool, "switch_to_next_account")
        assert callable(clean_pool.switch_to_next_account)
        with patch.object(clean_pool, "auto_switch_best_account", return_value={"id": 99, "email": "test@test.com"}):
            res = clean_pool.switch_to_next_account()
            assert res["email"] == "test@test.com"

    def test_api_stats_returns_tier_and_blacklist_metrics(self, clean_pool):
        client = app.test_client()
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.get_json()

        assert "total" in data
        assert "ready" in data
        assert "tier1_count" in data
        assert "tier2_count" in data
        assert "blacklist_count" in data
        assert "cooldown_count" in data
        assert "tier1_threshold" in data
        assert data["tier1_threshold"] == 50.0
