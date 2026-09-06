import os
import sys
import time
import uuid
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

import cursor_checksum
from token_pool import TokenPoolManager
from chat_lock_detector import QUOTA_EXHAUSTION_THRESHOLD


class TestCursorChecksum(unittest.TestCase):
    def setUp(self):
        cursor_checksum.reset_cached_machine_ids()

    def tearDown(self):
        cursor_checksum.reset_cached_machine_ids()

    def test_01_get_cursor_machine_ids_default_stable_fallback(self):
        """When storage.json does not exist, get_cursor_machine_ids returns stable UUID5 IDs."""
        with patch("os.getenv", return_value=None):
            with patch("os.path.exists", return_value=False):
                m_id, mac_id = cursor_checksum.get_cursor_machine_ids(force_refresh=True)
                self.assertTrue(len(m_id) > 0)
                self.assertTrue(len(mac_id) > 0)
                # Verify stability
                m_id2, mac_id2 = cursor_checksum.get_cursor_machine_ids(force_refresh=False)
                self.assertEqual(m_id, m_id2)
                self.assertEqual(mac_id, mac_id2)

    def test_02_get_cursor_machine_ids_reads_storage_json(self):
        """storage.json telemetry machine IDs are correctly extracted."""
        temp_dir = tempfile.mkdtemp()
        try:
            cursor_dir = os.path.join(temp_dir, "Cursor", "User", "globalStorage")
            os.makedirs(cursor_dir, exist_ok=True)
            storage_path = os.path.join(cursor_dir, "storage.json")

            sample_data = {
                "telemetry.machineId": "mock-machine-id-12345",
                "telemetry.macMachineId": "mock-mac-machine-id-67890",
                "telemetry.devDeviceId": "mock-dev-device-id"
            }
            with open(storage_path, "w", encoding="utf-8") as f:
                json.dump(sample_data, f)

            with patch("os.getenv", return_value=temp_dir):
                m_id, mac_id = cursor_checksum.get_cursor_machine_ids(force_refresh=True)
                self.assertEqual(m_id, "mock-machine-id-12345")
                self.assertEqual(mac_id, "mock-mac-machine-id-67890")
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_03_mtime_caching_and_force_refresh(self):
        """mtime change or force_refresh triggers reload of IDs."""
        temp_dir = tempfile.mkdtemp()
        try:
            cursor_dir = os.path.join(temp_dir, "Cursor", "User", "globalStorage")
            os.makedirs(cursor_dir, exist_ok=True)
            storage_path = os.path.join(cursor_dir, "storage.json")

            with open(storage_path, "w", encoding="utf-8") as f:
                json.dump({"telemetry.machineId": "initial-id", "telemetry.macMachineId": "initial-mac"}, f)

            with patch("os.getenv", return_value=temp_dir):
                m_id1, _ = cursor_checksum.get_cursor_machine_ids(force_refresh=True)
                self.assertEqual(m_id1, "initial-id")

                # Modify file on disk and update mtime
                time.sleep(0.05)
                with open(storage_path, "w", encoding="utf-8") as f:
                    json.dump({"telemetry.machineId": "updated-id", "telemetry.macMachineId": "updated-mac"}, f)

                # Without force_refresh, detected by mtime change
                m_id2, _ = cursor_checksum.get_cursor_machine_ids(force_refresh=False)
                self.assertEqual(m_id2, "updated-id")

                # Test manual reset
                cursor_checksum.reset_cached_machine_ids()
                m_id3, _ = cursor_checksum.get_cursor_machine_ids()
                self.assertEqual(m_id3, "updated-id")
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_04_generate_cursor_checksum_structure(self):
        """generate_cursor_checksum generates properly formatted and deterministic tokens."""
        m_id = "test-machine-id-abc"
        mac_id = "test-mac-id-def"
        fixed_time_ms = 1700000000000

        checksum = cursor_checksum.generate_cursor_checksum(
            machine_id=m_id,
            mac_machine_id=mac_id,
            timestamp_ms=fixed_time_ms
        )

        # Must end with machine_id/mac_machine_id
        suffix = f"{m_id}/{mac_id}"
        self.assertTrue(checksum.endswith(suffix))
        
        # Base64 prefix must exist
        b64_prefix = checksum[:-len(suffix)]
        self.assertTrue(len(b64_prefix) > 0)

        # Deterministic output for same inputs
        checksum2 = cursor_checksum.generate_cursor_checksum(
            machine_id=m_id,
            mac_machine_id=mac_id,
            timestamp_ms=fixed_time_ms
        )
        self.assertEqual(checksum, checksum2)

    def test_05_generate_cursor_checksum_without_mac_id(self):
        """Checksum formatting without mac_machine_id."""
        m_id = "solo-machine-id"
        checksum = cursor_checksum.generate_cursor_checksum(
            machine_id=m_id,
            mac_machine_id="",
            timestamp_ms=1700000000000
        )
        self.assertTrue(checksum.endswith(m_id))
        self.assertNotIn("/", checksum)


class TestTokenPoolManager(unittest.TestCase):
    def setUp(self):
        self.temp_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db_file.name
        self.temp_db_file.close()

        # Create schema
        con = sqlite3.connect(self.temp_db_path)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                access_token TEXT,
                refresh_token TEXT,
                usage_percent REAL DEFAULT 0.0,
                total_spend REAL DEFAULT 0.0,
                status TEXT DEFAULT 'READY',
                display_message TEXT,
                last_checked INTEGER
            )
        """)
        # Populate with test accounts
        accounts_data = [
            ("acc1@test.com", "token_1_valid_long_access_token_12345", 10.0, 5.0, "READY"),
            ("acc2@test.com", "token_2_valid_long_access_token_67890", 25.0, 15.0, "READY"),
            ("acc3@test.com", "token_3_valid_long_access_token_abcde", 45.0, 30.0, "HIGH_USAGE"),
            ("acc_exhausted@test.com", "token_exhausted_long_token_fffff", 80.0, 90.0, "EXHAUSTED"),
        ]
        cur.executemany("""
            INSERT INTO accounts (email, access_token, usage_percent, total_spend, status)
            VALUES (?, ?, ?, ?, ?)
        """, accounts_data)
        con.commit()
        con.close()

        self.pool = TokenPoolManager(db_path=self.temp_db_path)

    def tearDown(self):
        try:
            os.remove(self.temp_db_path)
        except Exception:
            pass

    def test_01_load_cache_and_fast_lookup(self):
        """Token pool loads ready accounts into cache and performs sub-millisecond retrieval."""
        count = self.pool.load_cache_from_db()
        # acc1, acc2, acc3 (< 50% threshold) should be loaded, acc_exhausted excluded
        self.assertEqual(count, 3)

        tok = self.pool.get_token_from_cache()
        self.assertIsNotNone(tok)
        self.assertIn("email", tok)
        self.assertIn(tok["email"], ["acc1@test.com", "acc2@test.com", "acc3@test.com"])
        self.assertIn("query_time_ms", tok)
        self.assertLess(tok["query_time_ms"], 10.0)  # Sub-millisecond in normal conditions

    def test_02_round_robin_fair_distribution(self):
        """Repeated cache queries rotate round-robin across available tokens."""
        self.pool.load_cache_from_db()
        t1 = self.pool.get_token_from_cache()
        t2 = self.pool.get_token_from_cache()
        t3 = self.pool.get_token_from_cache()
        t4 = self.pool.get_token_from_cache()

        # After 3 items, it should cycle back to the first
        self.assertNotEqual(t1["email"], t2["email"])
        self.assertNotEqual(t2["email"], t3["email"])
        self.assertEqual(t1["email"], t4["email"])

    def test_03_multi_token_exclusion(self):
        """Tokens provided in exclude_tokens (as str, set, or list) are skipped."""
        tok1 = self.pool.get_token_from_cache()
        excluded_token = tok1["access_token"]

        # String exclusion
        tok2 = self.pool.get_token_from_cache(exclude_tokens=excluded_token)
        self.assertNotEqual(tok2["access_token"], excluded_token)

        # Set exclusion
        tok3 = self.pool.get_token_from_cache(exclude_tokens={excluded_token, tok2["access_token"]})
        self.assertNotIn(tok3["access_token"], [excluded_token, tok2["access_token"]])

        # Exclude all
        tok_none = self.pool.get_token_from_cache(exclude_tokens=[
            "token_1_valid_long_access_token_12345",
            "token_2_valid_long_access_token_67890",
            "token_3_valid_long_access_token_abcde"
        ])
        self.assertIsNone(tok_none)

    def test_04_mark_rate_limited_in_memory_and_db(self):
        """mark_rate_limited updates in-memory status instantly and persists to database."""
        token_to_lock = "token_1_valid_long_access_token_12345"
        self.assertFalse(self.pool.is_token_rate_limited(token_to_lock))

        # Mark rate limited synchronously for immediate DB assert
        self.pool.mark_rate_limited(token_to_lock, reason="HTTP 429 Rate Limit", async_db=False)

        # In-memory check
        self.assertTrue(self.pool.is_token_rate_limited(token_to_lock))
        self.assertIn(token_to_lock, self.pool.get_rate_limited_tokens())

        # Cache lookup should now skip token_to_lock
        next_tok = self.pool.get_token_from_cache()
        self.assertNotEqual(next_tok["access_token"], token_to_lock)

        # Verify database record updated
        con = sqlite3.connect(self.temp_db_path)
        cur = con.cursor()
        cur.execute("SELECT status, usage_percent, display_message FROM accounts WHERE access_token = ?", (token_to_lock,))
        row = cur.fetchone()
        con.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "EXHAUSTED")
        self.assertEqual(row[1], 100.0)
        self.assertEqual(row[2], "HTTP 429 Rate Limit")

    def test_05_direct_db_lookup_and_exclusions(self):
        """get_token_from_db_direct queries database accurately with exclusion."""
        db_tok = self.pool.get_token_from_db_direct()
        self.assertIsNotNone(db_tok)
        self.assertEqual(db_tok["email"], "acc1@test.com")  # lowest usage (10.0%)

        # With exclusion
        db_tok2 = self.pool.get_token_from_db_direct(exclude_tokens=db_tok["access_token"])
        self.assertIsNotNone(db_tok2)
        self.assertEqual(db_tok2["email"], "acc2@test.com")  # next lowest usage (25.0%)

    def test_06_reset_rate_limits(self):
        """reset_rate_limits clears in-memory rate limited tokens and refreshes cache."""
        token = "token_2_valid_long_access_token_67890"
        self.pool.mark_rate_limited(token, async_db=False)
        self.assertTrue(self.pool.is_token_rate_limited(token))

        # Reset in-memory rate limits
        self.pool.reset_rate_limits()
        self.assertFalse(self.pool.is_token_rate_limited(token))

    def test_07_get_stats(self):
        """get_stats accurately reports cache and ready accounts."""
        stats = self.pool.get_stats()
        self.assertEqual(stats["cached_accounts"], 3)
        self.assertEqual(stats["ready_count"], 3)
        self.assertEqual(stats["rate_limited_count"], 0)
        self.assertEqual(stats["threshold"], QUOTA_EXHAUSTION_THRESHOLD)

        # Mark 1 rate limited
        self.pool.mark_rate_limited("token_1_valid_long_access_token_12345", async_db=False)
        stats2 = self.pool.get_stats()
        self.assertEqual(stats2["ready_count"], 2)
        self.assertEqual(stats2["rate_limited_count"], 1)

    def test_08_multithreaded_concurrency_stress(self):
        """Token pool handles high-concurrency requests across multiple threads without errors."""
        errors = []

        def worker():
            try:
                for _ in range(50):
                    tok = self.pool.get_token_from_cache()
                    if tok:
                        _ = tok["access_token"]
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Encountered thread errors: {errors}")

    def test_09_backward_compatibility_schema_without_total_spend(self):
        """Token pool falls back gracefully when total_spend column is not in accounts table."""
        legacy_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            con = sqlite3.connect(legacy_db)
            cur = con.cursor()
            cur.execute("""
                CREATE TABLE accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE,
                    access_token TEXT,
                    usage_percent REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'READY'
                )
            """)
            cur.execute("INSERT INTO accounts (email, access_token, usage_percent, status) VALUES ('legacy@test.com', 'legacy_token_123456789012345', 12.0, 'READY')")
            con.commit()
            con.close()

            legacy_pool = TokenPoolManager(db_path=legacy_db)
            loaded = legacy_pool.load_cache_from_db()
            self.assertEqual(loaded, 1)

            tok = legacy_pool.get_token_from_cache()
            self.assertIsNotNone(tok)
            self.assertEqual(tok["email"], "legacy@test.com")

            db_tok = legacy_pool.get_token_from_db_direct()
            self.assertIsNotNone(db_tok)
            self.assertEqual(db_tok["email"], "legacy@test.com")
        finally:
            try:
                os.remove(legacy_db)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
